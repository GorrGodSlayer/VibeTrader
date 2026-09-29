"""Self-tuning: which settings the AI may rewrite, within which bounds, and how its proposals are applied."""
from __future__ import annotations

import re
from typing import Any

from vibetrader.config import RuntimeSettings

# field -> (min, max, type). Anything not listed (hard backstops, live caps, fill model) is off-limits to the AI.
TUNABLE: dict[str, tuple[float, float, type]] = {
    "scan_interval_sec": (120, 3600, int),
    "max_candidates_per_tick": (1, 5, int),
    "decision_cooldown_min": (5, 720, int),
    "min_liquidity_usd": (5_000, 2_000_000, float),
    "min_volume_h24_usd": (0, 50_000_000, float),
    "min_age_min": (0, 10_080, float),
    "max_rug_score": (5, 80, float),
    "min_confidence": (0.4, 0.95, float),
    "max_position_pct": (0.5, 10, float),
    "max_open_positions": (1, 8, int),
    "max_liquidity_share_pct": (0.1, 2, float),
    "default_stop_loss_pct": (3, 35, float),
    "default_take_profit_pct": (5, 1000, float),
    "trailing_stop_pct": (0, 50, float),
    "trailing_activation_pct": (0, 400, float),
    "max_hold_min": (10, 2_880, float),
    "breakeven_after_pct": (10, 100, float),
    "profit_floor_after_pct": (20, 300, float),
    "profit_floor_keep": (0.2, 0.8, float),
    "time_stop_mature_hours": (2, 72, float),
    "scale_out_1_pct": (15, 200, float),
    "scale_out_1_frac": (0, 0.6, float),
    "scale_out_2_pct": (40, 600, float),
    "scale_out_2_frac": (0, 0.5, float),
    "scale_out_stop_lock_pct": (0, 20, float),
    "liquidity_drain_exit_pct": (10, 60, float),
    "max_h1_change_pct": (20, 1000, float),
    "min_h1_change_pct": (-90, 0, float),
    "max_entries_per_day": (1, 50, int),
    "loss_streak_pause": (0, 10, int),
    "loss_streak_pause_min": (0, 720, float),
    "breakeven_lock_pct": (-5, 20, float),
    "time_stop_min": (5, 720, float),
    "time_stop_min_gain_pct": (-10, 30, float),
    "explore_size_pct": (0.5, 5, float),
    "explore_min_confidence": (0.1, 0.6, float),
}
TUNABLE_BOOL = {"include_watchlist", "groq_bail_check", "explore_in_paper"}
MAX_WATCHLIST = 15
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def tunable_snapshot(s: RuntimeSettings) -> dict[str, Any]:
    d = s.model_dump()
    return {k: d[k] for k in [*TUNABLE, *sorted(TUNABLE_BOOL)]}


def bounds_text() -> str:
    lines = [f"- {k}: {lo:g} .. {hi:g}" for k, (lo, hi, _) in TUNABLE.items()]
    lines += [f"- {k}: true | false" for k in sorted(TUNABLE_BOOL)]
    return "\n".join(lines)


def apply_changes(s: RuntimeSettings, changes: dict[str, Any], add: list[str], remove: list[str]
                  ) -> tuple[RuntimeSettings, dict[str, list[Any]], list[str]]:
    """Clamp and apply an AI proposal. Returns (new settings, {field: [old, new]}, rejected notes)."""
    cur = s.model_dump()
    applied: dict[str, list[Any]] = {}
    rejected: list[str] = []
    for k, v in (changes or {}).items():
        if k in TUNABLE:
            lo, hi, typ = TUNABLE[k]
            try:
                raw = float(v)
            except (TypeError, ValueError):
                rejected.append(f"{k}={v!r} (not a number)")
                continue
            val = typ(min(hi, max(lo, raw)))
            if raw != float(val):
                rejected.append(f"{k}={v} clamped to {val}")
        elif k in TUNABLE_BOOL:
            val = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        else:
            rejected.append(f"{k} (not tunable by the AI)")
            continue
        if cur.get(k) != val:
            applied[k] = [cur.get(k), val]
            cur[k] = val

    wl = list(cur["watchlist"])
    for m in remove or []:
        if m in wl:
            wl.remove(m)
    for m in add or []:
        m = str(m).strip()
        if not MINT_RE.match(m):
            rejected.append(f"watchlist add {m[:12]!r} (not a Solana mint)")
        elif m not in wl:
            wl.append(m)
    wl = wl[:MAX_WATCHLIST]
    if wl != cur["watchlist"]:
        applied["watchlist"] = [cur["watchlist"], wl]
        cur["watchlist"] = wl
    # the AI can never stretch a hold past the hard backstop
    cur["max_hold_min"] = min(cur["max_hold_min"], cur["hard_max_hold_hours"] * 60)
    return RuntimeSettings(**cur), applied, rejected
