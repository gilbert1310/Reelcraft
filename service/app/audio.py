"""Music and rhythm: tempo of the soundtrack and how tightly the edit cuts on the beat."""
from __future__ import annotations

import logging
import subprocess

import numpy as np

log = logging.getLogger(__name__)
RATE = 11025          # Hz, plenty for beat tracking
HOP = 256             # samples per onset-envelope step (~23 ms)


def load_audio(path: str, max_seconds: float = 600) -> np.ndarray | None:
    """Mono float32 samples, or None if the video has no audio."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-t", str(max_seconds),
                        "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    x = np.frombuffer(r.stdout, dtype=np.float32)
    return x if x.size > RATE and np.abs(x).max() > 1e-4 else None


def onset_envelope(x: np.ndarray) -> np.ndarray:
    """Spectral-flux onset strength, one value per HOP samples."""
    win = np.hanning(1024).astype(np.float32)
    n = 1 + (len(x) - 1024) // HOP
    if n < 8:
        return np.zeros(0, np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, 1024)[::HOP][:n] * win
    mag = np.log1p(np.abs(np.fft.rfft(frames, axis=1)) * 10)
    flux = np.maximum(np.diff(mag, axis=0), 0).sum(axis=1)
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")   # remove slow loudness changes
    return np.maximum(flux, 0)


def estimate_tempo(env: np.ndarray) -> float | None:
    """Beats per minute from the autocorrelation of the onset envelope (60-180 BPM)."""
    if env.size < 64 or env.std() == 0:
        return None
    e = (env - env.mean()) / env.std()
    ac = np.correlate(e, e, mode="full")[e.size - 1:]
    fps = RATE / HOP
    lags = np.arange(ac.size)
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 60)
    if hi >= ac.size:
        return None
    # Prefer tempos near 120 BPM a little (resolves half/double-time ambiguity).
    bpm = 60 * fps / np.maximum(lags[lo:hi], 1)
    weight = np.exp(-0.5 * (np.log2(bpm / 120) / 0.9) ** 2)
    k = lo + int(np.argmax(ac[lo:hi] * weight))
    return round(float(60 * fps / k), 1)


def beat_times(env: np.ndarray, bpm: float, duration: float) -> np.ndarray:
    """Beat grid at `bpm`, with the phase that best lines up with the onsets."""
    fps = RATE / HOP
    period = 60 / bpm
    phases = np.linspace(0, period, 24, endpoint=False)
    def score(ph: float) -> float:
        idx = (np.arange(ph, duration, period) * fps).astype(int)
        idx = idx[idx < env.size]
        return float(env[idx].sum()) if idx.size else 0.0
    best = max(phases, key=score)
    return np.arange(best, duration, period)


def rhythm(path: str, cut_starts: list[float], duration: float) -> dict:
    """Tempo and how many cuts land on (or very near) a beat."""
    x = load_audio(path)
    if x is None:
        return {"has_audio": False}
    env = onset_envelope(x)
    bpm = estimate_tempo(env)
    if not bpm:
        return {"has_audio": True, "bpm": None}
    beats = beat_times(env, bpm, duration)
    cuts = [t for t in cut_starts if t > 0.05]
    tolerance = 0.09  # seconds: about two frames at 24-30fps
    on_beat = [t for t in cuts if beats.size and np.min(np.abs(beats - t)) <= tolerance]
    half = 60 / bpm / 2  # also count cuts on the off-beat (half-beat)
    on_half = [t for t in cuts if beats.size and np.min(np.abs(np.abs(beats - t) - half)) <= tolerance]
    return {
        "has_audio": True,
        "bpm": bpm,
        "cuts_on_beat": len(on_beat),
        "cuts_on_half_beat": len(set(on_half) - set(on_beat)),
        "cuts_total": len(cuts),
        "beat_sync_pct": round(100 * len(on_beat) / len(cuts)) if cuts else None,
    }
