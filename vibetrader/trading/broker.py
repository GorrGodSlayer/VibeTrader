"""Broker interface shared by paper + live execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Fill:
    qty: float          # token units (UI amount)
    price: float        # effective USD price per token, after slippage/fees
    usd: float          # USD spent (buy) or received (sell), net of fees
    fee_usd: float = 0.0
    tx: str | None = None
    raw_amount: str | None = None
    decimals: int | None = None


class Broker(Protocol):
    mode: str

    async def cash_usd(self) -> float: ...

    async def buy(self, mint: str, usd: float, price: float, liquidity_usd: float) -> Fill: ...

    async def sell(self, trade: dict[str, Any], price: float, liquidity_usd: float, qty: float | None = None) -> Fill: ...
