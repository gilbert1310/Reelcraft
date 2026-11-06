"""Render a cut plan to an MP4: virtual camera crops, shake, colour match, speed and
transitions, streamed frame by frame into ffmpeg."""
from __future__ import annotations

import logging
import math
import os
import subprocess
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .correct import Cam, RenderCut
from .media import FrameReader, MediaError, ffmpeg, has_filter

log = logging.getLogger(__name__)

DISSOLVE_S = 0.3
WHIP_FRAMES = 4
ZOOM_FRAMES = 6


def _ease(t: float) -> float:
    return t  # constant-speed moves, like a real camera operator


def _lerp_cam(a: Cam, b: Cam, t: float) -> Cam:
    t = _ease(t)
    # zoom interpolates geometrically so the push feels constant-speed
    return Cam(a.cx + (b.cx - a.cx) * t, a.cy + (b.cy - a.cy) * t,
               math.exp(math.log(a.z) + (math.log(b.z) - math.log(a.z)) * t))


def _shake(k: int, amp: float, seed: int) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    f = rng.uniform(0.6, 2.2, 6)
    ph = rng.uniform(0, 2 * np.pi, 6)
    t = k / 30.0
    dx = sum(np.sin(2 * np.pi * f[i] * t + ph[i]) for i in range(3)) / 3
    dy = sum(np.sin(2 * np.pi * f[i] * t + ph[i]) for i in range(3, 6)) / 3
    return amp * dx, amp * dy, amp * 20 * (dx - dy) / 2  # x, y (frac of width), degrees


def _colour_luts(c: dict) -> list[np.ndarray]:
    """Per-channel Reinhard transfer in Lab, baked into three 1D LUTs."""
    luts = []
    x = np.arange(256, dtype=np.float32)
    for ch in range(3):
        sm, ss = c["src_mean"][ch], max(c["src_std"][ch], 1.0)
        rm, rs = c["ref_mean"][ch], max(c["ref_std"][ch], 1.0)
        y = (x - sm) * (rs / ss) + rm
        y = x + (y - x) * c.get("strength", 0.7)
        luts.append(np.clip(y, 0, 255).astype(np.uint8))
    return luts


def _apply_colour(frame: np.ndarray, luts: list[np.ndarray]) -> np.ndarray:
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    chans = [cv2.LUT(lab[:, :, i], luts[i]) for i in range(3)]
    return cv2.cvtColor(cv2.merge(chans), cv2.COLOR_LAB2BGR)


def _prepass(rc: RenderCut, tmp: Path, out_fps: float, extra: float) -> tuple[str, float]:
    """Stabilise and/or interpolate the needed source span. Returns (path, time offset)."""
    if not (rc.stabilize or rc.interpolate):
        return rc.src_path, 0.0
    start = max(0.0, rc.src_in - 0.5)
    length = rc.src_len + extra + 1.0
    seg = tmp / f"pre_{rc.order_index}.mp4"
    vf = []
    if rc.stabilize:
        if has_filter("vidstabdetect"):
            trf = tmp / f"stab_{rc.order_index}.trf"
            ffmpeg("-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", rc.src_path,
                   "-vf", f"vidstabdetect=shakiness=6:result={trf}", "-f", "null", "-")
            vf.append(f"vidstabtransform=input={trf}:smoothing=12:zoom=4")
        else:
            vf.append("deshake")
    if rc.interpolate:
        target = min(240, max(out_fps / rc.speed, out_fps))
        vf.append(f"minterpolate=fps={target:.3f}:mi_mode=mci:mc_mode=aobmc:vsbmc=1")
    ffmpeg("-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", rc.src_path, "-vf", ",".join(vf),
           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(seg))
    return str(seg), start


