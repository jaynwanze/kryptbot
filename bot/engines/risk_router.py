from __future__ import annotations

import asyncio
import time
import logging
import math
from collections import defaultdict
from typing import Dict, Optional

from bot.infra import Signal, Position, get_client, get_private_ws
from bot.helpers import config


class RiskRouter:
    """
    Routes Signals into Bybit MARKET brackets with ATR-risk sizing and budget cap.
    Keeps a small local 'book' and reconciles it with private WS updates.
    """

    ACCOUNT_TYPE   = "UNIFIED"   # UTA
    COIN           = "USDT"
    MARGIN_BUFFER  = 0.95        # headroom to avoid 110007 on fast ticks
    RETRY_ON_110007 = 3          # shrink qty and retry if margin error

    def __init__(self, *, equity_usd: float = 20.0, testnet: bool = True):
        self.equity = float(equity_usd)
        self.http   = get_client(testnet=testnet)
        self.loop   = asyncio.get_event_loop()

        # private streams
        self.private_ws = get_private_ws(testnet=testnet)
        self.private_ws.order_stream(callback=self._on_order)
        # subscribe to position/execution if available; else fallback poller
        try:
            self.private_ws.position_stream(callback=self._on_position)
        except Exception:
            logging.warning("position_stream not available; enabling poll fallback")
            asyncio.create_task(self._poll_positions())

        try:
            self.private_ws.execution_stream(callback=self._on_execution)
        except Exception:
            pass  # optional

        # per-symbol meta
        self._qty_step:  Dict[str, float] = defaultdict(lambda: 0.001)
        self._min_qty:   Dict[str, float] = defaultdict(lambda: 0.0)
        self._tick_size: Dict[str, float] = defaultdict(lambda: 0.0001)
        self._lev_min:   Dict[str, float] = {}
        self._lev_max:   Dict[str, float] = {}
        self._leveraged: set[str] = set()
        self.last_sl_ts: dict[str, float] = defaultdict(float)

        # working state
        self.book: dict[str, Position] = {}
        self._oid_to_key: Dict[str, str] = {}
        self.lock = asyncio.Lock()
        self._ws_q: asyncio.Queue = asyncio.Queue()

        asyncio.create_task(self._track_fills())

    # ───────────────────────────── public API ───────────────────────────────
    async def handle(self, sig: Signal) -> None:
        async with self.lock:
            if sig.key in self.book:
                logging.info("%s already active – ignoring dup", sig.key)
                return

            if sig.symbol not in self._lev_max:
                await self._warm_meta(sig.symbol)

            qty_risk, qty_budget, qty = await self._size_with_budget(sig)
            if qty <= 0:
                logging.info("[%s] Skip: qty <= 0 (risk %.6f, budget %.6f)",
                             sig.symbol, qty_risk, qty_budget)
                return

            # exchange minimums
            step  = self._qty_step[sig.symbol]
            min_q = max(self._min_qty[sig.symbol], step)
            if qty < min_q:
                logging.info("[%s] Skip: qty %.6f < minOrderQty %.6f", sig.symbol, qty, min_q)
                return

            oid = await self._place_bracket(sig, qty)
            self.book[sig.key] = Position(sig, oid, qty)
            self._oid_to_key[oid] = sig.key
            logging.info("[%s] order placed  id=%s  qty=%.6f", sig.symbol, oid, qty)

    # ───────────────────────────── internals ────────────────────────────────
    async def _warm_meta(self, symbol: str) -> None:
        """Fetch lot-size, min qty, tick size and set leverage."""
        info = (
            await asyncio.to_thread(
                self.http.get_instruments_info,
                category="linear",
                symbol=symbol,
            )
        )["result"]["list"][0]

        lot = info.get("lotSizeFilter", {})
        px  = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})

        self._qty_step[symbol]  = float(lot.get("qtyStep", 0.001))
        self._min_qty[symbol]   = float(lot.get("minOrderQty", 0.0))
        self._tick_size[symbol] = float(px.get("tickSize", 0.0001))
        self._lev_min[symbol]   = float(lev.get("minLeverage", 1))
        self._lev_max[symbol]   = float(lev.get("maxLeverage", 100))

        if symbol not in self._leveraged:
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
                logging.info("[%s] leverage set to %.1fx (allowed %.2f–%.2f)",
                             symbol, lev_use, self._lev_min[symbol], self._lev_max[symbol])
                self._leveraged.add(symbol)
            except Exception as e:
                logging.warning("[%s] set_leverage failed: %s (continuing; may use 1x)", symbol, e)

    # helpers
    @staticmethod
    def _floor_step(x: float, step: float) -> float:
        if step <= 0:
            return x
        return math.floor(x / step) * step

    def _round_price(self, symbol: str, px: float) -> float:
        tick = self._tick_size[symbol]
        if tick <= 0:
            return float(px)
        return self._floor_step(float(px), tick)

    async def _get_available_usdt(self) -> float:
        """Available USDT (conservative) for sizing."""
        try:
            resp = await asyncio.to_thread(
                self.http.get_wallet_balance,
                accountType=self.ACCOUNT_TYPE,
                coin=self.COIN,
            )
            row = resp["result"]["list"][0]
            if "totalAvailableBalance" in row:
                return float(row["totalAvailableBalance"])
            for c in row.get("coin", []):
                if c.get("coin") == self.COIN:
                    return float(c.get("availableToWithdraw", c.get("equity", 0.0)))
        except Exception as e:
            logging.warning("wallet balance fetch failed: %s (using 0)", e)
        return 0.0

    def _atr_risk_qty(self, sig: Signal) -> float:
        risk_usd  = self.equity * float(getattr(config, "RISK_PCT", 0.2))
        stop_dist = max(abs(float(sig.entry) - float(sig.sl)), 1e-8)
        raw_qty   = risk_usd / stop_dist
        step      = self._qty_step[sig.symbol]
        return round(self._floor_step(raw_qty, step), 8)

    async def _size_with_budget(self, sig: Signal) -> tuple[float, float, float]:
        """Return (qty_risk, qty_budget, qty_final)."""
        step      = self._qty_step[sig.symbol]
        qty_risk  = self._atr_risk_qty(sig)

        avail  = await self._get_available_usdt()
        lev_cf = float(getattr(config, "LEVERAGE", 1.0))
        lev    = min(max(lev_cf, self._lev_min[sig.symbol]), self._lev_max[sig.symbol])

        notional_cap = avail * lev * self.MARGIN_BUFFER
        qty_budget   = 0.0
        if float(sig.entry) > 0:
            qty_budget = self._floor_step(notional_cap / float(sig.entry), step)

        qty = min(qty_risk, qty_budget)

        # detailed sizing log
        risk_usd  = self.equity * float(getattr(config, "RISK_PCT", 0.2))
        stop_dist = max(abs(float(sig.entry) - float(sig.sl)), 1e-8)
        logging.info(
            "[SIZER %s] px=%.6f stop=%.6f risk_usd=%.2f avail=%.4f lev=%.2f "
            "cap=%.4f qty_risk=%.6f qty_budget=%.6f step=%.6f -> qty=%.6f",
            sig.symbol, float(sig.entry), stop_dist, risk_usd,
            avail, lev, notional_cap, qty_risk, qty_budget, step, qty
        )
        return qty_risk, qty_budget, qty

    # ── order placement -------------------------------------------------------
    async def _place_bracket(self, s: Signal, qty: float) -> str:
        """Market entry + attach TP/SL via set_trading_stop (reduce-only)."""
        px_tp = self._round_price(s.symbol, float(s.tp))
        px_sl = self._round_price(s.symbol, float(s.sl))

        # try place; shrink on margin error (110007)
        step = self._qty_step[s.symbol]
        tries = self.RETRY_ON_110007 + 1
        last_err = None
        while tries > 0:
            try:
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
                    takeProfit=f"{px_tp:.6f}",
                    stopLoss=f"{px_sl:.6f}",
                    tpTriggerBy="LastPrice",
                    slTriggerBy="LastPrice",
                    positionIdx=0,
                )
                return oid
            except Exception as e:
                last_err = str(e)
                if "110007" in last_err or "insufficient" in last_err.lower():
                    qty = max(0.0, qty - step)
                    tries -= 1
                    logging.warning("[%s] margin error; shrinking qty and retrying (%d left). err=%s",
                                    s.symbol, tries, last_err)
                    if qty <= 0:
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise
        raise RuntimeError(f"place_bracket failed after retries: {last_err}")

    # ─────────────────────────── WS bridges & reconciler ─────────────────────
    def _on_order(self, msg: dict) -> None:
        # Unified V5 may bundle rows
        rows = msg.get("data", msg.get("result", {}).get("list", [])) or []
        for row in rows:
            topic = row.get("topic", msg.get("topic", "order"))
            if topic != "order":
                continue
            self.loop.call_soon_threadsafe(self._ws_q.put_nowait, {"_t": "order", "row": row})

    def _on_position(self, msg: dict) -> None:
        rows = msg.get("data", msg.get("result", {}).get("list", [])) or []
        for row in rows:
            topic = row.get("topic", msg.get("topic", "position"))
            if topic != "position":
                continue
            self.loop.call_soon_threadsafe(self._ws_q.put_nowait, {"_t": "position", "row": row})

    def _on_execution(self, msg: dict) -> None:
        rows = msg.get("data", msg.get("result", {}).get("list", [])) or []
        for row in rows:
            topic = row.get("topic", msg.get("topic", "execution"))
            if topic != "execution":
                continue
            self.loop.call_soon_threadsafe(self._ws_q.put_nowait, {"_t": "execution", "row": row})

    async def _track_fills(self) -> None:
        while True:
            envelope = await self._ws_q.get()
            t = envelope.get("_t")
            row = envelope.get("row", {})

            if t == "order":
                oid    = row.get("orderId", "")
                status = row.get("orderStatus", "")
                symbol = row.get("symbol", "")

                key = self._oid_to_key.get(oid) or self._key_by_oid(oid)
                if not key:
                    continue

                if status == "Filled":
                    logging.info("[%s] order %s filled", symbol, oid)

                # was this a reduce‑only bracket (TP / SL) … or the market entry?
                reduce_only = str(row.get("reduceOnly", "false")).lower() == "true"
                tp_sl_type  = (row.get("tpSlOrderType") or "").lower()  # "stoploss" / "takeprofit" / ""

                if reduce_only:                       # <<< guard starts
                    if tp_sl_type == "stoploss":      # start cool‑down if it was the SL
                        self.last_sl_ts[symbol] = time.time()

                 # safe to wipe local book – *only* the exit leg sets reduceOnly
                    self._clear_symbol(
                    symbol,
                    reason=f"reduceOnly {tp_sl_type or 'bracket‑fill'}"
                  )                        

                elif status in ("Cancelled", "Rejected"):
                    logging.warning("[%s] order %s %s", symbol, oid, status)
                    self.book.pop(key, None)
                    self._oid_to_key.pop(oid, None)

                elif status == "PartiallyFilled":
                    logging.info("[%s] order %s partially filled", symbol, oid)

            elif t == "position":
                p = row
                symbol = p.get("symbol", "")
                size = float(p.get("size", p.get("positionAmt", 0) or 0))
                if abs(size) == 0.0:
                    self._clear_symbol(symbol, reason="position stream flat")

            elif t == "execution":
                # If your wrapper sends reduce-only executions here, you can also clear
                try:
                    symbol = row.get("symbol", "")
                    reduce_only = str(row.get("reduceOnly", "false")).lower() == "true"
                    if reduce_only:
                        self._clear_symbol(symbol, reason="execution reduceOnly")
                except Exception:
                    pass

    async def _poll_positions(self):
        """Fallback when position stream is unavailable."""
        while True:
            try:
                resp = await asyncio.to_thread(self.http.get_positions, category="linear")
                open_syms = {
                    p["symbol"]
                    for p in resp["result"]["list"]
                    if float(p.get("size", 0) or 0) != 0.0
                }
                for k in list(self.book):
                    sym = self.book[k].signal.symbol
                    if sym not in open_syms:
                        self.book.pop(k, None)
                        logging.info("[%s] position flat (poll); cleared local book.", sym)
            except Exception as e:
                logging.warning("position poll failed: %s", e)
            await asyncio.sleep(15)

    # helpers
    def _key_by_oid(self, oid: str) -> Optional[str]:
        for k, pos in self.book.items():
            if pos.order_id == oid:
                return k
        return None

    def _clear_symbol(self, symbol: str, *, reason: str) -> None:
        for k in list(self.book):
            if self.book[k].signal.symbol == symbol:
                self._oid_to_key = {oid: kk for oid, kk in self._oid_to_key.items() if kk != k}
                self.book.pop(k, None)
                logging.info("[%s] cleared local book (%s).", symbol, reason)

    # ─────────────────────────── graceful shutdown ──────────────────────────
    async def close(self):
        await asyncio.to_thread(self.http.close)
        self.private_ws.exit()

    def has_open(self, symbol: str) -> bool:
        return any(pos.signal.symbol == symbol for pos in self.book.values())

    def open_for_key(self, key: str) -> bool:
        return key in self.book
