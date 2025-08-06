from __future__ import annotations
import ccxt.async_support as ccxt
from fastapi import Depends, Request
from .config import settings

def _rest_urls():
    if settings.testnet:
        return {"api": {"public": "https://api-testnet.bybit.com",
                        "private": "https://api-testnet.bybit.com"}}
    return {"api": {"public": "https://api.bybit.com",
                    "private": "https://api.bybit.com"}}

def build_bybit_client() -> ccxt.bybit:
    return ccxt.bybit({
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
        "urls": _rest_urls(),
    })

# FastAPI dependency

def get_rest(request: Request):
    """FastAPI dependency: return the shared ccxt client stored on app.state."""
    return request.app.state.rest