"""Timeline export for DaVinci Resolve: FCPXML 1.10 (File > Import > Timeline) and
OpenTimelineIO (.otio, Resolve 18.5+). Both reference the user's original clips by file
name, so Resolve relinks them to the full-quality media on the editor's machine."""
from __future__ import annotations

from fractions import Fraction
from xml.etree import ElementTree as ET

from .correct import Cam, RenderCut

MEDIA_ROOT = "file:///ReelCraft/Media/"


def _frame_duration(fps: float) -> Fraction:
    for nominal, fd in ((23.976, Fraction(1001, 24000)), (29.97, Fraction(1001, 30000)),
                        (59.94, Fraction(1001, 60000))):
        if abs(fps - nominal) < 0.01:
            return fd
    return Fraction(1, int(round(fps)) or 30)


def _t(seconds: float, fd: Fraction) -> str:
    """Seconds -> rational time string snapped to whole frames."""
    frames = round(Fraction(seconds).limit_denominator(100000) / fd)
    v = frames * fd
    return f"{v.numerator}/{v.denominator}s" if v.denominator != 1 else f"{v.numerator}s"


def _fmt_id(cache: dict, res: ET.Element, w: int, h: int, fps: float) -> str:
    key = (w, h, round(fps, 3))
    if key not in cache:
        fid = f"r{len(cache) + 1}"
        fd = _frame_duration(fps)
        ET.SubElement(res, "format", id=fid, frameDuration=f"{fd.numerator}/{fd.denominator}s",
                      width=str(w), height=str(h), name=f"FFVideoFormat{w}x{h}p{round(fps)}")
        cache[key] = fid
    return cache[key]


def _transform(rc: RenderCut, cam: Cam, out_w: int, out_h: int) -> tuple[float, float, float]:
    """FCPXML adjust-transform for a virtual-camera state: (scale, pos_x, pos_y).

    FCP conforms sources with "fit"; our zoom 1 is "cover", so scale by cover/fit first.
    Position is in percent of the sequence frame height, y up."""
    fit = min(out_w / rc.src_w, out_h / rc.src_h)
    cover = max(out_w / rc.src_w, out_h / rc.src_h)
    scale = cover / fit * cam.z
    px_per_src = fit * scale
    dx = -(cam.cx - 0.5) * rc.src_w * px_per_src
    dy = (cam.cy - 0.5) * rc.src_h * px_per_src
    return scale, dx / out_h * 100, dy / out_h * 100


# Transitions with no standard FCPXML form: a marker tells the editor where to drop one.
TRANSITION_TIPS = {
    "whip": "Add a whip-pan transition here (DaVinci: Effects > Video Transitions, or a directional blur)",
    "zoom": "Add a zoom transition here (DaVinci: Effects > Video Transitions > Smooth Cut / zoom)",
    "match": "Match cut here: line up the movement across the cut",
}


