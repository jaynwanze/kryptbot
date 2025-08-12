from __future__ import annotations
import asyncio, json, time
from typing import Any, Dict, Optional
import httpx

class EngineClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"content-type": "application/json"}
        if api_key:
            self.headers["x-engine-key"] = api_key
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def send_event(self, event: Dict[str, Any], max_retries: int = 5):
        url = f"{self.base_url}/engine/event"
        backoff = 0.25
        for attempt in range(max_retries):
            try:
                r = await self._client.post(url, headers=self.headers, json=event)
                r.raise_for_status()
                return True
            except Exception:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def aclose(self):
        await self._client.aclose()

# sync helper
def send_event_sync(base_url: str, event: Dict[str, Any], api_key: Optional[str] = None):
    c = EngineClient(base_url, api_key)
    try:
        return asyncio.run(c.send_event(event))
    finally:
        asyncio.run(c.aclose())
