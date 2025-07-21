import numpy as np
import pandas as pd
from helpers import config

# --- Break of structure -----------------
def is_bos(df: pd.DataFrame, idx: int, direction: str,
           left: int = 2, right: int = 2) -> bool:
    if idx < left + right + 1:
        return False
    win = df["h" if direction == "long" else "l"]
    if direction == "long":
        pivot_idx = idx - right - 1                # last fully‑formed bar
        window    = win.iloc[pivot_idx-left : pivot_idx+right+1]
        if win.iloc[pivot_idx] != window.max():    # not a swing‑high
            return False
        return df.c.iloc[idx] > win.iloc[pivot_idx]
    else:
        pivot_idx = idx - right - 1
        window    = win.iloc[pivot_idx-left : pivot_idx+right+1]
        if win.iloc[pivot_idx] != window.min():
            return False
        return df.c.iloc[idx] < win.iloc[pivot_idx]

# --- Fair-value gap detection ------------
def has_fvg(df: pd.DataFrame, idx: int, direction: str, min_gap_frac=0.0003) -> bool:
    """
    3‑candle Fair‑Value‑Gap:
        bullish → candle‑0 high < candle‑2 low
        bearish → candle‑0 low  > candle‑2 high
    *idx* is the **middle** candle.
    """
    if idx < 2:
        return False                     # need three candles

    c0, c1, c2 = df.iloc[idx-2], df.iloc[idx-1], df.iloc[idx]   # all historical

    MIN_GAP_ATR = 0.3          # 30 % of current ATR

    gap_px = (c0.h - c2.l) if direction == "long" else (c2.h - c0.l)
    return gap_px >= MIN_GAP_ATR * c1.atr


# ── 79 % fib touch of the *same candle* that raided liquidity ──
def fib_tag(raid_px: float, bar, direction: str) -> bool:
    """Require the same candle that swept liquidity to tag ≥ 79 % level."""
    full = bar.h - bar.l
    if full <= 0:                                 # avoid zero division
        return False
    if direction == "long":
        progress = (bar.h - raid_px) / full          # full ↑ retrace
    else:
        progress = (raid_px - bar.l) / full
    return progress >= config.FIB_EXT