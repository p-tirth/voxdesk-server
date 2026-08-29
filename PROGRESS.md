# Progress Tracker

A running log of what's built vs. pending, mapped to the PRD (`voice-agent-prd.md`)
phases. Update this as work lands. Legend: [x] done · [~] in progress · [ ] todo.

## Done

### Core voice loop (PRD Phase 1)

- [x] Cascade pipeline running: Deepgram STT → LLM → TTS (browser via SmallWebRTC).
- [x] Switchable **LLM** via `LLM_MODEL` in `.env` (`LLM_REGISTRY` in `bot.py`):
  gemini-3-flash / gemini-3.5-flash / gemini-3.1-flash-lite (fastest) /
  gemini-flash-lite / gpt-5-mini / gpt-5-nano / sonnet-5 / sonnet-4.6
  (+ gemini-2.5-flash — briefly 404'd mid-2026, working again as of 2026-07-16).
- [x] Switchable **TTS** via `TTS_MODEL` in `.env` (`TTS_REGISTRY` in `bot.py`):
  `cartesia` (default) and `sarvam` (Hinglish, Bulbul).
- [x] Per-turn **latency ladder** (`latency.py`): console waterfall + JSONL log
  (`metrics/turns.jsonl`), flagged against the ~1.2s target.

### Configurable use case

- [x] `business.py` — `BusinessProfile` drives system prompt + greeting + eval set;
  switch the whole use case via `BUSINESS` in `.env`. `store` profile live.
- [x] Per-profile `tts_language` (store = `hi-IN`) and optional `language_style`
  (currently unset for store → neutral "mirror the caller").

### Agentic: tools + product data (PRD Phase 2, partial)

- [x] Local structured data layer (`catalog.py` + `data/store/*.json`): products + orders.
- [x] Function-calling tools (`tools.py`): `search_products`, `check_stock`,
  `get_order_status`, `search_docs`; per-profile toolset registry.
- [x] Tools shared to handlers via `PipelineWorker(app_resources=...)`.

### Documents & retrieval — RAG (PRD Phase 2 / §7b #3-#4)

- [x] `search_docs` tool over a local FAQ/policy corpus (`data/store/docs/*.md`:
  returns, warranty, shipping, payments → 13 chunks).
- [x] `rag.py` — `DocStore` embeds the corpus at boot with **bge-m3 via Ollama**
  (local, multilingual) into an **in-memory Qdrant** (`QdrantClient(":memory:")`,
  no server; same client migrates to a Docker/server Qdrant later).
- [x] Recall-first retrieval + **LLM-judged relevance** (not a hard threshold —
  measured: grounded and off-corpus cosine scores overlap, and **Hinglish
  queries score systematically lower than English** for the same intent since
  the corpus is English, so any threshold high enough to reject off-corpus
  questions silently drops real Hinglish matches). Floor is 0.35 = junk filter
  only; the LLM decides relevance. Graceful if Ollama is down.
- [x] Two RAG eval scenarios (`docs_grounded`, `docs_refusal`) in the suite.
- [x] Verified: retrieval routing **17/17** (incl. Hinglish), suite **9/9** serial,
  plus 5 ad-hoc scenarios (Hinglish RAG, 2 refusals, catalog routing,
  multi-turn follow-up) all passing. Query cost ~**140ms** (embed+search),
  only on doc turns.
- [ ] Dense+sparse **hybrid** search (bge-m3 + Qdrant both support it) — deferred;
  overkill for 13 docs, would sharpen off-corpus separation later.

### Verification (PRD Phase 4, foundation)

- [x] Behavioral eval scenarios (`evals/store/`): greeting, products, stock_check,
  order_status, hours, returns, hinglish, docs_grounded, docs_refusal
  (+ audio_roundtrip, run on its own).
- [x] `function_call` assertions prove tools fire.
- [x] Fresh-bot suite (`evals/store/suite.yaml`) — all 9 text scenarios pass
  individually. Under high concurrency the suite can flake on timeouts (Ollama
  embed contention for the doc scenarios) and an occasional judge-JSON glitch;
  both clear on a serial re-run.
- [x] Gemini-backed eval judge (`eval_judge.py`) reusing `GOOGLE_API_KEY`.

### Hinglish TTS

- [x] Sarvam TTS wired + activated (`TTS_MODEL=sarvam`, `bulbul:v2`, `hi-IN`).
- [x] Local TTS preview page generator (`scripts/generate_tts_preview.py` →
  `recordings/tts_preview.html`) for A/B-ing voices.

### Model selection + metrics UI

- [x] **Switchable STT** (`STT_REGISTRY` in `bot.py`): `deepgram` (EN) /
  `sarvam` (Saaras `saaras:v3`, codemix) for Hinglish input. Added the
  `pipecat-ai[sarvam]` extra (pulls the `sarvamai` SDK).
- [x] **Per-session model selection**: `bot.py` builds STT/LLM/TTS from the
  connect body (`runner_args.body`), falling back to `.env`. Bot advertises
  the configured models at `GET /models`.
- [x] **UI dropdowns** (client `:3000`): STT/LLM/TTS pickers, options fetched
  from `/api/models` (configured-only), applied at connect. Pre-connect only —
  mid-call hot-swap is out of scope (Pipecat can't swap live pipeline services).
- [x] **Metrics dashboard**: Conversation/Metrics/Events tabs; per-turn
  STT→LLM→TTS waterfall vs the 1.2s target, per-layer avg/p50/p95 labeled by
  the selected model, plus an end-to-end trend.
- [x] Metrics-tab UI fixes: made the tab scrollable (flex `min-h-0` chain) and
  removed the duplicated built-in metrics view (`ConversationPanel noMetrics`),
  so the dedicated Metrics tab is the single source.

### Evidence layer (PRD §7b)

- [x] **Latency scorecard** (`server/metrics_summary.py`, §7b #2): aggregates
  `metrics/turns.jsonl` → markdown table of p50/p95 e2e + per-stage TTFB per
  LLM+TTS stack (STT shown when reported), nearest-rank percentiles, no
  outlier filtering. `--update-readme` rewrites the README between
  `<!-- scorecard:start/end -->` markers (idempotent; errors if markers
  missing). Stdlib-only, ruff-clean, edge cases tested (empty/malformed/
  missing input, missing markers).
- [x] **README story** (§7b #7): mermaid architecture diagram (validated with
  mermaid-cli), latency-ladder explainer, ≤10-min quickstart with free-tier
  key table + clone step, "Production gaps I know about" section, scorecard
  section generated in. Title/tagline now say VoxDesk.
- [x] `.env.example` default LLM switched `gpt-5-mini` → `gemini-3-flash` so the
  free-key quickstart works out of the box.
- [x] **Secret hygiene verified** (§7b #8's check): across *all* history of both
  repos, the only env-like file ever committed is `server/.env.example`, and
  every historical version has empty key values.
- [x] **Metrics provenance** (2026-08-07): every `turns.jsonl` row is tagged
  `mode: live|eval` at write time, derived from the session's transport in
  `bot.py` (can't be forgotten). The scorecard excludes eval rows by default
  and publishes the excluded counts; `--include-eval` pools them. Verified
  end-to-end with a live eval run (rows tagged `eval`, 15s contention TTFBs
  correctly kept out of the default table).
- [x] **Pluggable embedding backend** — the open decision, resolved:
  `RAG_BACKEND=ollama|fastembed|none` (default `ollama`/bge-m3, the quality
  pick). `fastembed` = in-process ONNX `bge-small-en-v1.5` (~67 MB, no daemon,
  floor 0.40) so CI runs the doc evals without Ollama. Measured bake-off:
  both backends route 12/12 grounded queries (EN + Hinglish); on a harder
  keyword-free romanized-Hindi probe bge-m3 4/8 vs bge-small 5/8, while
  fastembed's *multilingual* MiniLM scored 1/8 (romanized Hindi reads as noise
  to a Devanagari-trained model). Repeatable harness:
  `scripts/check_retrieval.py` (17-query battery, `--floor` override).
- [x] **CI workflow drafted** (`.github/workflows/evals.yml`, §7b #6): text-mode
  suite, serial via the real `-c 1` CLI override, `RAG_BACKEND=fastembed` with
  a cached model dir, one real secret needed (`GOOGLE_API_KEY`; Deepgram/
  Cartesia construct fine on dummies in text mode — evidenced in the file
  header). actionlint-clean. NOT yet run in CI — enabling = set the
  `GOOGLE_API_KEY` repo secret, then badge when green.
- [x] **Sarvam STT eval evidence** (`evals/store/suite_sarvam.yaml` +
  `stt_sarvam_order_number` / `stt_sarvam_codemix`): real synthesized speech
  through Saaras as the bot's STT, selected via `--runner-body` (the suite
  schema has no per-scenario env, and `.env` would clobber a shell var). Three
  consecutive green suite runs; spoken order numbers survived Saaras exactly
  (`543131`, `990001`) into the tool calls — the entity-accuracy thesis holds.
  Also hit the Moonshine-can't-judge-Hinglish gap live; codemix scenario
  judges from LLM text (user side stays real audio) as the honest workaround.
- [x] **Upstream issue drafted** (`UPSTREAM-ISSUE-hinglish-audio-evals.md`,
  local/gitignored, for Tirth to review + post): the PRD's "eval transcriber
  is English-only" framing was stale — it's pluggable (`transcription.factory:`)
  and ships multilingual Whisper; the real blocker is that pipecat's Sarvam STT
  is WebSocket-streaming-only, which `EvalTranscriber` can't consume. Ask: an
  HTTP-mode `SarvamHttpSTTService` (mirroring the Cartesia HTTP/WS split).

## In progress / next

- [ ] **Current scorecard numbers need a clean capture.** `metrics/turns.jsonl`
  mixes real browser calls with audio-mode eval runs (the 10-21s LLM TTFBs
  are eval contention, and 48/109 turns have no e2e bracket), so the table
  published in the README today looks worse than a live call. Before going
  public: clear the JSONL, run a clean session on the demo stack, regenerate
  (`uv run python metrics_summary.py --update-readme`).
- [ ] **Remaining §7b gate items (human-in-the-loop):** demo video (§7b #1),
  manual barge-in verification + README paragraph (§7b #5), enable the drafted
  CI action (§7b #6 — the workflow is on GitHub; set the `GOOGLE_API_KEY`
  secret, watch the first run, badge when green), repo public + pinned (§7b #8; the `.env`-history check already
  passes), post the drafted upstream issue.
- [x] **Judge flake fixed** (2026-08-29): the "(unstructured no)" verdicts that
  could redden a CI run traced to Pipecat's 200-token judge cap colliding with
  Gemini 2.5's thinking tokens — the JSON was truncated at `{"verdict": "` on
  0/20 probe calls. `eval_judge.py` now disables reasoning and forces JSON
  mode: 20/20 well-formed verdicts. Also added `scripts/run_evals.sh`, a
  one-command preflight + retrieval check + serial suite (+ `--audio`).
- [ ] `search_docs` is verified in text-mode evals only — not yet exercised on a
  live browser audio call.
- [ ] Audio-mode evals can't judge Hinglish (Moonshine is English-only) — hit
  live in the codemix STT scenario. Blocked on upstream (see the drafted
  issue: pipecat's Sarvam STT is streaming-only, unusable as an eval
  transcriber); per §7b non-goals, not building around it.

## Backlog (later PRD phases)

- [ ] Interruptions demo + simulated caller (Phase 3).
- [ ] CI gates + auto-published scorecard in README (Phase 4).
- [ ] Hinglish entity-accuracy ASR benchmark (Phase 5).
- [ ] Phone number / telephony + launch (Phase 6).
