#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  Multi-pair 15-minute live signal engine  –  TJR/LRS (low-freq)
# ────────────────────────────────────────────────────────────────
import os, json, asyncio, logging, time, traceback
from   datetime import datetime, timezone, timedelta
from   typing   import List, Optional, Tuple

import ccxt.async_support as ccxt
import numpy  as np
import pandas as pd
import websockets
from   asyncio import Queue
# import httpx
# EVENT_BASE = os.getenv("EVENT_API_BASE", "http://localhost:8000")
# HTTPX = httpx.AsyncClient(timeout=3.0)  # reuse one client

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from bot.infra.models import Signal, Position
from bot.engines.risk_router import RiskRouter
from bot.helpers import (
    config,
    compute_indicators,        # → ATR, ADX, Stoch, RSI, …
    build_htf_levels,          # 1H structure map with pools
    update_htf_levels_new,     # incremental HTF levels update
    tjr_long_signal, tjr_short_signal,
    telegram,
    build_h1, update_h1,       # H1 trend tracker (expects .slope)
)
from bot.data import preload_history

# ────────────────────────────────────────────────────────────────
#  Bybit + runtime constants
# ────────────────────────────────────────────────────────────────
WS_URL    = "wss://stream.bybit.com/v5/public/linear"
PING_SEC  = 20
REST      = ccxt.bybit({"enableRateLimit": True})
TF        = config.INTERVAL                  # "15"
TF_SEC    = int(TF) * 60
LOOKBACK  = 1_000
MAX_RETRY = 3

# Signal queue
SIGNAL_Q: Queue = Queue(maxsize=100)

# Frequency throttles (tune these to hit ~1–2 trades/month/pair)
MIN_GAP_DAYS_PER_PAIR = 14              # hard cool-down after ANY trade
                # short only when %K_fast ≥ 75

# Stricter market-quality veto to reduce frequency
def veto_thresholds(bar):
    vol_norm = bar.atr / bar.atr30
    #If trade freq still to low change from 12 to 10
    min_adx  = 12 + 6 * vol_norm
    #If trade freq still to low change from .45 to .40
    atr_veto = 0.45 + 0.25 * vol_norm
    return min_adx, atr_veto

# # Session filter (EU + NY by default). Comment out to disable.
# GOOD_HOURS = set(range(*config.SESSION_WINDOWS["eu"])) \
#            | set(range(*config.SESSION_WINDOWS["ny"]))
# def in_good_hours(ts):
#     return ts.hour in GOOD_HOURS

# Track last signal time per pair (for cool-down)
last_signal_ts: dict[str, float] = {}

