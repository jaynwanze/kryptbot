# Key changes from the previous 15‑minute version
# 1) 15‑minute candles → more signals.
# 2) ATR_SL_MULT = 1 (risk 1 ATR)  |  ATR_TP_MULT = 3 (target 3 ATR).
# 3) VOL_MULT 1.15 instead of 1.3 (a bit easier to trigger).
# 4) Cache length & topic strings adapted for 15‑minute Bybit V5 stream.
# -----------------------------------------------------------------------------
import asyncio, json, time, websockets, pandas as pd, ta, ccxt
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import Optional
from ta.trend import ADXIndicator
from telegram import Bot  # python-telegram-bot==13.*
import numpy as np

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SYMBOL          = "SOLUSDT"          # Bybit perp symbol for WS
PAIR_CCXT       = "SOL/USDT:USDT"    # ccxt inverse‑swap symbol
TF_LIVE         = "15m"               # our working timeframe (string for ccxt)
TF_H1           = "1h"               # higher‑TF bias
WS_INTERVAL     = "15"               # Bybit topic interval ("15" not "15m")

EMA_FAST, EMA_MID, EMA_SLOW = 7, 14, 28
ADX_LEN       = 14
ADX_THRES     = 10
VOL_MULT      = 1.05                # relaxed volume filter
HIGHER_TF_BIAS = False
REFRESH_H1_SECS = 60*12              # every 12 minutes
CACHE_LEN     = 2000                 # ~7 days of 5‑min bars
ATR_SL_MULT   = 1.0                  # 1 ATR risk
ATR_TP_MULT   = 3.0                  # 3 ATR target (≈3 : 1 R:R)
BREAKEVEN_AFTER_ATR = 1.0            # move SL→entry after +1 ATR
RSI_LONG_MIN  = 52     # need strength to go long
RSI_SHORT_MAX = 48     # need weakness to short
MIN_GAP_ATR = 0.08      # ≥ 0.25 × ATR between each pair of EMAs
TREND_LEN = 50



TELE_TOKEN = "8075330249:AAEhi81w2PsQmfPqy0OCrw3Gu25bSOqRLY0"        # <–– replace
CHAT_ID    = 123456789               # <–– replace
# ─────────────────────────────────────────────────────────────────────────────

bot  = Bot(TELE_TOKEN)
exch = ccxt.bybit({"enableRateLimit": True})

# ─── util --------------------------------------------------------------------
async def send_alert(text: str):
    print(text)
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

# ─── higher‑TF bias ----------------------------------------------------------
bias_long, last_h1 = None, 0
async def refresh_h1_bias():
    global bias_long, last_h1
    if not HIGHER_TF_BIAS or time.time()-last_h1 < REFRESH_H1_SECS:
        return
    ohlc = exch.fetch_ohlcv(PAIR_CCXT, TF_H1, limit=250)
    h1   = pd.DataFrame(ohlc, columns="ts o h l c v".split()).astype(float)
    ema50, ema200 = h1.c.ewm(span=50).mean().iloc[-1], h1.c.ewm(span=200).mean().iloc[-1]
    bias_long = ema50 > ema200
    last_h1   = time.time()
    print(f"[H1 bias] EMA50={ema50:.2f} / EMA200={ema200:.2f} → {'LONG' if bias_long else 'SHORT'}")

# ─── websocket stream (5‑minute) --------------------------------------------
async def kline_stream():
    print("[WS] Connecting to Bybit stream …")
    uri = "wss://stream.bybit.com/v5/public/linear"
    async for ws in websockets.connect(uri, ping_interval=20):
        try:
            topic = f"kline.{WS_INTERVAL}.{SYMBOL}"
            await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("topic", "").startswith("kline"):
                    yield msg["data"][0]
        except websockets.ConnectionClosed:
            print("WS reconnect …")
            continue

# ─── indicator helper --------------------------------------------------------
def add_indicators(df: pd.DataFrame):
    df["ema_fast"] = df.close.ewm(span=EMA_FAST).mean()
    df["ema_mid"]  = df.close.ewm(span=EMA_MID ).mean()
    df["ema_slow"] = df.close.ewm(span=EMA_SLOW).mean()

    adx = ADXIndicator(high=df.high, low=df.low, close=df.close, window=ADX_LEN)
    df["adx"]    = adx.adx()
    df["di_pos"] = adx.adx_pos()
    df["di_neg"] = adx.adx_neg()

    df["vol_sma20"] = df.volume.rolling(20).mean()
    df["atr"]        = ta.volatility.average_true_range(df.high, df.low, df.close, 14)
    df["rsi"]        = ta.momentum.rsi(df.close, 14)

