"""Local document retrieval (RAG) over the business's unstructured prose.

Where ``catalog.py`` handles *structured* data (products, orders) with exact
substring lookups, this module handles *prose* — the FAQ / policy documents under
``data/<business-key>/docs/*.md`` (returns, warranty, shipping, payments…). Those
questions ("how do I claim warranty on an appliance?") aren't answered by a field
lookup; they need semantic retrieval over free text. That's the one job a catalog
can't do, so it lives here as its own tool (``search_docs`` in ``tools.py``).

Stack (all local, no cloud):

  * **Embeddings — a pluggable backend, chosen with ``RAG_BACKEND``.** Both options
    run locally with no API key; they differ in whether a daemon has to be running
    and in how well they handle Hinglish:

    - ``ollama`` (default, and the quality pick) — ``bge-m3`` served by a local
      Ollama daemon over its HTTP API. Multilingual, so it holds up on the store's
      Hinglish questions. Costs a ~1.2 GB model pull and one more process running
      (``ollama pull bge-m3`` + ``ollama serve``). Host/model overrides:
      ``OLLAMA_BASE_URL`` / ``RAG_EMBED_MODEL``.
    - ``fastembed`` — in-process ONNX embeddings, no daemon and no separate setup
      step (the model downloads itself on first use). This is what makes the doc
      evals runnable in CI, where installing and running Ollama isn't worth it.
      Retrieval is measurably weaker on Hinglish than bge-m3 (see the floors
      below), which is an accepted trade for the zero-daemon path — the local demo
      stack stays on ``ollama``. Model override: ``RAG_FASTEMBED_MODEL``.
    - ``none`` — no doc store at all. The bot runs with no document knowledge and
      ``search_docs`` finds nothing.

    Two model env vars rather than one on purpose: the names live in different
    namespaces (an Ollama tag like ``bge-m3`` vs. a Hugging Face repo id like
    ``BAAI/bge-small-en-v1.5``), so a single var would be silently wrong the moment
    you flip ``RAG_BACKEND``. Each var applies only to its own backend.
  * **Vector store — Qdrant, embedded (local mode).** ``QdrantClient(":memory:")``
    runs Qdrant in-process with no server or Docker — the same client API you point
    at a Qdrant Docker server later, so moving to a persistent server for a larger
    corpus is a one-line change. For a handful of policy docs, in-memory is plenty.
    The vector dimension is read off the first embedding, so swapping the backend
    (or the model) needs no other change here.

A relevance floor (``min_score``) lets the tool tell "the corpus answers this"
from "it doesn't", so the bot can decline honestly instead of inventing a policy.
Missing docs (or *any* backend failure — Ollama unreachable, a bad model name, the
ONNX download failing) degrade to an empty store: a warning is logged, ``search``
returns nothing, and the bot simply has no document knowledge rather than crashing.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from qdrant_client import QdrantClient, models

# Docs live next to the structured data: data/<key>/docs/*.md.
DATA_ROOT = Path(__file__).parent / "data"

# Which embedding backend to use: "ollama" (default, best Hinglish), "fastembed"
# (in-process ONNX, no daemon — the CI path), or "none" (no doc store).
BACKENDS = ("ollama", "fastembed", "none")
BACKEND = os.getenv("RAG_BACKEND", "ollama").strip().lower() or "ollama"
if BACKEND not in BACKENDS:
    logger.warning(
        f"docs | unknown RAG_BACKEND={BACKEND!r} (expected one of {', '.join(BACKENDS)}); "
        f"falling back to 'ollama'"
    )
    BACKEND = "ollama"

# Backend "ollama": bge-m3 served by a local Ollama daemon. Multilingual (handles
# the store's Hinglish) and 1024-dim.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "bge-m3")

# Backend "fastembed": an ONNX model run in-process, downloaded on first use.
#
# bge-small-en-v1.5 (384-dim, ~67 MB) is the pick, and it beat the obvious
# multilingual candidate on measurement, not intuition. Bake-off with
# scripts/check_retrieval.py on this corpus:
#
#   model                                    battery (12 grounded)   hard Hinglish (8)
#   bge-m3 via Ollama                        12/12                   4/8
#   BAAI/bge-small-en-v1.5                   12/12                   5/8
#   paraphrase-multilingual-MiniLM-L12-v2    11/12                   1/8
#
# The multilingual model is *worse* on Hinglish than an English-only one because
# Hinglish here is romanized — Latin script carrying English nouns ("return",
# "delivery", "warranty") — while that model learned Hindi in Devanagari, so
# romanized Hindi reads as noise to it (scores collapsed to 0.09, one even
# negative). An English model at least reads the English half. Nothing else
# fastembed ships is both multilingual and small: the multilingual heavyweights
# (multilingual-e5-large, jina-v3) are ~2.3 GB — bigger than the bge-m3 pull this
# backend exists to avoid.
#
# The model lands in fastembed's own cache under the system temp dir
# (``$TMPDIR/fastembed_cache``, ~64 MB for this model) — worth persisting between
# CI runs so each one doesn't re-download it.
FASTEMBED_MODEL = os.getenv("RAG_FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")

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
# embedding models score on different scales, so every backend/model gets its own
# floor (see FASTEMBED_MIN_SCORE) — re-check it whenever you change one. The
# recalibration harness is `scripts/check_retrieval.py`: it runs a fixed battery of
# grounded (English + Hinglish) and off-corpus queries and prints each one's top
# hit and score, so you can set the floor below the ones that should hit. Too high
# silently drops real matches; too low only costs a few tokens (the LLM still
# judges relevance).
DEFAULT_MIN_SCORE = 0.35

# Same junk-filter role, measured for the fastembed backend's default model, which
# scores everything roughly 0.15 higher than bge-m3 on this corpus:
#   * grounded 0.645-0.915 (English and Hinglish alike)
#   * harder, keyword-free Hinglish bottoms out at 0.489
#   * off-corpus 0.560-0.678, and even nonsense strings land at 0.48-0.55
# So — exactly as with bge-m3 — no cutoff separates relevant from irrelevant, and
# this floor sits below every real hit measured rather than trying to.
FASTEMBED_MIN_SCORE = 0.40


def default_min_score(backend: str = BACKEND) -> float:
    """The junk-filter floor calibrated for ``backend``'s embedding scale."""
    return FASTEMBED_MIN_SCORE if backend == "fastembed" else DEFAULT_MIN_SCORE


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


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    """Embed texts via Ollama's ``/api/embed`` batch endpoint."""
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    return data["embeddings"]


