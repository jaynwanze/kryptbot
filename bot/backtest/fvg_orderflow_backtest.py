#!/usr/bin/env python3
"""
FVG ORDER FLOW BACKTEST - 15M TIMEFRAME

Tests if the FVG Order Flow model performs better on 15-minute timeframe.
- Order flow is more visible on faster timeframes
- 10-bar lookback = 2.5 hours (vs 10 hours on 1H)
- Volume Profile calculation has better granularity
- FVGs are more frequent and actionable
- Momentum signals are more responsive

ADJUSTED FOR 15M:
- Shorter lookback (5 bars instead of 10)
- Smaller VP period (48 bars = 12 hours instead of 24)
- Tighter stops (0.5% instead of 1%)
- Faster targets (1.5R and 2.5R instead of 3R)
- Lower OF score threshold (30 instead of 40)
"""
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
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
    compute_indicators,
    round_price,
    align_by_close,
    fees_usd,
    detect_fvg_bearish,
    detect_fvg_bullish,
    fvg_long_signal,
    fvg_short_signal,
    calculate_volume_profile,
    in_good_hours
)
from bot.helpers.config import fvg_orderflow_config as CONFIG
from bot.infra.models import FvgOrderFlowPosition


logging.basicConfig(level=logging.INFO)


# Session hours
def _union_hours(sw: dict[str, tuple[int, int]]) -> set[int]:
    s = set()
    for a, b in sw.values():
        s |= set(range(a, b))
    return s

SESSION_WINDOWS = getattr(CONFIG, "SESSION_WINDOWS", {
    "ASIA": (0, 8),
    "LONDON": (8, 16),
    "NY": (13, 22),
})
GOOD_HOURS = _union_hours(SESSION_WINDOWS)
SESSION_GATING = len(GOOD_HOURS) < 24

