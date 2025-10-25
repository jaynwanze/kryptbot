# bot/helpers/signals_breakout.py
"""
BREAKOUT STRATEGY (replaces mean-reversion)

Instead of fading HTF levels, we trade BREAKS of them:
- LONG: Price breaks ABOVE resistance, pullback, then bounce
- SHORT: Price breaks BELOW support, pullback, then reject

This works better in trending crypto markets.
"""
from typing import Optional, Tuple
from bot.helpers import breakout_config
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# BREAKOUT DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def level_broken(bar, prev_bar, htf_row, direction: str, break_threshold_atr: float = 0.3) -> Tuple[bool, float]:
    """
    Check if price has BROKEN through an HTF level.
    
    Args:
        bar: Current bar
        prev_bar: Previous bar  
        htf_row: HTF levels row
        direction: "long" or "short"
        break_threshold_atr: How far beyond level = confirmed break
        
    Returns:
        (broken: bool, level_price: float)
    """
    threshold = break_threshold_atr * bar.atr
    
    if direction == "long":
        # Check if price broke ABOVE resistance
        resistances = [
            htf_row.get('D_H'), htf_row.get('H4_H'),
            htf_row.get('asia_H'), htf_row.get('eu_H'), htf_row.get('ny_H')
        ]
        resistances = [r for r in resistances if pd.notna(r)]
        
        for level in resistances:
            level = float(level)
            # Previous bar was below, current closed above
            if prev_bar.c <= level and bar.c > (level + threshold):
                logger.info(f"[BREAKOUT] LONG: Broke above {level:.4f} (close={bar.c:.4f})")
                return True, level
                
    else:  # short
        # Check if price broke BELOW support
        supports = [
            htf_row.get('D_L'), htf_row.get('H4_L'),
            htf_row.get('asia_L'), htf_row.get('eu_L'), htf_row.get('ny_L')
        ]
        supports = [s for s in supports if pd.notna(s)]
        
        for level in supports:
            level = float(level)
            # Previous bar was above, current closed below
            if prev_bar.c >= level and bar.c < (level - threshold):
                logger.info(f"[BREAKOUT] SHORT: Broke below {level:.4f} (close={bar.c:.4f})")
                return True, level
    
    return False, 0.0


def pullback_to_level(df, i, broken_level: float, direction: str, 
                      lookback: int = 8, proximity_atr: float = 0.4) -> bool:
    """
    After a breakout, check if price has pulled back to the broken level.
    
    Old resistance becomes new support (for longs).
    Old support becomes new resistance (for shorts).
    
    Args:
        df: Dataframe
        i: Current index
        broken_level: The level that was broken
        direction: "long" or "short"
        lookback: How many bars to look back for pullback
        proximity_atr: How close to level = valid pullback
        
    Returns:
        True if valid pullback found
    """
    if i < lookback:
        return False
    
    recent_bars = df.iloc[max(0, i-lookback):i+1]
    
    for bar in recent_bars.itertuples():
        threshold = proximity_atr * bar.atr
        
        if direction == "long":
            # After breaking above, did price pull back to (but hold above) the level?
            # We want the LOW to come within threshold of the broken resistance
            if abs(bar.l - broken_level) <= threshold and bar.c > broken_level:
                logger.info(f"[PULLBACK] LONG: Found pullback to {broken_level:.4f} at {bar.Index}")
                return True
                
        else:  # short
            # After breaking below, did price pull back to (but hold below) the level?
            # We want the HIGH to come within threshold of the broken support
            if abs(bar.h - broken_level) <= threshold and bar.c < broken_level:
                logger.info(f"[PULLBACK] SHORT: Found pullback to {broken_level:.4f} at {bar.Index}")
                return True
    
    return False


