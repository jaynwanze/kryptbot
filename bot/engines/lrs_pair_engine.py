#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  Multi‑pair 15‑minute live signal engine  –  TJR/LRS strategy (Jul‑2025)
# ────────────────────────────────────────────────────────────────
import os, json, asyncio, math, logging, time, traceback
from   datetime import datetime, timezone
from   typing   import List, Optional, Tuple

import ccxt.async_support as ccxt
import numpy  as np
import pandas as pd
import websockets
from   telegram import Bot
from   dotenv   import load_dotenv
from asyncio import Queue
import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from bot.infra.models import Signal, Position
from bot.engines.risk_router import RiskRouter
from bot.helpers import (
    config,
    compute_indicators,        # → ATR, ADX, Stoch, RSI, …
    build_htf_levels,          # 4‑hour / daily structure map
    tjr_long_signal,           # entry rules
    tjr_short_signal,  
    update_htf_levels_new,     # incremental HTF levels update 
    telegram       
)
from bot.data import preload_history

# ────────────────────────────────────────────────────────────────
#  Logging & Telegram
# ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s – %(levelname)s – %(message)s",
)
# ────────────────────────────────────────────────────────────────
#  Bybit Web‑socket constants
# ────────────────────────────────────────────────────────────────
WS_URL   = "wss://stream.bybit.com/v5/public/linear"
PING_SEC = 20
REST     = ccxt.bybit({"enableRateLimit": True})
TF         = config.INTERVAL           # "15"  …minutes
TF_SEC     = int(TF) * 60
MAX_RETRY = 3
LOOKBACK   = 1_000                     # bars per pair

# ────────────────────────────────────────────────────────────────
#  Signal queue
SIGNAL_Q: Queue = Queue(maxsize=100)

