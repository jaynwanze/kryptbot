#!/usr/bin/env python3
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
from bot.helpers import (
    ema_config, compute_indicators, build_htf_levels, update_htf_levels_new,
    ema_short_signal, ema_long_signal, round_price, align_by_close,
    fees_usd, build_h1, update_h1, near_htf_level, in_good_hours, veto_thresholds,
    precompute_regime, regime_gate_fast, resample_to_htf, compute_htf_indicators
)

# --- Execution realism ---
STOP_FIRST = True
EXEC_DELAY_SEC = 8

# --- Trailing stop config (matches live engine) ---
BE_ARM_R = 0.8  # Move to BE after +0.8R
ATR_TRAIL_K = 0.9  # Trail distance = 0.9 * ATR

@dataclass
class Position:
    dir: int  # +1 long, -1 short
    entry: float
    sl: float
    tp: float
    qty: float
    risk: float
    time_entry: pd.Timestamp
    time_close: Optional[pd.Timestamp] = None
    adx_entry: float = 0.0
    k_entry: float = 0.0
    be_armed: bool = False  # Track if moved to BE
    trailing: bool = False  # Track if trailing active

def backtest(df: pd.DataFrame,
            regime_series: pd.Series,
             equity0: float = 1000.0,
             risk_pct: float = 0.1,
             pair: str = "LINKUSDT"):
    """
    Backtest aligned with live engine (lrs_pair_engine.py).
    Applies ALL gates: HTF proximity, session, veto, cooldown, daily cap, etc.
    """
    regime_enabled = getattr(ema_config, "REGIME_FILTER_ENABLED", True)

    # Warm-up guard
    required_warmup_days = getattr(ema_config, "HTF_DAYS", 15) + 5  # +5 for safety
    warmup_bars = required_warmup_days * 96  # 96 bars/day for 15m

    if len(df) < warmup_bars:
        logging.warning(
            "Insufficient history! Have %d bars, need %d for HTF warmup",
            len(df), warmup_bars
        )

    # # 1) Compute indicators
    # df = compute_indicators(df.copy())

    # 2) Build HTF levels & H1 trend
    htf_levels = build_htf_levels(df.copy())
    h1 = build_h1(df.copy())

    # 3) Session hours (from config)
    session_windows = getattr(ema_config, "SESSION_WINDOWS", {})
    good_hours = set()
    for h0, h1_end in session_windows.values():
        good_hours.update(range(h0, h1_end))
    session_gating = len(good_hours) < 24

    equity = equity0
    pos: Optional[Position] = None
    trades: List[Dict] = []
    curve: List[float] = []

    # Cooldown tracking (per-pair, but we're single-pair here)
    last_sl_ts = 0.0
    last_tp_ts = 0.0
    cooldown_sl_sec = getattr(ema_config, "COOLDOWN_DAYS_AFTER_SL", 0.5) * 86400
    cooldown_tp_sec = getattr(ema_config, "COOLDOWN_DAYS_AFTER_TP", 0.25) * 86400

    # Daily trade cap
    daily_count = defaultdict(int)
    max_daily = getattr(ema_config, "MAX_TRADES_PER_DAY", 3)

    # DIAGNOSTIC: track why signals are rejected
    drop_stats = {
        "total_bars": 0,
        "na_guard": 0,
        "htf_missing": 0,
        "not_near_htf": 0,
        "off_session": 0,
        "h1_missing": 0,
        "veto_adx": 0,
        "veto_atr": 0,
        "cooldown_sl": 0,
        "cooldown_tp": 0,
        "daily_cap": 0,
        "no_ltf_long": 0,
        "no_ltf_short": 0,
        "has_open": 0,
        "regime_choppy": 0,
    }

      # Then iterate only over the TEST period (e.g., last 30 days)
    test_start_idx = 30
    for i in range(test_start_idx, len(df)):
        bar = df.iloc[i]
        drop_stats["total_bars"] += 1

        # Indicator warm-up guard (same as live)
        if bar[["atr", "atr30", "adx", "k_fast"]].isna().any():
            drop_stats["na_guard"] += 1
            curve.append(equity)
            continue
        
        # REGIME FILTER (FAST - just a lookup!)
        if regime_enabled and not regime_gate_fast(bar.name, regime_series):
            drop_stats["regime_choppy"] += 1
            curve.append(equity)
            continue

        # --- Manage open position ---
        if pos is not None:
            if pos.dir == 1:
                hit_sl = bar.l <= pos.sl
                hit_tp = bar.h >= pos.tp
            else:
                hit_sl = bar.h >= pos.sl
                hit_tp = bar.l <= pos.tp

            reason = None
            if STOP_FIRST and hit_sl:
                reason = "SL"
            elif hit_tp:
                reason = "TP"
            elif hit_sl:
                reason = "SL"

            if reason:
                exit_px = pos.sl if reason == "SL" else pos.tp
                pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                fee = fees_usd(pos.entry, exit_px, pos.qty, ema_config.FEE_BPS)
                pnl = pnl_gross - fee

                equity += pnl

                # Update cooldown trackers
                bar_ts = bar.name.timestamp()
                if reason == "SL":
                    last_sl_ts = bar_ts
                else:
                    last_tp_ts = bar_ts

                trades.append(dict(
                    dir="LONG" if pos.dir == 1 else "SHORT",
                    t_entry=pos.time_entry, t_exit=bar.name,
                    entry=pos.entry, sl=pos.sl, tp=pos.tp,
                    exit=exit_px, reason=reason,
                    pnl=pnl, equity=equity,
                    adx=pos.adx_entry, k_fast=pos.k_entry
                ))
                logging.info(
                    "[%s] %s %s | entry %.4f sl %.4f tp %.4f | exit %.4f | pnl $%.2f | eq $%.2f | adx %.1f k %.1f",
                    pair, trades[-1]["dir"], reason,
                    pos.entry, pos.sl, pos.tp, exit_px, pnl, equity,
                    pos.adx_entry, pos.k_entry
                )
                pos = None

        # --- Look for new entry if flat ---
        if pos is None:
            # HTF snapshot (same as live)
            try:
                idx_prev = htf_levels.index.get_indexer([bar.name], method="ffill")[0]
                if idx_prev == -1:
                    drop_stats["htf_missing"] += 1
                    htf_levels = update_htf_levels_new(htf_levels, bar)
                    h1 = update_h1(h1, bar.name, float(bar.c))
                    curve.append(equity)
                    continue
                htf_row = htf_levels.iloc[idx_prev]
            except Exception:
                drop_stats["htf_missing"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue

            # 1) HTF proximity gate
            max_atr_mom = getattr(ema_config, "NEAR_HTF_MAX_ATR_MOM_BACKTEST", 2.0)
            if not near_htf_level(bar, htf_row, max_atr=max_atr_mom):
                drop_stats["not_near_htf"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue

            # 2) Session gate
            if session_gating and not in_good_hours(bar.name, good_hours=good_hours):
                drop_stats["off_session"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue

            # 3) H1 slope row
            try:
                h1row = h1.loc[bar.name.floor("1h")]
            except KeyError:
                drop_stats["h1_missing"] += 1
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue

            # 4) Market-quality veto (EXACTLY as live)
            min_adx, atr_veto = veto_thresholds(bar)
            adx_hard_floor = getattr(ema_config, "ADX_HARD_FLOOR", 30)
            min_adx = max(min_adx, adx_hard_floor)

            veto = (bar.adx < min_adx or bar.atr < atr_veto * bar.atr30)
            if veto:
                if bar.adx < min_adx:
                    drop_stats["veto_adx"] += 1
                if bar.atr < atr_veto * bar.atr30:
                    drop_stats["veto_atr"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue

            # 5) Cooldown checks (same as live)
            bar_ts = bar.name.timestamp()
            if last_sl_ts and (bar_ts - last_sl_ts) < cooldown_sl_sec:
                drop_stats["cooldown_sl"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue
            if last_tp_ts and (bar_ts - last_tp_ts) < cooldown_tp_sec:
                drop_stats["cooldown_tp"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue
            # 6) Daily trade cap
            day_key = bar.name.date().isoformat()
            if daily_count[day_key] >= max_daily:
                drop_stats["daily_cap"] += 1
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1 = update_h1(h1, bar.name, float(bar.c))
                curve.append(equity)
                continue

            # --- SL/TP calculation (EXACTLY as live) ---
            cushion = 1.3 if bar.adx >= 30 else 1.5
            sl_base = cushion * bar.atr
            wick_buf = getattr(ema_config, "WICK_BUFFER", 0.25)
            stop_off = cushion * sl_base + wick_buf * bar.atr
            rr_target = getattr(ema_config, "RR_TARGET", 1.5)
            tp_dist = rr_target * stop_off

            # --- Signal checks
             
            # Get breakout-specific thresholds from config
            k_long_max = getattr(ema_config, "MOMENTUM_STO_K_LONG_MAX", 70)
            k_short_min = getattr(ema_config, "MOMENTUM_STO_K_SHORT_MIN", 30)

            # LONG BREAKOUT: k_slow must be < 70 (not overbought)
            if (bar.k_slow < k_long_max
                and ema_long_signal(df, i)):

                # Entry with execution delay
                if i + 1 < len(df):
                    next_bar = df.iloc[i + 1]
                    entry = float(next_bar.o) * (1 + ema_config.SLIP_BPS / 10_000)
                else:
                    entry = float(bar.c) * (1 + ema_config.SLIP_BPS / 10_000)

                risk_usd = equity0 * risk_pct
                qty = risk_usd / stop_off
                sl = round_price(entry - stop_off, pair, ema_config)
                tp = round_price(entry + tp_dist, pair, ema_config)

                pos = Position(
                    dir=1, entry=entry, sl=sl, tp=tp, qty=qty,
                    risk=risk_usd, time_entry=bar.name,
                    adx_entry=float(bar.adx), k_entry=float(bar.k_slow)
                )
                daily_count[day_key] += 1
                logging.info(
                    "[%s] LONG BREAKOUT @ %s | entry %.4f sl %.4f tp %.4f | adx %.1f k_slow %.1f | daily %d/%d",
                    pair, bar.name, entry, sl, tp, bar.adx, bar.k_slow,
                    daily_count[day_key], max_daily
                )

            # SHORT BREAKOUT: k_slow must be > 30 (not oversold)
            elif (bar.k_slow > k_short_min
                  and ema_short_signal(df, i)):

                if i + 1 < len(df):
                    next_bar = df.iloc[i + 1]
                    entry = float(next_bar.o) * (1 - ema_config.SLIP_BPS / 10_000)
                else:
                    entry = float(bar.c) * (1 - ema_config.SLIP_BPS / 10_000)

                risk_usd = equity0 * risk_pct
                qty = risk_usd / stop_off
                sl = round_price(entry + stop_off, pair, ema_config)
                tp = round_price(entry - tp_dist, pair, ema_config)

                pos = Position(
                    dir=-1, entry=entry, sl=sl, tp=tp, qty=qty,
                    risk=risk_usd, time_entry=bar.name,
                    adx_entry=float(bar.adx), k_entry=float(bar.k_slow)
                )
                daily_count[day_key] += 1
                logging.info(
                    "[%s] SHORT BREAKOUT @ %s | entry %.4f sl %.4f tp %.4f | adx %.1f k_slow %.1f | daily %d/%d",
                    pair, bar.name, entry, sl, tp, bar.adx, bar.k_slow,
                    daily_count[day_key], max_daily
                )
            else:
                # Track rejections
                drop_stats["no_ltf_long"] += 1
                drop_stats["no_ltf_short"] += 1
        # AFTER decision: update HTF/H1 (same order as live)
        htf_levels = update_htf_levels_new(htf_levels, bar)
        h1 = update_h1(h1, bar.name, float(bar.c))
        curve.append(equity)

    # --- Summary ---
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total = wins + losses
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    pf = float("inf") if gl == 0 else (gp / gl if gl > 0 else 0)

    summary = dict(
        pair=pair, trades=total, wins=wins, losses=losses,
        win_rate=(wins / total * 100) if total else 0.0,
        profit_factor=pf,
        equity_final=equity,
        net_pnl=equity - equity0,
        avg_win=gp / wins if wins else 0,
        avg_loss=gl / losses if losses else 0,
        rr_target=rr_target,
        risk_pct=risk_pct * 100
    )

    # Print drop stats
    print("\n" + "="*80)
    print("FILTER ANALYSIS (why signals were rejected)")
    print("="*80)
    print(f"Total bars processed........ {drop_stats['total_bars']:>8}")
    print(f"  NA guard (warm-up)........ {drop_stats['na_guard']:>8} ({drop_stats['na_guard']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  HTF missing............... {drop_stats['htf_missing']:>8} ({drop_stats['htf_missing']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Not near HTF level........ {drop_stats['not_near_htf']:>8} ({drop_stats['not_near_htf']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Off session hours......... {drop_stats['off_session']:>8} ({drop_stats['off_session']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  H1 missing................ {drop_stats['h1_missing']:>8} ({drop_stats['h1_missing']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Veto: ADX too low......... {drop_stats['veto_adx']:>8} ({drop_stats['veto_adx']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Veto: ATR too low......... {drop_stats['veto_atr']:>8} ({drop_stats['veto_atr']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Cooldown after SL......... {drop_stats['cooldown_sl']:>8} ({drop_stats['cooldown_sl']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Cooldown after TP......... {drop_stats['cooldown_tp']:>8} ({drop_stats['cooldown_tp']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  Daily cap reached......... {drop_stats['daily_cap']:>8} ({drop_stats['daily_cap']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  No LTF long confirmation.. {drop_stats['no_ltf_long']:>8} ({drop_stats['no_ltf_long']/drop_stats['total_bars']*100:>5.1f}%)")
    print(f"  No LTF short confirmation. {drop_stats['no_ltf_short']:>8} ({drop_stats['no_ltf_short']/drop_stats['total_bars']*100:>5.1f}%)")
    print (f"  Regime filter (choppy).... {drop_stats['regime_choppy']:>8} ({drop_stats['regime_choppy']/drop_stats['total_bars']*100:>5.1f}%)")
    print("="*80 + "\n")

    return summary, trades, pd.Series(curve, index=df.index[:len(curve)])


def print_trades_table(trades: List[Dict]):
    """Print formatted trade log."""
    if not trades:
        print("\nNo trades executed.")
        return

    print(f"\n{'='*100}")
    print(f"{'Entry Time':<20} {'Dir':<6} {'Entry':<8} {'Exit':<8} {'Reason':<6} {'PnL':<10} {'Equity':<10}")
    print(f"{'='*100}")
    for t in trades:
        print(f"{str(t['t_entry']):<20} {t['dir']:<6} "
              f"{t['entry']:<8.4f} {t['exit']:<8.4f} {t['reason']:<6} "
              f"${t['pnl']:>8.2f} ${t['equity']:>9.2f}")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s"
    )

    # Config
    pair = 'ETHUSDT'
    interval = 60
    equity = getattr(ema_config, "STAKE_SIZE_USD", 1000.0)
    risk_pct = getattr(ema_config, "RISK_PCT", 0.1)

    logging.info("="*80)
    logging.info("LRS ALIGNED BACKTEST (matches live engine)")
    logging.info("Pair: %s | TF: %sm | Starting Equity: $%.2f | Risk/Trade: %.1f%%",
                 pair, interval, equity, risk_pct * 100)
    logging.info("="*80)

    # Determine how much history to load (180 days)
    days_back = 31
    bars_needed = days_back * 96  # 96 bars per day for 15m
    
    
    now = datetime.now(timezone.utc)
    target_start = now - pd.Timedelta(days=days_back)
    logging.info("Loading %d bars (back to %s)", bars_needed, target_start.date())

    # Load 30 days of data (30 days × 96 bars/day = 2880 bars for 15m)
    hist = asyncio.run(preload_history(
        symbol=pair,
        interval=interval,
        limit=3000 # match live engine
    ))

    hist = align_by_close(hist, int(interval))

    logging.info("Data loaded: %d bars (%s to %s)",
                 len(hist), hist.index[0], hist.index[-1])
    
     # PRE-COMPUTE REGIME (ONCE!)
    print("\nPre-computing regime filter...")
    regime_series = precompute_regime(hist, ema_config)

    # Run backtest
    summary, trades, curve = backtest(
        hist,
        regime_series=regime_series,
        equity0=equity,
        risk_pct=risk_pct,
        pair=pair
    )

    # Display results
    print("\n" + "="*80)
    print("BACKTEST SUMMARY")
    print("="*80)
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"{k:.<30} {v:>12.2f}")
        else:
            print(f"{k:.<30} {v:>12}")
    print("="*80)

    print_trades_table(trades)

    # Save to CSV
    if trades:
        df_trades = pd.DataFrame(trades)
        output_file = f"backtest_trades_{pair}_{datetime.now():%Y%m%d_%H%M}.csv"
        df_trades.to_csv(output_file, index=False)
        logging.info("Trades saved to %s", output_file)