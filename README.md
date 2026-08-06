# VoiceAgent

A Pipecat AI voice agent built with a cascade pipeline (STT → LLM → TTS).

## Configuration

- **Bot Type**: Web
- **Transport(s)**: SmallWebRTC
- **Pipeline**: Cascade
  - **STT**: switchable — Deepgram (EN) / Sarvam Saaras (Hinglish)
  - **LLM**: switchable — OpenAI / Anthropic / Google
  - **TTS**: switchable — Cartesia (EN) / Sarvam Bulbul (Hinglish)
- **Features**:
  - **Pick STT / LLM / TTS from the UI** before connecting (dropdowns)
  - **Live metrics dashboard**: per-layer latency waterfall vs the ~1.2s target
  - **Observability**: per-turn console latency ladder + `metrics/turns.jsonl`
    (`server/latency.py`), surfaced live in the client's Metrics tab

Each layer has an `.env` default (`STT_MODEL` / `LLM_MODEL` / `TTS_MODEL`), which
the client dropdowns can override per session.

## Setup

### Server

1. **Navigate to server directory**:

   ```bash
   cd server
   ```

2. **Install dependencies**:

   ```bash
   uv sync
   ```

3. **Configure environment variables**:

   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

4. **For document search (`search_docs` / RAG), run Ollama with `bge-m3`**:

   ```bash
   # one-time: pull the local embedding model (~1.2 GB)
   ollama pull bge-m3
   # keep the daemon running (the macOS app does this; or `ollama serve`)
   ```

   Only needed if the active profile ships a `docs/` folder (the `store` profile
   does). Without it the bot still runs — `search_docs` just returns nothing.

5. **Run the bot**:

   ```bash
   uv run bot.py
   ```

   The runner serves every transport; the caller selects which one (a web/mobile
   client picks its transport when it connects; a telephony provider connects to
   `/ws`).

## Configurable use case

The agent's use case is **not** hard-coded. A single business profile in
`server/business.py` drives both the bot's system prompt and which eval
scenarios apply. Switch the whole use case with `BUSINESS` in `server/.env`
(default `store`); add a new one by adding a profile entry plus a matching
`server/evals/<key>/` scenario folder.

## Choosing models (STT / LLM / TTS)

All three pipeline layers are switchable, and the choice can be made two ways:

- **From the UI** — the client (`:3000`) shows a dropdown per layer next to the
  Connect button. It lists only models whose API key is configured (the server
  advertises them at `/models`), and the selection is applied **at connect time**
  (dropdowns lock once connected — pick, then Connect). The choice rides along in
  the connect body and the bot builds that exact stack for the session.
- **From `.env`** — `STT_MODEL` / `LLM_MODEL` / `TTS_MODEL` set the defaults used
  when there's no UI selection (e.g. `uv run bot.py` alone, or evals). Each maps
  to a row in the matching registry in `bot.py` (`STT_REGISTRY`, `LLM_REGISTRY`,
  `TTS_REGISTRY`), where a model's provider, real model id, and required key live.

Options today:

| Layer | Options | Hinglish pick |
| --- | --- | --- |
| STT | `deepgram` (EN) · `sarvam` (Saaras `saaras:v3`, codemix) | `sarvam` |
| LLM | `gemini-3-flash` · `gemini-3.5-flash` · `gemini-3.1-flash-lite` (fastest) · `gemini-flash-lite` · `gemini-2.5-flash` · `gpt-5-mini` · `gpt-5-nano` · `sonnet-5` · `sonnet-4.6` | any |
| TTS | `cartesia` (EN) · `sarvam` (Bulbul) | `sarvam` |

