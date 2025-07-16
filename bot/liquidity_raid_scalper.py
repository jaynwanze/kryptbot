#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────
#  SOL/USDT – 15‑minute live signal bot (TJR strategy, July 2025)
# ────────────────────────────────────────────────────────────────
import os, json, asyncio, math, logging, time, traceback
from   datetime import datetime, timezone
from   typing   import Optional, Tuple

import numpy  as np
import pandas as pd
import websockets
from   telegram import Bot
from   dotenv   import load_dotenv

from helpers import (
    config,
    compute_indicators,        # → ATR, ADX, Stoch, RSI, …
    build_h1, update_h1, h1_row,     # quick bias filters
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

# ────────────────────────────────────────────────────────────────
#  Bybit Web‑socket constants
# ────────────────────────────────────────────────────────────────
WS_URL   = "wss://stream.bybit.com/v5/public/linear"
TOPIC    = f"kline.{config.INTERVAL}.{config.PAIR}"   # e.g. kline.15.SOLUSDT
PING_SEC = 20


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

    while True:        # auto‑reconnect loop
        try:
            async with websockets.connect(
                WS_URL, ping_interval=PING_SEC
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

                    # 2) Add the freshly closed 15‑min bar to `hist`
                    end_ts = kline.get("end")
                    if not isinstance(end_ts, (int, float)) or end_ts < 0:
                        logging.error("Invalid k['end']: %r", end_ts)
                        continue
                    if end_ts > 1e12:  # convert ms to s if needed
                        end_ts = end_ts / 1000
                    ts = datetime.fromtimestamp(end_ts, tz=timezone.utc)
                    new  = pd.DataFrame([[ts,
                                         float(kline["open"]),
                                         float(kline["high"]),
                                         float(kline["low"]),
                                         float(kline["close"]),
                                         float(kline["volume"])]],
                                       columns=["ts","o","h","l","c","v"]).set_index("ts")

                    hist = pd.concat([hist, new]).tail(config.LOOKBACK_BARS)
                    hist = compute_indicators(hist)

                    # (re)build HTF table – cheap for ≤ 1 000 rows, but
                    # TO:DO optimise by only refreshing at H4 closes
                    htf_levels = build_htf_levels(hist)

                    bar  = hist.iloc[-1]
                    prev = hist.iloc[-2]
                    htf_row = htf_levels.loc[bar.name]

                    if bar[["atr","adx","k_fast"]].isna().any():
                        continue      # indicators still warming up

                    # 3) Signals
                    i = len(hist) - 1
                    if tjr_long_signal(hist, i, htf_row):
                        logging.info("LONG – k_fast %.1f  adx %.1f  fvg_up %s  bos_up %s",
                                     bar.k_fast, bar.adx,
                                     htf_row.fvg_up, htf_row.bos_up)
                        alert_side(bar, "LONG")

                    elif tjr_short_signal(hist, i, htf_row):
                        logging.info("SHORT – k_fast %.1f  adx %.1f  fvg_dn %s  bos_dn %s",
                                     bar.k_fast, bar.adx,
                                     htf_row.fvg_dn, htf_row.bos_dn)
                        alert_side(bar, "SHORT")

        except Exception as exc:
            logging.error("WS stream error: %s\n%s", exc, traceback.format_exc())
            await asyncio.sleep(5)        # back‑off then reconnect


# ───────────────────────────────── entry‑point ───────────────────────────────
if __name__ == "__main__":
    logging.info("SOL/USDT TJR 15‑m signal bot starting %s", datetime.utcnow())
    asyncio.run(kline_stream())
