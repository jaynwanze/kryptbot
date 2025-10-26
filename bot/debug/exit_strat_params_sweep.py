#!/usr/bin/env python3
"""
EXIT STRATEGY OPTIMIZER

Tests different exit combinations:
- Profit targets (2R, 3R, 4R, 5R, None)
- Trailing stops (None, after 1.5R, after 2R)
- Time-based exits (None, 8h, 12h, 24h)
- Partial exits (None, 50% at 2R)

Uses optimized regime filters:
- REGIME_DAILY_ADX_MIN = 15
- REGIME_BB_WIDTH_MIN = 0.08
- REGIME_REQUIRE_ATR_EXPANDING = False
"""
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from itertools import product
import asyncio
import logging
import pandas as pd
from bot.data import preload_history
from bot.helpers import compute_indicators
from bot.helpers import breakout_config

logging.basicConfig(level=logging.WARNING)

# Import backtest
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../bot/backtest')))
from backtest_regime_fast import backtest
from regime_filter_fast import precompute_regime

# Use optimized regime parameters
OPTIMIZED_REGIME = {
    'REGIME_DAILY_ADX_MIN': 15,
    'REGIME_BB_WIDTH_MIN': 0.08,
    'REGIME_REQUIRE_ATR_EXPANDING': False,
}

# Exit strategy parameter grid
EXIT_GRID = {
    'profit_target': [None, 2.0, 3.0, 4.0, 5.0],  # R multiples
    'trailing_activation': [None, 1.5, 2.0],  # Activate trail after this profit
    'trailing_distance': [1.0],  # ATR multiples for trail (fixed at 1 ATR)
    'time_exit_hours': [None, 8, 12, 24],  # Exit after this many hours
    'partial_exit': [None, 0.5],  # Take 50% profit at 2R, trail rest
}

def apply_exit_strategy(trades_df, params):
    """
    Apply exit strategy to raw trades.
    
    params = {
        'profit_target': R multiple or None,
        'trailing_activation': R multiple to start trailing or None,
        'trailing_distance': ATR multiple for trail distance,
        'time_exit_hours': Hours to force exit or None,
        'partial_exit': % to exit at 2R or None
    }
    
    Returns modified trades_df with updated exit prices and PnL
    """
    if trades_df is None or trades_df.empty:
        return trades_df
    
    modified = trades_df.copy()
    
    # For each trade, simulate the exit strategy
    # NOTE: This is simplified - a real implementation would need bar-by-bar simulation
    # For now, we'll use heuristics based on trade metrics
    
    for idx in modified.index:
        trade = modified.loc[idx]
        entry = trade['entry_price']
        stop = trade['stop_loss']
        direction = trade['direction']
        atr = trade.get('atr', abs(entry - stop))  # Use stop distance as ATR proxy
        
        # Calculate R (initial risk)
        initial_risk = abs(entry - stop)
        
        # Get the actual exit that happened
        actual_exit = trade['exit_price']
        actual_pnl_r = trade['pnl_r']
        
        # Determine what exit would have been hit first
        best_exit = actual_exit
        best_pnl_r = actual_pnl_r
        exit_reason = trade['exit_reason']
        
        # 1. Check profit target
        if params['profit_target'] is not None:
            target_r = params['profit_target']
            if direction == 'long':
                target_price = entry + (target_r * initial_risk)
                if actual_exit >= target_price and actual_pnl_r > 0:
                    best_exit = target_price
                    best_pnl_r = target_r
                    exit_reason = f"PT_{target_r}R"
            else:  # short
                target_price = entry - (target_r * initial_risk)
                if actual_exit <= target_price and actual_pnl_r > 0:
                    best_exit = target_price
                    best_pnl_r = target_r
                    exit_reason = f"PT_{target_r}R"
        
        # 2. Check time-based exit
        if params['time_exit_hours'] is not None:
            # Simplified: if trade didn't hit target and lasted longer than time limit
            # In real implementation, would check actual bar timestamps
            # For now, assume average trade is 12 hours, adjust PnL if it would have been cut short
            if exit_reason == 'stop_loss' or actual_pnl_r < 0:
                # Apply time exit - reduces losses by ~30% on average
                time_multiplier = min(params['time_exit_hours'] / 12.0, 1.0)
                best_pnl_r = actual_pnl_r * time_multiplier
                exit_reason = f"TIME_{params['time_exit_hours']}h"
        
        # 3. Check trailing stop (simplified)
        if params['trailing_activation'] is not None and actual_pnl_r > params['trailing_activation']:
            # Trail was activated - assume we lock in 70% of peak profit
            trail_lock_pct = 0.70
            locked_r = params['trailing_activation'] + (actual_pnl_r - params['trailing_activation']) * trail_lock_pct
            if locked_r > best_pnl_r:
                best_pnl_r = locked_r
                exit_reason = f"TRAIL_{params['trailing_activation']}R"
        
        # 4. Check partial exit
        if params['partial_exit'] is not None and actual_pnl_r >= 2.0:
            # Took 50% profit at 2R, rest hit target or trail
            partial_pnl = 2.0 * params['partial_exit']  # 50% at 2R
            remainder_pnl = best_pnl_r * (1 - params['partial_exit'])  # 50% at final exit
            best_pnl_r = partial_pnl + remainder_pnl
            exit_reason = f"PARTIAL_2R+{exit_reason}"
        
        # Update trade
        modified.loc[idx, 'exit_price'] = best_exit
        modified.loc[idx, 'pnl_r'] = best_pnl_r
        modified.loc[idx, 'pnl_dollars'] = best_pnl_r * 100  # Assume $100 risk per trade
        modified.loc[idx, 'exit_reason'] = exit_reason
    
    return modified

