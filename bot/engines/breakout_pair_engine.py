#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  BREAKOUT LIVE ENGINE - ETH + SOL (1H Timeframe)
# ────────────────────────────────────────────────────────────────
"""
Live trading engine for BREAKOUT strategy on 1H timeframe.

Optimized for ETH + SOL based on 100-day backtest results:
- ETHUSDT: 53.3% WR, 2.61 PF, +$3,600
- SOLUSDT: 38.5% WR, 1.45 PF, +$2,273

Key differences from mean-reversion engine:
1. Uses 1H timeframe (not 15m)
2. Uses breakout_long_signal / breakout_short_signal (not TJR)
3. Momentum checks are FLIPPED (don't buy overbought)
4. Trades 24/7 (no session restrictions)
"""
import os, json, asyncio, logging, time, traceback
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
import ccxt.async_support as ccxt
import pandas as pd
import websockets
from asyncio import Queue
from pathlib import Path

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from bot.infra.models import Signal, Position
from bot.engines.risk_router import RiskRouter, audit_and_override_ticks
from bot.helpers import (
    breakout_config,  # ← CRITICAL: Use breakout_config, not config
    compute_indicators,
    build_htf_levels,
    update_htf_levels_new,
    breakout_long_signal,   # ← BREAKOUT signals
    breakout_short_signal,  # ← BREAKOUT signals
    telegram,
    build_h1,
    update_h1,
    in_good_hours,
    veto_thresholds,
    near_htf_level,
    append_csv,
    hours,
)
from bot.data import preload_history
from bot.commands import run_command_bot
from collections import deque, defaultdict
from bot.infra.bybit_client import REST

