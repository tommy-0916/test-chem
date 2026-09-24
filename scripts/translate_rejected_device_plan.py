"""Translate a rejected Device plan for connectivity diagnosis only.

This command does not resume the Device pipeline, certify feasibility, format
dispatch payloads, or contact a laboratory.  It calls only the existing
plan-to-workflow LLM translator and wraps any output as diagnostic_unvalidated.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEVICE_AGENT_DIR = REPO_ROOT / "device_agent"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(DEVICE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(DEVICE_AGENT_DIR))

WARNING = (
    "UNVALIDATED DIAGNOSTIC ONLY: rejected Research/Device candidate. "
    "No V2 Research acceptance, Device feasibility certificate, valid "
    "dispatch workflow, or 303 laboratory task is implied. Never dispatch."
)


class DiagnosticTranslationError(ValueError):
    """The saved state is not the exact rejected candidate expected here."""


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_rejected_candidate(
    source_path: Path,
    *,
    expected_source_sha256: str,
    expected_steps: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Bind the raw plan to the saved, failed Device state before any model use."""

    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_source_sha256):
        raise DiagnosticTranslationError("expected source SHA-256 must be 64 hex digits")
    if expected_steps < 1:
        raise DiagnosticTranslationError("expected step count must be positive")
    source_bytes = source_path.expanduser().resolve(strict=True).read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != expected_source_sha256.lower():
        raise DiagnosticTranslationError("saved Device state SHA-256 mismatch")
    source = json.loads(source_bytes.decode("utf-8"))
    if not isinstance(source, dict):
        raise DiagnosticTranslationError("saved Device state must be an object")
    if source.get("contract_version") != "v1" or source.get("status") != "manual_required":
        raise DiagnosticTranslationError("source must be a rejected V1-compat Device state")
    if source.get("feasibility_accepted") is not False or source.get("feasibility_certificate"):
        raise DiagnosticTranslationError("source must have no Device feasibility acceptance")
    if source.get("workflow_json") or source.get("workflow_txt"):
        raise DiagnosticTranslationError("source already contains a workflow")
    terminal = source.get("terminal_package")
    if not isinstance(terminal, dict) or terminal.get("status") != "manual_required":
        raise DiagnosticTranslationError("source terminal package must be manual_required")

    handoff = source.get("research_handoff")
    if not isinstance(handoff, dict):
        raise DiagnosticTranslationError("source Research handoff is missing")
    marker = handoff.get("macro_action")
    marker = marker.get("diagnostic_forced_continue") if isinstance(marker, dict) else None
    if (
        not isinstance(marker, dict)
        or marker.get("runtime_contract") != "v1_compat"
        or marker.get("formal_v2_validated") is not False
        or marker.get("dispatchable") is not False
        or marker.get("real_dispatch") is not False
    ):
        raise DiagnosticTranslationError("source lacks the rejected-Research diagnostic marker")

    raw = source.get("raw_llm_output")
    if not isinstance(raw, dict):
        raise DiagnosticTranslationError("raw Device candidate is missing")
    if raw.get("status") != "manual_required" or raw.get("failure_stage") != "plan_level_audit":
        raise DiagnosticTranslationError("raw candidate must have failed plan-level audit")
    if raw.get("feasibility_accepted") is not False or raw.get("feasibility_certificate"):
        raise DiagnosticTranslationError("raw candidate must have no feasibility certificate")
    if raw.get("workflow_json") or raw.get("workflow_txt"):
        raise DiagnosticTranslationError("raw candidate already contains a workflow")
    plan = raw.get("device_plan")
    if (
        not isinstance(plan, list)
        or len(plan) != expected_steps
        or not all(isinstance(step, dict) for step in plan)
    ):
        raise DiagnosticTranslationError(f"raw Device plan must contain {expected_steps} steps")
    plan_ids = [step.get("plan_step") for step in plan]
    if plan_ids != list(range(1, expected_steps + 1)):
        raise DiagnosticTranslationError("raw Device plan_step IDs must be contiguous")
    if not all(isinstance(step.get("workstation"), str) and step["workstation"].strip() for step in plan):
        raise DiagnosticTranslationError("each raw Device step needs a workstation")

    audit = raw.get("plan_audit_record")
    binding = raw.get("plan_audit_binding")
    if (
        not isinstance(audit, dict)
        or audit.get("run_layer_status") != "blocked"
        or not isinstance(audit.get("findings"), list)
        or not audit["findings"]
        or not isinstance(binding, dict)
        or binding.get("scope") != "device_plan_contract/v1"
        or binding.get("digest") != audit.get("device_plan_contract_digest")
    ):
        raise DiagnosticTranslationError("blocked plan audit or its binding is missing")

    # The production audit digest is checked, but never used as a certificate.
    from single_agent import device_plan_contract_digest

    if device_plan_contract_digest(raw) != binding["digest"]:
        raise DiagnosticTranslationError("raw Device plan changed after the failed audit")
    if not isinstance(source.get("device_truth_sha256"), str) or not source["device_truth_sha256"]:
        raise DiagnosticTranslationError("saved workstation truth binding is missing")
    return source, copy.deepcopy(raw), source_sha256


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def translate_diagnostic(
    source_path: Path,
    output_dir: Path,
    *,
    expected_source_sha256: str,
    expected_steps: int = 19,
    model_factory: Callable[[], Any],
) -> tuple[Path, bool]:
    """Run only the plan translator. ``model_factory`` enables no-network tests."""

    source_path = source_path.expanduser().resolve(strict=True)
    source, candidate, source_sha256 = load_rejected_candidate(
        source_path,
        expected_source_sha256=expected_source_sha256,
        expected_steps=expected_steps,
    )
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    candidate_path = output_dir / "rejected_device_candidate.json"
    result_path = output_dir / "diagnostic_workflow.json"
    manifest_path = output_dir / "diagnostic_manifest.json"
    candidate_digest = _sha256_json(candidate)
    _write_json(candidate_path, candidate)
    manifest = {
        "kind": "rejected_device_plan_translation_diagnostic",
        "status": "diagnostic_unvalidated",
        "translation_status": "not_started",
        "warning": WARNING,
        "source_device_state": str(source_path),
        "source_device_state_sha256": source_sha256,
        "raw_candidate_path": "raw_llm_output",
        "raw_candidate_sha256": candidate_digest,
        "raw_candidate_file": str(candidate_path),
        "raw_plan_steps": expected_steps,
        "plan_audit_binding": copy.deepcopy(candidate["plan_audit_binding"]),
        "plan_audit_finding_count": len(candidate["plan_audit_record"]["findings"]),
        "workstation_truth_sha256": source["device_truth_sha256"],
        "formal_v2_validated": False,
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "dispatchable": False,
        "real_dispatch": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(manifest_path, manifest)

    workflow_json: dict[str, Any] = {}
    workflow_txt = ""
    failure_type = ""
    chunk_cache: dict[int, dict[str, Any]] = {}
    try:
        from single_agent import SingleDeviceAgent, SingleDeviceAgentState

        agent = SingleDeviceAgent(model_factory(), contract_version="v1")
        state = SingleDeviceAgentState(
            research_handoff=copy.deepcopy(source["research_handoff"]),
            exp_id=str(source.get("exp_id") or "diagnostic-local"),
            contract_version="v1",
            device_truth_sha256=source["device_truth_sha256"],
            txt_format_reference=str(source.get("txt_format_reference") or ""),
            json_format_reference=str(source.get("json_format_reference") or ""),
        )
        agent._refresh_workstation_snapshot()
        agent._assert_workstation_snapshot_current(state)
        if (
            state.txt_format_reference != agent._txt_format_reference
            or state.json_format_reference != agent._json_format_reference
        ):
            raise DiagnosticTranslationError("format references changed since the failed Device run")
        workflow_json, workflow_txt, _, _ = agent._translate_plan_in_chunks(
            state, candidate, chunk_cache
        )
        if not isinstance(workflow_json, dict):
            workflow_json = {}
        if not isinstance(workflow_txt, str):
            workflow_txt = ""
    except Exception as exc:
        # Error strings and model diagnostics may contain remote request data.
        # Keep only the class name; never serialize a credential or response.
        failure_type = type(exc).__name__

    produced = bool(isinstance(workflow_json.get("steps"), list) and workflow_json["steps"])
    result = {
        "status": "diagnostic_unvalidated",
        "translation_status": "produced" if produced else "failed",
        "warning": WARNING,
        "source_device_state_sha256": source_sha256,
        "raw_candidate_sha256": candidate_digest,
        "raw_plan_steps": expected_steps,
        "completed_translation_chunks": len(chunk_cache),
        "translated_workflow_steps": len(workflow_json.get("steps") or []),
        "plan_audit_findings": copy.deepcopy(candidate["plan_audit_record"]["findings"]),
        "workflow_json": workflow_json,
        "workflow_txt": workflow_txt,
        "failure_type": failure_type,
        "formal_v2_validated": False,
        "feasibility_accepted": False,
        "feasibility_certificate": {},
        "dispatchable": False,
        "real_dispatch": False,
    }
    _write_json(result_path, result)
    manifest["translation_status"] = result["translation_status"]
    manifest["translated_workflow_steps"] = result["translated_workflow_steps"]
    manifest["completed_translation_chunks"] = len(chunk_cache)
    manifest["diagnostic_workflow_file"] = str(result_path)
    manifest["diagnostic_workflow_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
    manifest["failure_type"] = failure_type
    _write_json(manifest_path, manifest)
    return result_path, produced


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-device-state", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--expected-steps", type=int, default=19)
    parser.add_argument("--model-name", default="kimi-k3")
    parser.add_argument("--base-url", default="https://api.kimi.com/coding/v1")
    parser.add_argument("--wire-api", choices=["codex_responses"], default="codex_responses")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--max-tokens", type=int, default=65536)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    model_factory: Callable[[], Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if model_factory is None:
        def model_factory() -> Any:
            os.environ["CHEM_LLM_COMPONENT"] = "device"
            os.environ["REFINER_LLM_WIRE_API"] = args.wire_api
            os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
            from utils.llm_factory import LLMFactory

            return LLMFactory.create(
                model_name=args.model_name,
                endpoint_url=args.base_url,
                max_tokens=args.max_tokens,
                timeout=args.timeout_seconds,
            )

    result_path, produced = translate_diagnostic(
        args.source_device_state,
        args.output_dir,
        expected_source_sha256=args.source_sha256,
        expected_steps=args.expected_steps,
        model_factory=model_factory,
    )
    print(f"diagnostic artifact: {result_path}")
    print("translation status: produced" if produced else "translation status: failed")
    return 0 if produced else 1


if __name__ == "__main__":
    raise SystemExit(main())
