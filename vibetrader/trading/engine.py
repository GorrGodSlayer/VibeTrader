"""The main loop. In autonomous mode the AI runs the whole trading process:

  screen   - picks which scanned tokens deserve a full analysis          (screen model)
  decide   - buy/skip, size, stop and target for each pick               (decide model)
  manage   - reviews every open position each tick: hold/sell/move stops (manage model)
  reflect  - post-mortem after every closed trade -> lesson -> concepts  (reflect model)
  strategy - periodically rewrites its own settings, watchlist, pauses   (reflect model)

Hard backstops (hard stop, max hold, live caps, daily loss cap) are enforced by code, never by the model.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from vibetrader.brain import memory, playbook, prompts, strategy
from vibetrader.brain.concepts import ConceptGraph
from vibetrader.brain.fallback import heuristic_decision, heuristic_postmortem
from vibetrader.brain.groq_client import (
    BailCheck, BudgetSkip, Decision, Groq, PositionReview, PostMortem, Screen, StrategyReview,
)
from vibetrader.config import Env, RuntimeSettings
from vibetrader.db import DB, now_iso
from vibetrader.market.dexscreener import DexScreener
from vibetrader.market.features import buckets, build_features
from vibetrader.market.jupiter import Jupiter
from vibetrader.market.rugcheck import RugCheck
from vibetrader.trading import risk
from vibetrader.trading.broker import Broker
from vibetrader.trading.paper import PaperBroker

log = logging.getLogger("vibetrader.engine")

LIVE_CONFIRM_PHRASE = "GO LIVE"
SCREEN_POOL = 12          # tokens rug-checked and shown to the screening model per tick
RESTART_DELAY_S = 30      # watchdog delay before restarting a crashed loop
LESSON_COLS = "id, ts, trade_id, symbol, pnl_pct, lesson, what_worked, what_failed, tags, buckets, cited"


def _minutes_since(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60


def _utc_midnight() -> str:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


class Engine:
    def __init__(self, env: Env):
        self.env = env
        self.db = DB(env.db_path)
        self.http = httpx.AsyncClient(headers={"User-Agent": "VibeTrader/1.0"})
        self.dex = DexScreener(self.http)
        self.rug = RugCheck(self.http)
        self.jup = Jupiter(self.http, env.jupiter_api_base, env.jupiter_api_key)
        self.groq = Groq(env.groq_api_key, env.groq_models, env.groq_rpm, env.groq_tpm, env.groq_tpd)
        self.paper = PaperBroker(self.db, self.settings)
        self._live: Any = None
        self._concepts: ConceptGraph | None = None

        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_tick: str | None = None
        self.tick_count = 0
        self._cooldown: dict[str, float] = {}
        self._last_pairs: dict[str, dict[str, Any]] = {}
        self._notes: dict[str, float] = {}
        self._bg: set[asyncio.Task] = set()
        self.watch_task: asyncio.Task | None = None
        self._closing: set[int] = set()  # trades being sold right now (the watcher and the AI can race)
        self.subscribers: set[asyncio.Queue] = set()
        self.history: deque[dict[str, Any]] = deque(maxlen=150)

    # ------------------------------------------------------------ events
    def emit(self, event_type: str, /, **data: Any) -> None:
        evt = {**data, "type": event_type, "ts": now_iso()}
        self.history.append(evt)
        for q in list(self.subscribers):
            if q.qsize() < 500:
                q.put_nowait(evt)
        if event_type in ("log", "error"):
            log.log(logging.ERROR if event_type == "error" else logging.INFO, data.get("msg"))

    def say(self, msg: str, level: str = "info") -> None:
        self.emit("log", level=level, msg=msg)

    def note_once(self, key: str, msg: str, every_s: float = 600) -> None:
        """Log a recurring condition (e.g. budget skips) at most once per `every_s`."""
        now = time.monotonic()
        if now - self._notes.get(key, -1e9) >= every_s:
            self._notes[key] = now
            self.say(msg, "warn")

    # ------------------------------------------------------------ settings / state
    def settings(self) -> RuntimeSettings:
        return RuntimeSettings(**(self.db.get_setting("runtime") or {}))

    def save_settings(self, patch: dict[str, Any]) -> RuntimeSettings:
        s = RuntimeSettings(**{**self.settings().model_dump(), **patch})  # validates
        self.db.set_setting("runtime", s.model_dump())
        return s

    @property
    def mode(self) -> str:
        return self.db.get_setting("mode", "paper")

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def paper_initialized(self) -> bool:
        return self.db.get_setting("paper_starting_credit") is not None

    @property
    def brain_name(self) -> str:
        return self.env.groq_model if self.groq.enabled else "heuristic"

    @property
    def paused_until(self) -> str | None:
        until = self.db.get_setting("paused_until")
        if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
            return until
        return None

    @property
    def concepts(self) -> ConceptGraph:
        if self._concepts is None:
            self._concepts = ConceptGraph.build(self.db.query(f"SELECT {LESSON_COLS} FROM lessons ORDER BY id"),
                                                playbook.load())
        return self._concepts

    def autonomy_status(self) -> dict[str, Any]:
        s = self.settings()
        last_id = self.db.get_setting("last_review_trade_id", 0)
        pending = (self.db.one("SELECT COUNT(*) AS n FROM trades WHERE status = 'closed' AND id > ?", (last_id,)) or {}).get("n", 0)
        return {
            "autonomous": s.autonomous and self.groq.enabled,
            "engine_enabled": bool(self.db.get_setting("engine_enabled", False)),
            "paused_until": self.paused_until,
            "last_review_ts": self.db.get_setting("last_review_ts"),
            "trades_until_review": max(0, s.strategy_review_every_trades - pending),
        }

    def live_readiness(self) -> dict[str, Any]:
        s = self.settings()
        reasons = []
        if not self.groq.enabled:
            reasons.append("GROQ_API_KEY not set (live trading requires the Groq model)")
        if not self.env.wallet_private_key:
            reasons.append("WALLET_PRIVATE_KEY not set in .env")
        if not self.env.solana_rpc_url:
            reasons.append("SOLANA_RPC_URL not set in .env")
        if importlib.util.find_spec("solders") is None:
            reasons.append("solders not installed (pip install -r requirements-live.txt)")
        if s.live_max_sol_per_trade <= 0:
            reasons.append("set live_max_sol_per_trade")
        if s.live_max_exposure_usd <= 0:
            reasons.append("set live_max_exposure_usd")
        if s.daily_loss_cap_usd <= 0:
            reasons.append("set daily_loss_cap_usd")
        return {"ready": not reasons, "reasons": reasons, "dry_run": self.env.live_dry_run}

    def live_broker(self):
        if self._live is None:
            from vibetrader.trading.live import LiveBroker

            self._live = LiveBroker(
                self.http,
                self.jup,
                self.env.solana_rpc_url,
                self.env.wallet_private_key,
                self.env.live_dry_run,
                lambda: self.settings().live_slippage_bps,
            )
        return self._live

    def broker_for(self, mode: str) -> Broker:
        return self.live_broker() if mode == "live" else self.paper

    async def equity(self, mode: str) -> tuple[float, float]:
        """(equity, cash) in USD. Open positions are marked at last seen price."""
        cash = await self.broker_for(mode).cash_usd()
        held = sum((t["last_price"] or t["entry_price"]) * t["qty"] for t in self.db.open_trades(mode))
        return cash + held, cash

    # ------------------------------------------------------------ control
    def start(self, by_user: bool = True) -> None:
        if self.running:
            return
        if self.mode == "paper" and not self.paper_initialized:
            raise ValueError("Set a paper starting credit first")
        if self.mode == "live" and not self.live_readiness()["ready"]:
            raise ValueError("Live mode is not ready: " + "; ".join(self.live_readiness()["reasons"]))
        if by_user:
            self.db.set_setting("engine_enabled", True)
        self.task = asyncio.create_task(self._loop())
        self.task.add_done_callback(self._on_loop_done)
        self.watch_task = asyncio.create_task(self._watch_loop())
        auto = self.settings().autonomous and self.groq.enabled
        self.say(f"Engine started in {self.mode.upper()} mode ({'autonomous' if auto else 'rule-based'}, "
                 f"decisions by {self.brain_name})")

    async def stop(self, by_user: bool = True) -> None:
        if by_user:
            self.db.set_setting("engine_enabled", False)
        was_running = self.running
        for task in (self.task, self.watch_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self.task = self.watch_task = None
        if was_running:
            self.say("Engine stopped.")

    def autostart(self) -> None:
        """Called at server boot: resume if the engine was running when the server went down."""
        if not self.db.get_setting("engine_enabled", False):
            return
        try:
            self.start(by_user=False)
            self.say("Engine resumed automatically after a restart")
        except ValueError as e:
            self.say(f"Could not resume the engine automatically: {e}", "warn")

    def _on_loop_done(self, task: asyncio.Task) -> None:
        # watchdog: the loop only ends by cancellation; anything else is a crash, so bring it back
        if task.cancelled() or not self.db.get_setting("engine_enabled", False):
            return
        err = task.exception()
        self.emit("error", msg=f"engine loop crashed ({err}); restarting in {RESTART_DELAY_S}s")

        async def restart() -> None:
            await asyncio.sleep(RESTART_DELAY_S)
            if not self.running and self.db.get_setting("engine_enabled", False):
                self.autostart()

        self._spawn(restart())

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def kill(self, liquidate: bool) -> dict[str, Any]:
        await self.stop()
        closed, failed = [], []
        if liquidate:
            async with self.lock:
                for t in self.db.open_trades():
                    try:
                        price, liq = await self._quote_price(t)
                        await self._close(t, price or t["last_price"] or t["entry_price"], liq, "kill_switch")
                        closed.append(t["symbol"])
                    except Exception as e:
                        failed.append(f"{t['symbol']}: {e}")
                        self.emit("error", msg=f"KILL: could not sell {t['symbol']}: {e}")
        self.emit("killed", closed=closed, failed=failed)
        return {"closed": closed, "failed": failed}

    def set_mode(self, mode: str, confirm: str = "") -> None:
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if mode == "live":
            ready = self.live_readiness()
            if not ready["ready"]:
                raise ValueError("Live mode locked: " + "; ".join(ready["reasons"]))
            if confirm.strip().upper() != LIVE_CONFIRM_PHRASE:
                raise ValueError(f'Type "{LIVE_CONFIRM_PHRASE}" to confirm')
            self.live_broker()  # fail fast on a bad key
        self.db.set_setting("mode", mode)
        suffix = " (DRY RUN - simulated only)" if mode == "live" and self.env.live_dry_run else ""
        self.say(f"MODE SWITCHED TO {mode.upper()}{suffix}", "warn" if mode == "live" else "info")
        self.emit("mode", mode=mode)

    def resume_now(self) -> None:
        self.db.set_setting("paused_until", None)
        self.say("Pause lifted; new entries allowed again")

    async def reset_paper(self, starting_credit: float, wipe_brain: bool) -> None:
        if starting_credit <= 0:
            raise ValueError("starting credit must be > 0")
        async with self.lock:
            self.db.execute("DELETE FROM trades WHERE mode = 'paper' AND status = 'open'")
            self.db.execute("DELETE FROM equity WHERE mode = 'paper'")
            if wipe_brain:
                for table in ("lessons", "feature_stats", "decisions", "strategy_changes"):
                    self.db.execute(f"DELETE FROM {table}")
                self.db.execute("DELETE FROM trades WHERE mode = 'paper'")
                self.db.set_setting("last_review_trade_id", 0)
                self._concepts = None
            self.db.set_setting("paper_starting_credit", starting_credit)
            self.db.set_setting("paper_cash", starting_credit)
            self._cooldown.clear()
        self.say(f"Paper account reset to ${starting_credit:,.2f}" + (" (lessons, concepts and journal cleared)" if wipe_brain else ""))
        self.emit("reset")

    async def close_manual(self, trade_id: int) -> None:
        async with self.lock:
            t = self.db.one("SELECT * FROM trades WHERE id = ? AND status = 'open'", (trade_id,))
            if not t:
                raise ValueError("no such open trade")
            price, liq = await self._quote_price(t)
            await self._close(t, price or t["last_price"] or t["entry_price"], liq, "manual")

    # ------------------------------------------------------------ loop
    async def _loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                async with self.lock:
                    await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive no matter what
                self.last_error = str(e)
                self.emit("error", msg=f"tick failed: {e}")
                log.exception("tick failed")
            wait = max(5.0, self.settings().scan_interval_sec - (time.monotonic() - started))
            await asyncio.sleep(wait)

    async def tick(self) -> None:
        self.tick_count += 1
        self.last_tick = now_iso()
        s = self.settings()
        autonomous = s.autonomous and self.groq.enabled
        await self.manage_exits(s, autonomous)
        if autonomous:
            await self.review_positions(s)
        await self.snapshot()
        if autonomous:
            await self.maybe_review_strategy(s)
        await self.scan_and_trade(self.settings(), autonomous)
        self.emit("tick", n=self.tick_count)

    async def snapshot(self) -> None:
        modes = ["paper"] if self.paper_initialized else []
        if self.mode == "live":
            modes.append("live")
        for m in modes:
            try:
                eq, cash = await self.equity(m)
                self.db.insert("equity", {"ts": now_iso(), "mode": m, "equity_usd": eq, "cash_usd": cash})
                self.emit("equity", mode=m, equity=eq, cash=cash)
            except Exception as e:
                self.emit("error", msg=f"equity snapshot ({m}) failed: {e}")

    # ------------------------------------------------------------ exits
    async def _quote_price(self, t: dict[str, Any]) -> tuple[float | None, float]:
        pair = (await self.dex.best_pairs([t["mint"]])).get(t["mint"])
        if not pair:
            return None, 0.0
        return float(pair["priceUsd"]), float((pair.get("liquidity") or {}).get("usd") or 0)

    async def _watch_loop(self) -> None:
        """Price-only exit checks between AI ticks: memecoins can move 20% in the minutes between reviews.
        Uses DexScreener only, so it costs no Groq budget."""
        while True:
            s = self.settings()
            await asyncio.sleep(s.exit_check_sec)
            if not self.db.open_trades():
                continue
            try:
                await self.manage_exits(s, autonomous=True, quiet=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("exit watcher failed: %s", e)

    async def manage_exits(self, s: RuntimeSettings, autonomous: bool, quiet: bool = False) -> None:
        """Mechanical exits every tick: hard backstops plus the stop/target/trailing/max-hold currently in force
        (in autonomous mode the AI keeps moving those)."""
        open_trades = self.db.open_trades()
        if not open_trades:
            return
        pairs = await self.dex.best_pairs(sorted({t["mint"] for t in open_trades}))
        self._last_pairs = pairs
        for t in open_trades:
            pair = pairs.get(t["mint"])
            if not pair:
                if not quiet:
                    self.say(f"no price for {t['symbol']} this tick (rugged? delisted?)", "warn")
                continue
            price = float(pair["priceUsd"])
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            peak = max(t["peak_price"], price)
            path = (t["price_path"] or [])[-299:] + [[int(time.time()), price]]
            self.db.update("trades", t["id"], {"last_price": price, "peak_price": peak, "price_path": path})
            t.update(last_price=price, peak_price=peak, price_path=path)

            held = _minutes_since(t["opened_at"])
            reason = risk.liquidity_drained(t, price, liq, s) or risk.exit_reason(t, price, peak, held, s)
            if not reason:
                due = risk.scale_out_due(t, price, s)
                if due:
                    qty, level, pct = due
                    await self._scale_out(t, price, liq, qty, f"take_profit_{level} (+{pct:g}% level)", level, s)
                    continue
            if (not reason and not autonomous and s.groq_bail_check and self.groq.enabled
                    and held >= 10 and self.tick_count % 3 == 0):
                reason = await self._bail_check(t, pair, price, held)
            if reason:
                try:
                    await self._close(t, price, liq, reason)
                except Exception as e:
                    self.emit("error", msg=f"failed to close {t['symbol']}: {e}")

    async def _bail_check(self, t: dict[str, Any], pair: dict[str, Any], price: float, held: float) -> str | None:
        try:
            pnl = (price / t["entry_price"] - 1) * 100
            raw = await self.groq.json(prompts.BAIL_SYSTEM, prompts.bail_prompt(t, build_features(pair), pnl, held), 0.2,
                                       role="manage", optional=True)
            b = BailCheck(**raw)
            self.emit("thought", symbol=t["symbol"], kind="bail", action=b.action, reasoning=b.reasoning)
            return f"groq_bail: {b.reasoning[:120]}" if b.action == "sell" else None
        except BudgetSkip as e:
            self.note_once("bail", f"exit check skipped: {e}")
            return None
        except Exception as e:
            self.say(f"bail check failed for {t['symbol']}: {e}", "warn")
            return None

    @staticmethod
    def _compact(f: dict[str, Any]) -> dict[str, Any]:
        return {
            "liq_k": round((f.get("liquidity_usd") or 0) / 1e3), "h1": round(f.get("change_h1_pct") or 0, 1),
            "m5": round(f.get("change_m5_pct") or 0, 1), "br": f.get("buy_ratio_h1"), "turn": f.get("vol_to_liq_h24"),
            "vol_h1_k": round((f.get("volume_h1") or 0) / 1e3, 1),
        }

    async def review_positions(self, s: RuntimeSettings) -> None:
        """One batched call: the AI reviews every open position and decides hold/sell and where stops sit."""
        open_trades = self.db.open_trades()
        if not open_trades or self.tick_count % s.position_review_every_ticks:
            return
        rows, bks = [], set()
        for t in open_trades:
            pair = self._last_pairs.get(t["mint"])
            price = t["last_price"] or t["entry_price"]
            bks.update(t.get("buckets") or [])
            rows.append({
                "id": t["id"], "symbol": t["symbol"], "mode": t["mode"],
                "pnl_pct": round((price / t["entry_price"] - 1) * 100, 2),
                "peak_pct": round((t["peak_price"] / t["entry_price"] - 1) * 100, 2),
                "held_min": round(_minutes_since(t["opened_at"])),
                "stop_loss_pct": t["stop_loss_pct"], "take_profit_pct": t["take_profit_pct"],
                "sold_pct": round((1 - t["qty"] / (t.get("orig_qty") or t["qty"])) * 100),
                "tranches_done": t.get("tranches_done") or 0,
                "thesis": (t.get("thesis") or "")[:200],
                "at_entry": self._compact(t.get("features") or {}),
                "now": self._compact(build_features(pair)) if pair else "no price this tick",
            })
        try:
            mechanics = (f"hard stop -{s.hard_stop_loss_pct}%, breakeven lock at +{s.breakeven_lock_pct}% once up "
                         f"+{s.breakeven_after_pct}%, trailing {s.trailing_stop_pct}% after +{s.trailing_activation_pct}%, "
                         f"time stop after {s.time_stop_min:.0f} min below +{s.time_stop_min_gain_pct}%, "
                         f"max hold {s.max_hold_min:.0f} min")
            raw = await self.groq.json(
                prompts.MANAGE_SYSTEM,
                prompts.manage_prompt(rows, self.concepts.relevant(sorted(bks)),
                                      playbook.lines(playbook.select("manage", sorted(bks), k=12)), mechanics),
                0.2, role="manage", optional=True)
            review = PositionReview(**raw)
        except BudgetSkip as e:
            self.note_once("manage", f"position review skipped: {e}")
            return
        except Exception as e:
            self.say(f"position review failed: {e}", "warn")
            return

        by_id = {t["id"]: t for t in open_trades}
        for call in review.positions:
            t = by_id.get(call.id)
            if not t:
                continue
            if call.action == "trim":
                price = t["last_price"] or t["entry_price"]
                pair = self._last_pairs.get(t["mint"]) or {}
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                self.emit("thought", symbol=t["symbol"], kind="manage", action="trim", reasoning=call.reasoning,
                          fraction=call.sell_fraction)
                await self._scale_out(t, price, liq, t["qty"] * call.sell_fraction, f"ai_trim: {call.reasoning[:100]}",
                                      t.get("tranches_done") or 0, s)
                continue
            if call.action == "sell":
                price = t["last_price"] or t["entry_price"]
                pair = self._last_pairs.get(t["mint"]) or {}
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                self.emit("thought", symbol=t["symbol"], kind="manage", action="sell", reasoning=call.reasoning)
                try:
                    await self._close(t, price, liq, f"ai_exit: {call.reasoning[:120]}")
                except Exception as e:
                    self.emit("error", msg=f"failed to close {t['symbol']}: {e}")
                continue
            upd: dict[str, float] = {}
            if call.stop_loss_pct is not None:
                upd["stop_loss_pct"] = round(max(-500.0, min(call.stop_loss_pct, s.hard_stop_loss_pct - 0.5)), 2)
            if call.take_profit_pct is not None:
                upd["take_profit_pct"] = round(max(1.0, min(call.take_profit_pct, 1000.0)), 2)
            upd = {k: v for k, v in upd.items() if v != t[k]}
            if upd:
                self.db.update("trades", t["id"], upd)
            self.emit("thought", symbol=t["symbol"], kind="manage", action="hold", reasoning=call.reasoning,
                      stop=upd.get("stop_loss_pct", t["stop_loss_pct"]), target=upd.get("take_profit_pct", t["take_profit_pct"]),
                      moved=bool(upd))

    async def _close(self, t: dict[str, Any], price: float, liq: float, reason: str) -> None:
        # the exit watcher and the AI review run concurrently; never sell the same position twice
        current = self.db.one("SELECT * FROM trades WHERE id = ?", (t["id"],))
        if t["id"] in self._closing or not current or current["status"] != "open":
            return
        self._closing.add(t["id"])
        try:
            await self._sell_and_record(current, price, liq, reason)
        finally:
            self._closing.discard(t["id"])

    async def _scale_out(self, t: dict[str, Any], price: float, liq: float, qty: float, reason: str, level: int,
                         s: RuntimeSettings) -> None:
        """Sell part of a position (a take-profit tranche or an AI trim) and keep the rest running."""
        current = self.db.one("SELECT * FROM trades WHERE id = ?", (t["id"],))
        if t["id"] in self._closing or not current or current["status"] != "open":
            return
        if qty >= current["qty"] * 0.98:
            await self._close(current, price, liq, reason)
            return
        self._closing.add(t["id"])
        try:
            fill = await self.broker_for(current["mode"]).sell(current, price, liq, qty)
            left = current["qty"] - fill.qty
            raw_left = None
            if current.get("raw_amount") and fill.raw_amount:
                raw_left = str(max(0, int(current["raw_amount"]) - int(fill.raw_amount)))
            upd: dict[str, Any] = {
                "qty": left, "raw_amount": raw_left or current.get("raw_amount"),
                "realized_usd": (current.get("realized_usd") or 0) + fill.usd,
                "tranches_done": max(level, current.get("tranches_done") or 0),
            }
            if level >= 1 and current["stop_loss_pct"] > -s.scale_out_stop_lock_pct:
                upd["stop_loss_pct"] = -s.scale_out_stop_lock_pct  # after the first take-profit, never close as a loss
            self.db.update("trades", current["id"], upd)
            orig = current.get("orig_qty") or current["qty"]
            self.emit("scaled_out", id=current["id"], mode=current["mode"], symbol=current["symbol"], usd=fill.usd,
                      sold_pct=round(fill.qty / orig * 100), left_pct=round(left / orig * 100),
                      gain_pct=(price / current["entry_price"] - 1) * 100, reason=reason)
        except Exception as e:
            self.emit("error", msg=f"partial sell of {current['symbol']} failed: {e}")
        finally:
            self._closing.discard(t["id"])

    async def _sell_and_record(self, t: dict[str, Any], price: float, liq: float, reason: str) -> None:
        fill = await self.broker_for(t["mode"]).sell(t, price, liq)
        pnl_usd = fill.usd + (t.get("realized_usd") or 0) - t["cost_usd"]  # include earlier partial sells
        pnl_pct = pnl_usd / t["cost_usd"] * 100 if t["cost_usd"] else 0
        self.db.update(
            "trades",
            t["id"],
            {
                "status": "closed",
                "closed_at": now_iso(),
                "exit_price": fill.price,
                "proceeds_usd": fill.usd + (t.get("realized_usd") or 0),
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "exit_reason": reason,
                "exit_tx": fill.tx,
            },
        )
        self.emit("trade_closed", id=t["id"], mode=t["mode"], symbol=t["symbol"], pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                  reason=reason, tx=fill.tx)
        self._spawn(self.postmortem(t["id"]))

    async def postmortem(self, trade_id: int) -> None:
        t = self.db.one("SELECT * FROM trades WHERE id = ?", (trade_id,))
        if not t:
            return
        held = (datetime.fromisoformat(t["closed_at"]) - datetime.fromisoformat(t["opened_at"])).total_seconds() / 60
        brain = self.env.groq_reflect_model if self.groq.enabled else "heuristic"
        try:
            if self.groq.enabled:
                pm = PostMortem(**await self.groq.json(prompts.POSTMORTEM_SYSTEM, prompts.postmortem_prompt(t, held), 0.3,
                                                       role="reflect"))
            else:
                pm = PostMortem(**heuristic_postmortem(t))
        except Exception as e:
            self.say(f"post-mortem for {t['symbol']} failed ({e}); storing stats only", "warn")
            pm = PostMortem(**heuristic_postmortem(t))
            brain = "fallback"
        lesson_id = memory.record_outcome(self.db, t, pm.model_dump(), brain)
        self.learn_concept(lesson_id)

    def learn_concept(self, lesson_id: int) -> None:
        """Fold a new lesson into the concept graph and broadcast what it connected."""
        lesson = self.db.one(f"SELECT {LESSON_COLS} FROM lessons WHERE id = ?", (lesson_id,))
        if not lesson:
            return
        fresh_graph = self._concepts is None
        graph = self.concepts  # a fresh build already contains this lesson
        res = graph.assignments[lesson_id] if fresh_graph else graph.add(lesson)
        home = graph.concept(res["concept"])
        self.emit(
            "lesson", id=lesson_id, trade_id=lesson["trade_id"], symbol=lesson["symbol"], pnl_pct=lesson["pnl_pct"],
            lesson=lesson["lesson"], tags=lesson["tags"], buckets=lesson["buckets"], cited=0,
            concept=res["concept"], concept_label=graph.label(home) if home else "", new_concept=res["new_concept"],
            links=[{**lk, "label": graph.label(graph.concept(lk["concept"]))} for lk in res["links"]],
        )

    # ------------------------------------------------------------ strategy
    async def maybe_review_strategy(self, s: RuntimeSettings, force: bool = False) -> dict[str, Any] | None:
        last_id = self.db.get_setting("last_review_trade_id", 0)
        last_ts = self.db.get_setting("last_review_ts")
        new = self.db.query("SELECT * FROM trades WHERE status = 'closed' AND id > ? ORDER BY id", (last_id,))
        hours = _minutes_since(last_ts) / 60 if last_ts else 1e9
        due = len(new) >= s.strategy_review_every_trades or (new and hours >= s.strategy_review_max_hours)
        if not (force or due):
            return None

        mode = self.mode
        stats = [b for b in memory.bucket_stats(self.db) if b["n"] >= 5]
        stats.sort(key=lambda b: b["avg_pnl_pct"])
        recent = {"trades": len(new)}
        if new:
            wins = [t for t in new if (t["pnl_pct"] or 0) > 0]
            recent.update(win_rate=round(len(wins) / len(new), 3),
                          avg_pnl_pct=round(sum(t["pnl_pct"] or 0 for t in new) / len(new), 2),
                          exit_reasons=[str(t["exit_reason"]).split(" ")[0].rstrip(":") for t in new][-20:])
        concepts = sorted(self.concepts.to_dict()["concepts"], key=lambda c: -c["n"])[:12]
        lessons = self.db.query("SELECT symbol, pnl_pct, lesson FROM lessons ORDER BY id DESC LIMIT 12")
        history = [{"ts": h["ts"], "changes": h["changes"], "pause": h["pause_minutes"], "why": h["reasoning"]}
                   for h in self.db.query("SELECT * FROM strategy_changes ORDER BY id DESC LIMIT 5")]
        budget = {m["model"]: f"{m['budget_pct']}% of paced budget left, {m['tokens_today']} tokens used today"
                  for m in self.groq.status()["models"].values()}
        prompt = prompts.strategy_prompt(
            strategy.tunable_snapshot(s), strategy.bounds_text(), memory.overall_stats(self.db, mode), recent,
            stats[-5:][::-1], stats[:5], concepts, lessons, history, budget,
            playbook.lines(playbook.select("strategy", k=18)),
        )
        try:
            review = StrategyReview(**await self.groq.json(prompts.STRATEGY_SYSTEM, prompt, 0.3, role="reflect",
                                                           optional=not force))
        except BudgetSkip as e:
            self.note_once("strategy", f"strategy review postponed: {e}")
            return None
        except Exception as e:
            self.say(f"strategy review failed: {e}", "warn")
            return None

        new_s, applied, rejected = strategy.apply_changes(s, review.changes, review.watchlist_add, review.watchlist_remove)
        self.db.set_setting("runtime", new_s.model_dump())
        if review.pause_minutes > 0:
            until = (datetime.now(timezone.utc) + timedelta(minutes=review.pause_minutes)).isoformat(timespec="seconds")
            self.db.set_setting("paused_until", until)
        row = {
            "ts": now_iso(), "changes": applied, "rejected": rejected, "pause_minutes": review.pause_minutes,
            "reasoning": review.reasoning, "trades_reviewed": len(new), "model": self.env.groq_reflect_model,
        }
        row_id = self.db.insert("strategy_changes", row)
        self.db.set_setting("last_review_trade_id", new[-1]["id"] if new else last_id)
        self.db.set_setting("last_review_ts", row["ts"])
        self.emit("strategy", id=row_id, **row)
        return {"id": row_id, **row}

    # ------------------------------------------------------------ entries
    async def scan_and_trade(self, s: RuntimeSettings, autonomous: bool) -> None:
        mode = self.mode
        if mode == "paper" and not self.paper_initialized:
            self.say("No paper starting credit set; configure it in Settings", "warn")
            return
        if self.paused_until:
            self.note_once("paused", f"New entries paused by the strategist until {self.paused_until}", 1800)
            return
        if mode == "live" and not self.live_readiness()["ready"]:
            self.emit("error", msg="Live mode lost readiness; not opening new trades")
            return
        realized_today = self.db.realized_pnl_since(mode, _utc_midnight())
        if s.daily_loss_cap_usd > 0 and realized_today <= -s.daily_loss_cap_usd:
            self.note_once("losscap", f"Daily loss cap hit ({realized_today:.2f} USD). No new entries until 00:00 UTC.")
            return
        if len(self.db.open_trades(mode)) >= s.max_open_positions:
            return
        opened_today = (self.db.one("SELECT COUNT(*) AS n FROM trades WHERE mode = ? AND opened_at >= ?",
                                    (mode, _utc_midnight())) or {}).get("n", 0)
        if opened_today >= s.max_entries_per_day:
            self.note_once("entrycap", f"Daily entry cap reached ({opened_today}/{s.max_entries_per_day}); managing exits only")
            return
        if s.loss_streak_pause > 0:
            last = self.db.query("SELECT pnl_usd, closed_at FROM trades WHERE status = 'closed' AND mode = ? "
                                 "ORDER BY closed_at DESC LIMIT ?", (mode, s.loss_streak_pause))
            if (len(last) == s.loss_streak_pause and all((t["pnl_usd"] or 0) <= 0 for t in last)
                    and _minutes_since(last[0]["closed_at"]) < s.loss_streak_pause_min):
                self.note_once("streak", f"{s.loss_streak_pause} losses in a row; cooling down for "
                               f"{s.loss_streak_pause_min:.0f} min before new entries (no revenge trading)")
                return
        if self.groq.enabled and not self.groq.can_afford("decide", est=2500):
            self.note_once("decide-budget", f"{self.env.groq_model} daily budget paced out; skipping entries this tick")
            return

        now = time.time()
        held = {t["mint"] for t in self.db.open_trades()}
        mints = await self.dex.trending_mints()
        if s.include_watchlist:
            mints += [m for m in s.watchlist if m not in mints]
        fresh = [m for m in mints if m not in held and now - self._cooldown.get(m, 0) > s.decision_cooldown_min * 60]
        if not fresh:
            return
        pairs = await self.dex.best_pairs(fresh)

        # cheap pre-filter without rugcheck, then the most active few get a rug check
        pre = []
        for mint, pair in pairs.items():
            f = build_features(pair)
            if risk.filter_reason({**f, "rug_score": None}, s):
                self._cooldown[mint] = now
                continue
            pre.append((f["volume_h1"], mint, pair))
        pre.sort(key=lambda x: x[0], reverse=True)

        pool: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for _, mint, pair in pre[:SCREEN_POOL]:
            self._cooldown[mint] = now  # considered this round; fresh tokens get the next look
            f = build_features(pair, await self.rug.summary(mint))
            why = risk.filter_reason(f, s)
            if why:
                self._log_decision(mode, f, None, skip_reason=why, brain="filter")
                continue
            pool.append((f, pair))
        self.say(f"scan: {len(mints)} tokens, {len(pairs)} priced, {len(pre)} passed filters, {len(pool)} passed rug check")
        if not pool:
            return

        picks = pool[: s.max_candidates_per_tick]
        if autonomous and len(pool) > 1:
            picks = await self._screen(pool, s, mode) or []
        for f, pair in picks:
            if not await self._evaluate(mode, f, pair):
                break
            if len(self.db.open_trades(mode)) >= s.max_open_positions:
                break

    async def _screen(self, pool: list[tuple[dict[str, Any], dict[str, Any]]], s: RuntimeSettings, mode: str
                      ) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
        eq, cash = await self.equity(mode)
        closed = (self.db.one("SELECT COUNT(*) AS n FROM trades WHERE status = 'closed'") or {}).get("n", 0)
        portfolio = {"mode": mode, "closed_trades": closed, "cash_usd": round(cash), "equity_usd": round(eq),
                     "open": [t["symbol"] for t in self.db.open_trades(mode)]}
        concepts = sorted(self.concepts.to_dict()["concepts"], key=lambda c: -c["n"])[:8]
        try:
            raw = await self.groq.json(prompts.SCREEN_SYSTEM,
                                       prompts.screen_prompt([f for f, _ in pool], concepts, s.max_candidates_per_tick, portfolio,
                                                             playbook.lines(playbook.select("screen", k=8))),
                                       0.3, role="screen", optional=True)
            screen = Screen(**raw)
        except BudgetSkip as e:
            self.note_once("screen", f"screening skipped: {e}")
            return None
        except Exception as e:
            self.say(f"screening failed ({e}); falling back to the most active tokens", "warn")
            return pool[: s.max_candidates_per_tick]
        by_sym = {}
        for f, pair in pool:
            by_sym.setdefault(str(f["symbol"]).upper(), (f, pair))
        picks = []
        for p in screen.picks[: s.max_candidates_per_tick]:
            hit = by_sym.pop(p.symbol.strip().upper().lstrip("$"), None)
            if hit:
                picks.append(hit)
        self.emit("thought", kind="screen", symbol=", ".join(p.symbol for p in screen.picks) or "none",
                  action="screen", reasoning=screen.reasoning or "; ".join(f"{p.symbol}: {p.why}" for p in screen.picks),
                  pool=len(pool))
        return picks

    async def _decide(self, mode: str, f: dict[str, Any], b: list[str]) -> tuple[Decision, str]:
        if not self.groq.enabled:
            return Decision(**heuristic_decision(f)), "heuristic"
        eq, cash = await self.equity(mode)
        portfolio = {
            "mode": mode,
            "cash_usd": round(cash, 2),
            "equity_usd": round(eq, 2),
            "open_positions": [t["symbol"] for t in self.db.open_trades(mode)],
        }
        prompt = prompts.decision_prompt(
            f, b, memory.relevant_lessons(self.db, b), memory.bucket_stats(self.db, b), memory.overall_stats(self.db),
            portfolio, self.concepts.relevant(b), playbook.lines(playbook.select("decide", b, k=10)),
        )
        raw = await self.groq.json(prompts.DECISION_SYSTEM, prompt, role="decide", optional=True)
        return Decision(**raw), self.env.groq_model

    async def _evaluate(self, mode: str, f: dict[str, Any], pair: dict[str, Any]) -> bool:
        """Returns False when the decision budget ran out, so the caller stops for this tick."""
        s = self.settings()
        b = buckets(f)
        try:
            d, brain = await self._decide(mode, f, b)
        except BudgetSkip as e:
            self.note_once("decide", f"entry decisions paused: {e}")
            return False
        except Exception as e:
            self._log_decision(mode, f, None, skip_reason=f"brain error: {e}", brain=self.brain_name)
            self.emit("error", msg=f"brain error on {f.get('symbol')}: {e}")
            return True
        decision_id = self._log_decision(mode, f, d, brain=brain)
        if d.action != "buy":
            return True

        broker = self.broker_for(mode)
        eq, cash = await self.equity(mode)
        sol_px = await self.jup.sol_price() if mode == "live" else None
        order = risk.size_order(
            d, f, s, mode=mode, cash_usd=cash, equity_usd=eq, open_trades=self.db.open_trades(mode),
            realized_today_usd=self.db.realized_pnl_since(mode, _utc_midnight()), sol_price=sol_px,
        )
        if isinstance(order, str):
            self.db.update("decisions", decision_id, {"skip_reason": order})
            self.emit("decision_blocked", id=decision_id, symbol=f["symbol"], reason=order)
            return True
        try:
            fill = await broker.buy(f["mint"], order.usd, f["price_usd"], f["liquidity_usd"])
        except Exception as e:
            self.db.update("decisions", decision_id, {"skip_reason": f"execution failed: {e}"})
            self.emit("error", msg=f"BUY {f['symbol']} failed: {e}")
            return True

        trade_id = self.db.insert(
            "trades",
            {
                "mode": mode,
                "mint": f["mint"],
                "symbol": f["symbol"],
                "pair_address": pair.get("pairAddress"),
                "status": "open",
                "opened_at": now_iso(),
                "entry_price": fill.price,
                "qty": fill.qty,
                "orig_qty": fill.qty,
                "raw_amount": fill.raw_amount,
                "decimals": fill.decimals,
                "cost_usd": fill.usd,
                "stop_loss_pct": order.stop_loss_pct,
                "take_profit_pct": order.take_profit_pct,
                "peak_price": fill.price,
                "last_price": f["price_usd"],
                "price_path": [[int(time.time()), f["price_usd"]]],
                "features": f,
                "buckets": b,
                "thesis": d.reasoning,
                "decision_id": decision_id,
                "entry_tx": fill.tx,
            },
        )
        self.db.update("decisions", decision_id, {"executed": 1})
        self.emit("trade_opened", id=trade_id, mode=mode, symbol=f["symbol"], usd=fill.usd, price=fill.price,
                  hype=d.hype_score, reasoning=d.reasoning, tx=fill.tx, explore=d.explore and mode == "paper")
        return True

    def _log_decision(self, mode: str, f: dict[str, Any], d: Decision | None, *, skip_reason: str | None = None,
                      brain: str = "") -> int:
        row = {
            "ts": now_iso(),
            "mode": mode,
            "mint": f.get("mint"),
            "symbol": f.get("symbol", "???"),
            "action": d.action if d else "skip",
            "confidence": d.confidence if d else None,
            "size_pct": d.size_pct if d else None,
            "hype": d.hype_score if d else None,
            "reasoning": d.reasoning if d else None,
            "features": f,
            "skip_reason": skip_reason,
            "brain": brain,
        }
        decision_id = self.db.insert("decisions", row)
        self.emit("thought", id=decision_id, symbol=row["symbol"], kind="entry", action=row["action"],
                  confidence=row["confidence"], hype=row["hype"], reasoning=row["reasoning"], skip_reason=skip_reason,
                  brain=brain, price=f.get("price_usd"), liquidity=f.get("liquidity_usd"))
        return decision_id

    # ------------------------------------------------------------ brain Q&A
    async def ask(self, question: str) -> str:
        lessons = self.db.query("SELECT * FROM lessons ORDER BY id DESC LIMIT 60")
        stats = memory.bucket_stats(self.db)
        overall = memory.overall_stats(self.db)
        if not self.groq.enabled:
            return f"No GROQ_API_KEY is set, so there is no model to answer. Overall record: {overall}"
        concepts = sorted(self.concepts.to_dict()["concepts"], key=lambda c: -c["n"])[:15]
        return await self.groq.text(prompts.ASK_SYSTEM, prompts.ask_prompt(question, lessons, stats, overall, concepts))

    async def aclose(self) -> None:
        await self.stop(by_user=False)
        await self.http.aclose()
