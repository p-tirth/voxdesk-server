#!/usr/bin/env python3
"""Score the bot's *escalation decision*, not just that the tool exists.

Every eval scenario proves something happened; none of them tells you whether the
bot escalates at the right times. That's a classification problem, so it gets a
classifier's metrics: eight scenarios in ``evals/store/`` are labelled with whether
a handoff to a human is the correct outcome (``LABELS`` below), and this script
reads a suite run's logs to see what the bot actually did.

The ground truth is what the bot did, not what the judge thought of the wording:
a bot that says "I'm passing you to a person" without calling the tool has escalated
nothing, and that distinction is the whole point of the metric.

Two sources, in preference order:

* the **bot's own log** (``<run>.log``, written by the suite runner) — it records
  ``Calling function [escalate_to_human:<id>]`` for every call, always named.
* the **harness's eval log** (``<run>.eval.log``) — its ``event: function_call``
  lines, as a fallback for a ``pipecat.evals run`` that has no bot log.

The eval log alone is not sufficient: the harness only asks the bot to *report* a
call's name when the scenario asserts on one (``_required_report_level`` in
harness.py), so a scenario with a bare ``function_call`` expectation logs a nameless
``event: function_call``. Counting that as "not an escalation" would be a guess, so
a nameless call with no bot log to disambiguate is reported as ambiguous and fails
the run rather than quietly scoring it.

Usage, from ``server/``::

    uv run python -m pipecat.evals suite evals/store/suite.yaml -c 1
    uv run python scripts/escalation_score.py            # newest eval-runs/<ts>/
    uv run python scripts/escalation_score.py --run eval-runs/20260829_120000

Exits non-zero if any labelled scenario is missing from the run — a metric computed
over a partial run is worse than no metric, because it looks like a result.

Stdlib only, so it runs anywhere the repo is checked out.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The labelled set: scenario (the .yaml stem in evals/store/) -> should this
# conversation have ended in a human handoff?
#
# Labels are the specification, not a record of what the bot does. When a case comes
# out wrong, fix the prompt guidance in business.py — never relabel to make the
# numbers look better.
LABELS: dict[str, bool] = {
    "esc_asks_for_human": True,
    "esc_refund_dispute": True,
    "esc_lookup_failed": True,
    "esc_payment_double_charge": True,
    "noesc_simple_stock": False,
    "noesc_offcorpus_declined": False,
    "noesc_hours": False,
    "noesc_return_policy": False,
}

ESCALATION_TOOL = "escalate_to_human"

# Matches the harness's observation line, e.g.
#     5.861  [ t2]  event: function_call  'escalate_to_human'
# and deliberately NOT "match: waiting for 'function_call' (escalate_to_human)".
# The harness's observation line, e.g.
#     5.861  [ t2]  event: function_call  'escalate_to_human'
# Deliberately NOT "match: waiting for 'function_call' (escalate_to_human)", which
# is the harness stating an expectation rather than recording an observation.
_EVAL_CALL_RE = re.compile(r"event:\s*function_call(?:\s+'([^']+)')?")

# The bot's own line, e.g.
#     ... Calling function [escalate_to_human:call_274554] with arguments {...}
_BOT_CALL_RE = re.compile(r"Calling function \[([A-Za-z_][A-Za-z0-9_]*):")


def _log_re(scenario: str) -> re.Pattern[str]:
    """Match the log file a run wrote for ``scenario``, whichever runner made it.

    ``pipecat.evals run`` names it after the scenario's ``name:`` field
    (``store_esc_asks_for_human.eval.log``); ``suite`` prefixes the bot path so one
    bot's concurrent scenarios can't collide (``.._.._bot.py__esc_asks_for_human.eval.log``
    — note the leading dot, which hides it from a plain ``ls``). Anchoring on a
    ``__`` or ``store_`` boundary keeps ``hours`` from also matching
    ``noesc_hours.eval.log``.
    """
    return re.compile(rf"^(?:.*__)?(?:store_)?{re.escape(scenario)}\.eval\.log$")


RUNS_DIR = Path("eval-runs")


def newest_run(runs_dir: Path) -> Path:
    """The most recent suite run under ``eval-runs/`` that covers the labelled set.

    ``eval-runs/`` accumulates every kind of run — single-scenario ``-s`` runs, other
    manifests, aborted ones with an empty ``logs/`` — so "newest directory" is the
    wrong answer surprisingly often. Walk newest-first (by mtime, since ``--name``
    means the names aren't necessarily timestamps) and take the first run that holds
    a log for every labelled scenario. If none does, fall back to the newest run with
    any logs at all and let the missing-scenario report say so.
    """
    candidates = sorted(
        (d for d in runs_dir.glob("*") if d.is_dir() and (d / "logs").is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    with_logs = [d for d in candidates if any((d / "logs").glob("*.eval.log"))]
    if not with_logs:
        raise SystemExit(
            f"No suite runs with logs found under {runs_dir}/. Run the suite first:\n"
            "    uv run python -m pipecat.evals suite evals/store/suite.yaml -c 1"
        )
    for run in with_logs:
        names = [p.name for p in (run / "logs").iterdir()]
        if all(any(_log_re(sc).match(n) for n in names) for sc in LABELS):
            return run
    return with_logs[0]


def logs_dir_for(run: Path) -> Path:
    """Accept either a run directory or the ``logs/`` directory inside one."""
    return run if run.name == "logs" else run / "logs"


def calls_made(eval_log: Path, bot_log: Path | None) -> tuple[list[str], bool]:
    """Every tool call the scenario made, plus whether any call is unidentifiable.

    Returns ``(names, ambiguous)``. ``ambiguous`` is True only when we had to fall
    back to the eval log and it holds a nameless ``function_call`` — that call could
    be an escalation, so the caller must not score the scenario.
    """
    if bot_log is not None and bot_log.is_file():
        names = _BOT_CALL_RE.findall(bot_log.read_text(errors="replace"))
        # De-duplicate while keeping first-seen order.
        return list(dict.fromkeys(names)), False

    names: list[str] = []
    ambiguous = False
    for name in _EVAL_CALL_RE.findall(eval_log.read_text(errors="replace")):
        if not name:
            ambiguous = True
        elif name not in names:
            names.append(name)
    return names, ambiguous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run",
        type=Path,
        default=None,
        help="Suite run directory (or its logs/ dir). Defaults to the newest under eval-runs/.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=RUNS_DIR,
        help=f"Where suite runs live (default: {RUNS_DIR}).",
    )
    args = parser.parse_args()

    run = args.run if args.run is not None else newest_run(args.runs_dir)
    logs = logs_dir_for(run)
    if not logs.is_dir():
        raise SystemExit(f"No logs directory at {logs}")

    # tp: correctly escalated. fp: escalated when it shouldn't have (a caller
    # bounced to a queue for a question the bot could answer). fn: should have
    # escalated and didn't (a caller left stuck). tn: correctly handled itself.
    rows: list[tuple[str, bool, bool, str]] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    tp = fp = fn = tn = 0

    for scenario, should in sorted(LABELS.items()):
        pattern = _log_re(scenario)
        found = [p for p in logs.iterdir() if pattern.match(p.name)]
        if not found:
            missing.append(scenario)
            continue
        eval_log = found[0]
        # The suite writes the bot's stdout beside the eval log, same stem.
        bot_log = eval_log.with_name(eval_log.name.replace(".eval.log", ".log"))
        names, unclear = calls_made(eval_log, bot_log if bot_log != eval_log else None)
        if unclear:
            ambiguous.append(scenario)
            continue
        did = ESCALATION_TOOL in names
        tools = ", ".join(n for n in names if n != ESCALATION_TOOL) or "—"
        rows.append((scenario, should, did, tools))
        if should and did:
            tp += 1
        elif should and not did:
            fn += 1
        elif not should and did:
            fp += 1
        else:
            tn += 1

    print(f"Escalation decision — run: {run}\n")
    print("| scenario | labelled | escalated | outcome | other tools called |")
    print("| --- | --- | --- | --- | --- |")
    for scenario, should, did, tools in rows:
        outcome = (
            "TP"
            if should and did
            else "FN (missed)"
            if should
            else "FP (spurious)"
            if did
            else "TN"
        )
        print(
            f"| `{scenario}` | {'escalate' if should else 'handle it'} | "
            f"{'yes' if did else 'no'} | {outcome} | {tools} |"
        )

    print("\n**Confusion matrix**\n")
    print("| | predicted escalate | predicted handle |")
    print("| --- | --- | --- |")
    print(f"| **labelled escalate** | {tp} (TP) | {fn} (FN) |")
    print(f"| **labelled handle** | {fp} (FP) | {tn} (TN) |")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy = (tp + tn) / len(rows) if rows else 0.0

    print(
        f"\nprecision {precision:.2f} · recall {recall:.2f} · F1 {f1:.2f} · "
        f"accuracy {accuracy:.2f} ({tp + tn}/{len(rows)})"
    )

    if missing:
        print("\nMissing from this run: " + ", ".join(missing), file=sys.stderr)
    if ambiguous:
        print(
            "\nUnscorable (a nameless function_call and no bot log to identify it): "
            + ", ".join(ambiguous),
            file=sys.stderr,
        )
    if missing or ambiguous:
        print(
            "The numbers above are computed over a partial run — re-run the suite "
            "(`-c 1`) so every labelled scenario is present with its bot log.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
