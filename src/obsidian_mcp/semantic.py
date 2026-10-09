"""Optional semantic search through an OpenAI-compatible embeddings endpoint.

Off by default. Only the user can enable it (``obsidian-mcp config set semantic_search true``).
Privacy controls:

* The default endpoint is a local model server (Ollama at ``http://127.0.0.1:11434/v1``;
  LM Studio, llama.cpp and vLLM expose the same ``/embeddings`` API). Any non-local URL is
  refused unless the user also sets ``embedding_allow_remote true``.
* API keys are read from the environment variable named in ``embedding_api_key_env``; they
  are never stored in the config file or returned by any tool.
* Notes under ``embedding_exclude`` folders, and notes with the property ``ai: false``, are
  never sent to the endpoint.

Notes are split into heading sections (≈1,500 characters). Vectors are cached on disk per
vault and model, keyed by a hash of each chunk, so only new or changed text is embedded.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import markdown as md
from .config import Config
from .errors import Code, ObsidianError
from .index import VaultIndex
from .progress import Progress, no_progress

CHUNK_CHARS = 1500
BATCH = 32
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def chunk_note(text: str) -> list[tuple[str, int, str]]:
    """Split a note body into (heading path, 1-based start line, text) chunks."""
    note = md.parse(text)
    lines = text.split("\n")
    starts = [note.body_line] + [h.line for h in note.headings if h.line >= note.body_line]
    starts = sorted(set(starts))
    chunks: list[tuple[str, int, str]] = []
    trail: list[tuple[int, str]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        heading = next((h for h in note.headings if h.line == start), None)
        if heading:
            trail = [t for t in trail if t[0] < heading.level] + [(heading.level, heading.text)]
        title = " > ".join(t[1] for t in trail)
        section = "\n".join(lines[start:end]).strip()
        for offset in range(0, len(section), CHUNK_CHARS):
            piece = section[offset: offset + CHUNK_CHARS].strip()
            if piece:
                chunks.append((title, start + 1, piece))
    return chunks


class EmbeddingClient:
    def __init__(self, config: Config, timeout: float = 60.0) -> None:
        self.url = config.embedding_url.rstrip("/")
        self.model = config.embedding_model
        host = urllib.parse.urlparse(self.url).hostname or ""
        self.local = host in LOCAL_HOSTS
        if not self.local and not config.embedding_allow_remote:
            raise ObsidianError(Code.PERMISSION_DENIED,
                                f"Semantic search would send note text to {host!r}, which is not this computer.",
                                hint="Use a local model server, or run: obsidian-mcp config set "
                                     "embedding_allow_remote true (only if you accept sending note text there).")
        self.api_key = os.environ.get(config.embedding_api_key_env) if config.embedding_api_key_env else None
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url + "/embeddings", data=body, headers=headers, method="POST")
        handlers = [urllib.request.ProxyHandler({})] if self.local else []
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, f"Embedding endpoint error {exc.code}.",
                                hint=f"Check that model {self.model!r} exists at {self.url}.") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, f"Could not reach the embedding endpoint {self.url}.",
                                hint="Start your local model server (e.g. 'ollama serve' and "
                                     f"'ollama pull {self.model}').") from exc
        data = sorted(payload.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [d.get("embedding") for d in data]
        if len(vectors) != len(texts) or not all(isinstance(v, list) and v for v in vectors):
            raise ObsidianError(Code.ADAPTER_UNAVAILABLE, "The embedding endpoint returned an unexpected response.")
        return [_normalize(v) for v in vectors]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _excluded(path: str, frontmatter: dict[str, Any], folders: list[str]) -> bool:
    if any(path == f.strip("/") or path.startswith(f.strip("/") + "/") for f in folders if f.strip("/")):
        return True
    flag = frontmatter.get("ai")
    return flag is False or (isinstance(flag, str) and flag.lower() in ("false", "no", "off"))


class SemanticIndex:
    def __init__(self, index: VaultIndex, config: Config, state_dir: Path) -> None:
        self.index = index
        self.config = config
        key = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{config.embedding_model}@{config.embedding_url}")[:120]
        self.path = state_dir / "semantic" / index.vault.id / f"{key}.json"
        self._lock = threading.Lock()
        self._cache: dict[str, list[float]] | None = None
        self.chunks: list[dict[str, Any]] = []

    def _load(self) -> dict[str, list[float]]:
        if self._cache is None:
            try:
                self._cache = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _save(self, cache: dict[str, list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, progress: Progress = no_progress, cancelled: Callable[[], bool] = lambda: False
               ) -> dict[str, Any]:
        """Embed new or changed chunks. Returns counts; never sends excluded notes."""
        if not self.config.semantic_search:
            raise ObsidianError(Code.PERMISSION_DENIED, "Semantic search is off.",
                                hint="The vault owner can enable it with: obsidian-mcp config set "
                                     "semantic_search true (a local model server is used by default).")
        client = EmbeddingClient(self.config)
        with self._lock:
            cache = self._load()
            chunks: list[dict[str, Any]] = []
            excluded = 0
            for entry in self.index.notes():
                fm = md.to_plain(entry.note.frontmatter) if entry.note else {}
                if _excluded(entry.info.path, fm, self.config.embedding_exclude):
                    excluded += 1
                    continue
                for heading, line, text in chunk_note(entry.text or ""):
                    digest = hashlib.sha256(f"{entry.info.path}\n{text}".encode()).hexdigest()[:32]
                    chunks.append({"path": entry.info.path, "heading": heading, "line": line, "text": text,
                                   "hash": digest})
            missing = [c for c in chunks if c["hash"] not in cache]
            for start in range(0, len(missing), BATCH):
                if cancelled():
                    self._save(cache)
                    raise ObsidianError(Code.TIMEOUT, "Semantic indexing was cancelled; progress so far is kept.")
                progress(start, len(missing), f"embedding {start}/{len(missing)} chunks")
                batch = missing[start: start + BATCH]
                for item, vec in zip(batch, client.embed([c["text"] for c in batch]), strict=True):
                    cache[item["hash"]] = vec
            live = {c["hash"] for c in chunks}
            for stale in [h for h in cache if h not in live]:
                del cache[stale]
            if missing or len(cache) != len(live):
                self._save(cache)
            self.chunks = chunks
            progress(len(missing), len(missing), "done")
            return {"chunks": len(chunks), "embedded_now": len(missing), "excluded_notes": excluded,
                    "endpoint": "local" if client.local else "remote", "model": self.config.embedding_model}

    def search(self, query: str, *, limit: int = 10, folder: str | None = None, progress: Progress = no_progress,
               cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        if not query.strip():
            raise ObsidianError(Code.INVALID_ARGUMENT, "Query is empty.")
        status = self.update(progress, cancelled)
        qvec = EmbeddingClient(self.config).embed([query])[0]
        cache = self._load()
        prefix = folder.strip("/") + "/" if folder and folder.strip("/") else None
        scored = []
        for c in self.chunks:
            if prefix and not c["path"].startswith(prefix):
                continue
            vec = cache.get(c["hash"])
            if vec is None or len(vec) != len(qvec):
                continue
            scored.append((sum(a * b for a, b in zip(qvec, vec, strict=True)), c))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = [{"path": c["path"], "heading": c["heading"] or None, "line": c["line"], "score": round(s, 4),
                    "snippet": c["text"][:400]} for s, c in scored[:limit]]
        return {"query": query, "results": results, "index": status}