Sarvam (STT + TTS) needs `SARVAM_API_KEY` — free credits at
[dashboard.sarvam.ai](https://dashboard.sarvam.ai) (₹100, no card). The spoken
language for language-aware TTS comes from the active business profile's
`tts_language` (the `store` profile uses `hi-IN`, which also pronounces English
product names naturally); Cartesia ignores it.

## Metrics dashboard (client)

The client has a **Metrics** tab (next to Conversation / Events) that turns the
pipeline's live RTVI metrics into a latency view, labeled with the models you
picked for this session:

- **Latest-turn waterfall** — STT → LLM → TTS time-to-first-byte stacked into one
  bar, with a dashed marker and OK/OVER flag against the ~1.2s target.
- **Per-layer cards** — latest TTFB plus rolling avg / p50 / p95 across the call.
- **End-to-end trend** — a bar per recent turn (green under target, red over).

It's the browser-side companion to the server's console latency ladder
(`server/latency.py`), reading the same metrics the pipeline emits.

## Tools & product data

The bot answers questions about products, stock, and orders by **calling tools**
that look the answer up in local data — so those answers are grounded in real
records, not guessed. Nothing here is a service or an external database:

- **`server/data/<BUSINESS>/`** — the "database": `products.json` (the catalog)
  and `orders.json` (sample orders), loaded into memory at boot. Swap the data
  by editing these files; it swaps with the use case (`BUSINESS`).
- **`server/catalog.py`** — loads that data and does plain in-memory search
  (deterministic, instant, no embeddings).
- **`server/tools.py`** — the function-calling tools the LLM can invoke:
  `search_products` (what do you sell / do you carry X), `check_stock` (is a
  specific item available), `get_order_status` (look up an order number), and
  `search_docs` (policy/FAQ questions — see RAG below). Each profile picks its
  toolset in `TOOLSETS`.

Structured vs. unstructured: a product catalog is structured data, so the catalog
tools are exact substring lookups (deterministic, testable, no embeddings). Prose
questions — returns, warranty, shipping, payments — aren't a field lookup, so they
go through `search_docs`, a separate retrieval tool over the FAQ/policy documents.

## Documents & retrieval (RAG)

`search_docs` answers policy / how-it-works questions from a small corpus of prose
docs, grounded in the text rather than guessed. It's a thin, fully-local setup —
no cloud, no API key:

- **`server/data/<BUSINESS>/docs/*.md`** — the corpus (for `store`: returns,
  warranty, shipping, payments), split into paragraph chunks. Edit these to change
  what the bot knows; add a `docs/` folder to give another profile documents.
- **`server/rag.py`** — a `DocStore` that embeds the corpus at boot and searches it:
  - **Embeddings: `bge-m3` via Ollama** (local, multilingual — handles Hinglish).
    Needs the Ollama daemon running with the model pulled (`ollama pull bge-m3`);
    override the host/model with `OLLAMA_BASE_URL` / `RAG_EMBED_MODEL`.
  - **Vector store: Qdrant, embedded** (`QdrantClient(":memory:")` — in-process, no
    server/Docker). The same client points at a Qdrant Docker server later, so
    growing to a persistent store for a bigger corpus is a one-line change.
- **Recall-first + LLM judges relevance.** Retrieval uses a lenient floor and hands
  the closest passages to the LLM, which answers only if they actually address the
  question and otherwise declines — so the bot refuses off-corpus questions instead
  of forcing an answer from a loosely-similar passage. (A dense+sparse hybrid would
  sharpen this; overkill for a handful of docs.)

  Why the floor is deliberately low: measured on this corpus, grounded and
  off-corpus similarity scores overlap, and **Hinglish questions score lower than
  the English equivalent** (the corpus is English), so a cutoff strict enough to
  reject off-corpus questions would silently drop real Hinglish matches. The floor
  only filters junk; relevance is the LLM's call.

> **Swapping the embedding model?** Set `RAG_EMBED_MODEL` to any model your Ollama
> has (the vector dimension is detected automatically, so nothing else needs to
> change) — but the relevance floor `DEFAULT_MIN_SCORE` in `server/rag.py` is
> calibrated to **bge-m3's** score distribution. Other models score on different
> scales, so re-check it: embed a few questions that should hit and a few that
> shouldn't, then set the floor below the ones that should hit.

A document lookup costs roughly **140ms** (query embedding + vector search) and
only happens on turns where the LLM calls `search_docs` — other turns are
unaffected.

If Ollama is unreachable at boot, the store degrades to empty (no crash) — the bot
just has no document knowledge.

## Latency ladder

With `enable_metrics=True`, the bot prints a per-turn latency **waterfall** after
every turn (STT/LLM/TTS time-to-first-byte + end-to-end, flagged against the
~1.2s target) and appends a row to `server/metrics/turns.jsonl` for later
aggregation. See `server/latency.py`. It fires on real/audio turns (a browser
call or an audio-mode eval); text-mode evals bypass VAD, so it stays quiet there.

## Testing with evals

Behavioral evals are scripted conversations that drive the bot headless — no live
call needed. Scenarios for the active use case live in `server/evals/<BUSINESS>/`
(e.g. `server/evals/store/`), including `function_call` checks that assert the
tools actually fire. This includes two RAG scenarios: `docs_grounded` (a policy
question → asserts `search_docs` fires and the answer is grounded in the doc) and
`docs_refusal` (an off-corpus question → asserts the bot declines and invents
nothing). The judge LLM is configured once in
`server/evals/_shared/judge_eval.yaml`.

Note: the doc scenarios need Ollama + `bge-m3` running (see Setup). Run the suite
serially (or expect occasional timeout flakes) — under high concurrency the
Ollama embed calls contend and a slow turn can exceed the response window.

Use `python -m pipecat.evals` (not `pipecat eval`) so the working dir is
importable — the judge factory in `eval_judge.py` needs it.

**Whole suite (recommended):** the `suite` runner spawns a *fresh bot per
scenario*, so there's no context bleed between runs. From `server/`:

```bash
uv run python -m pipecat.evals suite evals/store/suite.yaml
```

**Iterating on one scenario (fast inner loop):** boot the bot once and drive
scenarios against it from a second terminal (the bot stays up across runs):

```bash
uv run bot.py -t eval
# In another terminal (server/):
uv run python -m pipecat.evals run evals/store/products.yaml -v          # one scenario
uv run python -m pipecat.evals run evals/store/audio_roundtrip.yaml -v   # full audio round trip
```

> Note: running many scenarios against one long-lived bot (`run` with several
> files) lets context carry over between them, which can flake the greeting turn.
> Use `suite` for a clean full-suite pass.

By default the judge reuses your `GOOGLE_API_KEY` via Gemini's OpenAI-compatible
endpoint (no extra key or model download). To switch to Ollama (`ollama pull
gemma2:9b`) or OpenAI, edit `server/evals/_shared/judge_eval.yaml`.

### Web client (separate repository)

The browser client — model dropdowns, transcript, and the live metrics dashboard
— lives in its own repo: **[voxdesk-client](https://github.com/p-tirth/voxdesk-client)**.
It's optional: the bot serves a built-in test UI at `http://localhost:7860`, so
you can talk to it without the client at all.

To run the full setup, clone the client next to this repo and start it:

```bash
git clone https://github.com/p-tirth/voxdesk-client.git
cd voxdesk-client
npm install
cp env.example .env.local     # defaults to the bot at localhost:7860
npm run dev                   # → http://localhost:3000
```

Keep the bot (`uv run bot.py`) running in another terminal; the client proxies to
it and reads `GET /models` to populate its dropdowns.

## Project Structure

This repository is the **bot server**. (The web client is a separate repo — see
above.)

```
voxdesk-server/
├── server/                  # Python bot server
│   ├── bot.py               # Pipeline + STT/LLM/TTS registries + /models endpoint
│   ├── business.py          # Configurable business profiles (persona + prompt)
│   ├── catalog.py           # Structured data loader/search (products, orders)
│   ├── rag.py               # Document retrieval: embeddings + in-memory Qdrant
│   ├── tools.py             # Function-calling tools + per-profile toolsets
│   ├── latency.py           # Per-turn latency ladder
│   ├── eval_judge.py        # Gemini-backed eval judge factory
│   ├── data/<business>/     # products.json + orders.json …
│   │   └── docs/            # …plus the FAQ/policy corpus for search_docs
│   ├── evals/               # Behavioral eval scenarios (+ suite.yaml)
│   ├── scripts/             # Helper scripts (e.g. generate_tts_preview.py)
│   ├── recordings/          # TTS preview page + sample audio (git-ignored)
│   ├── metrics/             # Per-turn latency JSONL log (git-ignored)
│   ├── pyproject.toml       # Python dependencies
│   ├── .env.example         # Environment variables template
│   ├── .env                 # Your API keys (git-ignored)
│   └── ...
├── .gitignore
└── README.md                # This file
```
## Observability

What's wired in this project:

- **Console latency ladder** — the per-turn STT/LLM/TTS waterfall printed after
  every audio turn (`server/latency.py`; see [Latency ladder](#latency-ladder)).
- **`metrics/turns.jsonl`** — one JSON row per turn for later aggregation.
- **Client Metrics tab** — the same live RTVI metrics rendered as a latency
  dashboard in the browser (see [Metrics dashboard](#metrics-dashboard-client)).

### Whisker — Live Pipeline Debugger (optional, not wired)

**Whisker** is Pipecat's live graphical debugger for visualizing pipelines and
inspecting frames in real time. It is **not enabled in this repo** — there's no
`pipecat-ai-whisker` dependency and no `WhiskerObserver` in `bot.py`. To add it:

1. Add the dependency: `uv add pipecat-ai-whisker` (in `server/`).
2. Attach the observer in `bot.py` alongside the latency one:

   ```python
   from pipecat_whisker import WhiskerObserver
   # ...
   observers=[latency_observer, WhiskerObserver(pipeline)],
   ```

3. Run the bot, expose port 9090 (e.g. `ngrok http 9090`), then open
   [https://whisker.pipecat.ai/](https://whisker.pipecat.ai/) and enter the URL.
## Building with an AI coding agent

Extending this bot with Claude Code, Codex, or another AI coding assistant? Give it live, accurate Pipecat context instead of stale training data with the **Pipecat Context Hub** — a local index of Pipecat docs, examples, and API source your agent queries over MCP:

```bash
# Build the local index (first run takes a couple of minutes)
uvx pipecat-ai-context-hub@latest refresh

# Add it to your agent (use the line for the one you use)
claude mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve   # Claude Code
codex mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve    # Codex
```

MCP servers load at session start, so add it before opening your coding session. See the [Pipecat Context Hub docs](https://docs.pipecat.ai/api-reference/context-hub) for the full setup.

## Learn More

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [Voice UI Kit Documentation](https://voiceuikit.pipecat.ai/)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord Community](https://discord.gg/pipecat)