from bot.infra.models import FvgOrderFlowSignal, VolumeProfile, FVG
from typing import Dict, List, Optional
import pandas as pd
from bot.helpers.config import fvg_orderflow_config as CONFIG
import numpy as np

def calculate_volume_profile(df: pd.DataFrame, lookback: int = 48) -> Optional[VolumeProfile]:
    """
    Calculate volume profile for 15m timeframe.
    lookback=48 = 12 hours of trading
    """
    if len(df) < lookback:
        return None

    recent = df.iloc[-lookback:]

    # Simple VWAP-based POC
    poc = (recent['c'] * recent['v']).sum() / recent['v'].sum()

    # VAH/VAL based on std dev
    std = recent['c'].std()
    vah = poc + std * 0.8  # Tighter bands for 15m
    val = poc - std * 0.8

    return VolumeProfile(poc=poc, vah=vah, val=val)


def detect_fvg_bullish(df: pd.DataFrame, i: int) -> Optional[FVG]:
    """Bullish FVG for 15m"""
    if i < 2:
        return None

    c0 = df.iloc[i-2]
    c1 = df.iloc[i-1]
    c2 = df.iloc[i]

    if c1.c <= c1.o:
        return None

    gap_low = c0.h
    gap_high = c2.l

    if gap_low >= gap_high:
        return None

    # Check minimum gap size
    gap_pct = (gap_high - gap_low) / gap_low * 100
    if gap_pct < CONFIG.FVG_MIN_GAP_PCT:
        return None

    return FVG(type='bullish', low=gap_low, high=gap_high, timestamp=c1.name)




def detect_fvg_bearish(df: pd.DataFrame, i: int) -> Optional[FVG]:
    """Bearish FVG for 15m"""
    if i < 2:
        return None

    c0 = df.iloc[i-2]
    c1 = df.iloc[i-1]
    c2 = df.iloc[i]

    if c1.c >= c1.o:
        return None

    gap_high = c0.l
    gap_low = c2.h

    if gap_low >= gap_high:
        return None

    gap_pct = (gap_high - gap_low) / gap_low * 100
    if gap_pct < CONFIG.FVG_MIN_GAP_PCT:
        return None

    return FVG(type='bearish', low=gap_low, high=gap_high, timestamp=c1.name)


def calculate_order_flow_score(df: pd.DataFrame, idx: int) -> float:
    """
    Calculate Order Flow score optimized for 15-minute timeframe.

    Components:
    - Momentum (40pts): 5-bar price change
    - Candle Bodies (30pts): Recent body strength
    - Volume (30pts): Volume surge vs average

    Total multiplied by 2.0 for 15m dynamics
    """
    lookback = CONFIG.OF_LOOKBACK

    if idx < lookback:
        return 0

    window = df.iloc[idx - lookback + 1:idx + 1]

    if len(window) < 2:
        return 0

    # Component 1: Price Momentum (faster response)
    price_change = (window['c'].iloc[-1] - window['c'].iloc[0]) / window['c'].iloc[0]
    momentum_score = np.clip(price_change * 1000, -40, 40)

    # Component 2: Candle Bodies (current candle strength)
    bodies = abs(window['c'] - window['o'])
    avg_body = bodies.mean()
    last_body = bodies.iloc[-1]

    body_score = 0
    if avg_body > 0:
        body_strength = (last_body / avg_body - 1) * 30
        body_score = np.clip(body_strength, -30, 30)

    # Component 3: Volume Surge
    volumes = window['v']
    avg_vol = volumes[:-1].mean() if len(volumes) > 1 else 1
    last_vol = volumes.iloc[-1]

    vol_score = 0
    if avg_vol > 0:
        vol_strength = (last_vol / avg_vol - 1) * 30
        vol_score = np.clip(vol_strength, -30, 30)

    # Apply 15m multiplier
    total_score = (momentum_score + body_score + vol_score) * CONFIG.OF_MULTIPLIER

    return total_score



