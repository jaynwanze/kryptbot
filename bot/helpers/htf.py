import pandas as pd
import bot.helpers.config.config as config
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
    
    # Store OHLC for incremental calculations
    out['o'] = df15['o']
    out['h'] = df15['h']
    out['l'] = df15['l']
    out['c'] = df15['c']

    # Daily Hi / Lo - use expanding window within each day
    out['day'] = out.index.date
    out['D_H'] = out.groupby('day')['h'].expanding().max().reset_index(level=0, drop=True)
    out['D_L'] = out.groupby('day')['l'].expanding().min().reset_index(level=0, drop=True)
    out = out.drop('day', axis=1)

    # 4-hour Hi / Lo (rolling window)
    h4 = (
        df15["h"].rolling("4h").max().to_frame("H4_H")
        .join(df15["l"].rolling("4h").min().to_frame("H4_L"))
    )
    out = out.join(h4)

    # Session highs / lows - match incremental behavior (day-bounded)
    for name, (h0, h1) in config.SESSION_WINDOWS.items():
        col_H, col_L = f"{name}_H", f"{name}_L"
        out[col_H] = pd.NA
        out[col_L] = pd.NA
        
        # Process day by day to match incremental
        for day in out.index.normalize().unique():
            day_mask = out.index.normalize() == day
            day_indices = out.index[day_mask]
            
            for ts in day_indices:
                hr = ts.hour
                in_sess = (h0 <= hr <= h1) if h0 <= h1 else (hr >= h0 or hr <= h1)
                
                if not in_sess:
                    # Propagate previous value
                    prev_idx = out.index.get_loc(ts) - 1
                    if prev_idx >= 0:
                        out.at[ts, col_H] = out.iloc[prev_idx][col_H]
                        out.at[ts, col_L] = out.iloc[prev_idx][col_L]
                    continue
                
                # Get session bars from today only
                sess_data = out.loc[day:ts]
                sess_mask = _session_mask(sess_data.index, h0, h1)
                sess_bars = sess_data[sess_mask]
                
                if len(sess_bars) > 0 and 'h' in sess_bars.columns:
                    out.at[ts, col_H] = sess_bars['h'].max()
                    out.at[ts, col_L] = sess_bars['l'].min()
                else:
                    out.at[ts, col_H] = out.loc[ts, 'h']
                    out.at[ts, col_L] = out.loc[ts, 'l']

    return out


# ────────────────────────── incremental HTF update ─────────────────────────
def update_htf_levels_new(htf: pd.DataFrame, bar15: pd.Series) -> pd.DataFrame:
    ts = bar15.name
    if ts not in htf.index:
        htf.loc[ts] = htf.iloc[-1]
    
    # Store new bar's OHLC
    htf.at[ts, 'o'] = bar15.o
    htf.at[ts, 'h'] = bar15.h
    htf.at[ts, 'l'] = bar15.l
    htf.at[ts, 'c'] = bar15.c

    # Daily: just accumulate today's bars
    current_day_start = ts.normalize()
    day_bars = htf.loc[current_day_start:ts]
    
    if len(day_bars) > 0 and 'h' in htf.columns:
        htf.at[ts, "D_H"] = day_bars['h'].max()
        htf.at[ts, "D_L"] = day_bars['l'].min()
    else:
        htf.at[ts, "D_H"] = bar15.h
        htf.at[ts, "D_L"] = bar15.l

    # 4H: rolling window
    cutoff_4h = ts - pd.Timedelta(hours=4)
    recent4_data = htf.loc[cutoff_4h:ts]
    
    if len(recent4_data) > 0 and 'h' in htf.columns:
        htf.at[ts, "H4_H"] = recent4_data['h'].max()
        htf.at[ts, "H4_L"] = recent4_data['l'].min()
    else:
        htf.at[ts, "H4_H"] = bar15.h
        htf.at[ts, "H4_L"] = bar15.l

    # Sessions: rolling window matching batch - BUT only within current day
    for name, (h0, h1) in config.SESSION_WINDOWS.items():
        col_H, col_L = f"{name}_H", f"{name}_L"
        sess_hours = (h1 - h0) if h0 <= h1 else (24 - h0 + h1)
        hr = ts.hour
        in_sess = (h0 <= hr <= h1) if h0 <= h1 else (hr >= h0 or hr <= h1)
        
        if not in_sess:
            if len(htf) > 1:
                htf.at[ts, col_H] = htf.iloc[-2][col_H]
                htf.at[ts, col_L] = htf.iloc[-2][col_L]
            continue
        
        # CRITICAL FIX: Only look at session bars from TODAY
        current_day_start = ts.normalize()
        cutoff_sess = max(current_day_start, ts - pd.Timedelta(hours=sess_hours))
        sess_data = htf.loc[cutoff_sess:ts]
        
        if len(sess_data) > 0 and 'h' in htf.columns:
            sess_mask = _session_mask(sess_data.index, h0, h1)
            sess_bars = sess_data[sess_mask]
            
            if len(sess_bars) > 0:
                htf.at[ts, col_H] = sess_bars['h'].max()
                htf.at[ts, col_L] = sess_bars['l'].min()
            else:
                htf.at[ts, col_H] = bar15.h
                htf.at[ts, col_L] = bar15.l
        else:
            htf.at[ts, col_H] = bar15.h
            htf.at[ts, col_L] = bar15.l

    # Trim history
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