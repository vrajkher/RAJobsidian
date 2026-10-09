"""OAuth resource-server mode: introspection, per-user vault isolation, admin-only changes, read-only scope."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import httpx2
import pytest
from conftest import posix_only
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from obsidian_mcp.errors import Code, ObsidianError

RESOURCE = "http://127.0.0.1/mcp"


@contextlib.contextmanager
def as_user(subject: str, scopes: list[str]):
    token = auth_context_var.set(AuthenticatedUser(AccessToken(token="t", client_id="c", scopes=scopes,
                                                               subject=subject)))
    try:
        yield
    finally:
        auth_context_var.reset(token)


@pytest.fixture()
def two_vaults(service, tmp_path):
    other = tmp_path / "Other"
    other.mkdir()
    (other / "Secret.md").write_bytes(b"bob only")
    service.connect_vault(str(other), name="BobVault")
    cfg = service.config
    cfg.user_vaults = {"alice": ["Test"], "bob": ["BobVault"], "root": ["*"]}
    cfg.admins = ["root"]
    cfg.oauth_write_scope = "vault:write"
    return service


def test_users_only_see_their_vaults(two_vaults):
    with as_user("alice", ["vault:write"]):
        names = [v["name"] for v in two_vaults.list_vaults()["connected"]]
        assert names == ["Test"]
        assert "discovered" not in two_vaults.list_vaults()
        assert two_vaults.read_note(None, "Ideas.md")["path"] == "Ideas.md"
        with pytest.raises(ObsidianError) as exc:
            two_vaults.read_note("BobVault", "Secret.md")
        assert exc.value.code == Code.VAULT_NOT_FOUND and exc.value.data["vaults"] == ["Test"]
    with as_user("bob", []):
        assert two_vaults.read_note(None, "Secret.md")["content"] == "bob only"
    with as_user("mallory", ["vault:write"]):
        assert two_vaults.list_vaults()["connected"] == []
        with pytest.raises(ObsidianError):
            two_vaults.read_note(None, "Ideas.md")


def test_read_only_tokens_and_admin_only_changes(two_vaults, tmp_path):
    with as_user("bob", []):  # no vault:write scope
        with pytest.raises(ObsidianError) as exc:
            two_vaults.append_note(None, "Secret.md", "x")
        assert "read-only" in exc.value.message
        with pytest.raises(ObsidianError) as exc:
            two_vaults.connect_vault(str(tmp_path))
        assert "administrators" in exc.value.message
    with as_user("alice", ["vault:write"]):
        assert two_vaults.append_note(None, "Ideas.md", "ok")["changed"]
        with pytest.raises(ObsidianError):
            two_vaults.set_default_vault("Test")
    with as_user("root", ["vault:write"]):
        assert {v["name"] for v in two_vaults.list_vaults()["connected"]} == {"Test", "BobVault"}
        two_vaults.set_default_vault("Test")


def test_no_principal_means_local_mode(two_vaults):
    assert {v["name"] for v in two_vaults.list_vaults()["connected"]} == {"Test", "BobVault"}


# ---- End-to-end over HTTP with a fake introspection endpoint ------------------------------

TOKENS = {
    "alice-token": {"active": True, "sub": "alice", "scope": "mcp vault:write", "aud": RESOURCE,
                    "client_id": "chatgpt", "exp": time.time() + 3600},
    "bob-ro-token": {"active": True, "sub": "bob", "scope": "mcp", "aud": RESOURCE, "client_id": "chatgpt"},
    "wrong-aud": {"active": True, "sub": "alice", "scope": "mcp vault:write", "aud": "https://elsewhere/mcp"},
    "revoked": {"active": False},
}


@pytest.fixture()
def introspection():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            calls.append(self.headers.get("Authorization"))
            payload = json.dumps(TOKENS.get(form["token"][0], {"active": False})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/introspect", calls
    httpd.shutdown()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_oauth_http_end_to_end(tmp_path, vault_dir, introspection):
    url, calls = introspection
    port = _free_port()
    home = tmp_path / "srv-home"
    home.mkdir()
    resource = f"http://127.0.0.1:{port}/mcp"
    for tok in TOKENS.values():
        if tok.get("aud") == RESOURCE:
            tok["aud"] = resource
    other = tmp_path / "Other"
    other.mkdir()
    (other / "Secret.md").write_bytes(b"bob only")
    (home / "config.json").write_text(json.dumps({
        "vaults": [{"id": "test-1", "name": "Test", "path": str(vault_dir)},
                   {"id": "bob-1", "name": "BobVault", "path": str(other)}],
        "default_vault": "test-1", "discover_obsidian_vaults": False,
        "oauth_issuer_url": "https://auth.example.com", "oauth_introspection_url": url,
        "oauth_resource_url": resource, "oauth_client_id_env": "INTROSPECT_ID",
        "oauth_client_secret_env": "INTROSPECT_SECRET", "oauth_required_scopes": ["mcp"],
        "oauth_write_scope": "vault:write", "user_vaults": {"alice": ["Test"], "bob": ["BobVault"]},
    }), encoding="utf-8")
    env = {**os.environ, "OBSIDIAN_MCP_HOME": str(home), "INTROSPECT_ID": "mcp-server",
           "INTROSPECT_SECRET": "s3cret"}
    proc = subprocess.Popen([sys.executable, "-m", "obsidian_mcp", "serve", "--transport", "http", "--port",
                             str(port)], env=env, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    try:
        async with httpx2.AsyncClient(timeout=5) as http:
            for _ in range(100):
                try:
                    r = await http.post(resource, json={})
                    break
                except httpx2.TransportError:
                    time.sleep(0.1)
            assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
            meta = await http.get(f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp")
            assert meta.json()["authorization_servers"] == ["https://auth.example.com"]
            for bad in ("revoked", "wrong-aud", "unknown"):
                r = await http.post(resource, json={}, headers={"Authorization": f"Bearer {bad}"})
                assert r.status_code == 401, bad

        async def connect(token):
            return Client(streamable_http_client(resource, http_client=httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=30)))

        async with await connect("alice-token") as alice:
            vaults = (await alice.call_tool("list_vaults", {})).structured_content["connected"]
            assert [v["name"] for v in vaults] == ["Test"]
            denied = await alice.call_tool("read_note", {"vault": "BobVault", "path": "Secret.md"})
            assert denied.is_error and "VAULT_NOT_FOUND" in denied.content[0].text
            ok = await alice.call_tool("append_note", {"path": "Ideas.md", "content": "from alice"})
            assert not ok.is_error
        async with await connect("bob-ro-token") as bob:
            note = await bob.call_tool("read_note", {"path": "Secret.md"})
            assert note.structured_content["content"] == "bob only"
            ro = await bob.call_tool("append_note", {"path": "Secret.md", "content": "x"})
            assert ro.is_error and "read-only" in ro.content[0].text
        assert calls and calls[0].startswith("Basic ")  # introspection used the server's credentials
        assert (vault_dir / "Ideas.md").read_bytes().endswith(b"from alice")
        assert (other / "Secret.md").read_bytes() == b"bob only"
    finally:
        proc.terminate()
        proc.wait()


@posix_only
def test_host_app_control_is_admin_only(two_vaults, tmp_path):
    fake = tmp_path / "obsidian"
    fake.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake.chmod(0o755)
    two_vaults.config.cli_path = str(fake)
    with as_user("alice", ["vault:write"]):
        for call in (lambda: two_vaults.cli_run(None, "vaults"),
                     lambda: two_vaults.bridge_call("GET", "/active"),
                     lambda: two_vaults.headless_run(None, "sync-list-remote"),
                     lambda: two_vaults.write_snippet(None, "x", "a{}"),
                     lambda: two_vaults.move_file(None, "Ideas.md", "I2.md", adapter="cli"),
                     lambda: two_vaults.trash_file(None, "Ideas.md", adapter="bridge")):
            with pytest.raises(ObsidianError) as exc:
                call()
            assert "administrators" in exc.value.message
        two_vaults.config.bridge_token = "configured"
        moved = two_vaults.move_file(None, "Ideas.md", "Concepts.md")  # auto falls back to filesystem
        assert moved["adapter"] == "filesystem"
    with as_user("root", ["vault:write"]):
        assert two_vaults.cli_run(None, "vaults")["output"] == "ok"
