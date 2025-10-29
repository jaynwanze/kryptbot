#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  FVG ORDER FLOW LIVE ENGINE (15m)
# ────────────────────────────────────────────────────────────────
"""
Live trading engine for FVG ORDER FLOW strategy on 15 timeframe.

Key Features:
- Fair Value Gap (FVG) detection and tracking
- Order Flow momentum scoring
- Volume Profile analysis (POC/LVN targeting)
- Trails winners, cuts losses

Strategy:
- LONG: Bullish FVG + positive Order Flow + high volume + near value area
- SHORT: Bearish FVG + negative Order Flow + high volume + near value area
"""
import os, json, asyncio, logging, time, traceback
from datetime import datetime, timezone
from typing import Optional, List
import pandas as pd
import websockets
from asyncio import Queue
from pathlib import Path

import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from bot.infra.models import FvgOrderFlowPosition, FvgOrderFlowSignal, Signal, Position
from bot.engines.risk_router import RiskRouter, audit_and_override_ticks
from bot.helpers import (
    fvg_orderflow_config,  # ← FVG config
    compute_indicators,
    fvg_long_signal,
    fvg_short_signal,
    telegram,
    in_good_hours,
    append_csv,
    calculate_volume_profile,
    detect_fvg_bullish,
    detect_fvg_bearish,
)
from bot.data import preload_history
from bot.commands import run_command_bot
from collections import deque, defaultdict
from bot.infra.bybit_client import REST

