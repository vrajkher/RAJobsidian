"""Obsidian Headless (``ob``, npm package ``obsidian-headless``, open beta) adapter.

Covers the documented Sync and Publish commands. Credentials never pass through this
server: ``ob login`` is interactive and must be run by the user. Passwords are never
accepted as tool arguments. The docs warn against using desktop Sync and Headless Sync
on the same device; ``sync`` refuses to run when the vault has the desktop Sync plugin
enabled, unless the caller explicitly overrides.
"""

from __future__ import annotations

import shutil
import subprocess
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
            timeout: float | None = None) -> dict[str, Any]:
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
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout or self.timeout,
                                  stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ObsidianError(Code.TIMEOUT, f"'ob {command}' timed out.") from exc
        out = proc.stdout.decode("utf-8", errors="replace")[:200_000]
        err = proc.stderr.decode("utf-8", errors="replace")[:20_000]
        if proc.returncode != 0:
            hint = "Run 'ob login' in your terminal." if "login" in (out + err).lower() else None
            raise ObsidianError(Code.HEADLESS_ERROR, (err.strip() or out.strip() or "failed")[:2000], hint=hint)
        return {"command": f"ob {command}", "output": out.strip(), "stderr": err.strip() or None}
