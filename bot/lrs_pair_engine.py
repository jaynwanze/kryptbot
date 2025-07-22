#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  Multi‑pair 15‑minute live signal engine  –  TJR strategy (Jul‑2025)
# ────────────────────────────────────────────────────────────────
import os, json, asyncio, logging, math, time, traceback
from   datetime import datetime, timezone
from   typing   import Dict, List

import ccxt.async_support as ccxt
import numpy  as np
import pandas as pd
import websockets
from   telegram import Bot
from   dotenv   import load_dotenv

from helpers import (
    config,
    compute_indicators,
    build_htf_levels,
    tjr_long_signal,
    tjr_short_signal,
    update_htf_levels_new,
)
from data import preload_history

# ───────── pairs you want to trade concurrently ─────────
PAIRS: List[str] = ["SOLUSDT", "ATOMUSDT", "WAVESUSDT", "XRPUSDT"]

# ───────── misc constants (shared across pairs) ─────────
TF         = config.INTERVAL           # "15"  …minutes
TF_SEC     = int(TF) * 60
LOOKBACK   = 1_000                     # bars per pair
MAX_RETRY  = 3
WS_URL     = "wss://stream.bybit.com/v5/public/linear"
PING_SEC   = 20                        # WS heartbeat

# ───────── Telegram  ─────────
load_dotenv()
TG_TOKEN   = os.getenv("TELE_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
bot        = Bot(token=TG_TOKEN)

# ────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────
def alert_side(pair: str, bar: pd.Series, side: str) -> None:
    """Send a nicely‑formatted alert to Telegram."""
    stop_off = (config.ATR_MULT_SL * 1.6 + config.WICK_BUFFER) * bar.atr
    if side == "LONG":
        sl = bar.c - stop_off
        tp = bar.c + config.ATR_MULT_TP * bar.atr
        emoji = "📈"
    else:
        sl = bar.c + stop_off
        tp = bar.c - config.ATR_MULT_TP * bar.atr
        emoji = "📉"

    msg = (
        "*LRS MULTI PAIR ENGINE LIVE TRADING BOT*\n"
        f"{emoji} *{pair} {TF}‑m {side} signal*\n"
        f"`{bar.name:%Y‑%m‑%d %H:%M}` UTC\n"
        f"Entry  : `{bar.c:.4f}`\n"
        f"Stop   : `{sl:.4f}`\n"
        f"Target : `{tp:.4f}`\n"
        f"ADX    : `{bar.adx:.1f}`  |  StochK: `{bar.k_fast:.1f}`"
    )
    try:
        bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")
        logging.info("Telegram alert sent  – %s  %s", pair, side)
    except Exception as exc:
        logging.error("Telegram error: %s", exc)

# ────────────────────────────────────────────────────────────────
#  One web‑socket task  (1 WS ⟷ 1 pair)
# ────────────────────────────────────────────────────────────────
async def kline_stream(pair: str) -> None:
    topic = f"kline.{TF}.{pair}"
    rest  = ccxt.bybit({"enableRateLimit": True})

    # preload history + indicators + HTF context
    hist        = await preload_history(symbol=pair, interval=TF, limit=LOOKBACK)
    htf_levels  = build_htf_levels(hist.copy())
    logging.info("[%s] history loaded  %d bars (%s → %s)",
                 pair, len(hist), hist.index[0], hist.index[-1])

    MAX_WARM = 200                      # indicators warm‑up
    last_ts  = None                     # to spot missed candles
    heartbeat_log = time.time()

    while True:                         # auto‑reconnect outer loop
        try:
            async with websockets.connect(
                WS_URL, ping_interval=PING_SEC, ping_timeout=PING_SEC*2
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                logging.info("[%s] subscribed  (%s)", pair, topic)

                async for raw in ws:
                    if time.time() - heartbeat_log > 600:
                        logging.info("[%s] heartbeat OK  %s", pair, datetime.now(timezone.utc))
                        heartbeat_log = time.time()

                    msg = json.loads(raw)
                    if msg.get("topic") != topic:
                        continue
                    kline = msg["data"][0]
                    if not kline["confirm"]:
                        continue        # skip still‑forming candle

                    current_end = int(kline["end"]) // 1000
                    if last_ts is None:
                        last_ts = current_end - TF_SEC
                    expected = last_ts + TF_SEC

                    # ── fill WS gaps via REST ──
                    if current_end > expected:
                        missing = list(range(expected, current_end, TF_SEC))
                        logging.warning("[%s] WS gap – back‑fill %d candle(s)", pair, len(missing))
                        since = (missing[0] - TF_SEC) * 1000
                        for _ in range(MAX_RETRY):
                            try:
                                kl = await rest.fetch_ohlcv(pair, timeframe=f"{TF}m",
                                                            since=since, limit=len(missing)+2)
                                df_miss = (pd.DataFrame(kl, columns=["ts","o","h","l","c","v"])
                                             .set_index("ts"))
                                df_miss.index = pd.to_datetime(df_miss.index, unit='ms', utc=True)
                                hist = (pd.concat([hist, df_miss])
                                          .drop_duplicates()
                                          .sort_index()
                                          .tail(LOOKBACK))
                                break
                            except Exception as e:
                                logging.error("[%s] REST back‑fill error: %s", pair, e)
                                await asyncio.sleep(0.5)

                    last_ts = current_end

                    # ── append new confirmed bar ──
                    ts  = datetime.fromtimestamp(current_end, tz=timezone.utc)
                    new = pd.DataFrame([[ts,
                                         float(kline["open"]),
                                         float(kline["high"]),
                                         float(kline["low"]),
                                         float(kline["close"]),
                                         float(kline["volume"])]],
                                       columns=["ts","o","h","l","c","v"]).set_index("ts")
                    hist = pd.concat([hist, new]).tail(LOOKBACK)
                    hist = compute_indicators(hist)

                    # indicator guard
                    bar = hist.iloc[-1]
                    if bar[["atr","adx","k_fast"]].isna().any():
                        continue
                    try:
                        htf_row = htf_levels.loc[bar.name]
                    except KeyError:
                        continue

                    # adx & volatility veto
                    vol_norm = bar.atr / bar.atr30
                    if bar.adx < 10 + 8*vol_norm:
                        continue
                    if bar.atr < (0.5 + 0.3*vol_norm) * bar.atr30:
                        continue

                    i = len(hist) - 1
                    if tjr_long_signal(hist, i, htf_row):
                        logging.info("[%s] LONG signal  %.1f/%.1f", pair, bar.k_fast, bar.adx)
                        alert_side(pair, bar, "LONG")
                    elif tjr_short_signal(hist, i, htf_row):
                        logging.info("[%s] SHORT signal %.1f/%.1f", pair, bar.k_fast, bar.adx)
                        alert_side(pair, bar, "SHORT")
                    elif logging.getLogger().isEnabledFor(logging.INFO):
                        logging.info("No‑trade %s  k_fast %.1f  adx %.1f",
                                     bar.name, bar.k_fast, bar.adx)              

                    # maintain HTF map
                    htf_levels = update_htf_levels_new(htf_levels, bar)

        except Exception as exc:
            logging.error("[%s] WS error: %s\n%s", pair, exc, traceback.format_exc())
            await asyncio.sleep(5)      # simple back‑off & retry

# ────────────────────────────────────────────────────────────────
#  Main entry‑point
# ────────────────────────────────────────────────────────────────
async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    logging.info("Multi‑pair engine starting  %s", datetime.utcnow().strftime("%F %T"))

    # spawn one WS task per pair
    tasks = [asyncio.create_task(kline_stream(p)) for p in PAIRS]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # close any ccxt instances cleanly
        try:
            loop = asyncio.get_event_loop()
            for task in asyncio.all_tasks(loop):
                if isinstance(task.result(), ccxt.Exchange):
                    loop.run_until_complete(task.result().close())
        except Exception:
            pass
