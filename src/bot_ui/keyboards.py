from __future__ import annotations

from typing import Dict, List, Tuple

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


AUDIO_QUALITIES: List[Tuple[str, str]] = [
    ("24 kbps", "24"),
    ("64 kbps", "64"),
    ("128 kbps", "128"),
    ("192 kbps", "192"),
    ("320 kbps", "320"),
]

VIDEO_QUALITIES: List[Tuple[str, str]] = [
    ("360p", "360"),
    ("480p", "480"),
    ("720p", "720"),
    ("1080p", "1080"),
    ("4K", "2160"),
]

SPEED_OPTIONS: List[Tuple[str, str]] = [
    ("0.5×", "0.5"),
    ("0.75×", "0.75"),
    ("1×", "1.0"),
    ("1.25×", "1.25"),
    ("1.5×", "1.5"),
    ("2×", "2.0"),
]


IMAGE_FORMATS: Dict[str, Tuple[str, str]] = {
    "jpg": ("JPG", "jpg"),
    "png": ("PNG", "png"),
    "webp": ("WEBP", "webp"),
    "gif": ("GIF", "gif"),
    "bmp": ("BMP", "bmp"),
    "tiff": ("TIFF", "tiff"),
}


AUDIO_FORMATS: Dict[str, Tuple[str, str, bool, str]] = {
    "mp3": ("MP3", "libmp3lame", True, "mp3"),
    "aac": ("AAC", "aac", True, "m4a"),
    "ogg": ("OGG", "libvorbis", True, "ogg"),
    "opus": ("OPUS", "libopus", True, "opus"),
    "m4a": ("M4A", "aac", True, "m4a"),
    "flac": ("FLAC", "flac", False, "flac"),
    "wav": ("WAV", "pcm_s16le", False, "wav"),
}


VIDEO_FORMATS: Dict[str, Tuple[str, str, str, str]] = {
    "mp4": ("MP4", "libx264", "aac", "mp4"),
    "mkv": ("MKV", "libx264", "aac", "mkv"),
    "mov": ("MOV", "libx264", "aac", "mov"),
    "webm": ("WEBM", "libvpx-vp9", "libopus", "webm"),
    "avi": ("AVI", "libxvid", "libmp3lame", "avi"),
}


def kb_media_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            [
                InlineKeyboardButton("🎵 Audio", callback_data="mt|a"),
                InlineKeyboardButton("🎬 Video", callback_data="mt|v"),
            ]
        ]]
    )


def kb_custom_bitrate_prompt() -> InlineKeyboardMarkup:
    """Simple prompt for custom bitrate selection.

    Callback creates a state where user replies with a value.
    """

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➡️ Enter bitrate", callback_data="cb|custom_bitrate")],
            [InlineKeyboardButton("🎲 Use recommended", callback_data="cb|recommended")],
        ]
    )


