"""RugCheck summary report (no key). score_normalised: 0 (clean) .. 100 (scary)."""
from __future__ import annotations

from typing import Any

import httpx

BASE = "https://api.rugcheck.xyz/v1"


class RugCheck:
    def __init__(self, client: httpx.AsyncClient):
        self.http = client
        self._cache: dict[str, dict[str, Any]] = {}

    async def summary(self, mint: str) -> dict[str, Any] | None:
        if mint in self._cache:
            return self._cache[mint]
        try:
            r = await self.http.get(f"{BASE}/tokens/{mint}/report/summary", timeout=15)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        out = {
            "score": data.get("score_normalised"),
            "lp_locked_pct": data.get("lpLockedPct"),
            "risks": [
                {"name": x.get("name"), "level": x.get("level")} for x in (data.get("risks") or [])
            ],
        }
        self._cache[mint] = out
        return out
