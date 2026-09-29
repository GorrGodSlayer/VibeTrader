"""Groq wrapper: per-job models, a pacer that respects every free-tier limit, tolerant JSON parsing, and
pydantic schemas for everything the brain returns.

Groq's free tier limits each model separately (30 requests/min, 1,000 requests/day, 8,000 tokens/min,
200,000 tokens/day). Tokens/day is the tight one, so each job runs on its own model and a token bucket per model
spreads the daily budget across the day instead of burning it in the first hour.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("vibetrader.groq")

ROLES = ("decide", "screen", "manage", "reflect")
MAX_TOKENS = {"decide": 2000, "screen": 1200, "manage": 1600, "reflect": 2400}
EXPECTED_OUT = {"decide": 700, "screen": 250, "manage": 500, "reflect": 900}  # for pre-call budgeting
MANDATORY_WAIT_S = 300  # longest a non-optional call (post-mortem) will wait for budget


class BudgetSkip(RuntimeError):
    """Raised instead of calling Groq when the call is optional and the model's budget can't afford it now."""


# ------------------------------------------------------------------ response schemas
class Decision(BaseModel):
    action: Literal["buy", "skip"]
    confidence: float = Field(0.0, ge=0, le=1)
    size_pct: float = Field(0.0, ge=0, le=100)
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    reasoning: str = ""
    hype_score: int = Field(5, ge=0, le=10)
    explore: bool = False

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return "buy" if v in ("buy", "long", "ape", "enter") else "skip"

    @field_validator("explore", mode="before")
    @classmethod
    def _explore(cls, v: Any) -> bool:
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes")

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> float:
        v = float(v or 0)
        if 1 < v <= 100:  # model answered in percent
            v /= 100
        return max(0.0, min(1.0, v))

    @field_validator("size_pct", mode="before")
    @classmethod
    def _size(cls, v: Any) -> float:
        return max(0.0, min(100.0, float(v or 0)))

    @field_validator("stop_loss_pct", "take_profit_pct", mode="before")
    @classmethod
    def _pct(cls, v: Any) -> float | None:
        if v in (None, ""):
            return None
        return abs(float(v))

    @field_validator("hype_score", mode="before")
    @classmethod
    def _hype(cls, v: Any) -> int:
        try:
            return max(0, min(10, round(float(v))))
        except (TypeError, ValueError):
            return 5


def _action(v: Any, sell_words: tuple[str, ...] = ("sell", "exit", "bail", "close")) -> str:
    return "sell" if str(v or "").strip().lower() in sell_words else "hold"


class BailCheck(BaseModel):
    action: Literal["hold", "sell"]
    reasoning: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _a(cls, v: Any) -> str:
        return _action(v)


class PositionCall(BaseModel):
    id: int
    action: Literal["hold", "trim", "sell"] = "hold"
    sell_fraction: float = Field(0.5, ge=0.1, le=0.9)
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    reasoning: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _a(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return "trim" if v in ("trim", "scale", "scale_out", "partial", "reduce") else _action(v)

    @field_validator("sell_fraction", mode="before")
    @classmethod
    def _frac(cls, v: Any) -> float:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.5
        v = v / 100 if v > 1 else v
        return max(0.1, min(0.9, v))

    @field_validator("stop_loss_pct", mode="before")
    @classmethod
    def _stop(cls, v: Any) -> float | None:
        # signed on purpose: a negative stop locks in profit
        return None if v in (None, "") else float(v)

    @field_validator("take_profit_pct", mode="before")
    @classmethod
    def _tp(cls, v: Any) -> float | None:
        return None if v in (None, "") else abs(float(v))


class PositionReview(BaseModel):
    positions: list[PositionCall] = Field(default_factory=list)


class ScreenPick(BaseModel):
    symbol: str
    why: str = ""


class Screen(BaseModel):
    picks: list[ScreenPick] = Field(default_factory=list)
    reasoning: str = ""


class StrategyReview(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    pause_minutes: float = Field(0, ge=0)
    watchlist_add: list[str] = Field(default_factory=list)
    watchlist_remove: list[str] = Field(default_factory=list)
    reasoning: str = ""

    @field_validator("pause_minutes", mode="before")
    @classmethod
    def _pause(cls, v: Any) -> float:
        try:
            return max(0.0, min(720.0, float(v or 0)))
        except (TypeError, ValueError):
            return 0.0


class PostMortem(BaseModel):
    lesson: str
    what_worked: str = ""
    what_failed: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = [t.strip() for t in v.split(",")]
        return [str(t)[:40] for t in (v or []) if str(t).strip()][:8]


def _unescape(v: Any) -> Any:
    # some models HTML-escape their text (e.g. "&lt;10%"); the UI escapes on render, so store plain text
    if isinstance(v, str):
        return html.unescape(v)
    if isinstance(v, list):
        return [_unescape(x) for x in v]
    if isinstance(v, dict):
        return {k: _unescape(x) for k, x in v.items()}
    return v


def parse_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of whatever the model said."""
    return _unescape(_parse_json(text))


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    raise ValueError(f"No JSON object in model output: {text[:200]!r}")


