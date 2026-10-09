"""Community plugin discovery and an extensible adapter registry.

Discovery reads the vault config folder (``community-plugins.json`` lists enabled IDs;
``plugins/<id>/manifest.json`` describes installed ones). Adapters declare only the
operations they really support, based on each plugin's documented syntax or API.
There is no universal compatibility: unknown plugins are listed with
``operations: []`` and can still be driven through their commands (CLI ``command``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .templates import moment_format
from .vault import Vault


@dataclass
class CommunityAdapter:
    plugin_id: str
    name: str
    docs: str
    operations: dict[str, str]  # operation -> how it works
    requires: list[str] = field(default_factory=list)


REGISTRY: dict[str, CommunityAdapter] = {}


def register(adapter: CommunityAdapter) -> None:
    REGISTRY[adapter.plugin_id] = adapter


register(CommunityAdapter(
    "dataview", "Dataview", "https://blacksmithgu.github.io/obsidian-dataview/",
    {"inline_fields": "Local parse of documented inline fields `key:: value` (works offline).",
     "dataview_query": "Runs a DQL query through the plugin's documented JS API "
                       "(app.plugins.plugins.dataview.api.queryMarkdown) via CLI eval."},
    requires=["dataview_query: Obsidian running, CLI, eval_code permission"]))
register(CommunityAdapter(
    "obsidian-tasks-plugin", "Tasks", "https://publish.obsidian.md/tasks/",
    {"tasks_metadata": "Local parse of the documented emoji format (due, scheduled, start, created, done, "
                       "cancelled, recurrence, priority, id, depends on)."}))
register(CommunityAdapter(
    "templater-obsidian", "Templater", "https://silentvoid13.github.io/Templater/",
    {"templater_settings": "Reads the templates folder from the plugin's settings.",
     "run_command": "Templater commands are interactive; run them with the CLI 'command' tool."},
    requires=["run_command: Obsidian running and ui_control permission"]))
register(CommunityAdapter(
    "periodic-notes", "Periodic Notes", "https://github.com/liamcain/obsidian-periodic-notes",
    {"periodic_path": "Computes weekly/monthly/quarterly/yearly note paths from the plugin's settings."}))


def installed(vault: Vault) -> list[dict[str, Any]]:
    enabled_raw = _read_json(vault, "community-plugins.json")
    enabled = set(enabled_raw if isinstance(enabled_raw, list) else [])
    plugins_dir = vault.root / vault.config_dir / "plugins"
    out = []
    if plugins_dir.is_dir():
        for d in sorted(plugins_dir.iterdir()):
            manifest = d / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = info.get("id", d.name)
            adapter = REGISTRY.get(pid)
            out.append({"id": pid, "name": info.get("name"), "version": info.get("version"),
                        "enabled": pid in enabled, "desktop_only": info.get("isDesktopOnly", False),
                        "adapter": adapter.name if adapter else None,
                        "operations": adapter.operations if adapter else {},
                        "requires": adapter.requires if adapter else []})
    return out


def plugin_settings(vault: Vault, plugin_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", plugin_id):
        return None
    path = vault.root / vault.config_dir / "plugins" / plugin_id / "data.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_json(vault: Vault, name: str) -> Any:
    try:
        return json.loads((vault.root / vault.config_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# Dataview inline fields ------------------------------------------------------------------
INLINE_FIELD_RE = re.compile(r"(?:^|[\[(])\s*([\w\s\-/\u0080-￿]+?)::\s*([^\])\n]*)")


def inline_fields(text: str) -> list[dict[str, Any]]:
    out = []
    for i, line in enumerate(text.split("\n")):
        for m in INLINE_FIELD_RE.finditer(line):
            key = m.group(1).strip()
            if key and not key.startswith("#"):
                out.append({"line": i + 1, "key": key, "value": m.group(2).strip()})
    return out


def dataview_eval_code(query: str) -> str:
    """JavaScript for CLI eval. The query is JSON-encoded, never spliced as code."""
    return ("(async()=>{const dv=app.plugins.plugins.dataview?.api;"
            "if(!dv)return JSON.stringify({error:'Dataview is not enabled'});"
            f"const r=await dv.queryMarkdown({json.dumps(query)});"
            "return JSON.stringify(r.successful?{markdown:r.value}:{error:r.error});})()")


# Tasks plugin emoji format -----------------------------------------------------------------
TASKS_FIELDS: dict[str, str] = {"📅": "due", "⏳": "scheduled", "🛫": "start", "➕": "created", "✅": "done",
                                "❌": "cancelled"}
PRIORITY = {"🔺": "highest", "⏫": "high", "🔼": "medium", "🔽": "low", "⏬": "lowest"}


def tasks_metadata(task_text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for emoji, key in TASKS_FIELDS.items():
        m = re.search(re.escape(emoji) + r"️?\s*(\d{4}-\d{2}-\d{2})", task_text)
        if m:
            meta[key] = m.group(1)
    for emoji, level in PRIORITY.items():
        if emoji in task_text:
            meta["priority"] = level
    if m := re.search(r"🔁️?\s*([^📅⏳🛫➕✅❌🔺⏫🔼🔽⏬🆔⛔]+)", task_text):
        meta["recurrence"] = m.group(1).strip()
    if m := re.search(r"🆔\s*([\w-]+)", task_text):
        meta["id"] = m.group(1)
    if m := re.search(r"⛔\s*([\w,\s-]+)", task_text):
        meta["depends_on"] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    return meta


# Periodic Notes ------------------------------------------------------------------------------
PERIOD_DEFAULTS = {"daily": "YYYY-MM-DD", "weekly": "gggg-[W]ww", "monthly": "YYYY-MM", "quarterly": "YYYY-[Q]Q",
                   "yearly": "YYYY"}


def periodic_path(vault: Vault, period: str, date: datetime) -> dict[str, Any]:
    settings = plugin_settings(vault, "periodic-notes") or {}
    conf = settings.get(period) or {}
    fmt = conf.get("format") or PERIOD_DEFAULTS[period]
    folder = (conf.get("folder") or "").strip("/")
    name = moment_format(date, fmt)
    return {"period": period, "path": f"{folder}/{name}.md" if folder else f"{name}.md",
            "enabled": bool(conf.get("enabled")), "template": conf.get("templatePath"),
            "source": "plugin settings" if conf else "defaults"}


ADAPTER_FUNCS: dict[str, Callable[..., Any]] = {"inline_fields": inline_fields, "tasks_metadata": tasks_metadata}
