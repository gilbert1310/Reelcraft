import json
import xml.etree.ElementTree as ET
from pathlib import Path

import opentimelineio as otio

from app import assemble, breakdown, checks, library
from app.media import probe


def _analysis(media):
    if "analysis" not in media:
        media["analysis"] = breakdown.analyze(str(media["reel"]))
    return media["analysis"]


def test_breakdown_groups_cuts_into_setups(media):
    res = _analysis(media)
    cuts, setups = res["cuts"], res["setups"]
    assert len(cuts) == media["expected_cuts"]
    assert len(setups) == 2, [c["setup_number"] for c in cuts]
    # Alternating pattern A B A B A B
    pattern = [c["setup_number"] for c in cuts]
    assert pattern[0::2] == [pattern[0]] * 3 and pattern[1::2] == [pattern[1]] * 3
    assert pattern[0] != pattern[1]
    assert all(len(c["embedding"]) == 512 for c in cuts)
    assert all(c["thumbnail_b64"] for c in cuts)
    # Edit pattern timing is preserved
    assert abs(cuts[2]["start_time"] - 2.0) < 0.1 and abs(cuts[2]["end_time"] - 3.2) < 0.1
    r = res["recipe"]
    assert r["cut_count"] == 6 and r["setup_count"] == 2 and len(r["embedding"]) == 512
    # Mandelbrot setup pushes in continuously
    b_cut = next(c for c in cuts if c["setup_number"] == pattern[1])
    assert b_cut["stats"]["zoom"] > 0.02, b_cut["stats"]


def test_coverage_check(media):
    res = _analysis(media)
    sn = res["cuts"][0]["setup_number"]
    setup = next(s for s in res["setups"] if s["setup_number"] == sn)
    cuts = [c for c in res["cuts"] if c["setup_number"] == sn]
    out = checks.coverage_check(str(media["user_a"]), setup, cuts, use_ai=False)
    assert out["check_status"] == "pass", out
    assert len(out["coverage"]) == 3
    # Matched windows move forward through the take
    starts = [c["match_start"] for c in out["coverage"]]
    assert starts == sorted(starts), starts
    # The other setup's clip should not cover these cuts
    wrong = checks.coverage_check(str(media["user_b"]), setup, cuts, use_ai=False)
    assert wrong["setup_similarity"] < out["setup_similarity"]


def test_suggest_setup(media):
    res = _analysis(media)
    out = checks.suggest_setup(str(media["user_b"]), res["setups"], use_ai=False)
    assert out["suggested_setup_number"] == res["cuts"][1]["setup_number"], out


def _payload(media, res):
    a_sn, b_sn = res["cuts"][0]["setup_number"], res["cuts"][1]["setup_number"]
    return {
        "captures": [
            {"id": "cap-a", "url": Path(media["user_a"]).as_uri(), "file_name": "IMG_0001.MOV", "setup_number": a_sn},
            {"id": "cap-b", "url": Path(media["user_b"]).as_uri(), "file_name": "IMG_0002.MOV", "setup_number": b_sn},
        ],
        "cuts": [{**c, "id": f"cut-{c['order_index']}"} for c in res["cuts"]],
        "outputs": {}, "fps": 30, "width": 540, "height": 960,
    }


def test_assemble_renders_preview_and_timelines(media, tmp_path, monkeypatch):
    res = _analysis(media)
    # Local files instead of signed URLs
    monkeypatch.setattr(assemble, "download", lambda url, dest, *a, **k: Path(url[7:]))
    out = assemble.assemble(_payload(media, res), tmp_path)
    info = probe(out["preview_local"])
    total = sum(c["end_time"] - c["start_time"] for c in res["cuts"])
    assert abs(info.duration - total) < 0.2, (info.duration, total)
    assert (info.width, info.height) == (540, 960)
    assert len(out["cut_matches"]) == 6 and not out["reshoots"]
    a_fixes = out["cut_matches"][0]["fixes"]
    assert "Colour matched" in a_fixes, a_fixes
    # Timelines
    tl = otio.adapters.read_from_file(str(tmp_path / "timeline.otio"))
    clips = list(tl.find_clips())
    assert len(clips) == 6 and clips[0].media_reference.target_url.endswith("IMG_0001.MOV")
    root = ET.parse(tmp_path / "timeline.fcpxml").getroot()
    assert root.tag == "fcpxml" and len(root.findall(".//spine/asset-clip")) == 6


