"""Stage 2 assembly: slice the user's clips to the reference edit pattern, run the
match engine on every cut, render the preview and write the Resolve timelines."""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import numpy as np

from .config import settings
from .correct import RenderCut, plan_cut
from .matching import ClipIndex, index_clip, match_sequence
from .media import FrameReader, download, ffmpeg, normalise, probe, to_jpeg, upload
from .motion import ShotStats, analyze_segment
from .render import _Source, render
from .timeline import edit_decision_summary, fcpxml, otio_json

log = logging.getLogger(__name__)


class Clip:
    def __init__(self, cid: str, path: str, name: str, setup_number: int | None):
        self.id, self.path, self.name, self.setup_number = cid, path, name, setup_number
        self.info = probe(path)
        with FrameReader(path) as r:
            first = r.frame_at(0)
            # Decoded frames are already rotated upright, so use their real size.
            self.frame_size = first.shape[:2] if first is not None else (self.info.height, self.info.width)
            self.index: ClipIndex = index_clip(r)


def _safe_name(name: str | None, fallback: str) -> str:
    name = (name or fallback).replace("/", "_").replace("\\", "_")
    return name if "." in name else name + ".mp4"


def load_clips(items: list[dict], tmp: Path) -> list[Clip]:
    clips = []
    for i, it in enumerate(items):
        path = normalise(download(it["url"], tmp / f"clip_{i}"))
        clips.append(Clip(str(it.get("id") or i), str(path), _safe_name(it.get("file_name"), f"clip_{i + 1}"),
                          it.get("setup_number")))
    return clips


def _thumb(rc: RenderCut, w: int, h: int, fps: float) -> str | None:
    try:
        s = _Source(rc, rc.src_path, 0.0)
        try:
            f = s.frame(int(rc.out_len * fps / 2), fps, w, h)
        finally:
            s.close()
        return base64.b64encode(to_jpeg(f, 360, 78)).decode()
    except Exception:
        return None


def _black_clip(tmp: Path, seconds: float, w: int, h: int) -> Clip:
    path = tmp / "graphic_placeholder.mp4"
    ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={w - w % 2}x{h - h % 2}:r=30:d={max(seconds, 1.0):.2f}",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path))
    return Clip("graphic", str(path), "GRAPHIC_PLACEHOLDER.mp4", None)


def build_cut_plan(ref_cuts: list[dict], clips: list[Clip], out_w: int, out_h: int,
                   graphic_setups: set[int] | None = None, tmp: Path | None = None) -> tuple[list[RenderCut], list[dict]]:
    """Match every reference cut to the best moment in the user's clips for its setup and
    plan corrections. Returns (render cuts in edit order, reshoots)."""
    ref_cuts = sorted(ref_cuts, key=lambda c: c["order_index"])
    by_setup: dict[int, list[dict]] = {}
    for c in ref_cuts:
        by_setup.setdefault(int(c["setup_number"]), []).append(c)

    chosen: dict[int, tuple[Clip, float, float]] = {}  # order_index -> (clip, start, score)
    reshoots = []
    graphic_setups = graphic_setups or set()
    graphic_cuts: set[int] = set()
    if graphic_setups & set(by_setup):
        # Logo/end cards aren't filmed: hold their place in the timing with black.
        g_cuts = [c for sn in graphic_setups for c in by_setup.get(sn, [])]
        black = _black_clip(tmp or Path(clips[0].path).parent, sum(c["end_time"] - c["start_time"] for c in g_cuts) + 1,
                            out_w, out_h)
        cursor = 0.0
        for c in g_cuts:
            chosen[c["order_index"]] = (black, cursor, 1.0)
            graphic_cuts.add(c["order_index"])
            cursor += c["end_time"] - c["start_time"]
    for sn, cuts in by_setup.items():
        if sn in graphic_setups:
            continue
        pool = [cl for cl in clips if cl.setup_number == sn]
        if not pool:
            reshoots.append({"setup_number": sn, "cut_order_indexes": [c["order_index"] for c in cuts],
                             "reason": f"No clip for setup {sn} yet."})
            pool = clips  # still render something so the rhythm is visible
        refs = [np.array(c["embedding"], np.float32) for c in cuts]
        needs = [(c["end_time"] - c["start_time"]) * float(c.get("speed") or 1) for c in cuts]
        per_clip = [(cl, match_sequence(cl.index, refs, needs)) for cl in pool]
        for j, c in enumerate(cuts):
            cl, ms = max(per_clip, key=lambda p: p[1][j].score)
            chosen[c["order_index"]] = (cl, ms[j].start, ms[j].score)
            if pool is not clips and ms[j].score < settings.weak_sim:
                reshoots.append({"setup_number": sn, "cut_order_indexes": [c["order_index"]],
                                 "reason": f"Cut {c['order_index'] + 1} needs "
                                           f"'{c.get('action') or 'a moment'}' and it wasn't found in your clips."})

    plan = []
    readers: dict[str, FrameReader] = {}
    try:
        for c in ref_cuts:
            cl, start, score = chosen[c["order_index"]]
            speed = float(c.get("speed") or 1)
            out_len = c["end_time"] - c["start_time"]
            h, w = cl.frame_size
            rc = RenderCut(order_index=c["order_index"], src_path=cl.path, src_name=cl.name,
                           src_w=w, src_h=h, src_fps=cl.info.fps, src_in=start, speed=speed,
                           out_len=out_len, transition_in=c.get("transition_in") or "cut",
                           effects=[e for e in (c.get("effects") or []) if isinstance(e, str)],
                           cut_id=c.get("id"), setup_number=int(c["setup_number"]))
            if c["order_index"] in graphic_cuts:
                rc.fixes, rc.score = ["Graphic: add your logo or end card in the editor"], 100
                plan.append(rc)
                continue
            r = readers.setdefault(cl.path, FrameReader(cl.path))
            user_stats = analyze_segment(r, start, start + rc.src_len)
            plan.append(plan_cut(rc, ShotStats.from_dict(c.get("stats")), user_stats, out_w, out_h, score))
    finally:
        for r in readers.values():
            r.close()
    return plan, reshoots


