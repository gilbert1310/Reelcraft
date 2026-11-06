"""Stage 3: label raw footage, pick fitting recipes from the style library, build and
revise cut plans, render previews and export."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from . import vision
from .assemble import Clip, finish, load_clips
from .config import settings
from .correct import RenderCut, plan_cut
from .embed import embed_texts, normalize
from .media import FrameReader
from .motion import ShotStats, analyze_segment

log = logging.getLogger(__name__)
LABEL_BATCH = 8


def _motion_stats(motion: str | None) -> ShotStats:
    """Turn a recipe's motion words ('push-in, pan right, handheld') into target stats."""
    s = ShotStats()
    m = (motion or "").lower()
    if "push-in" in m:
        s.zoom = 0.1
    if "pull-out" in m:
        s.zoom = -0.1
    if "pan right" in m:
        s.pan_x = 0.08
    if "pan left" in m:
        s.pan_x = -0.08
    if "tilt down" in m:
        s.pan_y = 0.06
    if "tilt up" in m:
        s.pan_y = -0.06
    s.shake = 0.009 if "handheld" in m else 0.002
    return s


def label_clips(clips: list[Clip]) -> list[dict]:
    labels: dict[int, dict] = {}
    for b in range(0, len(clips), LABEL_BATCH):
        batch = clips[b:b + LABEL_BATCH]
        parts = ["Raw clips from a videographer's shoot. Each clip shows 3 frames (start, middle, end)."]
        for i, cl in enumerate(batch, b + 1):
            with FrameReader(cl.path) as r:
                frames = [r.small_at(t) for t in np.linspace(0.1, 0.9, 3) * r.duration]
            parts += [f"Clip {i}: ({cl.info.duration:.1f}s)", *[f for f in frames if f is not None]]
        parts.append("Label every clip: subject, action, camera angle, shot type, usable quality 1-10, "
                     "and a few tags (e.g. walk, close-up, dance, car, venue, detail, couple).")
        try:
            out = vision.ask(vision.content(parts), vision.ClipLabels)
            for lab in out.clips:
                labels[lab.clip_number] = lab.model_dump()
        except Exception:
            log.exception("Clip labelling failed for batch starting %d", b + 1)
    result = []
    for i, cl in enumerate(clips, 1):
        lab = labels.get(i) or {"clip_number": i, "subject": "unknown", "action": "unknown", "angle": "other",
                                "shot_type": "medium", "quality": 5, "tags": []}
        lab.update(clip_id=cl.id, file_name=cl.name, duration=round(cl.info.duration, 2))
        result.append(lab)
    return result


def footage_summary(labels: list[dict], mood: str | None) -> str:
    tags = sorted({t for lab in labels for t in lab.get("tags", [])})
    shots = ", ".join(f"{lab['shot_type']} {lab['angle']} {lab['action']}" for lab in labels[:20])
    return f"{mood or ''} reel. Footage: {shots}. Tags: {' '.join(tags)}."


def rank_recipes(recipes: list[dict], summary: str, mood: str | None, k: int = 3) -> list[dict]:
    if not recipes:
        return []
    q = embed_texts([summary])[0]
    scored = []
    for r in recipes:
        if not r.get("embedding"):
            continue
        s = float(normalize(np.array(r["embedding"], np.float32)) @ q)
        if mood and mood.lower() in [m.lower() for m in r.get("moods") or []]:
            s += 0.15
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    picked, seen = [], set()
    for s, r in scored:
        key = r.get("style_name")
        if key in seen:
            continue
        seen.add(key)
        picked.append({**r, "match_score": round(s, 4)})
        if len(picked) == k:
            break
    return picked


def _slots(recipe: dict) -> list[dict]:
    lengths = recipe["pacing"]["lengths"]
    transitions = recipe.get("transitions") or ["cut"] * len(lengths)
    pattern = recipe.get("setup_pattern") or [1] * len(lengths)
    types = {t["setup_number"]: t for t in recipe.get("setup_types") or []}
    total = recipe.get("duration") or sum(lengths)
    slow = set(recipe.get("slowmo_beats") or [])
    slots, t = [], 0.0
    for i, L in enumerate(lengths):
        st = types.get(pattern[i] if i < len(pattern) else 1, {})
        slots.append({"duration": L, "transition_in": transitions[i] if i < len(transitions) else "cut",
                      "speed": 0.5 if round(t / max(total, 1e-3), 3) in slow else 1.0,
                      "setup_number": pattern[i] if i < len(pattern) else 1,
                      "angle": st.get("angle", ""), "subject": st.get("subject", ""), "motion": st.get("motion", "")})
        t += L
    return slots


