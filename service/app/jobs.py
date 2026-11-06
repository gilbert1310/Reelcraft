"""Background job runner: do the work, then POST the result to the callback URL."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import tempfile
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Video jobs are CPU and memory heavy: run at most this many at once, queue the rest.
_slots = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_JOBS", "2")))


class UserFacingError(Exception):
    """An error whose message is safe and useful to show the user."""

    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


def post_callback(url: str, body: dict) -> None:
    headers = {"x-reelcraft-secret": settings.callback_secret}
    for attempt in range(4):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=60)
            if r.status_code < 500:
                if r.status_code >= 400:
                    log.error("Callback rejected (%s): %s", r.status_code, r.text[:300])
                return
            log.warning("Callback HTTP %s, retrying", r.status_code)
        except httpx.HTTPError as e:
            log.warning("Callback failed (%s), retrying", e)
        time.sleep(2 ** (attempt + 1))
    log.error("Giving up on callback for job %s", body.get("job_id"))


def run_job(job_id: str, kind: str, callback_url: str, work: Callable[[Path], dict], ref: dict) -> None:
    """Runs in a FastAPI background task. Never raises."""
    with _slots:
        _run(job_id, kind, callback_url, work, ref)


def _run(job_id: str, kind: str, callback_url: str, work: Callable[[Path], dict], ref: dict) -> None:
    Path(settings.work_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=settings.work_dir))
    started = time.time()
    body = {"job_id": job_id, "kind": kind, **ref}
    try:
        result = work(tmp)
        body.update(status="done", result=result)
    except UserFacingError as e:
        log.warning("Job %s (%s) failed: %s", job_id, kind, e)
        body.update(status=e.status, error=str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("Job %s (%s) crashed", job_id, kind)
        body.update(status="failed", error="Something went wrong while processing. Please try again.",
                    detail=f"{type(e).__name__}: {e}"[:500])
    finally:
        # Source videos only ever live in this folder, so they're gone when the job ends.
        shutil.rmtree(tmp, ignore_errors=True)
    body["seconds"] = round(time.time() - started, 1)
    post_callback(callback_url, body)
