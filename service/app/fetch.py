"""Fetch a public reel from a social link with yt-dlp (see the risks in the README)."""
from __future__ import annotations

import os
from urllib.parse import urlparse

from .config import settings

ALLOWED_HOSTS = (
    "instagram.com", "tiktok.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be",
)


class LinkFetchError(Exception):
    pass


def check_link(url: str) -> None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise LinkFetchError("Only public Instagram, TikTok, Facebook or YouTube links are supported.")


def fetch_from_link(url: str, folder: str) -> str:
    import yt_dlp

    check_link(url)

    def too_long(info, *, incomplete):
        d = info.get("duration")
        if d and d > settings.max_video_seconds:
            return f"Video is longer than {settings.max_video_seconds // 60} minutes"
        return None

    opts = {
        "outtmpl": os.path.join(folder, "reel.%(ext)s"),
        "format": "best[ext=mp4][height<=1920]/bestvideo[height<=1920]+bestaudio/best",
        "merge_output_format": "mp4",
        "max_filesize": settings.max_download_mb * 1024 * 1024,
        "match_filter": too_long,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise LinkFetchError("That link has no downloadable video.")
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                base = os.path.splitext(path)[0]
                path = next((base + ext for ext in (".mp4", ".mkv", ".webm") if os.path.exists(base + ext)), path)
            if not os.path.exists(path):
                reason = info.get("__match_filter_reason") or "The video was skipped."
                raise LinkFetchError(str(reason))
            return path
    except LinkFetchError:
        raise
    except Exception as e:
        raise LinkFetchError("Couldn't fetch that link. It may be private or the platform blocked it.") from e
