"""The main loop: scan -> filter -> decide (Groq) -> execute -> manage exits -> post-mortem -> learn."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from vibetrader.brain import memory, prompts
from vibetrader.brain.fallback import heuristic_decision, heuristic_postmortem
from vibetrader.brain.groq_client import BailCheck, Decision, Groq, PostMortem
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

LIVE_CONFIRM_PHRASE = "SEND IT"


def _minutes_since(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60


def _utc_midnight() -> str:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


class Engine:
    def __init__(self, env: Env):
        self.env = env
        self.db = DB(env.db_path)
        self.http = httpx.AsyncClient(headers={"User-Agent": "VibeTrader/95.0 (Windows 95; Memecoin Edition)"})
        self.dex = DexScreener(self.http)
        self.rug = RugCheck(self.http)
        self.jup = Jupiter(self.http, env.jupiter_api_base, env.jupiter_api_key)
        self.groq = Groq(env.groq_api_key, env.groq_model)
        self.paper = PaperBroker(self.db, self.settings)
        self._live: Any = None

        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_tick: str | None = None
        self.tick_count = 0
        self._cooldown: dict[str, float] = {}
        self._bg: set[asyncio.Task] = set()
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
        return self.env.groq_model if self.groq.enabled else "magic8ball"

    def live_readiness(self) -> dict[str, Any]:
        s = self.settings()
        reasons = []
        if not self.groq.enabled:
            reasons.append("GROQ_API_KEY not set (no live trading on the magic 8-ball)")
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
    def start(self) -> None:
        if self.running:
            return
        if self.mode == "paper" and not self.paper_initialized:
            raise ValueError("Set a paper starting credit first")
        if self.mode == "live" and not self.live_readiness()["ready"]:
            raise ValueError("Live mode is not ready: " + "; ".join(self.live_readiness()["reasons"]))
        self.task = asyncio.create_task(self._loop())
        self.say(f"Engine started in {self.mode.upper()} mode with brain={self.brain_name}")

    async def stop(self) -> None:
        was_running = self.running
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        if was_running:
            self.say("Engine stopped. It is now safe to turn off your computer.")

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

    async def reset_paper(self, starting_credit: float, wipe_brain: bool) -> None:
        if starting_credit <= 0:
            raise ValueError("starting credit must be > 0")
        async with self.lock:
            self.db.execute("DELETE FROM trades WHERE mode = 'paper' AND status = 'open'")
            self.db.execute("DELETE FROM equity WHERE mode = 'paper'")
            if wipe_brain:
                self.db.execute("DELETE FROM lessons")
                self.db.execute("DELETE FROM feature_stats")
                self.db.execute("DELETE FROM trades WHERE mode = 'paper'")
                self.db.execute("DELETE FROM decisions")
            self.db.set_setting("paper_starting_credit", starting_credit)
            self.db.set_setting("paper_cash", starting_credit)
            self._cooldown.clear()
        self.say(f"Paper account reset to ${starting_credit:,.2f}" + (" and the brain was lobotomized" if wipe_brain else ""))
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
        await self.manage_exits()
        await self.snapshot()
        await self.scan_and_trade()
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

    async def manage_exits(self) -> None:
        open_trades = self.db.open_trades()
        if not open_trades:
            return
        s = self.settings()
        pairs = await self.dex.best_pairs(sorted({t["mint"] for t in open_trades}))
        for t in open_trades:
            pair = pairs.get(t["mint"])
            if not pair:
                self.say(f"no price for {t['symbol']} this tick (rugged? delisted?)", "warn")
                continue
            price = float(pair["priceUsd"])
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            peak = max(t["peak_price"], price)
            path = (t["price_path"] or [])[-299:] + [[int(time.time()), price]]
            self.db.update("trades", t["id"], {"last_price": price, "peak_price": peak, "price_path": path})
            t.update(last_price=price, peak_price=peak, price_path=path)

            held = _minutes_since(t["opened_at"])
            reason = risk.exit_reason(t, price, peak, held, s)
            if not reason and s.groq_bail_check and self.groq.enabled and held >= 10 and self.tick_count % 3 == 0:
                reason = await self._bail_check(t, pair, price, held)
            if reason:
                try:
                    await self._close(t, price, liq, reason)
                except Exception as e:
                    self.emit("error", msg=f"failed to close {t['symbol']}: {e}")

    async def _bail_check(self, t: dict[str, Any], pair: dict[str, Any], price: float, held: float) -> str | None:
        try:
            pnl = (price / t["entry_price"] - 1) * 100
            raw = await self.groq.json(prompts.BAIL_SYSTEM, prompts.bail_prompt(t, build_features(pair), pnl, held), 0.2)
            b = BailCheck(**raw)
            self.emit("thought", symbol=t["symbol"], kind="bail", action=b.action, reasoning=b.reasoning)
            return f"groq_bail: {b.reasoning[:120]}" if b.action == "sell" else None
        except Exception as e:
            self.say(f"bail check failed for {t['symbol']}: {e}", "warn")
            return None

    async def _close(self, t: dict[str, Any], price: float, liq: float, reason: str) -> None:
        fill = await self.broker_for(t["mode"]).sell(t, price, liq)
        pnl_usd = fill.usd - t["cost_usd"]
        pnl_pct = pnl_usd / t["cost_usd"] * 100 if t["cost_usd"] else 0
        self.db.update(
            "trades",
            t["id"],
            {
                "status": "closed",
                "closed_at": now_iso(),
                "exit_price": fill.price,
                "proceeds_usd": fill.usd,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "exit_reason": reason,
                "exit_tx": fill.tx,
            },
        )
        self.emit("trade_closed", id=t["id"], mode=t["mode"], symbol=t["symbol"], pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                  reason=reason, tx=fill.tx)
        task = asyncio.create_task(self.postmortem(t["id"]))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def postmortem(self, trade_id: int) -> None:
        t = self.db.one("SELECT * FROM trades WHERE id = ?", (trade_id,))
        if not t:
            return
        held = (datetime.fromisoformat(t["closed_at"]) - datetime.fromisoformat(t["opened_at"])).total_seconds() / 60
        brain = self.brain_name
        try:
            if self.groq.enabled:
                pm = PostMortem(**await self.groq.json(prompts.POSTMORTEM_SYSTEM, prompts.postmortem_prompt(t, held), 0.3))
            else:
                pm = PostMortem(**heuristic_postmortem(t))
        except Exception as e:
            self.say(f"post-mortem for {t['symbol']} failed ({e}); storing stats only", "warn")
            pm = PostMortem(**heuristic_postmortem(t))
            brain = "fallback"
        lesson_id = memory.record_outcome(self.db, t, pm.model_dump(), brain)
        self.emit("lesson", id=lesson_id, trade_id=trade_id, symbol=t["symbol"], pnl_pct=t["pnl_pct"], lesson=pm.lesson,
                  tags=pm.tags)

    # ------------------------------------------------------------ entries
    async def scan_and_trade(self) -> None:
        s = self.settings()
        mode = self.mode
        if mode == "paper" and not self.paper_initialized:
            self.say("No paper credit set - open SETTINGS and give me money", "warn")
            return
        if mode == "live" and not self.live_readiness()["ready"]:
            self.emit("error", msg="Live mode lost readiness; not opening new trades")
            return

        realized_today = self.db.realized_pnl_since(mode, _utc_midnight())
        if s.daily_loss_cap_usd > 0 and realized_today <= -s.daily_loss_cap_usd:
            self.say(f"Daily loss cap hit ({realized_today:.2f} USD). No new entries until 00:00 UTC.", "warn")
            return
        if len(self.db.open_trades(mode)) >= s.max_open_positions:
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

        # cheap pre-filter without rugcheck, then rank by recent volume
        pre = []
        for mint, pair in pairs.items():
            f = build_features(pair)
            if risk.filter_reason({**f, "rug_score": None}, s):
                self._cooldown[mint] = now
                continue
            pre.append((f["volume_h1"], mint, pair))
        pre.sort(key=lambda x: x[0], reverse=True)
        self.say(f"scan: {len(mints)} tokens, {len(pairs)} priced, {len(pre)} passed filters")

        for _, mint, pair in pre[: s.max_candidates_per_tick]:
            self._cooldown[mint] = now
            f = build_features(pair, await self.rug.summary(mint))
            why = risk.filter_reason(f, s)
            if why:
                self._log_decision(mode, f, None, skip_reason=why, brain="filter")
                continue
            await self._evaluate(mode, f, pair)
            if len(self.db.open_trades(mode)) >= s.max_open_positions:
                break

    async def _decide(self, mode: str, f: dict[str, Any], b: list[str]) -> tuple[Decision, str]:
        if not self.groq.enabled:
            return Decision(**heuristic_decision(f)), "magic8ball"
        eq, cash = await self.equity(mode)
        portfolio = {
            "mode": mode,
            "cash_usd": round(cash, 2),
            "equity_usd": round(eq, 2),
            "open_positions": [t["symbol"] for t in self.db.open_trades(mode)],
        }
        prompt = prompts.decision_prompt(
            f, b, memory.relevant_lessons(self.db, b), memory.bucket_stats(self.db, b), memory.overall_stats(self.db),
            portfolio,
        )
        return Decision(**await self.groq.json(prompts.DECISION_SYSTEM, prompt)), self.env.groq_model

    async def _evaluate(self, mode: str, f: dict[str, Any], pair: dict[str, Any]) -> None:
        s = self.settings()
        b = buckets(f)
        try:
            d, brain = await self._decide(mode, f, b)
        except Exception as e:
            self._log_decision(mode, f, None, skip_reason=f"brain error: {e}", brain=self.brain_name)
            self.emit("error", msg=f"brain error on {f.get('symbol')}: {e}")
            return
        decision_id = self._log_decision(mode, f, d, brain=brain)
        if d.action != "buy":
            return

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
            return
        try:
            fill = await broker.buy(f["mint"], order.usd, f["price_usd"], f["liquidity_usd"])
        except Exception as e:
            self.db.update("decisions", decision_id, {"skip_reason": f"execution failed: {e}"})
            self.emit("error", msg=f"BUY {f['symbol']} failed: {e}")
            return

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
                  vibe=d.meme_vibe_score, reasoning=d.reasoning, tx=fill.tx)

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
            "vibe": d.meme_vibe_score if d else None,
            "reasoning": d.reasoning if d else None,
            "features": f,
            "skip_reason": skip_reason,
            "brain": brain,
        }
        decision_id = self.db.insert("decisions", row)
        self.emit("thought", id=decision_id, symbol=row["symbol"], kind="entry", action=row["action"],
                  confidence=row["confidence"], vibe=row["vibe"], reasoning=row["reasoning"], skip_reason=skip_reason,
                  brain=brain, price=f.get("price_usd"), liquidity=f.get("liquidity_usd"))
        return decision_id

    # ------------------------------------------------------------ brain Q&A
    async def ask(self, question: str) -> str:
        lessons = self.db.query("SELECT * FROM lessons ORDER BY id DESC LIMIT 60")
        stats = memory.bucket_stats(self.db)
        overall = memory.overall_stats(self.db)
        if not self.groq.enabled:
            return ("It looks like you're trying to ask a question! I'd help, but GROQ_API_KEY is missing, "
                    f"so all I know is: {overall}")
        return await self.groq.text(prompts.ASK_SYSTEM, prompts.ask_prompt(question, lessons, stats, overall))

    async def aclose(self) -> None:
        await self.stop()
        await self.http.aclose()
