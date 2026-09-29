"""Prompt builders. Keep the model on a short leash: strict JSON, skip by default."""
from __future__ import annotations

import json
from typing import Any

DECISION_SYSTEM = """You are the decision model of VibeTrader, a short-term trading system for Solana memecoins and majors.
Act as a disciplined risk manager: most memecoins go to zero, so SKIP unless the setup is clearly asymmetric.
You get: token features, your own past lessons (written after real closed trades), win/loss stats per feature bucket,
and the current portfolio. Weigh the lessons and stats heavily - they are what you learned from your own P&L.

FEATURE GLOSSARY (read the numbers exactly this way):
- rug_score: RugCheck risk, 0 = clean ... 100 = worst. Below 20 is clean, 20-49 is a warning, 50+ is dangerous.
- rug_risks: the specific RugCheck warnings; lp_locked_pct: share of pool liquidity locked (higher is safer).
- buy_ratio_h1: buys / (buys + sells) over the last hour. Above 0.55 = buyers in control, below 0.45 = sellers.
- vol_to_liq_h24: 24h volume divided by pool liquidity (turnover). Below 1 is dead, 1-5 healthy, above 5 frenzied.
- age_min: minutes since the trading pair was created. Pairs under 60 minutes are the most rug-prone.
- change_*_pct: price change over that window, in percent.
- liquidity_usd: USD depth of the pool; your position must be a small fraction of it to exit cleanly.

Return ONLY a JSON object:
{
  "action": "buy" | "skip",
  "confidence": 0.0-1.0,
  "size_pct": percent of available cash to use (0-100, the risk engine will cap it),
  "stop_loss_pct": positive number, e.g. 15 means exit at -15%,
  "take_profit_pct": positive number, e.g. 40 means exit at +40%,
  "reasoning": "2-4 sentences, cite any lesson/stat you relied on",
  "hype_score": 0-10 integer, strength of the social/narrative momentum,
  "explore": true | false
}
EXPLORATION: in PAPER mode you are training. When your lessons and concepts say little about a setup and nothing in
the data is clearly dangerous, you may take a small exploratory position to learn from it: action "buy",
explore true, size_pct <= 3, an honest (even low) confidence. The lesson is the payoff. Never explore in LIVE mode."""

BAIL_SYSTEM = """You are the decision model of VibeTrader, managing an OPEN position. Decide whether to keep holding or sell now.
Hard stops/take-profits are handled elsewhere; only call "sell" if the data clearly says the thesis is dead
(e.g. liquidity pulled, sellers dominating, volume died). Return ONLY JSON:
{"action": "hold" | "sell", "reasoning": "1-2 sentences"}"""

POSTMORTEM_SYSTEM = """You are the decision model of VibeTrader, writing a post-mortem on one of your own CLOSED trades so future-you trades better.
Be concrete and falsifiable: tie the outcome to specific features (liquidity, age, momentum, buy/sell flow,
turnover, rug flags, exit rule that fired). Do not moralize. Return ONLY JSON:
{
  "lesson": "one reusable rule, <= 30 words, phrased as an instruction to future-you",
  "what_worked": "short",
  "what_failed": "short",
  "tags": ["3-6 short tags"]
}"""

SCREEN_SYSTEM = """You are the screening model of VibeTrader, an autonomous Solana memecoin trading system.
You see a compact table of tokens that already passed the hard safety filters. Pick the few that deserve a full,
expensive analysis by the decision model (each analysis costs budget, so be selective). Prefer setups your concepts
say have worked; avoid ones your concepts say have lost. In PAPER training you learn only from trades you take, so
while you have few concepts pick at least one candidate (the most promising or the most informative untested
setup) unless every candidate is clearly dangerous. In LIVE mode picking nothing is fine.
Columns: sym, liq_k (liquidity, $k), vol_k (24h volume, $k), h1 (% 1h change), br (1h buy ratio), turn (24h
volume / liquidity), age_h (pair age, hours), rug (0 clean .. 100 worst).
Return ONLY JSON: {"picks": [{"symbol": "SYM", "why": "short"}], "reasoning": "1 sentence"}"""

