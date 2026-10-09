"""Obsidian Headless (``ob``, npm package ``obsidian-headless``, open beta) adapter.

Covers the documented Sync and Publish commands. Credentials never pass through this
server: ``ob login`` is interactive and must be run by the user. Passwords are never
accepted as tool arguments. The docs warn against using desktop Sync and Headless Sync
on the same device; ``sync`` refuses to run when the vault has the desktop Sync plugin
enabled, unless the caller explicitly overrides.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import Code, ObsidianError

# Documented commands -> allowed options (value options end with '=').
COMMANDS: dict[str, set[str]] = {
    "sync-list-remote": set(),
    "sync-list-local": set(),
    "sync-status": {"--path="},
    "sync": {"--path=", "--continuous"},
    "sync-config": {"--path=", "--mode=", "--conflict-strategy=", "--file-types=", "--configs=",
                    "--excluded-folders=", "--device-name=", "--config-dir="},
    "sync-setup": {"--vault=", "--path=", "--device-name=", "--config-dir="},
    "sync-unlink": {"--path="},
    "publish-list-sites": set(),
    "publish-setup": {"--site=", "--path="},
    "publish": {"--path=", "--all", "--dry-run", "--yes"},
    "publish-config": {"--path=", "--includes=", "--excludes="},
    "publish-site-options": {"--path="},
    "publish-unlink": {"--path="},
}
FORBIDDEN_OPTIONS = {"--password", "--email", "--mfa"}


def find_binary(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    return shutil.which("ob")


class HeadlessAdapter:
    def __init__(self, binary: str | None, timeout: float = 300.0) -> None:
        self.binary = binary
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.binary is not None

    def run(self, command: str, options: dict[str, Any] | None = None, flags: list[str] | None = None,
            timeout: float | None = None, on_line: Callable[[int, str], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        if not self.available:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, "Obsidian Headless ('ob') was not found.",
                                hint="Install Node.js 22+, run 'npm install -g obsidian-headless', "
                                     "then 'ob login' in your own terminal.")
        allowed = COMMANDS.get(command)
        if allowed is None:
            raise ObsidianError(Code.UNSUPPORTED, f"'ob {command}' is not a supported documented command.")
        if command == "sync" and "continuous" in [f.lstrip("-") for f in flags or []]:
            raise ObsidianError(Code.UNSUPPORTED, "Continuous sync is a long-running process; run it yourself.")
        argv = [self.binary, command]
        for key, value in (options or {}).items():
            opt = "--" + key.lstrip("-").replace("_", "-")
            if opt in FORBIDDEN_OPTIONS:
                raise ObsidianError(Code.PERMISSION_DENIED, "Credentials are never passed through this server.",
                                    hint="Run 'ob login' yourself in a terminal.")
            if opt + "=" not in allowed:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"'ob {command}' has no option {opt}.")
            if value is not None:
                argv += [opt, str(value)]
        for flag in flags or []:
            opt = "--" + flag.lstrip("-")
            if opt not in allowed:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"'ob {command}' has no flag {opt}.")
            argv.append(opt)
        out, err, returncode = _stream(argv, timeout or self.timeout, on_line, cancelled, command)
        if returncode != 0:
            hint = "Run 'ob login' in your terminal." if "login" in (out + err).lower() else None
            raise ObsidianError(Code.HEADLESS_ERROR, (err.strip() or out.strip() or "failed")[:2000], hint=hint)
        return {"command": f"ob {command}", "output": out.strip(), "stderr": err.strip() or None}


MAX_OUT = 200_000


def _stream(argv: list[str], timeout: float, on_line: Callable[[int, str], None] | None,
            cancelled: Callable[[], bool] | None, command: str) -> tuple[str, str, int]:
    """Run ``ob``, forwarding stdout lines as they arrive; kill it on timeout or cancellation."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def pump(name: str, stream: Any) -> None:
        for raw in iter(stream.readline, b""):
            lines.put((name, raw.decode("utf-8", errors="replace")))
        lines.put((name, None))

    for name, stream in (("out", proc.stdout), ("err", proc.stderr)):
        threading.Thread(target=pump, args=(name, stream), daemon=True).start()
    out: list[str] = []
    err: list[str] = []
    open_streams = 2
    deadline = time.monotonic() + timeout
    count = 0
    try:
        while open_streams:
            if cancelled and cancelled():
                proc.kill()
                proc.wait()
                raise ObsidianError(Code.TIMEOUT, f"'ob {command}' was cancelled; the process was stopped.")
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise ObsidianError(Code.TIMEOUT, f"'ob {command}' timed out.")
            try:
                name, line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            if name == "out":
                out.append(line)
                count += 1
                if on_line and line.strip():
                    on_line(count, line.strip())
            else:
                err.append(line)
    finally:
        if proc.poll() is None:  # any exit path, including errors in callbacks, stops the process
            proc.kill()
            proc.wait()
    returncode = proc.wait()
    return "".join(out)[:MAX_OUT], "".join(err)[:20_000], returncode
