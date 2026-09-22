"""Text embeddings for the admin agent's retrieval layer.

Two providers behind one ``embed`` call:

* ``openai`` - any OpenAI-compatible ``/embeddings`` endpoint (OpenAI itself,
  Azure, Together, a local server). Configured with EMBEDDING_API_KEY,
  EMBEDDING_BASE_URL and EMBEDDING_MODEL.
* ``local``  - a deterministic hashed bag-of-words + character-trigram embedding.
  No model download, no network. Weaker than a neural model but good enough for
  the store's short, keyword-heavy records, and it keeps development and demos
  working with no extra credentials.

Vectors are always L2-normalised and padded/truncated to EMBEDDING_DIMENSIONS
so the pgvector column has one fixed width whichever provider is active.
"""

import hashlib
import logging
import math
import re
from collections.abc import Sequence

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 96
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-.'][a-z0-9]+)*")


def model_name() -> str:
    """Identifies which embedding space stored vectors live in."""
    if settings.embedding_provider == "openai":
        return f"openai:{settings.EMBEDDING_MODEL}:{settings.EMBEDDING_DIMENSIONS}"
    return f"local-hash-v1:{settings.EMBEDDING_DIMENSIONS}"


def _fit(vec: list[float]) -> list[float]:
    dim = settings.EMBEDDING_DIMENSIONS
    if len(vec) > dim:
        vec = vec[:dim]
    elif len(vec) < dim:
        vec = vec + [0.0] * (dim - len(vec))
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def _bucket(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "little") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def _local_embed_one(text: str) -> list[float]:
    dim = settings.EMBEDDING_DIMENSIONS
    vec = [0.0] * dim
    words = _WORD_RE.findall(text.lower())
    if not words:
        return vec
    # word unigrams (weight 1), bigrams (0.6) and character trigrams (0.35): the
    # trigrams let "restock" match "restocking" and SKUs match partial SKUs.
    for w in words:
        i, s = _bucket("w:" + w, dim)
        vec[i] += s
        padded = f" {w} "
        for k in range(len(padded) - 2):
            i, s = _bucket("c:" + padded[k : k + 3], dim)
            vec[i] += 0.35 * s
    for a, b in zip(words, words[1:]):
        i, s = _bucket(f"b:{a}_{b}", dim)
        vec[i] += 0.6 * s
    return _fit(vec)


async def _openai_embed(texts: Sequence[str]) -> list[list[float]]:
    url = settings.EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"}
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            body: dict = {"model": settings.EMBEDDING_MODEL, "input": batch}
            if settings.EMBEDDING_MODEL.startswith("text-embedding-3"):
                body["dimensions"] = settings.EMBEDDING_DIMENSIONS
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda d: d["index"])
            out.extend(_fit(d["embedding"]) for d in data)
    return out


async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of texts. Falls back to the local embedder if the remote
    provider fails, logging the failure, so indexing and chat never hard-fail on
    an embeddings outage."""
    if not texts:
        return []
    if settings.embedding_provider == "openai":
        try:
            return await _openai_embed(texts)
        except Exception:
            logger.exception("Remote embeddings failed; using the local embedder for this batch")
    return [_local_embed_one(t) for t in texts]


async def embed_one(text: str) -> list[float]:
    return (await embed([text]))[0]
