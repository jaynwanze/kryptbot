#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  SOL/USDT – 15‑minute live signal bot (TJR strategy, July 2025)
# ────────────────────────────────────────────────────────────────
import os, json, asyncio, math, logging, time, traceback
from   datetime import datetime, timezone
from   typing   import Optional, Tuple

import ccxt.async_support as ccxt
import numpy  as np
import pandas as pd
import websockets
from   telegram import Bot
from   dotenv   import load_dotenv
 
from helpers import (
    config,
    compute_indicators,        # → ATR, ADX, Stoch, RSI, …
    update_htf_levels,          # 4‑hour / daily structure map
    build_htf_levels,          # 4‑hour / daily structure map
    tjr_long_signal,           # entry rules
    tjr_short_signal,          
)
from data import preload_history


# ────────────────────────────────────────────────────────────────
#  Logging & Telegram
# ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s – %(levelname)s – %(message)s",
)

load_dotenv()
TG_TOKEN   = os.getenv("TELE_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
bot        = Bot(token=TG_TOKEN)
LOOKAHEAD = config.BOS_LOOKBACK          # look-ahead for BOS

# ────────────────────────────────────────────────────────────────
#  Bybit Web‑socket constants
# ────────────────────────────────────────────────────────────────
WS_URL   = "wss://stream.bybit.com/v5/public/linear"
TOPIC    = f"kline.{config.INTERVAL}.{config.PAIR}"   # e.g. kline.15.SOLUSDT
PING_SEC = 20
REST     = ccxt.bybit({"enableRateLimit": True})
TF_SEC   = int(config.INTERVAL) * 60          # 900 s for 15‑m
MAX_RETRY = 3



def alert_side(bar: pd.Series, side: str) -> None:
    """Send a nicely‑formatted alert to Telegram."""
    stop_off = (config.ATR_MULT_SL * 1.6 + config.WICK_BUFFER) * bar.atr
    if side == "LONG":
        sl = bar.c - stop_off
        tp = bar.c + config.ATR_MULT_TP * bar.atr
        emoji = "📈"
    else:  # SHORT
        sl = bar.c + stop_off
        tp = bar.c - config.ATR_MULT_TP * bar.atr
        emoji = "📉"

    msg = (
        f"{emoji} *{config.PAIR} {config.INTERVAL}‑m {side} signal*\n"
        f"`{bar.name:%Y‑%m-%d %H:%M}` UTC\n"
        f"Entry  : `{bar.c:.3f}`\n"
        f"Stop   : `{sl:.3f}`\n"
        f"Target : `{tp:.3f}`\n"
        f"ADX    : `{bar.adx:.1f}`  |  StochK: `{bar.k_fast:.1f}`"
    )

    try:
        bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")
        logging.info("Telegram alert sent: %s %s", side, bar.name)
    except Exception as exc:
        logging.error("Telegram error: %s", exc)


# ────────────────────────────────────────────────────────────────
#  Web‑socket coroutine
# ────────────────────────────────────────────────────────────────
async def kline_stream() -> None:
    # 1) Pull a chunk of history, build indicators & HTF context
    hist = await preload_history(limit=1000)           # 15‑min bars
    htf_levels = build_htf_levels(hist)                # 4‑hour/daily map

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
                await ws.send(json.dumps({"op": "subscribe", "args": [TOPIC]}))
                logging.info("Subscribed to topic %s", TOPIC)

                async for raw in ws:
                    # -------------- heartbeat --------------
                    if time.time() - last_heartbeat > HEARTBEAT_SECS:
                        logging.info("Heartbeat OK – %s", datetime.now(timezone.utc))
                        last_heartbeat = time.time()

                    msg = json.loads(raw)
                    if msg.get("topic") != TOPIC:
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
                                kl = await REST.fetch_ohlcv(config.PAIR, timeframe=config.INTERVAL + 'm', since=since, limit=len(missing)+2)
                                df_miss = (pd.DataFrame(kl, columns=["ts","o","h","l","c","v"])\
                                               .set_index("ts"))
                                df_miss.index = pd.to_datetime(df_miss.index, unit='ms', utc=True)
                                hist = (pd.concat([hist, df_miss])
                                          .drop_duplicates()
                                          .sort_index()
                                          .tail(config.LOOKBACK_BARS))
                                break
                            except Exception as e:
                                logging.error("REST back‑fill error: %s", e)
                                await asyncio.sleep(0.5)
                    last_ts = current_end  # move pointer

                  
                    ts = datetime.fromtimestamp(current_end, tz=timezone.utc)
                    new  = pd.DataFrame([[ts,
                                         float(kline["open"]),
                                         float(kline["high"]),
                                         float(kline["low"]),
                                         float(kline["close"]),
                                         float(kline["volume"])]],
                                       columns=["ts","o","h","l","c","v"]).set_index("ts")

                    hist = pd.concat([hist, new]).tail(config.LOOKBACK_BARS)
                    hist = compute_indicators(hist)

                    if len(hist) >= MAX_PERIODS:
                        logging.info("Warm‑up already satisfied – live trading ENABLED")

                    bar = hist.iloc[-1]
                    if bar[["atr","adx","k_fast"]].isna().any():
                        continue  # indicator NA guard
                    # higher‑TF context
                    try:
                        htf_levels = update_htf_levels(hist)
                        htf_row    = htf_levels.loc[bar.name]
                    except KeyError:
                            continue      

                    # Signals
                    i = len(hist) - 1
                    # four_h = hist['c'].iloc[:i+1].resample('4h').last()
                    # trend = four_h.pct_change().rolling(3).mean().abs().iloc[-1]
                    # if trend < 0.006:          # < 0.6 % move in the last 3 days ⇒ chop
                        # continue
                    if tjr_long_signal(hist, i, htf_row):
                        logging.info("Long‑signal %s  k_fast %.1f  adx %.1f",
                                     bar.name, bar.k_fast, bar.adx)
                        alert_side(bar, "LONG")

                    elif tjr_short_signal(hist, i, htf_row):
                        logging.info("Short‑signal %s  k_fast %.1f  adx %.1f",
                                     bar.name, bar.k_fast, bar.adx)
                        alert_side(bar, "SHORT")
                    elif logging.getLogger().isEnabledFor(logging.INFO):
                        logging.info("No‑trade %s  k_fast %.1f  adx %.1f",
                                     bar.name, bar.k_fast, bar.adx)
        except Exception as exc:
            logging.error("WS stream error: %s\n%s", exc, traceback.format_exc())
            await asyncio.sleep(5)        # back‑off then reconnect


# ───────────────────────────────── entry‑point ───────────────────────────────
if __name__ == "__main__":
    logging.info("SOL/USDT TJR 15‑m signal bot starting  %s", datetime.utcnow())
    try:
        asyncio.run(kline_stream())
    finally:
        # ensure REST client closes cleanly
        try:
            asyncio.get_event_loop().run_until_complete(REST.close())
        except Exception:
            pass

