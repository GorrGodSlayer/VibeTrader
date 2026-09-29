"""Seed a demo database with synthetic closed paper trades, lessons, decisions and an equity curve.

Usage:
    python scripts/seed_demo.py                    # 1000 trades into demo.db
    python scripts/seed_demo.py --trades 300 --db other.db --force

Then run the dashboard against it:
    python run.py --db demo.db --port 5001

The data is synthetic: every feature bucket has a built-in edge, so bucket stats and lessons show real patterns.
Do not seed your live database - lessons are fed into the model's decisions.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibetrader.brain import memory  # noqa: E402
from vibetrader.config import RuntimeSettings  # noqa: E402
from vibetrader.db import DB  # noqa: E402
from vibetrader.market.features import buckets  # noqa: E402

START_CREDIT = 10_000.0
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYMBOLS = [
    "BONK", "WIF", "POPCAT", "MEW", "BOME", "SLERF", "MYRO", "WEN", "PONKE", "GIGA", "MOODENG", "PNUT", "CHILLGUY",
    "FWOG", "GOAT", "ZEREBRO", "SIGMA", "MICHI", "BILLY", "SC", "LOCKIN", "HAMMY", "NUB", "DADDY", "SELFIE", "ANDY",
    "PEPECAT", "DOGGO", "CATWIF", "KITTY", "MUMU", "QUACK", "TOAD", "JUP", "JTO", "RAY", "PYTH", "ORCA",
]

# expected P&L contribution (percentage points) per bucket
EDGE = {
    "liq:<25k": -3.0, "liq:25k-100k": 0.0, "liq:100k-500k": 1.0, "liq:>500k": 0.5,
    "age:<1h": -2.0, "age:1-6h": 1.0, "age:6-24h": 1.5, "age:1-7d": 0.0, "age:>7d": -0.5, "age:unknown": -1.0,
    "h1:dump": -3.0, "h1:flat": -0.5, "h1:pump": 2.5, "h1:moon": -1.5,
    "flow:sellers": -3.0, "flow:balanced": 0.0, "flow:buyers": 2.0,
    "turnover:low": -1.0, "turnover:mid": 1.0, "turnover:high": 0.5,
    "rug:clean": 1.0, "rug:warn": -1.5, "rug:danger": -6.0, "rug:unknown": -1.0,
}

WIN_LESSONS = {
    "flow:buyers": "Buyer-led 1h flow ({br:.0%} buys) carried through. Keep favoring buyer-dominated tape.",
    "h1:pump": "Entering a {h1:+.0f}% 1h move with volume confirmation worked. Ride it with the trailing stop.",
    "rug:clean": "Clean RugCheck report (score {rug}) gave room to hold. Weight clean contracts higher.",
    "age:6-24h": "Pairs 6-24h old had settled holder bases. This age window keeps paying.",
    "age:1-6h": "Early but not newborn (1-6h) gave the best momentum follow-through.",
    "liq:100k-500k": "Mid liquidity (${liq:,.0f}) filled with little slippage. Size up in this band.",
    "turnover:mid": "Turnover of {vl:.1f}x liquidity showed real interest without churn.",
    "liq:>500k": "Deep liquidity kept exits clean; smaller moves but reliable.",
}
LOSS_LESSONS = {
    "rug:danger": "RugCheck flagged danger (score {rug}) and liquidity drained within {hold:.0f} min. Never override a danger flag.",
    "rug:warn": "Warn-level contract flags preceded a {pnl:.0f}% drop. Require a clean report.",
    "flow:sellers": "Sellers held {sell:.0%} of 1h flow at entry. Buying into distribution failed.",
    "h1:moon": "Chasing a {h1:+.0f}% 1h candle bought the top. Wait for a pullback after vertical moves.",
    "h1:dump": "A {h1:+.0f}% 1h dump did not bounce. Skip falling knives without buyer flow.",
    "liq:<25k": "Thin liquidity (${liq:,.0f}) cost several percent on the exit alone. Avoid sub-$25k pools.",
    "age:<1h": "A pair under 1h old whipsawed through the stop. Newborn pairs need smaller size.",
    "turnover:low": "Turnover of only {vl:.1f}x liquidity meant no follow-through. Volume never arrived.",
}


def mint(r: random.Random) -> str:
    return "".join(r.choice(BASE58) for _ in range(44))


def fake_features(r: random.Random, symbol: str) -> dict:
    liq = math.exp(r.gauss(math.log(90_000), 1.1))
    age = math.exp(r.gauss(math.log(600), 1.6))
    h1 = r.gauss(8, 22)
    br = min(0.92, max(0.18, r.gauss(0.54, 0.12)))
    vl = max(0.1, math.exp(r.gauss(math.log(3), 0.9)))
    rug = max(0, min(100, int(r.expovariate(1 / 18))))
    buys = int(r.uniform(40, 900))
    return {
        "symbol": symbol, "name": symbol.title(), "mint": mint(r),
        "dex": r.choice(["raydium", "orca", "meteora", "pumpswap"]),
        "price_usd": math.exp(r.gauss(math.log(0.002), 2.5)), "liquidity_usd": round(liq, 2),
        "fdv_usd": round(liq * r.uniform(4, 40), 2), "market_cap_usd": round(liq * r.uniform(4, 38), 2),
        "volume_m5": round(liq * vl / 288 * r.uniform(0.3, 3), 2), "volume_h1": round(liq * vl / 24 * r.uniform(0.5, 2), 2),
        "volume_h24": round(liq * vl, 2), "change_m5_pct": round(r.gauss(0, 4), 2), "change_h1_pct": round(h1, 2),
        "change_h6_pct": round(h1 * r.uniform(0.5, 2.5), 2), "change_h24_pct": round(h1 * r.uniform(0.8, 4), 2),
        "buys_h1": buys, "sells_h1": int(buys * (1 - br) / br), "buy_ratio_h1": round(br, 3),
        "vol_to_liq_h24": round(vl, 2), "age_min": round(age, 1), "rug_score": rug,
        "lp_locked_pct": round(r.uniform(0, 100), 1),
        "rug_risks": r.sample(["Mutable metadata", "Low LP providers", "Top 10 holders high ownership", "Freeze authority"],
                              k=min(3, rug // 25)),
    }


def outcome(r: random.Random, f: dict, bks: list[str], sl: float, tp: float) -> tuple[float, str, float]:
    """Returns (pnl_pct net of fees, exit_reason, hold_min)."""
    edge = sum(EDGE.get(b, 0) for b in bks)
    sigma = 9 + (10 if f["liquidity_usd"] < 25_000 else 0) + (12 if (f["age_min"] or 999) < 60 else 0)
    raw = edge + r.gauss(0, sigma)
    hold = min(240, r.expovariate(1 / 45) + 2)
    if "rug:danger" in bks and r.random() < 0.35:
        return -r.uniform(55, 95), "stop_loss (liquidity pulled)", r.uniform(2, 20)
    if raw <= -sl:
        gap = abs(r.gauss(0, 3)) if r.random() < 0.3 else 0
        return -sl - gap - 0.8, f"stop_loss ({-sl - gap:+.1f}%)", hold * 0.6
    if raw >= tp:
        return tp - 0.8, f"take_profit ({tp:+.1f}%)", hold * 0.8
    if raw > 12 and r.random() < 0.5:
        return raw - 0.8, f"trailing_stop (peak {raw + r.uniform(12, 25):+.1f}%, now {raw:+.1f}%)", hold
    if r.random() < 0.18:
        why = "sellers took over the 1h flow" if raw < 0 else "momentum faded, locking the gain"
        return raw - 0.8, f"groq_bail: {why}", hold
    return raw - 0.8, f"max_hold (240 min, {raw:+.1f}%)", 240.0


def price_path(r: random.Random, entry: float, pnl_pct: float, t0: datetime, hold: float) -> list:
    steps = 24
    target = math.log(max(1 + pnl_pct / 100, 0.01))
    pts = []
    for i in range(steps + 1):
        frac = i / steps
        x = target * frac + (r.gauss(0, 0.04) if 0 < i < steps else 0)
        pts.append([int((t0 + timedelta(minutes=hold * frac)).timestamp()), entry * math.exp(x)])
    return pts


def write_lesson(f: dict, bks: list[str], pnl: float, hold: float, reason: str) -> dict:
    won = pnl > 0
    table = WIN_LESSONS if won else LOSS_LESSONS
    ranked = sorted((b for b in bks if b in table), key=lambda b: EDGE[b], reverse=won)
    ctx = {"br": f["buy_ratio_h1"], "sell": 1 - f["buy_ratio_h1"], "h1": f["change_h1_pct"], "rug": f["rug_score"],
           "liq": f["liquidity_usd"], "vl": f["vol_to_liq_h24"], "hold": hold, "pnl": pnl}
    if ranked:
        key = ranked[0]
        lesson = table[key].format(**ctx)
    else:
        key = bks[0]
        lesson = f"{'Favor' if won else 'Avoid'} {', '.join(bks[:3])}; this trade {'paid' if won else 'lost'} {pnl:+.1f}%."
    exit_kind = reason.split(" ")[0].split(":")[0]
    return {
        "lesson": lesson,
        "what_worked": ("Exit discipline via " + exit_kind.replace("_", " ")) if won else "",
        "what_failed": "" if won else f"Entry thesis; {key} dominated the outcome",
        "tags": [key.split(":")[0], key.split(":")[1], exit_kind, "win" if won else "loss"],
    }


def seed(db_path: str, n: int, days: int, seed_value: int) -> None:
    r = random.Random(seed_value)
    db = DB(db_path)
    db.conn.execute("PRAGMA synchronous=OFF")
    s = RuntimeSettings()
    db.set_setting("runtime", s.model_dump())
    db.set_setting("mode", "paper")
    db.set_setting("paper_starting_credit", START_CREDIT)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    t_start = now - timedelta(days=days)
    opens = sorted(t_start + timedelta(minutes=r.uniform(0, days * 1440 - 300)) for _ in range(n))
    equity = START_CREDIT
    closes: list[tuple[datetime, float]] = []

    def iso(d: datetime) -> str:
        return d.isoformat(timespec="seconds")

    for i, opened in enumerate(opens):
        sym = r.choice(SYMBOLS)
        f = fake_features(r, sym)
        bks = buckets(f)
        sl, tp = round(r.uniform(10, 20), 1), round(r.uniform(25, 60), 1)
        pnl_pct, reason, hold = outcome(r, f, bks, sl, tp)
        closed = opened + timedelta(minutes=hold)
        cost = round(max(25.0, equity * r.uniform(0.02, 0.07)), 2)
        pnl_usd = round(cost * pnl_pct / 100, 2)
        entry = f["price_usd"]
        conf = round(min(0.95, max(0.6, 0.62 + sum(EDGE.get(b, 0) for b in bks) / 40 + r.gauss(0, 0.06))), 2)

        decision_id = db.insert("decisions", {
            "ts": iso(opened), "mode": "paper", "mint": f["mint"], "symbol": sym, "action": "buy", "confidence": conf,
            "size_pct": round(cost / equity * 100, 1), "hype": r.randint(3, 10), "executed": 1, "brain": "seed-demo",
            "reasoning": f"{f['buy_ratio_h1']:.0%} buys over 1h, {f['change_h1_pct']:+.1f}% 1h, "
                         f"liquidity ${f['liquidity_usd']:,.0f}, rug score {f['rug_score']}. "
                         f"Buckets {', '.join(bks[:3])} have held up in past trades.",
            "features": f,
        })
        trade_id = db.insert("trades", {
            "mode": "paper", "mint": f["mint"], "symbol": sym, "status": "closed", "opened_at": iso(opened),
            "closed_at": iso(closed), "entry_price": entry, "qty": cost / entry, "cost_usd": cost,
            "stop_loss_pct": sl, "take_profit_pct": tp, "peak_price": entry * (1 + max(pnl_pct, 0) / 100 * 1.2),
            "last_price": entry * (1 + pnl_pct / 100), "price_path": price_path(r, entry, pnl_pct, opened, hold),
            "features": f, "buckets": bks, "thesis": f"Seeded demo trade: {', '.join(bks[:3])}",
            "decision_id": decision_id, "entry_tx": "PAPER", "exit_tx": "PAPER",
            "exit_price": entry * (1 + pnl_pct / 100), "proceeds_usd": round(cost + pnl_usd, 2), "pnl_usd": pnl_usd,
            "pnl_pct": round(pnl_pct, 2), "exit_reason": reason,
        })
        trade = db.one("SELECT * FROM trades WHERE id = ?", (trade_id,))
        lesson_id = memory.record_outcome(db, trade, write_lesson(f, bks, pnl_pct, hold, reason), "seed-demo")
        db.update("lessons", lesson_id, {"ts": iso(closed), "cited": int(r.expovariate(1 / 3) * (1 + (n - i) / n))})
        closes.append((closed, pnl_usd))
        equity += pnl_usd

        # a couple of rejected candidates per trade so the decision history looks real
        for _ in range(2):
            g = fake_features(r, r.choice(SYMBOLS))
            filtered = r.random() < 0.5
            db.insert("decisions", {
                "ts": iso(opened - timedelta(minutes=r.uniform(1, 30))), "mode": "paper", "mint": g["mint"],
                "symbol": g["symbol"], "action": "skip", "features": g, "executed": 0,
                "brain": "filter" if filtered else "seed-demo",
                "confidence": None if filtered else round(r.uniform(0.2, 0.55), 2),
                "hype": None if filtered else r.randint(1, 7),
                "skip_reason": f"liquidity ${g['liquidity_usd']:,.0f} < ${s.min_liquidity_usd:,.0f}" if filtered else None,
                "reasoning": None if filtered else f"Sellers at {1 - g['buy_ratio_h1']:.0%} of 1h flow; no clear edge.",
            })

    # equity curve from realized P&L, in close order
    running = START_CREDIT
    for closed, pnl in sorted(closes):
        running += pnl
        db.insert("equity", {"ts": iso(closed), "mode": "paper", "equity_usd": round(running, 2), "cash_usd": round(running, 2)})
    db.set_setting("paper_cash", round(running, 2))

    o = memory.overall_stats(db, "paper")
    print(f"Seeded {n} trades into {db_path}")
    print(f"  win rate {o['win_rate']:.1%} | expectancy {o['expectancy_pct']:+.2f}% | total {o['total_pnl_usd']:+,.2f} USD")
    print(f"  equity ${START_CREDIT:,.0f} -> ${running:,.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="demo.db")
    ap.add_argument("--trades", type=int, default=1000)
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true", help="overwrite the database if it exists")
    a = ap.parse_args()

    path = Path(a.db)
    if path.resolve() == Path("vibetrader.db").resolve():
        sys.exit("Refusing to seed vibetrader.db: synthetic lessons would feed real trading decisions. Use another --db.")
    if path.exists():
        if not a.force:
            sys.exit(f"{path} exists. Pass --force to overwrite it.")
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    seed(str(path), a.trades, a.days, a.seed)


if __name__ == "__main__":
    main()
