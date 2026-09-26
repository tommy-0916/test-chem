"""Translate a blocked V2 Device plan in an isolated, non-dispatching lane.

This is a diagnostic preview, not an acceptance override.  The frozen Research
authority and workstation snapshot are checked by the same offline Plan audit
used by plan_repair.  Plan/material findings stay visible, while translation
continues only to expose downstream workflow errors.  Validation may build an
in-memory dispatch-format preview, but no certificate or payload is saved.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _digest(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--research-authority", type=Path, required=True)
    parser.add_argument("--device-state", type=Path, required=True)
    parser.add_argument("--relationship-bindings", type=Path, required=True)
    parser.add_argument("--repair-summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-name", default="kimi-k3")
    parser.add_argument("--endpoint-url", default="https://api.kimi.com/coding/v1")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--chunk-size", type=int, default=1,
        help="Diagnostic-only plan steps per translation call; production V2 remains 1.",
    )
    parser.add_argument(
        "--max-chunks", type=int, default=0,
        help="Stop after this many plan chunks; 0 translates the full plan.",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.max_chunks < 0:
        raise ValueError("--max-chunks cannot be negative")
    if not 1 <= args.chunk_size <= 6:
        raise ValueError("--chunk-size must be between 1 and 6")
    source_paths = {
        "candidate": args.candidate.resolve(),
        "research_authority": args.research_authority.resolve(),
        "device_state": args.device_state.resolve(),
        "relationship_bindings": args.relationship_bindings.resolve(),
        "repair_summary": args.repair_summary.resolve(),
    }
    output_dir = args.output_dir.resolve()
    if any(output_dir == path or output_dir in path.parents for path in source_paths.values()):
        raise ValueError("output directory must not contain an input artifact")
    candidate = _load_object(source_paths["candidate"])
    research = _load_object(source_paths["research_authority"])
    bindings = _load_object(source_paths["relationship_bindings"])
    repair_summary = _load_object(source_paths["repair_summary"])
    if repair_summary.get("status") != "stopped":
        raise ValueError("diagnostic preview requires a saved stopped repair summary")
    blocker = repair_summary.get("material_blocker_summary")
    if not isinstance(blocker, dict) or not blocker.get("issue_count"):
        raise ValueError("repair summary has no material compile blocker to preserve")
    if candidate.get("feasibility_accepted") is True:
        raise ValueError("diagnostic preview expects an unaccepted candidate")
    if repair_summary.get("final_candidate_digest") != _digest(candidate):
        raise ValueError("repair summary does not belong to the supplied candidate")
    saved_device_state = repair_summary.get("device_state")
    if not isinstance(saved_device_state, str) or (
        Path(saved_device_state).resolve() != source_paths["device_state"]
    ):
        raise ValueError("repair summary does not belong to the supplied Device state")
    attribution = blocker.get("binding_authority_attribution")
    if not isinstance(attribution, dict) or any(
        attribution.get(key) != bindings.get(key)
        for key in (
            "research_authority_sha256",
            "candidate_sha256",
            "workstation_truth_sha256",
            "authoring_mode",
        )
    ):
        raise ValueError("repair summary and relationship authority do not match")
    package = research.get("research_action_package_v2")
    if not isinstance(package, dict) or (
        bindings.get("research_authority_sha256") != _digest(package)
    ):
        raise ValueError("relationship authority does not bind this Research package")

    from plan_repair import _offline_full_plan_context

    runtime: dict[str, Any] = {}
    context = _offline_full_plan_context(
        source_paths["device_state"], research, bindings,
        diagnostic_runtime=runtime,
    )
    _, _, workstation_truth_sha256, _, audit_context_sha256, finalize = context
    if bindings.get("workstation_truth_sha256") != workstation_truth_sha256:
        raise ValueError("relationship authority does not bind this workstation snapshot")
    agent = runtime["agent"]
    state = runtime["state"]
    normalized, pre_findings, post_findings = finalize(candidate)
    active_material_findings = [
        item for item in post_findings
        if isinstance(item, dict)
        and item.get("type") == "binding_ledger_unresolved"
        and item.get("blocker_class") in (blocker.get("blocker_classes") or [])
    ]
    if not active_material_findings:
        raise ValueError(
            "the current Plan audit no longer has the saved material blocker; "
            "use the normal acceptance path"
        )
    plan_steps = [
        step for step in normalized.get("device_plan", [])
        if isinstance(step, dict)
    ]
    if not plan_steps:
        raise ValueError("candidate contains no Device Plan steps")
    state.feasibility_accepted = False
    state.feasibility_certificate = {}
    state.accepted_device_plan_contract = {}
    state.workflow_json = {}
    state.workflow_txt = ""
    state.txt_format_reference = state.txt_format_reference or agent._txt_format_reference
    state.json_format_reference = state.json_format_reference or agent._json_format_reference

    source_digests = {name: _file_digest(path) for name, path in source_paths.items()}
    implementation_paths = {
        name: Path(__file__).with_name(name)
        for name in (
            "diagnostic_workflow_preview.py",
            "single_agent.py",
            "plan_repair.py",
        )
    }
    implementation_paths["llm_factory.py"] = Path(__file__).parent / "utils" / "llm_factory.py"
    implementation_digests = {
        name: _file_digest(path) for name, path in implementation_paths.items()
    }
    endpoint = urlsplit(args.endpoint_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "api.kimi.com"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("diagnostic Kimi endpoint must be clean HTTPS on api.kimi.com")
    model_descriptor = {
        "model_name": args.model_name,
        "endpoint": f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path}",
        "wire_api": "codex_responses",
        "reasoning_effort": args.reasoning_effort,
        "max_tokens": args.max_tokens,
        "diagnostic_chunk_size": args.chunk_size,
    }
    context_digest = _digest({
        "source_digests": source_digests,
        "normalized_candidate_sha256": _digest(normalized),
        "workstation_truth_sha256": workstation_truth_sha256,
        "audit_context_sha256": audit_context_sha256,
        "model": model_descriptor,
        "implementation_sha256": implementation_digests,
    })
    manifest = {
        "schema_version": 1,
        "status": "diagnostic_blocked",
        "diagnostic_only": True,
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "dispatchable": False,
        "dispatch_payload": {},
        "real_dispatch": False,
        "source_paths": {name: str(path) for name, path in source_paths.items()},
        "source_sha256": source_digests,
        "normalized_candidate_sha256": _digest(normalized),
        "workstation_truth_sha256": workstation_truth_sha256,
        "audit_context_sha256": audit_context_sha256,
        "diagnostic_context_sha256": context_digest,
        "device_plan_steps": len(plan_steps),
        "model": model_descriptor,
        "implementation_sha256": implementation_digests,
    }
    manifest_path = output_dir / "run_manifest.json"
    if manifest_path.exists():
        previous = _load_object(manifest_path)
        if previous.get("diagnostic_context_sha256") != context_digest:
            raise ValueError("output directory belongs to a different diagnostic context")
    _write_json(manifest_path, manifest)
    _write_json(output_dir / "plan_audit.json", {
        "status": "diagnostic_blocked",
        "pre_findings": pre_findings,
        "post_findings": post_findings,
        "material_blocker_summary": blocker,
        "feasibility_accepted": False,
    })
    issue = {
        "schema_version": 1,
        "issue_id": "material-compile-" + context_digest[:12],
        "title": "V2 material relationship compilation blocks Device acceptance",
        "status": "open",
        "location": "local_only",
        "uploaded": False,
        "blocker": blocker,
        "active_material_finding_count": len(active_material_findings),
        "repair_summary_status": repair_summary.get("status"),
        "repair_stop_reason": repair_summary.get("stop_reason"),
        "source_sha256": source_digests,
        "diagnostic_context_sha256": context_digest,
        "diagnostic_scope": "workflow preview only; no certificate or dispatch",
        "remaining_plan_findings": len(post_findings),
    }
    _write_json(output_dir / "local_issue_not_uploaded.json", issue)
    print(
        f"[diagnostic-preview] plan={len(plan_steps)} "
        f"material_issues={blocker.get('issue_count')} "
        f"plan_findings={len(post_findings)}; acceptance remains blocked",
        flush=True,
    )
    if args.preflight_only:
        return 0

    api_key = os.getenv("REFINER_LLM_API_KEY", "").strip()
    model_initialized = False
    chunks = [
        plan_steps[start : start + args.chunk_size]
        for start in range(0, len(plan_steps), args.chunk_size)
    ]
    total = len(chunks)
    assembled_steps: list[dict[str, Any]] = []
    text_fragments: list[str] = []
    carryover: dict[str, Any] = {}
    resumed = 0
    limit = min(total, args.max_chunks) if args.max_chunks else total
    for index in range(limit):
        chunk = chunks[index]
        chunk_path = output_dir / "translation_chunks" / f"chunk_{index + 1:03d}_of_{total:03d}.json"
        chunk_binding = _digest({
            "diagnostic_context_sha256": context_digest,
            "chunk_index": index,
            "total_chunks": total,
            "chunk": chunk,
            "carryover": carryover,
        })
        if chunk_path.exists():
            record = _load_object(chunk_path)
            if record.get("chunk_binding_sha256") != chunk_binding:
                raise ValueError(f"saved chunk {index + 1} does not match frozen inputs")
            if record.get("result_sha256") != _digest(record.get("result")):
                raise ValueError(f"saved chunk {index + 1} has changed since capture")
            resumed += 1
        else:
            if not model_initialized:
                if not api_key:
                    raise RuntimeError(
                        "REFINER_LLM_API_KEY is required for an uncached translation chunk"
                    )
                os.environ["CHEM_LLM_COMPONENT"] = "device"
                os.environ["REFINER_LLM_MODEL_PROVIDER"] = "openai"
                os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
                os.environ["REFINER_LLM_ENDPOINT_URL"] = args.endpoint_url
                os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
                os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
                os.environ["REFINER_LLM_MAX_RETRIES"] = "2"
                from utils.llm_factory import LLMFactory

                agent._model = LLMFactory.create(
                    provider="openai", model_name=args.model_name,
                    endpoint_url=args.endpoint_url, api_key=api_key,
                    timeout=args.timeout_seconds, max_tokens=args.max_tokens,
                )
                model_initialized = True
            print(f"[diagnostic-preview] translating chunk {index + 1}/{total}", flush=True)
            try:
                translated = agent._invoke_translation_chunk_bounded(
                    state, normalized, chunk, index, total, carryover,
                )
            except Exception as exc:
                _write_json(output_dir / "progress.json", {
                    "status": "translation_stopped",
                    "completed_chunks": index,
                    "total_chunks": total,
                    "error_type": type(exc).__name__,
                    "diagnostic_only": True,
                    "dispatchable": False,
                })
                raise
            workflow = translated.get("workflow_json")
            workflow = workflow if isinstance(workflow, dict) else {}
            steps = [
                step for step in (workflow.get("steps") or [])
                if isinstance(step, dict)
            ]
            agent._inherit_workflow_source_traces(steps, chunk)
            agent._stamp_device_steps_with_macro_action(
                {"steps": steps}, state.research_handoff,
            )
            record = {
                "schema_version": 1,
                "chunk_binding_sha256": chunk_binding,
                "chunk_index": index,
                "total_chunks": total,
                "result": {
                    "steps": steps,
                    "workflow_txt": str(translated.get("workflow_txt", "")),
                },
            }
            record["result_sha256"] = _digest(record["result"])
            _write_json(chunk_path, record)
        result = record.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("steps"), list):
            raise ValueError(f"saved chunk {index + 1} has an invalid result")
        expected_plan_steps = {str(step.get("plan_step")) for step in chunk}
        if any(
            not isinstance(step, dict)
            or str(step.get("source_plan_step")) not in expected_plan_steps
            for step in result["steps"]
        ):
            raise ValueError(
                f"chunk {index + 1} contains a step outside its source Plan step"
            )
        assembled_steps.extend(copy.deepcopy(result["steps"]))
        text_value = result.get("workflow_txt")
        if isinstance(text_value, str) and text_value.strip():
            text_fragments.append(text_value.strip())
        carryover = agent._lid_and_container_state_after(assembled_steps)
        _write_json(output_dir / "progress.json", {
            "status": "translating" if index + 1 < total else "translated",
            "completed_chunks": index + 1,
            "total_chunks": total,
            "resumed_chunks": resumed,
            "workflow_steps_so_far": len(assembled_steps),
            "diagnostic_only": True,
            "feasibility_accepted": False,
            "dispatchable": False,
            "real_dispatch": False,
        })

    if limit < total:
        return 0
    for number, step in enumerate(assembled_steps, start=1):
        step["step_number"] = number
    workflow_json = {
        "steps": assembled_steps,
        "temporal_adaptations": copy.deepcopy(normalized.get("temporal_adaptations", [])),
        "offline_handoffs": copy.deepcopy(normalized.get("offline_handoffs", [])),
    }
    workflow_txt = agent._workflow_txt_from_json(workflow_json)
    check_input = agent._merge_plan_and_translation(
        normalized, {"workflow_json": workflow_json, "workflow_txt": workflow_txt},
    )
    if "quantity_audit" not in check_input and "quantity_audit" in candidate:
        check_input["quantity_audit"] = copy.deepcopy(candidate["quantity_audit"])
    workflow_check = agent._run_full_checks(check_input)
    package = {
        "schema_version": 1,
        "status": "diagnostic_blocked",
        "diagnostic_only": True,
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "dispatchable": False,
        "dispatch_payload": {},
        "real_dispatch": False,
        "diagnostic_context_sha256": context_digest,
        "material_blocker_summary": blocker,
        "plan_finding_count": len(post_findings),
        "workflow_json": check_input.get("workflow_json", workflow_json),
        "workflow_txt": check_input.get("workflow_txt", workflow_txt),
        "workflow_check": workflow_check,
    }
    _write_json(output_dir / "diagnostic_package.json", package)
    summary = {
        "status": "diagnostic_blocked",
        "diagnostic_only": True,
        "device_plan_steps": len(plan_steps),
        "translation_chunks": total,
        "workflow_steps": len(assembled_steps),
        "material_compile_issue_count": blocker.get("issue_count"),
        "plan_finding_count": len(post_findings),
        "workflow_check_status": workflow_check.get("status"),
        "workflow_check_error_count": len(workflow_check.get("errors", [])),
        "feasibility_accepted": False,
        "dispatchable": False,
        "real_dispatch": False,
    }
    _write_json(output_dir / "summary.json", summary)
    print(
        f"[diagnostic-preview] workflow steps={len(assembled_steps)} "
        f"check={workflow_check.get('status')}; acceptance remains blocked",
        flush=True,
    )
    if workflow_check.get("status") != "passed":
        # A failed full check must enter the existing scoped workflow-repair
        # loop automatically.  The bridge reuses these frozen chunks in their
        # original order and writes to a fresh sibling, never to this preview.
        repair_dir = output_dir.parent / (
            f"{output_dir.name}-workflow-repair-{uuid4().hex}"
        )
        bridge = Path(__file__).with_name("diagnostic_workflow_repair.py")
        bridge_exit_code: int | None = None
        bridge_error_type = ""
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--diagnostic-dir",
                    str(output_dir),
                    "--output-dir",
                    str(repair_dir),
                    "--timeout-seconds",
                    str(args.timeout_seconds),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            bridge_exit_code = completed.returncode
        except OSError as exc:
            # Do not persist subprocess output or exception text: provider
            # errors can contain sensitive request details.
            bridge_error_type = type(exc).__name__
        report_path = repair_dir / "repair_report.json"
        repair_report: dict[str, Any] = {}
        if report_path.is_file():
            try:
                repair_report = _load_object(report_path)
            except (OSError, ValueError, json.JSONDecodeError):
                bridge_error_type = "InvalidRepairReport"
        safe_report = (
            repair_report.get("status") == "diagnostic_blocked"
            and repair_report.get("diagnostic_only") is True
            and repair_report.get("feasibility_accepted") is False
            and repair_report.get("feasibility_certificate") == {}
            and repair_report.get("dispatchable") is False
            and repair_report.get("dispatch_payload") == {}
            and repair_report.get("real_dispatch") is False
        )
        linked = bridge_exit_code == 0 and safe_report
        linkage = {
            "schema_version": 1,
            "status": "diagnostic_blocked",
            "diagnostic_only": True,
            "handoff_status": "completed" if linked else "failed",
            "source_preview_manifest_sha256": _file_digest(manifest_path),
            "source_preview_package_sha256": _file_digest(
                output_dir / "diagnostic_package.json"
            ),
            "repair_output_dir": str(repair_dir),
            "repair_report_sha256": _file_digest(report_path) if safe_report else "",
            "bridge_exit_code": bridge_exit_code,
            "bridge_error_type": bridge_error_type,
            "workflow_subcheck_status": (
                repair_report.get("workflow_subcheck_status") if safe_report else ""
            ),
            "diagnostic_workflow_modifications": (
                repair_report.get("diagnostic_workflow_modifications")
                if safe_report else None
            ),
            "feasibility_accepted": False,
            "feasibility_certificate": {},
            "dispatchable": False,
            "dispatch_payload": {},
            "real_dispatch": False,
        }
        _write_json(output_dir / "repair_linkage.json", linkage)
        summary["workflow_repair_handoff"] = linkage["handoff_status"]
        summary["workflow_repair_output_dir"] = str(repair_dir)
        summary["workflow_repair_subcheck_status"] = linkage["workflow_subcheck_status"]
        _write_json(output_dir / "summary.json", summary)
        print(
            "[diagnostic-preview] workflow repair handoff="
            f"{linkage['handoff_status']}; acceptance remains blocked",
            flush=True,
        )
        if not linked:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
