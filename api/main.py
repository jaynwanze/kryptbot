from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.config import settings
from .core.clients import build_bybit_client
from .routers import health_router, account_router, risk_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.rest = build_bybit_client()
    print(f"[startup] testnet={settings.testnet} key_loaded={'yes' if settings.bybit_api_key else 'no'}")
    yield
    await app.state.rest.close()

app = FastAPI(title="KryptBot API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# mount routers
app.include_router(health_router)
app.include_router(account_router)
app.include_router(risk_router)
