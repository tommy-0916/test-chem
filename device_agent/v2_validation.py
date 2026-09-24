"""V2 validation issue localization and immutable repair guards."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

try:
    from .macro_identity import (
        MacroIdentityError,
        extract_source_macro_ids,
        macro_id_key,
        normalize_macro_id,
        semantic_macro_id,
    )
    from .workflow_validator import structure_validation_errors
except ImportError:  # pragma: no cover - direct script execution
    from macro_identity import (
        MacroIdentityError,
        extract_source_macro_ids,
        macro_id_key,
        normalize_macro_id,
        semantic_macro_id,
    )
    from workflow_validator import structure_validation_errors


_UNRESOLVED_MACRO_ID = object()


def _declares_source_macro_id(payload: Mapping[str, Any]) -> bool:
    return any(
        field in payload
        for field in (
            "source_macro_step_id",
            "source_macro_step",
            "source_macro_steps",
        )
    )


def _source_macro_ids(payload: Dict[str, Any], path: str) -> List[Any]:
    """Read explicit source bindings without truthiness or string coercion."""

    return extract_source_macro_ids(payload, path)


def _single_typed_macro_id(values: Sequence[Any]) -> Any:
    """Return one exact typed identity, or fail closed on absence/conflict."""

    unique: Dict[Tuple[str, Any], Any] = {}
    for index, value in enumerate(values):
        unique.setdefault(macro_id_key(value, f"macro_ids[{index}]"), value)
    if len(unique) != 1:
        return _UNRESOLVED_MACRO_ID
    return next(iter(unique.values()))


def _validation_macro_id(
    record: Dict[str, Any],
    step: Dict[str, Any],
    path: str,
) -> Any:
    """Resolve one issue's macro identity from explicit, typed evidence.

    The matched workflow step is structured evidence and therefore outranks an
    ID parsed from lossy validator text.  An explicitly declared but malformed
    or conflicting source binding blocks fallback.  Only when no source field
    is declared may the shared macro-step ID aliases be used; an action ID is
    never reinterpreted as a step ID.
    """

    for label, payload in (("workflow_step", step), ("validation_record", record)):
        if not _declares_source_macro_id(payload):
            continue
        try:
            return _single_typed_macro_id(
                _source_macro_ids(payload, f"{path}.{label}")
            )
        except MacroIdentityError:
            return _UNRESOLVED_MACRO_ID

    for label, payload in (("workflow_step", step), ("validation_record", record)):
        try:
            value = semantic_macro_id(
                payload,
                f"{path}.{label}",
                required=False,
            )
        except MacroIdentityError:
            return _UNRESOLVED_MACRO_ID
        if value is not None:
            return value
    return _UNRESOLVED_MACRO_ID


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


def namespaced_lock_hashes(
    *,
    chunk_digests: Mapping[str, str] | None = None,
    device_step_digests: Mapping[str, str] | None = None,
) -> Dict[str, str]:
    """Combine repair locks without sharing caller-controlled key space.

    ``device_step_id`` is an arbitrary nonempty string, so it may legitimately
    equal an internal chunk key such as ``chunk_0``.  Tag both namespaces
    before combining them; a plain ``dict.update`` would otherwise let the
    Device ID overwrite the chunk lock and hide an out-of-scope change.
    """

    combined = {
        f"chunk::{key}": digest
        for key, digest in (chunk_digests or {}).items()
    }
    combined.update(
        {
            f"device_step::{key}": digest
            for key, digest in (device_step_digests or {}).items()
        }
    )
    return combined


def device_step_hashes(
    steps: Sequence[Dict[str, Any]],
    *,
    exclude: Set[str] | None = None,
) -> Dict[str, str]:
    excluded = exclude or set()
    if any(
        not isinstance(value, str) or not value.strip()
        for value in excluded
    ):
        raise ValueError("excluded device_step_id values must be nonempty strings")
    indexed, errors = _index_device_steps(steps, "steps")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        step_id: canonical_hash(step, prefix="device_step")
        for step_id, step in indexed.items()
        if step_id not in excluded
    }


def device_step_hashes_for_chunks(
    chunk_cache: Mapping[int, Mapping[str, Any]],
) -> Dict[str, str]:
    """Hash the complete cached V2 workflow with one global ID namespace.

    Hashing each chunk independently and merging the resulting dictionaries can
    silently turn a cross-chunk duplicate into last-write-wins state.  Flatten
    the cached source steps first so :func:`device_step_hashes` enforces the
    same uniqueness rule across the whole workflow.  Callers must keep this on
    the V2 path; legacy V1 workflows are not required to carry Device step IDs.
    """

    flattened: List[Dict[str, Any]] = []
    for chunk_index in sorted(chunk_cache):
        chunk = chunk_cache[chunk_index]
        raw_steps = chunk.get("steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError(f"chunk_cache[{chunk_index}].steps must be an array")
        flattened.extend(raw_steps)
    return device_step_hashes(flattened)


def _index_device_steps(
    steps: Sequence[Dict[str, Any]], path: str
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Index strict string Device IDs without coercion or duplicate overwrite."""

    indexed: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        return indexed, [f"{path} must be an array of Device step objects"]
    for index, step in enumerate(steps):
        step_path = f"{path}[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{step_path} must be a Device step object")
            continue
        step_id = step.get("device_step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            errors.append(f"{step_path}.device_step_id must be a nonempty string")
            continue
        if step_id in indexed:
            errors.append(f"{path} contains duplicate device_step_id={step_id!r}")
            continue
        indexed[step_id] = step
    return indexed, errors


def merge_scoped_device_step_repair(
    previous: Sequence[Dict[str, Any]],
    candidate: Sequence[Dict[str, Any]],
    mutable_device_step_ids: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Accept replacements only for the issue-localized Device step IDs."""

    previous_by_id, previous_errors = _index_device_steps(previous, "previous")
    candidate_by_id, candidate_errors = _index_device_steps(candidate, "candidate")
    errors: List[str] = previous_errors + candidate_errors
    if any(
        not isinstance(value, str) or not value.strip()
        for value in mutable_device_step_ids
    ):
        errors.append("mutable device_step_id values must be nonempty strings")
    if errors:
        return [dict(step) for step in previous if isinstance(step, dict)], errors
    merged: List[Dict[str, Any]] = []
    for step in previous:
        step_id = step["device_step_id"]
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
        if step_id not in previous_by_id
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
        resolved_macro_id = _validation_macro_id(
            record,
            step,
            f"validation_issues[{index - 1}]",
        )
        macro_step_id = (
            "" if resolved_macro_id is _UNRESOLVED_MACRO_ID else resolved_macro_id
        )
        device_step_id = str(step.get("device_step_id") or "")
        error_code = str(record.get("error_code") or "unparsed")
        field_path = str(record.get("parameter_path") or "")
        if field_path and not field_path.startswith("parameters."):
            field_path = f"parameters.{field_path}"
        actual = record.get("actual")
        observed = {
            key: record[key]
            for key in ("observed_step_numbers", "observed_device_step_ids")
            if record.get(key)
        }
        if observed:
            if actual is not None:
                observed["value"] = actual
            actual = observed
        issues.append(
            {
                "issue_id": f"VI_{index:04d}",
                "validator": "deterministic_workstation_validator",
                "severity": "error",
                "macro_step_id": macro_step_id,
                "device_step_id": device_step_id,
                "field_path": field_path,
                "actual": actual,
                "expected": {
                    "rule_code": error_code,
                    "authorized_device_step_ids": (
                        [device_step_id] if device_step_id else []
                    ),
                },
                "rule": str(record.get("message") or ""),
                "source_ref": str(record.get("workstation") or ""),
                "repair_scope": "device_step" if device_step_id else "macro_step",
            }
        )
    return issues
