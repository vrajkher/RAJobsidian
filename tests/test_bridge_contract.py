"""Contract test: the built bridge plugin (mocked Obsidian runtime) against the Python BridgeAdapter.

Proves auth, routing, and payload shapes match. It does NOT prove behaviour inside a real
Obsidian app; that remains an unverified manual check (see docs/VERIFICATION.md).
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from obsidian_mcp.adapters.bridge import BridgeAdapter
from obsidian_mcp.errors import Code, ObsidianError

ROOT = Path(__file__).resolve().parents[1] / "bridge-plugin"
pytestmark = pytest.mark.skipif(not (ROOT / "main.js").exists() or not shutil.which("node"),
                                reason="bridge-plugin not built or node missing")


@pytest.fixture()
def bridge():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = "t" * 64
    proc = subprocess.Popen(["node", str(ROOT / "test" / "harness.cjs")], stdout=subprocess.PIPE,
                            env={**os.environ, "PORT": str(port), "TOKEN": token})
    assert proc.stdout.readline().strip() == b"READY"
    yield BridgeAdapter(f"http://127.0.0.1:{port}", token), port
    proc.terminate()
    proc.wait()


def test_status_active_and_editor(bridge):
    b, _ = bridge
    status = b.status()
    assert status["vault"] == "MockVault" and "editor" in status["features"]
    active = b.call("GET", "/active")
    assert active["file"] == "Note.md" and active["selection"] == "line"
    out = b.call("POST", "/editor", {"action": "replace_selection", "text": "LINE", "expected": "line"})
    assert out["selection"] == "LINE"
    with pytest.raises(ObsidianError) as exc:
        b.call("POST", "/editor", {"action": "replace_selection", "text": "x", "expected": "stale"})
    assert "409" in exc.value.message


def test_workspace_rename_trash_events(bridge):
    b, _ = bridge
    ws = b.call("GET", "/workspace")
    assert ws["leaves"][0]["file"] == "Note.md"
    assert b.call("POST", "/workspace/open", {"path": "Note.md", "line": 3})["opened"] == "Note.md"
    assert b.call("POST", "/file/rename", {"path": "Note.md", "newPath": "New/Name.md"})["to"] == "New/Name.md"
    events = b.call("GET", "/events", query={"since": 0, "timeout": 1})
    assert any(e["type"] == "rename" and e["oldPath"] == "Note.md" for e in events["events"])
    assert b.call("POST", "/link", {"path": "New/Name.md", "source": "x.md", "alias": "A"})["link"] == "[[New/Name|A]]"
    assert b.call("POST", "/file/trash", {"path": "New/Name.md"})["trashed"] == "New/Name.md"
    with pytest.raises(ObsidianError):
        b.call("POST", "/file/trash", {"path": "New/Name.md"})


def test_rejects_bad_token_origin_and_host(bridge):
    b, port = bridge
    with pytest.raises(ObsidianError) as exc:
        BridgeAdapter(b.url, "wrong").call("GET", "/status")
    assert exc.value.code == Code.PERMISSION_DENIED
    for headers in ({"Authorization": f"Bearer {b.token}", "Origin": "https://evil.example"},
                    {"Authorization": f"Bearer {b.token}", "Host": "evil.example"}):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/status", headers=headers)
        assert conn.getresponse().status == 401
        conn.close()
