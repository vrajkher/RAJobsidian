"""Local full-text search, related notes, and duplicate detection.

Works with Obsidian closed and needs no AI service. Query syntax is a subset of Obsidian's
documented search operators, evaluated locally:

    word "exact phrase" -excluded  a OR b  /regex/
    file:name  path:folder  tag:#tag  content:word  [property]  [property:value]

Native Obsidian search (exact app semantics) is available through the CLI adapter.
"""

from __future__ import annotations

import hashlib
import math
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import markdown as md
from .errors import Code, ObsidianError
from .index import Entry, VaultIndex

TOKEN_RE = re.compile(r"[^\W_]+(?:[ऀ-෿̀-ͯ]+[^\W_]*)*", re.UNICODE)
STOP = {"the", "and", "for", "that", "with", "this", "from", "are", "was", "but", "not", "you", "have",
        "has", "had", "its", "into", "your", "our", "their", "they", "them", "there", "then", "than"}


@dataclass
class Atom:
    negate: bool
    field: str  # text | file | path | tag | content | prop | regex
    value: str
    prop_value: str | None = None
    regex: re.Pattern[str] | None = None


def parse_query(query: str, case_sensitive: bool = False) -> list[list[Atom]]:
    """Return a conjunction of disjunctions: [[a OR b], [c], ...]."""
    try:
        tokens = shlex.split(query, posix=True)
    except ValueError:
        tokens = query.split()
    # shlex drops quotes; recover phrase-ness by re-scanning the raw query.
    groups: list[list[Atom]] = []
    pending_or = False
    flags = 0 if case_sensitive else re.IGNORECASE
    for tok in tokens:
        if tok == "OR":
            pending_or = True
            continue
        negate = tok.startswith("-") and len(tok) > 1
        if negate:
            tok = tok[1:]
        atom: Atom
        if len(tok) > 2 and tok.startswith("/") and tok.endswith("/"):
            try:
                atom = Atom(negate, "regex", tok, regex=re.compile(tok[1:-1], flags | re.M))
            except re.error as exc:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"Invalid regular expression {tok}: {exc}") from exc
        elif tok.startswith("[") and tok.endswith("]"):
            inner = tok[1:-1]
            key, _, val = inner.partition(":")
            atom = Atom(negate, "prop", key.strip(), val.strip() or None)
        elif ":" in tok and tok.split(":", 1)[0] in ("file", "path", "tag", "content"):
            field, val = tok.split(":", 1)
            atom = Atom(negate, field, val)
        else:
            atom = Atom(negate, "text", tok)
        if pending_or and groups:
            groups[-1].append(atom)
        else:
            groups.append([atom])
        pending_or = False
    return groups


def _contains(hay: str, needle: str, case: bool) -> bool:
    return needle in hay if case else needle.lower() in hay.lower()


def _match_atom(atom: Atom, entry: Entry, case: bool) -> bool:
    path = entry.info.path
    text = entry.text or ""
    if atom.field == "file":
        res = _contains(entry.info.name, atom.value, case)
    elif atom.field == "path":
        res = _contains(path, atom.value, case)
    elif atom.field == "tag":
        want = "#" + atom.value.lstrip("#").lower()
        tags = [t.lower() for t in (entry.note.tags if entry.note else [])]
        res = any(t == want or t.startswith(want + "/") for t in tags)
    elif atom.field == "prop":
        fm = md.to_plain(entry.note.frontmatter) if entry.note else {}
        keys = {k.lower(): k for k in fm}
        key = keys.get(atom.value.lower())
        if key is None:
            res = False
        elif atom.prop_value is None:
            res = True
        else:
            vals = fm[key] if isinstance(fm[key], list) else [fm[key]]
            res = any(_contains(str(v), atom.prop_value, case) for v in vals)
    elif atom.field == "regex":
        res = bool(atom.regex and (atom.regex.search(text) or atom.regex.search(entry.info.name)))
    elif atom.field == "content":
        res = _contains(text, atom.value, case)
    else:
        res = _contains(text, atom.value, case) or _contains(entry.info.name, atom.value, case)
    return res != atom.negate


def _snippets(text: str, atoms: list[Atom], case: bool, context: int, max_snippets: int) -> list[dict[str, Any]]:
    lines = text.split("\n")
    hits: list[int] = []
    for i, line in enumerate(lines):
        for atom in atoms:
            if atom.negate:
                continue
            if atom.field in ("text", "content") and _contains(line, atom.value, case) \
                    or atom.field == "regex" and atom.regex and atom.regex.search(line):
                hits.append(i)
                break
        if len(hits) >= max_snippets:
            break
    out = []
    for i in hits:
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        out.append({"line": i + 1, "text": "\n".join(lines[lo:hi]).strip()[:500]})
    return out


