from typing import Optional, Tuple
from bot.helpers import config
from bot.helpers import ltf
import pandas as pd

def raid_happened(bar, htf_row, tol_atr: float = 3.0) -> Tuple[bool, str]:
    tol = tol_atr * bar.atr
    liq_lows  = [htf_row[c] for c in htf_row.index if c.endswith("_L")]
    liq_highs = [htf_row[c] for c in htf_row.index if c.endswith("_H")]
    swept_low  = bar.l <= (min(liq_lows) + tol)
    swept_high = bar.h >= (max(liq_highs) - tol)
    if swept_low and not swept_high:
        return True, "long"
    if swept_high and not swept_low:
        return True, "short"
    return False, ""

def tjr_long_signal(df, i, htf_row, min_checks=2) -> bool:
    bar, prev = df.iloc[i], df.iloc[i-1]
    raided, side = raid_happened(bar, htf_row)  
    if not (raided and side == "long"):
        return False

    # BOS + FVG + fib tag (any two out of three is enough)
    bos = ltf.is_bos(df, i, "long", left=2, right=2)
    fvg = ltf.has_fvg(df, i-1, "long", min_gap_frac=0.30)
    fib = ltf.fib_tag(bar, "long", frac=0.50)
    
    checks = int(bos) + int(fvg) + int(fib)
    return checks >= min_checks

def tjr_short_signal(df, i, htf_row, min_checks=2) -> bool:
    bar, prev = df.iloc[i], df.iloc[i-1]
    raided, side = raid_happened(bar, htf_row)
    if not (raided and side == "short"):
        return False
    checks = 0
    checks += ltf.is_bos(df, i, "short", left=2, right=2)
    checks += ltf.has_fvg(df, i-1, "short", min_gap_frac=0.30)
    checks += ltf.fib_tag(bar, "short", frac=0.50)
    return checks >= min_checks