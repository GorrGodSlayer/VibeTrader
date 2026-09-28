"""Jupiter price + swap API (v1 swap, v3 price)."""
from __future__ import annotations

from typing import Any

import httpx

SOL_MINT = "So11111111111111111111111111111111111111112"


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, base: str, api_key: str = ""):
        self.http = client
        self.base = base.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else {}

    async def _price_info(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(mints), 50):
            r = await self.http.get(
                f"{self.base}/price/v3", params={"ids": ",".join(mints[i : i + 50])}, headers=self.headers, timeout=15
            )
            r.raise_for_status()
            out.update({k: v for k, v in (r.json() or {}).items() if v})
        return out

    async def prices(self, mints: list[str]) -> dict[str, float]:
        info = await self._price_info(mints)
        return {m: float(i["usdPrice"]) for m, i in info.items() if i.get("usdPrice")}

    async def sol_price(self) -> float:
        p = (await self.prices([SOL_MINT])).get(SOL_MINT)
        if not p:
            raise RuntimeError("Jupiter returned no SOL price")
        return p

    async def token_decimals(self, mint: str) -> int:
        info = (await self._price_info([mint])).get(mint) or {}
        if "decimals" not in info:
            raise RuntimeError(f"Unknown decimals for {mint}")
        return int(info["decimals"])

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict[str, Any]:
        r = await self.http.get(
            f"{self.base}/swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": slippage_bps,
            },
            headers=self.headers,
            timeout=20,
        )
        r.raise_for_status()
        q = r.json()
        if "outAmount" not in q:
            raise RuntimeError(f"Bad Jupiter quote: {q}")
        return q

    async def swap_tx(self, quote: dict[str, Any], user_pubkey: str) -> str:
        """Returns a base64 unsigned VersionedTransaction."""
        r = await self.http.post(
            f"{self.base}/swap/v1/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            },
            headers=self.headers,
            timeout=30,
        )
        r.raise_for_status()
        tx = r.json().get("swapTransaction")
        if not tx:
            raise RuntimeError(f"Jupiter swap returned no transaction: {r.text[:300]}")
        return tx
