"""Obsidian-flavoured Markdown parsing.

Implements the syntax documented at https://help.obsidian.md (Basic/Advanced formatting
syntax, Internal links, Embed files, Tags, Properties, Footnotes, Callouts, Math):
frontmatter, headings, block IDs, wikilinks, Markdown links, embeds, tags (including
nested tags and non-Latin scripts), tasks, footnotes, callouts. Content inside fenced
code, inline code, math, ``%%comments%%`` and HTML comments is ignored for links and tags.

Parsing never modifies text. Frontmatter edits use ruamel.yaml round-trip mode so key
order, comments and quoting survive; the body is preserved byte for byte.
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import unquote

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from .errors import Code, ObsidianError

FM_RE = re.compile(r"\A(?:\ufeff)?---[ \t]*\r?\n(.*?)(?:\r?\n)?^---[ \t]*(?:\r?\n|\Z)", re.S | re.M)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9-]+)[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[((?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
# Tag characters: anything except whitespace and ASCII punctuation other than - _ /.
# A negated class keeps combining marks (e.g. Gujarati/Devanagari vowel signs) inside tags.
TAG_CHAR = r"[^\s!-,.:-@\[-^`{-~]"
TAG_RE = re.compile(r"(?:(?<=^)|(?<=[\s(\[{,;:'\"]))#(" + TAG_CHAR + r"+)")
TASK_RE = re.compile(r"^([ \t]*)([-*+]|\d+[.)])[ \t]+\[(.)\][ \t]?(.*)$")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:[ \t]?(.*)$")
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
INLINE_FOOTNOTE_RE = re.compile(r"\^\[([^\]]+)\]")
CALLOUT_RE = re.compile(r"^[ \t]*>[ \t]*\[!([^\]\s]+)\]([+-]?)[ \t]*(.*)$")
URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
WORD_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"  # CJK counted per character
    r"|[^\s\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+"
)


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.allow_unicode = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


@dataclass
class Link:
    target: str  # link path as written (decoded), without subpath
    subpath: str | None  # "#Heading" or "#^block"
    alias: str | None
    embed: bool
    kind: str  # "wikilink" | "markdown" | "frontmatter"
    line: int  # 0-based
    start: int  # absolute character offsets of the whole link in the text
    end: int
    raw: str


@dataclass
class Heading:
    level: int
    text: str
    line: int


@dataclass
class Task:
    line: int
    status: str
    text: str
    indent: str
    marker: str

    @property
    def done(self) -> bool:
        return self.status not in (" ",)


@dataclass
class Callout:
    type: str
    fold: str
    title: str
    line: int


@dataclass
class ParsedNote:
    frontmatter: dict[str, Any]
    frontmatter_error: str | None
    body_offset: int  # character offset where body starts
    body_line: int  # 0-based line where body starts
    headings: list[Heading] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # with '#', in order of appearance, unique
    inline_tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    block_ids: dict[str, int] = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)
    footnote_refs: list[str] = field(default_factory=list)
    footnote_defs: dict[str, str] = field(default_factory=dict)
    inline_footnotes: list[str] = field(default_factory=list)
    callouts: list[Callout] = field(default_factory=list)
    has_math: bool = False
    code_blocks: list[dict[str, Any]] = field(default_factory=list)
    word_count: int = 0
    char_count: int = 0

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d["frontmatter"] = to_plain(self.frontmatter)
        return d


def to_plain(value: Any) -> Any:
    """Convert ruamel containers/scalars to plain JSON-friendly Python values."""
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def split_frontmatter(text: str) -> tuple[str | None, int]:
    """Return (frontmatter_yaml, body_offset)."""
    m = FM_RE.match(text)
    if not m:
        return None, 0
    return m.group(1), m.end()


def load_frontmatter(text: str) -> tuple[Any, str | None]:
    raw, _ = split_frontmatter(text)
    if raw is None:
        return {}, None
    if not raw.strip():
        return {}, None
    try:
        data = _yaml().load(raw)
    except YAMLError as exc:
        return {}, f"Invalid YAML frontmatter: {str(exc).splitlines()[0]}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, "Frontmatter is not a key/value mapping."
    return data, None


def mask_non_content(text: str) -> str:
    """Replace code, math, and comments with spaces (newlines kept) so offsets stay valid."""
    out = list(text)

    def blank(a: int, b: int) -> None:
        for i in range(a, b):
            if out[i] != "\n":
                out[i] = " "

    # Fenced code blocks.
    pos = 0
    lines = text.splitlines(keepends=True)
    fence: str | None = None
    fence_start = 0
    for line in lines:
        m = FENCE_RE.match(line)
        if fence is None and m:
            fence, fence_start = m.group(1), pos
        elif fence is not None and m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                and not line.strip()[len(m.group(1)):].strip():
            blank(fence_start, pos + len(line))
            fence = None
        pos += len(line)
    if fence is not None:
        blank(fence_start, len(text))
    masked = "".join(out)
    for pattern in (r"\$\$.*?\$\$", r"%%.*?%%", r"<!--.*?-->"):
        for m in re.finditer(pattern, masked, re.S):
            blank(m.start(), m.end())
    masked = "".join(out)
    for m in re.finditer(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", masked):
        blank(m.start(), m.end())
    masked = "".join(out)
    for m in re.finditer(r"(?<![\\$])\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)", masked):
        blank(m.start(), m.end())
    return "".join(out)


def parse_wikilink_inner(inner: str) -> tuple[str, str | None, str | None]:
    alias = None
    # Inside tables the pipe is escaped as "\|".
    m = re.search(r"\\?\|", inner)
    if m:
        alias = inner[m.end():]
        inner = inner[: m.start()]
    target, subpath = inner, None
    if "#" in inner:
        idx = inner.index("#")
        target, subpath = inner[:idx], inner[idx:]
    return target.strip(), subpath, alias


def is_external(url: str) -> bool:
    return bool(URL_SCHEME_RE.match(url)) and not url.lower().startswith("obsidian:")


def parse(text: str) -> ParsedNote:
    fm_raw, body_offset = split_frontmatter(text)
    fm, fm_error = load_frontmatter(text) if fm_raw is not None else ({}, None)
    body_line = text.count("\n", 0, body_offset)
    note = ParsedNote(frontmatter=fm, frontmatter_error=fm_error, body_offset=body_offset,
                      body_line=body_line)

    masked = mask_non_content(text)
    # Never parse links/tags inside the frontmatter block textually; handle via values.
    masked = re.sub(r"[^\n]", " ", masked[:body_offset]) + masked[body_offset:]
    line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

    def line_of(offset: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    # Line-based structures (use masked text to skip code blocks).
    masked_lines = masked.split("\n")
    raw_lines = text.split("\n")
    for i, mline in enumerate(masked_lines):
        if i < body_line:
            continue
        raw = raw_lines[i].rstrip("\r")
        if not mline.strip():
            continue
        if hm := HEADING_RE.match(mline.rstrip("\r")):
            note.headings.append(Heading(len(hm.group(1)), raw[len(hm.group(1)):].strip().rstrip("#").strip(), i))
        if bm := BLOCK_ID_RE.search(mline.rstrip("\r")):
            note.block_ids[bm.group(1)] = i
        if tm := TASK_RE.match(raw):
            note.tasks.append(Task(i, tm.group(3), tm.group(4), tm.group(1), tm.group(2)))
        if fd := FOOTNOTE_DEF_RE.match(mline.rstrip("\r")):
            note.footnote_defs[fd.group(1)] = raw[raw.find("]:") + 2:].strip()
        if cm := CALLOUT_RE.match(mline.rstrip("\r")):
            note.callouts.append(Callout(cm.group(1).lower(), cm.group(2), cm.group(3).strip(), i))

    for m in WIKILINK_RE.finditer(masked):
        inner = text[m.start(2): m.end(2)]
        target, subpath, alias = parse_wikilink_inner(inner)
        note.links.append(Link(target, subpath, alias, bool(m.group(1)), "wikilink",
                               line_of(m.start()), m.start(), m.end(), text[m.start(): m.end()]))
    for m in MDLINK_RE.finditer(masked):
        url = text[m.start(3): m.end(3)].strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        if is_external(url):
            continue
        decoded = unquote(url)
        target, subpath = decoded, None
        if "#" in decoded:
            idx = decoded.index("#")
            target, subpath = decoded[:idx], decoded[idx:]
        note.links.append(Link(target, subpath, text[m.start(2): m.end(2)] or None, bool(m.group(1)),
                               "markdown", line_of(m.start()), m.start(), m.end(),
                               text[m.start(): m.end()]))
    note.links.sort(key=lambda link: link.start)

    seen: set[str] = set()
    tag_text = list(masked)
    for link in note.links:
        for i in range(link.start, link.end):
            tag_text[i] = " "
    for m in TAG_RE.finditer("".join(tag_text)):
        name = m.group(1).rstrip("/")
        if not name or name.replace("/", "").isdigit() or "#" in name:
            continue
        tag = "#" + name
        note.inline_tags.append(tag)
    for m in FOOTNOTE_REF_RE.finditer(masked):
        if m.start() != line_starts[line_of(m.start())]:  # a definition starts its line
            note.footnote_refs.append(m.group(1))
    note.inline_footnotes = [m.group(1) for m in INLINE_FOOTNOTE_RE.finditer(masked)]
    note.has_math = bool(re.search(r"\$\$.+?\$\$|(?<![\\$])\$\S[^$\n]*?\S?\$", text[body_offset:], re.S))

    # Fenced code block languages (from raw text).
    fence = None
    for i, raw in enumerate(raw_lines[body_line:], start=body_line):
        fm2 = FENCE_RE.match(raw)
        if fence is None and fm2:
            fence = (fm2.group(1), raw.strip()[len(fm2.group(1)):].strip(), i)
        elif fence is not None and fm2 and fm2.group(1)[0] == fence[0][0]:
            note.code_blocks.append({"language": fence[1] or None, "start_line": fence[2], "end_line": i})
            fence = None

    # Frontmatter-derived tags, aliases, and links.
    fm_tags = _as_list(fm.get("tags", fm.get("tag")))
    for t in fm_tags:
        for part in re.split(r"[,\s]+", str(t)):
            part = part.strip().lstrip("#")
            if part:
                tag = "#" + part
                if tag.lower() not in seen:
                    seen.add(tag.lower())
                    note.tags.append(tag)
    for tag in note.inline_tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            note.tags.append(tag)
    note.aliases = [str(a) for a in _as_list(fm.get("aliases", fm.get("alias"))) if str(a).strip()]
    for key, value in fm.items():
        for item in _iter_strings(value):
            for m in WIKILINK_RE.finditer(item):
                target, subpath, alias = parse_wikilink_inner(m.group(2))
                note.links.append(Link(target, subpath, alias, bool(m.group(1)), "frontmatter", 0, -1, -1,
                                       f"{key}: {m.group(0)}"))

    body_masked = masked[body_offset:]
    note.word_count = len(WORD_RE.findall(re.sub(r"[#>*_`~=\-|\[\]()!]+", " ", body_masked)))
    note.char_count = len(text) - body_offset
    return note


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [v for v in re.split(r",", value)] if "," in value else [value]
    return [value]


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


# ---------------------------------------------------------------------------------------
# Frontmatter editing


def dump_frontmatter(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def update_frontmatter(text: str, set_values: dict[str, Any] | None = None,
                       remove: list[str] | None = None) -> str:
    """Set/remove properties, keeping body bytes, key order, comments, and line endings."""
    newline = "\r\n" if "\r\n" in text else "\n"
    raw, body_offset = split_frontmatter(text)
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    if raw is None:
        data: Any = CommentedMap()
        body = text[len(bom):]
    else:
        try:
            data = _yaml().load(raw) if raw.strip() else None
        except YAMLError as exc:
            raise ObsidianError(Code.PARSE_ERROR, f"Frontmatter is not valid YAML: {exc}",
                                hint="Fix the YAML by hand before editing properties.") from exc
        if data is None:
            data = CommentedMap()
        if not isinstance(data, dict):
            raise ObsidianError(Code.PARSE_ERROR, "Frontmatter is not a key/value mapping.")
        body = text[body_offset:]
    for key, value in (set_values or {}).items():
        data[key] = value
    for key in remove or []:
        if key in data:
            del data[key]
    if not data:
        return bom + body
    dumped = dump_frontmatter(data).rstrip("\n")
    if newline == "\r\n":
        dumped = dumped.replace("\r\n", "\n").replace("\n", "\r\n")
    head = f"{bom}---{newline}{dumped}{newline}---{newline}"
    return head + body


# ---------------------------------------------------------------------------------------
# Sections


def section_range(text: str, heading: str) -> tuple[int, int, int] | None:
    """Return (heading_line, content_start_line, end_line_exclusive) for a heading.

    ``heading`` may be ``"Title"`` or a nested path ``"Parent#Child"``. Matching is
    case-insensitive and ignores surrounding whitespace.
    """
    note = parse(text)
    parts = [p.strip().lower() for p in heading.lstrip("#").split("#") if p.strip()]
    lines = text.split("\n")
    candidates = note.headings
    chosen: Heading | None = None
    scope_end = len(lines)
    scope_start = -1
    level_floor = 0
    for depth, part in enumerate(parts):
        chosen = None
        for h in candidates:
            if h.line <= scope_start or h.line >= scope_end or h.level <= level_floor and depth:
                continue
            if h.text.strip().lower() == part:
                chosen = h
                break
        if chosen is None:
            return None
        scope_start = chosen.line
        level_floor = chosen.level
        scope_end = next((h.line for h in note.headings if h.line > chosen.line and h.level <= chosen.level),
                         scope_end)
    assert chosen is not None
    return chosen.line, chosen.line + 1, scope_end
