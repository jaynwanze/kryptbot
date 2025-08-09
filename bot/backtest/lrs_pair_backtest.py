#!/usr/bin/env python3
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict

import asyncio
import logging
import pandas as pd

from bot.data import preload_history
from bot.helpers import (
    config, compute_indicators, build_htf_levels, update_htf_levels_new,
    tjr_long_signal, tjr_short_signal, round_price, align_by_close,
    fees_usd, next_open_price, build_h1, update_h1, raid_happened, ltf
)

# --- knobs to mimic live fills ---
STOP_FIRST   = True         # if both TP & SL are hit in a candle, count SL first
ENTRY_POLICY = "next_open"  # or "close"
PENDING_BARS = 2            # how many bars a pending ticket can wait for its fib-tag

@dataclass
class Position:
    dir: int                     # +1 long, -1 short
    entry: float
    sl: float
    tp: float
    qty: float                   # position size in base currency
    risk: float                  # USD at risk at entry time
    time_entry: pd.Timestamp     # timestamp of entry (i+1)
    time_close: Optional[pd.Timestamp] = None  # timestamp of exit (if closed)


def backtest(df: pd.DataFrame,
             equity0: float = config.STAKE_SIZE_USD,
             risk_pct: float = config.RISK_PCT,
             pair: str = config.PAIR):

    # 1) compute indicators once (rolling ops use only past values)
    df = compute_indicators(df.copy())

    # 2) build HTF levels & H1 trend once; we’ll snapshot/update each bar
    htf_levels = build_htf_levels(df.copy())
    h1         = build_h1(df.copy())

    equity = equity0
    pos: Optional[Position] = None
    trades: List[Dict] = []
    curve: List[float] = []


    for i, (idx, bar) in enumerate(df.iterrows()):
        bar = df.iloc[i]

        # live-like NA guard
        if bar[["atr", "atr30", "adx", "k_fast"]].isna().any():
            curve.append(equity)
            continue

        # HTF snapshot via ffill (same as live)
        idx_prev = htf_levels.index.get_indexer([bar.name], method="ffill")[0]
        if idx_prev == -1:
            curve.append(equity)
            continue
        htf_row = htf_levels.iloc[idx_prev]

        # H1 slope row (guard early if not yet available)
        try:
            h1row = h1.loc[bar.name.floor("1h")]
        except KeyError:
            curve.append(equity)
            continue

        # --- manage open position (only from its entry bar onward) ---
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
                if reason == "SL":
                    exit_px   = pos.sl
                    pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                    fee       = fees_usd(pos.entry, exit_px, pos.qty, config.FEE_BPS)
                    pnl       = pnl_gross - fee
                else:
                    exit_px   = pos.tp
                    pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                    fee       = fees_usd(pos.entry, exit_px, pos.qty, config.FEE_BPS)
                    pnl       = pnl_gross - fee

                equity += pnl
                trades.append(dict(
                    dir="LONG" if pos.dir == 1 else "SHORT",
                    t_entry=pos.time_entry, t_exit=bar.name,
                    entry=pos.entry, sl=pos.sl, tp=pos.tp,
                    exit=exit_px, reason=reason, pnl=pnl, equity=equity
                ))
                logging.info("[%s] %s %s | entry %.3f sl %.3f tp %.3f | exit %.3f | pnl $%.2f | eq $%.2f",
                             pair, trades[-1]["dir"], reason,
                             pos.entry, pos.sl, pos.tp, exit_px, pnl, equity)
                pos = None

        # --- look for new entry if flat ---
        if pos is None:
            # same veto as live engine (slightly relaxed vs earlier)
            vol_norm = bar.atr / bar.atr30
            min_adx  = 12 + 6 * vol_norm       # was 10 + 8 * vol_norm
            atr_veto = 0.45 + 0.25 * vol_norm  # was 0.5 + 0.3 * vol_norm
            if bar.adx < min_adx or bar.atr < atr_veto * bar.atr30:
                curve.append(equity)
                # AFTER decision (none), keep HTF/H1 fresh
                htf_levels = update_htf_levels_new(htf_levels, bar)
                h1         = update_h1(h1, idx, float(bar.c))
                continue

            # SL/TP distances
            sl_base  = config.ATR_MULT_SL * bar.atr
            stop_off = config.SL_CUSHION_MULT * sl_base + config.WICK_BUFFER * bar.atr
            tp_dist  = config.RR_TARGET * stop_off

            # 2) Normal immediate entries (with H1 slope gates)
            if pos is None and tjr_long_signal(df, i, htf_row) and (h1row.slope > 0):
                side     = "LONG"; dir_ = 1
                entry    = next_open_price(df, i, side, pair, config.SLIP_BPS)
                risk_usd = equity0 * risk_pct
                qty      = risk_usd / stop_off
                sl       = round_price(entry - stop_off, pair)
                tp       = round_price(entry + tp_dist,  pair)
                pos = Position(dir=dir_, entry=entry, sl=sl, tp=tp, qty=qty,
                               risk=risk_usd, time_entry=idx, time_close=None)
                logging.info("[%s] %s signal @ %s | entry %.3f sl %.3f tp %.3f | adx %.1f k_fast %.1f",
                             pair, side, idx, entry, sl, tp, bar.adx, bar.k_fast)

            elif pos is None and tjr_short_signal(df, i, htf_row) and (h1row.slope < 0):
                side     = "SHORT"; dir_ = -1
                entry    = next_open_price(df, i, side, pair, config.SLIP_BPS)
                risk_usd = equity0 * risk_pct
                qty      = risk_usd / stop_off
                sl       = round_price(entry + stop_off, pair)
                tp       = round_price(entry - tp_dist,  pair)
                pos = Position(dir=dir_, entry=entry, sl=sl, tp=tp, qty=qty,
                               risk=risk_usd, time_entry=idx, time_close=None)
                logging.info("[%s] %s signal @ %s | entry %.3f sl %.3f tp %.3f | adx %.1f k_fast %.1f",
                             pair, side, idx, entry, sl, tp, bar.adx, bar.k_fast)

        # AFTER decision, keep HTF/H1 map fresh (same order as live)
        htf_levels = update_htf_levels_new(htf_levels, bar)
        h1         = update_h1(h1, idx, float(bar.c))
        curve.append(equity)

    # --- summary ---
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total  = wins + losses
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    pf = float("inf") if gl == 0 else gp / gl

    summary = dict(
        pair=pair, trades=total, wins=wins, losses=losses,
        win_rate=(wins/total) if total else 0.0, profit_factor=pf,
        equity_final=equity, rr=f"{config.RR_TARGET}:{config.ATR_MULT_SL}",
        risk_pct=risk_pct
    )
    return summary, trades, pd.Series(curve, index=df.index[:len(curve)])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    logging.info("LRS back-test starting  %s", datetime.now(timezone.utc).strftime("%F %T"))
    hist = asyncio.run(preload_history(symbol=config.PAIR, interval=config.INTERVAL, limit=3000))
    hist = align_by_close(hist, int(config.INTERVAL))
    summary, trades, curve = backtest(hist, equity0=config.STAKE_SIZE_USD, risk_pct=config.RISK_PCT, pair=config.PAIR)
    print("Summary:", summary)
