#!/usr/bin/env python3
"""
Telegram Media Bot
- Download from YouTube, YouTube Music, SoundCloud, Spotify, Deezer,
  Pinterest, TikTok, Instagram, Twitter/X, and 1000+ sites via yt-dlp
- Compress/convert uploaded audio or video files
- Inline button UI for format, quality, and speed selection
- Playlist support for YouTube and Spotify
- SoundCloud search, file merge, bot stats
- Downloads saved to ~/Documents/bot_downloads/, deleted after verified upload
"""

import io
import os
import re
import json
import uuid
import math
import shutil
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from imageio_ffmpeg import get_ffmpeg_exe

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Global ffmpeg path for container compatibility
FFMPEG_EXE = get_ffmpeg_exe()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DOWNLOAD_DIR = Path.home() / "Documents" / "bot_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATS_FILE = DOWNLOAD_DIR / "stats.json"

# Active jobs counter (asyncio is single-threaded, no lock needed)
active_jobs = 0
MAX_JOBS = 3

# Platforms where only audio makes sense (skip video option)
AUDIO_ONLY_DOMAINS = {
    "spotify.com", "open.spotify.com",
    "soundcloud.com", "m.soundcloud.com",
    "deezer.com", "www.deezer.com",
    "music.apple.com",
    "tidal.com",
}

AUDIO_QUALITIES = [("24 kbps", "24"), ("64 kbps", "64"), ("128 kbps", "128"),
                   ("192 kbps", "192"), ("320 kbps", "320")]
VIDEO_QUALITIES = [("360p", "360"), ("480p", "480"),
                   ("720p", "720"), ("1080p", "1080"), ("4K", "2160")]
SPEED_OPTIONS   = [("0.5×", "0.5"), ("0.75×", "0.75"), ("1×", "1.0"),
                   ("1.25×", "1.25"), ("1.5×", "1.5"), ("2×", "2.0")]
COMPRESS_OPTIONS = [
    ("None",   "none"),
    ("Light",  "light"),    # ~128 kbps + loudnorm  (~50% smaller vs 320)
    ("Medium", "medium"),   # ~80 kbps  + loudnorm
    ("Heavy",  "heavy"),    # ~48 kbps  + loudnorm  (max size cut)
]

# Image output formats: key → (label, file_ext, ffmpeg_extra_args)
IMAGE_FORMATS = {
    "jpg":  ("JPG",  "jpg",  ["-q:v", "2"]),
    "png":  ("PNG",  "png",  ["-update", "1"]),
    "webp": ("WEBP", "webp", ["-q:v", "90", "-update", "1"]),
    "gif":  ("GIF",  "gif",  []),
    "bmp":  ("BMP",  "bmp",  ["-update", "1"]),
    "tiff": ("TIFF", "tiff", ["-update", "1"]),
}

# Audio output formats: key → (label, ffmpeg_codec, lossy, file_ext)
AUDIO_FORMATS = {
    "mp3":  ("MP3",  "libmp3lame", True,  "mp3"),
    "aac":  ("AAC",  "aac",        True,  "m4a"),
    "ogg":  ("OGG",  "libvorbis",  True,  "ogg"),
    "opus": ("OPUS", "libopus",    True,  "opus"),
    "m4a":  ("M4A",  "aac",        True,  "m4a"),
    "flac": ("FLAC", "flac",       False, "flac"),
    "wav":  ("WAV",  "pcm_s16le",  False, "wav"),
}

# Video output formats: key → (label, vcodec, acodec, file_ext)
VIDEO_FORMATS = {
    "mp4":  ("MP4",  "libx264",    "aac",        "mp4"),
    "mkv":  ("MKV",  "libx264",    "aac",        "mkv"),
    "mov":  ("MOV",  "libx264",    "aac",        "mov"),
    "webm": ("WEBM", "libvpx-vp9", "libopus",    "webm"),
    "avi":  ("AVI",  "libxvid",    "libmp3lame", "avi"),
}


# ── helpers ────────────────────────────────────────────────────────────────────

def extract_urls(text: str) -> list[str]:
    return re.findall(r'https?://[^\s]+', text or "")


def domain_of(url: str) -> str:
    m = re.search(r'https?://([^/]+)', url)
    return m.group(1).lstrip("www.") if m else ""


def is_audio_only_platform(url: str) -> bool:
    d = domain_of(url)
    return any(d == ao or d.endswith("." + ao) for ao in AUDIO_ONLY_DOMAINS)


def is_youtube_playlist(url: str) -> bool:
    return "youtube.com" in url and "list=" in url and "watch?v=" not in url


def is_spotify_playlist(url: str) -> bool:
    return "spotify.com" in url and any(x in url for x in ("/playlist/", "/album/", "/artist/"))


def is_soundcloud_playlist(url: str) -> bool:
    return "soundcloud.com" in url and "/sets/" in url


def run_ffmpeg(args: list[str], timeout: int = 600) -> tuple[bool, str]:
    result = subprocess.run(
        [FFMPEG_EXE, "-y"] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode == 0:
        return True, ""
    lines = result.stderr.splitlines()
    # Strip ffmpeg header/config lines (they start with spaces or contain build flags)
    skip_prefixes = (" ", "\t")
    skip_keywords = ("configuration:", "built with", "copyright", "--enable", "--disable",
                     "libav", "ffmpeg version")
    useful = [
        l for l in lines
        if l.strip()
        and not l.startswith(skip_prefixes)
        and not any(kw in l.lower() for kw in skip_keywords)
    ]
    error_kws = ("error", "invalid", "failed", "no such", "unknown",
                 "not found", "unable", "codec", "does not", "no video", "no audio")
    error_lines = [l for l in useful if any(kw in l.lower() for kw in error_kws)]
    msg = "\n".join(error_lines[-15:]) if error_lines else "\n".join(useful[-15:])
    return False, msg or result.stderr[-300:]


def _relaxed_input_flags() -> list[str]:
    """Extra ffmpeg input flags for problematic/partially-corrupted files."""
    return ["-analyzeduration", "200M", "-probesize", "200M", "-err_detect", "ignore_err"]


def convert_audio(src: str, dst: str, fmt_key: str, kbps: str = "128") -> tuple[bool, str]:
    _, codec, lossy, _ = AUDIO_FORMATS[fmt_key]
    # opus requires 48000 Hz; everything else works fine at 44100
    sample_rate = "48000" if fmt_key == "opus" else "44100"
    args = ["-i", src, "-vn", "-acodec", codec]
    if lossy:
        if fmt_key == "opus":
            # libopus uses -b:a; VBR is on by default and works well
            args += ["-b:a", f"{kbps}k", "-vbr", "on", "-compression_level", "10"]
        elif fmt_key in ("ogg",):
            args += ["-b:a", f"{kbps}k", "-q:a", "-1"]
        else:
            args += ["-b:a", f"{kbps}k"]
    args += ["-ar", sample_rate, dst]
    ok, err = run_ffmpeg(args)
    if not ok:
        ok, err = run_ffmpeg(_relaxed_input_flags() + args)
    return ok, err


def convert_video(src: str, dst: str, fmt_key: str, height: str) -> tuple[bool, str]:
    _, vcodec, acodec, _ = VIDEO_FORMATS[fmt_key]
    extra = ["-crf", "30", "-b:v", "0"] if vcodec == "libvpx-vp9" else ["-crf", "23", "-preset", "fast"]
    base_args = [
        "-i", src,
        "-vf", f"scale=-2:{height}",
        "-c:v", vcodec, *extra,
        "-c:a", acodec, "-b:a", "128k",
        dst,
    ]
    ok, err = run_ffmpeg(base_args)
    if not ok:
        ok, err = run_ffmpeg(_relaxed_input_flags() + base_args)
    return ok, err


def compress_audio_sync(src: str, dst: str, level: str) -> tuple[bool, str]:
    """Reduce file size by re-encoding to opus at a lower bitrate (single pass, fast)."""
    if level == "none":
        shutil.copy2(src, dst)
        return True, ""
    bitrates = {"light": "128", "medium": "80", "heavy": "48"}
    kbps = bitrates.get(level, "80")
    args = [
        "-i", src,
        "-vn",
        "-acodec", "libopus",
        "-b:a", f"{kbps}k",
        "-vbr", "on",
        "-compression_level", "5",
        "-ar", "48000",
        dst,
    ]
    ok, err = run_ffmpeg(args)
    if not ok:
        ok, err = run_ffmpeg(_relaxed_input_flags() + args)
    return ok, err


def apply_speed_sync(src: str, dst: str, speed: str) -> tuple[bool, str]:
    """Apply playback speed to audio using atempo filter."""
    spf = float(speed)
    if spf == 1.0:
        shutil.copy2(src, dst)
        return True, ""
    return run_ffmpeg(["-i", src, "-filter:a", f"atempo={spf}", "-vn", dst])


def merge_audio_sync(files: list[str], dst: str) -> tuple[bool, str]:
    """Concatenate audio files using ffmpeg concat demuxer (stream copy)."""
    list_path = dst + "_list.txt"
    try:
        with open(list_path, "w") as f:
            for fp in files:
                f.write(f"file '{fp}'\n")
        return run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", dst])
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


def convert_image_sync(src: str, dst: str, fmt_key: str) -> tuple[bool, str]:
    _, _, extra = IMAGE_FORMATS[fmt_key]
    return run_ffmpeg(["-i", src] + extra + [dst])


def size_str(path: str) -> str:
    mb = os.path.getsize(path) / 1024 / 1024
    return f"{mb:.1f} MB"


def new_workdir() -> Path:
    d = DOWNLOAD_DIR / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── stats ──────────────────────────────────────────────────────────────────────

def stats_load() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            pass
    return {"downloads": 0, "files_processed": 0, "mb_downloaded": 0.0, "mb_saved": 0.0}


def stats_add(downloads: int = 0, files_proc: int = 0,
              mb_dl: float = 0.0, mb_saved: float = 0.0):
    s = stats_load()
    s["downloads"]      = s.get("downloads", 0)      + downloads
    s["files_processed"] = s.get("files_processed", 0) + files_proc
    s["mb_downloaded"]  = s.get("mb_downloaded", 0.0) + mb_dl
    s["mb_saved"]       = s.get("mb_saved", 0.0)      + mb_saved
    STATS_FILE.write_text(json.dumps(s, indent=2))


# ── keyboard builders ──────────────────────────────────────────────────────────

def kb_media_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 Audio", callback_data="mt|a"),
        InlineKeyboardButton("🎬 Video", callback_data="mt|v"),
    ]])


