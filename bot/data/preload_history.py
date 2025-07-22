import asyncio
import requests
import pandas as pd
from helpers import config, compute_indicators

async def preload_history(symbol:str, interval:str, limit: int = 1000) -> pd.DataFrame:
    """
    Fetch up to *limit* most-recent 15-m candles from Bybit REST and return a DataFrame.
    """
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit
    }
    all_rows = []
    last_ts = None
    while len(all_rows) < limit:
        if last_ts:
            params["end"] = last_ts
        r = requests.get(url, params=params, timeout=10).json()
        rows = r["result"]["list"]
        if not rows:
            break
        all_rows.extend(rows)
        last_ts = int(rows[-1][0]) - 1  # go backwards
        if len(rows) < 1000:
            break
    # Bybit REST returns newest-first; reverse so earliest comes first
    all_rows = list(reversed(all_rows))
    df = pd.DataFrame(
        [[float(o), float(h), float(l), float(c), float(v), int(t)]
         for t, o, h, l, c, v, *_ in all_rows],
        columns=["o", "h", "l", "c", "v", "ts"]
    ).assign(ts=lambda d: pd.to_datetime(d.ts, unit="ms", utc=True)).set_index("ts")
    return compute_indicators(df)
