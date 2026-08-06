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

## In progress / next

- [ ] Audio-mode evals can't judge Hinglish yet (Moonshine is English-only) —
      needs a Hinglish transcriber (Saaras) in the eval path.
- [ ] Add `store` eval scenarios that exercise the `sarvam` STT path.

## Backlog (later PRD phases)

- [ ] Interruptions demo + simulated caller (Phase 3).
- [ ] CI gates + auto-published scorecard in README (Phase 4).
- [ ] Hinglish entity-accuracy ASR benchmark (Phase 5).
- [ ] Phone number / telephony + launch (Phase 6).
