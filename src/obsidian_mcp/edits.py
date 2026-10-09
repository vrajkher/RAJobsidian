"""Pure text transformations: precise patches, link rewriting, Note composer operations.

Nothing here touches the disk. Each function returns new text (or a plan of per-file
changes) so the service layer can preview diffs before applying them atomically.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any
from urllib.parse import quote

from . import markdown as md
from .errors import Code, ObsidianError
from .index import VaultIndex


def _fail(msg: str, **data: Any) -> ObsidianError:
    return ObsidianError(Code.PATCH_FAILED, msg, hint="Read the note again and adjust the patch.", **data)


def _split_lines(text: str) -> tuple[list[str], str]:
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.split(nl), nl


def _norm_content(content: str, nl: str) -> list[str]:
    return content.replace("\r\n", "\n").split("\n") if content != "" else []


def apply_patch(text: str, ops: list[dict[str, Any]]) -> str:
    """Apply patch operations in order. Every op must match exactly or the whole patch fails.

    Ops:
      {"op": "replace", "find": str, "replace": str, "count": int=1}  # count = expected occurrences; 0 = all
      {"op": "regex_replace", "pattern": str, "replace": str, "count": int|None}
      {"op": "insert", "heading": "A#B", "content": str, "position": "start"|"end"}
      {"op": "replace_section", "heading": str, "content": str}
      {"op": "delete_section", "heading": str}
      {"op": "replace_block", "block_id": str, "content": str}
      {"op": "insert_after_block", "block_id": str, "content": str}
      {"op": "replace_lines", "start_line": int, "end_line": int, "content": str, "expected": str|None}
      {"op": "insert_at_line", "line": int, "content": str}   # 1-based; inserted before that line
      {"op": "append", "content": str} / {"op": "prepend", "content": str}  # prepend goes after frontmatter
    """
    for i, op in enumerate(ops):
        kind = op.get("op")
        try:
            text = _apply_one(text, op)
        except ObsidianError as exc:
            exc.data.setdefault("op_index", i)
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise _fail(f"Patch op #{i} ({kind}) is malformed: {exc}") from exc
    return text


def _apply_one(text: str, op: dict[str, Any]) -> str:
    kind = op["op"]
    if kind == "replace":
        find, repl = op["find"], op["replace"]
        if not find:
            raise _fail("'find' must not be empty.")
        found = text.count(find)
        expected = op.get("count", 1)
        if found == 0:
            raise _fail(f"Text to replace was not found: {find[:80]!r}")
        if expected and found != expected:
            raise _fail(f"Expected {expected} occurrence(s) of {find[:80]!r}, found {found}. "
                        "Add more surrounding text to make it unique or set count.", found=found)
        return text.replace(find, repl)
    if kind == "regex_replace":
        pattern = re.compile(op["pattern"], re.M)
        new, n = pattern.subn(op["replace"], text)
        if n == 0:
            raise _fail(f"Pattern {op['pattern']!r} matched nothing.")
        if op.get("count") is not None and n != op["count"]:
            raise _fail(f"Pattern matched {n} times, expected {op['count']}.", found=n)
        return new
    lines, nl = _split_lines(text)
    if kind in ("insert", "replace_section", "delete_section"):
        rng = md.section_range(text, op["heading"])
        if rng is None:
            raise _fail(f"Heading {op['heading']!r} not found.",
                        headings=[h.text for h in md.parse(text).headings])
        head, start, end = rng
        # Keep trailing blank lines between sections outside the replaced region.
        content_end = end
        while content_end > start and not lines[content_end - 1].strip():
            content_end -= 1
        new = _norm_content(op.get("content", ""), nl)
        if kind == "insert":
            if op.get("position", "end") == "start":
                lines[start:start] = new
            else:
                lines[content_end:content_end] = new
        elif kind == "replace_section":
            lines[start:content_end] = new
        else:
            lines[head:content_end] = []
        return nl.join(lines)
    if kind in ("replace_block", "insert_after_block"):
        bid = op["block_id"].lstrip("^")
        note = md.parse(text)
        if bid not in note.block_ids:
            raise _fail(f"Block ^{bid} not found.", blocks=list(note.block_ids))
        line = note.block_ids[bid]
        new = _norm_content(op["content"], nl)
        if kind == "replace_block":
            if new and not re.search(r"\^" + re.escape(bid) + r"\s*$", new[-1]):
                new[-1] = new[-1].rstrip() + f" ^{bid}"
            lines[line:line + 1] = new
        else:
            lines[line + 1:line + 1] = new
        return nl.join(lines)
    if kind == "replace_lines":
        s, e = int(op["start_line"]), int(op["end_line"])
        if not (1 <= s <= e <= len(lines)):
            raise _fail(f"Line range {s}-{e} is outside the note (1-{len(lines)}).")
        if op.get("expected") is not None:
            current = "\n".join(lines[s - 1:e])
            if current != op["expected"].replace("\r\n", "\n"):
                raise _fail("Lines do not contain the expected text (the note changed).", current=current[:500])
        lines[s - 1:e] = _norm_content(op["content"], nl)
        return nl.join(lines)
    if kind == "insert_at_line":
        n = int(op["line"])
        if not 1 <= n <= len(lines) + 1:
            raise _fail(f"Line {n} is outside the note.")
        lines[n - 1:n - 1] = _norm_content(op["content"], nl)
        return nl.join(lines)
    if kind == "append":
        return append_text(text, op["content"], inline=op.get("inline", False))
    if kind == "prepend":
        return prepend_text(text, op["content"], inline=op.get("inline", False))
    raise _fail(f"Unknown patch op {kind!r}.")


def append_text(text: str, content: str, inline: bool = False) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    content = content.replace("\r\n", "\n").replace("\n", nl)
    if inline or not text:
        return text + content
    return text + ("" if text.endswith(nl) else nl) + content


def prepend_text(text: str, content: str, inline: bool = False) -> str:
    """Insert after frontmatter, like the Obsidian CLI `prepend` command."""
    nl = "\r\n" if "\r\n" in text else "\n"
    content = content.replace("\r\n", "\n").replace("\n", nl)
    _, off = md.split_frontmatter(text)
    sep = "" if inline or content.endswith(nl) else nl
    return text[:off] + content + sep + text[off:]


# ---------------------------------------------------------------------------------------
# Link rewriting


def _md_url(path: str) -> str:
    return quote(path, safe="/#^-_.~!$&'()*+,;=@")


def format_link(link: md.Link, new_target: str, source: str, index: VaultIndex) -> str:
    """Rebuild one link so it points at ``new_target`` (vault path), keeping its style."""
    subpath = link.subpath or ""
    bang = "!" if link.embed else ""
    if link.kind in ("wikilink", "frontmatter"):
        is_md = new_target.lower().endswith(".md")
        name = posixpath.basename(new_target)
        short = name[:-3] if is_md else name
        had_path = "/" in link.target
        if not had_path and index.resolve(short, source) == new_target:
            target = short
        else:
            target = new_target[:-3] if is_md and not link.target.lower().endswith(".md") else new_target
        alias = f"|{link.alias}" if link.alias is not None else ""
        return f"{bang}[[{target}{subpath}{alias}]]"
    # Markdown link: keep relative style if the original was relative to the source folder.
    original = link.target
    base = posixpath.dirname(source)
    relative = original.startswith(("./", "../")) or (
        base and posixpath.normpath(posixpath.join(base, original)) in index.entries)
    target = posixpath.relpath(new_target, base or ".") if relative else new_target
    text = link.alias or ""
    return f"{bang}[{text}]({_md_url(target)}{_md_url(subpath) if subpath else ''})"


def plan_link_updates(index: VaultIndex, mapping: dict[str, str]) -> dict[str, str]:
    """Return {source_path_after_move: new_text} for every note whose links must change.

    ``mapping`` maps old vault paths to new ones. Must be computed *before* moving files.
    """
    updates: dict[str, str] = {}
    for src, links in index.link_graph().items():
        entry = index.entries[src]
        text = entry.text or ""
        new_src = mapping.get(src, src)
        edits: list[tuple[int, int, str]] = []
        fm_replacements: list[tuple[str, str]] = []
        for resolved, link in links:
            if resolved is None or not link.target:
                continue
            moved = resolved in mapping
            relative_md = link.kind == "markdown" and src in mapping and not link.target.startswith("/")
            if not moved and not relative_md:
                continue
            new_target = mapping.get(resolved, resolved)
            new_raw = format_link(link, new_target, new_src, index)
            if link.kind == "frontmatter":
                old_inner = link.raw.split(": ", 1)[1]
                new_inner = new_raw
                if old_inner != new_inner:
                    fm_replacements.append((old_inner, new_inner))
            elif new_raw != link.raw:
                edits.append((link.start, link.end, new_raw))
        if not edits and not fm_replacements:
            continue
        for start, end, new_raw in sorted(edits, reverse=True):
            text = text[:start] + new_raw + text[end:]
        if fm_replacements:
            _, off = md.split_frontmatter(text)
            head, body = text[:off], text[off:]
            for old, new in fm_replacements:
                head = head.replace(old, new)
            text = head + body
        updates[new_src] = text
    return updates


def retarget_links(index: VaultIndex, old_target: str, new_target: str) -> dict[str, str]:
    """Point every link to ``old_target`` at ``new_target`` (used by merge and link repair)."""
    updates: dict[str, str] = {}
    for src, links in index.link_graph().items():
        if src == old_target:
            continue
        text = index.entries[src].text or ""
        changed = False
        for resolved, link in sorted(links, key=lambda pair: pair[1].start, reverse=True):
            if resolved != old_target or link.kind == "frontmatter":
                continue
            text = text[:link.start] + format_link(link, new_target, src, index) + text[link.end:]
            changed = True
        if changed:
            updates[src] = text
    return updates


# ---------------------------------------------------------------------------------------
# Note composer


def merge_text(target_text: str, source_text: str, position: str = "end") -> str:
    """Add the source note's body to the target (Note composer 'Merge entire file')."""
    _, off = md.split_frontmatter(source_text)
    body = source_text[off:].strip("\r\n")
    if position == "start":
        return prepend_text(target_text, body + "\n")
    sep = "" if not target_text or target_text.endswith("\n\n") else ("\n" if target_text.endswith("\n") else "\n\n")
    return target_text + sep + body + "\n"


