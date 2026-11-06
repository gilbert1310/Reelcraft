"""Built-in transitions, per-cut effects and colour looks for the renderer.

Everything works on BGR uint8 frames of the output size, so the finished video is produced
on the platform with no editing app needed.
"""
from __future__ import annotations

import cv2
import numpy as np

# Transitions into a cut. OVERLAP ones blend the previous cut's continuing frames with the new cut.
TRANSITIONS = ("cut", "dissolve", "whip", "zoom", "match", "flash", "slide_left", "slide_right", "slide_up",
               "slide_down", "blur", "fade_black", "glitch")
OVERLAP = {"dissolve", "slide_left", "slide_right", "slide_up", "slide_down", "blur"}
OUTRO = {"whip": 4, "fade_black": 6, "glitch": 3}   # frames of the previous cut that get treated
INTRO = {"whip": 4, "zoom": 6, "flash": 4, "fade_black": 6, "glitch": 3}

EFFECTS = ("grain", "vignette", "rgb_split", "shake", "black_white", "letterbox")
LOOKS = ("none", "warm", "teal_orange", "black_white", "faded_film", "high_contrast", "cool")


def ease(a: float) -> float:
    a = min(max(a, 0.0), 1.0)
    return a * a * (3 - 2 * a)


def _motion_blur(frame: np.ndarray, strength: float, horizontal: bool = True) -> np.ndarray:
    k = max(3, int(frame.shape[1] * 0.08 * strength)) | 1
    kernel = np.zeros((1, k) if horizontal else (k, 1), np.float32)
    kernel[:] = 1.0 / k
    return cv2.filter2D(frame, -1, kernel)


def _rgb_split(frame: np.ndarray, px: int) -> np.ndarray:
    if px <= 0:
        return frame
    b, g, r = cv2.split(frame)
    return cv2.merge([np.roll(b, -px, axis=1), g, np.roll(r, px, axis=1)])