class _Source:
    def __init__(self, rc: RenderCut, path: str, offset: float):
        self.rc = rc
        self.reader = FrameReader(path)
        self.offset = offset
        self.luts = _colour_luts(rc.colour) if rc.colour else None

    def frame(self, k: int, out_fps: float, out_w: int, out_h: int, extra_zoom: float = 1.0) -> np.ndarray:
        rc = self.rc
        t_out = k / out_fps
        src_t = rc.src_in + t_out * rc.speed - self.offset
        img = self.reader.frame_at(max(0.0, src_t))
        if img is None:
            return np.zeros((out_h, out_w, 3), np.uint8)
        sh, sw = img.shape[:2]
        cam = _lerp_cam(rc.cam_start, rc.cam_end, min(1.0, t_out / max(rc.out_len, 1e-3)))
        aspect = out_w / out_h
        base_w = min(sw, sh * aspect)
        cw = base_w / (cam.z * extra_zoom)
        scale = out_w / cw
        dx = dy = rot = 0.0
        if rc.shake_amp:
            dx, dy, rot = _shake(k, rc.shake_amp, rc.order_index)
        # Map source -> output: rotate/scale about the crop centre, then centre on the canvas.
        cx, cy = cam.cx * sw, cam.cy * sh
        m = cv2.getRotationMatrix2D((cx, cy), rot, scale)
        m[0, 2] += out_w / 2 - cx + dx * out_w
        m[1, 2] += out_h / 2 - cy + dy * out_w
        out = cv2.warpAffine(img, m, (out_w, out_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        if self.luts:
            out = _apply_colour(out, self.luts)
        return out

    def close(self):
        self.reader.close()


def _whip_blur(frame: np.ndarray, strength: float, direction: int) -> np.ndarray:
    k = max(3, int(frame.shape[1] * 0.08 * strength)) | 1
    kernel = np.zeros((1, k), np.float32)
    kernel[0, :] = 1.0 / k
    blurred = cv2.filter2D(frame, -1, kernel)
    shift = int(direction * strength * frame.shape[1] * 0.15)
    return np.roll(blurred, shift, axis=1)


class _Encoder:
    def __init__(self, path: str, w: int, h: int, fps: float, music: str | None):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:.3f}", "-i", "-"]
        if music:
            cmd += ["-i", music, "-map", "0:v", "-map", "1:a", "-shortest", "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", path]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.count = 0

    def write(self, frame: np.ndarray) -> None:
        self.p.stdin.write(frame.tobytes())
        self.count += 1

    def close(self) -> None:
        self.p.stdin.close()
        err = self.p.stderr.read().decode(errors="replace")
        if self.p.wait() != 0:
            raise MediaError(f"Encoding failed: {err[-400:]}")


def render(cuts: list[RenderCut], out_path: str, out_w: int, out_h: int, fps: float,
           tmp: Path, music_path: str | None = None) -> dict:
    """Render the plan. Returns {"frames", "duration"}."""
    out_w, out_h = out_w - out_w % 2, out_h - out_h % 2
    enc = _Encoder(out_path, out_w, out_h, fps, music_path)
    queue: deque[np.ndarray] = deque()
    tail: list[np.ndarray] = []  # frames past the previous cut's end, for dissolves
    try:
        for i, rc in enumerate(cuts):
            nxt = cuts[i + 1] if i + 1 < len(cuts) else None
            n = max(1, int(round(rc.out_len * fps)))
            d_next = int(round(DISSOLVE_S * fps)) if nxt and nxt.transition_in == "dissolve" else 0
            d_next = min(d_next, n // 2, int(round(nxt.out_len * fps)) // 2) if nxt else 0
            path, offset = _prepass(rc, tmp, fps, extra=d_next / fps * rc.speed)
            src = _Source(rc, path, offset)
            try:
                if rc.transition_in == "whip" and queue:
                    for j, qf in enumerate(list(queue)[-WHIP_FRAMES:]):
                        idx = len(queue) - min(WHIP_FRAMES, len(queue)) + j
                        queue[idx] = _whip_blur(qf, (j + 1) / WHIP_FRAMES, -1)
                for k in range(n):
                    extra = 1.0
                    if rc.transition_in == "zoom" and k < ZOOM_FRAMES:
                        extra = 1.0 + 0.25 * (1 - k / ZOOM_FRAMES)
                    f = src.frame(k, fps, out_w, out_h, extra)
                    if rc.transition_in == "whip" and k < WHIP_FRAMES:
                        f = _whip_blur(f, 1 - k / WHIP_FRAMES, -1)
                    if rc.transition_in == "dissolve" and k < len(tail):
                        a = (k + 1) / (len(tail) + 1)
                        f = cv2.addWeighted(tail[k], 1 - a, f, a, 0)
                    queue.append(f)
                    while len(queue) > WHIP_FRAMES + 1:
                        enc.write(queue.popleft())
                tail = [src.frame(n + k, fps, out_w, out_h) for k in range(d_next)]
            finally:
                src.close()
                if path != rc.src_path and os.path.exists(path):
                    os.remove(path)
        while queue:
            enc.write(queue.popleft())
    finally:
        enc.close()
    return {"frames": enc.count, "duration": enc.count / fps}
