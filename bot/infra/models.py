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
    adx: float = 0.0
    k_fast: float = 50.0
    k_slow: float = 50.0
    d_slow: float = 50.0
    vol: float = 0.0
    off_sl: float = 0.0  # distance from entry to SL at signal time
    off_tp: float = 0.0  # distance from entry to TP at signal time


@dataclass
class Position:
    signal: Signal
    order_id: str  # Bybit parent order
    qty: float
    meta: Optional[dict] = None
