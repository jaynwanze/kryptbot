import numpy as np
import pandas as pd
from bot.helpers import config

# --- Break of structure -----------------
def is_bos(df: pd.DataFrame, idx: int, direction: str,
           left: int = 3, right: int = 3) -> bool:
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
def has_fvg(df: pd.DataFrame, idx: int, direction: str, min_gap_frac=0.3) -> bool:
    """
    3‑candle Fair‑Value‑Gap:
        bullish → candle‑0 high < candle‑2 low
        bearish → candle‑0 low  > candle‑2 high
    *idx* is the **middle** candle.
    """
    if idx < 2:
        return False                     # need three candles

    c0, c1, c2 = df.iloc[idx-2], df.iloc[idx-1], df.iloc[idx]   # all historical

    MIN_GAP_ATR = min_gap_frac

    gap_px = (c0.h - c2.l) if direction == "long" else (c2.h - c0.l)
    return gap_px >= MIN_GAP_ATR * c1.atr


# # ── 79 % fib touch of the *same candle* that raided liquidity ──
def fib_tag(bar, direction: str, frac=None, use_close=True) -> bool:
    frac = frac or config.FIB_EXT  # e.g. 0.79–0.90
    rng = bar.h - bar.l
    if rng <= 0:
        return False
    ref = bar.c if use_close else (bar.h if direction == "long" else bar.l)
    if direction == "long":
        level = bar.l + frac * rng
        return ref >= level
    else:
        level = bar.h - frac * rng
        return ref <= level