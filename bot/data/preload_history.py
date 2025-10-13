import asyncio
import requests
import pandas as pd
from bot.helpers import config, compute_indicators

async def preload_history(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    """
    Fetch up to *limit* most-recent candles from Bybit REST and return a DataFrame.
    Uses pagination to fetch more than 1000 bars if needed.
    """
    url = "https://api.bybit.com/v5/market/kline"
    all_rows = []
    last_ts = None
    max_per_request = 1000  # Bybit's per-request limit
    
    # Calculate how many requests we need
    requests_needed = (limit + max_per_request - 1) // max_per_request
    
    print(f"[PRELOAD] Fetching {limit} bars for {symbol} ({requests_needed} requests needed)")
    
    for req_num in range(requests_needed):
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": min(max_per_request, limit - len(all_rows))  # Don't over-fetch
        }
        
        if last_ts:
            params["end"] = last_ts  # Pagination: go backwards from last timestamp
        
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            
            if data.get("retCode") != 0:
                print(f"[PRELOAD] API error: {data.get('retMsg')}")
                break
            
            rows = data["result"]["list"]
            if not rows:
                print(f"[PRELOAD] No more data returned (got {len(all_rows)} total)")
                break
            
            all_rows.extend(rows)
            last_ts = int(rows[-1][0]) - 1  # Go backwards (oldest bar - 1ms)
            
            print(f"[PRELOAD] Request {req_num + 1}/{requests_needed}: "
                  f"fetched {len(rows)} bars (total: {len(all_rows)}/{limit})")
            
            # If we got fewer bars than requested, we've hit the end of available history
            if len(rows) < max_per_request:
                print(f"[PRELOAD] Reached end of available history")
                break
            
            # If we've collected enough bars, stop
            if len(all_rows) >= limit:
                break
            
            # Rate limiting: small delay between requests
            await asyncio.sleep(0.1)
            
        except Exception as e:
            print(f"[PRELOAD] Error on request {req_num + 1}: {e}")
            break
    
    if len(all_rows) < limit:
        print(f"[PRELOAD] WARNING: Only got {len(all_rows)}/{limit} bars. "
              f"Bybit may not have enough historical data for {symbol}.")
    
    # Bybit REST returns newest-first; reverse so earliest comes first
    all_rows = list(reversed(all_rows))
    
    # Parse into DataFrame
    df = pd.DataFrame(
        [[float(o), float(h), float(l), float(c), float(v), int(t)]
         for t, o, h, l, c, v, *_ in all_rows],
        columns=["o", "h", "l", "c", "v", "ts"]
    ).assign(ts=lambda d: pd.to_datetime(d.ts, unit="ms", utc=True)).set_index("ts")
    
    print(f"[PRELOAD] Final data range: {df.index[0]} to {df.index[-1]} ({len(df)} bars)")
    
    return compute_indicators(df)