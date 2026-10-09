from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from obsidian_mcp.config import ConfigStore
from obsidian_mcp.service import ObsidianService

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses /bin/sh fakes, symlinks or chmod")

NOTES = {
    "Home.md": "---\ntags: [hub]\naliases: [Start]\n---\n# Home\nSee [[Projects/Plan]] and [[Ideas|my ideas]].\n"
               "Also [rel](Projects/Plan.md#Goals) and ![[pic.png]].\n",
    "Ideas.md": "# Ideas\n- [ ] write docs ^task1\n- [x] ship 📅 2026-01-02 ⏫\nMentions Plan here.\n",
    "Projects/Plan.md": "# Plan\n## Goals\nGoal text\n## Notes\nBack to [[Home]].\n",
    "Projects/Other.md": "# Other\nLink to [[Missing Note]].\n",
    "Unicode/ગુજરાતી.md": "# નમસ્તે\nહિન્દી: नमस्ते #ટૅગ/ઉપ\n",
    "CRLF.md": "line one\r\nline two\r\n",
    "Orphan.md": "nobody links here\n",
}


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    root = tmp_path / "TestVault"
    for rel, text in NOTES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "daily-notes.json").write_text('{"format":"YYYY-MM-DD","folder":"Daily"}')
    return root


@pytest.fixture()
def service(tmp_path: Path, vault_dir: Path) -> ObsidianService:
    home = tmp_path / "home"
    os.environ.pop("OBSIDIAN_MCP_BRIDGE_TOKEN", None)
    store = ConfigStore(home)
    store.config.discover_obsidian_vaults = False
    store.config.cli_path = str(tmp_path / "no-such-obsidian")
    svc = ObsidianService(store)
    svc.connect_vault(str(vault_dir), name="Test")
    return svc
