"""FastAPI app: REST + SSE for the Win95 dashboard."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from vibetrader.brain import memory
from vibetrader.config import env
from vibetrader.trading.engine import Engine

log = logging.getLogger("vibetrader.api")
WEB = Path(__file__).resolve().parents[2] / "web"

engine: Engine | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine
    engine = Engine(env)
    log.info("VibeTrader booted. brain=%s mode=%s", engine.brain_name, engine.mode)
    yield
    await engine.aclose()


app = FastAPI(title="VibeTrader 95", lifespan=lifespan)


def eng() -> Engine:
    assert engine is not None
    return engine


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if env.dashboard_password:
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, env.dashboard_password)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="VibeTrader 95"'})
    return await call_next(request)


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def validation_error(_: Request, exc: ValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors(include_url=False, include_context=False)})


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


# ---------------------------------------------------------------- status
@app.get("/api/status")
async def status():
    e = eng()
    paper_cash = await e.paper.cash_usd()
    paper_eq, _ = await e.equity("paper")
    start = e.db.get_setting("paper_starting_credit")
    out: dict[str, Any] = {
        "running": e.running,
        "mode": e.mode,
        "brain": e.brain_name,
        "groq_enabled": e.groq.enabled,
        "dry_run": env.live_dry_run,
        "last_error": e.last_error or e.groq.last_error,
        "last_tick": e.last_tick,
        "tick_count": e.tick_count,
        "paper": {
            "initialized": start is not None,
            "starting_credit": start,
            "cash": paper_cash,
            "equity": paper_eq,
            "pnl_usd": paper_eq - start if start else 0,
            "pnl_pct": (paper_eq / start - 1) * 100 if start else 0,
            "open": len(e.db.open_trades("paper")),
            "summary": memory.overall_stats(e.db, "paper"),
        },
        "live": {"readiness": e.live_readiness(), "open": len(e.db.open_trades("live"))},
        "lessons": (e.db.one("SELECT COUNT(*) AS n FROM lessons") or {}).get("n", 0),
    }
    if e.mode == "live":
        try:
            lb = e.live_broker()
            eq, cash = await e.equity("live")
            out["live"].update(wallet=lb.pubkey, equity=eq, cash=cash, summary=memory.overall_stats(e.db, "live"))
        except Exception as ex:
            out["live"]["error"] = str(ex)
    return out


# ---------------------------------------------------------------- data
def _slim(t: dict[str, Any], path_points: int = 60) -> dict[str, Any]:
    t = dict(t)
    t["price_path"] = (t.get("price_path") or [])[-path_points:] if path_points else []
    return t


@app.get("/api/positions")
async def positions():
    out = []
    for t in eng().db.open_trades():
        t = _slim(t)
        px = t["last_price"] or t["entry_price"]
        value = px * t["qty"]
        t["value_usd"] = value
        t["unrealized_usd"] = value - t["cost_usd"]
        t["unrealized_pct"] = (value / t["cost_usd"] - 1) * 100 if t["cost_usd"] else 0
        out.append(t)
    return out


@app.get("/api/trades")
async def trades(limit: int = 100, mode: str | None = None):
    return [_slim(t, 0) for t in eng().db.closed_trades(mode, min(limit, 500))]


@app.get("/api/decisions")
async def decisions(limit: int = 100):
    return eng().db.query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (min(limit, 500),))


@app.get("/api/brain")
async def brain(q: str = "", sort: str = "newest", mode: str = "paper"):
    db = eng().db
    order = "cited DESC, id DESC" if sort == "cited" else "id DESC"
    if q:
        like = f"%{q}%"
        lessons = db.query(
            f"SELECT * FROM lessons WHERE lesson LIKE ? OR tags LIKE ? OR symbol LIKE ? ORDER BY {order} LIMIT 200",
            (like, like, like),
        )
    else:
        lessons = db.query(f"SELECT * FROM lessons ORDER BY {order} LIMIT 200")
    equity = db.query("SELECT ts, equity_usd FROM equity WHERE mode = ? ORDER BY rowid DESC LIMIT 600", (mode,))[::-1]
    return {
        "lessons": lessons,
        "stats": memory.bucket_stats(db),
        "overall": {
            "all": memory.overall_stats(db),
            "paper": memory.overall_stats(db, "paper"),
            "live": memory.overall_stats(db, "live"),
        },
        "equity": equity,
    }


class AskBody(BaseModel):
    question: str


@app.post("/api/brain/ask")
async def ask(body: AskBody):
    if not body.question.strip():
        raise HTTPException(400, "ask something")
    return {"answer": await eng().ask(body.question.strip()[:500])}


# ---------------------------------------------------------------- settings / control
@app.get("/api/settings")
async def get_settings():
    return eng().settings().model_dump()


@app.post("/api/settings")
async def post_settings(patch: dict[str, Any]):
    s = eng().save_settings(patch)
    eng().say("SETTINGS.INI saved")
    return s.model_dump()


@app.post("/api/engine/start")
async def start():
    eng().start()
    return {"running": True}


@app.post("/api/engine/stop")
async def stop():
    await eng().stop()
    return {"running": False}


class ResetBody(BaseModel):
    starting_credit: float
    wipe_brain: bool = False


@app.post("/api/paper/reset")
async def reset(body: ResetBody):
    await eng().reset_paper(body.starting_credit, body.wipe_brain)
    return {"ok": True}


class ModeBody(BaseModel):
    mode: str
    confirm: str = ""


@app.post("/api/mode")
async def set_mode(body: ModeBody):
    e = eng()
    was_running = e.running
    await e.stop()
    e.set_mode(body.mode, body.confirm)
    if was_running:
        e.start()
    return {"mode": e.mode}


class KillBody(BaseModel):
    liquidate: bool = False


@app.post("/api/kill")
async def kill(body: KillBody):
    return await eng().kill(body.liquidate)


@app.post("/api/positions/{trade_id}/close")
async def close_position(trade_id: int):
    await eng().close_manual(trade_id)
    return {"ok": True}


# ---------------------------------------------------------------- live feed
@app.get("/api/events")
async def events(request: Request):
    e = eng()
    q: asyncio.Queue = asyncio.Queue()
    e.subscribers.add(q)

    async def gen():
        try:
            for evt in list(e.history):
                yield f"data: {json.dumps({**evt, 'replay': True}, default=str)}\n\n"
            while not await request.is_disconnected():
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(evt, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            e.subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
