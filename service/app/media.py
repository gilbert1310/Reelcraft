"""Video I/O helpers: download, probe, frame sampling, ffmpeg wrappers."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import httpx
import numpy as np

log = logging.getLogger(__name__)


class MediaError(Exception):
    pass


ANALYSIS_MAX_SIDE = 640


def download(url: str, dest: str | Path, max_mb: int = 2048) -> Path:
    """Stream a (signed) URL to disk."""
    dest = Path(dest)
    limit = max_mb * 1024 * 1024
    size = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as r:
        if r.status_code >= 400:
            raise MediaError(f"Download failed with HTTP {r.status_code}")
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise MediaError(f"File is larger than {max_mb} MB")
                f.write(chunk)
    return dest


def upload(signed_url: str, path: str | Path, content_type: str) -> None:
    """PUT a file to a Supabase Storage signed upload URL."""
    with open(path, "rb") as f:
        r = httpx.put(signed_url, content=f.read(), headers={"content-type": content_type,
                                                              "x-upsert": "true"}, timeout=300)
    if r.status_code >= 400:
        raise MediaError(f"Upload failed with HTTP {r.status_code}: {r.text[:200]}")


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    frames: int
    rotation: int = 0
    codec: str = ""
    pix_fmt: str = ""
    container: str = ""

    @property
    def display_size(self) -> tuple[int, int]:
        return (self.height, self.width) if self.rotation in (90, 270) else (self.width, self.height)


def probe(path: str | Path) -> VideoInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,codec_name,pix_fmt"
         ":stream_side_data=rotation:format=duration,format_name", "-of", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise MediaError(f"Not a readable video: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise MediaError("No video stream found")
    s = streams[0]

    def rate(v: str | None) -> float:
        if not v or v == "0/0":
            return 0.0
        n, _, d = v.partition("/")
        return float(n) / float(d or 1)

    fps = rate(s.get("avg_frame_rate")) or rate(s.get("r_frame_rate")) or 30.0
    duration = float(s.get("duration") or data.get("format", {}).get("duration") or 0)
    frames = int(s.get("nb_frames") or round(duration * fps))
    rotation = 0
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"]) % 360
    return VideoInfo(int(s["width"]), int(s["height"]), fps, duration, frames, abs(rotation),
                     s.get("codec_name", ""), s.get("pix_fmt", ""), data.get("format", {}).get("format_name", ""))


DECODABLE_CODECS = ("h264", "hevc", "vp9", "mpeg4")
EIGHT_BIT_420 = ("yuv420p", "yuvj420p", "nv12")


def needs_normalising(info: VideoInfo) -> bool:
    """Browser recordings (WebM, often without a duration) and 10-bit camera files decode
    unreliably frame-by-frame; convert only those to 8-bit H.264 MP4. Re-encoding is slow
    and memory-hungry, so anything the frame reader handles well is left alone."""
    return (info.duration <= 0 or info.frames <= 0 or "webm" in info.container or "matroska" in info.container
            or info.codec not in DECODABLE_CODECS or info.pix_fmt not in EIGHT_BIT_420)


def normalise(path: str | Path) -> Path:
    """Return a path to a reliably decodable copy of the video (or the original if fine)."""
    path = Path(path)
    info = probe(path)
    if not needs_normalising(info):
        return path
    out = path.with_name(path.stem + "_norm.mp4")
    # Few threads and a short lookahead keep ffmpeg's memory use low on small servers.
    # Cap the size (NORMALISE_MAX_SIDE, default 1920): encoding 4K needs more memory than small
    # servers have, and 1080x1920 is the output size anyway.
    import os
    side = int(os.getenv("NORMALISE_MAX_SIDE", "1920"))
    vf = (f"scale='min(iw,{side})':'min(ih,{side})':force_original_aspect_ratio=decrease,"
          "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")
    ffmpeg("-threads", "2", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-vf", vf, "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "18", "-x264-params", "rc-lookahead=10:threads=2",
           "-c:a", "aac", "-movflags", "+faststart", str(out))
    return out


def fetch_video(url: str, dest: str | Path, max_mb: int = 2048) -> Path:
    """Download + normalise."""
    return normalise(download(url, dest, max_mb))


class FrameReader:
    """Random-access frame reader with a small forward-seek optimisation.

    OpenCV applies the container's rotation metadata, so frames come out upright.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise MediaError(f"Cannot open video {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._pos = 0
        self._last: np.ndarray | None = None
        self._last_idx = -1

    @property
    def duration(self) -> float:
        return self.count / self.fps if self.fps else 0.0

    def frame_at_index(self, idx: int) -> np.ndarray | None:
        idx = max(0, min(idx, max(self.count - 1, 0)))
        if idx == self._last_idx and self._last is not None:
            return self._last
        if idx < self._pos or idx - self._pos > 45:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            self._pos = idx
        frame = None
        while self._pos <= idx:
            ok, frame = self.cap.read()
            if not ok:
                frame = self._last
                break
            self._pos += 1
        if frame is not None:
            self._last, self._last_idx = frame, idx
        return frame

    def frame_at(self, t: float) -> np.ndarray | None:
        return self.frame_at_index(int(round(t * self.fps)))

    def small_at(self, t: float, max_side: int = ANALYSIS_MAX_SIDE) -> np.ndarray | None:
        """A downscaled frame for analysis. Holding full 1080x1920 (or 4K) frames in lists
        costs 6-25 MB each; every analysis step works at 640 px or less."""
        f = self.frame_at(t)
        return None if f is None else resize_max(f, max_side)

    def close(self) -> None:
        self.cap.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def sample_times(start: float, end: float, n: int, margins: tuple[float, ...] | None = None) -> list[float]:
    """n timestamps spread through [start, end]; defaults to 10%/50%/90% style spacing."""
    length = max(end - start, 1e-3)
    if margins is None:
        margins = tuple(np.linspace(0.1, 0.9, n)) if n > 1 else (0.5,)
    return [start + m * length for m in margins]


def resize_max(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return img
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)


def to_jpeg(img: np.ndarray, max_side: int = 512, quality: int = 82) -> bytes:
    ok, buf = cv2.imencode(".jpg", resize_max(img, max_side), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise MediaError("JPEG encode failed")
    return buf.tobytes()


def ffmpeg(*args: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode < 0 or r.returncode == 137:
        # Killed by a signal; on small servers that is almost always the out-of-memory killer.
        raise MediaError("Video processing was stopped because the server ran out of memory. "
                         "Give the video service more memory (2 GB or more) and try again.")
    if r.returncode != 0:
        raise MediaError(f"ffmpeg failed (exit {r.returncode}): {r.stderr.strip()[-400:] or 'no error output'}")


_FILTERS: set[str] | None = None


def has_filter(name: str) -> bool:
    global _FILTERS
    if _FILTERS is None:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        _FILTERS = {line.split()[1] for line in r.stdout.splitlines()[8:] if len(line.split()) > 1}
    return name in _FILTERS


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("ffmpeg and ffprobe must be installed")
