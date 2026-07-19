#!/usr/bin/env python3
"""Run the fixed eight-case evaluation through the Chem Agent public entrypoint.

The evaluation harness treats Chem Agent as a black box.  It supplies only the
exact Query plus the required online-literature flag.  Campaign identifiers and
output roots are instrumentation fields used to isolate and collect artifacts;
the harness never calls Research Agent or Device Agent directly.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from audit_workflows import audit_run, write_audit
from extract_cases import extract_cases
from llm_review_workflows import review_run, write_reviews


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
    direct = parse_key_list(env.get("CHEM_AGENT_SCHEMA_REVIEW_API_KEYS", ""))
    if direct:
        return direct
    direct = parse_key_list(env.get("CHEM_AGENT_EVAL_API_KEYS", ""))
    if direct:
        return direct
    pooled = [
        env[f"REFINER_LLM_POOL_{index}_API_KEY"].strip()
        for index in range(1, 100)
        if env.get(f"REFINER_LLM_POOL_{index}_API_KEY", "").strip()
    ]
    if pooled:
        return pooled
    fallback = env.get("REFINER_LLM_API_KEY", "").strip()
    return [fallback] if fallback else []


def command_text(command: list[str]) -> str:
    return " ".join(command)


def terminate_process(process: subprocess.Popen[Any], grace_seconds: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=grace_seconds)


def run_blackbox(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    campaign_dir: Path,
    timeout: int,
    poll_seconds: float,
    stop_at_awaiting_observation: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Run one whole Chem Agent process and observe only public artifacts."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        log_path.write_text("DRY RUN\n" + command_text(command) + "\n", encoding="utf-8")
        return {
            "return_code": 0,
            "elapsed_seconds": 0.0,
            "timed_out": False,
            "boundary_status": "dry_run",
            "awaiting_observation_marker": "",
        }

    started = time.monotonic()
    marker_path = ""
    boundary_status = "running"
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        while True:
            return_code = process.poll()
            if return_code is not None:
                boundary_status = "exited"
                break

            if stop_at_awaiting_observation and campaign_dir.exists():
                marker = next(campaign_dir.rglob("AWAITING_OBSERVATION.md"), None)
                if marker is not None:
                    marker_path = str(marker.resolve())
                    boundary_status = "awaiting_observation"
                    terminate_process(process)
                    return_code = process.returncode
                    break

            if time.monotonic() - started >= timeout:
                timed_out = True
                boundary_status = "timeout"
                terminate_process(process)
                return_code = process.returncode
                break
            time.sleep(max(0.1, poll_seconds))

    return {
        "return_code": return_code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
        "boundary_status": boundary_status,
        "awaiting_observation_marker": marker_path,
    }


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def relative_paths(paths: list[Path], root: Path) -> list[str]:
    result: list[str] = []
    for path in paths:
        try:
            result.append(str(path.resolve().relative_to(root.resolve())))
        except ValueError:
            result.append(str(path.resolve()))
    return result


def query_from_state(path: Path) -> str:
    state = read_json(path)
    event = state.get("event") if isinstance(state.get("event"), dict) else {}
    return str(event.get("query") or state.get("query") or "")


def online_literature_confirmed(paths: list[Path]) -> bool:
    signals = (
        "literature acquisition completed",
        "online literature",
        "paper_search",
        "seed resolution",
    )
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(signal in text for signal in signals):
            return True
    return False


def run_case(
    case: dict[str, str],
    *,
    repo: Path,
    run_root: Path,
    run_stamp: str,
    python: Path,
    args: argparse.Namespace,
    base_env: dict[str, str],
) -> dict[str, Any]:
    case_id = case["case_id"]
    local_id = f"{case_id}-{run_stamp}"
    case_dir = run_root / case_id
    blackbox_root = case_dir / "blackbox"
    campaign_dir = blackbox_root / local_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "input.json").write_text(
        json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (case_dir / "query.sha256").write_text(case["query_sha256"] + "\n", encoding="utf-8")

    command = [
        str(python),
        str(repo / "run_campaign.py"),
        "--query",
        case["query"],
        "--campaign-id",
        local_id,
        "--campaigns-root",
        str(blackbox_root),
        "--online-literature",
    ]
    result = run_blackbox(
        command,
        cwd=repo,
        env=dict(base_env),
        log_path=case_dir / "chem_agent.log",
        campaign_dir=campaign_dir,
        timeout=args.case_timeout,
        poll_seconds=args.poll_seconds,
        stop_at_awaiting_observation=not args.keep_waiting_for_observation,
        dry_run=args.dry_run,
    )

    summary_path = campaign_dir / "campaign_summary.json"
    campaign_summary = read_json(summary_path)
    research_states = sorted(campaign_dir.rglob("research_state.json")) if campaign_dir.exists() else []
    device_packages = sorted(campaign_dir.rglob("device_package.json")) if campaign_dir.exists() else []
    research_logs = sorted(campaign_dir.rglob("research_step.log")) if campaign_dir.exists() else []
    query_values = [query_from_state(path) for path in research_states]
    query_values = [value for value in query_values if value]
    query_exact_match = bool(query_values) and all(value == case["query"] for value in query_values)
    if not query_values and campaign_summary:
        query_exact_match = str(campaign_summary.get("query", "")) == case["query"]

    boundary_status = str(result["boundary_status"])
    if campaign_summary.get("stop_reason"):
        boundary_status = str(campaign_summary["stop_reason"])

    artifact_paths = sorted(path for path in campaign_dir.rglob("*") if path.is_file()) if campaign_dir.exists() else []
    case_summary = {
        "case_id": case_id,
        "local_run_id": local_id,
        "query_sha256": case["query_sha256"],
        "query_exact_match": query_exact_match if not args.dry_run else True,
        "blackbox_entrypoint": str((repo / "run_campaign.py").resolve()),
        "blackbox_input_contract": {
            "query": "exact extracted Query",
            "online_literature": True,
        },
        "harness_instrumentation": {
            "campaign_id": local_id,
            "campaigns_root": str(blackbox_root.resolve()),
        },
        "return_code": result["return_code"],
        "elapsed_seconds": result["elapsed_seconds"],
        "timed_out": result["timed_out"],
        "boundary_status": boundary_status,
        "awaiting_observation_marker": result["awaiting_observation_marker"],
        "campaign_dir": str(campaign_dir.resolve()),
        "campaign_summary_path": str(summary_path.resolve()) if summary_path.exists() else "",
        "stop_reason": str(campaign_summary.get("stop_reason", "")),
        "goal_reached": campaign_summary.get("goal_reached"),
        "online_literature_requested": True,
        "online_literature_log_confirmed": online_literature_confirmed(research_logs),
        "research_states": relative_paths(research_states, case_dir),
        "workflow_packages": relative_paths(device_packages, case_dir),
        "artifact_inventory": relative_paths(artifact_paths, case_dir),
    }
    (case_dir / "case_summary.json").write_text(
        json.dumps(case_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return case_summary


def workstation_root(repo: Path) -> Path:
    for candidate in (
        repo / "chem_resources/lab-design-main/skills/chemistry-experiment-workstation",
        repo / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation",
    ):
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("No workstation Skill root found for output auditing")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed eight-case suite through Chem Agent as a black box."
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--docx", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--case-timeout", type=int, default=14400)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--keep-waiting-for-observation",
        action="store_true",
        help="Do not stop after Chem Agent exposes an awaiting-observation marker.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--schema-review-model")
    parser.add_argument("--schema-review-endpoint")
    parser.add_argument(
        "--schema-review-wire-api", choices=["chat", "codex_responses"]
    )
    parser.add_argument("--schema-review-api-key-env")
    parser.add_argument("--schema-review-reasoning-effort", default="xhigh")
    parser.add_argument("--schema-review-timeout", type=int, default=7200)
    parser.add_argument("--schema-review-max-output-tokens", type=int, default=16000)
    parser.add_argument("--schema-review-attempts", type=int, default=2)
    parser.add_argument("--schema-review-workers", type=int, default=4)
    parser.add_argument(
        "--skip-schema-llm-review",
        action="store_true",
        help="Debug-only; a formal evaluation with generated workflows is incomplete when skipped.",
    )
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    entrypoint = repo / "run_campaign.py"
    if not entrypoint.exists():
        raise FileNotFoundError(entrypoint)
    docx = args.docx.expanduser().resolve()
    cases = extract_cases(docx)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = (
        args.output_root.expanduser().resolve() if args.output_root else repo / "result"
    ) / f"chem-agent-eval-{timestamp}"
    run_root.mkdir(parents=True, exist_ok=False)

    dotenv = load_dotenv(repo / ".env")
    base_env = dict(dotenv)
    base_env.update(os.environ)
    python = repo / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable)
    workstations = workstation_root(repo)
    review_model = args.schema_review_model or base_env.get("REFINER_LLM_MODEL_NAME", "gpt-5.6-sol")
    review_endpoint = args.schema_review_endpoint or base_env.get(
        "REFINER_LLM_ENDPOINT_URL", "https://anyrouter.top/v1"
    )
    review_wire_api = args.schema_review_wire_api or base_env.get(
        "REFINER_LLM_WIRE_API", "codex_responses"
    )
    review_keys = discover_keys(base_env, args.schema_review_api_key_env)
    if not review_keys and not args.dry_run and not args.skip_schema_llm_review:
        raise RuntimeError("No API key found for the independent schema reviewer")
    if not review_keys:
        review_keys = ["no-call-required"]

    manifest = {
        "created_at": datetime.now().isoformat(),
        "repo": str(repo),
        "docx": str(docx),
        "blackbox_entrypoint": str(entrypoint.resolve()),
        "blackbox_input_contract": ["exact_query", "online_literature=true"],
        "forbidden_internal_orchestration": [
            "direct Research Agent invocation",
            "direct Device Agent invocation",
            "evaluation-generated observations",
            "internal-stage retries or repairs",
        ],
        "online_literature": True,
        "real_device_dispatch": False,
        "stop_at_awaiting_observation": not args.keep_waiting_for_observation,
        "workstations_source": str(workstations),
        "case_ids": [case["case_id"] for case in cases],
        "local_run_ids": {
            case["case_id"]: f"{case['case_id']}-{timestamp}" for case in cases
        },
        "requested_workers": args.workers,
        "effective_workers": max(1, min(args.workers, 8)),
        "schema_llm_review": not args.skip_schema_llm_review,
        "schema_review_model": review_model,
        "schema_review_endpoint": review_endpoint,
        "schema_review_wire_api": review_wire_api,
        "schema_review_reasoning_effort": args.schema_review_reasoning_effort,
        "schema_review_api_key_env": args.schema_review_api_key_env or "auto-discovery",
        "api_keys_recorded": False,
        "dry_run": args.dry_run,
    }
    (run_root / "suite_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summaries: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(args.workers, 8))
    ) as executor:
        future_to_case = {
            executor.submit(
                run_case,
                case,
                repo=repo,
                run_root=run_root,
                run_stamp=timestamp,
                python=python,
                args=args,
                base_env=base_env,
            ): case
            for case in cases
        }
        for future in concurrent.futures.as_completed(future_to_case):
            case = future_to_case[future]
            try:
                summaries.append(future.result())
            except Exception as exc:
                failure = {
                    "case_id": case["case_id"],
                    "local_run_id": f"{case['case_id']}-{timestamp}",
                    "boundary_status": "harness_error",
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
    (run_root / "raw_summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    schema_audit = audit_run(
        run_dir=run_root,
        repo=repo,
        workstations=workstations,
        case_ids=[case["case_id"] for case in cases],
    )
    write_audit(schema_audit, run_root / "evaluation/workstation_schema_audit.json")

    review_complete = True
    if not args.skip_schema_llm_review:
        llm_reviews, combined_verdicts = review_run(
            run_dir=run_root,
            deterministic_audit=schema_audit,
            keys=review_keys,
            endpoint=review_endpoint,
            model=review_model,
            wire_api=review_wire_api,
            reasoning_effort=args.schema_review_reasoning_effort,
            timeout=args.schema_review_timeout,
            max_output_tokens=args.schema_review_max_output_tokens,
            attempts=args.schema_review_attempts,
            workers=args.schema_review_workers,
            repo=repo,
            workstations=workstations,
        )
        write_reviews(
            review_result=llm_reviews,
            combined_result=combined_verdicts,
            review_output=run_root / "evaluation/workstation_schema_llm_review.json",
            combined_output=run_root / "evaluation/workstation_schema_verdict.json",
        )
        review_complete = bool(combined_verdicts.get("evaluation_complete"))

    print(run_root)
    if args.skip_schema_llm_review:
        return 0 if args.dry_run else 2
    return 0 if review_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
