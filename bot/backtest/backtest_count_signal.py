from datetime import timedelta
import asyncio, pandas as pd
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from data import preload_history
from backtest import backtest
from helpers import config

PAIRS = ["SOLUSDT","ATOMUSDT","WAVESUSDT","XRPUSDT"]
DAYS  = 30

async def count_signals():
    rows = []
    for p in PAIRS:
        hist = await preload_history(symbol=p, interval=config.INTERVAL, limit=3000)
        look = hist[hist.index >= hist.index[-1] - timedelta(days=DAYS)]
        res  = backtest(look,pair=p)
        rows.append((p, res["trades"] / DAYS))
    df = pd.DataFrame(rows, columns=["pair","signals_per_day"])
    print(df)

asyncio.run(count_signals())
