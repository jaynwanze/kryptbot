from __future__ import annotations
import os
from typing import List
from pydantic_settings import BaseSettings
from pydantic import Field, AliasChoices
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # accept either BYBIT_API_KEY or BYBIT_KEY, same for secret
    bybit_api_key: str = Field(
        default="", validation_alias=AliasChoices("BYBIT_API_KEY", "BYBIT_KEY")
    )
    bybit_api_secret: str = Field(
        default="", validation_alias=AliasChoices("BYBIT_API_SECRET", "BYBIT_SECRET")
    )

    # also allow TESTNET / BYBIT_TESTNET
    testnet: bool = Field(
        default=False, validation_alias=AliasChoices("TESTNET", "BYBIT_TESTNET")
    )
    # defaults; tune for your account
    taker_fee: float = 0.00055
    maker_fee: float = 0.00010
    leverage_default: float = 10.0

    cors_origins: List[str] = ["*"]


settings = Settings()