# ═══════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def backtest(df, equity0, risk_pct, pair):
    """Run 15m FVG Order Flow backtest with refactored Signal/Position structure"""
    equity = equity0
    pos: Optional[FvgOrderFlowPosition] = None
    trades = []
    curve = []

    fvgs = []
    signals_checked = 0
    phase3_pass = 0

    for i in range(CONFIG.VP_PERIOD, len(df)):
        bar = df.iloc[i]

        # 3) Session gate (24/7 for crypto usually)
        if SESSION_GATING and not in_good_hours(
            bar.name, good_hours=GOOD_HOURS
        ):
            continue

        # Update FVG list
        bull_fvg = detect_fvg_bullish(df, i)
        if bull_fvg:
            fvgs.append(bull_fvg)

        bear_fvg = detect_fvg_bearish(df, i)
        if bear_fvg:
            fvgs.append(bear_fvg)

        # Keep only recent FVGs (within 24 hours)
        fvgs = [f for f in fvgs if (bar.name - f.timestamp).total_seconds() / 3600 < 24]

        # Calculate Volume Profile
        vp = calculate_volume_profile(df.iloc[: i + 1], CONFIG.VP_PERIOD)
        if vp is None:
            continue

        # ═══════════════════════════════════════════════════════════════
        # EXIT MANAGEMENT
        # ═══════════════════════════════════════════════════════════════
        if pos is not None:
            signal = pos.signal
            is_long = signal.side == "Buy"

            # Check exit conditions
            hit_sl = (is_long and bar.l <= signal.sl) or (not is_long and bar.h >= signal.sl)
            hit_tp1 = (is_long and bar.h >= signal.tp1) or (not is_long and bar.l <= signal.tp1)
            hit_tp2 = (is_long and bar.h >= signal.tp2) or (not is_long and bar.l <= signal.tp2)

            reason = None
            exit_px = None

            if hit_sl:
                reason = "SL"
                exit_px = signal.sl
            elif hit_tp2:
                reason = "TP2"
                exit_px = signal.tp2
            elif hit_tp1:
                reason = "TP1"
                exit_px = signal.tp1

            if reason:
                # Calculate P&L
                direction = 1 if is_long else -1
                pnl_gross = direction * pos.qty * (exit_px - signal.entry)
                fee = fees_usd(signal.entry, exit_px, pos.qty, CONFIG.FEE_BPS)
                pnl = pnl_gross - fee

                equity += pnl

                # Record trade
                trades.append({
                    'pair': pair,
                    'dir': signal.side,
                    't_entry': signal.ts,
                    't_exit': bar.name,
                    'entry': signal.entry,
                    'sl': signal.sl,
                    'tp1': signal.tp1,
                    'tp2': signal.tp2,
                    'exit': exit_px,
                    'reason': reason,
                    'pnl': pnl,
                    'equity': equity,
                    'of_score': signal.of_score,
                    'level_type': signal.level_type,
                    'narrative': signal.narrative,
                    'adx': signal.adx,
                    'key': signal.key
                })

                logging.info(
                    "[%s] %s %s | entry %.2f sl %.2f tp1 %.2f tp2 %.2f | exit %.2f | OF %.0f | pnl $%.2f | eq $%.2f",
                    pair, signal.side, reason,
                    signal.entry, signal.sl, signal.tp1, signal.tp2, exit_px,
                    signal.of_score, pnl, equity
                )

                pos = None

        # ═══════════════════════════════════════════════════════════════
        # ENTRY LOGIC
        # ═══════════════════════════════════════════════════════════════
        if pos is None:
            signals_checked += 1

            # Try LONG
            long_signal = fvg_long_signal(df, i, fvgs, vp)
            if long_signal:
                risk_usd = equity0 * risk_pct
                stop_off = long_signal.off_sl
                qty = risk_usd / stop_off if stop_off > 0 else 0

                if qty > 0:
                    pos = FvgOrderFlowPosition(
                        signal=long_signal,
                        order_id=f"backtest_{long_signal.key}",
                        qty=qty,
                        meta={'equity_at_entry': equity, 'risk_usd': risk_usd}
                    )

                    phase3_pass += 1
                    logging.info(
                        "[%s] FVG LONG @ %s | entry %.2f sl %.2f tp1 %.2f tp2 %.2f | OF %.0f | %s",
                        pair, bar.name,
                        long_signal.entry, long_signal.sl, long_signal.tp1, long_signal.tp2,
                        long_signal.of_score, long_signal.narrative
                    )

            # Try SHORT
            else:
                short_signal = fvg_short_signal(df, i, fvgs, vp)
                if short_signal:
                    risk_usd = equity0 * risk_pct
                    stop_off = short_signal.off_sl
                    qty = risk_usd / stop_off if stop_off > 0 else 0

                    if qty > 0:
                        pos = FvgOrderFlowPosition(
                            signal=short_signal,
                            order_id=f"backtest_{short_signal.key}",
                            qty=qty,
                            meta={'equity_at_entry': equity, 'risk_usd': risk_usd}
                        )

                        phase3_pass += 1

                        logging.info(
                            "[%s] FVG SHORT @ %s | entry %.2f sl %.2f tp1 %.2f tp2 %.2f | OF %.0f | %s",
                            pair, bar.name,
                            short_signal.entry, short_signal.sl, short_signal.tp1, short_signal.tp2,
                            short_signal.of_score, short_signal.narrative
                        )

        curve.append(equity)

    # ═══════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = wins + losses
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    pf = float("inf") if gl == 0 else (gp / gl if gl > 0 else 0)

    summary = {
        'pair': pair,
        'timeframe': "15m",
        'trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': (wins / total * 100) if total else 0.0,
        'profit_factor': pf,
        'equity_final': equity,
        'net_pnl': equity - equity0,
        'avg_win': gp / wins if wins else 0,
        'avg_loss': gl / losses if losses else 0,
        'min_of_score': CONFIG.MIN_OF_SCORE,
        'of_multiplier': CONFIG.OF_MULTIPLIER,
        'of_lookback': CONFIG.OF_LOOKBACK,
        'risk_pct': risk_pct * 100,
        'signals_checked': signals_checked,
        'phase3_pass': phase3_pass,
        'signal_rate': (phase3_pass / signals_checked * 100) if signals_checked else 0,
    }

    return summary, trades, pd.Series(curve, index=df.index[: len(curve)])



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    # Config
    pair = "DOGEUSDT"
    interval = "15"  # 15-minute timeframe
    equity = 1000.0
    risk_pct = CONFIG.RISK_PCT

    logging.info("=" * 80)
    logging.info("FVG ORDER FLOW BACKTEST - 15M TIMEFRAME")
    logging.info("Testing if faster timeframe improves Order Flow edge")
    logging.info(
        "Pair: %s | TF: %sm | Starting Equity: $%.2f | Risk: %.1f%%",
        pair,
        interval,
        equity,
        risk_pct * 100,
    )
    logging.info(
        "OF Lookback: %d bars | OF Multiplier: %.1fx | Min Score: %.0f",
        CONFIG.OF_LOOKBACK,
        CONFIG.OF_MULTIPLIER,
        CONFIG.MIN_OF_SCORE,
    )
    logging.info("=" * 80)

    # Load data (30 days of 15m = ~2880 bars)
    days_back = 30
    bars_needed = days_back * 96

    hist = asyncio.run(
        preload_history(symbol=pair, interval=interval, limit=bars_needed)
    )

    hist = align_by_close(hist, int(interval))
    hist = compute_indicators(hist)

    logging.info(
        "Data loaded: %d bars (%s to %s)", len(hist), hist.index[0], hist.index[-1]
    )

    # Run backtest
    summary, trades, curve = backtest(
        hist, equity0=equity, risk_pct=risk_pct, pair=pair
    )

    # Display results
    print("\n" + "=" * 80)
    print("BACKTEST SUMMARY")
    print("=" * 80)
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"{k:.<35} {v:>12.2f}")
        else:
            print(f"{k:.<35} {v:>12}")
    print("=" * 80)

    # Trade table
    if trades:
        print(f"\n{'='*100}")
        print(
            f"{'Entry Time':<20} {'Dir':<6} {'Entry':<8} {'Exit':<8} {'Reason':<6} {'OF':<5} {'PnL':<10} {'Equity':<10}"
        )
        print(f"{'='*100}")
        for t in trades:
            print(
                f"{str(t['t_entry']):<20} {t['dir']:<6} "
                f"{t['entry']:<8.2f} {t['exit']:<8.2f} {t['reason']:<6} "
                f"{t['of_score']:<5.0f} ${t['pnl']:>8.2f} ${t['equity']:>9.2f}"
            )
        print(f"{'='*100}\n")

        # Save
        df_trades = pd.DataFrame(trades)
        output_file = f"backtest_fvg_15m_{pair}_{datetime.now():%Y%m%d_%H%M}.csv"
        df_trades.to_csv(output_file, index=False)
        logging.info("Trades saved to %s", output_file)

    print("\n" + "=" * 80)
    print(f"  Trades:        {summary['trades']} in {days_back} days")
    print(f"  Win Rate:      {summary['win_rate']:.1f}%")
    print(f"  Signal Rate:   {summary['signal_rate']:.2f}%")
    print(f"  Final Equity:  ${summary['equity_final']:.2f}")
    print("\n" + "=" * 80)
