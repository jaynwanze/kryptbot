#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  Multi-pair 15-minute live signal engine  –  TJR/LRS (low-freq)
# ────────────────────────────────────────────────────────────────
import os, json, asyncio, logging, time, traceback
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
import ccxt.async_support as ccxt
import pandas as pd
import websockets
from asyncio import Queue
from pathlib import Path
# import httpx
# EVENT_BASE = os.getenv("EVENT_API_BASE", "http://localhost:8000")
# HTTPX = httpx.AsyncClient(timeout=3.0)  # reuse one client
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from bot.infra.models import Signal, Position
from bot.engines.risk_router import RiskRouter, audit_and_override_ticks
from bot.helpers import (
    config,
    compute_indicators,  # → ATR, ADX, Stoch, RSI, …
    build_htf_levels,  # 1H structure map with pools
    update_htf_levels_new,  # incremental HTF levels update
    tjr_long_signal,
    tjr_short_signal,
    telegram,
    build_h1,
    update_h1,  # H1 trend tracker (expects .slope)
    in_good_hours,
    veto_thresholds,
    near_htf_level,
    append_csv,
    hours,
)
from bot.data import preload_history
from bot.commands import run_command_bot

# ────────────────────────────────────────────────────────────────
#  Global constants
# ────────────────────────────────────────────────────────────────
LOG_DIR = Path(getattr(config, "LOG_DIR", "./logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
# --- cooldown after SL ---
COOLDOWN_DAYS_AFTER_SL = 1
COOLDOWN_SEC = COOLDOWN_DAYS_AFTER_SL * 86400
GOOD_HOURS = hours("eu", session_windows=config.SESSION_WINDOWS) | hours(
    "ny", session_windows=config.SESSION_WINDOWS
)
# ────────────────────────────────────────────────────────────────
#  Bybit + runtime constants
# ────────────────────────────────────────────────────────────────
WS_URL = "wss://stream.bybit.com/v5/public/linear"
PING_SEC = 20
REST = ccxt.bybit({"enableRateLimit": True})
TF = config.INTERVAL  # "15"
TF_SEC = int(TF) * 60
LOOKBACK = 1_000
MAX_RETRY = 3
# ────────────────────────────────────────────────────────────────
# Signal queue
# ────────────────────────────────────────────────────────────────
SIGNAL_Q: Queue = Queue(maxsize=100)
# Track last signal time per pair (for cool-down)
last_signal_ts: dict[str, float] = {}

# ────────────────────────────────────────────────────────────────
# async methods
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
    last_ts: Optional[int] = None  # pointer to detect WS gaps

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
                        continue  # wait for closed candle

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

                    # Construct the just-closed 15m bar at exact close boundary
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

                    # Look at drop stats every hour
                    if int(time.time()) % 3600 < TF_SEC:
                        logging.info("DROP_STATS %s %s", pair, drop_stats)

                    bar = hist.iloc[-1]
                    if bar[["atr", "atr30", "adx", "k_fast"]].isna().any():
                        continue  # indicator warm-up guard
                    # if positions open trail
                    try:
                        await router.maybe_trail(pair, bar)
                    except Exception as e:
                        logging.warning("[%s] trail update failed: %s", pair, e)

                    # 1) HTF snapshot (so htf_row exists)
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

                    # 2) Proximity to HTF levels gate (now htf_row is defined)
                    if not near_htf_level(bar, htf_row, max_atr=0.8):
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        drop_stats["not_near_htf"] += 1
                        continue

                    # 3) Session  gate before sending a signal
                    if not in_good_hours(bar.name,good_hours= GOOD_HOURS):
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        drop_stats["off_session"] += 1
                        continue

                    # 4/ H1 trend row (gate longs/shorts)
                    try:
                        h1row = h1.loc[bar.name.floor("1h")]
                    except KeyError:
                        # Not enough H1 data yet; update and continue
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        drop_stats["h1_missing"] += 1
                        continue

                    # 5) Market-quality veto (stricter)
                    min_adx, atr_veto = veto_thresholds(bar)
                    if bar.adx < min_adx:
                        drop_stats["veto_adx"] += 1
                    if bar.atr < atr_veto * bar.atr30:
                        drop_stats["veto_atr"] += 1
                    if bar.adx < min_adx or bar.atr < atr_veto * bar.atr30:
                        logging.info(
                            "[%s] No-trade (veto)  k=%.1f  adx=%.1f  atr=%.5f",
                            pair,
                            bar.k_fast,
                            bar.adx,
                            bar.atr,
                        )
                        if bar.adx < min_adx:
                            logging.info(f"adx_low {bar.adx:.1f}<{min_adx:.1f}")
                        if bar.atr < atr_veto * bar.atr30:
                            logging.info(
                                f"atr_low {bar.atr:.4f}<{(atr_veto*bar.atr30):.4f}"
                            )
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        continue

                    # 5.5) Simple cool-down after SL hit ON PAIR
                    last_sl = router.last_sl_ts.get(
                        pair, 0.0
                    )  # populated by RiskRouter when a reduce-only SL fills
                    if last_sl and (time.time() - last_sl) < COOLDOWN_SEC:
                        left_days = (COOLDOWN_SEC - (time.time() - last_sl)) / 86400.0
                        logging.info(
                            "[%s] Cool-down after SL active — %.1f days left",
                            pair,
                            left_days,
                        )
                        # keep maps fresh then move on
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        continue

                    # Distances
                    sl_base = config.ATR_MULT_SL * bar.atr
                    stop_off = (
                        config.SL_CUSHION_MULT * sl_base + config.WICK_BUFFER * bar.atr
                    )
                    tp_dist = config.RR_TARGET * stop_off

                    # Signal checks with H1 slope + %K extremes to lower frequency
                    i = len(hist) - 1
                    header = "LRS MULTI-PAIR Engine (low-freq)"

                    # Check if we have enough confirmations
                    min_checks = 2 if bar.adx >= 25 else 3
                    # Longs: need tjr_long AND H1 slope up AND stoch low
                    if (
                        bar.k_fast <= 40
                        and h1row.slope > 0
                        and tjr_long_signal(hist, i, htf_row, min_checks)
                    ):
                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info(
                                "[%s] LONG signal  k=%.1f  adx=%.1f",
                                pair,
                                bar.k_fast,
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
                            # write to csv, right after building sig:
                            append_csv(
                                "signals.csv",
                                {
                                    "ts": sig.ts.isoformat(),
                                    "symbol": sig.symbol,
                                    "side": sig.side,
                                    "entry": float(sig.entry),
                                    "sl": float(sig.sl),
                                    "tp": float(sig.tp),
                                    "adx": getattr(sig, "adx", 0.0),
                                    "k_fast": getattr(sig, "k_fast", 0.0),
                                    "k_slow": getattr(sig, "k_slow", 0.0),
                                    "d_slow": getattr(sig, "d_slow", 0.0),
                                    "off_sl": getattr(sig, "off_sl", 0.0),
                                    "off_tp": getattr(sig, "off_tp", 0.0),
                                    "key": sig.key,
                                },
                                [
                                    "ts",
                                    "symbol",
                                    "side",
                                    "entry",
                                    "sl",
                                    "tp",
                                    "adx",
                                    "k_fast",
                                    "k_slow",
                                    "d_slow",
                                    "off_sl",
                                    "off_tp",
                                    "key",
                                ],
                                log_dir=LOG_DIR,
                            )

                            await SIGNAL_Q.put(sig)
                            last_signal_ts[pair] = time.time()

                    # Shorts: need tjr_short AND H1 slope down AND stoch high
                    elif (
                        bar.k_fast >= 60
                        and h1row.slope < 0
                        and tjr_short_signal(hist, i, htf_row, min_checks)
                    ):
                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info(
                                "[%s] SHORT signal k=%.1f  adx=%.1f",
                                pair,
                                bar.k_fast,
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
                                "signals.csv",
                                {
                                    "ts": sig.ts.isoformat(),
                                    "symbol": sig.symbol,
                                    "side": sig.side,
                                    "entry": float(sig.entry),
                                    "sl": float(sig.sl),
                                    "tp": float(sig.tp),
                                    "adx": getattr(sig, "adx", 0.0),
                                    "k_fast": getattr(sig, "k_fast", 0.0),
                                    "k_slow": getattr(sig, "k_slow", 0.0),
                                    "d_slow": getattr(sig, "d_slow", 0.0),
                                    "off_sl": getattr(sig, "off_sl", 0.0),
                                    "off_tp": getattr(sig, "off_tp", 0.0),
                                    "key": sig.key,
                                },
                                [
                                    "ts",
                                    "symbol",
                                    "side",
                                    "entry",
                                    "sl",
                                    "tp",
                                    "adx",
                                    "k_fast",
                                    "k_slow",
                                    "d_slow",
                                    "off_sl",
                                    "off_tp",
                                    "key",
                                ],
                                log_dir=LOG_DIR,
                            )
                            await SIGNAL_Q.put(sig)
                            last_signal_ts[pair] = time.time()
                    else:
                        if h1row.slope > 0:
                            drop_stats["no_ltf_long"] += 1
                        elif h1row.slope < 0:
                            drop_stats["no_ltf_short"] += 1
                        logging.info(
                            "[%s] No-trade (no gated signal) %s  k=%.1f  adx=%.1f",
                            pair,
                            bar.name,
                            bar.k_fast,
                            bar.adx,
                        )

                    # AFTER decision: keep HTF/H1 fresh (same order as live)
                    htf_levels = update_htf_levels_new(htf_levels, bar)
                    h1 = update_h1(h1, bar.name, float(bar.c))

        except Exception as exc:
            logging.error(
                "WS stream error (%s): %s\n%s", pair, exc, traceback.format_exc()
            )
            await asyncio.sleep(5)  # back-off then reconnect


# SET BEOFRE EACH CONTINUE OR ENTRY
# async def post_decision(payload: dict):
#     try:
#         r = await HTTPX.post(f"{EVENT_BASE}/engine/event", json=payload)
#         r.raise_for_status()
#     except Exception as e:
#         logging.warning("post_decision failed: %s", e)
# ────────────────────────────────────────────────────────────────
#  Runner
# ────────────────────────────────────────────────────────────────
async def consume(router: RiskRouter):
    MAX_OPEN = getattr(config, "MAX_OPEN_CONCURRENT", 3)
    MAX_RISK = getattr(config, "MAX_TOTAL_RISK_PCT", 0.30)
    MAX_PER_SIDE = getattr(config, "MAX_PER_SIDE_OPEN", 1)
    MAX_AGE = getattr(config, "MAX_SIGNAL_AGE_SEC", 30)  # 20–30s on 15m
    COALESCE_SEC = getattr(config, "COALESCE_SEC", 2)

    def score(s: Signal) -> float:
        width = abs(float(s.tp) - float(s.sl))
        adx_w = max(0.0, min(1.0, (float(getattr(s, "adx", 0.0)) - 12.0) / 25.0))
        ks = float(getattr(s, "k_slow", getattr(s, "k_fast", 50.0)))
        stoch_w = (
            max(0.0, min(1.0, (100.0 - ks) / 100.0))
            if s.side.lower() == "buy"
            else max(0.0, min(1.0, ks / 100.0))
        )
        return width * (0.5 + 0.5 * adx_w) * (0.5 + 0.5 * stoch_w)

    while True:
        # ── 1) Coalesce a small batch of signals
        sig0 = await SIGNAL_Q.get()
        batch = [sig0]
        until = time.time() + COALESCE_SEC
        while time.time() < until:
            try:
                batch.append(SIGNAL_Q.get_nowait())
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)

        # mark dequeued items as done exactly once
        for _ in batch:
            SIGNAL_Q.task_done()

        # ── 2) Stale filter
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
                telegram.bybit_alert(
                    msg=f"[SIGNAL {s.symbol}] Drop stale signal age={age:.1f}s"
                )
        if not fresh:
            continue

        # ── 3) Dedupe per symbol (keep latest)
        dedup = {}
        for s in fresh:
            dedup[s.symbol] = s
        fresh = list(dedup.values())

        # ── 4) Strongest first
        fresh.sort(key=score, reverse=True)

        # ── 5) Take a snapshot of portfolio state, then
        #        use *ephemeral counters* to reserve capacity within this batch.
        open_used = router.open_count()
        buy_used = router.open_count_side("Buy")
        sell_used = router.open_count_side("Sell")
        risk_used = router.open_risk_pct()

        busy_clusters = {
            config.CLUSTER.get(p.signal.symbol)
            for p in router.book.values()
            if config.CLUSTER.get(p.signal.symbol)
        }

        for s in fresh:
            side_norm = "Buy" if s.side.lower() == "buy" else "Sell"
            cid = config.CLUSTER.get(s.symbol)

            # hard caps
            if open_used >= MAX_OPEN:
                logging.info("[PORTFOLIO] Concurrency full — drop %s", s.symbol)
                telegram.bybit_alert(
                    msg=f"[PORTFOLIO] Concurrency full — drop {s.symbol}"
                )
                continue
            if risk_used >= MAX_RISK:
                logging.info("[PORTFOLIO] Risk full — drop %s", s.symbol)
                telegram.bybit_alert(msg=f"[PORTFOLIO] Risk full — drop {s.symbol}")
                continue
            if side_norm == "Buy" and buy_used >= MAX_PER_SIDE:
                logging.info("[PORTFOLIO] Long-side full — drop %s", s.symbol)
                telegram.bybit_alert(
                    msg=f"[PORTFOLIO] Long-side full — drop {s.symbol}"
                )
                continue
            if side_norm == "Sell" and sell_used >= MAX_PER_SIDE:
                logging.info("[PORTFOLIO] Short-side full — drop %s", s.symbol)
                telegram.bybit_alert(
                    msg=f"[PORTFOLIO] Short-side full — drop {s.symbol}"
                )
                continue
            if cid and cid in busy_clusters:
                logging.info("[PORTFOLIO] Cluster busy (%s) — drop %s", cid, s.symbol)
                telegram.bybit_alert(msg=f"[PORTFOLIO] Cluster busy — drop {s.symbol}")
                continue

            # ── Reserve capacity immediately so the rest of the batch sees it
            open_used += 1
            if side_norm == "Buy":
                buy_used += 1
            else:
                sell_used += 1
            if cid:
                busy_clusters.add(cid)

            # (Optional) if your router can estimate added risk, bump risk_used here:
            # if hasattr(router, "estimate_risk_pct"):
            #     risk_used += router.estimate_risk_pct(s)
            try:
                await router.handle(s)
                logging.info(
                    "Processed: %s | reserved open=%d (buy=%d/sell=%d) risk≈%.1f%%",
                    s.symbol,
                    open_used,
                    buy_used,
                    sell_used,
                    risk_used * 100,
                )
            except Exception as e:
                logging.error("Router error for %s: %s", s.symbol, e, exc_info=True)
                # rollback reservation so another candidate can try
                open_used -= 1
                if side_norm == "Buy":
                    buy_used -= 1
                else:
                    sell_used -= 1
                if cid:
                    busy_clusters.discard(cid)
                continue

async def main():
    router = RiskRouter(equity_usd=20, testnet=False)  # use your real equity
    pairs = getattr(config, "PAIRS_LRS", None) or config.PAIRS_LRS
    await audit_and_override_ticks(router, pairs)

    streams = [asyncio.create_task(kline_stream(p, router)) for p in pairs]
    # START the consumer and WAIT on everything
    streams.append(asyncio.create_task(consume(router)))
    streams.append(asyncio.create_task(_refresh_meta(router, pairs)))
    run_command_bot(router)  # returns immediately; polling runs in PTB's own thread
    await asyncio.gather(*streams)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s – %(levelname)s – %(message)s"
    )
    asyncio.run(main())
