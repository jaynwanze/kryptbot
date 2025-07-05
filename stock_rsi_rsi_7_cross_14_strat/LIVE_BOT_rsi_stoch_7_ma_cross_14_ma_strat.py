import os, json, asyncio, math, logging, time
from   datetime import datetime, timezone

import numpy  as np
import pandas as pd
from typing import Tuple, Optional
import websockets
from   telegram import Bot
from   dotenv   import load_dotenv
import traceback
import requests 
from helpers import config, compute_indicators, update_h1, h1_row, long_signal, short_signal, build_h1
from data import preload_history


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s – %(levelname)s – %(message)s")

# Telegram
load_dotenv()
TG_TOKEN   = os.getenv("TELE_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
bot        = Bot(token=TG_TOKEN)

# WebSocket
WS_URL     = "wss://stream.bybit.com/v5/public/linear"
TOPIC      = f"kline.{config.INTERVAL}.{config.PAIR}"
PING_SEC   = 20

def alert_side(bar, side: str):
    """Send a nicely formatted Telegram alert."""
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
        f"`{bar.name:%Y‑%m‑%d %H:%M}` UTC\n"
        f"Entry  : `{bar.c:.3f}`\n"
        f"Stop   : `{sl:.3f}`\n"
        f"Target : `{tp:.3f}`\n"
        f"ADX    : `{bar.adx:.1f}`\n"
        f"StochK : `{bar.k_fast:.1f}`"
    )

    try:
        bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")
        logging.info("Telegram alert sent: %s %s", side, bar.name)
    except Exception as exc:
        logging.error("Telegram error: %s", exc)

# ────────────────────────────────────────────────────────────────
#  WebSocket stream coroutine
# ────────────────────────────────────────────────────────────────
async def kline_stream():
    # -------------------------------------------------------------
    # 1)  Load history and build BOTH 15-m & 1-h frames once
    # -------------------------------------------------------------
    hist = await preload_history(1000)
    h1   = build_h1(hist)

    logging.info("History pre-loaded: %d bars (from %s to %s)",
                 len(hist), hist.index[0], hist.index[-1])

    last_heartbeat     = time.time()
    HEARTBEAT_INTERVAL = 600     # 10 min

    while True:          # auto-reconnect loop
        try:
            async with websockets.connect(WS_URL, ping_interval=PING_SEC) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [TOPIC]}))
                logging.info("Subscribed to %s", TOPIC)

                async for raw in ws:
                    # -------------- heartbeat --------------
                    now = time.time()
                    if now - last_heartbeat > HEARTBEAT_INTERVAL:
                        logging.info("Heartbeat: bot alive at %s",
                                     datetime.now(timezone.utc))
                        last_heartbeat = now

                    msg = json.loads(raw)
                    if msg.get("topic") != TOPIC:
                        continue

                    k = msg["data"][0]
                    if not k["confirm"]:               # skip unclosed candle
                        continue

                    # -------------- build a new 15-m row --------------
                    ts = datetime.fromtimestamp(
                             k["end"]/1000 if k["end"] > 1e12 else k["end"],
                             tz=timezone.utc)

                    new = pd.DataFrame([[ts,
                                         float(k["open"]),
                                         float(k["high"]),
                                         float(k["low"]),
                                         float(k["close"]),
                                         float(k["volume"])]],
                                       columns=["ts","o","h","l","c","v"]
                                      ).set_index("ts")

                    hist = pd.concat([hist, new]).tail(config.LOOKBACK_BARS)
                    hist = compute_indicators(hist)

                    bar, prev = hist.iloc[-1], hist.iloc[-2]
                    if bar[["atr","adx","k_fast"]].isna().any():
                        continue          # warm-up

                    # -------------------------------------------------
                    # 2)  UPDATE the 1-hour table if we crossed an hour
                    # -------------------------------------------------
                    h1 = update_h1(h1, ts, bar.c)

                    # fetch the higher-TF row that matches this 15-m bar
                    h1r = h1_row(h1, ts)

                    # -------------- signals & alerts --------------
                    if long_signal(bar, prev, h1r):
                        logging.info("LONG signal – k_fast %.1f  rsi %.1f  "
                                     "adx %.1f  slope %.4f",
                                     bar.k_fast, bar.rsi, bar.adx, h1r.slope)
                        alert_side(bar, "LONG")

                    elif short_signal(bar, prev, h1r):
                        logging.info("SHORT signal – k_fast %.1f  rsi %.1f  "
                                     "adx %.1f  slope %.4f",
                                     bar.k_fast, bar.rsi, bar.adx, h1r.slope)
                        alert_side(bar, "SHORT")

        except Exception as exc:
            logging.error("WS error: %s\n%s", exc, traceback.format_exc())
            await asyncio.sleep(5)        # back-off then reconnect


# ───────────────────────────────── entrypoint ────────────────────────────────
if __name__ == "__main__":
    logging.info("SOL/USDT 15-m signal bot starting %s", datetime.utcnow())
    asyncio.run(kline_stream())