def _fallback_plan(slots: list[dict], labels: list[dict]) -> list[dict]:
    """Round-robin: each recipe setup gets its own clip, best quality first."""
    order = sorted(labels, key=lambda lab: -lab.get("quality", 5))
    setup_clip: dict[int, int] = {}
    out = []
    for i, s in enumerate(slots):
        sn = s["setup_number"]
        if sn not in setup_clip:
            setup_clip[sn] = order[len(setup_clip) % len(order)]["clip_number"]
        out.append({"order_index": i, "clip_number": setup_clip[sn], "duration": s["duration"],
                    "speed": s["speed"], "transition_in": s["transition_in"], "note": ""})
    return out


def _valid(plan: vision.CutPlan, n_clips: int) -> list[dict] | None:
    cuts = [c.model_dump() for c in sorted(plan.cuts, key=lambda c: c.order_index)]
    if not cuts or any(not (1 <= c["clip_number"] <= n_clips) or c["duration"] <= 0.05 for c in cuts):
        return None
    for i, c in enumerate(cuts):
        c["order_index"] = i
        c["speed"] = min(max(c["speed"], 0.25), 2.0)
    return cuts


def build_plan(recipe: dict, labels: list[dict], instruction: str | None = None,
               current: list[dict] | None = None) -> list[dict]:
    slots = _slots(recipe) if recipe else []
    parts = []
    if current:
        parts.append("Current cut plan (JSON): " + str([{k: c[k] for k in ("order_index", "clip_number", "duration",
                                                                         "speed", "transition_in")} for c in current]))
        parts.append(f"The user asks: \"{instruction}\". Rewrite the cut plan to do that, keeping "
                     "everything else about the style. You may add, remove or reorder cuts.")
    else:
        parts.append(f"Adapt this editing style to the user's footage: {recipe.get('style_name')}: "
                     f"{recipe.get('summary')}. Fill each slot with the best clip. Keep the slot durations, "
                     "transitions and speeds (that is the style). Use the same clip for slots that share a "
                     "setup number when it makes sense, and match the slot's angle and subject.")
        for i, s in enumerate(slots, 1):
            parts.append(f"Slot {i}: {s['duration']:.2f}s, transition {s['transition_in']}, speed {s['speed']}, "
                         f"setup {s['setup_number']} ({s['angle']} {s['subject']}, {s['motion']})")
    parts.append("Available clips:")
    for lab in labels:
        parts.append(f"Clip {lab['clip_number']}: {lab['duration']}s, {lab['shot_type']} {lab['angle']} shot of "
                     f"{lab['subject']} {lab['action']}, quality {lab['quality']}, tags {', '.join(lab['tags'])}")
    parts.append("Return the cut plan with order_index from 0, clip_number from the list above, duration "
                 "in seconds on the timeline, speed (0.5 = slow motion) and transition_in.")
    try:
        plan = vision.ask(vision.content(parts), vision.CutPlan)
        cuts = _valid(plan, len(labels))
        if cuts:
            return cuts
        log.warning("AI cut plan was invalid; using fallback")
    except Exception:
        log.exception("AI cut plan failed; using fallback")
    if current:
        return current
    return _fallback_plan(slots, labels)


def plan_to_render(plan: list[dict], clips: list[Clip], recipe: dict | None, out_w: int, out_h: int) -> list[RenderCut]:
    motions = {t["setup_number"]: t.get("motion") for t in (recipe or {}).get("setup_types") or []}
    pattern = (recipe or {}).get("setup_pattern") or []
    cursor: dict[int, float] = {}
    out: list[RenderCut] = []
    readers: dict[str, FrameReader] = {}
    try:
        for c in plan:
            cl = clips[c["clip_number"] - 1]
            speed = float(c.get("speed") or 1)
            need = c["duration"] * speed
            dur = cl.info.duration
            start = cursor.get(c["clip_number"], min(0.3, max(0.0, dur - need)))
            if start + need > dur:
                start = max(0.0, (dur - need) * ((len(out) * 0.37) % 1))  # reuse a different part
            if c.get("src_in") is not None and 0 <= c["src_in"] <= max(0.0, dur - need):
                start = float(c["src_in"])
            cursor[c["clip_number"]] = start + need + 0.2
            h, w = cl.frame_size
            rc = RenderCut(order_index=c["order_index"], src_path=cl.path, src_name=cl.name, src_w=w, src_h=h,
                           src_fps=cl.info.fps, src_in=start, speed=speed, out_len=c["duration"],
                           transition_in=c.get("transition_in") or "cut", cut_id=cl.id)
            sn = pattern[c["order_index"]] if c["order_index"] < len(pattern) else None
            r = readers.setdefault(cl.path, FrameReader(cl.path))
            user = analyze_segment(r, start, start + need)
            target = _motion_stats(motions.get(sn)) if sn else ShotStats(zoom=user.zoom, pan_x=user.pan_x,
                                                                          pan_y=user.pan_y, shake=user.shake)
            target.subject_start = target.subject_end = None
            target.lab_mean, target.lab_std = [], []  # no colour target for raw footage
            rc = plan_cut(rc, target, user, out_w, out_h, match_score=0.9)
            c["src_in"] = round(start, 3)
            out.append(rc)
    finally:
        for r in readers.values():
            r.close()
    return out


