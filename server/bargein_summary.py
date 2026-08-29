"""Barge-in scorecard: aggregate ``metrics/bargein.jsonl`` into a markdown table.

``bargein.py`` writes one JSON row every time the caller's speech starts while
the bot's audio is playing. This script is the other half of that loop — it
reads the accumulated rows and answers the three questions the barge-in claim
rests on:

    how fast does the bot go quiet, how often does it fail to stop when it
    should, and how often does it stop when it shouldn't?

Run it from ``server/``::

    uv run python bargein_summary.py                  # print the table
    uv run python bargein_summary.py --update-readme  # write it into README.md

Stdlib only, on purpose — same reason as ``metrics_summary.py``: this is a
reporting script, not part of the bot, and it should stay runnable with no
environment beyond Python.

**Nothing is filtered.** Percentiles are nearest-rank, so every published number
is a real measured overlap rather than an interpolation between two of them, and
a slow stop stays in the p95 instead of being trimmed. The three outcome rates
are plain counts over plain denominators.

Where this differs from the latency scorecard: that one *excludes* eval rows by
default, because a live call is the population it claims to describe. Here
almost every row is an eval row — barge-in needs someone talking over the bot,
which the harness can script and a human tester mostly can't be bothered to do
reproducibly — so hiding them would leave an empty table. Instead every
provenance bucket gets its own row and the reader can see exactly which
population each number came from. The pooled row is last, and labelled.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INPUT = Path(__file__).parent / "metrics" / "bargein.jsonl"
DEFAULT_README = Path(__file__).parent.parent / "README.md"

# The README region this script owns. Both markers must already exist.
START_MARKER = "<!-- bargein:start -->"
END_MARKER = "<!-- bargein:end -->"

# Row provenance, in the order the table shows it. Same vocabulary as
# metrics_summary.py: "untagged" is a row written before ``mode`` existed, or
# one carrying a value this script doesn't recognise.
MODES = ("live", "eval", "untagged")


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: sort, take index ``ceil(p/100 * n) - 1``.

    No interpolation, for the same reason ``metrics_summary.py`` gives: over a
    handful of overlaps an interpolated p95 invents a number no barge-in
    actually took.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[index]


@dataclass
class Bucket:
    """Accumulated overlaps for one provenance bucket."""

    name: str
    overlaps: int = 0
    # Time-to-silence for every overlap the bot actually went quiet for.
    stop_secs: list[float] = field(default_factory=list)
    # Denominators: overlaps the bot *should* have stopped for, and shouldn't.
    utterances: int = 0
    missed: int = 0
    holds: int = 0
    false_bargeins: int = 0

    def add(self, row: dict) -> None:
        """Fold one JSONL row in. The row already carries its own verdict."""
        self.overlaps += 1
        secs = row.get("time_to_silence_secs")
        if isinstance(secs, (int, float)):
            self.stop_secs.append(float(secs))
        outcome = row.get("outcome")
        if outcome in ("clean_bargein", "missed"):
            self.utterances += 1
            self.missed += outcome == "missed"
        elif outcome in ("false_bargein", "correct_hold"):
            self.holds += 1
            self.false_bargeins += outcome == "false_bargein"


def load_rows(path: Path) -> tuple[list[dict], int]:
    """Parse the JSONL log; return (usable rows, skipped-line count).

    A line is skipped if it isn't valid JSON, isn't an object, or carries no
    ``outcome`` — a row bargein.py couldn't score is a row this can't either.
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
        if not isinstance(row, dict) or not row.get("outcome"):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def row_mode(row: dict) -> str:
    """Provenance of a row: ``live``, ``eval``, or ``untagged``."""
    mode = row.get("mode")
    return mode if mode in ("live", "eval") else "untagged"


def bucket_rows(rows: list[dict]) -> dict[str, Bucket]:
    """Split rows by provenance, keeping only the buckets that have any."""
    buckets = {mode: Bucket(mode) for mode in MODES}
    for row in rows:
        buckets[row_mode(row)].add(row)
    return {name: b for name, b in buckets.items() if b.overlaps}


def _ms(value: float | None) -> str:
    return f"{value * 1000:.1f}" if value is not None else "—"


