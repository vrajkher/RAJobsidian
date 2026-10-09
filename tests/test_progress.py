"""Progress notifications and cancellation for long-running tools."""

from __future__ import annotations

import os
import time

import anyio
import pytest
from conftest import posix_only
from mcp import Client

from obsidian_mcp.server import build_server


async def test_batch_edit_reports_progress(service):
    events = []

    async def on_progress(progress, total, message):
        events.append((progress, total, message))

    ops = [{"action": "append_note", "path": "Ideas.md", "content": f"line {i}"} for i in range(3)]
    async with Client(build_server(service)) as client:
        out = await client.call_tool("batch_edit", {"operations": ops, "dry_run": False},
                                     progress_callback=on_progress)
    assert out.structured_content["status"] == "ok"
    assert [e[0] for e in events] == [0, 1, 2, 3] and all(e[1] == 3 for e in events)
    assert events[0][2] == "append_note Ideas.md" and events[-1][2] == "done"


def test_batch_cancellation_rolls_back(service, vault_dir):
    before = (vault_dir / "Ideas.md").read_bytes()
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2  # cancel before the third item

    ops = [{"action": "append_note", "path": "Ideas.md", "content": f"line {i}"} for i in range(3)]
    out = service.batch(None, ops, dry_run=False, cancelled=cancelled)
    assert out["status"] == "cancelled" and out["completed"] == 2 and len(out["rolled_back"]) == 2
    assert (vault_dir / "Ideas.md").read_bytes() == before


@posix_only
async def test_headless_streams_output_as_progress(service, tmp_path):
    fake = tmp_path / "ob"
    fake.write_text("#!/bin/sh\necho 'Uploading 1/2 Notes.md'\nsleep 0.3\necho 'Uploading 2/2 Plan.md'\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    service.config.headless_path = str(fake)
    service.config.permissions.headless = True
    messages = []

    async def on_progress(progress, total, message):
        messages.append(message)

    async with Client(build_server(service)) as client:
        out = await client.call_tool("headless", {"command": "sync-list-remote"}, progress_callback=on_progress)
    assert "Uploading 2/2" in out.structured_content["output"]
    assert messages == ["Uploading 1/2 Notes.md", "Uploading 2/2 Plan.md"]


@posix_only
async def test_cancelled_headless_run_kills_process(service, tmp_path):
    pid_file = tmp_path / "pid"
    fake = tmp_path / "ob"
    fake.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 30\n", encoding="utf-8")
    fake.chmod(0o755)
    service.config.headless_path = str(fake)
    service.config.permissions.headless = True
    async with Client(build_server(service)) as client:
        with anyio.move_on_after(1.5):
            await client.call_tool("headless", {"command": "sync-list-remote"})
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await anyio.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("ob process kept running after the request was cancelled")
