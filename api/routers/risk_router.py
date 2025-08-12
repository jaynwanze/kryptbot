from __future__ import annotations
import math
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from ..core.clients import get_rest
from ..core.config import settings
from ..schemas import RiskSizeReq, RiskSizeResp, ATRReq, ATRResp

router = APIRouter(prefix="/risk", tags=["risk"])

# ---------- helpers ----------


async def _symbol_meta(rest, symbol: str):
    """
    Pull tick size, qty step, min qty from Bybit v5.
    """
    res = await rest.public_get_v5_market_instruments_info(
        {"category": "linear", "symbol": symbol}
    )
    info = (res["result"]["list"] or [])[0]
    tick = float(info["priceFilter"]["tickSize"])
    step = float(info["lotSizeFilter"]["qtyStep"])
    minq = float(info["lotSizeFilter"]["minOrderQty"])
    return tick, step, minq


def _round_tick(x: float, tick: float) -> float:
    return round(x / tick) * tick if tick else x


def _floor_step(x: float, step: float) -> float:
    if step == 0:
        return x
    return math.floor(x / step) * step


# ---------- /risk/size ----------


@router.post("/size", response_model=RiskSizeResp)
async def risk_size(req: RiskSizeReq, rest=Depends(get_rest)):
    try:
        # symbol meta (allow client overrides)
        if req.tick_size and req.qty_step and req.min_qty:
            tick, step, minq = req.tick_size, req.qty_step, req.min_qty
        else:
            tick, step, minq = await _symbol_meta(rest, req.symbol)

        stop_distance = abs(req.entry - req.stop)
        if stop_distance <= 0:
            raise HTTPException(400, "stop must differ from entry")

        # risk- and budget-based quantities
        risk_usd = req.equity_usd * req.risk_pct
        qty_risk = risk_usd / stop_distance
        lev = req.leverage or settings.leverage_default
        qty_budget = (req.equity_usd * lev) / req.entry

        qty = min(qty_risk, qty_budget)
        qty = max(_floor_step(qty, step), minq)

        notional = qty * req.entry
        est_fees = notional * (req.fee_rate_entry + req.fee_rate_exit)
        rr_2to1 = (
            req.entry + 2 * stop_distance
            if req.side == "Buy"
            else req.entry - 2 * stop_distance
        )
        rr_2to1 = _round_tick(rr_2to1, tick)

        return RiskSizeResp(
            qty=qty,
            qty_risk=_floor_step(qty_risk, step),
            qty_budget=_floor_step(qty_budget, step),
            notional=notional,
            est_fees=est_fees,
            stop_distance=stop_distance,
            rr_2to1_tp=rr_2to1,
            min_tick=tick,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"risk size failed: {e}")


# ---------- /risk/atr ----------


@router.post("/atr-plan", response_model=ATRResp)
async def atr_plan(req: ATRReq, rest=Depends(get_rest)):
    """
    Pull candles, compute ATR(period), propose entry/SL/TP at 1×ATR (configurable if you want later).
    """
    try:
        # ohlcv
        bars = await rest.fetch_ohlcv(
            req.symbol,
            timeframe=req.timeframe,
            limit=max(req.candles, req.period + 5),
            params={"category": "linear"},
        )
        df = pd.DataFrame(bars, columns=["ts", "o", "h", "l", "c", "v"])
        df["prev_c"] = df["c"].shift(1)
        tr = (df["h"] - df["l"]).to_frame("a")
        tr["b"] = (df["h"] - df["prev_c"]).abs()
        tr["c"] = (df["l"] - df["prev_c"]).abs()
        df["tr"] = tr.max(axis=1)
        atr = float(df["tr"].ewm(alpha=1 / req.period, adjust=False).mean().iloc[-1])

        # symbol meta for rounding
        tick, step, _ = await _symbol_meta(rest, req.symbol)

        entry = float(df["c"].iloc[-1])
        if req.side == "Buy":
            stop = _round_tick(entry - atr, tick)
            tp = _round_tick(entry + req.rr * atr, tick)
        else:
            stop = _round_tick(entry + atr, tick)
            tp = _round_tick(entry - req.rr * atr, tick)

        stop_distance = abs(entry - stop)
        qty = (req.equity_usd * req.risk_pct) / max(stop_distance, 1e-12)
        qty = max(_floor_step(qty, step), step)

        est_fees = (req.fee_rate_entry + req.fee_rate_exit) * entry * qty

        return ATRResp(
            atr=atr,
            entry=_round_tick(entry, tick),
            stop=stop,
            tp=tp,
            qty=qty,
            est_fees=est_fees,
        )
    except Exception as e:
        raise HTTPException(500, f"atr plan failed: {e}")
