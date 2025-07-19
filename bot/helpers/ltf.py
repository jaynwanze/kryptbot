import numpy as np
import pandas as pd
from helpers import config

# --- Break of structure -----------------
def is_bos(df: pd.DataFrame, idx: int, direction: str) -> bool:
    """
    direction: 'long' means price should put in a higher-high after sweeping a low.
    """
    if direction == "long":
        pivot_low = df.l.iloc[idx]
        look = df.h.iloc[idx:idx+config.BOS_LOOKBACK]
        return look.max() > df.h.iloc[idx-1]
    else:  # short
        pivot_high = df.h.iloc[idx]
        look = df.l.iloc[idx:idx+config.BOS_LOOKBACK]
        return look.min() < df.l.iloc[idx-1]

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

    c0, c1, c2 = df.iloc[idx-1], df.iloc[idx], df.iloc[idx+1]

    if direction == "long":
        gap_px = c0.h - c2.l
    else:                                # short
        gap_px = c2.h - c0.l

    return gap_px > min_gap_frac * c1.c

# ── 79 % fib touch of the *same candle* that raided liquidity ──
def fib_tag(raid_px: float, bar, direction: str) -> bool:
    """Require the same candle that swept liquidity to tag ≥ 79 % level."""
    full = bar.h - bar.l
    if full <= 0:                                 # avoid zero division
        return False
    if direction == "long":
        progress = (bar.h - raid_px) / full       # 0 ➜ 1
    else:
        progress = (raid_px - bar.l) / full
    return progress >= config.FIB_EXT             # default 0.79  (= 79 %)