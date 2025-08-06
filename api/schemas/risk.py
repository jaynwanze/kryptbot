from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel

class RiskSizeReq(BaseModel):
    symbol: str
    side: Literal["Buy", "Sell"]
    entry: float
    stop: float
    equity_usd: float = 20.0
    risk_pct: float = 0.20
    leverage: Optional[float] = None
    qty_step: Optional[float] = None
    min_qty: Optional[float] = None
    tick_size: Optional[float] = None
    fee_rate_entry: float = 0.00055
    fee_rate_exit: float = 0.00055

class RiskSizeResp(BaseModel):
    qty: float
    qty_risk: float
    qty_budget: float
    notional: float
    est_fees: float
    stop_distance: float
    rr_2to1_tp: float
    min_tick: float

class ATRReq(BaseModel):
    symbol: str
    timeframe: str = "15m"
    period: int = 14
    candles: int = 200
    equity_usd: float = 20.0
    risk_pct: float = 0.2
    rr: float = 2.0
    side: Literal["Buy","Sell"] = "Buy"
    fee_rate_entry: float = 0.00055
    fee_rate_exit: float = 0.00055

class ATRResp(BaseModel):
    atr: float
    entry: float
    stop: float
    tp: float
    qty: float
    est_fees: float
