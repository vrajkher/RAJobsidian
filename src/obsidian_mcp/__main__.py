"""Command line: run the server and manage configuration.

    obsidian-mcp serve [--transport stdio|http] [--host 127.0.0.1] [--port 8765] [--vault PATH]
    obsidian-mcp vault add PATH [--name NAME] [--default] | vault list | vault remove NAME
    obsidian-mcp config show | config set KEY VALUE
    obsidian-mcp doctor
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
from dataclasses import fields
from typing import Any

from .config import Config, ConfigStore, Permissions


def _parse_value(key: str, raw: str) -> Any:
    types = {f.name: f.type for f in fields(Config)} | {f.name: "bool" for f in fields(Permissions)}
    kind = str(types.get(key, "str"))
    if "bool" in kind:
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        raise SystemExit(f"{key} expects true/false")
    if "int" in kind:
        return int(raw)
    if "float" in kind:
        return float(raw)
    return None if raw.lower() in ("none", "null", "") else raw


def cmd_config(args: argparse.Namespace) -> None:
    store = ConfigStore()
    if args.action == "show":
        print(json.dumps(store.config.public_dict(), indent=2, ensure_ascii=False))
        print(f"\nConfig file: {store.path}", file=sys.stderr)
        return
    try:
        store.update(**{args.key: _parse_value(args.key, args.value)})
    except KeyError:
        names = sorted({f.name for f in fields(Config)} | {f.name for f in fields(Permissions)} - {"vaults"})
        raise SystemExit(f"Unknown key {args.key!r}. Known: {', '.join(names)}") from None
    print(f"{args.key} updated.")


def cmd_vault(args: argparse.Namespace) -> None:
    from .registry import VaultRegistry, discover

    store = ConfigStore()
    reg = VaultRegistry(store)
    if args.action == "add":
        e = reg.register(args.path, args.name, args.default)
        print(f"Connected {e.name} ({e.id}) at {e.path}")
    elif args.action == "remove":
        e = reg.unregister(args.path)
        print(f"Disconnected {e.name}; files untouched.")
    else:
        for e in reg.entries:
            mark = "*" if e.id == store.config.default_vault else " "
            print(f"{mark} {e.name:30} {e.id:24} {e.path}")
        others = [d for d in discover() if d["path"] not in {e.path for e in reg.entries}]
        if others:
            print("\nDiscovered (not connected):")
            for d in others:
                print(f"  {d['name']:30} {d['path']}")


def cmd_doctor(args: argparse.Namespace) -> None:
    from .service import ObsidianService

    svc = ObsidianService()
    caps = svc.capabilities(probe=args.probe)
    print(json.dumps(caps, indent=2, ensure_ascii=False, default=str))


def _bearer_app(app: Any, token: str) -> Any:
    expected = f"Bearer {token}".encode()

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await app(scope, receive, send)

    return guarded


def cmd_serve(args: argparse.Namespace) -> None:
    import anyio

    from .server import build_server, run_with_poller
    from .service import ObsidianService

    svc = ObsidianService()
    if args.vault:
        svc.connect_vault(args.vault, make_default=True)
    elif os.environ.get("OBSIDIAN_VAULT"):
        svc.connect_vault(os.environ["OBSIDIAN_VAULT"], make_default=True)
    server = build_server(svc)
    if args.transport == "stdio":
        anyio.run(run_with_poller, server, server.run_stdio_async)
        return
    import uvicorn

    token = os.environ.get("OBSIDIAN_MCP_HTTP_TOKEN")
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not token and not (local and args.no_auth):
        token = secrets.token_urlsafe(24)
        print(f"OBSIDIAN_MCP_HTTP_TOKEN not set; generated one for this run:\n  {token}", file=sys.stderr)
    app = server.streamable_http_app()
    if token:
        app = _bearer_app(app, token)

    async def main() -> None:
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        await uvicorn.Server(config).serve()

    anyio.run(run_with_poller, server, main)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="obsidian-mcp", description="Obsidian MCP server")
    sub = parser.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="Run the MCP server (default)")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--vault", help="Connect this vault folder and make it the default")
    s.add_argument("--no-auth", action="store_true", help="Allow unauthenticated HTTP on localhost only")
    c = sub.add_parser("config", help="Show or change configuration")
    c.add_argument("action", choices=["show", "set"])
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    v = sub.add_parser("vault", help="Manage connected vaults")
    v.add_argument("action", choices=["add", "list", "remove"])
    v.add_argument("path", nargs="?")
    v.add_argument("--name")
    v.add_argument("--default", action="store_true")
    d = sub.add_parser("doctor", help="Check adapters and prerequisites")
    d.add_argument("--probe", action="store_true", help="Contact the CLI and bridge (may launch Obsidian)")
    args = parser.parse_args(argv)
    if args.cmd in (None, "serve"):
        if args.cmd is None:
            args = parser.parse_args(["serve", *(argv or [])])
        cmd_serve(args)
    elif args.cmd == "config":
        if args.action == "set" and (args.key is None or args.value is None):
            parser.error("config set KEY VALUE")
        cmd_config(args)
    elif args.cmd == "vault":
        if args.action in ("add", "remove") and not args.path:
            parser.error(f"vault {args.action} needs a path or name")
        cmd_vault(args)
    elif args.cmd == "doctor":
        cmd_doctor(args)


if __name__ == "__main__":
    main()
