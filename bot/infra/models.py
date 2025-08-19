from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class Signal:
    symbol: str
    side: str  # "Buy" or "Sell"
    entry: float
    sl: float
    tp: float
    key: str  # unique id = f"{symbol}-{entry:%H%M}"
    ts: datetime
    # optional meta (safe defaults)
    adx: Optional[float] = None
    k_fast: Optional[float] = None
    vol: Optional[float] = None


@dataclass
class Position:
    signal: Signal
    order_id: str  # Bybit parent order
    qty: float
