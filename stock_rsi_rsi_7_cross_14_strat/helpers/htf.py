import pandas as pd

def update_h1(h1: pd.DataFrame, ts: pd.Timestamp, close: float) -> pd.DataFrame:
    stamp = ts.floor("1H")
    if stamp not in h1.index:
        h1.loc[stamp, "close"] = close
    else:
        h1.at[stamp, "close"] = close
    h1["ema50"] = h1.close.ewm(span=50).mean()
    h1["slope"] = h1.ema50.diff(3)
    return h1

def h1_row(h1: pd.DataFrame, ts: pd.Timestamp) -> pd.Series:
    return h1.loc[ts.floor("1H")]

def build_h1(df15: pd.DataFrame) -> pd.DataFrame:
    h1 = (df15.c.resample("1H").last().to_frame("close").ffill())
    h1["ema50"] = h1.close.ewm(span=50).mean()
    h1["slope"] = h1.ema50.diff(3)
    return h1