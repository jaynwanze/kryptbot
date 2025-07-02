import os, json, asyncio, math, logging, time
from   datetime import datetime, timezone

from typing import Tuple
import numpy  as np
import pandas as pd
import websockets
from   telegram import Bot
from   dotenv   import load_dotenv
import traceback



logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s – %(levelname)s – %(message)s")

# ────────────────────────────────────────────────────────────────
#  Configuration
# ────────────────────────────────────────────────────────────────
PAIR            = "SOLUSDT"          # Bybit symbol
TF_SECONDS      = 15 * 60            # 15‑minute bars
INTERVAL        = "15"               # stream interval, string
LOOKBACK_BARS   = 800                # kept in memory (≈ 8 days)

# Strategy param
RISK_PCT        = 0.02               # not used (alerts only)
ATR_MULT_SL     = 2.0
ATR_MULT_TP     = 4.0                # RR 2:1
WICK_BUFFER     = 0.25               # extra ATR cushion
ADX_FLOOR       = 20
STO_K_MIN_LONG  = 45
STO_K_MIN_SHORT = 30

# Telegram
load_dotenv()
TG_TOKEN   = os.getenv("TELE_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
bot        = Bot(token=TG_TOKEN)

# WebSocket
WS_URL     = "wss://stream.bybit.com/v5/public/linear"
TOPIC      = f"kline.{INTERVAL}.{PAIR}"
PING_SEC   = 20

# ────────────────────────────────────────────────────────────────
#  Indicator helpers (vectorised over a DataFrame)
# ────────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure *df* has o,h,l,c,v columns.  Adds all lenses we need."""
    # EMAs
    df["ema7"]   = df.c.ewm(span=7).mean()
    df["ema14"]  = df.c.ewm(span=14).mean()

    # ATR
    tr           = np.maximum.reduce([
                        df.h - df.l,
                        (df.h - df.c.shift()).abs(),
                        (df.l - df.c.shift()).abs(),
                    ])
    df["atr"]    = pd.Series(tr, index=df.index).rolling(14).mean()

    # RSI / Stoch‑RSI
    delta        = df.c.diff()
    gain         = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss         = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi          = 100 - 100/(1+gain/loss)
    rsi_min      = rsi.rolling(14).min();   rsi_max = rsi.rolling(14).max()
    df["k_fast"] = ((rsi - rsi_min)/(rsi_max - rsi_min)).rolling(3).mean()*100

    # ADX
    plus_dm      = np.where(df.h.diff() > df.l.diff(),
                            df.h.diff().clip(lower=0), 0)
    minus_dm     = np.where(df.l.diff() > df.h.diff(),
                            df.l.diff().abs(), 0)
    tr_n         = pd.Series(tr, index=df.index).rolling(14).sum()
    plus_di      = 100 * pd.Series(plus_dm, index=df.index).rolling(14).sum() / tr_n
    minus_di     = 100 * pd.Series(minus_dm, index=df.index).rolling(14).sum() / tr_n
    dx           = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df["adx"]    = dx.rolling(14).mean()

    # Volume MA (optional filter)
    df["vol20"]  = df.v.rolling(20).mean()

    return df

# ────────────────────────────────────────────────────────────────
#  Signal logic - 
# ────────────────────────────────────────────────────────────────


def h1_trend(df_15: pd.DataFrame) -> Tuple[bool, float]:
    """Return (is_uptrend, ema50_slope) from the *latest* 1‑hour data."""
    h1 = df_15.c.resample("1H").last().ffill()
    h1["ema50"] = h1.close.ewm(span=50).mean()
    h1["slope"] = h1.ema50.diff(3)
    last        = h1.iloc[-1]
    return last.close > last.ema50, last.slope


def long_signal(bar, prev, trend_ok, slope_ok) -> bool:
    cross_up = bar.ema7 > bar.ema14 and prev.ema7 <= prev.ema14
    return (cross_up and trend_ok and slope_ok > 0 and
            bar.k_fast > STO_K_MIN_LONG and
            bar.rsi     > 45               and
            bar.adx     >= ADX_FLOOR)


def short_signal(bar, prev, trend_ok, slope_ok) -> bool:
    cross_dn = bar.ema7 < bar.ema14 and prev.ema7 >= prev.ema14
    return (cross_dn and (not trend_ok) and slope_ok < 0 and
            bar.k_fast > STO_K_MIN_SHORT and
            bar.rsi     > 30               and
            bar.adx     >= ADX_FLOOR)

# ────────────────────────────────────────────────────────────────
#  Telegram alert
# ────────────────────────────────────────────────────────────────

def alert_side(bar, side: str):
    """Send a nicely formatted Telegram alert."""
    stop_off = (ATR_MULT_SL * 1.6 + WICK_BUFFER) * bar.atr
    if side == "LONG":
        sl = bar.c - stop_off
        tp = bar.c + ATR_MULT_TP * bar.atr
        emoji = "📈"
    else:  # SHORT
        sl = bar.c + stop_off
        tp = bar.c - ATR_MULT_TP * bar.atr
        emoji = "📉"

    msg = (
        f"{emoji} *{PAIR} {INTERVAL}‑m {side} signal*\n"
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
    hist = pd.DataFrame()   # stores last LOOKBACK_BARS 15‑m bars
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 600  # seconds (10 minutes)

    while True:   # auto‑reconnect
        try:
            async with websockets.connect(WS_URL, ping_interval=PING_SEC) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [TOPIC]}))
                logging.info("Subscribed to %s", TOPIC)

                async for raw in ws:
                        # Heartbeat: log or send Telegram message every 10 minutes
                    now = time.time()
                    if now - last_heartbeat > HEARTBEAT_INTERVAL:
                        logging.info("Heartbeat: bot is alive at %s", datetime.now(timezone.utc))
                        # Optional: send Telegram heartbeat
                        # try:
                        #     bot.send_message(chat_id=TG_CHAT_ID, text="❤️ Bot heartbeat: alive at {}".format(datetime.datetime.now(datetime.timezone.utc)), parse_mode="Markdown")
                        # except Exception as exc:
                        #     logging.error("Telegram heartbeat error: %s", exc)
                        last_heartbeat = now
                    msg = json.loads(raw)
                    if msg.get("topic") != TOPIC:
                        continue

                    k = msg["data"][0]
                    if not k["confirm"]:   # skip live/unclosed candles
                        continue

                    end_ts = k.get("end")
                    if not isinstance(end_ts, (int, float)) or end_ts < 0:
                        logging.error("Invalid k['end']: %r", end_ts)
                        continue
                    if end_ts > 1e12:  # convert ms to s if needed
                        end_ts = end_ts / 1000
                    ts = datetime.fromtimestamp(end_ts, tz=timezone.utc)
                    new = pd.DataFrame([[ts,
                                         float(k["open"]),
                                         float(k["high"]),
                                         float(k["low"]),
                                         float(k["close"]),
                                         float(k["volume"]) ]],
                                        columns=["ts", "o", "h", "l", "c", "v" ]
                                        ).set_index("ts")

                    hist = pd.concat([hist, new]).tail(LOOKBACK_BARS)
                    hist = compute_indicators(hist)

                    if len(hist) < 50:   # need warm‑up for EMAs/ATR
                        continue

                    bar  = hist.iloc[-1]
                    prev = hist.iloc[-2]

                    trend_up, slope = h1_trend(hist)

                    if long_signal(bar, prev, trend_up, slope):
                        alert_side(bar, "LONG")
                    elif short_signal(bar, prev, trend_up, slope):
                        alert_side(bar, "SHORT")

        except Exception as exc:
              logging.error("WS error: %s\n%s", exc, traceback.format_exc())
              await asyncio.sleep(5)

# ────────────────────────────────────────────────────────────────
#  Entry point
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.info("SOL/USDT 15‑m signal bot starting – %s", datetime.now(timezone.utc))
    logging.info("Telegram chat id  : %s", TG_CHAT_ID)
    asyncio.run(kline_stream())
