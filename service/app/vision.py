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


class PlannedSetup(BaseModel):
    setup_number: int
    cut_numbers: list[int] = Field(description="Every cut filmed in this one continuous take")
    description: str = Field(description="Where to put the camera and how to film, then 'Direct her/him "
                             "through:' and the numbered beats to call out, in order")
    angle: Angle
    subject: str
    film_duration_seconds: float
    beginner_tip: str
    reason: str = Field(description="Why these cuts can come from one take")


class ShotPlan(BaseModel):
    shoot_notes: str = Field(description="First, what physically happened on set: each location and camera "
                             "placement, and everything the subject did there, in story order")
    summary: str = Field(description="One or two plain sentences: how many takes and what they are")
    setups: list[PlannedSetup]
    graphics: list[int] = Field(default_factory=list, description="Cut numbers that are not filmed footage: "
                                "logo or end cards, title or text cards, blank frames added in the edit")
    cuts: list[CutDetail] = Field(description="One entry per cut, by cut number")


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


SHOT_PLANNER = (
    "You are an experienced videographer and director planning how to shoot a copy of this reel in one "
    "efficient session. Below is a storyboard of the reel (frames every half-second, each stamped with its "
    "time and the edit cut it belongs to), then the list of cuts with timing and measured camera motion.\n\n"
    "Plan the TAKES: each time the videographer sets up the camera and presses record. Think like someone "
    "on set with a model and a phone or camera, not like an editor counting cuts. Editors slice a few long "
    "takes into many short cuts, so plan the fewest takes that a real crew would shoot to get every cut. "
    "For a 15 to 60 second reel that is usually 3 to 6 takes; go beyond that only when the reel truly has "
    "more locations or camera placements.\n\n"
    "Organise takes by location and camera placement. Inside one take the model performs a flow of beats "
    "while the camera keeps rolling, so these do NOT need a new take:\n"
    "- changes of pose, gesture, expression or hand position (a smile, a hand gesture, a look away and "
    "back);\n"
    "- the model turning to show a profile or her back while the camera stays put;\n"
    "- repeating the same movement several times with variations (walk towards camera three times, "
    "changing the hands each pass);\n"
    "- full, medium and close framing from the same spot (shoot in 4K and crop in, or step closer between "
    "beats);\n"
    "- slow motion or speed ramps (shoot at 60fps);\n"
    "- a walk that passes the camera and continues away (pivot and keep filming).\n"
    "Start a new take only for a different location or background, a clearly different camera height or "
    "angle (for example a low-angle face close-up), a different lens feel (very wide vs long), or a camera "
    "move that can't be done in the same recording.\n"
    "Ignore app interface overlays (usernames, buttons, captions, progress bars): the video may be a "
    "screen recording. Cuts that are not filmed at all (logo or end cards, title or text cards, blank "
    "frames) go in graphics, not in a take. The fingerprint hint is a rough colour-similarity grouping that "
    "over-splits; use it only as a clue.\n\n"
    "Step 1: watch the storyboard as a story and write shoot_notes: each location and camera placement, "
    "and everything the subject does there.\n"
    "Step 2: plan the takes. For each take give the cut numbers it covers (every cut appears exactly once "
    "across takes and graphics); a description that says where to put the camera and how to film (height, "
    "framing and lens feel, movement, light, focus, frame rate), followed by 'Direct her through:' (or "
    "him/them) and the numbered beats to call out in order, so a beginner can run the take; seconds to film "
    "so every beat is covered with margin; one practical beginner tip; and a one-line reason these cuts "
    "come from one take. Number takes in the order they first appear. For each cut give a few-word action, "
    "the transition into it (keep the detected one unless the frames clearly show otherwise; 'match' for "
    "a match cut) and whether it plays in slow motion. Finish with a plain summary such as 'Filmed as 5 "
    "takes: …', naming each take."
)

