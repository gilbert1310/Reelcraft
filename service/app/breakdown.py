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
        except vision.VisionUnavailable:
            raise
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
    except vision.VisionUnavailable:
        raise
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
    except vision.VisionUnavailable:
        raise
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


# ---------- 3. Plan the takes (whole reel at once) ----------

MAX_PLAN_IMAGES = 90
STORYBOARD_STEP = 0.5   # seconds between storyboard frames
STORYBOARD_PX = 384


def storyboard(reader: FrameReader, cuts: list[Cut], duration: float) -> list[tuple[float, int, np.ndarray]]:
    """Frames across the whole reel, every STORYBOARD_STEP seconds, labelled with their cut, so the AI
    can follow the action as a story rather than only seeing cut edges. Every cut gets at least one."""
    n = int(min(MAX_PLAN_IMAGES - len(cuts), max(12, round(duration / STORYBOARD_STEP))))
    times = list(np.linspace(0.05, max(0.05, duration - 0.05), max(n, 1)))
    def cut_of(t: float) -> int:
        return next((c.index for c in cuts if c.start <= t < c.end), cuts[-1].index)
    covered = {cut_of(t) for t in times}
    times += [(c.start + c.end) / 2 for c in cuts if c.index not in covered]
    board = []
    for t in sorted(times):
        f = reader.small_at(t, STORYBOARD_PX)
        if f is not None:
            board.append((float(t), cut_of(t), f))
    return board


def _partition_problem(groups: list[list[int]], n: int) -> str | None:
    flat = [i for g in groups for i in g]
    missing = sorted(set(range(n)) - set(flat))
    dupes = sorted({i for i in flat if flat.count(i) > 1})
    extra = sorted(set(flat) - set(range(n)))
    if not (missing or dupes or extra) and all(groups):
        return None
    bits = []
    if missing:
        bits.append("missing cuts " + ", ".join(str(i + 1) for i in missing))
    if dupes:
        bits.append("cuts in more than one take: " + ", ".join(str(i + 1) for i in dupes))
    if extra:
        bits.append("cut numbers that don't exist: " + ", ".join(str(i + 1) for i in extra))
    return "; ".join(bits) or "an empty take"


def _plan_groups(plan: vision.ShotPlan) -> list[list[int]]:
    """0-based cut groups for the coverage check: one per take, plus the graphics."""
    groups = [[k - 1 for k in s.cut_numbers] for s in plan.setups]
    return groups + ([[k - 1 for k in plan.graphics]] if plan.graphics else [])


def plan_setups(cuts: list[Cut], hint: list[list[int]], duration: float,
                board: list[tuple[float, int, np.ndarray]], feedback: str | None = None) -> vision.ShotPlan | None:
    """Show the vision AI the whole reel as a storyboard and ask which takes were filmed.
    Returns None if the answer never covers every cut exactly once."""
    n = len(cuts)
    parts: list = [vision.SHOT_PLANNER, f"The reel is {duration:.1f}s long with {n} cuts.", "Storyboard:"]
    for t, ci, frame in board:
        parts += [f"t={t:.1f}s, cut {ci + 1}", frame]
    parts.append("Cuts:")
    for c in cuts:
        parts.append(f"Cut {c.index + 1}: {c.start:.2f}s to {c.end:.2f}s ({c.length:.1f}s), camera: "
                     f"{describe_motion(c.stats or ShotStats())}, detected transition in: {c.transition_in}")
    parts.append("Fingerprint hint: " + " ".join("[" + ",".join(str(i + 1) for i in g) + "]" for g in hint))
    if feedback and feedback.strip():
        parts.append("The videographer reviewed an earlier plan for this reel and says: \"" + feedback.strip()[:1000] +
                     "\". Take this seriously and correct the plan where the storyboard supports it; every cut must "
                     "still be covered.")
    draft = None
    for attempt in range(2):
        plan = vision.ask(vision.content(parts), vision.ShotPlan, max_tokens=16000, effort="high")
        problem = _partition_problem(_plan_groups(plan), n)
        if problem is None:
            draft = plan
            break
        log.warning("Shot plan attempt %d didn't cover every cut once: %s", attempt + 1, problem)
        parts.append(f"Your previous answer had {problem}. Answer again: every cut from 1 to {n} must "
                     "appear in exactly one take.")
    if draft is None:
        return None
    log.info("Draft shot plan: %s", draft.summary)

    # Second pass: audit the draft cut by cut and add anything it missed.
    audit_parts = parts + ["Draft plan to audit:\n" + draft.model_dump_json(indent=1), vision.PLAN_AUDIT]
    try:
        audited = vision.ask(vision.content(audit_parts), vision.ShotPlan, max_tokens=16000, effort="high")
    except vision.VisionUnavailable:
        raise
    except Exception:
        log.exception("Plan audit failed; using the draft")
        return draft
    problem = _partition_problem(_plan_groups(audited), n)
    if problem is not None:
        log.warning("Audited plan didn't cover every cut once (%s); using the draft", problem)
        return draft
    log.info("Audited shot plan: %s | %s", audited.summary, audited.shoot_notes[:400])
    return audited


