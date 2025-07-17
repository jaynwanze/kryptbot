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
def has_fvg(bar_prev, bar_curr) -> bool:
    # bullish gap: prev.l > curr.h  (on 1-min this is common)
    gap = (bar_prev.l - bar_curr.h) / bar_prev.c
    return gap > config.FVG_MIN_PX

# --- 79 % fib touch ----------------------
# helpers/ltf.py – no look‑ahead in fib_tag
def fib_tag(raid_px: float, bar, direction: str) -> bool:
    """Require the same candle that swept liquidity to tag ≥ 79 % level."""
    if direction == "long":
        return (bar.h - raid_px) / raid_px >= config.FIB_EXT
    else:
        return (raid_px - bar.l) / raid_px >= config.FIB_EXT
