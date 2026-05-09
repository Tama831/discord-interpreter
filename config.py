"""環境変数の読み込みと型変換を一元化。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    gemini_api_key: str
    gemini_model: str
    guild_id: int | None
    chunk_max_seconds: float
    silence_timeout_seconds: float
    daily_budget_usd: float
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN が未設定です (.env を確認)")
        if not key:
            raise RuntimeError("GEMINI_API_KEY が未設定です (.env を確認)")
        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        return cls(
            discord_bot_token=token,
            gemini_api_key=key,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            guild_id=int(guild_raw) if guild_raw else None,
            chunk_max_seconds=float(os.getenv("CHUNK_MAX_SECONDS", "8.0")),
            silence_timeout_seconds=float(os.getenv("SILENCE_TIMEOUT_SECONDS", "0.8")),
            daily_budget_usd=float(os.getenv("DAILY_BUDGET_USD", "2.0")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
