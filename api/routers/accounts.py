from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from fastapi.params import Query
from ..core.clients import build_bybit_client, get_rest
from ..utils.helpers import _sf
from datetime import datetime, timezone, timedelta
from collections import defaultdict

router = APIRouter(prefix="/account", tags=["account"])


def _rest_dep():
    # simple local dep; we’ll pull the global on startup
    from fastapi import Request

    def dep(request: Request):
        return request.app.state.rest

    return dep


@router.get("/pnl")
async def pnl(
    symbol: str | None = None,
    days: int = Query(30, ge=1, le=180),
    rest=Depends(get_rest),
):
    """
    Realized PnL history from executions, grouped by day (UTC).
    Returns: { days: [{day, realizedPnl, fees, net, cumNet, trades}], totals: {...} }
    """
    try:
        end = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        start = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000
        )

        cursor = None
        items: list[dict] = []
        while True:
            params = {
                "category": "linear",
                "startTime": start,
                "endTime": end,
                "limit": 1000,
            }
            if symbol:
                params["symbol"] = symbol
            if cursor:
                params["cursor"] = cursor

            resp = await rest.private_get_v5_execution_list(params)
            result = resp.get("result") or {}
            batch = result.get("list") or []
            items.extend(batch)

            cursor = result.get("nextPageCursor")
            if not cursor or not batch:
                break

        # group by UTC day
        by_day = defaultdict(lambda: {"realizedPnl": 0.0, "fees": 0.0, "trades": 0})
        for e in items:
            ts_ms = int(e.get("execTime") or e.get("execTimeMs") or 0)
            if not ts_ms:
                continue
            day = datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
            pnl = _sf(e.get("execPnl", 0))
            fee = _sf(e.get("execFee", 0))
            by_day[day]["realizedPnl"] += pnl
            by_day[day]["fees"] += fee
            by_day[day]["trades"] += 1

        days_sorted = sorted(by_day.items(), key=lambda x: x[0])
        out = []
        cum = 0.0
        for day, vals in days_sorted:
            net = vals["realizedPnl"] - vals["fees"]
            cum += net
            out.append(
                {
                    "day": day,
                    "realizedPnl": round(vals["realizedPnl"], 6),
                    "fees": round(vals["fees"], 6),
                    "net": round(net, 6),
                    "cumNet": round(cum, 6),
                    "trades": vals["trades"],
                }
            )

        totals = {
            "days": len(out),
            "trades": sum(d["trades"] for d in out),
            "realizedPnl": round(sum(d["realizedPnl"] for d in out), 6),
            "fees": round(sum(d["fees"] for d in out), 6),
            "net": round(sum(d["net"] for d in out), 6),
            "cumNet": round(out[-1]["cumNet"], 6) if out else 0.0,
        }
        return {"days": out, "totals": totals}
    except Exception as e:
        raise HTTPException(500, f"pnl failed: {e}")


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