def test_slow_motion_and_transitions_render(media, tmp_path, monkeypatch):
    res = _analysis(media)
    p = _payload(media, res)
    p["cuts"][1]["speed"] = 0.5
    p["cuts"][2]["transition_in"] = "dissolve"
    p["cuts"][3]["transition_in"] = "whip"
    p["cuts"][4]["transition_in"] = "zoom"
    monkeypatch.setattr(assemble, "download", lambda url, dest, *a, **k: Path(url[7:]))
    out = assemble.assemble(p, tmp_path)
    total = sum(c["end_time"] - c["start_time"] for c in res["cuts"])
    assert abs(probe(out["preview_local"]).duration - total) < 0.2
    assert any(f.startswith("Slow motion") for f in out["cut_matches"][1]["fixes"])
    fx = (tmp_path / "timeline.fcpxml").read_text()
    assert "<timeMap>" in fx
    root = ET.fromstring(fx.split("\n", 2)[2])
    spine = root.find(".//spine")
    kinds = [el.tag for el in spine]
    # a real cross dissolve sits between cut 2 and cut 3, and whip/zoom get markers
    assert kinds[:4] == ["asset-clip", "asset-clip", "transition", "asset-clip"], kinds
    assert root.find(".//resources/effect[@name='Cross Dissolve']") is not None
    markers = [m.get("value") for m in spine.iter("marker")]
    assert any("whip-pan" in m for m in markers) and any("zoom transition" in m for m in markers)
    for clip in spine.findall("asset-clip"):
        tags = [c.tag for c in clip]
        if "marker" in tags:
            assert tags.index("marker") > max(tags.index(t) for t in ("adjust-transform",) if t in tags)


def test_missing_setup_becomes_reshoot(media, tmp_path, monkeypatch):
    res = _analysis(media)
    p = _payload(media, res)
    p["captures"] = p["captures"][:1]
    monkeypatch.setattr(assemble, "download", lambda url, dest, *a, **k: Path(url[7:]))
    out = assemble.assemble(p, tmp_path)
    assert out["reshoots"] and out["reshoots"][0]["setup_number"] == res["cuts"][1]["setup_number"]


def test_stage3_suggest_revise_export(media, tmp_path, monkeypatch):
    res = _analysis(media)
    recipe = {"id": "rec-1", **res["recipe"]}
    monkeypatch.setattr(assemble, "download", lambda url, dest, *a, **k: Path(url[7:]))
    clips = [{"id": "raw-1", "url": Path(media["user_a"]).as_uri(), "file_name": "A.mp4"},
             {"id": "raw-2", "url": Path(media["user_b"]).as_uri(), "file_name": "B.mp4"}]
    out = library.suggest({"clips": clips, "recipes": [recipe], "mood": "energetic", "outputs": [],
                           "width": 540, "height": 960, "preview_scale": 0.5}, _mk(tmp_path / "s"))
    assert out["suggestions"] and out["suggestions"][0]["recipe_id"] == "rec-1"
    plan = out["suggestions"][0]["plan"]
    rev = library.revise({"clips": clips, "labels": out["labels"], "plan": plan, "recipe": recipe,
                          "instruction": "more slow motion", "width": 540, "height": 960},
                         _mk(tmp_path / "r"))
    assert rev["plan"]
    exp = library.export({"clips": clips, "plan": rev["plan"], "recipe": recipe, "width": 540, "height": 960},
                         _mk(tmp_path / "e"))
    assert exp["edit_list"] and (tmp_path / "e" / "timeline.otio").exists()


