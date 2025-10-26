#!/usr/bin/env python3
"""
REGIME FILTER PARAMETER OPTIMIZER

Tests different combinations of:
- Daily ADX threshold (15, 20, 25, 30)
- BB width threshold (0.04, 0.06, 0.08, 0.10)
- ATR expansion (True/False)

Finds optimal balance of:
- High win rate
- Good profit factor
- Reasonable trade frequency (100-200 trades)
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
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from bot.backtest.breakout_pair_backtest import backtest
from bot.helpers.regieme.regieme_filter import precompute_regime

# Parameter grid
PARAM_GRID = {
    'REGIME_DAILY_ADX_MIN': [15, 20, 25, 30],
    'REGIME_BB_WIDTH_MIN': [0.04, 0.06, 0.08, 0.10],
    'REGIME_REQUIRE_ATR_EXPANDING': [False, True],
}

def test_params(df, params):
    """Test one parameter combination."""
    # Temporarily set params
    original = {}
    for k, v in params.items():
        original[k] = getattr(breakout_config, k, None)
        setattr(breakout_config, k, v)
    
    try:
        # Pre-compute regime with these params
        regime_series = precompute_regime(df, breakout_config)
        
        # Run backtest
        summary, trades, _ = backtest(df, regime_series, pair="SOLUSDT")
        
        # Calculate tradeable %
        tradeable_pct = regime_series.sum() / len(regime_series) * 100
        
        result = {
            **params,
            'trades': summary['trades'],
            'win_rate': summary['win_rate'],
            'profit_factor': summary['profit_factor'],
            'net_pnl': summary['net_pnl'],
            'tradeable_pct': tradeable_pct,
        }
        
        # Score: prioritize profitability + reasonable trade count
        # Good systems have 100-200 trades, 38%+ WR, 1.3+ PF
        trade_score = 1.0 if 100 <= result['trades'] <= 200 else 0.5
        wr_score = result['win_rate'] / 40  # Normalize around 40%
        pf_score = min(result['profit_factor'] / 1.3, 2.0)  # Normalize around 1.3
        
        result['score'] = trade_score * wr_score * pf_score * (1 + result['net_pnl']/1000)
        
        return result
        
    finally:
        # Restore original params
        for k, v in original.items():
            setattr(breakout_config, k, v)

def main():
    print("="*80)
    print("REGIME FILTER PARAMETER OPTIMIZER")
    print("="*80)
    
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
    
    # Generate combinations
    keys = list(PARAM_GRID.keys())
    values = [PARAM_GRID[k] for k in keys]
    combinations = list(product(*values))
    
    print(f"\nTesting {len(combinations)} parameter combinations...")
    print()
    
    results = []
    
    for idx, combo in enumerate(combinations):
        params = dict(zip(keys, combo))
        
        print(f"Test {idx+1}/{len(combinations)}: ADX={params['REGIME_DAILY_ADX_MIN']}, "
              f"BB={params['REGIME_BB_WIDTH_MIN']:.2f}, ATR_EXP={params['REGIME_REQUIRE_ATR_EXPANDING']}")
        
        result = test_params(df, params)
        
        if result['trades'] >= 20:  # Minimum 20 trades
            results.append(result)
            print(f"  → Trades: {result['trades']}, WR: {result['win_rate']:.1f}%, "
                  f"PF: {result['profit_factor']:.2f}, P&L: ${result['net_pnl']:.0f}, "
                  f"Tradeable: {result['tradeable_pct']:.1f}%")
        else:
            print(f"  → Skipped (only {result['trades']} trades)")
        print()
    
    if not results:
        print("No valid results!")
        return
    
    # Sort by score
    results.sort(key=lambda x: x['score'], reverse=True)
    
    # Top 5 by overall score
    print("\n" + "="*80)
    print("TOP 5 CONFIGURATIONS (Best Overall)")
    print("="*80)
    for i, r in enumerate(results[:5], 1):
        print(f"\n#{i} - Score: {r['score']:.2f}")
        print(f"  ADX Min: {r['REGIME_DAILY_ADX_MIN']}")
        print(f"  BB Width Min: {r['REGIME_BB_WIDTH_MIN']:.2f}")
        print(f"  ATR Expansion: {r['REGIME_REQUIRE_ATR_EXPANDING']}")
        print(f"  Trades: {r['trades']}")
        print(f"  Win Rate: {r['win_rate']:.1f}%")
        print(f"  Profit Factor: {r['profit_factor']:.2f}")
        print(f"  Net P&L: ${r['net_pnl']:.0f}")
        print(f"  Tradeable: {r['tradeable_pct']:.1f}%")
    
    # Top 5 by profit
    print("\n" + "="*80)
    print("TOP 5 BY NET P&L")
    print("="*80)
    by_pnl = sorted(results, key=lambda x: x['net_pnl'], reverse=True)[:5]
    for i, r in enumerate(by_pnl, 1):
        print(f"\n#{i} - P&L: ${r['net_pnl']:.0f}")
        print(f"  ADX: {r['REGIME_DAILY_ADX_MIN']}, BB: {r['REGIME_BB_WIDTH_MIN']:.2f}, "
              f"ATR_EXP: {r['REGIME_REQUIRE_ATR_EXPANDING']}")
        print(f"  Trades: {r['trades']}, WR: {r['win_rate']:.1f}%, PF: {r['profit_factor']:.2f}")
    
    # Top 5 by win rate
    print("\n" + "="*80)
    print("TOP 5 BY WIN RATE")
    print("="*80)
    by_wr = sorted(results, key=lambda x: x['win_rate'], reverse=True)[:5]
    for i, r in enumerate(by_wr, 1):
        print(f"\n#{i} - WR: {r['win_rate']:.1f}%")
        print(f"  ADX: {r['REGIME_DAILY_ADX_MIN']}, BB: {r['REGIME_BB_WIDTH_MIN']:.2f}, "
              f"ATR_EXP: {r['REGIME_REQUIRE_ATR_EXPANDING']}")
        print(f"  Trades: {r['trades']}, PF: {r['profit_factor']:.2f}, P&L: ${r['net_pnl']:.0f}")
    
    # Recommendation
    print("\n" + "="*80)
    print("RECOMMENDED CONFIGURATION")
    print("="*80)
    best = results[0]
    print(f"\nUse these settings in breakout_config.py:")
    print(f"```python")
    print(f"REGIME_DAILY_ADX_MIN = {best['REGIME_DAILY_ADX_MIN']}")
    print(f"REGIME_BB_WIDTH_MIN = {best['REGIME_BB_WIDTH_MIN']}")
    print(f"REGIME_REQUIRE_ATR_EXPANDING = {best['REGIME_REQUIRE_ATR_EXPANDING']}")
    print(f"```")
    print(f"\nExpected Performance:")
    print(f"  Trades: {best['trades']}")
    print(f"  Win Rate: {best['win_rate']:.1f}%")
    print(f"  Profit Factor: {best['profit_factor']:.2f}")
    print(f"  Net P&L: ${best['net_pnl']:.0f}")
    print(f"  Tradeable Time: {best['tradeable_pct']:.1f}%")
    
    print("\n" + "="*80)
    print("CURRENT vs OPTIMIZED")
    print("="*80)
    print(f"Current (ADX=25, BB=0.08, ATR_EXP=True):")
    print(f"  35.8% WR, 1.30 PF, $1,351, 67 trades (13% tradeable)")
    print(f"\nOptimized (ADX={best['REGIME_DAILY_ADX_MIN']}, BB={best['REGIME_BB_WIDTH_MIN']}, "
          f"ATR_EXP={best['REGIME_REQUIRE_ATR_EXPANDING']}):")
    print(f"  {best['win_rate']:.1f}% WR, {best['profit_factor']:.2f} PF, "
          f"${best['net_pnl']:.0f}, {best['trades']} trades ({best['tradeable_pct']:.1f}% tradeable)")
    print("="*80)

if __name__ == "__main__":
    main()