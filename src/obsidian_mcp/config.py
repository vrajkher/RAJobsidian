"""Persistent configuration: registered vaults, permissions, adapter locations.

Stored as JSON in ``$OBSIDIAN_MCP_HOME/config.json`` (default ``~/.config/obsidian-mcp``),
written atomically with mode 0600 because it may contain the plugin-bridge token.
Secrets are never returned by ``public_dict``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

SECRET_FIELDS = {"bridge_token"}


def default_home() -> Path:
    env = os.environ.get("OBSIDIAN_MCP_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "obsidian-mcp"


@dataclass
class VaultEntry:
    id: str
    name: str
    path: str


@dataclass
class Permissions:
    """What the server may do. Defaults: read + safe edits on, everything risky off."""

    write: bool = True
    trash: bool = True
    permanent_delete: bool = False
    ui_control: bool = True
    settings_changes: bool = False
    plugin_management: bool = False
    publish: bool = False
    sync_control: bool = False
    developer_tools: bool = False
    eval_code: bool = False
    headless: bool = False
    community_plugins: bool = True


@dataclass
class Config:
    vaults: list[VaultEntry] = field(default_factory=list)
    default_vault: str | None = None
    permissions: Permissions = field(default_factory=Permissions)
    cli_path: str | None = None
    cli_timeout_seconds: float = 30.0
    bridge_url: str = "http://127.0.0.1:27125"
    bridge_token: str | None = None
    headless_path: str | None = None
    backups_enabled: bool = True
    backups_keep: int = 50
    max_read_bytes: int = 2_000_000
    page_size: int = 50
    semantic_search: bool = False
    embedding_url: str = "http://127.0.0.1:11434/v1"
    embedding_model: str = "nomic-embed-text"
    embedding_api_key_env: str | None = None
    embedding_allow_remote: bool = False
    embedding_exclude: list[str] = field(default_factory=list)
    oauth_issuer_url: str | None = None
    oauth_introspection_url: str | None = None
    oauth_resource_url: str | None = None
    oauth_client_id_env: str | None = None
    oauth_client_secret_env: str | None = None
    oauth_required_scopes: list[str] = field(default_factory=list)
    oauth_write_scope: str | None = None
    user_vaults: dict[str, list[str]] = field(default_factory=dict)
    admins: list[str] = field(default_factory=list)
    discover_obsidian_vaults: bool = True

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in SECRET_FIELDS:
            data[key] = "***" if data.get(key) else None
        return data


class ConfigStore:
    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or default_home()).expanduser()
        self.path = self.home / "config.json"
        self._lock = threading.RLock()
        self.config = self._load()

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    def _load(self) -> Config:
        if not self.path.exists():
            cfg = Config()
        else:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            perms = Permissions(**_known(Permissions, raw.pop("permissions", {}) or {}))
            vaults = [VaultEntry(**_known(VaultEntry, v)) for v in raw.pop("vaults", []) or []]
            cfg = Config(**_known(Config, raw), vaults=vaults, permissions=perms)
        if os.environ.get("OBSIDIAN_MCP_BRIDGE_TOKEN"):
            cfg.bridge_token = os.environ["OBSIDIAN_MCP_BRIDGE_TOKEN"]
        return cfg

    def save(self) -> None:
        with self._lock:
            self.home.mkdir(parents=True, exist_ok=True)
            data = json.dumps(asdict(self.config), indent=2, ensure_ascii=False)
            fd, tmp = tempfile.mkstemp(dir=self.home, prefix=".config.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def update(self, **changes: Any) -> Config:
        with self._lock:
            perm_names = {f.name for f in fields(Permissions)}
            for key, value in changes.items():
                if key in perm_names:
                    setattr(self.config.permissions, key, value)
                elif hasattr(self.config, key):
                    setattr(self.config, key, value)
                else:
                    raise KeyError(key)
            self.save()
            return self.config


def _known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}