def extract_range(text: str, *, heading: str | None = None, start_line: int | None = None,
                  end_line: int | None = None, expected: str | None = None) -> tuple[str, int, int]:
    """Return (extracted_text, start_idx, end_idx_exclusive) as 0-based line indexes."""
    lines, _ = _split_lines(text)
    if heading:
        rng = md.section_range(text, heading)
        if rng is None:
            raise _fail(f"Heading {heading!r} not found.")
        s, _, e = rng
        while e > s + 1 and not lines[e - 1].strip():
            e -= 1
    elif start_line and end_line:
        if not 1 <= start_line <= end_line <= len(lines):
            raise _fail(f"Line range {start_line}-{end_line} is outside the note.")
        s, e = start_line - 1, end_line
    else:
        raise ObsidianError(Code.INVALID_ARGUMENT, "Pass either heading or start_line and end_line.")
    extracted = "\n".join(lines[s:e])
    if expected is not None and extracted != expected.replace("\r\n", "\n"):
        raise _fail("Selected lines do not match the expected text.", current=extracted[:500])
    return extracted, s, e


def replace_lines(text: str, s: int, e: int, replacement: str) -> str:
    lines, nl = _split_lines(text)
    lines[s:e] = _norm_content(replacement, nl)
    return nl.join(lines)
