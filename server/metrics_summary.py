"""Latency scorecard: aggregate ``metrics/turns.jsonl`` into a markdown table.

``latency.py`` writes one JSON row per bot turn while the bot runs. This script
is the other half of that loop — it reads the accumulated rows and answers the
question the PRD's ship gate asks: *what is the p95 end-to-end turn latency, per
STT/LLM/TTS stack, and where does the time go?*

Run it from ``server/``::

    uv run python metrics_summary.py                  # print the scorecard
    uv run python metrics_summary.py --update-readme  # write it into README.md
    uv run python metrics_summary.py --include-eval   # ...eval-harness turns too

Stdlib only, on purpose: this is a reporting script, not part of the bot, and it
should stay runnable with no environment beyond Python.

**Honest reporting is the point.** Nothing here filters *outliers*. Cold-start
turns with a 15s LLM TTFB are real turns a user would have sat through, so they
stay in the percentiles, and a stack that misses the 1.2s target is published
with an ❌ rather than quietly dropped.

Two different things, don't conflate them: the no-outlier-filtering rule is about
*which measured turns count within a population*. Excluding eval rows is about
*which population is being measured at all* — a turn driven by the eval harness
shares the machine with the harness's own STT, TTS, and LLM judge, so its timings
measure that contention, not what a caller would experience. That's provenance
filtering, and the footer always publishes the counts it dropped.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Same target as latency.py's per-turn ladder — kept as a literal so this script
# has no import-time dependency on the bot's package environment.
TARGET_E2E_SECS = 1.2

DEFAULT_INPUT = Path(__file__).parent / "metrics" / "turns.jsonl"
DEFAULT_README = Path(__file__).parent.parent / "README.md"

# The README region this script owns. Both markers must already exist.
START_MARKER = "<!-- scorecard:start -->"
END_MARKER = "<!-- scorecard:end -->"


def _stage(processor: str) -> str:
    """Map a processor name to its pipeline stage.

    Processor names arrive instance-tagged (``GoogleLLMService#0``,
    ``SarvamTTSService#1``) — the ``#N`` is a per-process counter, so the *same*
    model shows up under different suffixes across sessions. Strip it and
    classify by class name.
    """
    cls = processor.split("#")[0]
    if "STT" in cls:
        return "stt"
    if "TTS" in cls:
        return "tts"
    return "llm"


@dataclass
class Group:
    """Accumulated turns for one stack combo."""

    llm: str
    tts: str
    stt_models: set[str] = field(default_factory=set)
    total_turns: int = 0
    e2e: list[float] = field(default_factory=list)
    llm_ttfb: list[float] = field(default_factory=list)
    tts_ttfb: list[float] = field(default_factory=list)
    fn_turns: int = 0

    @property
    def label(self) -> str:
        return f"{self.llm} + {self.tts}"

    @property
    def stt_label(self) -> str:
        return " / ".join(sorted(self.stt_models)) if self.stt_models else "—"


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: sort, take index ``ceil(p/100 * n) - 1``.

    No interpolation on purpose. Some combos have a handful of turns, and an
    interpolated p95 over n=6 invents a number that no turn actually took. The
    nearest-rank value is always a real measured turn, and it's deterministic —
    the same rows always produce the same scorecard.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[index]


def load_rows(path: Path) -> tuple[list[dict], int]:
    """Parse the JSONL log; return (usable rows, skipped-line count).

    A line is skipped if it isn't valid JSON, isn't an object, or has no
    ``ttfb`` list — an aborted write mid-session shouldn't kill the report.
    """
    rows: list[dict] = []
    skipped = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(row, dict) or not isinstance(row.get("ttfb"), list):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def row_mode(row: dict) -> str:
    """Provenance of a row: ``live``, ``eval``, or ``untagged``.

    ``latency.py`` stamps ``mode`` at write time from the session's transport.
    Rows written before that field existed have no ``mode`` key — and so does any
    row carrying a value this script doesn't recognise. Both are ``untagged``:
    unknown provenance, not known-good and not known-bad.
    """
    mode = row.get("mode")
    return mode if mode in ("live", "eval") else "untagged"


def select_rows(rows: list[dict], include_eval: bool) -> tuple[list[dict], dict[str, int]]:
    """Split rows by provenance; return (rows to aggregate, counts per mode).

    Default: ``live`` + ``untagged`` in, ``eval`` out. Untagged rows stay in
    because dropping them would blank the scorecard for every turn recorded
    before ``mode`` existed — an unknown-provenance turn is still a real turn.
    """
    counts = {"live": 0, "untagged": 0, "eval": 0}
    kept: list[dict] = []
    for row in rows:
        mode = row_mode(row)
        counts[mode] += 1
        if include_eval or mode != "eval":
            kept.append(row)
    return kept, counts


def group_turns(rows: list[dict]) -> dict[tuple[str, str], Group]:
    """Bucket turns by stack combo (LLM model + TTS model).

    Two schema facts shape this:

      * **A processor can report TTFB more than once in a turn.** When the LLM
        calls a tool it's invoked twice (once to decide, once to answer), so the
        row carries two LLM entries. We take the *first* per stage — that's the
        time-to-first-byte of the turn, which is what the user actually waits
        through before hearing anything.
      * **STT usually reports no TTFB at all.** Deepgram/Sarvam STT emit it on
        only some turns, so keying the group on "which stages appeared" would
        split one physical stack across two rows (43 turns of gemini-2.5-flash +
        sonic-3.5 become 23 and 20). The group key is therefore LLM+TTS — the
        stages that always report — and any STT model seen on those turns is
        carried alongside as a label, so it's still published when present.
    """
    groups: dict[tuple[str, str], Group] = {}
    for row in rows:
        first: dict[str, dict] = {}
        for entry in row["ttfb"]:
            if isinstance(entry, dict) and entry.get("processor"):
                first.setdefault(_stage(entry["processor"]), entry)
        llm = first.get("llm", {}).get("model") or "unknown-llm"
        tts = first.get("tts", {}).get("model") or "unknown-tts"

        group = groups.setdefault((llm, tts), Group(llm=llm, tts=tts))
        group.total_turns += 1
        if stt := first.get("stt", {}).get("model"):
            group.stt_models.add(stt)
        # e2e is null on greeting turns (no user utterance to measure from) and
        # on any turn the observer couldn't bracket. Those turns still count in
        # `total_turns` — the "n" column shows both, so a thin e2e sample is
        # visible rather than hidden.
        if isinstance(row.get("e2e_secs"), (int, float)):
            group.e2e.append(float(row["e2e_secs"]))
        for stage, bucket in (("llm", group.llm_ttfb), ("tts", group.tts_ttfb)):
            secs = first.get(stage, {}).get("secs")
            if isinstance(secs, (int, float)):
                bucket.append(float(secs))
        if row.get("function_calls"):
            group.fn_turns += 1
    return groups


def _cell(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "—"


def _row(name: str, stt: str, g: Group, bold: bool = False) -> str:
    """One markdown table row. ``bold`` marks the all-stacks summary line."""
    e50, e95 = percentile(g.e2e, 50), percentile(g.e2e, 95)
    # A stack with no measurable e2e can't be scored against the target.
    verdict = "n/a" if e95 is None else ("✅" if e95 <= TARGET_E2E_SECS else "❌")
    name = f"**{name}**" if bold else name
    return (
        f"| {name} | {stt} | {len(g.e2e)} / {g.total_turns} | {_cell(e50)} | {_cell(e95)} "
        f"| {_cell(percentile(g.llm_ttfb, 50))} | {_cell(percentile(g.llm_ttfb, 95))} "
        f"| {_cell(percentile(g.tts_ttfb, 50))} | {_cell(percentile(g.tts_ttfb, 95))} "
        f"| {g.fn_turns} | {verdict} |"
    )


def render(
    groups: dict[tuple[str, str], Group],
    source: Path,
    rows: int,
    skipped: int,
    counts: dict[str, int],
    include_eval: bool,
) -> str:
    """Render the whole scorecard as markdown."""
    scope = (
        "measured on real audio calls and eval-harness runs pooled together"
        if include_eval
        else "measured on real audio calls (eval-harness turns excluded)"
    )
    lines = [
        "### Latency scorecard",
        "",
        f"End-to-end turn latency (user stops speaking → bot starts speaking), "
        f"{scope}. Target: **p95 ≤ {TARGET_E2E_SECS:.1f}s** — rows that "
        f"miss it are marked ❌ and published anyway. All times in seconds; TTFB is "
        f"the first response per stage in a turn. Outliers (cold starts) are *not* "
        f"filtered.",
        "",
        "| Stack (LLM + TTS) | STT | Turns (e2e / all) | e2e p50 | e2e p95 "
        "| LLM p50 | LLM p95 | TTS p50 | TTS p95 | Tool turns | Target |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|",
    ]
    # Busiest stacks first — the ones with the most evidence behind them.
    for group in sorted(groups.values(), key=lambda g: (-g.total_turns, g.label)):
        lines.append(_row(group.label, group.stt_label, group))

    # Pooled row: the honest headline number across every turn ever recorded.
    overall = Group(llm="", tts="")
    for group in groups.values():
        overall.total_turns += group.total_turns
        overall.e2e += group.e2e
        overall.llm_ttfb += group.llm_ttfb
        overall.tts_ttfb += group.tts_ttfb
        overall.fn_turns += group.fn_turns
        overall.stt_models |= group.stt_models
    if groups:
        lines.append(_row("All stacks", overall.stt_label, overall, bold=True))

    # Show the source relative to server/ when it lives there — an absolute path
    # would bake this machine's home directory into the committed README.
    try:
        shown = f"server/{source.resolve().relative_to(Path(__file__).parent.resolve())}"
    except ValueError:
        shown = str(source)
    disposition = (
        "included via `--include-eval`"
        if include_eval
        else "excluded — `--include-eval` to include them"
    )
    provenance = (
        f"{counts['live']} live, {counts['untagged']} untagged, "
        f"{counts['eval']} eval rows {disposition}"
    )
    lines += [
        "",
        f"<sub>{rows} turns aggregated ({skipped} malformed line(s) skipped) from "
        f"`{shown}` — {provenance}. Regenerate with `uv run python "
        f"metrics_summary.py --update-readme`.</sub>",
    ]
    return "\n".join(lines)


def update_readme(readme: Path, scorecard: str) -> None:
    """Replace the text between the scorecard markers, keeping the markers.

    Raises ``SystemExit`` if either marker is missing — appending a scorecard to
    an arbitrary spot in the README would be worse than doing nothing.
    """
    if not readme.exists():
        sys.exit(f"error: README not found: {readme}")
    text = readme.read_text()
    start, end = text.find(START_MARKER), text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        sys.exit(
            f"error: {readme} is missing the scorecard markers "
            f"({START_MARKER} … {END_MARKER}); add them where the table should go."
        )
    head = text[: start + len(START_MARKER)]
    tail = text[end:]
    readme.write_text(f"{head}\n{scorecard}\n{tail}")
    print(f"updated {readme}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="turns.jsonl path")
    parser.add_argument("--readme", type=Path, default=DEFAULT_README, help="README to update")
    parser.add_argument(
        "--update-readme",
        action="store_true",
        help="write the scorecard between the scorecard markers in the README",
    )
    parser.add_argument(
        "--include-eval",
        action="store_true",
        help="also count turns recorded during eval-harness runs (excluded by default)",
    )
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(
            f"error: no metrics at {args.input} — run the bot on a live call first "
            f"(latency.py appends a row per turn), or pass --input."
        )
    rows, skipped = load_rows(args.input)
    if not rows:
        sys.exit(f"error: {args.input} has no usable turns ({skipped} malformed line(s)).")

    kept, counts = select_rows(rows, args.include_eval)
    if not kept:
        sys.exit(
            f"error: {args.input} has no scorecard-worthy turns — all "
            f"{counts['eval']} usable row(s) came from eval runs. Record a real "
            f"call, or pass --include-eval to report on the eval turns anyway."
        )

    scorecard = render(group_turns(kept), args.input, len(kept), skipped, counts, args.include_eval)
    if args.update_readme:
        update_readme(args.readme, scorecard)
    else:
        print(scorecard)


if __name__ == "__main__":
    main()
