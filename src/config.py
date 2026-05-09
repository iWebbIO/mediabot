from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


def _load_env() -> None:
    # Keep dotenv loading optional and centralized.
    # If your runtime forbids .env loading, comment out this line.
    load_dotenv()


_load_env()


@dataclass(frozen=True)
class Settings:
    # Telegram
    api_id: int
    api_hash: str
    bot_token: str
    session_name: str

    # App behavior
    ffmpeg_timeout_sec: int = 600

    # Paths
    base_download_dir: str = "bot_downloads"

    # Limits
    max_jobs_global: int = 3
    max_jobs_per_chat_default: int = 1

    # Upload strategy
    upload_strategy: str = "pyrogram"  # pyrogram | mtproto_legacy


def get_settings() -> Settings:
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    session_name = os.getenv("PYROGRAM_SESSION_NAME", "mediabot")

    if not api_id or not api_hash or not bot_token:
        raise RuntimeError(
            "Missing required env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN"
        )

    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        session_name=session_name,
        base_download_dir=os.getenv("BOT_DOWNLOAD_DIR", "bot_downloads"),
        max_jobs_global=int(os.getenv("MAX_JOBS_GLOBAL", "3")),
        max_jobs_per_chat_default=int(os.getenv("MAX_JOBS_PER_CHAT", "1")),
        upload_strategy=os.getenv("UPLOAD_STRATEGY", "pyrogram"),
    )

