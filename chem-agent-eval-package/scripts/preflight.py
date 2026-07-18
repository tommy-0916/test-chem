#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


EXPECTED_CASES = ["A01", "A02", "B01", "B02", "C01", "C02", "D01", "D02"]
CASE_RE = re.compile(r"^(A01|A02|B01|B02|C01|C02|D01|D02)\b")
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def extract_cases(path: Path) -> list[tuple[str, str]]:
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{WORD_NS}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{WORD_NS}t" and node.text:
                pieces.append(node.text)
            elif node.tag == f"{WORD_NS}tab":
                pieces.append("\t")
            elif node.tag == f"{WORD_NS}br":
                pieces.append("\n")
        paragraphs.append("".join(pieces).strip())

    cases: list[tuple[str, str]] = []
    index = 0
    while index < len(paragraphs):
        match = CASE_RE.match(paragraphs[index])
        if not match:
            index += 1
            continue
        query_index = index + 1
        while query_index < len(paragraphs) and not paragraphs[query_index]:
            query_index += 1
        if query_index >= len(paragraphs) or CASE_RE.match(paragraphs[query_index]):
            raise ValueError(f"missing Query after {match.group(1)}")
        cases.append((match.group(1), paragraphs[query_index]))
        index = query_index + 1
    return cases


def help_text(python: Path, script: Path, cwd: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [str(python), str(script), "--help"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return completed.returncode == 0, completed.stdout or ""


def api_key_present(repo: Path, preferred: str | None) -> tuple[bool, str]:
    values = load_dotenv(repo / ".env")
    values.update(os.environ)
    if preferred:
        return bool(values.get(preferred, "").strip()), preferred
    for name in ("CHEM_AGENT_EVAL_API_KEYS", "REFINER_LLM_API_KEY"):
        if values.get(name, "").strip():
            return True, name
    for index in range(1, 100):
        name = f"REFINER_LLM_POOL_{index}_API_KEY"
        if values.get(name, "").strip():
            return True, "REFINER_LLM_POOL_<N>_API_KEY"
    return False, "automatic discovery"


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight the Chem Agent eight-case evaluation setup.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--docx", type=Path, help="Default: <repo>/测试题目.docx")
    parser.add_argument("--api-key-env", help="Environment variable to check without printing its value.")
    parser.add_argument("--python", type=Path, help="Python executable. Default: <repo>/.venv/bin/python or current Python.")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    docx = (args.docx or repo / "测试题目.docx").expanduser().resolve()
    python = args.python.expanduser().resolve() if args.python else repo / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable).resolve()

    results: list[tuple[bool, str]] = []

    def check(condition: bool, message: str) -> None:
        results.append((condition, message))
        print(f"{'PASS' if condition else 'FAIL'}  {message}")

    check(repo.is_dir(), f"Chem Agent repository exists: {repo}")
    check(python.exists(), f"Python executable exists: {python}")

    try:
        cases = extract_cases(docx)
        ids = [case_id for case_id, _ in cases]
        unique_queries = len({query for _, query in cases})
        check(ids == EXPECTED_CASES and unique_queries == 8, "DOCX contains exactly A01-D02 and eight unique Queries")
    except Exception as exc:
        check(False, f"DOCX extraction failed: {type(exc).__name__}: {exc}")

    workstations = repo / "chem_resources/lab-design-main/skills/chemistry-experiment-workstation"
    modules = [
        "references-Synthesis-Module",
        "references-Reaction-and-Testing-Module",
        "references-Characterization-Module",
    ]
    station_dirs: list[Path] = []
    modules_ok = True
    for module in modules:
        module_path = workstations / module
        if not module_path.is_dir():
            modules_ok = False
            continue
        station_dirs.extend(path for path in module_path.iterdir() if path.is_dir())
    check(modules_ok and len(station_dirs) == 45, f"workstation Skill directory count is 45 (found {len(station_dirs)})")

    research_script = repo / "reaserch_agent/run_research_agent.py"
    research_ok, research_help = help_text(python, research_script, repo)
    research_flags = [
        "--online-literature",
        "--include-device-context",
        "--device-workstations-dir",
        "--model-name",
        "--base-url",
        "--wire-api",
        "--reasoning-effort",
        "--save-state",
    ]
    missing_research = [flag for flag in research_flags if flag not in research_help]
    check(research_ok and not missing_research, "Research CLI supports online retrieval, device context, custom LLM settings, and state output")
    if missing_research:
        print("      missing Research flags: " + ", ".join(missing_research))

    device_script = repo / "device_agent/run_from_research_state.py"
    device_ok, device_help = help_text(python, device_script, repo)
    device_flags = [
        "--exp-id",
        "--full-workstations",
        "--workstations-dir",
        "--model-name",
        "--base-url",
        "--wire-api",
        "--reasoning-effort",
        "--package-output",
    ]
    missing_device = [flag for flag in device_flags if flag not in device_help]
    check(device_ok and not missing_device, "Device CLI supports isolated IDs, all workstation Skills, custom LLM settings, and package output")
    if missing_device:
        print("      missing Device flags: " + ", ".join(missing_device))

    codex_home = args.codex_home.expanduser().resolve()
    installed_skill = codex_home / "skills/chem-agent-eval-sop/SKILL.md"
    check(installed_skill.is_file(), f"evaluation Skill is installed: {installed_skill}")

    key_ok, key_name = api_key_present(repo, args.api_key_env)
    check(key_ok, f"API key is available through {key_name}; value was not printed")

    runner = installed_skill.parent / "scripts/run_suite.py"
    runner_ok = runner.is_file()
    if runner_ok:
        source = runner.read_text(encoding="utf-8", errors="replace")
        forbidden = [token for token in ("--dispatch", "execution_adapter", "lab_dispatch", "requests.post(", "httpx.post(") if token in source]
        runner_ok = not forbidden
    else:
        forbidden = []
    check(runner_ok, "evaluation runner is planning-only and contains no real device dispatch path")
    if forbidden:
        print("      suspicious runner tokens: " + ", ".join(forbidden))

    check((PACKAGE_ROOT / ".env.example").is_file(), "share package includes a placeholder-only .env.example")

    passed = sum(1 for ok, _ in results if ok)
    print(json.dumps({"passed": passed, "total": len(results), "ready": passed == len(results)}, ensure_ascii=False))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
