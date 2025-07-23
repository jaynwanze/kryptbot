import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import logging
from datetime import datetime
import pandas as pd
import sys
from helpers import config,build_htf_levels, tjr_long_signal, tjr_short_signal, update_htf_levels_new,ltf,round_price,alert_side
from data import preload_history

LOOKBACK_BARS = 1_000       # keep history light
GOOD_HOURS = config.SESSION_WINDOWS.get("asia", []) + \
                 config.SESSION_WINDOWS.get("eu", []) + \
                 config.SESSION_WINDOWS.get("ny", [])

# ───────── lightweight back-test ──────────
def backtest(df: pd.DataFrame,
             equity0: float = config.STAKE_SIZE_USD,
             risk_pct: float = config.RISK_PCT,
             pair: str = config.PAIR,
             ) -> None:
    """
    Replicates the notebook logic exactly:
    • same SL/TP math  (RR 2 : 1, no 20-bar cut-off)
    • GOOD_HOURS + 4-hour EMA50 bias filters
    • 1-bar look-back for EMA cross
    • tracks a *single* position at a time
    • prints every trade + summary and returns nothing
    """
    # ------------- build tf with hisotry data -------------------
    htf_levels   = build_htf_levels(df.copy())

    # ------------- back-test loop ------------------------------------
    equity   = equity0
    pos      = None                    # current position dict or None
    trades   = []
    curve    = []

    good_hours = GOOD_HOURS

    for i,(idx, bar) in enumerate(df.iterrows()):
        
        bar  = df.iloc[i]
        prev = df.iloc[i-1] if i else bar      # guard for i==0

        if bar[["atr", "adx", "k_fast"]].isna().any():
            curve.append(equity); continue     # still warming up
        
        # ---- update HTF levels  -----------------------------------
        try:
            htf_row = htf_levels.loc[bar.name]
        except KeyError:
            curve.append(equity); continue     # HTF still warming up

        # ---- manage open position ----------------------------------
        if pos:
            if (pos['dir'] ==  1 and bar.l <= pos['sl']) or \
                (pos['dir'] == -1 and bar.h >= pos['sl']):
                pnl = -pos['risk']
                equity += pnl            
                pos['time_close'] = idx
               # stop-loss
                trades.append({'exit': bar.name, 'pnl': -pos['risk'], 'dir': pos['dir']})
                direction = "⬆LONG" if pos['dir'] == 1 else "🔻SHORT"
                print (f"{direction} Trade Entry: {pos['entry']} | Timestamp: {pos['time_entry']}  | Take Proft: {pos['tp']} | Stop Loss: {pos['sl']}  |  PnL: ${pnl:.2f}  |  Equity: ${equity:.2f} | Volume: {bar.v:.0f}")
                print(f"🔴Trade closed: {idx} | Stop Loss hit: {pos['time_close']} | Exit: {bar.c} | SL: {pos['sl']}")
                pos = None

            elif (pos['dir'] ==  1 and bar.h >= pos['tp']) or \
                (pos['dir'] == -1 and bar.l <= pos['tp']):
                pnl = pos['risk'] * config.ATR_MULT_TP/config.ATR_MULT_SL
                equity += pnl
                pos['time_close'] = idx
                trades.append({'exit': bar.name,
                           'pnl' :  pos['risk']*config.ATR_MULT_TP/config.ATR_MULT_SL, 'dir': pos['dir']})
                direction = "⬆LONG" if pos['dir'] == 1 else "🔻SHORT"
                print (f"{direction} Trade Entry: {pos['entry']} | Timestamp: {pos['time_entry']}  | Take Proft: {pos['tp']} | Stop Loss: {pos['sl']}  |  PnL: ${pnl:.2f}  |  Equity: ${equity:.2f} | Volume: {bar.v:.0f}")
                print(f"🟢Trade closed: {idx} | Target hit: {pos['time_close']} | Exit: {bar.c} | TP: {pos['tp']}")
                pos = None

        # ---- look for new entry  -----------------------------------
        if pos is None:
            # inside the back‑test loop, **before** you call tjr_long/short_signal
            # four_h = df['c'].iloc[:i+1].resample('4h').last()
            # trend = four_h.pct_change().rolling(3).mean().abs().iloc[-1]
            # if trend < 0.006:          # < 0.6 % move in the last 3 days ⇒ chop
            #         continue
            # if i % 4 == 0:          # print probe every 4th candle, not every one

            #     probe_b = ltf.is_bos(df, i, "long")
            #     probe_f = ltf.has_fvg(df, i-1, "long")
            #     probe_x = ltf.fib_tag(bar.l, bar, "long")
            #     print (f"Probe: BOS {probe_b}  FVG {probe_f}  FIB {probe_x}")

        
            vol_norm = bar.atr / bar.atr30
            min_adx  = 10 + 8 * vol_norm            
            atr_veto = 0.5 + 0.3 * vol_norm        

            # Wide range veto - for multi pair stra
              # min_adx  = 6 + 6 * vol_norm            
            # atr_veto = 0.3 + 0.2 * vol_norm        
            if bar.adx < min_adx or bar.atr < atr_veto * bar.atr30:
                    continue 
            # distance between entry and SL
            stop_off = (config.ATR_MULT_SL * 1.6 + config.WICK_BUFFER) * bar.atr
            if tjr_long_signal(df, i, htf_row):
                # if bar.k_fast < config.STO_K_MIN_LONG:
                #     continue
                b = ltf.is_bos(df, i, "long")
                f = ltf.has_fvg(df, i-1, "long")
                x = ltf.fib_tag(bar.l, bar, "long")
                print(i)
                print(f"{bar.name}  BOS {b}  FVG {f}  FIB {x}")
                entry = round_price(bar.c, pair)
                sl    = round_price(entry - stop_off, pair)
                tp    = round_price(entry + config.ATR_MULT_TP * bar.atr, pair)
                pos  = dict(dir=1, entry=entry,
                        sl=sl, tp=tp,
                        risk=equity0*risk_pct, half=False, time_entry=idx, time_close=None)

            elif tjr_short_signal(df, i, htf_row):
                # if bar.k_fast > 100 - config.STO_K_MIN_SHORT:
                #     continue
                print(i)
                b = ltf.is_bos(df, i, "short")
                f = ltf.has_fvg(df, i-1, "short")
                x = ltf.fib_tag(bar.h, bar, "short")
                entry = round_price(bar.c, pair)
                sl    = round_price(entry + stop_off, pair)
                tp    = round_price(entry - config.ATR_MULT_TP * bar.atr, pair)
                print(f"{bar.name}  BOS {b}  FVG {f}  FIB {x}")
                pos  = dict(dir=-1, entry=entry,
                        sl=sl, tp=tp,
                        risk=equity0*risk_pct, half=False,time_entry=idx, time_close=None)

        curve.append(equity)
        # ---- update HTF levels  -----------------------------------
        htf_levels = update_htf_levels_new(htf_levels, bar)

    # ------------- final statistics ---------------------------------
    wins  = sum(1 for t in trades if t["pnl"] > 0)
    losses= sum(1 for t in trades if t["pnl"] < 0)
    # print(f"Back-test {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}  "
    #       f"entries {len(trades)}  |  wins {wins}  losses {losses}  "
    #       f"win-rate {wins/(wins+losses):.1%}  |  final ${equity:,.0f}")
       # ---------- final statistics ----------
    # wins   = sum(1 for t in trades if t["pnl"] > 0)
    # losses = sum(1 for t in trades if t["pnl"] < 0)
    total  = wins + losses
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))

    # profit‑factor: gross‑profit / |gross‑loss|
    pf = float("inf") if gross_loss == 0 else round(gross_profit / gross_loss, 2)

    
    print(f"... entries {len(trades)} | wins {wins} losses {losses} "
      f"win-rate {wins/total:.1%}" if total else "win-rate n/a",
      f"| final ${equity:,.0f} | R.R {config.ATR_MULT_TP}:{config.ATR_MULT_SL} | ")
    longs  = sum(p["dir"] ==  1 for p in trades)
    shorts = sum(p["dir"] == -1 for p in trades)
    print(f"longs {longs}, shorts {shorts}, win% {wins/total:.1%}")
    
    # optional: equity curve
    pd.Series(curve, index=df.index[-len(curve):]).plot(grid=True, figsize=(10,3))

    summary = dict(
        name          = "TJR-raid",      # or "Live-bot"
        trades        = len(trades),
        wins          = wins,
        losses        = losses,
        win_rate      = wins / total if total else 0,
        profit_factor = pf,
        equity_final  = equity,
    )
    return summary                   #  ←  **important**



# ───────────────────────────────── entrypoint ────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    logging.info("LRS back‑test starting  %s", datetime.utcnow().strftime("%F %T"))
    hist = asyncio.run(preload_history(symbol=config.PAIR, interval=config.INTERVAL, limit=3000))
    hist_30d = hist[hist.index >= hist.index[-1] - pd.Timedelta(days=30)]
    backtest(hist_30d, pair=config.PAIR)