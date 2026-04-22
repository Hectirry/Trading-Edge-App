from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEA_", case_sensitive=False, extra="ignore")

    trading_env: Literal["dev", "staging", "production"] = "staging"
    hostname: str = "localhost"

    pg_host: str = "tea-postgres"
    pg_port: int = 5432
    pg_db: str = "trading_edge"
    pg_user: str = "tea"
    pg_password: str = ""

    redis_host: str = "tea-redis"
    redis_port: int = 6379

    binance_api_key: str = ""
    binance_api_secret: str = ""
    bybit_api_key: str = ""
    bybit_api_secret: str = ""

    polymarket_gamma_api: str = "https://gamma-api.polymarket.com"
    polymarket_data_api: str = "https://data-api.polymarket.com"
    polymarket_clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    metrics_port: int = Field(
        default=9000, description="Prometheus metrics port, internal net only"
    )

    api_token: str = Field(default="", description="X-TEA-Token auth for tea-api")
    api_base_url: str = Field(
        default="http://tea-api:8000",
        description="Internal base URL for tea-api; telegram bot uses this to dispatch",
    )
    dashboard_public_url: str = Field(
        default="https://187-124-130-221.nip.io",
        description="Public base URL the bot uses when linking back to /research/<id>",
    )
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_authorized_users: str = ""  # CSV of user_ids

    @property
    def pg_dsn(self) -> str:
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
