#!/usr/bin/env python3
"""Validate that a Chem Agent eight-case evaluation is genuinely complete."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


CASE_IDS = ["A01", "A02", "B01", "B02", "C01", "C02", "D01", "D02"]
PROCESS_VALUES = {"yes", "partial", "no"}
PAPER_VALUES = {"high", "mixed", "low"}
MATCH_VALUES = {"yes", "no", "not_evaluable"}
LLM_FAILURE_PATTERNS = {
    "llm_call_failed": re.compile(r"\bllm (?:call|invocation).*failed\b", re.IGNORECASE),
    "llm_empty_response": re.compile(r"\bllm returned (?:none|empty)\b", re.IGNORECASE),
    "llm_fallback": re.compile(r"\b(?:llm[^\n]{0,120})?fallback used\b", re.IGNORECASE),
    "codex_response_failed": re.compile(r"\bcodex responses call failed\b", re.IGNORECASE),
    "model_pool_failed": re.compile(r"\ball model backends failed\b", re.IGNORECASE),
}
SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def by_case(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return {}
    return {
        str(case.get("case_id")): case
        for case in cases
        if isinstance(case, dict) and case.get("case_id")
    }


def workflow_count(summary: dict[str, Any]) -> int:
    workflows = summary.get("workflow_packages")
    return len(workflows) if isinstance(workflows, list) else 0


def scan_llm_failures(case_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    blackbox = case_dir / "blackbox"
    if not blackbox.exists():
        return findings
    for path in sorted(blackbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".log", ".md", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for code, pattern in LLM_FAILURE_PATTERNS.items():
            match = pattern.search(text)
            if match:
                findings.append(
                    {
                        "code": code,
                        "path": str(path.resolve()),
                        "evidence": text[max(0, match.start() - 80) : match.end() + 160].replace("\n", " "),
                    }
                )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate all mandatory artifacts and verdicts for one eight-case evaluation."
    )
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Optional historical report that the overall report must compare against.",
    )
    parser.add_argument(
        "--allow-recorded-llm-failures",
        action="store_true",
        help="Debug only. Formal real-LLM evaluations must not use this option.",
    )
    args = parser.parse_args()

    run = args.run.expanduser().resolve()
    evaluation = run / "evaluation"
    failures: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    def check(ok: bool, code: str, message: str, evidence: Any = None) -> None:
        item = {"ok": bool(ok), "code": code, "message": message}
        if evidence not in (None, "", [], {}):
            item["evidence"] = evidence
        checks.append(item)
        if not ok:
            failures.append(item)

    check(run.is_dir(), "run_exists", f"Run directory exists: {run}")
    if not run.is_dir():
        print(json.dumps({"complete": False, "failures": failures}, ensure_ascii=False, indent=2))
        return 1

    manifest_path = run / "suite_manifest.json"
    raw_path = run / "raw_summary.json"
    check(manifest_path.is_file(), "manifest_exists", "suite_manifest.json exists")
    check(raw_path.is_file(), "raw_summary_exists", "raw_summary.json exists")
    if not manifest_path.is_file() or not raw_path.is_file():
        result = {"complete": False, "checks": checks, "failures": failures}
        evaluation.mkdir(parents=True, exist_ok=True)
        (evaluation / "completion_check.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return 1

    manifest = read_json(manifest_path)
    raw = read_json(raw_path)
    check(isinstance(manifest, dict), "manifest_object", "suite_manifest.json is an object")
    check(isinstance(raw, list), "raw_summary_array", "raw_summary.json is an array")
    if not isinstance(manifest, dict) or not isinstance(raw, list):
        return 1

    manifest_text = manifest_path.read_text(encoding="utf-8", errors="replace")
    check(not SECRET_RE.search(manifest_text), "manifest_has_no_secret", "Manifest contains no API key")
    check(manifest.get("case_ids") == CASE_IDS, "manifest_case_ids", "Manifest contains exactly A01-D02")
    check(manifest.get("online_literature") is True, "online_required", "Online literature was required")
    check(manifest.get("real_device_dispatch") is False, "no_real_dispatch", "Real laboratory dispatch was disabled")
    check(manifest.get("dry_run") is not True, "not_dry_run", "Evaluation was not a dry run")
    check(bool(manifest.get("campaign_model")), "campaign_model", "Campaign model is recorded")
    check(bool(manifest.get("campaign_endpoint")), "campaign_endpoint", "Campaign endpoint is recorded")
    check(bool(manifest.get("campaign_wire_api")), "campaign_wire_api", "Campaign wire API is recorded")
    check(manifest.get("schema_llm_review") is True, "llm_review_enabled", "Independent LLM review was enabled")

    summaries = {
        str(item.get("case_id")): item
        for item in raw
        if isinstance(item, dict) and item.get("case_id")
    }
    check(sorted(summaries) == CASE_IDS, "summary_case_ids", "Raw summary contains exactly A01-D02")

    llm_failures: dict[str, list[dict[str, Any]]] = {}
    for case_id in CASE_IDS:
        case_dir = run / case_id
        summary = summaries.get(case_id, {})
        input_path = case_dir / "input.json"
        hash_path = case_dir / "query.sha256"
        case_summary_path = case_dir / "case_summary.json"
        check(input_path.is_file(), f"{case_id}_input", f"{case_id} input.json exists")
        check(hash_path.is_file(), f"{case_id}_hash", f"{case_id} query.sha256 exists")
        check(case_summary_path.is_file(), f"{case_id}_summary", f"{case_id} case_summary.json exists")
        if input_path.is_file() and hash_path.is_file():
            case_input = read_json(input_path)
            query = case_input.get("query", "") if isinstance(case_input, dict) else ""
            expected_hash = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
            recorded_hash = hash_path.read_text(encoding="utf-8").strip()
            check(
                recorded_hash == expected_hash == summary.get("query_sha256"),
                f"{case_id}_query_integrity",
                f"{case_id} Query hash is unchanged",
            )
        check(summary.get("query_exact_match") is True, f"{case_id}_query_exact", f"{case_id} Query remained exact")
        check(
            summary.get("online_literature_requested") is True,
            f"{case_id}_online_requested",
            f"{case_id} requested online literature",
        )
        check(
            summary.get("boundary_status") not in {None, "", "running", "dry_run", "harness_error"},
            f"{case_id}_boundary",
            f"{case_id} reached a readable external boundary",
            summary.get("boundary_status"),
        )
        check(
            bool(summary.get("research_states")),
            f"{case_id}_llm_artifact",
            f"{case_id} produced an LLM-dependent research state",
        )
        found_failures = scan_llm_failures(case_dir)
        if found_failures:
            llm_failures[case_id] = found_failures
        check(
            args.allow_recorded_llm_failures or not found_failures,
            f"{case_id}_llm_returns",
            f"{case_id} has no recorded failed, empty, or fallback LLM call",
            found_failures[:10],
        )

    audit_path = evaluation / "workstation_schema_audit.json"
    review_path = evaluation / "workstation_schema_llm_review.json"
    verdict_path = evaluation / "workstation_schema_verdict.json"
    for code, path in (
        ("audit_exists", audit_path),
        ("review_exists", review_path),
        ("verdict_exists", verdict_path),
    ):
        check(path.is_file(), code, f"{path.name} exists")

    audit_cases: dict[str, dict[str, Any]] = {}
    review_cases: dict[str, dict[str, Any]] = {}
    verdict_cases: dict[str, dict[str, Any]] = {}
    verdict_payload: dict[str, Any] = {}
    if audit_path.is_file():
        audit_cases = by_case(read_json(audit_path))
    if review_path.is_file():
        review_cases = by_case(read_json(review_path))
    if verdict_path.is_file():
        verdict_payload = read_json(verdict_path)
        verdict_cases = by_case(verdict_payload)
    check(sorted(audit_cases) == CASE_IDS, "audit_case_ids", "Deterministic audit covers A01-D02")
    check(sorted(review_cases) == CASE_IDS, "review_case_ids", "LLM review covers A01-D02")
    check(sorted(verdict_cases) == CASE_IDS, "verdict_case_ids", "Combined verdict covers A01-D02")
    check(verdict_payload.get("evaluation_complete") is True, "combined_complete", "Combined schema review is complete")

    evaluations: dict[str, dict[str, Any]] = {}
    for case_id in CASE_IDS:
        expected_workflows = workflow_count(summaries.get(case_id, {}))
        audit_case = audit_cases.get(case_id, {})
        review_case = review_cases.get(case_id, {})
        verdict_case = verdict_cases.get(case_id, {})
        check(
            audit_case.get("checked_workflows") == expected_workflows,
            f"{case_id}_audit_coverage",
            f"{case_id} deterministic audit covers every workflow",
            {"expected": expected_workflows, "actual": audit_case.get("checked_workflows")},
        )
        check(
            review_case.get("checked_workflow_count") == expected_workflows,
            f"{case_id}_review_coverage",
            f"{case_id} LLM review covers every workflow",
            {"expected": expected_workflows, "actual": review_case.get("checked_workflow_count")},
        )
        expected_review_status = "completed" if expected_workflows else "not_evaluable"
        check(
            review_case.get("review_status") == expected_review_status,
            f"{case_id}_review_status",
            f"{case_id} LLM review has the expected terminal status",
            review_case.get("review_status"),
        )
        check(
            verdict_case.get("evaluation_complete") is True,
            f"{case_id}_combined_complete",
            f"{case_id} combined schema verdict is complete",
        )

        case_eval_json = evaluation / case_id / "evaluation.json"
        case_eval_md = evaluation / case_id / "evaluation.md"
        check(case_eval_json.is_file(), f"{case_id}_evaluation_json", f"{case_id} evaluation.json exists")
        check(case_eval_md.is_file(), f"{case_id}_evaluation_md", f"{case_id} evaluation.md exists")
        if not case_eval_json.is_file():
            continue
        case_eval = read_json(case_eval_json)
        evaluations[case_id] = case_eval
        check(case_eval.get("case_id") == case_id, f"{case_id}_evaluation_id", f"{case_id} report ID matches")
        check(case_eval.get("process_completion") in PROCESS_VALUES, f"{case_id}_process", f"{case_id} has process_completion")
        check(case_eval.get("paper_quality_summary") in PAPER_VALUES, f"{case_id}_papers", f"{case_id} has paper_quality_summary")
        check(case_eval.get("plan_workstation_match") in MATCH_VALUES, f"{case_id}_plan_match", f"{case_id} has plan_workstation_match")
        check(
            case_eval.get("dispatch_schema_match") == verdict_case.get("dispatch_schema_match"),
            f"{case_id}_dispatch_copy",
            f"{case_id} dispatch_schema_match copies the combined verdict",
        )

    matrix_path = evaluation / "verdict_matrix.json"
    overall_path = evaluation / "overall_report.md"
    direct_path = evaluation / "workstation_direct_acceptance.md"
    check(matrix_path.is_file(), "matrix_exists", "verdict_matrix.json exists")
    check(overall_path.is_file(), "overall_exists", "overall_report.md exists")
    check(direct_path.is_file(), "direct_acceptance_exists", "workstation_direct_acceptance.md exists")
    if matrix_path.is_file():
        matrix_cases = by_case(read_json(matrix_path))
        check(sorted(matrix_cases) == CASE_IDS, "matrix_case_ids", "Verdict matrix covers A01-D02")
        for case_id in CASE_IDS:
            matrix = matrix_cases.get(case_id, {})
            case_eval = evaluations.get(case_id, {})
            for field in (
                "process_completion",
                "paper_quality_summary",
                "plan_workstation_match",
                "dispatch_schema_match",
            ):
                check(
                    matrix.get(field) == case_eval.get(field),
                    f"{case_id}_matrix_{field}",
                    f"{case_id} matrix {field} matches evaluation.json",
                )
    if args.baseline:
        baseline = args.baseline.expanduser().resolve()
        check(baseline.is_file(), "baseline_exists", f"Baseline exists: {baseline}")
        if baseline.is_file() and overall_path.is_file():
            overall_text = overall_path.read_text(encoding="utf-8", errors="replace")
            check(
                baseline.name in overall_text,
                "baseline_compared",
                "Overall report names the historical baseline",
            )

    result = {
        "schema_version": "chem-agent-eval-completion-v1",
        "generated_at": datetime.now().isoformat(),
        "run_dir": str(run),
        "complete": not failures,
        "strict_real_llm": not args.allow_recorded_llm_failures,
        "llm_failure_count": sum(len(items) for items in llm_failures.values()),
        "checks": checks,
        "failures": failures,
    }
    evaluation.mkdir(parents=True, exist_ok=True)
    output = evaluation / "completion_check.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"complete": result["complete"], "failure_count": len(failures), "output": str(output)}, ensure_ascii=False))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