def kb_audio_quality() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(label, callback_data=f"aq|{val}")
           for label, val in AUDIO_QUALITIES]
    return InlineKeyboardMarkup([row])


def kb_video_quality() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(label, callback_data=f"vq|{val}")
           for label, val in VIDEO_QUALITIES]
    return InlineKeyboardMarkup([row])


def kb_file_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 Extract / Convert Audio", callback_data="mt|a"),
        InlineKeyboardButton("🎬 Convert Video",           callback_data="mt|v"),
    ]])


def kb_audio_format() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(AUDIO_FORMATS[k][0], callback_data=f"af|{k}")
            for k in ("mp3", "aac", "ogg", "opus", "m4a")]
    row2 = [InlineKeyboardButton(AUDIO_FORMATS[k][0], callback_data=f"af|{k}")
            for k in ("flac", "wav")]
    return InlineKeyboardMarkup([row1, row2])


def kb_video_format() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(VIDEO_FORMATS[k][0], callback_data=f"vf|{k}")
           for k in VIDEO_FORMATS]
    return InlineKeyboardMarkup([row])


def kb_speed() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(label, callback_data=f"sp|{val}")
           for label, val in SPEED_OPTIONS]
    return InlineKeyboardMarkup([row])


def kb_image_format() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(IMAGE_FORMATS[k][0], callback_data=f"if|{k}")
           for k in IMAGE_FORMATS]
    return InlineKeyboardMarkup([row])


def kb_compress() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(label, callback_data=f"cp|{val}")
           for label, val in COMPRESS_OPTIONS]
    return InlineKeyboardMarkup([row])


# ── Instagram helpers ──────────────────────────────────────────────────────────

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi"}


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in domain_of(url)


def instagram_url_type(url: str) -> str:
    if "/p/" in url:
        return "post"
    if "/reel/" in url:
        return "reel"
    return "other"


def _shortcode_from_url(url: str) -> str | None:
    m = re.search(r'/p/([A-Za-z0-9_-]+)', url)
    return m.group(1) if m else None


def _instaloader_dl(shortcode: str, workdir: str) -> tuple[list[str], list[str]]:
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        save_metadata=False,
        download_comments=False,
        post_metadata_txt_pattern="",
        quiet=True,
        dirname_pattern=workdir,
        filename_pattern="{shortcode}_{mediaid}",
    )
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=Path(workdir))
    all_files = sorted(p for p in Path(workdir).iterdir() if p.is_file())
    images = [str(p) for p in all_files if p.suffix.lstrip(".").lower() in IMAGE_EXTS]
    videos = [str(p) for p in all_files if p.suffix.lstrip(".").lower() in VIDEO_EXTS]
    return images, videos


async def download_instagram_post(url: str, workdir: str) -> tuple[list[str], list[str]]:
    shortcode = _shortcode_from_url(url)
    if not shortcode:
        return [], []
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _instaloader_dl, shortcode, workdir)
    except Exception as e:
        logger.warning("instaloader failed: %s", e)
        return [], []


async def send_instagram_post(message: Message, url: str):
    from telegram import InputMediaPhoto
    status = await message.reply_text("⬇️ Downloading Instagram post…")
    workdir = new_workdir()
    images, videos = await download_instagram_post(url, str(workdir))

    if not images and not videos:
        await status.edit_text("❌ Could not download Instagram post.")
        shutil.rmtree(workdir, ignore_errors=True)
        return

    total_batches = math.ceil(len(images) / 10) if images else 0
    sent = 0
    total_items = len(images) + len(videos)

    for batch_num, i in enumerate(range(0, len(images), 10), 1):
        batch = images[i:i + 10]
        await status.edit_text(f"⬆️ Uploading photos… ({batch_num}/{total_batches})")
        handles = [open(p, "rb") for p in batch]
        try:
            media = [InputMediaPhoto(media=fh) for fh in handles]
            await message.reply_media_group(
                media=media, read_timeout=300, write_timeout=300, connect_timeout=60,
            )
            sent += len(batch)
        except Exception as e:
            logger.warning("Batch %d failed: %s", batch_num, e)
            await message.reply_text(f"⚠️ Batch {batch_num}/{total_batches} failed: {e}")
        finally:
            for fh in handles:
                fh.close()

    for idx, v in enumerate(videos, 1):
        await status.edit_text(f"⬆️ Uploading video {idx}/{len(videos)}…")
        with open(v, "rb") as f:
            await message.reply_video(
                video=f, filename=Path(v).name, supports_streaming=True,
                read_timeout=300, write_timeout=300, connect_timeout=60,
            )
        sent += 1

    await status.edit_text(f"📸 instagram.com\n{sent}/{total_items} item(s) sent")
    shutil.rmtree(workdir, ignore_errors=True)


# ── yt-dlp download ────────────────────────────────────────────────────────────

async def ytdlp_download(
    url: str, workdir: str, media_type: str, quality: str,
    status_cb=None,
) -> tuple[str | None, str]:
    import yt_dlp
    import time

    loop = asyncio.get_running_loop()
    last_update = [0.0]

    def _make_hook():
        def hook(d):
            now = time.monotonic()
            if now - last_update[0] < 4:
                return
            last_update[0] = now
            if status_cb is None:
                return
            if d["status"] == "downloading":
                pct   = d.get("_percent_str", "").strip()
                speed = d.get("_speed_str",   "").strip()
                eta   = d.get("_eta_str",     "").strip()
                text  = f"⬇️ Downloading… {pct}  {speed}  ETA {eta}"
            elif d["status"] == "finished":
                text = "⚙️ Converting…"
            else:
                return
            asyncio.run_coroutine_threadsafe(status_cb(text), loop)
        return hook

    if media_type == "audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            "js_runtimes": {"node": {}},
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_make_hook()],
        }
    else:
        ydl_opts = {
            "format": f"bestvideo[height<={quality}]+bestaudio/best",
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "extractor_args": {"youtube": {"player_client": ["tv_embedded"]}},
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_make_hook()],
        }

    COOKIES_FILE = os.path.expanduser("~/Documents/youtube_cookies.txt")

    def _dl_with_opts(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

    def _dl():
        # First try: android client (no cookies — android doesn't support cookies)
        try:
            _dl_with_opts(ydl_opts)
            return
        except Exception as first_err:
            err_str = str(first_err).lower()
            # Only retry with cookies for bot-detection / sign-in errors
            if not any(k in err_str for k in ("sign in", "bot", "confirm", "age", "unavailable", "not available")):
                raise first_err
        # Second try: tv_embedded with browser cookies
        opts2 = dict(ydl_opts)
        opts2["extractor_args"] = {"youtube": {"player_client": ["tv_embedded"]}}
        opts2.pop("cookiefile", None)
        opts2["cookiesfrombrowser"] = ("firefox",)
        _dl_with_opts(opts2)

    try:
        await loop.run_in_executor(None, _dl)
    except Exception as e:
        err_str = str(e)
        # signal 15 = SIGTERM (e.g. service restarted mid-download) — retry once
        if "signal 15" in err_str.lower() or "received signal 15" in err_str.lower():
            if status_cb:
                await status_cb("⚠️ Interrupted, retrying…")
            try:
                await loop.run_in_executor(None, _dl)
            except Exception as e2:
                return None, str(e2)
        else:
            return None, err_str

    ext = "mp3" if media_type == "audio" else "mp4"
    thumbnail_exts = {".jpg", ".png", ".webp", ".jpeg"}
    candidates = list(Path(workdir).glob(f"*.{ext}"))
    if not candidates:
        candidates = [
            p for p in Path(workdir).iterdir()
            if p.is_file() and p.suffix.lower() not in thumbnail_exts and not p.name.endswith(".part")
        ]
    if not candidates:
        return None, "No output file produced."
    return str(max(candidates, key=lambda p: p.stat().st_size)), ""



# ── playlist download ──────────────────────────────────────────────────────────

def get_playlist_info_sync(url: str) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title", "Playlist"),
        "count": len(info.get("entries") or []),
    }


async def ytdlp_playlist_download(
    url: str, workdir: str, quality: str, status_cb=None
) -> list[str]:
    import yt_dlp
    import time

    loop = asyncio.get_running_loop()
    last_update = [0.0]
    track_num = [0]

    def _make_hook():
        def hook(d):
            now = time.monotonic()
            if now - last_update[0] < 4:
                return
            last_update[0] = now
            if status_cb is None:
                return
            if d["status"] == "downloading":
                pct   = d.get("_percent_str", "").strip()
                speed = d.get("_speed_str",   "").strip()
                text  = f"⬇️ Track {track_num[0]} — {pct}  {speed}"
            elif d["status"] == "finished":
                track_num[0] += 1
                text = f"⚙️ Converting track {track_num[0]}…"
            else:
                return
            asyncio.run_coroutine_threadsafe(status_cb(text), loop)
        return hook

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(workdir, "%(playlist_index)02d - %(title)s.%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "js_runtimes": {"node": {}},
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_make_hook()],
    }

    try:
        def _dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
        await loop.run_in_executor(None, _dl)
    except Exception as e:
        logger.warning("Playlist download error: %s", e)

    files = sorted(Path(workdir).glob("*.mp3"))
    return [str(f) for f in files]


# ── Spotify via spotdl ─────────────────────────────────────────────────────────

