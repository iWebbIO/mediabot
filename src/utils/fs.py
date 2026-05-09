from __future__ import annotations

import os
from pathlib import Path

import uuid


def size_str(path: str) -> str:
    mb = os.path.getsize(path) / 1024 / 1024
    return f"{mb:.1f} MB"


def new_workdir(base_download_dir: Path) -> Path:
    d = base_download_dir / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    return d

