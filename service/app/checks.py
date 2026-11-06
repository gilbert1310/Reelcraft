"""On-set checks: technical checks, AI coverage check per reference cut, and setup
suggestion for clips imported from a pro camera."""
from __future__ import annotations

import logging

import cv2
import httpx
import numpy as np

from . import vision
from .config import settings
from .embed import normalize
from .matching import index_clip, match_sequence
from .media import FrameReader, probe
from .motion import ShotStats, analyze_segment

log = logging.getLogger(__name__)


def _fetch_image(url: str | None) -> np.ndarray | None:
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        log.warning("Could not fetch reference thumbnail")
        return None


def technical_checks(path: str, setup: dict, cuts: list[dict]) -> tuple[list[str], list[str], dict]:
    """Returns (problems that need a retake, warnings, measurements)."""
    info = probe(path)
    problems, warnings = [], []
    target = float(setup.get("film_duration_seconds") or 0)
    needed = sum((c["end_time"] - c["start_time"]) * float(c.get("speed") or 1) for c in cuts)
    if info.duration < needed * 1.1:
        problems.append(f"Too short: hold the shot about {max(1, round(max(target, needed * 1.5) - info.duration))} more seconds.")
    elif target and info.duration < target * 0.85:
        warnings.append(f"A bit short of the {target:.0f}s target. More footage gives the editor options.")
    w, h = info.display_size
    if min(w, h) < 1080:
        warnings.append("Low resolution: film in 4K (or at least 1080p) so the app can reframe and push in.")
    if any(float(c.get("speed") or 1) < 1 for c in cuts) and info.fps < 50:
        warnings.append("This setup has slow motion. Film at 60fps for smooth slow motion.")
    with FrameReader(path) as r:
        stats = analyze_segment(r, 0, info.duration, rate=6, max_frames=60)
    ref_handheld = any(ShotStats.from_dict(c.get("stats")).shake > 0.006 for c in cuts)
    if stats.shake > 0.03 and not ref_handheld:
        problems.append("Too shaky to fix. Hold the phone with both hands and keep your elbows tucked in.")
    elif stats.shake > 0.012 and not ref_handheld:
        warnings.append("A little shaky. The app will stabilise it.")
    if stats.brightness < 18:
        warnings.append("Very dark. Find more light or face the subject toward the light.")
    meas = {"duration": info.duration, "fps": info.fps, "width": w, "height": h, "shake": stats.shake,
            "brightness": stats.brightness}
    return problems, warnings, meas


