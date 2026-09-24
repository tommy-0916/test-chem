#!/usr/bin/env python3
"""Run the exact preflight bundled inside the distributable Skill."""

from __future__ import annotations

import os
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skill/chem-agent-eval-sop/scripts/preflight.py"
)


if __name__ == "__main__":
    repo = None
    for index, argument in enumerate(sys.argv[1:], start=1):
        if argument == "--repo" and index + 1 < len(sys.argv):
            repo = Path(sys.argv[index + 1]).expanduser().resolve()
            break
        if argument.startswith("--repo="):
            repo = Path(argument.split("=", 1)[1]).expanduser().resolve()
            break
    python = repo / ".venv/bin/python" if repo else Path(sys.executable)
    if not python.exists():
        python = Path(sys.executable)
    os.execv(str(python), [str(python), str(SCRIPT), *sys.argv[1:]])
