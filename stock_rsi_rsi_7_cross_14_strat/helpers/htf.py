import pandas as pd


def update_h1(h1: pd.DataFrame, ts, close: float) -> pd.DataFrame:
    ts = pd.Timestamp(ts)
    stamp = ts.floor("1H")
    if stamp not in h1.index:
        h1.loc[stamp, "close"] = close
    else:
        h1.at[stamp, "close"] = close
    h1["ema50"] = h1.close.ewm(span=50).mean()
    h1["slope"] = h1.ema50.diff(3)
    return h1

def h1_row(h1: pd.DataFrame, ts) -> pd.Series:
    """Return the 1-hour row that matches any 15-m timestamp."""
    ts = pd.Timestamp(ts)             # same safety guard
    return h1.loc[ts.floor("1H")]

def build_h1(df15: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse a 15-minute frame to hourly closes, then
    attach EMA-50 and its 3-period slope.
    """
    h1 = (
        df15["c"]                      # use the close column
        .resample("1H")
        .last()
        .to_frame("close")
        .ffill()
    )
    h1["ema50"] = h1.close.ewm(span=50).mean()
    h1["slope"] = h1.ema50.diff(3)
    return h1