def check_long_signal(df: pd.DataFrame, idx: int, fvgs: List[FVG],
                          vp: VolumeProfile) -> Optional[Dict]:
    """
    LONG signal for 15m:
    1. Price near unfilled bullish FVG
    2. Order Flow score > 30 (bullish momentum)
    3. ADX > 20 (some trend strength)
    4. Volume surge (1.2x average)
    5. Below POC (value area)
    """
    bar = df.iloc[idx]

    # Check for recent bullish FVG
    active_fvg = None
    for fvg in fvgs:
        if fvg.type == 'bullish' and not fvg.filled:
            # Price testing FVG zone
            if bar.l <= fvg.high and bar.c >= fvg.low:
                active_fvg = fvg
                break

    if not active_fvg:
        return None

    # Calculate OF score
    of_score = calculate_order_flow_score(df, idx)
    if of_score < CONFIG.MIN_OF_SCORE:
        return None

    # ADX filter (relaxed for 15m)
    if bar.adx < CONFIG.MIN_ADX:
        return None

    # Volume filter
    if idx >= 20:
        avg_vol = df.iloc[idx-20:idx]['v'].mean()
        if bar.v < avg_vol * CONFIG.MIN_VOLUME_RATIO:
            return None

    # Value area filter (prefer entries below POC)
    if bar.c > vp.poc * 1.01:  # Allow 1% above POC
        return None

    # Entry at FVG mid-point
    entry = (active_fvg.low + active_fvg.high) / 2

    # Stop Loss (tight for 15m)
    atr = bar.get('atr', bar.c * 0.01)
    sl = entry - (atr * CONFIG.ATR_MULT_SL)

    # Take Profits
    risk = entry - sl
    tp1 = entry + (risk * CONFIG.TARGET_R1)
    tp2 = entry + (risk * CONFIG.TARGET_R2)

    # Mark FVG as filled
    active_fvg.filled = True

    return {
        'entry': entry,
        'sl': sl,
        'tp1': tp1,
        'tp2': tp2,
        'of_score': of_score,
        'level_type': 'FVG',
        'narrative': f"Bullish FVG @ {active_fvg.low:.2f}-{active_fvg.high:.2f} | OF {of_score:.0f} | Lower VA targeting POC {vp.poc:.2f}"
    }


def check_long_signal(
    df: pd.DataFrame,
    idx: int,
    fvgs: List[FVG],
    vp: VolumeProfile,
    pair: str = "UNKNOWN"
) -> Optional[FvgOrderFlowSignal]:
    """
    LONG signal for 15m - Returns FvgOrderFlowSignal dataclass.

    Conditions:
    1. Price near unfilled bullish FVG
    2. Order Flow score > 30 (bullish momentum)
    3. ADX > 20 (some trend strength)
    4. Volume surge (1.2x average)
    5. Below POC (value area)

    Returns:
        FvgOrderFlowSignal if all conditions met, None otherwise
    """
    bar = df.iloc[idx]

    # Check for recent bullish FVG
    active_fvg = None
    for fvg in fvgs:
        if fvg.type == 'bullish' and not fvg.filled:
            # Price testing FVG zone
            if bar.l <= fvg.high and bar.c >= fvg.low:
                active_fvg = fvg
                break

    if not active_fvg:
        return None

    # Calculate OF score
    of_score = calculate_order_flow_score(df, idx)
    if of_score < CONFIG.MIN_OF_SCORE:
        return None

    # ADX filter (relaxed for 15m)
    if bar.adx < CONFIG.MIN_ADX:
        return None

    # Volume filter
    if idx >= 20:
        avg_vol = df.iloc[idx-20:idx]['v'].mean()
        if bar.v < avg_vol * CONFIG.MIN_VOLUME_RATIO:
            return None

    # Value area filter (prefer entries below POC)
    if bar.c > vp.poc * 1.01:  # Allow 1% above POC
        return None

    # Entry at FVG mid-point
    entry = (active_fvg.low + active_fvg.high) / 2

    # Stop Loss (tight for 15m)
    atr = bar.get('atr', bar.c * 0.01)
    sl = entry - (atr * CONFIG.ATR_MULT_SL)

    # Take Profits
    risk = entry - sl
    tp1 = entry + (risk * CONFIG.TARGET_R1)
    tp2 = entry + (risk * CONFIG.TARGET_R2)

    # Mark FVG as filled
    active_fvg.filled = True

    # Create unique key
    key = f"{pair}-{bar.name.strftime('%Y%m%d%H%M')}"

    # Build narrative
    narrative = (
        f"Bullish FVG @ {active_fvg.low:.2f}-{active_fvg.high:.2f} | "
        f"OF {of_score:.0f} | Lower VA targeting POC {vp.poc:.2f}"
    )

    # Return FvgOrderFlowSignal dataclass
    return FvgOrderFlowSignal(
        symbol=pair,
        side="Buy",
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        key=key,
        ts=bar.name.to_pydatetime(),
        adx=bar.adx,
        k_fast=bar.get('stoch_k', 50.0),
        k_slow=bar.get('stoch_k_slow', 50.0),
        d_slow=bar.get('stoch_d_slow', 50.0),
        vol=bar.v,
        off_sl=risk,
        off_tp1=risk * CONFIG.TARGET_R1,
        off_tp2=risk * CONFIG.TARGET_R2,
        of_score=of_score,
        level_type='FVG',
        narrative=narrative
    )


