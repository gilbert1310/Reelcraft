"""Runtime settings, read once from environment variables."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Shared secret the Supabase edge function sends in the x-api-key header.
    api_key: str = field(default_factory=lambda: os.getenv("REELCRAFT_API_KEY", ""))
    # Secret this service sends back to the reelcraft-callback edge function.
    callback_secret: str = field(default_factory=lambda: os.getenv("REELCRAFT_CALLBACK_SECRET", ""))
    # Results are only ever POSTed to URLs starting with this (your reelcraft-callback function).
    callback_url_prefix: str = field(default_factory=lambda: os.getenv("CALLBACK_URL_PREFIX", ""))

    # Vision AI. "anthropic" for real use, "mock" for offline tests.
    vision_provider: str = field(default_factory=lambda: os.getenv("VISION_PROVIDER", "anthropic"))
    vision_model: str = field(default_factory=lambda: os.getenv("VISION_MODEL", "claude-opus-5-5"))
    vision_effort: str = field(default_factory=lambda: os.getenv("VISION_EFFORT", "medium"))
    vision_fallbacks: bool = field(default_factory=lambda: _bool("VISION_FALLBACKS", True))
    vision_image_px: int = field(default_factory=lambda: int(os.getenv("VISION_IMAGE_PX", "512")))

    # Image embeddings. "clip" (open-clip) in production, "histogram" for tests/small hosts.
    embedder: str = field(default_factory=lambda: os.getenv("EMBEDDER", "clip"))
    clip_model: str = field(default_factory=lambda: os.getenv("CLIP_MODEL", "ViT-B-32"))
    clip_pretrained: str = field(default_factory=lambda: os.getenv("CLIP_PRETRAINED", "laion2b_s34b_b79k"))

    # Stage 1 tuning.
    group_threshold: float = field(default_factory=lambda: float(os.getenv("GROUP_THRESHOLD", "0.85")))
    scene_threshold: float = field(default_factory=lambda: float(os.getenv("SCENE_THRESHOLD", "27")))
    min_cut_seconds: float = field(default_factory=lambda: float(os.getenv("MIN_CUT_SECONDS", "0.25")))

    # Coverage thresholds (cosine similarity of embeddings).
    covered_sim: float = field(default_factory=lambda: float(os.getenv("COVERED_SIM", "0.80")))
    weak_sim: float = field(default_factory=lambda: float(os.getenv("WEAK_SIM", "0.70")))

    # Output.
    out_width: int = field(default_factory=lambda: int(os.getenv("OUT_WIDTH", "1080")))
    out_height: int = field(default_factory=lambda: int(os.getenv("OUT_HEIGHT", "1920")))
    preview_scale: float = field(default_factory=lambda: float(os.getenv("PREVIEW_SCALE", "1.0")))

    # Link fetching.
    max_video_seconds: int = field(default_factory=lambda: int(os.getenv("MAX_VIDEO_SECONDS", "600")))
    max_download_mb: int = field(default_factory=lambda: int(os.getenv("MAX_DOWNLOAD_MB", "300")))

    work_dir: str = field(default_factory=lambda: os.getenv("WORK_DIR", "/tmp/reelcraft"))


settings = Settings()