def _spotify_track_info_embed(url: str) -> dict:
    """Get track title/artist/thumbnail from Spotify embed page — no API auth needed."""
    import re as _re, json as _json, requests as _req
    m = _re.search(r"/track/([A-Za-z0-9]+)", url)
    if not m:
        return {}
    tid = m.group(1)
    r = _req.get(
        f"https://open.spotify.com/embed/track/{tid}",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
    )
    r.raise_for_status()
    nd = _re.findall(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text)
    if not nd:
        return {}
    data = _json.loads(nd[0])
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    title = entity.get("name", "")
    artists = [a["name"] for a in entity.get("artists", [])]
    # Get highest-res cover image
    images = entity.get("coverArt", {}).get("sources") or []
    thumb_url = images[0]["url"] if images else None
    thumb_bytes = None
    if thumb_url:
        try:
            thumb_bytes = _req.get(thumb_url, timeout=10).content
        except Exception:
            pass
    return {"title": title, "artist": ", ".join(artists), "thumb_bytes": thumb_bytes}


def _embed_spotify_tags(mp3_path: str, title: str, artist: str, thumb_bytes: bytes | None):
    """Overwrite ID3 tags on an mp3 with Spotify metadata."""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, APIC, error as ID3Error
        try:
            tags = ID3(mp3_path)
        except ID3Error:
            tags = ID3()
        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=artist)
        if thumb_bytes:
            tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=thumb_bytes)
        tags.save(mp3_path)
    except Exception:
        pass


