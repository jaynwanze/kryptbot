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
