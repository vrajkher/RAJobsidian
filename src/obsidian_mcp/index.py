"""Incremental metadata index for one vault (the filesystem adapter's MetadataCache).

Refreshes by comparing (mtime, size) on a directory walk, re-parsing only changed notes.
Link resolution follows Obsidian's documented behaviour for internal links: a link is a
path or a file name; names resolve without the ``.md`` extension; when several files
share a name the closest one (same folder) wins, then the shortest path.
"""

from __future__ import annotations

import posixpath
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from . import markdown as md
from .vault import FileInfo, Vault

REFRESH_INTERVAL = 1.0


@dataclass
class Entry:
    info: FileInfo
    text: str | None = None
    note: md.ParsedNote | None = None


class VaultIndex:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.entries: dict[str, Entry] = {}
        self._by_name: dict[str, list[str]] = defaultdict(list)
        self._by_stem: dict[str, list[str]] = defaultdict(list)
        self._lock = threading.RLock()
        self._last_refresh = 0.0
        self._dirty: set[str] = set()
        self.generation = 0

    # Maintenance ----------------------------------------------------------------------
    def invalidate(self, *paths: str) -> None:
        with self._lock:
            self._dirty.update(paths)
            self._last_refresh = 0.0

    def refresh(self, force: bool = False) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_refresh < REFRESH_INTERVAL and not self._dirty:
                return {"changed": 0}
            seen: set[str] = set()
            changed = 0
            for info in self.vault.list(""):
                seen.add(info.path)
                old = self.entries.get(info.path)
                if old and old.info.mtime == info.mtime and old.info.size == info.size \
                        and info.path not in self._dirty:
                    continue
                entry = Entry(info)
                if info.kind == "note":
                    try:
                        entry.text, _ = self.vault.read_text(info.path)
                        entry.note = md.parse(entry.text)
                    except Exception:  # unreadable/non-UTF-8 note: keep it listed, unparsed
                        entry.text, entry.note = None, None
                self.entries[info.path] = entry
                changed += 1
            removed = [p for p in self.entries if p not in seen]
            for p in removed:
                del self.entries[p]
            if changed or removed:
                self._rebuild_name_maps()
                self.generation += 1
            self._dirty.clear()
            self._last_refresh = now
            return {"changed": changed, "removed": len(removed), "files": len(self.entries)}

    def _rebuild_name_maps(self) -> None:
        self._by_name.clear()
        self._by_stem.clear()
        for path in self.entries:
            name = posixpath.basename(path)
            self._by_name[name.lower()].append(path)
            if name.lower().endswith(".md"):
                self._by_stem[name[:-3].lower()].append(path)

    def get(self, path: str) -> Entry | None:
        self.refresh()
        return self.entries.get(path)

    def notes(self) -> list[Entry]:
        self.refresh()
        return [e for e in self.entries.values() if e.note is not None]

    # Link resolution --------------------------------------------------------------------
    def resolve(self, target: str, source: str = "") -> str | None:
        self.refresh()
        target = target.strip()
        if not target:
            return source or None
        t = target.replace("\\", "/").lstrip("/")
        lower = t.lower()
        candidates: list[str] = []
        if "/" in t or t.startswith("."):
            base = posixpath.dirname(source)
            for cand in (posixpath.normpath(posixpath.join(base, t)), posixpath.normpath(t)):
                for c in (cand, cand + ".md"):
                    if c in self.entries:
                        return c
                    match = next((p for p in self.entries if p.lower() == c.lower()), None)
                    if match:
                        return match
            for path in self.entries:
                pl = path.lower()
                if pl.endswith("/" + lower) or pl.endswith("/" + lower + ".md"):
                    candidates.append(path)
        else:
            candidates = list(self._by_stem.get(lower, [])) + list(self._by_name.get(lower, []))
        if not candidates:
            return None
        src_dir = posixpath.dirname(source)
        candidates = sorted(set(candidates),
                            key=lambda p: (posixpath.dirname(p) != src_dir, p.count("/"), len(p), p))
        return candidates[0]

    def outgoing(self, path: str) -> list[dict[str, Any]]:
        entry = self.get(path)
        if entry is None or entry.note is None:
            return []
        out = []
        for link in entry.note.links:
            resolved = self.resolve(link.target, path) if link.target else path
            out.append({"target": link.target, "subpath": link.subpath, "alias": link.alias,
                        "embed": link.embed, "kind": link.kind, "line": link.line + 1,
                        "resolved": resolved, "raw": link.raw})
        return out

    def link_graph(self) -> dict[str, list[tuple[str | None, md.Link]]]:
        """source -> list of (resolved target or None, link)."""
        graph: dict[str, list[tuple[str | None, md.Link]]] = {}
        for entry in self.notes():
            src = entry.info.path
            graph[src] = [(self.resolve(link.target, src) if link.target else src, link)
                          for link in entry.note.links]  # type: ignore[union-attr]
        return graph

    def backlinks(self, path: str) -> list[dict[str, Any]]:
        out = []
        for src, links in self.link_graph().items():
            if src == path:
                continue
            for resolved, link in links:
                if resolved == path:
                    text = self.entries[src].text or ""
                    out.append({"source": src, "line": link.line + 1, "raw": link.raw, "embed": link.embed,
                                "context": _line(text, link.line).strip()[:300]})
        return out

    def unresolved(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for src, links in self.link_graph().items():
            for resolved, link in links:
                if resolved is None:
                    out[link.target].append({"source": src, "line": link.line + 1, "raw": link.raw})
        return dict(out)

    def incoming_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for src, links in self.link_graph().items():
            for resolved in {r for r, _ in links if r and r != src}:
                counts[resolved] += 1
        return counts

    def orphans(self, notes_only: bool = True) -> list[str]:
        incoming = self.incoming_counts()
        self.refresh()
        return sorted(p for p, e in self.entries.items()
                      if incoming[p] == 0 and (not notes_only or e.info.kind == "note"))

    def deadends(self) -> list[str]:
        return sorted(src for src, links in self.link_graph().items() if not links)

    def unlinked_mentions(self, path: str, limit: int = 200) -> list[dict[str, Any]]:
        entry = self.get(path)
        if entry is None:
            return []
        names = [posixpath.basename(path).rsplit(".", 1)[0]]
        if entry.note:
            names += entry.note.aliases
        names = [n for n in {n.strip() for n in names} if len(n) >= 2]
        if not names:
            return []
        pattern = re.compile(r"(?<![\w\[|#])(" + "|".join(re.escape(n) for n in names) + r")(?![\w\]])",
                             re.IGNORECASE)
        out = []
        for other in self.notes():
            src = other.info.path
            if src == path or other.text is None:
                continue
            masked = md.mask_non_content(other.text)
            chars = list(masked)
            for link in other.note.links:  # type: ignore[union-attr]
                if link.start >= 0:
                    for i in range(link.start, link.end):
                        chars[i] = " "
            masked = "".join(chars)
            body_start = other.note.body_offset  # type: ignore[union-attr]
            for m in pattern.finditer(masked, body_start):
                line_no = masked.count("\n", 0, m.start())
                out.append({"source": src, "line": line_no + 1, "match": other.text[m.start():m.end()],
                            "context": _line(other.text, line_no).strip()[:300]})
                if len(out) >= limit:
                    return out
        return out

    # Aggregates -------------------------------------------------------------------------
    def tags(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for entry in self.notes():
            for tag in {t.lower(): t for t in entry.note.tags}.values():  # type: ignore[union-attr]
                counts[tag] += 1
        return dict(sorted(counts.items(), key=lambda kv: kv[0].lower()))

    def tag_tree(self) -> dict[str, Any]:
        """Nested tags as a tree: each node has its own count and a total including children."""
        tree: dict[str, Any] = {}
        for tag, count in self.tags().items():
            level = tree
            parts = tag.lstrip("#").split("/")
            for i, part in enumerate(parts):
                node = level.setdefault(part, {"count": 0, "total": 0, "children": {}})
                node["total"] += count
                if i == len(parts) - 1:
                    node["count"] += count
                level = node["children"]
        return tree

    def properties(self) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for entry in self.notes():
            for key, value in entry.note.frontmatter.items():  # type: ignore[union-attr]
                s = stats.setdefault(str(key), {"count": 0, "types": Counter()})
                s["count"] += 1
                s["types"][_ptype(value)] += 1
        return {k: {"count": v["count"], "type": v["types"].most_common(1)[0][0]}
                for k, v in sorted(stats.items())}

    def graph(self, include_unresolved: bool = False, include_attachments: bool = False,
              include_tags: bool = False, folder: str | None = None, limit: int = 2000) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: Counter[tuple[str, str]] = Counter()

        def add(node_id: str, kind: str) -> None:
            nodes.setdefault(node_id, {"id": node_id, "kind": kind})

        for src, links in self.link_graph().items():
            if folder and not src.startswith(folder.rstrip("/") + "/"):
                continue
            add(src, "note")
            for resolved, link in links:
                if resolved is None:
                    if not include_unresolved:
                        continue
                    target = "unresolved:" + link.target
                    add(target, "unresolved")
                else:
                    kind = self.entries[resolved].info.kind if resolved in self.entries else "note"
                    if kind != "note" and not include_attachments:
                        continue
                    target = resolved
                    add(target, kind)
                if target != src:
                    edges[(src, target)] += 1
            if include_tags and self.entries[src].note:
                for tag in self.entries[src].note.tags:  # type: ignore[union-attr]
                    add(tag, "tag")
                    edges[(src, tag)] += 1
        node_list = list(nodes.values())[:limit]
        keep = {n["id"] for n in node_list}
        edge_list = [{"source": a, "target": b, "count": c} for (a, b), c in edges.items()
                     if a in keep and b in keep]
        return {"nodes": node_list, "edges": edge_list, "truncated": len(nodes) > limit}

    def stats(self) -> dict[str, Any]:
        self.refresh()
        kinds = Counter(e.info.kind for e in self.entries.values())
        return {"files": len(self.entries), "by_kind": dict(kinds),
                "total_bytes": sum(e.info.size for e in self.entries.values()),
                "words": sum(e.note.word_count for e in self.entries.values() if e.note),
                "tags": len(self.tags()), "unresolved_links": len(self.unresolved())}


def _line(text: str, line: int) -> str:
    lines = text.split("\n")
    return lines[line] if 0 <= line < len(lines) else ""


def _ptype(value: Any) -> str:
    if isinstance(value, bool):
        return "checkbox"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, tuple)):
        return "list"
    if hasattr(value, "isoformat"):
        return "datetime" if hasattr(value, "hour") else "date"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "date"
    return "text"
