"""DexScreener public API (no key). Docs: https://docs.dexscreener.com/api/reference"""
from __future__ import annotations

from typing import Any

import httpx

BASE = "https://api.dexscreener.com"
CHAIN = "solana"


class DexScreener:
    def __init__(self, client: httpx.AsyncClient):
        self.http = client

    async def _get(self, path: str) -> Any:
        r = await self.http.get(f"{BASE}{path}", timeout=15)
        r.raise_for_status()
        return r.json()

    async def trending_mints(self, limit: int = 40) -> list[str]:
        """Boosted + freshly profiled Solana tokens, deduped, boosted first."""
        mints: list[str] = []
        for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
            try:
                data = await self._get(path)
            except httpx.HTTPError:
                continue
            for item in data if isinstance(data, list) else []:
                if item.get("chainId") == CHAIN and item.get("tokenAddress") not in mints:
                    mints.append(item["tokenAddress"])
        return mints[:limit]

    async def best_pairs(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """mint -> most liquid pair where the mint is the base token."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(mints), 30):  # API accepts up to 30 addresses per call
            chunk = mints[i : i + 30]
            try:
                pairs = await self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}")
            except httpx.HTTPError:
                continue
            for p in pairs if isinstance(pairs, list) else []:
                mint = (p.get("baseToken") or {}).get("address")
                if mint not in chunk or not p.get("priceUsd"):
                    continue
                liq = (p.get("liquidity") or {}).get("usd") or 0
                prev = out.get(mint)
                if prev is None or liq > ((prev.get("liquidity") or {}).get("usd") or 0):
                    out[mint] = p
        return out
