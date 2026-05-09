from __future__ import annotations

import re


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text or "")


def domain_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    if not m:
        return ""
    return m.group(1).lstrip("www.")


AUDIO_ONLY_DOMAINS = {
    "spotify.com",
    "open.spotify.com",
    "soundcloud.com",
    "m.soundcloud.com",
    "deezer.com",
    "www.deezer.com",
    "music.apple.com",
    "tidal.com",
}


def is_audio_only_platform(url: str) -> bool:
    d = domain_of(url)
    return any(d == ao or d.endswith("." + ao) for ao in AUDIO_ONLY_DOMAINS)


def is_youtube_playlist(url: str) -> bool:
    return "youtube.com" in url and "list=" in url and "watch?v=" not in url


def is_spotify_playlist(url: str) -> bool:
    return "spotify.com" in url and any(x in url for x in ("/playlist/", "/album/", "/artist/"))


def is_soundcloud_playlist(url: str) -> bool:
    return "soundcloud.com" in url and "/sets/" in url

