from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from typing import Dict, Optional

from bot.infra import Signal, Position, get_client, get_private_ws
from bot.helpers import config


class RiskRouter:
    """Route *Signal*s into risk‑adjusted Bybit MARKET brackets and keep
    our local *book* in‑sync with fills/cancels coming from the private WS.
    """

    # ────────────────────────────── life‑cycle ──────────────────────────────
    def __init__(self, *, equity_usd: float = 200.0, testnet: bool = True):
        self.equity = equity_usd
        self.http = get_client(testnet=testnet)            # REST (blocking)
        self.loop = asyncio.get_event_loop()

        # private stream (runs a dedicated background thread internally)
        self.private_ws = get_private_ws(testnet=testnet)
        self.private_ws.order_stream(callback=self._on_order)  # subscribe

        # pair‑specific cache (qtyStep & leverage already set?)
        self._qty_step: Dict[str, float] = defaultdict(lambda: 0.001)
        self._leveraged: set[str] = set()

        # working state and synchronisation primitives
        self.book: dict[str, Position] = {}
        self._oid_to_key: Dict[str, str] = {}
        self.lock = asyncio.Lock()
        self._ws_q: asyncio.Queue = asyncio.Queue()

        # consumer task (won't block __init__)
        asyncio.create_task(self._track_fills())

    # ───────────────────────────── public API ───────────────────────────────
    async def handle(self, sig: Signal) -> None:
        """Main entry – called by the strategy whenever a new *Signal* fires."""
        async with self.lock:
            # duplicate protection
            if sig.key in self.book:
                logging.info("%s already active – ignoring dup", sig.key)
                return

            # lazy instrument warm‑up
            if sig.symbol not in self._qty_step:
                await self._warm_meta(sig.symbol)

            qty = self._calc_qty(sig)
            order_id = await self._place_bracket(sig, qty)

            self.book[sig.key] = Position(sig, order_id, qty)
            self._oid_to_key[order_id] = sig.key
            logging.info("[%s] order placed  id=%s  qty=%f", sig.symbol, order_id, qty)

    # ───────────────────────────── internals ────────────────────────────────
    async def _warm_meta(self, symbol: str) -> None:
        """Fetch lot‑size step & set leverage (once per symbol)."""
        info = (
            await asyncio.to_thread(
                self.http.get_instruments_info,
                category="linear",
                symbol=symbol,
            )
        )["result"]["list"][0]

        self._qty_step[symbol] = float(info["lotSizeFilter"]["qtyStep"])

        if symbol not in self._leveraged:
            await asyncio.to_thread(
                self.http.set_leverage,
                category="linear",
                symbol=symbol,
                buyLeverage=config.LEV,
                sellLeverage=config.LEV,
            )
            self._leveraged.add(symbol)

    # ── sizing ----------------------------------------------------------------
    def _calc_qty(self, sig: Signal) -> float:
        risk_usd = self.equity * config.RISK_PCT
        stop_dist = max(abs(sig.entry - sig.sl), 1e-8)      # protect div/0
        raw_qty = risk_usd / stop_dist

        step = self._qty_step[sig.symbol]
        qty = math.floor(raw_qty / step) * step             # round *down*
        return round(qty, 8)                                # cosmetic only

    # ── order placement -------------------------------------------------------
    async def _place_bracket(self, s: Signal, qty: float) -> str:
        """Market‑entry then attach TP/SL via *set_trading_stop*."""
        resp = await asyncio.to_thread(
            self.http.place_order,
            category="linear",
            symbol=s.symbol,
            side=s.side,
            orderType="Market",
            qty=f"{qty}",
            reduceOnly=False,
            closeOnTrigger=False,
        )
        oid: str = resp["result"]["orderId"]

        await asyncio.to_thread(
            self.http.set_trading_stop,
            category="linear",
            symbol=s.symbol,
            takeProfit=f"{s.tp:.3f}",
            stopLoss=f"{s.sl:.3f}",
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice",
            positionIdx=0,
        )
        return oid

    # ─────────────────────────── callbacks / WS bridge ──────────────────────
    def _on_order(self, msg: dict) -> None:
        """Thread‑safe hand‑off of private‑stream messages to the asyncio loop."""
        # Unified‑V5 sometimes bundles multiple rows ⇒ iterate defensively
        for row in msg.get("data", []):
            if row.get("topic", msg.get("topic")) != "order":
                continue
            self.loop.call_soon_threadsafe(self._ws_q.put_nowait, row)

    async def _track_fills(self) -> None:
        """Async consumer that reconciles private WS updates with *book*."""
        while True:
            row = await self._ws_q.get()
            oid     = row["orderId"]
            status  = row["orderStatus"]
            symbol  = row["symbol"]

            key = self._oid_to_key.get(oid) or self._key_by_oid(oid)
            if not key:
                continue  # stray order not in our book

            if status == "Filled":
                logging.info("[%s] order %s filled", symbol, oid)
                # At this point you could start tracking PnL, break‑even stops…

            elif status in ("Cancelled", "Rejected"):
                logging.warning("[%s] order %s %s", symbol, oid, status)
                self.book.pop(key, None)
                self._oid_to_key.pop(oid, None)

            elif status == "PartiallyFilled":
                logging.info("[%s] order %s partially filled", symbol, oid)

            # … extend with position‑close events, TP hit, SL hit, etc.

    # helper – reverse lookup if dictionary fell out of sync (O(n) but tiny n)
    def _key_by_oid(self, oid: str) -> Optional[str]:
        for k, pos in self.book.items():
            if pos.order_id == oid:
                return k
        return None

    # ─────────────────────────── graceful shutdown ──────────────────────────
    async def close(self):
        await asyncio.to_thread(self.http.close)  # pybit HTTP has .close()
        # WebSocket object exposes .exit() for a clean stop
        self.private_ws.exit()
