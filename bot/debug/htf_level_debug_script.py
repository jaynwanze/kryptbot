#!/usr/bin/env python3
"""
Debug HTF level discrepancies between live and backtest.

This script compares:
1. HTF levels calculated by build_htf_levels (backtest method)
2. HTF levels calculated by incremental updates (live method)
3. Data timestamps and alignment
"""
import os, sys

from bot.helpers.config import config
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import asyncio
import pandas as pd
from bot.data import preload_history
from bot.helpers import (
    compute_indicators, build_htf_levels, 
    update_htf_levels_new, build_h1, update_h1
)

# The specific bar we're investigating
TARGET_TIME = pd.Timestamp("2025-10-17 00:30:00", tz='UTC')
PAIR = "LINKUSDT"
INTERVAL = "15"

print("="*80)
print(f"HTF LEVEL DISCREPANCY DEBUGGER")
print(f"Investigating: {PAIR} @ {TARGET_TIME}")
print("="*80)

async def main():
    # Load data
    days_back = 31
    bars_needed = days_back * 96
    
    hist = await preload_history(
        symbol=PAIR,
        interval=INTERVAL,
        limit=bars_needed
    )
    
    # Align to CLOSE timestamps
    hist.index = hist.index + pd.Timedelta(minutes=int(INTERVAL))
    hist = compute_indicators(hist)
    
    print(f"\nData loaded: {len(hist)} bars")
    # Add this to your debug script after loading data
    print("\n" + "="*80)
    print("DAILY LEVEL DIAGNOSTIC")
    print("="*80)

    target_day_start = pd.Timestamp("2025-10-17 00:00:00", tz='UTC')
    target_day_end = pd.Timestamp("2025-10-17 23:59:59", tz='UTC')

    # What was the actual daily low for Oct 17?
    oct17_data = hist[(hist.index >= target_day_start) & (hist.index <= target_day_end)]
    actual_daily_low = oct17_data['l'].min()
    actual_daily_high = oct17_data['h'].max()

    print(f"\nOct 17 actual full-day data:")
    print(f"  D_H (full day): {actual_daily_high:.4f}")
    print(f"  D_L (full day): {actual_daily_low:.4f}")

    # What does resample give us at 00:30?
    daily_resample = hist["l"].resample("1D").min()
    oct17_resample = daily_resample[daily_resample.index.date == target_day_start.date()]
    print(f"\nResample result at midnight:")
    print(f"  D_L: {oct17_resample.iloc[0] if len(oct17_resample) > 0 else 'N/A'}")

    # Check the previous day
    oct16_data = hist[(hist.index >= target_day_start - pd.Timedelta(days=1)) & 
                    (hist.index < target_day_start)]
    print(f"\nOct 16 (previous day):")
    print(f"  D_L: {oct16_data['l'].min():.4f}")
    print(f"Range: {hist.index[0]} to {hist.index[-1]}")
    
    # Find target bar
    if TARGET_TIME not in hist.index:
        print(f"\n❌ Target bar not in data!")
        return
    
    target_idx = hist.index.get_loc(TARGET_TIME)
    print(f"\n✅ Target bar found at index {target_idx}")
    
    # ========================================================================
    # METHOD 1: Build HTF levels from scratch (BACKTEST METHOD)
    # ========================================================================
    print("\n" + "="*80)
    print("METHOD 1: build_htf_levels() - BACKTEST METHOD")
    print("="*80)
    
    htf_batch = build_htf_levels(hist.copy())
    
    try:
        idx_prev = htf_batch.index.get_indexer([TARGET_TIME], method="ffill")[0]
        htf_row_batch = htf_batch.iloc[idx_prev]
        
        print(f"\nHTF snapshot from: {htf_batch.index[idx_prev]}")
        print("\nLevels (batch method):")
        for col in ["D_H", "D_L", "H4_H", "H4_L", "asia_H", "asia_L", "eu_H", "eu_L", "ny_H", "ny_L"]:
            if col in htf_row_batch.index:
                print(f"  {col:10s}: {htf_row_batch[col]:10.4f}")
    except Exception as e:
        print(f"❌ Error: {e}")
        htf_row_batch = None
    
    # ========================================================================
    # METHOD 2: Incremental updates (LIVE ENGINE METHOD)
    # ========================================================================
    print("\n" + "="*80)
    print("METHOD 2: update_htf_levels_new() - LIVE ENGINE METHOD")
    print("="*80)
    
    # Start with initial HTF levels
    warmup_bars = 15 * 96  # 15 days warmup
    htf_incremental = build_htf_levels(hist.iloc[:warmup_bars].copy())
    
    print(f"\nStarting with {len(htf_incremental)} warmup bars")
    print("Applying incremental updates...")
    
    # Update incrementally bar by bar (like live engine does)
    for i in range(warmup_bars, target_idx + 1):
        bar = hist.iloc[i]
        htf_incremental = update_htf_levels_new(htf_incremental, bar)
        
        if i % 500 == 0:
            print(f"  Updated {i}/{target_idx} bars...")
    
    try:
        idx_prev = htf_incremental.index.get_indexer([TARGET_TIME], method="ffill")[0]
        htf_row_incr = htf_incremental.iloc[idx_prev]
        
        print(f"\nHTF snapshot from: {htf_incremental.index[idx_prev]}")
        print("\nLevels (incremental method):")
        for col in ["D_H", "D_L", "H4_H", "H4_L", "asia_H", "asia_L", "eu_H", "eu_L", "ny_H", "ny_L"]:
            if col in htf_row_incr.index:
                print(f"  {col:10s}: {htf_row_incr[col]:10.4f}")
    except Exception as e:
        print(f"❌ Error: {e}")
        htf_row_incr = None
    
    # ========================================================================
    # COMPARISON: Show differences
    # ========================================================================
    if htf_row_batch is not None and htf_row_incr is not None:
        print("\n" + "="*80)
        print("COMPARISON: Batch vs Incremental")
        print("="*80)
        
        cols = ["D_H", "D_L", "H4_H", "H4_L", "asia_H", "asia_L", "eu_H", "eu_L", "ny_H", "ny_L"]
        
        print(f"\n{'Level':<12} {'Batch':<12} {'Incremental':<12} {'Diff':<12} {'Match?'}")
        print("-" * 65)
        
        max_diff = 0.0
        diffs = {}
        
        for col in cols:
            if col in htf_row_batch.index and col in htf_row_incr.index:
                batch_val = htf_row_batch[col]
                incr_val = htf_row_incr[col]
                diff = abs(batch_val - incr_val)
                match = "✅" if diff < 0.01 else "❌"
                
                print(f"{col:<12} {batch_val:<12.4f} {incr_val:<12.4f} {diff:<12.4f} {match}")
                
                diffs[col] = diff
                max_diff = max(max_diff, diff)
        
        print("\n" + "-" * 65)
        print(f"Maximum difference: {max_diff:.4f}")
        
        if max_diff > 0.01:
            print("\n⚠️  SIGNIFICANT DISCREPANCY FOUND!")
            print(f"   Levels differ by up to {max_diff:.4f}")
            print("   This COULD cause different trading decisions.")
        else:
            print("\n✅ No significant discrepancy")
            print("   HTF levels match between methods")
            
    
    # =======================================================================
    # SESSION DEBUGGING
    # =======================================================================
        # Add this after the "COMPARISON" section in your debug script:

    print("\n" + "="*80)
    print("SESSION DEBUGGING: EU Session")
    print("="*80)

    target_time = pd.Timestamp("2025-10-17 00:30:00", tz='UTC')

    # EU session: 7-16 UTC
    eu_start_hour = 7
    eu_end_hour = 16
    eu_length = 9  # hours

    # What does batch method see?
    print("\nBatch method (rolling window on masked data):")
    eu_mask = (hist.index.hour >= eu_start_hour) & (hist.index.hour <= eu_end_hour)
    eu_rolling = hist["h"].where(eu_mask).rolling("9h").max()
    batch_eu_h = eu_rolling.loc[target_time]
    print(f"  EU_H at {target_time}: {batch_eu_h:.4f}")

    # What bars contributed to this?
    lookback_9h = target_time - pd.Timedelta(hours=9)
    window_data = hist.loc[lookback_9h:target_time]
    eu_mask_window = (window_data.index.hour >= eu_start_hour) & (window_data.index.hour <= eu_end_hour)
    eu_bars_in_window = window_data[eu_mask_window]
    print(f"\n  Bars in 9h window that are in EU session:")
    if len(eu_bars_in_window) > 0:
        for idx, row in eu_bars_in_window.tail(5).iterrows():
            print(f"    {idx}: high={row.h:.4f}")
    else:
        print(f"    (none - 00:30 is outside EU session)")

    # What does incremental see?
    print("\nIncremental method:")
    current_day_start = target_time.normalize()
    cutoff_sess = max(current_day_start, target_time - pd.Timedelta(hours=9))
    print(f"  Current day start: {current_day_start}")
    print(f"  Cutoff (max of day start or 9h back): {cutoff_sess}")

    incr_window = htf_incremental.loc[cutoff_sess:target_time]
    incr_eu_mask = (incr_window.index.hour >= eu_start_hour) & (incr_window.index.hour <= eu_end_hour)
    incr_eu_bars = incr_window[incr_eu_mask]

    if 'h' in incr_eu_bars.columns and len(incr_eu_bars) > 0:
        incr_eu_h = incr_eu_bars['h'].max()
        print(f"  EU_H: {incr_eu_h:.4f}")
        print(f"\n  Bars in incremental window that are in EU session:")
        for idx, row in incr_eu_bars.tail(5).iterrows():
            print(f"    {idx}: high={row.h:.4f}")
    else:
        print(f"  (no EU bars in window - should propagate previous value)")
        print(f"  EU_H from previous bar: {htf_incremental.iloc[-2]['eu_H'] if len(htf_incremental) > 1 else 'N/A'}")

    # Check what the last EU session bar was
    print("\nLast EU session bar before 00:30:")
    eu_bars_before_target = hist[(hist.index < target_time) & eu_mask]
    if len(eu_bars_before_target) > 0:
        last_eu_bar = eu_bars_before_target.iloc[-1]
        print(f"  Time: {eu_bars_before_target.index[-1]}")
        print(f"  High: {last_eu_bar.h:.4f}")
        
        # What was EU_H at that time?
        last_eu_time = eu_bars_before_target.index[-1]
        batch_eu_at_last = eu_rolling.loc[last_eu_time]
        print(f"  Batch EU_H at that time: {batch_eu_at_last:.4f}")
        
        # Add after your current diagnostic sections:

    print("\n" + "="*80)
    print("SOLUSDT SIGNAL CHECK: 2025-10-17 01:15")
    print("="*80)

    sol_time = pd.Timestamp("2025-10-17 01:15:00", tz='UTC')

    # Load SOLUSDT data
    sol_hist = await preload_history(
        symbol="SOLUSDT",
        interval=INTERVAL,
        limit=bars_needed
    )
    sol_hist.index = sol_hist.index + pd.Timedelta(minutes=int(INTERVAL))
    sol_hist = compute_indicators(sol_hist)

    if sol_time in sol_hist.index:
        sol_bar = sol_hist.loc[sol_time]
        sol_htf_batch = build_htf_levels(sol_hist.copy())
        sol_htf_row = sol_htf_batch.loc[sol_time]
        
        print(f"\nBar data:")
        print(f"  Close: {sol_bar.c:.2f}")
        print(f"  ADX: {sol_bar.adx:.1f} (threshold: {config.ADX_HARD_FLOOR})")
                # In your SOLUSDT diagnostic, add after the ATR line:
        print(f"  StochK (k_slow): {sol_bar['k_slow']:.1f} (short needs < {config.MOMENTUM_STO_K_SHORT})")
        print(f"  StochD (d_slow): {sol_bar['d_slow']:.1f}")
        print(f"  ATR: {sol_bar.atr:.4f}")
                
        print(f"\nHTF levels:")
        for col in ["D_H","D_L","H4_H","H4_L","asia_H","asia_L","eu_H","eu_L","ny_H","ny_L"]:
            if col in sol_htf_row.index:
                print(f"  {col}: {sol_htf_row[col]:.4f}")
        
        # Check proximity
        levels = [sol_htf_row[c] for c in ["D_H","D_L","H4_H","H4_L","asia_H","asia_L","eu_H","eu_L","ny_H","ny_L"] 
                if c in sol_htf_row.index and pd.notna(sol_htf_row[c])]
        if levels:
            distances = [abs(float(sol_bar.c) - float(L)) / float(sol_bar.atr) for L in levels]
            min_dist = min(distances)
            closest = levels[distances.index(min_dist)]
            print(f"\nProximity check:")
            print(f"  Closest level: {closest:.4f}")
            print(f"  Distance: {min_dist:.2f} ATR")
            print(f"  Threshold: {config.NEAR_HTF_MAX_ATR_MOM_BACKTEST}")
            print(f"  Result: {'✅ PASS' if min_dist <= config.NEAR_HTF_MAX_ATR_MOM_BACKTEST else '❌ FAIL'}")
            
        # Add this diagnostic to check why LINKUSDT 00:30 might be rejected:

    print("\n" + "="*80)
    print("LINKUSDT 00:30 DETAILED CHECK")
    print("="*80)

    link_time = pd.Timestamp("2025-10-17 00:30:00", tz='UTC')
    if link_time in hist.index:
        link_bar = hist.loc[link_time]
        link_htf = htf_batch.loc[link_time]
        link_h1 = h1.loc[link_time.floor("1h")]
        
        print(f"\nIndicators:")
        print(f"  k_slow: {link_bar.k_slow:.1f} (short needs >= 65)")
        print(f"  ADX: {link_bar.adx:.1f} (needs >= 28)")
        
        print(f"\nHTF Proximity:")
        levels = [link_htf[c] for c in ["D_H","D_L","H4_H","H4_L","asia_H","asia_L","eu_H","eu_L","ny_H","ny_L"] 
                if c in link_htf.index and pd.notna(link_htf[c])]
        if levels and link_bar.atr > 0:
            distances = [abs(float(link_bar.c) - float(L)) / float(link_bar.atr) for L in levels]
            min_dist = min(distances)
            print(f"  Distance: {min_dist:.2f} ATR (needs <= 0.9)")
            print(f"  Result: {'✅ PASS' if min_dist <= 0.9 else '❌ FAIL'}")
        
        # Check what tjr_short_signal would return
        print(f"\nWould pass basic filters:")
        print(f"  ✅ k_slow >= 65: {link_bar.k_slow >= 65}")
        print(f"  ✅ ADX >= 28: {link_bar.adx >= 28}")
        print(f"  ✅ H1 slope < -0.05: {link_h1.slope < -0.05}")
        print(f"  ✅ Near HTF: {min_dist <= 0.9 if 'min_dist' in locals() else 'N/A'}")
        
    # ========================================================================
    # HYPOTHESIS 3: Check proximity to levels
    # ========================================================================
    print("\n" + "="*80)
    print("HYPOTHESIS 3: HTF Proximity Check")
    print("="*80)
    
    bar = hist.iloc[target_idx]
    
    if htf_row_batch is not None:
        from bot.helpers import near_htf_level
        
        print(f"\nBar close: {bar.c:.4f}")
        print(f"Bar ATR: {bar.atr:.4f}")
        
        # Test different proximity thresholds
        thresholds = [0.7, 0.9, 1.5, 2.0, 3.0]
        
        print(f"\nProximity test (batch HTF):")
        for thresh in thresholds:
            is_near = near_htf_level(bar, htf_row_batch, max_atr=thresh)
            status = "✅ PASS" if is_near else "❌ FAIL"
            print(f"  max_atr={thresh:.1f}: {status}")
        
        # Calculate actual distance
        cols = ["D_H","D_L","H4_H","H4_L","asia_H","asia_L","eu_H","eu_L","ny_H","ny_L"]
        levels = [htf_row_batch[c] for c in cols if c in htf_row_batch.index and pd.notna(htf_row_batch[c])]
        
        if levels and bar.atr > 0:
            distances = [abs(float(bar.c) - float(L)) / float(bar.atr) for L in levels]
            min_dist = min(distances)
            closest_level = levels[distances.index(min_dist)]
            
            print(f"\n  Closest level: {closest_level:.4f}")
            print(f"  Distance: {min_dist:.2f} ATR")
            print(f"  Your config: NEAR_HTF_MAX_ATR_MOM = {getattr(config, 'NEAR_HTF_MAX_ATR_MOM', 0.9)}")
    
    # ========================================================================
    # HYPOTHESIS 2: Data timing
    # ========================================================================
    print("\n" + "="*80)
    print("HYPOTHESIS 2: Data Timing Check")
    print("="*80)
    
    # Check if timestamps are aligned properly
    print(f"\nBar timestamps around target:")
    for i in range(max(0, target_idx - 2), min(len(hist), target_idx + 3)):
        bar_time = hist.index[i]
        bar_data = hist.iloc[i]
        marker = " <-- TARGET" if i == target_idx else ""
        print(f"  {bar_time}  close={bar_data.c:8.4f}{marker}")
    
    print("\n" + "="*80)
    print("SUMMARY OF FINDINGS")
    print("="*80)
    
    # Generate actionable insights
    print("\n1. HTF Level Method:")
    if max_diff > 0.01:
        print(f"   ❌ Methods produce DIFFERENT results (diff={max_diff:.4f})")
        print(f"   → This explains live/backtest discrepancy!")
    else:
        print(f"   ✅ Methods produce SAME results")
        print(f"   → HTF levels are not the issue")
    
    print("\n2. Proximity Threshold:")
    if htf_row_batch is not None:
        print(f"   Distance to nearest level: {min_dist:.2f} ATR")
        print(f"   Current config: {getattr(config, 'NEAR_HTF_MAX_ATR_MOM', 0.9)}")
        if min_dist > 1.5:
            print(f"   ❌ Too far from HTF levels - signal blocked")
            print(f"   → Increase NEAR_HTF_MAX_ATR_MOM to {min_dist + 0.5:.1f}")
        else:
            print(f"   ✅ Within reasonable distance")
    
    print("\n3. Data Timing:")
    print(f"   Bar timestamps: {'Properly aligned to CLOSE' if hist.index[1] - hist.index[0] == pd.Timedelta(minutes=15) else 'MISALIGNED'}")

if __name__ == "__main__":
    asyncio.run(main())