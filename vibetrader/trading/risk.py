"""Guardrails. The LLM proposes; this module disposes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vibetrader.brain.groq_client import Decision
from vibetrader.config import RuntimeSettings


@dataclass
class Order:
    usd: float
    stop_loss_pct: float
    take_profit_pct: float


def filter_reason(f: dict[str, Any], s: RuntimeSettings) -> str | None:
    """Hard pre-LLM filters. Returns why a candidate is rejected, or None."""
    if (f.get("price_usd") or 0) <= 0:
        return "no price"
    if (f.get("liquidity_usd") or 0) < s.min_liquidity_usd:
        return f"liquidity ${f.get('liquidity_usd', 0):,.0f} < ${s.min_liquidity_usd:,.0f}"
    if (f.get("volume_h24") or 0) < s.min_volume_h24_usd:
        return f"24h volume ${f.get('volume_h24', 0):,.0f} < ${s.min_volume_h24_usd:,.0f}"
    age = f.get("age_min")
    if age is not None and age < s.min_age_min:
        return f"too young ({age:.0f} min)"
    rug = f.get("rug_score")
    if rug is not None and rug > s.max_rug_score:
        return f"rug score {rug} > {s.max_rug_score}"
    return None


def size_order(
    d: Decision,
    f: dict[str, Any],
    s: RuntimeSettings,
    *,
    mode: str,
    cash_usd: float,
    equity_usd: float,
    open_trades: list[dict[str, Any]],
    realized_today_usd: float,
    sol_price: float | None = None,
) -> Order | str:
    """Turn an LLM decision into a capped order, or a rejection reason."""
    if d.action != "buy":
        return "brain said skip"
    if d.confidence < s.min_confidence:
        return f"confidence {d.confidence:.2f} < {s.min_confidence:.2f}"
    if len(open_trades) >= s.max_open_positions:
        return f"max open positions ({s.max_open_positions})"
    if any(t["mint"] == f.get("mint") for t in open_trades):
        return "already holding"
    if s.daily_loss_cap_usd > 0 and realized_today_usd <= -s.daily_loss_cap_usd:
        return f"daily loss cap hit ({realized_today_usd:.2f} USD)"

    pct = min(d.size_pct or s.max_position_pct / 2, s.max_position_pct)
    usd = min(equity_usd * pct / 100, cash_usd * 0.98)
    usd = min(usd, (f.get("liquidity_usd") or 0) * s.max_liquidity_share_pct / 100)

    if mode == "live":
        if not sol_price or sol_price <= 0:
            return "no SOL price"
        usd = min(usd, s.live_max_sol_per_trade * sol_price)
        exposure = sum(t["cost_usd"] for t in open_trades)
        usd = min(usd, s.live_max_exposure_usd - exposure)

    if usd < 1:
        return f"size too small (${usd:.2f})"

    sl = d.stop_loss_pct or s.default_stop_loss_pct
    tp = d.take_profit_pct or s.default_take_profit_pct
    return Order(
        usd=round(usd, 2),
        stop_loss_pct=round(min(max(sl, 3.0), 50.0), 2),
        take_profit_pct=round(min(max(tp, 5.0), 500.0), 2),
    )


def exit_reason(trade: dict[str, Any], price: float, peak: float, held_min: float, s: RuntimeSettings) -> str | None:
    entry = trade["entry_price"]
    chg = (price / entry - 1) * 100
    if chg <= -trade["stop_loss_pct"]:
        return f"stop_loss ({chg:+.1f}%)"
    if chg >= trade["take_profit_pct"]:
        return f"take_profit ({chg:+.1f}%)"
    peak_gain = (peak / entry - 1) * 100
    if s.trailing_stop_pct > 0 and peak_gain >= s.trailing_activation_pct:
        drawdown = (1 - price / peak) * 100
        if drawdown >= s.trailing_stop_pct:
            return f"trailing_stop (peak {peak_gain:+.1f}%, now {chg:+.1f}%)"
    if held_min >= s.max_hold_min:
        return f"max_hold ({held_min:.0f} min, {chg:+.1f}%)"
    return None
