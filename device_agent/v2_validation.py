"""V2 validation issue localization and immutable repair guards."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

try:
    from .workflow_validator import structure_validation_errors
except ImportError:  # pragma: no cover - direct script execution
    from workflow_validator import structure_validation_errors


def canonical_hash(value: Any, *, prefix: str = "sha256") -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()}"


def chunk_hashes(chunk_cache: Mapping[int, Dict[str, Any]]) -> Dict[str, str]:
    """Hash each independently replaceable translation unit."""

    return {
        f"chunk_{index}": canonical_hash(record.get("steps", []), prefix="locked")
        for index, record in sorted(chunk_cache.items())
    }


def locked_chunk_violations(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
) -> List[str]:
    return [
        f"{key} changed outside the authorized repair scope"
        for key, digest in expected.items()
        if actual.get(key) != digest
    ]


def device_step_hashes(
    steps: Sequence[Dict[str, Any]],
    *,
    exclude: Set[str] | None = None,
) -> Dict[str, str]:
    excluded = exclude or set()
    return {
        str(step.get("device_step_id")): canonical_hash(step, prefix="device_step")
        for step in steps
        if str(step.get("device_step_id") or "")
        and str(step.get("device_step_id")) not in excluded
    }


def merge_scoped_device_step_repair(
    previous: Sequence[Dict[str, Any]],
    candidate: Sequence[Dict[str, Any]],
    mutable_device_step_ids: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Accept replacements only for the issue-localized Device step IDs."""

    candidate_by_id = {
        str(step.get("device_step_id")): step
        for step in candidate
        if str(step.get("device_step_id") or "")
    }
    errors: List[str] = []
    merged: List[Dict[str, Any]] = []
    for step in previous:
        step_id = str(step.get("device_step_id") or "")
        if step_id in mutable_device_step_ids:
            replacement = candidate_by_id.get(step_id)
            if replacement is None:
                errors.append(f"repair omitted authorized device_step_id={step_id}")
                merged.append(dict(step))
            else:
                merged.append(dict(replacement))
        else:
            merged.append(dict(step))
    unexpected = sorted(
        step_id
        for step_id in candidate_by_id
        if step_id not in {str(step.get("device_step_id") or "") for step in previous}
    )
    if unexpected:
        errors.append(
            "repair introduced unauthorized device_step_id(s): " + ", ".join(unexpected)
        )
    return merged, errors


def build_validation_issues_v2(
    errors: Iterable[Any],
    workflow_json: Any,
) -> List[Dict[str, Any]]:
    """Convert legacy validator messages to the complete V2 issue shape."""

    structured = structure_validation_errors(list(errors), workflow_json)
    steps_by_number: Dict[Any, Dict[str, Any]] = {}
    if isinstance(workflow_json, dict):
        for step in workflow_json.get("steps", []) or []:
            if isinstance(step, dict):
                steps_by_number[step.get("step_number")] = step
    issues: List[Dict[str, Any]] = []
    for index, record in enumerate(structured, start=1):
        step = steps_by_number.get(record.get("step_number"), {})
        macro_step_id = str(
            record.get("source_macro_step_id")
            or step.get("source_macro_step_id")
            or record.get("source_macro_step")
            or step.get("source_macro_step")
            or ""
        )
        device_step_id = str(step.get("device_step_id") or "")
        error_code = str(record.get("error_code") or "unparsed")
        field_path = str(record.get("parameter_path") or "")
        if field_path and not field_path.startswith("parameters."):
            field_path = f"parameters.{field_path}"
        issues.append(
            {
                "issue_id": f"VI_{index:04d}",
                "validator": "deterministic_workstation_validator",
                "severity": "error",
                "macro_step_id": macro_step_id,
                "device_step_id": device_step_id,
                "field_path": field_path,
                "actual": record.get("actual"),
                "expected": {"rule_code": error_code},
                "rule": str(record.get("message") or ""),
                "source_ref": str(record.get("workstation") or ""),
                "repair_scope": "device_step" if device_step_id else "macro_step",
            }
        )
    return issues