# ------------------------------------------------------------------ pacing
class Bucket:
    """Token bucket: holds up to `capacity`, refills at `rate` per second."""

    def __init__(self, capacity: float, rate: float):
        self.capacity = capacity
        self.rate = rate
        self.level = capacity
        self.t = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.level = min(self.capacity, self.level + (now - self.t) * self.rate)
        self.t = now

    def wait_for(self, amount: float) -> float:
        self._refill()
        need = min(amount, self.capacity) - self.level
        return 0.0 if need <= 0 else need / self.rate

    def take(self, amount: float) -> None:
        self._refill()
        self.level -= amount

    def refund(self, amount: float) -> None:
        self._refill()
        self.level = min(self.capacity, self.level + amount)


class ModelLimiter:
    """All four free-tier limits for one model. The per-day budget is paced: it refills at tpd/86400 per second
    and can only bank ~15% of a day, so the model never spends its whole day in one burst."""

    def __init__(self, model: str, rpm: int, tpm: int, tpd: int):
        self.model = model
        self.tpd = tpd
        self.rpm = Bucket(rpm, rpm / 60)
        self.tpm = Bucket(tpm, tpm / 60)
        self.day = Bucket(max(tpd * 0.15, 4000), tpd / 86400)
        self.cooldown_until = 0.0
        self.usage_day = ""
        self.requests_today = 0
        self.tokens_today = 0
        self.skipped_today = 0
        self.lock = asyncio.Lock()

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.usage_day:
            self.usage_day, self.requests_today, self.tokens_today, self.skipped_today = today, 0, 0, 0

    def can_afford(self, est: float) -> bool:
        return time.monotonic() >= self.cooldown_until and self.day.wait_for(est) == 0

    async def acquire(self, est: float, optional: bool) -> None:
        async with self.lock:
            self._roll_day()
            if optional and not self.can_afford(est):
                self.skipped_today += 1
                raise BudgetSkip(f"{self.model}: daily token budget paced out; skipping optional call")
            deadline = time.monotonic() + MANDATORY_WAIT_S
            while True:
                wait = max(self.cooldown_until - time.monotonic(), self.rpm.wait_for(1), self.tpm.wait_for(est),
                           0 if optional else self.day.wait_for(est))
                if wait <= 0:
                    break
                if optional and wait > 20:
                    self.skipped_today += 1
                    raise BudgetSkip(f"{self.model}: rate limited for {wait:.0f}s; skipping optional call")
                if time.monotonic() + wait > deadline:
                    raise BudgetSkip(f"{self.model}: no budget within {MANDATORY_WAIT_S}s")
                await asyncio.sleep(min(wait, 30))
            self.rpm.take(1)
            self.tpm.take(est)
            self.day.take(est)

    def settle(self, est: float, actual: int) -> None:
        self._roll_day()
        self.tpm.refund(est - actual)
        self.day.refund(est - actual)
        self.requests_today += 1
        self.tokens_today += actual

    def rate_limited(self, retry_after: float, daily: bool) -> None:
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + max(retry_after, 1))
        if daily:
            self.day.level = min(self.day.level, 0)

    def status(self) -> dict[str, Any]:
        self._roll_day()
        self.day._refill()
        return {
            "model": self.model,
            "tokens_today": self.tokens_today,
            "requests_today": self.requests_today,
            "skipped_today": self.skipped_today,
            "tokens_per_day": self.tpd,
            "budget_pct": round(max(0.0, self.day.level) / self.day.capacity * 100, 1),
            "cooldown_s": round(max(0.0, self.cooldown_until - time.monotonic()), 1),
        }


