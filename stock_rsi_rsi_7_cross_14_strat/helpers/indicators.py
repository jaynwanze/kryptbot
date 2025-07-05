import numpy as np
import pandas as pd

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
    df["rsi"] = rsi                       # <‑‑ stored for signal logic
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
