"""Function-calling tools the LLM can invoke mid-conversation.

Each tool is a Pipecat *direct function*: an ``async def`` whose first parameter
is ``params`` (a ``FunctionCallParams``) and whose remaining, type-hinted
parameters plus docstring ``Args:`` become the tool's schema automatically — no
hand-written JSON schema, no separate registration step. Listing a tool on
``LLMContext(tools=[...])`` registers it (see ``bot.py``).

The tools are intentionally thin: they read the session's ``BusinessData`` (the
local catalog/orders, shared via ``PipelineWorker(app_resources=...)`` and reached
here as ``params.app_resources``), do a lookup, and report structured facts back
through ``params.result_callback``. The LLM then phrases the spoken answer — so
grounding lives in the data, not in the prompt.

Tools are per use case. ``tools_for(key)`` returns the set a given business
profile exposes, so a different profile can ship a different toolset without
touching the pipeline.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from catalog import BusinessData

# Where queued handoffs are appended, one JSON object per line. Same directory as
# the latency ladder's turns.jsonl (gitignored local output) — a stand-in for the
# ticketing system a real deployment would POST to.
ESCALATIONS_JSONL_PATH = Path(os.getenv("ESCALATIONS_JSONL", "metrics/escalations.jsonl").strip())

# The reason categories the LLM may pass. Kept small and closed so the escalation
# decision is *scoreable* — scripts/escalation_score.py labels scenarios by whether
# a handoff should happen, and a free-text reason would make the rows unaggregatable.
ESCALATION_REASONS = (
    "caller_requested_human",
    "complaint_or_dispute",
    "policy_exception",
    "lookup_failed",
    "payment_issue",
)


def _product_view(product: dict) -> dict:
    """The caller-facing subset of a product record (drops internal ids)."""
    return {
        "name": product.get("name"),
        "category": product.get("category"),
        "price_rupees": product.get("price_rupees"),
        "in_stock": product.get("in_stock"),
        "description": product.get("description"),
    }


async def search_products(
    params: FunctionCallParams,
    query: str | None = None,
    category: str | None = None,
) -> None:
    """Search the store's product catalog to answer what we sell or carry.

    Use this whenever the caller asks what the store sells, asks about a
    category (like appliances or kitchenware), or asks whether a kind of product
    is available. Call with no arguments to get an overview of every category.

    Args:
        query: Free-text of what the caller is looking for, e.g. "air fryer" or
            "coffee". Omit to list the whole catalog.
        category: Restrict to one category, e.g. "appliances", "kitchenware",
            or "home goods". Optional.
    """
    data: BusinessData = params.app_resources
    matches = data.search_products(query=query, category=category)
    if not matches:
        await params.result_callback(
            {
                "products": [],
                "categories_available": data.categories(),
                "note": (
                    "No matching products. Tell the caller you couldn't find that "
                    "and offer the categories the store does carry."
                ),
            }
        )
        return
    # Cap the payload so a broad query doesn't stuff the whole catalog into the
    # context; the LLM should summarize rather than read every item aloud.
    await params.result_callback(
        {
            "count": len(matches),
            "products": [_product_view(p) for p in matches[:10]],
            "categories_available": data.categories(),
        }
    )


async def check_stock(params: FunctionCallParams, product: str) -> None:
    """Check whether a specific product is in stock right now.

    Use this when the caller asks if a particular item is available or how many
    are left.

    Args:
        product: The product name to check, e.g. "air fryer" or "pressure
            cooker".
    """
    data: BusinessData = params.app_resources
    match = data.find_product(product)
    if not match:
        await params.result_callback(
            {
                "found": False,
                "query": product,
                "categories_available": data.categories(),
                "note": (
                    "No such product in the catalog. Don't invent stock; offer to "
                    "look for something similar."
                ),
            }
        )
        return
    await params.result_callback(
        {
            "found": True,
            "name": match.get("name"),
            "in_stock": match.get("in_stock"),
            "stock_count": match.get("stock_count"),
            "price_rupees": match.get("price_rupees"),
        }
    )


async def search_docs(params: FunctionCallParams, question: str) -> None:
    """Answer questions about store policies and how things work.

    Use this for questions about returns, exchanges, refunds, warranty, shipping,
    delivery, or payment — anything about the store's policies or process rather
    than a specific product, stock level, or order. It searches the store's help
    documents and returns the relevant passages; answer from them. If it finds
    nothing relevant, say you don't have that information and offer to take the
    caller's details or connect a human — do not invent a policy.

    Args:
        question: What the caller wants to know, phrased as a search query **in
            English** — even when the caller spoke Hinglish or Hindi. The documents
            are written in English, so an English query retrieves far better than a
            transliterated one (e.g. for "warranty claim kaise karun?" search "how
            do I claim warranty on an appliance"). Keep it to a single intent; ask
            again for a second one.
    """
    docs = getattr(params.app_resources, "docs", None)
    hits = docs.search(question) if docs else []
    if not hits:
        await params.result_callback(
            {
                "found": False,
                "question": question,
                "note": (
                    "No relevant policy document. Don't guess a policy; tell the "
                    "caller you don't have that information and offer to take their "
                    "details or connect a human."
                ),
            }
        )
        return
    # Retrieval is recall-first, so the closest passages may still not address the
    # question. Hand them over with an explicit instruction to judge relevance —
    # answer only if they actually cover it, otherwise decline. This is what makes
    # the bot refuse off-corpus questions instead of forcing an answer from a
    # loosely-similar passage.
    await params.result_callback(
        {
            "found": True,
            "passages": [{"source": chunk.source, "text": chunk.text} for chunk, _ in hits],
            "note": (
                "Answer only if these passages actually address the caller's "
                "question. If they don't, say you don't have that information and "
                "offer to take details or connect a human — do not force an answer "
                "from an unrelated passage or invent a policy."
            ),
        }
    )


async def get_order_status(params: FunctionCallParams, order_number: str) -> None:
    """Look up the status of an existing order by its order number.

    Use this only once the caller has given an order number. If they haven't,
    ask for it first instead of calling this.

    Args:
        order_number: The caller's order number, digits only, e.g. "543131".
    """
    data: BusinessData = params.app_resources
    order = data.get_order(order_number)
    if not order:
        await params.result_callback(
            {
                "found": False,
                "order_number": str(order_number),
                "note": (
                    "No order with that number. Ask the caller to double-check it; "
                    "don't invent a status."
                ),
            }
        )
        return
    await params.result_callback(
        {
            "found": True,
            "order_number": str(order_number),
            "status": order.get("status"),
            "items": order.get("items"),
            "placed_on": order.get("placed_on"),
            "eta": order.get("eta"),
            "carrier": order.get("carrier"),
        }
    )


async def escalate_to_human(
    params: FunctionCallParams,
    reason: str,
    summary: str,
    callback_number: str | None = None,
) -> None:
    """Hand the caller off to a human support agent, with a written summary.

    Call this when the conversation is genuinely beyond what you can resolve:
    the caller asks to speak to a person, they are making a complaint or
    disputing a refund, they want an exception to a stated policy, an order
    lookup failed after they confirmed the number, or there is a payment or
    billing problem. Do NOT call it for ordinary questions the product, stock,
    order, or document tools can answer, and do NOT call it just because a
    document search came back with nothing — in that case decline honestly and
    *offer* a human, and only call this if the caller accepts the offer.

    It queues a ticket for a human agent; nobody joins the call. After calling
    it, tell the caller in one short line what you are passing on and that a
    person will follow up. Don't read the ticket id aloud unless they ask.

    Args:
        reason: Why the handoff is needed — exactly one of
            "caller_requested_human" (they asked for a person),
            "complaint_or_dispute" (a complaint, or a contested refund/return),
            "policy_exception" (they want an exception to a stated policy),
            "lookup_failed" (an order or record couldn't be found after the
            caller confirmed the details), or "payment_issue" (a payment,
            charge, or billing problem).
        summary: One sentence the human agent will read before calling back:
            who the caller is (if known), what they want, and what you already
            tried. Write it in English, in the third person, e.g. "Caller says
            order 543131 was charged twice; confirmed the number and the order
            lookup shows a single shipped order."
        callback_number: The caller's phone number, if they gave one. Omit it if
            they haven't — never invent or guess a number.
    """
    normalized = str(reason).strip().lower()
    if normalized not in ESCALATION_REASONS:
        # Don't reject the handoff over a bad label — a caller waiting on a human
        # matters more than a clean enum — but keep the row honest and greppable.
        logger.warning(f"escalation | unknown reason {reason!r}; recording as 'other'")
        normalized = "other"

    ticket_id = f"ESC-{uuid.uuid4().hex[:6].upper()}"
    row = {
        "ts": time.time(),
        "ts_iso": datetime.now(UTC).isoformat(timespec="seconds"),
        "ticket_id": ticket_id,
        "reason": normalized,
        "summary": str(summary).strip(),
        "callback_number": callback_number,
    }
    try:
        ESCALATIONS_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ESCALATIONS_JSONL_PATH.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        # A ticket that couldn't be written is still a handoff the caller was
        # promised, so report success to the LLM and make the failure loud here.
        logger.warning(f"escalation | could not write {ESCALATIONS_JSONL_PATH}: {e}")

    logger.info(f"escalation | {ticket_id} reason={normalized} summary={row['summary']!r}")
    await params.result_callback(
        {
            "ticket_id": ticket_id,
            "status": "queued",
            "message": (
                "A human support agent has been queued and will follow up. Tell the "
                "caller in one short line what you're passing on and that a person "
                "will get back to them — don't read the ticket id aloud unless asked."
            ),
        }
    )


# Which tools each business profile exposes. Add a use case by adding its key
# here (and its data folder + profile). Profiles not listed simply have no tools.
TOOLSETS: dict[str, list] = {
    "store": [search_products, check_stock, get_order_status, search_docs, escalate_to_human],
}


def tools_for(key: str) -> list:
    """Return the list of tool functions for a business profile ``key``."""
    return TOOLSETS.get(key, [])
