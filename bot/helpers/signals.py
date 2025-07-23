from typing import Optional, Tuple
from bot.helpers import config
from bot.helpers import ltf
import pandas as pd

def raid_happened(bar, htf_row) -> Tuple[bool, str]:
    """
    True + 'long'  if bar just swept a *low* liquidity pool,
    True + 'short' if bar just swept a *high* liquidity pool.
    """
    # Check against any of the stored HTF lows/highs
    liq_lows  = [htf_row[c] for c in htf_row.index if c.endswith("_L")]
    liq_highs = [htf_row[c] for c in htf_row.index if c.endswith("_H")]

    swept_low  = bar.l <= min(liq_lows)
    swept_high = bar.h >= max(liq_highs)
    if swept_low and not swept_high:
        return True, "long"
    if swept_high and not swept_low:
        return True, "short"
    return False, ""

def tjr_long_signal(df, i, htf_row) -> bool:
    bar, prev = df.iloc[i], df.iloc[i-1]
    raided, side = raid_happened(bar, htf_row)
    if not (raided and side == "long"):
        return False

    #  BOS + FVG + fib tag (any two out of three is enough)
    checks = 0
    checks += ltf.is_bos(df, i, "long")
    checks += ltf.has_fvg(df, i-1, "long")
    checks += ltf.fib_tag(bar.l, bar, "long")
    return checks >= 2

def tjr_short_signal(df, i, htf_row) -> bool:
    bar, prev = df.iloc[i], df.iloc[i-1]
    raided, side = raid_happened(bar, htf_row)
    if not (raided and side == "short"):
        return False
    checks = 0
    checks += ltf.is_bos(df, i, "short")
    checks += ltf.has_fvg(df, i-1, "short")
    checks += ltf.fib_tag(bar.h, bar, "short")
    return checks >= 2