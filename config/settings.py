"""Binance Arbitrage Trading Bot - Configuration"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
import os


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Binance API
    binance_api_key: str = Field(default="", description="Binance API key")
    binance_api_secret: str = Field(default="", description="Binance API secret")
    binance_testnet: bool = Field(default=True, description="Use testnet")

    # Trading
    trade_amount_usdt: float = Field(default=100.0, description="Trade amount in USDT")
    max_open_orders: int = Field(default=5, description="Max concurrent open orders")
    min_profit_threshold: float = Field(
        default=0.001, description="Min profit ratio to trigger trade (0.1%)"
    )

    # Risk Management
    max_drawdown_pct: float = Field(default=5.0, description="Max drawdown percentage")
    daily_loss_limit_usdt: float = Field(
        default=50.0, description="Daily loss limit in USDT"
    )
    position_size_pct: float = Field(
        default=10.0, description="Position size as % of portfolio"
    )

    # Logging
    log_level: str = Field(default="INFO", description="Log level")
    log_file: str = Field(default="logs/bot.log", description="Log file path")

    # Performance
    price_update_interval: float = Field(
        default=0.1, description="Price update interval in seconds"
    )
    order_timeout: float = Field(
        default=5.0, description="Order execution timeout in seconds"
    )
    scan_interval: float = Field(
        default=0.5, description="Arbitrage scan interval in seconds"
    )
    api_rate_limit: int = Field(
        default=18, description="Max API calls per second (Binance limit: 20/s)"
    )
    ws_reconnect_delay: float = Field(
        default=2.0, description="WebSocket reconnect delay in seconds"
    )
    recv_window: int = Field(
        default=5000, description="Binance recvWindow parameter (ms)"
    )

    # Triangular Arbitrage
    base_currencies: list[str] = Field(
        default=["BTC", "ETH", "BNB"],
        description="Base currencies for triangular arbitrage",
    )
    quote_currency: str = Field(
        default="USDT", description="Quote currency for triangular arbitrage"
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Global settings singleton
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