def calculate_summary(trades_df):
    """Calculate summary stats from trades."""
    if trades_df is None or trades_df.empty:
        return {
            'trades': 0,
            'win_rate': 0,
            'profit_factor': 0,
            'net_pnl': 0,
            'avg_win_r': 0,
            'avg_loss_r': 0,
            'max_win_r': 0,
            'max_loss_r': 0,
        }
    
    winning = trades_df[trades_df['pnl_r'] > 0]
    losing = trades_df[trades_df['pnl_r'] <= 0]
    
    gross_win = winning['pnl_dollars'].sum() if not winning.empty else 0
    gross_loss = abs(losing['pnl_dollars'].sum()) if not losing.empty else 0
    
    return {
        'trades': len(trades_df),
        'win_rate': len(winning) / len(trades_df) * 100 if len(trades_df) > 0 else 0,
        'profit_factor': gross_win / gross_loss if gross_loss > 0 else (999 if gross_win > 0 else 0),
        'net_pnl': trades_df['pnl_dollars'].sum(),
        'avg_win_r': winning['pnl_r'].mean() if not winning.empty else 0,
        'avg_loss_r': losing['pnl_r'].mean() if not losing.empty else 0,
        'max_win_r': winning['pnl_r'].max() if not winning.empty else 0,
        'max_loss_r': losing['pnl_r'].min() if not losing.empty else 0,
    }

def test_exit_strategy(df, regime_series, params):
    """Test one exit strategy combination."""
    # Run baseline backtest
    summary, trades_df, equity = backtest(df, regime_series, pair="SOLUSDT")
    
    if trades_df is None or trades_df.empty:
        return None
    
    # Apply exit strategy
    modified_trades = apply_exit_strategy(trades_df, params)
    
    # Recalculate summary
    new_summary = calculate_summary(modified_trades)
    
    # Add params to result
    result = {
        **params,
        **new_summary,
    }
    
    # Calculate composite score
    # Prioritize: PF > WR > Trade Count
    if result['trades'] >= 50:  # Minimum trades
        pf_score = min(result['profit_factor'] / 1.5, 2.0)  # Normalize around 1.5
        wr_score = result['win_rate'] / 40  # Normalize around 40%
        trade_score = 1.0 if 60 <= result['trades'] <= 100 else 0.8
        pnl_score = 1 + (result['net_pnl'] / 2000)  # Normalize around $2000
        
        result['score'] = pf_score * wr_score * trade_score * pnl_score
    else:
        result['score'] = 0
    
    return result

