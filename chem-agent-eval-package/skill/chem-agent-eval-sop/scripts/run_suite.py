#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from extract_cases import extract_cases


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def parse_key_list(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        parsed = json.loads(value)
        return [str(item).strip() for item in parsed if str(item).strip()]
    normalized = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def discover_keys(env: dict[str, str], preferred_env: str | None = None) -> list[str]:
    if preferred_env:
        preferred = parse_key_list(env.get(preferred_env, ""))
        if preferred:
            return preferred
    direct = parse_key_list(env.get("CHEM_AGENT_EVAL_API_KEYS", ""))
    if direct:
        return direct
    pooled: list[str] = []
    for index in range(1, 100):
        value = env.get(f"REFINER_LLM_POOL_{index}_API_KEY", "").strip()
        if value:
            pooled.append(value)
    if pooled:
        return pooled
    fallback = env.get("REFINER_LLM_API_KEY", "").strip()
    return [fallback] if fallback else []


def command_text(command: list[str]) -> str:
    return " ".join(command)


def run_command(command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, timeout: int, dry_run: bool) -> tuple[int, float, bool]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if dry_run:
        log_path.write_text("DRY RUN\n" + command_text(command) + "\n", encoding="utf-8")
        return 0, time.monotonic() - started, False
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        log_path.write_text(completed.stdout or "", encoding="utf-8")
        return completed.returncode, time.monotonic() - started, False
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        log_path.write_text(f"{output}\nTIMEOUT after {timeout} seconds\n", encoding="utf-8")
        return 124, time.monotonic() - started, True


def read_device_status(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "not_produced"
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "malformed_json"
    if not isinstance(package, dict):
        return False, "malformed_json"
    status = str(package.get("status", "unknown"))
    return status in {"success", "feasibility_error"}, status


def macro_plan_from_state(state: dict) -> list:
    macro = state.get("macro_plan")
    if isinstance(macro, list):
        return macro
    handoff = state.get("device_adaptation_handoff")
    if isinstance(handoff, dict) and isinstance(handoff.get("待执行 macro plan"), list):
        return handoff["待执行 macro plan"]
    return []


def run_case(case: dict[str, str], *, index: int, repo: Path, run_root: Path, run_stamp: str, python: Path, key: str, args: argparse.Namespace, base_env: dict[str, str]) -> dict:
    case_id = case["case_id"]
    local_id = f"{case_id}-{run_stamp}"
    case_dir = run_root / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "input.json").write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (case_dir / "query.sha256").write_text(case["query_sha256"] + "\n", encoding="utf-8")

    child_env = dict(base_env)
    child_env["REFINER_LLM_API_KEY"] = key
    child_env["REFINER_LLM_MODEL_NAME"] = args.model
    child_env["REFINER_LLM_ENDPOINT_URL"] = args.endpoint
    child_env["REFINER_LLM_WIRE_API"] = args.wire_api
    child_env["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort

    research_state = case_dir / "research_state.json"
    knowledge_base = case_dir / "knowledge_base"
    ledger = case_dir / "plan_versions.jsonl"
    research_command = [
        str(python),
        "reaserch_agent/run_research_agent.py",
        "--event-type", "bootstrap",
        "--query", case["query"],
        "--campaign-id", local_id,
        "--ledger-path", str(ledger),
        "--knowledge-base-dir", str(knowledge_base),
        "--include-device-context",
        "--device-workstations-dir", str(args.workstations),
        "--online-literature",
        "--model-name", args.model,
        "--base-url", args.endpoint,
        "--wire-api", args.wire_api,
        "--reasoning-effort", args.reasoning_effort,
        "--llm-timeout-seconds", str(args.llm_timeout),
        "--save-state", str(research_state),
    ]
    if args.deep_literature:
        research_command.extend(["--web-search", "--download-pdfs"])
    research_code, research_seconds, research_timed_out = run_command(
        research_command,
        cwd=repo,
        env=child_env,
        log_path=case_dir / "research_cli.log",
        timeout=args.case_timeout,
        dry_run=args.dry_run,
    )

    state: dict = {}
    if research_state.exists():
        state = json.loads(research_state.read_text(encoding="utf-8"))
    state_event = state.get("event") if isinstance(state.get("event"), dict) else {}
    state_query = state_event.get("query", state.get("query", ""))
    query_exact_match = bool(state) and state_query == case["query"]
    macro_plan = macro_plan_from_state(state)
    device_code: int | None = None
    device_seconds = 0.0
    device_attempts: list[dict] = []
    device_state = case_dir / "device_state.json"
    device_package = case_dir / "device_package.json"
    if macro_plan and not args.dry_run:
        for attempt in (1, 2):
            attempt_state = case_dir / f"device_state_attempt_{attempt}.json"
            attempt_package = case_dir / f"device_package_attempt_{attempt}.json"
            attempt_log = case_dir / f"device_cli_attempt_{attempt}.log"
            device_command = [
                str(python),
                "device_agent/run_from_research_state.py",
                "--research-state", str(research_state),
                "--output", str(attempt_state),
                "--package-output", str(attempt_package),
                "--exp-id", local_id,
                "--model-name", args.model,
                "--base-url", args.endpoint,
                "--wire-api", args.wire_api,
                "--reasoning-effort", args.reasoning_effort,
                "--workstations-dir", str(args.workstations),
                "--full-workstations",
                "--timeout-seconds", str(args.device_timeout),
            ]
            code, elapsed, timed_out = run_command(
                device_command,
                cwd=repo,
                env=child_env,
                log_path=attempt_log,
                timeout=args.case_timeout,
                dry_run=False,
            )
            device_code = code
            device_seconds += elapsed
            valid, status = read_device_status(attempt_package)
            device_attempts.append(
                {
                    "attempt": attempt,
                    "return_code": code,
                    "elapsed_seconds": round(elapsed, 3),
                    "timed_out": timed_out,
                    "valid_terminal_package": valid,
                    "status": status,
                }
            )
            if valid and code == 0:
                shutil.copy2(attempt_state, device_state)
                shutil.copy2(attempt_package, device_package)
                shutil.copy2(attempt_log, case_dir / "device_cli.log")
                break
    elif args.dry_run:
        (case_dir / "device_cli.log").write_text("DRY RUN: device command depends on a non-empty Macro Plan.\n", encoding="utf-8")

    _, device_status = read_device_status(device_package)
    summary = {
        "case_id": case_id,
        "local_run_id": local_id,
        "query_sha256": case["query_sha256"],
        "query_exact_match": query_exact_match if not args.dry_run else True,
        "research_return_code": research_code,
        "research_elapsed_seconds": round(research_seconds, 3),
        "research_timed_out": research_timed_out,
        "research_status": state.get("status", "not_produced" if not args.dry_run else "dry_run"),
        "paper_hits": len(state.get("knowledge_hits", [])) if isinstance(state.get("knowledge_hits"), list) else 0,
        "macro_steps": len(macro_plan),
        "device_return_code": device_code,
        "device_elapsed_seconds": round(device_seconds, 3),
        "device_attempts": device_attempts,
        "device_status": device_status,
        "online_literature_required": True,
        "web_search_enabled": args.deep_literature,
        "download_pdfs_enabled": args.deep_literature,
    }
    (case_dir / "case_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fixed eight-case Chem Agent online evaluation suite.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--docx", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--endpoint", default="https://anyrouter.top/v1")
    parser.add_argument("--wire-api", choices=["chat", "codex_responses"], default="codex_responses")
    parser.add_argument("--reasoning-effort", default="xhigh")
    parser.add_argument("--api-key-env", help="Environment variable containing one API key or a key list. The value is never logged.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--llm-timeout", type=int, default=7200)
    parser.add_argument("--device-timeout", type=int, default=7200)
    parser.add_argument("--case-timeout", type=int, default=14400)
    parser.add_argument("--deep-literature", action="store_true", help="Also enable open-Web search and open-access PDF download.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    args.workstations = repo / "chem_resources/lab-design-main/skills/chemistry-experiment-workstation"
    if not args.workstations.exists():
        raise FileNotFoundError(args.workstations)
    docx = args.docx.expanduser().resolve()
    cases = extract_cases(docx)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = (args.output_root.expanduser().resolve() if args.output_root else repo / "result") / f"chem-agent-eval-{timestamp}"
    run_root.mkdir(parents=True, exist_ok=False)

    dotenv = load_dotenv(repo / ".env")
    base_env = dict(dotenv)
    base_env.update(os.environ)
    keys = discover_keys(base_env, args.api_key_env)
    if not keys and not args.dry_run:
        requested = f"{args.api_key_env}, " if args.api_key_env else ""
        raise RuntimeError(
            "No API key found in "
            f"{requested}CHEM_AGENT_EVAL_API_KEYS, REFINER_LLM_POOL_<N>_API_KEY, or REFINER_LLM_API_KEY"
        )
    if not keys:
        keys = ["dry-run-placeholder"]

    python = repo / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable)

    workstation_count = sum(
        1
        for module in (
            "references-Synthesis-Module",
            "references-Reaction-and-Testing-Module",
            "references-Characterization-Module",
        )
        for path in (args.workstations / module).iterdir()
        if path.is_dir()
    )
    manifest = {
        "created_at": datetime.now().isoformat(),
        "repo": str(repo),
        "docx": str(docx),
        "model": args.model,
        "endpoint": args.endpoint,
        "wire_api": args.wire_api,
        "reasoning_effort": args.reasoning_effort,
        "online_literature": True,
        "web_search": args.deep_literature,
        "download_pdfs": args.deep_literature,
        "real_device_dispatch": False,
        "workstations_source": str(args.workstations),
        "workstation_directory_count": workstation_count,
        "case_ids": [case["case_id"] for case in cases],
        "local_run_ids": {case["case_id"]: f"{case['case_id']}-{timestamp}" for case in cases},
        "api_key_count": len(keys),
        "api_key_env": args.api_key_env or "auto-discovery",
        "api_keys_recorded": False,
        "requested_workers": args.workers,
        "effective_workers": max(1, min(args.workers, 8)),
        "dry_run": args.dry_run,
    }
    (run_root / "suite_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summaries: list[dict] = []
    effective_workers = max(1, min(args.workers, 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
        future_to_case = {
            executor.submit(
                run_case,
                case,
                index=index,
                repo=repo,
                run_root=run_root,
                run_stamp=timestamp,
                python=python,
                key=keys[index % len(keys)],
                args=args,
                base_env=base_env,
            ): case
            for index, case in enumerate(cases)
        }
        for future in concurrent.futures.as_completed(future_to_case):
            case = future_to_case[future]
            try:
                summaries.append(future.result())
            except Exception as exc:
                failure = {
                    "case_id": case["case_id"],
                    "local_run_id": f"{case['case_id']}-{timestamp}",
                    "runner_error": f"{type(exc).__name__}: {exc}",
                }
                case_dir = run_root / case["case_id"]
                case_dir.mkdir(parents=True, exist_ok=True)
                (case_dir / "case_summary.json").write_text(
                    json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                summaries.append(failure)
    summaries.sort(key=lambda item: item["case_id"])
    (run_root / "raw_summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
