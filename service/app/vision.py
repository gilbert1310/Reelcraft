"""Vision AI calls (Claude via the Anthropic SDK), with typed JSON outputs.

Every call sends JPEG frames plus an instruction and gets back a validated Pydantic
object. VISION_PROVIDER=mock returns deterministic placeholder answers so the whole
pipeline can be exercised offline.
"""
from __future__ import annotations

import base64
import logging
from typing import Literal, Optional, TypeVar

import numpy as np
from pydantic import BaseModel, Field

from .config import settings
from .media import to_jpeg

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

Angle = Literal["behind", "front", "side", "overhead", "low", "pov", "other"]
Transition = Literal["cut", "whip", "dissolve", "zoom", "match"]


# ---------- Output schemas ----------

class GroupVerdict(BaseModel):
    """Answer for one candidate setup: which cuts belong together."""
    groups: list[list[int]] = Field(description="Cut numbers split into groups that share one camera setup")
    reason: str


class CutDetail(BaseModel):
    cut_number: int
    action: str = Field(description="What visibly happens in this cut, in a few words")
    transition_in: Transition
    slow_motion: bool


class CutDetails(BaseModel):
    cuts: list[CutDetail]


class MergeVerdict(BaseModel):
    """Which candidate setups are really the same camera setup."""
    groups: list[list[int]] = Field(description="Setup numbers grouped; setups in one group are the same angle and action")
    reason: str


class SetupDirection(BaseModel):
    description: str
    angle: Angle
    subject: str
    film_duration_seconds: float
    beginner_tip: str


class SetupBreakdown(SetupDirection):
    cuts: list[CutDetail] = Field(description="One entry per cut shown, by cut number")


class RecipeTags(BaseModel):
    genre: str
    moods: list[str]
    style_name: str
    summary: str


class ClipCheck(BaseModel):
    matches_setup: bool
    reason: str


class CutCoverageVerdict(BaseModel):
    cut_number: int
    status: Literal["covered", "weak", "missing"]
    reason: str


class CoverageVerdict(BaseModel):
    matches_setup: bool
    setup_feedback: str
    cuts: list[CutCoverageVerdict]


class SetupChoice(BaseModel):
    setup_number: int
    confidence: float
    reason: str


class ClipLabel(BaseModel):
    clip_number: int
    subject: str
    action: str
    angle: Angle
    shot_type: Literal["wide", "medium", "close", "detail"]
    quality: int = Field(description="1-10 usable quality")
    tags: list[str]


class ClipLabels(BaseModel):
    clips: list[ClipLabel]


class PlannedCut(BaseModel):
    order_index: int
    clip_number: int
    duration: float
    speed: float = 1.0
    transition_in: Transition = "cut"
    note: str = ""


class CutPlan(BaseModel):
    title: str
    cuts: list[PlannedCut]


# ---------- Prompts ----------

CINEMATOGRAPHER = (
    "You are a senior cinematographer breaking down a reel so a beginner can recreate it. "
    "These frames all come from ONE camera setup that the editor cut into several pieces. "
    "Describe the setup as one filming direction: framing and lens feel, camera position relative "
    "to the subject (behind, in front, side, overhead), camera movement and speed, the subject's "
    "action, lighting and mood, focus behaviour, and any slow motion. Recommend how many seconds "
    "to film so the editor has enough footage (the edit uses {used:.1f}s from this setup; recommend "
    "comfortably more). Add one practical beginner tip."
)


# ---------- Client ----------

def _image_block(img: np.ndarray) -> dict:
    data = base64.standard_b64encode(to_jpeg(img, settings.vision_image_px)).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def content(*parts) -> list[dict]:
    """Build a content list from strings and frames (np arrays), in order."""
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
        elif isinstance(p, np.ndarray):
            out.append(_image_block(p))
        elif isinstance(p, (list, tuple)):
            out.extend(content(*p))
    return out


class VisionRefused(Exception):
    pass