# ------------------------------------------------------------------ client
class Groq:
    def __init__(self, api_key: str, models: dict[str, str] | str, rpm: int = 30, tpm: int = 8000, tpd: int = 200_000):
        if isinstance(models, str):
            models = {r: models for r in ROLES}
        self.models = {r: models.get(r) or models.get("decide") for r in ROLES}
        self.client = None
        self.last_error: str | None = None
        self.limiters: dict[str, ModelLimiter] = {}
        for m in set(self.models.values()):
            self.limiters[m] = ModelLimiter(m, rpm, tpm, tpd)
        if api_key:
            from groq import AsyncGroq

            self.client = AsyncGroq(api_key=api_key)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @property
    def model(self) -> str:
        return self.models["decide"]

    def limiter(self, role: str) -> ModelLimiter:
        return self.limiters[self.models[role]]

    @staticmethod
    def estimate(system: str, user: str, role: str) -> float:
        return (len(system) + len(user)) / 3.6 + EXPECTED_OUT[role]

    def can_afford(self, role: str, system: str = "", user: str = "", est: float | None = None) -> bool:
        return self.enabled and self.limiter(role).can_afford(est if est is not None else self.estimate(system, user, role))

    def _model_kwargs(self, model: str, role: str) -> dict[str, Any]:
        if "gpt-oss" in model:
            return {"reasoning_effort": "medium" if role in ("decide", "reflect") else "low"}
        if "qwen" in model:
            return {"reasoning_format": "hidden", **({"reasoning_effort": "none"} if role in ("screen", "manage") else {})}
        return {}

    async def _chat(self, system: str, user: str, *, role: str, json_mode: bool, temperature: float, optional: bool) -> str:
        assert self.client is not None
        model = self.models[role]
        lim = self.limiters[model]
        est = self.estimate(system, user, role)
        last: Exception | None = None
        strict_json = json_mode
        for attempt in range(3):
            await lim.acquire(est, optional)
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "temperature": temperature,
                    # reasoning models spend part of this budget thinking before they write the answer
                    "max_tokens": MAX_TOKENS[role],
                    **self._model_kwargs(model, role),
                }
                if strict_json:
                    kwargs["response_format"] = {"type": "json_object"}
                raw = await self.client.chat.completions.with_raw_response.create(**kwargs)
                resp = await raw.parse()
                used = getattr(resp.usage, "total_tokens", None) or int(est)
                lim.settle(est, used)
                self.last_error = None
                return resp.choices[0].message.content or ""
            except BudgetSkip:
                raise
            except Exception as e:  # rate limits, 5xx, json_validate_failed...
                lim.settle(est, 0)
                status = getattr(e, "status_code", None)
                if status in (401, 403, 404):  # bad key / unknown model: retrying won't help
                    self.last_error = f"Groq rejected {model} ({status}): check GROQ_API_KEY / model ids"
                    raise RuntimeError(self.last_error) from e
                if status == 429:
                    headers = getattr(getattr(e, "response", None), "headers", {}) or {}
                    try:
                        retry = float(headers.get("retry-after") or 30)
                    except ValueError:
                        retry = 30.0
                    lim.rate_limited(retry, daily="per day" in str(e).lower())
                    if optional:
                        raise BudgetSkip(f"{model}: 429 rate limited ({retry:.0f}s)") from e
                if "json_validate_failed" in str(e):
                    strict_json = False  # retry without Groq-side validation; parse_json copes with loose output
                last = e
                log.warning("groq %s call failed (attempt %d): %s", role, attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        self.last_error = f"Groq {role} failed after retries: {last}"
        raise RuntimeError(self.last_error)

    async def json(self, system: str, user: str, temperature: float = 0.4, *, role: str = "decide",
                   optional: bool = False) -> dict[str, Any]:
        return parse_json(await self._chat(system, user, role=role, json_mode=True, temperature=temperature,
                                           optional=optional))

    async def text(self, system: str, user: str, temperature: float = 0.7, *, role: str = "reflect",
                   optional: bool = False) -> str:
        return await self._chat(system, user, role=role, json_mode=False, temperature=temperature, optional=optional)

    def status(self) -> dict[str, Any]:
        return {"roles": dict(self.models), "models": {m: lim.status() for m, lim in self.limiters.items()}}
