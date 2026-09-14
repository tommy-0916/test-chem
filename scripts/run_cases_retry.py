#!/usr/bin/env python3
"""Re-attempt selected chem-agent-eval cases as fresh black-box runs.

The chem-agent-eval SOP forbids rerunning only failed internal stages, but a
repeated CASE is allowed when it is a whole new black-box attempt in a new
timestamped run directory. This driver reuses the skill's own run_case /
audit_run / review_run implementations unchanged; it only narrows the case
set and writes into a separate run root so the first attempt stays intact.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

SKILL_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".kimi/skills/chem-agent-eval/scripts"
)
sys.path.insert(0, str(SKILL_SCRIPTS))

from audit_workflows import audit_run, write_audit  # noqa: E402
from extract_cases import extract_cases  # noqa: E402
from llm_review_workflows import review_run, write_reviews  # noqa: E402
from run_suite import (  # noqa: E402
    discover_campaign_keys,
    discover_keys,
    load_dotenv,
    run_case,
    workstation_root,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--docx", type=Path, required=True)
    parser.add_argument("--cases", required=True, help="Comma-separated case ids.")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--case-timeout", type=int, default=14400)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--retry-of", type=Path, required=True)
    parser.add_argument("--schema-review-model")
    parser.add_argument("--schema-review-endpoint")
    parser.add_argument("--schema-review-wire-api", choices=["chat", "codex_responses"])
    parser.add_argument("--schema-review-api-key-env")
    parser.add_argument("--schema-review-reasoning-effort", default="high")
    parser.add_argument("--schema-review-timeout", type=int, default=7200)
    parser.add_argument("--schema-review-max-output-tokens", type=int, default=16000)
    parser.add_argument("--schema-review-attempts", type=int, default=2)
    parser.add_argument("--schema-review-workers", type=int, default=4)
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    docx = args.docx.expanduser().resolve()
    wanted = [item.strip() for item in args.cases.split(",") if item.strip()]
    cases = [case for case in extract_cases(docx) if case["case_id"] in wanted]
    if sorted(case["case_id"] for case in cases) != sorted(wanted):
        raise SystemExit(f"case id mismatch: wanted {wanted}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = (
        args.output_root.expanduser().resolve() if args.output_root else repo / "result"
    ) / f"chem-agent-eval-retry-{timestamp}"
    run_root.mkdir(parents=True, exist_ok=False)

    dotenv = load_dotenv(repo / ".env")
    base_env = dict(dotenv)
    base_env.update(os.environ)
    python = repo / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable)
    workstations = workstation_root(repo)
    review_model = args.schema_review_model or base_env.get("REFINER_LLM_MODEL_NAME", "k3")
    review_endpoint = args.schema_review_endpoint or base_env.get(
        "REFINER_LLM_ENDPOINT_URL", "http://127.0.0.1:18765/v1"
    )
    review_wire_api = args.schema_review_wire_api or base_env.get(
        "REFINER_LLM_WIRE_API", "chat"
    )
    review_keys = discover_keys(base_env, args.schema_review_api_key_env)
    campaign_keys = discover_campaign_keys(base_env)
    if not campaign_keys:
        raise RuntimeError("No API key found for the black-box Chem Agent campaigns")
    if not review_keys:
        raise RuntimeError("No API key found for the independent schema reviewer")

    run_args = SimpleNamespace(
        case_timeout=args.case_timeout,
        poll_seconds=args.poll_seconds,
        keep_waiting_for_observation=False,
        dry_run=False,
    )

    manifest = {
        "created_at": datetime.now().isoformat(),
        "repo": str(repo),
        "docx": str(docx),
        "blackbox_entrypoint": str((repo / "run_campaign.py").resolve()),
        "blackbox_input_contract": ["exact_query", "online_literature=true"],
        "retry_of_first_attempt": str(args.retry_of.expanduser().resolve()),
        "retry_scope": "whole-case fresh black-box attempts only; no internal-stage reruns",
        "online_literature": True,
        "real_device_dispatch": False,
        "stop_at_awaiting_observation": True,
        "workstations_source": str(workstations),
        "case_ids": [case["case_id"] for case in cases],
        "requested_workers": args.workers,
        "schema_review_model": review_model,
        "schema_review_endpoint": review_endpoint,
        "schema_review_wire_api": review_wire_api,
        "schema_review_reasoning_effort": args.schema_review_reasoning_effort,
        "api_keys_recorded": False,
        "campaign_api_key_count": len(campaign_keys),
    }
    (run_root / "suite_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summaries: list[dict] = []
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
                args=run_args,
                base_env=base_env,
                api_key=campaign_keys[index % len(campaign_keys)],
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

    print(run_root)
    return 0 if combined_verdicts.get("evaluation_complete") else 2


if __name__ == "__main__":
    raise SystemExit(main())
