from __future__ import annotations
import time
from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])

@router.get("")
async def health():
    return {"ok": True, "ts": int(time.time())}
