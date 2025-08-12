from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from ..core.clients import get_rest

router = APIRouter(prefix="/market", tags=["market"])

@router.get("/klines")
async def klines(
    symbol: str,
    timeframe: str = Query("15m"),
    limit: int = Query(200, ge=1, le=1000),
    rest = Depends(get_rest),
):
    """
    Return OHLCV from Bybit (USDT perps).
    """
    try:
        rows = await rest.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, params={"category": "linear"})
        # ccxt returns [ts, o, h, l, c, v]
        return [
            {"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, f"klines failed: {e}")

@router.get("/ticker")
async def ticker(symbol: str, rest = Depends(get_rest)):
    try:
        res = await rest.public_get_v5_market_tickers({"category": "linear", "symbol": symbol})
        it = (res["result"]["list"] or [])[0]
        return {
            "symbol": symbol,
            "last": float(it["lastPrice"]),
            "mark": float(it.get("markPrice") or it["lastPrice"]),
            "index": float(it.get("indexPrice") or it["lastPrice"]),
            "change24h": float(it.get("price24hPcnt") or 0.0),
            "turnover24h": float(it.get("turnover24h") or 0.0),
        }
    except Exception as e:
        raise HTTPException(500, f"ticker failed: {e}")
