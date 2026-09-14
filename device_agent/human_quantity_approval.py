"""Validate human attestations used by Device-only quantity repair resumes.

The records accepted here are deliberately separate from ordinary observations
and from model-generated Device plans.  A record becomes trusted only after it
is bound to the exact repair-request file, feasibility certificate, frozen
signatures, and one concrete transition child in the submitted override.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any, Dict, List, Tuple


# ``run_from_research_state.py`` supports both ``python -m device_agent...``
# and direct-script execution.  Keep the package and legacy bare import names
# pinned to one module object so the typed class and process-local capability
# registry cannot split across two imports.
_THIS_MODULE = sys.modules[__name__]
if __name__ == "device_agent.human_quantity_approval":
    sys.modules.setdefault("human_quantity_approval", _THIS_MODULE)
elif __name__ == "human_quantity_approval":
    sys.modules.setdefault("device_agent.human_quantity_approval", _THIS_MODULE)


APPROVAL_BASES = {"observed_quantity", "planning_yield_lower_bound"}
APPROVED_QUANTITY_CONTRACT_FIELDS = (
    "device_plan",
    "quantity_adjustments",
    "batch_plan",
    "material_transitions",
    "material_ledger",
)
RESERVED_VALIDATION_FIELDS = {
    "_trusted_human_quantity_approvals",
    "approval_capability_token",
    "human_quantity_approval_validation",
    "validated_human_quantity_approvals",
}
_CAPABILITY_TTL_SECONDS = 300.0
_CAPABILITY_LOCK = threading.Lock()
_CAPABILITY_REGISTRY: Dict[str, Tuple[str, float]] = {}


class HumanQuantityApprovalError(ValueError):
    """Raised when an untrusted quantity approval fails its binding checks."""


@dataclass(frozen=True, repr=False)
class ValidatedHumanQuantityApprovalBundle:
    """Opaque in-process handoff; ordinary JSON/model output cannot construct it."""

    _validation_json: str
    _approvals_json: str
    _capability_token: str

    def validation_copy(self) -> Dict[str, Any]:
        value = json.loads(self._validation_json)
        return value if isinstance(value, dict) else {}

    def approvals_copy(self) -> List[Dict[str, Any]]:
        value = json.loads(self._approvals_json)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _capability_digest(
    envelope: Dict[str, Any],
    records: List[Dict[str, Any]],
) -> str:
    clean_envelope = copy.deepcopy(envelope)
    payload = json.dumps(
        {"envelope": clean_envelope, "records": records},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prune_expired_capabilities(now: float) -> None:
    expired = [
        token
        for token, (_, expires_at) in _CAPABILITY_REGISTRY.items()
        if expires_at <= now
    ]
    for token in expired:
        _CAPABILITY_REGISTRY.pop(token, None)


def mint_validated_approval_capability(
    envelope: Dict[str, Any],
    records: List[Dict[str, Any]],
) -> str:
    """Mint a short-lived, process-local capability for one validated bundle."""

    digest = _capability_digest(envelope, records)
    now = time.monotonic()
    with _CAPABILITY_LOCK:
        _prune_expired_capabilities(now)
        token = secrets.token_urlsafe(32)
        while token in _CAPABILITY_REGISTRY:  # pragma: no cover - cryptographic collision
            token = secrets.token_urlsafe(32)
        _CAPABILITY_REGISTRY[token] = (
            digest,
            now + _CAPABILITY_TTL_SECONDS,
        )
    return token


def consume_validated_approval_capability(
    token: str,
    envelope: Dict[str, Any],
    records: List[Dict[str, Any]],
) -> bool:
    """Atomically consume a capability; replay and tampering both fail closed."""

    if not isinstance(token, str) or not token:
        return False
    now = time.monotonic()
    with _CAPABILITY_LOCK:
        _prune_expired_capabilities(now)
        registered = _CAPABILITY_REGISTRY.pop(token, None)
    if registered is None:
        return False
    expected_digest, expires_at = registered
    if expires_at <= now:
        return False
    return secrets.compare_digest(
        expected_digest,
        _capability_digest(envelope, records),
    )


def consume_validated_human_quantity_approval_bundle(
    bundle: Any,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]] | None:
    """Consume an opaque bundle once and return fresh validated data copies."""

    if not isinstance(bundle, ValidatedHumanQuantityApprovalBundle):
        return None
    envelope = bundle.validation_copy()
    records = bundle.approvals_copy()
    if not consume_validated_approval_capability(
        bundle._capability_token,
        envelope,
        records,
    ):
        return None
    return envelope, records


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(record: Dict[str, Any], field: str, index: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}] missing non-empty {field}"
        )
    return value.strip()


def _reserved_validation_paths(value: Any, path: str = "override") -> List[str]:
    paths: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in RESERVED_VALIDATION_FIELDS:
                paths.append(child_path)
            paths.extend(_reserved_validation_paths(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_reserved_validation_paths(item, f"{path}[{index}]"))
    return paths


def _parse_approved_at(value: str, index: int) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_at must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_at must include a timezone"
        )
    return value


def _canonical_quantity(value: Any, *, index: int) -> Tuple[float, str, str]:
    if not isinstance(value, dict):
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_quantity must be an object"
        )
    raw_value = value.get("value")
    if isinstance(raw_value, bool):
        raw_value = None
    try:
        numeric = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_quantity.value must be numeric"
        ) from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_quantity.value must be finite and > 0"
        )
    unit = str(value.get("unit") or "").strip()
    if not unit:
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_quantity.unit is required"
        )
    normalized = (
        unit.replace("μ", "u")
        .replace("µ", "u")
        .replace(" ", "")
        .lower()
    )
    unit_table = {
        "mol": ("amount", 1.0),
        "mmol": ("amount", 1e-3),
        "umol": ("amount", 1e-6),
        "nmol": ("amount", 1e-9),
        "kg": ("mass", 1e3),
        "g": ("mass", 1.0),
        "mg": ("mass", 1e-3),
        "ug": ("mass", 1e-6),
        "ng": ("mass", 1e-9),
        "l": ("volume", 1.0),
        "ml": ("volume", 1e-3),
        "ul": ("volume", 1e-6),
        "nl": ("volume", 1e-9),
    }
    dimension_and_factor = unit_table.get(normalized)
    if dimension_and_factor is None:
        raise HumanQuantityApprovalError(
            f"human_quantity_approvals[{index}].approved_quantity.unit must be an "
            "extensive amount/mass/volume unit, not a concentration or free-form unit"
        )
    dimension, factor = dimension_and_factor
    return numeric * factor, dimension, unit


def _quantities_equal(left: Any, right: Any, *, index: int) -> bool:
    left_value, left_dimension, _ = _canonical_quantity(left, index=index)
    right_value, right_dimension, _ = _canonical_quantity(right, index=index)
    if left_dimension != right_dimension:
        return False
    return abs(left_value - right_value) <= max(
        1e-12,
        abs(left_value) * 1e-6,
        abs(right_value) * 1e-6,
    )


def _plan_payload(override: Dict[str, Any]) -> Dict[str, Any]:
    supplied = override.get("device_plan")
    if isinstance(supplied, dict):
        plan = copy.deepcopy(supplied)
    elif isinstance(supplied, list):
        plan = {"device_plan": copy.deepcopy(supplied)}
    else:
        plan = {}
    for field in (
        "quantity_adjustments",
        "batch_plan",
        "material_transitions",
        "material_ledger",
    ):
        if field in override:
            plan[field] = copy.deepcopy(override[field])
    return plan


def approved_quantity_contract_view(value: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical plan projection covered by a human approval.

    ``value`` may be the public override wrapper or the Device core's merged
    plan.  ``_plan_payload`` makes nested and top-level sidecar forms
    equivalent before hashing.
    """

    plan = _plan_payload(value) if isinstance(value, dict) else {}
    return {
        field: copy.deepcopy(
            plan.get(field, {} if field == "material_ledger" else [])
        )
        for field in APPROVED_QUANTITY_CONTRACT_FIELDS
    }


