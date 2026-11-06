"""Find, inside a user's clip, the moment that best matches each reference cut."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .embed import embed_images, normalize
from .media import FrameReader


@dataclass
class ClipIndex:
    """Embeddings of a clip sampled on a regular grid."""
    times: np.ndarray     # sample timestamps (s)
    vecs: np.ndarray      # (n, d) unit vectors
    duration: float
    step: float


def index_clip(reader: FrameReader, rate: float = 2.0, max_samples: int = 240) -> ClipIndex:
    dur = reader.duration
    n = int(min(max_samples, max(2, dur * rate)))
    times = np.linspace(0, max(dur - 1.0 / reader.fps, 0), n)
    frames, kept = [], []
    for t in times:
        f = reader.small_at(t)
        if f is not None:
            frames.append(f)
            kept.append(t)
    vecs = embed_images(frames) if frames else np.zeros((0, 512), np.float32)
    kept = np.array(kept)
    return ClipIndex(kept, vecs, dur, float(kept[1] - kept[0]) if len(kept) > 1 else dur)


def window_scores(idx: ClipIndex, ref: np.ndarray, need: float) -> tuple[np.ndarray, np.ndarray]:
    """For each candidate start time, the mean similarity of the window [t, t+need]."""
    if len(idx.times) == 0:
        return np.zeros(0), np.zeros(0)
    sims = idx.vecs @ normalize(ref)
    starts = idx.times[idx.times + need <= idx.duration + 1e-6]
    if len(starts) == 0:
        starts = idx.times[:1]
    scores = np.array([sims[(idx.times >= s - 1e-6) & (idx.times <= s + need + 1e-6)].mean()
                       if np.any((idx.times >= s - 1e-6) & (idx.times <= s + need + 1e-6)) else sims[0]
                       for s in starts])
    return starts, scores


@dataclass
class Match:
    start: float
    score: float


def match_sequence(idx: ClipIndex, refs: list[np.ndarray], needs: list[float],
                   back_penalty: float = 0.08) -> list[Match]:
    """Pick one window per reference cut (in edit order) that maximises total similarity,
    preferring windows that move forward through the clip like the original take did.

    Dynamic programming over candidate start times; going backwards is allowed but
    penalised, so a short clip can still be reused."""
    if not refs:
        return []
    cand = [window_scores(idx, r, n) for r, n in zip(refs, needs)]
    best = [cand[0][1].copy()]
    back: list[np.ndarray] = []
    for j in range(1, len(refs)):
        prev_starts, cur_starts = cand[j - 1][0], cand[j][0]
        prev_end = prev_starts + needs[j - 1]
        # penalty[k_prev, k_cur]
        pen = np.where(cur_starts[None, :] + 1e-6 >= prev_end[:, None] - idx.step, 0.0, back_penalty)
        total = best[-1][:, None] - pen
        arg = total.argmax(axis=0)
        best.append(total[arg, np.arange(len(cur_starts))] + cand[j][1])
        back.append(arg)
    k = int(best[-1].argmax())
    picks = [k]
    for arg in reversed(back):
        k = int(arg[k])
        picks.append(k)
    picks.reverse()
    return [Match(float(cand[j][0][k]), float(cand[j][1][k])) for j, k in enumerate(picks)]


def best_window(idx: ClipIndex, ref: np.ndarray, need: float) -> Match:
    starts, scores = window_scores(idx, ref, need)
    if len(scores) == 0:
        return Match(0.0, 0.0)
    k = int(scores.argmax())
    return Match(float(starts[k]), float(scores[k]))
