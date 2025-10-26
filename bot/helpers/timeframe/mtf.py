# bot/helpers/mtf_helpers.py
"""
Multi-Timeframe (MTF) Analysis Helpers

Fetches higher timeframe data and determines trend direction.
Used to filter lower timeframe signals.
"""
import pandas as pd
import numpy as np
from typing import Literal, Optional
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# HTF DATA ALIGNMENT
# ═══════════════════════════════════════════════════════════════════════════

def resample_to_htf(df_ltf: pd.DataFrame, htf_minutes: int) -> pd.DataFrame:
    """
    Resample lower timeframe data to higher timeframe.
    
    Args:
        df_ltf: Lower timeframe DataFrame (15m)
        htf_minutes: Higher timeframe in minutes (60 for 1H, 240 for 4H)
        
    Returns:
        Resampled higher timeframe DataFrame
    """
    if df_ltf.empty:
        return pd.DataFrame()
    
    # Ensure index is datetime
    if not isinstance(df_ltf.index, pd.DatetimeIndex):
        df_ltf.index = pd.to_datetime(df_ltf.index)
    
    # Resample OHLCV
    htf = pd.DataFrame()
    htf['o'] = df_ltf['o'].resample(f'{htf_minutes}T').first()
    htf['h'] = df_ltf['h'].resample(f'{htf_minutes}T').max()
    htf['l'] = df_ltf['l'].resample(f'{htf_minutes}T').min()
    htf['c'] = df_ltf['c'].resample(f'{htf_minutes}T').last()
    htf['v'] = df_ltf['v'].resample(f'{htf_minutes}T').sum()
    
    # Drop NaN rows
    htf = htf.dropna()
    
    logger.info(f"Resampled {len(df_ltf)} LTF bars to {len(htf)} HTF bars ({htf_minutes}m)")
    return htf


def compute_htf_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute indicators for HTF data.
    Focus on trend indicators only.
    """
    # EMAs for trend
    df["ema21"] = df.c.ewm(span=21, adjust=False).mean()
    df["ema50"] = df.c.ewm(span=50, adjust=False).mean()
    df["ema200"] = df.c.ewm(span=200, adjust=False).mean()  # KEY TREND FILTER
    
    # ATR for context
    tr = np.maximum.reduce([
        df.h - df.l,
        (df.h - df.c.shift()).abs(),
        (df.l - df.c.shift()).abs(),
    ])
    df["atr"] = pd.Series(tr, index=df.index).rolling(14).mean()
    
    return df


# ═══════════════════════════════════════════════════════════════════════════
# TREND DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def get_htf_trend(htf_bar) -> Literal["bull", "bear", "neutral"]:
    """
    Determine HTF trend direction.
    
    Args:
        htf_bar: Current HTF bar (must have ema200)
        
    Returns:
        "bull": Strong uptrend (trade longs only)
        "bear": Strong downtrend (trade shorts only)
        "neutral": Choppy/sideways (no trades)
    """
    if not hasattr(htf_bar, 'ema200') or pd.isna(htf_bar.ema200):
        return "neutral"
    
    # Distance from 200 EMA
    distance_pct = (htf_bar.c - htf_bar.ema200) / htf_bar.ema200
    
    # Thresholds for clear trend
    BULL_THRESHOLD = 0.02   # 2% above 200 EMA = clear bull
    BEAR_THRESHOLD = -0.02  # 2% below 200 EMA = clear bear
    
    if distance_pct > BULL_THRESHOLD:
        return "bull"
    elif distance_pct < BEAR_THRESHOLD:
        return "bear"
    else:
        return "neutral"  # Too close to 200 EMA = chop


def get_htf_bar_for_time(htf_df: pd.DataFrame, ltf_time: pd.Timestamp):
    """
    Get the corresponding HTF bar for a given LTF timestamp.
    
    Args:
        htf_df: Higher timeframe DataFrame
        ltf_time: Lower timeframe timestamp
        
    Returns:
        HTF bar (Series) or None if not found
    """
    # Find the HTF bar that contains this LTF time
    # HTF bar at 10:00 contains LTF bars from 10:00-10:59
    
    # Get all HTF bars up to and including this time
    mask = htf_df.index <= ltf_time
    if not mask.any():
        return None
    
    # Return the most recent HTF bar
    return htf_df[mask].iloc[-1]


# ═══════════════════════════════════════════════════════════════════════════
# MTF ALIGNMENT HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def mtf_trend_aligned(ltf_bar, htf_df: pd.DataFrame, direction: str) -> bool:
    """
    Check if LTF signal aligns with HTF trend.
    
    Args:
        ltf_bar: Current LTF bar
        htf_df: Higher timeframe DataFrame with indicators
        direction: "long" or "short"
        
    Returns:
        True if signal aligns with HTF trend
    """
    # Get corresponding HTF bar
    htf_bar = get_htf_bar_for_time(htf_df, ltf_bar.name)
    if htf_bar is None:
        logger.debug(f"[MTF] No HTF bar found for {ltf_bar.name}")
        return False
    
    # Get HTF trend
    htf_trend = get_htf_trend(htf_bar)
    
    # Check alignment
    if direction == "long":
        aligned = htf_trend == "bull"
        if not aligned:
            logger.debug(f"[MTF] LONG rejected: HTF trend is {htf_trend} (need bull)")
        else:
            logger.info(f"[MTF] ✅ LONG aligned: HTF trend is BULL "
                       f"(price {htf_bar.c:.2f} > 200ema {htf_bar.ema200:.2f})")
        return aligned
    
    elif direction == "short":
        aligned = htf_trend == "bear"
        if not aligned:
            logger.debug(f"[MTF] SHORT rejected: HTF trend is {htf_trend} (need bear)")
        else:
            logger.info(f"[MTF] ✅ SHORT aligned: HTF trend is BEAR "
                       f"(price {htf_bar.c:.2f} < 200ema {htf_bar.ema200:.2f})")
        return aligned
    
    return False