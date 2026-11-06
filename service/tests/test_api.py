from pathlib import Path

from fastapi.testclient import TestClient

from app import jobs, main


def test_requires_api_key():
    c = TestClient(main.app)
    r = c.post("/analyze", json={"job_id": "j", "callback_url": "http://cb", "upload_id": "u", "video_url": "http://v"})
    assert r.status_code == 401


def test_rejects_unsupported_links():
    c = TestClient(main.app)
    r = c.post("/analyze", headers={"x-api-key": "test-key"},
               json={"job_id": "j", "callback_url": "http://cb", "upload_id": "u", "source_url": "https://evil.example/x"})
    assert r.status_code == 400


def test_analyze_posts_result_to_callback(media, monkeypatch):
    sent = []
    monkeypatch.setattr(jobs, "post_callback", lambda url, body: sent.append((url, body)))
    monkeypatch.setattr(main, "fetch_video", lambda url, dest, *a, **k: Path(media["reel"]))
    c = TestClient(main.app)
    r = c.post("/analyze", headers={"x-api-key": "test-key"},
               json={"job_id": "job-1", "callback_url": "http://cb", "upload_id": "up-1", "video_url": "http://v"})
    assert r.status_code == 200 and r.json()["status"] == "processing"
    (url, body), = sent
    assert url == "http://cb" and body["job_id"] == "job-1" and body["upload_id"] == "up-1"
    assert body["status"] == "done", body
    assert len(body["result"]["setups"]) == 2


def test_failure_is_reported_not_raised(monkeypatch):
    sent = []
    monkeypatch.setattr(jobs, "post_callback", lambda url, body: sent.append(body))
    monkeypatch.setattr(main, "fetch_video", lambda *a, **k: (_ for _ in ()).throw(main.MediaError("HTTP 403")))
    c = TestClient(main.app)
    c.post("/check-clip", headers={"x-api-key": "test-key"},
           json={"job_id": "j2", "callback_url": "http://cb", "capture_id": "c", "clip_url": "http://x",
                 "setup": {}, "cuts": []})
    assert sent[0]["status"] == "failed" and sent[0]["capture_id"] == "c"


def test_webm_and_hevc_are_normalised(media, tmp_path):
    from app.media import normalise, probe
    import subprocess
    webm = tmp_path / "rec.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(media["user_a"]), "-t", "2", "-c:v", "libvpx",
                    "-deadline", "realtime", str(webm)], check=True)
    out = normalise(webm)
    assert out.suffix == ".mp4" and probe(out).codec == "h264" and probe(out).duration > 1.5
    assert normalise(media["user_a"]) == media["user_a"]  # already fine: untouched


def test_health_does_not_load_the_model():
    from app import embed
    embed._embedder = None
    r = TestClient(main.app).get("/health")
    assert r.status_code == 200 and r.json()["ok"] and embed._embedder is None


def test_killed_ffmpeg_reports_out_of_memory(monkeypatch):
    import subprocess
    import pytest
    from app import media
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, -9, "", ""))
    with pytest.raises(media.MediaError, match="ran out of memory"):
        media.ffmpeg("-i", "x")


def test_api_key_paste_mistakes_are_tidied(monkeypatch):
    from app import vision
    real = "sk-ant-api03-" + "x" * 95
    for pasted in [f" {real}\n", f'"{real}"', f"ANTHROPIC_API_KEY={real}"]:
        assert vision.clean_api_key(pasted) == real
    monkeypatch.setenv("ANTHROPIC_API_KEY", f' "{real}" ')
    assert vision.key_status().startswith("looks valid") and "tidied" in vision.key_status()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "YOUR-KEY-FROM-console.anthropic.com")
    assert "doesn't look like" in vision.key_status()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert vision.key_status() == "missing"
    body = TestClient(main.app).get("/health").json()
    assert body["anthropic_key"] == "missing" and "sk-ant" not in str(body).replace("start with sk-ant-", "")
