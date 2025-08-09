import pandas as pd
import bot.helpers.config as config
HTF_WINDOW = f"{config.HTF_DAYS}D"          # e.g. '15D'



def update_h1(h1: pd.DataFrame, ts, close: float) -> pd.DataFrame:
    ts = pd.Timestamp(ts)
    stamp = ts.floor("1h")
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
    return h1.loc[ts.floor("1h")]

def build_h1(df15: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse a 15-minute frame to hourly closes, then
    attach EMA-50 and its 3-period slope.
    """
    h1 = (
        df15["c"]                      # use the close column
        .resample("1h")
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


# ────────────────────────── incremental HTF update ─────────────────────────
def update_htf_levels_new(htf: pd.DataFrame, bar15: pd.Series) -> pd.DataFrame:
    ts = bar15.name
    if ts not in htf.index:
        htf.loc[ts] = htf.iloc[-1]

    # ------------------------------------------------------- daily
    prev_day = htf.index[-2].date()
    this_day = ts.date()
    if this_day != prev_day:
        # new UTC day  → start fresh
        htf.at[ts, "D_H"] = bar15.h
        htf.at[ts, "D_L"] = bar15.l
    else:
        htf.at[ts, "D_H"] = max(htf.iat[-2, htf.columns.get_loc("D_H")], bar15.h)
        htf.at[ts, "D_L"] = min(htf.iat[-2, htf.columns.get_loc("D_L")], bar15.l)

    # ------------------------------------------------------- rolling 4 h
    cutoff_4h = ts - pd.Timedelta(hours=4)
    recent4   = htf.loc[cutoff_4h:]
    htf.at[ts, "H4_H"] = max(recent4["H4_H"].iat[-1], bar15.h)
    htf.at[ts, "H4_L"] = min(recent4["H4_L"].iat[-1], bar15.l)

    # ------------------------------------------------------- sessions
    for name, (h0, h1) in config.SESSION_WINDOWS.items():
        col_H, col_L = f"{name}_H", f"{name}_L"

        # ‘in session’ test with midnight wrap
        hr = ts.hour
        in_sess = (h0 <= hr <= h1) if h0 <= h1 else (hr >= h0 or hr <= h1)

        # find the *last* row that belongs to the same session window
        # (could be previous day for Asia for example)
        same_sess_mask = _session_mask(htf.index, h0, h1)
        same_sess_rows = htf.index[same_sess_mask & (htf.index.date == ts.date())]
        if not in_sess or len(same_sess_rows) == 0:   # brand‑new session row
            htf.at[ts, col_H] = bar15.h
            htf.at[ts, col_L] = bar15.l
        else:                                        # extend current session range
            prev_H = htf.at[same_sess_rows[-1], col_H]
            prev_L = htf.at[same_sess_rows[-1], col_L]
            htf.at[ts, col_H] = max(prev_H, bar15.h)
            htf.at[ts, col_L] = min(prev_L, bar15.l)

    # ---- trim history to keep RAM footprint identical
    cutoff = ts - pd.Timedelta(days=config.HTF_DAYS)
    if htf.index[0] < cutoff:
        htf.drop(htf.index[0], inplace=True)

    return htf


# ─────────────────────────────── helpers ────────────────────────────────
def _session_mask(idx: pd.DatetimeIndex, h0: int, h1: int) -> pd.Series:
    """Boolean mask marking the hours [h0 … h1] *inclusive*."""
    hr = idx.hour
    if h0 <= h1:                         # normal window (e.g. 8‑17)
        return (hr >= h0) & (hr <= h1)
    return (hr >= h0) | (hr <= h1)       # wraps midnight (e.g. 22‑2)