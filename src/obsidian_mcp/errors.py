"""Stable error codes.

Every error raised to an MCP client carries a code in brackets, a short message, and an
optional hint, e.g. ``[NOT_FOUND] Note "Ideas.md" does not exist. Hint: call search_notes``.
Errors subclass the SDK's ``ToolError`` so the text reaches the client instead of being
hidden as an unexpected crash.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError


class Code(StrEnum):
    VAULT_NOT_FOUND = "VAULT_NOT_FOUND"
    NO_VAULT = "NO_VAULT"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    INVALID_PATH = "INVALID_PATH"
    PATH_OUTSIDE_VAULT = "PATH_OUTSIDE_VAULT"
    CONFLICT = "CONFLICT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    CLI_ERROR = "CLI_ERROR"
    BRIDGE_ERROR = "BRIDGE_ERROR"
    HEADLESS_ERROR = "HEADLESS_ERROR"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TOO_LARGE = "TOO_LARGE"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    PARSE_ERROR = "PARSE_ERROR"
    PATCH_FAILED = "PATCH_FAILED"


class ObsidianError(ToolError):
    def __init__(self, code: Code, message: str, hint: str | None = None, **data: Any) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data
        text = f"[{code}] {message}"
        if hint:
            text += f" Hint: {hint}"
        super().__init__(text)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.data:
            out["data"] = self.data
        return out