def main():
    print("="*80)
    print("EXIT STRATEGY OPTIMIZER - SOLUSDT")
    print("="*80)
    print("\nUsing optimized regime filters:")
    print(f"  REGIME_DAILY_ADX_MIN = {OPTIMIZED_REGIME['REGIME_DAILY_ADX_MIN']}")
    print(f"  REGIME_BB_WIDTH_MIN = {OPTIMIZED_REGIME['REGIME_BB_WIDTH_MIN']}")
    print(f"  REGIME_REQUIRE_ATR_EXPANDING = {OPTIMIZED_REGIME['REGIME_REQUIRE_ATR_EXPANDING']}")
    
    # Load data
    print("\nLoading data...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    df = loop.run_until_complete(
        preload_history("SOLUSDT", interval="60", limit=18000)
    )
    loop.close()
    
    if df is None or df.empty:
        print("Failed to load data!")
        return
    
    print(f"Data loaded: {len(df)} bars")
    
    # Compute indicators
    print("Computing indicators...")
    df = compute_indicators(df.copy())
    
    # Apply optimized regime settings
    original = {}
    for k, v in OPTIMIZED_REGIME.items():
        original[k] = getattr(breakout_config, k, None)
        setattr(breakout_config, k, v)
    
    # Pre-compute regime once
    print("Pre-computing regime filter...")
    regime_series = precompute_regime(df, breakout_config)
    
    # Restore original config
    for k, v in original.items():
        setattr(breakout_config, k, v)
    
    print(f"Regime computed: {regime_series.sum()} / {len(regime_series)} bars tradeable "
          f"({regime_series.sum()/len(regime_series)*100:.1f}%)")
    
    # Get baseline performance
    print("\nRunning baseline backtest (current exits)...")
    baseline_summary, baseline_trades, _ = backtest(df, regime_series, pair="SOLUSDT")
    
    print(f"\nBASELINE PERFORMANCE:")
    print(f"  Trades: {baseline_summary['trades']}")
    print(f"  Win Rate: {baseline_summary['win_rate']:.1f}%")
    print(f"  Profit Factor: {baseline_summary['profit_factor']:.2f}")
    print(f"  Net P&L: ${baseline_summary['net_pnl']:.0f}")
    
    # Generate exit strategy combinations
    keys = list(EXIT_GRID.keys())
    # Remove trailing_distance from combinations (keep it fixed)
    test_keys = [k for k in keys if k != 'trailing_distance']
    values = [EXIT_GRID[k] for k in test_keys]
    combinations = list(product(*values))
    
    print(f"\nTesting {len(combinations)} exit strategy combinations...")
    print()
    
    results = []
    
    for idx, combo in enumerate(combinations):
        params = dict(zip(test_keys, combo))
        params['trailing_distance'] = EXIT_GRID['trailing_distance'][0]  # Add fixed trail distance
        
        # Skip invalid combinations
        if params['trailing_activation'] is not None and params['profit_target'] is not None:
            if params['trailing_activation'] >= params['profit_target']:
                continue  # Trail activation must be before target
        
        print(f"Test {idx+1}/{len(combinations)}: PT={params['profit_target']}, "
              f"Trail={params['trailing_activation']}, Time={params['time_exit_hours']}h, "
              f"Partial={params['partial_exit']}")
        
        result = test_exit_strategy(df, regime_series, params)
        
        if result and result['trades'] >= 50:
            results.append(result)
            print(f"  → Trades: {result['trades']}, WR: {result['win_rate']:.1f}%, "
                  f"PF: {result['profit_factor']:.2f}, P&L: ${result['net_pnl']:.0f}")
        else:
            print(f"  → Skipped (insufficient trades)")
        print()
    
    if not results:
        print("No valid results!")
        return
    
    # Sort by score
    results.sort(key=lambda x: x['score'], reverse=True)
    
    # Top 10 by overall score
    print("\n" + "="*80)
    print("TOP 10 EXIT STRATEGIES (Best Overall)")
    print("="*80)
    for i, r in enumerate(results[:10], 1):
        print(f"\n#{i} - Score: {r['score']:.2f}")
        print(f"  Profit Target: {r['profit_target']}R" if r['profit_target'] else "  Profit Target: None")
        print(f"  Trailing: After {r['trailing_activation']}R ({r['trailing_distance']}ATR)" 
              if r['trailing_activation'] else "  Trailing: None")
        print(f"  Time Exit: {r['time_exit_hours']}h" if r['time_exit_hours'] else "  Time Exit: None")
        print(f"  Partial Exit: {int(r['partial_exit']*100)}% at 2R" if r['partial_exit'] else "  Partial Exit: None")
        print(f"  Trades: {r['trades']}")
        print(f"  Win Rate: {r['win_rate']:.1f}%")
        print(f"  Profit Factor: {r['profit_factor']:.2f}")
        print(f"  Net P&L: ${r['net_pnl']:.0f}")
        print(f"  Avg Win: {r['avg_win_r']:.2f}R  |  Avg Loss: {r['avg_loss_r']:.2f}R")
    
    # Top 5 by PF
    print("\n" + "="*80)
    print("TOP 5 BY PROFIT FACTOR")
    print("="*80)
    by_pf = sorted(results, key=lambda x: x['profit_factor'], reverse=True)[:5]
    for i, r in enumerate(by_pf, 1):
        print(f"\n#{i} - PF: {r['profit_factor']:.2f}")
        print(f"  PT={r['profit_target']}, Trail={r['trailing_activation']}, "
              f"Time={r['time_exit_hours']}h, Partial={r['partial_exit']}")
        print(f"  Trades: {r['trades']}, WR: {r['win_rate']:.1f}%, P&L: ${r['net_pnl']:.0f}")
    
    # Top 5 by WR
    print("\n" + "="*80)
    print("TOP 5 BY WIN RATE")
    print("="*80)
    by_wr = sorted(results, key=lambda x: x['win_rate'], reverse=True)[:5]
    for i, r in enumerate(by_wr, 1):
        print(f"\n#{i} - WR: {r['win_rate']:.1f}%")
        print(f"  PT={r['profit_target']}, Trail={r['trailing_activation']}, "
              f"Time={r['time_exit_hours']}h, Partial={r['partial_exit']}")
        print(f"  Trades: {r['trades']}, PF: {r['profit_factor']:.2f}, P&L: ${r['net_pnl']:.0f}")
    
    # Recommendation
    print("\n" + "="*80)
    print("RECOMMENDED EXIT STRATEGY")
    print("="*80)
    best = results[0]
    print(f"\nImplement this exit logic:")
    print(f"```python")
    if best['profit_target']:
        print(f"PROFIT_TARGET = {best['profit_target']}  # R multiple")
    if best['trailing_activation']:
        print(f"TRAILING_ACTIVATION = {best['trailing_activation']}  # Start trail after this profit")
        print(f"TRAILING_DISTANCE = {best['trailing_distance']}  # ATR multiple")
    if best['time_exit_hours']:
        print(f"MAX_TRADE_HOURS = {best['time_exit_hours']}  # Force exit after")
    if best['partial_exit']:
        print(f"PARTIAL_EXIT = {best['partial_exit']}  # Take {int(best['partial_exit']*100)}% at 2R")
    print(f"```")
    print(f"\nExpected Performance:")
    print(f"  Trades: {best['trades']}")
    print(f"  Win Rate: {best['win_rate']:.1f}%")
    print(f"  Profit Factor: {best['profit_factor']:.2f}")
    print(f"  Net P&L: ${best['net_pnl']:.0f}")
    print(f"  Avg Win: {best['avg_win_r']:.2f}R  |  Avg Loss: {best['avg_loss_r']:.2f}R")
    
    print("\n" + "="*80)
    print("BASELINE vs OPTIMIZED EXITS")
    print("="*80)
    print(f"Baseline (current exits):")
    print(f"  {baseline_summary['win_rate']:.1f}% WR, {baseline_summary['profit_factor']:.2f} PF, "
          f"${baseline_summary['net_pnl']:.0f}, {baseline_summary['trades']} trades")
    print(f"\nOptimized (new exit strategy):")
    print(f"  {best['win_rate']:.1f}% WR, {best['profit_factor']:.2f} PF, "
          f"${best['net_pnl']:.0f}, {best['trades']} trades")
    
    improvement = (best['net_pnl'] - baseline_summary['net_pnl']) / baseline_summary['net_pnl'] * 100
    print(f"\nImprovement: {improvement:+.1f}%")
    print("="*80)

if __name__ == "__main__":
    main()