"""Summarize a forced Device replay without exporting an actionable workflow.

The raw Device package is retained separately for debugging.  This summary
contains only measured counts/statuses and is *always* diagnostic-only, even
if the V1 compatibility runtime reports success, a feasibility certificate,
or a dispatch payload.  It never authorizes or performs laboratory dispatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DiagnosticSummaryError(ValueError):
    """The diagnostic chain is missing, mismatched, or untrusted."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_object(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise DiagnosticSummaryError(f"expected JSON object: {path}")
    return value, raw


def _verified_handoff(manifest: dict[str, Any]) -> None:
    if (
        manifest.get("kind") != "rejected_research_v1_compat_device_diagnostic"
        or manifest.get("source_contract_version") != "v2"
        or manifest.get("source_status") != "manual_required"
        or manifest.get("runtime_contract") != "v1_compat"
        or manifest.get("formal_v2_validated") is not False
        or manifest.get("dispatchable") is not False
        or manifest.get("real_dispatch") is not False
    ):
        raise DiagnosticSummaryError("manifest is not a rejected V2 diagnostic")
    for path_key, hash_key in (
        ("source_research_state", "source_sha256"),
        ("diagnostic_state", "diagnostic_state_sha256"),
    ):
        path_text = manifest.get(path_key)
        expected_hash = manifest.get(hash_key)
        if not isinstance(path_text, str) or not path_text:
            raise DiagnosticSummaryError(f"manifest lacks {path_key}")
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise DiagnosticSummaryError(f"manifest lacks valid {hash_key}")
        path = Path(path_text).expanduser().resolve(strict=True)
        if _sha256(path.read_bytes()) != expected_hash:
            raise DiagnosticSummaryError(f"manifest {path_key} SHA mismatch")


def _list_count(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def build_summary(
    manifest: dict[str, Any],
    device_package: dict[str, Any],
    *,
    device_package_path: Path,
    device_package_sha256: str,
) -> dict[str, Any]:
    """Build a safe observation summary, not a second terminal package."""

    _verified_handoff(manifest)
    workflow = device_package.get("workflow_json")
    workflow = workflow if isinstance(workflow, dict) else {}
    formatting = device_package.get("dispatch_formatting")
    formatting = formatting if isinstance(formatting, dict) else {}
    error = device_package.get("error_package")
    error = error if isinstance(error, dict) else {}
    feasibility = device_package.get("feasibility")
    feasibility = feasibility if isinstance(feasibility, dict) else {}
    raw_status = device_package.get("status")
    raw_contract = device_package.get("contract_version")
    raw_exp_id = device_package.get("exp_id")
    verification = device_package.get("verification_summary")
    verification = verification if isinstance(verification, dict) else {}

    return {
        "kind": "forced_device_engineering_connectivity_observation",
        "status": "diagnostic_only_unvalidated",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_research_status": manifest["source_status"],
        "source_research_sha256": manifest["source_sha256"],
        "source_candidate_step_count": manifest["candidate_step_count"],
        "runtime_contract": "v1_compat",
        "formal_v2_validated": False,
        "formal_success": False,
        "feasibility_certified": False,
        "dispatchable": False,
        "real_dispatch": False,
        "laboratory_task_id": None,
        "device_package_source": str(device_package_path),
        "device_package_sha256": device_package_sha256,
        "measured": {
            "raw_device_reported_status": (
                str(raw_status) if raw_status is not None else None
            ),
            "raw_device_reported_contract_version": (
                str(raw_contract) if raw_contract is not None else None
            ),
            "raw_local_exp_id": (
                str(raw_exp_id) if raw_exp_id is not None else None
            ),
            "raw_verification_result": (
                str(verification.get("result"))
                if verification.get("result") is not None
                else None
            ),
            "device_plan_step_count": _list_count(device_package.get("device_plan")),
            "workflow_step_count": _list_count(workflow.get("steps")),
            "offline_handoff_count": _list_count(workflow.get("offline_handoffs")),
            "dispatch_formatting_mapped_step_count": _nonnegative_int(
                formatting.get("mapped_steps")
            ),
            "dispatch_formatting_unmapped_step_count": _nonnegative_int(
                formatting.get("unmapped_steps")
            ),
            "dispatch_formatting_warning_count": _list_count(
                formatting.get("warnings")
            ),
            "raw_feasibility_is_feasible": (
                feasibility.get("is_feasible")
                if isinstance(feasibility.get("is_feasible"), bool)
                else None
            ),
            "raw_error_type": (
                str(error.get("type")) if error.get("type") is not None else None
            ),
            "raw_blocking_constraint_count": _list_count(
                error.get("blocking_constraints")
            ),
            "raw_dispatch_payload_present": bool(device_package.get("dispatch_payload")),
            "raw_certificate_present": bool(
                device_package.get("feasibility_certificate")
            ),
            "raw_workflow_text_present": bool(device_package.get("workflow_txt")),
        },
        "warning": (
            "Raw Device status and payload presence are observations only. "
            "This rejected Research V2 candidate was replayed through V1 "
            "compatibility; no formal quality gate, certificate, actionable "
            "workflow, or laboratory dispatch is established by this summary."
        ),
    }


def write_summary(
    manifest_path: Path, device_package_path: Path, output_path: Path
) -> Path:
    manifest_path = manifest_path.expanduser().resolve(strict=True)
    device_package_path = device_package_path.expanduser().resolve(strict=True)
    manifest, _ = _load_object(manifest_path)
    package, raw_package_bytes = _load_object(device_package_path)
    summary = build_summary(
        manifest,
        package,
        device_package_path=device_package_path,
        device_package_sha256=_sha256(raw_package_bytes),
    )
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--device-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        output = write_summary(args.manifest, args.device_package, args.output)
    except (
        DiagnosticSummaryError,
        FileNotFoundError,
        FileExistsError,
        OSError,
        json.JSONDecodeError,
        UnicodeError,
    ) as exc:
        parser.error(str(exc))
    print(f"diagnostic_summary={output}")
    print("status=diagnostic_only_unvalidated; formal_success=false; real_dispatch=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