def fcpxml(cuts: list[RenderCut], title: str, out_w: int, out_h: int, fps: float) -> str:
    fd = _frame_duration(fps)
    root = ET.Element("fcpxml", version="1.10")
    res = ET.SubElement(root, "resources")
    formats: dict = {}
    seq_fmt = _fmt_id(formats, res, out_w, out_h, fps)
    assets: dict[str, str] = {}
    for rc in cuts:
        if rc.src_name in assets:
            continue
        aid = f"a{len(assets) + 1}"
        afmt = _fmt_id(formats, res, rc.src_w, rc.src_h, rc.src_fps)
        a = ET.SubElement(res, "asset", id=aid, name=rc.src_name, start="0s",
                          duration=_t(3600, _frame_duration(rc.src_fps)), hasVideo="1", hasAudio="1",
                          format=afmt, audioSources="1", audioChannels="2")
        ET.SubElement(a, "media-rep", kind="original-media", src=MEDIA_ROOT + rc.src_name)
        assets[rc.src_name] = aid

    # Final Cut's Cross Dissolve; DaVinci Resolve and Premiere map it to their own dissolve.
    dissolve_ref = None
    if any(rc.transition_in == "dissolve" for rc in cuts[1:]):
        dissolve_ref = f"r{len(formats) + len(assets) + 10}"
        ET.SubElement(res, "effect", id=dissolve_ref, name="Cross Dissolve",
                      uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265")

    lib = ET.SubElement(root, "library")
    event = ET.SubElement(lib, "event", name="ReelCraft")
    project = ET.SubElement(event, "project", name=title)
    total = sum(rc.out_len for rc in cuts)
    seq = ET.SubElement(project, "sequence", format=seq_fmt, duration=_t(total, fd), tcStart="0s",
                        tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    offset = 0.0
    for i, rc in enumerate(cuts):
        if i > 0 and rc.transition_in == "dissolve" and dissolve_ref:
            half = min(0.15, rc.out_len / 3, cuts[i - 1].out_len / 3)
            t = ET.SubElement(spine, "transition", name="Cross Dissolve", offset=_t(offset - half, fd),
                              duration=_t(2 * half, fd))
            ET.SubElement(t, "filter-video", ref=dissolve_ref, name="Cross Dissolve")
        retimed = abs(rc.speed - 1) > 1e-3
        # With a timeMap, start/duration are in the clip's local (retimed) time.
        local_start = rc.src_in / rc.speed if retimed else rc.src_in
        clip = ET.SubElement(spine, "asset-clip", ref=assets[rc.src_name], name=f"Cut {rc.order_index + 1}",
                             offset=_t(offset, fd), start=_t(local_start, fd), duration=_t(rc.out_len, fd),
                             format=seq_fmt, tcFormat="NDF")
        if retimed:
            tm = ET.SubElement(clip, "timeMap")
            end_local = local_start + rc.out_len + 1
            ET.SubElement(tm, "timept", time="0s", value="0s", interp="linear")
            ET.SubElement(tm, "timept", time=_t(end_local, fd), value=_t(end_local * rc.speed, fd), interp="linear")
        s0, x0, y0 = _transform(rc, rc.cam_start, out_w, out_h)
        s1, x1, y1 = _transform(rc, rc.cam_end, out_w, out_h)
        animated = abs(s1 - s0) > 1e-3 or abs(x1 - x0) > 1e-2 or abs(y1 - y0) > 1e-2
        tr = ET.SubElement(clip, "adjust-transform", position=f"{x0:.3f} {y0:.3f}", scale=f"{s0:.4f} {s0:.4f}")
        if animated:
            t0, t1 = _t(local_start, fd), _t(local_start + rc.out_len, fd)
            for name, a, b in (("position", f"{x0:.3f} {y0:.3f}", f"{x1:.3f} {y1:.3f}"),
                               ("scale", f"{s0:.4f} {s0:.4f}", f"{s1:.4f} {s1:.4f}")):
                p = ET.SubElement(tr, "param", name=name)
                ka = ET.SubElement(p, "keyframeAnimation")
                ET.SubElement(ka, "keyframe", time=t0, value=a)
                ET.SubElement(ka, "keyframe", time=t1, value=b)
        # Markers go last inside the clip (FCPXML element order).
        notes = []
        if i > 0 and rc.transition_in in TRANSITION_TIPS:
            notes.append(TRANSITION_TIPS[rc.transition_in])
        if rc.fixes:
            notes.append("Fixes: " + ", ".join(rc.fixes))
        notes += rc.flags
        if notes:
            ET.SubElement(clip, "marker", start=_t(local_start, fd), duration=_t(1 / fps, fd), value=" | ".join(notes))
        offset += rc.out_len
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode")


def otio_json(cuts: list[RenderCut], title: str, out_w: int, out_h: int, fps: float) -> str:
    import opentimelineio as otio

    rate = 1 / float(_frame_duration(fps))
    tl = otio.schema.Timeline(name=title, global_start_time=otio.opentime.RationalTime(0, rate))
    tl.metadata["reelcraft"] = {"width": out_w, "height": out_h, "fps": fps}
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    tl.tracks.append(track)
    for rc in cuts:
        src_rate = rc.src_fps or rate
        ref = otio.schema.ExternalReference(
            target_url=MEDIA_ROOT + rc.src_name,
            available_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0, src_rate),
                                                    otio.opentime.RationalTime(3600 * src_rate, src_rate)))
        # source_range duration = time on the track; the LinearTimeWarp scales media consumption.
        sr = otio.opentime.TimeRange(otio.opentime.RationalTime(round(rc.src_in * src_rate), src_rate),
                                     otio.opentime.RationalTime(round(rc.out_len * src_rate), src_rate))
        clip = otio.schema.Clip(name=f"Cut {rc.order_index + 1}", media_reference=ref, source_range=sr)
        if abs(rc.speed - 1) > 1e-3:
            clip.effects.append(otio.schema.LinearTimeWarp(name="Speed", time_scalar=rc.speed))
        clip.metadata["reelcraft"] = rc.to_dict()
        if rc.fixes or rc.flags:
            clip.markers.append(otio.schema.Marker(
                name=", ".join(rc.fixes) or "Note",
                marked_range=otio.opentime.TimeRange(sr.start_time, otio.opentime.RationalTime(1, src_rate)),
                metadata={"flags": rc.flags}))
        if rc.transition_in == "dissolve" and len(track):
            half = otio.opentime.RationalTime(round(0.15 * rate), rate)
            track.append(otio.schema.Transition(name="Dissolve",
                                                transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                                                in_offset=half, out_offset=half))
        track.append(clip)
    return otio.adapters.write_to_string(tl, "otio_json")


def edit_decision_summary(cuts: list[RenderCut]) -> list[dict]:
    """Plain-language cut list, shown in the app and handy when relinking by hand."""
    out, t = [], 0.0
    for rc in cuts:
        out.append({"cut": rc.order_index + 1, "timeline_in": round(t, 3), "clip": rc.src_name,
                    "source_in": round(rc.src_in, 3), "source_out": round(rc.src_in + rc.src_len, 3),
                    "speed": rc.speed, "transition_in": rc.transition_in, "fixes": rc.fixes})
        t += rc.out_len
    return out

