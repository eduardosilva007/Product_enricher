#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    args = [sys.executable, str(ROOT / "tools" / "release_gate.py")]
    args.extend(sys.argv[1:] or ["--phase", "preflight", "--mode", "local"])
    raise SystemExit(subprocess.call(args, cwd=ROOT))