def _mk(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_shot_planner_groups_by_take(media):
    res = _analysis(media)
    assert res["plan_summary"]
    # The planner (mock follows the fingerprint hint) still yields the 2 alternating takes.
    assert len(res["setups"]) == 2


def test_ai_unavailable_fails_loudly(media, monkeypatch, tmp_path):
    from app import vision, jobs, main
    from fastapi.testclient import TestClient

    def boom(*a, **k):
        raise vision.VisionUnavailable("The Anthropic account is out of credit.")
    monkeypatch.setattr(vision, "ask", boom)
    sent = []
    monkeypatch.setattr(jobs, "post_callback", lambda url, body: sent.append(body))
    monkeypatch.setattr(main, "fetch_video", lambda *a, **k: Path(media["reel"]))
    TestClient(main.app).post("/analyze", headers={"x-api-key": "test-key"},
                              json={"job_id": "j", "callback_url": "http://cb", "upload_id": "u", "video_url": "http://v"})
    assert sent[0]["status"] == "failed" and "out of credit" in sent[0]["error"], sent[0]


def test_partition_problem_reports_gaps():
    from app.breakdown import _partition_problem
    assert _partition_problem([[0, 1], [2]], 3) is None
    assert "missing cuts 3" in _partition_problem([[0, 1]], 3)
    assert "more than one take" in _partition_problem([[0, 1], [1, 2]], 3)


def test_storyboard_covers_whole_reel_and_every_cut(media):
    from app.breakdown import Cut, MAX_PLAN_IMAGES, storyboard
    from app.media import FrameReader, probe
    info = probe(media["reel"])
    res = _analysis(media)
    cuts = [Cut(c["order_index"], c["start_time"], c["end_time"]) for c in res["cuts"]]
    with FrameReader(str(media["reel"])) as r:
        board = storyboard(r, cuts, info.duration)
    times = [t for t, _, _ in board]
    assert times == sorted(times) and times[0] < 0.5 and times[-1] > info.duration - 0.6
    assert {ci for _, ci, _ in board} == {c.index for c in cuts}      # every cut seen
    assert len(board) <= MAX_PLAN_IMAGES
    assert max(max(f.shape[:2]) for _, _, f in board) <= 384          # small frames only


def test_audit_pass_can_add_a_missed_take(media, monkeypatch):
    """The second pass sees the draft and may split it; its corrected plan is used."""
    from app import breakdown, vision
    res = _analysis(media)
    cuts = [breakdown.Cut(c["order_index"], c["start_time"], c["end_time"]) for c in res["cuts"]]
    calls = []

    def fake_ask(parts, schema, **kw):
        calls.append(parts)
        audit = any(p.get("type") == "text" and p["text"].startswith("Draft plan to audit") for p in parts)
        groups = [[1, 3, 5], [2, 4], [6]] if audit else [[1, 3, 5], [2, 4, 6]]
        return vision.ShotPlan(shoot_notes="x", summary=f"{len(groups)} takes", setups=[
            vision.PlannedSetup(setup_number=k + 1, cut_numbers=g, description="d", angle="front", subject="s",
                                film_duration_seconds=8, beginner_tip="t", reason="r") for k, g in enumerate(groups)],
            cuts=[])
    monkeypatch.setattr(vision, "ask", fake_ask)
    plan = breakdown.plan_setups(cuts, [[0, 2, 4], [1, 3, 5]], 6.8, [])
    assert len(calls) == 2 and plan.summary == "3 takes"


def test_planner_prompt_has_no_fixed_example_list():
    from app import vision
    p = vision.SHOT_PLANNER.lower()
    # Generic guidance only: nothing copied from a specific reel.
    assert "hand on head" not in p and "turns around to look" not in p
    assert "fewest takes" in p and "graphics" in p


def test_graphics_are_not_takes_and_render_black(media, tmp_path, monkeypatch):
    from app import breakdown, vision
    res = _analysis(media)

    def fake_ask(parts, schema, **kw):
        if schema is vision.ShotPlan:
            return vision.ShotPlan(shoot_notes="x", summary="1 take + end card", graphics=[6], setups=[
                vision.PlannedSetup(setup_number=1, cut_numbers=[1, 2, 3, 4, 5], description="d", angle="front",
                                    subject="s", film_duration_seconds=10, beginner_tip="t", reason="r")], cuts=[])
        return vision._mock(schema, parts)
    monkeypatch.setattr(vision, "ask", fake_ask)
    out = breakdown.analyze(str(media["reel"]))
    takes = [s for s in out["setups"] if not s["is_graphic"]]
    graphic = [s for s in out["setups"] if s["is_graphic"]]
    assert len(takes) == 1 and len(graphic) == 1 and graphic[0]["film_duration_seconds"] == 0
    assert out["cuts"][5]["setup_number"] == graphic[0]["setup_number"]

    # Assembly with no clip for the graphic: black placeholder, no reshoot request.
    monkeypatch.setattr(assemble, "download", lambda url, dest, *a, **k: Path(url[7:]))
    p = _payload(media, out)
    p["captures"] = [{**p["captures"][0], "setup_number": takes[0]["setup_number"]}]
    p["graphic_setups"] = [graphic[0]["setup_number"]]
    res2 = assemble.assemble(p, tmp_path)
    assert not any(5 in r["cut_order_indexes"] for r in res2["reshoots"])  # the end card never asks for a reshoot
    assert res2["cut_matches"][5]["fixes"][0].startswith("Graphic")


def test_takes_carry_beats_linked_to_their_cuts(media):
    res = _analysis(media)
    for s in res["setups"]:
        members = {c["order_index"] for c in res["cuts"] if c["setup_number"] == s["setup_number"]}
        assert s["beats"], s
        for b in s["beats"]:
            assert b["text"] and set(b["cuts"]) <= members


def test_rhythm_finds_tempo_and_cuts_on_beat(tmp_path):
    import subprocess
    from app import audio
    wav = tmp_path / "clicks.mp4"
    # 120 BPM click track (a short 1 kHz blip every 0.5 s) over a 12 s test video
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x284:rate=30:duration=12",
                    "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t,0.5),0.03),sin(2*PI*1000*t),0)':s=44100:d=12",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(wav)], check=True)
    r = audio.rhythm(str(wav), [0, 2.0, 3.5, 5.0, 7.27, 9.0], 12)
    assert r["has_audio"] and abs(r["bpm"] - 120) < 4, r
    assert r["cuts_on_beat"] == 4 and r["cuts_total"] == 5, r      # 7.27 s is off the beat


