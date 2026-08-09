"""Application settings, loaded from environment / ``.env``.

Non-secret configuration lives here. Secret *values* (API keys, HMAC secrets) are
read through :mod:`config.secrets`, not stored on this object, so they never end
up in logs or serialized settings dumps.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict

from domain.modes import TradingMode


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class BinanceEnv(StrEnum):
    """Which Binance environment every venue call and every stored row belongs to.

    This is not a convenience flag. Binance reuses symbols across testnet and
    production — ``BTCUSDT`` exists in both — so an unscoped row is ambiguous and
    a mixed series is silently corrupt. Every market-keyed table carries this
    value and every symbol-resolving read filters on it (ADR-0010).
    """

    TESTNET = "testnet"
    PRODUCTION = "production"


class RawStoreBackend(StrEnum):
    """Where raw API payloads are retained. Local is the default everywhere."""

    LOCAL = "local"
    S3 = "s3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: AppEnv = AppEnv.LOCAL
    trading_mode: TradingMode = TradingMode.RESEARCH
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://crypto:crypto@localhost:5433/crypto"
    redis_url: str = "redis://localhost:6380/1"

    # Which Binance environment the venue adapter targets and every row is scoped
    # to. Credentials themselves are read via the SecretProvider, never here.
    binance_env: BinanceEnv = BinanceEnv.TESTNET
    binance_api_key_id: str = ""

    # Raw-payload retention. Defaults keep a local checkout working with no
    # configuration; a deployed runtime sets the backend and bucket via env.
    raw_store_backend: RawStoreBackend = RawStoreBackend.LOCAL
    raw_store_local_dir: str = "data/raw"
    raw_store_s3_bucket: str = ""
    raw_store_s3_prefix: str = ""
    aws_region: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION


def load_settings() -> Settings:
    """Load settings from the environment. Kept as a function so tests can build
    ``Settings`` with explicit values instead of relying on process state."""
    return Settings()