async def spotdl_download(url: str, workdir: str, kbps: str) -> tuple[str | None, str]:
    loop = asyncio.get_event_loop()

    # Try spotdl first (fast path)
    last_err = ""
    def _dl_spotdl():
        import threading
        proc = subprocess.Popen(
            ["python3.13", "-m", "spotdl", url,
             "--output", workdir, "--bitrate", f"{kbps}k", "--format", "mp3",
             "--client-id", SP_CLIENT_ID,
             "--client-secret", SP_CLIENT_SECRET,
             "--cookie-file", "cookies.txt",
             "--yt-dlp-args", "--cookies-from-browser firefox --js-runtimes node"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stdout_lines, stderr_lines = [], []
        def _read(stream, buf):
            for line in stream:
                buf.append(line)
                low = line.lower()
                if "rate" in low and ("limit" in low or "retry" in low):
                    proc.kill()
        t1 = threading.Thread(target=_read, args=(proc.stdout, stdout_lines), daemon=True)
        t2 = threading.Thread(target=_read, args=(proc.stderr, stderr_lines), daemon=True)
        t1.start(); t2.start()
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
        t1.join(5); t2.join(5)
        class _R:
            returncode = proc.returncode
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
        return _R()

    try:
        result = await loop.run_in_executor(None, _dl_spotdl)
        combined = (result.stdout + result.stderr).lower()
        if "rate" not in combined or ("limit" not in combined and "retry" not in combined):
            if result.returncode == 0:
                candidates = list(Path(workdir).glob("*.mp3"))
                if candidates:
                    return str(max(candidates, key=lambda p: p.stat().st_size)), ""
            last_err = result.stderr[-300:] or result.stdout[-300:]
        else:
            last_err = "rate_limit"
    except Exception as e:
        last_err = str(e)

    if last_err == "rate_limit" or not last_err.strip():
        # Fallback: scrape track info from embed page, search YouTube via yt-dlp
        try:
            info = await loop.run_in_executor(None, _spotify_track_info_embed, url)
            if info.get("title"):
                query = f"{info['artist']} - {info['title']}" if info.get("artist") else info["title"]
                out, err = await ytdlp_download(f"ytsearch1:{query}", workdir, "audio", kbps)
                if out:
                    await loop.run_in_executor(
                        None, _embed_spotify_tags,
                        out, info["title"], info.get("artist", ""), info.get("thumb_bytes"),
                    )
                    return out, ""
                last_err = err
        except Exception as e:
            last_err = str(e)

    return None, last_err


async def spotdl_playlist_download(
    url: str, workdir: str, kbps: str, status_cb=None
) -> list[str]:
    loop = asyncio.get_event_loop()
    if status_cb:
        await status_cb("⬇️ Fetching Spotify playlist tracks…")

    # Get track URLs from embed page (works for playlists), fall back to spotdl native for albums
    track_urls = []
    if "/playlist/" in url:
        try:
            info = await loop.run_in_executor(None, _sp_embed_playlist, url)
            track_urls = info.get("track_urls", [])
        except Exception as e:
            logger.warning("Embed playlist fetch failed: %s", e)

    if track_urls:
        sem = asyncio.Semaphore(10)
        done = [0]
        total_tracks = len(track_urls)

        async def _dl_track(t_url: str, idx: int):
            def _run():
                return subprocess.run(
                    ["python3.13", "-m", "spotdl", t_url,
                     "--output", os.path.join(workdir, f"{idx:03d} - {{title}}"),
                     "--bitrate", f"{kbps}k", "--format", "mp3",
                     "--client-id", SP_CLIENT_ID,
                     "--client-secret", SP_CLIENT_SECRET,
                     "--cookie-file", "cookies.txt",
                     "--yt-dlp-args", "--cookies-from-browser firefox --js-runtimes node"],
                    capture_output=True, text=True, timeout=300,
                )
            async with sem:
                for attempt in range(3):
                    try:
                        result = await loop.run_in_executor(None, _run)
                        if result.returncode == 0:
                            break
                        logger.warning("spotdl track %d attempt %d failed: %s", idx, attempt + 1, result.stderr[-300:])
                    except Exception as e:
                        logger.warning("spotdl track %d attempt %d error: %s", idx, attempt + 1, e)
                    if attempt < 2:
                        await asyncio.sleep(3)
            done[0] += 1
            if status_cb:
                await status_cb(f"⬇️ Downloading Spotify playlist… {done[0]}/{total_tracks}")

        await asyncio.gather(*[_dl_track(t_url, i) for i, t_url in enumerate(track_urls, 1)])
    else:
        # Albums / artists: spotdl handles these natively
        if status_cb:
            await status_cb("⬇️ Downloading Spotify collection via spotdl…")
        def _dl():
            return subprocess.run(
                ["python3.13", "-m", "spotdl", url,
                 "--output", os.path.join(workdir, "{list-position:03d} - {title}"),
                 "--bitrate", f"{kbps}k", "--format", "mp3",
                 "--client-id", SP_CLIENT_ID,
                 "--client-secret", SP_CLIENT_SECRET,
                 "--cookie-file", "cookies.txt",
                 "--yt-dlp-args", "--cookies-from-browser firefox --js-runtimes node"],
                capture_output=True, text=True, timeout=1800,
            )
        try:
            result = await loop.run_in_executor(None, _dl)
            if result.returncode != 0:
                logger.warning("spotdl collection failed: %s", result.stderr[-500:])
        except Exception as e:
            logger.warning("spotdl collection error: %s", e)

    def _sort_key(path: Path) -> int:
        try:
            return int(path.stem.split(" - ")[0].strip())
        except (ValueError, IndexError):
            pass
        try:
            from mutagen import File as MutagenFile
            tags = MutagenFile(str(path))
            if tags and tags.tags:
                t = tags.tags.get("TRCK") or tags.tags.get("tracknumber")
                if t:
                    return int(str(t).split("/")[0])
        except Exception:
            pass
        return 9999

    files = sorted(Path(workdir).glob("*.mp3"), key=_sort_key)
    return [str(f) for f in files]


# ── file splitting ─────────────────────────────────────────────────────────────

PART_LIMIT = 49 * 1024 * 1024


def get_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _split_file_sync(src: str, workdir: str) -> list[str]:
    size = os.path.getsize(src)
    if size <= PART_LIMIT:
        return [src]

    duration = get_duration(src)
    if duration <= 0:
        # Can't probe duration; try a raw byte-level split as fallback
        return [src]

    n_parts = math.ceil(size / PART_LIMIT)
    part_duration = duration / n_parts
    stem = Path(src).stem
    ext  = Path(src).suffix
    pattern = os.path.join(workdir, f"{stem}_part%03d{ext}")

    result = subprocess.run(
        [FFMPEG_EXE, "-y", "-i", src, "-c", "copy",
         "-f", "segment", "-segment_time", str(part_duration),
         "-reset_timestamps", "1", pattern],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        logger.warning("Split failed: %s", result.stderr[-400:])
        return [src]

    parts = sorted(Path(workdir).glob(f"{stem}_part*{ext}"))
    return [str(p) for p in parts] if parts else [src]


# ── send helpers ───────────────────────────────────────────────────────────────

async def _mtproto_upload(chat_id: int, file_path: str, caption: str, status_msg=None) -> bool:
    """Upload a file via Telethon MTProto (bypasses 50 MB Bot API limit, supports up to 2 GB)."""
    proc = await asyncio.create_subprocess_exec(
        "python3.13", "/home/debian/telegram-tools/pyro_upload.py",
        str(chat_id), file_path, caption,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(1800):
            async for raw in proc.stdout:
                line = raw.decode().strip()
                if line.startswith("PROGRESS:") and status_msg:
                    try:
                        await status_msg.edit_text(f"⬆️ MTProto upload: {line[9:]}")
                    except Exception:
                        pass
                elif line == "OK":
                    return True
                elif line.startswith("ERROR:"):
                    logger.error("MTProto upload error: %s", line)
                    return False
                elif line:
                    logger.info("MTProto upload stdout: %s", line)
    finally:
        stderr = await proc.stderr.read()
        if stderr:
            logger.error("MTProto upload stderr: %s", stderr.decode()[-1000:])
    return False


def extract_audio_meta(path: str) -> tuple[str | None, str | None, bytes | None]:
    """Return (title, performer, thumbnail_bytes) from embedded ID3/MP4 tags."""
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3NoHeaderError
        audio = MutagenFile(path)
        if audio is None:
            return None, None, None
        tags = audio.tags
        if tags is None:
            return None, None, None

        title = performer = None
        thumb_bytes = None

        # ID3 (MP3)
        if hasattr(tags, "getall"):
            t = tags.get("TIT2") or tags.get("TT2")
            if t:
                title = str(t)
            a = tags.get("TPE1") or tags.get("TP1")
            if a:
                performer = str(a)
            for key in tags.keys():
                if key.startswith("APIC"):
                    thumb_bytes = tags[key].data
                    break
        # MP4/M4A
        elif hasattr(tags, "get"):
            t = tags.get("\xa9nam")
            if t:
                title = t[0]
            a = tags.get("\xa9ART") or tags.get("aART")
            if a:
                performer = a[0]
            covr = tags.get("covr")
            if covr:
                thumb_bytes = bytes(covr[0])

        return title, performer, thumb_bytes
    except Exception:
        return None, None, None


async def send_audio_file(message: Message, path: str, caption: str, status_msg=None) -> bool:
    loop = asyncio.get_event_loop()
    if os.path.getsize(path) > 49 * 1024 * 1024:
        parts = [path]
    else:
        workdir = str(Path(path).parent)
        parts = await loop.run_in_executor(None, _split_file_sync, path, workdir)
    total = len(parts)
    used_mtproto = False

    for i, part in enumerate(parts, 1):
        size = os.path.getsize(part)
        part_caption = caption if total == 1 else f"Part {i}/{total}"
        if size > 49 * 1024 * 1024:
            if status_msg:
                try:
                    await status_msg.edit_text(f"⬆️ File is {size/1024/1024:.0f} MB — uploading via MTProto…")
                except Exception:
                    pass
            ok = await _mtproto_upload(message.chat_id, part, "", status_msg)
            if not ok:
                await message.reply_text("❌ MTProto upload failed.")
                return False
            used_mtproto = True
        else:
            title, performer, thumb_bytes = extract_audio_meta(part)
            thumb_io = io.BytesIO(thumb_bytes) if thumb_bytes else None
            with open(part, "rb") as f:
                await message.reply_audio(
                    audio=f, filename=Path(part).name,
                    title=title, performer=performer,
                    thumbnail=thumb_io,
                    read_timeout=300, write_timeout=300, connect_timeout=60,
                )
        if total > 1 and size <= 49 * 1024 * 1024:
            await message.reply_text(f"Part {i}/{total}")

    if caption:
        await message.reply_text(caption)
    return True


async def send_video_file(message: Message, path: str, caption: str, status_msg=None) -> bool:
    loop = asyncio.get_event_loop()
    if os.path.getsize(path) > 49 * 1024 * 1024:
        parts = [path]
    else:
        workdir = str(Path(path).parent)
        parts = await loop.run_in_executor(None, _split_file_sync, path, workdir)
    total = len(parts)
    used_mtproto = False

    for i, part in enumerate(parts, 1):
        size = os.path.getsize(part)
        part_caption = caption if total == 1 else f"Part {i}/{total}"
        if size > 49 * 1024 * 1024:
            if status_msg:
                try:
                    await status_msg.edit_text(f"⬆️ File is {size/1024/1024:.0f} MB — uploading via MTProto…")
                except Exception:
                    pass
            ok = await _mtproto_upload(message.chat_id, part, "", status_msg)
            if not ok:
                await message.reply_text("❌ MTProto upload failed.")
                return False
            used_mtproto = True
        else:
            with open(part, "rb") as f:
                await message.reply_video(
                    video=f, filename=Path(part).name, supports_streaming=True,
                    read_timeout=300, write_timeout=300, connect_timeout=60,
                )
        if total > 1 and size <= 49 * 1024 * 1024:
            await message.reply_text(f"Part {i}/{total}")

    await message.reply_text(caption)
    return True


# ── search helpers ─────────────────────────────────────────────────────────────

def _yt_search_sync(query: str, n: int = 10) -> list[dict]:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    results = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        dur = entry.get("duration") or 0
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        results.append({
            "title": entry.get("title", "Unknown"),
            "url":   f"https://www.youtube.com/watch?v={entry['id']}",
            "dur":   dur_str,
        })
    return results


SP_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SP_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

def _sp_get_token() -> str:
    import base64
    import requests as _req
    creds = base64.b64encode(f"{SP_CLIENT_ID}:{SP_CLIENT_SECRET}".encode()).decode()
    r = _req.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {creds}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _sp_embed_playlist(url: str) -> dict:
    """Scrape playlist info + track URLs from the Spotify embed page (no auth needed)."""
    import re as _re, json as _json, requests as _req
    m = _re.search(r"/(playlist|album)/([A-Za-z0-9]+)", url)
    if not m:
        return {"title": "Spotify Collection", "count": "?", "track_urls": []}
    kind, pid = m.group(1), m.group(2)
    r = _req.get(
        f"https://open.spotify.com/embed/{kind}/{pid}",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
    )
    r.raise_for_status()
    nd = _re.findall(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text)
    if not nd:
        raise ValueError("Could not parse Spotify embed page")
    data = _json.loads(nd[0])
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = entity.get("trackList") or []
    track_urls = []
    for t in tracks:
        tid = t["uri"].split(":")[-1]
        track_urls.append(f"https://open.spotify.com/track/{tid}")
    return {"title": entity.get("name") or "Spotify Playlist", "count": len(track_urls), "track_urls": track_urls}


def _sp_collection_info(url: str) -> dict:
    """Return {title, count} for a Spotify playlist, album, or artist URL."""
    import re, requests as _req
    token = _sp_get_token()
    headers = {"Authorization": f"Bearer {token}"}

    if "/playlist/" in url:
        info = _sp_embed_playlist(url)
        return {"title": info["title"], "count": info["count"]}

    if "/album/" in url:
        aid = re.search(r"/album/([A-Za-z0-9]+)", url).group(1)
        r = _req.get(f"https://api.spotify.com/v1/albums/{aid}", headers=headers, timeout=10)
        r.raise_for_status()
        d = r.json()
        return {"title": d["name"], "count": d["total_tracks"]}

    if "/artist/" in url:
        aid = re.search(r"/artist/([A-Za-z0-9]+)", url).group(1)
        r = _req.get(f"https://api.spotify.com/v1/artists/{aid}/top-tracks",
                     params={"market": "US"}, headers=headers, timeout=10)
        r.raise_for_status()
        d = r.json()
        artist_r = _req.get(f"https://api.spotify.com/v1/artists/{aid}", headers=headers, timeout=10)
        artist_r.raise_for_status()
        return {"title": artist_r.json()["name"] + " — Top Tracks", "count": len(d["tracks"])}

    return {"title": "Spotify Collection", "count": "?"}


def _sp_search_sync(query: str, n: int = 10) -> list[dict]:
    import requests as _req
    token = _sp_get_token()
    r = _req.get(
        "https://api.spotify.com/v1/search",
        params={"q": query, "type": "track", "limit": n},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    results = []
    for item in r.json().get("tracks", {}).get("items") or []:
        dur_ms = item.get("duration_ms") or 0
        m, s = divmod(dur_ms // 1000, 60)
        dur_str = f"{m}:{s:02d}"
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        title = f"{artists} - {item['name']}"
        results.append({
            "title": title,
            "url":   item["external_urls"]["spotify"],
            "dur":   dur_str,
        })
    return results


def _sc_search_sync(query: str, n: int = 10) -> list[dict]:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
        info = ydl.extract_info(f"scsearch{n}:{query}", download=False)
    results = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        dur = entry.get("duration") or 0
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        # webpage_url is the real soundcloud.com/... URL; entry["url"] is an internal API stub
        url = entry.get("webpage_url") or entry.get("url", "")
        results.append({
            "title": entry.get("title", "Unknown"),
            "url":   url,
            "dur":   dur_str,
        })
    return results


async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     platform: str, search_fn, result_type: str, cb_prefix: str, icon: str):
    query = " ".join(context.args) if context.args else ""
    if not query:
        cmd = {"YouTube": "search", "SoundCloud": "search_sc", "Spotify": "search_sp"}.get(platform, "search")
        await update.message.reply_text(f"Usage: /{cmd} <query>")
        return

    status = await update.message.reply_text(f"🔍 Searching {platform} for: {query}…")
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, search_fn, query)
    except Exception as e:
        await status.edit_text(f"❌ Search failed: {e}")
        return

    if not results:
        await status.edit_text("No results found.")
        return

    key = str(uuid.uuid4())[:8]
    context.user_data[key] = {"type": result_type, "results": results}
    context.user_data["last_key"] = key

    keyboard = []
    for i, r in enumerate(results):
        label = f"{i+1}. {r['title'][:45]}{'…' if len(r['title'])>45 else ''} [{r['dur']}]"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{cb_prefix}|{key}|{i}")])

    await status.edit_text(
        f"{icon} Top {len(results)} *{platform}* results for *{query}*:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def search_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await _do_search(update, context, "YouTube", _yt_search_sync, "yt_results", "yt", "🎬")
    else:
        context.user_data["pending_search"] = "youtube"
        await update.message.reply_text("🎬 Enter your YouTube search query:")


async def search_soundcloud(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await _do_search(update, context, "SoundCloud", _sc_search_sync, "sc_results", "sc", "☁️")
    else:
        context.user_data["pending_search"] = "soundcloud"
        await update.message.reply_text("☁️ Enter your SoundCloud search query:")


async def search_spotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await _do_search(update, context, "Spotify", _sp_search_sync, "sp_results", "sp_s", "🎵")
    else:
        context.user_data["pending_search"] = "spotify"
        await update.message.reply_text("🎵 Enter your Spotify search query:")


# ── command handlers ───────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Media Bot*\n\n"
        "Send me:\n"
        "• A URL — YouTube, Spotify, SoundCloud, TikTok, Instagram, Twitter/X, 1000+ sites\n"
        "• A YouTube or Spotify *playlist* URL → download all tracks\n"
        "• An audio or video file → convert/compress\n\n"
        "Commands:\n"
        "/search — Search YouTube\n"
        "/search\\_sc — Search SoundCloud\n"
        "/search\\_sp — Search Spotify\n"
        "/shazam — Identify a song from voice message\n"
        "/lyrics — Get lyrics for a song\n"
        "/find — Find a song from lyric snippet\n"
        "/merge — Merge multiple audio files into one\n"
        "/stats — Bot statistics\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = stats_load()
    jobs_str = f"\n🔁 Active jobs: {active_jobs}" if active_jobs > 0 else ""
    await update.message.reply_text(
        "📊 *Bot Statistics*\n\n"
        f"📥 Downloads: {s.get('downloads', 0)}\n"
        f"🔄 Files processed: {s.get('files_processed', 0)}\n"
        f"💾 Total downloaded: {s.get('mb_downloaded', 0.0):.1f} MB\n"
        f"📉 Saved by compression: {s.get('mb_saved', 0.0):.1f} MB"
        f"{jobs_str}",
        parse_mode="Markdown",
    )


async def merge_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["merge_session"] = {"active": True, "files": []}
    await update.message.reply_text(
        "📎 *Merge mode active*\n\n"
        "Send me audio files one by one.\n"
        "/done — merge and send\n"
        "/cancel — abort",
        parse_mode="Markdown",
    )


async def merge_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.user_data.get("merge_session", {})
    if not session.get("active"):
        await update.message.reply_text("No merge session active. Use /merge to start one.")
        return

    files_info = session.get("files", [])
    context.user_data.pop("merge_session", None)

    if len(files_info) < 2:
        await update.message.reply_text("⚠️ Send at least 2 audio files to merge.")
        return

    status = await update.message.reply_text(f"⬇️ Downloading {len(files_info)} files…")
    workdir = new_workdir()
    local_files = []

    for i, fi in enumerate(files_info, 1):
        try:
            await status.edit_text(f"⬇️ Downloading file {i}/{len(files_info)}…")
            tg_file = await context.bot.get_file(fi["file_id"])
            ext = Path(fi["file_name"]).suffix or ".mp3"
            dest = str(workdir / f"input_{i:03d}{ext}")
            await tg_file.download_to_drive(dest)
            local_files.append(dest)
        except Exception as e:
            await status.edit_text(f"❌ Failed to download file {i}: {e}")
            shutil.rmtree(workdir, ignore_errors=True)
            return

    await status.edit_text("⚙️ Merging…")
    dst = str(workdir / "merged.mp3")
    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, merge_audio_sync, local_files, dst)

    if not ok:
        await status.edit_text(f"❌ Merge failed:\n{err[:400]}")
        shutil.rmtree(workdir, ignore_errors=True)
        return

    sz = size_str(dst)
    await status.edit_text("⬆️ Uploading…")
    uploaded = await send_audio_file(
        update.message, dst,
        f"🔗 Merged {len(local_files)} files\n💾 {sz}"
    )
    if uploaded:
        shutil.rmtree(workdir, ignore_errors=True)
    try:
        await status.delete()
    except Exception:
        pass


async def merge_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("merge_session", None):
        await update.message.reply_text("✅ Merge session cancelled.")
    else:
        await update.message.reply_text("No active session.")


# ── shazam ────────────────────────────────────────────────────────────────────

async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Find a song from lyrics: /find <lyric snippet>"""
    if not context.args:
        context.user_data["pending_search"] = "find"
        await update.message.reply_text("🔎 Send me a lyric snippet and I'll find the song:")
        return
    query = " ".join(context.args)
    await _do_find(update.message, context, query)


def _clean_lyrics_search_title(title: str) -> str:
    import re as _re
    t = (title or "").strip()
    t = t.replace("–", "-").replace("—", "-")
    t = _re.sub(r'\s*\((official music video|official video|official audio|lyrics?|lyric video|audio)\)\s*', '', t, flags=_re.I)
    t = _re.sub(r'\s*\[(official music video|official video|official audio|lyrics?|lyric video|audio)\]\s*', '', t, flags=_re.I)
    t = _re.sub(r'\s*[-|]\s*(official music video|official video|official audio|lyrics?|lyric video|audio)\s*$', '', t, flags=_re.I)
    t = _re.sub(r'\s*\((official video remastered|official remastered|remastered)\)\s*', '', t, flags=_re.I)
    t = _re.sub(r'\s*\((karaoke version|karaoke|4k remaster|remaster(ed)?)\)\s*', '', t, flags=_re.I)
    t = _re.sub(r'\s*[-|]\s*(karaoke version|karaoke|with lyrics|lyrics)\s*$', '', t, flags=_re.I)
    t = _re.sub(r'\s*-\s*-\s*with lyrics\s*$', '', t, flags=_re.I)
    t = _re.sub(r'\s*with lyrics\s*$', '', t, flags=_re.I)
    t = _re.sub(r'"\s*.*$', '', t)
    t = _re.sub(r'\s*lyrics\s*$', '', t, flags=_re.I)
    t = _re.sub(r'\s*-\s*', ' - ', t)
    t = _re.sub(r'\s+', ' ', t).strip(" -|")
    return t


def _find_by_lyrics_sync(query: str, n: int = 5) -> list[str]:
    import yt_dlp

    search_query = f"{query} lyrics"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{search_query}", download=False)

    seen = set()
    songs = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        title = _clean_lyrics_search_title(entry.get("title", ""))
        if not title:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        songs.append(title)
    return songs[:5]


async def _do_find(message: Message, context: ContextTypes.DEFAULT_TYPE, query: str):
    status = await message.reply_text(f"🔎 Searching for: {query}…")
    loop = asyncio.get_event_loop()
    try:
        songs = await loop.run_in_executor(None, _find_by_lyrics_sync, query)
    except Exception as e:
        await status.edit_text(f"❌ Search failed: {e}")
        return
    if not songs:
        await status.edit_text("❌ No songs found for those lyrics.")
        return
    lines = ["🎵 *Songs matching those lyrics:*\n"]
    find_key = str(uuid.uuid4())[:8]
    context.user_data[find_key] = {
        "type": "find_results",
        "results": [{"title": title, "sp_query": title} for title in songs],
    }
    context.user_data["last_key"] = find_key
    keyboard = []
    for i, title in enumerate(songs, 1):
        lines.append(f"{i}. {title}")
        keyboard.append([
            InlineKeyboardButton(
                f"⬇️ Spotify {i}",
                callback_data=f"find_dl|{find_key}|{i-1}",
            )
        ])
    await status.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def shazam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate shazam mode: bot will identify the next voice message sent."""
    context.chat_data["shazam_pending"] = True
    await update.message.reply_text("🎵 Send a voice message and I'll identify the song.")


async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for lyrics: /lyrics <artist - title>"""
    if not context.args:
        context.user_data["pending_search"] = "lyrics"
        await update.message.reply_text("🎤 Enter the song name (e.g. Rick Astley - Never Gonna Give You Up):")
        return
    query = " ".join(context.args)
    await _do_lyrics(update.message, query)


async def _do_lyrics(message: Message, query: str):
    status = await message.reply_text(f"🔍 Searching lyrics for: {query}…")
    loop = asyncio.get_event_loop()
    try:
        import syncedlyrics
        lyrics = await loop.run_in_executor(None, syncedlyrics.search, query)
    except Exception as e:
        await status.edit_text(f"❌ Lyrics search failed: {e}")
        return
    if not lyrics:
        await status.edit_text(f"❌ No lyrics found for: {query}")
        return
    # Strip timestamps [mm:ss.xx] for plain text display
    import re as _re
    plain = _re.sub(r'\[\d+:\d+\.\d+\]', '', lyrics).strip()
    # Telegram message limit is 4096 chars
    header = f"🎤 *{query}*\n\n"
    body = plain[:4096 - len(header) - 10]
    await status.edit_text(header + body, parse_mode="Markdown")


SHAZAM_SCRIPT = Path(__file__).parent / "shazam_identify.py"

async def _recognize(path: str) -> dict | None:
    """Identify a song using Shazam (via python3.13 subprocess)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3.13", str(SHAZAM_SCRIPT), path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        lines = [l for l in stdout.decode().splitlines() if l.strip()]
        if not lines:
            return None
        result = json.loads(lines[-1])
        if "error" in result:
            return None
        return result
    except Exception as e:
        logger.warning("Recognition error: %s", e)
        return None


# ── text / URL handler ─────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ── Pending search query ───────────────────────────────────────────────────
    pending = context.user_data.pop("pending_search", None)
    if pending:
        context.args = update.message.text.strip().split()
        if pending == "youtube":
            await _do_search(update, context, "YouTube", _yt_search_sync, "yt_results", "yt", "🎬")
        elif pending == "soundcloud":
            await _do_search(update, context, "SoundCloud", _sc_search_sync, "sc_results", "sc", "☁️")
        elif pending == "lyrics":
            await _do_lyrics(update.message, update.message.text.strip())
        elif pending == "find":
            await _do_find(update.message, context, update.message.text.strip())
        else:
            await _do_search(update, context, "Spotify", _sp_search_sync, "sp_results", "sp_s", "🎵")
        return

    urls = extract_urls(update.message.text)
    if not urls:
        if update.effective_chat.type == "private":
            await update.message.reply_text("Send me a URL or a media file.")
        return

    url    = urls[0]
    domain = domain_of(url)

    # ── Instagram routing ──────────────────────────────────────────────────────
    if is_instagram_url(url):
        if instagram_url_type(url) == "post":
            await send_instagram_post(update.message, url)
            return

    # ── Playlist detection ─────────────────────────────────────────────────────
    if is_youtube_playlist(url) or is_spotify_playlist(url) or is_soundcloud_playlist(url):
        status = await update.message.reply_text("📋 Getting playlist info…")
        loop = asyncio.get_event_loop()

        if is_youtube_playlist(url):
            try:
                info = await loop.run_in_executor(None, get_playlist_info_sync, url)
            except Exception as e:
                logger.warning("Playlist info failed: %s", e)
                info = {"title": "YouTube Playlist", "count": "?"}
        elif is_soundcloud_playlist(url):
            try:
                info = await loop.run_in_executor(None, get_playlist_info_sync, url)
            except Exception as e:
                logger.warning("SC playlist info failed: %s", e)
                info = {"title": "SoundCloud Playlist", "count": "?"}
        else:
            try:
                info = await loop.run_in_executor(None, _sp_collection_info, url)
            except Exception as e:
                logger.warning("Spotify info failed: %s", e)
                info = {"title": "Spotify Collection", "count": "?"}

        key = str(uuid.uuid4())[:8]
        context.user_data[key] = {"type": "playlist", "url": url, "domain": domain, "info": info}
        context.user_data["last_key"] = key

        await status.edit_text(
            f"📃 *{info['title']}*\n{info['count']} tracks\n\nChoose audio quality:",
            reply_markup=kb_audio_quality(),
            parse_mode="Markdown",
        )
        return

    # ── Normal URL flow ────────────────────────────────────────────────────────
    key = str(uuid.uuid4())[:8]
    context.user_data[key] = {"type": "url", "url": url}
    context.user_data["last_key"] = key

    if is_audio_only_platform(url):
        context.user_data[key]["media_type"] = "audio"
        await update.message.reply_text(
            f"🔗 {domain}\nChoose audio quality:",
            reply_markup=kb_audio_quality(),
        )
    else:
        await update.message.reply_text(
            f"🔗 {domain}\nWhat do you want?",
            reply_markup=kb_media_type(),
        )


# ── file handler ───────────────────────────────────────────────────────────────

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = None
    original_name = "file"
    is_video = False

    if msg.photo:
        # Telegram sends photos as compressed JPEGs; use the highest resolution
        photo = msg.photo[-1]
        key = str(uuid.uuid4())[:8]
        context.user_data[key] = {
            "type": "image",
            "file_id": photo.file_id,
            "file_name": "photo.jpg",
            "chat_id": msg.chat.id,
            "message_id": msg.message_id,
        }
        context.user_data["last_key"] = key
        await msg.reply_text("🖼 Photo received. Convert to:", reply_markup=kb_image_format())
        return

    # ── Shazam intercept ──────────────────────────────────────────────────────
    if msg.voice and context.chat_data.pop("shazam_pending", False):
        status = await msg.reply_text("🎵 Identifying song…")
        workdir = new_workdir()
        try:
            tg_file = await msg.voice.get_file()
            ogg_path = str(workdir / "shazam_voice.ogg")
            await tg_file.download_to_drive(ogg_path)
            track = await _recognize(ogg_path)
            if not track:
                await status.edit_text("❌ Couldn't identify the song. Try a clearer recording.")
                return
            title    = track.get("title", "Unknown")
            subtitle = track.get("subtitle", "Unknown artist")
            sections = track.get("sections", [])
            meta_section = next((s for s in sections if s.get("type") == "SONG"), None)
            meta = {m["title"]: m.get("text") for m in (meta_section or {}).get("metadata", [])} if meta_section else {}
            album = meta.get("Album", "")
            year  = meta.get("Released", "")
            genre = track.get("genres", {}).get("primary", "")
            info_lines = [f"🎵 *{title}*", f"👤 {subtitle}"]
            if album: info_lines.append(f"💿 {album}")
            if year:  info_lines.append(f"📅 {year}")
            if genre: info_lines.append(f"🎼 {genre}")
            shazam_key = str(uuid.uuid4())[:8]
            context.user_data[shazam_key] = {
                "type": "url",
                "media_type": "audio",
                "shazam_title": f"{subtitle} - {title}",
                "shazam_sp_query": f"{title} {subtitle}",
            }
            context.user_data["last_key"] = shazam_key
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬇️ Download from Spotify", callback_data=f"shazam_dl|{shazam_key}"),
            ]])
            await status.edit_text("\n".join(info_lines), parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.exception("Shazam error")
            await status.edit_text(f"❌ Error: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    if msg.audio:
        file_obj, original_name = msg.audio, msg.audio.file_name or "audio"
    elif msg.voice:
        file_obj, original_name = msg.voice, "voice.ogg"
    elif msg.video:
        file_obj, original_name, is_video = msg.video, msg.video.file_name or "video.mp4", True
    elif msg.video_note:
        file_obj, original_name, is_video = msg.video_note, "videonote.mp4", True
    elif msg.document:
        doc = msg.document
        name = doc.file_name or "document"
        ext = Path(name).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}:
            key = str(uuid.uuid4())[:8]
            context.user_data[key] = {
                "type": "image",
                "file_id": doc.file_id,
                "file_name": name,
                "chat_id": msg.chat.id,
                "message_id": msg.message_id,
            }
            context.user_data["last_key"] = key
            await msg.reply_text(f"🖼 {name}\nConvert to:", reply_markup=kb_image_format())
            return
        is_video = ext in {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".3gp"}
        file_obj, original_name = doc, name
    else:
        await msg.reply_text("Unsupported file type.")
        return

    # ── Merge session: collect file ────────────────────────────────────────────
    session = context.user_data.get("merge_session", {})
    if session.get("active") and not is_video:
        session["files"].append({"file_id": file_obj.file_id, "file_name": original_name})
        count = len(session["files"])
        await msg.reply_text(f"✅ Added file {count}. Send more or /done to merge.")
        return

    key = str(uuid.uuid4())[:8]
    context.user_data[key] = {
        "type": "file",
        "file_id": file_obj.file_id,
        "file_name": original_name,
        "is_video": is_video,
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
    }
    context.user_data["last_key"] = key

    if is_video:
        await msg.reply_text(f"📁 {original_name}\nWhat do you want?", reply_markup=kb_file_type())
    else:
        context.user_data[key]["media_type"] = "audio"
        await msg.reply_text(f"🎵 {original_name}\nChoose output format:", reply_markup=kb_audio_format())


# ── callback handler ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data  = query.data
    key   = context.user_data.get("last_key")

    if not key or key not in context.user_data:
        await query.edit_message_text("Session expired. Send the file or URL again.")
        return

    state  = context.user_data[key]
    parts  = data.split("|")
    action = parts[0]

    # ── YouTube search result ──────────────────────────────────────────────────
    if action == "yt":
        idx    = int(parts[2])
        result = state["results"][idx]
        new_key = str(uuid.uuid4())[:8]
        context.user_data[new_key] = {"type": "url", "url": result["url"]}
        context.user_data["last_key"] = new_key
        context.user_data.pop(key, None)
        await query.edit_message_text(
            f"🎬 {result['title']}\nWhat do you want?", reply_markup=kb_media_type()
        )
        return

    # ── SoundCloud search result ───────────────────────────────────────────────
    if action == "sc":
        idx    = int(parts[2])
        result = state["results"][idx]
        new_key = str(uuid.uuid4())[:8]
        context.user_data[new_key] = {"type": "url", "url": result["url"], "media_type": "audio"}
        context.user_data["last_key"] = new_key
        context.user_data.pop(key, None)
        await query.edit_message_text(
            f"☁️ {result['title']}\nChoose audio quality:", reply_markup=kb_audio_quality()
        )
        return

    # ── Spotify search result ──────────────────────────────────────────────────
    if action == "sp_s":
        idx    = int(parts[2])
        result = state["results"][idx]
        new_key = str(uuid.uuid4())[:8]
        context.user_data[new_key] = {"type": "url", "url": result["url"], "media_type": "audio"}
        context.user_data["last_key"] = new_key
        context.user_data.pop(key, None)
        await query.edit_message_text(
            f"🎵 {result['title']}\nChoose audio quality:", reply_markup=kb_audio_quality()
        )
        return

    # ── Shazam → download from Spotify ────────────────────────────────────────
    if action == "shazam_dl":
        shazam_key = parts[1]
        shazam_state = context.user_data.get(shazam_key, {})
        title_label = shazam_state.get("shazam_title", "")
        sp_query    = shazam_state.get("shazam_sp_query", "")
        if not sp_query:
            await query.edit_message_text("❌ Session expired.")
            return
        await query.edit_message_text(f"🔍 Searching Spotify for: {title_label}…")
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, _sp_search_sync, sp_query, 5)
        except Exception as e:
            await query.edit_message_text(f"❌ Spotify search failed: {e}")
            return
        if not results:
            await query.edit_message_text("❌ No Spotify results found.")
            return
        top = results[0]
        new_key = str(uuid.uuid4())[:8]
        context.user_data[new_key] = {"type": "url", "url": top["url"], "media_type": "audio"}
        context.user_data["last_key"] = new_key
        await query.edit_message_text(
            f"🎵 {top['title']}\nChoose audio quality:", reply_markup=kb_audio_quality()
        )
        return

    # ── Lyrics find → download from Spotify ───────────────────────────────────
    if action == "find_dl":
        find_key = parts[1]
        idx = int(parts[2])
        find_state = context.user_data.get(find_key, {})
        results = find_state.get("results") or []
        if idx >= len(results):
            await query.edit_message_text("❌ Session expired.")
            return
        picked = results[idx]
        title_label = picked.get("title", "")
        sp_query = picked.get("sp_query", title_label)
        await query.edit_message_text(f"🔍 Searching Spotify for: {title_label}…")
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, _sp_search_sync, sp_query, 5)
        except Exception as e:
            await query.edit_message_text(f"❌ Spotify search failed: {e}")
            return
        if not results:
            await query.edit_message_text("❌ No Spotify results found.")
            return
        top = results[0]
        new_key = str(uuid.uuid4())[:8]
        context.user_data[new_key] = {"type": "url", "url": top["url"], "media_type": "audio"}
        context.user_data["last_key"] = new_key
        await query.edit_message_text(
            f"🎵 {top['title']}\nChoose audio quality:", reply_markup=kb_audio_quality()
        )
        return

    value = parts[1] if len(parts) > 1 else ""

    # ── Media type selection ───────────────────────────────────────────────────
    if action == "mt":
        state["media_type"] = "audio" if value == "a" else "video"
        if state["type"] == "file":
            if value == "a":
                await query.edit_message_text("Choose output format:", reply_markup=kb_audio_format())
            else:
                await query.edit_message_text("Choose output format:", reply_markup=kb_video_format())
        else:
            if value == "a":
                await query.edit_message_text("Choose audio quality:", reply_markup=kb_audio_quality())
            else:
                # Skip quality picker — always use best available quality
                state["quality"] = "1080"
                state["media_type"] = "video"
                await query.edit_message_text("⏳ Processing…")
                message = query.message
                try:
                    await process_url(query, message, context, state)
                except Exception as e:
                    logger.exception("Error during processing")
                    try:
                        await query.edit_message_text(f"❌ Unexpected error: {e}")
                    except Exception:
                        pass
        return

    # ── Audio format (file conversions) ───────────────────────────────────────
    if action == "af":
        state["audio_format"] = value
        _, _, lossy, _ = AUDIO_FORMATS[value]
        if lossy:
            await query.edit_message_text("Choose audio quality:", reply_markup=kb_audio_quality())
        else:
            state["quality"] = "lossless"
            state["media_type"] = "audio"
            await query.edit_message_text("🎚 Choose playback speed:", reply_markup=kb_speed())
        return

    # ── Video format (file conversions) ───────────────────────────────────────
    if action == "vf":
        state["video_format"] = value
        state["quality"] = "1080"
        state["media_type"] = "video"
        await query.edit_message_text("⏳ Processing…")
        message = query.message
        try:
            await process_file(query, message, context, state)
        except Exception as e:
            logger.exception("Error during processing")
            try:
                await query.edit_message_text(f"❌ Unexpected error: {e}")
            except Exception:
                pass
        return

    # ── Audio quality → show speed ─────────────────────────────────────────────
    if action == "aq":
        state["quality"] = value
        state["media_type"] = "audio"
        await query.edit_message_text("🎚 Choose playback speed:", reply_markup=kb_speed())
        return

    # ── Video quality → process immediately ───────────────────────────────────
    if action == "vq":
        state["quality"] = value
        state["media_type"] = "video"
        await query.edit_message_text("⏳ Processing…")
        message = query.message
        try:
            if state["type"] == "url":
                await process_url(query, message, context, state)
            elif state["type"] == "file":
                await process_file(query, message, context, state)
        except Exception as e:
            logger.exception("Error during processing")
            try:
                await query.edit_message_text(f"❌ Unexpected error: {e}")
            except Exception:
                pass
        context.user_data.pop(key, None)
        context.user_data.pop("last_key", None)
        return

    # ── Image format selection → process ──────────────────────────────────────
    if action == "if":
        state["image_format"] = value
        await query.edit_message_text("⏳ Converting image…")
        try:
            await process_image(query, query.message, context, state)
        except Exception as e:
            logger.exception("Image processing error")
            try:
                await query.edit_message_text(f"❌ Error: {e}")
            except Exception:
                pass
        context.user_data.pop(key, None)
        context.user_data.pop("last_key", None)
        return

    # ── Speed selection → show compression ───────────────────────────────────
    if action == "sp":
        state["speed"] = value
        await query.edit_message_text("🗜 Compress file size? (re-encodes at lower bitrate)", reply_markup=kb_compress())
        return

    # ── Compression selection → process ───────────────────────────────────────
    if action == "cp":
        state["compress"] = value
        await query.edit_message_text("⏳ Processing…")
        message = query.message
        try:
            if state["type"] == "url":
                await process_url(query, message, context, state)
            elif state["type"] == "file":
                await process_file(query, message, context, state)
            elif state["type"] == "playlist":
                await process_playlist(query, message, context, state)
        except Exception as e:
            logger.exception("Error during processing")
            try:
                await query.edit_message_text(f"❌ Unexpected error: {e}")
            except Exception:
                pass
        context.user_data.pop(key, None)
        context.user_data.pop("last_key", None)
        return


# ── Image processing ──────────────────────────────────────────────────────────

async def process_image(query, message: Message, context: ContextTypes.DEFAULT_TYPE, state: dict):
    file_id   = state["file_id"]
    orig_name = state["file_name"]
    fmt_key   = state["image_format"]
    label, out_ext, _ = IMAGE_FORMATS[fmt_key]

    workdir = new_workdir()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                tg_file = await context.bot.get_file(file_id)
            except Exception as e:
                await query.edit_message_text(f"❌ Could not download image: {e}")
                return
            ext = Path(orig_name).suffix or ".jpg"
            src = os.path.join(tmpdir, f"input{ext}")
            await tg_file.download_to_drive(src)

            stem = Path(orig_name).stem
            dst  = str(workdir / f"{stem}.{out_ext}")
            loop = asyncio.get_event_loop()
            ok, err = await loop.run_in_executor(None, convert_image_sync, src, dst, fmt_key)
            if not ok:
                await query.edit_message_text(f"❌ Conversion failed:\n{err[:400]}")
                return

            orig_sz = size_str(src)
            out_sz  = size_str(dst)
            caption = f"🖼 {Path(dst).name}\nOriginal: {orig_sz}  →  {out_sz}\n📐 {label}"

            await query.edit_message_text("⬆️ Uploading…")
            with open(dst, "rb") as f:
                await message.reply_document(
                    document=f,
                    filename=Path(dst).name,
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                )

        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        raise

    try:
        await query.delete_message()
    except Exception:
        pass


# ── URL processing ─────────────────────────────────────────────────────────────

async def process_url(query, message: Message, context: ContextTypes.DEFAULT_TYPE, state: dict):
    global active_jobs
    if active_jobs >= MAX_JOBS:
        await query.edit_message_text(f"⏳ Bot is busy ({active_jobs}/{MAX_JOBS} jobs running). Please try again shortly.")
        return
    url        = state["url"]
    media_type = state["media_type"]
    quality    = state["quality"]
    speed      = state.get("speed", "1.0")
    compress   = state.get("compress", "none")
    domain     = domain_of(url)

    active_jobs += 1
    workdir = new_workdir()
    try:
        jobs_str = f"  [{active_jobs} active]" if active_jobs > 1 else ""
        await query.edit_message_text(f"⬇️ Downloading from {domain}…{jobs_str}")

        async def _status(text):
            try:
                await query.edit_message_text(text)
            except Exception:
                pass

        if "spotify.com" in url and media_type == "audio":
            out, err = await spotdl_download(url, str(workdir), quality)
            if not out:
                if "rate" in err.lower() or "limit" in err.lower():
                    await query.edit_message_text("❌ Spotify rate limit hit — try again in 24 hours.")
                else:
                    await query.edit_message_text(f"❌ Spotify download failed:\n{err[:400]}")
                return
        else:
            out, err = await ytdlp_download(url, str(workdir), media_type, quality, _status)

        if not out:
            if "drm" in err.lower():
                await query.edit_message_text("❌ This content is DRM-protected and cannot be downloaded.")
            else:
                await query.edit_message_text(f"❌ Download failed:\n{err[:400]}")
            return

        # Apply speed / compression to audio
        if media_type == "audio":
            loop = asyncio.get_event_loop()
            if speed != "1.0":
                await _status(f"⚡ Applying {speed}× speed…")
                spd_dst = str(Path(out).with_stem(Path(out).stem + f"_{speed}x"))
                ok, _ = await loop.run_in_executor(None, apply_speed_sync, out, spd_dst, speed)
                if ok:
                    out = spd_dst
            if compress != "none":
                await _status(f"🎛 Compressing…")
                cmp_dst = str(Path(out).with_name(Path(out).stem + f"_{compress}c.opus"))
                ok, _ = await loop.run_in_executor(None, compress_audio_sync, out, cmp_dst, compress)
                if ok:
                    out = cmp_dst

        mb_dl = os.path.getsize(out) / 1024 / 1024
        stats_add(downloads=1, mb_dl=mb_dl)

        effects = []
        if speed != "1.0":
            effects.append(f"⚡ {speed}×")
        if compress != "none":
            effects.append(f"🎛 {compress.capitalize()} compression")
        fx_label = "\n" + "  ".join(effects) if effects else ""
        caption = (
            f"🔗 {domain}\n"
            f"📁 {Path(out).name}\n"
            f"💾 {size_str(out)}\n"
            f"🎚 {quality}{'kbps' if media_type == 'audio' else 'p'}"
            f"{fx_label}"
        )

        await query.edit_message_text("⬆️ Uploading…")
        status_msg = await query.message.reply_text("⬆️ Uploading…") if os.path.getsize(out) > 49 * 1024 * 1024 else None
        if media_type == "audio":
            uploaded = await send_audio_file(message, out, caption, status_msg)
        else:
            uploaded = await send_video_file(message, out, caption, status_msg)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

    finally:
        active_jobs -= 1
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        await query.delete_message()
    except Exception:
        pass


# ── playlist processing ────────────────────────────────────────────────────────

async def process_playlist(query, message: Message, context: ContextTypes.DEFAULT_TYPE, state: dict):
    global active_jobs
    if active_jobs >= MAX_JOBS:
        await query.edit_message_text(f"⏳ Bot is busy ({active_jobs}/{MAX_JOBS} jobs running). Please try again shortly.")
        return
    url     = state["url"]
    quality = state["quality"]
    domain  = state.get("domain", domain_of(url))

    active_jobs += 1
    workdir = new_workdir()
    try:
        await query.edit_message_text(f"⬇️ Downloading playlist from {domain}…")

        async def _status(text):
            try:
                await query.edit_message_text(text)
            except Exception:
                pass

        if is_spotify_playlist(url):
            files = await spotdl_playlist_download(url, str(workdir), quality, _status)
        else:  # YouTube or SoundCloud — both handled by yt-dlp
            files = await ytdlp_playlist_download(url, str(workdir), quality, _status)

        if not files:
            await query.edit_message_text("❌ No tracks downloaded.")
            return

        total = len(files)
        collection_title = state.get("info", {}).get("title", domain)
        await query.edit_message_text(f"✅ {total} tracks ready. Uploading…")

        up_sem = asyncio.Semaphore(10)
        gates = [asyncio.Event() for _ in range(total + 1)]
        gates[0].set()

        async def _upload_ordered(i: int, fpath: str):
            async with up_sem:
                await gates[i].wait()
                gates[i + 1].set()
                try:
                    await send_audio_file(message, fpath, "")
                    stats_add(downloads=1, mb_dl=os.path.getsize(fpath) / 1024 / 1024)
                except Exception as e:
                    logger.warning("Track %d send failed: %s", i + 1, e)
                    await message.reply_text(f"⚠️ Track {i + 1} failed: {e}")

        await asyncio.gather(*[_upload_ordered(i, f) for i, f in enumerate(files)])

        await message.reply_text(f"🎵 {collection_title}\n{total} tracks")

    finally:
        active_jobs -= 1
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        await query.delete_message()
    except Exception:
        pass


# ── file processing ────────────────────────────────────────────────────────────

async def process_file(query, message: Message, context: ContextTypes.DEFAULT_TYPE, state: dict):
    global active_jobs
    if active_jobs >= MAX_JOBS:
        await query.edit_message_text(f"⏳ Bot is busy ({active_jobs}/{MAX_JOBS} jobs running). Please try again shortly.")
        return
    file_id    = state["file_id"]
    orig_name  = state["file_name"]
    media_type = state["media_type"]
    quality    = state["quality"]
    audio_fmt  = state.get("audio_format", "mp3")
    video_fmt  = state.get("video_format", "mp4")
    speed      = state.get("speed", "1.0")
    compress   = state.get("compress", "none")

    await query.edit_message_text("⬇️ Downloading your file…")

    active_jobs += 1
    workdir = new_workdir()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(orig_name).suffix or ".bin"
            src = os.path.join(tmpdir, f"input{ext}")
            try:
                tg_file = await context.bot.get_file(file_id)
                await tg_file.download_to_drive(src)
            except Exception:
                # Bot API limit hit — try Telethon MTProto downloader (up to 2 GB)
                await query.edit_message_text("⬇️ Large file — downloading via MTProto…")
                dl_chat_id    = state.get("chat_id")
                dl_message_id = state.get("message_id")
                if not dl_chat_id or not dl_message_id:
                    await query.edit_message_text("❌ Cannot download: missing message reference.")
                    return
                proc = await asyncio.create_subprocess_exec(
                    "python3.13", "/home/debian/telegram-tools/pyro_download.py",
                    str(dl_chat_id), str(dl_message_id), tmpdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out_path = ""
                try:
                    async with asyncio.timeout(600):
                        async for raw in proc.stdout:
                            line = raw.decode().strip()
                            if line.startswith("PROGRESS:"):
                                try:
                                    await query.edit_message_text(f"⬇️ MTProto: {line[9:]}")
                                except Exception:
                                    pass
                            elif line.startswith("ERROR:"):
                                out_path = line
                            elif line:
                                out_path = line   # final file path
                except asyncio.TimeoutError:
                    proc.kill()
                    await query.edit_message_text("❌ Download timed out (10 min limit).")
                    return
                await proc.wait()
                if not out_path or out_path.startswith("ERROR"):
                    stderr = (await proc.stderr.read()).decode().strip()
                    await query.edit_message_text(f"❌ Download failed: {out_path or stderr}")
                    return
                import shutil as _sh
                _sh.move(out_path, src)
            orig_sz    = size_str(src)
            orig_bytes = os.path.getsize(src)

            await query.edit_message_text("⚙️ Converting…")
            loop = asyncio.get_event_loop()

            # Check file actually downloaded
            src_size = os.path.getsize(src)
            if src_size < 1024:
                await query.edit_message_text(
                    f"❌ Download produced an empty/tiny file ({src_size} bytes).\n"
                    "Try re-sending the file."
                )
                return

            # Try to repair MOV/MP4 with moov atom at end (common with iPhone/Android recordings)
            repaired = os.path.join(tmpdir, f"repaired{ext}")
            repair_ok, _ = await loop.run_in_executor(None, run_ffmpeg, [
                "-analyzeduration", "200M", "-probesize", "200M",
                "-ignore_editlist", "1",
                "-i", src, "-c", "copy", "-movflags", "faststart", repaired
            ])
            if repair_ok and os.path.exists(repaired) and os.path.getsize(repaired) > 1024:
                src = repaired

            stem = Path(orig_name).stem

            if media_type == "audio":
                _, _, lossy, out_ext = AUDIO_FORMATS[audio_fmt]
                quality_label = "lossless" if not lossy else f"{quality} kbps"
                suffix = f"_{quality}k" if lossy else "_lossless"
                dst = str(workdir / f"{stem}{suffix}.{out_ext}")
                ok, err = await loop.run_in_executor(
                    None, convert_audio, src, dst, audio_fmt, quality if lossy else "128"
                )
                if not ok:
                    await query.edit_message_text(f"❌ Conversion failed:\n{err[:400]}")
                    return

                if speed != "1.0":
                    spd_dst = str(workdir / f"{stem}{suffix}_{speed}x.{out_ext}")
                    ok2, _ = await loop.run_in_executor(None, apply_speed_sync, dst, spd_dst, speed)
                    if ok2:
                        dst = spd_dst
                if compress != "none":
                    cmp_dst = str(workdir / f"{stem}{suffix}_{compress}c.opus")
                    ok3, _ = await loop.run_in_executor(None, compress_audio_sync, dst, cmp_dst, compress)
                    if ok3:
                        dst = cmp_dst

                out_sz = size_str(dst)
                ratio  = (1 - os.path.getsize(dst) / orig_bytes) * 100
                effects = []
                if speed != "1.0":
                    effects.append(f"⚡ {speed}×")
                if compress != "none":
                    effects.append(f"🎛 {compress.capitalize()} compression")
                fx_label = "\n" + "  ".join(effects) if effects else ""
                caption = (
                    f"📁 {Path(dst).name}\n"
                    f"Original: {orig_sz}  →  {out_sz}  ({ratio:+.0f}%)\n"
                    f"🎚 {AUDIO_FORMATS[audio_fmt][0]}  {quality_label}"
                    f"{fx_label}"
                )
                await query.edit_message_text("⬆️ Uploading…")
                status_msg = await query.message.reply_text("⬆️ Uploading…") if os.path.getsize(dst) > 49 * 1024 * 1024 else None
                uploaded = await send_audio_file(message, dst, caption, status_msg)
                if status_msg:
                    try: await status_msg.delete()
                    except Exception: pass
                stats_add(files_proc=1, mb_saved=max(0, (orig_bytes - os.path.getsize(dst)) / 1024 / 1024))

            else:
                _, _, _, out_ext = VIDEO_FORMATS[video_fmt]
                dst = str(workdir / f"{stem}_{quality}p.{out_ext}")
                ok, err = await loop.run_in_executor(
                    None, convert_video, src, dst, video_fmt, quality
                )
                if not ok:
                    await query.edit_message_text(f"❌ Conversion failed:\n{err[:400]}")
                    return
                out_sz = size_str(dst)
                ratio  = (1 - os.path.getsize(dst) / orig_bytes) * 100
                caption = (
                    f"📁 {Path(dst).name}\n"
                    f"Original: {orig_sz}  →  {out_sz}  ({ratio:+.0f}%)\n"
                    f"🎬 {VIDEO_FORMATS[video_fmt][0]}  {quality}p"
                )
                await query.edit_message_text("⬆️ Uploading…")
                status_msg = await query.message.reply_text("⬆️ Uploading…") if os.path.getsize(dst) > 49 * 1024 * 1024 else None
                uploaded = await send_video_file(message, dst, caption, status_msg)
                if status_msg:
                    try: await status_msg.delete()
                    except Exception: pass
                stats_add(files_proc=1, mb_saved=max(0, (orig_bytes - os.path.getsize(dst)) / 1024 / 1024))

    finally:
        active_jobs -= 1
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        await query.delete_message()
    except Exception:
        pass


# ── main ───────────────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception for update %s", update, exc_info=context.error)


async def post_init(app):
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start",     "Start the bot"),
        BotCommand("search",    "Search YouTube — /search <query>"),
        BotCommand("search_sc", "Search SoundCloud — /search_sc <query>"),
        BotCommand("search_sp", "Search Spotify — /search_sp <query>"),
        BotCommand("shazam",    "Identify a song — send /shazam then a voice message"),
        BotCommand("lyrics",    "Find lyrics — /lyrics <song name>"),
        BotCommand("find",      "Find song from lyrics — /find <lyric snippet>"),
        BotCommand("merge",     "Merge audio files — /merge"),
        BotCommand("stats",     "Bot statistics"),
        BotCommand("help",      "Help"),
    ])
    logger.info("Bot commands registered")


