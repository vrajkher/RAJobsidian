"""Filesystem access to one vault, confined to its root.

Safety properties (all covered by tests):

* Paths are vault-relative POSIX strings. Absolute paths, ``..`` segments, NUL bytes,
  drive letters, and anything resolving (through symlinks) outside the root are rejected.
* Hidden entries (``.obsidian``, ``.git``...) are skipped when listing and refused for
  writes; the Obsidian trash folder ``.trash`` is managed only through trash/restore.
* Writes are atomic (temp file + fsync + rename) and keep the existing file mode,
  line-ending style, and UTF-8 BOM. ``if_match`` compares ETags (SHA-256 of bytes) to
  detect concurrent edits. The previous version is copied to a backup store outside the
  vault before it is replaced, and every mutation is recorded in an operation log that
  ``rollback`` can undo.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import Code, ObsidianError

TRASH_DIR = ".trash"
TEXT_EXTS = {".md", ".canvas", ".base", ".txt", ".json", ".css", ".csv", ".tsv", ".js", ".html", ".svg",
             ".xml", ".yaml", ".yml"}
OBSIDIAN_EXTS = {".md", ".canvas", ".base"}

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("application/json", ".canvas")
mimetypes.add_type("application/yaml", ".base")
for _ext, _mime in {".webp": "image/webp", ".avif": "image/avif", ".m4a": "audio/mp4", ".flac": "audio/flac",
                    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".3gp": "audio/3gpp", ".webm": "video/webm",
                    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".ogv": "video/ogg"}.items():
    mimetypes.add_type(_mime, _ext)


def etag_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def mime_of(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def file_kind(path: str) -> str:
    ext = PurePosixPath(path).suffix.lower()
    if ext == ".md":
        return "note"
    if ext == ".canvas":
        return "canvas"
    if ext == ".base":
        return "base"
    mime = mime_of(path)
    for prefix, kind in (("image/", "image"), ("audio/", "audio"), ("video/", "video")):
        if mime.startswith(prefix):
            return kind
    if mime == "application/pdf":
        return "pdf"
    return "attachment"


def normalize_rel(path: str) -> str:
    """Normalize a user-supplied vault-relative path or raise INVALID_PATH."""
    if path is None:
        raise ObsidianError(Code.INVALID_PATH, "Path is required.")
    p = path.replace("\\", "/").strip()
    if "\x00" in p:
        raise ObsidianError(Code.INVALID_PATH, "Path contains a NUL byte.")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ObsidianError(Code.INVALID_PATH, f"Use a vault-relative path, not {path!r}.",
                            hint="Example: 'Projects/Plan.md'.")
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if any(s == ".." for s in parts):
        raise ObsidianError(Code.PATH_OUTSIDE_VAULT, "Paths may not contain '..'.")
    return "/".join(parts)


def detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def match_style(old_text: str | None, new_text: str) -> str:
    """Give new text the same newline style and BOM as the file it replaces."""
    if old_text is None:
        return new_text
    if detect_newline(old_text) == "\r\n" and "\n" in new_text:
        new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
    if old_text.startswith("﻿") and not new_text.startswith("﻿"):
        new_text = "﻿" + new_text
    return new_text


def unified_diff(old: str, new: str, path: str, context: int = 3, limit: int = 400) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}",
                                      n=context, lineterm=""))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... diff truncated ({len(lines) - limit} more lines)"]
    return "\n".join(lines)


@dataclass
class FileInfo:
    path: str
    name: str
    kind: str
    ext: str
    size: int
    mtime: float
    ctime: float
    mime: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "name": self.name, "kind": self.kind, "ext": self.ext, "size": self.size,
                "modified": _iso(self.mtime), "created": _iso(self.ctime), "mime": self.mime}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


class BackupStore:
    """Copies of replaced/deleted content, kept outside the vault."""

    def __init__(self, root: Path, keep: int) -> None:
        self.root = root
        self.keep = keep

    def save(self, rel: str, data: bytes) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        backup_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        (self.root / f"{backup_id}.bin").write_bytes(data)
        (self.root / f"{backup_id}.json").write_text(json.dumps(
            {"id": backup_id, "path": rel, "etag": etag_of(data), "size": len(data), "time": time.time()},
            ensure_ascii=False), encoding="utf-8")
        self._prune()
        return backup_id

    def load(self, backup_id: str) -> tuple[dict[str, Any], bytes]:
        if not backup_id.replace("-", "").isalnum():
            raise ObsidianError(Code.INVALID_ARGUMENT, "Invalid backup id.")
        meta_path = self.root / f"{backup_id}.json"
        if not meta_path.exists():
            raise ObsidianError(Code.NOT_FOUND, f"Backup {backup_id} does not exist (it may have been pruned).")
        return json.loads(meta_path.read_text(encoding="utf-8")), (self.root / f"{backup_id}.bin").read_bytes()

    def list(self, rel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        metas = []
        for meta in sorted(self.root.glob("*.json"), reverse=True):
            info = json.loads(meta.read_text(encoding="utf-8"))
            if rel is None or info["path"] == rel:
                metas.append(info)
            if len(metas) >= limit:
                break
        return metas

    def _prune(self) -> None:
        metas = sorted(self.root.glob("*.json"))
        for meta in metas[: max(0, len(metas) - self.keep)]:
            meta.unlink(missing_ok=True)
            meta.with_suffix(".bin").unlink(missing_ok=True)


class OperationLog:
    """Append-only JSONL log of mutations, used for rollback information."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, action: str, changes: list[dict[str, Any]], **extra: Any) -> str:
        op_id = "op-" + uuid.uuid4().hex[:12]
        entry = {"id": op_id, "time": time.time(), "action": action, "changes": changes, **extra}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return op_id

    def entries(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in reversed(lines[-limit:])]

    def get(self, op_id: str) -> dict[str, Any]:
        for entry in self.entries(limit=100000):
            if entry["id"] == op_id:
                return entry
        raise ObsidianError(Code.NOT_FOUND, f"Operation {op_id} not found in the log.")


