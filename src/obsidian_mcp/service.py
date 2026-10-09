"""ObsidianService: the single service layer behind every MCP tool.

Adapter choice is automatic and explained:

* filesystem  - always available for connected vaults; works with Obsidian closed.
* cli         - official Obsidian CLI (1.12.7+ installer, app running) for native features.
* bridge      - optional local plugin for editor/workspace/events and link-aware renames.
* headless    - optional ``ob`` client for Sync/Publish without the desktop app.

Content read from notes is returned as data only; it never changes permissions or tools.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import posixpath
import random
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from . import bases as bases_mod
from . import canvas as canvas_mod
from . import community, edits
from . import markdown as md
from . import templates as tpl
from .adapters import cli as cli_mod
from .adapters import headless as headless_mod
from .adapters.bridge import BridgeAdapter
from .config import ConfigStore
from .errors import Code, ObsidianError
from .index import VaultIndex
from .policy import CONFIRM_RISKS, Policy, Risk, cli_risk
from .progress import Progress, no_progress
from .registry import VaultRegistry, discover
from .search import RelatedNotes, duplicates, search
from .semantic import SemanticIndex
from .vault import TRASH_DIR, Vault, etag_of, file_kind, mime_of, normalize_rel, unified_diff

NOTE_EXT = ".md"


def _ensure_md(path: str) -> str:
    p = normalize_rel(path)
    if not posixpath.splitext(p)[1]:
        p += NOTE_EXT
    return p


class ObsidianService:
    def __init__(self, store: ConfigStore | None = None) -> None:
        self.store = store or ConfigStore()
        self.registry = VaultRegistry(self.store)
        self.policy = Policy(self.store.config.permissions)
        self._indexes: dict[str, VaultIndex] = {}
        self._related: dict[str, RelatedNotes] = {}
        self._semantic: dict[str, SemanticIndex] = {}
        self._lock = threading.RLock()
        self.listeners: list[Any] = []  # callables(vault_id, paths) for resource-updated notifications

    # Config/adapters ---------------------------------------------------------------------
    @property
    def config(self):
        return self.store.config

    @property
    def cli(self) -> cli_mod.CLIAdapter:
        return cli_mod.CLIAdapter(cli_mod.find_binary(self.config.cli_path), self.config.cli_timeout_seconds)

    @property
    def bridge(self) -> BridgeAdapter:
        return BridgeAdapter(self.config.bridge_url, self.config.bridge_token)

    @property
    def headless(self) -> headless_mod.HeadlessAdapter:
        return headless_mod.HeadlessAdapter(headless_mod.find_binary(self.config.headless_path))

    def vault(self, ref: str | None) -> Vault:
        return self.registry.get(ref)

    def index(self, vault: Vault) -> VaultIndex:
        with self._lock:
            idx = self._indexes.get(vault.id)
            if idx is None:
                idx = self._indexes[vault.id] = VaultIndex(vault)
            return idx

    def _changed(self, vault: Vault, *paths: str) -> None:
        self.index(vault).invalidate(*paths)
        for listener in list(self.listeners):
            try:
                listener(vault.id, [p for p in paths if p])
            except Exception:
                pass

    # Vaults -------------------------------------------------------------------------------
    def list_vaults(self, include_discovered: bool = True) -> dict[str, Any]:
        connected = [{"id": e.id, "name": e.name, "path": e.path,
                      "default": e.id == self.config.default_vault} for e in self.registry.entries]
        result: dict[str, Any] = {"connected": connected}
        if include_discovered and self.config.discover_obsidian_vaults:
            known = {c["path"] for c in connected}
            result["discovered"] = [d for d in discover() if d["path"] not in known]
        if not connected:
            result["next_step"] = "Call connect_vault with a vault folder path to start."
        return result

    def connect_vault(self, path: str, name: str | None = None, make_default: bool = False) -> dict[str, Any]:
        entry = self.registry.register(path, name, make_default)
        vault = self.vault(entry.id)
        is_obsidian = (vault.root / vault.config_dir).is_dir()
        return {"vault": {"id": entry.id, "name": entry.name, "path": entry.path},
                "obsidian_config_found": is_obsidian,
                "note": None if is_obsidian else "No .obsidian folder found: this folder has not been opened "
                                                 "as a vault in Obsidian yet. Files still work.",
                "capabilities": self.capabilities(entry.id)}

    def disconnect_vault(self, vault: str) -> dict[str, Any]:
        entry = self.registry.unregister(vault)
        self._indexes.pop(entry.id, None)
        return {"disconnected": entry.name, "files_untouched": True}

    def set_default_vault(self, vault: str) -> dict[str, Any]:
        entry = self.registry.entry(vault)
        self.store.update(default_vault=entry.id)
        return {"default_vault": entry.name}

    def vault_info(self, vault: str | None) -> dict[str, Any]:
        v = self.vault(vault)
        stats = self.index(v).stats()
        return {"id": v.id, "name": v.name, "path": str(v.root), "stats": stats,
                "core_plugins_enabled": v.read_config_json("core-plugins.json"),
                "daily_notes": tpl.daily_settings(v), "templates": tpl.templates_settings(v)}

    def capabilities(self, vault: str | None = None, probe: bool = False) -> dict[str, Any]:
        cli = self.cli
        bridge = self.bridge
        headless = self.headless
        caps: dict[str, Any] = {
            "filesystem": {"available": bool(self.registry.entries),
                           "detail": "Read/write vault files directly. Works with Obsidian closed."
                           if self.registry.entries else "Connect a vault first (connect_vault)."},
            "cli": {"available": cli.available, "binary": cli.binary,
                    "requires": "Obsidian 1.12.7+ installer, Settings → General → Command line interface, "
                                "app running (the first CLI call launches Obsidian)."},
            "bridge": {"configured": bridge.configured, "url": bridge.url,
                       "requires": "Install bridge-plugin/ into the vault and set bridge_token."},
            "headless": {"available": headless.available, "binary": headless.binary,
                         "requires": "Node.js 22+, npm install -g obsidian-headless, then 'ob login' yourself. "
                                     "Sync/Publish subscriptions required. Open beta."},
            "permissions": dict(self.config.permissions.__dict__),
            "semantic_search": {"enabled": self.config.semantic_search,
                                "endpoint": self.config.embedding_url, "model": self.config.embedding_model,
                                "remote_allowed": self.config.embedding_allow_remote,
                                "detail": "related_notes (local TF-IDF) always works offline. semantic_search uses "
                                          "your embedding endpoint (local by default) once you enable it."},
        }
        if probe:
            if cli.available:
                caps["cli"]["version"] = cli.version()
                caps["cli"]["reachable"] = caps["cli"]["version"] is not None
            status = bridge.status() if bridge.configured else None
            caps["bridge"]["reachable"] = status is not None
            caps["bridge"]["status"] = status
        if vault or self.registry.entries:
            try:
                v = self.vault(vault)
                caps["vault"] = {"name": v.name, "community_plugins": community.installed(v)}
            except ObsidianError:
                pass
        return caps

    # Files -------------------------------------------------------------------------------
    def list_files(self, vault: str | None, folder: str = "", recursive: bool = True,
                   kinds: list[str] | None = None, include_folders: bool = False, offset: int = 0,
                   limit: int | None = None) -> dict[str, Any]:
        v = self.vault(vault)
        items = v.list(folder, recursive=recursive, kinds=kinds, include_folders=include_folders)
        limit = limit or self.config.page_size
        page = items[offset: offset + limit]
        return {"folder": normalize_rel(folder) if folder else "", "total": len(items), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(items) else None,
                "items": [i.to_dict() for i in page]}

    def file_info(self, vault: str | None, path: str) -> dict[str, Any]:
        v = self.vault(vault)
        info = v.info(path).to_dict()
        if info["kind"] != "folder":
            info["etag"] = etag_of(v.read_bytes(path))
        return info

    def read_note(self, vault: str | None, path: str, start_line: int | None = None,
                  end_line: int | None = None, include_metadata: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        text, etag = v.read_text(path, max_bytes=self.config.max_read_bytes)
        lines = text.split("\n")
        result: dict[str, Any] = {"path": path, "etag": etag, "line_count": len(lines),
                                  "line_ending": "CRLF" if "\r\n" in text else "LF"}
        if start_line or end_line:
            s = max(1, start_line or 1)
            e = min(len(lines), end_line or len(lines))
            result.update(start_line=s, end_line=e, content="\n".join(lines[s - 1:e]))
        else:
            result["content"] = text
        if include_metadata and path.endswith(".md"):
            result["metadata"] = self.note_metadata(vault, path)
        return result

    def _resolve_note_path(self, v: Vault, path: str) -> str:
        """Accept 'Folder/Note', 'Folder/Note.md', or a bare note name like a wikilink."""
        norm = normalize_rel(path)
        if v.resolve(norm).exists():  # raises for paths escaping the vault
            return norm
        if v.exists(norm + NOTE_EXT):
            return norm + NOTE_EXT
        resolved = self.index(v).resolve(norm)
        if resolved:
            return resolved
        raise ObsidianError(Code.NOT_FOUND, f"No note matches {path!r}.",
                            hint="Use search_notes or list_files to find it.")

    def read_attachment(self, vault: str | None, path: str) -> dict[str, Any]:
        v = self.vault(vault)
        return v.read_blob(path, max_bytes=self.config.max_read_bytes * 5)

    def _write_guard(self) -> None:
        self.policy.require(Risk.WRITE, "Editing the vault")

    def _apply_text(self, v: Vault, path: str, new_text: str, *, old_text: str | None, if_match: str | None,
                    dry_run: bool, mode: str = "upsert", action: str = "write") -> dict[str, Any]:
        diff = unified_diff(old_text or "", new_text, path)
        if dry_run:
            return {"path": path, "dry_run": True, "changed": old_text != new_text, "diff": diff}
        if old_text == new_text:
            return {"path": path, "changed": False, "message": "No changes needed."}
        self._write_guard()
        result = v.write_text(path, new_text, if_match=if_match, mode=mode, action=action)
        self._changed(v, path)
        result.pop("change", None)
        result.update(changed=True, diff=diff)
        return result

    def write_note(self, vault: str | None, path: str, content: str, *, overwrite: bool = False,
                   if_match: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = _ensure_md(path)
        old = v.read_text(path)[0] if v.exists(path) else None
        if old is not None and not overwrite and if_match is None:
            raise ObsidianError(Code.ALREADY_EXISTS, f"{path!r} already exists.",
                                hint="Pass overwrite=true (ideally with if_match) or use patch_note/append_note.")
        return self._apply_text(v, path, content, old_text=old, if_match=if_match, dry_run=dry_run,
                                mode="upsert" if old is not None else "create", action="write_note")

    def append_note(self, vault: str | None, path: str, content: str, *, create: bool = True,
                    inline: bool = False, if_match: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = _ensure_md(path) if not v.exists(path) else normalize_rel(path)
        if not v.exists(path) and not create:
            raise ObsidianError(Code.NOT_FOUND, f"{path!r} does not exist.")
        old = v.read_text(path)[0] if v.exists(path) else ""
        return self._apply_text(v, path, edits.append_text(old, content, inline), old_text=old,
                                if_match=if_match, dry_run=dry_run, action="append")

    def prepend_note(self, vault: str | None, path: str, content: str, *, inline: bool = False,
                     if_match: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        old = v.read_text(path)[0]
        return self._apply_text(v, path, edits.prepend_text(old, content, inline), old_text=old,
                                if_match=if_match, dry_run=dry_run, action="prepend")

    def patch_note(self, vault: str | None, path: str, ops: list[dict[str, Any]], *, if_match: str | None = None,
                   dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        old, etag = v.read_text(path)
        if if_match and if_match != etag:
            raise ObsidianError(Code.CONFLICT, f"{path!r} changed since you read it.", current_etag=etag)
        new = edits.apply_patch(old, ops)
        return self._apply_text(v, path, new, old_text=old, if_match=etag, dry_run=dry_run, action="patch")

    def set_properties(self, vault: str | None, path: str, set_values: dict[str, Any] | None = None,
                       remove: list[str] | None = None, *, if_match: str | None = None,
                       dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        old, etag = v.read_text(path)
        if if_match and if_match != etag:
            raise ObsidianError(Code.CONFLICT, f"{path!r} changed since you read it.", current_etag=etag)
        new = md.update_frontmatter(old, set_values, remove)
        return self._apply_text(v, path, new, old_text=old, if_match=etag, dry_run=dry_run,
                                action="set_properties")

    def create_folder(self, vault: str | None, path: str) -> dict[str, Any]:
        self._write_guard()
        v = self.vault(vault)
        result = v.mkdir(path)
        self._changed(v, normalize_rel(path))
        return result

    def copy_file(self, vault: str | None, path: str, new_path: str, overwrite: bool = False) -> dict[str, Any]:
        self._write_guard()
        v = self.vault(vault)
        result = v.copy(path, new_path, overwrite=overwrite)
        self._changed(v, result["path"])
        result.pop("change", None)
        return result

    def move_file(self, vault: str | None, path: str, new_path: str, *, update_links: bool = True,
                  adapter: str = "auto", dry_run: bool = False) -> dict[str, Any]:
        """Move or rename a file/folder, keeping links working."""
        v = self.vault(vault)
        src, dst = normalize_rel(path), normalize_rel(new_path)
        if not v.exists(src):
            src = self._resolve_note_path(v, path)
        src_path = v.resolve(src)
        if src_path.is_file() and not posixpath.splitext(dst)[1]:
            dst += posixpath.splitext(src)[1]
        if v.exists(dst):
            raise ObsidianError(Code.ALREADY_EXISTS, f"{dst!r} already exists.")
        use_bridge = adapter == "bridge" or (adapter == "auto" and update_links and src_path.is_file()
                                             and self.bridge.configured and self.bridge.status() is not None)
        if adapter == "cli":
            if dry_run:
                return {"dry_run": True, "adapter": "cli", "from": src, "to": dst,
                        "note": "Obsidian updates links according to its 'Automatically update internal links' "
                                "setting; no preview is available."}
            self._write_guard()
            out = self.cli.run("move", {"path": src, "to": dst}, vault=v.name)
            self._changed(v, src, dst)
            return {"from": src, "to": dst, "adapter": "cli", "output": out["output"]}
        if use_bridge:
            if dry_run:
                return {"dry_run": True, "adapter": "bridge", "from": src, "to": dst,
                        "note": "Obsidian's FileManager.renameFile updates links per the user's settings."}
            self._write_guard()
            self.bridge.call("POST", "/file/rename", {"path": src, "newPath": dst})
            self._changed(v, src, dst)
            return {"from": src, "to": dst, "adapter": "bridge", "links": "updated by Obsidian"}
        # Filesystem: compute link updates before moving.
        idx = self.index(v)
        idx.refresh(force=True)
        if src_path.is_dir():
            prefix = src + "/"
            mapping = {p: dst + "/" + p[len(prefix):] for p in idx.entries if p.startswith(prefix)}
        else:
            mapping = {src: dst}
        updates = edits.plan_link_updates(idx, mapping) if update_links else {}
        previews = []
        for new_src, text in updates.items():
            old_src = next((o for o, n in mapping.items() if n == new_src), new_src)
            old_text = idx.entries[old_src].text or ""
            previews.append({"path": new_src, "diff": unified_diff(old_text, text, new_src)})
        if dry_run:
            return {"dry_run": True, "adapter": "filesystem", "from": src, "to": dst,
                    "files_moved": len(mapping), "link_updates": previews}
        self._write_guard()
        with v.lock:
            move = v.move(src, dst, record=False)
            changes = [move["change"]]
            for new_src, text in updates.items():
                res = v.write_text(new_src, text, record=False)
                changes.append(res["change"])
            op = v.oplog.record("move_with_links", changes)
        self._changed(v, src, dst, *updates.keys())
        return {"from": src, "to": dst, "adapter": "filesystem", "operation_id": op,
                "links_updated_in": sorted(updates), "files_moved": len(mapping)}

    def rename_file(self, vault: str | None, path: str, new_name: str, **kw: Any) -> dict[str, Any]:
        if "/" in new_name or "\\" in new_name:
            raise ObsidianError(Code.INVALID_ARGUMENT, "new_name must be a file name; use move_file for folders.")
        v = self.vault(vault)
        src = normalize_rel(path) if v.exists(path) else self._resolve_note_path(v, path)
        folder = posixpath.dirname(src)
        if not posixpath.splitext(new_name)[1] and v.resolve(src).is_file():
            new_name += posixpath.splitext(src)[1]
        return self.move_file(vault, src, posixpath.join(folder, new_name) if folder else new_name, **kw)

    def trash_file(self, vault: str | None, path: str, *, adapter: str = "auto") -> dict[str, Any]:
        self.policy.require(Risk.TRASH, "Moving files to trash")
        v = self.vault(vault)
        norm = normalize_rel(path) if v.exists(path) else self._resolve_note_path(v, path)
        backlinks = self.index(v).backlinks(norm) if norm.endswith(".md") else []
        if adapter == "bridge":
            self.bridge.call("POST", "/file/trash", {"path": norm})
            result: dict[str, Any] = {"path": norm, "adapter": "bridge",
                                      "detail": "Trashed according to Obsidian's 'Deleted files' setting."}
        else:
            result = v.trash(norm)
            result.pop("change", None)
            result["adapter"] = "filesystem"
            result["restore_with"] = {"tool": "restore_from_trash", "trash_path": result["trash_path"]}
        self._changed(v, norm)
        if backlinks:
            result["warning"] = f"{len(backlinks)} link(s) now point to a missing note."
            result["broken_links_from"] = sorted({b["source"] for b in backlinks})
        return result

    def list_trash(self, vault: str | None) -> dict[str, Any]:
        v = self.vault(vault)
        return {"trash_folder": TRASH_DIR, "items": v.list_trash(),
                "note": "Only Obsidian's vault trash (.trash) is listed. System-trash items and File recovery "
                        "snapshots are not visible here; use file_history (CLI) for snapshots."}

    def restore_from_trash(self, vault: str | None, trash_path: str, to: str | None = None) -> dict[str, Any]:
        self._write_guard()
        v = self.vault(vault)
        result = v.restore(trash_path, to)
        self._changed(v, result["restored"])
        return result

    def delete_permanently(self, vault: str | None, path: str, confirm_token: str | None = None) -> dict[str, Any]:
        self.policy.require(Risk.PERMANENT_DELETE, "Permanent deletion")
        v = self.vault(vault)
        norm = normalize_rel(path)
        args = {"vault": v.id, "path": norm}
        if not self.policy.consume(confirm_token, "delete_permanently", args):
            target = v.resolve(norm, allow_hidden=norm.startswith(TRASH_DIR + "/"))
            if not target.exists():
                raise ObsidianError(Code.NOT_FOUND, f"{norm!r} does not exist.")
            return self.policy.confirmation("delete_permanently", args,
                                            f"Permanently delete {norm!r}. It will not go to trash. "
                                            "A backup copy is kept outside the vault for rollback.")
        result = v.delete_permanently(norm)
        self._changed(v, norm)
        return result

    def write_attachment(self, vault: str | None, path: str, base64_data: str, *, overwrite: bool = False,
                         if_match: str | None = None) -> dict[str, Any]:
        self._write_guard()
        v = self.vault(vault)
        try:
            data = base64.b64decode(base64_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ObsidianError(Code.INVALID_ARGUMENT, "base64_data is not valid base64.") from exc
        norm = normalize_rel(path)
        result = v.write_bytes(norm, data, if_match=if_match, mode="upsert" if overwrite else "create",
                               action="write_attachment")
        result.pop("change", None)
        result.update(mime=mime_of(norm), kind=file_kind(norm), size=len(data),
                      embed=f"![[{posixpath.basename(norm)}]]")
        self._changed(v, norm)
        return result

    def attachment_folder(self, vault: str | None, source_note: str | None = None) -> dict[str, Any]:
        """Where Obsidian would save new attachments ('Default location for new attachments')."""
        v = self.vault(vault)
        app = v.read_config_json("app.json") or {}
        setting = app.get("attachmentFolderPath", "/")
        if setting in ("/", "", None):
            folder = ""
        elif setting == "./" or setting.startswith("./"):
            base = posixpath.dirname(normalize_rel(source_note)) if source_note else ""
            folder = posixpath.normpath(posixpath.join(base, setting[2:])) if setting[2:] else base
            folder = "" if folder == "." else folder
        else:
            folder = setting.strip("/")
        return {"folder": folder, "setting": setting, "source": "app.json" if app else "default (vault root)"}

    def batch(self, vault: str | None, operations: list[dict[str, Any]], *, dry_run: bool = True,
              atomic: bool = True, progress: Progress = no_progress,
              cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        """Run several edits. With atomic=true, a failure rolls back the earlier items."""
        handlers = {
            "write_note": self.write_note, "append_note": self.append_note, "prepend_note": self.prepend_note,
            "patch_note": self.patch_note, "set_properties": self.set_properties,
            "move_file": self.move_file, "rename_file": self.rename_file, "copy_file": self.copy_file,
            "create_folder": self.create_folder, "trash_file": self.trash_file,
        }
        results: list[dict[str, Any]] = []
        done_ops: list[str] = []
        v = self.vault(vault)
        total = len(operations)
        for i, op in enumerate(operations):
            if cancelled():
                rolled = [v.rollback(o, force=True) for o in reversed(done_ops)] if atomic and not dry_run else []
                return {"status": "cancelled", "completed": i, "results": results,
                        "rolled_back": [r["rolled_back"] for r in rolled]}
            progress(i, total, f"{op.get('action', '?')} {op.get('path', '')}".strip())
            op = dict(op)
            name = op.pop("action", None)
            handler = handlers.get(name or "")
            if handler is None:
                results.append({"index": i, "status": "error", "error": f"Unknown action {name!r}.",
                                "allowed": sorted(handlers)})
                if atomic:
                    break
                continue
            if "set" in op and name == "set_properties":
                op["set_values"] = op.pop("set")
            try:
                if dry_run and name in ("create_folder", "copy_file", "trash_file"):
                    out: dict[str, Any] = {"dry_run": True, "would": name, **op}
                elif dry_run:
                    out = handler(v.id, **op, dry_run=True)
                else:
                    out = handler(v.id, **op)
                results.append({"index": i, "action": name, "status": "ok", "result": out})
                if out.get("operation_id"):
                    done_ops.append(out["operation_id"])
            except ObsidianError as exc:
                results.append({"index": i, "action": name, "status": "error", "error": exc.to_dict()})
                if atomic:
                    rolled = [v.rollback(o, force=True) for o in reversed(done_ops)] if not dry_run else []
                    return {"status": "failed", "failed_index": i, "results": results,
                            "rolled_back": [r["rolled_back"] for r in rolled]}
            except TypeError as exc:
                results.append({"index": i, "action": name, "status": "error",
                                "error": {"code": "INVALID_ARGUMENT", "message": str(exc)}})
                if atomic:
                    rolled = [v.rollback(o, force=True) for o in reversed(done_ops)] if not dry_run else []
                    return {"status": "failed", "failed_index": i, "results": results,
                            "rolled_back": [r["rolled_back"] for r in rolled]}
        progress(total, total, "done")
        ok = all(r["status"] == "ok" for r in results)
        return {"status": "ok" if ok else "partial", "dry_run": dry_run, "results": results,
                "operation_ids": done_ops}

    def operations(self, vault: str | None, limit: int = 20) -> dict[str, Any]:
        v = self.vault(vault)
        return {"operations": v.oplog.entries(limit)}

    def rollback(self, vault: str | None, operation_id: str, force: bool = False) -> dict[str, Any]:
        self._write_guard()
        v = self.vault(vault)
        result = v.rollback(operation_id, force=force)
        self.index(v).invalidate()
        return result

    def backups(self, vault: str | None, path: str | None = None) -> dict[str, Any]:
        v = self.vault(vault)
        return {"backups": v.backups.list(normalize_rel(path) if path else None)}

    def restore_backup(self, vault: str | None, backup_id: str, to: str | None = None,
                       dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        meta, data = v.backups.load(backup_id)
        target = normalize_rel(to) if to else meta["path"]
        old = v.read_text(target)[0] if v.exists(target) else ""
        try:
            new = data.decode("utf-8")
        except UnicodeDecodeError:
            if dry_run:
                return {"dry_run": True, "path": target, "binary": True, "size": len(data)}
            self._write_guard()
            res = v.write_bytes(target, data, action="restore_backup")
            self._changed(v, target)
            res.pop("change", None)
            return res
        return self._apply_text(v, target, new, old_text=old, if_match=None, dry_run=dry_run,
                                action="restore_backup")

    # Note metadata & knowledge -------------------------------------------------------------
    def note_metadata(self, vault: str | None, path: str) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        entry = self.index(v).get(path)
        if entry is None or entry.note is None:
            raise ObsidianError(Code.UNSUPPORTED, f"{path!r} is not a parseable Markdown note.")
        n = entry.note
        return {"path": path, "frontmatter": md.to_plain(n.frontmatter), "frontmatter_error": n.frontmatter_error,
                "tags": n.tags, "aliases": n.aliases,
                "headings": [{"level": h.level, "text": h.text, "line": h.line + 1} for h in n.headings],
                "block_ids": {k: v_ + 1 for k, v_ in n.block_ids.items()},
                "links": self.index(v).outgoing(path),
                "tasks": [{"line": t.line + 1, "status": t.status, "text": t.text} for t in n.tasks],
                "footnotes": {"references": n.footnote_refs, "definitions": n.footnote_defs,
                              "inline": n.inline_footnotes},
                "callouts": [{"type": c.type, "fold": c.fold, "title": c.title, "line": c.line + 1}
                             for c in n.callouts],
                "code_blocks": n.code_blocks, "has_math": n.has_math,
                "word_count": n.word_count, "character_count": n.char_count}

    def outline(self, vault: str | None, path: str) -> dict[str, Any]:
        meta = self.note_metadata(vault, path)
        return {"path": meta["path"], "headings": meta["headings"]}

    def search_notes(self, vault: str | None, query: str = "", **kw: Any) -> dict[str, Any]:
        v = self.vault(vault)
        kw.setdefault("limit", self.config.page_size)
        return search(self.index(v), query, **kw)

    def backlinks(self, vault: str | None, path: str, include_unlinked: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        out: dict[str, Any] = {"path": path, "backlinks": self.index(v).backlinks(path)}
        if include_unlinked:
            out["unlinked_mentions"] = self.index(v).unlinked_mentions(path)
        return out

    def outgoing_links(self, vault: str | None, path: str) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        return {"path": path, "links": self.index(v).outgoing(path)}

    def link_report(self, vault: str | None, kind: str, limit: int = 200) -> dict[str, Any]:
        v = self.vault(vault)
        idx = self.index(v)
        if kind == "unresolved":
            data = idx.unresolved()
            items = [{"target": t, "count": len(s), "sources": s[:20]} for t, s in
                     sorted(data.items(), key=lambda kv: -len(kv[1]))]
        elif kind == "orphans":
            items = idx.orphans()
        elif kind == "deadends":
            items = idx.deadends()
        else:
            raise ObsidianError(Code.INVALID_ARGUMENT, "kind must be unresolved, orphans, or deadends.")
        return {"kind": kind, "total": len(items), "items": items[:limit], "truncated": len(items) > limit}

    def graph(self, vault: str | None, **kw: Any) -> dict[str, Any]:
        return self.index(self.vault(vault)).graph(**kw)

    def tags(self, vault: str | None, tree: bool = False) -> dict[str, Any]:
        idx = self.index(self.vault(vault))
        return {"tags": idx.tag_tree() if tree else idx.tags()}

    def properties(self, vault: str | None) -> dict[str, Any]:
        return {"properties": self.index(self.vault(vault)).properties()}

    def related_notes(self, vault: str | None, path: str, limit: int = 10) -> dict[str, Any]:
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        rel = self._related.setdefault(v.id, RelatedNotes(self.index(v)))
        return {"path": path, "method": "local TF-IDF (no data leaves this machine)",
                "related": rel.related(path, limit)}

    def semantic_search(self, vault: str | None, query: str, *, limit: int = 10, folder: str | None = None,
                        progress: Progress = no_progress,
                        cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        """Search by meaning through the user's embedding endpoint (opt-in; local by default)."""
        v = self.vault(vault)
        with self._lock:
            sem = self._semantic.get(v.id)
            if sem is None or sem.config is not self.config:
                sem = self._semantic[v.id] = SemanticIndex(self.index(v), self.config, self.store.state_dir)
        return sem.search(query, limit=limit, folder=folder, progress=progress, cancelled=cancelled)

    def find_duplicates(self, vault: str | None, threshold: float = 0.8) -> dict[str, Any]:
        return duplicates(self.index(self.vault(vault)), threshold)

    def link_notes(self, vault: str | None, source: str, target: str, *, heading: str | None = None,
                   text: str | None = None, embed: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Add a wikilink from source to target (at the end, or under a heading)."""
        v = self.vault(vault)
        source = self._resolve_note_path(v, source)
        target = self._resolve_note_path(v, target)
        idx = self.index(v)
        link = md.Link(target=target, subpath=None, alias=None, embed=embed, kind="wikilink", line=0,
                       start=0, end=0, raw="")
        rendered = edits.format_link(link, target, source, idx)
        line = f"{text} {rendered}".strip() if text else rendered
        ops = [{"op": "insert", "heading": heading, "content": line, "position": "end"}] if heading else \
            [{"op": "append", "content": line}]
        return self.patch_note(vault, source, ops, dry_run=dry_run)

    def repair_links(self, vault: str | None, fixes: dict[str, str] | None = None, *,
                     dry_run: bool = True) -> dict[str, Any]:
        """Suggest targets for unresolved links, or apply {broken_target: existing_path} fixes."""
        v = self.vault(vault)
        idx = self.index(v)
        unresolved = idx.unresolved()
        names = {posixpath.basename(p).rsplit(".", 1)[0]: p for p in idx.entries}
        if not fixes:
            suggestions = []
            for target, sources in unresolved.items():
                base = posixpath.basename(target).rsplit(".md", 1)[0]
                close = difflib.get_close_matches(base, list(names), n=3, cutoff=0.6)
                suggestions.append({"target": target, "sources": sorted({s["source"] for s in sources}),
                                    "candidates": [names[c] for c in close]})
            return {"unresolved": len(unresolved), "suggestions": suggestions,
                    "next_step": "Call repair_links with fixes={broken_target: path} to apply."}
        changes = []
        for broken, new_path in fixes.items():
            new_path = self._resolve_note_path(v, new_path)
            for src in sorted({s["source"] for s in unresolved.get(broken, [])}):
                entry = idx.entries[src]
                text = entry.text or ""
                for link in sorted(entry.note.links, key=lambda lk: lk.start, reverse=True):  # type: ignore[union-attr]
                    if link.target == broken and link.start >= 0:
                        text = text[:link.start] + edits.format_link(link, new_path, src, idx) + text[link.end:]
                changes.append((src, entry.text or "", text))
        previews = [{"path": p, "diff": unified_diff(o, n, p)} for p, o, n in changes]
        if dry_run:
            return {"dry_run": True, "changes": previews}
        self._write_guard()
        for p, _, new in changes:
            v.write_text(p, new, action="repair_links")
        self._changed(v, *[p for p, _, _ in changes])
        return {"updated": [p for p, _, _ in changes], "changes": previews}

    def merge_notes(self, vault: str | None, source: str, target: str, *, position: str = "end",
                    dry_run: bool = True) -> dict[str, Any]:
        """Note composer 'Merge': add source to target, retarget links, trash source."""
        v = self.vault(vault)
        source = self._resolve_note_path(v, source)
        target = self._resolve_note_path(v, target)
        if source == target:
            raise ObsidianError(Code.INVALID_ARGUMENT, "Source and target are the same note.")
        idx = self.index(v)
        src_text, _ = v.read_text(source)
        tgt_text, tgt_etag = v.read_text(target)
        merged = edits.merge_text(tgt_text, src_text, position)
        relinks = edits.retarget_links(idx, source, target)
        relinks.pop(target, None)
        preview = {"target_diff": unified_diff(tgt_text, merged, target),
                   "link_updates": [{"path": p, "diff": unified_diff(idx.entries[p].text or "", t, p)}
                                    for p, t in relinks.items()],
                   "source_after": "moved to .trash"}
        if dry_run:
            return {"dry_run": True, **preview}
        self._write_guard()
        self.policy.require(Risk.TRASH, "Merging (the source note is trashed)")
        with v.lock:
            changes = [v.write_text(target, merged, if_match=tgt_etag, record=False)["change"]]
            for p, t in relinks.items():
                changes.append(v.write_text(p, t, record=False)["change"])
            changes.append(v.trash(source, record=False)["change"])
            op = v.oplog.record("merge_notes", changes)
        self._changed(v, source, target, *relinks)
        return {"merged_into": target, "source_trashed": source, "operation_id": op,
                "links_updated_in": sorted(relinks)}

    def extract_note(self, vault: str | None, source: str, new_path: str, *, heading: str | None = None,
                     start_line: int | None = None, end_line: int | None = None, expected: str | None = None,
                     replace_with: str = "link", position: str = "end", dry_run: bool = True) -> dict[str, Any]:
        """Note composer 'Extract': move part of a note into another note.

        replace_with: link | embed | none (Note composer's 'Text after extraction' options).
        If new_path exists the text is added at its start/end; otherwise a new note is created.
        """
        v = self.vault(vault)
        source = self._resolve_note_path(v, source)
        dest = _ensure_md(new_path)
        src_text, src_etag = v.read_text(source)
        extracted, s, e = edits.extract_range(src_text, heading=heading, start_line=start_line,
                                              end_line=end_line, expected=expected)
        dest_exists = v.exists(dest)
        dest_old = v.read_text(dest)[0] if dest_exists else ""
        if dest_exists:
            dest_new = edits.prepend_text(dest_old, extracted + "\n") if position == "start" else \
                edits.append_text(dest_old, extracted)
        else:
            dest_new = extracted.rstrip("\n") + "\n"
        name = posixpath.basename(dest)[:-3]
        replacement = {"link": f"[[{name}]]", "embed": f"![[{name}]]", "none": ""}.get(replace_with)
        if replacement is None:
            raise ObsidianError(Code.INVALID_ARGUMENT, "replace_with must be link, embed, or none.")
        src_new = edits.replace_lines(src_text, s, e, replacement)
        preview = {"source_diff": unified_diff(src_text, src_new, source),
                   "destination_diff": unified_diff(dest_old, dest_new, dest), "destination_created": not dest_exists}
        if dry_run:
            return {"dry_run": True, **preview}
        self._write_guard()
        with v.lock:
            changes = [v.write_text(dest, dest_new, mode="upsert", record=False)["change"],
                       v.write_text(source, src_new, if_match=src_etag, record=False)["change"]]
            op = v.oplog.record("extract_note", changes)
        self._changed(v, source, dest)
        return {"source": source, "destination": dest, "operation_id": op, **preview}

    # Tasks ---------------------------------------------------------------------------------
    def list_tasks(self, vault: str | None, *, path: str | None = None, folder: str | None = None,
                   status: str | None = None, done: bool | None = None, include_metadata: bool = True,
                   offset: int = 0, limit: int = 100) -> dict[str, Any]:
        v = self.vault(vault)
        idx = self.index(v)
        target = self._resolve_note_path(v, path) if path else None
        items = []
        for entry in sorted(idx.notes(), key=lambda e: e.info.path):
            p = entry.info.path
            if target and p != target or folder and not p.startswith(folder.strip("/") + "/"):
                continue
            for t in entry.note.tasks:  # type: ignore[union-attr]
                if status is not None and t.status != status:
                    continue
                if done is not None and (t.status != " ") != done:
                    continue
                item = {"path": p, "line": t.line + 1, "status": t.status, "text": t.text,
                        "ref": f"{p}:{t.line + 1}"}
                if include_metadata:
                    meta = community.tasks_metadata(t.text)
                    if meta:
                        item["tasks_plugin"] = meta
                items.append(item)
        return {"total": len(items), "offset": offset, "items": items[offset: offset + limit],
                "next_offset": offset + limit if offset + limit < len(items) else None}

    def set_task_status(self, vault: str | None, path: str, line: int, status: str = "x", *,
                        expected_text: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        if len(status) != 1:
            raise ObsidianError(Code.INVALID_ARGUMENT, "status must be one character, e.g. 'x', ' ', '-', '/'.")
        v = self.vault(vault)
        path = self._resolve_note_path(v, path)
        text, etag = v.read_text(path)
        lines = text.split("\n")
        if not 1 <= line <= len(lines):
            raise ObsidianError(Code.INVALID_ARGUMENT, f"Line {line} is outside the note.")
        raw = lines[line - 1]
        m = md.TASK_RE.match(raw.rstrip("\r"))
        if not m:
            raise ObsidianError(Code.PATCH_FAILED, f"Line {line} is not a task: {raw.strip()[:80]!r}")
        if expected_text is not None and expected_text.strip() not in m.group(4):
            raise ObsidianError(Code.CONFLICT, "The task text on that line changed.", current=m.group(4))
        bracket = raw.index("[", len(m.group(1)) + len(m.group(2)))
        lines[line - 1] = raw[:bracket + 1] + status + raw[bracket + 2:]
        return self._apply_text(v, path, "\n".join(lines), old_text=text, if_match=etag, dry_run=dry_run,
                                action="set_task_status")

    # Templates, daily & unique notes ----------------------------------------------------------
    def list_templates(self, vault: str | None) -> dict[str, Any]:
        v = self.vault(vault)
        settings = tpl.templates_settings(v)
        folder = settings["folder"]
        items = [i.path for i in v.list(folder, kinds=["note"])] if folder and v.exists(folder) else []
        templater = community.plugin_settings(v, "templater-obsidian")
        return {"settings": settings, "templates": items,
                "templater_folder": (templater or {}).get("templates_folder"),
                "note": None if folder else "No Templates folder configured; pass a template path directly."}

    def render_template(self, vault: str | None, template: str, title: str = "",
                        date: str | None = None) -> dict[str, Any]:
        v = self.vault(vault)
        path = tpl.template_path(v, template) if not v.exists(template) else normalize_rel(template)
        text, _ = v.read_text(path)
        s = tpl.templates_settings(v)
        now = datetime.fromisoformat(date) if date else datetime.now()
        rendered, unknown = tpl.render(text, title=title, now=now, date_format=s["date_format"],
                                       time_format=s["time_format"])
        return {"template": path, "content": rendered, "unresolved_variables": unknown}

    def create_from_template(self, vault: str | None, path: str, template: str, *,
                             dry_run: bool = False) -> dict[str, Any]:
        path = _ensure_md(path)
        rendered = self.render_template(vault, template, title=posixpath.basename(path)[:-3])
        out = self.write_note(vault, path, rendered["content"], dry_run=dry_run)
        out["unresolved_variables"] = rendered["unresolved_variables"]
        return out

    def daily_note(self, vault: str | None, date: str | None = None, *, create: bool = True,
                   offset_days: int = 0) -> dict[str, Any]:
        v = self.vault(vault)
        day = (datetime.fromisoformat(date) if date else datetime.now()) + timedelta(days=offset_days)
        path = tpl.daily_path(v, day)
        settings = tpl.daily_settings(v)
        if v.exists(path):
            return {**self.read_note(vault, path), "created": False, "settings": settings}
        if not create:
            return {"path": path, "exists": False, "settings": settings}
        content, unknown = "", []
        if settings["template"]:
            try:
                tpath = tpl.template_path(v, settings["template"])
                t = tpl.templates_settings(v)
                content, unknown = tpl.render(v.read_text(tpath)[0], title=posixpath.basename(path)[:-3],
                                              now=day, date_format=t["date_format"], time_format=t["time_format"])
            except ObsidianError:
                unknown = [f"template {settings['template']!r} not found"]
        res = self.write_note(vault, path, content)
        return {"path": path, "created": True, "etag": res.get("etag"), "content": content,
                "settings": settings, "unresolved_variables": unknown}

    def unique_note(self, vault: str | None, content: str = "", title: str | None = None) -> dict[str, Any]:
        v = self.vault(vault)
        s = tpl.unique_settings(v)
        prefix = tpl.moment_format(datetime.now(), s["format"])
        name = f"{prefix} {title}".strip() if title else prefix
        path = f"{s['folder']}/{name}.md" if s["folder"] else f"{name}.md"
        n = 1
        while v.exists(path):
            n += 1
            path = path[:-3] + f" {n}.md"
        if not content and s["template"]:
            try:
                content = tpl.render(v.read_text(tpl.template_path(v, s["template"]))[0], title=name)[0]
            except ObsidianError:
                pass
        res = self.write_note(vault, path, content)
        return {"path": path, "etag": res.get("etag"), "settings": s}

    def periodic_note(self, vault: str | None, period: str, date: str | None = None) -> dict[str, Any]:
        if period not in community.PERIOD_DEFAULTS:
            raise ObsidianError(Code.INVALID_ARGUMENT, f"period must be one of {list(community.PERIOD_DEFAULTS)}.")
        v = self.vault(vault)
        info = community.periodic_path(v, period, datetime.fromisoformat(date) if date else datetime.now())
        info["exists"] = v.exists(info["path"])
        return info

    def random_note(self, vault: str | None, folder: str | None = None) -> dict[str, Any]:
        v = self.vault(vault)
        notes = [i.path for i in v.list(folder or "", kinds=["note"])]
        if not notes:
            raise ObsidianError(Code.NOT_FOUND, "No notes found.")
        return self.read_note(vault, random.choice(notes))

    def word_count(self, vault: str | None, path: str | None = None, folder: str | None = None) -> dict[str, Any]:
        v = self.vault(vault)
        idx = self.index(v)
        if path:
            p = self._resolve_note_path(v, path)
            e = idx.get(p)
            return {"path": p, "words": e.note.word_count if e and e.note else 0,
                    "characters": e.note.char_count if e and e.note else 0,
                    "method": "local approximation; use cli wordcount for Obsidian's exact count"}
        notes = [e for e in idx.notes() if not folder or e.info.path.startswith(folder.strip("/") + "/")]
        return {"notes": len(notes), "words": sum(e.note.word_count for e in notes),  # type: ignore[union-attr]
                "characters": sum(e.note.char_count for e in notes)}  # type: ignore[union-attr]

    def slides(self, vault: str | None, path: str) -> dict[str, Any]:
        """Slides core plugin data: slides are separated by '---' lines in the note body."""
        note = self.read_note(vault, path)
        _, off = md.split_frontmatter(note["content"])
        body = note["content"][off:]
        masked = md.mask_non_content(body)
        parts, last = [], 0
        for m in re.finditer(r"^---[ \t]*\r?$", masked, re.M):
            parts.append(body[last:m.start()].strip())
            last = m.end()
        parts.append(body[last:].strip())
        return {"path": note["path"], "slide_count": len(parts), "slides": parts,
                "present": "Use run_command with the Slides command (find it via list_commands)."}

    # Canvas & Bases -------------------------------------------------------------------------
    def read_canvas(self, vault: str | None, path: str) -> dict[str, Any]:
        v = self.vault(vault)
        text, etag = v.read_text(normalize_rel(path), max_bytes=self.config.max_read_bytes)
        data = canvas_mod.load(text)
        return {"path": normalize_rel(path), "etag": etag, "canvas": data, "summary": canvas_mod.summarize(data),
                "problems": canvas_mod.validate(data)}

    def write_canvas(self, vault: str | None, path: str, canvas: dict[str, Any] | None = None,
                     ops: list[dict[str, Any]] | None = None, *, if_match: str | None = None,
                     dry_run: bool = False, create: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = normalize_rel(path)
        if not path.endswith(".canvas"):
            path += ".canvas"
        exists = v.exists(path)
        old = v.read_text(path)[0] if exists else ""
        if create and exists:
            raise ObsidianError(Code.ALREADY_EXISTS, f"{path!r} already exists.")
        if canvas is not None:
            data = canvas
            canvas_mod.require_valid(data)
            results: list[dict[str, Any]] = []
        else:
            data, results = canvas_mod.apply_ops(canvas_mod.load(old), ops or [])
        out = self._apply_text(v, path, canvas_mod.dump(data), old_text=old if exists else None,
                               if_match=if_match, dry_run=dry_run, mode="upsert", action="canvas")
        out["ops"] = results
        out["summary"] = canvas_mod.summarize(data)
        return out

    def read_base(self, vault: str | None, path: str) -> dict[str, Any]:
        v = self.vault(vault)
        text, etag = v.read_text(normalize_rel(path))
        return {"path": normalize_rel(path), "etag": etag, "yaml": text,
                "definition": bases_mod.describe(bases_mod.load(text)),
                "query": "Results require Obsidian's engine: call query_base (CLI base:query)."}

    def write_base(self, vault: str | None, path: str, *, yaml_text: str | None = None,
                   ops: list[dict[str, Any]] | None = None, if_match: str | None = None,
                   dry_run: bool = False) -> dict[str, Any]:
        v = self.vault(vault)
        path = normalize_rel(path)
        if not path.endswith(".base"):
            path += ".base"
        exists = v.exists(path)
        old = v.read_text(path)[0] if exists else ""
        if yaml_text is not None:
            data = bases_mod.load(yaml_text)
            check = bases_mod.validate(data)
            if check["errors"]:
                raise ObsidianError(Code.INVALID_ARGUMENT, "Base is invalid: " + "; ".join(check["errors"]))
            new = yaml_text
        else:
            data = bases_mod.apply_ops(bases_mod.load(old), ops or [])
            new = bases_mod.dump(data)
        out = self._apply_text(v, path, new, old_text=old if exists else None, if_match=if_match,
                               dry_run=dry_run, action="base")
        out["validation"] = bases_mod.validate(bases_mod.load(new))
        return out

    # Obsidian config inventory (read-only) ------------------------------------------------------
    def app_config(self, vault: str | None, name: str) -> dict[str, Any]:
        allowed = {"app.json", "appearance.json", "core-plugins.json", "community-plugins.json", "hotkeys.json",
                   "bookmarks.json", "workspaces.json", "daily-notes.json", "templates.json", "graph.json",
                   "zk-prefixer.json", "note-composer.json", "page-preview.json", "backlink.json",
                   "switcher.json", "types.json", "webviewer.json", "workspace.json"}
        if name not in allowed:
            raise ObsidianError(Code.INVALID_ARGUMENT, f"Config {name!r} is not readable here.", allowed=sorted(allowed))
        v = self.vault(vault)
        return {"name": name, "data": v.read_config_json(name),
                "note": "Obsidian config files are internal and may change between versions; "
                        "use CLI commands for authoritative values."}

    def snippets(self, vault: str | None) -> dict[str, Any]:
        v = self.vault(vault)
        folder = v.root / v.config_dir / "snippets"
        appearance = v.read_config_json("appearance.json") or {}
        enabled = set(appearance.get("enabledCssSnippets") or [])
        items = [{"name": p.stem, "enabled": p.stem in enabled, "size": p.stat().st_size}
                 for p in sorted(folder.glob("*.css"))] if folder.is_dir() else []
        themes_dir = v.root / v.config_dir / "themes"
        themes = sorted(p.name for p in themes_dir.iterdir() if p.is_dir()) if themes_dir.is_dir() else []
        return {"snippets": items, "themes": themes, "active_theme": appearance.get("cssTheme") or "default",
                "base_theme": appearance.get("theme")}

    def write_snippet(self, vault: str | None, name: str, css: str) -> dict[str, Any]:
        self.policy.require(Risk.SETTINGS, "Writing CSS snippets")
        if not re.fullmatch(r"[\w .-]{1,80}", name):
            raise ObsidianError(Code.INVALID_ARGUMENT, "Snippet name may contain letters, digits, space, . _ -")
        v = self.vault(vault)
        folder = v.root / v.config_dir / "snippets"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.css"
        v._atomic_write(path, css.encode("utf-8"))  # noqa: SLF001 - controlled config path
        return {"snippet": name, "path": f"{v.config_dir}/snippets/{name}.css",
                "next_step": "Enable it with cli_run snippet:enable name=..., or in Settings → Appearance."}

    # CLI -------------------------------------------------------------------------------------
    def cli_commands(self, section: str | None = None) -> dict[str, Any]:
        cat = cli_mod.catalog()
        cmds = [c for c in cat["commands"] if not section or c["section"].lower() == section.lower()]
        return {"source": cat["source"], "source_commit": cat["source_commit"],
                "minimum_installer": cat["minimum_installer"],
                "commands": [{**c, "risk": cli_risk(c["name"], {}, []).value} for c in cmds]}

    def cli_run(self, vault: str | None, command: str, params: dict[str, Any] | None = None,
                flags: list[str] | None = None, confirm_token: str | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        flags = list(flags or [])
        risk = cli_risk(command, params, flags)
        self.policy.require(risk, f"CLI '{command}'")
        cli = self.cli
        cli.validate(command, params, flags)
        vault_name = self.vault(vault).name if (vault or self.registry.entries) else None
        if risk in CONFIRM_RISKS:
            args = {"vault": vault_name, "command": command, "params": params, "flags": sorted(flags)}
            if not self.policy.consume(confirm_token, "cli_run", args):
                return self.policy.confirmation("cli_run", args, f"Run 'obsidian {command}' "
                                                 f"({risk.value}): {params} {flags}".strip())
        result = cli.run(command, params, flags, vault=vault_name)
        if risk is not Risk.READ and vault_name:
            self.index(self.vault(vault)).invalidate()
        result["risk"] = risk.value
        return result

    # Bridge ------------------------------------------------------------------------------------
    def bridge_call(self, method: str, path: str, body: dict[str, Any] | None = None,
                    query: dict[str, Any] | None = None, risk: Risk = Risk.READ) -> Any:
        self.policy.require(risk, f"Bridge {path}")
        result = self.bridge.call(method, path, body, query)
        if risk is not Risk.READ:
            for idx in self._indexes.values():
                idx.invalidate()
        return result

    # Headless ------------------------------------------------------------------------------------
    def headless_run(self, vault: str | None, command: str, options: dict[str, Any] | None = None,
                     flags: list[str] | None = None, confirm_token: str | None = None,
                     allow_desktop_sync_conflict: bool = False, progress: Progress = no_progress,
                     cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        self.policy.require(Risk.HEADLESS, "Obsidian Headless")
        options = dict(options or {})
        flags = list(flags or [])
        needs_path = command not in ("sync-list-remote", "sync-list-local", "publish-list-sites")
        if needs_path:
            v = self.vault(vault)
            options["path"] = str(v.root)
            if command == "sync" and not allow_desktop_sync_conflict:
                core = v.read_config_json("core-plugins.json") or {}
                if core.get("sync") is True:
                    raise ObsidianError(Code.PERMISSION_DENIED,
                                        "Desktop Sync is enabled for this vault. The Obsidian docs say not to use "
                                        "desktop Sync and Headless Sync on the same device.",
                                        hint="Use the CLI sync tools instead, or pass allow_desktop_sync_conflict "
                                             "if this vault copy is not synced by the desktop app.")
        dry_run = "dry-run" in [f.lstrip("-") for f in flags]
        if command == "sync":
            self.policy.require(Risk.SYNC, "Headless sync")
        elif command == "publish" and not dry_run:
            self.policy.require(Risk.PUBLISH, "Public publishing")
            flags = [f for f in flags if f.lstrip("-") != "yes"] + ["yes"]
            args = {"command": command, "options": options, "flags": sorted(flags)}
            if not self.policy.consume(confirm_token, "headless_publish", args):
                dry = self.headless.run("publish", options, ["dry-run"])
                return self.policy.confirmation("headless_publish", args,
                                                "Publish these changes to your PUBLIC Obsidian Publish site.",
                                                preview=dry["output"][:20000])
        elif command in ("sync-setup", "sync-unlink", "publish-setup", "publish-unlink") or (
                command in ("sync-config", "publish-config", "publish-site-options")
                and (options.keys() - {"path"} or flags)):
            self.policy.require(Risk.SETTINGS, f"'ob {command}'")
        return self.headless.run(command, options, flags, on_line=lambda n, line: progress(n, None, line[:200]),
                                 cancelled=cancelled)
