#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = PACKAGE_ROOT / "skill" / "chem-agent-eval-sop"
TEST_SOURCE = PACKAGE_ROOT / "test-data" / "测试题目.docx"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.backup-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup-{stamp}-{index}")
        index += 1
    return candidate


def replace_directory(source: Path, target: Path) -> str:
    if target.exists():
        if target.is_dir():
            backup = backup_path(target)
            shutil.move(str(target), str(backup))
        else:
            raise RuntimeError(f"Skill target exists but is not a directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return f"installed {target}"


def replace_file(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if digest(source) == digest(target):
            return f"already current {target}"
        backup = backup_path(target)
        shutil.copy2(target, backup)
    shutil.copy2(source, target)
    return f"installed {target}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Chem Agent eight-case evaluation package.")
    parser.add_argument("--repo", type=Path, required=True, help="Path to the Chem Agent repository.")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        help="Codex home directory. Default: $CODEX_HOME or ~/.codex.",
    )
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    codex_home = args.codex_home.expanduser().resolve()
    required = [repo / "reaserch_agent", repo / "device_agent", repo / "chem_resources"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Not a compatible Chem Agent repository; missing: " + ", ".join(missing))

    for source in (SKILL_SOURCE, TEST_SOURCE):
        if not source.exists():
            raise FileNotFoundError(f"Package is incomplete: {source}")

    messages = [replace_directory(SKILL_SOURCE, codex_home / "skills" / "chem-agent-eval-sop")]
    messages.append(replace_file(TEST_SOURCE, repo / "测试题目.docx"))

    print("Chem Agent evaluation package installed:")
    for message in messages:
        print(f"- {message}")
    print("The Chem Agent repository source was not modified.")
    print("Next: configure the repository .env, then run scripts/preflight.py from this package.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