class Vault:
    def __init__(self, vault_id: str, name: str, root: Path, state_dir: Path, *, backups: bool = True,
                 backups_keep: int = 50, config_dir: str = ".obsidian") -> None:
        self.id = vault_id
        self.name = name
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise ObsidianError(Code.VAULT_NOT_FOUND, f"Vault folder {str(self.root)!r} does not exist.")
        self.config_dir = config_dir
        self.backups_enabled = backups
        self.backups = BackupStore(state_dir / "backups" / vault_id, backups_keep)
        self.oplog = OperationLog(state_dir / "oplog" / f"{vault_id}.jsonl")
        self.lock = threading.RLock()

    # Paths ------------------------------------------------------------------------------
    def resolve(self, rel: str, *, allow_hidden: bool = False, allow_root: bool = False) -> Path:
        norm = normalize_rel(rel)
        if not norm and not allow_root:
            raise ObsidianError(Code.INVALID_PATH, "A file path is required.")
        if not allow_hidden and any(part.startswith(".") for part in norm.split("/") if part):
            raise ObsidianError(Code.INVALID_PATH, f"{norm!r} is inside a hidden folder.",
                                hint="Hidden folders such as .obsidian and .trash are not vault content.")
        candidate = self.root / norm
        real = candidate.resolve(strict=False)
        if real != self.root and self.root not in real.parents:
            raise ObsidianError(Code.PATH_OUTSIDE_VAULT, f"{norm!r} resolves outside the vault.",
                                hint="Symlinks that leave the vault are not followed.")
        return candidate

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def exists(self, rel: str) -> bool:
        try:
            return self.resolve(rel).exists()
        except ObsidianError:
            return False

    def info(self, rel: str) -> FileInfo:
        path = self.resolve(rel)
        if not path.exists():
            raise ObsidianError(Code.NOT_FOUND, f"{normalize_rel(rel)!r} does not exist.",
                                hint="Use list_files or search_notes to find the right path.")
        return self._info(path)

    def _info(self, path: Path) -> FileInfo:
        st = path.stat()
        rel = self.rel(path)
        ctime = getattr(st, "st_birthtime", st.st_ctime)
        return FileInfo(rel, path.name, "folder" if path.is_dir() else file_kind(rel),
                        path.suffix.lower().lstrip("."), st.st_size, st.st_mtime, ctime,
                        "inode/directory" if path.is_dir() else mime_of(rel))

    # Listing ----------------------------------------------------------------------------
    def walk(self, folder: str = "", *, recursive: bool = True, include_folders: bool = False) -> Iterator[Path]:
        base = self.resolve(folder, allow_root=True)
        if not base.is_dir():
            raise ObsidianError(Code.NOT_FOUND, f"Folder {folder!r} does not exist.")
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda e: e.name.lower())
            except OSError:
                continue
            subdirs = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                path = Path(entry.path)
                if entry.is_symlink():
                    real = path.resolve()
                    if real != self.root and self.root not in real.parents:
                        continue
                if entry.is_dir():
                    if include_folders:
                        yield path
                    if recursive:
                        subdirs.append(path)
                else:
                    yield path
            stack.extend(reversed(subdirs))

    def list(self, folder: str = "", *, recursive: bool = True, kinds: list[str] | None = None,
             include_folders: bool = False) -> list[FileInfo]:
        out = []
        for path in self.walk(folder, recursive=recursive, include_folders=include_folders):
            try:
                info = self._info(path)
            except OSError:
                continue
            if kinds and info.kind not in kinds:
                continue
            out.append(info)
        return out

    # Reading ----------------------------------------------------------------------------
    def read_bytes(self, rel: str, *, max_bytes: int | None = None, allow_hidden: bool = False) -> bytes:
        path = self.resolve(rel, allow_hidden=allow_hidden)
        if not path.is_file():
            raise ObsidianError(Code.NOT_FOUND, f"{normalize_rel(rel)!r} does not exist or is a folder.",
                                hint="Use list_files or search_notes to find the right path.")
        size = path.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise ObsidianError(Code.TOO_LARGE, f"{rel!r} is {size} bytes; the limit is {max_bytes}.",
                                hint="Read a range with read_note(start_line, end_line) or raise max_read_bytes.")
        return path.read_bytes()

    def read_text(self, rel: str, *, max_bytes: int | None = None, allow_hidden: bool = False) -> tuple[str, str]:
        data = self.read_bytes(rel, max_bytes=max_bytes, allow_hidden=allow_hidden)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObsidianError(Code.UNSUPPORTED, f"{rel!r} is not UTF-8 text.",
                                hint="Use read_attachment for binary files.") from exc
        return text, etag_of(data)

    def read_blob(self, rel: str, *, max_bytes: int | None = None) -> dict[str, Any]:
        data = self.read_bytes(rel, max_bytes=max_bytes)
        return {"path": normalize_rel(rel), "mime": mime_of(rel), "size": len(data), "etag": etag_of(data),
                "base64": base64.b64encode(data).decode("ascii")}

    # Writing ----------------------------------------------------------------------------
    def _check_writable(self, rel: str) -> Path:
        path = self.resolve(rel)
        if path.exists() and path.is_dir():
            raise ObsidianError(Code.INVALID_PATH, f"{rel!r} is a folder.")
        return path

    def _atomic_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o7777 if path.exists() else None
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".obsidian-mcp-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def write_bytes(self, rel: str, data: bytes, *, if_match: str | None = None, mode: str = "upsert",
                    action: str = "write", record: bool = True) -> dict[str, Any]:
        """mode: 'create' (must not exist), 'overwrite' (must exist), 'upsert'."""
        norm = normalize_rel(rel)
        with self.lock:
            path = self._check_writable(norm)
            exists = path.exists()
            old = path.read_bytes() if exists else None
            if mode == "create" and exists:
                raise ObsidianError(Code.ALREADY_EXISTS, f"{norm!r} already exists.",
                                    hint="Pass overwrite=true or choose another name.")
            if mode == "overwrite" and not exists:
                raise ObsidianError(Code.NOT_FOUND, f"{norm!r} does not exist.")
            current = etag_of(old) if old is not None else None
            if if_match is not None and if_match != current:
                raise ObsidianError(Code.CONFLICT, f"{norm!r} changed since you read it.",
                                    hint="Read it again and reapply your edit.", current_etag=current)
            backup_id = self.backups.save(norm, old) if old is not None and self.backups_enabled else None
            self._atomic_write(path, data)
            new_etag = etag_of(data)
            change = {"path": norm, "kind": "modify" if exists else "create", "before_backup": backup_id,
                      "before_etag": current, "after_etag": new_etag}
            op_id = self.oplog.record(action, [change]) if record else None
            return {"path": norm, "etag": new_etag, "previous_etag": current, "created": not exists,
                    "backup_id": backup_id, "operation_id": op_id, "change": change}

    def write_text(self, rel: str, text: str, *, if_match: str | None = None, mode: str = "upsert",
                   preserve_style: bool = True, action: str = "write", record: bool = True) -> dict[str, Any]:
        path = self._check_writable(rel)
        if preserve_style and path.exists():
            try:
                text = match_style(path.read_bytes().decode("utf-8"), text)
            except UnicodeDecodeError:
                pass
        return self.write_bytes(rel, text.encode("utf-8"), if_match=if_match, mode=mode, action=action,
                                record=record)

    def mkdir(self, rel: str) -> dict[str, Any]:
        path = self.resolve(rel)
        if path.exists():
            raise ObsidianError(Code.ALREADY_EXISTS, f"{rel!r} already exists.")
        path.mkdir(parents=True)
        op = self.oplog.record("create_folder", [{"path": normalize_rel(rel), "kind": "create_folder"}])
        return {"path": normalize_rel(rel), "operation_id": op}

    def copy(self, src: str, dst: str, *, overwrite: bool = False) -> dict[str, Any]:
        data = self.read_bytes(src)
        return self.write_bytes(dst, data, mode="upsert" if overwrite else "create", action="copy")

    def move(self, src: str, dst: str, *, overwrite: bool = False, record: bool = True) -> dict[str, Any]:
        """Move without touching links. Use the service layer for link-aware moves."""
        s, d = normalize_rel(src), normalize_rel(dst)
        with self.lock:
            sp, dp = self.resolve(s), self.resolve(d)
            if not sp.exists():
                raise ObsidianError(Code.NOT_FOUND, f"{s!r} does not exist.")
            if dp.exists() and not overwrite:
                raise ObsidianError(Code.ALREADY_EXISTS, f"{d!r} already exists.")
            if sp.is_dir() and (dp == sp or sp in dp.parents):
                raise ObsidianError(Code.INVALID_PATH, "Cannot move a folder into itself.")
            if dp.exists() and (dp.is_dir() or sp.is_dir()):
                raise ObsidianError(Code.ALREADY_EXISTS, f"{d!r} already exists; folders are never overwritten.")
            backup = None
            if dp.exists() and self.backups_enabled:
                backup = self.backups.save(d, dp.read_bytes())
            dp.parent.mkdir(parents=True, exist_ok=True)
            os.replace(sp, dp)
            change = {"path": d, "from": s, "kind": "move", "overwritten_backup": backup}
            op = self.oplog.record("move", [change]) if record else None
            return {"from": s, "to": d, "operation_id": op, "change": change}

    # Trash ------------------------------------------------------------------------------
    def trash(self, rel: str, *, record: bool = True) -> dict[str, Any]:
        """Move to the vault's Obsidian trash folder (.trash), keeping the relative path."""
        norm = normalize_rel(rel)
        with self.lock:
            src = self.resolve(norm)
            if not src.exists():
                raise ObsidianError(Code.NOT_FOUND, f"{norm!r} does not exist.")
            dest_rel = f"{TRASH_DIR}/{norm}"
            dest = self.root / dest_rel
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                dest = dest.with_name(f"{stem} {time.strftime('%Y%m%d%H%M%S')}{suffix}")
                dest_rel = self.rel(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            change = {"path": norm, "kind": "trash", "trash_path": dest_rel}
            op = self.oplog.record("trash", [change]) if record else None
            return {"path": norm, "trash_path": dest_rel, "operation_id": op, "change": change}

    def list_trash(self) -> list[dict[str, Any]]:
        root = self.root / TRASH_DIR
        if not root.is_dir():
            return []
        out = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                st = path.stat()
                out.append({"trash_path": self.rel(path), "original_path": self.rel(path)[len(TRASH_DIR) + 1:],
                            "size": st.st_size, "trashed": _iso(st.st_mtime)})
        return out

    def restore(self, trash_path: str, to: str | None = None) -> dict[str, Any]:
        norm = normalize_rel(trash_path)
        if not norm.startswith(TRASH_DIR + "/"):
            norm = f"{TRASH_DIR}/{norm}"
        src = self.resolve(norm, allow_hidden=True)
        if not src.exists():
            raise ObsidianError(Code.NOT_FOUND, f"{norm!r} is not in the trash.")
        target = normalize_rel(to) if to else norm[len(TRASH_DIR) + 1:]
        dest = self.resolve(target)
        if dest.exists():
            raise ObsidianError(Code.ALREADY_EXISTS, f"{target!r} already exists.",
                                hint="Pass 'to' with a different destination.")
        with self.lock:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            op = self.oplog.record("restore", [{"path": target, "kind": "restore", "trash_path": norm}])
        return {"restored": target, "operation_id": op}

    def delete_permanently(self, rel: str, *, allow_trash: bool = True) -> dict[str, Any]:
        norm = normalize_rel(rel)
        path = self.resolve(norm, allow_hidden=allow_trash and norm.startswith(TRASH_DIR + "/"))
        if not path.exists():
            raise ObsidianError(Code.NOT_FOUND, f"{norm!r} does not exist.")
        with self.lock:
            backup = None
            if path.is_file():
                backup = self.backups.save(norm, path.read_bytes()) if self.backups_enabled else None
                path.unlink()
            else:
                shutil.rmtree(path)
            op = self.oplog.record("delete_permanently", [{"path": norm, "kind": "delete",
                                                           "before_backup": backup}])
        return {"deleted": norm, "backup_id": backup, "operation_id": op}

    # Rollback ---------------------------------------------------------------------------
    def rollback(self, op_id: str, *, force: bool = False) -> dict[str, Any]:
        entry = self.oplog.get(op_id)
        results = []
        with self.lock:
            for change in reversed(entry["changes"]):
                results.append(self._undo(change, force))
            new_op = self.oplog.record("rollback", [], rolled_back=op_id)
        return {"rolled_back": op_id, "results": results, "operation_id": new_op}

    def _undo(self, change: dict[str, Any], force: bool) -> dict[str, Any]:
        kind, rel = change["kind"], change["path"]
        try:
            if kind in ("modify", "create"):
                path = self.resolve(rel)
                current = etag_of(path.read_bytes()) if path.exists() else None
                if current != change.get("after_etag") and not force:
                    return {"path": rel, "status": "conflict",
                            "message": "File changed after this operation; pass force=true to override."}
                if kind == "create":
                    if path.exists():
                        self.trash(rel, record=False)
                    return {"path": rel, "status": "undone", "detail": "created file moved to .trash"}
                _, data = self.backups.load(change["before_backup"])
                self._atomic_write(path, data)
                return {"path": rel, "status": "undone"}
            if kind == "move":
                self.move(rel, change["from"], record=False)
                return {"path": rel, "status": "undone", "detail": f"moved back to {change['from']}"}
            if kind == "trash":
                self.restore(change["trash_path"], change["path"])
                return {"path": rel, "status": "undone", "detail": "restored from trash"}
            if kind == "delete" and change.get("before_backup"):
                _, data = self.backups.load(change["before_backup"])
                self.write_bytes(rel, data, mode="create", record=False)
                return {"path": rel, "status": "undone", "detail": "recreated from backup"}
            return {"path": rel, "status": "skipped", "message": f"Cannot undo '{kind}'."}
        except ObsidianError as exc:
            return {"path": rel, "status": "error", "message": exc.message}

    # Obsidian config (read-only helpers) ---------------------------------------------------
    def read_config_json(self, name: str) -> dict[str, Any] | None:
        """Read a JSON file from the vault's config folder (e.g. 'daily-notes.json').

        The config folder layout is not a documented API; callers must treat missing or
        unexpected content as "unknown" and fall back to documented defaults.
        """
        if "/" in name or "\\" in name or name.startswith("."):
            raise ObsidianError(Code.INVALID_ARGUMENT, "Config name must be a plain file name.")
        path = self.root / self.config_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else {"value": data}