def _rate(hits: int, total: int) -> str:
    """``n/total (pct)``, or an em dash when the denominator is empty.

    A rate with no denominator is not zero, it's unmeasured — printing 0% for
    "we never tried" would be the exact dishonesty this report exists to avoid.
    """
    if total == 0:
        return "— (0)"
    return f"{hits}/{total} ({hits / total * 100:.0f}%)"


def _row(name: str, b: Bucket, bold: bool = False) -> str:
    """One markdown table row. ``bold`` marks the pooled summary line."""
    label = f"**{name}**" if bold else name
    return (
        f"| {label} | {b.overlaps} | {len(b.stop_secs)} "
        f"| {_ms(percentile(b.stop_secs, 50))} | {_ms(percentile(b.stop_secs, 95))} "
        f"| {_rate(b.missed, b.utterances)} | {_rate(b.false_bargeins, b.holds)} |"
    )


def render(buckets: dict[str, Bucket], source: Path, skipped: int) -> str:
    """Render the whole barge-in table as markdown."""
    lines = [
        "### Barge-in scorecard",
        "",
        "One row per *overlap* — every time the caller's speech started while the "
        "bot's audio was playing. **Time-to-silence** is measured from the VAD "
        "confirming the caller's speech to the output transport reporting its audio "
        "stopped, over every overlap the bot did stop for. **Missed** counts real "
        "utterances the bot talked straight through; **false barge-ins** counts "
        'backchannels ("hmm", "haan", "okay") and noise it stopped for anyway. '
        "Percentiles are nearest-rank and nothing is filtered.",
        "",
        "| Source | Overlaps | Stops | Time-to-silence p50 (ms) | p95 (ms) "
        "| Missed / real utterances | False / backchannel+noise |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in MODES:
        if name in buckets:
            lines.append(_row(name, buckets[name]))

    pooled = Bucket("all")
    for b in buckets.values():
        pooled.overlaps += b.overlaps
        pooled.stop_secs += b.stop_secs
        pooled.utterances += b.utterances
        pooled.missed += b.missed
        pooled.holds += b.holds
        pooled.false_bargeins += b.false_bargeins
    if len(buckets) > 1:
        lines.append(_row("All sources", pooled, bold=True))

    # Show the source relative to server/ when it lives there — an absolute path
    # would bake this machine's home directory into the committed README.
    try:
        shown = f"server/{source.resolve().relative_to(Path(__file__).parent.resolve())}"
    except ValueError:
        shown = str(source)
    provenance = ", ".join(f"{b.overlaps} {name}" for name, b in buckets.items()) or "none"
    lines += [
        "",
        f"<sub>{pooled.overlaps} overlaps aggregated ({skipped} malformed line(s) skipped) "
        f"from `{shown}` — {provenance}. Regenerate with `uv run python "
        f"bargein_summary.py --update-readme`.</sub>",
    ]
    return "\n".join(lines)


def update_readme(readme: Path, table: str) -> None:
    """Replace the text between the barge-in markers, keeping the markers.

    Exits non-zero if either marker is missing — appending this table to an
    arbitrary spot in the README would be worse than doing nothing.
    """
    if not readme.exists():
        sys.exit(f"error: README not found: {readme}")
    text = readme.read_text()
    start, end = text.find(START_MARKER), text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        sys.exit(
            f"error: {readme} is missing the barge-in markers "
            f"({START_MARKER} … {END_MARKER}); add them where the table should go."
        )
    head = text[: start + len(START_MARKER)]
    tail = text[end:]
    readme.write_text(f"{head}\n{table}\n{tail}")
    print(f"updated {readme}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="bargein.jsonl path")
    parser.add_argument("--readme", type=Path, default=DEFAULT_README, help="README to update")
    parser.add_argument(
        "--update-readme",
        action="store_true",
        help="write the table between the barge-in markers in the README",
    )
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(
            f"error: no barge-in metrics at {args.input} — run the barge-in suite "
            f"first (`uv run python -m pipecat.evals suite evals/store/suite_bargein.yaml`), "
            f"or pass --input."
        )
    rows, skipped = load_rows(args.input)
    if not rows:
        sys.exit(f"error: {args.input} has no usable overlaps ({skipped} malformed line(s)).")

    table = render(bucket_rows(rows), args.input, skipped)
    if args.update_readme:
        update_readme(args.readme, table)
    else:
        print(table)


if __name__ == "__main__":
    main()