def main():
    # If a local Bot API server is running on port 8081, uncomment these two lines
    # to raise the file size limit from 20 MB → 2 GB:
    # LOCAL_API = "http://127.0.0.1:8081/bot"
    # LOCAL_FILE_API = "http://127.0.0.1:8081/file/bot"

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        # .base_url(LOCAL_API)
        # .base_file_url(LOCAL_FILE_API)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      start))
    app.add_handler(CommandHandler("search",    search_youtube))
    app.add_handler(CommandHandler("search_sc", search_soundcloud))
    app.add_handler(CommandHandler("search_sp", search_spotify))
    app.add_handler(CommandHandler("stats",     stats_cmd))
    app.add_handler(CommandHandler("shazam",    shazam_cmd))
    app.add_handler(CommandHandler("lyrics",    lyrics_cmd))
    app.add_handler(CommandHandler("find",      find_cmd))
    app.add_handler(CommandHandler("merge",     merge_start))
    app.add_handler(CommandHandler("done",      merge_done))
    app.add_handler(CommandHandler("cancel",    merge_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        (filters.AUDIO | filters.VIDEO | filters.VOICE | filters.PHOTO |
         filters.VIDEO_NOTE | filters.Document.ALL)
        & ~filters.ANIMATION
        & ~filters.Document.GIF,
        handle_file,
    ))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot running — downloads → %s", DOWNLOAD_DIR)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