def _setup_row(cuts: list[Cut], members: list[int], n: int, description: str, angle: str, subject: str,
               film_seconds: float, tip: str, is_graphic: bool = False) -> dict:
    used = sum(cuts[i].length / cuts[i].speed for i in members)
    emb = normalize(np.mean([cuts[i].embedding for i in members], axis=0))
    return {
        "setup_number": n,
        "description": description,
        "angle": angle,
        "subject": subject,
        # Always leave the editor at least 2x what the edit uses, and never under 5 seconds.
        "film_duration_seconds": 0 if is_graphic else round(max(film_seconds, used * 2, 5.0), 1),
        "is_graphic": is_graphic,
        "beginner_tip": tip,
        "motion": describe_motion(cuts[members[0]].stats),
        "embedding": _vec(emb),
    }


def _apply_cut_details(cuts: list[Cut], details: list[vision.CutDetail]) -> None:
    for cd in details:
        i = cd.cut_number - 1
        if 0 <= i < len(cuts):
            cuts[i].action = cd.action
            if i > 0 and cd.transition_in in ("match", "dissolve", "whip", "zoom", "cut"):
                cuts[i].transition_in = cd.transition_in
            cuts[i].speed = 0.5 if cd.slow_motion else 1.0


def _setups_from_plan(cuts: list[Cut], plan: vision.ShotPlan) -> list[dict]:
    _apply_cut_details(cuts, plan.cuts)
    ordered = sorted(plan.setups, key=lambda s: min(s.cut_numbers))
    setups = []
    for n, ps in enumerate(ordered, 1):
        members = sorted(k - 1 for k in ps.cut_numbers)
        for i in members:
            cuts[i].group = n - 1
        setups.append(_setup_row(cuts, members, n, ps.description, ps.angle, ps.subject,
                                 ps.film_duration_seconds, ps.beginner_tip))
    if plan.graphics:
        members = sorted(k - 1 for k in plan.graphics)
        n = len(setups) + 1
        for i in members:
            cuts[i].group = n - 1
        setups.append(_setup_row(cuts, members, n, "Added in the edit (logo, end card or title). Nothing to film.",
                                 "other", "graphic", 0, "", is_graphic=True))
    return setups


def _setups_by_review(cuts: list[Cut], groups: list[list[int]]) -> list[dict]:
    setups = []
    for n, members in enumerate(groups, 1):
        for i in members:
            cuts[i].group = n - 1
        d = describe_setup(cuts, members)
        _apply_cut_details(cuts, d.cuts)
        setups.append(_setup_row(cuts, members, n, d.description, d.angle, d.subject,
                                 d.film_duration_seconds, d.beginner_tip))
    return setups


# ---------- Pipeline ----------

def analyze(path: str, feedback: str | None = None) -> dict:
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
        board = storyboard(reader, cuts, info.duration)

    hint = cluster(cuts, settings.group_threshold)
    plan = plan_setups(cuts, hint, info.duration, board, feedback)
    if plan is not None:
        setups = _setups_from_plan(cuts, plan)
        summary = plan.summary
    else:
        log.warning("Falling back to per-group review")
        setups = _setups_by_review(cuts, verify_groups(cuts, hint))
        summary = f"Filmed as {len(setups)} takes."
    cuts[0].transition_in = "cut"

    recipe = build_recipe(cuts, setups, info.duration, reel_frames)
    return {
        "video": {"duration": info.duration, "fps": info.fps, "width": info.display_size[0],
                  "height": info.display_size[1]},
        "embedder": embedder_name(),
        "plan_summary": summary,
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