def test_recipe_review_is_in_the_result(media):
    res = _analysis(media)
    rv = res["review"]
    assert rv and rv["hook"] and rv["shoot_schedule"] and rv["measured"]["cuts"] == 6
    assert rv["measured"]["rhythm"].get("has_audio") is False   # the test reel is silent


def test_every_transition_effect_and_look_renders(media, tmp_path):
    import os
    from app import effects as fx
    from app.correct import RenderCut
    from app.media import probe
    from app.render import render
    srcs = [str(media["user_a"]), str(media["user_b"])]
    cuts = []
    for i, t in enumerate(fx.TRANSITIONS):
        cuts.append(RenderCut(order_index=i, src_path=srcs[i % 2], src_name="x.mp4", src_w=540, src_h=960, src_fps=30,
                              src_in=0.4 * (i % 5), speed=1.0, out_len=0.6, transition_in=t,
                              effects=[fx.EFFECTS[i % len(fx.EFFECTS)]]))
    out = tmp_path / "fx.mp4"
    for look in fx.LOOKS:
        stats = render(cuts if look == "warm" else cuts[:2], str(out), 180, 320, 30, tmp_path, look=look)
    stats = render(cuts, str(out), 180, 320, 30, tmp_path, look="teal_orange")
    expected = sum(round(c.out_len * 30) for c in cuts)
    assert stats["frames"] == expected and abs(probe(out).duration - expected / 30) < 0.1
    sheet = os.environ.get("FX_SHEET")
    if sheet:
        import shutil
        shutil.copy(out, sheet)


def test_low_memory_mode_renders_slow_motion_and_stabilised_cuts(media, tmp_path, monkeypatch):
    from app.correct import RenderCut
    from app.media import x264
    from app.render import render
    monkeypatch.setenv("LOW_MEMORY", "1")
    assert "ultrafast" in x264()
    cuts = [RenderCut(order_index=0, src_path=str(media["user_a"]), src_name="a.mp4", src_w=540, src_h=960, src_fps=30,
                      src_in=0.5, speed=0.5, out_len=1.0, interpolate=True),
            RenderCut(order_index=1, src_path=str(media["user_b"]), src_name="b.mp4", src_w=540, src_h=960, src_fps=30,
                      src_in=0.5, speed=1.0, out_len=1.0, stabilize=True, transition_in="dissolve")]
    stats = render(cuts, str(tmp_path / "lm.mp4"), 270, 480, 30, tmp_path)
    assert stats["frames"] == 60
