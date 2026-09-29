# VibeTrader

A Solana memecoin and crypto trading system that uses Groq-hosted LLMs for trade decisions and learns from its own closed trades.

> ⚠️ **Read this first.** Memecoins mostly go to zero, and an LLM is not an edge by itself. Treat this as an experiment.
> Live mode signs real transactions from the wallet you give it. **Use a fresh burner wallet with money you can
> lose entirely.** Nothing here is financial advice.

## What it does

In **autonomous mode** (the default) the AI runs the whole trading process on paper, learns from every result, and
retunes itself. Going live is always your decision.

1. **Scans** DexScreener (boosted + new Solana tokens) plus a watchlist the AI curates.
2. **Hard filters** drop thin, brand-new or rug-flagged tokens (the AI tunes these thresholds within fixed bounds).
3. **Screens**: one cheap model call picks which of the survivors deserve a full analysis.
4. **Decides**: the entry model returns buy/skip, confidence, size, stop and target, using its past lessons,
   the concepts they formed and per-feature win/loss stats.
5. **Risk engine** clamps every proposal (position %, liquidity share, max positions, daily loss cap, live caps).
6. **Manages positions**: every tick one call reviews all open trades and decides hold / sell / where the stop and
   target sit (it can raise a stop into profit). A hard stop and a hard max-hold always apply regardless.
7. **Learns**: after every closed trade a post-mortem becomes a lesson; similar lessons bunch into **concepts**,
   and lessons that relate to several concepts fire **synapses** between them. Concepts feed later decisions.
8. **Tunes its strategy**: every few closed trades the AI reviews its results and rewrites its own settings
   (filters, sizing, confidence bar, pace, exits, watchlist) within bounds, or pauses itself. Every change is
   logged with its reasoning in the strategy journal.
9. **Runs itself**: the engine resumes automatically when the server restarts and restarts itself after a crash.

"Learning" means reflective memory fed back into prompts, not model fine-tuning.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
pip install -r requirements-live.txt   # only if you want live trading
copy .env.example .env            # then put your GROQ_API_KEY in .env
python run.py
```

Open http://127.0.0.1:8000 (set `PORT` in `.env`, or `python run.py --port 5000`). On first run you set the
**paper starting balance**, then press **Start**.

Values in `.env` take priority over shell environment variables, so a stale global `GROQ_API_KEY` cannot shadow it.
Without a `GROQ_API_KEY` the engine falls back to a simple rule-based heuristic so you can exercise the pipeline
(paper only; live mode requires the model).

### Groq

Each job runs on its own model, because every Groq model has a separate quota:

| Job | Default model |
| --- | --- |
| Entry decisions | `openai/gpt-oss-120b` |
| Screening | `qwen/qwen3.8-27b` |
| Position reviews | `openai/gpt-oss-20b` |
| Post-mortems, strategy reviews, questions | `qwen/qwen3.8-27b` |

The free tier allows, per model, 30 requests/min, 1,000 requests/day, 8,000 tokens/min and **200,000 tokens/day**.
Tokens per day is the binding limit (a full entry decision costs about 2,000 tokens), so a pacer spreads each model's
daily budget across the day and skips optional calls when a model is over its pace; post-mortems wait for budget
instead of being skipped. The dashboard's *Groq budget* figure shows the lowest paced budget across models. Change
models or limits in `.env` (`GROQ_*`), e.g. after moving to a paid tier.

### Demo data

To see a populated dashboard (and a dense concept brain) without waiting for real trades:

```bash
python scripts/seed_demo.py                   # 1000 synthetic trades -> demo.db
python run.py --db demo.db --port 5001
```

The script refuses to write to `vibetrader.db`, because seeded lessons would feed real trading decisions.

## Dashboard

| View | Contents |
| --- | --- |
| **Desk** | account KPIs, open positions (manual close), live activity feed, closed trades |
| **Brain** | 3D concept brain (concepts, lessons, synapses, live learning), learning log, AI strategy journal, lessons, charts, bucket stats, ask the model |
| **Settings** | autonomy, hard backstops, every engine parameter (AI-tuned ones are badged), paper account reset |
| **Live** | mode switch, pre-flight checks, paper track record, wallet, emergency stop |

## Going live

The switch stays locked until **all** of these are true:

- `GROQ_API_KEY`, `WALLET_PRIVATE_KEY` (base58, burner!) and `SOLANA_RPC_URL` are set in `.env`
- `solders` is installed (`requirements-live.txt`)
- `live_max_sol_per_trade`, `live_max_exposure_usd` and `daily_loss_cap_usd` are set in Settings

Then enable it from the **Live** view and type `GO LIVE` to confirm.

- `LIVE_DRY_RUN=true` (the default) builds and signs real Jupiter swaps but only **simulates** them over RPC.
  Do this first. Set it to `false` to send real transactions.
- The public RPC is rate-limited; use a proper RPC provider for anything serious.
- The private key stays in the backend process. It is never logged or returned by the API.
- **Emergency stop** stops the engine and, if ticked, market-sells every open position.
- Switching back to paper leaves live positions open. They are still managed (stops/targets) while the engine runs.

## Security notes

- The server binds to `127.0.0.1` and refuses to bind elsewhere without `DASHBOARD_PASSWORD` (HTTP basic auth).
- Token names/symbols come from strangers on the internet. The UI escapes everything; keep it that way if you hack on it.

## Tests

```bash
pytest
```

Covers paper fill math, risk clamps, exit rules, feature buckets, tolerant Groq JSON parsing and the memory/stats loop.

## Layout

```
vibetrader/
  config.py            env + runtime settings
  db.py                SQLite (trades, decisions, lessons, feature_stats, equity, settings)
  market/              dexscreener, rugcheck, jupiter, features/buckets
  brain/               groq client + schemas, prompts, memory, heuristic fallback
  trading/             broker interface, paper, live (Jupiter), risk, engine loop
  api/routes.py        FastAPI REST + SSE
web/                   index.html, styles.css, app.js
```
