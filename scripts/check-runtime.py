"""Deployment checks without provider calls or Discord messages."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from music import BoundedAudio
from security import ALLOW_DMS, API_LIMITS, MAX_INFLIGHT

if not sys.platform.startswith("linux"):
    raise RuntimeError("Production music requires Linux resource limits")
if os.geteuid() == 0:
    raise RuntimeError("Run the production bot as an unprivileged user")
if not shutil.which("ffmpeg"):
    raise RuntimeError("FFmpeg is missing")

output = io.BytesIO()
with wave.open(output, "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(48000)
    wav.writeframes(b"\0\0" * 4800)
source = BoundedAudio(output.getvalue())
try:
    if not source.read():
        raise RuntimeError("Restricted FFmpeg decoder produced no audio")
finally:
    source.cleanup()

print("OWAUA_RUNTIME_VERIFIED " + json.dumps({
    "python": sys.version.split()[0],
    "unprivileged": True,
    "native_decoder": "passed",
    "allow_dms": ALLOW_DMS,
    "max_inflight": MAX_INFLIGHT,
    "api_limits": vars(API_LIMITS),
}), flush=True)
