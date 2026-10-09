"""Client for the optional Obsidian plugin bridge (``bridge-plugin/``).

The bridge is a tiny desktop-only Obsidian plugin that serves a JSON API on 127.0.0.1,
protected by a random bearer token. It exposes only public Plugin API features that need
the running app: active editor, selection and cursor, workspace leaves, app events, and
link-aware rename/trash through ``FileManager``. Commands are NOT exposed because
``app.commands`` is not part of the public API; use the CLI ``commands``/``command``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import Code, ObsidianError


class BridgeAdapter:
    def __init__(self, url: str, token: str | None, timeout: float = 10.0) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ObsidianError(Code.INVALID_ARGUMENT, "The bridge URL must point at localhost.")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None,
             query: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if not self.token:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, "The plugin bridge is not configured.",
                                hint="Install the 'MCP Bridge' plugin from bridge-plugin/, copy its token, "
                                     "then run: obsidian-mcp config set bridge_token <token>")
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route via proxies
        try:
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", "")
            except Exception:
                detail = ""
            if exc.code == 401:
                raise ObsidianError(Code.PERMISSION_DENIED, "The bridge rejected the token.",
                                    hint="Copy the current token from the plugin settings.") from exc
            raise ObsidianError(Code.BRIDGE_ERROR, f"Bridge error {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, "Could not reach the plugin bridge.",
                                hint="Open Obsidian and enable the MCP Bridge plugin.") from exc
        return json.loads(payload) if payload else None

    def status(self) -> dict[str, Any] | None:
        try:
            return self.call("GET", "/status", timeout=2)
        except ObsidianError:
            return None
