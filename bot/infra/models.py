from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

@dataclass
class Signal:
    symbol: str
    side: str          # "Buy" or "Sell"
    entry: float
    sl:    float
    tp:    float
    key:   str         # unique id = f"{symbol}-{entry:%H%M}"
    ts:    datetime

@dataclass
class Position:
    signal:  Signal
    order_id:str       # Bybit parent order
    qty:     float
