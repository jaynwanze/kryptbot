#!/usr/bin/env python3
import os, sys
sys.path.append(os.path.abspath(
    os.path.join(os.path.dirname(__file__), '../..')))

from bot.data import preload_history
from bot.helpers import (
    config, compute_indicators, build_htf_levels, update_htf_levels_new,
    tjr_long_signal, tjr_short_signal, round_price, align_by_close, fees_usd, next_open_price
)
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional, List, Dict
import pandas as pd

# --- knobs to mimic live fills ---
STOP_FIRST = True         # if both TP & SL are hit in a candle, count SL first
ENTRY_POLICY = "next_open"   # or "close"

# def entry_px(df, i, pair, side, slip_bps):
#     if ENTRY_POLICY == "close":
#         j = i
#         px = df.iloc[i].c
#     else:  # next_open (live-like MARKET after candle close)
#         j = min(i + 1, len(df) - 1)
#         px = df.iloc[j].o
#     if slip_bps:
#         s = slip_bps / 1e4
#         px = px * (1 + s) if side == 1 else px * (1 - s)
#     return round_price(px, pair), j, df.index[j]

@dataclass
class Position:
    dir: int                  # +1 long, -1 short
    entry: float
    sl: float
    tp: float
    qty: float              # position size in base currency    
    risk: float               # USD at risk at entry time
    time_entry: pd.Timestamp     # timestamp of entry (i+1)
    time_close: Optional[pd.Timestamp] = None  # timestamp of exit (if closed)

def backtest(df: pd.DataFrame,
             equity0: float = config.STAKE_SIZE_USD,
             risk_pct: float = config.RISK_PCT,
             pair: str = config.PAIR):

    # 1) compute indicators once (rolling ops use only past values)
    df = compute_indicators(df.copy())

    # 2) build HTF levels once; we’ll snapshot with ffill and update after decisions
    htf_levels = build_htf_levels(df.copy())

    equity = equity0
    pos: Optional[Position] = None
    trades: List[Dict] = []
    curve: List[float] = []

    for i,(idx, bar) in enumerate(df.iterrows()):
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
                    exit_px = pos.sl
                    pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                    # fee = fees_usd(pos.entry, exit_px, pos.qty, config.FEE_BPS)
                    pnl = pnl_gross
                else:
                     exit_px = pos.tp
                     pnl_gross = pos.dir * pos.qty * (exit_px - pos.entry)
                    #  fee = fees_usd(pos.entry, exit_px, pos.qty, config.FEE_BPS)
                     pnl = pnl_gross
                     
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
            # same veto as live engine
            vol_norm = bar.atr / bar.atr30
            min_adx = 10 + 8 * vol_norm
            atr_veto = 0.5 + 0.3 * vol_norm
            if bar.adx < min_adx or bar.atr < atr_veto * bar.atr30:
                curve.append(equity)
                htf_levels = update_htf_levels_new(htf_levels, bar)
                continue
            sl_base   = config.ATR_MULT_SL * bar.atr
            stop_off  = config.SL_CUSHION_MULT * sl_base + config.WICK_BUFFER * bar.atr
            tp_dist   = config.RR_TARGET * stop_off       # ← exact 2:1 vs *your* stop math
            ratio = tp_dist / stop_off
            if tjr_long_signal(df, i, htf_row):
                side = "LONG"; dir_ = 1
                entry = next_open_price(df, i, side, pair, config.SLIP_BPS)
                sl = round_price(entry - stop_off, pair)
                tp = round_price(entry + tp_dist, pair)
                risk_usd = equity0 * risk_pct
                qty = risk_usd / stop_off
                pos = Position(dir=dir_, entry=entry, sl=sl, tp=tp, qty=qty,
                               risk=risk_usd, time_entry=idx, time_close=None)
                logging.info("[%s] %s signal @ %s | entry %.3f sl %.3f tp %.3f | adx %.1f k_fast %.1f",
             pair, side, idx, entry, sl, tp, bar.adx, bar.k_fast)
                logging.info("[RATIO] atr=%.5f tp_dist=%.5f sl_dist=%.5f ratio=%.3f",
             bar.atr,
             tp_dist,
             stop_off,
             ratio)
            elif tjr_short_signal(df, i, htf_row):
                side  = "SHORT"; dir_ = -1
                entry = next_open_price(df, i, side, pair, config.SLIP_BPS)
                sl    = round_price(entry + stop_off, pair)
                tp    = round_price(entry - tp_dist,  pair)
                risk_usd = equity0 * risk_pct
                qty      = risk_usd / stop_off

                pos = Position(dir=dir_, entry=entry, sl=sl, tp=tp, qty=qty,risk=risk_usd,
               time_entry=idx, time_close=None)
                logging.info("[%s] %s signal @ %s | entry %.3f sl %.3f tp %.3f | adx %.1f k_fast %.1f",
             pair, side, idx, entry, sl, tp, bar.adx, bar.k_fast)
                logging.info("[RATIO] atr=%.5f tp_dist=%.5f sl_dist=%.5f ratio=%.3f",
             bar.atr,
             tp_dist,
             stop_off,
             ratio)
                # optional debug
                # b = ltf.is_bos(df, i, "long" if side==1 else "short")
                # f = ltf.has_fvg(df, i-1, "long" if side==1 else "short")
                # x = ltf.fib_tag(bar.l if side==1 else bar.h, bar, "long" if side==1 else "short")
           

        # AFTER decision, keep HTF map fresh (same order as live)
        htf_levels = update_htf_levels_new(htf_levels, bar)
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
    logging.info("LRS back‑test starting  %s", datetime.now(timezone.utc).strftime("%F %T"))
    hist = asyncio.run(preload_history(symbol=config.PAIR, interval=config.INTERVAL, limit=3000))
    hist = align_by_close(hist, int(config.INTERVAL))
    summary, trades, curve = backtest(hist, equity0=config.STAKE_SIZE_USD, risk_pct=config.RISK_PCT, pair=config.PAIR)
    print("Summary:", summary)
