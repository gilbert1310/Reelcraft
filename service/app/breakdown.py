"""Stage 1: break a reel down into camera setups (not cuts), remember the edit
pattern, and distil a reusable style recipe."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import vision
from .config import settings
from .embed import embed_images, embed_texts, embedder_name, normalize
from .media import FrameReader, VideoInfo, probe, sample_times, to_jpeg
from .motion import ShotStats, analyze_segment, describe_motion

log = logging.getLogger(__name__)

MAX_GROUP_CUTS_PER_CALL = 16


@dataclass
class Cut:
    index: int
    start: float
    end: float
    frames: list[np.ndarray] = field(default_factory=list)
    embedding: np.ndarray | None = None
    stats: ShotStats | None = None
    group: int = -1
    transition_in: str = "cut"
    speed: float = 1.0
    action: str = ""

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def mid(self) -> np.ndarray:
        return self.frames[len(self.frames) // 2]


# ---------- 1. Split ----------

def _secs(tc) -> float:
    return float(tc.seconds) if hasattr(type(tc), "seconds") else float(tc.get_seconds())


def detect_cuts(path: str, info: VideoInfo) -> list[tuple[float, float]]:
    from scenedetect import ContentDetector, detect

    scenes = detect(path, ContentDetector(threshold=settings.scene_threshold,
                                          min_scene_len=max(2, int(info.fps * settings.min_cut_seconds))))
    spans = [(_secs(s), _secs(e)) for s, e in scenes]
    if not spans:
        spans = [(0.0, info.duration)]
    # Merge slivers (flash frames, dissolve midpoints) into the previous cut.
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and e - s < settings.min_cut_seconds:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


# ---------- 2. Fingerprint ----------

def fingerprint(reader: FrameReader, cuts: list[Cut]) -> None:
    all_frames, owners = [], []
    for c in cuts:
        times = sample_times(c.start, c.end, 3)
        c.frames = [f for f in (reader.small_at(t) for t in times) if f is not None]
        if not c.frames:
            raise RuntimeError(f"Could not read frames for cut {c.index + 1}")
        all_frames += c.frames
        owners += [c.index] * len(c.frames)
    vecs = embed_images(all_frames)
    owners = np.array(owners)
    for c in cuts:
        c.embedding = normalize(vecs[owners == c.index].mean(axis=0))


# ---------- 3. Group ----------

def cluster(cuts: list[Cut], threshold: float) -> list[list[int]]:
    if len(cuts) == 1:
        return [[0]]
    from sklearn.cluster import AgglomerativeClustering

    X = np.stack([c.embedding for c in cuts])
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=1 - threshold).fit_predict(X)
    groups: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        groups.setdefault(int(lab), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def _valid_partition(groups: list[list[int]], expected: set[int]) -> bool:
    flat = [n for g in groups for n in g]
    return len(flat) == len(expected) and set(flat) == expected and all(groups)


def verify_groups(cuts: list[Cut], groups: list[list[int]]) -> list[list[int]]:
    """Ask the vision AI to split candidate groups that mix angles, then merge
    candidates that are really the same setup."""
    out: list[list[int]] = []
    for g in groups:
        if len(g) < 2 or len(g) > MAX_GROUP_CUTS_PER_CALL:
            out.append(g)
            continue
        parts = ["These are cuts from one reel that look similar. For each cut you see its first "
                 "and last frame."]
        for i in g:
            parts += [f"Cut {i + 1}:", cuts[i].frames[0], cuts[i].frames[-1]]
        parts.append("Are these cuts from the same camera angle and the same continuous action? "
                     "Split them into groups that each share one camera setup. Use every cut number "
                     "exactly once. If they all belong together, return a single group.")
        try:
            verdict = vision.ask(vision.content(parts), vision.GroupVerdict)
            proposed = [[n - 1 for n in grp] for grp in verdict.groups]
            out += proposed if _valid_partition(proposed, set(g)) else [g]
        except Exception:
            log.exception("Group verification failed; keeping embedding grouping")
            out.append(g)

    if len(out) < 2 or len(out) > 12:
        return out
    parts = ["Each image is a representative frame of a candidate camera setup from one reel."]
    for n, g in enumerate(out, 1):
        parts += [f"Setup {n}:", cuts[g[len(g) // 2]].mid]
    parts.append("Group together setups that are the same camera angle on the same continuous action "
                 "(one take the editor cut into pieces). Keep different angles separate. Use every "
                 "setup number exactly once.")
    try:
        verdict = vision.ask(vision.content(parts), vision.MergeVerdict)
        if _valid_partition(verdict.groups, set(range(1, len(out) + 1))):
            out = [sorted(i for n in grp for i in out[n - 1]) for grp in verdict.groups]
    except Exception:
        log.exception("Setup merge check failed; keeping groups")
    return sorted(out, key=lambda g: g[0])


# ---------- Transitions (heuristic, refined by the vision AI) ----------

def _sharp(img: np.ndarray) -> float:
    g = cv2.cvtColor(cv2.resize(img, (320, int(320 * img.shape[0] / img.shape[1]))), cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def detect_transition(reader: FrameReader, boundary: float, cut: Cut) -> str:
    dt = 1.0 / reader.fps
    prev, mid, nxt = (reader.small_at(boundary - 6 * dt), reader.small_at(boundary),
                      reader.small_at(boundary + 6 * dt))
    if prev is None or mid is None or nxt is None:
        return "cut"
    p, m, n = (cv2.resize(x, (96, 96)).astype(np.float32) for x in (prev, mid, nxt))
    blend_err = np.abs(m - (p + n) / 2).mean()
    if blend_err < 0.6 * min(np.abs(m - p).mean(), np.abs(m - n).mean()) and np.abs(p - n).mean() > 12:
        return "dissolve"
    around = [reader.small_at(boundary - dt), reader.small_at(boundary + dt)]
    if any(f is None for f in around):
        return "cut"
    edge = min(_sharp(f) for f in around)
    typical = np.median([_sharp(f) for f in cut.frames])
    if typical > 20 and edge < 0.3 * typical:
        return "whip"
    if cut.stats and cut.stats.zoom > 0.6:
        return "zoom"
    return "cut"


# ---------- 4. Describe ----------

def describe_setup(cuts: list[Cut], members: list[int]) -> vision.SetupBreakdown:
    used = sum(cuts[i].length for i in members)
    motion = describe_motion(cuts[members[0]].stats or ShotStats())
    parts = [vision.CINEMATOGRAPHER.format(used=used),
             f"Measured camera motion: {motion}. The cuts below are shown in edit order."]
    shown = members if len(members) <= 10 else list(np.linspace(0, len(members) - 1, 10).astype(int))
    for k in shown:
        i = members[k] if len(members) > 10 else k
        c = cuts[i]
        parts += [f"Cut {c.index + 1}: ({c.length:.1f}s, detected transition in: {c.transition_in})",
                  c.frames[0], c.frames[-1]]
    parts.append("For each cut shown, also give a few-word action, the transition into it "
                 "(keep the detected one unless the frames clearly show otherwise; use 'match' for a "
                 "match cut), and whether it is in slow motion.")
    return vision.ask(vision.content(parts), vision.SetupBreakdown)


# ---------- 5/6. Pattern + recipe ----------

def build_recipe(cuts: list[Cut], setups: list[dict], duration: float, reel_frames: list[np.ndarray]) -> dict:
    lengths = [round(c.length, 3) for c in cuts]
    total = max(duration, 1e-3)
    recipe = {
        "duration": round(duration, 3),
        "cut_count": len(cuts),
        "setup_count": len(setups),
        "pacing": {
            "cuts_per_second": round(len(cuts) / total, 3),
            "mean_cut": round(float(np.mean(lengths)), 3),
            "min_cut": round(float(np.min(lengths)), 3),
            "max_cut": round(float(np.max(lengths)), 3),
            "lengths": lengths,
        },
        "transitions": [c.transition_in for c in cuts],
        "slowmo_beats": [round(c.start / total, 3) for c in cuts if c.speed < 1],
        "setup_pattern": [c.group + 1 for c in cuts],
        "setup_types": [{"setup_number": s["setup_number"], "angle": s["angle"], "subject": s["subject"],
                         "motion": s["motion"]} for s in setups],
    }
    parts = ["Frames from across one short-form reel, in order:", *reel_frames,
             f"Edit stats: {len(cuts)} cuts from {len(setups)} setups over {duration:.1f}s; "
             f"transitions {sorted(set(recipe['transitions']))}; "
             f"{len(recipe['slowmo_beats'])} slow-motion beats. Setups: " +
             "; ".join(f"{s['angle']} {s['subject']} ({s['motion']})" for s in setups) +
             ". Tag this editing style: genre (e.g. wedding, travel, fitness, fashion, food, real "
             "estate, event), moods (e.g. romantic, energetic, cinematic, moody, playful), a short "
             "catchy style name, and a one-sentence summary of the editing technique (pacing, "
             "transitions, camera moves), not the specific people or place."]
    try:
        tags = vision.ask(vision.content(parts), vision.RecipeTags)
        recipe.update(genre=tags.genre, moods=[m.lower() for m in tags.moods], style_name=tags.style_name,
                      summary=tags.summary)
    except Exception:
        log.exception("Recipe tagging failed")
        recipe.update(genre="general", moods=[], style_name="Untitled style",
                      summary=f"{len(cuts)} cuts from {len(setups)} setups")
    text = (f"{recipe['genre']} {' '.join(recipe['moods'])} reel. {recipe['summary']} "
            f"{recipe['pacing']['cuts_per_second']:.1f} cuts per second, transitions "
            f"{' '.join(sorted(set(recipe['transitions'])))}.")
    recipe["embedding_text"] = text
    recipe["embedding"] = _vec(embed_texts([text])[0])
    return recipe


def _vec(v: np.ndarray) -> list[float]:
    return [round(float(x), 5) for x in v]


def _thumb(img: np.ndarray) -> str:
    return base64.b64encode(to_jpeg(img, 360, 78)).decode()


# ---------- Pipeline ----------

def analyze(path: str) -> dict:
    info = probe(path)
    if info.duration <= 0:
        raise RuntimeError("Video has no duration")
    spans = detect_cuts(path, info)
    log.info("Detected %d cuts", len(spans))
    cuts = [Cut(i, s, e) for i, (s, e) in enumerate(spans)]

    with FrameReader(path) as reader:
        fingerprint(reader, cuts)
        for c in cuts:
            c.stats = analyze_segment(reader, c.start, c.end)
        for c in cuts[1:]:
            c.transition_in = detect_transition(reader, c.start, c)
        reel_frames = [reader.small_at(t) for t in np.linspace(0.05, 0.95, 6) * info.duration]
        reel_frames = [f for f in reel_frames if f is not None]

    groups = verify_groups(cuts, cluster(cuts, settings.group_threshold))
    for n, g in enumerate(groups):
        for i in g:
            cuts[i].group = n

    setups = []
    for n, members in enumerate(groups):
        try:
            d = describe_setup(cuts, members)
        except vision.VisionRefused:
            raise
        except Exception:
            log.exception("Describe failed for setup %d", n + 1)
            d = vision.SetupBreakdown(description="Recreate this shot as shown in the reference frames.",
                                      angle="other", subject="subject",
                                      film_duration_seconds=0, beginner_tip="", cuts=[])
        by_num = {cd.cut_number - 1: cd for cd in d.cuts}
        for i in members:
            if i in by_num:
                cd = by_num[i]
                cuts[i].action = cd.action
                if i > 0 and cd.transition_in in ("match", "dissolve", "whip", "zoom"):
                    cuts[i].transition_in = cd.transition_in
                cuts[i].speed = 0.5 if cd.slow_motion else 1.0
        used = sum(cuts[i].length / cuts[i].speed for i in members)
        emb = normalize(np.mean([cuts[i].embedding for i in members], axis=0))
        setups.append({
            "setup_number": n + 1,
            "description": d.description,
            "angle": d.angle,
            "subject": d.subject,
            # Always leave the editor at least 2x what the edit uses, and never under 5 seconds.
            "film_duration_seconds": round(max(d.film_duration_seconds, used * 2, 5.0), 1),
            "beginner_tip": d.beginner_tip,
            "motion": describe_motion(cuts[members[0]].stats),
            "embedding": _vec(emb),
        })
    cuts[0].transition_in = "cut"

    recipe = build_recipe(cuts, setups, info.duration, reel_frames)
    return {
        "video": {"duration": info.duration, "fps": info.fps, "width": info.display_size[0],
                  "height": info.display_size[1]},
        "embedder": embedder_name(),
        "setups": setups,
        "cuts": [{
            "order_index": c.index,
            "setup_number": c.group + 1,
            "start_time": round(c.start, 3),
            "end_time": round(c.end, 3),
            "transition_in": c.transition_in,
            "speed": c.speed,
            "action": c.action,
            "embedding": _vec(c.embedding),
            "stats": c.stats.to_dict() if c.stats else None,
            "thumbnail_b64": _thumb(c.mid),
        } for c in cuts],
        "recipe": recipe,
    }
