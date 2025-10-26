# bot/helpers/signals.py
"""
PURE EMA TREND FOLLOWING SYSTEM with 200 EMA FILTER

Core Logic:
- Fast EMA (21) crosses above Slow EMA (50) → LONG trend
- Fast EMA (21) crosses below Slow EMA (50) → SHORT trend
- ADX confirms trend strength
- 200 EMA FILTER: Only long above 200 EMA, only short below 200 EMA

This filters out choppy markets and only trades genuine trends.
"""
from typing import Optional, Tuple
from bot.helpers import ema_config
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# 200 EMA TREND FILTER
# ═══════════════════════════════════════════════════════════════════════════

def above_200ema(bar) -> bool:
    """Check if price is in bull market (above 200 EMA)."""
    if not hasattr(bar, 'ema200') or pd.isna(bar.ema200):
        return False
    return bar.c > bar.ema200


def below_200ema(bar) -> bool:
    """Check if price is in bear market (below 200 EMA)."""
    if not hasattr(bar, 'ema200') or pd.isna(bar.ema200):
        return False
    return bar.c < bar.ema200


# ═══════════════════════════════════════════════════════════════════════════
# EMA TREND DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def ema_crossover(df, i, fast_period=21, slow_period=50) -> Tuple[bool, str]:
    """Detect EMA crossovers."""
    if i < 2:
        return False, ""
    
    bar = df.iloc[i]
    prev_bar = df.iloc[i-1]
    
    fast_col = f"ema{fast_period}"
    slow_col = f"ema{slow_period}"
    
    if fast_col not in df.columns or slow_col not in df.columns:
        logger.warning(f"Missing EMA columns: {fast_col} or {slow_col}")
        return False, ""
    
    fast_now = bar[fast_col]
    slow_now = bar[slow_col]
    fast_prev = prev_bar[fast_col]
    slow_prev = prev_bar[slow_col]
    
    # Bullish crossover
    if fast_prev <= slow_prev and fast_now > slow_now:
        logger.info(f"[EMA] LONG crossover at {bar.name}: fast={fast_now:.4f} > slow={slow_now:.4f}")
        return True, "long"
    
    # Bearish crossover
    if fast_prev >= slow_prev and fast_now < slow_now:
        logger.info(f"[EMA] SHORT crossover at {bar.name}: fast={fast_now:.4f} < slow={slow_now:.4f}")
        return True, "short"
    
    return False, ""


def trend_aligned(df, i, direction: str, fast_period=21, slow_period=50) -> bool:
    """Check if EMAs confirm trend direction."""
    bar = df.iloc[i]
    
    fast_col = f"ema{fast_period}"
    slow_col = f"ema{slow_period}"
    
    if fast_col not in df.columns or slow_col not in df.columns:
        return False
    
    fast = bar[fast_col]
    slow = bar[slow_col]
    
    if direction == "long":
        return fast > slow
    else:
        return fast < slow


def momentum_confirms(bar, direction: str) -> bool:
    """Check ADX for trend strength."""
    adx_floor = ema_config.ADX_HARD_FLOOR
    
    if bar.adx < adx_floor:
        logger.debug(f"[MOMENTUM] Rejected: ADX {bar.adx:.1f} < {adx_floor}")
        return False
    
    return True


def volatility_filter(bar) -> bool:
    """Ensure sufficient volatility."""
    min_atr_ratio = ema_config.MIN_ATR_RATIO
    atr_ratio = bar.atr / bar.atr30 if bar.atr30 > 0 else 0
    
    if atr_ratio < min_atr_ratio:
        logger.debug(f"[VOLATILITY] Rejected: ATR ratio {atr_ratio:.2f} < {min_atr_ratio}")
        return False
    
    return True


