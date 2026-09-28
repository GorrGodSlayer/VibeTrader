"""Groq wrapper + tolerant JSON parsing + pydantic schemas for everything the brain returns."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("vibetrader.groq")


class Decision(BaseModel):
    action: Literal["buy", "skip"]
    confidence: float = Field(0.0, ge=0, le=1)
    size_pct: float = Field(0.0, ge=0, le=100)
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    reasoning: str = ""
    meme_vibe_score: int = Field(5, ge=0, le=10)

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return "buy" if v in ("buy", "long", "ape", "enter") else "skip"

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

    @field_validator("meme_vibe_score", mode="before")
    @classmethod
    def _vibe(cls, v: Any) -> int:
        try:
            return max(0, min(10, round(float(v))))
        except (TypeError, ValueError):
            return 5


class BailCheck(BaseModel):
    action: Literal["hold", "sell"]
    reasoning: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v: Any) -> str:
        return "sell" if str(v or "").strip().lower() in ("sell", "exit", "bail", "close") else "hold"


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


def parse_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of whatever the model said."""
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


class Groq:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = None
        self.last_error: str | None = None
        if api_key:
            from groq import AsyncGroq

            self.client = AsyncGroq(api_key=api_key)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def _chat(self, system: str, user: str, *, json_mode: bool, temperature: float) -> str:
        assert self.client is not None
        last: Exception | None = None
        for attempt in range(3):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "temperature": temperature,
                    "max_tokens": 900,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await self.client.chat.completions.create(**kwargs)
                self.last_error = None
                return resp.choices[0].message.content or ""
            except Exception as e:  # rate limits, 5xx, json_validate_failed...
                if getattr(e, "status_code", None) in (401, 403, 404):  # bad key / unknown model: retrying won't help
                    self.last_error = f"Groq rejected the request ({e.status_code}): check GROQ_API_KEY / GROQ_MODEL"
                    raise RuntimeError(self.last_error) from e
                last = e
                log.warning("groq call failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        self.last_error = f"Groq failed after retries: {last}"
        raise RuntimeError(self.last_error)

    async def json(self, system: str, user: str, temperature: float = 0.4) -> dict[str, Any]:
        return parse_json(await self._chat(system, user, json_mode=True, temperature=temperature))

    async def text(self, system: str, user: str, temperature: float = 0.7) -> str:
        return await self._chat(system, user, json_mode=False, temperature=temperature)
