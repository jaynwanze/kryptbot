#!/usr/bin/env python3
"""
FVG ORDER FLOW BACKTEST

3-Phase Reactive Trading Model:
Phase 1: PROFILE - Where is value? (Volume Profile: POC, VAH, VAL, LVN)
Phase 2: LOCATION - At institutional level? (FVG, Order Blocks)
Phase 3: ORDER FLOW - Is there a clear winner? (Buyer/Seller momentum)

Aligned with existing backtest structure for easy integration.
"""
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict
from collections import defaultdict
import asyncio
import logging
import pandas as pd
import numpy as np
from bot.data import preload_history
from bot.helpers import (
    config, compute_indicators, build_htf_levels, update_htf_levels_new,
    round_price, align_by_close, fees_usd, build_h1, update_h1
)

logging.basicConfig(level=logging.INFO)

# ═══════════════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    dir: int  # +1 long, -1 short
    entry: float
    sl: float
    tp1: float  # Target 1 (POC)
    tp2: float  # Target 2 (VAH/VAL)
    qty: float
    risk: float
    time_entry: pd.Timestamp
    time_close: Optional[pd.Timestamp] = None
    of_score: float = 0.0  # Order Flow Score
    level_type: str = ""  # FVG or Order Block
    narrative: str = ""


@dataclass
class FVG:
    """Fair Value Gap"""
    type: str  # 'bullish' or 'bearish'
    low: float
    high: float
    timestamp: pd.Timestamp
    filled: bool = False


@dataclass
class OrderBlock:
    """Order Block (last opposite candle before strong move)"""
    type: str  # 'bullish' or 'bearish'
    high: float
    low: float
    timestamp: pd.Timestamp
    tested: bool = False


@dataclass
class VolumeProfile:
    """Volume Profile for a session"""
    poc: float  # Point of Control (highest volume)
    vah: float  # Value Area High
    val: float  # Value Area Low
    hvn_zones: List[float] = None  # High Volume Nodes
    lvn_zones: List[float] = None  # Low Volume Nodes


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1: VOLUME PROFILE (MARKET STRUCTURE)
# ═══════════════════════════════════════════════════════════════════════════

def calculate_volume_profile(df: pd.DataFrame, num_bins: int = 50) -> VolumeProfile:
    """
    Calculate volume profile for session.
    Shows where price was accepted (high volume) vs rejected (low volume).
    """
    if len(df) < 10:
        mid = df['c'].mean()
        return VolumeProfile(poc=mid, vah=mid*1.01, val=mid*0.99, hvn_zones=[], lvn_zones=[])
    
    # Get price range
    price_min = df['l'].min()
    price_max = df['h'].max()
    
    # Create price bins
    bins = np.linspace(price_min, price_max, num_bins)
    volume_at_price = np.zeros(len(bins) - 1)
    
    # Distribute volume across price levels
    for idx in df.index:
        row = df.loc[idx]
        candle_low = row['l']
        candle_high = row['h']
        candle_volume = row['v']
        
        # Find which bins this candle touches
        touching_bins = (bins[:-1] >= candle_low) & (bins[1:] <= candle_high)
        num_touching = touching_bins.sum()
        
        if num_touching > 0:
            volume_at_price[touching_bins] += candle_volume / num_touching
    
    # POC = highest volume bin
    poc_idx = volume_at_price.argmax()
    poc = (bins[poc_idx] + bins[poc_idx + 1]) / 2
    
    # Value Area = 70% of volume around POC
    total_volume = volume_at_price.sum()
    target_volume = total_volume * 0.70
    
    value_area_volume = volume_at_price[poc_idx]
    lower_idx = poc_idx
    upper_idx = poc_idx
    
    while value_area_volume < target_volume and (lower_idx > 0 or upper_idx < len(volume_at_price) - 1):
        lower_vol = volume_at_price[lower_idx - 1] if lower_idx > 0 else 0
        upper_vol = volume_at_price[upper_idx + 1] if upper_idx < len(volume_at_price) - 1 else 0
        
        if lower_vol > upper_vol and lower_idx > 0:
            lower_idx -= 1
            value_area_volume += volume_at_price[lower_idx]
        elif upper_idx < len(volume_at_price) - 1:
            upper_idx += 1
            value_area_volume += volume_at_price[upper_idx]
        else:
            break
    
    val = (bins[lower_idx] + bins[lower_idx + 1]) / 2
    vah = (bins[upper_idx] + bins[upper_idx + 1]) / 2
    
    # HVN (High Volume Nodes) - areas of acceptance
    avg_volume = volume_at_price.mean()
    hvn_zones = [
        (bins[i] + bins[i + 1]) / 2
        for i, vol in enumerate(volume_at_price)
        if vol > avg_volume * 1.5
    ]
    
    # LVN (Low Volume Nodes) - areas of rejection/imbalance
    lvn_zones = [
        (bins[i] + bins[i + 1]) / 2
        for i, vol in enumerate(volume_at_price)
        if vol < avg_volume * 0.5 and vol > 0
    ]
    
    return VolumeProfile(
        poc=poc,
        vah=vah,
        val=val,
        hvn_zones=hvn_zones if hvn_zones else [],
        lvn_zones=lvn_zones if lvn_zones else []
    )


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2: LOCATION (FVG & ORDER BLOCKS)
# ═══════════════════════════════════════════════════════════════════════════