# ────────────────────────────────────────────────────────────────
#  Web‑socket coroutine
# ────────────────────────────────────────────────────────────────
async def kline_stream(pair: str, router: RiskRouter) -> None:
    # WebSocket topic
    topic = f"kline.{TF}.{pair}"
    # Pull a chunk of history, build indicators & HTF context
    hist = await preload_history(symbol=pair, interval=TF, limit=LOOKBACK)           # 15‑min bars
    htf_levels   = build_htf_levels(hist.copy())

    logging.info("History pre‑loaded: %d bars (%s → %s)",
                 len(hist), hist.index[0], hist.index[-1])

    last_heartbeat, HEARTBEAT_SECS = time.time(), 600  # log every 10 min
    
    # Indicator warm‑up
    MAX_PERIODS = 200  # longest MA / oscillator period + cushion

    # Pointer to detect WS gaps
    last_ts: int | None = None

    while True:  # auto‑reconnect loop
        await asyncio.sleep(1)      
        try:
            async with websockets.connect(
                WS_URL, ping_interval=PING_SEC, ping_timeout=PING_SEC*2
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                logging.info("Subscribed to topic %s", topic)

                async for raw in ws:
                    # -------------- heartbeat --------------
                    if time.time() - last_heartbeat > HEARTBEAT_SECS:
                        logging.info("Heartbeat OK – %s", datetime.now(timezone.utc))
                        last_heartbeat = time.time()

                    msg = json.loads(raw)
                    if msg.get("topic") != topic:
                        continue

                    kline = msg["data"][0]
                    if not kline["confirm"]:        # ignore still‑open candle
                        continue

                      # --- watchdog: fill missed candles --------------------------------
                    current_end = int(kline["end"]) // 1000  # to seconds
                    if last_ts is None:
                        last_ts = current_end - TF_SEC
                    expected = last_ts + TF_SEC
                    if current_end > expected:
                        missing = list(range(expected, current_end, TF_SEC))
                        logging.warning("WS gap: %d candle(s) missed – back‑filling", len(missing))
                        since = (missing[0] - TF_SEC) * 1000  # ms
                        for _ in range(MAX_RETRY):
                            try:
                                kl = await REST.fetch_ohlcv(pair, timeframe=f"{TF}m", since=since, limit=len(missing)+2)
                                df_miss = (pd.DataFrame(kl, columns=["ts","o","h","l","c","v"])\
                                               .set_index("ts"))
                                df_miss.index = pd.to_datetime(df_miss.index, unit='ms', utc=True)
                                hist = (pd.concat([hist, df_miss])
                                          .drop_duplicates()
                                          .sort_index()
                                          .tail(LOOKBACK))
                                break
                            except Exception as e:
                                logging.error("REST back‑fill error: %s", e)
                                await asyncio.sleep(0.5)
                    last_ts = current_end  # move pointer
                  
                   #(exact close boundary)
                    start_ms = int(kline["start"])            # Bybit sends start/end in ms
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

                    if len(hist) >= MAX_PERIODS:
                        logging.info("Warm‑up already satisfied – live trading ENABLED")

                    bar = hist.iloc[-1]
                    if bar[["atr","atr30","adx","k_fast"]].isna().any():
                        continue  # indicator NA guard
                    # higher‑TF context
                    try:
                        # nearest index *≤ bar.name*; raises KeyError if the table is still empty
                        idx_prev = htf_levels.index.get_indexer([bar.name], method="ffill")[0]
                        if idx_prev == -1:          # HTF table still warming‑up
                            continue
                        htf_row = htf_levels.iloc[idx_prev]
                    except KeyError:
                            continue      

                    # Signals
                    i = len(hist) - 1
                    
                    #4H trend filter
                    # four_h = hist['c'].iloc[:i+1].resample('4h').last()
                    # trend = four_h.pct_change().rolling(3).mean().abs().iloc[-1]
                    # if trend < 0.006:          # < 0.6 % move in the last 3 days ⇒ chop
                    #     continue
                    
                    # ADX & volume veto
                    vol_norm = bar.atr / bar.atr30
                    min_adx  = 10 + 8 * vol_norm            
                    atr_veto = 0.5 + 0.3 * vol_norm        
                    if bar.adx < min_adx or bar.atr < atr_veto * bar.atr30:
                            logging.info("[%s] No‑trade (veto)  k_fast %.1f  adx %.1f  atr %.4f",
                            pair, bar.k_fast, bar.adx, bar.atr)
                            continue 
                    
                    stop_off = (config.ATR_MULT_SL * 1.6 + config.WICK_BUFFER) * bar.atr
                    tp   = config.ATR_MULT_TP * bar.atr
                    header = "LRS MULTI-PAIR Engine"
                    if tjr_long_signal(hist, i, htf_row):
                        if router.has_open(pair):
                            logging.info("[%s] No‑trade (already open)", pair)
                            continue
                        logging.info("[%s] LONG signal  %.1f/%.1f", pair, bar.k_fast, bar.adx)
                        logging.info("Entry|TP|SL  %.1f/%.1f/%.1f", bar.c, tp, stop_off)
                        telegram.alert_side(pair, bar, TF, "LONG", stop_off=stop_off, tp=tp, header=header)
                        sig = Signal(pair, "Buy", bar.c, sl=bar.c-stop_off, tp=bar.c+tp,
                              key=f"{pair}-{bar.name:%H%M}", ts=bar.name)
                        await SIGNAL_Q.put(sig)
#                         logging.info(
#   "[PROBE %s] O=%.3f H=%.3f L=%.3f C=%.3f  ADX=%.1f ATR=%.4f ATR30=%.4f k=%.1f",
#   bar.name, bar.o, bar.h, bar.l, bar.c, bar.adx, bar.atr, bar.atr30, bar.k_fast
# )
                    elif tjr_short_signal(hist, i, htf_row):
                        if router.has_open(pair):
                            logging.info("[%s] No‑trade (already open)", pair)
                            continue
                        logging.info("[%s] SHORT signal %.1f/%.1f", pair, bar.k_fast, bar.adx)
                        logging.info("Entry|TP|SL  %.1f/%.1f/%.1f", bar.c, tp, stop_off)
                        telegram.alert_side(pair, bar, TF, "SHORT", stop_off=stop_off, tp=tp, header=header)
                        sig = Signal(pair, "Sell", bar.c, sl=bar.c+stop_off, tp=bar.c-tp,
                              key=f"{pair}-{bar.name:%H%M}", ts=bar.name)
                        await SIGNAL_Q.put(sig)
#                         logging.info(
#   "[PROBE %s] O=%.3f H=%.3f L=%.3f C=%.3f  ADX=%.1f ATR=%.4f ATR30=%.4f k=%.1f",
#   bar.name, bar.o, bar.h, bar.l, bar.c, bar.adx, bar.atr, bar.atr30, bar.k_fast
# )
                    else:
                        if logging.getLogger().isEnabledFor(logging.INFO):
                            logging.info("[%s] No-trade(No signal) %s  k_fast %.1f  adx %.1f",
                                         pair, bar.name, bar.k_fast, bar.adx)

                    # AFTER signal logic: maintain HTF map
                    htf_levels = update_htf_levels_new(htf_levels, bar)
        except Exception as exc:
            logging.error("WS stream error: %s\n%s", exc, traceback.format_exc())
            await asyncio.sleep(5)        # back‑off then reconnect

async def main():
    router = RiskRouter(equity_usd=20, testnet=False)

    # producer tasks
    streams = [asyncio.create_task(kline_stream(p, router)) for p in config.PAIRS_LRS]

    # consumer task
    async def consume():
        while True:
            sig = await SIGNAL_Q.get()
            try:
                await router.handle(sig)
                if logging.getLogger().isEnabledFor(logging.INFO):
                    logging.info("Signal processed: %s", sig)
            except Exception as e:
                logging.error("Router error: %s", e)
            finally:
                SIGNAL_Q.task_done()
                if logging.getLogger().isEnabledFor(logging.INFO):
                    logging.info("Signal processed: %s", sig)

    streams.append(asyncio.create_task(consume()))
    await asyncio.gather(*streams)

# # ───────────────────────────────── entry‑point ───────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())