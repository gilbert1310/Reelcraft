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
    assert "more than one" in _partition_problem([[0, 1], [1, 2]], 3)
