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

    ACCOUNT_TYPE = "UNIFIED"   # "UNIFIED" (UTA) or "CONTRACT" (classic)
    COIN = "USDT"
    MARGIN_BUFFER = 0.95       # keep 5% headroom to avoid 110007 on fast moves

    # ────────────────────────────── life‑cycle ──────────────────────────────
    def __init__(self, *, equity_usd: float = 20.0, testnet: bool = True):
        self.equity = equity_usd
        self.http = get_client(testnet=testnet)            # REST (blocking)
        self.loop = asyncio.get_event_loop()

        # private stream (runs a dedicated background thread internally)
        self.private_ws = get_private_ws(testnet=testnet)
        self.private_ws.order_stream(callback=self._on_order)  # subscribe

        # per‑symbol meta cache
        self._qty_step: Dict[str, float] = defaultdict(lambda: 0.001)
        self._min_qty:  Dict[str, float] = defaultdict(lambda: 0.0)
        self._lev_min:  Dict[str, float] = {}
        self._lev_max:  Dict[str, float] = {}
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
            if sig.symbol not in self._lev_max:
                await self._warm_meta(sig.symbol)

            # --- sizing (ATR‑risk + budget cap) ---
            qty_risk, qty_budget, qty = await self._size_with_budget(sig)

            if qty <= 0:
                logging.info("[%s] Skip: qty <= 0 after sizing (risk %.6f, budget %.6f)",
                             sig.symbol, qty_risk, qty_budget)
                return

            # exchange minimum guard
            min_q = self._min_qty[sig.symbol]
            if qty < max(min_q, self._qty_step[sig.symbol]):
                logging.info("[%s] Skip: qty %.6f < minOrderQty %.6f", sig.symbol, qty, min_q)
                return

            order_id = await self._place_bracket(sig, qty)

            self.book[sig.key] = Position(sig, order_id, qty)
            self._oid_to_key[order_id] = sig.key
            logging.info("[%s] order placed  id=%s  qty=%f", sig.symbol, order_id, qty)

    # ───────────────────────────── internals ────────────────────────────────
    async def _warm_meta(self, symbol: str) -> None:
        """Fetch lot‑size, leverage range & set leverage (once per symbol)."""
        info = (
            await asyncio.to_thread(
                self.http.get_instruments_info,
                category="linear",
                symbol=symbol,
            )
        )["result"]["list"][0]

        lot = info["lotSizeFilter"]
        self._qty_step[symbol] = float(lot["qtyStep"])
        self._min_qty[symbol]  = float(lot.get("minOrderQty", 0.0))

        lev = info.get("leverageFilter", {})
        self._lev_min[symbol] = float(lev.get("minLeverage", 1))
        self._lev_max[symbol] = float(lev.get("maxLeverage", 100))

        if symbol not in self._leveraged:
            # clamp configured leverage into allowed band and send as STRING
            cfg_lev = float(getattr(config, "LEVERAGE", 1))
            lev_use = min(max(cfg_lev, self._lev_min[symbol]), self._lev_max[symbol])
            try:
                await asyncio.to_thread(
                    self.http.set_leverage,
                    category="linear",
                    symbol=symbol,
                    buyLeverage=str(lev_use),
                    sellLeverage=str(lev_use),
                )
                logging.info("[%s] leverage set to %s× (allowed %.2f–%.2f)",
                             symbol, lev_use, self._lev_min[symbol], self._lev_max[symbol])
                self._leveraged.add(symbol)
            except Exception as e:
                logging.warning("[%s] set_leverage failed: %s (continuing; may rely on x1)", symbol, e)

    # helpers
    @staticmethod
    def _floor_step(x: float, step: float) -> float:
        if step <= 0:
            return x
        return math.floor(x / step) * step

    async def _get_available_usdt(self) -> float:
        """Return available USDT margin for order sizing."""
        try:
            resp = await asyncio.to_thread(
                self.http.get_wallet_balance,
                accountType=self.ACCOUNT_TYPE,
                coin=self.COIN,
            )
            row = resp["result"]["list"][0]
            # Prefer account‑level available; fall back to coin’s bucket
            if "totalAvailableBalance" in row:
                return float(row["totalAvailableBalance"])
            for c in row.get("coin", []):
                if c.get("coin") == self.COIN:
                    # availableToWithdraw is a decent conservative proxy
                    return float(c.get("availableToWithdraw", c.get("equity", 0.0)))
        except Exception as e:
            logging.warning("wallet balance fetch failed: %s (using 0)", e)
        return 0.0

    def _atr_risk_qty(self, sig: Signal) -> float:
        """Pure ATR‑risk sizing rounded DOWN to qtyStep."""
        risk_usd = self.equity * config.RISK_PCT
        stop_dist = max(abs(sig.entry - sig.sl), 1e-8)      # protect div/0
        raw_qty = risk_usd / stop_dist
        step = self._qty_step[sig.symbol]
        return round(self._floor_step(raw_qty, step), 8)

    async def _size_with_budget(self, sig: Signal) -> tuple[float, float, float]:
        """Return (qty_risk, qty_budget, qty_final)."""
        step = self._qty_step[sig.symbol]
        qty_risk = self._atr_risk_qty(sig)

        avail = await self._get_available_usdt()
        lev_cfg = float(getattr(config, "LEVERAGE", 1.0))
        lev_use = min(max(lev_cfg, self._lev_min[sig.symbol]), self._lev_max[sig.symbol])

        notional_cap = avail * lev_use * self.MARGIN_BUFFER
        qty_budget = 0.0
        if sig.entry > 0:
            qty_budget = self._floor_step(notional_cap / float(sig.entry), step)

        qty = min(qty_risk, qty_budget)

        # Detailed sizing log
        risk_usd = self.equity * config.RISK_PCT
        stop_dist = max(abs(sig.entry - sig.sl), 1e-8)
        logging.info(
            "[SIZER %s] px=%.6f stop=%.6f risk_usd=%.2f "
            "avail=%.4f lev=%.2f cap=%.4f qty_risk=%.6f qty_budget=%.6f step=%.6f -> qty=%.6f",
            sig.symbol, float(sig.entry), stop_dist, risk_usd,
            avail, lev_use, notional_cap, qty_risk, qty_budget, step, qty
        )
        return qty_risk, qty_budget, qty

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
            takeProfit=f"{s.tp:.6f}",
            stopLoss=f"{s.sl:.6f}",
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
            elif status in ("Cancelled", "Rejected"):
                logging.warning("[%s] order %s %s", symbol, oid, status)
                self.book.pop(key, None)
                self._oid_to_key.pop(oid, None)
            elif status == "PartiallyFilled":
                logging.info("[%s] order %s partially filled", symbol, oid)

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

    def has_open(self, symbol: str) -> bool:
        return any(pos.signal.symbol == symbol for pos in self.book.values())

    def open_for_key(self, key: str) -> bool:
        return key in self.book
