"""Judge LLM factory for the behavioral eval harness.

The eval judge scores natural-language criteria (``eval: "the bot says ..."``).
Out of the box Pipecat's judge only knows ``ollama`` (needs a ~5 GB local model
pull) and ``openai`` (needs an OpenAI key). This factory lets the evals reuse the
``GOOGLE_API_KEY`` you already have instead: Gemini exposes an OpenAI-compatible
endpoint, and the judge only needs a service with ``run_inference()``, which a
plain ``OpenAILLMService`` pointed at that endpoint provides.

Wired from a scenario's judge block (see ``evals/_shared/judge_eval.yaml``)::

    judge:
      eval: !include ../_shared/judge_eval.yaml

To switch the judge, edit that one shared file — e.g. ``service: ollama`` (after
``ollama pull gemma2:9b``) or ``service: openai`` (with ``OPENAI_API_KEY`` set).
"""

import os

from dotenv import load_dotenv
from pipecat.services.openai.llm import OpenAILLMService

# The eval harness runs in its own process (the `pipecat`/`python -m pipecat.evals`
# CLI), which does not load the bot's .env — so load it here so the factory can
# read GOOGLE_API_KEY from server/.env.
load_dotenv()

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def gemini_judge(config: dict):
    """Build a judge LLM backed by Gemini via its OpenAI-compatible endpoint.

    Args:
        config: The scenario's ``judge.eval`` mapping. Honors ``model``
            (default ``gemini-2.5-flash``).
    """
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "gemini_judge needs GOOGLE_API_KEY in the environment (server/.env)."
        )
    model = config.get("model", "gemini-2.5-flash")
    return OpenAILLMService(
        api_key=api_key,
        base_url=GEMINI_OPENAI_BASE_URL,
        settings=OpenAILLMService.Settings(
            model=model,
            temperature=0,
            # Pipecat's EvalJudge caps the reply at 200 tokens, and Gemini 2.5 is a
            # thinking model whose hidden reasoning counts against that cap on the
            # OpenAI-compatible endpoint. Left alone it spends the whole budget
            # thinking and the verdict arrives truncated — measured: 0/20 replies
            # were parseable JSON, every one cut off at '{"verdict": "' — and the
            # harness then guesses from whichever of "yes"/"no" survived. Turning
            # reasoning off and forcing JSON output gave 20/20 well-formed
            # verdicts. A one-line grading call doesn't need chain-of-thought.
            extra={
                "reasoning_effort": "none",
                "response_format": {"type": "json_object"},
            },
        ),
    )
