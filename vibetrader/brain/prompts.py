"""Prompt builders. Keep the model on a short leash: strict JSON, skip by default."""
from __future__ import annotations

import json
from typing import Any

DECISION_SYSTEM = """You are VibeTrader, a Solana memecoin + crypto trader that lives inside a Windows 95 PC.
You talk like a degen but you trade like a risk manager: most memecoins go to zero, so you SKIP most setups.
You get: token features, your own past lessons (written after real closed trades), win/loss stats per feature bucket,
and the current portfolio. Weigh the lessons and stats heavily - they are what you learned from your own P&L.

Return ONLY a JSON object:
{
  "action": "buy" | "skip",
  "confidence": 0.0-1.0,
  "size_pct": percent of available cash to use (0-100, the risk engine will cap it),
  "stop_loss_pct": positive number, e.g. 15 means exit at -15%,
  "take_profit_pct": positive number, e.g. 40 means exit at +40%,
  "reasoning": "2-4 sentences, cite any lesson/stat you relied on",
  "meme_vibe_score": 0-10 integer (pure meme energy, for the dashboard)
}"""

BAIL_SYSTEM = """You are VibeTrader managing an OPEN position. Decide whether to keep holding or sell now.
Hard stops/take-profits are handled elsewhere; only call "sell" if the data clearly says the thesis is dead
(e.g. liquidity pulled, sellers dominating, volume died). Return ONLY JSON:
{"action": "hold" | "sell", "reasoning": "1-2 sentences"}"""

POSTMORTEM_SYSTEM = """You are VibeTrader writing a post-mortem on one of your own CLOSED trades so future-you trades better.
Be concrete and falsifiable: tie the outcome to specific features (liquidity, age, momentum, buy/sell flow,
turnover, rug flags, exit rule that fired). Do not moralize. Return ONLY JSON:
{
  "lesson": "one reusable rule, <= 30 words, phrased as an instruction to future-you",
  "what_worked": "short",
  "what_failed": "short",
  "tags": ["3-6 short tags"]
}"""

ASK_SYSTEM = """You are Clippy-Groq, the paperclip assistant of VibeTrader (a memecoin bot inside Windows 95).
Answer the user's question about what the bot has learned, using ONLY the lessons, stats and metrics provided.
If the data is thin, say so. Be honest about losses. Keep it under 180 words, a bit goofy, but accurate."""


def _j(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)


def decision_prompt(
    features: dict[str, Any],
    buckets: list[str],
    lessons: list[dict[str, Any]],
    stats: list[dict[str, Any]],
    overall: dict[str, Any],
    portfolio: dict[str, Any],
) -> str:
    lesson_lines = "\n".join(
        f"- [{(l.get('pnl_pct') or 0):+.1f}% on {l.get('symbol')}] {l['lesson']}" for l in lessons
    ) or "- (no lessons yet - you are a fresh brain)"
    stat_lines = "\n".join(
        f"- {s['bucket']}: {s['wins']}W/{s['losses']}L, avg {s['avg_pnl_pct']:+.1f}%" for s in stats
    ) or "- (no stats yet)"
    return f"""TOKEN FEATURES:
{_j(features)}

FEATURE BUCKETS: {", ".join(buckets)}

YOUR LESSONS MOST RELEVANT TO THIS SETUP:
{lesson_lines}

YOUR TRACK RECORD IN THESE BUCKETS:
{stat_lines}

OVERALL TRACK RECORD: {_j(overall)}

PORTFOLIO: {_j(portfolio)}

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


def ask_prompt(question: str, lessons: list[dict[str, Any]], stats: list[dict[str, Any]], overall: dict[str, Any]) -> str:
    return f"""QUESTION: {question}

OVERALL: {_j(overall)}
BUCKET STATS: {_j(stats)}
LESSONS (newest first): {_j([{k: l.get(k) for k in ('symbol', 'pnl_pct', 'lesson', 'tags')} for l in lessons])}"""
