# risk_router.py
from __future__ import annotations
from decimal import Decimal, ROUND_DOWN

import asyncio
import time
import logging
import math
from collections import defaultdict
from typing import Dict, Optional, Tuple

from bot.infra import Signal, Position, get_client, get_private_ws
from bot.helpers import config, telegram


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

        asyncio.create_task(self._track_fills())

    # --- portfolio caps (read from config with sensible defaults)

    @property
    def max_open_concurrent(self) -> int:
        return int(getattr(config, "MAX_OPEN_CONCURRENT", 5))

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

    # ───────────────────────────── public API ───────────────────────────────
    async def handle(self, sig: Signal) -> None:
        async with self.lock:
            if sig.key in self.book:
                logging.info("%s already active – ignoring dup", sig.key)
                return

        # per-pair rule stays: only 1 open per symbol
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

        # size the order first so we know *its* risk
        qty_risk, qty_budget, qty = await self._size_with_budget(sig)
        if qty <= 0:
            logging.info(
                "[%s] Skip: qty <= 0 (risk %.6f, budget %.6f)",
                sig.symbol,
                qty_risk,
                qty_budget,
            )
            return

        # compute proposed risk for THIS order
        proposed_risk = abs(float(sig.entry) - float(sig.sl)) * float(qty)
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
        self.book[sig.key] = Position(sig, oid, qty)
        self._oid_to_key[oid] = sig.key

        logging.info(
            "[%s] order placed id=%s qty=%.6f | router: open=%d risk=%.1f%%",
            sig.symbol,
            oid,
            qty,
            self.open_count(),
            self.open_risk_pct() * 100.0,
        )

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
                logging.warning(
                    "[%s] set_leverage failed: %s (continuing; may use 1x)", symbol, e
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

        avail = await self._get_available_usdt()
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
            "[SIZER %s] px=%.6f stop=%.6f risk_usd=%.2f avail=%.4f lev=%.2f "
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
                        # Entry leg filled
                        self._entry_price[symbol] = self._safe_float(
                            row.get("avgPrice") or row.get("price"), 0.0
                        )
                        self._side[symbol] = row.get("side", self._side.get(symbol, ""))
                        self._reset_accumulators(symbol)

                elif status in ("Cancelled", "Rejected"):
                    logging.warning("[%s] order %s %s", symbol, oid, status)
                    telegram.bybit_alert(msg=f"Order {oid} {status} for {symbol}")
                    self.book.pop(key, None)
                    self._oid_to_key.pop(oid, None)

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
                except Exception:
                    pass

    async def _poll_positions(self):
        """Fallback when position stream is unavailable."""
        while True:
            try:
                resp = await asyncio.to_thread(
                    self.http.get_positions, category="linear"
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
        approx = ""
        try:
            if (
                entry > 0
                and exitp > 0
                and symbol in (pos.signal.symbol for pos in self.book.values())
            ):
                # derive qty from our local book for this symbol
                qty = 0.0
                for pos in self.book.values():
                    if pos.signal.symbol == symbol:
                        qty = float(pos.qty if hasattr(pos, "qty") else 0.0) or 0.0
                        break
                # gross PnL estimate using last known fill prices
                direction = 1 if side == "buy" else -1
                gross = direction * (exitp - entry) * qty
                net = gross - fees
                approx = f" gross≈{gross:.6f} USDT  fees≈{fees:.6f}  net≈{net:.6f}"
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

    # helpers
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
    async def print_recent_fees(
        self, symbol: Optional[str] = None, minutes: int = 180
    ) -> None:
        """
        Pull recent executions from Bybit REST and print total fees per symbol.
        """
        try:
            # v5 execution list
            params = {
                "category": "linear",
                "limit": 200,
            }
            if symbol:
                params["symbol"] = symbol
            resp = await asyncio.to_thread(self.http.get_executions, **params)
            rows = resp.get("result", {}).get("list", []) or []
            cutoff = time.time() - minutes * 60
            agg: Dict[str, float] = defaultdict(float)
            for r in rows:
                ts = (
                    self._safe_float(r.get("execTime"), 0.0) / 1000.0
                )  # ms -> s if present
                if ts and ts < cutoff:
                    continue
                sym = r.get("symbol", "")
                fee = abs(self._safe_float(r.get("execFee"), 0.0))
                agg[sym] += fee
            if not agg:
                logging.info("No executions (fees) in the last %d minutes.", minutes)
            else:
                for sym, total in agg.items():
                    logging.info(
                        "[FEES %s] last %d min: %.6f USDT", sym, minutes, total
                    )
                    telegram.bybit_alert(
                        msg=f"[FEES {sym}] last {minutes} min: {total:.6f} USDT"
                    )
        except Exception as e:
            logging.warning("print_recent_fees failed: %s", e)

    # ─────────────────────────── graceful shutdown ──────────────────────────
    async def close(self):
        await asyncio.to_thread(self.http.close)
        self.private_ws.exit()

    def has_open(self, symbol: str) -> bool:
        return any(pos.signal.symbol == symbol for pos in self.book.values())

    def open_for_key(self, key: str) -> bool:
        return key in self.book