def detect_fvg_bullish(df: pd.DataFrame, i: int) -> Optional[FVG]:
    """
    Bullish FVG: Gap between candle[i-2].high and candle[i].low
    where candle[i-1] is strong bullish move.
    """
    if i < 2:
        return None
    
    c0 = df.iloc[i-2]  # Earlier candle
    c1 = df.iloc[i-1]  # Middle candle (should be bullish)
    c2 = df.iloc[i]    # Current candle
    
    # Middle candle must be bullish
    if c1.c <= c1.o:
        return None
    
    # Check for gap
    gap_low = c0.h
    gap_high = c2.l
    
    if gap_low >= gap_high:
        return None
    
    # Middle candle must be strong (body > 60% of range)
    body = abs(c1.c - c1.o)
    candle_range = c1.h - c1.l
    
    if candle_range > 0 and body / candle_range < 0.6:
        return None
    
    return FVG(
        type='bullish',
        low=gap_low,
        high=gap_high,
        timestamp=c1.name
    )


def detect_fvg_bearish(df: pd.DataFrame, i: int) -> Optional[FVG]:
    """
    Bearish FVG: Gap between candle[i-2].low and candle[i].high
    where candle[i-1] is strong bearish move.
    """
    if i < 2:
        return None
    
    c0 = df.iloc[i-2]
    c1 = df.iloc[i-1]
    c2 = df.iloc[i]
    
    # Middle candle must be bearish
    if c1.c >= c1.o:
        return None
    
    # Check for gap
    gap_high = c0.l
    gap_low = c2.h
    
    if gap_low >= gap_high:
        return None
    
    # Middle candle must be strong
    body = abs(c1.c - c1.o)
    candle_range = c1.h - c1.l
    
    if candle_range > 0 and body / candle_range < 0.6:
        return None
    
    return FVG(
        type='bearish',
        low=gap_low,
        high=gap_high,
        timestamp=c1.name
    )


def detect_order_block_bullish(df: pd.DataFrame, i: int, lookback: int = 10) -> Optional[OrderBlock]:
    """
    Bullish OB: Last bearish candle before strong bullish move.
    Shows where institutions accumulated before pushing price up.
    """
    if i < lookback:
        return None
    
    bar = df.iloc[i]
    
    # Current candle must be strongly bullish
    if bar.c <= bar.o:
        return None
    
    move_size = bar.h - bar.l
    if move_size < 2.0 * bar.atr:
        return None
    
    # Find last bearish candle in lookback
    for j in range(i-1, max(0, i-lookback), -1):
        prev = df.iloc[j]
        if prev.c < prev.o:  # Bearish candle
            return OrderBlock(
                type='bullish',
                high=prev.h,
                low=prev.l,
                timestamp=prev.name
            )
    
    return None


def detect_order_block_bearish(df: pd.DataFrame, i: int, lookback: int = 10) -> Optional[OrderBlock]:
    """
    Bearish OB: Last bullish candle before strong bearish move.
    Shows where institutions distributed before pushing price down.
    """
    if i < lookback:
        return None
    
    bar = df.iloc[i]
    
    # Current candle must be strongly bearish
    if bar.c >= bar.o:
        return None
    
    move_size = bar.h - bar.l
    if move_size < 2.0 * bar.atr:
        return None
    
    # Find last bullish candle in lookback
    for j in range(i-1, max(0, i-lookback), -1):
        prev = df.iloc[j]
        if prev.c > prev.o:  # Bullish candle
            return OrderBlock(
                type='bearish',
                high=prev.h,
                low=prev.l,
                timestamp=prev.name
            )
    
    return None


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 3: ORDER FLOW (BUYER VS SELLER MOMENTUM)
# ═══════════════════════════════════════════════════════════════════════════