def check_short_signal(
    df: pd.DataFrame,
    idx: int,
    fvgs: List[FVG],
    vp: VolumeProfile,
    pair: str = "UNKNOWN"
) -> Optional[FvgOrderFlowSignal]:
    """
    SHORT signal for 15m - Returns FvgOrderFlowSignal dataclass.

    Conditions:
    1. Price near unfilled bearish FVG
    2. Order Flow score < -30 (bearish momentum)
    3. ADX > 20 (some trend strength)
    4. Volume surge (1.2x average)
    5. Above POC (value area)

    Returns:
        FvgOrderFlowSignal if all conditions met, None otherwise
    """
    bar = df.iloc[idx]

    # Check for bearish FVG
    active_fvg = None
    for fvg in fvgs:
        if fvg.type == 'bearish' and not fvg.filled:
            if bar.h >= fvg.low and bar.c <= fvg.high:
                active_fvg = fvg
                break

    if not active_fvg:
        return None

    # OF score (negative for shorts)
    of_score = calculate_order_flow_score(df, idx)
    if of_score > -CONFIG.MIN_OF_SCORE:
        return None

    # ADX filter
    if bar.adx < CONFIG.MIN_ADX:
        return None

    # Volume filter
    if idx >= 20:
        avg_vol = df.iloc[idx-20:idx]['v'].mean()
        if bar.v < avg_vol * CONFIG.MIN_VOLUME_RATIO:
            return None

    # Value area filter (prefer entries above POC)
    if bar.c < vp.poc * 0.99:
        return None

    # Entry
    entry = (active_fvg.low + active_fvg.high) / 2

    # Stop Loss
    atr = bar.get('atr', bar.c * 0.01)
    sl = entry + (atr * CONFIG.ATR_MULT_SL)

    # Take Profits
    risk = sl - entry
    tp1 = entry - (risk * CONFIG.TARGET_R1)
    tp2 = entry - (risk * CONFIG.TARGET_R2)

    active_fvg.filled = True

    # Create unique key
    key = f"{pair}-{bar.name.strftime('%Y%m%d%H%M')}"

    # Build narrative
    narrative = (
        f"Bearish FVG @ {active_fvg.low:.2f}-{active_fvg.high:.2f} | "
        f"OF {of_score:.0f} | Upper VA targeting POC {vp.poc:.2f}"
    )

    # Return FvgOrderFlowSignal dataclass
    return FvgOrderFlowSignal(
        symbol=pair,
        side="Sell",
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        key=key,
        ts=bar.name.to_pydatetime(),
        adx=bar.adx,
        k_fast=bar.get('stoch_k', 50.0),
        k_slow=bar.get('stoch_k_slow', 50.0),
        d_slow=bar.get('stoch_d_slow', 50.0),
        vol=bar.v,
        off_sl=risk,
        off_tp1=risk * CONFIG.TARGET_R1,
        off_tp2=risk * CONFIG.TARGET_R2,
        of_score=of_score,
        level_type='FVG',
        narrative=narrative
    )
