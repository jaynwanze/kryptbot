# infra/bybit_client.py
from pybit.unified_trading import HTTP, WebSocket
import os, time, hmac, hashlib
import logging

def get_client(testnet: bool = True) -> HTTP:
    key = (os.getenv("BYBIT_KEY_TEST" if testnet else "BYBIT_KEY") or "").strip()
    sec = (os.getenv("BYBIT_SECRET_TEST" if testnet else "BYBIT_SECRET") or "").strip()
    logging.info("HTTP testnet=%s key=***%s", testnet, key[-4:])
    return HTTP(testnet=testnet, api_key=key, api_secret=sec, recv_window=5000)

def get_private_ws(testnet: bool = True) -> WebSocket:
    key = (os.getenv("BYBIT_KEY_TEST" if testnet else "BYBIT_KEY") or "").strip()
    sec = (os.getenv("BYBIT_SECRET_TEST" if testnet else "BYBIT_SECRET") or "").strip()
    logging.info("WS   testnet=%s key=***%s", testnet, key[-4:])
    return WebSocket(
        testnet=testnet,
        channel_type="private",
        api_key=key,
        api_secret=sec,
        ping_interval=20,
    )