MANAGE_SYSTEM = """You are the position manager of VibeTrader, an autonomous Solana memecoin trading system.
You review every open position and decide for each: keep holding or sell now, and where its stop and target
should sit from here. Both are measured from the ENTRY price:
- stop_loss_pct 12 = sell if the position falls to -12%; a NEGATIVE stop locks in profit (-8 = sell if the gain
  falls back to +8%), which is how you trail a winner.
- take_profit_pct 40 = sell at +40%.
Trade like a disciplined memecoin trader, not a hopeful holder:
- Cut losers whose thesis is dead (sellers took over, liquidity drained, volume died). Do not wait for the stop.
- Never let a winner become a loser: once a position has been clearly up, raise the stop into profit.
- Momentum in memecoins decays fast. A position going nowhere is a failed trade; sell it rather than "hold for target".
- "Thesis still valid" is not a reason to hold on its own; judge the tape now vs. at entry (flow, volume, m5/h1). A hard safety stop and a maximum holding time are enforced separately. Return ONLY JSON:
{"positions": [{"id": 12, "action": "hold" | "trim" | "sell", "sell_fraction": 0.5, "stop_loss_pct": 12,
                "take_profit_pct": 40, "reasoning": "1 sentence"}]}
"trim" sells sell_fraction (0.1-0.9) of what is left: use it to take profit into strength. Code already sells
tranches automatically at the scale-out levels, so trims are for extra profit-taking or de-risking."""

STRATEGY_SYSTEM = """You are the strategist of VibeTrader, an autonomous Solana memecoin trading system that trains itself
on paper trading. Review your own recent results, your concepts (clusters of related lessons) and your current settings,
then decide how to adjust your strategy. Change only what the evidence supports; small steps beat big swings; say
"no changes" when the sample is too small. You may also pause new entries (pause_minutes, max 720) during a clear
losing streak or regime you do not understand, and curate the watchlist of established tokens (Solana mint addresses)
you always consider. You cannot touch hard safety stops, live-trading limits or the fill model.
Your Groq budget is limited: a shorter scan_interval_sec or more max_candidates_per_tick spends it faster.
Return ONLY JSON:
{"changes": {"setting_name": new_value}, "pause_minutes": 0, "watchlist_add": [], "watchlist_remove": [],
 "reasoning": "2-4 sentences citing the numbers that drove each change"}"""

ASK_SYSTEM = """You are the analyst for VibeTrader, a Solana memecoin trading system.
Answer the user's question about what the system has learned, using ONLY the lessons, stats and metrics provided.
Cite numbers. If the sample is too small to conclude anything, say so. Be direct about losses. Under 180 words."""


def _j(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)


def decision_prompt(
    features: dict[str, Any],
    buckets: list[str],
    lessons: list[dict[str, Any]],
    stats: list[dict[str, Any]],
    overall: dict[str, Any],
    portfolio: dict[str, Any],
    concepts: list[dict[str, Any]] | None = None,
    playbook: str = "",
) -> str:
    lesson_lines = "\n".join(
        f"- [{(l.get('pnl_pct') or 0):+.1f}% on {l.get('symbol')}] {l['lesson']}" for l in lessons
    ) or "- (no lessons yet - you are a fresh brain)"
    stat_lines = "\n".join(
        f"- {s['bucket']}: {s['wins']}W/{s['losses']}L, avg {s['avg_pnl_pct']:+.1f}%" for s in stats
    ) or "- (no stats yet)"
    concept_lines = "\n".join(
        f"- \"{c['label']}\": {c['n']} lessons, {c['wins']}W/{c['losses']}L, avg {c['avg_pnl_pct']:+.1f}%"
        for c in concepts or []
    ) or "- (no concepts formed yet)"
    slim = {k: v for k, v in features.items() if k not in ("name", "mint", "dex")}
    return f"""TOKEN FEATURES:
{_j(slim)}

FEATURE BUCKETS: {", ".join(buckets)}

YOUR LESSONS MOST RELEVANT TO THIS SETUP:
{lesson_lines}

PLAYBOOK (base trading knowledge; your own lessons and stats override it once they have enough trades):
{playbook or "- (none)"}

YOUR CONCEPTS THAT MATCH THIS SETUP (clusters of related lessons):
{concept_lines}

YOUR TRACK RECORD IN THESE BUCKETS:
{stat_lines}

OVERALL TRACK RECORD: {_j(overall)}

PORTFOLIO: {_j(portfolio)}
TRAINING: {portfolio.get('mode', 'paper').upper()} mode, {overall.get('trades', 0)} closed trades learned from so far.

Decide."""


def bail_prompt(trade: dict[str, Any], features: dict[str, Any], pnl_pct: float, held_min: float) -> str:
    return f"""POSITION: {trade['symbol']} entered at {trade['entry_price']}, now {pnl_pct:+.1f}% after {held_min:.0f} min.
ENTRY THESIS: {trade.get('thesis') or 'n/a'}
FEATURES AT ENTRY: {_j(trade.get('features'))}
FEATURES NOW: {_j(features)}
Hold or sell?"""


