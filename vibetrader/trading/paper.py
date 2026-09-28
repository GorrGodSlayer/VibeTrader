"""Paper broker: fills at live prices with a pessimistic slippage + fee model."""
from __future__ import annotations

from typing import Any, Callable

from vibetrader.config import RuntimeSettings
from vibetrader.db import DB
from vibetrader.trading.broker import Fill


def slippage_pct(usd: float, liquidity_usd: float, base_pct: float) -> float:
    """Base slippage + constant-product-ish price impact against one side of the pool."""
    pool_side = max(liquidity_usd / 2, 1.0)
    return base_pct + usd / pool_side * 100


class PaperBroker:
    mode = "paper"

    def __init__(self, db: DB, settings: Callable[[], RuntimeSettings]):
        self.db = db
        self.settings = settings

    async def cash_usd(self) -> float:
        return float(self.db.get_setting("paper_cash", 0.0))

    def _set_cash(self, v: float) -> None:
        self.db.set_setting("paper_cash", round(v, 8))

    async def buy(self, mint: str, usd: float, price: float, liquidity_usd: float) -> Fill:
        s = self.settings()
        cash = await self.cash_usd()
        if usd <= 0 or price <= 0:
            raise ValueError("paper buy needs positive size and price")
        if usd > cash + 1e-9:
            raise ValueError(f"insufficient paper cash: {cash:.2f} < {usd:.2f}")
        fee = usd * s.paper_fee_pct / 100 + s.paper_network_fee_usd
        fill_price = price * (1 + slippage_pct(usd, liquidity_usd, s.paper_base_slippage_pct) / 100)
        qty = max(usd - fee, 0) / fill_price
        self._set_cash(cash - usd)
        return Fill(qty=qty, price=usd / qty if qty else fill_price, usd=usd, fee_usd=fee, tx="PAPER")

    async def sell(self, trade: dict[str, Any], price: float, liquidity_usd: float) -> Fill:
        s = self.settings()
        qty = float(trade["qty"])
        gross = qty * price
        fill_price = price * (1 - slippage_pct(gross, liquidity_usd, s.paper_base_slippage_pct) / 100)
        proceeds = qty * max(fill_price, 0)
        fee = proceeds * s.paper_fee_pct / 100 + s.paper_network_fee_usd
        net = max(proceeds - fee, 0.0)
        self._set_cash(await self.cash_usd() + net)
        return Fill(qty=qty, price=net / qty if qty else 0, usd=net, fee_usd=fee, tx="PAPER")
