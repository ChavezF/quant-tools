#!/usr/bin/env python3.12
"""No-agent hourly intraday position guard for Hermes cron."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT = Path(os.environ.get("QUANT_TOOLS_HOME", "/home/chavez_f/code/quant-tools"))
PY = os.environ.get("QUANT_PYTHON", "/usr/bin/python3.12")
CONFIG = Path(os.environ.get("QUANT_CONFIG", PROJECT / "config.json"))


def main() -> int:
    if not CONFIG.exists() or CONFIG.name == "config.example.json":
        print(f"cron_intraday_sentinel requires production config; got {CONFIG}", file=sys.stderr)
        return 1
    proc = subprocess.run(
        [
            PY,
            str(PROJECT / "scripts" / "quant.py"),
            "--config",
            str(CONFIG),
            "sentinel",
            "--send",
            "--json",
        ],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        timeout=420,
    )
    if proc.returncode:
        print(proc.stderr.strip() or "intraday sentinel failed", file=sys.stderr)
        return proc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