# ─── signal filter -----------------------------------------------------------
def qualified_signal(df: pd.DataFrame) -> Optional[str]:
    # Returns 'bull', 'bear' or None for the last finished candle.
    # Implements:
    #   • triple-cross: EMA7 must have crossed BOTH EMA14 & EMA28 on
    #     the just-closed bar
    #   • gap filter : fast-mid and mid-slow gaps ≥ MIN_GAP_ATR × ATR
    # Everything else (ADX, volume, RSI, etc.) stays the same.
    if len(df) < EMA_SLOW + 2:
        return None

    prev, cur = df.iloc[-2], df.iloc[-1]

    # --- 1. triple-cross test ----------------------------------------------
    bull_x = (
        prev.ema_fast < prev.ema_mid and prev.ema_fast < prev.ema_slow and   # below both
        cur.ema_fast  > cur.ema_mid  and cur.ema_fast  > cur.ema_slow        # now above both
    )
    bear_x = (
        prev.ema_fast > prev.ema_mid and prev.ema_fast > prev.ema_slow and   # above both
        cur.ema_fast  < cur.ema_mid  and cur.ema_fast  < cur.ema_slow        # now below both
    )
    if not (bull_x or bear_x):
        return None

    # --- 2. “decent gap” filter --------------------------------------------
    atr_gap = cur.atr * MIN_GAP_ATR
    if not (
        (abs(cur.ema_fast - cur.ema_mid) >= atr_gap) and
        (abs(cur.ema_mid  - cur.ema_slow) >= atr_gap)
    ):
        return None

    # --- 3. keep the existing quality gates -------------------------------
    if ADX_THRES and cur.adx < ADX_THRES:
        return None             # skip signal

    if bull_x and cur.di_pos <= cur.di_neg:
        return None
    if bear_x and cur.di_neg <= cur.di_pos:
        return None
    if cur.volume < cur.vol_sma20 * VOL_MULT:
        return None
    if bull_x and cur.rsi < RSI_LONG_MIN:
        return None
    if bear_x and cur.rsi > RSI_SHORT_MAX:
        return None
    # if cur.rsi < RSI_MIN:
    #     return None
    if HIGHER_TF_BIAS:
        if bull_x and bias_long is False:
            return None
        if bear_x and bias_long is True:
            return None

    return "bull" if bull_x else "bear"


# ─── position tracking -------------------------------------------------------
@dataclass
class Position:
    side: str; entry: float; sl: float; tp: float; opened: pd.Timestamp; risk: float; breakeven: bool=False

# ─── main coroutine ----------------------------------------------------------
async def main():
    buf, pos = deque(maxlen=CACHE_LEN), None
    async for k in kline_stream():
        await refresh_h1_bias()
        buf.append(k)
        if not k["confirm"]: continue  # still forming candle
        df = pd.DataFrame(list(buf)).astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
        df["ts"] = pd.to_datetime(df.start, unit="ms", utc=True); df.set_index("ts", inplace=True)
        add_indicators(df)
        bar, atr = df.iloc[-1], df.atr.iloc[-1]

        # manage open trade ---------------------------------------------------
        if pos:
         hit_tp = bar.low  <= pos.tp <= bar.high
         hit_sl = bar.low  <= pos.sl <= bar.high

        if hit_tp and hit_sl:          # rare “both” case
            # safer to count as SL (worst-case) or skip the trade
            outcome = "SL"
        elif hit_tp:
            outcome = "TP"
        elif hit_sl:
            outcome = "SL"
        else:
            outcome = None
   # price moved UP to stop
        if hit_tp or hit_sl:
                txt = "TP" if hit_tp else "SL"
                await send_alert(f"✅ {pos.side.upper()} {txt} @ {pos.tp if hit_tp else pos.sl:.2f}")
                pos = None
        elif not pos.breakeven:
                move = (bar.close >= pos.entry+BREAKEVEN_AFTER_ATR*atr if pos.side=="long" else bar.close<=pos.entry-BREAKEVEN_AFTER_ATR*atr)
                if move:
                    pos.sl = pos.entry; pos.breakeven=True
                    await send_alert(f"🔒 {pos.side.upper()} BE locked @ {pos.entry:.2f}")
        elif pos.breakeven:
           two_r = pos.entry + 2 * pos.risk if pos.side == "long" else pos.entry - 2 * pos.risk
           if (pos.side == "long" and bar.close >= two_r):
               new_sl = bar.close - atr
               pos.sl = max(pos.sl, new_sl)  # Only move up, never down
           elif (pos.side == "short" and bar.close <= two_r):
               new_sl = bar.close + atr
               pos.sl = min(pos.sl, new_sl)  # Only move down, never up

        # check new signal ----------------------------------------------------
        sig = qualified_signal(df)
        if sig and pos is None:
            side   = "long" if sig=="bull" else "short"
            entry  = bar.close
            sl     = entry-ATR_SL_MULT*atr if side=="long" else entry+ATR_SL_MULT*atr
            tp     = entry+ATR_TP_MULT*atr if side=="long" else entry-ATR_TP_MULT*atr
            risk0 = abs(entry-sl)  # risk per contract
            pos    = Position(side, entry, sl, tp, bar.name, risk0)
            await send_alert(f"🚀 NEW {side.upper()} @ {entry:.2f} | SL {sl:.2f} | TP {tp:.2f} | ADX {bar.adx:.1f}")

# ─── heartbeat ---------------------------------------------------------------
async def heartbeat():
    while True:
        print(f"[{datetime.utcnow()}] alive …")
        await asyncio.sleep(60)

# ─── run ---------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting KryptBot (15‑min, 3 : 1) …")
    asyncio.run(asyncio.gather(main(), heartbeat()))