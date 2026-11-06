import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("VISION_PROVIDER", "mock")
os.environ.setdefault("EMBEDDER", "histogram")
os.environ.setdefault("REELCRAFT_API_KEY", "test-key")
os.environ.setdefault("REELCRAFT_CALLBACK_SECRET", "test-secret")

W, H = 540, 960


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("media")
    a, b = d / "src_a.mp4", d / "src_b.mp4"
    # Setup A: test pattern with motion. Setup B: mandelbrot (continuous push-in).
    ff("-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=30:duration=14", "-pix_fmt", "yuv420p", str(a))
    ff("-f", "lavfi", "-i", f"mandelbrot=size={W}x{H}:rate=30", "-t", "14", "-pix_fmt", "yuv420p", str(b))
    # Reference reel: A B A B A B, each setup advancing through its own take.
    spans = [(a, 0.0, 1.0), (b, 0.0, 1.0), (a, 1.0, 2.2), (b, 1.0, 1.8), (a, 2.2, 3.4), (b, 1.8, 3.0)]
    parts = []
    for i, (src, s, e) in enumerate(spans):
        p = d / f"part{i}.mp4"
        ff("-ss", str(s), "-t", str(e - s), "-i", str(src), "-r", "30", "-pix_fmt", "yuv420p", str(p))
        parts.append(p)
    lst = d / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    reel = d / "reel.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(reel))
    # User captures: same setups, filmed differently (later in the take, colour shifted for A).
    ua, ub = d / "user_a.mp4", d / "user_b.mp4"
    ff("-ss", "1.5", "-i", str(a), "-t", "10", "-vf", "lutrgb=r='clipval*1.25':g='val':b='val*0.7',eq=brightness=0.06",
       "-pix_fmt", "yuv420p", str(ua))
    ff("-ss", "0.5", "-i", str(b), "-t", "10", "-pix_fmt", "yuv420p", str(ub))
    return {"dir": d, "reel": reel, "user_a": ua, "user_b": ub, "expected_cuts": len(spans)}