def approved_quantity_contract_digest(value: Dict[str, Any]) -> str:
    """SHA-256 of the exact effective quantity-bearing Device-plan contract."""

    encoded = json.dumps(
        approved_quantity_contract_view(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _records_by_id(
    records: Any,
    id_field: str,
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(records, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get(id_field) or "").strip()
        if not record_id:
            continue
        if record_id in result:
            raise HumanQuantityApprovalError(
                f"submitted device_plan contains duplicate {id_field}: {record_id}"
            )
        result[record_id] = record
    return result


def _output_quantity_for_batch(
    transition: Dict[str, Any],
    batch_id: str,
) -> Any:
    outputs = transition.get("output_allocations")
    if not isinstance(outputs, list):
        return None
    matches = [
        item.get("quantity")
        for item in outputs
        if isinstance(item, dict)
        and str(item.get("batch_id") or "").strip() == batch_id
    ]
    return matches[0] if len(matches) == 1 else None


def _unknown_yield_transition_ids(request: Dict[str, Any]) -> set[str]:
    transition_ids: set[str] = set()
    stack: List[Any] = [request.get("structured_errors", [])]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            code = str(
                value.get("code")
                or value.get("error_code")
                or value.get("type")
                or ""
            ).strip().lower()
            if code == "unknown_yield":
                transition_id = str(value.get("transition_id") or "").strip()
                if transition_id:
                    transition_ids.add(transition_id)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return transition_ids


def _certificate_bindings(request: Dict[str, Any]) -> Dict[str, str]:
    certificate = request.get("feasibility_certificate")
    if not isinstance(certificate, dict) or certificate.get("accepted") is not True:
        raise HumanQuantityApprovalError(
            "human quantity approval requires an accepted feasibility_certificate"
        )
    bindings = {
        "feasibility_certificate_id": str(
            certificate.get("certificate_id") or ""
        ).strip(),
        "feasibility_certificate_digest": str(
            certificate.get("protected_digest") or ""
        ).strip(),
        "research_plan_signature": str(
            request.get("frozen_route_signature")
            or certificate.get("research_plan_signature")
            or certificate.get("route_signature")
            or ""
        ).strip(),
        "sample_matrix_signature": str(
            request.get("frozen_sample_matrix_signature")
            or certificate.get("sample_matrix_signature")
            or ""
        ).strip(),
        "device_snapshot_signature": str(
            request.get("device_snapshot_signature")
            or certificate.get("device_snapshot_signature")
            or ""
        ).strip(),
    }
    missing = [field for field, value in bindings.items() if not value]
    if missing:
        raise HumanQuantityApprovalError(
            "repair request certificate is missing approval bindings: "
            + ", ".join(missing)
        )

    certificate_pairs = (
        ("research_plan_signature", "research_plan_signature", "route_signature"),
        ("sample_matrix_signature", "sample_matrix_signature", ""),
        ("device_snapshot_signature", "device_snapshot_signature", ""),
    )
    for binding_name, primary_key, fallback_key in certificate_pairs:
        certificate_value = str(
            certificate.get(primary_key)
            or (certificate.get(fallback_key) if fallback_key else "")
            or ""
        ).strip()
        if certificate_value and certificate_value != bindings[binding_name]:
            raise HumanQuantityApprovalError(
                f"repair request {binding_name} drifts from feasibility_certificate"
            )
    return bindings


def validate_human_quantity_approvals(
    override: Dict[str, Any],
    repair_request: Dict[str, Any],
    *,
    repair_request_sha256: str,
    create_runtime_bundle: bool = False,
) -> Tuple[
    Dict[str, Any],
    Dict[str, Any],
    List[Dict[str, Any]],
    ValidatedHumanQuantityApprovalBundle | None,
]:
    """Return a sanitized override plus a trusted validation envelope.

    No caller-supplied ``validated_*`` field is accepted.  When the input list
    is empty, the returned envelope and validated records are empty too.
    """

    if not isinstance(override, dict) or not isinstance(repair_request, dict):
        raise HumanQuantityApprovalError(
            "device override and repair request must both be JSON objects"
        )
    forged = sorted(_reserved_validation_paths(override))
    if forged:
        raise HumanQuantityApprovalError(
            "device override contains reserved validation fields: "
            + ", ".join(forged)
        )
    sanitized = copy.deepcopy(override)
    raw_approvals = sanitized.get("human_quantity_approvals", [])
    if not isinstance(raw_approvals, list):
        raise HumanQuantityApprovalError(
            "human_quantity_approvals must be an array"
        )
    request_id = str(repair_request.get("request_id") or "").strip()
    if not request_id:
        raise HumanQuantityApprovalError("repair request is missing request_id")
    if str(sanitized.get("request_id") or "").strip() != request_id:
        raise HumanQuantityApprovalError(
            "device plan override request_id does not match repair request"
        )
    if not raw_approvals:
        sanitized.pop("human_quantity_approvals", None)
        sanitized.pop("human_quantity_approval_template", None)
        return sanitized, {}, [], None

    request_digest = str(repair_request_sha256 or "").strip().lower()
    if len(request_digest) != 64 or any(
        character not in "0123456789abcdef" for character in request_digest
    ):
        raise HumanQuantityApprovalError("repair request SHA-256 is invalid")
    certificate_bindings = _certificate_bindings(repair_request)
    approvable_transition_ids = _unknown_yield_transition_ids(repair_request)
    if not approvable_transition_ids:
        raise HumanQuantityApprovalError(
            "repair request contains no transition-bound unknown_yield error to approve"
        )

    plan = _plan_payload(sanitized)
    transition_by_id = _records_by_id(
        plan.get("material_transitions"), "transition_id"
    )
    batch_by_id = _records_by_id(plan.get("batch_plan"), "batch_id")
    if not transition_by_id or not batch_by_id:
        raise HumanQuantityApprovalError(
            "human quantity approvals require material_transitions and batch_plan "
            "in the submitted device_plan override"
        )

    validated: List[Dict[str, Any]] = []
    approval_ids = set()
    approved_targets = set()
    for index, raw in enumerate(raw_approvals):
        if not isinstance(raw, dict):
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] must be an object"
            )
        approval_id = _required_text(raw, "approval_id", index)
        if approval_id in approval_ids:
            raise HumanQuantityApprovalError(
                f"duplicate human quantity approval_id: {approval_id}"
            )
        approval_ids.add(approval_id)

        if _required_text(raw, "repair_request_id", index) != request_id:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] belongs to a different repair request"
            )
        supplied_request_digest = _required_text(
            raw, "repair_request_sha256", index
        ).lower()
        if supplied_request_digest != request_digest:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] repair request hash mismatch"
            )
        for field, expected in certificate_bindings.items():
            if _required_text(raw, field, index) != expected:
                raise HumanQuantityApprovalError(
                    f"human_quantity_approvals[{index}].{field} does not match "
                    "the frozen repair request"
                )

        transition_id = _required_text(raw, "transition_id", index)
        sample_id = _required_text(raw, "sample_id", index)
        material_id = _required_text(raw, "material_id", index)
        batch_id = _required_text(raw, "batch_id", index)
        target = (transition_id, sample_id, material_id, batch_id)
        if target in approved_targets:
            raise HumanQuantityApprovalError(
                f"duplicate human quantity approval target: {target}"
            )
        approved_targets.add(target)

        if transition_id not in approvable_transition_ids:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] transition_id is not an "
                "unknown_yield target in this repair request"
            )

        transition = transition_by_id.get(transition_id)
        if transition is None:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] references unknown transition_id"
            )
        if str(transition.get("transition_kind") or "").strip() != "state_change":
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] can approve only a state_change transition"
            )
        child_ids = {
            str(value).strip()
            for value in transition.get("child_batch_ids", [])
            if str(value).strip()
        } if isinstance(transition.get("child_batch_ids"), list) else set()
        if batch_id not in child_ids:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] batch_id is not a child of transition_id"
            )
        batch = batch_by_id.get(batch_id)
        if batch is None:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] references unknown batch_id"
            )
        if str(batch.get("sample_id") or "").strip() != sample_id:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] sample_id drifts from child batch"
            )
        if str(batch.get("material_id") or "").strip() != material_id:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] material_id drifts from child batch"
            )

        approved_quantity = copy.deepcopy(raw.get("approved_quantity"))
        _canonical_quantity(approved_quantity, index=index)
        batch_quantity = batch.get("total_quantity")
        output_quantity = _output_quantity_for_batch(transition, batch_id)
        if batch_quantity is None or output_quantity is None:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] target lacks a unique child quantity"
            )
        if not _quantities_equal(approved_quantity, batch_quantity, index=index):
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] approved quantity does not match child batch"
            )
        if not _quantities_equal(approved_quantity, output_quantity, index=index):
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] approved quantity does not "
                "match transition output"
            )

        basis = _required_text(raw, "basis", index)
        if basis not in APPROVAL_BASES:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}].basis must be one of "
                + ", ".join(sorted(APPROVAL_BASES))
            )
        expected_transition_basis = (
            "measured_observation"
            if basis == "observed_quantity"
            else "planning_yield_lower_bound"
        )
        if str(transition.get("quantity_basis") or "").strip() != expected_transition_basis:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] basis does not match "
                "transition.quantity_basis"
            )
        yield_lower_bound = None
        if basis == "planning_yield_lower_bound":
            raw_lower_bound = raw.get("yield_lower_bound")
            if isinstance(raw_lower_bound, bool):
                raw_lower_bound = None
            try:
                yield_lower_bound = float(raw_lower_bound)
            except (TypeError, ValueError) as exc:
                raise HumanQuantityApprovalError(
                    f"human_quantity_approvals[{index}].yield_lower_bound must be numeric"
                ) from exc
            if not math.isfinite(yield_lower_bound) or not 0 < yield_lower_bound <= 1:
                raise HumanQuantityApprovalError(
                    f"human_quantity_approvals[{index}].yield_lower_bound must be in (0, 1]"
                )
            transition_lower_bound = transition.get("yield_lower_bound")
            try:
                transition_lower_bound_value = float(transition_lower_bound)
            except (TypeError, ValueError) as exc:
                raise HumanQuantityApprovalError(
                    f"human_quantity_approvals[{index}] target transition lacks yield_lower_bound"
                ) from exc
            if not math.isclose(
                yield_lower_bound,
                transition_lower_bound_value,
                rel_tol=1e-9,
                abs_tol=1e-12,
            ):
                raise HumanQuantityApprovalError(
                    f"human_quantity_approvals[{index}] yield lower bound drifts from transition"
                )

        approved_by = _required_text(raw, "approved_by", index)
        approved_at = _parse_approved_at(
            _required_text(raw, "approved_at", index), index
        )
        if raw.get("acknowledges_scientific_review") is not True:
            raise HumanQuantityApprovalError(
                f"human_quantity_approvals[{index}] must explicitly acknowledge scientific review"
            )

        validated_record = {
            "approval_id": approval_id,
            "approval_basis": basis,
            "attestation_kind": (
                "human_attested_measurement"
                if basis == "observed_quantity"
                else "human_approved_planning_bound"
            ),
            "transition_id": transition_id,
            "sample_id": sample_id,
            "material_id": material_id,
            "batch_id": batch_id,
            "approved_quantity": approved_quantity,
            "repair_request_id": request_id,
            "repair_request_digest": request_digest,
            **certificate_bindings,
            "scientific_review": True,
            "acknowledges_scientific_review": True,
            "approved_by": approved_by,
            "approved_at": approved_at,
        }
        if yield_lower_bound is not None:
            validated_record["yield_lower_bound"] = yield_lower_bound
        validated.append(validated_record)

    validation_envelope = {
        "validated": True,
        "validation_version": "chem-human-quantity-approval/v1",
        "repair_request_id": request_id,
        "repair_request_digest": request_digest,
        "research_plan_signature": certificate_bindings[
            "research_plan_signature"
        ],
        "sample_matrix_signature": certificate_bindings[
            "sample_matrix_signature"
        ],
        "device_snapshot_signature": certificate_bindings[
            "device_snapshot_signature"
        ],
        "feasibility_certificate_id": certificate_bindings[
            "feasibility_certificate_id"
        ],
        "feasibility_certificate_digest": certificate_bindings[
            "feasibility_certificate_digest"
        ],
        "approved_quantity_contract_digest": (
            approved_quantity_contract_digest(plan)
        ),
        "approval_count": len(validated),
    }
    runtime_bundle = None
    if create_runtime_bundle:
        capability_token = mint_validated_approval_capability(
            validation_envelope,
            validated,
        )
        runtime_bundle = ValidatedHumanQuantityApprovalBundle(
            _validation_json=json.dumps(
                validation_envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            _approvals_json=json.dumps(
                validated,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            _capability_token=capability_token,
        )
    # Raw human records and template guidance are input-only.  The Device core
    # receives neither raw nor "validated" JSON fields; only the opaque typed
    # bundle can cross the trusted in-process boundary.
    sanitized.pop("human_quantity_approvals", None)
    sanitized.pop("human_quantity_approval_template", None)
    return sanitized, validation_envelope, validated, runtime_bundle


def approval_contract_template(
    request: Dict[str, Any],
    *,
    repair_request_sha256: str,
) -> Dict[str, Any]:
    """Build a pre-bound record which a human can copy into the approvals list."""

    certificate = (
        request.get("feasibility_certificate")
        if isinstance(request.get("feasibility_certificate"), dict)
        else {}
    )
    bindings = {
        "feasibility_certificate_id": str(
            certificate.get("certificate_id") or ""
        ),
        "feasibility_certificate_digest": str(
            certificate.get("protected_digest") or ""
        ),
        "research_plan_signature": str(
            request.get("frozen_route_signature")
            or certificate.get("research_plan_signature")
            or certificate.get("route_signature")
            or ""
        ),
        "sample_matrix_signature": str(
            request.get("frozen_sample_matrix_signature")
            or certificate.get("sample_matrix_signature")
            or ""
        ),
        "device_snapshot_signature": str(
            request.get("device_snapshot_signature")
            or certificate.get("device_snapshot_signature")
            or ""
        ),
    }
    return {
        "approval_id": "REPLACE_WITH_UNIQUE_APPROVAL_ID",
        "repair_request_id": str(request.get("request_id") or ""),
        "repair_request_sha256": repair_request_sha256,
        **bindings,
        "transition_id": "REPLACE_WITH_TRANSITION_ID",
        "sample_id": "REPLACE_WITH_SAMPLE_ID",
        "material_id": "REPLACE_WITH_MATERIAL_ID",
        "batch_id": "REPLACE_WITH_BATCH_ID",
        "basis": "observed_quantity | planning_yield_lower_bound",
        "approved_quantity": {"value": 0.0, "unit": "REPLACE_WITH_UNIT"},
        "yield_lower_bound": "REQUIRED_ONLY_FOR_PLANNING_BASIS_0_TO_1",
        "approved_by": "REPLACE_WITH_HUMAN_REVIEWER",
        "approved_at": "REPLACE_WITH_TIMEZONE_AWARE_ISO8601",
        "acknowledges_scientific_review": False,
    }