def _dims(payload: dict) -> tuple[int, int, float]:
    return (int(payload.get("width") or settings.out_width), int(payload.get("height") or settings.out_height),
            float(payload.get("fps") or 30))


def suggest(payload: dict, tmp: Path) -> dict:
    out_w, out_h, fps = _dims(payload)
    clips = load_clips(payload["clips"], tmp)
    labels = label_clips(clips)
    summary = footage_summary(labels, payload.get("mood"))
    picks = rank_recipes(payload.get("recipes") or [], summary, payload.get("mood"))
    if not picks:
        # Empty library: one generic recipe built from the footage itself.
        picks = [{"id": None, "style_name": "Quick cut", "summary": "Even 1.2s cuts, alternating clips.",
                  "pacing": {"lengths": [1.2] * min(12, max(4, len(clips) * 2))},
                  "setup_pattern": [i % max(1, len(clips)) + 1 for i in range(min(12, max(4, len(clips) * 2)))],
                  "transitions": ["cut"] * 12, "moods": [], "match_score": 0}]
    outputs = payload.get("outputs") or []
    suggestions = []
    for k, recipe in enumerate(picks):
        plan = build_plan(recipe, labels)
        rcs = plan_to_render(plan, clips, recipe, out_w, out_h)
        sub = tmp / f"s{k}"
        sub.mkdir(parents=True, exist_ok=True)
        res = finish(rcs, {"outputs": {"preview_upload_url": outputs[k]["preview_upload_url"]} if k < len(outputs) else {},
                           "preview_scale": payload.get("preview_scale", 0.5)},
                     sub, out_w, out_h, fps, [], recipe.get("style_name") or "ReelCraft edit")
        suggestions.append({"recipe_id": recipe.get("id"), "style_name": recipe.get("style_name"),
                            "moods": recipe.get("moods"), "match_score": recipe.get("match_score"),
                            "plan": plan, "preview": res.get("preview"), "index": k})
    return {"labels": labels, "footage_summary": summary, "suggestions": suggestions}


def _labels_or_make(payload: dict, clips: list[Clip]) -> list[dict]:
    labels = payload.get("labels")
    if labels and len(labels) == len(clips):
        return labels
    return label_clips(clips)


def revise(payload: dict, tmp: Path) -> dict:
    out_w, out_h, fps = _dims(payload)
    clips = load_clips(payload["clips"], tmp)
    labels = _labels_or_make(payload, clips)
    recipe = payload.get("recipe") or {}
    plan = build_plan(recipe, labels, payload["instruction"], payload["plan"])
    rcs = plan_to_render(plan, clips, recipe, out_w, out_h)
    res = finish(rcs, {"outputs": payload.get("outputs") or {}, "preview_scale": payload.get("preview_scale", 0.5)},
                 tmp, out_w, out_h, fps, [], recipe.get("style_name") or "ReelCraft edit")
    return {"plan": plan, "preview": res.get("preview"), "labels": labels}


def export(payload: dict, tmp: Path) -> dict:
    """Full-resolution render plus Resolve timelines for the chosen plan."""
    out_w, out_h, fps = _dims(payload)
    clips = load_clips(payload["clips"], tmp)
    recipe = payload.get("recipe") or {}
    rcs = plan_to_render(payload["plan"], clips, recipe, out_w, out_h)
    res = finish(rcs, {"outputs": payload.get("outputs") or {}, "preview_scale": 1.0,
                       "music_url": payload.get("music_url")},
                 tmp, out_w, out_h, fps, [], payload.get("title") or recipe.get("style_name") or "ReelCraft edit")
    return {k: v for k, v in res.items() if k != "preview_local"}

