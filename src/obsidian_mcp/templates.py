"""Core Templates, Daily notes, and Unique note creator semantics.

Implements the documented template variables ``{{title}}``, ``{{date}}``, ``{{time}}`` and
``{{date:FORMAT}}`` / ``{{time:FORMAT}}`` with Moment.js format tokens, plus Note
composer's ``{{content}}``, ``{{fromTitle}}``, ``{{newTitle}}``. Plugin settings are read
from the vault config folder when present; that file layout is undocumented, so every
value falls back to the documented default.

Advanced templating (Templater, Periodic Notes) is handled by optional community adapters.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .vault import Vault

# Longest tokens first so "YYYY" wins over "YY".
_TOKENS = [
    "YYYY", "YY", "MMMM", "MMM", "MM", "Mo", "M", "DDDD", "DDD", "DD", "Do", "D", "dddd", "ddd", "dd", "d",
    "HH", "H", "hh", "h", "kk", "k", "mm", "m", "ss", "s", "SSS", "A", "a", "Q", "WW", "W", "ww", "w",
    "GGGG", "gggg", "X", "x", "ZZ", "Z", "E", "e",
]
_TOKEN_RE = re.compile(r"\[([^\]]*)\]|" + "|".join(_TOKENS))
VAR_RE = re.compile(r"{{\s*(\w+)(?::([^}]*))?\s*}}")


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")


def moment_format(dt: datetime, fmt: str) -> str:
    """Format a datetime with Moment.js tokens (English locale)."""

    def repl(m: re.Match[str]) -> str:
        if m.group(1) is not None:
            return m.group(1)
        t = m.group(0)
        iso_year, iso_week, iso_wd = dt.isocalendar()
        week_sun = int(dt.strftime("%U")) + (0 if dt.replace(month=1, day=1).weekday() == 6 else 1)
        values: dict[str, str] = {
            "YYYY": f"{dt.year:04d}", "YY": f"{dt.year % 100:02d}",
            "MMMM": dt.strftime("%B"), "MMM": dt.strftime("%b"), "MM": f"{dt.month:02d}",
            "Mo": _ordinal(dt.month), "M": str(dt.month),
            "DDDD": f"{dt.timetuple().tm_yday:03d}", "DDD": str(dt.timetuple().tm_yday),
            "DD": f"{dt.day:02d}", "Do": _ordinal(dt.day), "D": str(dt.day),
            "dddd": dt.strftime("%A"), "ddd": dt.strftime("%a"), "dd": dt.strftime("%a")[:2],
            "d": str((dt.weekday() + 1) % 7), "e": str((dt.weekday() + 1) % 7), "E": str(iso_wd),
            "HH": f"{dt.hour:02d}", "H": str(dt.hour),
            "hh": f"{(dt.hour % 12) or 12:02d}", "h": str((dt.hour % 12) or 12),
            "kk": f"{dt.hour or 24:02d}", "k": str(dt.hour or 24),
            "mm": f"{dt.minute:02d}", "m": str(dt.minute), "ss": f"{dt.second:02d}", "s": str(dt.second),
            "SSS": f"{dt.microsecond // 1000:03d}",
            "A": "AM" if dt.hour < 12 else "PM", "a": "am" if dt.hour < 12 else "pm",
            "Q": str((dt.month - 1) // 3 + 1),
            "WW": f"{iso_week:02d}", "W": str(iso_week), "GGGG": f"{iso_year:04d}",
            "ww": f"{week_sun:02d}", "w": str(week_sun), "gggg": f"{dt.year:04d}",
            "X": str(int(dt.timestamp())), "x": str(int(dt.timestamp() * 1000)),
            "Z": dt.strftime("%z")[:3] + ":" + dt.strftime("%z")[3:] if dt.tzinfo else "+00:00",
            "ZZ": dt.strftime("%z") if dt.tzinfo else "+0000",
        }
        return values[t]

    return _TOKEN_RE.sub(repl, fmt)


def render(template: str, *, title: str = "", now: datetime | None = None, date_format: str = "YYYY-MM-DD",
           time_format: str = "HH:mm", extra: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Resolve template variables. Returns (text, unknown_variables_left_untouched)."""
    now = now or datetime.now()
    unknown: list[str] = []
    extra = extra or {}

    def repl(m: re.Match[str]) -> str:
        name, fmt = m.group(1), m.group(2)
        if name == "title":
            return title
        if name == "date":
            return moment_format(now, fmt.strip() if fmt else date_format)
        if name == "time":
            return moment_format(now, fmt.strip() if fmt else time_format)
        if name in extra:
            return extra[name]
        unknown.append(m.group(0))
        return m.group(0)

    return VAR_RE.sub(repl, template), unknown


def templates_settings(vault: Vault) -> dict[str, Any]:
    cfg = vault.read_config_json("templates.json") or {}
    return {"folder": cfg.get("folder") or None, "date_format": cfg.get("dateFormat") or "YYYY-MM-DD",
            "time_format": cfg.get("timeFormat") or "HH:mm", "source": "config" if cfg else "defaults"}


def daily_settings(vault: Vault) -> dict[str, Any]:
    cfg = vault.read_config_json("daily-notes.json") or {}
    return {"format": cfg.get("format") or "YYYY-MM-DD", "folder": (cfg.get("folder") or "").strip("/"),
            "template": cfg.get("template") or None, "source": "config" if cfg else "defaults"}


def unique_settings(vault: Vault) -> dict[str, Any]:
    cfg = vault.read_config_json("zk-prefixer.json") or {}
    return {"format": cfg.get("format") or "YYYYMMDDHHmm", "folder": (cfg.get("folder") or "").strip("/"),
            "template": cfg.get("template") or None, "source": "config" if cfg else "defaults (assumed)"}


def note_composer_settings(vault: Vault) -> dict[str, Any]:
    cfg = vault.read_config_json("note-composer.json") or {}
    return {"template": cfg.get("template") or None,
            "replace_text": cfg.get("replacementText") or "link",
            "source": "config" if cfg else "defaults"}


def daily_path(vault: Vault, date: datetime) -> str:
    s = daily_settings(vault)
    name = moment_format(date, s["format"])
    return f"{s['folder']}/{name}.md" if s["folder"] else f"{name}.md"


def template_path(vault: Vault, name: str) -> str:
    """Resolve a template name to a path inside the configured templates folder."""
    folder = templates_settings(vault)["folder"]
    name = name.removesuffix(".md")
    candidates = [f"{folder.strip('/')}/{name}.md"] if folder else []
    candidates.append(f"{name}.md")
    for c in candidates:
        if vault.exists(c):
            return c
    return candidates[0]
