# app/routers/engine_router.py
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/engine", tags=["engine"])

# ---- Models -----------------------------------------------------------------

EventType = Literal["heartbeat", "decision", "order", "fill", "tp", "sl", "error"]
DecisionType = Literal["no-trade", "long", "short", "flat"]

class EngineEvent(BaseModel):
    type: EventType
    symbol: str
    ts: datetime = Field(default_factory=datetime.utcnow)

    # Optional strategy telemetry (include what you have; others are fine too)
    decision: Optional[DecisionType] = None
    reason: Optional[str] = None
    k: Optional[float] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    meta: Optional[dict[str, Any]] = None

    # Anything extra that doesn’t fit above
    extra: Optional[dict[str, Any]] = None


# ---- In-memory store + pub/sub ---------------------------------------------

MAX_EVENTS = 2000
_EVENTS: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
_SUBSCRIBERS: set[asyncio.Queue] = set()

def _publish(ev: dict[str, Any]) -> None:
    """Fan-out to all active subscribers and append to ring buffer."""
    _EVENTS.append(ev)
    dead: list[asyncio.Queue] = []
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            # client is too slow; drop it
            dead.append(q)
        except Exception:
            dead.append(q)
    for q in dead:
        _SUBSCRIBERS.discard(q)


# ---- Endpoints --------------------------------------------------------------

@router.post("/event", status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(event: EngineEvent):
    """
    Engine/bot posts events here. Keep it fast (no awaits).
    """
    try:
        # Convert to JSON-friendly dict (datetime -> ISO8601)
        payload = event.model_dump(mode="json")
        _publish(payload)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, f"bad event: {e}")


@router.get("/recent")
async def recent(
    limit: int = Query(200, ge=1, le=MAX_EVENTS),
    symbol: Optional[str] = None,
    types: Optional[str] = Query(
        None,
        description="Comma-separated types to include (e.g. 'heartbeat,decision')",
    ),
):
    """
    Fetch the most recent events for bootstrapping a UI.
    """
    type_set = {t.strip() for t in types.split(",")} if types else None
    data = list(_EVENTS)
    if symbol:
        data = [e for e in data if e.get("symbol") == symbol]
    if type_set:
        data = [e for e in data if e.get("type") in type_set]
    return data[-limit:]


@router.get("/stream")
async def stream(
    request: Request,
    symbols: Optional[str] = Query(
        None, description="Comma-separated symbols to filter (e.g. 'SOLUSDT,ETHUSDT')"
    ),
    types: Optional[str] = Query(
        None, description="Comma-separated event types (e.g. 'heartbeat,decision')"
    ),
    replay: int = Query(
        0, ge=0, le=MAX_EVENTS, description="Send last N events before live stream"
    ),
):
    """
    Live SSE stream. Filters are optional.
    """
    symbol_set = {s.strip() for s in symbols.split(",")} if symbols else None
    type_set = {t.strip() for t in types.split(",")} if types else None

    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _SUBSCRIBERS.add(q)

    async def event_generator():
        try:
            # SSE recommends a retry directive (client reconnection backoff)
            yield "retry: 3000\n\n"

            # Optional replay for instant UI fill
            if replay:
                snapshot = list(_EVENTS)[-replay:]
                for e in snapshot:
                    if symbol_set and e.get("symbol") not in symbol_set:
                        continue
                    if type_set and e.get("type") not in type_set:
                        continue
                    yield f"data: {json.dumps(e)}\n\n"

            # Live stream loop
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    if symbol_set and ev.get("symbol") not in symbol_set:
                        continue
                    if type_set and ev.get("type") not in type_set:
                        continue
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    # comment line = SSE keep-alive; prevents proxies from closing idle conns
                    yield ": keep-alive\n\n"
        finally:
            _SUBSCRIBERS.discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # for nginx
        },
    )
