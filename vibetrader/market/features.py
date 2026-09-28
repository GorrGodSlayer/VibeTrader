"""Raw DexScreener pair -> compact features + coarse buckets the brain learns over."""
from __future__ import annotations

import time
from typing import Any


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def build_features(pair: dict[str, Any], rug: dict[str, Any] | None = None) -> dict[str, Any]:
    liq = _f((pair.get("liquidity") or {}).get("usd"))
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    tx_h1 = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = int(tx_h1.get("buys") or 0), int(tx_h1.get("sells") or 0)
    created = pair.get("pairCreatedAt")
    age_min = (time.time() * 1000 - created) / 60000 if created else None

    return {
        "symbol": (pair.get("baseToken") or {}).get("symbol", "???"),
        "name": (pair.get("baseToken") or {}).get("name", ""),
        "mint": (pair.get("baseToken") or {}).get("address"),
        "dex": pair.get("dexId"),
        "price_usd": _f(pair.get("priceUsd")),
        "liquidity_usd": round(liq, 2),
        "fdv_usd": _f(pair.get("fdv")),
        "market_cap_usd": _f(pair.get("marketCap")),
        "volume_m5": _f(vol.get("m5")),
        "volume_h1": _f(vol.get("h1")),
        "volume_h24": _f(vol.get("h24")),
        "change_m5_pct": _f(chg.get("m5")),
        "change_h1_pct": _f(chg.get("h1")),
        "change_h6_pct": _f(chg.get("h6")),
        "change_h24_pct": _f(chg.get("h24")),
        "buys_h1": buys,
        "sells_h1": sells,
        "buy_ratio_h1": round(buys / (buys + sells), 3) if buys + sells else 0.5,
        "vol_to_liq_h24": round(_f(vol.get("h24")) / liq, 2) if liq else 0.0,
        "age_min": round(age_min, 1) if age_min is not None else None,
        "rug_score": (rug or {}).get("score"),
        "lp_locked_pct": (rug or {}).get("lp_locked_pct"),
        "rug_risks": [r["name"] for r in (rug or {}).get("risks", []) if r.get("level") in ("warn", "danger")][:6],
    }


def buckets(f: dict[str, Any]) -> list[str]:
    """Coarse, human-readable tags. Stats are aggregated per bucket."""
    out: list[str] = []

    liq = f.get("liquidity_usd") or 0
    out.append(
        "liq:<25k" if liq < 25_000 else "liq:25k-100k" if liq < 100_000 else "liq:100k-500k" if liq < 500_000 else "liq:>500k"
    )

    age = f.get("age_min")
    if age is None:
        out.append("age:unknown")
    else:
        out.append(
            "age:<1h" if age < 60 else "age:1-6h" if age < 360 else "age:6-24h" if age < 1440
            else "age:1-7d" if age < 10080 else "age:>7d"
        )

    h1 = f.get("change_h1_pct") or 0
    out.append("h1:dump" if h1 < -10 else "h1:flat" if h1 < 10 else "h1:pump" if h1 < 50 else "h1:moon")

    br = f.get("buy_ratio_h1", 0.5)
    out.append("flow:sellers" if br < 0.45 else "flow:balanced" if br <= 0.55 else "flow:buyers")

    vl = f.get("vol_to_liq_h24") or 0
    out.append("turnover:low" if vl < 1 else "turnover:mid" if vl < 5 else "turnover:high")

    rs = f.get("rug_score")
    out.append("rug:unknown" if rs is None else "rug:clean" if rs < 20 else "rug:warn" if rs < 50 else "rug:danger")

    return out