def calculate_order_flow_score(df: pd.DataFrame, i: int, lookback: int = 10) -> float:
    """
    Calculate Order Flow Score (-100 to +100).
    
    Positive = Buyers winning
    Negative = Sellers winning
    Near 0 = Battle zone (AVOID)
    
    Based on:
    - Candle closes (bullish vs bearish)
    - Volume on up moves vs down moves
    - Price momentum (higher highs/lows)
    - Wick analysis (rejection patterns)
    """
    if i < lookback:
        return 0.0
    
    recent = df.iloc[i-lookback+1:i+1]
    
    # 1. Candle direction score (40% weight)
    bullish_candles = (recent.c > recent.o).sum()
    bearish_candles = (recent.c < recent.o).sum()
    candle_score = (bullish_candles - bearish_candles) / lookback * 100
    
    # 2. Volume-weighted direction (30% weight)
    up_volume = recent[recent.c > recent.o]['v'].sum()
    down_volume = recent[recent.c < recent.o]['v'].sum()
    total_volume = up_volume + down_volume
    
    if total_volume > 0:
        volume_score = ((up_volume - down_volume) / total_volume) * 100
    else:
        volume_score = 0
    
    # 3. Momentum score (20% weight)
    recent_highs = recent['h'].values
    recent_lows = recent['l'].values
    
    higher_highs = sum(recent_highs[i] > recent_highs[i-1] for i in range(1, len(recent_highs)))
    higher_lows = sum(recent_lows[i] > recent_lows[i-1] for i in range(1, len(recent_lows)))
    lower_highs = sum(recent_highs[i] < recent_highs[i-1] for i in range(1, len(recent_highs)))
    lower_lows = sum(recent_lows[i] < recent_lows[i-1] for i in range(1, len(recent_lows)))
    
    bullish_structure = higher_highs + higher_lows
    bearish_structure = lower_highs + lower_lows
    total_points = bullish_structure + bearish_structure
    
    if total_points > 0:
        momentum_score = ((bullish_structure - bearish_structure) / total_points) * 100
    else:
        momentum_score = 0
    
    # 4. Last candle rejection (10% weight)
    last = recent.iloc[-1]
    body = abs(last.c - last.o)
    lower_wick = min(last.o, last.c) - last.l
    upper_wick = last.h - max(last.o, last.c)
    
    if body > 0:
        if lower_wick > body:  # Rejection of lows = bullish
            rejection_score = 100
        elif upper_wick > body:  # Rejection of highs = bearish
            rejection_score = -100
        else:
            rejection_score = 0
    else:
        rejection_score = 0
    
    # Weighted combination
    final_score = (
        candle_score * 0.40 +
        volume_score * 0.30 +
        momentum_score * 0.20 +
        rejection_score * 0.10
    )
    
    return np.clip(final_score, -100, 100)


def is_near_level(price: float, level: float, atr: float, tolerance: float = 0.5) -> bool:
    """Check if price is near a level (within tolerance * ATR)."""
    return abs(price - level) <= tolerance * atr


# ═══════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION (3-PHASE MODEL)
# ═══════════════════════════════════════════════════════════════════════════