PLAN_AUDIT = (
    "Now review the draft plan above as the director on set. Your goal is the fewest takes a real crew "
    "would shoot. Merge takes that share a location and camera placement: their differences in pose, "
    "gesture, the model's facing direction, framing or speed become beats inside one take. Check every "
    "cut is still covered by the take it is in. Move any non-filmed cut (logo or end card, title) to "
    "graphics. Split a take only if its cuts truly need the camera in a different place. Return the "
    "complete corrected plan in the same format, with shoot_notes saying what you changed and why."
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


class VisionUnavailable(Exception):
    """The vision AI can't be used at all (bad key, no credit, wrong model, unreachable).
    The message is written for the person running ReelCraft and is safe to show."""


_client = None
_fallbacks_ok = True


def clean_api_key(raw: str | None) -> str:
    """Forgive common paste mistakes: spaces, newlines, quotes, or the variable name itself."""
    key = (raw or "").strip().strip('"').strip("'").strip()
    if key.upper().startswith("ANTHROPIC_API_KEY="):
        key = key.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def key_status() -> str:
    """A safe description of the configured key for /health (never shows the key itself)."""
    import os
    raw = os.getenv("ANTHROPIC_API_KEY")
    key = clean_api_key(raw)
    if not key:
        return "missing"
    if not key.startswith("sk-ant-"):
        return "set, but doesn't look like an Anthropic key (should start with sk-ant-)"
    if len(key) < 90:
        return f"set, but looks too short ({len(key)} characters), maybe cut off while copying"
    tidied = " (tidied: removed extra spaces or quotes)" if key != raw else ""
    return f"looks valid ({len(key)} characters){tidied}"


def _anthropic():
    global _client
    if _client is None:
        import os
        import anthropic
        _client = anthropic.Anthropic(api_key=clean_api_key(os.getenv("ANTHROPIC_API_KEY")) or None,
                                      max_retries=3, timeout=300)
    return _client


def _call(client, kwargs: dict):
    global _fallbacks_ok
    import anthropic
    if settings.vision_fallbacks and _fallbacks_ok:
        try:
            # Re-run on Anthropic's recommended fallback model if a safety classifier declines.
            return client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"],
                                              fallbacks="default", **kwargs)
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            log.warning("Refusal fallbacks not accepted for this account/model; continuing without them")
            _fallbacks_ok = False
    return client.messages.parse(**kwargs)


def ask(parts: list[dict], schema: type[T], system: str | None = None, max_tokens: int = 8000,
        effort: str | None = None) -> T:
    """One vision call that returns a validated `schema` instance."""
    if settings.vision_provider == "mock":
        return _mock(schema, parts)
    import anthropic

    kwargs = dict(
        model=settings.vision_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": parts}],
        output_format=schema,
        output_config={"effort": effort or settings.vision_effort},
    )
    if system:
        kwargs["system"] = system
    try:
        resp = _call(_anthropic(), kwargs)
    except anthropic.AuthenticationError as e:
        raise VisionUnavailable("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY on Railway.") from e
    except anthropic.PermissionDeniedError as e:
        raise VisionUnavailable(f"This Anthropic key can't use the model {settings.vision_model}. "
                                "Check the key's workspace, or set VISION_MODEL on Railway.") from e
    except anthropic.NotFoundError as e:
        raise VisionUnavailable(f"Anthropic doesn't recognise the model {settings.vision_model}. "
                                "Check VISION_MODEL on Railway.") from e
    except anthropic.BadRequestError as e:
        msg = str(e)
        if "credit balance" in msg.lower():
            raise VisionUnavailable("The Anthropic account is out of credit. Add credit at "
                                    "console.anthropic.com > Billing.") from e
        raise VisionUnavailable(f"Anthropic rejected the request: {msg[:240]}") from e
    except anthropic.APIConnectionError as e:
        raise VisionUnavailable("Couldn't reach Anthropic from the video service. Try again in a minute.") from e
    except TypeError as e:
        if "api_key" in str(e) or "authentication" in str(e).lower():
            raise VisionUnavailable("ANTHROPIC_API_KEY isn't set on Railway.") from e
        raise
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
    if schema is ShotPlan:
        import re
        hint = next((p["text"] for p in parts if p.get("type") == "text" and p["text"].startswith("Fingerprint hint")), "")
        groups = [[int(n) for n in g.split(",") if n.strip()] for g in re.findall(r"\[([\d,\s]+)\]", hint)]
        cuts = _count(parts, "Cut ")
        if not groups:
            groups = [cuts]
        return ShotPlan(shoot_notes="mock", summary=f"Filmed as {len(groups)} takes (mock).", setups=[
            PlannedSetup(setup_number=k + 1, cut_numbers=g, description="Mock take.", angle="front",
                         subject="subject", film_duration_seconds=10, beginner_tip="Mock tip.", reason="mock")
            for k, g in enumerate(groups)],
            cuts=[CutDetail(cut_number=n, action="moves", transition_in="cut", slow_motion=False) for n in cuts])
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
