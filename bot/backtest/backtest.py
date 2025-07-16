import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import logging
from datetime import datetime
import pandas as pd
import sys
from helpers import config, is_bos, has_fvg,fib_tag,build_htf_levels, tjr_long_signal, tjr_short_signal
from data import preload_history

# ────────────────────────────────────────────────────────────────
#  Telegram alert
# ────────────────────────────────────────────────────────────────

# def test_alert_side(bar, side: str):
#     """Test function to send a nicely formatted Telegram alert."""
#     stop_off = (ATR_MULT_SL * 1.6 + WICK_BUFFER) * bar.atr
#     if side == "LONG":
#         sl = bar.c - stop_off
#         tp = bar.c + ATR_MULT_TP * bar.atr
#         emoji = "📈"
#     else:  # SHORT
#         sl = bar.c + stop_off
#         tp = bar.c - ATR_MULT_TP * bar.atr
#         emoji = "📉"

#     msg = (
#         f"{emoji} *{PAIR} {INTERVAL}‑m {side} signal*\n"
#         f"`{bar.name:%Y‑%m‑%d %H:%M}` UTC\n"
#         f"Entry  : `{bar.c:.3f}`\n"
#         f"Stop   : `{sl:.3f}`\n"
#         f"Target : `{tp:.3f}`\n"
#         f"ADX    : `{bar.adx:.1f}`\n"
#         f"StochK : `{bar.k_fast:.1f}`"
#     )
#     try:
#         bot.send_message(chat_id=TG_CHAT_ID, text=msg, parse_mode="Markdown")
#         logging.info("Telegram alert sent: %s %s", side, bar.name)
#     except Exception as exc:
#         logging.error("Telegram error: %s", exc)

# ───────── lightweight back-test ──────────
def backtest(df: pd.DataFrame,
             equity0: float = config.STAKE_SIZE_USD,
             risk_pct: float = config.RISK_PCT,
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
    htf_levels   = build_htf_levels(df)

    # ------------- back-test loop ------------------------------------
    equity   = equity0
    pos      = None                    # current position dict or None
    trades   = []
    curve    = []

    # good_hours = GOOD_HOURS if good_hours is None else good_hours

    for i,(idx, bar) in enumerate(df.iterrows()):
        bar, prev = df.iloc[i], df.iloc[i-1] if i > 0 else df.iloc[i]
        htf_row = htf_levels.loc[idx]

        # if bar[["atr", "adx", "k_fast"]].isna().any():
        #     curve.append(equity); continue     # still warming up

        # ---- manage open position ----------------------------------
        if pos:
            if (pos['dir'] ==  1 and bar.l <= pos['sl']) or \
                (pos['dir'] == -1 and bar.h >= pos['sl']):
                pnl = -pos['risk']
                equity += pnl            
                pos['time_close'] = idx
               # stop-loss
                trades.append({'exit': bar.name, 'pnl': -pos['risk']})
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
                           'pnl' :  pos['risk']*config.ATR_MULT_TP/config.ATR_MULT_SL})
                direction = "⬆LONG" if pos['dir'] == 1 else "🔻SHORT"
                print (f"{direction} Trade Entry: {pos['entry']} | Timestamp: {pos['time_entry']}  | Take Proft: {pos['tp']} | Stop Loss: {pos['sl']}  |  PnL: ${pnl:.2f}  |  Equity: ${equity:.2f} | Volume: {bar.v:.0f}")
                print(f"🟢Trade closed: {idx} | Target hit: {pos['time_close']} | Exit: {bar.c} | TP: {pos['tp']}")
                pos = None

        # ---- look for new entry  -----------------------------------
        # Optional condition to add - and bar.name.hour in good_hours
        if pos is None :
            if tjr_long_signal(df, i, htf_row):
                print(i)
                stop = config.ATR_MULT_SL*bar.atr *1.6
                pos  = dict(dir=1, entry=bar.c,
                        sl=bar.c-stop - config.WICK_BUFFER * bar.atr, tp=bar.c+config.ATR_MULT_TP*bar.atr,
                        risk=equity0*risk_pct, half=False, time_entry=idx, time_close=None)

            elif tjr_short_signal(df, i, htf_row):
                print(i)
                stop = config.ATR_MULT_SL*bar.atr *1.6
                pos  = dict(dir=-1, entry=bar.c,
                        sl=bar.c+stop + config.WICK_BUFFER * bar.atr, tp=bar.c-config.ATR_MULT_TP*bar.atr,
                        risk=equity0*risk_pct, half=False,time_entry=idx, time_close=None)

        curve.append(equity)

    # ------------- final statistics ---------------------------------
    wins  = sum(1 for t in trades if t["pnl"] > 0)
    losses= sum(1 for t in trades if t["pnl"] < 0)
    # print(f"Back-test {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}  "
    #       f"entries {len(trades)}  |  wins {wins}  losses {losses}  "
    #       f"win-rate {wins/(wins+losses):.1%}  |  final ${equity:,.0f}")
       # ---------- final statistics ----------
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total  = wins + losses
    pf     = (sum(t["pnl"] for t in trades if t["pnl"] > 0) /
              abs(sum(t["pnl"] for t in trades if t["pnl"] < 0) or 1))
    
    print(f"... entries {len(trades)} | wins {wins} losses {losses} "
      f"win-rate {wins/total:.1%}" if total else "win-rate n/a",
      f"| final ${equity:,.0f} | R.R {config.ATR_MULT_TP}:{config.ATR_MULT_SL} | ")
    
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
    logging.info("SOL/USDT TJR Strategy Backtest Starting %s", datetime.utcnow())
    hist = asyncio.run(preload_history(limit=3000))
    hist_30d = hist[hist.index >= hist.index[-1] - pd.Timedelta(days=30)]
    backtest(hist_30d)   # pass accessor