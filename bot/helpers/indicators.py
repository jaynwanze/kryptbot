import numpy as np
import pandas as pd

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # EMAs
    df["ema7"]  = df.c.ewm(span=7,  adjust=False).mean()
    df["ema14"] = df.c.ewm(span=14, adjust=False).mean()
    df["ema21"] = df.c.ewm(span=21, adjust=False).mean()
    df["ema28"] = df.c.ewm(span=28, adjust=False).mean()
    df["ema50"] = df.c.ewm(span=50, adjust=False).mean()
    df["ema200"] = df.c.ewm(span=200, adjust=False).mean()  # TREND FILTER

    # ATR
    tr = np.maximum.reduce([
        df.h - df.l,
        (df.h - df.c.shift()).abs(),
        (df.l - df.c.shift()).abs(),
    ])
    df["atr"]   = pd.Series(tr, index=df.index).rolling(14).mean()
    df["atr30"] = df["atr"].rolling(30).mean()

    # RSI
    win = 14
    delta = df.c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/win, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/win, adjust=False).mean()
    rsi   = 100 - 100 / (1 + gain / loss)
    df["rsi"] = rsi

    # StochRSI (classic fast→slow chain)
    rsi_min = rsi.rolling(win).min()
    rsi_max = rsi.rolling(win).max()
    rng     = (rsi_max - rsi_min).replace(0, np.nan)

    k_raw   = 100 * (rsi - rsi_min) / rng
    k_fast  = k_raw.rolling(3).mean()
    d_fast  = k_fast.rolling(3).mean()
    k_slow  = d_fast
    d_slow  = k_slow.rolling(3).mean()

    df["k_fast"] = k_fast.clip(0, 100)
    df["d_fast"] = d_fast.clip(0, 100)
    df["k_slow"] = k_slow.clip(0, 100)
    df["d_slow"] = d_slow.clip(0, 100)

    # ADX
    up_move   = df.h.diff()
    down_move = -df.l.diff()
    plus_dm   = np.where((up_move > down_move) & (up_move > 0),  up_move,  0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_n     = pd.Series(tr, index=df.index).rolling(14).sum()
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).rolling(14).sum() / tr_n
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).sum() / tr_n
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df["adx"]      = dx.rolling(14).mean()
    df["adx_prev"] = df["adx"].shift()

    df["vol20"] = df.v.rolling(20).mean()
    return df
