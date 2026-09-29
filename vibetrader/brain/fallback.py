"""Rule-based stand-in brain, used only when no GROQ_API_KEY is set (paper only)."""
from __future__ import annotations

from typing import Any


def heuristic_decision(f: dict[str, Any]) -> dict[str, Any]:
    score = 0.0
    br = f.get("buy_ratio_h1", 0.5)
    h1 = f.get("change_h1_pct") or 0
    rug = f.get("rug_score")
    score += 0.35 if br > 0.55 else 0.1 if br > 0.5 else 0
    score += 0.3 if 2 < h1 < 40 else 0.1 if 0 < h1 <= 2 else 0
    score += 0.2 if (f.get("vol_to_liq_h24") or 0) > 1 else 0
    score += 0.15 if rug is not None and rug < 20 else 0
    buy = score >= 0.6
    return {
        "action": "buy" if buy else "skip",
        "confidence": round(score, 2),
        "size_pct": 5,
        "stop_loss_pct": 12,
        "take_profit_pct": 30,
        "reasoning": f"Heuristic score {score:.2f} (1h buy ratio {br:.2f}, 1h change {h1:+.1f}%).",
        "hype_score": min(10, int(score * 10)),
    }


def heuristic_postmortem(trade: dict[str, Any]) -> dict[str, Any]:
    won = (trade.get("pnl_pct") or 0) > 0
    b = ", ".join(trade.get("buckets") or [])
    return {
        "lesson": f"{'Favor' if won else 'Avoid'} setups matching {b} (exit: {trade['exit_reason']}).",
        "what_worked": "",
        "what_failed": "",
        "tags": ["heuristic", "win" if won else "loss"],
    }
