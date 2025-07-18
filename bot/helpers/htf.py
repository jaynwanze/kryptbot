import pandas as pd
import helpers.config as config
HTF_WINDOW = f"{config.HTF_DAYS}D"          # e.g. '15D'



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

def build_htf_levels(df15: pd.DataFrame) -> pd.DataFrame:
    """Return a DF with columns: daily_H, daily_L, h4_H, h4_L, asia_H/L, eu_H/L, ny_H/L."""
    out = pd.DataFrame(index=df15.index)

    #  Daily Hi / Lo
    daily = df15["h"].resample("1D").max().to_frame("D_H")
    daily["D_L"] = df15["l"].resample("1D").min()
    out = out.join(daily.ffill())

    #  4-hour Hi / Lo (rolling window, not calendar)
    h4 = (
        df15["h"].rolling("4h").max().to_frame("H4_H")
        .join(df15["l"].rolling("4h").min().to_frame("H4_L"))
    )
    out = out.join(h4)

    #  Session highs / lows
    for name, (h0, h1) in config.SESSION_WINDOWS.items():
        hours = df15.index.hour
        mask = (hours >= h0) & (hours <= h1)
        lo = df15["l"].where(mask).rolling(f"{h1-h0}h").min().rename(f"{name}_L")
        hi = df15["h"].where(mask).rolling(f"{h1-h0}h").max().rename(f"{name}_H")
        out = out.join(hi).join(lo)

    return out.ffill()


def update_htf_levels(df15: pd.DataFrame) -> pd.DataFrame:
    # use only the last X days to compute D / H4 / session extrema
    window = pd.Timedelta(config.HTF_DAYS, unit="D")
    recent = df15.loc[df15.index >= df15.index[-1] - window]  
    return build_htf_levels(recent)