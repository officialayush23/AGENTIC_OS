"""Embedding back-ends.

* `local`  - feature-hashing embedder. Pure Python, offline, deterministic, zero downloads.
             Good lexical/sub-word recall; it is the default so the knowledge base works
             on a fresh install with no API key and no model download.
* `ollama` - any local Ollama embedding model (e.g. nomic-embed-text). Private and semantic.
* `openai` - OpenAI or any OpenAI-compatible /v1/embeddings endpoint.
* `gemini` - Google Gemini embeddings.

Remote embedders reuse the API key of a configured provider (stored in the OS
credential store), so there is only one place to manage keys.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from collections import Counter
from typing import Callable, Dict, List, Optional

DEFAULTS: Dict[str, dict] = {
    "local": {"model": "hash-512-v1", "dim": 512},
    "ollama": {"model": "nomic-embed-text", "base_url": "http://127.0.0.1:11434"},
    "openai": {"model": "text-embedding-3-small", "base_url": "https://api.openai.com/v1"},
    "gemini": {"model": "gemini-embedding-001", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "dim": 768},
}


class EmbedderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Embedder:
    kind = "base"
    model = ""
    batch_size = 64

    @property
    def name(self) -> str:
        """Identity stored with every vector; vectors from different names are never compared."""
        return f"{self.kind}:{self.model}"

    def embed(self, texts: List[str]) -> List[List[float]]:  # pragma: no cover - interface
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


def l2_normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


# ---------------------------------------------------------------------------- local
_WORD = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|[0-9]+|[a-z]+")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def _features(text: str) -> Counter:
    feats: Counter = Counter()
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    for i, tok in enumerate(tokens):
        feats["w:" + tok] += 1.0
        if "_" in tok or any(c.isdigit() for c in tok):
            for part in re.split(r"[_0-9]+", tok):
                if len(part) > 1:
                    feats["w:" + part] += 0.5
        if len(tok) > 3:
            padded = f"<{tok}>"
            for j in range(len(padded) - 2):
                feats["c:" + padded[j:j + 3]] += 0.25
        if i + 1 < len(tokens):
            feats["b:" + tok + " " + tokens[i + 1]] += 0.5
    for raw in _TOKEN.findall(text):                     # camelCase pieces
        parts = _WORD.findall(raw)
        if len(parts) > 1:
            for part in parts:
                feats["w:" + part.lower()] += 0.5
    return feats


class LocalHashEmbedder(Embedder):
    kind = "local"
    batch_size = 256

    def __init__(self, dim: int = 512):
        self.dim = int(dim)
        self.model = f"hash-{self.dim}-v1"

    def _vector(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for feat, count in _features(text).items():
            h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(count)) if count >= 1 else sign * count
        return l2_normalize(vec)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._vector(t) for t in texts]


# --------------------------------------------------------------------------- remote
def _post_json(url: str, payload: dict, headers: Dict[str, str], timeout: float = 60.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https/loopback URLs
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        code = "EMBED_AUTH" if exc.code in (401, 403) else ("EMBED_RATE_LIMIT" if exc.code == 429 else "EMBED_HTTP")
        raise EmbedderError(code, f"embedding request failed with HTTP {exc.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbedderError("EMBED_UNREACHABLE", f"could not reach the embedding service: {exc}") from None


class OllamaEmbedder(Embedder):
    kind = "ollama"
    batch_size = 32

    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def embed(self, texts: List[str]) -> List[List[float]]:
        data = _post_json(f"{self.base_url}/api/embed", {"model": self.model, "input": texts}, {})
        vecs = data.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise EmbedderError("EMBED_BAD_RESPONSE", "Ollama returned no embeddings (is the model pulled?)")
        return [l2_normalize([float(x) for x in v]) for v in vecs]


class OpenAIEmbedder(Embedder):
    kind = "openai"
    batch_size = 96

    def __init__(self, model: str, base_url: str, api_key: Optional[str]):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def embed(self, texts: List[str]) -> List[List[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = _post_json(f"{self.base_url}/embeddings", {"model": self.model, "input": texts}, headers)
        items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        if len(items) != len(texts):
            raise EmbedderError("EMBED_BAD_RESPONSE", "the embedding service returned the wrong number of vectors")
        return [l2_normalize([float(x) for x in it["embedding"]]) for it in items]


class GeminiEmbedder(Embedder):
    kind = "gemini"
    batch_size = 100

    def __init__(self, model: str, base_url: str, api_key: Optional[str], dim: int = 768):
        if not api_key:
            raise EmbedderError("EMBED_NO_KEY", "Gemini embeddings need the API key of a configured Gemini provider")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dim = dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        reqs = [{"model": f"models/{self.model}", "content": {"parts": [{"text": t}]},
                 "outputDimensionality": self.dim} for t in texts]
        data = _post_json(f"{self.base_url}/models/{self.model}:batchEmbedContents", {"requests": reqs},
                          {"x-goog-api-key": self.api_key})
        vecs = [e.get("values") for e in data.get("embeddings") or []]
        if len(vecs) != len(texts) or any(v is None for v in vecs):
            raise EmbedderError("EMBED_BAD_RESPONSE", "Gemini returned the wrong number of vectors")
        return [l2_normalize([float(x) for x in v]) for v in vecs]


def build_embedder(config: Optional[dict], api_key_for: Callable[[str], Optional[str]] = lambda _pid: None) -> Embedder:
    cfg = dict(config or {})
    kind = cfg.get("kind") or "local"
    if kind not in DEFAULTS:
        raise EmbedderError("EMBED_KIND_UNKNOWN", f"unknown embedder kind {kind!r}")
    merged = {**DEFAULTS[kind], **{k: v for k, v in cfg.items() if v not in (None, "")}}
    if kind == "local":
        return LocalHashEmbedder(int(merged.get("dim") or 512))
    if kind == "ollama":
        return OllamaEmbedder(merged["model"], merged["base_url"])
    key = api_key_for(cfg["provider_id"]) if cfg.get("provider_id") else None
    if kind == "openai":
        return OpenAIEmbedder(merged["model"], merged["base_url"], key)
    return GeminiEmbedder(merged["model"], merged["base_url"], key, int(merged.get("dim") or 768))
