"""Shot analysis used by the match engine and checks: camera motion, shake, subject
framing, colour. All numbers are normalised to the frame so different resolutions compare."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from .media import FrameReader

ANALYSIS_W = 360

_hog = None


def _hog_detector():
    """OpenCV's built-in person detector (None if this OpenCV build lacks it)."""
    global _hog
    if _hog is None:
        if not hasattr(cv2, "HOGDescriptor"):
            _hog = False
        else:
            _hog = cv2.HOGDescriptor()
            _hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    return _hog or None


@dataclass
class Box:
    cx: float  # centre x, 0..1
    cy: float  # centre y, 0..1
    h: float   # height as a fraction of frame height
    w: float
    source: str = "saliency"


@dataclass
class ShotStats:
    pan_x: float = 0.0       # frame-widths per second (+ = camera pans right / content moves left)
    pan_y: float = 0.0       # frame-heights per second (+ = camera tilts down)
    zoom: float = 0.0        # log-scale per second (+ = pushing in)
    rotation: float = 0.0    # degrees per second
    shake: float = 0.0       # jitter, fraction of frame width (0.002 steady, >0.01 handheld)
    brightness: float = 0.0  # mean L, 0..100
    lab_mean: list[float] = field(default_factory=list)  # OpenCV 8-bit Lab
    lab_std: list[float] = field(default_factory=list)
    subject_start: dict | None = None
    subject_end: dict | None = None
    sharpness: float = 0.0
    duration: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ShotStats":
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _small(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    s = ANALYSIS_W / w
    return cv2.resize(frame, (ANALYSIS_W, max(2, int(h * s))), interpolation=cv2.INTER_AREA)


def subject_box(frame: np.ndarray) -> Box:
    """Largest person (HOG) or, failing that, the most salient region."""
    small = _small(frame)
    H, W = small.shape[:2]
    rects, weights = [], []
    hog = _hog_detector()
    if hog is not None:
        try:
            rects, weights = hog.detectMultiScale(small, winStride=(8, 8), padding=(8, 8), scale=1.05)
        except cv2.error:
            pass
    if len(rects):
        scores = [float(np.ravel(w)[0]) * r[2] * r[3] for r, w in zip(rects, weights)]
        x, y, w, h = rects[int(np.argmax(scores))]
        if scores[int(np.argmax(scores))] > 0:
            return Box((x + w / 2) / W, (y + h / 2) / H, h / H, w / W, "person")
    # Spectral-residual saliency (Hou & Zhang 2007).
    g = cv2.resize(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (64, 64)).astype(np.float32)
    f = np.fft.fft2(g)
    log_amp = np.log(np.abs(f) + 1e-6)
    residual = log_amp - cv2.blur(log_amp, (3, 3))
    sal = np.abs(np.fft.ifft2(np.exp(residual + 1j * np.angle(f)))) ** 2
    sal = cv2.GaussianBlur(sal, (9, 9), 2.5)
    mask = sal > np.percentile(sal, 90)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return Box(0.5, 0.5, 0.5, 0.5)
    return Box(float(xs.mean() / 64), float(ys.mean() / 64),
               float(max(np.ptp(ys) / 64, 0.2)), float(max(np.ptp(xs) / 64, 0.2)))


def _pair_motion(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float, float] | None:
    """Similarity transform a->b: (dx, dy, log_scale, rot_deg) in analysis pixels."""
    ga, gb = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    pts = cv2.goodFeaturesToTrack(ga, maxCorners=300, qualityLevel=0.01, minDistance=8)
    if pts is None or len(pts) < 8:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(ga, gb, pts, None, winSize=(21, 21), maxLevel=3)
    good = st.ravel() == 1
    if good.sum() < 8:
        return None
    m, _ = cv2.estimateAffinePartial2D(pts[good], nxt[good], method=cv2.RANSAC, ransacReprojThreshold=3)
    if m is None:
        return None
    scale = float(np.hypot(m[0, 0], m[1, 0]))
    rot = float(np.degrees(np.arctan2(m[1, 0], m[0, 0])))
    # Remove the zoom's contribution to translation (zoom about the frame centre).
    h, w = ga.shape
    cx, cy = w / 2, h / 2
    dx = m[0, 2] - (cx - (m[0, 0] * cx + m[0, 1] * cy))
    dy = m[1, 2] - (cy - (m[1, 0] * cx + m[1, 1] * cy))
    return float(dx), float(dy), float(np.log(max(scale, 1e-3))), rot


def analyze_frames(frames: list[np.ndarray], step_seconds: float) -> ShotStats:
    stats = ShotStats(duration=step_seconds * max(len(frames) - 1, 0))
    if not frames:
        return stats
    small = [_small(f) for f in frames]
    H, W = small[0].shape[:2]
    motions = [m for m in (_pair_motion(a, b) for a, b in zip(small, small[1:])) if m is not None]
    if motions:
        arr = np.array(motions)
        # Content moving left (dx<0) means the camera pans right.
        stats.pan_x = float(-np.median(arr[:, 0]) / W / step_seconds)
        stats.pan_y = float(-np.median(arr[:, 1]) / H / step_seconds)
        stats.zoom = float(np.median(arr[:, 2]) / step_seconds)
        stats.rotation = float(np.median(arr[:, 3]) / step_seconds)
        if len(arr) >= 3:
            traj = np.cumsum(arr[:, :2], axis=0)
            k = min(5, len(traj))
            smooth = np.array([np.convolve(traj[:, i], np.ones(k) / k, mode="same") for i in range(2)]).T
            stats.shake = float(np.sqrt(((traj - smooth)[1:-1] ** 2).sum(axis=1)).mean() / W) if len(traj) > 2 else 0.0
    labs = [cv2.cvtColor(s, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32) for s in small[:: max(1, len(small) // 4)]]
    lab = np.concatenate(labs)
    # OpenCV 8-bit Lab: L in 0..255 -> 0..100
    stats.lab_mean = [float(v) for v in lab.mean(axis=0)]
    stats.lab_std = [float(v) for v in lab.std(axis=0)]
    stats.brightness = stats.lab_mean[0] * 100 / 255
    stats.sharpness = float(np.mean([cv2.Laplacian(cv2.cvtColor(s, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                                     for s in small[:: max(1, len(small) // 3)]]))
    stats.subject_start = asdict(subject_box(frames[0]))
    stats.subject_end = asdict(subject_box(frames[-1]))
    return stats


def analyze_segment(reader: FrameReader, start: float, end: float, rate: float = 8.0,
                    max_frames: int = 48) -> ShotStats:
    length = max(end - start, 1.0 / reader.fps)
    n = int(min(max_frames, max(2, length * rate)))
    times = np.linspace(start, max(start, end - 1.0 / reader.fps), n)
    frames = [f for f in (reader.small_at(t) for t in times) if f is not None]
    step = length / max(n - 1, 1)
    s = analyze_frames(frames, step)
    s.duration = float(length)
    return s


def describe_motion(s: ShotStats) -> str:
    parts = []
    if s.zoom > 0.04:
        parts.append("push-in")
    elif s.zoom < -0.04:
        parts.append("pull-out")
    if abs(s.pan_x) > 0.04:
        parts.append("pan right" if s.pan_x > 0 else "pan left")
    if abs(s.pan_y) > 0.04:
        parts.append("tilt down" if s.pan_y > 0 else "tilt up")
    parts.append("handheld" if s.shake > 0.006 else "steady")
    return ", ".join(parts)
