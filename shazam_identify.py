#!/usr/bin/env python3.13
"""Run song recognition on a local audio file and print a Shazam-like JSON result."""
import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from shazamio import Shazam
from imageio_ffmpeg import get_ffmpeg_exe

BASE_DIR = Path(__file__).resolve().parent
ACRCLOUD_CONFIG_CANDIDATES = (
    BASE_DIR / "acrcloud.json",
    BASE_DIR / ".acrcloud.json",
)

FFMPEG_EXE = get_ffmpeg_exe()

def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _load_acrcloud_config() -> dict | None:
    cfg = {}
    for path in ACRCLOUD_CONFIG_CANDIDATES:
        if path.exists():
            cfg = _load_json(path)
            if cfg:
                break

    host = (
        os.getenv("ACRCLOUD_HOST")
        or os.getenv("ACR_HOST")
        or cfg.get("host")
        or ""
    ).strip()
    access_key = (
        os.getenv("ACRCLOUD_ACCESS_KEY")
        or os.getenv("ACR_ACCESS_KEY")
        or cfg.get("access_key")
        or ""
    ).strip()
    access_secret = (
        os.getenv("ACRCLOUD_ACCESS_SECRET")
        or os.getenv("ACR_ACCESS_SECRET")
        or cfg.get("access_secret")
        or ""
    ).strip()

    if not (host and access_key and access_secret):
        return None
    return {
        "host": host.removeprefix("https://").removeprefix("http://").rstrip("/"),
        "access_key": access_key,
        "access_secret": access_secret,
    }


def _build_multipart(fields: dict[str, str], file_field: str, file_name: str, payload: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----codex-acrcloud-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ]
        )

    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_name}"\r\n'
            ).encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def _trim_sample_for_acrcloud(src_path: str) -> tuple[bytes, str, str]:
    """ACRCloud recommends short clips; produce a compact 12s mono MP3 sample."""
    fd, tmp_path = tempfile.mkstemp(prefix="acr_sample_", suffix=".mp3")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                FFMPEG_EXE, "-y",
                "-i", src_path,
                "-t", "12",
                "-ac", "1",
                "-ar", "16000",
                "-codec:a", "libmp3lame",
                "-b:a", "64k",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and os.path.exists(tmp_path):
            return Path(tmp_path).read_bytes(), Path(tmp_path).name, "audio/mpeg"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    path = Path(src_path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.read_bytes(), path.name, mime


def _normalize_acrcloud_track(item: dict, bucket: str) -> dict | None:
    title = (item or {}).get("title") or ""
    artists = item.get("artists") or []
    artist = artists[0].get("name", "") if artists else ""
    if not title:
        return None

    album = ((item.get("album") or {}).get("name") or "").strip()
    release_date = (item.get("release_date") or "").strip()
    genres = item.get("genres") or []
    genre = ""
    if genres:
        first_genre = genres[0]
        if isinstance(first_genre, dict):
            genre = (first_genre.get("name") or "").strip()
        else:
            genre = str(first_genre).strip()
    score = item.get("score")

    metadata = []
    if album:
        metadata.append({"title": "Album", "text": album})
    if release_date:
        metadata.append({"title": "Released", "text": release_date})
    if score not in (None, ""):
        metadata.append({"title": "Score", "text": str(score)})
    metadata.append({"title": "Matched By", "text": f"ACRCloud {bucket.title()}"})

    track = {
        "title": title,
        "subtitle": artist or "Unknown artist",
        "sections": [{"type": "SONG", "metadata": metadata}],
        "genres": {"primary": genre},
    }
    return track


def _acrcloud_identify(path: str) -> dict | None:
    config = _load_acrcloud_config()
    if not config:
        return None

    sample, sample_name, mime = _trim_sample_for_acrcloud(path)
    if not sample:
        return None

    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(int(time.time()))
    string_to_sign = "\n".join(
        [
            http_method,
            http_uri,
            config["access_key"],
            data_type,
            signature_version,
            timestamp,
        ]
    )
    signature = base64.b64encode(
        hmac.new(
            config["access_secret"].encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha1,
        ).digest()
    ).decode("ascii")

    fields = {
        "access_key": config["access_key"],
        "data_type": data_type,
        "signature_version": signature_version,
        "signature": signature,
        "sample_bytes": str(len(sample)),
        "timestamp": timestamp,
    }
    body, boundary = _build_multipart(fields, "sample", sample_name, sample, mime)
    request = urllib.request.Request(
        f"https://{config['host']}{http_uri}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    status = payload.get("status") or {}
    if status.get("code") != 0:
        return None

    metadata = payload.get("metadata") or {}
    for bucket in ("humming", "music"):
        matches = metadata.get(bucket) or []
        if matches:
            return _normalize_acrcloud_track(matches[0], bucket)
    return None


async def _shazam_identify(path: str) -> dict | None:
    shazam = Shazam()
    out = await shazam.recognize(path)
    matches = out.get("matches", [])
    if not matches:
        return None
    track = out.get("track", {})
    return track or None


async def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no file provided"}))
        sys.exit(1)

    path = sys.argv[1]

    try:
        track = await asyncio.to_thread(_acrcloud_identify, path)
        if not track:
            track = await _shazam_identify(path)
        if not track:
            print(json.dumps({"error": "no match"}))
            sys.exit(0)
        print(json.dumps(track))
    except Exception:
        print(json.dumps({"error": "recognition failed"}))
        sys.exit(0)


asyncio.run(main())
