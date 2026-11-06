"""ReelCraft video service (FastAPI).

Every endpoint answers immediately with {"status": "processing"} and does the work in the
background; the result is POSTed to the request's callback_url (the reelcraft-callback
Supabase edge function), signed with REELCRAFT_CALLBACK_SECRET.
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import assemble as assemble_mod
from . import breakdown, checks, library, vision
from .config import settings
from .fetch import LinkFetchError, check_link, fetch_from_link
from .jobs import UserFacingError, run_job
from .media import MediaError, fetch_video, normalise, probe, require_ffmpeg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reelcraft")


@asynccontextmanager
async def lifespan(_app):
    require_ffmpeg()
    yield


app = FastAPI(title="ReelCraft video service", version="1.0.0", lifespan=lifespan)


def auth(x_api_key: str = Header(None)) -> None:
    if not settings.api_key:
        raise HTTPException(503, "Service is not configured (REELCRAFT_API_KEY is empty)")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(401, "Invalid API key")


class Job(BaseModel):
    job_id: str
    callback_url: str


def _check_callback(job: Job) -> None:
    if settings.callback_url_prefix and not job.callback_url.startswith(settings.callback_url_prefix):
        raise HTTPException(400, "callback_url is not allowed")


def _start(bg: BackgroundTasks, job: Job, kind: str, work, ref: dict) -> dict:
    _check_callback(job)
    bg.add_task(run_job, job.job_id, kind, job.callback_url, work, ref)
    return {"status": "processing", "job_id": job.job_id, "kind": kind}


@app.get("/health")
def health() -> dict:
    # Don't load the image model here: it's ~600 MB and health checks shouldn't cost memory.
    from . import embed
    return {"ok": True, "vision_provider": settings.vision_provider, "vision_model": settings.vision_model,
            "anthropic_key": vision.key_status(),
            "embedder": settings.embedder, "embedder_loaded": embed._embedder is not None}


# ---------- Stage 1 ----------

class AnalyzeRequest(Job):
    upload_id: str
    video_url: Optional[str] = None   # signed URL for an uploaded file
    source_url: Optional[str] = None  # Instagram / TikTok / Facebook / YouTube link
    feedback: Optional[str] = Field(default=None, max_length=1000)  # notes on a previous plan


@app.post("/analyze", dependencies=[Depends(auth)])
def analyze(req: AnalyzeRequest, bg: BackgroundTasks) -> dict:
    if not (req.video_url or req.source_url):
        raise HTTPException(400, "Send video_url or source_url")
    if req.source_url:
        try:
            check_link(req.source_url)
        except LinkFetchError as e:
            raise HTTPException(400, str(e))

    def work(tmp: Path) -> dict:
        try:
            if req.source_url:
                path = str(normalise(fetch_from_link(req.source_url, str(tmp))))
            else:
                path = str(fetch_video(req.video_url, tmp / "reel.mp4", settings.max_download_mb))
        except LinkFetchError as e:
            raise UserFacingError(str(e), status="link_failed")
        except MediaError as e:
            raise UserFacingError(str(e))
        try:
            info = probe(path)
        except MediaError:
            raise UserFacingError("That file isn't a video we can read. Try exporting it as MP4.")
        if info.duration > settings.max_video_seconds:
            raise UserFacingError(f"Reels longer than {settings.max_video_seconds // 60} minutes aren't supported.")
        try:
            return breakdown.analyze(path, feedback=req.feedback)
        except vision.VisionUnavailable as e:
            raise UserFacingError(f"The AI step couldn't run: {e}")
        except vision.VisionRefused:
            raise UserFacingError("The AI declined to analyse this video. Try a different reel.")

    return _start(bg, req, "analysis", work, {"upload_id": req.upload_id})


# ---------- Stage 2: on-set checks ----------

class CheckClipRequest(Job):
    capture_id: str
    clip_url: str
    setup: dict[str, Any]
    cuts: list[dict[str, Any]]
    use_ai: bool = True


@app.post("/check-clip", dependencies=[Depends(auth)])
def check_clip(req: CheckClipRequest, bg: BackgroundTasks) -> dict:
    def work(tmp: Path) -> dict:
        try:
            path = str(fetch_video(req.clip_url, tmp / "clip"))
        except MediaError:
            raise UserFacingError("We couldn't read that clip. Try recording again.")
        return checks.coverage_check(path, req.setup, req.cuts, req.use_ai)

    return _start(bg, req, "check", work, {"capture_id": req.capture_id})


class SuggestSetupRequest(Job):
    capture_id: str
    clip_url: str
    setups: list[dict[str, Any]]


@app.post("/suggest-setup", dependencies=[Depends(auth)])
def suggest_setup(req: SuggestSetupRequest, bg: BackgroundTasks) -> dict:
    def work(tmp: Path) -> dict:
        path = str(fetch_video(req.clip_url, tmp / "clip"))
        return checks.suggest_setup(path, req.setups)

    return _start(bg, req, "suggest_setup", work, {"capture_id": req.capture_id})


# ---------- Stage 2: assembly ----------

class Outputs(BaseModel):
    preview_upload_url: Optional[str] = None
    fcpxml_upload_url: Optional[str] = None
    otio_upload_url: Optional[str] = None


class AssembleRequest(Job):
    project_id: str
    captures: list[dict[str, Any]] = Field(description="[{id, url, file_name, setup_number}]")
    cuts: list[dict[str, Any]]
    graphic_setups: list[int] = []
    outputs: Outputs
    fps: float = 30
    width: int = settings.out_width
    height: int = settings.out_height
    title: str = "ReelCraft edit"
    music_url: Optional[str] = None


@app.post("/assemble", dependencies=[Depends(auth)])
def assemble(req: AssembleRequest, bg: BackgroundTasks) -> dict:
    if not req.captures or not req.cuts:
        raise HTTPException(400, "Need captures and cuts")
    payload = req.model_dump()

    def work(tmp: Path) -> dict:
        res = assemble_mod.assemble(payload, tmp)
        res.pop("preview_local", None)
        return res

    return _start(bg, req, "assemble", work, {"project_id": req.project_id})


# ---------- Stage 3 ----------

class SuggestRequest(Job):
    raw_project_id: str
    clips: list[dict[str, Any]]
    recipes: list[dict[str, Any]] = []
    mood: Optional[str] = None
    outputs: list[Outputs] = []
    fps: float = 30
    preview_scale: float = 0.5


@app.post("/suggest", dependencies=[Depends(auth)])
def suggest(req: SuggestRequest, bg: BackgroundTasks) -> dict:
    if not req.clips:
        raise HTTPException(400, "Need clips")
    payload = req.model_dump()
    return _start(bg, req, "suggest", lambda tmp: library.suggest(payload, tmp),
                  {"raw_project_id": req.raw_project_id})


class ReviseRequest(Job):
    raw_project_id: str
    instruction: str = Field(max_length=500)
    plan: list[dict[str, Any]]
    clips: list[dict[str, Any]]
    labels: list[dict[str, Any]] = []
    recipe: dict[str, Any] = {}
    outputs: Outputs = Outputs()
    fps: float = 30
    preview_scale: float = 0.5


@app.post("/revise", dependencies=[Depends(auth)])
def revise(req: ReviseRequest, bg: BackgroundTasks) -> dict:
    payload = req.model_dump()
    return _start(bg, req, "revise", lambda tmp: library.revise(payload, tmp),
                  {"raw_project_id": req.raw_project_id})


class ExportRequest(Job):
    raw_project_id: str
    plan: list[dict[str, Any]]
    clips: list[dict[str, Any]]
    recipe: dict[str, Any] = {}
    outputs: Outputs = Outputs()
    fps: float = 30
    title: str = "ReelCraft edit"
    music_url: Optional[str] = None


@app.post("/export", dependencies=[Depends(auth)])
def export(req: ExportRequest, bg: BackgroundTasks) -> dict:
    payload = req.model_dump()
    return _start(bg, req, "export", lambda tmp: library.export(payload, tmp),
                  {"raw_project_id": req.raw_project_id})
