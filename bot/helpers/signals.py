from typing import Tuple
from helpers.config import STO_K_MIN_LONG, STO_K_MIN_SHORT, ADX_FLOOR

def long_signal(bar, prev, h1r):
    cross_up = bar.ema7 > bar.ema14 and prev.ema7 <= prev.ema14
    return (cross_up and bar.k_fast > STO_K_MIN_LONG
            and bar.rsi  > 45 and bar.adx >= ADX_FLOOR
            and h1r.close > h1r.ema50 and h1r.slope > 0)

def short_signal(bar, prev, h1r):
    cross_dn = bar.ema7 < bar.ema14 and prev.ema7 >= prev.ema14
    return (cross_dn and bar.k_fast > STO_K_MIN_SHORT
            and bar.rsi  > 30 and bar.adx >= ADX_FLOOR
            and h1r.close < h1r.ema50 and h1r.slope < 0)
from helpers import htf, ltf, config

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
    checks += ltf.has_fvg(prev, bar)
    checks += ltf.fib_tag(bar.l, bar, "long")
    return checks >= 1  # need at least two confirmations

def tjr_short_signal(df, i, htf_row) -> bool:
    bar, prev = df.iloc[i], df.iloc[i-1]
    raided, side = raid_happened(bar, htf_row)
    if not (raided and side == "short"):
        return False
    checks = 0
    checks += ltf.is_bos(df, i, "short")
    checks += ltf.has_fvg(prev, bar)
    checks += ltf.fib_tag(bar.h, bar, "short")
    return checks >= 1
