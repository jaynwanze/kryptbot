#!/usr/bin/env python3
"""
FAST BREAKOUT BACKTEST WITH REGIME FILTER

Pre-computes regime filter ONCE instead of per-bar.
Should complete in ~30 seconds instead of hanging forever.
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
from bot.data import preload_history
from bot.helpers import breakout_config, compute_indicators, round_price, fees_usd
from bot.helpers import build_htf_levels, update_htf_levels_new
from bot.helpers.regieme.regieme_filter import precompute_regime, regime_gate_fast
from bot.helpers.signals.breakout_signals import breakout_long_signal, breakout_short_signal

logging.basicConfig(level=logging.INFO)

@dataclass
class Position:
    dir: int
    entry: float
    sl: float
    tp: float
    qty: float
    time_entry: pd.Timestamp
    adx_entry: float = 0.0

def backtest(df: pd.DataFrame,
             regime_series: pd.Series,
             equity0: float = 1000.0,
             risk_pct: float = 0.10,
             pair: str = "SOLUSDT"):
    """
    Fast backtest with pre-computed regime filter.
    """
    max_daily = getattr(breakout_config, "MAX_TRADES_PER_DAY", 5)
    rr_target = getattr(breakout_config, "RR_TARGET", 2.5)
    cooldown_sl_sec = 0.1 * 86400
    cooldown_tp_sec = 0.05 * 86400
    regime_enabled = getattr(breakout_config, "REGIME_FILTER_ENABLED", True)
    
    equity = equity0
    pos: Optional[Position] = None
    trades = []
    curve = []
    
    last_sl_ts = 0.0
    last_tp_ts = 0.0
    daily_count = defaultdict(int)
    
    htf_levels = build_htf_levels(df.copy())
    
    drop_stats = {
        "total_bars": 0,
        "regime_choppy": 0,
        "veto_adx": 0,
        "veto_atr": 0,
        "cooldown": 0,
        "no_signal": 0,
        "has_open": 0,
    }
    
    test_start_idx = 30
    
    logging.info(f"Starting backtest: {len(df) - test_start_idx} bars")
    
    for i in range(test_start_idx, len(df)):
        bar = df.iloc[i]
        drop_stats["total_bars"] += 1
        
        if i % 1000 == 0:
            logging.info(f"Progress: {i}/{len(df)} bars ({i/len(df)*100:.1f}%)")
        
        # Warmup
        if bar[["atr", "adx"]].isna().any():
            curve.append(equity)
            continue
        
        # REGIME FILTER (FAST - just a lookup!)
        if regime_enabled and not regime_gate_fast(bar.name, regime_series):
            drop_stats["regime_choppy"] += 1
            curve.append(equity)
            continue
        
        # Manage position
        if pos is not None:
            drop_stats["has_open"] += 1
            
            if pos.dir == 1:
                hit_sl = bar.l <= pos.sl
                hit_tp = bar.h >= pos.tp
            else:
                hit_sl = bar.h >= pos.sl
                hit_tp = bar.l <= pos.tp
            
            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                exit_px = pos.sl if hit_sl else pos.tp
                pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                fee = fees_usd(pos.entry, exit_px, pos.qty, 10)
                pnl = pnl_gross - fee
                
                equity += pnl
                
                bar_ts = bar.name.timestamp()
                if reason == "SL":
                    last_sl_ts = bar_ts
                else:
                    last_tp_ts = bar_ts
                
                trades.append({
                    'dir': "LONG" if pos.dir == 1 else "SHORT",
                    't_entry': pos.time_entry,
                    't_exit': bar.name,
                    'entry': pos.entry,
                    'sl': pos.sl,
                    'tp': pos.tp,
                    'exit': exit_px,
                    'reason': reason,
                    'pnl': pnl,
                    'equity': equity,
                    'adx': pos.adx_entry
                })
                
                pos = None
            
            curve.append(equity)
            continue
        
        # Look for entry
        try:
            idx = htf_levels.index.get_indexer([bar.name], method="ffill")[0]
            if idx == -1:
                curve.append(equity)
                continue
            htf_row = htf_levels.iloc[idx]
        except:
            curve.append(equity)
            continue
        
        # Market quality
        if bar.adx < breakout_config.ADX_HARD_FLOOR:
            drop_stats["veto_adx"] += 1
            curve.append(equity)
            continue
        
        atr_ratio = bar.atr / bar.atr30 if bar.atr30 > 0 else 0
        if atr_ratio < breakout_config.MIN_ATR_RATIO:
            drop_stats["veto_atr"] += 1
            curve.append(equity)
            continue
        
        # Cooldown
        bar_ts = bar.name.timestamp()
        if (last_sl_ts and (bar_ts - last_sl_ts) < cooldown_sl_sec) or \
           (last_tp_ts and (bar_ts - last_tp_ts) < cooldown_tp_sec):
            drop_stats["cooldown"] += 1
            curve.append(equity)
            continue
        
        # Daily cap
        day_key = bar.name.date().isoformat()
        if daily_count[day_key] >= max_daily:
            curve.append(equity)
            continue
        
        # SL/TP calc
        stop_off = 1.3 * 1.2 * bar.atr + 0.25 * bar.atr
        tp_dist = rr_target * stop_off
        
        # Signals
        long_signal, _ = breakout_long_signal(df, i, htf_row)
        short_signal, _ = breakout_short_signal(df, i, htf_row)
        
        if long_signal:
            entry = float(bar.c) * 1.00002
            risk_usd = equity0 * risk_pct
            qty = risk_usd / stop_off
            sl = round_price(entry - stop_off, pair)
            tp = round_price(entry + tp_dist, pair)
            
            pos = Position(
                dir=1, entry=entry, sl=sl, tp=tp, qty=qty,
                time_entry=bar.name, adx_entry=float(bar.adx)
            )
            daily_count[day_key] += 1
            
            logging.info(f"[{pair}] LONG @ {bar.name} | {entry:.4f}")
        
        elif short_signal:
            entry = float(bar.c) * 0.99998
            risk_usd = equity0 * risk_pct
            qty = risk_usd / stop_off
            sl = round_price(entry + stop_off, pair)
            tp = round_price(entry - tp_dist, pair)
            
            pos = Position(
                dir=-1, entry=entry, sl=sl, tp=tp, qty=qty,
                time_entry=bar.name, adx_entry=float(bar.adx)
            )
            daily_count[day_key] += 1
            
            logging.info(f"[{pair}] SHORT @ {bar.name} | {entry:.4f}")
        
        else:
            drop_stats["no_signal"] += 1
        
        curve.append(equity)
    
    # Summary
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total = len(trades)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    pf = (gp / gl) if gl > 0 else float('inf')
    
    summary = {
        'pair': pair,
        'trades': total,
        'wins': wins,
        'losses': total - wins,
        'win_rate': (wins / total * 100) if total else 0,
        'profit_factor': pf,
        'equity_final': equity,
        'net_pnl': equity - equity0,
        'avg_win': gp / wins if wins else 0,
        'avg_loss': gl / (total - wins) if (total - wins) else 0,
    }
    
    # Print stats
    print("\n" + "="*80)
    print("FILTER ANALYSIS")
    print("="*80)
    for k, v in drop_stats.items():
        pct = (v / drop_stats["total_bars"] * 100) if drop_stats["total_bars"] else 0
        print(f"{k:20s} {v:6d} ({pct:5.1f}%)")
    print("="*80)
    
    return summary, trades, curve


if __name__ == "__main__":
    pair = "ETHUSDT"
    
    print("="*80)
    print("FAST BREAKOUT BACKTEST WITH REGIME FILTER")
    print("="*80)
    
    # Load data
    print("\nLoading data...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    df = loop.run_until_complete(
        preload_history(pair, interval="60", limit=3000)
    )
    loop.close()
    
    if df is None or df.empty:
        print("Failed to load data!")
        sys.exit(1)
    
    print(f"Data loaded: {len(df)} bars ({df.index[0]} to {df.index[-1]})")
    
    # Compute indicators
    print("Computing indicators...")
    df = compute_indicators(df.copy())
    
    # PRE-COMPUTE REGIME (ONCE!)
    print("\nPre-computing regime filter...")
    regime_series = precompute_regime(df, breakout_config)
    
    # Run backtest
    print("\nRunning backtest...")
    summary, trades, curve = backtest(df, regime_series, pair=pair)
    
    # Results
    print("\n" + "="*80)
    print("BACKTEST SUMMARY")
    print("="*80)
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"{k:30s} {v:12.2f}")
        else:
            print(f"{k:30s} {v:12}")
    print("="*80)
    
    # Comparison
    print("\n" + "="*80)
    print("vs BASELINE (no regime filter)")
    print("="*80)
    print("Baseline: 29.9% WR, 0.96 PF, -$942")
    print(f"Current:  {summary['win_rate']:.1f}% WR, {summary['profit_factor']:.2f} PF, ${summary['net_pnl']:.0f}")
    
    if summary['win_rate'] > 40 and summary['profit_factor'] > 1.3:
        print("\n✅ REGIME FILTER WORKS!")
    elif summary['win_rate'] > 35:
        print("\n⚠️  MARGINAL IMPROVEMENT - Try tuning")
    else:
        print("\n❌ NEEDS WORK - Try different approach")
    
    print("="*80)
    
    # Save
    if trades:
        trades_df = pd.DataFrame(trades)
        filename = f"backtest_regime_fast_{pair}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        trades_df.to_csv(filename, index=False)
        print(f"\nTrades saved to {filename}")