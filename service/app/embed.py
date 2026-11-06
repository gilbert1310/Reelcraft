"""Image/text embeddings ("fingerprints"). Both backends return unit-length 512-d vectors,
so the database column vector(512) works with either."""
from __future__ import annotations

import hashlib
import logging
import re
import threading

import cv2
import numpy as np

from .config import settings

log = logging.getLogger(__name__)
DIM = 512


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-8)


def cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return normalize(a) @ normalize(b).T


class HistogramEmbedder:
    """Dependency-free fallback: colour histogram + coarse layout. Good enough to tell
    camera setups apart in tests; use CLIP in production."""

    def images(self, frames: list[np.ndarray]) -> np.ndarray:
        out = []
        for f in frames:
            small = cv2.resize(f, (64, 64), interpolation=cv2.INTER_AREA)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 4], [0, 180, 0, 256, 0, 256]).flatten()
            hist = np.sqrt(hist / max(hist.sum(), 1))
            gray = cv2.resize(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (16, 16)).astype(np.float32).flatten()
            gray = (gray - gray.mean()) / (gray.std() + 1e-6) * 0.06
            out.append(np.concatenate([hist, gray]))
        return normalize(np.array(out))

    def texts(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                out[i, h % DIM] += 1.0 if (h >> 9) & 1 else -1.0
        return normalize(out)


class ClipEmbedder:
    def __init__(self):
        import open_clip
        import torch
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            settings.clip_model, pretrained=settings.clip_pretrained)
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(settings.clip_model)
        torch.set_num_threads(max(1, torch.get_num_threads()))

    def images(self, frames: list[np.ndarray]) -> np.ndarray:
        from PIL import Image
        if not frames:
            return np.zeros((0, DIM), np.float32)
        vecs = []
        with self.torch.no_grad():
            for i in range(0, len(frames), 32):
                batch = self.torch.stack([
                    self.preprocess(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)))
                    for f in frames[i:i + 32]])
                vecs.append(self.model.encode_image(batch).float().numpy())
        return normalize(np.concatenate(vecs))

    def texts(self, texts: list[str]) -> np.ndarray:
        with self.torch.no_grad():
            v = self.model.encode_text(self.tokenizer(texts)).float().numpy()
        return normalize(v)


_embedder = None
_lock = threading.Lock()


def get_embedder():
    global _embedder
    with _lock:
        if _embedder is None:
            if settings.embedder == "clip":
                try:
                    _embedder = ClipEmbedder()
                except Exception:
                    log.exception("CLIP unavailable; falling back to histogram embedder")
                    _embedder = HistogramEmbedder()
            else:
                _embedder = HistogramEmbedder()
        return _embedder


def embed_images(frames: list[np.ndarray]) -> np.ndarray:
    return get_embedder().images(frames)


def embed_texts(texts: list[str]) -> np.ndarray:
    return get_embedder().texts(texts)


def embedder_name() -> str:
    return type(get_embedder()).__name__
