"""Tiny SQLite layer. Synchronous + a lock: every query here is sub-millisecond."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,                -- paper | live
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    pair_address TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    entry_price REAL NOT NULL,
    qty REAL NOT NULL,
    raw_amount TEXT,                   -- live: token base units held
    decimals INTEGER,
    cost_usd REAL NOT NULL,
    stop_loss_pct REAL NOT NULL,
    take_profit_pct REAL NOT NULL,
    peak_price REAL NOT NULL,
    last_price REAL,
    price_path TEXT NOT NULL DEFAULT '[]',
    features TEXT NOT NULL DEFAULT '{}',
    buckets TEXT NOT NULL DEFAULT '[]',
    thesis TEXT,
    decision_id INTEGER,
    entry_tx TEXT,
    closed_at TEXT,
    exit_price REAL,
    proceeds_usd REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    exit_tx TEXT,
    lesson_id INTEGER
);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status, mode);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL,
    size_pct REAL,
    hype INTEGER,
    reasoning TEXT,
    features TEXT NOT NULL DEFAULT '{}',
    executed INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT,
    brain TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trade_id INTEGER,
    symbol TEXT,
    pnl_pct REAL,
    lesson TEXT NOT NULL,
    what_worked TEXT,
    what_failed TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    buckets TEXT NOT NULL DEFAULT '[]',
    cited INTEGER NOT NULL DEFAULT 0,
    brain TEXT
);
CREATE TABLE IF NOT EXISTS feature_stats (
    bucket TEXT PRIMARY KEY,
    n INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    total_pnl_pct REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strategy_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    changes TEXT NOT NULL DEFAULT '{}',   -- {setting: [old, new]}
    rejected TEXT NOT NULL DEFAULT '[]',
    pause_minutes REAL NOT NULL DEFAULT 0,
    reasoning TEXT,
    trades_reviewed INTEGER,
    model TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    equity_usd REAL NOT NULL,
    cash_usd REAL NOT NULL
);
"""

JSON_COLS = {"price_path", "features", "buckets", "tags", "changes", "rejected"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def _encode(v: Any) -> Any:
    return json.dumps(v) if isinstance(v, (dict, list)) else v


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.lock = threading.Lock()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(decisions)")}
        if "vibe" in cols and "hype" not in cols:
            self.conn.execute("ALTER TABLE decisions RENAME COLUMN vibe TO hype")
        trade_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        for col, ddl in (("orig_qty", "REAL"), ("tranches_done", "INTEGER NOT NULL DEFAULT 0"),
                         ("realized_usd", "REAL NOT NULL DEFAULT 0")):
            if col not in trade_cols:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {ddl}")
        self.conn.commit()

    # ---- generic ----
    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [_decode(r) for r in self.conn.execute(sql, params).fetchall()]  # type: ignore[misc]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.lock:
            return _decode(self.conn.execute(sql, params).fetchone())

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid or 0

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(_encode(v) for v in row.values()))

    def update(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*(_encode(v) for v in fields.values()), row_id))

    # ---- settings kv ----
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ---- trades ----
    def open_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        if mode:
            return self.query("SELECT * FROM trades WHERE status = 'open' AND mode = ? ORDER BY id", (mode,))
        return self.query("SELECT * FROM trades WHERE status = 'open' ORDER BY id")

    def closed_trades(self, mode: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        if mode:
            return self.query(
                "SELECT * FROM trades WHERE status = 'closed' AND mode = ? ORDER BY id DESC LIMIT ?", (mode, limit)
            )
        return self.query("SELECT * FROM trades WHERE status = 'closed' ORDER BY id DESC LIMIT ?", (limit,))

    def realized_pnl_since(self, mode: str, since_iso: str) -> float:
        row = self.one(
            "SELECT COALESCE(SUM(pnl_usd), 0) AS s FROM trades WHERE status = 'closed' AND mode = ? AND closed_at >= ?",
            (mode, since_iso),
        )
        return float(row["s"]) if row else 0.0
