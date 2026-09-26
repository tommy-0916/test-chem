"""Opt-in, non-dispatching workflow preview from a stopped V2 Device run.

Only a saved, reproducible material-relationship compile blocker can enter this
lane.  The adapter copies the actual frozen candidate and binding authority;
it does not revise Research facts or turn a failed Plan audit into acceptance.
The downstream preview owns translation and its automatic workflow-repair
handoff.  This module is intentionally not called by the production agent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("saved input is not a JSON object")
    return value


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _issue_multiset(issues: list[dict[str, Any]]) -> Counter[str]:
    return Counter(_digest(issue) for issue in issues)


def _blocked(reason: str, *, error_type: str = "") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "diagnostic_prep_blocked",
        "reason": reason,
        "error_type": error_type,
        "diagnostic_only": True,
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "dispatchable": False,
        "dispatch_payload": {},
        "real_dispatch": False,
    }


def run_from_blocked_files(
    *,
    research_state_path: Path,
    device_state_path: Path,
    package_path: Path,
    output_dir: Path,
    model_name: str,
    endpoint_url: str,
    reasoning_effort: str,
    timeout_seconds: int,
    max_tokens: int,
) -> dict[str, Any]:
    """Prepare and run the isolated preview from one exact stopped V2 run.

    A structured blocked result means the available files cannot prove the
    required boundary.  It must never be interpreted as a repaired Plan.
    Credentials are inherited from the process environment by the preview;
    neither this API nor its saved report accepts or stores an API key.
    """

    try:
        research_path = Path(research_state_path).resolve(strict=True)
        device_path = Path(device_state_path).resolve(strict=True)
        terminal_path = Path(package_path).resolve(strict=True)
        destination = Path(output_dir).resolve()
        sources = {
            "research_state": research_path,
            "device_state": device_path,
            "terminal_package": terminal_path,
        }
        if any(destination == path or destination in path.parents for path in sources.values()):
            return _blocked("output_contains_frozen_input")
        if destination.exists() and any(destination.iterdir()):
            return _blocked("output_directory_not_empty")
        if endpoint_url != "https://api.kimi.com/coding/v1":
            return _blocked("unapproved_diagnostic_endpoint")
        if not isinstance(model_name, str) or not model_name.strip():
            return _blocked("diagnostic_model_missing")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            return _blocked("diagnostic_reasoning_effort_missing")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            return _blocked("diagnostic_timeout_invalid")
        if type(max_tokens) is not int or max_tokens <= 0:
            return _blocked("diagnostic_token_budget_invalid")

        research = _object(research_path)
        saved = _object(device_path)
        terminal = _object(terminal_path)
        if saved.get("terminal_package") != terminal:
            return _blocked("terminal_package_not_from_device_state")
        resolution = saved.get("contract_resolution")
        if not (
            saved.get("contract_version") == "v2"
            and isinstance(resolution, dict)
            and resolution.get("requested") == "v2"
            and resolution.get("effective") == "v2"
            and resolution.get("requested_matches_effective") is True
            and terminal.get("contract_version") == "v2"
            and terminal.get("contract_resolution") == resolution
        ):
            return _blocked("normal_run_was_not_resolved_v2")
        error_package = terminal.get("error_package")
        if not (
            saved.get("status") == "manual_required"
            and terminal.get("status") == "manual_required"
            and terminal.get("failure_scope") == "device_plan"
            and terminal.get("failure_stage") == "plan_level_audit"
            and isinstance(error_package, dict)
            and error_package.get("type") == "device_plan_audit_blocked"
        ):
            return _blocked("terminal_stop_is_not_plan_material_audit")
        if (
            saved.get("feasibility_accepted") is not False
            or saved.get("feasibility_certificate") not in ({}, None)
            or saved.get("accepted_device_plan_contract") not in ({}, None)
            or terminal.get("feasibility_accepted") is not False
            or terminal.get("feasibility_certificate") not in ({}, None)
            or terminal.get("dispatchable") is True
            or terminal.get("dispatch_payload") not in ({}, None)
            or terminal.get("real_dispatch") is True
            or terminal.get("workflow_json") not in ({}, None)
            or str(terminal.get("workflow_txt") or "").strip()
        ):
            return _blocked("stopped_run_retains_acceptance_or_dispatch_authority")

        # The terminal package is the actual blocked candidate presented to
        # callers.  A deterministic Plan patch may legitimately make it
        # differ from an earlier raw model draft; the Plan-contract audit and
        # candidate-bound relationship authority below prove its identity.
        candidate = terminal
        if not isinstance(candidate, dict) or not isinstance(candidate.get("device_plan"), list):
            return _blocked("stopped_run_has_no_frozen_device_candidate")
        if not candidate["device_plan"] or any(
            not isinstance(step, dict) for step in candidate["device_plan"]
        ):
            return _blocked("stopped_run_device_candidate_invalid")
        if candidate.get("feasibility_accepted") is True:
            return _blocked("candidate_claims_acceptance")

        handoff = saved.get("research_handoff")
        research_package = research.get("research_action_package_v2")
        if not (
            isinstance(handoff, dict)
            and isinstance(research_package, dict)
            and isinstance(handoff.get("research_action_package_v2"), dict)
            and _digest(handoff["research_action_package_v2"])
            == _digest(research_package)
        ):
            return _blocked("research_authority_not_same_as_normal_run")
        steps = research_package.get("macro_steps")
        if not (
            isinstance(steps, list)
            and any(
                isinstance(step, dict)
                and isinstance(step.get("material_contract_status"), dict)
                for step in steps
            )
        ):
            return _blocked("no_explicit_research_material_contract")

        binding = saved.get("relationship_binding_authority")
        origin = saved.get("relationship_binding_authority_origin")
        if not isinstance(binding, dict) or not binding:
            return _blocked("normal_run_has_no_frozen_binding_authority")
        mode = binding.get("authoring_mode")
        if not (
            (origin == "caller_frozen" and mode == "manual_evidence_bound"
             and binding.get("automation_claim") is False)
            or (origin == "automated_resolver" and mode == "automated_evidence_bound"
                and binding.get("automation_claim") is True)
        ):
            return _blocked("normal_run_binding_authority_origin_invalid")

        try:
            from . import material_relationship_compiler, plan_repair
            from .feasibility_certificate import device_plan_contract_digest
        except ImportError:  # direct script/module import from device_agent/
            import material_relationship_compiler  # type: ignore
            import plan_repair  # type: ignore
            from feasibility_certificate import device_plan_contract_digest  # type: ignore

        if (
            binding.get("research_authority_sha256") != _digest(research_package)
            or binding.get("candidate_digest_scope")
            != material_relationship_compiler.CANDIDATE_BINDING_DIGEST_SCOPE
            or binding.get("candidate_sha256")
            != material_relationship_compiler.candidate_binding_sha256(candidate)
            or binding.get("workstation_truth_sha256")
            != saved.get("device_truth_sha256")
        ):
            return _blocked("binding_authority_not_frozen_to_research_plan_truth")
        audit_binding = terminal.get("plan_audit_binding")
        if not (
            isinstance(audit_binding, dict)
            and audit_binding.get("digest") == device_plan_contract_digest(candidate)
        ):
            return _blocked("terminal_audit_not_bound_to_frozen_device_plan")

        diagnostic_runtime: dict[str, Any] = {}
        context = plan_repair._offline_full_plan_context(
            device_path, research, binding,
            diagnostic_runtime=diagnostic_runtime,
        )
        _, _, truth_digest, capability_index, _, finalize = context
        if truth_digest != binding.get("workstation_truth_sha256"):
            return _blocked("current_workstation_truth_changed")
        agent = diagnostic_runtime["agent"]
        replay_state = diagnostic_runtime["state"]
        _, compile_issues, _ = (
            material_relationship_compiler.compile_material_relationships(
                candidate,
                replay_state.research_handoff,
                relationship_bindings=binding,
                resolve_workstation=lambda name: (
                    agent._truth_workstation_code(name) or ""
                ),
                workstation_truth_digest=truth_digest,
                workstation_capability_index=(
                    capability_index if mode == "automated_evidence_bound" else None
                ),
            )
        )
        actual_issues = [dict(issue) for issue in compile_issues]
        if not actual_issues:
            return _blocked("material_compiler_no_longer_blocks_candidate")
        saved_issues = saved.get("binding_ledger_issues")
        if not (
            isinstance(saved_issues, list)
            and all(isinstance(issue, dict) for issue in saved_issues)
            and _issue_multiset(saved_issues) == _issue_multiset(actual_issues)
        ):
            return _blocked("material_blocker_differs_from_saved_device_state")
        findings = error_package.get("structured_errors")
        if not isinstance(findings, list):
            return _blocked("terminal_has_no_structured_material_findings")
        terminal_material_findings = [
            finding
            for finding in findings
            if isinstance(finding, dict)
            and finding.get("type") == "binding_ledger_unresolved"
        ]
        _, _, post_findings = finalize(candidate)
        replayed_material_findings = [
            finding
            for finding in post_findings
            if isinstance(finding, dict)
            and finding.get("type") == "binding_ledger_unresolved"
        ]
        if not (
            terminal_material_findings
            and _issue_multiset(terminal_material_findings)
            == _issue_multiset(replayed_material_findings)
        ):
            return _blocked("material_blocker_differs_from_terminal_package")

        classes = plan_repair._material_compile_issue_summary(actual_issues)
        attribution = plan_repair._binding_authority_attribution(binding)
        blocker = {
            "issue_count": len(actual_issues),
            "issue_sha256": _digest(actual_issues),
            **classes,
            **attribution,
        }
        # This is a diagnostic evidence adapter, *not* a claim that the
        # production Plan-repair loop ran or fixed the blocker.
        summary = {
            "schema_version": 1,
            "source": "normal_device_terminal_material_blocker_reproduction",
            "status": "stopped",
            "stop_reason": "material_relationship_compile_blocked",
            "device_state": str(device_path),
            "terminal_package_sha256": _file_sha256(terminal_path),
            "final_candidate_digest": _digest(candidate),
            "material_blocker_summary": blocker,
            "automation_claim": False,
            "feasibility_accepted": False,
            "dispatchable": False,
            "real_dispatch": False,
        }
        source_hashes = {name: _file_sha256(path) for name, path in sources.items()}
        preparation = destination / "preparation"
        candidate_path = preparation / "frozen_device_candidate.json"
        binding_path = preparation / "frozen_relationship_bindings.json"
        summary_path = preparation / "material_blocker_evidence.json"
        _save(candidate_path, copy.deepcopy(candidate))
        _save(binding_path, copy.deepcopy(binding))
        _save(summary_path, summary)
        _save(preparation / "source_manifest.json", {
            "schema_version": 1,
            "source": "normal_device_terminal_material_blocker_reproduction",
            "diagnostic_only": True,
            "normal_run_sources": {name: str(path) for name, path in sources.items()},
            "normal_run_source_sha256": source_hashes,
            "candidate_sha256": _file_sha256(candidate_path),
            "binding_sha256": _file_sha256(binding_path),
            "blocker_evidence_sha256": _file_sha256(summary_path),
            "material_compile_issue_count": len(actual_issues),
            "material_compile_issue_sha256": _digest(actual_issues),
            "feasibility_accepted": False,
            "dispatchable": False,
            "real_dispatch": False,
        })

        preview_dir = destination / "preview"
        command = [
            sys.executable,
            str(Path(__file__).with_name("diagnostic_workflow_preview.py")),
            "--candidate", str(candidate_path),
            "--research-authority", str(research_path),
            "--device-state", str(device_path),
            "--relationship-bindings", str(binding_path),
            "--repair-summary", str(summary_path),
            "--output-dir", str(preview_dir),
            "--model-name", model_name,
            "--endpoint-url", endpoint_url,
            "--reasoning-effort", reasoning_effort,
            "--timeout-seconds", str(timeout_seconds),
            "--max-tokens", str(max_tokens),
            "--chunk-size", "6",
        ]
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        preview_summary_path = preview_dir / "summary.json"
        preview_summary: dict[str, Any] = {}
        if preview_summary_path.is_file():
            preview_summary = _object(preview_summary_path)
        preview_package_path = preview_dir / "diagnostic_package.json"
        preview_package: dict[str, Any] = {}
        if preview_package_path.is_file():
            preview_package = _object(preview_package_path)
        workflow_check_status = preview_summary.get("workflow_check_status")
        repair_handoff = preview_summary.get("workflow_repair_handoff")
        frozen_sources_unchanged = all(
            _file_sha256(path) == source_hashes[name]
            for name, path in sources.items()
        )
        safe_preview = bool(
            completed.returncode == 0
            and frozen_sources_unchanged
            and preview_summary.get("status") == "diagnostic_blocked"
            and (
                workflow_check_status == "passed"
                or (workflow_check_status == "failed" and repair_handoff == "completed")
            )
            and preview_package.get("status") == "diagnostic_blocked"
            and preview_package.get("diagnostic_only") is True
            and preview_package.get("feasibility_accepted") is False
            and preview_package.get("feasibility_certificate") == {}
            and preview_package.get("dispatchable") is False
            and preview_package.get("dispatch_payload") == {}
            and preview_package.get("real_dispatch") is False
        )
        result = {
            "schema_version": 1,
            "status": "diagnostic_blocked" if safe_preview else "diagnostic_preview_failed",
            "reason": (
                "" if safe_preview else (
                    "frozen_source_changed_during_diagnostic"
                    if not frozen_sources_unchanged
                    else "downstream_preview_incomplete_or_failed"
                )
            ),
            "source": "normal_device_terminal_material_blocker_reproduction",
            "diagnostic_only": True,
            "normal_run_source_sha256": source_hashes,
            "material_compile_issue_count": len(actual_issues),
            "material_compile_issue_sha256": _digest(actual_issues),
            "preview_output_dir": str(preview_dir),
            "preview_exit_code": completed.returncode,
            "workflow_check_status": workflow_check_status or "",
            "workflow_repair_handoff": repair_handoff or "",
            "feasibility_accepted": False,
            "feasibility_certificate": {},
            "dispatchable": False,
            "dispatch_payload": {},
            "real_dispatch": False,
        }
        _save(destination / "diagnostic_linkage.json", result)
        return result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        # Do not return exception text: a provider/loader exception can contain
        # paths, request details or credentials.  The error type is sufficient
        # to distinguish a preparation failure from a real workflow repair.
        return _blocked("diagnostic_preparation_exception", error_type=type(exc).__name__)