def coverage_check(path: str, setup: dict, cuts: list[dict], use_ai: bool = True) -> dict:
    """Does this clip cover every reference cut that comes from this setup?"""
    cuts = sorted(cuts, key=lambda c: c["order_index"])
    problems, warnings, meas = technical_checks(path, setup, cuts)
    with FrameReader(path) as r:
        idx = index_clip(r)
        refs = [np.array(c["embedding"], np.float32) for c in cuts]
        needs = [(c["end_time"] - c["start_time"]) * float(c.get("speed") or 1) for c in cuts]
        matches = match_sequence(idx, refs, needs)
        setup_sim = float(np.mean(idx.vecs @ normalize(np.array(setup["embedding"], np.float32)))) \
            if setup.get("embedding") and len(idx.vecs) else 0.0
        per_cut = []
        for c, m, need in zip(cuts, matches, needs):
            status = "covered" if m.score >= settings.covered_sim else "weak" if m.score >= settings.weak_sim else "missing"
            per_cut.append({"cut_id": c.get("id"), "order_index": c["order_index"], "status": status,
                            "score": round(m.score, 4), "match_start": round(m.start, 3),
                            "match_end": round(m.start + need, 3),
                            "reason": "" if status == "covered" else
                            f"Couldn't find '{c.get('action') or 'this moment'}' clearly in your clip."})
        matches_setup = True
        setup_feedback = ""
        if use_ai:
            parts = [f"Camera setup to film: {setup.get('description')}",
                     "For each reference cut below you see the original frame, then two frames from the "
                     "part of the user's clip that seems to match it."]
            for c, pc in zip(cuts, per_cut):
                ref_img = _fetch_image(c.get("thumbnail_url"))
                user = [r.small_at(t) for t in (pc["match_start"] + 0.1, pc["match_end"] - 0.1)]
                parts.append(f"Reference cut {c['order_index'] + 1}: needs '{c.get('action') or 'the same moment'}'")
                if ref_img is not None:
                    parts.append(ref_img)
                parts += [u for u in user if u is not None]
            parts.append("Does the user's clip match the setup (angle, subject, action)? For each reference "
                         "cut, is the needed moment covered, weak (partly, or from a slightly different "
                         "angle), or missing? Be specific and friendly in reasons, e.g. 'Cut 7 needs the "
                         "bride's face as she turns; she faces away in your clip.'")
            try:
                v = vision.ask(vision.content(parts), vision.CoverageVerdict)
                matches_setup, setup_feedback = v.matches_setup, v.setup_feedback
                by_num = {x.cut_number - 1: x for x in v.cuts}
                for pc in per_cut:
                    x = by_num.get(pc["order_index"])
                    if x:
                        pc["status"] = x.status
                        pc["reason"] = "" if x.status == "covered" else x.reason
            except Exception:
                log.exception("AI coverage check failed; using fingerprint coverage only")
                warnings.append("AI check unavailable; checked by visual similarity only.")
    if not matches_setup:
        problems.insert(0, setup_feedback or "This doesn't look like the requested setup.")
    missing = [pc for pc in per_cut if pc["status"] == "missing"]
    for pc in missing:
        problems.append(f"Cut {pc['order_index'] + 1}: {pc['reason']}")
    status = "pass" if not problems else "retry"
    feedback = " ".join(problems) if problems else ("Great shot! " + " ".join(warnings)).strip()
    return {"check_status": status, "check_feedback": feedback, "problems": problems, "warnings": warnings,
            "measurements": meas, "coverage": per_cut, "setup_similarity": round(setup_sim, 4)}


def suggest_setup(path: str, setups: list[dict], use_ai: bool = True) -> dict:
    with FrameReader(path) as r:
        idx = index_clip(r, rate=1.0, max_samples=60)
        clip_vec = normalize(idx.vecs.mean(axis=0))
        frames = [r.small_at(t) for t in np.linspace(0.15, 0.85, 3) * r.duration]
    ranking = sorted(({"setup_number": s["setup_number"],
                       "score": round(float(clip_vec @ normalize(np.array(s["embedding"], np.float32))), 4)}
                      for s in setups if s.get("embedding")), key=lambda x: -x["score"])
    if not ranking:
        return {"suggested_setup_number": None, "confidence": 0, "ranking": []}
    best = ranking[0]
    confidence = min(1.0, max(0.0, (best["score"] - (ranking[1]["score"] if len(ranking) > 1 else 0)) * 10))
    if use_ai and len(ranking) > 1 and confidence < 0.5:
        parts = ["A videographer imported this clip from their camera:", *[f for f in frames if f is not None],
                 "Which of these setups from the shoot plan does it belong to?"]
        for s in setups:
            parts.append(f"Setup {s['setup_number']}: {s.get('description', '')}")
            img = _fetch_image(s.get("thumbnail_url"))
            if img is not None:
                parts.append(img)
        try:
            v = vision.ask(vision.content(parts), vision.SetupChoice)
            if any(s["setup_number"] == v.setup_number for s in setups):
                best = {"setup_number": v.setup_number}
                confidence = v.confidence
        except Exception:
            log.exception("AI setup suggestion failed")
    return {"suggested_setup_number": best["setup_number"], "confidence": round(confidence, 3), "ranking": ranking}