# ────────────────────────────────────────────────────────────────
#  Global constants
# ────────────────────────────────────────────────────────────────
LOG_DIR = Path(getattr(breakout_config, "LOG_DIR", "./logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Cooldowns
COOLDOWN_DAYS_AFTER_SL = getattr(breakout_config, "COOLDOWN_DAYS_AFTER_SL", 0.5)
COOLDOWN_DAYS_AFTER_TP = getattr(breakout_config, "COOLDOWN_DAYS_AFTER_TP", 0.25)
COOLDOWN_SEC = COOLDOWN_DAYS_AFTER_SL * 86400

# Session hours
def _union_hours(sw: dict[str, tuple[int, int]]) -> set[int]:
    s = set()
    for a, b in sw.values():
        s |= set(range(a, b))
    return s

GOOD_HOURS = _union_hours(breakout_config.SESSION_WINDOWS)
SESSION_GATING = len(GOOD_HOURS) < 24

# ────────────────────────────────────────────────────────────────
#  Bybit + runtime constants
# ────────────────────────────────────────────────────────────────
WS_URL = "wss://stream.bybit.com/v5/public/linear"
PING_SEC = 20
TF = breakout_config.INTERVAL  # "60" for 1H
TF_SEC = int(TF) * 60  # 3600 seconds
LOOKBACK = 1_000

MAX_RETRY = 3

# Signal queue
SIGNAL_Q: Queue = Queue(maxsize=100)
last_signal_ts: dict[str, float] = {}
recent_exec_ts = deque(maxlen=256)
daily_count = defaultdict(int)


# ────────────────────────────────────────────────────────────────
# Async helpers
# ────────────────────────────────────────────────────────────────
async def _refresh_meta(router, pairs, every_min=60):
    while True:
        await audit_and_override_ticks(router, pairs)
        await asyncio.sleep(every_min * 60)


# ────────────────────────────────────────────────────────────────
#  Web-socket coroutine per pair
# ────────────────────────────────────────────────────────────────
async def kline_stream(pair: str, router: RiskRouter) -> None:
    topic = f"kline.{TF}.{pair}"

    # Preload recent history
    hist = await preload_history(symbol=pair, interval=TF, limit=LOOKBACK)
    drop_stats = {
        "htf_missing": 0,
        "not_near_htf": 0,
        "off_session": 0,
        "h1_missing": 0,
        "veto_adx": 0,
        "veto_atr": 0,
        "no_ltf_long": 0,
        "no_ltf_short": 0,
    }

    htf_levels = build_htf_levels(hist.copy())
    h1 = build_h1(hist.copy())

    logging.info(
        "[%s] History pre-loaded: %d bars (%s → %s)",
        pair,
        len(hist),
        hist.index[0],
        hist.index[-1],
    )

    last_heartbeat, HEARTBEAT_SECS = time.time(), 600
    last_ts: Optional[int] = None

    while True:
        await asyncio.sleep(1)
        try:
            async with websockets.connect(
                WS_URL, ping_interval=PING_SEC, ping_timeout=PING_SEC * 2
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                logging.info("[%s] Subscribed to %s", pair, topic)

                async for raw in ws:
                    if time.time() - last_heartbeat > HEARTBEAT_SECS:
                        logging.info(
                            "[%s] Heartbeat OK – %s", pair, datetime.now(timezone.utc)
                        )
                        last_heartbeat = time.time()

                    msg = json.loads(raw)
                    if msg.get("topic") != topic:
                        continue

                    kline = msg["data"][0]
                    if not kline["confirm"]:
                        continue

                    # Fill missed candles if any
                    current_end = int(kline["end"]) // 1000
                    if last_ts is None:
                        last_ts = current_end - TF_SEC
                    expected = last_ts + TF_SEC
                    if current_end > expected:
                        missing = list(range(expected, current_end, TF_SEC))
                        logging.warning(
                            "[%s] WS gap: %d candle(s) – back-filling",
                            pair,
                            len(missing),
                        )
                        since = (missing[0] - TF_SEC) * 1000
                        for _ in range(MAX_RETRY):
                            try:
                                kl = await REST.fetch_ohlcv(
                                    pair,
                                    timeframe=f"{TF}m",
                                    since=since,
                                    limit=len(missing) + 2,
                                )
                                df_miss = pd.DataFrame(
                                    kl, columns=["ts", "o", "h", "l", "c", "v"]
                                ).set_index("ts")
                                df_miss.index = pd.to_datetime(
                                    df_miss.index, unit="ms", utc=True
                                )
                                hist = (
                                    pd.concat([hist, df_miss])
                                    .drop_duplicates()
                                    .sort_index()
                                    .tail(LOOKBACK)
                                )
                                break
                            except Exception as e:
                                logging.error("[%s] REST back-fill error: %s", pair, e)
                                await asyncio.sleep(0.5)
                    last_ts = current_end

                    # Construct the just-closed bar
                    start_ms = int(kline["start"])
                    ts_close = pd.to_datetime(
                        start_ms, unit="ms", utc=True
                    ) + pd.Timedelta(seconds=TF_SEC)

                    new = pd.DataFrame(
                        [
                            [
                                ts_close,
                                float(kline["open"]),
                                float(kline["high"]),
                                float(kline["low"]),
                                float(kline["close"]),
                                float(kline["volume"]),
                            ]
                        ],
                        columns=["ts", "o", "h", "l", "c", "v"],
                    ).set_index("ts")
                    hist = pd.concat([hist, new]).tail(LOOKBACK)
                    hist = compute_indicators(hist)

                    # Log drop stats every hour
                    if int(time.time()) % 3600 < TF_SEC:
                        logging.info("DROP_STATS %s %s", pair, drop_stats)
                        async with breakout_config.GLOBAL_DROP_STATS_LOCK:
                            for key, val in drop_stats.items():
                                breakout_config.GLOBAL_DROP_STATS[pair][key] = val

                    bar = hist.iloc[-1]
                    if bar[["atr", "atr30", "adx", "k_fast"]].isna().any():
                        continue

                    # Trail open positions
                    try:
                        await router.maybe_trail(pair, bar)
                    except Exception as e:
                        logging.warning("[%s] trail update failed: %s", pair, e)

                    # 1) HTF snapshot
                    try:
                        idx_prev = htf_levels.index.get_indexer(
                            [bar.name], method="ffill"
                        )[0]
                        if idx_prev == -1:
                            h1 = update_h1(h1, bar.name, float(bar.c))
                            htf_levels = update_htf_levels_new(htf_levels, bar)
                            drop_stats["htf_missing"] += 1
                            continue
                        htf_row = htf_levels.iloc[idx_prev]
                    except Exception:
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        drop_stats["htf_missing"] += 1
                        continue

                    # 2) Proximity to HTF levels (relaxed for breakouts)
                    if not near_htf_level(
                        bar,
                        htf_row,
                        max_atr=getattr(breakout_config, "NEAR_HTF_MAX_ATR_MOM", 2.0),
                    ):
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        drop_stats["not_near_htf"] += 1
                        continue

                    # 3) Session gate (24/7 for breakouts, but keep logic)
                    if SESSION_GATING and not in_good_hours(
                        bar.name, good_hours=GOOD_HOURS
                    ):
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        drop_stats["off_session"] += 1
                        continue

                    # 4) H1 trend row (not used for breakouts, but keep for compatibility)
                    try:
                        h1row = h1.loc[bar.name.floor("1h")]
                    except KeyError:
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        drop_stats["h1_missing"] += 1
                        continue

                    # 5) Market-quality veto
                    min_adx, atr_veto = veto_thresholds(bar)
                    min_adx = max(
                        min_adx, getattr(breakout_config, "ADX_HARD_FLOOR", 50)
                    )

                    if bar.adx < min_adx:
                        drop_stats["veto_adx"] += 1
                    if bar.atr < atr_veto * bar.atr30:
                        drop_stats["veto_atr"] += 1

                    veto = (bar.adx < min_adx or bar.atr < atr_veto * bar.atr30)
                    if veto:
                        logging.info(
                            "[%s] No-trade (veto)  k_slow=%.1f  adx=%.1f  atr=%.5f",
                            pair, bar.k_slow, bar.adx, bar.atr
                        )
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        continue

                    # 5.5) Cool-down after SL
                    last_sl = router.last_sl_ts.get(pair, 0.0)
                    if last_sl and (time.time() - last_sl) < COOLDOWN_SEC:
                        left_days = (COOLDOWN_SEC - (time.time() - last_sl)) / 86400.0
                        logging.info(
                            "[%s] Cool-down after SL active — %.1f days left",
                            pair,
                            left_days,
                        )
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        continue

                    # 5.6) Cool-down after TP
                    if COOLDOWN_DAYS_AFTER_TP > 0 and router.last_tp_ts:
                        last_tp = max(router.last_tp_ts.values())
                        if (time.time() - last_tp) < (COOLDOWN_DAYS_AFTER_TP * 86400):
                            left_days = (COOLDOWN_DAYS_AFTER_TP * 86400 - (time.time() - last_tp)) / 86400.0
                            logging.info(
                                "[%s] Cool-down after TP active — %.2f days left",
                                pair,
                                left_days,
                            )
                            h1 = update_h1(h1, bar.name, float(bar.c))
                            htf_levels = update_htf_levels_new(htf_levels, bar)
                            continue

                    # SL/TP distances
                    cushion = 1.3 if bar.adx >= 30 else 1.5
                    sl_base = cushion * bar.atr
                    stop_off = (
                        cushion * sl_base + breakout_config.WICK_BUFFER * bar.atr
                    )
                    tp_dist = breakout_config.RR_TARGET * stop_off

                    i = len(hist) - 1
                    header = "BREAKOUT Engine (1H)"

                    # Decision log
                    decision_log = {
                        "ts": bar.name.isoformat(),
                        "symbol": pair,
                        "htf_ok": near_htf_level(bar, htf_row, max_atr=2.0),
                        "session_ok": in_good_hours(bar.name, GOOD_HOURS),
                        "adx": float(bar.adx),
                        "adx_ok": float(bar.adx) >= min_adx,
                        "atr_ratio": float(bar.atr / bar.atr30),
                        "atr_ok": float(bar.atr) >= atr_veto * float(bar.atr30),
                        "k_slow": float(bar.k_slow),
                        "min_adx": min_adx,
                    }
                    append_csv("decisions_breakout.csv", decision_log, list(decision_log.keys()), log_dir=LOG_DIR)

                    # ═══════════════════════════════════════════════════════
                    # BREAKOUT SIGNAL CHECKS
                    # ═══════════════════════════════════════════════════════
                    
                    # Get momentum thresholds
                    k_long_max = getattr(breakout_config, "MOMENTUM_STO_K_LONG_MAX", 70)
                    k_short_min = getattr(breakout_config, "MOMENTUM_STO_K_SHORT_MIN", 30)

                    # LONG BREAKOUT: k_slow must be < 70 (not overbought)
                    if (bar.k_slow < k_long_max
                        and breakout_long_signal(hist, i, htf_row)):

                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info(
                                "[%s] LONG BREAKOUT signal  k_slow=%.1f  adx=%.1f",
                                pair,
                                bar.k_slow,
                                bar.adx,
                            )
                            telegram.alert_side(
                                pair,
                                bar,
                                TF,
                                "LONG",
                                stop_off=stop_off,
                                tp_dist=tp_dist,
                                header=header,
                            )
                            sig = Signal(
                                pair,
                                "Buy",
                                bar.c,
                                sl=bar.c - stop_off,
                                tp=bar.c + tp_dist,
                                key=f"{pair}-{bar.name:%Y%m%d-%H%M}",
                                ts=bar.name,
                                adx=float(bar.adx),
                                k_fast=float(bar.k_fast),
                                k_slow=float(bar.k_slow),
                                d_slow=float(bar.d_slow),
                                vol=float(getattr(bar, "v", 0.0)),
                                off_sl=stop_off,
                                off_tp=tp_dist,
                            )
                            append_csv(
                                "signals_breakout.csv",
                                {
                                    "ts": sig.ts.isoformat(),
                                    "symbol": sig.symbol,
                                    "side": sig.side,
                                    "entry": float(sig.entry),
                                    "sl": float(sig.sl),
                                    "tp": float(sig.tp),
                                    "adx": sig.adx,
                                    "k_fast": sig.k_fast,
                                    "k_slow": sig.k_slow,
                                    "d_slow": sig.d_slow,
                                    "off_sl": sig.off_sl,
                                    "off_tp": sig.off_tp,
                                    "key": sig.key,
                                },
                                [
                                    "ts", "symbol", "side", "entry", "sl", "tp",
                                    "adx", "k_fast", "k_slow", "d_slow",
                                    "off_sl", "off_tp", "key",
                                ],
                                log_dir=LOG_DIR,
                            )
                            await SIGNAL_Q.put(sig)
                            last_signal_ts[pair] = time.time()

                    # SHORT BREAKOUT: k_slow must be > 30 (not oversold)
                    elif (bar.k_slow > k_short_min
                          and breakout_short_signal(hist, i, htf_row)):

                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info(
                                "[%s] SHORT BREAKOUT signal k_slow=%.1f  adx=%.1f",
                                pair,
                                bar.k_slow,
                                bar.adx,
                            )
                            telegram.alert_side(
                                pair,
                                bar,
                                TF,
                                "SHORT",
                                stop_off=stop_off,
                                tp_dist=tp_dist,
                                header=header,
                            )
                            sig = Signal(
                                pair,
                                "Sell",
                                bar.c,
                                sl=bar.c + stop_off,
                                tp=bar.c - tp_dist,
                                key=f"{pair}-{bar.name:%Y%m%d-%H%M}",
                                ts=bar.name,
                                adx=float(bar.adx),
                                k_fast=float(bar.k_fast),
                                k_slow=float(bar.k_slow),
                                d_slow=float(bar.d_slow),
                                vol=float(getattr(bar, "v", 0.0)),
                                off_sl=stop_off,
                                off_tp=tp_dist,
                            )
                            append_csv(
                                "signals_breakout.csv",
                                {
                                    "ts": sig.ts.isoformat(),
                                    "symbol": sig.symbol,
                                    "side": sig.side,
                                    "entry": float(sig.entry),
                                    "sl": float(sig.sl),
                                    "tp": float(sig.tp),
                                    "adx": sig.adx,
                                    "k_fast": sig.k_fast,
                                    "k_slow": sig.k_slow,
                                    "d_slow": sig.d_slow,
                                    "off_sl": sig.off_sl,
                                    "off_tp": sig.off_tp,
                                    "key": sig.key,
                                },
                                [
                                    "ts", "symbol", "side", "entry", "sl", "tp",
                                    "adx", "k_fast", "k_slow", "d_slow",
                                    "off_sl", "off_tp", "key",
                                ],
                                log_dir=LOG_DIR,
                            )
                            await SIGNAL_Q.put(sig)
                            last_signal_ts[pair] = time.time()
                    else:
                        drop_stats["no_ltf_long"] += 1
                        drop_stats["no_ltf_short"] += 1
                        logging.info(
                            "[%s] No-trade (no breakout signal)  k_slow=%.1f  adx=%.1f  atr=%.5f",
                            pair,
                            bar.k_slow,
                            bar.adx,
                            bar.atr,
                        )

                    # AFTER decision: update HTF/H1
                    htf_levels = update_htf_levels_new(htf_levels, bar)
                    h1 = update_h1(h1, bar.name, float(bar.c))

        except Exception as exc:
            logging.error(
                "WS stream error (%s): %s\n%s", pair, exc, traceback.format_exc()
            )
            await asyncio.sleep(5)


# ────────────────────────────────────────────────────────────────
#  Signal consumer (same as mean-reversion engine)
# ────────────────────────────────────────────────────────────────
async def consume(router: RiskRouter):
    MAX_OPEN = getattr(breakout_config, "MAX_OPEN_CONCURRENT", 2)
    MAX_RISK = getattr(breakout_config, "MAX_TOTAL_RISK_PCT", 0.30)
    MAX_PER_SIDE = getattr(breakout_config, "MAX_PER_SIDE_OPEN", 1)
    MAX_AGE = getattr(breakout_config, "MAX_SIGNAL_AGE_SEC", 60)
    COALESCE_SEC = getattr(breakout_config, "COALESCE_SEC", 5)

    def score(s: Signal) -> float:
        width = abs(float(s.tp) - float(s.sl))
        adx_w = max(0.0, min(1.0, (float(s.adx) - 12.0) / 25.0))
        ks = float(s.k_slow)
        stoch_w = (
            max(0.0, min(1.0, (100.0 - ks) / 100.0))
            if s.side.lower() == "buy"
            else max(0.0, min(1.0, ks / 100.0))
        )
        return width * (0.5 + 0.5 * adx_w) * (0.5 + 0.5 * stoch_w)

    while True:
        today = datetime.utcnow().date().isoformat()
        if daily_count[today] >= getattr(breakout_config, "MAX_TRADES_PER_DAY", 3):
            logging.info("Daily trade cap reached - waiting for next day")
            await asyncio.sleep(60)
            continue

        # Coalesce signals
        sig0 = await SIGNAL_Q.get()
        batch = [sig0]
        until = time.time() + COALESCE_SEC
        while time.time() < until:
            try:
                batch.append(SIGNAL_Q.get_nowait())
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)

        for _ in batch:
            SIGNAL_Q.task_done()

        # Stale filter
        now_utc = pd.Timestamp.now("UTC")
        fresh = []
        for s in batch:
            ts = pd.Timestamp(s.ts)
            sig_ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
            age = (now_utc - sig_ts).total_seconds()
            if age <= MAX_AGE:
                fresh.append(s)
            else:
                logging.info("[%s] Drop stale signal age=%.1fs", s.symbol, age)

        if not fresh:
            continue

        # Dedupe per symbol
        dedup = {}
        for s in fresh:
            dedup[s.symbol] = s
        fresh = list(dedup.values())

        # Strongest first
        fresh.sort(key=score, reverse=True)

        # Portfolio snapshot
        open_used = router.open_count() + router.pending_count()
        buy_used = router.open_count_side("Buy") + router.pending_count_side("Buy")
        sell_used = router.open_count_side("Sell") + router.pending_count_side("Sell")
        risk_used = router.open_risk_pct() + router.pending_risk_pct()

        busy_clusters = {
            breakout_config.CLUSTER.get(p.signal.symbol)
            for p in list(router.book.values()) + list(router.pending.values())
            if breakout_config.CLUSTER.get(p.signal.symbol)
        }

        for s in fresh:
            side_norm = "Buy" if s.side.lower() == "buy" else "Sell"
            cid = breakout_config.CLUSTER.get(s.symbol)

            # Hard caps
            if open_used >= MAX_OPEN:
                logging.info("[PORTFOLIO] Concurrency full — drop %s", s.symbol)
                continue
            if risk_used >= MAX_RISK:
                logging.info("[PORTFOLIO] Risk full — drop %s", s.symbol)
                continue
            if side_norm == "Buy" and buy_used >= MAX_PER_SIDE:
                logging.info("[PORTFOLIO] Long-side full — drop %s", s.symbol)
                continue
            if side_norm == "Sell" and sell_used >= MAX_PER_SIDE:
                logging.info("[PORTFOLIO] Short-side full — drop %s", s.symbol)
                continue
            if cid and cid in busy_clusters:
                logging.info("[PORTFOLIO] Cluster busy (%s) — drop %s", cid, s.symbol)
                continue

            # Reserve capacity
            open_used += 1
            if side_norm == "Buy":
                buy_used += 1
            else:
                sell_used += 1
            if cid:
                busy_clusters.add(cid)

            try:
                await router.handle(s)
                daily_count[today] += 1
                logging.info(
                    "Processed: %s | daily=%d/%d | open=%d risk≈%.1f%%",
                    s.symbol,
                    daily_count[today],
                    getattr(breakout_config, "MAX_TRADES_PER_DAY", 3),
                    open_used,
                    risk_used * 100,
                )
            except Exception as e:
                logging.error("Router error for %s: %s", s.symbol, e, exc_info=True)
                open_used -= 1
                if side_norm == "Buy":
                    buy_used -= 1
                else:
                    sell_used -= 1
                if cid:
                    busy_clusters.discard(cid)
                continue


# ────────────────────────────────────────────────────────────────
#  Main
# ────────────────────────────────────────────────────────────────
async def main():
    router = RiskRouter(equity_usd=20, testnet=False)
    pairs = getattr(breakout_config, "PAIRS_LRS", ["ETHUSDT", "DOTUSDT", "ADAUSDT"])
    
    logging.info("="*80)
    logging.info("BREAKOUT LIVE ENGINE - 1H Timeframe")
    logging.info("Pairs: %s", ", ".join(pairs))
    logging.info("Config: ADX_FLOOR=%d, RR_TARGET=%.1f",
                 breakout_config.ADX_HARD_FLOOR,
                 breakout_config.RR_TARGET)
    logging.info("="*80)
    
    await audit_and_override_ticks(router, pairs)

    streams = [asyncio.create_task(kline_stream(p, router)) for p in pairs]
    streams.append(asyncio.create_task(consume(router)))
    streams.append(asyncio.create_task(_refresh_meta(router, pairs)))
    
    run_command_bot(router, breakout_config)
    await asyncio.gather(*streams)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s – %(levelname)s – %(message)s"
    )
    
    # Silence noisy loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)  # ← Fixes ping spam
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    
    asyncio.run(main())