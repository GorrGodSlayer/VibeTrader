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
    h1 = f.get("change_h1_pct") or 0
    if h1 > s.max_h1_change_pct:
        return f"vertical candle ({h1:+.0f}% in 1h): don't chase"
    if h1 < s.min_h1_change_pct:
        return f"falling knife ({h1:+.0f}% in 1h)"
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
    exploring = d.explore and mode == "paper" and s.explore_in_paper
    bar = s.explore_min_confidence if exploring else s.min_confidence
    if d.confidence < bar:
        return f"confidence {d.confidence:.2f} < {bar:.2f}" + (" (exploration bar)" if exploring else "")
    if len(open_trades) >= s.max_open_positions:
        return f"max open positions ({s.max_open_positions})"
    if any(t["mint"] == f.get("mint") for t in open_trades):
        return "already holding"
    if s.daily_loss_cap_usd > 0 and realized_today_usd <= -s.daily_loss_cap_usd:
        return f"daily loss cap hit ({realized_today_usd:.2f} USD)"

    pct = min(d.size_pct or s.max_position_pct / 2, s.max_position_pct)
    if exploring:
        pct = min(pct, s.explore_size_pct)
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
        take_profit_pct=round(min(max(tp, 5.0), 1000.0), 2),
    )


def exit_reason(trade: dict[str, Any], price: float, peak: float, held_min: float, s: RuntimeSettings) -> str | None:
    entry = trade["entry_price"]
    chg = (price / entry - 1) * 100
    # hard backstops first: the AI can move its own stop and target, never these
    if chg <= -s.hard_stop_loss_pct:
        return f"hard_stop ({chg:+.1f}%)"
    if held_min >= s.hard_max_hold_hours * 60:
        return f"hard_max_hold ({held_min / 60:.1f} h, {chg:+.1f}%)"
    if chg <= -trade["stop_loss_pct"]:
        kind = "profit_lock" if trade["stop_loss_pct"] < 0 else "stop_loss"
        return f"{kind} ({chg:+.1f}%)"
    if chg >= trade["take_profit_pct"]:
        return f"take_profit ({chg:+.1f}%)"
    peak_gain = (peak / entry - 1) * 100
    # round-trip guard: a trade that was clearly up is never allowed to give it all back
    if s.breakeven_after_pct > 0 and peak_gain >= s.breakeven_after_pct and chg <= s.breakeven_lock_pct:
        return f"roundtrip_guard (peak {peak_gain:+.1f}%, now {chg:+.1f}%)"
    # profit floor: keep a share of the best gain seen, ratcheting up with new highs
    if s.profit_floor_after_pct > 0 and peak_gain >= s.profit_floor_after_pct:
        floor = peak_gain * s.profit_floor_keep
        if chg <= floor:
            return f"profit_floor (peak {peak_gain:+.1f}%, floor {floor:+.1f}%, now {chg:+.1f}%)"
    if s.trailing_stop_pct > 0 and peak_gain >= s.trailing_activation_pct:
        drawdown = (1 - price / peak) * 100
        if drawdown >= s.trailing_stop_pct:
            return f"trailing_stop (peak {peak_gain:+.1f}%, now {chg:+.1f}%)"
    # memecoin momentum decays in hours: no progress in time means the thesis failed.
    # established tokens (pair older than 7 days at entry) get far longer to work.
    mature = ((trade.get("features") or {}).get("age_min") or 0) > 7 * 1440
    limit = s.time_stop_mature_hours * 60 if mature else s.time_stop_min
    if limit > 0 and held_min >= limit and chg < s.time_stop_min_gain_pct:
        return f"time_stop ({held_min:.0f} min, {chg:+.1f}%)"
    if held_min >= s.max_hold_min:
        return f"max_hold ({held_min:.0f} min, {chg:+.1f}%)"
    return None


def scale_out_due(trade: dict[str, Any], price: float, s: RuntimeSettings) -> tuple[float, int, float] | None:
    """Next take-profit tranche, as (token qty to sell, tranche number, trigger %), or None."""
    chg = (price / trade["entry_price"] - 1) * 100
    done = trade.get("tranches_done") or 0
    levels = [(s.scale_out_1_pct, s.scale_out_1_frac), (s.scale_out_2_pct, s.scale_out_2_frac)]
    if done >= len(levels):
        return None
    pct, frac = levels[done]
    if frac <= 0 or pct <= 0 or chg < pct or chg >= trade["take_profit_pct"]:
        return None  # not there yet, or the full take-profit fires instead
    orig = trade.get("orig_qty") or trade["qty"]
    qty = min(trade["qty"], orig * frac)
    return (qty, done + 1, pct) if qty > 0 else None


def liquidity_drained(trade: dict[str, Any], price: float, liquidity_usd: float, s: RuntimeSettings) -> str | None:
    """In a constant-product pool the USD liquidity moves with sqrt(price); losing much more than that means
    liquidity was pulled, which is how rugs happen."""
    f = trade.get("features") or {}
    entry_liq, entry_px = f.get("liquidity_usd") or 0, f.get("price_usd") or 0
    if s.liquidity_drain_exit_pct <= 0 or not entry_liq or not entry_px or not liquidity_usd:
        return None
    expected = entry_liq * (price / entry_px) ** 0.5
    if liquidity_usd < expected * (1 - s.liquidity_drain_exit_pct / 100):
        return f"liquidity_drain (pool ${liquidity_usd:,.0f} vs ~${expected:,.0f} expected)"
    return None
