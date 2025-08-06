from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from ..core.clients import build_bybit_client,get_rest

router = APIRouter(prefix="/account", tags=["account"])


def _rest_dep():
    # simple local dep; we’ll pull the global on startup
    from fastapi import Request

    def dep(request: Request):
        return request.app.state.rest

    return dep


@router.get("/balance")
async def balance(rest = Depends(get_rest)):
    try:
        resp = await rest.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"})
        print(resp)
        row = resp["result"]["list"][0]
        coins = row.get("coin", [])
        usdt = next(
            (
                float(c.get("walletBalance", 0))
                for c in coins
                if c.get("coin") == "USDT"
            ),
            0.0,
        )
        avail = next(
            (
                float(c.get("availableToWithdraw", 0))
                for c in coins
                if c.get("coin") == "USDT"
            ),
            0.0,
        )
        return {"walletBalanceUSDT": usdt, "availableUSDT": avail}
    except Exception as e:
        raise HTTPException(500, f"balance failed: {e}")


@router.get("/positions")
async def positions(symbol: str | None = None, rest=Depends(get_rest)):
    try:
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        resp = await rest.private_get_v5_position_list(params)
        return resp["result"]["list"]
    except Exception as e:
        raise HTTPException(500, f"positions failed: {e}")


@router.get("/orders")
async def orders(symbol: str, limit: int = 50, rest=Depends(get_rest)):
    try:
        resp = await rest.private_get_v5_order_history(
            {"category": "linear", "symbol": symbol, "limit": limit}
        )
        return resp["result"]["list"]
    except Exception as e:
        raise HTTPException(500, f"orders failed: {e}")


@router.get("/executions")
async def executions(symbol: str, limit: int = 100, rest=Depends(get_rest)):
    try:
        resp = await rest.private_get_v5_execution_list(
            {"category": "linear", "symbol": symbol, "limit": limit}
        )
        return resp["result"]["list"]
    except Exception as e:
        raise HTTPException(500, f"executions failed: {e}")