# ────────────────────────────────────────────────────────────────
#  Web-socket coroutine per pair
# ────────────────────────────────────────────────────────────────
async def kline_stream(pair: str, router: RiskRouter) -> None:
    topic = f"kline.{TF}.{pair}"

    # Preload recent history
    hist = await preload_history(symbol=pair, interval=TF, limit=LOOKBACK)
    htf_levels = build_htf_levels(hist.copy())
    h1         = build_h1(hist.copy())

    logging.info("[%s] History pre-loaded: %d bars (%s → %s)",
                 pair, len(hist), hist.index[0], hist.index[-1])

    last_heartbeat, HEARTBEAT_SECS = time.time(), 600
    last_ts: Optional[int] = None  # pointer to detect WS gaps

    while True:
        await asyncio.sleep(1)
        try:
            async with websockets.connect(
                WS_URL, ping_interval=PING_SEC, ping_timeout=PING_SEC*2
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                logging.info("[%s] Subscribed to %s", pair, topic)

                async for raw in ws:
                    if time.time() - last_heartbeat > HEARTBEAT_SECS:
                        logging.info("[%s] Heartbeat OK – %s", pair, datetime.now(timezone.utc))
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
                        logging.warning("[%s] WS gap: %d candle(s) – back-filling", pair, len(missing))
                        since = (missing[0] - TF_SEC) * 1000
                        for _ in range(MAX_RETRY):
                            try:
                                kl = await REST.fetch_ohlcv(pair, timeframe=f"{TF}m", since=since, limit=len(missing)+2)
                                df_miss = pd.DataFrame(kl, columns=["ts","o","h","l","c","v"]).set_index("ts")
                                df_miss.index = pd.to_datetime(df_miss.index, unit='ms', utc=True)
                                hist = (pd.concat([hist, df_miss])
                                          .drop_duplicates()
                                          .sort_index()
                                          .tail(LOOKBACK))
                                break
                            except Exception as e:
                                logging.error("[%s] REST back-fill error: %s", pair, e)
                                await asyncio.sleep(0.5)
                    last_ts = current_end

                    # Construct the just-closed 15m bar at exact close boundary
                    start_ms = int(kline["start"])
                    ts_close = pd.to_datetime(start_ms, unit="ms", utc=True) + pd.Timedelta(seconds=TF_SEC)

                    new = (pd.DataFrame([[ts_close,
                                          float(kline["open"]),
                                          float(kline["high"]),
                                          float(kline["low"]),
                                          float(kline["close"]),
                                          float(kline["volume"])]],
                                        columns=["ts","o","h","l","c","v"])
                           .set_index("ts"))
                    hist = pd.concat([hist, new]).tail(LOOKBACK)
                    hist = compute_indicators(hist)

                    bar = hist.iloc[-1]
                    if bar[["atr", "atr30", "adx", "k_fast"]].isna().any():
                        continue  # indicator warm-up guard

                    # Optional session filter to reduce trades further
                    # if not in_good_hours(bar.name):
                    #     continue

                    # H1 trend row (gate longs/shorts)
                    try:
                        h1row = h1.loc[bar.name.floor("1h")]
                    except KeyError:
                        # Not enough H1 data yet; update and continue
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        continue

                    # Market-quality veto (stricter)
                    min_adx, atr_veto = veto_thresholds(bar)
                    if bar.adx < min_adx or bar.atr < atr_veto * bar.atr30:
                        logging.info("[%s] No-trade (veto)  k=%.1f  adx=%.1f  atr=%.5f",
                                     pair, bar.k_fast, bar.adx, bar.atr)
                        if bar.adx < min_adx: logging.info(f"adx_low {bar.adx:.1f}<{min_adx:.1f}")
                        if bar.atr < atr_veto * bar.atr30: logging.info(f"atr_low {bar.atr:.4f}<{(atr_veto*bar.atr30):.4f}")
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        htf_levels = update_htf_levels_new(htf_levels, bar)
                        continue

                    # # Per-pair cool-down (target ~1–2 trades/month/pair)
                    # last_t = last_signal_ts.get(pair, 0.0)
                    # if last_t and (time.time() - last_t) < (MIN_GAP_DAYS_PER_PAIR * 86400):
                    #     remain = int((MIN_GAP_DAYS_PER_PAIR * 86400) - (time.time() - last_t))
                    #     logging.info("[%s] Cool-down %dd %02dh left – skipping",
                    #                  pair, remain//86400, (remain%86400)//3600)
                    #     h1 = update_h1(h1, bar.name, float(bar.c))
                    #     htf_levels = update_htf_levels_new(htf_levels, bar)
                    #     continue

                    # HTF snapshot for tjr_* (no look-ahead; use ffill to <= bar.name)
                    try:
                        idx_prev = htf_levels.index.get_indexer([bar.name], method="ffill")[0]
                        if idx_prev == -1:
                            h1 = update_h1(h1, bar.name, float(bar.c))
                            continue
                        htf_row = htf_levels.iloc[idx_prev]
                    except KeyError:
                        h1 = update_h1(h1, bar.name, float(bar.c))
                        continue

                    # Distances
                    sl_base  = config.ATR_MULT_SL * bar.atr
                    stop_off = config.SL_CUSHION_MULT * sl_base + config.WICK_BUFFER * bar.atr
                    tp_dist  = config.RR_TARGET * stop_off

                    # Signal checks with H1 slope + %K extremes to lower frequency
                    i = len(hist) - 1
                    header = "LRS MULTI-PAIR Engine (low-freq)"

                    # Longs: need tjr_long AND H1 slope up AND stoch low
                    if (h1row.slope > 0
                        and tjr_long_signal(hist, i, htf_row)):
                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info("[%s] LONG signal  k=%.1f  adx=%.1f", pair, bar.k_fast, bar.adx)
                            telegram.alert_side(pair, bar, TF, "LONG", stop_off=stop_off, tp_dist=tp_dist, header=header)
                            sig = Signal(pair, "Buy", bar.c, sl=bar.c - stop_off, tp=bar.c + tp_dist,
                                         key=f"{pair}-{bar.name:%H%M}", ts=bar.name)
                            await SIGNAL_Q.put(sig)
                            last_signal_ts[pair] = time.time()

                    # Shorts: need tjr_short AND H1 slope down AND stoch high
                    elif (h1row.slope < 0
                          and tjr_short_signal(hist, i, htf_row)):
                        if router.has_open(pair):
                            logging.info("[%s] No-trade (already open)", pair)
                        else:
                            logging.info("[%s] SHORT signal k=%.1f  adx=%.1f", pair, bar.k_fast, bar.adx)
                            telegram.alert_side(pair, bar, TF, "SHORT", stop_off=stop_off, tp_dist=tp_dist, header=header)
                            sig = Signal(pair, "Sell", bar.c, sl=bar.c + stop_off, tp=bar.c - tp_dist,
                                         key=f"{pair}-{bar.name:%H%M}", ts=bar.name)
                            await SIGNAL_Q.put(sig)
                            last_signal_ts[pair] = time.time()
                    else:
                        logging.info("[%s] No-trade (no gated signal) %s  k=%.1f  adx=%.1f",
                                     pair, bar.name, bar.k_fast, bar.adx)

                    # AFTER decision: keep HTF/H1 fresh (same order as live)
                    htf_levels = update_htf_levels_new(htf_levels, bar)
                    h1         = update_h1(h1, bar.name, float(bar.c))

        except Exception as exc:
            logging.error("WS stream error (%s): %s\n%s", pair, exc, traceback.format_exc())
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
async def main():
    router = RiskRouter(equity_usd=20, testnet=False)

    # Producer tasks (use the multi-pair list)
    pairs = getattr(config, "PAIRS_LRS", None) or config.PAIRS_LRS
    streams = [asyncio.create_task(kline_stream(p, router)) for p in pairs]

    # Consumer: execute queued signals
    async def consume():
        while True:
            sig = await SIGNAL_Q.get()
            try:
                await router.handle(sig)
                logging.info("Signal processed: %s", sig)
            except Exception as e:
                logging.error("Router error: %s", e)
            finally:
                SIGNAL_Q.task_done()

    streams.append(asyncio.create_task(consume()))
    await asyncio.gather(*streams)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s – %(levelname)s – %(message)s")
    asyncio.run(main())
