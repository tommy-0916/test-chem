#!/usr/bin/env python3
"""Run selected Chem Agent cases in parallel and independently review outputs.

The script never dispatches to a real laboratory. Each campaign stops at the
manual awaiting-observation boundary, then its workflow is checked against the
45-workstation ``lab-design-all`` truth source by the deterministic auditor and
an independent, context-isolated LLM call.  When multiple credentials are
available the reviewer rotates to a different credential.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
EVAL_SCRIPTS = (
    REPO / "chem-agent-eval-package/skill/chem-agent-eval-sop/scripts"
)
if str(EVAL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EVAL_SCRIPTS))

from audit_workflows import audit_run, write_audit  # noqa: E402
from extract_cases import extract_cases  # noqa: E402
from llm_review_workflows import review_run, write_reviews  # noqa: E402
from preflight import probe_api  # noqa: E402
from run_suite import run_case  # noqa: E402
from device_agent.feasibility_certificate import (  # noqa: E402
    FEASIBILITY_CERTIFICATE_VERSION,
    FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS,
    device_plan_contract_digest,
    feasibility_certificate_id,
    feasibility_certificate_protected_payload,
    stable_digest as _stable_digest,
    strict_feasibility_certificate_version,
)


MODEL = "gpt-5.6-sol"
ENDPOINT = "https://api.aigateway.qzz.io"
WIRE_API = "codex_responses"
REASONING_EFFORT = "xhigh"
ALL_CASE_IDS = ("A01", "A02", "B01", "B02", "C01", "C02", "D01", "D02")
KEY_ENV_NAMES = (
    "CHEM_SELECTED_EVAL_API_KEY",
    "CHEM_PARALLEL_EVAL_KEY_1",
    "CHEM_PARALLEL_EVAL_KEY_2",
)
WORKSTATIONS = (
    REPO
    / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation"
)
BUNDLED_DEPENDENCIES = (
    Path.home()
    / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_cases(docx: Path) -> dict[str, dict[str, str]]:
    return {case["case_id"]: case for case in extract_cases(docx)}


def _certificate_integrity_errors(
    certificate: dict[str, Any],
    device_plan_package: Any,
    plan_level_repair: Any,
    *,
    expected_contract_version: str = "",
) -> list[str]:
    errors: list[str] = []
    package = (
        device_plan_package
        if isinstance(device_plan_package, dict)
        else {}
    )
    raw_package_contract = package.get("contract_version")
    package_contract = (
        raw_package_contract if isinstance(raw_package_contract, str) else ""
    )
    raw_certificate_contract = certificate.get("contract_version")
    certificate_contract = (
        raw_certificate_contract
        if isinstance(raw_certificate_contract, str)
        else ""
    )
    certificate_version = strict_feasibility_certificate_version(certificate)
    v2_required = (
        expected_contract_version == "v2"
        or package_contract == "v2"
        or certificate_contract == "v2"
    )
    if v2_required:
        if package_contract != "v2":
            errors.append("package_contract_version_mismatch")
        if (
            certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS
            and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
        ):
            errors.append("obsolete_full_plan_certificate_requires_reaudit")
        elif certificate_version != FEASIBILITY_CERTIFICATE_VERSION:
            errors.append("unsupported_certificate_version")
        if certificate_contract != "v2":
            errors.append("certificate_contract_version_mismatch")
        if certificate.get("acceptance_scope") not in {
            "accepted_device_plan",
            "accepted_device_plan_revision",
        }:
            errors.append("certificate_acceptance_scope_invalid_for_success")
        resolution = package.get("contract_resolution")
        resolution = resolution if isinstance(resolution, dict) else {}
        if (
            resolution.get("requested") != "v2"
            or resolution.get("effective") != "v2"
            or resolution.get("requested_matches_effective") is not True
        ):
            errors.append("package_contract_resolution_invalid")
    protected = feasibility_certificate_protected_payload(certificate)
    if certificate.get("protected_digest") != _stable_digest(protected):
        errors.append("protected_digest_mismatch")
    if certificate.get("route_signature") != certificate.get(
        "research_plan_signature"
    ):
        errors.append("route_signature_mismatch")
    if certificate.get("sample_matrix_signature") != _stable_digest(
        certificate.get("sample_control_matrix", []),
        prefix="sample_matrix",
    ):
        errors.append("sample_matrix_signature_mismatch")
    if certificate.get("device_snapshot_signature") != certificate.get(
        "device_truth_sha256"
    ):
        errors.append("device_snapshot_signature_mismatch")
    repair = plan_level_repair if isinstance(plan_level_repair, dict) else {}
    if certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS:
        if certificate.get(
            "accepted_device_plan_contract_sha256"
        ) != device_plan_contract_digest(device_plan_package):
            errors.append("accepted_device_plan_contract_mismatch")
    if certificate.get("accepted_device_plan_signature") != _stable_digest(
        package.get("device_plan", []),
        prefix="device_plan",
    ):
        errors.append("accepted_device_plan_signature_mismatch")
    if repair and repair.get("status") != "accepted":
        errors.append("plan_level_repair_not_accepted")
    if repair and repair.get("errors"):
        errors.append("accepted_plan_level_repair_contains_errors")
    if certificate.get("certificate_id") != feasibility_certificate_id(
        protected_digest=str(certificate.get("protected_digest") or ""),
        device_snapshot_id=str(certificate.get("device_snapshot_id") or ""),
        device_truth_sha256=str(certificate.get("device_truth_sha256") or ""),
    ):
        errors.append("certificate_id_mismatch")
    for required in (
        "research_plan_signature",
        "device_snapshot_id",
        "device_truth_sha256",
        "certificate_id",
    ):
        if not str(certificate.get(required) or "").strip():
            errors.append(f"missing_{required}")
    return list(dict.fromkeys(errors))


def _terminal_status_is_ready(payload: dict[str, Any]) -> bool:
    """Accept the two equivalent pre-dispatch terminal envelopes.

    Raw V1/legacy output uses ``success``/``none``.  Attaching the canonical V2
    wire contract deliberately rewrites that pair to
    ``ready_for_dispatch``/``success``.  Neither representation means that a
    laboratory dispatch occurred.
    """

    status = str(payload.get("status") or "").strip().lower()
    route = str(payload.get("feedback_route") or "").strip().lower()
    feedback_type = str(payload.get("feedback_type") or "").strip().lower()
    failure_scope = str(payload.get("failure_scope") or "").strip().lower()
    canonical_pair = (status == "ready_for_dispatch" and route == "success") or (
        status == "success" and route in {"", "none"}
    )
    return (
        canonical_pair
        and feedback_type in {"", "none"}
        and failure_scope in {"", "none"}
    )


def _device_acceptance(
    run_root: Path,
    *,
    expected_query_sha256: str = "",
    expected_contract_version: str = "",
) -> dict[str, Any]:
    """Read raw black-box packages and prove that Device accepted a workflow."""

    summary_records: list[dict[str, Any]] = []
    for path in sorted(run_root.rglob("case_summary.json")):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            summary_records.append(
                {
                    "path": str(path.resolve()),
                    "boundary_accepted": False,
                    "read_error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        marker_text = str(summary.get("awaiting_observation_marker") or "").strip()
        marker = Path(marker_text).expanduser().resolve() if marker_text else None
        run_root_resolved = run_root.expanduser().resolve()
        marker_in_run = bool(
            marker
            and marker.name == "AWAITING_OBSERVATION.md"
            and marker.is_relative_to(run_root_resolved)
        )
        input_path = path.parent / "input.json"
        sha_path = path.parent / "query.sha256"
        try:
            input_payload = json.loads(input_path.read_text(encoding="utf-8"))
            query = str(input_payload.get("query") or "")
            input_sha = hashlib.sha256(query.encode("utf-8")).hexdigest()
            recorded_sha = sha_path.read_text(encoding="utf-8").strip()
            query_chain_valid = bool(
                query
                and input_payload.get("query_sha256") == input_sha
                and summary.get("query_sha256") == input_sha
                and recorded_sha == input_sha
                and (
                    not expected_query_sha256
                    or input_sha == expected_query_sha256
                )
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            input_sha = ""
            recorded_sha = ""
            query_chain_valid = False
        boundary_accepted = (
            summary.get("query_exact_match") is True
            and query_chain_valid
            and summary.get("online_literature_requested") is True
            and summary.get("boundary_status") == "awaiting_observation"
            and marker is not None
            and marker_in_run
            and marker.is_file()
        )
        summary_records.append(
            {
                "path": str(path.resolve()),
                "query_exact_match": summary.get("query_exact_match"),
                "query_sha256": input_sha,
                "recorded_query_sha256": recorded_sha,
                "expected_query_sha256": expected_query_sha256,
                "query_chain_valid": query_chain_valid,
                "online_literature_requested": summary.get(
                    "online_literature_requested"
                ),
                "boundary_status": summary.get("boundary_status"),
                "awaiting_observation_marker": marker_text,
                "marker_exists": bool(marker and marker.is_file()),
                "marker_in_run": marker_in_run,
                "boundary_accepted": boundary_accepted,
            }
        )

    packages: list[dict[str, Any]] = []
    for path in sorted(run_root.rglob("device_package.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            packages.append(
                {
                    "path": str(path.resolve()),
                    "accepted": False,
                    "read_error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        terminal = payload.get("terminal_package")
        if isinstance(terminal, dict):
            payload = terminal
        dispatch = payload.get("dispatch_validation")
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        certificate = payload.get("feasibility_certificate")
        certificate = certificate if isinstance(certificate, dict) else {}
        workflow = payload.get("workflow_json")
        workflow = workflow if isinstance(workflow, dict) else {}
        steps = workflow.get("steps")
        steps = steps if isinstance(steps, list) else []
        skill_review = payload.get("workflow_skill_review")
        skill_review = skill_review if isinstance(skill_review, dict) else {}
        recipe = payload.get("recipe_materialization")
        recipe = recipe if isinstance(recipe, dict) else {}
        quantity = payload.get("quantity_audit")
        quantity = quantity if isinstance(quantity, dict) else {}
        formatting = payload.get("dispatch_formatting")
        formatting = formatting if isinstance(formatting, dict) else {}
        capability = payload.get("capability_audit")
        capability = capability if isinstance(capability, dict) else {}
        dispatch_payload = payload.get("dispatch_payload")
        quantity_contract_complete = (
            isinstance(payload.get("quantity_adjustments"), list)
            and isinstance(payload.get("batch_plan"), list)
            and bool(payload.get("batch_plan"))
            and isinstance(payload.get("material_ledger"), dict)
            and bool(payload.get("material_ledger"))
        )
        # New packages must carry the accepted certificate.  The legacy
        # fallback remains evidence-only for historical runs produced before
        # the certificate contract existed.
        certificate_errors = _certificate_integrity_errors(
            certificate,
            payload,
            payload.get("plan_level_repair"),
            expected_contract_version=expected_contract_version,
        )
        certificate_ok = (
            payload.get("feasibility_accepted") is True
            and certificate.get("accepted") is True
            and not certificate_errors
        )
        # ``attach_device_v2_contract`` canonicalizes an otherwise successful
        # V2 package to ready_for_dispatch/success.  Keep accepting the legacy
        # success/none pair for historical artifacts, but judge both views as
        # the same non-dispatched terminal state.
        terminal_ready = _terminal_status_is_ready(payload)
        scope_clear = str(payload.get("failure_scope") or "").strip().lower() in {
            "",
            "none",
        }
        skill_review_ok = (
            skill_review.get("status") == "passed"
            and skill_review.get("final_verdict") in {None, "", "executable"}
            and not skill_review.get("errors")
            and not skill_review.get("issues")
        )
        recipe_ok = recipe.get("status") == "passed" and not recipe.get("errors")
        formatting_ok = (
            formatting.get("unmapped_steps") == 0
            and formatting.get("mapped_steps") == len(steps)
            and str(formatting.get("status") or "").strip().lower()
            in {"", "passed", "success"}
            and not formatting.get("errors")
            and not formatting.get("warnings")
            and not formatting.get("dropped_parameters")
        )
        quantity_ok = (
            quantity_contract_complete
            and quantity.get("status") == "passed"
            and not quantity.get("errors")
        )
        accepted = (
            terminal_ready
            and scope_clear
            and dispatch.get("status") == "passed"
            and not dispatch.get("errors")
            and certificate_ok
            and bool(steps)
            and skill_review_ok
            and recipe_ok
            and quantity_ok
            and formatting_ok
            and capability.get("status") in {None, "", "clean", "passed"}
            and not capability.get("errors")
            and not capability.get("findings")
            and payload.get("requires_scientific_review") is not True
            and isinstance(dispatch_payload, dict)
            and bool(dispatch_payload)
        )
        packages.append(
            {
                "path": str(path.resolve()),
                "status": payload.get("status"),
                "dispatch_status": dispatch.get("status"),
                "feasibility_accepted": certificate_ok,
                "certificate_integrity_errors": certificate_errors,
                "workflow_step_count": len(steps),
                "workflow_skill_review_status": skill_review.get("status"),
                "recipe_materialization_status": recipe.get("status"),
                "quantity_audit_status": quantity.get("status"),
                "dispatch_mapped_steps": formatting.get("mapped_steps"),
                "dispatch_unmapped_steps": formatting.get("unmapped_steps"),
                "requires_scientific_review": payload.get(
                    "requires_scientific_review"
                ),
                "accepted": accepted,
                "iteration": max(
                    (
                        int(part.removeprefix("iteration_"))
                        for part in path.parts
                        if part.startswith("iteration_")
                        and part.removeprefix("iteration_").isdigit()
                    ),
                    default=-1,
                ),
            }
        )
    boundary_accepted = any(
        item.get("boundary_accepted") is True for item in summary_records
    )
    latest_package = max(packages, key=lambda item: item.get("iteration", -1), default={})
    package_accepted = latest_package.get("accepted") is True
    return {
        "accepted": boundary_accepted and package_accepted,
        "boundary_accepted": boundary_accepted,
        "package_accepted": package_accepted,
        "latest_package": latest_package,
        "case_summaries": summary_records,
        "packages": packages,
    }


def _probe(index: int, key: str) -> dict[str, Any]:
    errors: list[str] = []
    for probe_attempt in range(1, 4):
        try:
            result = probe_api(
                endpoint=ENDPOINT,
                api_key=key,
                model=MODEL,
                wire_api=WIRE_API,
                reasoning_effort=REASONING_EFFORT,
                timeout=180,
            )
            return {
                "attempt": index + 1,
                "ok": True,
                "probe_attempts_used": probe_attempt,
                "prior_errors": errors,
                **result,
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if probe_attempt < 3:
                time.sleep(2 * probe_attempt)
    return {
        "attempt": index + 1,
        "ok": False,
        "probe_attempts_used": 3,
        "errors": errors,
    }


def _run_attempt(
    index: int,
    *,
    case: dict[str, str],
    key: str,
    parallel_root: Path,
    timeout: int,
) -> tuple[Path, dict[str, Any]]:
    case_id = case["case_id"]
    run_root = parallel_root / f"task_{case_id}"
    run_root.mkdir(parents=True, exist_ok=False)
    _write_json(
        run_root / "suite_manifest.json",
        {
            "created_at": datetime.now().isoformat(),
            "case_ids": [case_id],
            "parallel_task_index": index + 1,
            "exact_query_sha256": case["query_sha256"],
            "blackbox_entrypoint": str((REPO / "run_campaign.py").resolve()),
            "campaign_model": MODEL,
            "campaign_endpoint": ENDPOINT,
            "campaign_wire_api": WIRE_API,
            "responses_transport": "codex_cli_responses_stateless",
            "campaign_default_max_iterations": 12,
            "research_device_context_default": True,
            "online_literature_default": True,
            "open_web_search_default": True,
            "device_full_workstations_default": True,
            "real_device_dispatch": False,
            "stop_at_awaiting_observation": True,
            "workstations_source": str(WORKSTATIONS.resolve()),
            "api_keys_recorded": False,
        },
    )
    args = SimpleNamespace(
        case_timeout=timeout,
        poll_seconds=1.0,
        keep_waiting_for_observation=False,
        dry_run=False,
    )
    base_env = dict(os.environ)
    # The configured gateway accepts the Responses wire protocol used by the
    # Codex CLI, but blocks the SDK-shaped request for chemistry prompts.  Use
    # the stateless/isolated CLI adapter directly so every call remains on the
    # requested Responses API without first incurring a rejected SDK request.
    base_env["REFINER_RESPONSES_TRANSPORT"] = "cli"
    base_env["REFINER_RESPONSES_CLI_FALLBACK"] = "0"
    bundled_node = BUNDLED_DEPENDENCIES / "node/bin/node"
    bundled_modules = BUNDLED_DEPENDENCIES / "node/node_modules"
    if bundled_node.is_file() and bundled_modules.is_dir():
        base_env.setdefault("CHEM_SPREADSHEET_NODE", str(bundled_node))
        base_env.setdefault("CHEM_SPREADSHEET_NODE_MODULES", str(bundled_modules))
    summary = run_case(
        case,
        repo=REPO,
        run_root=run_root,
        run_stamp=f"parallel-{parallel_root.name}-{case_id}",
        python=REPO / ".venv/bin/python",
        args=args,
        base_env=base_env,
        api_key=key,
        campaign_model=MODEL,
        campaign_endpoint=ENDPOINT,
        campaign_wire_api=WIRE_API,
        campaign_reasoning_effort=REASONING_EFFORT,
        campaign_llm_timeout_seconds=3600,
        campaign_max_iterations=12,
    )
    return run_root, summary


def _audit_and_review(
    index: int,
    *,
    run_root: Path,
    case_id: str,
    review_key: str,
) -> dict[str, Any]:
    deterministic = audit_run(
        run_dir=run_root,
        repo=REPO,
        workstations=WORKSTATIONS,
        case_ids=[case_id],
        expected_workstation_count=45,
    )
    deterministic_path = run_root / "evaluation/workstation_schema_audit.json"
    write_audit(deterministic, deterministic_path)
    llm_review, combined = review_run(
        run_dir=run_root,
        deterministic_audit=deterministic,
        keys=[review_key],
        endpoint=ENDPOINT,
        model=MODEL,
        wire_api=WIRE_API,
        reasoning_effort=REASONING_EFFORT,
        timeout=7200,
        max_output_tokens=16000,
        attempts=2,
        workers=1,
        repo=REPO,
        workstations=WORKSTATIONS,
    )
    write_reviews(
        review_result=llm_review,
        combined_result=combined,
        review_output=run_root / "evaluation/workstation_schema_llm_review.json",
        combined_output=run_root / "evaluation/workstation_schema_verdict.json",
    )
    verdict = combined["cases"][0]
    return {
        "task_index": index + 1,
        "case_id": case_id,
        "run_root": str(run_root.resolve()),
        "deterministic": deterministic["cases"][0],
        "llm_review": llm_review["cases"][0],
        "combined_verdict": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", type=Path, default=REPO / "测试题目.docx")
    parser.add_argument("--output-root", type=Path, default=REPO / "report")
    parser.add_argument("--case-timeout", type=int, default=21600)
    parser.add_argument(
        "--case-ids",
        nargs="+",
        choices=ALL_CASE_IDS,
        default=["A01", "B01"],
        help="Run selected exact DOCX cases; default: A01 B01.",
    )
    args = parser.parse_args()

    keys = [
        value
        for name in KEY_ENV_NAMES
        if (value := os.environ.get(name, "").strip())
    ]
    if not keys:
        raise SystemExit(
            "missing secret environment variable: CHEM_SELECTED_EVAL_API_KEY "
            "(or CHEM_PARALLEL_EVAL_KEY_1)"
        )

    case_by_id = _load_cases(args.docx.expanduser().resolve())
    selected_ids = list(dict.fromkeys(args.case_ids))
    indexed_cases = [
        (index, case_by_id[case_id])
        for index, case_id in enumerate(selected_ids)
    ]
    cases = [case for _, case in indexed_cases]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parallel_root = (
        args.output_root.expanduser().resolve()
        / f"chem-agent-{'-'.join(selected_ids).lower()}-parallel-{stamp}"
    )
    parallel_root.mkdir(parents=True, exist_ok=False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        probes = list(executor.map(lambda item: _probe(*item), enumerate(keys)))
    _write_json(parallel_root / "provider_preflight.json", {"probes": probes})
    if not all(item["ok"] for item in probes):
        print(
            json.dumps(
                {"output_root": str(parallel_root), "probes": probes},
                ensure_ascii=False,
            )
        )
        return 3

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(indexed_cases)) as executor:
        futures = [
            executor.submit(
                _run_attempt,
                index,
                case=case,
                key=keys[index % len(keys)],
                parallel_root=parallel_root,
                timeout=args.case_timeout,
            )
            for index, case in indexed_cases
        ]
        campaign_results = [future.result() for future in futures]

    # The review is a fresh, isolated model invocation.  When two credentials
    # are available, rotate away from the campaign credential; one credential
    # is still valid because no campaign context or output repair is shared.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(indexed_cases)) as executor:
        futures = [
            executor.submit(
                _audit_and_review,
                index,
                run_root=campaign_result[0],
                case_id=case["case_id"],
                review_key=keys[(index + 1) % len(keys)],
            )
            for (index, case), campaign_result in zip(
                indexed_cases, campaign_results
            )
        ]
        evaluated = [future.result() for future in futures]

    result = {
        "created_at": datetime.now().isoformat(),
        "output_root": str(parallel_root.resolve()),
        "case_ids": [case["case_id"] for case in cases],
        "query_sha256": {
            case["case_id"]: case["query_sha256"] for case in cases
        },
        "real_device_dispatch": False,
        "campaigns": [item[1] for item in campaign_results],
        "evaluations": evaluated,
        "device_acceptance": {
            case["case_id"]: _device_acceptance(
                campaign_result[0],
                expected_query_sha256=case["query_sha256"],
                expected_contract_version="v2",
            )
            for (_, case), campaign_result in zip(indexed_cases, campaign_results)
        },
    }
    schema_passed = all(
        item["combined_verdict"].get("dispatch_schema_match") == "yes"
        and item["combined_verdict"].get("evaluation_complete") is True
        for item in evaluated
    )
    device_accepted = all(
        result["device_acceptance"][case_id]["accepted"] is True
        for case_id in selected_ids
    )
    passed = schema_passed and device_accepted
    result["selected_tasks_device_accepted"] = device_accepted
    result["selected_tasks_schema_match"] = schema_passed
    result["selected_tasks_dispatchable"] = passed
    _write_json(parallel_root / "parallel_selected_cases_summary.json", result)
    print(
        json.dumps(
            {
                "output_root": str(parallel_root.resolve()),
                "selected_tasks_device_accepted": device_accepted,
                "selected_tasks_schema_match": schema_passed,
                "selected_tasks_dispatchable": passed,
                "verdicts": [
                    item["combined_verdict"].get("dispatch_schema_match")
                    for item in evaluated
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
