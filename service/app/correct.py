"""Match engine: compare each original cut with the matched piece of the user's footage
and plan the fixes that make it look like the original (the "virtual camera")."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .motion import ShotStats


@dataclass
class Cam:
    """Virtual camera: crop centre in source-normalised coords and zoom (1 = largest
    crop with the output aspect that fits the source)."""
    cx: float = 0.5
    cy: float = 0.5
    z: float = 1.0


@dataclass
class RenderCut:
    order_index: int
    src_path: str
    src_name: str
    src_w: int
    src_h: int
    src_fps: float
    src_in: float
    speed: float
    out_len: float
    transition_in: str = "cut"
    cam_start: Cam = field(default_factory=Cam)
    cam_end: Cam = field(default_factory=Cam)
    shake_amp: float = 0.0           # fraction of output width
    stabilize: bool = False
    interpolate: bool = False
    colour: dict | None = None       # {"src_mean","src_std","ref_mean","ref_std","strength"}
    fixes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    score: int = 0
    cut_id: str | None = None
    setup_number: int | None = None

    @property
    def src_len(self) -> float:
        return self.out_len * self.speed

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("src_path")
        return d


def _zoom_limits(src_w: int, src_h: int, out_w: int, out_h: int) -> float:
    """Highest zoom before the crop drops below ~90% of the output resolution."""
    aspect = out_w / out_h
    base_h = min(src_h, src_w / aspect)
    return max(1.0, base_h / (out_h * 0.9))


def _clamp_cam(cam: Cam, src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[Cam, bool]:
    aspect = out_w / out_h
    base_w = min(src_w, src_h * aspect)
    cw, ch = base_w / cam.z / src_w, base_w / aspect / cam.z / src_h  # normalised crop size
    cx = min(max(cam.cx, cw / 2), 1 - cw / 2)
    cy = min(max(cam.cy, ch / 2), 1 - ch / 2)
    hit = abs(cx - cam.cx) > 1e-3 or abs(cy - cam.cy) > 1e-3
    return Cam(cx, cy, cam.z), hit


def _framing_cam(ref_box: dict | None, user_box: dict | None, src_w: int, src_h: int,
                 out_w: int, out_h: int, zmax: float) -> Cam | None:
    """Crop that puts the user's subject at the reference subject's size and position."""
    if not ref_box or not user_box:
        return None
    aspect = out_w / out_h
    base_h = min(src_h, src_w / aspect)
    # subject height in source pixels / desired fraction of the crop
    want_ch = user_box["h"] * src_h / max(ref_box["h"], 0.05)
    z = min(max(base_h / want_ch, 1.0), zmax)
    ch = base_h / z
    cw = ch * aspect
    # place the subject at the reference's relative position inside the crop
    left = user_box["cx"] * src_w - ref_box["cx"] * cw
    top = user_box["cy"] * src_h - ref_box["cy"] * ch
    return Cam((left + cw / 2) / src_w, (top + ch / 2) / src_h, z)


def plan_cut(rc: RenderCut, ref: ShotStats, user: ShotStats, out_w: int, out_h: int,
             match_score: float) -> RenderCut:
    zmax = _zoom_limits(rc.src_w, rc.src_h, out_w, out_h)
    dur = rc.out_len
    start = Cam()

    # 1. Framing: only when both detections are people, or the difference is large.
    rb, ub = ref.subject_start, user.subject_start
    if rb and ub:
        both_people = rb.get("source") == "person" and ub.get("source") == "person"
        size_gap = abs(rb["h"] - ub["h"])
        pos_gap = math.hypot(rb["cx"] - ub["cx"], rb["cy"] - ub["cy"])
        if both_people and (size_gap > 0.08 or pos_gap > 0.08):
            cam = _framing_cam(rb, ub, rc.src_w, rc.src_h, out_w, out_h, zmax)
            if cam:
                start = cam
                rc.fixes.append("Reframed")
                if cam.z >= zmax - 1e-3 and rb["h"] > ub["h"] * 1.2:
                    rc.flags.append("Subject is smaller than the original and there isn't enough "
                                    "resolution to crop in fully. Film closer or in 4K.")

    # 2. Motion the user's clip is missing (virtual camera).
    end = Cam(start.cx, start.cy, start.z)
    dz = ref.zoom - user.zoom
    if abs(dz) > 0.03:
        if dz > 0:
            # Leave room to push in: start a little wider if we can't go tighter.
            target = start.z * math.exp(dz * dur)
            if target > zmax:
                start.z = max(1.0, start.z / (target / zmax))
                target = zmax
                if start.z <= 1.0 + 1e-6 and target < start.z * math.exp(dz * dur) * 0.98:
                    rc.flags.append("Push-in is gentler than the original (not enough resolution).")
            end.z = target
            rc.fixes.append("Digital push-in")
        else:
            # A pull-out needs a tighter start so there is room to widen.
            start.z = min(zmax, max(start.z, math.exp(-dz * dur)))
            end.z = max(1.0, start.z * math.exp(dz * dur))
            rc.fixes.append("Digital pull-out")
    dpx, dpy = ref.pan_x - user.pan_x, ref.pan_y - user.pan_y
    if abs(dpx) > 0.03 or abs(dpy) > 0.03:
        # pan speed is in output-frame widths per second; convert to source-normalised
        aspect = out_w / out_h
        base_w = min(rc.src_w, rc.src_h * aspect)
        view_w = base_w / max(end.z, 1.0) / rc.src_w
        view_h = view_w * rc.src_w / aspect / rc.src_h
        if start.z <= 1.02:
            start.z = end.z = max(start.z, min(zmax, 1.25))
        sx, sy = dpx * dur * view_w, dpy * dur * view_h
        start.cx, end.cx = start.cx - sx / 2, end.cx + sx / 2
        start.cy, end.cy = start.cy - sy / 2, end.cy + sy / 2
        rc.fixes.append("Virtual pan" if abs(dpx) >= abs(dpy) else "Virtual tilt")

    # 3. Shake
    if user.shake > max(ref.shake * 1.5, 0.006):
        rc.stabilize = True
        rc.fixes.append("Stabilized")
    elif ref.shake > 0.006 and user.shake < ref.shake * 0.5:
        rc.shake_amp = min(ref.shake - user.shake, 0.02)
        start.z, end.z = max(start.z, 1.05), max(end.z, 1.05)
        rc.fixes.append("Handheld shake added")

    # 4. Speed
    if rc.speed < 0.99:
        if rc.src_fps * rc.speed < 24:
            rc.interpolate = True
            rc.fixes.append("Slow motion (interpolated)")
            if rc.src_fps < 50:
                rc.flags.append("Slow motion was made by frame interpolation. Film at 60fps for cleaner results.")
        else:
            rc.fixes.append("Slow motion")
    elif rc.speed > 1.01:
        rc.fixes.append("Sped up")

    # 5. Colour
    if ref.lab_std and user.lab_std and ref.lab_mean and user.lab_mean:
        gap = sum(abs(a - b) for a, b in zip(ref.lab_mean, user.lab_mean)) + \
            0.5 * sum(abs(a - b) for a, b in zip(ref.lab_std, user.lab_std))
        if gap > 9:
            rc.colour = {"src_mean": user.lab_mean, "src_std": user.lab_std,
                         "ref_mean": ref.lab_mean, "ref_std": ref.lab_std, "strength": 0.7}
            rc.fixes.append("Colour matched")

    start, hit1 = _clamp_cam(start, rc.src_w, rc.src_h, out_w, out_h)
    end, hit2 = _clamp_cam(end, rc.src_w, rc.src_h, out_w, out_h)
    if (hit1 or hit2) and any(f.startswith("Virtual") for f in rc.fixes):
        rc.flags.append("Camera move was limited by the edge of the frame.")
    rc.cam_start, rc.cam_end = start, end

    # Score: similarity of content, minus penalties for what couldn't be fixed.
    score = 100 * max(0.0, min(1.0, (match_score - 0.5) / 0.45))
    score -= 8 * len(rc.flags)
    rc.score = int(max(0, min(100, round(score))))
    return rc
