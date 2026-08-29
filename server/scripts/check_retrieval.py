"""Check (and recalibrate) document retrieval for the active embedding backend.

``rag.py``'s relevance floor is a *junk filter*, not the relevance decision — but
it is calibrated per embedding model, so swapping ``RAG_BACKEND`` (or either model
env var) invalidates it. This script is the repeatable measurement behind that
number: it loads the doc store exactly as the bot does, runs a fixed battery of
routing queries — grounded English, the same questions in Hinglish, and
off-corpus questions the corpus should *not* answer — and prints, per query, the
top-1 source doc, its score, and whether it clears the floor.

Two things to read off the output:

  * **Routing** — does each grounded question's #1 hit come from the right doc?
    That is the retrieval quality number, and it is what the tally reports.
  * **Score scale** — the grounded scores tell you where to set the floor for a new
    model: below the weakest grounded hit (Hinglish is always the weakest), with
    room to spare. Off-corpus questions *also* clearing the floor is expected and
    fine: their scores overlap the grounded ones on this corpus, which is exactly
    why the floor is lenient and ``search_docs`` leaves relevance to the LLM.

Run from server/::

    uv run python scripts/check_retrieval.py                       # active backend
    RAG_BACKEND=fastembed uv run python scripts/check_retrieval.py  # the CI backend
    RAG_BACKEND=fastembed uv run python scripts/check_retrieval.py --floor 0.25
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing rag: it reads RAG_BACKEND and the model names at
# import time. Real environment variables still win, so `RAG_BACKEND=fastembed
# uv run …` overrides the file.
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
load_dotenv(SERVER_DIR / ".env")

import rag  # noqa: E402
from business import get_active_profile  # noqa: E402

# The battery. Each row is (query, expected source doc, label) where the expected
# doc is the ``# heading`` of the file in data/<key>/docs/ that answers it, or None
# for a question the corpus genuinely cannot answer. The Hinglish block asks the
# same things as the English one so the two are directly comparable — that gap is
# the whole reason bge-m3 is the default backend.
RETURNS = "Returns and exchanges"
WARRANTY = "Warranty"
SHIPPING = "Shipping and delivery"
PAYMENTS = "Payments"

QUERIES: list[tuple[str, str | None, str]] = [
    # Grounded, English — at least one per doc topic.
    ("How many days do I have to return an item?", RETURNS, "en"),
    ("When will I get my refund after returning something?", RETURNS, "en"),
    ("How do I claim warranty on an appliance?", WARRANTY, "en"),
    ("How long does delivery take in Bengaluru?", SHIPPING, "en"),
    ("Is there a delivery charge on small orders?", SHIPPING, "en"),
    ("What payment methods do you accept?", PAYMENTS, "en"),
    # Grounded, Hinglish — same intents, code-mixed.
    ("Return karne ke liye kitna time milta hai?", RETURNS, "hi"),
    ("Refund kitne din mein aa jata hai?", RETURNS, "hi"),
    ("Warranty claim kaise karun?", WARRANTY, "hi"),
    ("Delivery kitne din mein ho jaati hai?", SHIPPING, "hi"),
    ("Delivery charge kitna lagta hai?", SHIPPING, "hi"),
    ("Cash on delivery ka option hai kya?", PAYMENTS, "hi"),
    # Off-corpus — nothing in the docs answers these.
    ("What are your store hours on Sunday?", None, "off"),
    ("Do you sell laptops?", None, "off"),
    ("What is the weather in Bengaluru today?", None, "off"),
    ("Can I apply for a job at your store?", None, "off"),
    ("Kya aap mobile phone repair karte ho?", None, "off"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floor",
        type=float,
        default=None,
        help="override the backend's calibrated min_score (for recalibrating it)",
    )
    args = parser.parse_args()

    floor = rag.default_min_score() if args.floor is None else args.floor
    profile = get_active_profile()
    # Load with no floor so every raw top-1 score is visible — the floor is applied
    # here, in the report, since it's the thing being checked.
    store = rag.DocStore.load(profile.key, min_score=-1.0)
    model = rag.FASTEMBED_MODEL if rag.BACKEND == "fastembed" else rag.EMBED_MODEL

    print(f"\nbusiness : {profile.key}  ({len(store.chunks)} chunks)")
    print(f"backend  : {rag.BACKEND}  model={model}")
    print(f"floor    : {floor}\n")

    if store._client is None:
        print("Store is empty — nothing to check (see the log line above for why).")
        return 1

    header = f"{'lang':4}  {'query':47}  {'top-1 source':22}  {'score':>6}  {'':4}"
    print(header)
    print("-" * len(header))

    grounded_hits = 0
    grounded_total = 0
    off_above_floor = 0
    grounded_scores: list[float] = []
    off_scores: list[float] = []

    for query, expected, lang in QUERIES:
        results = store.search(query, k=1)
        source, score = (results[0][0].source, results[0][1]) if results else ("-", 0.0)
        clears = score >= floor

        if expected is None:
            off_scores.append(score)
            off_above_floor += int(clears)
            # No pass/fail: an off-corpus question scoring above the floor is
            # expected here and is the LLM's call in the real tool.
            mark = "  ~ " if clears else "  · "
        else:
            grounded_total += 1
            grounded_scores.append(score)
            ok = source == expected and clears
            grounded_hits += int(ok)
            mark = "  ok" if ok else "FAIL"

        print(f"{lang:4}  {query:47.47}  {source:22.22}  {score:6.3f}  {mark}")

    print()
    print(f"grounded routed correctly : {grounded_hits}/{grounded_total}")
    if grounded_scores:
        print(
            f"grounded top-1 scores     : min {min(grounded_scores):.3f}  "
            f"max {max(grounded_scores):.3f}   (keep the floor below the min)"
        )
    if off_scores:
        print(
            f"off-corpus top-1 scores   : min {min(off_scores):.3f}  "
            f"max {max(off_scores):.3f}   "
            f"({off_above_floor}/{len(off_scores)} above the floor — by design; "
            f"the LLM judges relevance)"
        )
    return 0 if grounded_hits == grounded_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
