"""Configurable business profiles.

The agent's use case is deliberately *not* hard-coded. One profile in this file
is the single source of truth that drives both:

  1. the bot's persona (its system prompt, built by ``system_instruction()``), and
  2. the eval scenarios written against it (see ``evals/<profile-key>/``).

Switch the whole use case by setting ``BUSINESS`` in ``.env`` to another profile
key; add a new use case by adding one entry to ``PROFILES`` (and a matching
``evals/<key>/`` folder). Nothing else in ``bot.py`` needs to change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Voice-safe guard — carried into every profile's system prompt. TTS reads
# exactly what the LLM writes, so formatting that can't be spoken is noise.
VOICE_SAFE_GUARD = (
    "Your responses will be spoken aloud, so avoid emojis, bullet points, or "
    "other formatting that can't be spoken. Keep replies brief and natural."
)

# The wedge (see the PRD): callers speak Hinglish or English; match them. This is
# the default; a profile can override the tone via ``language_style`` (below).
LANGUAGE_GUARD = (
    "Callers may speak in Hinglish (a natural mix of Hindi and English) or in "
    "English. Reply in the same language and style the caller uses."
)

# Added to the prompt only for profiles that expose tools (see tools.py). Nudges
# the LLM to look facts up instead of guessing, and to keep answers short since
# tool results can be long.
TOOL_GUARD = (
    "You have tools to look up live product, stock, and order information. When a "
    "caller asks about products, availability, prices, or an order, call the "
    "relevant tool and answer from its result instead of guessing. Summarize "
    "briefly — don't read long lists aloud; offer a few options and ask what they "
    "want to hear more about."
)


@dataclass(frozen=True)
class BusinessProfile:
    """A single support-desk use case.

    Attributes:
        key: Short identifier used by ``BUSINESS`` and the ``evals/<key>/`` folder.
        display_name: The business's name, spoken to callers.
        role: One clause describing who the bot is, e.g. "the support desk for X".
        description: A sentence or two about what the business does.
        capabilities: What this desk can help callers with (informs the prompt
            and the eval scenarios).
        facts: A few ground-truth facts the bot can rely on (small, spoken-often
            details like hours or location). Structured data the caller queries —
            the product catalog, orders — lives in ``data/<key>/`` and is reached
            through tools, not here.
        uses_tools: Whether this profile exposes function-calling tools (see
            ``tools.py``). When true, the system prompt gains tool guidance.
        tts_language: BCP-47-ish language code handed to language-aware TTS
            engines (e.g. Sarvam) so they pronounce this profile's speech
            correctly. ``hi-IN`` handles Hindi *and* embedded English, which is
            what a Hinglish desk wants; ``en-IN`` for an English-first desk.
            Ignored by engines that don't take a language (e.g. Cartesia's
            single-voice default).
        language_style: Optional override for the default language guidance
            (``LANGUAGE_GUARD``). Use it to set the bot's tone — e.g. lean a
            little Hinglish. ``None`` uses the neutral "mirror the caller" guard.
        greeting: Instruction for the bot's opening turn (a ``developer`` message,
            not a fixed line — the LLM phrases it).
    """

    key: str
    display_name: str
    role: str
    description: str
    capabilities: list[str]
    facts: dict[str, str] = field(default_factory=dict)
    uses_tools: bool = False
    tts_language: str = "en-IN"
    language_style: str | None = None
    greeting: str = (
        "Greet the caller warmly, say who you are in one short sentence, and "
        "ask how you can help."
    )

    def system_instruction(self) -> str:
        """Compose the full system prompt for this business."""
        parts = [
            f"You are {self.role}. {self.description}",
            "You can help callers with: " + "; ".join(self.capabilities) + ".",
            (
                "Answer only from what you actually know about this business. If "
                "you don't know something, or it needs an action or live lookup "
                "you can't do yet, say so honestly and offer to take the caller's "
                "details or hand them to a human — never invent facts, prices, "
                "stock, or order details."
            ),
        ]
        if self.facts:
            known = " ".join(f"{k}: {v}." for k, v in self.facts.items())
            parts.append("Facts you can rely on — " + known)
        if self.uses_tools:
            parts.append(TOOL_GUARD)
        parts.append(self.language_style or LANGUAGE_GUARD)
        parts.append(VOICE_SAFE_GUARD)
        return " ".join(parts)


PROFILES: dict[str, BusinessProfile] = {
    "store": BusinessProfile(
        key="store",
        display_name="Mango & Co.",
        role="the phone support desk for Mango & Co., a small home and kitchen store",
        description=(
            "Mango & Co. sells kitchenware, small appliances, and home goods, both "
            "in-store and online."
        ),
        capabilities=[
            "telling callers what products the store sells, across appliances, "
            "kitchenware, and home goods, with prices",
            "checking whether a specific item is in stock",
            "looking up the status of an existing order by its order number",
            "explaining how returns and exchanges work",
            "sharing store hours, location, and contact details",
        ],
        facts={
            "store hours": "open 10 AM to 9 PM every day",
            "location": "Indiranagar, Bengaluru",
            "return window": "7 days from delivery, with the receipt",
            "phone number": "080-4000-1234",
        },
        uses_tools=True,
        # A Bengaluru store desk: Hindi/Hinglish-first pronunciation, which also
        # handles the English product names well.
        tts_language="hi-IN",
        # No language_style override: the bot uses the default LANGUAGE_GUARD and
        # simply mirrors the caller's language rather than leaning into Hinglish.
        greeting=(
            "Greet the caller warmly, say in one short sentence that you're the "
            "Mango and Co. support desk, and ask how you can help."
        ),
    ),
}

DEFAULT_BUSINESS = "store"


def get_active_profile() -> BusinessProfile:
    """Return the profile selected by ``BUSINESS`` in the environment.

    Fails fast with a clear message on an unknown key, so a misconfigured
    ``.env`` surfaces at boot instead of mid-call.
    """
    key = os.getenv("BUSINESS", DEFAULT_BUSINESS).strip()
    if key not in PROFILES:
        raise ValueError(
            f"Unknown BUSINESS={key!r}. Choose one of: {', '.join(PROFILES)}"
        )
    return PROFILES[key]
