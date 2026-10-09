"""Generate src/obsidian_mcp/data/cli_catalog.json from the official Obsidian CLI help page.

Source: https://github.com/obsidianmd/obsidian-help  en/Extending Obsidian/Obsidian CLI.md
Usage:  python scripts/gen_cli_catalog.py <path-to-obsidian-help-checkout>
The catalog is the allow-list used to validate every CLI invocation; never hand-edit it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

DOC = Path("en") / "Extending Obsidian" / "Obsidian CLI.md"
OUT = Path(__file__).resolve().parents[1] / "src" / "obsidian_mcp" / "data" / "cli_catalog.json"
CMD_HEADING = re.compile(r"^### `([a-z:-]+)`\s*$")
SECTION = re.compile(r"^## (.+?)\s*$")
PARAM = re.compile(r'^([A-Za-z][A-Za-z0-9_-]*)(?:=(\S+))?\s+#\s*(.*)$')


def parse(text: str) -> list[dict]:
    commands: list[dict] = []
    section = ""
    current: dict | None = None
    in_block = False
    block_done = False
    for line in text.splitlines():
        if not in_block:
            if m := SECTION.match(line):
                section = m.group(1)
                current = None
                continue
            if m := CMD_HEADING.match(line):
                current = {"name": m.group(1), "section": section, "description": "",
                           "params": [], "flags": []}
                commands.append(current)
                block_done = False
                continue
        if current is None:
            continue
        if line.startswith("```"):
            if in_block:
                in_block = False
                block_done = True
            elif not block_done and line.strip() in ("```bash", "```"):
                in_block = True
            continue
        if in_block:
            m = PARAM.match(line.strip())
            if not m:
                continue
            name, value, desc = m.groups()
            required = "(required)" in desc
            desc = desc.replace("(required)", "").strip()
            if value is None:
                current["flags"].append({"name": name, "description": desc})
            else:
                value = value.strip('"')
                choices = None
                if not value.startswith("<"):
                    choices = value.split("|")
                current["params"].append({"name": name, "placeholder": value, "choices": choices,
                                          "required": required, "description": desc})
        elif not current["description"] and line.strip() and not line.startswith(("**", ">", "|")):
            current["description"] = line.strip()
    return commands


def main() -> None:
    root = Path(sys.argv[1])
    text = (root / DOC).read_text(encoding="utf-8")
    commit = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H %cI"],
                            capture_output=True, text=True, check=False).stdout.split()
    commands = parse(text)
    payload = {
        "source": "https://github.com/obsidianmd/obsidian-help/blob/master/en/Extending%20Obsidian/Obsidian%20CLI.md",
        "source_commit": commit[0] if commit else None,
        "source_date": commit[1] if len(commit) > 1 else None,
        "minimum_installer": "1.12.7",
        "global_params": ["vault"],
        "global_flags": ["--copy"],
        "commands": commands,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(commands)} commands to {OUT}")


if __name__ == "__main__":
    main()
