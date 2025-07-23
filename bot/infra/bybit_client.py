# infra/bybit_client.py
from pybit.unified_trading import HTTP, WebSocket
import os, time, hmac, hashlib

def get_client(testnet: bool = True) -> HTTP:
    key = os.getenv("BYBIT_SECRET_TEST" if testnet else "BYBIT_KEY")
    sec = os.getenv("BYBIT_SECRET_TEST" if testnet else "BYBIT_SEC")
    return HTTP(
        testnet=testnet,
        api_key=key,
        api_secret=sec,
        recv_window=5000,
    )

def get_private_ws(testnet: bool = True) -> WebSocket:
    return WebSocket(
        testnet=testnet,
        channel_type="private",
        api_key=os.getenv("BYBIT_KEY_TEST" if testnet else "BYBIT_KEY"),
        api_secret=os.getenv("BYBIT_SECRET_TEST" if testnet else "BYBIT_SECRET"),
        ping_interval=20,
    )
