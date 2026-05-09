from __future__ import annotations

import logging

from pyrogram import Client

from ..config import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()

    app = Client(
        name=settings.session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        in_memory=True,
    )

    # Handlers will be added in subsequent steps.
    logger.info("Pyrogram client started (handlers pending).")
    app.run() 