def check_long_signal(df: pd.DataFrame, i: int,
                      fvgs: List[FVG],
                      obs: List[OrderBlock],
                      vp: VolumeProfile,
                      min_of_score: int = 60) -> Optional[Dict]:
    """
    LONG signal requires ALL 3 phases:
    
    Phase 1 (PROFILE): Price in lower value area, near LVN
    Phase 2 (LOCATION): At bullish FVG or Order Block
    Phase 3 (ORDER FLOW): Clear bullish momentum (score > min_of_score)
    """
    bar = df.iloc[i]
    price = bar.c
    atr = bar.atr
    
    # ========== PHASE 1: PROFILE ==========
    # Must be in lower value area (below POC) or at VAL
    if price > vp.vah:
        return None  # Too high, no value
    
    # Must be near a Low Volume Node (area of imbalance)
    near_lvn = any(is_near_level(price, lvn, atr, 0.5) for lvn in vp.lvn_zones)
    
    if not near_lvn and not is_near_level(price, vp.val, atr, 0.5):
        return None  # Not at key rejection zone
    
    # ========== PHASE 2: LOCATION ==========
    # Must be at bullish FVG or Order Block
    at_fvg = False
    fvg_level = None
    
    for fvg in fvgs:
        if fvg.type == 'bullish' and not fvg.filled:
            if fvg.low <= price <= fvg.high:
                at_fvg = True
                fvg_level = (fvg.low + fvg.high) / 2
                fvg.filled = True  # Mark as filled
                break
    
    at_ob = False
    ob_level = None
    
    for ob in obs:
        if ob.type == 'bullish' and not ob.tested:
            if ob.low <= price <= ob.high:
                at_ob = True
                ob_level = (ob.low + ob.high) / 2
                ob.tested = True  # Mark as tested
                break
    
    if not (at_fvg or at_ob):
        return None  # Not at institutional level
    
    level_type = "FVG" if at_fvg else "OrderBlock"
    level_price = fvg_level if at_fvg else ob_level
    
    # ========== PHASE 3: ORDER FLOW ==========
    of_score = calculate_order_flow_score(df, i)
    
    if of_score < min_of_score:
        return None  # Buyers not clearly winning yet - WAIT
    
    # Check for rejection of lows (bullish wick)
    lower_wick = min(bar.o, bar.c) - bar.l
    body = abs(bar.c - bar.o)
    
    if body > 0 and lower_wick < body:
        return None  # No clear rejection
    
    # Must close above the level (showing strength)
    if bar.c <= level_price:
        return None
    
    # ========== ALL PHASES PASSED - GENERATE SIGNAL ==========
    stop_loss = bar.l - (atr * 0.2)  # Below rejection wick
    target_1 = vp.poc  # First target: POC (fair value)
    target_2 = vp.vah  # Second target: VAH (upper value)
    
    risk = bar.c - stop_loss
    reward_1 = target_1 - bar.c
    reward_2 = target_2 - bar.c
    
    rr_1 = reward_1 / risk if risk > 0 else 0
    rr_2 = reward_2 / risk if risk > 0 else 0
    
    return {
        'direction': 'LONG',
        'entry': bar.c,
        'sl': stop_loss,
        'tp1': target_1,
        'tp2': target_2,
        'rr1': rr_1,
        'rr2': rr_2,
        'of_score': of_score,
        'level_type': level_type,
        'level_price': level_price,
        'narrative': (
            f"Bullish {level_type} @ {level_price:.2f} | "
            f"OF Score {of_score:.0f} | "
            f"Lower VA targeting POC {vp.poc:.2f}"
        )
    }


def check_short_signal(df: pd.DataFrame, i: int,
                       fvgs: List[FVG],
                       obs: List[OrderBlock],
                       vp: VolumeProfile,
                       min_of_score: int = 60) -> Optional[Dict]:
    """
    SHORT signal requires ALL 3 phases:
    
    Phase 1 (PROFILE): Price in upper value area, near LVN
    Phase 2 (LOCATION): At bearish FVG or Order Block
    Phase 3 (ORDER FLOW): Clear bearish momentum (score < -min_of_score)
    """
    bar = df.iloc[i]
    price = bar.c
    atr = bar.atr
    
    # ========== PHASE 1: PROFILE ==========
    if price < vp.val:
        return None  # Too low
    
    near_lvn = any(is_near_level(price, lvn, atr, 0.5) for lvn in vp.lvn_zones)
    
    if not near_lvn and not is_near_level(price, vp.vah, atr, 0.5):
        return None
    
    # ========== PHASE 2: LOCATION ==========
    at_fvg = False
    fvg_level = None
    
    for fvg in fvgs:
        if fvg.type == 'bearish' and not fvg.filled:
            if fvg.low <= price <= fvg.high:
                at_fvg = True
                fvg_level = (fvg.low + fvg.high) / 2
                fvg.filled = True
                break
    
    at_ob = False
    ob_level = None
    
    for ob in obs:
        if ob.type == 'bearish' and not ob.tested:
            if ob.low <= price <= ob.high:
                at_ob = True
                ob_level = (ob.low + ob.high) / 2
                ob.tested = True
                break
    
    if not (at_fvg or at_ob):
        return None
    
    level_type = "FVG" if at_fvg else "OrderBlock"
    level_price = fvg_level if at_fvg else ob_level
    
    # ========== PHASE 3: ORDER FLOW ==========
    of_score = calculate_order_flow_score(df, i)
    
    if of_score > -min_of_score:
        return None  # Sellers not clearly winning yet - WAIT
    
    # Check for rejection of highs (bearish wick)
    upper_wick = bar.h - max(bar.o, bar.c)
    body = abs(bar.c - bar.o)
    
    if body > 0 and upper_wick < body:
        return None
    
    # Must close below the level
    if bar.c >= level_price:
        return None
    
    # ========== ALL PHASES PASSED - GENERATE SIGNAL ==========
    stop_loss = bar.h + (atr * 0.2)
    target_1 = vp.poc
    target_2 = vp.val
    
    risk = stop_loss - bar.c
    reward_1 = bar.c - target_1
    reward_2 = bar.c - target_2
    
    rr_1 = reward_1 / risk if risk > 0 else 0
    rr_2 = reward_2 / risk if risk > 0 else 0
    
    return {
        'direction': 'SHORT',
        'entry': bar.c,
        'sl': stop_loss,
        'tp1': target_1,
        'tp2': target_2,
        'rr1': rr_1,
        'rr2': rr_2,
        'of_score': of_score,
        'level_type': level_type,
        'level_price': level_price,
        'narrative': (
            f"Bearish {level_type} @ {level_price:.2f} | "
            f"OF Score {of_score:.0f} | "
            f"Upper VA targeting POC {vp.poc:.2f}"
        )
    }


