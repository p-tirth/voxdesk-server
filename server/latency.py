"""Per-turn latency ladder.

With ``enable_metrics=True`` the pipeline already emits per-stage timing
(time-to-first-byte for STT/LLM/TTS, text aggregation, function-call duration).
This module turns that stream into two things the PRD calls for:

  1. a readable per-turn *waterfall* printed after every bot turn, and
  2. an optional JSONL log the future scorecard (Phase 4) can aggregate for a
     p95 latency number.

It's built on Pipecat's own ``UserBotLatencyObserver``, which brackets each
user->bot cycle (VADUserStoppedSpeaking -> BotStartedSpeaking) and reports the
per-service breakdown — so we don't parse raw ``MetricsFrame``s by hand.

Note: the breakdown only fires when there's real VAD/turn-taking (a live call or
an audio-mode eval). In text-mode evals VAD is bypassed, so nothing prints —
which is fine, the ladder is about the audio path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)

# PRD target: p95 end-to-end turn latency under ~1.2s on the demo stack.
TARGET_E2E_SECS = 1.2


def create_latency_observer(jsonl_path: Path | None = None) -> UserBotLatencyObserver:
    """Build a latency observer that logs a ladder (and optionally writes JSONL).

    Attach it via ``PipelineWorker(..., observers=[observer])``.

    Args:
        jsonl_path: If given, append one JSON row per turn here (parent dirs are
            created on demand). If ``None``, only log to the console.
    """
    observer = UserBotLatencyObserver()

    # on_latency_measured fires just before on_latency_breakdown at each
    # BotStartedSpeaking, so stash the end-to-end number and read it below.
    state: dict[str, float | int | None] = {"e2e_secs": None, "turn": 0}

    @observer.event_handler("on_first_bot_speech_latency")
    async def _on_first_bot_speech(_obs, latency_secs: float):
        logger.info(f"latency | greeting: connect -> first speech {latency_secs * 1000:.0f} ms")

    @observer.event_handler("on_latency_measured")
    async def _on_latency_measured(_obs, latency_secs: float):
        state["e2e_secs"] = latency_secs

    @observer.event_handler("on_latency_breakdown")
    async def _on_latency_breakdown(_obs, breakdown: LatencyBreakdown):
        state["turn"] = int(state["turn"] or 0) + 1
        turn = int(state["turn"])
        e2e = state["e2e_secs"]
        logger.info("\n" + _format_ladder(turn, breakdown, e2e if isinstance(e2e, float) else None))
        if jsonl_path is not None:
            _append_jsonl(jsonl_path, turn, breakdown, e2e if isinstance(e2e, float) else None)
        state["e2e_secs"] = None

    return observer


def _format_ladder(turn: int, breakdown: LatencyBreakdown, e2e_secs: float | None) -> str:
    """Render a single turn's breakdown as an indented waterfall."""
    lines = [f"latency | turn {turn} ladder"]
    events = breakdown.chronological_events()
    if events:
        lines.extend(f"    {label}" for label in events)
    else:
        lines.append("    (no per-stage metrics for this turn)")
    if e2e_secs is not None:
        flag = "OK" if e2e_secs <= TARGET_E2E_SECS else "OVER"
        lines.append("    " + "-" * 40)
        lines.append(
            f"    end-to-end (user silence -> bot speech): {e2e_secs * 1000:.0f} ms "
            f"[{flag}, target <= {TARGET_E2E_SECS:.1f}s]"
        )
    return "\n".join(lines)


def _append_jsonl(
    path: Path, turn: int, breakdown: LatencyBreakdown, e2e_secs: float | None
) -> None:
    """Append one turn's metrics as a JSON line for later aggregation."""
    row = {
        "ts": time.time(),
        "turn": turn,
        "e2e_secs": e2e_secs,
        "user_turn_secs": breakdown.user_turn_secs,
        "ttfb": [
            {"processor": t.processor, "model": t.model, "secs": t.duration_secs}
            for t in breakdown.ttfb
        ],
        "text_aggregation_secs": (
            breakdown.text_aggregation.duration_secs if breakdown.text_aggregation else None
        ),
        "function_calls": [
            {"name": fc.function_name, "secs": fc.duration_secs}
            for fc in breakdown.function_calls
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        logger.warning(f"latency | could not write {path}: {e}")
