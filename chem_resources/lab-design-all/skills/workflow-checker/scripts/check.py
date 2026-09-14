#!/usr/bin/env python3
"""Repository-local Skill entrypoint for the shared offline dispatch checker."""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    # Anchor imports to this checkout, not the caller's CWD or an old installation.
    script = Path(__file__).resolve()
    if len(script.parents) <= 5:
        print("workflow-checker requires the complete chem-agent repository layout.", file=sys.stderr)
        return 2
    repo_root = script.parents[5]
    engine = repo_root / "device_agent" / "check_workflow.py"
    expected = repo_root / "chem_resources" / "lab-design-all" / "skills" / "workflow-checker" / "scripts" / "check.py"
    if script != expected or not engine.is_file():
        print(
            "workflow-checker requires this Skill inside the complete chem-agent repository; "
            "device_agent/check_workflow.py was not found in the expected checkout.",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(repo_root))
    try:
        from device_agent.check_workflow import main as check_workflow
    except (ImportError, OSError) as exc:
        print(f"The local workflow-checker engine could not be loaded: {exc}", file=sys.stderr)
        return 2
    return check_workflow(argv)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
