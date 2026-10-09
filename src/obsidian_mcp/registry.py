"""Vault discovery and registration.

Only *registered* vaults are accessible. Discovery reads Obsidian's own vault list
(``obsidian.json`` in the app's config folder; an undocumented file read best-effort and
never written) and, when available, the CLI ``vaults verbose`` output, so the user can
pick a vault to connect.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import threading
from pathlib import Path
from typing import Any

from .config import ConfigStore, VaultEntry
from .errors import Code, ObsidianError
from .vault import Vault


def obsidian_json_paths() -> list[Path]:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return [home / "Library" / "Application Support" / "obsidian" / "obsidian.json"]
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "obsidian" / "obsidian.json"] if appdata else []
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [xdg / "obsidian" / "obsidian.json",
            home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
            home / "snap" / "obsidian" / "current" / ".config" / "obsidian" / "obsidian.json"]


def discover() -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in obsidian_json_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for vid, info in (data.get("vaults") or {}).items():
            p = info.get("path")
            if p and Path(p).is_dir():
                found[str(Path(p).resolve())] = {"obsidian_id": vid, "name": Path(p).name,
                                                 "path": str(Path(p).resolve()), "open": bool(info.get("open")),
                                                 "source": str(path)}
    return list(found.values())


def make_id(name: str, path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "vault"
    return f"{slug}-{hashlib.sha256(path.encode()).hexdigest()[:6]}"


class VaultRegistry:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self._vaults: dict[str, Vault] = {}
        self._lock = threading.RLock()

    @property
    def entries(self) -> list[VaultEntry]:
        return self.store.config.vaults

    def register(self, path: str, name: str | None = None, make_default: bool = False) -> VaultEntry:
        root = Path(path).expanduser()
        if not root.is_dir():
            raise ObsidianError(Code.VAULT_NOT_FOUND, f"Folder {path!r} does not exist.")
        root = root.resolve()
        with self._lock:
            for e in self.entries:
                if Path(e.path).resolve() == root:
                    if make_default:
                        self.store.update(default_vault=e.id)
                    return e
            name = name or root.name
            if any(e.name == name for e in self.entries):
                name = f"{name} ({root.parent.name})"
            entry = VaultEntry(make_id(name, str(root)), name, str(root))
            self.store.config.vaults.append(entry)
            if make_default or not self.store.config.default_vault:
                self.store.config.default_vault = entry.id
            self.store.save()
            return entry

    def unregister(self, ref: str) -> VaultEntry:
        with self._lock:
            entry = self.entry(ref)
            self.store.config.vaults = [e for e in self.entries if e.id != entry.id]
            if self.store.config.default_vault == entry.id:
                self.store.config.default_vault = self.entries[0].id if self.entries else None
            self.store.save()
            self._vaults.pop(entry.id, None)
            return entry

    def entry(self, ref: str | None) -> VaultEntry:
        if not self.entries:
            raise ObsidianError(Code.NO_VAULT, "No vault is connected yet.",
                                hint="Call list_vaults to see discovered vaults, then connect_vault.")
        if ref is None:
            ref = self.store.config.default_vault
            if ref is None:
                if len(self.entries) == 1:
                    return self.entries[0]
                raise ObsidianError(Code.NO_VAULT, "Several vaults are connected; say which one.",
                                    vaults=[e.name for e in self.entries])
        for e in self.entries:
            if ref in (e.id, e.name):
                return e
        lowered = ref.lower()
        matches = [e for e in self.entries if e.name.lower() == lowered]
        if len(matches) == 1:
            return matches[0]
        raise ObsidianError(Code.VAULT_NOT_FOUND, f"No connected vault named {ref!r}.",
                            hint="Call list_vaults.", vaults=[e.name for e in self.entries])

    def get(self, ref: str | None) -> Vault:
        entry = self.entry(ref)
        with self._lock:
            vault = self._vaults.get(entry.id)
            if vault is None:
                cfg = self.store.config
                vault = Vault(entry.id, entry.name, Path(entry.path), self.store.state_dir,
                              backups=cfg.backups_enabled, backups_keep=cfg.backups_keep)
                self._vaults[entry.id] = vault
            return vault
