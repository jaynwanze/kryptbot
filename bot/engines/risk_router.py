# risk_router.py
from __future__ import annotations
from decimal import Decimal
import asyncio
import time
import logging
import math
from collections import deque, defaultdict
from typing import Dict, Optional, Tuple
from bot.infra import Signal, Position, get_client, get_private_ws
from bot.helpers import config, telegram
import ccxt.async_support as ccxt
from datetime import datetime
from pathlib import Path, PurePath
import csv


# Config & logging
TRAIL_ENABLE = getattr(config, "TRAIL_ENABLE", True)
BE_ARM_R = getattr(config, "BE_ARM_R", 0.8)  # move stop to BE after +0.8R
ATR_TRAIL_K = getattr(config, "ATR_TRAIL_K", 0.9)  # trail distance = k * ATR
REMOVE_TP_WHEN_TRAIL = getattr(
    config, "REMOVE_TP_WHEN_TRAIL", False
)  # keep your 1.5R TP by default

LOG_DIR = Path(getattr(config, "LOG_DIR", "./logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _append_csv(name: str, row: dict, fields: list[str]):
    p = LOG_DIR / name
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


# in risk_router.py or a helper module
async def audit_and_override_ticks(router, symbols):
    for sym in symbols:
        # warm caches (gets qtyStep, minOrderQty, tickSize, etc.)
        await router._warm_meta(sym)
        live_tick = router._tick_size[sym]
        cfg_tick = getattr(config, "TICK_SIZE", {}).get(sym)
        mark = "✅" if cfg_tick is None or float(cfg_tick) == live_tick else "❌"
        logging.info(
            "[FILTERS %s] tickSize live=%s cfg=%s %s", sym, live_tick, cfg_tick, mark
        )
        # optional: override the config value so any code that still reads it is correct
        try:
            config.TICK_SIZE[sym] = live_tick
        except Exception:
            pass


def quantize_step(value, step):
    step = Decimal(str(step))
    v = (Decimal(str(value)) // step) * step  # floor to step
    decimals = max(0, -step.as_tuple().exponent)
    return format(v, f".{decimals}f")  # clean string like "192.7" or "0.7"


class RiskRouter:
    """
    Routes Signals into Bybit MARKET brackets with ATR-risk sizing and a budget cap.
    Keeps a small local 'book' and reconciles it with private WS updates.
    Book is cleared ONLY on reduce-only (TP/SL) fills
    Cool-down starts ONLY on StopLoss fills
    Execution stream fee accumulator & round-trip fee logging
    Entry/exit price tracking and net (approx) PnL logging
    Safer qty rounding, robust field parsing
    """

    ACCOUNT_TYPE = "UNIFIED"  # UTA
    COIN = "USDT"
    MARGIN_BUFFER = 0.95  # headroom to avoid 110007 on fast ticks
    RETRY_ON_110007 = 3  # shrink qty and retry if margin error

    def __init__(self, *, equity_usd: float = 20.0, testnet: bool = True):
        self.equity = float(equity_usd)
        self.http = get_client(testnet=testnet)
        self.loop = asyncio.get_event_loop()
        self.ccxt = ccxt.bybit({"enableRateLimit": True})
        try:
            if testnet:
                self.ccxt.set_sandbox_mode(True)
        except Exception:
            pass
        # private streams
        self.private_ws = get_private_ws(testnet=testnet)
        self.private_ws.order_stream(callback=self._on_order)
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
        self._qty_step: Dict[str, float] = defaultdict(lambda: 0.001)
        self._min_qty: Dict[str, float] = defaultdict(lambda: 0.0)
        self._tick_size: Dict[str, float] = defaultdict(lambda: 0.0001)
        self._lev_min: Dict[str, float] = {}
        self._lev_max: Dict[str, float] = {}
        self._leveraged: set[str] = set()

        # cool-down bookkeeping (start only on SL)
        self.last_sl_ts: dict[str, float] = defaultdict(float)

        # working state
        self.book: dict[str, Position] = {}
        self._oid_to_key: Dict[str, str] = {}
        self.lock = asyncio.Lock()
        self._ws_q: asyncio.Queue = asyncio.Queue()

        # round-trip accounting / diagnostics
        self._entry_price: Dict[str, float] = defaultdict(float)
        self._exit_price: Dict[str, float] = defaultdict(float)
        self._fees_usdt: Dict[str, float] = defaultdict(float)
        self._side: Dict[str, str] = defaultdict(str)  # "Buy"/"Sell"

        self.pending: dict[str, Position] = {}
        self._pending_ts: dict[str, float] = {}
        self.PENDING_TTL_SEC = int(getattr(config, "PENDING_TTL_SEC", 20))
        asyncio.create_task(self._gc_pending())
        # daily stats
        self._daily = defaultdict(
            lambda: {
                "trades": 0,
                "tp": 0,
                "sl": 0,
                "wins": 0,
                "losses": 0,
                "pos": 0.0,
                "neg": 0.0,
                "gross": 0.0,
                "fees": 0.0,
                "net": 0.0,
                "by_symbol": defaultdict(
                    lambda: {
                        "trades": 0,
                        "tp": 0,
                        "sl": 0,
                        "gross": 0.0,
                        "fees": 0.0,
                        "net": 0.0,
                    }
                ),
            }
        )
        self.closed_trades = deque(maxlen=500)

        asyncio.create_task(self._track_fills())

    # --- portfolio caps (read from config with sensible defaults)

    @property
    def max_open_concurrent(self) -> int:
        return int(getattr(config, "MAX_OPEN_CONCURRENT", 5))

    @property
    def max_per_side_open(self) -> int:
        return int(getattr(config, "MAX_PER_SIDE_OPEN", 1))

    @property
    def max_total_risk_pct(self) -> float:
        return float(getattr(config, "MAX_TOTAL_RISK_PCT", 0.30))

    def open_count(self) -> int:
        return len(self.book)

    def _pos_risk_usd(self, pos: Position) -> float:
        """Estimated risk in USDT for an open position: |entry - SL| * qty."""
        sym = pos.signal.symbol
        # prefer actual filled entry if we have it; else planned entry
        entry = self._entry_price.get(sym, 0.0) or float(pos.signal.entry)
        sl = float(pos.signal.sl)
        qty = float(getattr(pos, "qty", 0.0))
        return abs(entry - sl) * qty

    def open_risk_usd(self) -> float:
        return sum(self._pos_risk_usd(p) for p in self.book.values())

    def open_risk_pct(self) -> float:
        if self.equity <= 0:
            return 1.0
        return self.open_risk_usd() / self.equity

    def _fmt_px(self, symbol: str, px: float) -> str:
        tick = Decimal(str(self._tick_size[symbol]))
        decs = max(0, -tick.as_tuple().exponent)
        q = (Decimal(str(px)) // tick) * tick  # floor to tick
        return format(q, f".{decs}f")

    def open_count_side(self, side: str) -> int:
        s = side.lower()
        return sum(1 for p in self.book.values() if p.signal.side.lower() == s)

    def round_qty(self, symbol: str, qty: float) -> float:
        step = self._qty_step[symbol]
        return float(quantize_step(qty, step))

    # ───────────────────────────── public API ───────────────────────────────
    async def handle(self, sig: Signal) -> None:
        async with self.lock:
            if sig.key in self.book:
                logging.info("%s already active – ignoring dup", sig.key)
                return

        # ---  portfolio caps ---
        side = sig.side.lower()
        if side == "buy":
            if (
                self.open_count_side("Buy") + self.pending_count_side("Buy")
            ) >= self.max_per_side_open:
                logging.info("[PORTFOLIO] Long-side full — skip %s", sig.symbol)
                return
        else:
            if (
                self.open_count_side("Sell") + self.pending_count_side("Sell")
            ) >= self.max_per_side_open:
                logging.info("[PORTFOLIO] Short-side full — skip %s", sig.symbol)

        # ---  check for existing positions
        if any(p.signal.symbol == sig.symbol for p in self.book.values()) or any(
            p.signal.symbol == sig.symbol for p in self.pending.values()
        ):
            logging.info("[%s] Skip: already have an open/pending position", sig.symbol)
            return

        # per-pair rule: only 1 open per symbol
        if any(p.signal.symbol == sig.symbol for p in self.book.values()):
            logging.info("[%s] Skip: already have an open position", sig.symbol)
            return

        # portfolio concurrency guard
        if self.open_count() >= self.max_open_concurrent:
            logging.info(
                "[PORTFOLIO] Skip %s: concurrent cap reached (%d)",
                sig.symbol,
                self.max_open_concurrent,
            )
            return

        # ensure meta (lot size, ticks, leverage) is warm
        if sig.symbol not in self._lev_max:
            await self._warm_meta(sig.symbol)

        # Get budget/step bounds from your existing helper
        # (qty_from_plan is ignored – we recompute risk-based qty with live price)
        _qty_risk_plan, qty_budget, _qty_from_plan = await self._size_with_budget(sig)

        # -------- live price & risk distance --------
        px = await self.mark_or_last_price(sig.symbol)

        if sig.side == "Buy":
            risk_dist = max(1e-12, float(px) - float(sig.sl))
        else:
            risk_dist = max(1e-12, float(sig.sl) - float(px))

        risk_usd = self.risk_usd_per_trade()
        qty_risk = risk_usd / risk_dist

        # round to lot step and cap by budget
        qty = self.round_qty(sig.symbol, min(qty_risk, qty_budget))

        # proposed risk at current price
        proposed_risk = risk_dist * float(qty)

        # portfolio risk guard
        if (self.open_risk_usd() + proposed_risk) > (
            self.equity * self.max_total_risk_pct
        ):
            logging.info(
                "[PORTFOLIO] Skip %s: risk cap %.0f%% exceeded "
                "(open=%.2f, proposed=%.2f, cap=%.2f)",
                sig.symbol,
                self.max_total_risk_pct * 100,
                self.open_risk_usd(),
                proposed_risk,
                self.equity * self.max_total_risk_pct,
            )
            return

        # exchange minimums
        step = self._qty_step[sig.symbol]
        min_q = max(self._min_qty[sig.symbol], step)
        if qty < min_q:
            logging.info(
                "[%s] Skip: qty %.6f < minOrderQty %.6f", sig.symbol, qty, min_q
            )
            return

        # place & track
        oid = await self._place_bracket(sig, qty)
        pos = Position(sig, oid, qty)
        self.pending[sig.key] = pos  # <— was: self.book[...]
        self._pending_ts[sig.key] = time.time()
        self._oid_to_key[oid] = sig.key
        # pos meta for tracking trailing/BE
        pos.meta = {
            "stop_off": float(sig.off_sl),  # you already compute this
            "be_armed": False,
            "trail_on": False,
        }

        logging.info(
            "[%s] order placed id=%s qty=%.6f | router: open=%d risk=%.1f%% (counting fills only)",
            sig.symbol,
            oid,
            qty,
            self.open_count(),
            self.open_risk_pct() * 100.0,
        )

    async def amend_stop(self, symbol: str, new_sl: float) -> None:
        """Tighten SL only. Keeps current TP unchanged."""
        sl_str = self._fmt_px(symbol, new_sl)
        try:
            await asyncio.to_thread(
                self.http.set_trading_stop,
                category="linear",
                symbol=symbol,
                stopLoss=sl_str,
                slTriggerBy="LastPrice",
                positionIdx=0,
            )
            logging.info("[%s] SL amended -> %s", symbol, sl_str)
            telegram.bybit_alert(msg=f"[{symbol}] SL amended -> {sl_str}")
            # also update the local signal snapshot so we don't widen later
            for pos in self.book.values():
                if pos.signal.symbol == symbol:
                    pos.signal.sl = float(sl_str)
                    break
        except Exception as e:
            logging.warning("[%s] amend_stop failed: %s", symbol, e)

    async def cancel_tp(self, symbol: str) -> None:
        """Remove TP (for full trail mode)."""
        try:
            await asyncio.to_thread(
                self.http.set_trading_stop,
                category="linear",
                symbol=symbol,
                takeProfit="",
                tpTriggerBy="LastPrice",
                positionIdx=0,
            )
            logging.info("[%s] TP cleared (trail mode)", symbol)
            telegram.bybit_alert(msg=f"[{symbol}] TP cleared (trail mode)")
            # optional: reflect locally
            for pos in self.book.values():
                if pos.signal.symbol == symbol:
                    pos.signal.tp = 0.0
                    break
        except Exception as e:
            logging.warning("[%s] cancel_tp failed: %s", symbol, e)

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
        px = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})

        self._qty_step[symbol] = self._safe_float(lot.get("qtyStep"), 0.001)
        self._min_qty[symbol] = self._safe_float(lot.get("minOrderQty"), 0.0)
        self._tick_size[symbol] = self._safe_float(px.get("tickSize"), 0.0001)
        self._lev_min[symbol] = self._safe_float(lev.get("minLeverage"), 1)
        self._lev_max[symbol] = self._safe_float(lev.get("maxLeverage"), 100)

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
                logging.info(
                    "[%s] leverage set to %.1fx (allowed %.2f–%.2f)",
                    symbol,
                    lev_use,
                    self._lev_min[symbol],
                    self._lev_max[symbol],
                )
                self._leveraged.add(symbol)
            except Exception as e:
                if "110043" in str(e):
                    # treat as OK; avoid spamming & re-trying
                    self._leveraged.add(symbol)
                    logging.info(
                        "[%s] leverage unchanged (110043) — continuing", symbol
                    )
                else:
                    logging.warning(
                        "[%s] set_leverage failed: %s (continuing; may use 1x)",
                        symbol,
                        e,
                    )

    # helpers
    @staticmethod
    def _floor_step(x: float, step: float) -> float:
        if step <= 0:
            return x
        return math.floor(x / step) * step

    @staticmethod
    def _safe_float(x, default=0.0) -> float:
        try:
            return float(x)
        except Exception:
            return float(default)

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
                return self._safe_float(row["totalAvailableBalance"], 0.0)
            for c in row.get("coin", []):
                if c.get("coin") == self.COIN:
                    return self._safe_float(
                        c.get("availableToWithdraw", c.get("equity", 0.0))
                    )
        except Exception as e:
            logging.warning("wallet balance fetch failed: %s (using 0)", e)
        return 0.0

    async def mark_or_last_price(self, symbol: str) -> float:
        try:
            t = await self.ccxt.fetch_ticker(symbol)
            info = t.get("info", {})
            # Bybit often exposes mark price here:
            mark = info.get("markPrice") or info.get("mark_price")
            if mark is not None:
                return float(mark)
            if t.get("last"):
                return float(t["last"])
        except Exception:
            pass
        # Fallback to mid of book if ticker fails
        ob = await self.ccxt.fetch_order_book(symbol, limit=5)
        bid = ob["bids"][0][0] if ob["bids"] else None
        ask = ob["asks"][0][0] if ob["asks"] else None
        if bid and ask:
            return float((bid + ask) / 2.0)
        raise RuntimeError(f"Cannot obtain live price for {symbol}")

    def _atr_risk_qty(self, sig: Signal) -> float:
        risk_usd = self.equity * float(getattr(config, "RISK_PCT", 0.1))
        stop_dist = max(abs(float(sig.entry) - float(sig.sl)), 1e-8)
        raw_qty = risk_usd / stop_dist
        step = self._qty_step[sig.symbol]
        return round(self._floor_step(raw_qty, step), 8)

    async def _size_with_budget(self, sig: Signal) -> Tuple[float, float, float]:
        """Return (qty_risk, qty_budget, qty_final)."""
        step = self._qty_step[sig.symbol]
        qty_risk = self._atr_risk_qty(sig)

        avail = max(0.0, await self._get_available_usdt())
        lev_cf = float(getattr(config, "LEVERAGE", 1.0))
        lev = min(max(lev_cf, self._lev_min[sig.symbol]), self._lev_max[sig.symbol])

        notional_cap = avail * lev * self.MARGIN_BUFFER
        qty_budget = 0.0
        if float(sig.entry) > 0:
            qty_budget = self._floor_step(notional_cap / float(sig.entry), step)

        qty = min(qty_risk, qty_budget)

        # detailed sizing log
        risk_usd = self.equity * float(getattr(config, "RISK_PCT", 0.1))
        stop_dist = max(abs(float(sig.entry) - float(sig.sl)), 1e-8)
        logging.info(
            "[SIZER %s] px=%.6f stop_dist=%.6f risk_usd=%.2f avail=%.4f lev=%.2f "
            "cap=%.4f qty_risk=%.6f qty_budget=%.6f step=%.6f -> qty=%.6f",
            sig.symbol,
            float(sig.entry),
            stop_dist,
            risk_usd,
            avail,
            lev,
            notional_cap,
            qty_risk,
            qty_budget,
            step,
            qty,
        )
        telegram.bybit_alert(
            msg=f"[SIZER {sig.symbol}] px={sig.entry:.6f} stop={sig.sl:.6f} "
            f"risk_usd={risk_usd:.2f} avail={avail:.4f} lev={lev:.2f} "
            f"cap={notional_cap:.4f} qty_risk={qty_risk:.6f} "
            f"qty_budget={qty_budget:.6f} step={step:.6f} -> qty={qty:.6f}"
        )
        return qty_risk, qty_budget, qty

    # ── order placement -------------------------------------------------------

    async def _place_bracket(self, s: Signal, qty: float) -> str:
        # round TP/SL to tick
        tp_str = self._fmt_px(s.symbol, float(s.tp))
        sl_str = self._fmt_px(s.symbol, float(s.sl))

        # try place; shrink on margin error (110007)
        step = self._qty_step[s.symbol]
        tries = self.RETRY_ON_110007 + 1
        last_err = None

        # **USE the symbol’s lot step and send a clean string**
        qty_str = quantize_step(qty, step)

        while tries > 0:
            try:
                resp = await asyncio.to_thread(
                    self.http.place_order,
                    category="linear",
                    symbol=s.symbol,
                    side=s.side,
                    orderType="Market",
                    qty=qty_str,  # <— use the quantized string
                    reduceOnly=False,
                    closeOnTrigger=False,
                )
                oid: str = resp["result"]["orderId"]

                await asyncio.to_thread(
                    self.http.set_trading_stop,
                    category="linear",
                    symbol=s.symbol,
                    takeProfit=tp_str,
                    stopLoss=sl_str,
                    tpTriggerBy="LastPrice",
                    slTriggerBy="LastPrice",
                    positionIdx=0,
                )
                _append_csv(
                    "orders.csv",
                    {
                        "ts": _now(),
                        "symbol": s.symbol,
                        "event": "PLACED",
                        "order_id": oid,
                        "side": s.side,
                        "qty": qty_str,
                        "price": "",
                        "reduce_only": "false",
                    },
                    [
                        "ts",
                        "symbol",
                        "event",
                        "order_id",
                        "side",
                        "qty",
                        "price",
                        "reduce_only",
                    ],
                )
                return oid

            except Exception as e:
                last_err = str(e)
                if "110007" in last_err or "insufficient" in last_err.lower():
                    # shrink then re-quantize (avoids 0.7000000000000001)
                    new_qty = max(0.0, float(qty_str) - step)
                    qty_str = quantize_step(new_qty, step)
                    tries -= 1
                    logging.warning(
                        "[%s] margin error; shrinking qty and retrying (%d left). err=%s",
                        s.symbol,
                        tries,
                        last_err,
                    )
                    telegram.bybit_alert(
                        msg=f"Margin error; shrinking qty and retrying ({tries} left). err={last_err}"
                    )
                    if float(qty_str) <= 0:
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise
        raise RuntimeError(f"place_bracket failed after retries: {last_err}")

    # ─────────────────────────── WS bridges & reconciler ─────────────────────
    def _on_order(self, msg: dict) -> None:
        rows = msg.get("data", msg.get("result", {}).get("list", [])) or []
        for row in rows:
            topic = row.get("topic", msg.get("topic", "order"))
            if topic != "order":
                continue
            self.loop.call_soon_threadsafe(
                self._ws_q.put_nowait, {"_t": "order", "row": row}
            )

    def _on_position(self, msg: dict) -> None:
        rows = msg.get("data", msg.get("result", {}).get("list", [])) or []
        for row in rows:
            topic = row.get("topic", msg.get("topic", "position"))
            if topic != "position":
                continue
            self.loop.call_soon_threadsafe(
                self._ws_q.put_nowait, {"_t": "position", "row": row}
            )

    def _on_execution(self, msg: dict) -> None:
        rows = msg.get("data", msg.get("result", {}).get("list", [])) or []
        for row in rows:
            topic = row.get("topic", msg.get("topic", "execution"))
            if topic != "execution":
                continue
            self.loop.call_soon_threadsafe(
                self._ws_q.put_nowait, {"_t": "execution", "row": row}
            )

    # async functions
    async def _reseed_bracket_after_fill(self, symbol: str) -> None:
        # find the position for this symbol
        for pos in self.book.values():
            if pos.signal.symbol != symbol:
                continue
            s = pos.signal
            entry = float(self._entry_price.get(symbol, 0.0))
            if entry <= 0:
                return
            off_sl = float(getattr(s, "off_sl", 0.0))
            off_tp = float(getattr(s, "off_tp", 0.0))
            if off_sl <= 0 or off_tp <= 0:
                return

            if s.side.lower() == "buy":
                new_sl = self._fmt_px(symbol, entry - off_sl)
                new_tp = self._fmt_px(symbol, entry + off_tp)
            else:
                new_sl = self._fmt_px(symbol, entry + off_sl)
                new_tp = self._fmt_px(symbol, entry - off_tp)

                # To test
            # # Place 50% take at 1R (reduce-only limit), then move SL to BE when hit.
            #             half = max(0.0, float(getattr(pos, "qty", 0.0)) * 0.5)
            #             if half > 0:
            #                 tp1 = new_tp if getattr(s, "rr_first", 1.0) >= 1.0 else self._fmt_px(symbol, entry + (off_tp if s.side=="Buy" else -off_tp))
            #                 await asyncio.to_thread(
            #                     self.http.place_order,
            #                     category="linear", symbol=symbol,
            #                     side="Sell" if s.side=="Buy" else "Buy",
            #                     orderType="Limit", qty=quantize_step(half, self._qty_step[symbol]),
            #                     price=tp1, reduceOnly=True, timeInForce="PostOnly"
            #                 )

            await asyncio.to_thread(
                self.http.set_trading_stop,
                category="linear",
                symbol=symbol,
                takeProfit=new_tp,
                stopLoss=new_sl,
                tpTriggerBy="LastPrice",
                slTriggerBy="LastPrice",
                positionIdx=0,
            )
            logging.info(
                "[%s] adjusted TP/SL after fill: entry=%.6f SL=%s TP=%s",
                symbol,
                entry,
                new_sl,
                new_tp,
            )
            telegram.bybit_alert(
                msg=f"[{symbol}] adjusted TP/SL after fill: entry={entry:.6f} SL={new_sl} TP={new_tp}"
            )
            return

    async def _track_fills(self) -> None:
        while True:
            envelope = await self._ws_q.get()
            t = envelope.get("_t")
            row = envelope.get("row", {})

            if t == "order":
                oid = row.get("orderId", "")
                status = row.get("orderStatus", "")
                symbol = row.get("symbol", "")

                key = self._oid_to_key.get(oid) or self._key_by_oid(oid)
                if not key:
                    continue

                if status == "Filled":
                    logging.info("[%s] order %s filled", symbol, oid)
                    telegram.bybit_alert(msg=f"Order {oid} filled for {symbol}")
                    reduce_only = str(row.get("reduceOnly", "false")).lower() == "true"
                    tp_sl_type = (
                        row.get("tpSlOrderType") or ""
                    ).lower()  # "stoploss"/"takeprofit"/""

                    if reduce_only:
                        # Exit leg filled (TP or SL)
                        if tp_sl_type == "stoploss":
                            self.last_sl_ts[symbol] = time.time()
                        # best-effort exit price for diagnostics
                        self._exit_price[symbol] = self._safe_float(
                            row.get("avgPrice") or row.get("price"), 0.0
                        )
                        self._log_round_trip(symbol, tp_sl_type or "bracket")
                        self._clear_symbol(
                            symbol, reason=f"reduceOnly {tp_sl_type or ''}"
                        )

                    else:
                        # ENTRY filled → move PENDING → BOOK
                        key = self._oid_to_key.get(oid) or self._key_by_oid(oid)
                        if key and key in self.pending:
                            self.book[key] = self.pending.pop(key)
                            self._pending_ts.pop(key, None)
                            try:
                                self.book[key].is_open = True
                            except Exception:
                                pass
                        self._entry_price[symbol] = self._safe_float(
                            row.get("avgPrice") or row.get("price"), 0.0
                        )
                        self._side[symbol] = row.get("side", self._side.get(symbol, ""))
                        self._reset_accumulators(symbol)
                        await self._reseed_bracket_after_fill(symbol)

                elif status in ("Cancelled", "Rejected"):
                    logging.warning("[%s] order %s %s", symbol, oid, status)
                    telegram.bybit_alert(msg=f"Order {oid} {status} for {symbol}")
                    self.book.pop(key, None)
                    self._oid_to_key.pop(oid, None)
                    if key in self.pending:
                        self.pending.pop(key, None)
                        self._pending_ts.pop(key, None)

                elif status == "PartiallyFilled":
                    logging.info(
                        "[%s] order %s partially filled (entry likely)", symbol, oid
                    )
                    telegram.bybit_alert(
                        msg=f"Order {oid} partially filled for {symbol}"
                    )

            elif t == "position":
                p = row
                symbol = p.get("symbol", "")
                size = self._safe_float(p.get("size", p.get("positionAmt", 0) or 0))
                if abs(size) == 0.0:
                    self._clear_symbol(symbol, reason="position stream flat")

            elif t == "execution":
                # accumulate fees; clear on reduce-only execution as an extra safeguard
                try:
                    symbol = row.get("symbol", "")
                    fee = abs(self._safe_float(row.get("execFee"), 0.0))
                    if fee:
                        self._fees_usdt[symbol] += fee

                    reduce_only = str(row.get("reduceOnly", "false")).lower() == "true"
                    if reduce_only:
                        # try to capture a more exact exit price
                        self._exit_price[symbol] = self._safe_float(
                            row.get("execPrice") or row.get("avgPrice"), 0.0
                        )
                        # we don't know if it's TP or SL here; try to detect
                        ord_cat = (row.get("orderCategory") or "").lower()
                        tp_sl_type = (
                            "stoploss"
                            if "sl" in ord_cat
                            else ("takeprofit" if "tp" in ord_cat else "")
                        )
                        if tp_sl_type == "stoploss":
                            self.last_sl_ts[symbol] = time.time()
                        self._log_round_trip(symbol, tp_sl_type or "execution")
                        self._clear_symbol(symbol, reason="execution reduceOnly")
                    else:
                        # ENTRY execution: promote pending → book if still pending
                        oid = row.get("orderId", "")
                        key = self._oid_to_key.get(oid) or next(
                            (
                                k
                                for k, p in self.pending.items()
                                if p.signal.symbol == symbol
                            ),
                            None,
                        )
                        if key and key in self.pending:
                            self.book[key] = self.pending.pop(key)
                            self._pending_ts.pop(key, None)
                            try:
                                self.book[key].is_open = True
                            except Exception:
                                pass
                        # keep a reasonable entry price snapshot
                        px = self._safe_float(
                            row.get("execPrice") or row.get("avgPrice"), 0.0
                        )
                        if px:
                            self._entry_price[symbol] = px
                        self._side[symbol] = row.get("side", self._side.get(symbol, ""))
                except Exception:
                    pass

    async def _poll_positions(self):
        """Fallback when position stream is unavailable."""
        while True:
            try:
                resp = await asyncio.to_thread(
                    self.http.get_positions,
                    category="linear",
                    settleCoin="USDT",  # <-- add this
                )
                open_syms = {
                    p["symbol"]
                    for p in resp["result"]["list"]
                    if self._safe_float(p.get("size", 0) or 0) != 0.0
                }
                for k in list(self.book):
                    sym = self.book[k].signal.symbol
                    if sym not in open_syms:
                        self.book.pop(k, None)
                        logging.info(
                            "[%s] position flat (poll); cleared local book.", sym
                        )
                        telegram.bybit_alert(
                            msg=f"Position flat (poll); cleared local book for {sym}"
                        )
            except Exception as e:
                logging.warning("position poll failed: %s", e)
                telegram.bybit_alert(msg=f"Position poll failed: {e}")
            await asyncio.sleep(15)

    # ───────────────────────── fee/PnL diagnostics ──────────────────────────
    def _reset_accumulators(self, symbol: str) -> None:
        self._fees_usdt[symbol] = 0.0
        self._exit_price[symbol] = 0.0

    def _log_round_trip(self, symbol: str, exit_kind: str) -> None:
        """Best-effort round-trip summary when a bracket leg closes the position."""
        fees = self._fees_usdt.get(symbol, 0.0)
        entry = self._entry_price.get(symbol, 0.0)
        exitp = self._exit_price.get(symbol, 0.0)
        side = (self._side.get(symbol) or "").lower()

        qty = 0.0
        for pos in self.book.values():
            if pos.signal.symbol == symbol:
                qty = float(getattr(pos, "qty", 0.0) or 0.0)
                break

        gross = net = 0.0
        approx = ""
        try:
            if entry > 0 and exitp > 0 and qty > 0:
                direction = 1 if side == "buy" else -1
                gross = direction * (exitp - entry) * qty
                net = gross - fees
                approx = f" gross≈{gross:.6f} USDT  fees≈{fees:.6f}  net≈{net:.6f}"
        except Exception:
            pass
        # record the trade (always try to persist something)
        try:
            self.closed_trades.append(
                {
                    "ts": time.time(),
                    "symbol": symbol,
                    "side": side,
                    "entry": entry,
                    "exit": exitp,
                    "fees": fees,
                    "net": net,
                }
            )
        except Exception:
            pass

        logging.info(
            "[%s] round-trip closed via %s. fees=%s USDT.%s",
            symbol,
            exit_kind,
            f"{fees:.6f}",
            approx,
        )
        telegram.bybit_alert(
            msg=f"Round-trip closed for {symbol}. {exit_kind}. fees={fees:.6f}. {approx}"
        )
        try:
            self._accumulate_daily(symbol, exit_kind, gross, fees, net)
        except Exception:
            pass
        # at end of _log_round_trip()
        try:
            # find the signal to get SL for R-multiple
            sig_sl = None
            qty = 0.0
            for pos in list(self.book.values()) + list(self.pending.values()):
                if pos.signal.symbol == symbol:
                    sig_sl = float(pos.signal.sl)
                    qty = float(getattr(pos, "qty", 0.0) or 0.0)
                    break
            risk = abs((entry or 0.0) - (sig_sl or 0.0)) * (qty or 0.0)
            r_mult = (net / risk) if risk else 0.0

            _append_csv(
                "trades.csv",
                {
                    "ts_close": _now(),
                    "symbol": symbol,
                    "side": side,
                    "entry": entry,
                    "exit": exitp,
                    "fees": fees,
                    "net": net,
                    "gross": gross,
                    "exit_kind": exit_kind,
                    "risk": risk,
                    "r": r_mult,
                },
                [
                    "ts_close",
                    "symbol",
                    "side",
                    "entry",
                    "exit",
                    "fees",
                    "net",
                    "gross",
                    "exit_kind",
                    "risk",
                    "r",
                ],
            )
        except Exception:
            pass

    ## Garbage collection for pending orders
    async def _gc_pending(self):
        while True:
            now = time.time()
            for key, ts in list(self._pending_ts.items()):
                if now - ts <= self.PENDING_TTL_SEC:
                    continue

                pos = self.pending.get(key)
                if not pos:
                    self._pending_ts.pop(key, None)
                    continue

                symbol = pos.signal.symbol

                # 1) check live position first (did we actually fill?)
                open_size = 0.0
                avg_px = 0.0
                try:
                    resp = await asyncio.to_thread(
                        self.http.get_positions, category="linear", symbol=symbol
                    )
                    items = resp.get("result", {}).get("list", []) or []
                    for it in items:
                        if it.get("symbol") == symbol:
                            open_size = self._safe_float(it.get("size", 0) or 0)
                            avg_px = self._safe_float(
                                it.get("avgPrice") or it.get("entryPrice") or 0
                            )
                            break
                except Exception as e:
                    logging.warning("position check before cancel failed: %s", e)

                if abs(open_size) > 0:
                    # Treat as filled → promote to book and reseed brackets
                    self.book[key] = self.pending.pop(key)
                    self._pending_ts.pop(key, None)
                    self._entry_price[symbol] = (
                        self._entry_price.get(symbol, 0.0) or avg_px
                    )
                    self._side[symbol] = pos.signal.side
                    self._reset_accumulators(symbol)
                    await self._reseed_bracket_after_fill(symbol)
                    logging.info(
                        "[%s] pending→book via GC (size=%s).", symbol, open_size
                    )
                    continue

                # 2) still no position — try to cancel
                try:
                    await asyncio.to_thread(
                        self.http.cancel_order,
                        category="linear",
                        symbol=symbol,
                        orderId=pos.order_id,
                    )
                    logging.info(
                        "[%s] pending expired → canceled %s", symbol, pos.order_id
                    )
                except Exception as e:
                    # 110001 = already filled/canceled → treat as benign
                    if "110001" in str(e):
                        logging.info("[%s] pending cancel benign (110001).", symbol)
                    else:
                        logging.warning("[%s] cancel pending failed: %s", symbol, e)
                finally:
                    self.pending.pop(key, None)
                    self._pending_ts.pop(key, None)

            await asyncio.sleep(5)

    # helpers
    # in RiskRouter
    def pending_count(self) -> int:
        return len(self.pending)

    def pending_count_side(self, side: str) -> int:
        s = side.lower()
        return sum(1 for p in self.pending.values() if p.signal.side.lower() == s)

    def pending_risk_pct(self) -> float:
        if self.equity <= 0:
            return 1.0
        risk = 0.0
        for p in self.pending.values():
            s = p.signal
            risk += abs(float(s.entry) - float(s.sl)) * float(
                getattr(p, "qty", 0.0) or 0.0
            )
        return risk / self.equity

    def risk_usd_per_trade(self) -> float:
        return self.equity * float(getattr(config, "RISK_PCT", 0.1))

    def _key_by_oid(self, oid: str) -> Optional[str]:
        for k, pos in self.book.items():
            if pos.order_id == oid:
                return k
        return None

    def _clear_symbol(self, symbol: str, *, reason: str) -> None:
        for k in list(self.book):
            if self.book[k].signal.symbol == symbol:
                self._oid_to_key = {
                    oid: kk for oid, kk in self._oid_to_key.items() if kk != k
                }
                self.book.pop(k, None)
                logging.info("[%s] cleared local book (%s).", symbol, reason)
                telegram.bybit_alert(
                    msg=f"Position flat (poll); cleared local book for {symbol} for reason: {reason}"
                )
        # reset diagnostics
        self._reset_accumulators(symbol)
        self._entry_price.pop(symbol, None)
        self._side.pop(symbol, None)

    # ───────────────────────── convenience: fee lookup ──────────────────────
    # pretty printers
    async def format_recent_fees(self, minutes: int = 180) -> str:
        # reuse REST fetch but aggregate to text instead of TG spam
        try:
            params = {"category": "linear", "limit": 200}
            resp = await asyncio.to_thread(self.http.get_executions, **params)
            rows = resp.get("result", {}).get("list", []) or []
            cut = time.time() - minutes * 60
            agg = {}
            for r in rows:
                ts = float(r.get("execTime", 0)) / 1000.0
                if ts < cut:
                    continue
                sym = r.get("symbol", "")
                fee = abs(float(r.get("execFee", 0) or 0))
                agg[sym] = agg.get(sym, 0.0) + fee
            if not agg:
                return ""
            lines = [f"Fees last {minutes} min:"]
            for sym, tot in sorted(agg.items()):
                lines.append(f"• {sym}: {tot:.6f} USDT")
            return "\n".join(lines)
        except Exception as e:
            logging.warning("format_recent_fees failed: %s", e)
            return "Could not fetch recent fees."

    async def format_open_positions(self) -> str:
        if not self.book:
            return ""
        lines = ["Open positions:"]
        for pos in self.book.values():
            s = pos.signal
            entry = self._entry_price.get(s.symbol, 0.0) or float(s.entry)
            risk_usd = abs(entry - float(s.sl)) * float(pos.qty)
            lines.append(
                f"• {s.symbol} {s.side} qty={pos.qty:.6f} entry≈{entry:.6f} "
                f"SL={float(s.sl):.6f} TP={float(s.tp):.6f} risk≈{risk_usd:.2f} USDT"
            )
        return "\n".join(lines)

    def format_past_trades(self, n: int = 10, day: Optional[str] = None) -> str:
        items = list(self.closed_trades)
        if day:
            from datetime import datetime

            y, m, d = map(int, day.split("-"))
            start = datetime(y, m, d).timestamp()
            end = start + 86400
            items = [t for t in items if start <= t["ts"] < end]
        else:
            items = items[-n:]
        if not items:
            return ""
        wins = sum(1 for t in items if t["net"] > 0)
        total = sum(t["net"] for t in items)
        lines = [
            f"Trades ({'day '+day if day else f'last {len(items)}'}):  "
            f"wins={wins}/{len(items)} | PnL={total:.2f} USDT"
        ]
        for t in items:
            ts = time.strftime("%m-%d %H:%M", time.gmtime(t["ts"]))
            lines.append(
                f"• {ts} {t['symbol']} {t['side']} net={t['net']:.2f} fees={t['fees']:.4f}"
            )
        return "\n".join(lines)

    # ─────────────────────────── graceful shutdown ──────────────────────────
    async def close(self):
        await asyncio.to_thread(self.http.close)
        self.private_ws.exit()

    def has_open(self, symbol: str) -> bool:
        return any(pos.signal.symbol == symbol for pos in self.book.values())

    def open_for_key(self, key: str) -> bool:
        return key in self.book

    def _accumulate_daily(
        self, symbol: str, exit_kind: str, gross: float, fees: float, net: float
    ) -> None:
        day = datetime.utcfromtimestamp(time.time()).date().isoformat()
        d = self._daily[day]
        b = d["by_symbol"][symbol]

        d["trades"] += 1
        b["trades"] += 1

        is_tp = exit_kind.lower() == "takeprofit"
        is_sl = exit_kind.lower() == "stoploss"

        if is_tp:
            d["tp"] += 1
            b["tp"] += 1
        if is_sl:
            d["sl"] += 1
            b["sl"] += 1

        if net >= 0:
            d["wins"] += 1
            d["pos"] += net
        else:
            d["losses"] += 1
            d["neg"] += net

        d["gross"] += gross
        d["fees"] += fees
        d["net"] += net
        b["gross"] += gross
        b["fees"] += fees
        b["net"] += net

    async def telegram_daily_summary(self, day: Optional[str] = None) -> None:
        if day is None:
            day = datetime.utcfromtimestamp(time.time()).date().isoformat()

        d = self._daily.get(day)
        if not d:
            telegram.bybit_alert(msg=f"[SUMMARY {day}] No trades.")
            return

        trades = d["trades"]
        wins = d["wins"]
        losses = d["losses"]
        wr = (wins / trades * 100.0) if trades else 0.0
        pf = "∞" if d["neg"] == 0 else f"{d['pos'] / abs(d['neg']):.2f}"

        lines = [
            f"*Daily Summary* `{day}`",
            f"Trades: `{trades}` | Wins: `{wins}` | Losses: `{losses}` | Win%: `{wr:.1f}%`",
            f"Gross: `{d['gross']:.4f}`  Fees: `{d['fees']:.4f}`  Net: `{d['net']:.4f}`",
            f"Profit Factor: `{pf}`",
        ]

        # top 5 symbols by abs net
        bysym = d["by_symbol"]
        top = sorted(bysym.items(), key=lambda kv: abs(kv[1]["net"]), reverse=True)[:5]
        if top:
            lines.append("Top (by |net|):")
            for sym, x in top:
                lines.append(
                    f"• `{sym}`  t:{x['trades']}  TP:{x['tp']} SL:{x['sl']}  net:`{x['net']:.4f}`"
                )

        try:
            telegram.bybit_alert("\n".join(lines))
        except Exception as e:
            logging.warning("summary telegram send failed: %s", e)

    async def maybe_trail(self, symbol: str, bar) -> None:
        if not getattr(config, "TRAIL_ENABLE", True):
            return
        # find open position for this symbol
        pos = next(
            (
                p
                for p in self.book.values()
                if p.signal.symbol == symbol and getattr(p, "is_open", True)
            ),
            None,
        )
        if not pos:
            return

        side = pos.signal.side.lower()  # "buy" or "sell"
        entry = float(self._entry_price.get(symbol, float(pos.signal.entry)))
        atr = float(getattr(bar, "atr", 0.0)) or 0.0
        if atr <= 0:
            return

        stop_off = float(pos.meta.get("stop_off", 0.0))
        r_gain = (
            (float(bar.c) - entry) if side == "buy" else (entry - float(bar.c))
        ) / stop_off
        tick = self._tick_size[symbol]  # <-- bug fix

        # 1) move to BE once in profit by BE_ARM_R
        if not pos.meta.get("be_armed", False) and r_gain >= BE_ARM_R:
            be = entry  # (optionally add a tick to cover fees)
            await self.amend_stop(
                symbol, be
            )  # your router already has amend/cancel helpers
            pos.meta["be_armed"] = True
            pos.meta["trail_on"] = True
            if REMOVE_TP_WHEN_TRAIL:
                await self.cancel_tp(symbol)
            return

        # 2) ATR trailing once armed
        if pos.meta.get("trail_on", False):
            if side == "buy":
                new_sl = max(float(pos.signal.sl), float(bar.c) - ATR_TRAIL_K * atr)
                tighten = new_sl > float(pos.signal.sl) + tick
            else:
                new_sl = min(float(pos.signal.sl), float(bar.c) + ATR_TRAIL_K * atr)
                tighten = new_sl < float(pos.signal.sl) - tick

            if tighten:
                await self.amend_stop(symbol, new_sl)
