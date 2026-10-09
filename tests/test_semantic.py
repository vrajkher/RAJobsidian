"""Opt-in semantic search against a fake OpenAI-compatible embeddings server."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import Client

from obsidian_mcp.errors import Code, ObsidianError
from obsidian_mcp.semantic import chunk_note
from obsidian_mcp.server import build_server

DIMS = 64


def fake_vector(text: str) -> list[float]:
    vec = [0.0] * DIMS
    for word in text.lower().split():
        vec[int(hashlib.md5(word.strip(".,#").encode()).hexdigest(), 16) % DIMS] += 1.0
    return vec


@pytest.fixture()
def embed_server():
    seen: list[str] = []
    headers: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.extend(body["input"])
            headers.append(self.headers.get("Authorization"))
            data = [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(body["input"])]
            payload = json.dumps({"data": data, "model": body["model"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1", seen, headers
    httpd.shutdown()


@pytest.fixture()
def semantic_service(service, embed_server, vault_dir):
    url, _, _ = embed_server
    service.config.semantic_search = True
    service.config.embedding_url = url
    (vault_dir / "Private").mkdir()
    (vault_dir / "Private" / "Diary.md").write_bytes("secret garden thoughts".encode())
    (vault_dir / "Opted out.md").write_bytes("---\nai: false\n---\ngarden plans to keep offline\n".encode())
    (vault_dir / "Garden.md").write_bytes("# Garden\n## Tomatoes\nWater the tomatoes every morning.\n".encode())
    service.config.embedding_exclude = ["Private"]
    return service


def test_off_by_default(service):
    with pytest.raises(ObsidianError) as exc:
        service.semantic_search(None, "anything")
    assert exc.value.code == Code.PERMISSION_DENIED and "semantic_search true" in exc.value.hint


def test_remote_endpoint_needs_explicit_permission(service):
    service.config.semantic_search = True
    service.config.embedding_url = "https://api.example.com/v1"
    with pytest.raises(ObsidianError) as exc:
        service.semantic_search(None, "anything")
    assert exc.value.code == Code.PERMISSION_DENIED and "embedding_allow_remote" in exc.value.hint


def test_search_ranks_by_meaning_and_respects_exclusions(semantic_service, embed_server):
    _, seen, _ = embed_server
    out = semantic_service.semantic_search(None, "water tomatoes morning")
    top = out["results"][0]
    assert top["path"] == "Garden.md" and top["heading"] == "Garden > Tomatoes" and top["line"] == 2
    assert out["index"]["excluded_notes"] == 2 and out["index"]["endpoint"] == "local"
    sent = "\n".join(seen)
    assert "secret garden" not in sent and "keep offline" not in sent


def test_only_changed_sections_are_reembedded(semantic_service, embed_server, vault_dir):
    _, seen, _ = embed_server
    first = semantic_service.semantic_search(None, "garden")
    assert first["index"]["embedded_now"] > 0
    second = semantic_service.semantic_search(None, "garden")
    assert second["index"]["embedded_now"] == 0
    semantic_service.append_note(None, "Garden.md", "Also plant basil.")
    third = semantic_service.semantic_search(None, "basil")
    assert third["index"]["embedded_now"] == 1
    assert seen.count("basil") == 1  # the query itself


def test_api_key_comes_from_named_env_var(semantic_service, embed_server, monkeypatch):
    _, _, headers = embed_server
    monkeypatch.setenv("MY_EMBED_KEY", "sk-test")
    semantic_service.config.embedding_api_key_env = "MY_EMBED_KEY"
    semantic_service.semantic_search(None, "garden")
    assert headers[-1] == "Bearer sk-test"
    assert "sk-test" not in json.dumps(semantic_service.capabilities())


def test_unreachable_endpoint_explains_fix(service):
    service.config.semantic_search = True
    service.config.embedding_url = "http://127.0.0.1:9/v1"
    with pytest.raises(ObsidianError) as exc:
        service.semantic_search(None, "x")
    assert exc.value.code == Code.ADAPTER_UNAVAILABLE and "ollama" in exc.value.hint


def test_chunking_follows_headings():
    chunks = chunk_note("---\na: 1\n---\nIntro\n# A\ntext a\n## B\ntext b\n")
    assert [(h, line) for h, line, _ in chunks] == [("", 4), ("A", 5), ("A > B", 7)]


async def test_semantic_tool_reports_progress(semantic_service):
    events = []

    async def on_progress(progress, total, message):
        events.append(message)

    async with Client(build_server(semantic_service)) as client:
        out = await client.call_tool("semantic_search", {"query": "tomatoes"}, progress_callback=on_progress)
    assert out.structured_content["results"][0]["path"] == "Garden.md"
    assert events[0].startswith("embedding 0/") and events[-1] == "done"
