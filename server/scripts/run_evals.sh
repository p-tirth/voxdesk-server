#!/usr/bin/env bash
# One-command test run for VoxDesk. Run from anywhere:
#
#     server/scripts/run_evals.sh            # preflight + retrieval check + text suite
#     server/scripts/run_evals.sh --audio    # ...plus the audio round trip and the Sarvam STT suite
#     RAG_BACKEND=fastembed server/scripts/run_evals.sh   # same, on the no-Ollama backend
#
# What each stage actually exercises:
#   retrieval   the embedding + Qdrant path in isolation (no LLM, no bot)
#   text suite  the brain: system prompt, tool routing, RAG grounding/refusal,
#               Hinglish handling — STT/VAD/TTS are bypassed (user turns go in as text)
#   --audio     the ears and mouth too: synthesized speech in (Kokoro), the bot's real
#               STT + TTS, its audio transcribed back for the judge (Moonshine).
#               The Sarvam suite runs the same with Saaras as the bot's STT.
#
# Every stage needs GOOGLE_API_KEY (the bot's LLM and the judge). The audio stages
# also need DEEPGRAM/CARTESIA/SARVAM keys as configured in .env. Ollama must be
# running for the default RAG backend; set RAG_BACKEND=fastembed to skip it.
#
# Exit code is non-zero if any stage fails. Suites run serially on purpose — the
# judge occasionally returns an unstructured verdict; re-run a lone failure once
# before treating it as a regression (see PROGRESS.md).

set -uo pipefail
cd "$(dirname "$0")/.."

AUDIO=0
for arg in "$@"; do
  case "$arg" in
    --audio) AUDIO=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

pass=(); fail=()
stage() {  # stage <name> <command...>
  local name=$1; shift
  echo; echo "=================== $name ==================="
  if "$@"; then pass+=("$name"); else fail+=("$name"); echo ">>> $name FAILED"; fi
}

# ---------------------------------------------------------------- preflight
echo "=================== preflight ==================="
ok=1
command -v uv >/dev/null || { echo "uv not found — https://docs.astral.sh/uv/"; ok=0; }
[ -f .env ] || { echo ".env missing — cp .env.example .env and add keys"; ok=0; }
if [ -f .env ] && ! grep -qE '^GOOGLE_API_KEY=.+' .env; then
  echo "GOOGLE_API_KEY is empty in .env — needed by the bot's LLM and the judge"; ok=0
fi
backend="${RAG_BACKEND:-ollama}"
if [ "$backend" = "ollama" ]; then
  if curl -sf -m 3 "${OLLAMA_BASE_URL:-http://localhost:11434}/api/tags" >/dev/null; then
    echo "ollama: reachable"
  else
    echo "ollama: NOT reachable — doc scenarios will fail. Start it (ollama serve)"
    echo "        or run with RAG_BACKEND=fastembed"; ok=0
  fi
fi
echo "rag backend: $backend"
[ "$ok" = 1 ] || { echo "preflight failed"; exit 1; }
echo "preflight ok"

# ---------------------------------------------------------------- stages
stage "retrieval routing ($backend)" uv run python scripts/check_retrieval.py
stage "text suite (9 scenarios, serial)" \
  uv run python -m pipecat.evals suite evals/store/suite.yaml -c 1

if [ "$AUDIO" = 1 ]; then
  stage "sarvam STT suite (audio, serial)" \
    uv run python -m pipecat.evals suite evals/store/suite_sarvam.yaml -c 1
fi

# ---------------------------------------------------------------- summary
echo; echo "=================== summary ==================="
for s in "${pass[@]:-}"; do [ -n "$s" ] && echo "  PASS  $s"; done
for s in "${fail[@]:-}"; do [ -n "$s" ] && echo "  FAIL  $s"; done
echo
echo "latency scorecard (live rows only):"
uv run python metrics_summary.py | sed -n '/^|/p' | tail -n +3
[ "${#fail[@]}" -eq 0 ]