def momentum_confirmation(bar, direction: str) -> bool:
    """
    Confirm we have momentum in the breakout direction.
    
    For breakouts, we want:
    - Strong ADX (trending)
    - Momentum aligned (for longs: don't enter if already overbought)
    """
    adx_floor = getattr(breakout_config, "ADX_HARD_FLOOR", 25)
    
    if bar.adx < adx_floor:
        return False
    
    # Don't chase extremes even in breakouts
    k_long_max = getattr(breakout_config, "MOMENTUM_STO_K_LONG_MAX", 70)  # Don't buy if > 70
    k_short_min = getattr(breakout_config, "MOMENTUM_STO_K_SHORT_MIN", 30)  # Don't sell if < 30
    
    if direction == "long":
        # For longs: Don't enter if already overbought
        if bar.k_slow > k_long_max:
            logger.info(f"[MOMENTUM] LONG rejected: k_slow={bar.k_slow:.1f} > {k_long_max}")
            return False
    else:
        # For shorts: Don't enter if already oversold
        if bar.k_slow < k_short_min:
            logger.info(f"[MOMENTUM] SHORT rejected: k_slow={bar.k_slow:.1f} < {k_short_min}")
            return False
    
    return True


# ═══════════════════════════════════════════════════════════════════════════
# MAIN SIGNAL FUNCTIONS (BREAKOUT STRATEGY)
# ═══════════════════════════════════════════════════════════════════════════

def breakout_long_signal(df, i, htf_row) -> Tuple[bool, Optional[float]]:
    """
    LONG BREAKOUT SIGNAL:
    1. Price broke ABOVE an HTF resistance
    2. Pulled back toward that level (now support)
    3. Bouncing off it with momentum
    
    Returns:
        (signal: bool, broken_level: float or None)
    """
    if i < 10:  # Need history for pullback detection
        return False, None
    
    bar = df.iloc[i]
    prev_bar = df.iloc[i-1]
    
    # Check recent bars (last 20) for any breaks
    lookback_for_break = 20
    for j in range(max(1, i-lookback_for_break), i+1):
        check_bar = df.iloc[j]
        check_prev = df.iloc[j-1]
        
        broken, level = level_broken(check_bar, check_prev, htf_row, "long", 
                                     break_threshold_atr=0.3)
        
        if not broken:
            continue
        
        # Found a break - now check if we've pulled back
        pulled_back = pullback_to_level(df, i, level, "long", 
                                        lookback=min(8, i-j),
                                        proximity_atr=0.4)
        
        if not pulled_back:
            continue
        
        # Check momentum
        if not momentum_confirmation(bar, "long"):
            continue
        
        # Additional filter: Current bar should be bouncing (close > open)
        if bar.c <= bar.o:
            continue
        
        logger.info(f"[SIGNAL] LONG BREAKOUT confirmed at {bar.name}: "
                   f"broke {level:.4f}, pulled back, now bouncing")
        return True, level
    
    return False, None


def breakout_short_signal(df, i, htf_row) -> Tuple[bool, Optional[float]]:
    """
    SHORT BREAKOUT SIGNAL:
    1. Price broke BELOW an HTF support
    2. Pulled back toward that level (now resistance)
    3. Rejecting from it with momentum
    
    Returns:
        (signal: bool, broken_level: float or None)
    """
    if i < 10:
        return False, None
    
    bar = df.iloc[i]
    prev_bar = df.iloc[i-1]
    
    # Check recent bars for breaks
    lookback_for_break = 20
    for j in range(max(1, i-lookback_for_break), i+1):
        check_bar = df.iloc[j]
        check_prev = df.iloc[j-1]
        
        broken, level = level_broken(check_bar, check_prev, htf_row, "short",
                                     break_threshold_atr=0.3)
        
        if not broken:
            continue
        
        # Found a break - check pullback
        pulled_back = pullback_to_level(df, i, level, "short",
                                        lookback=min(8, i-j),
                                        proximity_atr=0.4)
        
        if not pulled_back:
            continue
        
        # Check momentum
        if not momentum_confirmation(bar, "short"):
            continue
        
        # Current bar should be rejecting (close < open)
        if bar.c >= bar.o:
            continue
        
        logger.info(f"[SIGNAL] SHORT BREAKOUT confirmed at {bar.name}: "
                   f"broke {level:.4f}, pulled back, now rejecting")
        return True, level
    
    return False, None

