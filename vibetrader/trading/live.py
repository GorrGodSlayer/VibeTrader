"""Live broker: Jupiter quote -> swap tx -> sign locally -> send (or simulate) via Solana RPC.

The private key never leaves this process: it is not logged, not returned by any API, not sent anywhere
except as a signature on transactions you asked for.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Callable

import httpx

from vibetrader.market.jupiter import SOL_MINT, Jupiter
from vibetrader.trading.broker import Fill

log = logging.getLogger("vibetrader.live")

FEE_RESERVE_SOL = 0.02  # never spend the SOL needed for tx fees / rent


class LiveBroker:
    mode = "live"

    def __init__(
        self,
        http: httpx.AsyncClient,
        jup: Jupiter,
        rpc_url: str,
        private_key: str,
        dry_run: bool,
        slippage_bps: Callable[[], int],
    ):
        from solders.keypair import Keypair  # optional dependency (requirements-live.txt)

        self.http = http
        self.jup = jup
        self.rpc_url = rpc_url
        self.dry_run = dry_run
        self.slippage_bps = slippage_bps  # callable so settings changes apply immediately
        self._kp = Keypair.from_base58_string(private_key.strip())
        self.pubkey = str(self._kp.pubkey())

    # ---------- rpc ----------
    async def _rpc(self, method: str, params: list[Any]) -> Any:
        r = await self.http.post(
            self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30
        )
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"RPC {method} error: {body['error']}")
        return body["result"]

    async def sol_balance(self) -> float:
        res = await self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])
        return res["value"] / 1e9

    async def token_balance_raw(self, mint: str) -> int:
        res = await self._rpc(
            "getTokenAccountsByOwner",
            [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])

    async def cash_usd(self) -> float:
        sol = max(await self.sol_balance() - FEE_RESERVE_SOL, 0)
        return sol * await self.jup.sol_price()

    async def _sign_and_submit(self, tx_b64: str) -> str:
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [self._kp])
        encoded = base64.b64encode(bytes(signed)).decode()

        if self.dry_run:
            res = await self._rpc(
                "simulateTransaction",
                [encoded, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed"}],
            )
            value = res.get("value") or {}
            if value.get("err"):
                raise RuntimeError(f"simulation failed: {value['err']} {(value.get('logs') or [])[-3:]}")
            return "DRYRUN"

        sig = await self._rpc("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        for _ in range(45):  # ~90s
            await asyncio.sleep(2)
            st = (await self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]))["value"][0]
            if st:
                if st.get("err"):
                    raise RuntimeError(f"tx {sig} failed: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig
        raise RuntimeError(f"tx {sig} not confirmed in time (check an explorer before retrying!)")

    # ---------- orders ----------
    async def buy(self, mint: str, usd: float, price: float, liquidity_usd: float) -> Fill:
        sol_px = await self.jup.sol_price()
        lamports = int(usd / sol_px * 1e9)
        before = 0
        if not self.dry_run:
            available = await self.sol_balance() - FEE_RESERVE_SOL
            if lamports / 1e9 > available:
                raise RuntimeError(f"not enough SOL: need {lamports / 1e9:.4f}, have {available:.4f}")
            before = await self.token_balance_raw(mint)
        quote = await self.jup.quote(SOL_MINT, mint, lamports, self.slippage_bps())
        tx = await self.jup.swap_tx(quote, self.pubkey)
        sig = await self._sign_and_submit(tx)
        decimals = await self.jup.token_decimals(mint)
        raw_out = int(quote["outAmount"])
        if not self.dry_run:  # measure what actually landed
            delta = await self.token_balance_raw(mint) - before
            raw_out = delta if delta > 0 else raw_out
        qty = raw_out / 10**decimals
        spent = lamports / 1e9 * sol_px
        return Fill(qty=qty, price=spent / qty if qty else price, usd=spent, tx=sig, raw_amount=str(raw_out), decimals=decimals)

    async def sell(self, trade: dict[str, Any], price: float, liquidity_usd: float, qty: float | None = None) -> Fill:
        """Sell `qty` tokens (default: the whole position)."""
        raw = int(trade.get("raw_amount") or 0)
        if qty is not None and trade.get("qty"):
            raw = int(raw * min(1.0, qty / float(trade["qty"])))
        decimals = trade.get("decimals")
        if decimals is None:
            decimals = await self.jup.token_decimals(trade["mint"])
        if not self.dry_run:
            held = await self.token_balance_raw(trade["mint"])
            raw = min(raw, held) if raw or qty is not None else held
        if raw <= 0:
            raise RuntimeError(f"no {trade['symbol']} balance to sell")
        quote = await self.jup.quote(trade["mint"], SOL_MINT, raw, self.slippage_bps())
        tx = await self.jup.swap_tx(quote, self.pubkey)
        sig = await self._sign_and_submit(tx)
        sol_px = await self.jup.sol_price()
        proceeds = int(quote["outAmount"]) / 1e9 * sol_px
        qty = raw / 10**decimals
        return Fill(qty=qty, price=proceeds / qty if qty else 0, usd=proceeds, tx=sig, raw_amount=str(raw), decimals=decimals)
