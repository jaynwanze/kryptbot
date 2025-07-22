# engine.py  ─────────────────────────────────────────────────────
import asyncio, logging, pandas as pd
from datetime import datetime, timedelta
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from helpers import config
from data    import preload_history                               # ← already async
from backtest import backtest                                     # ← your function

PAIRS = ["SOLUSDT", "ATOMUSDT", "WAVESUSDT", "XRPUSDT"]           # ❶ run these
DAYS  = 30                                                        # ❷ look‑back window
BAR_LIMIT = 3_000                                                 # ❸ preload cap

async def backtest_one(pair: str) -> dict:
    """Load history for *pair*, slice the last DAYS and run the existing back‑test."""
    raw = await preload_history(symbol=pair, interval=config.INTERVAL, limit=BAR_LIMIT)
    lookback = raw[raw.index >= raw.index[-1] - timedelta(days=DAYS)]
    summary = backtest(lookback)          # ← your original fn – returns dict
    summary["pair"] = pair
    return summary

async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    logging.info("Multi‑pair engine starting %s", datetime.utcnow().strftime("%F %T"))

    # ── kick off back‑tests in parallel
    tasks = [asyncio.create_task(backtest_one(p)) for p in PAIRS]
    results = await asyncio.gather(*tasks)

    # ── pretty print
    df = pd.DataFrame(results).set_index("pair")
    cols = ["trades","wins","losses","win_rate","profit_factor","equity_final"]
    print("\n===== 30‑day summary (15‑m RR 2:1) =====")
    print(df[cols].to_string(float_format=lambda x: f"{x:,.2f}" if isinstance(x,float) else f"{x:,}"))
    print("=========================================")

if __name__ == "__main__":
    asyncio.run(main())