def search(index: VaultIndex, query: str = "", *, folder: str | None = None, tags: list[str] | None = None,
           properties: dict[str, Any] | None = None, kinds: list[str] | None = None,
           modified_after: str | None = None, case_sensitive: bool = False, context_lines: int = 1,
           max_snippets: int = 3, offset: int = 0, limit: int = 20, sort: str = "relevance") -> dict[str, Any]:
    groups = parse_query(query, case_sensitive) if query.strip() else []
    flat = [a for g in groups for a in g]
    after = None
    if modified_after:
        try:
            after = datetime.fromisoformat(modified_after).timestamp()
        except ValueError as exc:
            raise ObsidianError(Code.INVALID_ARGUMENT, "modified_after must be an ISO date like 2026-01-31.") from exc
    index.refresh()
    results = []
    folder_prefix = folder.strip("/") + "/" if folder and folder.strip("/") else None
    for entry in index.entries.values():
        info = entry.info
        if kinds and info.kind not in kinds:
            continue
        if not kinds and info.kind != "note" and not query:
            continue
        if folder_prefix and not info.path.startswith(folder_prefix):
            continue
        if after and info.mtime < after:
            continue
        if tags:
            note_tags = [t.lower() for t in (entry.note.tags if entry.note else [])]
            if not all(any(t == "#" + w.lstrip("#").lower() or t.startswith("#" + w.lstrip("#").lower() + "/")
                           for t in note_tags) for w in tags):
                continue
        if properties:
            fm = md.to_plain(entry.note.frontmatter) if entry.note else {}
            if not all(_prop_equals(fm.get(k), v) for k, v in properties.items()):
                continue
        if groups and not all(any(_match_atom(a, entry, case_sensitive) for a in g) for g in groups):
            continue
        text = entry.text or ""
        score = 0.0
        for a in flat:
            if a.negate or a.field not in ("text", "content", "regex"):
                continue
            if a.field == "regex" and a.regex:
                score += len(a.regex.findall(text))
            else:
                hay = text if case_sensitive else text.lower()
                needle = a.value if case_sensitive else a.value.lower()
                score += hay.count(needle)
                if needle in (info.name if case_sensitive else info.name.lower()):
                    score += 10
        results.append((score, info.mtime, entry))
    if sort == "modified":
        results.sort(key=lambda r: -r[1])
    elif sort == "path":
        results.sort(key=lambda r: r[2].info.path.lower())
    else:
        results.sort(key=lambda r: (-r[0], -r[1]))
    total = len(results)
    page = results[offset: offset + limit]
    items = []
    for score, _, entry in page:
        item: dict[str, Any] = {"path": entry.info.path, "kind": entry.info.kind, "score": score,
                                "modified": entry.info.to_dict()["modified"]}
        if entry.text is not None and flat:
            item["matches"] = _snippets(entry.text, flat, case_sensitive, context_lines, max_snippets)
        items.append(item)
    nxt = offset + limit if offset + limit < total else None
    return {"query": query, "total": total, "offset": offset, "next_offset": nxt, "results": items}


def _prop_equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return expected in actual or str(expected) in [str(a) for a in actual]
    return actual == expected or str(actual) == str(expected)


# Related notes (local TF-IDF) ------------------------------------------------------------

def tokens(text: str) -> list[str]:
    return [t for t in (m.group(0).lower() for m in TOKEN_RE.finditer(text)) if len(t) > 2 and t not in STOP]


class RelatedNotes:
    def __init__(self, index: VaultIndex) -> None:
        self.index = index
        self._generation = -1
        self._vectors: dict[str, dict[str, float]] = {}

    def _build(self) -> None:
        self.index.refresh()
        if self._generation == self.index.generation:
            return
        docs = {e.info.path: Counter(tokens(md.mask_non_content(e.text or ""))) for e in self.index.notes()}
        df: Counter[str] = Counter()
        for tf in docs.values():
            df.update(tf.keys())
        n = max(1, len(docs))
        vectors = {}
        for path, tf in docs.items():
            vec = {t: (1 + math.log(c)) * math.log((n + 1) / (df[t] + 0.5)) for t, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            vectors[path] = {t: v / norm for t, v in vec.items()}
        self._vectors = vectors
        self._generation = self.index.generation

    def related(self, path: str, limit: int = 10) -> list[dict[str, Any]]:
        self._build()
        vec = self._vectors.get(path)
        if vec is None:
            raise ObsidianError(Code.NOT_FOUND, f"{path!r} is not an indexed note.")
        scores = []
        for other, ov in self._vectors.items():
            if other == path:
                continue
            small, big = (vec, ov) if len(vec) < len(ov) else (ov, vec)
            s = sum(v * big.get(t, 0.0) for t, v in small.items())
            if s > 0:
                scores.append((s, other))
        scores.sort(reverse=True)
        return [{"path": p, "similarity": round(s, 4)} for s, p in scores[:limit]]


def duplicates(index: VaultIndex, near_threshold: float = 0.8, max_notes: int = 3000) -> dict[str, Any]:
    notes = index.notes()
    by_hash: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    shingles: dict[str, set[int]] = {}
    for e in notes:
        body = (e.text or "")[e.note.body_offset:] if e.note else ""
        norm = re.sub(r"\s+", " ", body).strip().lower()
        if norm:
            by_hash.setdefault(hashlib.sha256(norm.encode()).hexdigest(), []).append(e.info.path)
        by_name.setdefault(e.info.name.lower(), []).append(e.info.path)
        words = norm.split()
        if len(words) >= 8 and len(shingles) < max_notes:
            shingles[e.info.path] = {hash(" ".join(words[i:i + 5])) for i in range(len(words) - 4)}
    exact = [paths for paths in by_hash.values() if len(paths) > 1]
    exact_sets = [set(p) for p in exact]
    near = []
    items = list(shingles.items())
    for i in range(len(items)):
        a_path, a = items[i]
        for j in range(i + 1, len(items)):
            b_path, b = items[j]
            if any({a_path, b_path} <= s for s in exact_sets):
                continue
            small, big = (a, b) if len(a) < len(b) else (b, a)
            if len(small) / len(big) < near_threshold:
                continue
            jac = len(a & b) / len(a | b)
            if jac >= near_threshold:
                near.append({"paths": [a_path, b_path], "similarity": round(jac, 3)})
    same_name = [paths for paths in by_name.values() if len(paths) > 1]
    return {"exact_content": exact, "near_duplicates": near, "same_name": same_name}