def assemble(payload: dict, tmp: Path) -> dict:
    out_w = int(payload.get("width") or settings.out_width)
    out_h = int(payload.get("height") or settings.out_height)
    fps = float(payload.get("fps") or 30)
    fps = 30.0 if fps > 61 or fps < 15 else fps
    clips = load_clips(payload["captures"], tmp)
    plan, reshoots = build_cut_plan(payload["cuts"], clips, out_w, out_h,
                                    set(int(n) for n in payload.get("graphic_setups") or []), tmp)
    return finish(plan, payload, tmp, out_w, out_h, fps, reshoots, payload.get("title") or "ReelCraft edit")


def finish(plan: list[RenderCut], payload: dict, tmp: Path, out_w: int, out_h: int, fps: float,
           reshoots: list[dict], title: str) -> dict:
    """Render + export + upload; shared by Stage 2 and Stage 3."""
    out = payload.get("outputs") or {}
    music = None
    if payload.get("music_url"):
        music = str(download(payload["music_url"], tmp / "music.m4a"))
    scale = float(payload.get("preview_scale") or settings.preview_scale)
    pw, ph = int(out_w * scale), int(out_h * scale)
    result: dict = {}
    if out.get("preview_upload_url") or payload.get("render_preview", True):
        preview = tmp / "preview.mp4"
        stats = render(plan, str(preview), pw, ph, fps, tmp, music, look=payload.get("look"))
        result["preview"] = stats
        if out.get("preview_upload_url"):
            upload(out["preview_upload_url"], preview, "video/mp4")
        result["preview_local"] = str(preview)
    fx = fcpxml(plan, title, out_w, out_h, fps)
    ot = otio_json(plan, title, out_w, out_h, fps)
    (tmp / "timeline.fcpxml").write_text(fx)
    (tmp / "timeline.otio").write_text(ot)
    if out.get("fcpxml_upload_url"):
        upload(out["fcpxml_upload_url"], tmp / "timeline.fcpxml", "application/xml")
    if out.get("otio_upload_url"):
        upload(out["otio_upload_url"], tmp / "timeline.otio", "application/json")
    matches = [{"cut_id": rc.cut_id, "order_index": rc.order_index, "score": rc.score, "fixes": rc.fixes,
                "flags": rc.flags, "clip": rc.src_name, "source_in": round(rc.src_in, 3),
                "thumbnail_b64": _thumb(rc, 360, int(360 * out_h / out_w), fps)} for rc in plan]
    result.update(
        overall_score=int(round(float(np.mean([m["score"] for m in matches])))) if matches else 0,
        cut_matches=matches,
        reshoots=reshoots,
        edit_list=edit_decision_summary(plan),
        plan=[rc.to_dict() for rc in plan],
    )
    return result
