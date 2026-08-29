"""Barge-in measurement: what actually happens when a caller talks over the bot.

The pipeline already *handles* interruptions — Silero VAD detects the caller,
the user aggregator broadcasts an ``InterruptionFrame``, and the output
transport flushes the in-flight audio. None of that produced a number. This
module is the number.

It watches the frames that bracket a barge-in and writes one JSON row per
*overlap event* — every time the caller's speech starts while the bot's audio is
playing — so the scorecard side (``bargein_summary.py``) can report three things
the PRD's honesty thesis needs:

  1. **time-to-silence** — caller starts speaking over the bot → the bot's audio
     actually stops. This is the only interruption number a caller feels.
  2. **missed interruptions** — the caller said something real over the bot and
     the bot kept talking.
  3. **false barge-ins** — the caller emitted a backchannel ("hmm", "haan",
     "okay") and the bot stopped anyway, when it should have kept going.

WHICH FRAMES, AND WHY
---------------------
Verified against the installed Pipecat (1.5.0) rather than assumed:

* ``VADUserStartedSpeakingFrame`` — the *earliest* evidence the caller opened
  their mouth. Broadcast by the user aggregator's VAD callback
  (``_on_vad_speech_started``). Note it fires only after the VAD's ``start_secs``
  confirmation window (default 0.2s) has already elapsed, so every number here
  excludes that window — see ``vad_start_secs`` on the row.
* ``UserStartedSpeakingFrame`` / ``InterruptionFrame`` — the turn-start path.
  ``_on_user_turn_started`` broadcasts the first, then calls
  ``broadcast_interruption()`` which broadcasts the second, both directions.
  The VAD frame is queued separately from the turn callback, so its ordering
  against these two isn't guaranteed; we anchor on whichever of the three
  arrives first and record which one it was in ``anchor``.
* ``BotStoppedSpeakingFrame`` — the *stop*. This is the load-bearing choice:
  ``BaseOutputTransport.MediaSender.handle_interruptions()`` cancels the audio
  task, clears the buffers, and only then calls ``_bot_stopped_speaking()``.
  So this frame marks the moment the transport genuinely has nothing left to
  play, not the moment somebody asked it to stop.

That also gives us the miss/false-positive signal for free: the *same* frame is
emitted when a bot turn simply finishes. A stop preceded by an
``InterruptionFrame`` was caused by the barge-in; a stop with no interruption
means the bot talked right through the caller.

Frames are seen once per pipeline hop and once per direction, so every
transition here is edge-triggered (act only when the state actually flips)
rather than deduplicated by frame id — the upstream and downstream copies are
distinct objects with distinct ids.

Like ``latency.py``, this only fires when there's real VAD/turn-taking (a live
call or an audio-mode eval). Text-mode evals bypass VAD entirely, so nothing is
written there — which is correct: you cannot barge into text.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    StopFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

# Short acknowledgements a listener drops on top of a speaker without meaning
# "stop talking". English plus the Hinglish ones this desk actually hears.
# Deliberately a small, explicit list: it is a *heuristic label* on the measured
# row, not a decision the bot makes — the bot's behaviour is unchanged either
# way. Widening it would quietly reclassify real interruptions as backchannels
# and flatter the numbers.
BACKCHANNELS = frozenset(
    {
        "a",  # Deepgram's rendering of a grunted "haan"
        "acha",
        "accha",
        "achha",
        "ah",
        "aha",
        "ha",
        "haan",
        "han",
        "hanji",
        "hm",
        "hmm",
        "hmmm",
        "huh",
        "ji",
        "mhm",
        "mm",
        "mmhmm",
        "ok",
        "okay",
        "okey",
        "right",
        "sure",
        "theek",
        "thik",
        "uh",
        "uhhuh",
        "um",
        "yeah",
        "yep",
        "yes",
        "yup",
    }
)

# A backchannel is short by definition. "okay so about my order" is a real
# interruption that happens to start with an acknowledgement token.
MAX_BACKCHANNEL_WORDS = 2

_WORD_RE = re.compile(r"[a-z]+")


def classify(transcript: str) -> str:
    """Label the overlapping utterance: ``backchannel``, ``utterance``, or ``no_speech``.

    ``no_speech`` means VAD fired but STT returned nothing for the overlap — a
    cough, a door, or a noise floor blip. It shares the backchannel's
    expectation (the bot should *not* stop), because a bot that goes silent for
    a noise is doing the same wrong thing as one that goes silent for an "hmm".
    """
    words = _WORD_RE.findall(transcript.lower())
    if not words:
        return "no_speech"
    if len(words) <= MAX_BACKCHANNEL_WORDS and all(w in BACKCHANNELS for w in words):
        return "backchannel"
    return "utterance"


def outcome_of(kind: str, stopped: bool) -> str:
    """Score one overlap: was stopping (or not stopping) the right call?

    Returns one of ``clean_bargein`` / ``missed`` / ``false_bargein`` /
    ``correct_hold``. ``bargein_summary.py`` does nothing but count these, so the
    judgement lives here, next to the definitions it depends on.
    """
    if kind == "utterance":
        return "clean_bargein" if stopped else "missed"
    return "false_bargein" if stopped else "correct_hold"


class BargeInObserver(BaseObserver):
    """Records one row per overlap of caller speech onto bot audio.

    Attach it via ``PipelineWorker(..., observers=[observer])``.
    """

    def __init__(self, jsonl_path: Path | None = None, mode: str = "live"):
        """Build the observer.

        Args:
            jsonl_path: If given, append one JSON row per overlap here (parent
                dirs are created on demand). ``None`` logs to the console only.
            mode: Provenance stamped on every row — ``"live"`` for a real call,
                ``"eval"`` for a harness-driven session. Same contract as
                ``latency.py``: an eval barge-in is synthesized speech through a
                loaded machine, so it must not be pooled with a real one. The
                caller derives it from the transport so it can't be forgotten.
        """
        super().__init__()
        self._jsonl_path = jsonl_path
        self._mode = mode
        self._bot_speaking = False
        self._bot_started_at: float | None = None
        self._count = 0
        # The overlap currently being measured, if any. See _open/_resolve/_flush.
        self._pending: dict | None = None

    async def on_push_frame(self, data: FramePushed):
        """Drive the overlap state machine off the frames listed in the module docstring."""
        frame = data.frame
        now = time.time()

        if isinstance(frame, BotStartedSpeakingFrame):
            # A new bot turn is the boundary that closes out the previous
            # overlap: by now its outcome and its transcript are both known.
            await self._flush(force=True)
            if not self._bot_speaking:
                self._bot_speaking = True
                self._bot_started_at = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._bot_speaking:
                self._bot_speaking = False
                self._resolve(now)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._open("vad_user_started_speaking", now, vad_start_secs=frame.start_secs)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._open("user_started_speaking", now)
        elif isinstance(frame, InterruptionFrame):
            # Anchors the overlap if it somehow arrives first, and in every case
            # marks this overlap as one the pipeline decided to act on.
            self._open("interruption", now)
            if self._pending is not None and self._pending["interrupted_at"] is None:
                self._pending["interrupted_at"] = now
        elif isinstance(frame, TranscriptionFrame):
            if self._pending is not None and frame.text.strip():
                self._pending["transcript"] = f"{self._pending['transcript']} {frame.text}".strip()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            # The caller's turn is over, so every STT segment of the overlapping
            # utterance has landed and it can finally be classified. Waiting for
            # this rather than flushing on the first TranscriptionFrame matters:
            # STT splits on pauses, and "Okay, so about my order..." arrives as
            # "Okay." first — classifying on that segment alone would file a real
            # interruption as a backchannel and inflate the false-barge-in rate.
            await self._flush()
        elif isinstance(frame, (EndFrame, CancelFrame, StopFrame)):
            # Last chance: the session is going away. Publish whatever we have
            # rather than losing the final barge-in of a run.
            await self._flush(force=True)

    def _open(self, anchor: str, now: float, vad_start_secs: float = 0.0) -> None:
        """Start measuring an overlap, if the bot is speaking and one isn't already open."""
        if not self._bot_speaking or self._pending is not None:
            return
        self._count += 1
        self._pending = {
            "event": self._count,
            "anchor": anchor,
            "vad_start_secs": vad_start_secs,
            "started_at": now,
            "overlap_secs": (
                round(now - self._bot_started_at, 4) if self._bot_started_at else None
            ),
            "interrupted_at": None,
            "stopped_at": None,
            "transcript": "",
            "resolved": False,
        }

    def _resolve(self, now: float) -> None:
        """The bot's audio went silent — decide whether this overlap caused it."""
        if self._pending is None or self._pending["resolved"]:
            return
        self._pending["resolved"] = True
        # Only a stop that followed an InterruptionFrame was caused by the
        # caller. Without one, the bot simply reached the end of its turn and
        # talked straight through the barge-in.
        if self._pending["interrupted_at"] is not None:
            self._pending["stopped_at"] = now

    async def _flush(self, force: bool = False) -> None:
        """Emit the pending row once its outcome (and ideally its transcript) is known.

        Normally this fires when the caller's turn ends, by which point the STT
        segments needed to classify the utterance have all arrived. ``force`` is
        the boundary path (next bot turn, or pipeline shutdown) and publishes
        even a row whose STT returned nothing, which is itself a finding.
        """
        pending = self._pending
        if pending is None:
            return
        if not pending["resolved"]:
            if not force:
                return
            # Forced at a boundary with the bot still notionally speaking: the
            # overlap never produced a stop, so publish it as one that didn't.
            pending["resolved"] = True
        if not force and not pending["transcript"]:
            return
        self._pending = None

        stopped = pending["stopped_at"] is not None
        kind = classify(pending["transcript"])
        row = {
            "ts": time.time(),
            # Same provenance contract as latency.py — stamped at write time.
            "mode": self._mode,
            "event": pending["event"],
            # Which frame anchored the measurement (see the module docstring).
            "anchor": pending["anchor"],
            # The VAD confirmation window already spent before the anchor fired;
            # add it to time_to_silence for speech-onset-to-silence.
            "vad_start_secs": pending["vad_start_secs"],
            # How long the bot had been talking when the caller cut in.
            "overlap_secs": pending["overlap_secs"],
            "interrupted": pending["interrupted_at"] is not None,
            "stopped": stopped,
            "time_to_silence_secs": (
                round(pending["stopped_at"] - pending["started_at"], 4) if stopped else None
            ),
            # Sub-breakdown, for finding where a slow stop went slow.
            "interrupt_dispatch_secs": (
                round(pending["interrupted_at"] - pending["started_at"], 4)
                if pending["interrupted_at"] is not None
                else None
            ),
            "flush_secs": (
                round(pending["stopped_at"] - pending["interrupted_at"], 4) if stopped else None
            ),
            "transcript": pending["transcript"],
            "kind": kind,
            # What the bot *should* have done with an utterance of this kind.
            "expected_stop": kind == "utterance",
            "outcome": outcome_of(kind, stopped),
        }

        tts = row["time_to_silence_secs"]
        logger.info(
            f"barge-in | #{row['event']} {row['outcome']} ({kind}) "
            f"time-to-silence {f'{tts * 1000:.0f} ms' if tts is not None else 'n/a — bot kept talking'} "
            f"| heard {row['transcript']!r}"
        )
        if self._jsonl_path is not None:
            _append_jsonl(self._jsonl_path, row)


def create_bargein_observer(jsonl_path: Path | None = None, mode: str = "live") -> BargeInObserver:
    """Build a barge-in observer. See ``BargeInObserver.__init__`` for the contract."""
    return BargeInObserver(jsonl_path=jsonl_path, mode=mode)


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one overlap as a JSON line for later aggregation."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        logger.warning(f"barge-in | could not write {path}: {e}")