# ═══════════════════════════════════════════════════════════════════════════
# MAIN SIGNAL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def ema_long_signal(df, i, htf_row=None, min_checks=1) -> bool:
    """
    LONG TREND SIGNAL with 200 EMA FILTER
    
    1. Price ABOVE 200 EMA (bull market)
    2. Fast EMA > Slow EMA (in uptrend)
    3. Recent crossover OR strong alignment
    4. ADX confirms trend strength
    5. Sufficient volatility
    """
    if i < 210:  # Need 200 EMA warmup
        return False
    
    bar = df.iloc[i]
    
    # ═══════════════════════════════════════════════════════════════════════
    # CRITICAL: 200 EMA FILTER FOR LONGS
    # ═══════════════════════════════════════════════════════════════════════
    if not above_200ema(bar):
        logger.debug(f"[200EMA] LONG rejected: price {bar.c:.4f} below 200ema {bar.ema200:.4f}")
        return False
    
    # Check for trend alignment
    if not trend_aligned(df, i, "long"):
        return False
    
    # Look for recent crossover
    crossover_lookback = ema_config.EMA_CROSSOVER_LOOKBACK
    found_cross = False
    
    for j in range(max(2, i - crossover_lookback), i + 1):
        crossed, direction = ema_crossover(df, j)
        if crossed and direction == "long":
            found_cross = True
            bars_since = i - j
            logger.debug(f"[SIGNAL] Found long crossover {bars_since} bars ago")
            break
    
    if not found_cross:
        # Alternative: strong trend alignment (fast >> slow)
        fast = bar.ema21
        slow = bar.ema50
        separation = (fast - slow) / slow if slow > 0 else 0
        min_separation = ema_config.MIN_EMA_SEPARATION
        
        if separation < min_separation:
            return False
        
        logger.debug(f"[SIGNAL] Strong uptrend: EMA separation {separation:.2%}")
    
    # Momentum confirmation
    if not momentum_confirms(bar, "long"):
        return False
    
    # Volatility filter
    if not volatility_filter(bar):
        return False
    
    logger.info(f"[SIGNAL] ✅ LONG TREND confirmed at {bar.name}: "
               f"c={bar.c:.4f} > 200ema={bar.ema200:.4f} | "
               f"ema21={bar.ema21:.4f} > ema50={bar.ema50:.4f} | adx={bar.adx:.1f}")
    return True


def ema_short_signal(df, i, htf_row=None, min_checks=1) -> bool:
    """
    SHORT TREND SIGNAL with 200 EMA FILTER
    
    1. Price BELOW 200 EMA (bear market)
    2. Fast EMA < Slow EMA (in downtrend)
    3. Recent crossover OR strong alignment
    4. ADX confirms trend strength
    5. Sufficient volatility
    """
    if i < 210:  # Need 200 EMA warmup
        return False
    
    bar = df.iloc[i]
    
    # ═══════════════════════════════════════════════════════════════════════
    # CRITICAL: 200 EMA FILTER FOR SHORTS
    # ═══════════════════════════════════════════════════════════════════════
    if not below_200ema(bar):
        logger.debug(f"[200EMA] SHORT rejected: price {bar.c:.4f} above 200ema {bar.ema200:.4f}")
        return False
    
    # Check for trend alignment
    if not trend_aligned(df, i, "short"):
        return False
    
    # Look for recent crossover
    crossover_lookback = ema_config.EMA_CROSSOVER_LOOKBACK
    found_cross = False
    
    for j in range(max(2, i - crossover_lookback), i + 1):
        crossed, direction = ema_crossover(df, j)
        if crossed and direction == "short":
            found_cross = True
            bars_since = i - j
            logger.debug(f"[SIGNAL] Found short crossover {bars_since} bars ago")
            break
    
    if not found_cross:
        # Alternative: strong trend alignment
        fast = bar.ema21
        slow = bar.ema50
        separation = (slow - fast) / slow if slow > 0 else 0
        min_separation = ema_config.MIN_EMA_SEPARATION
        
        if separation < min_separation:
            return False
        
        logger.debug(f"[SIGNAL] Strong downtrend: EMA separation {separation:.2%}")
    
    # Momentum confirmation
    if not momentum_confirms(bar, "short"):
        return False
    
    # Volatility filter
    if not volatility_filter(bar):
        return False
    
    logger.info(f"[SIGNAL] ✅ SHORT TREND confirmed at {bar.name}: "
               f"c={bar.c:.4f} < 200ema={bar.ema200:.4f} | "
               f"ema21={bar.ema21:.4f} < ema50={bar.ema50:.4f} | adx={bar.adx:.1f}")
    return True