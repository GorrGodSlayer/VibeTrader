"""Reflective memory: lessons + per-bucket stats that feed back into every decision."""
from __future__ import annotations

from typing import Any

from vibetrader.db import DB, now_iso


def relevant_lessons(db: DB, buckets: list[str], k: int = 6) -> list[dict[str, Any]]:
    pool = db.query("SELECT * FROM lessons ORDER BY id DESC LIMIT 400")
    if not pool:
        return []
    want = set(buckets)
    n = len(pool)
    # overlap dominates; recency breaks ties; big wins/losses are more instructive
    scored = sorted(
        enumerate(pool),
        key=lambda il: (
            len(want & set(il[1].get("buckets") or []))
            + (n - il[0]) / n * 0.5
            + min(abs(il[1].get("pnl_pct") or 0), 100) / 200
        ),
        reverse=True,
    )
    picked = [l for _, l in scored[:k]]
    if picked:
        ids = ",".join(str(int(l["id"])) for l in picked)
        db.execute(f"UPDATE lessons SET cited = cited + 1 WHERE id IN ({ids})")
    return picked


def _stat_row(r: dict[str, Any]) -> dict[str, Any]:
    n = r["n"] or 0
    return {
        "bucket": r["bucket"],
        "n": n,
        "wins": r["wins"],
        "losses": r["losses"],
        "win_rate": round(r["wins"] / n, 3) if n else 0,
        "avg_pnl_pct": round(r["total_pnl_pct"] / n, 2) if n else 0,
    }


def bucket_stats(db: DB, buckets: list[str] | None = None) -> list[dict[str, Any]]:
    if buckets:
        qs = ",".join("?" for _ in buckets)
        rows = db.query(f"SELECT * FROM feature_stats WHERE bucket IN ({qs})", tuple(buckets))
    else:
        rows = db.query("SELECT * FROM feature_stats ORDER BY bucket")
    return [_stat_row(r) for r in rows]


def overall_stats(db: DB, mode: str | None = None) -> dict[str, Any]:
    trades = db.closed_trades(mode, limit=100_000)
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t for t in trades if (t["pnl_usd"] or 0) > 0]
    losses = [t for t in trades if (t["pnl_usd"] or 0) <= 0]
    pnl_pcts = [t["pnl_pct"] or 0 for t in trades]
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    wr = len(wins) / n
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 3),
        "avg_pnl_pct": round(sum(pnl_pcts) / n, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "expectancy_pct": round(wr * avg_win + (1 - wr) * avg_loss, 2),
        "total_pnl_usd": round(sum(t["pnl_usd"] or 0 for t in trades), 2),
        "best_pct": round(max(pnl_pcts), 2),
        "worst_pct": round(min(pnl_pcts), 2),
    }


def record_outcome(db: DB, trade: dict[str, Any], pm: dict[str, Any], brain: str) -> int:
    pnl = trade["pnl_pct"] or 0
    for b in trade.get("buckets") or []:
        db.execute(
            """INSERT INTO feature_stats (bucket, n, wins, losses, total_pnl_pct) VALUES (?, 1, ?, ?, ?)
               ON CONFLICT(bucket) DO UPDATE SET n = n + 1, wins = wins + excluded.wins,
               losses = losses + excluded.losses, total_pnl_pct = total_pnl_pct + excluded.total_pnl_pct""",
            (b, int(pnl > 0), int(pnl <= 0), pnl),
        )
    lesson_id = db.insert(
        "lessons",
        {
            "ts": now_iso(),
            "trade_id": trade["id"],
            "symbol": trade["symbol"],
            "pnl_pct": pnl,
            "lesson": pm["lesson"],
            "what_worked": pm.get("what_worked", ""),
            "what_failed": pm.get("what_failed", ""),
            "tags": pm.get("tags", []),
            "buckets": trade.get("buckets") or [],
            "brain": brain,
        },
    )
    db.update("trades", trade["id"], {"lesson_id": lesson_id})
    return lesson_id