# ────────────────────────────────────────────────────────────────
#  Global constants
# ────────────────────────────────────────────────────────────────
LOG_DIR = Path(getattr(fvg_orderflow_config, "LOG_DIR", "./logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Cooldowns
COOLDOWN_DAYS_AFTER_SL = getattr(fvg_orderflow_config, "COOLDOWN_DAYS_AFTER_SL", 0.5)
COOLDOWN_DAYS_AFTER_TP = getattr(fvg_orderflow_config, "COOLDOWN_DAYS_AFTER_TP", 0.25)
COOLDOWN_SEC = COOLDOWN_DAYS_AFTER_SL * 86400


# Session hours
def _union_hours(sw: dict[str, tuple[int, int]]) -> set[int]:
    s = set()
    for a, b in sw.values():
        s |= set(range(a, b))
    return s


SESSION_WINDOWS = getattr(
    fvg_orderflow_config,
    "SESSION_WINDOWS",
    {
        "ASIA": (0, 8),
        "LONDON": (8, 16),
        "NY": (13, 22),
    },
)
GOOD_HOURS = _union_hours(SESSION_WINDOWS)
SESSION_GATING = len(GOOD_HOURS) < 24

# ────────────────────────────────────────────────────────────────
#  Bybit + runtime constants
# ────────────────────────────────────────────────────────────────
WS_URL = "wss://stream.bybit.com/v5/public/linear"
PING_SEC = 20
TF = fvg_orderflow_config.INTERVAL  # "60" for 1H
TF_SEC = int(TF) * 60  # 3600 seconds
LOOKBACK = 1_000

MAX_RETRY = 3

# Signal queue
SIGNAL_Q: Queue = Queue(maxsize=100)
last_signal_ts: dict[str, float] = {}
recent_exec_ts = deque(maxlen=256)
daily_count = defaultdict(int)
fvgs: dict[str, list] = {}


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
    # Initialize per-pair FVG list
    if pair not in fvgs:
        fvgs[pair] = []

    signals_checked = 0
    phase3_pass = 0

    # Preload recent history
    hist = await preload_history(symbol=pair, interval=TF, limit=LOOKBACK)
    drop_stats = {
        "off_session": 0,
        "no_fvg_long": 0,
        "no_fvg_short": 0,
    }

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
                        async with fvg_orderflow_config.GLOBAL_DROP_STATS_LOCK:
                            for key, val in drop_stats.items():
                                fvg_orderflow_config.GLOBAL_DROP_STATS[pair][key] = val

                    bar = hist.iloc[-1]
                    if bar[["atr", "atr30", "adx"]].isna().any():
                        continue

                    # 3) Session gate (24/7 for crypto usually)
                    if SESSION_GATING and not in_good_hours(
                        bar.name, good_hours=GOOD_HOURS
                    ):
                        continue

                    # # 5.5) Cool-down after SL
                    last_sl = router.last_sl_ts.get(pair, 0.0)
                    if last_sl and (time.time() - last_sl) < COOLDOWN_SEC:
                        left_days = (COOLDOWN_SEC - (time.time() - last_sl)) / 86400.0
                        logging.info(
                            "[%s] Cool-down after SL active — %.1f days left",
                            pair,
                            left_days,
                        )
                        continue

                    # # 5.6) Cool-down after TP
                    if COOLDOWN_DAYS_AFTER_TP > 0 and router.last_tp_ts:
                        last_tp = max(router.last_tp_ts.values())
                        if (time.time() - last_tp) < (COOLDOWN_DAYS_AFTER_TP * 86400):
                            left_days = (
                                COOLDOWN_DAYS_AFTER_TP * 86400 - (time.time() - last_tp)
                            ) / 86400.0
                            logging.info(
                                "[%s] Cool-down after TP active — %.2f days left",
                                pair,
                                left_days,
                            )
                            continue

                    # SL/TP distances
                    i = len(hist) - 1
                    header = "FVG Order Flow Engine (1H)"

                    # Decision log
                    decision_log = {
                        "ts": bar.name.isoformat(),
                        "symbol": pair,
                        "session_ok": in_good_hours(bar.name, GOOD_HOURS),
                        "adx": float(bar.adx),
                        "atr_ratio": float(bar.atr / bar.atr30),
                        "price": float(bar.c),
                        "volume": float(bar.v),
                    }
                    append_csv(
                        "decisions_fvg.csv",
                        decision_log,
                        list(decision_log.keys()),
                        log_dir=LOG_DIR,
                    )

                    # ═══════════════════════════════════════════════════════
                    # FVG DETECTION AND FILTERING
                    # ═══════════════════════════════════════════════════════
                    bull_fvg = detect_fvg_bullish(hist, i)
                    if bull_fvg:
                        fvgs[pair].append(bull_fvg)
                        logging.debug("[%s] Bullish FVG detected @ %.5f", pair, bar.c)

                    bear_fvg = detect_fvg_bearish(hist, i)
                    if bear_fvg:
                        fvgs[pair].append(bear_fvg)
                        logging.debug("[%s] Bearish FVG detected @ %.5f", pair, bar.c)
                    fvgs[pair] = [
                        f
                        for f in fvgs[pair]
                        if (bar.name - f.timestamp).total_seconds() / 3600 < 24
                    ]

                    # Calculate Volume Profile BEFORE signal checks
                    vp = calculate_volume_profile(
                        hist.iloc[: i + 1], fvg_orderflow_config.VP_PERIOD
                    )
                    if vp is None:
                        continue

                    # Now pass it to signals
                    fvg_long = fvg_long_signal(hist, i, fvgs[pair], vp)
                    fvg_short = fvg_short_signal(hist, i, fvgs[pair], vp)
                    # ═══════════════════════════════════════════════════════
                    # FVG ORDER FLOW SIGNAL CHECKS
                    # ═══════════════════════════════════════════════════════
                    # LONG: Bullish FVG + positive Order Flow + quality filters
                    if fvg_long:
                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info(
                                "[%s] LONG FVG signal  OF_score=%.0f | price=%.5f | adx=%.1f",
                                pair,
                                fvg_long.of_score,
                                bar.c,
                                bar.adx,
                            )
                            risk_usd = (
                                fvg_orderflow_config.EQUITY
                                * fvg_orderflow_config.RISK_PCT
                            )
                            stop_off = fvg_long.off_sl
                            qty = risk_usd / stop_off if stop_off > 0 else 0

                            if qty <= 0:
                                logging.warning(
                                    "[%s] Skipping LONG - invalid qty (stop_off=%.5f)",
                                    pair,
                                    stop_off,
                                )
                                continue

                            phase3_pass += 1
                            telegram.alert_side_fvg_orderflow_signal(
                                pair,
                                bar,
                                TF,
                                "LONG",
                                stop_off=fvg_long.off_sl,
                                tp_dist=fvg_long.off_tp1,
                                tp2_dist=fvg_long.off_tp2,
                                header=header,
                            )

                            append_csv(
                                "signals_fvg.csv",
                                {
                                    "ts": fvg_long.ts.isoformat(),
                                    "symbol": fvg_long.symbol,
                                    "side": fvg_long.side,
                                    "entry": float(fvg_long.entry),
                                    "sl": float(fvg_long.sl),
                                    "tp1": float(fvg_long.tp1),
                                    "tp2": float(fvg_long.tp2),
                                    "adx": fvg_long.adx,
                                    "of_score": fvg_long.of_score,
                                    "level_type": fvg_long.level_type,
                                    "narrative": fvg_long.narrative,
                                    "off_sl": fvg_long.off_sl,
                                    "off_tp1": fvg_long.off_tp1,
                                    "off_tp2": fvg_long.off_tp2,
                                    "key": fvg_long.key,
                                },
                                [
                                    "ts",
                                    "symbol",
                                    "side",
                                    "entry",
                                    "sl",
                                    "tp1",
                                    "tp2",
                                    "adx",
                                    "of_score",
                                    "level_type",
                                    "narrative",
                                    "off_sl",
                                    "off_tp1",
                                    "off_tp2",
                                    "key",
                                ],
                                log_dir=LOG_DIR,
                            )

                            await SIGNAL_Q.put(fvg_long)
                            last_signal_ts[pair] = time.time()

                    # SHORT: Bearish FVG + negative Order Flow + quality filters
                    else:
                        if fvg_short:
                            if router.has_open(pair):
                                logging.info("[%s] No-trade (already open)", pair)
                            else:
                                logging.info(
                                    "[%s] SHORT FVG signal  OF_score=%.0f | price=%.5f | adx=%.1f",
                                    pair,
                                    fvg_short.of_score,
                                    bar.c,
                                    bar.adx,
                                )
                                risk_usd = (
                                    fvg_orderflow_config.EQUITY
                                    * fvg_orderflow_config.RISK_PCT
                                )
                                stop_off = fvg_short.off_sl
                                qty = risk_usd / stop_off if stop_off > 0 else 0

                                if qty <= 0:
                                    logging.warning(
                                        "[%s] Skipping LONG - invalid qty (stop_off=%.5f)",
                                        pair,
                                        stop_off,
                                    )
                                    continue

                                phase3_pass += 1
                                telegram.alert_side_fvg_orderflow_signal(
                                    pair,
                                    bar,
                                    TF,
                                    "SHORT",
                                    stop_off=fvg_short.off_sl,
                                    tp_dist=fvg_short.off_tp1,
                                    tp2_dist=fvg_short.off_tp2,
                                    header=header,
                                )

                                append_csv(
                                    "signals_fvg.csv",
                                    {
                                        "ts": fvg_short.ts.isoformat(),
                                        "symbol": fvg_short.symbol,
                                        "side": fvg_short.side,
                                        "entry": float(fvg_short.entry),
                                        "sl": float(fvg_short.sl),
                                        "tp1": float(fvg_short.tp1),
                                        "tp2": float(fvg_short.tp2),
                                        "adx": fvg_short.adx,
                                        "of_score": fvg_short.of_score,
                                        "level_type": fvg_short.level_type,
                                        "narrative": fvg_short.narrative,
                                        "off_sl": fvg_short.off_sl,
                                        "off_tp1": fvg_short.off_tp1,
                                        "off_tp2": fvg_short.off_tp2,
                                        "key": fvg_short.key,
                                    },
                                    [
                                        "ts",
                                        "symbol",
                                        "side",
                                        "entry",
                                        "sl",
                                        "tp1",
                                        "tp2",
                                        "adx",
                                        "of_score",
                                        "level_type",
                                        "narrative",
                                        "off_sl",
                                        "off_tp1",
                                        "off_tp2",
                                        "key",
                                    ],
                                    log_dir=LOG_DIR,
                                )
                                await SIGNAL_Q.put(fvg_short)
                                last_signal_ts[pair] = time.time()
                        else:
                            drop_stats["no_fvg_long"] += 1
                            drop_stats["no_fvg_short"] += 1
                            logging.info(
                                "[%s] No-trade (no FVG signal)  price=%.5f | adx=%.1f  atr=%.5f",
                                pair,
                                bar.c,
                                bar.adx,
                                bar.atr,
                            )

        except Exception as exc:
            logging.error(
                "WS stream error (%s): %s\n%s", pair, exc, traceback.format_exc()
            )
            await asyncio.sleep(5)


# ────────────────────────────────────────────────────────────────
#  Signal consumer
# ────────────────────────────────────────────────────────────────
async def consume(router: RiskRouter):
    MAX_OPEN = getattr(fvg_orderflow_config, "MAX_OPEN_CONCURRENT", 3)
    MAX_RISK = getattr(fvg_orderflow_config, "MAX_TOTAL_RISK_PCT", 0.30)
    MAX_PER_SIDE = getattr(fvg_orderflow_config, "MAX_PER_SIDE_OPEN", 1)
    MAX_AGE = getattr(fvg_orderflow_config, "MAX_SIGNAL_AGE_SEC", 60)
    COALESCE_SEC = getattr(fvg_orderflow_config, "COALESCE_SEC", 5)

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
        if daily_count[today] >= getattr(fvg_orderflow_config, "MAX_TRADES_PER_DAY", 5):
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
            fvg_orderflow_config.CLUSTER.get(p.signal.symbol)
            for p in list(router.book.values()) + list(router.pending.values())
            if fvg_orderflow_config.CLUSTER.get(p.signal.symbol)
        }

        for s in fresh:
            side_norm = "Buy" if s.side.lower() == "buy" else "Sell"
            cid = fvg_orderflow_config.CLUSTER.get(s.symbol)

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
                    getattr(fvg_orderflow_config, "MAX_TRADES_PER_DAY", 5),
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
    router = RiskRouter(equity_usd=fvg_orderflow_config.EQUITY, testnet=False)
    pairs = getattr(fvg_orderflow_config, "PAIRS", ["DOGEUSDT"])

    logging.info("=" * 80)
    logging.info("FVG ORDER FLOW LIVE ENGINE - 15M Timeframe")
    logging.info("Pairs: %s", ", ".join(pairs))
    logging.info(
        "Config: ADX_FLOOR=%d, TARGET_R1=%.1f, TARGET_R2=%s",
        getattr(fvg_orderflow_config, "ADX_FLOOR", 25),
        getattr(fvg_orderflow_config, "TARGET_R1", 1.5),
        getattr(fvg_orderflow_config, "TARGET_R2", "Disabled"),
    )
    logging.info("=" * 80)

    await audit_and_override_ticks(router, pairs)

    streams = [asyncio.create_task(kline_stream(p, router)) for p in pairs]
    streams.append(asyncio.create_task(consume(router)))
    streams.append(asyncio.create_task(_refresh_meta(router, pairs)))

    run_command_bot(router, fvg_orderflow_config)
    await asyncio.gather(*streams)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s – %(levelname)s – %(message)s"
    )

    # Silence noisy loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    asyncio.run(main())
