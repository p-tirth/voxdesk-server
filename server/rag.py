"""Local document retrieval (RAG) over the business's unstructured prose.

Where ``catalog.py`` handles *structured* data (products, orders) with exact
substring lookups, this module handles *prose* — the FAQ / policy documents under
``data/<business-key>/docs/*.md`` (returns, warranty, shipping, payments…). Those
questions ("how do I claim warranty on an appliance?") aren't answered by a field
lookup; they need semantic retrieval over free text. That's the one job a catalog
can't do, so it lives here as its own tool (``search_docs`` in ``tools.py``).

Stack (all local, no cloud):

  * **Embeddings — ``bge-m3`` via Ollama.** A local, multilingual embedding model
    (good for Hinglish), reached over Ollama's HTTP API. No PyTorch in-process and
    no API key; the bot only needs the Ollama daemon running with the model pulled
    (``ollama pull bge-m3``). Override the host/model with ``OLLAMA_BASE_URL`` /
    ``RAG_EMBED_MODEL``.
  * **Vector store — Qdrant, embedded (local mode).** ``QdrantClient(":memory:")``
    runs Qdrant in-process with no server or Docker — the same client API you point
    at a Qdrant Docker server later, so moving to a persistent server for a larger
    corpus is a one-line change. For a handful of policy docs, in-memory is plenty.

A relevance floor (``min_score``) lets the tool tell "the corpus answers this"
from "it doesn't", so the bot can decline honestly instead of inventing a policy.
Missing docs (or Ollama being unreachable at boot) degrade to an empty store —
``search`` returns nothing and the bot simply has no document knowledge, rather
than crashing.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from qdrant_client import QdrantClient, models

# Docs live next to the structured data: data/<key>/docs/*.md.
DATA_ROOT = Path(__file__).parent / "data"

# Local embedding model, served by Ollama. bge-m3 is multilingual (handles the
# store's Hinglish) and 1024-dim.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "bge-m3")

# One in-memory Qdrant collection per bot process holds the embedded corpus.
COLLECTION = "docs"

# Recall-first floor: a cheap junk filter, NOT the relevance decision. Measured on
# this corpus, grounded and off-corpus scores overlap badly and there is no cutoff
# that separates them:
#   * English grounded ~0.55-0.74, English off-corpus ~0.52-0.57
#   * Hinglish grounded ~0.38-0.66 — systematically lower than English for the same
#     intent, because the corpus is English and bge-m3's cross-lingual similarity
#     runs lower (the correct doc still ranks #1, it just scores lower)
# A floor high enough to reject off-corpus questions would silently drop real
# Hinglish matches — unacceptable for a Hinglish-first bot. So we keep the floor
# low (drop only clear junk) and make relevance the LLM's call: search_docs hands
# the passages over with an explicit instruction to answer only if they actually
# address the question. (A dense+sparse hybrid — both bge-m3 and Qdrant support it
# — would sharpen ranking later; overkill for a handful of docs.)
#
# NOTE: this number is calibrated to *bge-m3's* score distribution. Different
# embedding models score on different scales, so if you change RAG_EMBED_MODEL,
# re-check this floor: embed a few questions you expect to hit and a few you expect
# to miss, and set it below the ones that should hit. Too high silently drops real
# matches; too low only costs a few tokens (the LLM still judges relevance).
DEFAULT_MIN_SCORE = 0.35


@dataclass
class DocChunk:
    """One retrievable passage: a paragraph plus the doc title it came from."""

    source: str
    text: str


def _read_chunks(base: Path) -> list[DocChunk]:
    """Read ``*.md`` under ``base`` into paragraph-level chunks.

    Each file's first ``# heading`` becomes the ``source`` label; the rest is
    split on blank lines so each paragraph is an independently retrievable chunk.
    """
    if not base.exists():
        return []
    chunks: list[DocChunk] = []
    for path in sorted(base.glob("*.md")):
        text = path.read_text()
        heading = re.search(r"^#\s+(.+)$", text, flags=re.M)
        title = heading.group(1).strip() if heading else path.stem
        body = re.sub(r"^#\s+.+$", "", text, count=1, flags=re.M)
        for para in re.split(r"\n\s*\n", body):
            para = " ".join(para.split())
            if para:
                chunks.append(DocChunk(source=title, text=para))
    return chunks


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts via Ollama's ``/api/embed`` batch endpoint.

    Raises on transport/HTTP errors so callers can decide whether to fail the
    build (boot) or degrade the query (search).
    """
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    return data["embeddings"]


@dataclass
class DocStore:
    """In-memory semantic index (Qdrant + Ollama embeddings) over help docs.

    Attributes:
        key: The business/profile key whose ``data/<key>/docs/`` this indexes.
        chunks: The retrievable passages.
        min_score: Cosine floor for a chunk to count as relevant.
    """

    key: str
    chunks: list[DocChunk] = field(default_factory=list)
    min_score: float = DEFAULT_MIN_SCORE
    _client: QdrantClient | None = field(default=None, repr=False)

    @classmethod
    def load(cls, key: str, min_score: float = DEFAULT_MIN_SCORE) -> DocStore:
        """Load and embed ``data/<key>/docs/``; empty if absent or Ollama is down."""
        chunks = _read_chunks(DATA_ROOT / key / "docs")
        store = cls(key=key, chunks=chunks, min_score=min_score)
        if chunks:
            store._build_index()
        else:
            logger.info(f"docs | {key}: no documents found; search_docs will be empty")
        return store

    def _build_index(self) -> None:
        """Embed every chunk once and load them into an in-memory Qdrant collection.

        A failure here (e.g. Ollama not running) is logged, not raised: the store
        stays empty and the bot boots without document knowledge instead of dying.
        """
        try:
            vectors = _embed([c.text for c in self.chunks])
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            logger.warning(
                f"docs | {self.key}: could not embed corpus via Ollama ({EMBED_MODEL}) "
                f"at {OLLAMA_BASE_URL}: {e}. search_docs will be empty — is `ollama "
                f"serve` running with `ollama pull {EMBED_MODEL}`?"
            )
            return
        client = QdrantClient(":memory:")
        client.create_collection(
            COLLECTION,
            vectors_config=models.VectorParams(
                size=len(vectors[0]), distance=models.Distance.COSINE
            ),
        )
        client.upsert(
            COLLECTION,
            points=[
                models.PointStruct(
                    id=i, vector=vec, payload={"source": chunk.source, "text": chunk.text}
                )
                for i, (chunk, vec) in enumerate(zip(self.chunks, vectors))
            ],
        )
        self._client = client
        logger.info(
            f"docs | {self.key}: indexed {len(self.chunks)} chunks in Qdrant "
            f"({EMBED_MODEL}, dim {len(vectors[0])})"
        )

    def search(self, query: str, k: int = 3) -> list[tuple[DocChunk, float]]:
        """Return up to ``k`` (chunk, cosine) pairs above ``min_score``, best first.

        Empty when there are no docs, the index didn't build, or nothing clears the
        relevance floor — which the tool reports as "not in the corpus" so the bot
        declines rather than guessing. A query-time embedding failure degrades to
        empty too (logged), so one Ollama hiccup doesn't crash the turn.
        """
        if not self.chunks or self._client is None:
            return []
        try:
            query_vector = _embed([query])[0]
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            logger.warning(f"docs | {self.key}: query embed failed: {e}")
            return []
        points = self._client.query_points(
            COLLECTION, query=query_vector, limit=k, with_payload=True
        ).points
        return [
            (DocChunk(source=p.payload["source"], text=p.payload["text"]), float(p.score))
            for p in points
            if p.score >= self.min_score
        ]