_client = None


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=3)  # reads ANTHROPIC_API_KEY
    return _client


def ask(parts: list[dict], schema: type[T], system: str | None = None, max_tokens: int = 8000) -> T:
    """One vision call that returns a validated `schema` instance."""
    if settings.vision_provider == "mock":
        return _mock(schema, parts)

    kwargs = dict(
        model=settings.vision_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": parts}],
        output_format=schema,
        output_config={"effort": settings.vision_effort},
    )
    if system:
        kwargs["system"] = system
    client = _anthropic()
    if settings.vision_fallbacks:
        # Re-run on Anthropic's recommended fallback model if a safety classifier declines.
        resp = client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"],
                                          fallbacks="default", **kwargs)
    else:
        resp = client.messages.parse(**kwargs)
    if resp.stop_reason == "refusal":
        raise VisionRefused("The vision model declined to analyse these frames")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Vision response was cut off (max_tokens)")
    parsed = resp.parsed_output
    if parsed is None:
        raise RuntimeError("Vision response did not match the expected format")
    return parsed


# ---------- Mock provider ----------

def _count(parts: list[dict], marker: str) -> list[int]:
    nums = []
    for p in parts:
        if p.get("type") == "text" and p["text"].startswith(marker):
            try:
                nums.append(int(p["text"][len(marker):].split()[0].strip(":")))
            except ValueError:
                pass
    return nums


def _mock(schema: type[T], parts: list[dict]) -> T:
    if schema is GroupVerdict:
        return GroupVerdict(groups=[_count(parts, "Cut ")], reason="mock")
    if schema is MergeVerdict:
        return MergeVerdict(groups=[[n] for n in _count(parts, "Setup ")], reason="mock")
    if schema is SetupBreakdown:
        return SetupBreakdown(description="Mock direction: medium shot following the subject.",
                              angle="behind", subject="subject", film_duration_seconds=12,
                              beginner_tip="Keep your elbows tucked in for steadier shots.",
                              cuts=[CutDetail(cut_number=n, action="subject moves", transition_in="cut",
                                              slow_motion=False) for n in _count(parts, "Cut ")])
    if schema is SetupDirection:
        return SetupDirection(description="Mock direction: medium shot following the subject.",
                              angle="behind", subject="subject", film_duration_seconds=12,
                              beginner_tip="Keep your elbows tucked in for steadier shots.")
    if schema is CutDetails:
        return CutDetails(cuts=[CutDetail(cut_number=n, action="subject moves", transition_in="cut",
                                          slow_motion=False) for n in _count(parts, "Cut ")])
    if schema is RecipeTags:
        return RecipeTags(genre="lifestyle", moods=["energetic"], style_name="Mock style",
                          summary="Fast alternating cuts between two setups.")
    if schema is ClipCheck:
        return ClipCheck(matches_setup=True, reason="mock")
    if schema is CoverageVerdict:
        return CoverageVerdict(matches_setup=True, setup_feedback="mock", cuts=[
            CutCoverageVerdict(cut_number=n, status="covered", reason="mock")
            for n in _count(parts, "Reference cut ")])
    if schema is SetupChoice:
        nums = _count(parts, "Setup ")
        return SetupChoice(setup_number=nums[0] if nums else 1, confidence=0.5, reason="mock")
    if schema is ClipLabels:
        return ClipLabels(clips=[ClipLabel(clip_number=n, subject="person", action="walking",
                                           angle="front", shot_type="medium", quality=7, tags=["walk"])
                                 for n in _count(parts, "Clip ")])
    if schema is CutPlan:
        nums = _count(parts, "Clip ") or [1]
        slots = _count(parts, "Slot ")
        return CutPlan(title="Mock plan", cuts=[
            PlannedCut(order_index=i, clip_number=nums[i % len(nums)], duration=1.0)
            for i in range(len(slots) or 4)])
    raise NotImplementedError(schema)
