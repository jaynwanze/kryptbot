from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from ..core.clients import build_bybit_client, get_rest
from ..utils.helpers import _sf

router = APIRouter(prefix="/account", tags=["account"])


def _rest_dep():
    # simple local dep; we’ll pull the global on startup
    from fastapi import Request

    def dep(request: Request):
        return request.app.state.rest

    return dep


@router.get("/balance")
async def balance(rest=Depends(get_rest)):
    try:
        resp = await rest.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"})
        print(resp)
        row = resp["result"]["list"][0]

        # per-coin row (prefer USDT), but Bybit sometimes puts '' in fields
        coins = row.get("coin") or []
        usdt = next((c for c in coins if c.get("coin") == "USDT"), {}) or {}

        wallet = _sf(usdt.get("walletBalance", row.get("totalWalletBalance")))
        available = _sf(
            usdt.get(
                "availableToWithdraw",
                usdt.get("availableBalance", row.get("totalAvailableBalance")),
            )
        )
        equity = _sf(usdt.get("equity", row.get("totalEquity")))

        return {
            "walletBalanceUSDT": wallet,
            "availableUSDT": available,
            "equityUSDT": equity,
        }
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