def blend_in(kind: str, prev: np.ndarray, new: np.ndarray, a: float) -> np.ndarray:
    """Overlap transitions: `a` goes 0 -> 1 across the overlap."""
    e = ease(a)
    h, w = new.shape[:2]
    if kind == "dissolve":
        return cv2.addWeighted(prev, 1 - a, new, a, 0)
    if kind == "blur":
        k = int(31 * (1 - abs(2 * a - 1))) | 1
        mix = cv2.addWeighted(prev, 1 - a, new, a, 0)
        return cv2.GaussianBlur(mix, (k, k), 0) if k > 1 else mix
    if kind.startswith("slide_"):
        out = prev.copy()
        if kind in ("slide_left", "slide_right"):
            dx = int(w * (1 - e)) * (1 if kind == "slide_left" else -1)
            m = np.float32([[1, 0, dx], [0, 1, 0]])
            pm = np.float32([[1, 0, dx - (w if kind == "slide_left" else -w)], [0, 1, 0]])
        else:
            dy = int(h * (1 - e)) * (1 if kind == "slide_up" else -1)
            m = np.float32([[1, 0, 0], [0, 1, dy]])
            pm = np.float32([[1, 0, 0], [0, 1, dy - (h if kind == "slide_up" else -h)]])
        out = cv2.warpAffine(prev, pm, (w, h), borderMode=cv2.BORDER_CONSTANT)
        newm = cv2.warpAffine(new, m, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        mask = cv2.warpAffine(np.full((h, w), 255, np.uint8), m, (w, h), borderMode=cv2.BORDER_CONSTANT)
        out[mask > 0] = newm[mask > 0]
        return out
    return new


def intro(kind: str, frame: np.ndarray, k: int) -> np.ndarray:
    """Effects on the first frames of a cut (k = frame index within the cut)."""
    n = INTRO.get(kind, 0)
    if k >= n:
        return frame
    a = 1 - k / n  # strength, 1 -> 0
    if kind == "whip":
        return np.roll(_motion_blur(frame, a), int(-a * frame.shape[1] * 0.15), axis=1)
    if kind == "flash":
        return cv2.addWeighted(frame, 1 - a * 0.9, np.full_like(frame, 255), a * 0.9, 0)
    if kind == "fade_black":
        return (frame.astype(np.float32) * (1 - a)).astype(np.uint8)
    if kind == "glitch":
        out = _rgb_split(frame, int(frame.shape[1] * 0.02 * a))
        rng = np.random.default_rng(k)
        h = out.shape[0]
        for _ in range(4):
            y = int(rng.integers(0, h - 20))
            hh = int(rng.integers(8, max(9, h // 12)))
            out[y:y + hh] = np.roll(out[y:y + hh], int(rng.integers(-40, 40)), axis=1)
        return out
    return frame


def outro(kind: str, frame: np.ndarray, j: int, n: int) -> np.ndarray:
    """Effects on the last n frames of the previous cut (j = 0 .. n-1, last is n-1)."""
    a = (j + 1) / n  # strength, rising towards the cut
    if kind == "whip":
        return np.roll(_motion_blur(frame, a), int(a * frame.shape[1] * 0.15), axis=1)
    if kind == "fade_black":
        return (frame.astype(np.float32) * (1 - a)).astype(np.uint8)
    if kind == "glitch":
        return _rgb_split(frame, int(frame.shape[1] * 0.02 * a))
    return frame


_vignette_cache: dict[tuple[int, int], np.ndarray] = {}


def _vignette_mask(h: int, w: int) -> np.ndarray:
    if (h, w) not in _vignette_cache:
        y, x = np.ogrid[:h, :w]
        d = np.sqrt(((x - w / 2) / (w / 2)) ** 2 + ((y - h / 2) / (h / 2)) ** 2)
        _vignette_cache[(h, w)] = np.clip(1.15 - 0.45 * d ** 2, 0.45, 1.0).astype(np.float32)[..., None]
    return _vignette_cache[(h, w)]


def apply_effects(frame: np.ndarray, effects: list[str], k: int) -> np.ndarray:
    """Per-cut effects, applied to every frame of the cut ('shake' is handled by the camera)."""
    if not effects:
        return frame
    out = frame
    if "black_white" in effects:
        out = cv2.cvtColor(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    if "rgb_split" in effects:
        out = _rgb_split(out, max(2, out.shape[1] // 200))
    if "vignette" in effects:
        out = (out.astype(np.float32) * _vignette_mask(*out.shape[:2])).astype(np.uint8)
    if "grain" in effects:
        rng = np.random.default_rng(k * 7919 + 13)
        noise = rng.normal(0, 9, out.shape[:2]).astype(np.float32)[..., None]
        out = np.clip(out.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    if "letterbox" in effects:
        out = out.copy()
        bar = out.shape[0] // 9
        out[:bar] = 0
        out[-bar:] = 0
    return out


def _look_luts(look: str) -> list[np.ndarray] | None:
    x = np.arange(256, dtype=np.float32) / 255
    def s_curve(v, amt):
        return v + amt * (v - 0.5) * (1 - np.abs(2 * v - 1))
    if look == "warm":
        b, g, r = x * 0.92, x * 1.0, np.minimum(x * 1.08 + 0.02, 1)
    elif look == "cool":
        b, g, r = np.minimum(x * 1.08 + 0.02, 1), x * 1.0, x * 0.92
    elif look == "teal_orange":
        sh = 1 - x  # shadows teal, highlights orange
        b = x + 0.06 * sh - 0.05 * x
        g = x + 0.02 * sh
        r = x - 0.05 * sh + 0.06 * x
        b, g, r = s_curve(b, 0.25), s_curve(g, 0.25), s_curve(r, 0.25)
    elif look == "faded_film":
        f = lambda v: 0.08 + v * 0.86
        b, g, r = f(x) * 0.97, f(x), f(x) * 1.02
    elif look == "high_contrast":
        b = g = r = s_curve(x, 0.6)
    else:
        return None
    return [np.clip(c * 255, 0, 255).astype(np.uint8) for c in (b, g, r)]


_look_cache: dict[str, list[np.ndarray] | None] = {}


def apply_look(frame: np.ndarray, look: str | None) -> np.ndarray:
    """A colour look over the whole edit."""
    if not look or look == "none":
        return frame
    if look == "black_white":
        return cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    if look not in _look_cache:
        _look_cache[look] = _look_luts(look)
    luts = _look_cache[look]
    if luts is None:
        return frame
    b, g, r = cv2.split(frame)
    return cv2.merge([cv2.LUT(b, luts[0]), cv2.LUT(g, luts[1]), cv2.LUT(r, luts[2])])
