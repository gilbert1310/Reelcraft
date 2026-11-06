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