# ═══════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def backtest(df: pd.DataFrame,
             equity0: float = 1000.0,
             risk_pct: float = 0.1,
             pair: str = "LINKUSDT",
             min_of_score: int = 60):
    """
    FVG Order Flow backtest with 3-phase model.
    
    Args:
        df: DataFrame with OHLCV data
        equity0: Starting equity
        risk_pct: Risk per trade (0.1 = 10%)
        pair: Trading pair
        min_of_score: Minimum Order Flow score to enter (60 = balanced)
    """
    # Compute indicators
    df = compute_indicators(df.copy())
    
    equity = equity0
    pos: Optional[Position] = None
    trades: List[Dict] = []
    curve: List[float] = []
    
    # Pattern tracking
    fvgs: List[FVG] = []
    obs: List[OrderBlock] = []
    
    # Session tracking for Volume Profile
    vp_period = 24  # Recalculate every 24 bars (1 day for hourly, 1.5 days for 15m)
    session_start = 0
    current_vp = None
    
    # Stats
    signals_checked = 0
    phase1_pass = 0
    phase2_pass = 0
    phase3_pass = 0
    
    # Test on last 30 days
    test_start = max(0, len(df) - 100*96)  # 30 days of 15m bars
    
    for i in range(test_start, len(df)):
        bar = df.iloc[i]
        
        # Warm-up guard
        if bar[["atr", "atr30", "adx", "k_fast"]].isna().any():
            curve.append(equity)
            continue
        
        # Update Volume Profile periodically
        if i % vp_period == 0 or current_vp is None:
            session_df = df.iloc[max(0, i-vp_period):i+1]
            current_vp = calculate_volume_profile(session_df)
        
        # Update patterns
        new_fvg_bull = detect_fvg_bullish(df, i)
        if new_fvg_bull:
            fvgs.append(new_fvg_bull)
            fvgs = [f for f in fvgs if not f.filled][-50:]  # Keep last 50 unfilled
        
        new_fvg_bear = detect_fvg_bearish(df, i)
        if new_fvg_bear:
            fvgs.append(new_fvg_bear)
            fvgs = [f for f in fvgs if not f.filled][-50:]
        
        new_ob_bull = detect_order_block_bullish(df, i)
        if new_ob_bull:
            obs.append(new_ob_bull)
            obs = [o for o in obs if not o.tested][-20:]  # Keep last 20 untested
        
        new_ob_bear = detect_order_block_bearish(df, i)
        if new_ob_bear:
            obs.append(new_ob_bear)
            obs = [o for o in obs if not o.tested][-20:]
        
        # --- Manage open position ---
        if pos is not None:
            if pos.dir == 1:
                hit_sl = bar.l <= pos.sl
                hit_tp1 = bar.h >= pos.tp1
                hit_tp2 = bar.h >= pos.tp2
            else:
                hit_sl = bar.h >= pos.sl
                hit_tp1 = bar.l <= pos.tp1
                hit_tp2 = bar.l <= pos.tp2
            
            reason = None
            exit_px = None
            
            if hit_sl:
                reason = "SL"
                exit_px = pos.sl
            elif hit_tp2:
                reason = "TP2"
                exit_px = pos.tp2
            elif hit_tp1:
                reason = "TP1"
                exit_px = pos.tp1
            
            if reason:
                pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                fee = fees_usd(pos.entry, exit_px, pos.qty, config.FEE_BPS)
                pnl = pnl_gross - fee
                
                equity += pnl
                
                trades.append(dict(
                    dir="LONG" if pos.dir == 1 else "SHORT",
                    t_entry=pos.time_entry, t_exit=bar.name,
                    entry=pos.entry, sl=pos.sl, tp1=pos.tp1, tp2=pos.tp2,
                    exit=exit_px, reason=reason,
                    pnl=pnl, equity=equity,
                    of_score=pos.of_score,
                    level_type=pos.level_type,
                    narrative=pos.narrative
                ))
                
                logging.info(
                    "[%s] %s %s | entry %.4f sl %.4f tp1 %.4f tp2 %.4f | exit %.4f | OF %.0f | pnl $%.2f | eq $%.2f",
                    pair, trades[-1]["dir"], reason,
                    pos.entry, pos.sl, pos.tp1, pos.tp2, exit_px,
                    pos.of_score, pnl, equity
                )
                pos = None
        
        # --- Look for new entry ---
        if pos is None and current_vp is not None:
            signals_checked += 1
            
            # Check LONG
            long_sig = check_long_signal(df, i, fvgs, obs, current_vp, min_of_score)
            
            if long_sig:
                if i + 1 < len(df):
                    entry = float(df.iloc[i+1].o) * (1 + config.SLIP_BPS / 10_000)
                else:
                    entry = float(bar.c) * (1 + config.SLIP_BPS / 10_000)
                
                risk_usd = equity0 * risk_pct
                stop_off = entry - long_sig['sl']
                qty = risk_usd / stop_off if stop_off > 0 else 0
                
                if qty > 0:
                    pos = Position(
                        dir=1,
                        entry=entry,
                        sl=round_price(long_sig['sl'], pair, config),
                        tp1=round_price(long_sig['tp1'], pair, config),
                        tp2=round_price(long_sig['tp2'], pair, config),
                        qty=qty,
                        risk=risk_usd,
                        time_entry=bar.name,
                        of_score=long_sig['of_score'],
                        level_type=long_sig['level_type'],
                        narrative=long_sig['narrative']
                    )
                    
                    phase3_pass += 1
                    
                    logging.info(
                        "[%s] FVG LONG @ %s | entry %.4f sl %.4f tp1 %.4f tp2 %.4f | OF %.0f | %s",
                        pair, bar.name, entry, pos.sl, pos.tp1, pos.tp2,
                        pos.of_score, pos.narrative
                    )
            
            # Check SHORT
            else:
                short_sig = check_short_signal(df, i, fvgs, obs, current_vp, min_of_score)
                
                if short_sig:
                    if i + 1 < len(df):
                        entry = float(df.iloc[i+1].o) * (1 - config.SLIP_BPS / 10_000)
                    else:
                        entry = float(bar.c) * (1 - config.SLIP_BPS / 10_000)
                    
                    risk_usd = equity0 * risk_pct
                    stop_off = short_sig['sl'] - entry
                    qty = risk_usd / stop_off if stop_off > 0 else 0
                    
                    if qty > 0:
                        pos = Position(
                            dir=-1,
                            entry=entry,
                            sl=round_price(short_sig['sl'], pair, config),
                            tp1=round_price(short_sig['tp1'], pair, config),
                            tp2=round_price(short_sig['tp2'], pair, config),
                            qty=qty,
                            risk=risk_usd,
                            time_entry=bar.name,
                            of_score=short_sig['of_score'],
                            level_type=short_sig['level_type'],
                            narrative=short_sig['narrative']
                        )
                        
                        phase3_pass += 1
                        
                        logging.info(
                            "[%s] FVG SHORT @ %s | entry %.4f sl %.4f tp1 %.4f tp2 %.4f | OF %.0f | %s",
                            pair, bar.name, entry, pos.sl, pos.tp1, pos.tp2,
                            pos.of_score, pos.narrative
                        )
        
        curve.append(equity)
    
    # Summary
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = wins + losses
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    pf = float("inf") if gl == 0 else (gp / gl if gl > 0 else 0)
    
    # Exit breakdown
    sl_exits = sum(1 for t in trades if t["reason"] == "SL")
    tp1_exits = sum(1 for t in trades if t["reason"] == "TP1")
    tp2_exits = sum(1 for t in trades if t["reason"] == "TP2")
    
    # Level type breakdown
    fvg_trades = sum(1 for t in trades if t["level_type"] == "FVG")
    ob_trades = sum(1 for t in trades if t["level_type"] == "OrderBlock")
    
    summary = dict(
        pair=pair,
        trades=total,
        wins=wins,
        losses=losses,
        win_rate=(wins / total * 100) if total else 0.0,
        profit_factor=pf,
        equity_final=equity,
        net_pnl=equity - equity0,
        avg_win=gp / wins if wins else 0,
        avg_loss=gl / losses if losses else 0,
        sl_exits=sl_exits,
        tp1_exits=tp1_exits,
        tp2_exits=tp2_exits,
        fvg_entries=fvg_trades,
        ob_entries=ob_trades,
        min_of_score=min_of_score,
        risk_pct=risk_pct * 100,
        signals_checked=signals_checked,
        phase3_pass=phase3_pass,
        signal_rate=(phase3_pass / signals_checked * 100) if signals_checked else 0
    )
    
    return summary, trades, pd.Series(curve, index=df.index[:len(curve)])


