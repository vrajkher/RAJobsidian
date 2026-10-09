"""Permission policy and confirmation tokens.

Risky capabilities are gated twice:

1. A permission flag in the user's config (off by default for anything destructive,
   public, credential-related, or code-executing). Flags for risky capabilities are
   deliberately *not* exposed through MCP settings tools, so a model cannot enable them;
   the user changes them with ``obsidian-mcp config set`` or by editing config.json.
2. A one-time confirmation token for irreversible or public actions: the first call
   returns a preview plus ``confirm_token``; only a second call with that token acts.

Note content is data. Nothing read from a vault can change these rules.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from enum import StrEnum
from typing import Any

from .config import Permissions
from .errors import Code, ObsidianError


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    TRASH = "trash"
    PERMANENT_DELETE = "permanent_delete"
    UI = "ui_control"
    SETTINGS = "settings_changes"
    PLUGINS = "plugin_management"
    PUBLISH = "publish"
    SYNC = "sync_control"
    DEV = "developer_tools"
    EVAL = "eval_code"
    HEADLESS = "headless"


# Risks that additionally need a confirmation token on every call.
CONFIRM_RISKS = {Risk.PERMANENT_DELETE, Risk.PUBLISH, Risk.EVAL, Risk.PLUGINS}

# Every command in data/cli_catalog.json must appear here (enforced by tests).
CLI_RISK: dict[str, Risk] = {
    # General
    "help": Risk.READ, "version": Risk.READ, "reload": Risk.UI, "restart": Risk.UI,
    # Bases
    "bases": Risk.READ, "base:views": Risk.READ, "base:create": Risk.WRITE, "base:query": Risk.READ,
    # Bookmarks
    "bookmarks": Risk.READ, "bookmark": Risk.WRITE,
    # Command palette
    "commands": Risk.READ, "command": Risk.UI, "hotkeys": Risk.READ, "hotkey": Risk.READ,
    # Daily notes
    "daily": Risk.UI, "daily:path": Risk.READ, "daily:read": Risk.READ,
    "daily:append": Risk.WRITE, "daily:prepend": Risk.WRITE,
    # File history
    "diff": Risk.READ, "history": Risk.READ, "history:list": Risk.READ, "history:read": Risk.READ,
    "history:restore": Risk.WRITE, "history:open": Risk.UI,
    # Files and folders
    "file": Risk.READ, "files": Risk.READ, "folder": Risk.READ, "folders": Risk.READ,
    "open": Risk.UI, "create": Risk.WRITE, "read": Risk.READ, "append": Risk.WRITE,
    "prepend": Risk.WRITE, "move": Risk.WRITE, "rename": Risk.WRITE, "delete": Risk.TRASH,
    # Links
    "backlinks": Risk.READ, "links": Risk.READ, "unresolved": Risk.READ, "orphans": Risk.READ,
    "deadends": Risk.READ,
    # Outline
    "outline": Risk.READ,
    # Plugins
    "plugins": Risk.READ, "plugins:enabled": Risk.READ, "plugins:restrict": Risk.PLUGINS,
    "plugin": Risk.READ, "plugin:enable": Risk.PLUGINS, "plugin:disable": Risk.PLUGINS,
    "plugin:install": Risk.PLUGINS, "plugin:uninstall": Risk.PLUGINS, "plugin:reload": Risk.DEV,
    # Properties
    "aliases": Risk.READ, "properties": Risk.READ, "property:set": Risk.WRITE,
    "property:remove": Risk.WRITE, "property:read": Risk.READ,
    # Publish
    "publish:site": Risk.READ, "publish:list": Risk.READ, "publish:status": Risk.READ,
    "publish:add": Risk.PUBLISH, "publish:remove": Risk.PUBLISH, "publish:open": Risk.UI,
    # Random notes
    "random": Risk.UI, "random:read": Risk.READ,
    # Search
    "search": Risk.READ, "search:context": Risk.READ, "search:open": Risk.UI,
    # Sync
    "sync": Risk.SYNC, "sync:status": Risk.READ, "sync:history": Risk.READ, "sync:read": Risk.READ,
    "sync:restore": Risk.WRITE, "sync:open": Risk.UI, "sync:deleted": Risk.READ,
    # Tags
    "tags": Risk.READ, "tag": Risk.READ,
    # Tasks
    "tasks": Risk.READ, "task": Risk.WRITE,
    # Templates
    "templates": Risk.READ, "template:read": Risk.READ, "template:insert": Risk.WRITE,
    # Themes and snippets
    "themes": Risk.READ, "theme": Risk.READ, "theme:set": Risk.SETTINGS,
    "theme:install": Risk.PLUGINS, "theme:uninstall": Risk.PLUGINS,
    "snippets": Risk.READ, "snippets:enabled": Risk.READ,
    "snippet:enable": Risk.SETTINGS, "snippet:disable": Risk.SETTINGS,
    # Unique notes
    "unique": Risk.WRITE,
    # Vault
    "vault": Risk.READ, "vaults": Risk.READ, "vault:open": Risk.UI,
    # Web viewer
    "web": Risk.UI,
    # Word count
    "wordcount": Risk.READ,
    # Workspace
    "workspace": Risk.READ, "workspaces": Risk.READ, "workspace:save": Risk.UI,
    "workspace:load": Risk.UI, "workspace:delete": Risk.UI, "tabs": Risk.READ,
    "tab:open": Risk.UI, "recents": Risk.READ,
    # Developer
    "devtools": Risk.DEV, "dev:debug": Risk.DEV, "dev:cdp": Risk.EVAL, "dev:errors": Risk.DEV,
    "dev:screenshot": Risk.DEV, "dev:console": Risk.DEV, "dev:css": Risk.DEV, "dev:dom": Risk.DEV,
    "dev:mobile": Risk.DEV, "eval": Risk.EVAL,
}

# Flag-dependent escalations: (command, flag) -> risk.
CLI_FLAG_RISK: dict[tuple[str, str], Risk] = {
    ("delete", "permanent"): Risk.PERMANENT_DELETE,
    ("create", "overwrite"): Risk.WRITE,
    ("sync", "on"): Risk.SYNC,
    ("sync", "off"): Risk.SYNC,
}

# Read-only commands that still need a permission because they reveal app internals.
CLI_READ_GATES: dict[str, Risk] = {}

# Commands that write when given a flag, otherwise read.
CLI_READ_UNLESS: dict[str, set[str]] = {
    "task": {"toggle", "done", "todo", "status"},
    "plugins:restrict": {"on", "off"},
    "sync": {"on", "off"},
}


def cli_risk(command: str, params: dict[str, Any], flags: list[str]) -> Risk:
    base = CLI_RISK.get(command)
    if base is None:
        raise ObsidianError(Code.UNSUPPORTED, f"CLI command {command!r} is not in the documented catalog.")
    for flag in flags:
        if (command, flag) in CLI_FLAG_RISK:
            return CLI_FLAG_RISK[(command, flag)]
    if command in CLI_READ_UNLESS:
        triggers = CLI_READ_UNLESS[command]
        if not (triggers & set(flags)) and not (triggers & set(params)):
            return Risk.READ
    return base


class Policy:
    def __init__(self, permissions: Permissions) -> None:
        self.permissions = permissions
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def allowed(self, risk: Risk) -> bool:
        if risk is Risk.READ:
            return True
        return bool(getattr(self.permissions, risk.value))

    scope_check: Any = None  # optional callable(risk, action) for OAuth scope enforcement

    def require(self, risk: Risk, action: str) -> None:
        if self.scope_check is not None:
            self.scope_check(risk, action)
        if not self.allowed(risk):
            raise ObsidianError(
                Code.PERMISSION_DENIED,
                f"{action} needs the '{risk.value}' permission, which is off.",
                hint=f"The vault owner can enable it with: obsidian-mcp config set {risk.value} true",
            )

    # Confirmation tokens -------------------------------------------------------------
    @staticmethod
    def fingerprint(action: str, args: dict[str, Any]) -> str:
        blob = json.dumps({"a": action, "args": args}, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def issue(self, action: str, args: dict[str, Any], ttl: float = 300.0) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            now = time.monotonic()
            self._tokens = {k: v for k, v in self._tokens.items() if v[1] > now}
            self._tokens[token] = (self.fingerprint(action, args), now + ttl)
        return token

    def consume(self, token: str | None, action: str, args: dict[str, Any]) -> bool:
        if not token:
            return False
        with self._lock:
            entry = self._tokens.pop(token, None)
        if entry is None or entry[1] < time.monotonic():
            raise ObsidianError(Code.CONFIRMATION_REQUIRED, "Confirmation token is unknown or expired.",
                                hint="Call again without confirm_token to get a fresh preview.")
        if entry[0] != self.fingerprint(action, args):
            raise ObsidianError(Code.CONFIRMATION_REQUIRED,
                                "Confirmation token was issued for different arguments.",
                                hint="Request a new preview for exactly these arguments.")
        return True

    def confirmation(self, action: str, args: dict[str, Any], summary: str,
                     preview: Any = None) -> dict[str, Any]:
        return {
            "status": "confirmation_required",
            "action": action,
            "summary": summary,
            "preview": preview,
            "confirm_token": self.issue(action, args),
            "next_step": "Show the summary to the user. If they agree, call the same tool again "
                         "with the same arguments plus confirm_token.",
        }