def postmortem_prompt(trade: dict[str, Any], held_min: float) -> str:
    path = trade.get("price_path") or []
    # downsample the path so the prompt stays small
    step = max(1, len(path) // 20)
    sampled = [round(p[1] / trade["entry_price"] * 100 - 100, 1) for p in path[::step]] if path else []
    return f"""TRADE: {trade['symbol']} ({trade['mode']})
RESULT: {trade['pnl_pct']:+.2f}% ({trade['pnl_usd']:+.2f} USD) after {held_min:.0f} min, exit reason: {trade['exit_reason']}
ENTRY THESIS: {trade.get('thesis') or 'n/a'}
ENTRY FEATURES: {_j(trade.get('features'))}
BUCKETS: {", ".join(trade.get('buckets') or [])}
STOP/TP SET: -{trade['stop_loss_pct']}% / +{trade['take_profit_pct']}%
PRICE PATH (% vs entry, sampled): {sampled}
Write the post-mortem."""


def ask_prompt(question: str, lessons: list[dict[str, Any]], stats: list[dict[str, Any]], overall: dict[str, Any],
               concepts: list[dict[str, Any]] | None = None) -> str:
    return f"""QUESTION: {question}

OVERALL: {_j(overall)}
CONCEPTS (clusters of related lessons, largest first): {_j([{k: c[k] for k in ('label', 'n', 'win_rate', 'avg_pnl_pct')} for c in concepts or []])}
BUCKET STATS: {_j(stats)}
LESSONS (newest first): {_j([{k: l.get(k) for k in ('symbol', 'pnl_pct', 'lesson', 'tags')} for l in lessons])}"""


def screen_prompt(rows: list[dict[str, Any]], concepts: list[dict[str, Any]], max_picks: int, portfolio: dict[str, Any],
                  playbook: str = "") -> str:
    head = "sym | liq_k | vol_k | h1 | br | turn | age_h | rug"
    table = "\n".join(
        f"{r['symbol']} | {r['liquidity_usd'] / 1e3:.0f} | {r['volume_h24'] / 1e3:.0f} | {r['change_h1_pct']:+.0f} | "
        f"{r['buy_ratio_h1']:.2f} | {r['vol_to_liq_h24']:.1f} | "
        f"{(r['age_min'] or 0) / 60:.0f} | {r['rug_score'] if r['rug_score'] is not None else '?'}"
        for r in rows
    )
    concept_lines = "\n".join(
        f"- \"{c['label']}\": {c['n']} lessons, avg {c['avg_pnl_pct']:+.1f}%" for c in concepts
    ) or "- (none yet)"
    return f"""PICK UP TO {max_picks}.
TRAINING: {portfolio.get('mode', 'paper').upper()} mode, {portfolio.get('closed_trades', 0)} closed trades learned from so far.

CANDIDATES:
{head}
{table}

YOUR STRONGEST CONCEPTS:
{concept_lines}

PLAYBOOK:
{playbook or "- (none)"}

PORTFOLIO: {_j(portfolio)}"""


def manage_prompt(positions: list[dict[str, Any]], concepts: list[dict[str, Any]], playbook: str = "",
                  mechanics: str = "") -> str:
    concept_lines = "\n".join(
        f"- \"{c['label']}\": avg {c['avg_pnl_pct']:+.1f}% over {c['n']} lessons" for c in concepts
    ) or "- (none yet)"
    return f"""OPEN POSITIONS:
{_j(positions)}

RELEVANT CONCEPTS:
{concept_lines}

EXIT PLAYBOOK (follow it; "thesis still valid" is not a reason to ignore it):
{playbook or "- (none)"}

AUTOMATIC EXITS ALREADY IN FORCE: {mechanics}

Review every position."""


def strategy_prompt(settings: dict[str, Any], bounds: str, overall: dict[str, Any], recent: dict[str, Any],
                    best: list[dict[str, Any]], worst: list[dict[str, Any]], concepts: list[dict[str, Any]],
                    lessons: list[dict[str, Any]], history: list[dict[str, Any]], budget: dict[str, Any],
                    playbook: str = "") -> str:
    return f"""CURRENT SETTINGS: {_j(settings)}

ALLOWED RANGES:
{bounds}

ALL-TIME RESULTS: {_j(overall)}
SINCE YOUR LAST REVIEW: {_j(recent)}

BEST FEATURE BUCKETS: {_j(best)}
WORST FEATURE BUCKETS: {_j(worst)}

YOUR CONCEPTS (largest first): {_j([{k: c[k] for k in ('label', 'n', 'win_rate', 'avg_pnl_pct')} for c in concepts])}

RECENT LESSONS: {_j([{k: l.get(k) for k in ('symbol', 'pnl_pct', 'lesson')} for l in lessons])}

YOUR PREVIOUS STRATEGY CHANGES (newest first): {_j(history)}

PLAYBOOK (base knowledge; tune toward it unless your own results clearly disagree):
{playbook or "- (none)"}

GROQ BUDGET TODAY: {_j(budget)}

Decide your strategy changes."""