def print_trades_table(trades: List[Dict]):
    """Print formatted trade log."""
    if not trades:
        print("\nNo trades executed.")
        return
    
    print(f"\n{'='*120}")
    print(f"{'Entry Time':<20} {'Dir':<6} {'Entry':<8} {'Exit':<8} {'Reason':<6} "
          f"{'OF':<5} {'Level':<12} {'PnL':<10} {'Equity':<10}")
    print(f"{'='*120}")
    for t in trades:
        print(f"{str(t['t_entry']):<20} {t['dir']:<6} "
              f"{t['entry']:<8.4f} {t['exit']:<8.4f} {t['reason']:<6} "
              f"{t['of_score']:<5.0f} {t['level_type']:<12} "
              f"${t['pnl']:>8.2f} ${t['equity']:>9.2f}")
    print(f"{'='*120}\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s"
    )
    
    # Config
    pair = "SOLUSDT"
    interval = "60"
    equity = getattr(config, "STAKE_SIZE_USD", 1000.0)
    risk_pct = getattr(config, "RISK_PCT", 0.1)
    min_of_score = 40  # Minimum Order Flow score to enter
    
    logging.info("="*80)
    logging.info("FVG ORDER FLOW BACKTEST (3-Phase Reactive Model)")
    logging.info("Pair: %s | TF: %sm | Starting Equity: $%.2f | Risk/Trade: %.1f%%",
                 pair, interval, equity, risk_pct * 100)
    logging.info("Min Order Flow Score: %d (adjust for more/fewer signals)", min_of_score)
    logging.info("="*80)
    
    # Load data
    days_back = 100
    bars_needed = days_back * 96
    
    hist = asyncio.run(preload_history(
        symbol=pair,
        interval=interval,
        limit=bars_needed
    ))
    
    hist = align_by_close(hist, int(interval))
    
    logging.info("Data loaded: %d bars (%s to %s)",
                 len(hist), hist.index[0], hist.index[-1])
    
    # Run backtest
    summary, trades, curve = backtest(
        hist,
        equity0=equity,
        risk_pct=risk_pct,
        pair=pair,
        min_of_score=min_of_score
    )
    
    # Display results
    print("\n" + "="*80)
    print("BACKTEST SUMMARY")
    print("="*80)
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"{k:.<35} {v:>12.2f}")
        else:
            print(f"{k:.<35} {v:>12}")
    print("="*80)
    
    print_trades_table(trades)
    
    # Save to CSV
    if trades:
        df_trades = pd.DataFrame(trades)
        output_file = f"backtest_fvg_{pair}_{datetime.now():%Y%m%d_%H%M}.csv"
        df_trades.to_csv(output_file, index=False)
        logging.info("Trades saved to %s", output_file)