"""
Application settings loaded from .env file.

Usage:
    from config import load_settings
    settings = load_settings()
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Settings:
    # CoinDCX (order execution only)
    coindcx_api_key:    str
    coindcx_api_secret: str

    # Telegram bot
    telegram_bot_token: str
    telegram_chat_id:   str

    # Default symbol/interval for the dashboard
    symbol:   str = "BTCUSDT"
    interval: str = "1m"

    # SQLite database path
    db_path: str = "data/trading.db"

    # Dashboard port
    port: int = 8000

    # Trading phase: 1=manual alerts, 2=semi-auto approval, 3=full auto
    trading_phase: int = 1

    # Risk management (used in Phase 2+)
    max_position_pct: float = 0.02    # 2% of USDT balance per trade
    stop_loss_pct:    float = 0.015   # 1.5% stop-loss
    take_profit_pct:  float = 0.030   # 3% take-profit


def load_settings() -> Settings:
    """Load settings from .env file and environment variables."""
    load_dotenv()

    def _require(key: str) -> str:
        val = os.getenv(key, "").strip()
        if not val:
            raise ValueError(
                f"Missing required environment variable: {key}\n"
                f"Copy .env.example to .env and fill in your credentials."
            )
        return val

    def _optional(key: str, default: str) -> str:
        return os.getenv(key, default).strip() or default

    return Settings(
        coindcx_api_key=_require("COINDCX_API_KEY"),
        coindcx_api_secret=_require("COINDCX_API_SECRET"),
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
        symbol=_optional("SYMBOL", "BTCUSDT"),
        interval=_optional("INTERVAL", "1m"),
        db_path=_optional("DB_PATH", "data/trading.db"),
        port=int(_optional("PORT", "8000")),
        trading_phase=int(_optional("TRADING_PHASE", "1")),
        max_position_pct=float(_optional("MAX_POSITION_PCT", "0.02")),
        stop_loss_pct=float(_optional("STOP_LOSS_PCT", "0.015")),
        take_profit_pct=float(_optional("TAKE_PROFIT_PCT", "0.030")),
    )
