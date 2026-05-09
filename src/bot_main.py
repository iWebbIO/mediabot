#!/usr/bin/env python3

"""Mediabot production entrypoint (Pyrogram-first).

This file is intentionally minimal until the migration is completed.
"""

from __future__ import annotations

import logging

from .telegram.pyrogram_bot import main


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    setup_logging()
    main()

