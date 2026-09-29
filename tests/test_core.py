import asyncio

import pytest

from vibetrader.brain import memory
from vibetrader.brain.groq_client import BailCheck, Decision, PostMortem, parse_json
from vibetrader.config import RuntimeSettings
from vibetrader.db import DB, now_iso
from vibetrader.market.features import buckets, build_features
from vibetrader.trading import risk
from vibetrader.trading.paper import PaperBroker, slippage_pct


@pytest.fixture
def db(tmp_path):
    return DB(str(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


PAIR = {
    "baseToken": {"address": "MINT1", "symbol": "BONK", "name": "Bonk"},
    "priceUsd": "0.00002",
    "liquidity": {"usd": 80_000},
    "volume": {"h24": 400_000, "h1": 20_000, "m5": 1_000},
    "priceChange": {"m5": 1, "h1": 12, "h6": 20, "h24": 30},
    "txns": {"h1": {"buys": 300, "sells": 100}},
    "pairCreatedAt": None,
}


# ---------------------------------------------------------------- paper fills
def test_slippage_grows_with_size():
    assert slippage_pct(100, 100_000, 0.5) == pytest.approx(0.5 + 100 / 50_000 * 100)
    assert slippage_pct(10_000, 100_000, 0.5) > slippage_pct(100, 100_000, 0.5)


def test_paper_round_trip_loses_fees_at_flat_price(db):
    s = RuntimeSettings()
    db.set_setting("paper_cash", 1000.0)
    b = PaperBroker(db, lambda: s)
    buy = run(b.buy("MINT1", 100, 1.0, 1_000_000))
    assert run(b.cash_usd()) == pytest.approx(900)
    assert buy.qty < 100  # fees + slippage
    sell = run(b.sell({"qty": buy.qty}, 1.0, 1_000_000))
    assert sell.usd < 100
    assert run(b.cash_usd()) == pytest.approx(900 + sell.usd)


def test_paper_rejects_overspend(db):
    db.set_setting("paper_cash", 50.0)
    b = PaperBroker(db, lambda: RuntimeSettings())
    with pytest.raises(ValueError):
        run(b.buy("MINT1", 100, 1.0, 1_000_000))


# ---------------------------------------------------------------- risk
def _decision(**kw):
    base = dict(action="buy", confidence=0.9, size_pct=50, stop_loss_pct=80, take_profit_pct=2000, reasoning="x")
    return Decision(**{**base, **kw})


def test_size_order_caps_everything():
    s = RuntimeSettings(max_position_pct=10, max_liquidity_share_pct=1)
    f = {"mint": "M", "liquidity_usd": 200_000}
    o = risk.size_order(_decision(), f, s, mode="paper", cash_usd=10_000, equity_usd=10_000, open_trades=[],
                        realized_today_usd=0)
    assert isinstance(o, risk.Order)
    assert o.usd == 1000  # 10% of equity; 1% of liq = 2000
    assert o.stop_loss_pct == 50  # clamped
    assert o.take_profit_pct == 1000  # clamped


def test_size_order_liquidity_cap():
    s = RuntimeSettings(max_position_pct=50, max_liquidity_share_pct=1)
    o = risk.size_order(_decision(), {"mint": "M", "liquidity_usd": 30_000}, s, mode="paper", cash_usd=10_000,
                        equity_usd=10_000, open_trades=[], realized_today_usd=0)
    assert o.usd == 300


@pytest.mark.parametrize(
    "kw,expect",
    [
        (dict(d=_decision(action="skip")), "skip"),
        (dict(d=_decision(confidence=0.1)), "confidence"),
        (dict(open_trades=[{"mint": "M", "cost_usd": 1}]), "already holding"),
        (dict(realized_today_usd=-500), "daily loss cap"),
    ],
)
def test_size_order_rejections(kw, expect):
    s = RuntimeSettings(daily_loss_cap_usd=100)
    args = dict(d=_decision(), open_trades=[], realized_today_usd=0)
    args.update(kw)
    o = risk.size_order(args["d"], {"mint": "M", "liquidity_usd": 1e6}, s, mode="paper", cash_usd=1000,
                        equity_usd=1000, open_trades=args["open_trades"], realized_today_usd=args["realized_today_usd"])
    assert isinstance(o, str) and expect in o


def test_paper_exploration_is_small_and_paper_only():
    s = RuntimeSettings(min_confidence=0.6, explore_size_pct=2, explore_min_confidence=0.25, max_position_pct=10)
    probe = _decision(confidence=0.35, size_pct=10, explore=True)
    f = {"mint": "M", "liquidity_usd": 1e6}
    o = risk.size_order(probe, f, s, mode="paper", cash_usd=1000, equity_usd=1000, open_trades=[], realized_today_usd=0)
    assert isinstance(o, risk.Order) and o.usd == 20  # 2% of equity despite asking for 10%
    live = risk.size_order(probe, f, s, mode="live", cash_usd=1000, equity_usd=1000, open_trades=[],
                           realized_today_usd=0, sol_price=100)
    assert isinstance(live, str) and "confidence" in live  # no exploring with real money
    too_unsure = _decision(confidence=0.1, explore=True)
    assert "exploration bar" in risk.size_order(too_unsure, f, s, mode="paper", cash_usd=1000, equity_usd=1000,
                                                open_trades=[], realized_today_usd=0)


def test_live_caps_apply():
    s = RuntimeSettings(max_position_pct=100, live_max_sol_per_trade=0.5, live_max_exposure_usd=120)
    o = risk.size_order(_decision(size_pct=100), {"mint": "M", "liquidity_usd": 1e9}, s, mode="live", cash_usd=10_000,
                        equity_usd=10_000, open_trades=[{"mint": "X", "cost_usd": 50}], realized_today_usd=0,
                        sol_price=200)
    assert o.usd == 70  # min(0.5 SOL=$100, exposure room $70)
    assert isinstance(risk.size_order(_decision(), {"mint": "M", "liquidity_usd": 1e9}, s, mode="live", cash_usd=1e4,
                                      equity_usd=1e4, open_trades=[], realized_today_usd=0, sol_price=None), str)


def test_exit_rules():
    s = RuntimeSettings(trailing_stop_pct=10, trailing_activation_pct=20, max_hold_min=60)
    t = {"entry_price": 1.0, "stop_loss_pct": 15, "take_profit_pct": 50}
    assert risk.exit_reason(t, 0.84, 1.0, 5, s).startswith("stop_loss")
    assert risk.exit_reason(t, 1.51, 1.51, 5, s).startswith("take_profit")
    assert risk.exit_reason(t, 1.25, 1.45, 5, s).startswith("trailing_stop")
    assert risk.exit_reason(t, 1.05, 1.1, 5, s) is None
    assert risk.exit_reason(t, 1.05, 1.1, 61, s).startswith("max_hold")


def test_filters():
    f = build_features(PAIR, {"score": 10})
    assert "liquidity" in risk.filter_reason(f, RuntimeSettings(min_liquidity_usd=100_000))
    assert "rug" in risk.filter_reason(f, RuntimeSettings(min_liquidity_usd=10_000, max_rug_score=5))
    assert risk.filter_reason(f, RuntimeSettings(min_liquidity_usd=10_000)) is None


# ---------------------------------------------------------------- features
def test_features_and_buckets():
    f = build_features(PAIR, {"score": 10, "risks": [{"name": "Mutable metadata", "level": "warn"}]})
    assert f["buy_ratio_h1"] == 0.75
    assert f["vol_to_liq_h24"] == 5.0
    assert f["rug_risks"] == ["Mutable metadata"]
    assert buckets(f) == ["liq:25k-100k", "age:unknown", "h1:pump", "flow:buyers", "turnover:high", "rug:clean"]


# ---------------------------------------------------------------- groq parsing
@pytest.mark.parametrize(
    "text",
    [
        '{"action":"buy","confidence":0.8}',
        '```json\n{"action":"buy","confidence":0.8}\n```',
        'Sure! Here you go: {"action":"buy","confidence":0.8} hope that helps',
        'noise {broken json} then {"action":"buy","confidence":0.8}',
    ],
)
def test_parse_json_tolerant(text):
    assert parse_json(text)["action"] == "buy"


def test_parse_json_unescapes_html():
    out = parse_json('{"lesson": "skip if 5m volume &lt;10% of 1h", "tags": ["a&amp;b"]}')
    assert out == {"lesson": "skip if 5m volume <10% of 1h", "tags": ["a&b"]}


def test_parse_json_garbage():
    with pytest.raises(ValueError):
        parse_json("I refuse to answer in JSON")


def test_decision_coercion():
    d = Decision(action="APE", confidence=85, size_pct=250, stop_loss_pct=-12, take_profit_pct="40", hype_score="11")
    assert d.action == "buy" and d.confidence == 0.85 and d.size_pct == 100
    assert d.stop_loss_pct == 12 and d.take_profit_pct == 40 and d.hype_score == 10
    assert Decision(action="hold").action == "skip"
    assert BailCheck(action="EXIT").action == "sell"
    assert PostMortem(lesson="x", tags="a, b,,c").tags == ["a", "b", "c"]


def test_limiter_paces_daily_tokens():
    from vibetrader.brain.groq_client import BudgetSkip, ModelLimiter

    lim = ModelLimiter("m", rpm=30, tpm=8000, tpd=200_000)
    assert lim.can_afford(2000)
    run(lim.acquire(2000, optional=True))
    lim.settle(2000, 1500)
    assert lim.tokens_today == 1500 and lim.requests_today == 1
    lim.day.level = 100  # daily budget paced out
    assert not lim.can_afford(2000)
    with pytest.raises(BudgetSkip):
        run(lim.acquire(2000, optional=True))
    lim.rate_limited(retry_after=60, daily=False)
    assert lim.status()["cooldown_s"] > 50


def test_groq_routes_jobs_to_models():
    from vibetrader.brain.groq_client import Groq

    g = Groq("", {"decide": "big", "screen": "small", "manage": "mid", "reflect": "small"})
    assert g.models["decide"] == "big" and g.limiter("screen") is g.limiter("reflect")
    assert set(g.limiters) == {"big", "small", "mid"}  # one quota per distinct model


def test_strategy_changes_are_clamped():
    from vibetrader.brain.strategy import apply_changes

    s = RuntimeSettings()
    new, applied, rejected = apply_changes(
        s,
        {"max_position_pct": 90, "min_confidence": 0.7, "live_max_sol_per_trade": 5, "hard_stop_loss_pct": 90,
         "include_watchlist": "false"},
        add=["DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "not-a-mint"], remove=[s.watchlist[0]],
    )
    assert new.max_position_pct == 10 and new.min_confidence == 0.7 and new.include_watchlist is False
    assert new.live_max_sol_per_trade == s.live_max_sol_per_trade and new.hard_stop_loss_pct == s.hard_stop_loss_pct
    assert any("live_max_sol_per_trade" in r for r in rejected) and any("not-a-mint" in r for r in rejected)
    assert s.watchlist[0] not in new.watchlist and "max_position_pct" in applied


def test_concepts_bunch_similar_lessons_and_fire_synapses():
    from vibetrader.brain.concepts import ConceptGraph

    def l(i, text, bks, pnl):
        return {"id": i, "ts": f"t{i}", "pnl_pct": pnl, "lesson": text, "tags": [], "buckets": bks}

    rug = ["rug:danger", "liq:<25k"]
    g = ConceptGraph.build([
        l(1, "Rug danger flag: liquidity drained within minutes", rug, -60),
        l(2, "Danger rug score again; liquidity drained fast", rug, -45),
        l(3, "Buyer-led flow carried the pump through", ["flow:buyers", "h1:pump"], 12),
        l(4, "Strong buyer flow and a clean pump paid", ["flow:buyers", "h1:pump"], 9),
    ])
    a = g.assignments
    assert a[1]["concept"] == a[2]["concept"] and a[3]["concept"] == a[4]["concept"]
    assert a[1]["concept"] != a[3]["concept"]
    out = g.add(l(5, "Buyer flow pump but rug danger flag drained liquidity", ["flow:buyers", "h1:pump", *rug], -30))
    assert out["links"], "a lesson touching both clusters should fire a synapse"
    assert g.relevant(["rug:danger"])[0]["id"] == a[1]["concept"]


def test_playbook_selection_and_innate_concepts(tmp_path, monkeypatch):
    import json as _json

    from vibetrader.brain import playbook
    from vibetrader.brain.concepts import ConceptGraph

    rules = [
        {"id": "time-stop", "category": "exit", "rule": "Exit if the trade has not moved after 30 minutes",
         "confidence": "high", "applies_to": []},
        {"id": "moon-trim", "category": "exit", "rule": "Take partial profit into vertical moon candles",
         "confidence": "medium", "applies_to": ["h1:moon"]},
        {"id": "mint-auth", "category": "rug_detection", "rule": "Skip tokens whose mint authority is not revoked",
         "confidence": "high", "applies_to": ["rug:danger"]},
    ]
    f = tmp_path / "playbook.json"
    f.write_text(_json.dumps({"rules": rules}))
    monkeypatch.setattr(playbook, "PATH", f)
    playbook.load.cache_clear()
    try:
        manage = [r["id"] for r in playbook.select("manage", ["h1:moon"])]
        assert manage[0] == "moon-trim" and "mint-auth" not in manage  # bucket match first, wrong job excluded
        assert "moon-trim" not in [r["id"] for r in playbook.select("manage", ["h1:flat"])]
        assert playbook.summary()["count"] == 3

        g = ConceptGraph.build([{"id": 1, "ts": "t", "pnl_pct": -40, "lesson": "Mint authority was not revoked; rugged",
                                 "tags": ["rug"], "buckets": ["rug:danger"]}], playbook.load())
        priors = [c for c in g.concepts if c.prior]
        assert {c.category for c in priors} == {"exit", "rug_detection"}
        a = g.assignments[1]
        assert not g.concept(a["concept"]).prior, "lessons never join a playbook concept"
        assert any(g.concept(lk["concept"]).prior for lk in a["links"]), "but they fire synapses to it"
        assert all(not c["prior"] for c in g.relevant(["rug:danger"], min_members=1))
    finally:
        playbook.load.cache_clear()


def test_breakeven_and_time_stops():
    s = RuntimeSettings(breakeven_after_pct=10, breakeven_lock_pct=1, time_stop_min=45, time_stop_min_gain_pct=3,
                        trailing_stop_pct=0, max_hold_min=500)
    t = {"entry_price": 1.0, "stop_loss_pct": 15, "take_profit_pct": 100}
    assert risk.exit_reason(t, 1.005, 1.12, 5, s).startswith("roundtrip_guard")  # was +12%, back to +0.5%
    assert risk.exit_reason(t, 1.05, 1.12, 5, s) is None                         # still +5%: fine
    assert risk.exit_reason(t, 1.01, 1.02, 50, s).startswith("time_stop")         # 50 min, +1%: stale
    assert risk.exit_reason(t, 1.06, 1.06, 50, s) is None                         # 50 min, +6%: working
    assert risk.exit_reason(t, 1.01, 1.02, 30, s) is None                         # too early to judge


def test_profit_floor_and_mature_time_stop():
    s = RuntimeSettings(profit_floor_after_pct=50, profit_floor_keep=0.5, breakeven_after_pct=0, trailing_stop_pct=0,
                        time_stop_min=90, time_stop_min_gain_pct=10, time_stop_mature_hours=24, max_hold_min=5000)
    t = {"entry_price": 1.0, "stop_loss_pct": 20, "take_profit_pct": 500}
    assert risk.exit_reason(t, 1.39, 1.8, 30, s).startswith("profit_floor")  # peak +80%, floor +40%, now +39%
    assert risk.exit_reason(t, 1.45, 1.8, 30, s) is None
    young = {**t, "features": {"age_min": 120}}
    major = {**t, "features": {"age_min": 30 * 1440}}  # pair a month old: a JUP/BONK-type token
    assert risk.exit_reason(young, 1.02, 1.03, 100, s).startswith("time_stop")
    assert risk.exit_reason(major, 1.02, 1.03, 100, s) is None       # majors get 24h to work
    assert risk.exit_reason(major, 1.02, 1.03, 24 * 60 + 1, s).startswith("time_stop")


def test_scale_out_tranches():
    s = RuntimeSettings(scale_out_1_pct=50, scale_out_1_frac=0.4, scale_out_2_pct=150, scale_out_2_frac=0.3)
    t = {"entry_price": 1.0, "take_profit_pct": 300, "qty": 100, "orig_qty": 100, "tranches_done": 0}
    assert risk.scale_out_due(t, 1.3, s) is None
    assert risk.scale_out_due(t, 1.55, s) == (40, 1, 50)                 # sell 40% of the original at +50%
    t2 = {**t, "qty": 60, "tranches_done": 1}
    assert risk.scale_out_due(t2, 1.6, s) is None                        # waits for the second level
    assert risk.scale_out_due(t2, 2.6, s) == (30, 2, 150)
    assert risk.scale_out_due({**t, "qty": 30, "tranches_done": 2}, 3.5, s) is None  # the runner is left to trail


def test_liquidity_drain_and_chase_filters():
    s = RuntimeSettings(liquidity_drain_exit_pct=30, min_liquidity_usd=1000)
    t = {"features": {"liquidity_usd": 100_000, "price_usd": 1.0}}
    assert risk.liquidity_drained(t, 0.81, 90_000, s) is None           # price down 19%: liquidity ~90k is normal
    assert risk.liquidity_drained(t, 0.81, 40_000, s).startswith("liquidity_drain")
    f = build_features(PAIR, {"score": 5})
    assert "vertical" in risk.filter_reason({**f, "change_h1_pct": 250}, s)
    assert "falling knife" in risk.filter_reason({**f, "change_h1_pct": -45}, s)


def test_partial_sell_then_close_counts_all_proceeds(tmp_path):
    from vibetrader.config import Env
    from vibetrader.trading.engine import Engine

    async def go():
        e = Engine(Env(db_path=str(tmp_path / "e.db"), groq_api_key=""))
        e.db.set_setting("paper_cash", 0.0)
        s = e.settings()
        tid = e.db.insert("trades", {
            "mode": "paper", "mint": "M", "symbol": "X", "status": "open", "opened_at": now_iso(), "entry_price": 1.0,
            "qty": 100, "orig_qty": 100, "cost_usd": 100, "stop_loss_pct": 20, "take_profit_pct": 300,
            "peak_price": 1.6, "buckets": [],
        })
        t = e.db.one("SELECT * FROM trades WHERE id = ?", (tid,))
        await e._scale_out(t, 1.6, 1e9, 40, "take_profit_1", 1, s)
        mid = e.db.one("SELECT * FROM trades WHERE id = ?", (tid,))
        await e._close(mid, 1.6, 1e9, "trailing_stop")
        done = e.db.one("SELECT * FROM trades WHERE id = ?", (tid,))
        await e.aclose()
        return mid, done

    mid, done = run(go())
    assert mid["qty"] == pytest.approx(60) and mid["tranches_done"] == 1 and mid["stop_loss_pct"] < 0
    assert done["status"] == "closed" and 55 < done["pnl_usd"] < 60  # +60% on the whole position, minus costs


def test_close_never_sells_twice(tmp_path):
    from vibetrader.config import Env
    from vibetrader.trading.engine import Engine

    async def go():
        e = Engine(Env(db_path=str(tmp_path / "e.db"), groq_api_key=""))
        e.db.set_setting("paper_starting_credit", 1000)
        e.db.set_setting("paper_cash", 900.0)
        tid = e.db.insert("trades", {
            "mode": "paper", "mint": "M", "symbol": "X", "status": "open", "opened_at": now_iso(), "entry_price": 1.0,
            "qty": 100, "cost_usd": 100, "stop_loss_pct": 10, "take_profit_pct": 50, "peak_price": 1.0, "buckets": [],
        })
        t = e.db.one("SELECT * FROM trades WHERE id = ?", (tid,))
        await asyncio.gather(e._close(dict(t), 1.0, 1e6, "watcher"), e._close(dict(t), 1.0, 1e6, "ai_exit"))
        await e._close(dict(t), 1.0, 1e6, "late")
        cash = await e.paper.cash_usd()
        await e.aclose()
        return cash

    cash = run(go())
    assert 990 < cash < 1000  # credited exactly once (minus fees), not twice


def test_hard_backstops_and_profit_lock():
    s = RuntimeSettings(hard_stop_loss_pct=40, hard_max_hold_hours=1, max_hold_min=500, trailing_stop_pct=0)
    t = {"entry_price": 1.0, "stop_loss_pct": 80, "take_profit_pct": 500}  # an AI stop wider than the backstop
    assert risk.exit_reason(t, 0.55, 1.0, 5, s).startswith("hard_stop")
    assert risk.exit_reason(t, 1.0, 1.0, 61, s).startswith("hard_max_hold")
    locked = {"entry_price": 1.0, "stop_loss_pct": -8, "take_profit_pct": 500}  # stop raised into profit
    assert risk.exit_reason(locked, 1.07, 1.3, 5, s).startswith("profit_lock")
    assert risk.exit_reason(locked, 1.2, 1.3, 5, s) is None


def test_env_file_beats_process_env(tmp_path, monkeypatch):
    from vibetrader.config import Env

    (tmp_path / ".env").write_text("GROQ_API_KEY=from_dotenv\n")
    monkeypatch.setenv("GROQ_API_KEY", "stale_global")
    monkeypatch.chdir(tmp_path)
    assert Env().groq_api_key == "from_dotenv"


# ---------------------------------------------------------------- engine events
def test_emit_accepts_kind_field(tmp_path):
    from vibetrader.config import Env
    from vibetrader.trading.engine import Engine

    async def go():
        e = Engine(Env(db_path=str(tmp_path / "e.db"), groq_api_key=""))
        e.emit("thought", kind="entry", symbol="X")  # "kind" used to collide with emit's own parameter
        await e.aclose()
        return e.history[-1]

    evt = run(go())
    assert evt["type"] == "thought" and evt["kind"] == "entry"


# ---------------------------------------------------------------- memory
def _closed_trade(db, pnl_pct, bks):
    tid = db.insert("trades", {
        "mode": "paper", "mint": "M", "symbol": "BONK", "status": "closed", "opened_at": now_iso(), "closed_at": now_iso(),
        "entry_price": 1, "qty": 1, "cost_usd": 100, "stop_loss_pct": 10, "take_profit_pct": 20, "peak_price": 1,
        "pnl_pct": pnl_pct, "pnl_usd": pnl_pct, "exit_reason": "test", "buckets": bks,
    })
    return db.one("SELECT * FROM trades WHERE id = ?", (tid,))


def test_memory_learns(db):
    t1 = _closed_trade(db, 25, ["liq:<25k", "h1:moon"])
    t2 = _closed_trade(db, -40, ["liq:<25k", "flow:sellers"])
    memory.record_outcome(db, t1, {"lesson": "moons pay", "tags": ["win"]}, "test")
    memory.record_outcome(db, t2, {"lesson": "sellers dump", "tags": ["loss"]}, "test")

    stats = {s["bucket"]: s for s in memory.bucket_stats(db)}
    assert stats["liq:<25k"]["n"] == 2 and stats["liq:<25k"]["wins"] == 1
    assert stats["liq:<25k"]["avg_pnl_pct"] == pytest.approx(-7.5)

    rel = memory.relevant_lessons(db, ["flow:sellers"], k=1)
    assert rel[0]["lesson"] == "sellers dump"
    assert db.one("SELECT cited FROM lessons WHERE id = ?", (rel[0]["id"],))["cited"] == 1

    o = memory.overall_stats(db, "paper")
    assert o["trades"] == 2 and o["win_rate"] == 0.5 and o["expectancy_pct"] == pytest.approx(-7.5)
