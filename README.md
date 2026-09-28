# VibeTrader 95 🚀💾

A Groq-brained Solana memecoin + crypto trading bot wearing a deliberately hideous Windows 95 costume.

> ⚠️ **Read this first.** Memecoins mostly go to zero, and an LLM is not an edge by itself. This is a toy/experiment.
> Live mode signs real transactions from the wallet you give it. **Use a fresh burner wallet with money you can
> lose entirely.** Nothing here is financial advice. Not even advice.

## What it does

1. **Scans** DexScreener (boosted + new Solana tokens) plus a watchlist of majors (JUP, BONK, WIF, JTO, RAY).
2. **Filters** hard: min liquidity, min 24h volume, min pair age, RugCheck risk score.
3. **Asks Groq** for a JSON decision (`buy`/`skip`, confidence, size, stop, target, reasoning, meme vibe score).
   Each prompt includes the bot's **own past lessons** and **win/loss stats** for similar setups.
4. **Risk engine** clamps everything the model says (position %, liquidity share, max positions, daily loss cap,
   live SOL/exposure caps). The LLM proposes; the risk engine disposes.
5. **Manages exits**: stop-loss, take-profit, trailing stop, max hold, and an optional Groq "should I bail?" check.
6. **Learns**: after every closed trade Groq writes a post-mortem → a lesson + tags, and per-bucket stats
   (liquidity tier, age, momentum, buy/sell flow, turnover, rug score) are updated. Browse it all in `BRAIN.EXE`
   or ask Clippy-Groq questions about it.

"Learning" = reflective memory fed back into prompts, not model fine-tuning.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
pip install -r requirements-live.txt   # only if you want live trading
copy .env.example .env            # then put your GROQ_API_KEY in .env
python run.py
```

Open http://127.0.0.1:8000. The setup wizard asks for your **paper starting credit**. Press **START**.

No `GROQ_API_KEY`? The bot runs on `MAGIC8BALL.EXE`, a dumb heuristic, so you can still watch the plumbing
(paper only; live mode refuses to run without Groq). Note: a `GROQ_API_KEY` set in your shell environment
overrides `.env`.

## The desktop

| Window | What |
| --- | --- |
| `PORTFOLIO.EXE` | equity, P&L, open bags (sell button), start/stop |
| `GROQ_THOUGHTS.TXT` | live stream of every decision, filter, trade and lesson |
| `TRADES.LOG` | closed trades |
| `BRAIN.EXE` | lessons (search/sort), bucket stats, equity + P&L charts, Ask Clippy-Groq |
| `SETTINGS.INI` | every knob, paper reset (optionally wipe the brain) |
| `LIVE_MODE.BAT` | the real-money switch, pre-flight checklist, paper report card, kill switch |

## Going live

The switch stays locked until **all** of these are true:

- `GROQ_API_KEY`, `WALLET_PRIVATE_KEY` (base58, burner!) and `SOLANA_RPC_URL` are set in `.env`
- `solders` is installed (`requirements-live.txt`)
- `live_max_sol_per_trade`, `live_max_exposure_usd` and `daily_loss_cap_usd` are set in SETTINGS.INI

Then: confirm the warning, type `SEND IT`.

- `LIVE_DRY_RUN=true` (the default) builds and signs real Jupiter swaps but only **simulates** them over RPC.
  Do this first. Set it to `false` to send real transactions.
- The public RPC is rate-limited; use a proper RPC provider for anything serious.
- The private key stays in the backend process. It is never logged or returned by the API.
- **Kill switch** ("It is now safe to turn off your computer") stops the engine and, if ticked, market-sells every open position.
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
  brain/               groq client + schemas, prompts, memory, magic8ball fallback
  trading/             broker interface, paper, live (Jupiter), risk, engine loop
  api/routes.py        FastAPI REST + SSE
web/                   index.html, win95.css, app.js (the slop)
```
