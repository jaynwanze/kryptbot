# bot/helpers/regime_filter_fast.py
"""
FAST REGIME FILTER (Cached)

Calculates regime ONCE per day instead of every bar.
Caches results to avoid expensive recalculations.

Performance:
- Old: 18,000 regime checks (10+ minutes)
- New: ~730 regime checks (10 seconds)
"""
import pandas as pd
import numpy as np
from typing import Dict
import logging

logger = logging.getLogger(__name__)

# Global cache
_REGIME_CACHE = {}

def calculate_daily_adx_fast(df: pd.DataFrame, lookback_days: int = 14) -> pd.Series:
    """
    Calculate ADX on daily timeframe ONCE for entire dataset.
    Returns Series indexed by daily timestamps.
    """
    # Resample to daily
    daily = df.resample('1D').agg({
        'o': 'first',
        'h': 'max',
        'l': 'min',
        'c': 'last',
        'v': 'sum'
    }).dropna()
    
    if len(daily) < lookback_days + 1:
        return pd.Series()
    
    # Calculate True Range
    high_low = daily['h'] - daily['l']
    high_close = np.abs(daily['h'] - daily['c'].shift())
    low_close = np.abs(daily['l'] - daily['c'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    
    # Calculate Directional Movement
    up_move = daily['h'] - daily['h'].shift()
    down_move = daily['l'].shift() - daily['l']
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    
    # Smooth with EMA
    atr = tr.ewm(span=lookback_days, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=daily.index).ewm(span=lookback_days, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=daily.index).ewm(span=lookback_days, adjust=False).mean() / atr
    
    # Calculate ADX
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.ewm(span=lookback_days, adjust=False).mean()
    
    return adx


def calculate_bb_width_rolling(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    """
    Calculate BB width for entire dataset at once.
    Returns Series of BB width values.
    """
    sma = df['c'].rolling(period).mean()
    std = df['c'].rolling(period).std()
    
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    
    width = (upper - lower) / sma
    
    return width


def precompute_regime(df: pd.DataFrame, config) -> pd.Series:
    """
    Pre-compute regime for entire dataset.
    Returns Series with True (can trade) / False (choppy).
    
    This is calculated ONCE, not per bar.
    """
    logger.info("Pre-computing regime filter for entire dataset...")
    
    # Get thresholds
    daily_adx_min = getattr(config, 'REGIME_DAILY_ADX_MIN', 25)
    bb_width_min = getattr(config, 'REGIME_BB_WIDTH_MIN', 0.08)
    require_atr_expanding = getattr(config, 'REGIME_REQUIRE_ATR_EXPANDING', True)
    
    # Calculate all filters ONCE
    try:
        # Daily ADX (expensive, do once)
        daily_adx = calculate_daily_adx_fast(df)
        
        # Map daily ADX to 1H bars
        df_with_daily_adx = df.copy()
        df_with_daily_adx['daily_adx'] = np.nan
        
        for date in daily_adx.index:
            # Get all 1H bars for this day
            mask = df_with_daily_adx.index.date == date.date()
            df_with_daily_adx.loc[mask, 'daily_adx'] = daily_adx.loc[date]
        
        # Forward fill for bars before first daily value
        df_with_daily_adx['daily_adx'] = df_with_daily_adx['daily_adx'].fillna(method='ffill')
        
        logger.info(f"Daily ADX calculated: min={df_with_daily_adx['daily_adx'].min():.1f}, max={df_with_daily_adx['daily_adx'].max():.1f}")
        
    except Exception as e:
        logger.error(f"Failed to calculate daily ADX: {e}")
        df_with_daily_adx = df.copy()
        df_with_daily_adx['daily_adx'] = 0
    
    # BB Width (fast)
    try:
        bb_width = calculate_bb_width_rolling(df)
        df_with_daily_adx['bb_width'] = bb_width
        logger.info(f"BB width calculated: min={bb_width.min():.3f}, max={bb_width.max():.3f}")
    except Exception as e:
        logger.error(f"Failed to calculate BB width: {e}")
        df_with_daily_adx['bb_width'] = 0
    
    # ATR expanding (fast)
    if require_atr_expanding:
        try:
            atr_avg = df['atr'].rolling(20).mean()
            df_with_daily_adx['atr_expanding'] = df['atr'] > atr_avg
            expanding_pct = df_with_daily_adx['atr_expanding'].sum() / len(df) * 100
            logger.info(f"ATR expansion calculated: {expanding_pct:.1f}% of bars expanding")
        except Exception as e:
            logger.error(f"Failed to calculate ATR expansion: {e}")
            df_with_daily_adx['atr_expanding'] = True
    else:
        df_with_daily_adx['atr_expanding'] = True
    
    # Combine all filters
    can_trade = (
        (df_with_daily_adx['daily_adx'] >= daily_adx_min) &
        (df_with_daily_adx['bb_width'] >= bb_width_min) &
        (df_with_daily_adx['atr_expanding'])
    )
    
    tradeable_pct = can_trade.sum() / len(df) * 100
    logger.info(f"Regime filter: {tradeable_pct:.1f}% of bars are TRADEABLE")
    
    return can_trade


def regime_gate_fast(bar_time: pd.Timestamp, regime_series: pd.Series) -> bool:
    """
    Fast regime check using pre-computed series.
    
    Args:
        bar_time: Current bar timestamp
        regime_series: Pre-computed regime series from precompute_regime()
        
    Returns:
        True if can trade, False if choppy
    """
    try:
        return bool(regime_series.loc[bar_time])
    except:
        return False