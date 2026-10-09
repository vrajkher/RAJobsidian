"""Official Obsidian CLI adapter (requires the Obsidian 1.12.7+ installer and a running app).

Every invocation is checked against ``data/cli_catalog.json``, generated from the official
CLI documentation. Arguments go straight to ``subprocess`` as a list (no shell). Running a
command launches Obsidian if it is closed, so capability discovery only looks for the
binary; it does not execute anything until a tool actually needs the CLI.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from ..errors import Code, ObsidianError

MAX_OUTPUT = 4_000_000


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    raw = resources.files("obsidian_mcp.data").joinpath("cli_catalog.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    data["by_name"] = {c["name"]: c for c in data["commands"]}
    return data


def candidate_paths() -> list[str]:
    home = Path.home()
    system = platform.system()
    paths = []
    if system == "Darwin":
        paths += ["/usr/local/bin/obsidian", "/Applications/Obsidian.app/Contents/MacOS/obsidian-cli"]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        paths += [str(Path(local) / "Programs" / "Obsidian" / "Obsidian.com")] if local else []
    else:
        paths += [str(home / ".local" / "bin" / "obsidian")]
    return paths


def find_binary(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    found = shutil.which("obsidian")
    if found:
        return found
    return next((p for p in candidate_paths() if Path(p).exists()), None)


def encode_value(value: Any) -> str:
    """Encode a parameter value using the CLI's documented \\n and \\t escapes."""
    text = str(value).lower() if isinstance(value, bool) else str(value)
    if "\\n" in text or "\\t" in text:
        raise ObsidianError(Code.UNSUPPORTED,
                            "Value contains a literal backslash-n/t sequence, which the CLI would "
                            "turn into a newline/tab.", hint="Use the filesystem tools for this content.")
    return text.replace("\r\n", "\n").replace("\n", "\\n").replace("\t", "\\t")


class CLIAdapter:
    def __init__(self, binary: str | None, timeout: float = 30.0) -> None:
        self.binary = binary
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.binary is not None

    def validate(self, command: str, params: dict[str, Any], flags: list[str]) -> dict[str, Any]:
        spec = catalog()["by_name"].get(command)
        if spec is None:
            raise ObsidianError(Code.UNSUPPORTED, f"{command!r} is not a documented Obsidian CLI command.",
                                hint="Call cli_commands to list documented commands.")
        allowed = {p["name"]: p for p in spec["params"]}
        allowed_flags = {f["name"] for f in spec["flags"]}
        for name, value in params.items():
            if name not in allowed:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"{command} has no parameter {name!r}.",
                                    allowed=sorted(allowed))
            choices = allowed[name].get("choices")
            if choices and str(value) not in choices:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"{command} {name} must be one of {choices}.")
        for p in spec["params"]:
            if p["required"] and p["name"] not in params:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"{command} requires {p['name']}=.")
        for flag in flags:
            if flag not in allowed_flags:
                raise ObsidianError(Code.INVALID_ARGUMENT, f"{command} has no flag {flag!r}.",
                                    allowed=sorted(allowed_flags))
        return spec

    def build_argv(self, command: str, params: dict[str, Any] | None = None, flags: list[str] | None = None,
                   vault: str | None = None) -> list[str]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        flags = list(flags or [])
        self.validate(command, params, flags)
        argv = [self.binary or "obsidian"]
        if vault:
            argv.append(f"vault={encode_value(vault)}")  # must precede the command
        argv.append(command)
        argv += [f"{k}={encode_value(v)}" for k, v in params.items()]
        argv += flags
        return argv

    def run(self, command: str, params: dict[str, Any] | None = None, flags: list[str] | None = None,
            vault: str | None = None, cwd: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        if not self.available:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, "Obsidian CLI was not found.",
                                hint="Install the Obsidian 1.12.7+ installer, enable Settings → General → "
                                     "Command line interface, and keep Obsidian running.")
        argv = self.build_argv(command, params, flags, vault)
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout or self.timeout, cwd=cwd,
                                  stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ObsidianError(Code.TIMEOUT, f"Obsidian CLI '{command}' timed out.",
                                hint="Make sure Obsidian is running and not showing a dialog.") from exc
        except OSError as exc:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, f"Could not start the Obsidian CLI: {exc}") from exc
        out = proc.stdout[:MAX_OUTPUT].decode("utf-8", errors="replace")
        err = proc.stderr[:20000].decode("utf-8", errors="replace").strip()
        first = out.lstrip().split("\n", 1)[0]
        if proc.returncode != 0 or first.startswith("Error:"):
            raise ObsidianError(Code.CLI_ERROR, (err or first or f"exit code {proc.returncode}")[:2000],
                                command=command, exit_code=proc.returncode)
        result: dict[str, Any] = {"command": command, "output": out.rstrip("\n"),
                                  "truncated": len(proc.stdout) > MAX_OUTPUT}
        if (params or {}).get("format") == "json":
            try:
                result["data"] = json.loads(out)
            except json.JSONDecodeError:
                pass
        return result

    def version(self) -> str | None:
        try:
            return self.run("version", timeout=10)["output"].strip() or None
        except ObsidianError:
            return None
