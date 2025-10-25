"""
bot/data/data_manager.py

Unified data loader supporting:
1. REST API (live/backtest)
2. Cached CSV/Parquet (backtest only)
3. Automatic caching for reproducibility
"""

import asyncio
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from bot.helpers import compute_indicators
from typing import Literal
    
# Cache directory
CACHE_DIR = Path("./data_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class DataManager:
    """
    Unified data source manager.
    
    Usage:
        # Live trading (always use REST)
        dm = DataManager(mode='live')
        df = await dm.load_history('SOLUSDT', '15', limit=1000)
        
        # Backtesting (use cache if exists, else REST + save cache)
        dm = DataManager(mode='backtest')
        df = await dm.load_history('SOLUSDT', '15', limit=2880)
    """
    
    def __init__(self, mode: Literal['live', 'backtest'] = 'live'):
        self.mode = mode
        self.rest_url = "https://api.bybit.com/v5/market/kline"
    
    async def load_history(
        self, 
        symbol: str, 
        interval: str, 
        limit: int = 1000,
        force_refresh: bool = False
    ) -> pd.DataFrame:
        """
        Load historical data.
        
        Args:
            symbol: Trading pair (e.g., 'SOLUSDT')
            interval: Timeframe (e.g., '15' for 15m)
            limit: Number of bars to load
            force_refresh: If True, bypass cache and fetch from REST
        
        Returns:
            DataFrame with OHLCV + indicators
        """
        if self.mode == 'live':
            # Live mode: always use REST
            return await self._fetch_from_rest(symbol, interval, limit)
        
        # Backtest mode: try cache first
        cache_file = self._cache_path(symbol, interval, limit)
        
        if not force_refresh and cache_file.exists():
            print(f"[DATA] Loading from cache: {cache_file.name}")
            return self._load_from_cache(cache_file)
        
        # Cache miss: fetch from REST and save
        print(f"[DATA] Cache miss, fetching from REST...")
        df = await self._fetch_from_rest(symbol, interval, limit)
        self._save_to_cache(df, cache_file)
        return df
    
    async def _fetch_from_rest(
        self, 
        symbol: str, 
        interval: str, 
        limit: int
    ) -> pd.DataFrame:
        """Fetch data from Bybit REST API (same as preload_history)."""
        all_rows = []
        last_ts = None
        max_per_request = 1000
        requests_needed = (limit + max_per_request - 1) // max_per_request
        
        print(f"[REST] Fetching {limit} bars for {symbol} ({requests_needed} requests)")
        
        for req_num in range(requests_needed):
            params = {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": min(max_per_request, limit - len(all_rows))
            }
            
            if last_ts:
                params["end"] = last_ts
            
            try:
                r = requests.get(self.rest_url, params=params, timeout=10)
                r.raise_for_status()
                data = r.json()
                
                if data.get("retCode") != 0:
                    print(f"[REST] API error: {data.get('retMsg')}")
                    break
                
                rows = data["result"]["list"]
                if not rows:
                    break
                
                all_rows.extend(rows)
                last_ts = int(rows[-1][0]) - 1
                
                if len(rows) < max_per_request or len(all_rows) >= limit:
                    break
                
                await asyncio.sleep(0.1)  # Rate limiting
                
            except Exception as e:
                print(f"[REST] Error on request {req_num + 1}: {e}")
                break
        
        if len(all_rows) < limit:
            print(f"[REST] WARNING: Only got {len(all_rows)}/{limit} bars")
        
        # Bybit returns newest-first; reverse for chronological order
        all_rows = list(reversed(all_rows))
        
        # Parse into DataFrame
        df = pd.DataFrame(
            [[float(o), float(h), float(l), float(c), float(v), int(t)]
             for t, o, h, l, c, v, *_ in all_rows],
            columns=["o", "h", "l", "c", "v", "ts"]
        ).assign(
            ts=lambda d: pd.to_datetime(d.ts, unit="ms", utc=True)
        ).set_index("ts")
        
        print(f"[REST] Loaded {len(df)} bars: {df.index[0]} to {df.index[-1]}")
        
        return compute_indicators(df)
    
    def _cache_path(self, symbol: str, interval: str, limit: int) -> Path:
        """Generate cache file path."""
        # Include limit in filename to avoid conflicts
        return CACHE_DIR / f"{symbol}_{interval}m_{limit}bars.parquet"
    
    def _save_to_cache(self, df: pd.DataFrame, path: Path) -> None:
        """Save DataFrame to cache (Parquet for speed/compression)."""
        try:
            df.to_parquet(path, compression='snappy')
            print(f"[CACHE] Saved to {path.name}")
        except Exception as e:
            print(f"[CACHE] Save failed: {e}")
    
    def _load_from_cache(self, path: Path) -> pd.DataFrame:
        """Load DataFrame from cache."""
        df = pd.read_parquet(path)
        # Ensure indicators are computed (in case cache is old)
        return compute_indicators(df)
    
    def clear_cache(self, symbol: str = None) -> None:
        """Clear cache files (all or specific symbol)."""
        pattern = f"{symbol}_*" if symbol else "*"
        for f in CACHE_DIR.glob(pattern):
            f.unlink()
            print(f"[CACHE] Deleted {f.name}")


# Convenience function (backward compatibility)
async def preload_history(
    symbol: str, 
    interval: str, 
    limit: int = 1000,
    mode: Literal['live', 'backtest'] = 'live',
    force_refresh: bool = False
) -> pd.DataFrame:
    """
    Backward-compatible wrapper.
    
    For live trading, always fetches from REST.
    For backtesting, uses cache if available.
    """
    dm = DataManager(mode=mode)
    return await dm.load_history(symbol, interval, limit, force_refresh)