# The ONNX session is expensive to build, so it's created once per process on first
# use — and only on the fastembed path, so `ollama`/`none` never pay the import.
_fastembed: object | None = None


def _embed_fastembed(texts: list[str]) -> list[list[float]]:
    """Embed texts in-process with fastembed's ONNX runtime (no daemon)."""
    global _fastembed
    if _fastembed is None:
        from fastembed import TextEmbedding

        logger.info(f"docs | loading fastembed model {FASTEMBED_MODEL} (downloads on first use)")
        _fastembed = TextEmbedding(model_name=FASTEMBED_MODEL)
    return [vector.tolist() for vector in _fastembed.embed(texts)]  # type: ignore[attr-defined]


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured backend, normalized to plain float lists.

    Raises on any backend failure so callers can decide whether to fail the build
    (boot) or degrade the query (search) — both of which log and carry on.
    """
    if BACKEND == "fastembed":
        return _embed_fastembed(texts)
    return _embed_ollama(texts)


def _embed_failure_hint() -> str:
    """Backend-specific "here's what to check" text for the degradation warning."""
    if BACKEND == "fastembed":
        return (
            f"is RAG_FASTEMBED_MODEL={FASTEMBED_MODEL} a model fastembed supports, "
            f"and can it reach Hugging Face to download it?"
        )
    return f"is `ollama serve` running at {OLLAMA_BASE_URL} with `ollama pull {EMBED_MODEL}`?"


@dataclass
class DocStore:
    """In-memory semantic index (Qdrant + local embeddings) over help docs.

    Attributes:
        key: The business/profile key whose ``data/<key>/docs/`` this indexes.
        chunks: The retrievable passages.
        min_score: Cosine floor for a chunk to count as relevant. Defaults to the
            floor calibrated for the active backend's score scale.
    """

    key: str
    chunks: list[DocChunk] = field(default_factory=list)
    min_score: float = field(default_factory=default_min_score)
    _client: QdrantClient | None = field(default=None, repr=False)

    @classmethod
    def load(cls, key: str, min_score: float | None = None) -> DocStore:
        """Load and embed ``data/<key>/docs/``.

        The store ends up empty — search finds nothing, the bot runs without
        document knowledge — when there are no docs, when ``RAG_BACKEND=none``, or
        when the backend fails to embed (all logged, never raised).
        """
        chunks = _read_chunks(DATA_ROOT / key / "docs")
        store = cls(
            key=key,
            chunks=chunks,
            min_score=default_min_score() if min_score is None else min_score,
        )
        if BACKEND == "none":
            logger.info(f"docs | {key}: RAG_BACKEND=none; search_docs will be empty")
        elif chunks:
            store._build_index()
        else:
            logger.info(f"docs | {key}: no documents found; search_docs will be empty")
        return store

    def _build_index(self) -> None:
        """Embed every chunk once and load them into an in-memory Qdrant collection.

        A failure here (Ollama not running, an unsupported fastembed model, a failed
        model download…) is logged, not raised: the store stays empty and the bot
        boots without document knowledge instead of dying. The catch is deliberately
        broad because each backend fails in its own vocabulary (urllib errors, ONNX
        runtime errors, HTTP-download errors) and none of them is worth a crash.
        """
        try:
            vectors = _embed([c.text for c in self.chunks])
        except Exception as e:
            logger.warning(
                f"docs | {self.key}: could not embed corpus with RAG_BACKEND={BACKEND}: {e}. "
                f"search_docs will be empty — {_embed_failure_hint()}"
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
        model = FASTEMBED_MODEL if BACKEND == "fastembed" else EMBED_MODEL
        logger.info(
            f"docs | {self.key}: indexed {len(self.chunks)} chunks in Qdrant "
            f"({BACKEND}/{model}, dim {len(vectors[0])}, min_score {self.min_score})"
        )

    def search(self, query: str, k: int = 3) -> list[tuple[DocChunk, float]]:
        """Return up to ``k`` (chunk, cosine) pairs above ``min_score``, best first.

        Empty when there are no docs, the index didn't build, or nothing clears the
        relevance floor — which the tool reports as "not in the corpus" so the bot
        declines rather than guessing. A query-time embedding failure degrades to
        empty too (logged), so one backend hiccup doesn't crash the turn.
        """
        if not self.chunks or self._client is None:
            return []
        try:
            query_vector = _embed([query])[0]
        except Exception as e:
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
