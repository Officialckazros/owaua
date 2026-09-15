"""Apply native decoder resource limits before exec; Unix production hosts only."""

import os
import resource
import shutil
import sys

if not sys.platform.startswith("linux"):
    raise RuntimeError("Hardened music playback requires Linux resource limits")
resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
executable = shutil.which(sys.argv[1])
if executable is None:
    raise RuntimeError("FFmpeg is unavailable")
os.execv(executable, [executable, *sys.argv[2:]])
