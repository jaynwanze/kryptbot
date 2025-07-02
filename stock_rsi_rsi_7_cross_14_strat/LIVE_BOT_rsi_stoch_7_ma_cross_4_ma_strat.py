import os, json, asyncio, math, websockets, pandas as pd, numpy as np
from unicodedata import name
from datetime import datetime, timezone
from telegram import Bot
from dotenv import load_dotenv

# ───────── USER SETTINGS ──────────────────────────────────────────


load_dotenv()
TG_TOKEN = os.getenv("TELE_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
print("Loaded TG_CHAT_ID:", TG_CHAT_ID)


PAIR       = "SOLUSDT"          # Bybit symbol for the stream
INTERVAL   = "15"               # 15-minute kline
ADX_FLOOR  = 20
# VOL_MULT   = 1.6
STO_K_MIN  = 50
ATR_SL, ATR_TP = 2.0, 4.0       # info only (not auto-trading)

bot = Bot(token=TG_TOKEN)

# ───────── HELPERS: indicator calcs on a DataFrame row  ───────────
def add_indis(df: pd.DataFrame) -> pd.DataFrame:
    df["ema7"]  = df.c.ewm(span=7).mean()
    df["ema14"] = df.c.ewm(span=14).mean()
    tr = np.maximum.reduce([df.h-df.l,
                            (df.h-df.c.shift()).abs(),
                            (df.l-df.c.shift()).abs()])
    df["atr"] = pd.Series(tr,index=df.index).rolling(14).mean()

    # RSI + Stoch-RSI
    delta = df.c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi   = 100 - 100/(1+gain/loss)
    rsi_min = rsi.rolling(14).min(); rsi_max = rsi.rolling(14).max()
    df["k_fast"] = ((rsi-rsi_min)/(rsi_max-rsi_min)).rolling(3).mean()*100

    # ADX
    plus_dm  = np.where(df.h.diff()>df.l.diff(), df.h.diff().clip(lower=0), 0)
    minus_dm = np.where(df.l.diff()>df.h.diff(), df.l.diff().abs(), 0)
    tr_n     = pd.Series(tr,index=df.index).rolling(14).sum()
    plus_di  = 100*pd.Series(plus_dm,index=df.index).rolling(14).sum()/tr_n
    minus_di = 100*pd.Series(minus_dm,index=df.index).rolling(14).sum()/tr_n
    dx       = 100*(plus_di-minus_di).abs()/(plus_di+minus_di)
    df["adx"] = dx.rolling(14).mean()

    df["vol20"] = df.v.rolling(20).mean()
    return df

# ───────── TELEGRAM  ──────────────────────────────────────────────
async def send_alert(bar):
    stop = bar.c - ATR_SL*bar.atr
    tp   = bar.c + ATR_TP*bar.atr
    msg  = (
      f"📈 *SOL 15-m LONG signal*\n"
      f"`{bar.name:%Y-%m-%d %H:%M}` UTC\n"
      f"Entry  : {bar.c:.3f}\n"
      f"Stop   : {stop:.3f}\n"
      f"Target : {tp :.3f}\n"
      f"ADX    : {bar.adx:.1f} | Vol gate OK"
    )
    bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")

# ───────── STREAM LOOP ────────────────────────────────────────────
URI = "wss://stream.bybit.com/v5/public/linear"
topic = f"kline.{INTERVAL}.{PAIR}"

async def kline_stream():
    hist = pd.DataFrame()   # will hold last 300 closed bars

    while True:                         # reconnect loop
        try:
            async with websockets.connect(URI, ping_interval=20) as ws:
                await ws.send(json.dumps({"op":"subscribe","args":[topic]}))
                async for raw in ws:
                    msg = json.loads(raw)
                    # test alert (uncomment to enable)
                    # asyncio.create_task(send_test_alert())
                    print(raw)

                    # print(raw)  # debug output
                    if msg.get("topic") != topic: continue
                    k = msg["data"][0]
                    if not k["confirm"]:       # we only care about closed bars
                        continue

                    ts  = datetime.fromtimestamp(k["end"], tz=timezone.utc)
                    new = pd.DataFrame([[ts,
                                         float(k["open"]),
                                         float(k["high"]),
                                         float(k["low"]),
                                         float(k["close"]),
                                         float(k["volume"])]],
                                       columns=["ts","o","h","l","c","v"]
                                       ).set_index("ts")
                    hist = pd.concat([hist, new]).last("400H")
                    hist = add_indis(hist)

                    bar = hist.iloc[-1]
                    cross_up = (
                        (bar.ema7 > bar.ema14)
                        and (hist.ema7.shift().iloc[-1] <= hist.ema14.shift().iloc[-1]) and
                        (bar.ema7 < bar.ema28) and
                        (bar.ema14 < bar.ema28) 
                    )
                               

                    if (cross_up and bar.k_fast > STO_K_MIN and
                        bar.adx >= ADX_FLOOR ):
                        # bar.v   >= VOL_MULT*bar.vol20
                        asyncio.create_task(send_alert(bar))
                        print("Alert sent", bar.name)

        except Exception as e:
            print("WS error:", e, "— reconnecting in 5 s")
            await asyncio.sleep(5)


async def send_test_alert():
    msg = "Test alert from SOLUSDT 15-min stream"
    await bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")
    print("Test alert sent:", msg)
    


if __name__ == "__main__":
    print(f"Starting SOLUSDT 15-min stream at {datetime.now(timezone.utc)}")
    print(f"Telegram alerts to {TG_CHAT_ID} via {TG_TOKEN}")
    asyncio.run(kline_stream())

