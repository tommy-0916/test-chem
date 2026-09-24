"""Persistent, permission-constrained local repair for Device plan candidates.

This module implements the four repair responsibilities around the existing
Stage-1 repair flow:

- ``diagnose_candidate``    run the validator and normalize field-level errors
- ``build_repair_context``  assemble the per-round view from the RepairCase
                            (never "append the whole chat history")
- ``propose_repair``        deterministic JSON-Patch proposal inside the
                            authorized scope, or a controlled non-patch verdict
- ``validate_repair``       verify versions/permissions, apply on an isolated
                            copy, re-run the full audit, and preserve only
                            monotonic, no-regression progress; whole-candidate
                            acceptance still requires zero findings

A RepairCase is persisted program-side JSON (contracts, baseline, draft,
append-only log).  The model never writes it, a saved baseline is not an
approval, and a promoted draft is still not dispatchable until the normal
acceptance gates pass.

Offline usage (no model, no network, no devices):

    python device_agent/plan_repair.py \
        --device-state path/to/device_state.json --progress-index 3 \
        --case-dir result/device_repair_cases/demo --output report.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    _here = Path(__file__).resolve().parent
    _root = _here.parent
    for _path in (str(_here), str(_root)):
        if _path not in sys.path:
            sys.path.insert(0, _path)
    import single_agent as _single_agent
    from single_agent import (
        RECIPE_FIELD_CONTRACT,
        _recipe_normalized_key,
        recipe_step_diagnostics,
        recipe_step_is_file_dosing,
    )
    from contract_value_normalizer import (
        RULE_ID as CONTRACT_SCALAR_RULE_ID,
        normalize_contract_scalar,
    )
    from macro_identity import MacroIdentityError, macro_id_key, normalize_macro_id
    from material_relationship_compiler import (
        BINDING_AUTHORITY_SCHEMA,
        RULE_ID as MATERIAL_RELATIONSHIP_COMPILER_RULE_ID,
        compile_material_relationships,
        relationship_binding_records,
    )
else:
    from . import single_agent as _single_agent
    from .single_agent import (
        RECIPE_FIELD_CONTRACT,
        _recipe_normalized_key,
        recipe_step_diagnostics,
        recipe_step_is_file_dosing,
    )
    from .contract_value_normalizer import (
        RULE_ID as CONTRACT_SCALAR_RULE_ID,
        normalize_contract_scalar,
    )
    from .macro_identity import MacroIdentityError, macro_id_key, normalize_macro_id
    from .material_relationship_compiler import (
        BINDING_AUTHORITY_SCHEMA,
        RULE_ID as MATERIAL_RELATIONSHIP_COMPILER_RULE_ID,
        compile_material_relationships,
        relationship_binding_records,
    )

SCHEMA_VERSION = 2
PATCH_FORMAT = "chem-plan-patch/1"
MATERIAL_PATCH_FORMAT = "chem-material-relationship-compile/1"
DEFAULT_PATCH_LIMIT = 3
DEFAULT_MAX_ROUNDS = 6
DIGEST_ALGORITHM = "sha256"
DIGEST_SCOPE_CANONICAL_JSON = "canonical_json_utf8_sorted_compact/v1"
RESEARCH_AUTHORITY_VALIDATION_RULE = "research-authority-validation/v1"

_RESEARCH_AUTHORITY_VALIDATION_MODES = {
    "package_only_schema_hash": ("not_available", "not_available"),
    "handoff_mirror_rebuild": ("verified", "verified"),
    "full_state_mirror_rebuild": ("verified", "verified"),
}

# Findings whose failed checks can be repaired by a deterministic, authorized
# value-type patch.  Anything else routes to the controlled non-patch verdicts.
PATCHABLE_RULES = {"json_number", "json_integer"}

RECIPE_FINDING_MESSAGE = (
    "文件传参固体称量缺少可物化的逐瓶配方。必须给出每行瓶号、"
    "确定加样量(g)和料罐号；不能使用按实测质量/按比例/适量等运行时未知值。"
)

FROZEN_TOP_LEVEL_KEYS = ("sample_control_matrix",)

# Finding types that must never be (re)introduced by a recipe patch.  They
# were cleared by earlier rounds (or were never present); a patch that makes
# any of them appear is a scientific regression, not a repair.
SEMANTIC_REGRESSION_TYPES = frozenset(
    {
        "sample_matrix_drift",
        "frozen_material_transition_coverage_missing",
        "frozen_research_route_drift",
    }
)

_AUDITOR = Callable[[Dict[str, Any]], List[Dict[str, Any]]]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def full_plan_audit_context_digest(
    state_payload: Dict[str, Any],
    *,
    contract_version: str,
    active_semantic_analysis: Dict[str, Any],
) -> str:
    """Bind a repair case to every restored input of the full Plan auditor."""

    return _digest(
        {
            "state_payload": copy.deepcopy(state_payload),
            "contract_version": str(contract_version),
            "active_semantic_analysis": copy.deepcopy(active_semantic_analysis),
        }
    )


def _validated_research_v2_authority(
    authority: Any,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Validate one Research V2 authority through the production boundary.

    Full persisted Research states are checked with the same mirror, schema,
    hash, and raw-state rebuild gate used by ``run_from_research_state``.
    Package-only inputs cannot prove raw mirror consistency, but they still
    undergo strict ``ResearchActionPackageV2`` schema/hash validation and are
    labelled accordingly.  Repair must never silently select one of several
    divergent mirrors or freeze an unvalidated package.
    """

    if not isinstance(authority, dict):
        return None, {}

    from chem_agent_contracts.v2 import ResearchActionPackageV2

    if isinstance(authority.get("macro_steps"), list):
        if not str(authority.get("research_contract_hash") or "").strip():
            raise ValueError(
                "package-only Research V2 authority must carry its original "
                "non-empty research_contract_hash"
            )
        try:
            parsed = ResearchActionPackageV2.model_validate(authority)
        except Exception as exc:
            raise ValueError(
                f"invalid package-only Research V2 authority: {exc}"
            ) from exc
        package = parsed.model_dump(mode="json", exclude_none=True)
        return package, {
            "validation_rule": RESEARCH_AUTHORITY_VALIDATION_RULE,
            "validation_mode": "package_only_schema_hash",
            "source_kind": "bare_package",
            "canonical_mirror_paths": [],
            "raw_mirror_paths": [],
            "raw_mirror_consistency": "not_available",
            "raw_to_canonical_consistency": "not_available",
            "research_contract_hash": parsed.research_contract_hash,
            "source_authority_digest": _digest(authority),
        }

    for container_key in (
        "device_adaptation_handoff",
        "research_handoff",
        "persistent_outputs",
        "A. research layer 内部持久化输出",
        "B. 发给下游 device adaptation layer agent 的外部交接输出",
    ):
        if container_key in authority and not isinstance(
            authority[container_key], dict
        ):
            raise ValueError(
                f"invalid Research authority container {container_key}: "
                "expected an object"
            )

    canonical_mirrors: List[Tuple[str, Dict[str, Any]]] = []
    for container_key in (
        None,
        "device_adaptation_handoff",
        "research_handoff",
        "persistent_outputs",
        "A. research layer 内部持久化输出",
        "B. 发给下游 device adaptation layer agent 的外部交接输出",
    ):
        container = authority if container_key is None else authority.get(container_key)
        if not isinstance(container, dict) or "research_action_package_v2" not in container:
            continue
        label = (
            "research_action_package_v2"
            if container_key is None
            else f"{container_key}.research_action_package_v2"
        )
        value = container["research_action_package_v2"]
        if not isinstance(value, dict) or not value:
            raise ValueError(
                f"invalid Research canonical package mirror {label}: expected "
                "a non-empty object"
            )
        if not str(value.get("research_contract_hash") or "").strip():
            raise ValueError(
                f"invalid Research canonical package mirror {label}: missing "
                "research_contract_hash"
            )
        canonical_mirrors.append((label, value))
    parsed_mirrors: List[Tuple[str, ResearchActionPackageV2]] = []
    for label, value in canonical_mirrors:
        try:
            parsed_mirrors.append(
                (label, ResearchActionPackageV2.model_validate(value))
            )
        except Exception as exc:
            raise ValueError(f"invalid {label}: {exc}") from exc
    if parsed_mirrors:
        expected_hash = parsed_mirrors[0][1].research_contract_hash
        disagreements = [
            label
            for label, package in parsed_mirrors[1:]
            if package.research_contract_hash != expected_hash
        ]
        if disagreements:
            raise ValueError(
                "research_authority_package_mismatch: "
                + ", ".join([parsed_mirrors[0][0], *disagreements])
            )
    raw_locations: List[Tuple[str, Dict[str, Any], str]] = [
        ("macro_plan", authority, "macro_plan"),
        ("macro_action_steps", authority, "macro_action_steps"),
    ]
    for container_key, display_name in (
        ("device_adaptation_handoff", "device_adaptation_handoff"),
        (
            "B. 发给下游 device adaptation layer agent 的外部交接输出",
            "external_handoff",
        ),
        ("persistent_outputs", "persistent_outputs"),
        (
            "A. research layer 内部持久化输出",
            "internal_persistent_outputs",
        ),
    ):
        container = authority.get(container_key)
        if not isinstance(container, dict):
            continue
        raw_locations.extend(
            (
                (f"{display_name}.待执行 macro plan", container, "待执行 macro plan"),
                (f"{display_name}.macro_plan", container, "macro_plan"),
                (
                    f"{display_name}.macro_action_steps",
                    container,
                    "macro_action_steps",
                ),
            )
        )
    compatibility_handoff = authority.get("research_handoff")
    if isinstance(compatibility_handoff, dict):
        raw_locations.extend(
            (
                (
                    "research_handoff.待执行 macro plan",
                    compatibility_handoff,
                    "待执行 macro plan",
                ),
                (
                    "research_handoff.macro_plan",
                    compatibility_handoff,
                    "macro_plan",
                ),
                (
                    "research_handoff.macro_action_steps",
                    compatibility_handoff,
                    "macro_action_steps",
                ),
            )
        )
    raw_mirrors: List[Tuple[str, List[Dict[str, Any]]]] = []
    for label, container, key in raw_locations:
        if key not in container:
            continue
        value = container[key]
        if not isinstance(value, list) or not value:
            raise ValueError(
                f"invalid Research raw macro-plan mirror {label}: expected a "
                "non-empty array"
            )
        if any(not isinstance(item, dict) for item in value):
            raise ValueError(
                f"invalid Research raw macro-plan mirror {label}: every step "
                "must be an object"
            )
        raw_mirrors.append((label, value))
    raw_present = bool(raw_mirrors)
    top_level_raw_present = any(label == "macro_plan" for label, _ in raw_mirrors)
    if not parsed_mirrors and not raw_present:
        return None, {}

    if not raw_present:
        parsed = parsed_mirrors[0][1]
        return parsed.model_dump(mode="json", exclude_none=True), {
            "validation_rule": RESEARCH_AUTHORITY_VALIDATION_RULE,
            "validation_mode": "package_only_schema_hash",
            "source_kind": "canonical_mirror_only",
            "canonical_mirror_paths": [label for label, _ in parsed_mirrors],
            "raw_mirror_paths": [],
            "raw_mirror_consistency": "not_available",
            "raw_to_canonical_consistency": "not_available",
            "research_contract_hash": parsed.research_contract_hash,
            "source_authority_digest": _digest(authority),
        }

    if __package__ in (None, ""):
        from run_from_research_state import (  # type: ignore
            extract_macro_plan,
            validate_v2_research_handoff_consistency,
        )
    else:
        from .run_from_research_state import (
            extract_macro_plan,
            validate_v2_research_handoff_consistency,
        )

    selected_macro_plan: List[Dict[str, Any]] = []
    validation_authority = authority
    if raw_mirrors and not any(
        not label.startswith("research_handoff.") for label, _ in raw_mirrors
    ):
        # ``research_handoff`` is a repair-helper compatibility wrapper, not a
        # second canonical Research-state container.  Validate its contents as
        # the direct handoff object, while the outer canonical mirrors are
        # still compared below.
        validation_authority = compatibility_handoff
    if raw_present:
        try:
            selected_macro_plan = extract_macro_plan(validation_authority)
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        selected_raw_digest = _digest(selected_macro_plan)
        divergent_raw = [
            label
            for label, value in raw_mirrors
            if _digest(value) != selected_raw_digest
        ]
        if divergent_raw:
            raise ValueError(
                "research_authority_raw_plan_mismatch: "
                + ", ".join(divergent_raw)
            )
    try:
        package = validate_v2_research_handoff_consistency(
            validation_authority,
            selected_macro_plan,
        )
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    parsed = ResearchActionPackageV2.model_validate(package)
    if parsed_mirrors and (
        parsed.research_contract_hash
        != parsed_mirrors[0][1].research_contract_hash
    ):
        raise ValueError(
            "validated Research package does not match all supplied canonical mirrors"
        )
    return parsed.model_dump(mode="json", exclude_none=True), {
        "validation_rule": RESEARCH_AUTHORITY_VALIDATION_RULE,
        "validation_mode": (
            "full_state_mirror_rebuild"
            if top_level_raw_present
            else "handoff_mirror_rebuild"
        ),
        "source_kind": "research_state" if top_level_raw_present else "handoff_only",
        "canonical_mirror_paths": [label for label, _ in parsed_mirrors],
        "raw_mirror_paths": [label for label, _ in raw_mirrors],
        "raw_mirror_consistency": "verified",
        "raw_to_canonical_consistency": "verified",
        "research_contract_hash": parsed.research_contract_hash,
        "source_authority_digest": _digest(authority),
    }


def _research_v2_package(authority: Any) -> Optional[Dict[str, Any]]:
    package, _ = _validated_research_v2_authority(authority)
    return package


def _material_repair_inputs(
    research_authority: Optional[Dict[str, Any]],
    relationship_bindings: Optional[Dict[str, Any]],
    *,
    workstation_truth_digest: str = "",
    full_plan_audit_context_digest: str = "",
) -> Dict[str, Any]:
    """Freeze the exact deterministic relationship-repair authority."""

    if research_authority is None:
        return {}
    package, validation = _validated_research_v2_authority(research_authority)
    if package is None:
        raise ValueError(
            "material_relationship_repair requires an explicit "
            "research_action_package_v2"
        )
    bindings = copy.deepcopy(
        {} if relationship_bindings is None else relationship_bindings
    )
    if not isinstance(bindings, dict):
        raise ValueError("relationship_bindings must be an object")
    return {
        "research_action_package_v2": package,
        "research_authority_digest": _digest(package),
        "research_authority_validation": validation,
        "research_authority_validation_digest": _digest(validation),
        "relationship_bindings": bindings,
        "relationship_bindings_digest": _digest(bindings),
        "workstation_truth_digest": str(workstation_truth_digest or "").strip(),
        "full_plan_audit_context_digest": str(
            full_plan_audit_context_digest or ""
        ).strip(),
        "compiler_rule": MATERIAL_RELATIONSHIP_COMPILER_RULE_ID,
    }


def _validate_material_repair_inputs(case: Dict[str, Any]) -> Dict[str, Any]:
    inputs = case.get("material_relationship_repair")
    if inputs is None:
        return {}
    if not isinstance(inputs, dict):
        raise ValueError("repair_case_invalid: material repair inputs must be an object")
    if not inputs:
        return {}
    package = inputs.get("research_action_package_v2")
    bindings = inputs.get("relationship_bindings")
    if not isinstance(package, dict) or not isinstance(bindings, dict):
        raise ValueError("repair_case_invalid: material repair authority is malformed")
    if inputs.get("research_authority_digest") != _digest(package):
        raise ValueError("repair_case_digest_mismatch: Research material authority changed")
    validation = inputs.get("research_authority_validation")
    if not isinstance(validation, dict) or inputs.get(
        "research_authority_validation_digest"
    ) != _digest(validation):
        raise ValueError(
            "repair_case_digest_mismatch: Research authority validation changed"
        )
    try:
        from chem_agent_contracts.v2 import ResearchActionPackageV2

        parsed = ResearchActionPackageV2.model_validate(package)
    except Exception as exc:
        raise ValueError(
            f"repair_case_invalid: frozen Research V2 package failed schema/hash: {exc}"
        ) from exc
    if validation.get("research_contract_hash") != parsed.research_contract_hash:
        raise ValueError(
            "repair_case_digest_mismatch: validated Research contract hash changed"
        )
    if validation.get("validation_rule") != RESEARCH_AUTHORITY_VALIDATION_RULE:
        raise ValueError(
            "contract_mismatch: Research authority validation rule changed"
        )
    validation_mode = validation.get("validation_mode")
    expected_consistency = _RESEARCH_AUTHORITY_VALIDATION_MODES.get(
        validation_mode
    )
    if expected_consistency is None:
        raise ValueError(
            "repair_case_invalid: unknown Research authority validation mode"
        )
    actual_consistency = (
        validation.get("raw_mirror_consistency"),
        validation.get("raw_to_canonical_consistency"),
    )
    if actual_consistency != expected_consistency:
        raise ValueError(
            "repair_case_invalid: Research authority validation mode and "
            "mirror-consistency claims disagree"
        )
    source_kind = validation.get("source_kind")
    allowed_source_kinds = {
        "package_only_schema_hash": {"bare_package", "canonical_mirror_only"},
        "handoff_mirror_rebuild": {"handoff_only"},
        "full_state_mirror_rebuild": {"research_state"},
    }
    if source_kind not in allowed_source_kinds[validation_mode]:
        raise ValueError(
            "repair_case_invalid: Research authority source kind and "
            "validation mode disagree"
        )
    canonical_paths = validation.get("canonical_mirror_paths")
    raw_paths = validation.get("raw_mirror_paths")
    if not isinstance(canonical_paths, list) or not isinstance(raw_paths, list):
        raise ValueError(
            "repair_case_invalid: Research authority mirror inventory is missing"
        )
    if source_kind == "bare_package" and (canonical_paths or raw_paths):
        raise ValueError(
            "repair_case_invalid: bare package cannot claim persisted mirrors"
        )
    if source_kind == "canonical_mirror_only" and (
        not canonical_paths or raw_paths
    ):
        raise ValueError(
            "repair_case_invalid: canonical-only authority mirror inventory "
            "is inconsistent"
        )
    if source_kind in {"handoff_only", "research_state"} and not raw_paths:
        raise ValueError(
            "repair_case_invalid: raw Research mirror inventory is missing"
        )
    if inputs.get("relationship_bindings_digest") != _digest(bindings):
        raise ValueError("repair_case_digest_mismatch: relationship bindings changed")
    if inputs.get("compiler_rule") != MATERIAL_RELATIONSHIP_COMPILER_RULE_ID:
        raise ValueError("contract_mismatch: material compiler rule changed")
    if inputs.get("workstation_truth_digest") and not str(
        inputs.get("full_plan_audit_context_digest") or ""
    ).strip():
        raise ValueError(
            "repair_case_invalid: full Plan audit context digest is missing"
        )
    return inputs


_TYPED_STEP_SELECTOR_PREFIX = "typed:"


def _plan_step_key(value: Any, path: str = "plan_step") -> Tuple[str, Any]:
    """Use the shared opaque-scalar identity contract for plan-step IDs."""

    try:
        return macro_id_key(value, path)
    except MacroIdentityError as exc:
        raise ValueError(str(exc)) from exc


def _plan_step_identity_token(value: Any) -> Tuple[str, Any]:
    """Comparable token for audit identities, including malformed input."""

    try:
        return _plan_step_key(value)
    except ValueError:
        return (f"invalid:{type(value).__name__}", _digest(value))


def _plan_step_ref(value: Any) -> str:
    """Format a type-preserving selector for one scalar plan-step ID.

    Integer selectors retain the historical readable syntax.  Other valid JSON
    scalar IDs use a hex-encoded JSON payload so punctuation cannot interfere
    with the path grammar and ``1`` remains distinct from ``"1"``.
    """

    try:
        normalized = normalize_macro_id(value, "plan_step")
    except MacroIdentityError as exc:
        raise ValueError(str(exc)) from exc
    if isinstance(normalized, int):
        selector = str(normalized)
    else:
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        selector = _TYPED_STEP_SELECTOR_PREFIX + payload.hex()
    return f"device_plan[step={selector}]"


def _diagnostic_step_ref(value: Any) -> str:
    """Return an unresolvable marker instead of guessing an invalid ID."""

    try:
        return _plan_step_ref(value)
    except ValueError:
        return (
            "device_plan[step=invalid:"
            f"{type(value).__name__}:{_digest(value)[:12]}]"
        )


def _parse_plan_step_selector(selector: str) -> Any:
    try:
        if selector.startswith(_TYPED_STEP_SELECTOR_PREFIX):
            encoded = selector[len(_TYPED_STEP_SELECTOR_PREFIX) :]
            payload = bytes.fromhex(encoded).decode("utf-8")
            value = json.loads(payload)
        else:
            # Historical selectors were unquoted JSON integers.  Parsing them
            # (instead of coercing with int/str) keeps zero and rejects "01".
            value = json.loads(selector)
        return normalize_macro_id(value, "plan_step selector")
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise KeyError(f"invalid typed plan-step selector {selector!r}") from exc


def _resolve_plan_step(
    steps: Any, wanted: Any, *, path: str
) -> Tuple[int, Dict[str, Any]]:
    """Resolve exactly one typed plan-step identity or fail closed."""

    if not isinstance(steps, list):
        raise KeyError(f"{path} has no device_plan list")
    try:
        wanted_key = _plan_step_key(wanted, path)
    except ValueError as exc:
        raise KeyError(str(exc)) from exc
    matches: List[Tuple[int, Dict[str, Any]]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise KeyError(f"device_plan[{index}] is not an object")
        try:
            candidate_key = _plan_step_key(
                step.get("plan_step"), f"device_plan[{index}].plan_step"
            )
        except ValueError as exc:
            raise KeyError(str(exc)) from exc
        if candidate_key == wanted_key:
            matches.append((index, step))
    if not matches:
        raise KeyError(f"path step {wanted!r} not found in device_plan")
    if len(matches) != 1:
        raise KeyError(f"path step {wanted!r} is ambiguous in device_plan")
    return matches[0]


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recipe_contract_version() -> str:
    """Read the contract version at call time so tests can simulate drift."""
    return _single_agent.RECIPE_FIELD_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# RepairCase persistence
# ---------------------------------------------------------------------------


def create_repair_case(
    candidate: Dict[str, Any],
    diagnosis: List[Dict[str, Any]],
    *,
    case_id: Optional[str] = None,
    patch_limit: int = DEFAULT_PATCH_LIMIT,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    provenance: Optional[Dict[str, Any]] = None,
    research_authority: Optional[Dict[str, Any]] = None,
    relationship_bindings: Optional[Dict[str, Any]] = None,
    workstation_truth_digest: str = "",
    full_plan_audit_context_digest: str = "",
) -> Dict[str, Any]:
    """Open a repair case around a candidate that failed validation.

    The baseline is preserved verbatim — including its illegality — for
    comparison, rollback and audit.  Saving a baseline never approves it.
    ``provenance`` records where an imported candidate came from; an imported
    candidate is always an unverified draft, never a cache hit.
    """
    baseline = copy.deepcopy(candidate)
    case = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id or uuid.uuid4().hex,
        "created_at": _utc_now(),
        "provenance": copy.deepcopy(provenance) if provenance else {},
        "contracts": {
            "recipe_contract_version": recipe_contract_version(),
            "repair_handlers": {
                WRAPPER_RECIPE_HANDLER_ID: WRAPPER_RECIPE_HANDLER_VERSION,
                TARGET_RECIPE_HANDLER_ID: TARGET_RECIPE_HANDLER_VERSION,
                "deterministic.recipe_scalar_adapter": (
                    CONTRACT_MASS_FIELD_ADAPTER_VERSION
                ),
                MATERIAL_EPISODE_EVIDENCE_HANDLER_ID: (
                    MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION
                ),
                MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID: (
                    MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION
                ),
            },
            "recipe_contract": {
                key: (
                    sorted(value)
                    if isinstance(value, (set, frozenset))
                    else copy.deepcopy(value)
                )
                for key, value in RECIPE_FIELD_CONTRACT.items()
            },
        },
        "baseline": {
            "candidate_digest": _digest(baseline),
            "candidate_digest_algorithm": DIGEST_ALGORITHM,
            "candidate_digest_scope": DIGEST_SCOPE_CANONICAL_JSON,
            "candidate": baseline,
            "diagnosis": copy.deepcopy(diagnosis),
            "captured_at": _utc_now(),
        },
        "draft": {
            "version": 1,
            "parent_version": None,
            "candidate": copy.deepcopy(candidate),
            "candidate_digest": _digest(candidate),
            "candidate_digest_algorithm": DIGEST_ALGORITHM,
            "candidate_digest_scope": DIGEST_SCOPE_CANONICAL_JSON,
            "status": "working",
            "diagnosis": copy.deepcopy(diagnosis),
            "updated_at": _utc_now(),
        },
        "progress": {"verified": [], "open_issues": []},
        "history_constraints": [],
        "log": [],
        "budgets": {
            "patches_used": 0,
            "rounds_used": 0,
            "patch_limit": patch_limit,
            "max_rounds": max_rounds,
        },
        "stop_reason": "",
    }
    material_inputs = _material_repair_inputs(
        research_authority,
        relationship_bindings,
        workstation_truth_digest=workstation_truth_digest,
        full_plan_audit_context_digest=full_plan_audit_context_digest,
    )
    if material_inputs:
        case["material_relationship_repair"] = material_inputs
    return case


def save_repair_case(case: Dict[str, Any], path: Path) -> None:
    _atomic_write_json(path, case)


def load_repair_case(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _log_round(case: Dict[str, Any], entry: Dict[str, Any]) -> None:
    entry = dict(entry)
    entry.setdefault("at", _utc_now())
    entry["round"] = len(case["log"]) + 1
    case["log"].append(entry)


# ---------------------------------------------------------------------------
# diagnose_candidate
# ---------------------------------------------------------------------------


def _contract_shape_finding(path: str, expected: str, actual: Any) -> Dict[str, Any]:
    """Turn malformed validator/candidate structure into a blocking finding."""

    return {
        "type": "invalid_contract_shape",
        "path": path,
        "message": f"{path} must be {expected}; got {type(actual).__name__}",
        "details": {
            "path": path,
            "expected": expected,
            "actual_type": type(actual).__name__,
        },
    }


def diagnose_candidate(
    auditor: _AUDITOR, candidate: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Run the validator and normalize findings to field-level diagnostics."""
    raw_findings = auditor(candidate)
    if raw_findings is None:
        findings: List[Any] = []
    elif not isinstance(raw_findings, list):
        return [
            _contract_shape_finding(
                "auditor.findings", "an array of finding objects", raw_findings
            )
        ]
    else:
        findings = raw_findings
    normalized: List[Dict[str, Any]] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            normalized.append(
                _contract_shape_finding(
                    f"auditor.findings[{index}]", "a finding object", finding
                )
            )
            continue
        item = copy.deepcopy(finding)
        item.setdefault("type", "plan_level_finding")
        item.setdefault("message", str(finding))
        normalized.append(item)
    return prepare_findings_for_repair(candidate, normalized)


def recipe_auditor(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Offline deterministic auditor for the file-dosing recipe contract."""
    findings: List[Dict[str, Any]] = []
    raw_plan = candidate.get("device_plan")
    if raw_plan is None:
        plan: List[Any] = []
    elif not isinstance(raw_plan, list):
        return [
            _contract_shape_finding(
                "device_plan", "an array of Device plan step objects", raw_plan
            )
        ]
    else:
        plan = raw_plan
    for index, step in enumerate(plan):
        if not isinstance(step, dict):
            findings.append(
                _contract_shape_finding(
                    f"device_plan[{index}]", "a Device plan step object", step
                )
            )
            continue
        if not recipe_step_is_file_dosing(step):
            continue
        key_values = step.get("key_values")
        wrapper_fields = (
            sorted(key for key in _WRAPPER_SOURCE_KEYS if key in key_values)
            if isinstance(key_values, dict)
            else []
        )
        if wrapper_fields:
            # A structured row does not make a coexisting wrapper harmless:
            # both representations must be reconciled or the stale one could
            # contradict the executable recipe.  Route every complete or
            # partial wrapper through the same strict adapter.
            details = {
                "contract_version": recipe_contract_version(),
                "plan_step": step.get("plan_step"),
                "path": f"{_diagnostic_step_ref(step.get('plan_step'))}.key_values",
                "declared": True,
                "checks": [
                    {
                        "field": "recipe_wrapper",
                        "path": "key_values",
                        "rule": "legacy_text_recipe",
                        "expected": "complete, internally consistent wrapper schema",
                        "actual": wrapper_fields,
                        "actual_type": "object",
                        "status": "failed",
                    }
                ],
            }
        else:
            details = recipe_step_diagnostics(step)
        if details is None:
            continue
        details = copy.deepcopy(details)
        # The shared diagnostic predates typed plan-step identities and builds
        # its path through string interpolation.  Replace that presentation
        # path with this module's typed selector without changing the check.
        details["plan_step"] = step.get("plan_step")
        details["path"] = f"{_diagnostic_step_ref(step.get('plan_step'))}.key_values"
        findings.append(
            {
                "type": "missing_concrete_recipe_evidence",
                "plan_step": step.get("plan_step"),
                "message": (
                    f"{_diagnostic_step_ref(step.get('plan_step'))} "
                    f"{RECIPE_FINDING_MESSAGE}"
                ),
                "details": details,
            }
        )
    return findings


def failed_checks(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten every failed check across findings (never short-circuited)."""
    checks: List[Dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        details = finding.get("details")
        if not isinstance(details, dict):
            continue
        raw_checks = details.get("checks")
        if not isinstance(raw_checks, list):
            continue
        for check in raw_checks:
            if isinstance(check, dict) and check.get("status") == "failed":
                item = dict(check)
                item.setdefault("plan_step", details.get("plan_step"))
                item.setdefault("contract_version", details.get("contract_version"))
                checks.append(item)
    return checks


def _findings_signature(findings: List[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
    signature = []
    for check in failed_checks(findings):
        signature.append(
            (
                _plan_step_identity_token(check.get("plan_step")),
                check.get("field"),
                check.get("rule"),
                type(check.get("actual")).__name__,
                str(check.get("actual"))[:80],
            )
        )
    for finding in findings:
        if not failed_checks([finding]):
            signature.append((finding.get("type"), str(finding.get("message"))[:120]))
    # Sorting is presentation only; the signature itself retains typed IDs.
    return tuple(sorted(signature, key=lambda item: tuple(str(p) for p in item)))


# ---------------------------------------------------------------------------
# build_repair_context
# ---------------------------------------------------------------------------


def _failing_step_excerpts(
    candidate: Dict[str, Any], findings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Dependency-selected excerpts: only steps named by the diagnosis."""
    plan_steps = candidate.get("device_plan") or []
    step_ids: List[Any] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        details = finding.get("details")
        if not isinstance(details, dict):
            continue
        if details.get("plan_step") is not None:
            step_ids.append(details["plan_step"])
    excerpts: List[Dict[str, Any]] = []
    seen: set[Tuple[str, Any]] = set()
    for identifier in step_ids:
        try:
            identity = _plan_step_key(identifier)
        except ValueError:
            # An invalid/non-scalar diagnosis target is deliberately not
            # matched to any live step.  Its patch path remains unresolvable.
            continue
        if identity in seen:
            continue
        seen.add(identity)
        try:
            _, step = _resolve_plan_step(
                plan_steps, identifier, path=f"diagnosis plan_step {identifier!r}"
            )
        except (KeyError, ValueError, TypeError):
            continue
        excerpts.append(
            {
                "step_ref": _plan_step_ref(step.get("plan_step")),
                "plan_step": step.get("plan_step"),
                "workstation": step.get("workstation"),
                "operation_intent": step.get("operation_intent"),
                "key_values": copy.deepcopy(step.get("key_values", {})),
                "containers": copy.deepcopy(step.get("containers", {})),
                "sample_lineage": copy.deepcopy(step.get("sample_lineage", {})),
            }
        )
    return excerpts


def build_repair_context(case: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble this round's model-facing view from the repair case.

    Facts and draft data are explicitly separated; the excerpt contains only
    the steps the current diagnosis depends on — never the whole plan, the
    chat history, or environment secrets.
    """
    draft = case["draft"]
    findings = draft.get("diagnosis") or []
    contract_version = case["contracts"]["recipe_contract_version"]
    history = [
        entry["statement"]
        for entry in case.get("history_constraints", [])
        if entry.get("contract_version") == contract_version
    ]
    errors = []
    for check in failed_checks(findings):
        errors.append(
            {
                "step_ref": _diagnostic_step_ref(check.get("plan_step")),
                "path": check.get("path"),
                "field": check.get("field"),
                "rule": check.get("rule"),
                "repair_rule": check.get("repair_rule"),
                "repair_rule_version": check.get("repair_rule_version"),
                "handler_id": check.get("handler_id"),
                "repair_disposition": check.get("repair_disposition"),
                "repair_reasons": copy.deepcopy(check.get("repair_reasons") or []),
                "expected": check.get("expected"),
                "actual": check.get("actual"),
                "actual_type": check.get("actual_type"),
            }
        )
    context = {
        "data_notice": (
            "当前草稿为待验证的模型输出，不是已批准的执行事实；"
            "合同与权限来自程序控制来源。"
        ),
        "task": (
            "在已注册的确定性配方处理器范围内修复当前配方表示或 JSON 值类型。"
            "不重新生成完整设备计划，不重新审查工作站，不改变实验方案。"
        ),
        "target": {
            "step_refs": sorted({error["step_ref"] for error in errors}),
            "draft_version": draft.get("version"),
        },
        "permissions": {
            "allowed": [
                "仅修改诊断指出的字段值类型（数字字符串 → JSON number）",
                "当严格处理器已逐项核对瓶号、实际容器、质量、料罐和谱系端点时，"
                "可将同一步骤的旧配方表示原子替换为合同规定的 key_values 表示",
                "保持数值大小与对象映射不变",
            ],
            "forbidden": [
                "Research 方案、冻结样品矩阵、物料身份",
                "瓶位映射与其他计划步骤",
                "校验器、技能合同与别名表",
            ],
        },
        "contract": {
            "name": "file_dosing_recipe",
            "version": contract_version,
            "fields": {
                "bottle": sorted(RECIPE_FIELD_CONTRACT["bottle_keys"]),
                "mass": sorted(RECIPE_FIELD_CONTRACT["mass_keys"]),
                "hopper": sorted(RECIPE_FIELD_CONTRACT["hopper_keys"]),
                "wrappers": sorted(RECIPE_FIELD_CONTRACT["wrapper_keys"]),
            },
            "value_rules": copy.deepcopy(RECIPE_FIELD_CONTRACT["value_rules"]),
        },
        "draft_excerpt": _failing_step_excerpts(
            draft.get("candidate", {}), findings
        ),
        "field_errors": errors,
        "open_findings": [
            {
                "type": finding.get("type"),
                "message": finding.get("message"),
                "repair_route": copy.deepcopy(finding.get("repair_route") or {}),
            }
            for finding in findings
        ],
        "verified_progress": list(case.get("progress", {}).get("verified", [])),
        "open_issues": list(case.get("progress", {}).get("open_issues", [])),
        "history_constraints": history,
    }
    return context


def context_manifest(context: Dict[str, Any]) -> Dict[str, str]:
    """Content hashes of each context section for the append-only log."""
    return {
        key: _digest(value)
        for key, value in sorted(context.items())
        if isinstance(value, (dict, list, str))
    }


# ---------------------------------------------------------------------------
# path grammar: device_plan[step=N].key_values.配方行[0].瓶号
# ---------------------------------------------------------------------------


def _parse_path(path: str) -> List[Tuple[str, Any]]:
    segments: List[Tuple[str, Any]] = []
    for part in path.split("."):
        while "[" in part:
            head, rest = part.split("[", 1)
            index_text, part = rest.split("]", 1)
            if head:
                segments.append(("key", head))
            if index_text.startswith("step="):
                segments.append(("step", index_text[len("step=") :]))
            else:
                segments.append(("index", int(index_text)))
        if part:
            segments.append(("key", part))
    return segments


def resolve_path(
    candidate: Dict[str, Any], path: str
) -> Tuple[Any, Any, List[Tuple[str, Any]]]:
    """Resolve a path to (container, final_key, resolved_segments)."""
    segments = _parse_path(path)
    node: Any = candidate
    resolved: List[Tuple[str, Any]] = []
    for position, (kind, value) in enumerate(segments):
        last = position == len(segments) - 1
        if kind == "step":
            steps = node.get("device_plan") if isinstance(node, dict) else node
            wanted = _parse_plan_step_selector(value)
            match = _resolve_plan_step(steps, wanted, path=f"path step {value!r}")
            node = match[1]
            resolved.append(("step_index", match[0]))
            continue
        if kind == "key":
            if not isinstance(node, dict) or value not in node:
                raise KeyError(f"path key {value!r} not found")
            if last:
                return node, value, resolved
            node = node[value]
            resolved.append(("key", value))
            continue
        # list index
        if not isinstance(node, list) or not (0 <= int(value) < len(node)):
            raise KeyError(f"path index {value!r} out of range")
        if last:
            return node, int(value), resolved
        node = node[int(value)]
        resolved.append(("index", int(value)))
    raise KeyError(f"path {path!r} does not select a value")


def canonical_path(candidate: Dict[str, Any], path: str) -> str:
    """Resolve step selectors to stable index-based paths for diffing.

    The output format matches ``_diff_paths`` exactly so permission checks
    compare like with like.
    """
    segments = _parse_path(path)
    node: Any = candidate
    chunks: List[str] = []
    for kind, value in segments:
        if kind == "step":
            steps = node.get("device_plan") if isinstance(node, dict) else node
            wanted = _parse_plan_step_selector(value)
            index, step = _resolve_plan_step(
                steps, wanted, path=f"path step {value!r}"
            )
            chunk = f"device_plan[{index}]"
            if chunks and chunks[-1] == "device_plan":
                chunks[-1] = chunk
            else:
                chunks.append(chunk)
            node = step
            continue
        if kind == "key":
            chunks.append(str(value))
            node = node[value] if isinstance(node, dict) else None
            continue
        if not chunks:
            raise KeyError(f"path index {value!r} without container")
        chunks[-1] = f"{chunks[-1]}[{value}]"
        node = node[int(value)] if isinstance(node, list) else None
    return ".".join(chunks)


def _diff_paths(before: Any, after: Any, prefix: str = "") -> List[str]:
    paths: List[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[key], after[key], child))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{prefix}[{index}]"
            if index >= len(before) or index >= len(after):
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[index], after[index], child))
        return paths
    if before != after:
        paths.append(prefix)
    return paths


# ---------------------------------------------------------------------------
# propose_repair
# ---------------------------------------------------------------------------


_NUMERIC_STRING = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)$")
_INTEGER_STRING = re.compile(r"^\d+$")


def _coerce_numeric_string(value: Any, rule: str) -> Optional[Any]:
    """Lossless numeric-string -> JSON number coercion, or None if unsafe."""
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    text = value.strip()
    pattern = _INTEGER_STRING if rule == "json_integer" else _NUMERIC_STRING
    if not pattern.match(text):
        return None
    try:
        number: Any = int(text) if rule == "json_integer" else float(text)
    except ValueError:  # pragma: no cover - regex already guarded
        return None
    import math

    if isinstance(number, float) and not math.isfinite(number):
        return None
    if number <= 0:
        return None
    if rule == "json_integer" and isinstance(number, float):
        return None
    return number


# ---------------------------------------------------------------------------
# Strict unstructured-wrapper -> recipe-contract adapter (0-token)
#
# Some producers serialize a file-dosing recipe as four mutually dependent
# wrapper fields rather than the contract's structured row array.  This
# adapter is experiment-agnostic: it requires the complete wrapper schema,
# validates every cross-reference, and fails closed on unknown prose,
# conflicts, ambiguity, or unit mismatch.  Nothing is inferred from an
# experiment id, sample id, material name, or previously observed artifact.
# ---------------------------------------------------------------------------

STRUCTURED_RECIPE_ADAPTER_RULE = "structured_recipe_schema_adapter"
STRUCTURED_RECIPE_ADAPTER_VERSION = "structured-recipe-schema-adapter/v1"
WRAPPER_RECIPE_HANDLER_ID = "deterministic.wrapper_recipe_adapter"
TARGET_RECIPE_HANDLER_ID = "deterministic.target_recipe_row_adapter"
WRAPPER_RECIPE_HANDLER_VERSION = "wrapper-recipe-adapter/v1"
TARGET_RECIPE_HANDLER_VERSION = "target-recipe-row-adapter/v1"
MATERIAL_EPISODE_EVIDENCE_HANDLER_ID = (
    "deterministic.material_episode_evidence_gate"
)
MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION = "material-episode-evidence-gate/v2"
MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID = (
    "deterministic.material_relationship_compiler"
)
MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION = (
    "material-relationship-compiler/v1"
)
_WRAPPER_MAPPING_ROW = re.compile(r"文件瓶号(\d+)=容器(\d+)")
_WRAPPER_HOPPER_UNIFORM = re.compile(r"^单文件料罐号全局统一=(\d+)$")
_WRAPPER_CONTAINER_LIST = re.compile(r"^\[(\d+(?:\s*,\s*\d+)*)?\]$")
_WRAPPER_SOURCE_KEYS = ("上传文件CSV表头", "上传文件CSV行", "瓶号映射", "料罐号约束")
_AMBIGUITY_MARKERS = (
    "适量", "按比例", "根据实测", "按实测", "运行时确定", "待定", "约", "~",
)


def _contains_ambiguity(*values: Any) -> bool:
    for value in values:
        if isinstance(value, str) and any(m in value for m in _AMBIGUITY_MARKERS):
            return True
        if isinstance(value, list) and _contains_ambiguity(*value):
            return True
    return False


def _strict_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        number = _coerce_numeric_string(value, "json_integer")
        return int(number) if number is not None else None
    return None


def _adapt_wrapper_recipe_step(
    step: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Adapt one complete wrapper-schema recipe step to contract form.

    Returns ``(new_key_values, reasons)``; ``new_key_values`` is ``None``
    unless the whole representation converts unambiguously and all
    cross-checks (header order, row parsing, bottle↔container bijection,
    uniform hopper, container list/count) pass.
    """
    reasons: List[str] = []
    key_values = step.get("key_values")
    if not isinstance(key_values, dict):
        return None, ["key_values 缺失或不是对象"]
    header = key_values.get("上传文件CSV表头")
    csv_rows = key_values.get("上传文件CSV行")
    mapping_text = key_values.get("瓶号映射")
    hopper_text = key_values.get("料罐号约束")
    for name, value in (
        ("上传文件CSV表头", header),
        ("上传文件CSV行", csv_rows),
        ("瓶号映射", mapping_text),
        ("料罐号约束", hopper_text),
    ):
        if value in (None, ""):
            reasons.append(f"wrapper schema 字段 {name} 缺失")
    if reasons:
        return None, reasons
    if _contains_ambiguity(header, csv_rows, mapping_text, hopper_text):
        return None, ["包含歧义标记（适量/按比例/按实测等），拒绝自动物化"]

    columns = [_recipe_normalized_key(c) for c in str(header).split(",")]
    if columns != ["瓶号", "加样量(g)", "料罐号"]:
        return None, [f"CSV表头不是标准的 瓶号,加样量(g),料罐号 顺序: {header!r}"]
    if not isinstance(csv_rows, list) or not csv_rows:
        return None, ["上传文件CSV行 不是非空列表"]

    rows: List[Tuple[int, Any, int, str]] = []  # bottle, mass, hopper, raw
    for raw in csv_rows:
        if not isinstance(raw, str):
            return None, [f"CSV 行不是字符串: {raw!r}"]
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            return None, [f"CSV 行列数不为 3: {raw!r}"]
        bottle = _strict_positive_int(parts[0])
        mass_result = normalize_contract_scalar(parts[1], "number", "g")
        mass = mass_result.new_value if mass_result.accepted else None
        hopper = _strict_positive_int(parts[2])
        if (
            bottle is None
            or mass is None
            or isinstance(mass, bool)
            or not isinstance(mass, (int, float))
            or mass <= 0
            or hopper is None
        ):
            return None, [
                "CSV 行无法按显式 bottle:number / mass:number[g] / "
                f"hopper:number 合同解析（拒绝猜测）: {raw!r}; "
                f"mass_reason={mass_result.reason}"
            ]
        rows.append((bottle, mass, hopper, raw))

    if not isinstance(mapping_text, str):
        return None, ["瓶号映射 不是字符串"]
    mapping: Dict[int, int] = {}
    for bottle_s, container_s in _WRAPPER_MAPPING_ROW.findall(mapping_text):
        bottle_id, container_id = int(bottle_s), int(container_s)
        if bottle_id in mapping:
            return None, [f"瓶号映射中瓶号 {bottle_id} 重复"]
        mapping[bottle_id] = container_id
    leftover = _WRAPPER_MAPPING_ROW.sub("", mapping_text)
    if leftover.replace("；", "").replace(";", "").strip():
        return None, [f"瓶号映射含无法解析的内容: {mapping_text!r}"]
    csv_bottles = [row[0] for row in rows]
    if sorted(mapping) != sorted(csv_bottles):
        return None, [
            f"瓶号映射与 CSV 行瓶号不构成一一对应: 映射{sorted(mapping)} vs 行{sorted(csv_bottles)}"
        ]
    containers = [mapping[b] for b in csv_bottles]
    if len(set(containers)) != len(containers):
        return None, ["瓶号映射中容器编号重复"]

    if not isinstance(hopper_text, str):
        return None, ["料罐号约束 不是字符串"]
    hopper_match = _WRAPPER_HOPPER_UNIFORM.match(hopper_text.strip())
    if not hopper_match:
        return None, [f"料罐号约束无法唯一解析: {hopper_text!r}"]
    uniform_hopper = int(hopper_match.group(1))
    if any(row[2] != uniform_hopper for row in rows):
        return None, [f"CSV 行料罐号与统一约束 {uniform_hopper} 不一致"]

    declared_containers = key_values.get("容器编号")
    if isinstance(declared_containers, str):
        match = _WRAPPER_CONTAINER_LIST.match(declared_containers.strip())
        if not match:
            return None, [f"容器编号无法唯一解析: {declared_containers!r}"]
        body = match.group(1) or ""
        container_list = [int(x) for x in body.split(",") if x.strip()] if body else []
    elif isinstance(declared_containers, list):
        container_list = [_strict_positive_int(x) for x in declared_containers]
        if any(x is None for x in container_list):
            return None, [f"容器编号含非正整数元素: {declared_containers!r}"]
    else:
        return None, [f"容器编号形式不支持: {declared_containers!r}"]
    if sorted(container_list) != sorted(containers):
        return None, [
            f"容器编号 {sorted(container_list)} 与瓶号映射容器 {sorted(containers)} 不一致"
        ]

    declared_count = _strict_positive_int(key_values.get("容器数量"))
    if declared_count is None or declared_count != len(rows):
        return None, [f"容器数量与配方行数 {len(rows)} 不一致: {key_values.get('容器数量')!r}"]

    structured_rows = [
        {
            "瓶号": bottle,
            "实际容器编号": mapping[bottle],
            "加样量(g)": mass,
            "料罐号": hopper,
        }
        for bottle, mass, hopper, _raw in rows
    ]
    csv_content = "\n".join(
        [str(header).strip()] + [raw.strip() for _b, _m, _h, raw in rows]
    )
    derived_fields = {
        "上传文件逐瓶配方": structured_rows,
        "上传文件CSV内容": csv_content,
    }
    for field, derived in derived_fields.items():
        if field in key_values and key_values[field] != derived:
            return None, [f"既存目标字段 {field} 与 wrapper schema 推导值冲突"]

    new_key_values = copy.deepcopy(key_values)
    for key in _WRAPPER_SOURCE_KEYS:
        new_key_values.pop(key, None)
    new_key_values.update(derived_fields)
    new_key_values["容器编号"] = containers
    new_key_values["容器数量"] = len(rows)
    return new_key_values, []


# A second, equally strict legacy representation is emitted by some Device
# plan producers as one or more dynamic ``目标瓶N`` fields.  It is not accepted
# as executable recipe data until every value can be parsed exactly and agrees
# with both the step container binding and the structured sample-lineage edge.
_TARGET_RECIPE_KEY = re.compile(r"^目标瓶\s*(\d+)$")
_TARGET_RECIPE_ROW = re.compile(
    r"^CSV行\s*[:：]\s*瓶号\s*(\d+)\s*"
    r"[（(]\s*对应容器(?:编号)?\s*(\d+)\s*[）)]\s*[,，]\s*"
    r"加样(?:量|质量)(?:\s*[（(]g[）)])?\s*[:：]?\s*"
    r"((?:\d+(?:\.\d+)?|\.\d+)\s*(?:g|G|克))\s*[,，]\s*"
    r"料(?:罐|斗)(?:号|编号)?\s*[:：]?\s*(\d+)\s*$"
)
_TARGET_NOMINAL_MASS_KEYS = ("名义剂量", "目标剂量", "名义加样量")
_TARGET_NOMINAL_MASS = re.compile(
    r"^\s*((?:\d+(?:\.\d+)?|\.\d+))\s*(mg|g|毫克|克)(?:\s+(.+?))?\s*$",
    re.IGNORECASE,
)
_AMBIGUOUS_NOMINAL_SUFFIX = re.compile(
    r"(?:\b(?:or|about|approx(?:imately)?)\b|或者|或|约|大约|±|~|～|/|"
    r"\d\s*[-–—]\s*\d|\d+(?:\.\d+)?\s*(?:mg|g|毫克|克)\b)",
    re.IGNORECASE,
)


def _target_nominal_mass_g(
    key_values: Dict[str, Any],
) -> Tuple[Optional[float], List[str]]:
    """Parse optional nominal-mass evidence without guessing its unit/value."""

    parsed: List[Tuple[str, float]] = []
    for key in _TARGET_NOMINAL_MASS_KEYS:
        if key not in key_values:
            continue
        value = key_values[key]
        if not isinstance(value, str):
            return None, [f"{key} 不是带显式单位的字符串"]
        match = _TARGET_NOMINAL_MASS.fullmatch(value)
        if match is None:
            return None, [f"{key} 无法唯一解析为 mg 或 g: {value!r}"]
        suffix = str(match.group(3) or "").strip()
        if suffix and _AMBIGUOUS_NOMINAL_SUFFIX.search(suffix):
            return None, [f"{key} 含第二数值、范围或歧义剂量: {value!r}"]
        try:
            mass = Decimal(match.group(1))
        except InvalidOperation:
            return None, [f"{key} 无法换算为有限正数 g: {value!r}"]
        unit = match.group(2).lower()
        if unit in {"mg", "毫克"}:
            mass /= Decimal("1000")
        elif unit not in {"g", "克"}:
            return None, [f"{key} 使用了不支持的质量单位: {value!r}"]
        mass_g = float(mass)
        if not _finite_positive(mass_g):
            return None, [f"{key} 无法换算为有限正数 g: {value!r}"]
        parsed.append((key, mass_g))
    if not parsed:
        return None, []
    first = parsed[0][1]
    if any(
        not math.isclose(value, first, rel_tol=0.0, abs_tol=1e-12)
        for _key, value in parsed[1:]
    ):
        return None, ["多个名义剂量字段的质量不一致"]
    return first, []


def _container_number(value: Any, *, prefixes: Tuple[str, ...]) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    prefix_pattern = "|".join(re.escape(prefix) for prefix in prefixes)
    match = re.fullmatch(rf"(?:(?:{prefix_pattern})\s*)?(\d+)", text)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _container_type_family(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if normalized in {"料斗", "料罐", "hopper", "solid_hopper"}:
        return "hopper"
    if normalized in {"进样瓶", "样品瓶", "vial", "sample_vial"}:
        return "sample_vial"
    return ""


def _adapt_target_recipe_step(
    step: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Convert exact ``目标瓶N: CSV行...`` evidence to typed rows.

    The dynamic text is treated only as source evidence.  No bottle, mass,
    hopper or container value is inferred: the key, embedded row, step
    container declaration and sample-lineage endpoints must agree exactly.
    """

    key_values = step.get("key_values")
    if not isinstance(key_values, dict):
        return None, ["key_values 缺失或不是对象"]
    target_items = [
        (key, value, _TARGET_RECIPE_KEY.fullmatch(str(key).strip()))
        for key, value in key_values.items()
        if _TARGET_RECIPE_KEY.fullmatch(str(key).strip()) is not None
    ]
    if not target_items:
        return None, ["未发现可识别的 目标瓶N 配方字段"]
    if _contains_ambiguity(*(value for _key, value, _match in target_items)):
        return None, ["目标瓶配方包含歧义标记，拒绝自动物化"]

    lineage = step.get("sample_lineage")
    if not isinstance(lineage, dict):
        return None, ["目标瓶配方缺少结构化 sample_lineage 交叉证据"]
    if lineage.get("trace_complete") is not True:
        return None, ["sample_lineage.trace_complete 不是 true"]
    if str(lineage.get("transfer_reason") or "").strip() != "solid_hopper":
        return None, ["sample_lineage.transfer_reason 不是 solid_hopper"]
    source_container = lineage.get("source_container")
    destination_container = lineage.get("destination_container")
    if not isinstance(source_container, dict) or not isinstance(
        destination_container, dict
    ):
        return None, ["sample_lineage 缺少结构化 source/destination_container"]
    if _container_type_family(source_container.get("container_type")) != "hopper":
        return None, ["sample_lineage.source_container 不是料斗/料罐"]
    if (
        _container_type_family(destination_container.get("container_type"))
        != "sample_vial"
    ):
        return None, ["sample_lineage.destination_container 不是进样瓶"]
    lineage_hopper = _container_number(
        source_container.get("container_id"), prefixes=("料罐", "料斗")
    )
    lineage_destination = _container_number(
        destination_container.get("container_id"),
        prefixes=("进样瓶", "样品瓶", "容器"),
    )
    if lineage_hopper is None or lineage_destination is None:
        return None, ["sample_lineage 的料罐或目标容器编号无法唯一解析"]

    containers = step.get("containers")
    if not isinstance(containers, dict):
        return None, ["步骤缺少结构化 containers"]
    if _container_type_family(containers.get("容器类型")) != "sample_vial":
        return None, ["containers.容器类型 不是进样瓶"]
    declared = containers.get("容器编号")
    if not isinstance(declared, list):
        return None, ["containers.容器编号 必须是数组"]
    declared_numbers = [
        _container_number(value, prefixes=("进样瓶", "样品瓶", "容器"))
        for value in declared
    ]
    if any(number is None for number in declared_numbers):
        return None, ["containers.容器编号 含无法唯一解析的编号"]

    rows: List[Dict[str, Any]] = []
    seen_bottles: set[int] = set()
    seen_containers: set[int] = set()
    for key, value, key_match in target_items:
        if not isinstance(value, str) or key_match is None:
            return None, [f"{key} 的 CSV 行不是字符串"]
        row_match = _TARGET_RECIPE_ROW.fullmatch(value.strip())
        if row_match is None:
            return None, [f"{key} 不符合严格 目标瓶 CSV 行语法"]
        target_from_key = int(key_match.group(1))
        bottle = int(row_match.group(1))
        target_from_row = int(row_match.group(2))
        mass_text = row_match.group(3)
        hopper = int(row_match.group(4))
        mass_result = normalize_contract_scalar(mass_text, "number", "g")
        mass = mass_result.new_value if mass_result.accepted else None
        if target_from_key != target_from_row:
            return None, [f"{key} 与行内对应容器 {target_from_row} 不一致"]
        if target_from_row != lineage_destination:
            return None, [
                f"{key} 与 sample_lineage 目标容器 {lineage_destination} 不一致"
            ]
        if hopper != lineage_hopper:
            return None, [
                f"{key} 的料罐号 {hopper} 与 sample_lineage 料罐 {lineage_hopper} 不一致"
            ]
        if bottle <= 0 or hopper <= 0 or mass is None or not _finite_positive(mass):
            return None, [
                f"{key} 的瓶号/质量/料罐号无法按显式合同解析；"
                f"mass_reason={mass_result.reason}"
            ]
        if bottle in seen_bottles or target_from_row in seen_containers:
            return None, ["目标瓶配方含重复瓶号或重复实际容器编号"]
        seen_bottles.add(bottle)
        seen_containers.add(target_from_row)
        rows.append(
            {
                "瓶号": bottle,
                "实际容器编号": target_from_row,
                "加样量(g)": mass,
                "料罐号": hopper,
            }
        )

    if sorted(number for number in declared_numbers if number is not None) != sorted(
        seen_containers
    ):
        return None, [
            "containers.容器编号 与目标瓶配方的实际容器集合不一致"
        ]
    if len(rows) != 1:
        # A single sample_lineage edge proves exactly one hopper→container
        # relation.  Multi-row text needs per-row lineage, not positional
        # guessing from one shared edge.
        return None, ["单个 sample_lineage 仅授权一个目标瓶配方行"]

    nominal_mass_g, nominal_reasons = _target_nominal_mass_g(key_values)
    if nominal_reasons:
        return None, nominal_reasons
    if nominal_mass_g is not None and not math.isclose(
        float(rows[0]["加样量(g)"]),
        nominal_mass_g,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        return None, [
            "目标瓶配方质量与步骤名义剂量不一致: "
            f"recipe={rows[0]['加样量(g)']!r} g, nominal={nominal_mass_g!r} g"
        ]

    csv_content = "\n".join(
        ["瓶号,加样量(g),料罐号"]
        + [f"{row['瓶号']},{row['加样量(g)']},{row['料罐号']}" for row in rows]
    )
    derived_fields = {
        "上传文件逐瓶配方": rows,
        "上传文件CSV内容": csv_content,
        "容器数量": len(rows),
        "容器编号": sorted(seen_containers),
    }
    for field, derived in derived_fields.items():
        if field in key_values and key_values[field] != derived:
            return None, [f"既存目标字段 {field} 与目标瓶证据推导值冲突"]
    new_key_values = copy.deepcopy(key_values)
    for key, _value, _match in target_items:
        new_key_values.pop(key, None)
    new_key_values.update(copy.deepcopy(derived_fields))
    # The immutable RepairCase baseline and patch log preserve the source
    # strings; they are deliberately not copied into executable-looking data.
    return new_key_values, []


def _finite_positive(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value > 0
        and value != float("inf")
        and value != float("-inf")
        and value == value
    )


def adapt_unstructured_recipe_step(
    step: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Dispatch an unstructured recipe only to a shape-matched adapter."""

    _handler_id, adapted, reasons = select_unstructured_recipe_adapter(step)
    return adapted, reasons


def select_unstructured_recipe_adapter(
    step: Dict[str, Any],
) -> Tuple[str, Optional[Dict[str, Any]], List[str]]:
    """Return the explicit handler selected by the candidate's exact shape."""

    key_values = step.get("key_values")
    if not isinstance(key_values, dict):
        return "", None, ["key_values 缺失或不是对象"]
    wrapper_present = any(key in key_values for key in _WRAPPER_SOURCE_KEYS)
    target_present = any(
        _TARGET_RECIPE_KEY.fullmatch(str(key).strip()) is not None
        for key in key_values
    )
    if wrapper_present and target_present:
        return "", None, ["同时存在 wrapper 与 目标瓶 配方表示，拒绝猜测权威来源"]
    if wrapper_present:
        adapted, reasons = _adapt_wrapper_recipe_step(step)
        return WRAPPER_RECIPE_HANDLER_ID, adapted, reasons
    if target_present:
        adapted, reasons = _adapt_target_recipe_step(step)
        return TARGET_RECIPE_HANDLER_ID, adapted, reasons
    return "", None, ["没有注册处理器可识别的非结构化配方表示"]


# ---------------------------------------------------------------------------
# Deterministic contract-mass field adaptation (0-token)
#
# An unstructured recipe row may use the bare key 加样量 with a
# scalar ``number + supported mass unit`` string.
# The contract wants a JSON number under a mass alias (加样量(g) etc.).
# The conversion is attempted only when the row is the diagnosed one, the
# source key is exactly 加样量 (normalized), the shared scalar normalizer
# accepts the value under an explicit number+g contract, no conflicting
# contract mass key exists, and no ambiguity marker is present.
# ---------------------------------------------------------------------------

CONTRACT_MASS_FIELD_ADAPTER_RULE = "contract_mass_field_adapter"
CONTRACT_MASS_FIELD_ADAPTER_VERSION = "contract-mass-field-adapter/v1"
_UNIT_MASS_SOURCE_KEY = "加样量"  # normalized exact-match source field
_CONTRACT_MASS_LABEL = "加样量(g)"
_NORMALIZED_MASS_KEYS = frozenset(
    _recipe_normalized_key(alias) for alias in RECIPE_FIELD_CONTRACT["mass_keys"]
)


def _is_contract_mass_adapter_check(check: Dict[str, Any]) -> bool:
    """Diagnosed shape this rule owns: contract mass key missing on a row."""
    return (
        check.get("rule") == "json_number"
        and check.get("field") == _CONTRACT_MASS_LABEL
        and check.get("actual") is None
        and isinstance(check.get("path"), str)
    )


def normalize_contract_mass_row(
    candidate: Dict[str, Any], step_ref: str, check: Dict[str, Any]
) -> Tuple[
    Optional[Tuple[str, Dict[str, Any], Dict[str, Any], str, Dict[str, Any]]],
    List[str],
]:
    """Normalize one diagnosed recipe row under the shared number+g contract.

    The row is replaced wholesale (test+replace on the diagnosed path), so
    bottle/hopper keys and unrelated fields pass through untouched.  Shared
    normalizer evidence is returned for the patch audit log.
    """
    if not _is_contract_mass_adapter_check(check):
        return None, ["不是可授权的 contract mass adapter 诊断形态"]
    path = f"{step_ref}.{check.get('path')}"
    try:
        container, key, _ = resolve_path(candidate, path)
    except (KeyError, AttributeError, TypeError) as exc:
        return None, [f"路径不可解析: {exc}"]
    row = container[key]
    if not isinstance(row, dict):
        return None, [f"路径未指向配方行对象: {type(row).__name__}"]

    source_keys = [
        row_key
        for row_key in row
        if _recipe_normalized_key(row_key) == _UNIT_MASS_SOURCE_KEY
    ]
    if len(source_keys) != 1:
        return None, [
            f"配方行中精确源字段 加样量 命中 {len(source_keys)} 个，拒绝猜测"
        ]
    source_key = source_keys[0]
    raw = row[source_key]
    if not isinstance(raw, str):
        return None, [
            f"加样量 值不是字符串（本 adapter 仅处理待解析标量文本）: {type(raw).__name__}"
        ]
    text = raw.strip()
    if _contains_ambiguity(text):
        return None, [f"含歧义标记（适量/约/按实测等），拒绝转换: {raw!r}"]
    scalar = normalize_contract_scalar(text, "number", "g")
    if not scalar.accepted:
        return None, [
            f"加样量 不满足 number+g 合同: {raw!r}; reason={scalar.reason}"
        ]
    number = scalar.new_value
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or number <= 0
    ):
        return None, [f"加样量必须为有限正数: {raw!r}"]
    evidence = {
        "rule_id": scalar.rule_id,
        "declared_type": scalar.declared_type,
        "declared_unit": scalar.declared_unit,
        "input_unit": scalar.input_unit,
        "canonical_unit": scalar.canonical_unit,
        "reason": scalar.reason,
    }

    # Conflict guard: a pre-existing contract mass key must agree exactly.
    existing_mass_key = next(
        (
            row_key
            for row_key in row
            if row_key != source_key
            and _recipe_normalized_key(row_key) in _NORMALIZED_MASS_KEYS
        ),
        None,
    )
    if existing_mass_key is not None:
        existing = row[existing_mass_key]
        existing_scalar = normalize_contract_scalar(existing, "number", "g")
        existing_number = (
            existing_scalar.new_value
            if existing_scalar.accepted
            and isinstance(existing, (int, float))
            and not isinstance(existing, bool)
            else None
        )
        if existing_number is None or existing_number != number:
            return None, [
                f"已存在合同质量键 {existing_mass_key}={existing!r} 与 {raw!r} 不一致，拒绝"
            ]
        # Values agree: dropping the duplicate source key is the whole fix.
        new_row = {
            row_key: copy.deepcopy(value)
            for row_key, value in row.items()
            if row_key != source_key
        }
        return (path, copy.deepcopy(row), new_row, raw, evidence), []

    # Insert the contract label at the source key's position; every other
    # key (瓶号/料罐号/容器编号/...) passes through unchanged.
    new_row = {}
    for row_key, value in row.items():
        if row_key == source_key:
            new_row[_CONTRACT_MASS_LABEL] = number
        else:
            new_row[row_key] = copy.deepcopy(value)
    return (path, copy.deepcopy(row), new_row, raw, evidence), []


def _finding_identity(finding: Dict[str, Any]) -> Tuple[Any, ...]:
    finding_id = str(finding.get("finding_id") or "").strip()
    if finding_id:
        return (str(finding.get("type")), "finding_id", finding_id)
    details = finding.get("details")
    if not isinstance(details, dict):
        details = {}
    plan_step = details.get("plan_step", finding.get("plan_step"))
    return (
        str(finding.get("type")),
        _plan_step_identity_token(plan_step),
        str(finding.get("message")),
    )


def _check_identity(check: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        _plan_step_identity_token(check.get("plan_step")),
        str(check.get("path")),
        str(check.get("field")),
        str(check.get("rule")),
    )


def _step_by_ref(candidate: Dict[str, Any], step_ref: str) -> Optional[Dict[str, Any]]:
    """Resolve one typed ``device_plan[step=...]`` reference exactly once."""

    match = re.fullmatch(r"device_plan\[step=([^\]]+)\]", str(step_ref))
    if not match:
        return None
    try:
        wanted = _parse_plan_step_selector(match.group(1))
        _, step = _resolve_plan_step(
            candidate.get("device_plan"), wanted, path=f"step_ref {step_ref!r}"
        )
        return step
    except KeyError:
        return None


def prepare_findings_for_repair(
    candidate: Dict[str, Any], findings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Bind validator findings to registered repair handlers.

    Finding type and repair strategy are intentionally separate contracts.
    The production auditor may keep its historical ``legacy_text_recipe``
    problem code; this dispatcher records which deterministic handler owns the
    finding, whether its input preconditions hold, and why it cannot run.
    Unrelated findings remain present so a partial recipe patch can never make
    the whole Device Plan appear accepted.
    """

    routed = copy.deepcopy(findings)
    for finding in routed:
        if not isinstance(finding, dict):
            continue
        if finding.get("type") == "frozen_material_transition_coverage_missing":
            # The production material auditor already distinguishes missing
            # Research evidence, missing/invalid Device bindings, runtime-guard
            # gaps, and compiler support gaps.  Do not collapse those trusted
            # non-patch verdicts back into a generic deterministic compiler
            # request merely because the Research relation itself is declared.
            existing_route = finding.get("repair_route")
            existing_status = (
                str(existing_route.get("status") or "").strip()
                if isinstance(existing_route, dict)
                else ""
            )
            if existing_status in {
                "evidence_insufficient",
                "handler_input_incompatible",
                "plan_missing_operation",
                "plan_binding_required",
                "repair_strategy_unavailable",
                "runtime_guard_missing",
                "software_unsupported",
            }:
                existing_route.setdefault(
                    "handler_id", MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID
                )
                existing_route.setdefault(
                    "handler_version",
                    MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION,
                )
                continue
            evidence = finding.get("material_episode_repair_evidence")
            if not isinstance(evidence, dict):
                finding["repair_route"] = {
                    "status": "handler_input_incompatible",
                    "handler_id": MATERIAL_EPISODE_EVIDENCE_HANDLER_ID,
                    "handler_version": MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION,
                    "reason": "transition finding lacks structured repair evidence",
                }
                continue
            optional_counts = {
                "material_inputs": evidence.get("material_input_count"),
                "material_intermediates": evidence.get(
                    "material_intermediate_count"
                ),
                "material_outputs": evidence.get("material_output_count"),
                "logical_containers": evidence.get("logical_container_count"),
                "material_relations": evidence.get("material_relation_count"),
            }
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in optional_counts.values()
                if value is not None
            ):
                finding["repair_route"] = {
                    "status": "handler_input_incompatible",
                    "handler_id": MATERIAL_EPISODE_EVIDENCE_HANDLER_ID,
                    "handler_version": MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION,
                    "reason": "transition repair evidence counts are malformed",
                }
                continue
            status = evidence.get("material_contract_status")
            status_fields = tuple(optional_counts)
            if status is None:
                status = {}
            if not isinstance(status, dict) or any(
                str(status.get(field) or "")
                not in {"declared", "not_applicable", "unresolved"}
                for field in status_fields
            ):
                finding["repair_route"] = {
                    "status": "handler_input_incompatible",
                    "handler_id": MATERIAL_EPISODE_EVIDENCE_HANDLER_ID,
                    "handler_version": MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION,
                    "reason": "Research material contract status is malformed",
                }
                continue
            relation_ids = evidence.get("material_relation_ids")
            if relation_ids is None:
                relation_ids = []
            if not isinstance(relation_ids, list) or any(
                not isinstance(value, str) or not value.strip()
                for value in relation_ids
            ):
                finding["repair_route"] = {
                    "status": "handler_input_incompatible",
                    "handler_id": MATERIAL_EPISODE_EVIDENCE_HANDLER_ID,
                    "handler_version": MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION,
                    "reason": "Research material relation identities are malformed",
                }
                continue
            declared_relation_count = optional_counts.get("material_relations")
            if declared_relation_count is not None and declared_relation_count != len(
                relation_ids
            ):
                finding["repair_route"] = {
                    "status": "handler_input_incompatible",
                    "handler_id": MATERIAL_EPISODE_EVIDENCE_HANDLER_ID,
                    "handler_version": MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION,
                    "reason": "Research material relation count disagrees with IDs",
                }
                continue
            unresolved = [
                field
                for field in status_fields
                if not str(status.get(field) or "")
                or str(status.get(field) or "") == "unresolved"
            ]
            if unresolved:
                finding["repair_route"] = {
                    "status": "evidence_insufficient",
                    "handler_id": MATERIAL_EPISODE_EVIDENCE_HANDLER_ID,
                    "handler_version": MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION,
                    "reason": (
                        "deterministic material repair cannot use unresolved "
                        "Research dimensions=" + ",".join(unresolved)
                    ),
                }
            elif status.get("material_relations") == "not_applicable":
                finding["repair_route"] = {
                    "status": "repair_strategy_unavailable",
                    "handler_id": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID,
                    "handler_version": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION,
                    "reason": (
                        "Research explicitly marks material relations not_applicable; "
                        "Device cannot synthesize a relationship to cover a missing operation"
                    ),
                }
            elif status.get("material_relations") != "declared" or not relation_ids:
                finding["repair_route"] = {
                    "status": "evidence_insufficient",
                    "handler_id": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID,
                    "handler_version": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION,
                    "reason": "no declared executable Research material relation",
                }
            else:
                finding["repair_route"] = {
                    "status": "deterministic_patch",
                    "handler_id": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID,
                    "handler_version": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION,
                    "reason": (
                        "explicit Research relations may be compiled only with exact "
                        "relation-to-step and logical-to-physical bindings"
                    ),
                }
            continue
        if finding.get("type") != "missing_concrete_recipe_evidence":
            continue
        details = finding.get("details")
        if not isinstance(details, dict):
            finding["repair_route"] = {
                "status": "handler_input_incompatible",
                "reason": "recipe finding details is not an object",
            }
            continue
        step_ref = _diagnostic_step_ref(
            details.get("plan_step", finding.get("plan_step"))
        )
        step = _step_by_ref(candidate, step_ref)
        if step is None:
            finding["repair_route"] = {
                "status": "handler_input_incompatible",
                "handler_id": "",
                "reason": "diagnosed plan step is absent or has invalid identity",
            }
            continue

        route_status = "repair_strategy_unavailable"
        route_reasons: List[str] = []
        route_priority = {
            "repair_strategy_unavailable": 0,
            "evidence_insufficient": 1,
            "handler_input_incompatible": 2,
            "deterministic_patch": 3,
        }
        checks = details.get("checks")
        if not isinstance(checks, list):
            finding["repair_route"] = {
                "status": "handler_input_incompatible",
                "handler_id": "",
                "reason": "recipe finding checks is not an array",
            }
            continue
        for check in checks:
            if not isinstance(check, dict) or check.get("status") != "failed":
                continue
            rule = check.get("rule")
            if rule == "legacy_text_recipe":
                # Explicit registry edge: problem type/rule -> deterministic
                # strategy.  Adapter preflight still decides whether this
                # particular evidence is sufficient; no value is guessed.
                check["repair_rule"] = STRUCTURED_RECIPE_ADAPTER_RULE
                check["repair_rule_version"] = STRUCTURED_RECIPE_ADAPTER_VERSION
                handler_id, adapted, reasons = select_unstructured_recipe_adapter(
                    step
                )
                check["handler_id"] = handler_id
                if adapted is not None:
                    check["repair_disposition"] = "deterministic_patch"
                    route_status = "deterministic_patch"
                else:
                    key_values = step.get("key_values")
                    wrapper_present = isinstance(key_values, dict) and any(
                        key in key_values for key in _WRAPPER_SOURCE_KEYS
                    )
                    target_present = isinstance(key_values, dict) and any(
                        _TARGET_RECIPE_KEY.fullmatch(str(key).strip()) is not None
                        for key in key_values
                    )
                    if not isinstance(key_values, dict) or (
                        wrapper_present and target_present
                    ):
                        disposition = "handler_input_incompatible"
                    elif not handler_id:
                        disposition = "repair_strategy_unavailable"
                    else:
                        disposition = "evidence_insufficient"
                    check["repair_disposition"] = disposition
                    check["repair_reasons"] = list(reasons)
                    route_reasons.extend(reasons)
                    if route_priority[disposition] > route_priority[route_status]:
                        route_status = disposition
            elif rule in PATCHABLE_RULES or _is_contract_mass_adapter_check(check):
                check["handler_id"] = "deterministic.recipe_scalar_adapter"
                check["repair_rule"] = rule
                check["repair_disposition"] = "deterministic_patch"
                route_status = "deterministic_patch"
            else:
                check["repair_disposition"] = "repair_strategy_unavailable"
                route_reasons.append(f"no registered handler for rule {rule!r}")
        finding["repair_route"] = {
            "status": route_status,
            "handler_id": (
                next(
                    (
                        str(check.get("handler_id") or "")
                        for check in checks
                        if isinstance(check, dict) and check.get("handler_id")
                    ),
                    "",
                )
            ),
            "reasons": list(dict.fromkeys(route_reasons)),
        }
    return routed


def propose_repair(
    context: Dict[str, Any], draft_candidate: Dict[str, Any]
) -> Dict[str, Any]:
    """Propose a deterministic local patch, or a controlled non-patch verdict.

    A patch is proposed only when every field error is an authorized value
    type error (contract-correct field name, actual value a losslessly
    parseable numeric string, single-value replace inside the diagnosed
    path).  Anything else returns a precise controlled non-patch verdict —
    never a best-effort rewrite or an implicit request for a model.
    """
    errors = context.get("field_errors") or []
    if not errors:
        open_findings = context.get("open_findings") or []
        if open_findings:
            route_statuses = {
                str((finding.get("repair_route") or {}).get("status") or "")
                for finding in open_findings
                if isinstance(finding, dict)
                and isinstance(finding.get("repair_route") or {}, dict)
            }
            if "handler_input_incompatible" in route_statuses:
                status = "handler_input_incompatible"
            elif "evidence_insufficient" in route_statuses:
                status = "evidence_insufficient"
            else:
                status = "repair_strategy_unavailable"
            return {
                "status": status,
                "patch": None,
                "reason": (
                    "open findings cannot produce an authorized deterministic patch; "
                    f"repair_routes={sorted(value for value in route_statuses if value)}"
                ),
            }
        return {"status": "accepted", "patch": None, "reason": "no open errors"}
    operations: List[Dict[str, Any]] = []
    unpatchable: List[Dict[str, Any]] = []
    handled_wrapper_steps: set = set()
    handled_contract_mass_rows: set = set()
    for error in errors:
        rule = error.get("rule")
        repair_rule = error.get("repair_rule") or rule
        if repair_rule == STRUCTURED_RECIPE_ADAPTER_RULE:
            step_ref = str(error.get("step_ref"))
            if step_ref in handled_wrapper_steps:
                continue
            handled_wrapper_steps.add(step_ref)
            step = _step_by_ref(draft_candidate, step_ref)
            if step is None:
                unpatchable.append(
                    {
                        **error,
                        "reason": "step not found in draft",
                        "disposition": "handler_input_incompatible",
                    }
                )
                continue
            new_key_values, reasons = adapt_unstructured_recipe_step(step)
            if new_key_values is None:
                unpatchable.append(
                    {
                        **error,
                        "reason": "recipe wrapper does not satisfy the strict schema adapter: "
                        + "; ".join(reasons),
                        "disposition": error.get("repair_disposition")
                        or "evidence_insufficient",
                    }
                )
                continue
            kv_path = f"{step_ref}.key_values"
            operations.append(
                {
                    "op": "test",
                    "path": kv_path,
                    "value": copy.deepcopy(step.get("key_values")),
                }
            )
            operations.append(
                {
                    "op": "replace",
                    "path": kv_path,
                    "value": new_key_values,
                    "original_text": "complete unstructured recipe evidence",
                    "rule": STRUCTURED_RECIPE_ADAPTER_RULE,
                    "rule_version": STRUCTURED_RECIPE_ADAPTER_VERSION,
                    "handler_id": error.get("handler_id"),
                    "handler_version": {
                        WRAPPER_RECIPE_HANDLER_ID: WRAPPER_RECIPE_HANDLER_VERSION,
                        TARGET_RECIPE_HANDLER_ID: TARGET_RECIPE_HANDLER_VERSION,
                    }.get(str(error.get("handler_id") or ""), ""),
                    "source_evidence_sha256": _digest(step.get("key_values")),
                }
            )
            continue
        if _is_contract_mass_adapter_check(error):
            step_ref = str(error.get("step_ref"))
            dedupe_key = (step_ref, str(error.get("path")))
            if dedupe_key in handled_contract_mass_rows:
                continue
            handled_contract_mass_rows.add(dedupe_key)
            normalized, reasons = normalize_contract_mass_row(
                draft_candidate, step_ref, error
            )
            if normalized is None:
                unpatchable.append(
                    {
                        **error,
                        "reason": "mass field does not satisfy the shared scalar "
                        "contract: " + "; ".join(reasons),
                        "disposition": "evidence_insufficient",
                    }
                )
                continue
            row_path, old_row, new_row, raw_text, scalar_evidence = normalized
            operations.append({"op": "test", "path": row_path, "value": old_row})
            operations.append(
                {
                    "op": "replace",
                    "path": row_path,
                    "value": new_row,
                    "original_text": raw_text,
                    "rule": CONTRACT_SCALAR_RULE_ID,
                    "adapter_rule": CONTRACT_MASS_FIELD_ADAPTER_RULE,
                    "adapter_rule_version": CONTRACT_MASS_FIELD_ADAPTER_VERSION,
                    "normalization": scalar_evidence,
                }
            )
            continue
        if repair_rule not in PATCHABLE_RULES:
            unpatchable.append(
                {
                    **error,
                    "reason": f"no registered handler for rule {rule!r}",
                    "disposition": "repair_strategy_unavailable",
                }
            )
            continue
        coerced = _coerce_numeric_string(error.get("actual"), str(repair_rule))
        if coerced is None:
            unpatchable.append(
                {
                    **error,
                    "reason": "actual value is not a lossless numeric string",
                    "disposition": "evidence_insufficient",
                }
            )
            continue
        path = f"{error['step_ref']}.{error['path']}"
        try:
            container, key, _ = resolve_path(draft_candidate, path)
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            unpatchable.append(
                {
                    **error,
                    "reason": f"path unresolvable: {exc}",
                    "disposition": "handler_input_incompatible",
                }
            )
            continue
        if container[key] != error.get("actual"):
            unpatchable.append(
                {
                    **error,
                    "reason": "draft value no longer matches diagnosis",
                    "disposition": "handler_input_incompatible",
                }
            )
            continue
        operations.append(
            {"op": "test", "path": path, "value": container[key]}
        )
        operations.append(
            {
                "op": "replace",
                "path": path,
                "value": coerced,
                "original_text": error.get("actual"),
            }
        )
    if unpatchable:
        if operations:
            # Item-wise triage: the patchable subset is still proposed (the
            # validator decides per-item promotion); unpatchable items stay
            # open and keep the case blocked.
            return {
                "status": "partial",
                "patch": {
                    "format": PATCH_FORMAT,
                    "base_draft_version": context.get("target", {}).get(
                        "draft_version"
                    ),
                    "contract_version": context.get("contract", {}).get("version"),
                    "ops": operations,
                },
                "unpatchable": unpatchable,
                "patchable_count": len(operations) // 2,
            }
        dispositions = {
            str(item.get("disposition") or item.get("repair_disposition") or "")
            for item in unpatchable
        }
        if "handler_input_incompatible" in dispositions:
            reason = "handler_input_incompatible"
        elif "evidence_insufficient" in dispositions:
            reason = "evidence_insufficient"
        else:
            reason = "repair_strategy_unavailable"
        return {
            "status": reason,
            "patch": None,
            "unpatchable": unpatchable,
            "patchable_count": 0,
        }
    return {
        "status": "patch_ready",
        "patch": {
            "format": PATCH_FORMAT,
            "base_draft_version": context.get("target", {}).get("draft_version"),
            "contract_version": context.get("contract", {}).get("version"),
            "ops": operations,
        },
    }


# ---------------------------------------------------------------------------
# validate_repair (commit entry)
# ---------------------------------------------------------------------------


def _refresh_progress(
    case: Dict[str, Any], findings: List[Dict[str, Any]], *, mark_resolved: bool = False
) -> None:
    """Validator-owned progress table: models never self-report progress."""
    verified: List[str] = []
    open_issues: List[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        details = finding.get("details")
        if not isinstance(details, dict):
            details = {}
        step_ref = _diagnostic_step_ref(details.get("plan_step"))
        raw_checks = details.get("checks")
        checks = raw_checks if isinstance(raw_checks, list) else []
        if not checks and not mark_resolved:
            text = (
                f"[{finding.get('type', 'plan_level_finding')}] "
                f"{finding.get('message', '')}"
            )
            if text not in open_issues:
                open_issues.append(text)
        for check in checks:
            if not isinstance(check, dict):
                continue
            if check.get("status") == "passed":
                text = f"{step_ref} 字段 {check.get('field')} 符合合同（{check.get('rule')}）"
                if text not in verified:
                    verified.append(text)
            elif check.get("status") == "failed" and mark_resolved:
                text = (
                    f"{step_ref} 字段 {check.get('field')} 的 {check.get('rule')} 错误"
                    "已由授权补丁修复并复检通过"
                )
                if text not in verified:
                    verified.append(text)
            elif check.get("status") == "failed":
                text = (
                    f"{step_ref} 字段 {check.get('field')}：期望 {check.get('expected')}，"
                    f"实际 {check.get('actual_type')} {check.get('actual')!r}"
                )
                if text not in open_issues:
                    open_issues.append(text)
    case["progress"] = {"verified": verified, "open_issues": open_issues}


def _record_history_constraints(
    case: Dict[str, Any], findings: List[Dict[str, Any]]
) -> None:
    contract_version = case["contracts"]["recipe_contract_version"]
    seen = {
        (entry.get("contract_version"), entry.get("statement"))
        for entry in case["history_constraints"]
    }
    for check in failed_checks(findings):
        statement = (
            f"字段 {check.get('field')} 以 {check.get('actual_type')} 提供，"
            f"未通过 {check.get('rule')}（已排除）"
        )
        key = (contract_version, statement)
        if key not in seen:
            seen.add(key)
            case["history_constraints"].append(
                {"contract_version": contract_version, "statement": statement}
            )


def validate_repair(
    case: Dict[str, Any], patch: Dict[str, Any], auditor: _AUDITOR
) -> Dict[str, Any]:
    """Validate and commit a patch.  Any failure leaves formal state intact."""
    draft = case["draft"]
    candidate = draft.get("candidate", {})
    pre_patch_findings = copy.deepcopy(draft.get("diagnosis") or [])

    if patch.get("format") != PATCH_FORMAT:
        return {"status": "rejected", "reason": "unsupported_patch_format"}
    if patch.get("contract_version") != case["contracts"]["recipe_contract_version"]:
        return {"status": "rejected", "reason": "contract_mismatch"}
    if recipe_contract_version() != case["contracts"]["recipe_contract_version"]:
        return {"status": "rejected", "reason": "contract_mismatch_current"}
    if patch.get("base_draft_version") != draft.get("version"):
        return {"status": "rejected", "reason": "draft_version_mismatch"}

    # Resolve every op path up front: frozen keys reject before any mutation,
    # and permissions are checked per path during application.
    resolved_ops: List[Tuple[Dict[str, Any], str]] = []
    for operation in patch.get("ops") or []:
        if not isinstance(operation, dict) or operation.get("op") not in {
            "test",
            "replace",
        }:
            return {"status": "rejected", "reason": "unsupported_operation"}
        try:
            path = canonical_path(candidate, str(operation.get("path", "")))
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            return {"status": "rejected", "reason": f"path_error: {exc}"}
        for frozen in FROZEN_TOP_LEVEL_KEYS:
            if path == frozen or path.startswith(frozen + ".") or path.startswith(
                frozen + "["
            ):
                return {"status": "rejected", "reason": f"frozen_key_modified: {frozen}"}
        resolved_ops.append((operation, path))

    # Permission scope: only paths named by the current diagnosis may change.
    # A structured-recipe adapter may replace exactly one whole key_values
    # object, but that permission is granted only after re-running the current
    # registered handler and proving that the proposed value is its unique
    # deterministic output.  The contract-mass adapter authorizes the
    # diagnosed recipe-row object
    # itself, because the normalization rewrites that row (drops the bare
    # 加样量 key, inserts the contract 加样量(g) number).
    allowed_paths = set()
    allowed_containers = set()
    adapter_diff_containers = set()
    adapter_expectations: Dict[str, Dict[str, Any]] = {}
    for check in failed_checks(draft.get("diagnosis") or []):
        targets: List[Tuple[str, set]] = [
            (
                f"{_diagnostic_step_ref(check.get('plan_step'))}.{check.get('path')}",
                allowed_paths,
            )
        ]
        if (
            check.get("repair_rule") or check.get("rule")
        ) == STRUCTURED_RECIPE_ADAPTER_RULE:
            step_ref = _diagnostic_step_ref(check.get("plan_step"))
            step = _step_by_ref(candidate, step_ref)
            if step is None:
                return {
                    "status": "rejected",
                    "reason": "adapter_step_missing_or_invalid",
                }
            selected_handler, adapted, reasons = select_unstructured_recipe_adapter(
                step
            )
            expected_handler = str(check.get("handler_id") or "")
            expected_version = {
                WRAPPER_RECIPE_HANDLER_ID: WRAPPER_RECIPE_HANDLER_VERSION,
                TARGET_RECIPE_HANDLER_ID: TARGET_RECIPE_HANDLER_VERSION,
            }.get(selected_handler, "")
            if (
                adapted is None
                or not selected_handler
                or selected_handler != expected_handler
                or not expected_version
            ):
                return {
                    "status": "rejected",
                    "reason": "adapter_revalidation_failed",
                    "details": list(reasons),
                }
            target = f"{step_ref}.key_values"
            try:
                canonical_target = canonical_path(candidate, target)
            except (KeyError, ValueError, AttributeError, TypeError) as exc:
                return {
                    "status": "rejected",
                    "reason": f"adapter_path_error: {exc}",
                }
            adapter_expectations[canonical_target] = {
                "source": copy.deepcopy(step.get("key_values")),
                "source_digest": _digest(step.get("key_values")),
                "adapted": adapted,
                "handler_id": selected_handler,
                "handler_version": expected_version,
            }
            allowed_paths.add(canonical_target)
            adapter_diff_containers.add(canonical_target)
        elif _is_contract_mass_adapter_check(check):
            targets.append(
                (
                    f"{_diagnostic_step_ref(check.get('plan_step'))}.{check.get('path')}",
                    allowed_containers,
                )
            )
        for target, sink in targets:
            try:
                sink.add(canonical_path(candidate, target))
            except (KeyError, ValueError, AttributeError, TypeError):
                # The diagnosed path may not exist yet (e.g. a missing notes
                # key); permission bookkeeping must not crash on it.
                sink.add(target)

    def _operation_path_allowed(path: str) -> bool:
        if path in allowed_paths:
            return True
        return any(
            path == container
            or path.startswith(container + ".")
            or path.startswith(container + "[")
            for container in allowed_containers
        )

    def _diff_path_allowed(path: str) -> bool:
        if _operation_path_allowed(path):
            return True
        # Recipe adapters are authorized to emit one exact whole-key_values
        # replacement only.  Its already re-computed deterministic value is
        # checked above; leaf-level diff reporting may therefore contain
        # descendants of that object even though descendant patch operations
        # remain forbidden.
        return any(
            path == container
            or path.startswith(container + ".")
            or path.startswith(container + "[")
            for container in adapter_diff_containers
        )

    # Adapter metadata is audit-only; authority comes from this fresh handler
    # execution.  Require one atomic old-value test and one exact replacement
    # for every adapter-owned key_values object, with no descendant writes.
    for path, expectation in adapter_expectations.items():
        test_ops = [
            operation
            for operation, resolved_path in resolved_ops
            if resolved_path == path and operation.get("op") == "test"
        ]
        replace_ops = [
            operation
            for operation, resolved_path in resolved_ops
            if resolved_path == path and operation.get("op") == "replace"
        ]
        if len(test_ops) != 1 or test_ops[0].get("value") != expectation["source"]:
            return {
                "status": "rejected",
                "reason": f"adapter_source_test_mismatch: {path}",
            }
        if len(replace_ops) != 1:
            return {
                "status": "rejected",
                "reason": f"adapter_replace_count_mismatch: {path}",
            }
        replacement = replace_ops[0]
        if replacement.get("value") != expectation["adapted"]:
            return {
                "status": "rejected",
                "reason": f"adapter_output_mismatch: {path}",
            }
        expected_metadata = {
            "rule": STRUCTURED_RECIPE_ADAPTER_RULE,
            "rule_version": STRUCTURED_RECIPE_ADAPTER_VERSION,
            "handler_id": expectation["handler_id"],
            "handler_version": expectation["handler_version"],
            "source_evidence_sha256": expectation["source_digest"],
        }
        if any(replacement.get(key) != value for key, value in expected_metadata.items()):
            return {
                "status": "rejected",
                "reason": f"adapter_metadata_mismatch: {path}",
            }

    patched = copy.deepcopy(candidate)
    for operation, path in resolved_ops:
        try:
            container, key, _ = resolve_path(patched, str(operation.get("path", "")))
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            return {"status": "rejected", "reason": f"path_error: {exc}"}
        if operation["op"] == "test":
            if container[key] != operation.get("value"):
                return {"status": "rejected", "reason": f"test_failed: {path}"}
            continue
        if not _operation_path_allowed(path):
            return {"status": "rejected", "reason": f"path_not_authorized: {path}"}
        container[key] = operation.get("value")

    # Frozen invariants: baseline comparisons on an isolated copy only.
    for key in FROZEN_TOP_LEVEL_KEYS:
        if patched.get(key) != case["baseline"]["candidate"].get(key):
            return {"status": "rejected", "reason": f"frozen_key_modified: {key}"}
    if len(patched.get("device_plan") or []) != len(
        case["baseline"]["candidate"].get("device_plan") or []
    ):
        return {"status": "rejected", "reason": "device_plan_length_changed"}

    unauthorized = [
        path
        for path in _diff_paths(candidate, patched)
        if not _diff_path_allowed(path)
    ]
    if unauthorized:
        return {
            "status": "rejected",
            "reason": "unauthorized_changes",
            "paths": unauthorized[:10],
        }

    findings = diagnose_candidate(auditor, patched)
    _log_round(
        case,
        {
            "kind": "validate",
            "patch_digest": _digest(patch),
            "patch_ops": len(patch.get("ops") or []),
            "result_findings": len(findings),
        },
    )
    # Regression gate: semantic findings that were resolved before this patch
    # must stay resolved.  A patch that re-breaks the sample matrix, frozen
    # material coverage, or the research route is rejected outright — the
    # model may never trade recipe compliance for scientific damage.
    def _finding_plan_step(finding: Dict[str, Any]) -> Tuple[str, Any]:
        details = finding.get("details")
        plan_step = details.get("plan_step") if isinstance(details, dict) else None
        return _plan_step_identity_token(plan_step)

    pre_types = {
        (
            f.get("type"),
            _finding_plan_step(f),
        )
        for f in pre_patch_findings
    }
    regressed = [
        f
        for f in findings
        if f.get("type") in SEMANTIC_REGRESSION_TYPES
        and (
            f.get("type"),
            _finding_plan_step(f),
        )
        not in pre_types
    ]
    if regressed:
        _log_round(
            case,
            {
                "kind": "regression",
                "types": sorted({str(f.get("type")) for f in regressed}),
            },
        )
        return {
            "status": "rejected",
            "reason": f"semantic_regression:{regressed[0].get('type')}",
            "findings": regressed,
        }
    if findings:
        # Item-wise subset promotion: a patch may be promoted when it resolves
        # at least one diagnosed item, introduces no new finding and no new
        # failed check, and every remaining item was already open before the
        # patch.  The remaining findings stay attached to the draft and keep
        # the case blocked — progress is saved, nothing is signed off.
        pre_ids = {_finding_identity(f) for f in pre_patch_findings}
        post_ids = {_finding_identity(f) for f in findings}
        pre_check_ids = {_check_identity(c) for c in failed_checks(pre_patch_findings)}
        post_check_ids = {_check_identity(c) for c in failed_checks(findings)}
        resolved_checks = pre_check_ids - post_check_ids
        if post_ids <= pre_ids and post_check_ids <= pre_check_ids and (
            resolved_checks or len(post_ids) < len(pre_ids)
        ):
            draft["parent_version"] = draft["version"]
            draft["version"] = int(draft["version"]) + 1
            draft["candidate"] = patched
            draft["candidate_digest"] = _digest(patched)
            draft["candidate_digest_algorithm"] = DIGEST_ALGORITHM
            draft["candidate_digest_scope"] = DIGEST_SCOPE_CANONICAL_JSON
            draft["diagnosis"] = copy.deepcopy(findings)
            draft["status"] = "working"
            draft["updated_at"] = _utc_now()
            _record_history_constraints(case, pre_patch_findings)
            _refresh_progress(case, findings)
            _log_round(
                case,
                {
                    "kind": "promote_partial",
                    "patch_digest": _digest(patch),
                    "resolved_checks": len(resolved_checks),
                    "remaining_findings": len(findings),
                },
            )
            return {
                "status": "promoted_partial",
                "draft_version": draft["version"],
                "parent_version": draft["parent_version"],
                "patch_digest": _digest(patch),
                "resolved_findings": len(pre_patch_findings) - len(findings),
                "remaining_findings": len(findings),
            }
        _record_history_constraints(case, findings)
        _refresh_progress(case, findings)
        return {
            "status": "rejected",
            "reason": "audit_still_failing",
            "findings": findings,
        }

    draft["parent_version"] = draft["version"]
    draft["version"] = int(draft["version"]) + 1
    draft["candidate"] = patched
    draft["candidate_digest"] = _digest(patched)
    draft["candidate_digest_algorithm"] = DIGEST_ALGORITHM
    draft["candidate_digest_scope"] = DIGEST_SCOPE_CANONICAL_JSON
    draft["diagnosis"] = []
    draft["status"] = "working"
    draft["updated_at"] = _utc_now()
    _refresh_progress(case, pre_patch_findings, mark_resolved=True)
    return {
        "status": "promoted",
        "draft_version": draft["version"],
        "parent_version": draft["parent_version"],
        "patch_digest": _digest(patch),
        "resolved_findings": len(pre_patch_findings),
    }


# ---------------------------------------------------------------------------
# explicit Research material-relationship compiler (commit entry)
# ---------------------------------------------------------------------------


def _manifest_authorized_missing_relation_ids(
    findings: List[Dict[str, Any]],
    relationship_bindings: Optional[Dict[str, Any]],
) -> List[str]:
    """Find audit-missing relations named by the frozen repair sidecar.

    The full-plan auditor intentionally sees only the candidate, so a relation
    absent from ``research_material_relation_ids`` is initially reported as a
    missing Device operation.  A repair case may separately carry an operator-
    authorized positive binding or an explicit ``missing_operation`` record.
    Merely matching the relation ID is permission to invoke the compiler, not
    proof that the record is valid, that the operation exists, or that the
    relation is repairable; the atomic compiler validates every record.
    """

    if not isinstance(relationship_bindings, dict):
        return []
    binding_records = relationship_binding_records(relationship_bindings)
    if relationship_bindings.get("schema") != BINDING_AUTHORITY_SCHEMA:
        # Trigger-only compatibility: let the compiler emit the explicit
        # unversioned-authority failure.  These values never become authority.
        binding_records = relationship_bindings
    bound: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("type") != (
            "frozen_material_transition_coverage_missing"
        ):
            continue
        route = finding.get("repair_route")
        if not isinstance(route, dict) or route.get("handler_id") != (
            MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID
        ):
            continue
        missing = finding.get("missing_material_relation_ids")
        if not isinstance(missing, list):
            continue
        for relation_id in missing:
            if not isinstance(relation_id, str) or not relation_id.strip():
                continue
            normalized_id = relation_id.strip()
            if normalized_id in binding_records:
                bound.add(normalized_id)
    return sorted(bound)


def _material_handler_requested(
    findings: List[Dict[str, Any]],
    relationship_bindings: Optional[Dict[str, Any]] = None,
) -> bool:
    if any(
        isinstance(finding, dict)
        and isinstance(finding.get("repair_route"), dict)
        and finding["repair_route"].get("status") == "deterministic_patch"
        and finding["repair_route"].get("handler_id")
        == MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID
        for finding in findings
    ):
        return True
    if (
        isinstance(relationship_bindings, dict)
        and relationship_bindings
        and relationship_bindings.get("schema") == BINDING_AUTHORITY_SCHEMA
        and not isinstance(relationship_bindings.get("bindings"), dict)
        and any(
            isinstance(finding, dict)
            and finding.get("type") == "frozen_material_transition_coverage_missing"
            for finding in findings
        )
    ):
        # A malformed versioned envelope must reach the compiler's fail-closed
        # diagnostic instead of being mistaken for an absent repair strategy.
        return True
    # Do not blanket-promote plan_missing_operation.  Invoke the compiler only
    # when the frozen repair authority explicitly mentions at least one relation
    # that the candidate-only auditor currently reports missing.  A negative
    # record only enters atomic classification; it never counts as coverage.
    return bool(
        _manifest_authorized_missing_relation_ids(findings, relationship_bindings)
    )


def _material_coverage_units(
    findings: List[Dict[str, Any]],
) -> set[Tuple[str, Tuple[Tuple[str, Any], ...], str]]:
    """Compare material-audit progress by its smallest stable authority unit.

    Explicit V2 contracts are repaired per Research relation ID.  Falling back
    to one ``<unspecified>`` unit per macro would hide partial progress when a
    macro owns several independent relations.  Historical category-derived
    findings retain their category identity for compatibility.
    """

    units: set[Tuple[str, Tuple[Tuple[str, Any], ...], str]] = set()
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("type") != (
            "frozen_material_transition_coverage_missing"
        ):
            continue
        macro_values = finding.get("source_macro_steps")
        if not isinstance(macro_values, list):
            macro_values = []
        macro_keys = tuple(
            sorted(
                (_plan_step_identity_token(value) for value in macro_values),
                key=lambda item: tuple(str(part) for part in item),
            )
        )
        relation_ids = finding.get("missing_material_relation_ids")
        if isinstance(relation_ids, list) and relation_ids:
            for relation_id in relation_ids:
                units.add(
                    (
                        str(finding.get("type")),
                        macro_keys,
                        "relation:" + str(relation_id),
                    )
                )
            continue
        categories = finding.get("missing_processing_categories")
        if not isinstance(categories, list) or not categories:
            categories = ["<unspecified>"]
        for category in categories:
            units.add((str(finding.get("type")), macro_keys, str(category)))
    return units


def _material_compile_diff_allowed(path: str) -> bool:
    if path == "material_relationship_compiler" or path.startswith(
        "material_relationship_compiler."
    ):
        return True
    if any(
        path == field
        or path.startswith(field + ".")
        or path.startswith(field + "[")
        for field in ("batch_plan", "material_transitions", "material_ledger")
    ):
        return True
    match = re.match(r"^device_plan\[\d+\]\.([^\.\[]+)", path)
    return bool(
        match
        and match.group(1)
        in {
            "material_event_kind",
            "material_transition_ids",
            "research_material_relation_ids",
            "logical_container_bindings",
        }
    )


def _material_compile_issue_summary(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Preserve compiler blocker ownership at the repair-loop boundary."""

    blocker_counts: Dict[str, int] = {}
    owner_by_class = {
        "evidence_insufficient": "research_contract",
        "plan_missing_operation": "device_plan",
        "plan_binding_missing": "device_plan",
        "plan_binding_ambiguous": "device_plan",
        "plan_binding_invalid": "device_plan",
        "runtime_guard_missing": "device_runtime",
        "software_unsupported": "device_software",
        "software_internal_error": "device_software",
        "contract_invalid": "contract_authority_review",
    }
    owners: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            blocker_class = "contract_invalid"
        else:
            blocker_class = str(
                issue.get("blocker_class") or "contract_invalid"
            ).strip() or "contract_invalid"
        blocker_counts[blocker_class] = blocker_counts.get(blocker_class, 0) + 1
        owners.add(owner_by_class.get(blocker_class, "manual_triage"))
    repair_owners = sorted(owners)
    return {
        "blocker_classes": sorted(blocker_counts),
        "blocker_counts": {
            key: blocker_counts[key] for key in sorted(blocker_counts)
        },
        "repair_owner": (
            repair_owners[0] if len(repair_owners) == 1 else "mixed"
        ),
        "repair_owners": repair_owners,
    }


def _binding_authority_attribution(authority: Any) -> Dict[str, Any]:
    if not isinstance(authority, dict):
        return {"binding_authority_attribution": {"schema": None}}
    records = relationship_binding_records(authority)
    return {
        "binding_authority_attribution": {
            "schema": authority.get("schema"),
            "schema_version": authority.get("schema_version"),
            "authoring_mode": authority.get("authoring_mode"),
            "automation_claim": authority.get("automation_claim"),
            "research_authority_sha256": authority.get(
                "research_authority_sha256"
            ),
            "candidate_digest_scope": authority.get("candidate_digest_scope"),
            "candidate_sha256": authority.get("candidate_sha256"),
            "workstation_truth_sha256": authority.get(
                "workstation_truth_sha256"
            ),
            "manual_support_schema": authority.get("manual_support_schema"),
            "manual_support_document_sha256": authority.get(
                "manual_support_document_sha256"
            ),
            "manual_support_record_count": len(
                authority.get("manual_support_record_sha256")
                if isinstance(
                    authority.get("manual_support_record_sha256"), dict
                )
                else {}
            ),
            "manual_support_embedded_record_count": len(
                authority.get("manual_support_records")
                if isinstance(authority.get("manual_support_records"), dict)
                else {}
            ),
            "binding_record_count": len(records),
        }
    }


def _run_material_relationship_handler(
    case: Dict[str, Any],
    auditor: _AUDITOR,
    *,
    resolve_workstation: Optional[Callable[[str], str]],
    workstation_truth_digest: str,
) -> Dict[str, Any]:
    """Compile, replay, full-audit, and atomically promote one material graph.

    The compiler receives only the frozen Research graph and the explicit
    relation/physical-container bindings stored in the repair case.  Findings
    select the handler but never supply material semantics or bindings.
    """

    inputs = _validate_material_repair_inputs(case)
    if not inputs:
        return {
            "status": "unavailable",
            "reason": "material_research_authority_missing",
        }
    expected_truth = str(inputs.get("workstation_truth_digest") or "").strip()
    current_truth = str(workstation_truth_digest or "").strip()
    if not expected_truth or not current_truth:
        return {
            "status": "unavailable",
            "reason": "workstation_truth_digest_missing",
        }
    if expected_truth != current_truth:
        return {
            "status": "rejected",
            "reason": "workstation_truth_digest_mismatch",
        }
    if resolve_workstation is None:
        return {
            "status": "unavailable",
            "reason": "workstation_truth_resolver_missing",
        }

    draft = case["draft"]
    candidate = draft["candidate"]
    pre_findings = copy.deepcopy(draft.get("diagnosis") or [])
    package = inputs["research_action_package_v2"]
    bindings = inputs["relationship_bindings"]
    binding_attribution = _binding_authority_attribution(bindings)
    compiled, issues, applied = compile_material_relationships(
        candidate,
        package,
        relationship_bindings=bindings,
        resolve_workstation=resolve_workstation,
        workstation_truth_digest=current_truth,
    )
    if issues:
        normalized_issues = [dict(issue) for issue in issues]
        return {
            "status": "unavailable",
            "reason": "material_relationship_compile_blocked",
            "issues": normalized_issues,
            **binding_attribution,
            **_material_compile_issue_summary(normalized_issues),
        }

    # Replay the exact handler over its own output.  A relationship repair is
    # not promotable unless it reaches a fixed point in one pass.
    replayed, replay_issues, replay_applied = compile_material_relationships(
        compiled,
        package,
        relationship_bindings=bindings,
        resolve_workstation=resolve_workstation,
        workstation_truth_digest=current_truth,
    )
    if replay_issues or replayed != compiled or replay_applied:
        normalized_replay_issues = [dict(issue) for issue in replay_issues]
        replay_summary_issues = normalized_replay_issues or [
            {
                "code": "material_relationship_compile_not_idempotent",
                "blocker_class": "software_internal_error",
            }
        ]
        return {
            "status": "rejected",
            "reason": "material_relationship_compile_not_idempotent",
            "issues": normalized_replay_issues,
            **binding_attribution,
            **_material_compile_issue_summary(replay_summary_issues),
            "replay_applied": list(replay_applied),
        }
    if compiled == candidate or not applied:
        return {
            "status": "unavailable",
            "reason": "material_relationship_compile_no_change",
        }

    if len(compiled.get("device_plan") or []) != len(
        candidate.get("device_plan") or []
    ):
        return {"status": "rejected", "reason": "device_plan_length_changed"}
    for key in FROZEN_TOP_LEVEL_KEYS:
        if compiled.get(key) != candidate.get(key):
            return {"status": "rejected", "reason": f"frozen_key_modified: {key}"}
    unauthorized = [
        path
        for path in _diff_paths(candidate, compiled)
        if not _material_compile_diff_allowed(path)
    ]
    if unauthorized:
        return {
            "status": "rejected",
            "reason": "material_relationship_unauthorized_changes",
            "paths": unauthorized[:10],
        }

    findings = diagnose_candidate(auditor, compiled)
    pre_material = _material_coverage_units(pre_findings)
    post_material = _material_coverage_units(findings)
    pre_other = {
        _finding_identity(finding)
        for finding in pre_findings
        if finding.get("type") != "frozen_material_transition_coverage_missing"
    }
    post_other = {
        _finding_identity(finding)
        for finding in findings
        if finding.get("type") != "frozen_material_transition_coverage_missing"
    }
    if not post_material <= pre_material or not post_other <= pre_other:
        return {
            "status": "rejected",
            "reason": "material_relationship_audit_regression",
            "new_material_units": sorted(
                post_material - pre_material,
                key=str,
            ),
            "new_findings": len(post_other - pre_other),
            "findings": findings,
        }
    if findings and len(post_material) >= len(pre_material):
        return {
            "status": "rejected",
            "reason": "material_relationship_audit_no_progress",
            "findings": findings,
        }

    draft["parent_version"] = draft["version"]
    draft["version"] = int(draft["version"]) + 1
    draft["candidate"] = compiled
    draft["candidate_digest"] = _digest(compiled)
    draft["candidate_digest_algorithm"] = DIGEST_ALGORITHM
    draft["candidate_digest_scope"] = DIGEST_SCOPE_CANONICAL_JSON
    draft["diagnosis"] = copy.deepcopy(findings)
    draft["status"] = "working"
    draft["updated_at"] = _utc_now()
    _refresh_progress(case, findings)
    _log_round(
        case,
        {
            "kind": "promote_material_relationships",
            "handler_id": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID,
            "handler_version": MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION,
            "compiler_rule": MATERIAL_RELATIONSHIP_COMPILER_RULE_ID,
            "research_authority_digest": inputs["research_authority_digest"],
            "relationship_bindings_digest": inputs[
                "relationship_bindings_digest"
            ],
            "candidate_digest": draft["candidate_digest"],
            "applied": list(applied),
            "resolved_material_units": len(pre_material - post_material),
            "remaining_findings": len(findings),
        },
    )
    return {
        "status": "promoted" if not findings else "promoted_partial",
        "reason": "material_relationship_compiled",
        "draft_version": draft["version"],
        "remaining_findings": len(findings),
        "applied": list(applied),
        **binding_attribution,
    }


# ---------------------------------------------------------------------------
# repair loop
# ---------------------------------------------------------------------------


def _finalize_repair_case(
    case: Dict[str, Any],
    final_status: str,
    stop_reason: str,
    case_dir: Optional[Path],
) -> Dict[str, Any]:
    """Seal the case: stop reason plus the validator-owned remaining findings."""
    case["stop_reason"] = stop_reason
    case["remaining_findings"] = copy.deepcopy(case["draft"].get("diagnosis") or [])
    if case_dir is not None:
        save_repair_case(case, Path(case_dir) / "repair_case.json")
    return {
        "status": final_status,
        "stop_reason": stop_reason,
        "case": case,
        "final_candidate": case["draft"]["candidate"],
        "final_candidate_digest": _digest(case["draft"]["candidate"]),
        "digest_algorithm": DIGEST_ALGORITHM,
        "digest_scope": DIGEST_SCOPE_CANONICAL_JSON,
        "draft_status": case["draft"]["status"],
        "material_blocker_summary": copy.deepcopy(
            case.get("material_blocker_summary") or {}
        ),
    }


def _drive_repair_loop(
    case: Dict[str, Any],
    auditor: _AUDITOR,
    *,
    resolve_workstation: Optional[Callable[[str], str]] = None,
    workstation_truth_digest: str = "",
) -> Tuple[str, str]:
    """Drive the propose/validate/promote loop on an opened case.

    Works on whatever draft the case currently carries, so both a fresh
    ``run_repair_loop`` and a ``resume_repair_loop`` share this body.  Every
    round re-diagnoses the current draft with the validator — the model never
    self-reports progress.
    """
    final_status = "stopped"
    stop_reason = ""
    previous_signature: Optional[Tuple[Tuple[Any, ...], ...]] = None

    while True:
        # Diagnose first: the accept verdict and every stop reason must be
        # based on the current draft's truth, and a clean draft is accepted
        # even when the patch budget is exactly exhausted.
        findings = diagnose_candidate(auditor, case["draft"]["candidate"])
        case["draft"]["diagnosis"] = findings
        if not findings:
            case["draft"]["status"] = "accepted"
            final_status = "accepted"
            stop_reason = "accepted"
            _log_round(case, {"kind": "accept", "reason": "no findings remain"})
            break
        # A persisted accepted bit is never sticky across a fresh validator
        # verdict.  Resume may discover drift or corruption; any current
        # finding immediately revokes accepted status before a budget or
        # non-patchable stop can seal the case.
        case["draft"]["status"] = "working"
        case["draft"]["updated_at"] = _utc_now()
        if int(case["budgets"]["patches_used"]) >= int(case["budgets"]["patch_limit"]):
            final_status = "stopped"
            stop_reason = "patch_budget_exhausted"
            _log_round(case, {"kind": "stop", "reason": stop_reason})
            break
        if int(case["budgets"]["rounds_used"]) >= int(
            case["budgets"]["max_rounds"]
        ):
            final_status = "stopped"
            stop_reason = "round_budget_exhausted"
            _log_round(case, {"kind": "stop", "reason": stop_reason})
            break
        signature = _findings_signature(findings)
        if signature == previous_signature:
            final_status = "stopped"
            stop_reason = "no_progress"
            _log_round(
                case,
                {
                    "kind": "stop",
                    "reason": "no_progress",
                    "detail": "same error signature as previous round",
                },
            )
            break
        previous_signature = signature

        case["budgets"]["rounds_used"] = int(
            case["budgets"]["rounds_used"]
        ) + 1
        material_result: Optional[Dict[str, Any]] = None
        material_inputs = case.get("material_relationship_repair")
        frozen_relationship_bindings = (
            material_inputs.get("relationship_bindings")
            if isinstance(material_inputs, dict)
            and isinstance(material_inputs.get("relationship_bindings"), dict)
            else {}
        )
        sidecar_trigger_relation_ids = _manifest_authorized_missing_relation_ids(
            findings, frozen_relationship_bindings
        )
        if _material_handler_requested(findings, frozen_relationship_bindings):
            material_result = _run_material_relationship_handler(
                case,
                auditor,
                resolve_workstation=resolve_workstation,
                workstation_truth_digest=workstation_truth_digest,
            )
            if material_result.get("blocker_classes"):
                case["material_blocker_summary"] = {
                    "status": material_result.get("status"),
                    "reason": material_result.get("reason"),
                    "issue_count": len(material_result.get("issues") or []),
                    "blocker_classes": copy.deepcopy(
                        material_result.get("blocker_classes") or []
                    ),
                    "blocker_counts": copy.deepcopy(
                        material_result.get("blocker_counts") or {}
                    ),
                    "repair_owner": material_result.get("repair_owner"),
                    "repair_owners": copy.deepcopy(
                        material_result.get("repair_owners") or []
                    ),
                    "sidecar_trigger_relation_count": len(
                        sidecar_trigger_relation_ids
                    ),
                    "sidecar_trigger_relation_ids": copy.deepcopy(
                        sidecar_trigger_relation_ids
                    ),
                    "binding_authority_attribution": copy.deepcopy(
                        material_result.get("binding_authority_attribution") or {}
                    ),
                }
            _log_round(
                case,
                {
                    "kind": "propose_material_relationships",
                    "status": material_result.get("status"),
                    "reason": material_result.get("reason"),
                    "issues": copy.deepcopy(material_result.get("issues") or []),
                    "blocker_classes": copy.deepcopy(
                        material_result.get("blocker_classes") or []
                    ),
                    "blocker_counts": copy.deepcopy(
                        material_result.get("blocker_counts") or {}
                    ),
                    "repair_owner": material_result.get("repair_owner"),
                    "repair_owners": copy.deepcopy(
                        material_result.get("repair_owners") or []
                    ),
                    "sidecar_trigger_relation_count": len(
                        sidecar_trigger_relation_ids
                    ),
                    "sidecar_trigger_relation_ids": copy.deepcopy(
                        sidecar_trigger_relation_ids
                    ),
                    "binding_authority_attribution": copy.deepcopy(
                        material_result.get("binding_authority_attribution") or {}
                    ),
                },
            )
            if material_result.get("status") in {
                "promoted",
                "promoted_partial",
            }:
                case["budgets"]["patches_used"] = int(
                    case["budgets"]["patches_used"]
                ) + 1
                continue
            if material_result.get("status") == "rejected":
                final_status = "stopped"
                stop_reason = (
                    "material_patch_rejected:"
                    + str(material_result.get("reason") or "unknown")
                )
                _log_round(case, {"kind": "stop", "reason": stop_reason})
                break
        context = build_repair_context(case)
        proposal = propose_repair(context, case["draft"]["candidate"])
        patch = proposal.get("patch")
        if proposal["status"] in ("patch_ready", "partial") and patch:
            case["budgets"]["patches_used"] = int(case["budgets"]["patches_used"]) + 1
        _log_round(
            case,
            {
                "kind": "propose",
                "status": proposal["status"],
                "context_manifest": context_manifest(context),
                "patch_ops": len((proposal.get("patch") or {}).get("ops") or []),
            },
        )
        if proposal["status"] not in ("patch_ready", "partial") or not patch:
            final_status = "stopped"
            # "accepted" here means the remaining findings carry no field-level
            # checks at all — nothing a patch could target, so the case is
            # blocked for triage, not signed off.
            if material_result is not None:
                stop_reason = str(
                    material_result.get("reason")
                    or "material_relationship_compile_unavailable"
                )
            else:
                stop_reason = (
                    proposal["status"]
                    if proposal["status"] != "accepted"
                    else "manual_no_patchable_checks"
                )
            _log_round(
                case,
                {
                    "kind": "stop",
                    "reason": stop_reason,
                    "unpatchable": proposal.get("unpatchable", []),
                    "material_issues": copy.deepcopy(
                        (material_result or {}).get("issues") or []
                    ),
                    "material_blocker_classes": copy.deepcopy(
                        (material_result or {}).get("blocker_classes") or []
                    ),
                    "material_blocker_counts": copy.deepcopy(
                        (material_result or {}).get("blocker_counts") or {}
                    ),
                    "material_repair_owner": (material_result or {}).get(
                        "repair_owner"
                    ),
                },
            )
            break
        result = validate_repair(case, patch, auditor)
        if result["status"] in ("promoted", "promoted_partial"):
            _log_round(
                case,
                {
                    "kind": (
                        "promote" if result["status"] == "promoted" else "promote_partial"
                    ),
                    "draft_version": result["draft_version"],
                    "parent_version": result.get("parent_version"),
                    "patch_digest": result.get("patch_digest"),
                    "resolved_findings": result.get("resolved_findings"),
                    "remaining_findings": result.get("remaining_findings"),
                },
            )
            continue
        _log_round(case, {"kind": "reject", "reason": result["reason"]})
        if result["reason"] == "audit_still_failing":
            # Keep D1 and give the loop one more chance; an unchanged error
            # signature stops the loop as no_progress above.
            continue
        final_status = "stopped"
        stop_reason = f"patch_rejected:{result['reason']}"
        break

    return final_status, stop_reason


def run_repair_loop(
    candidate: Dict[str, Any],
    auditor: _AUDITOR,
    *,
    case_dir: Optional[Path] = None,
    patch_limit: int = DEFAULT_PATCH_LIMIT,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    provenance: Optional[Dict[str, Any]] = None,
    research_authority: Optional[Dict[str, Any]] = None,
    relationship_bindings: Optional[Dict[str, Any]] = None,
    resolve_workstation: Optional[Callable[[str], str]] = None,
    workstation_truth_digest: str = "",
    full_plan_audit_context_digest: str = "",
) -> Dict[str, Any]:
    """Run the controlled local-repair loop for one failed candidate."""
    diagnosis = diagnose_candidate(auditor, candidate)
    case = create_repair_case(
        candidate,
        diagnosis,
        patch_limit=patch_limit,
        max_rounds=max_rounds,
        provenance=provenance,
        research_authority=research_authority,
        relationship_bindings=relationship_bindings,
        workstation_truth_digest=workstation_truth_digest,
        full_plan_audit_context_digest=full_plan_audit_context_digest,
    )
    _log_round(
        case,
        {
            "kind": "open",
            "baseline_digest": case["baseline"]["candidate_digest"],
            "initial_findings": len(diagnosis),
        },
    )
    final_status, stop_reason = _drive_repair_loop(
        case,
        auditor,
        resolve_workstation=resolve_workstation,
        workstation_truth_digest=workstation_truth_digest,
    )
    return _finalize_repair_case(case, final_status, stop_reason, case_dir)


def resume_repair_loop(
    saved: Any,
    auditor: _AUDITOR,
    *,
    case_dir: Optional[Path] = None,
    resolve_workstation: Optional[Callable[[str], str]] = None,
    workstation_truth_digest: str = "",
    full_plan_audit_context_digest: str = "",
    research_authority_revalidation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Continue a saved repair case from its current draft.

    The draft — never the baseline — is the resume point, so an interrupted
    repair keeps every patch already promoted instead of re-importing P0 and
    starting over.  Budgets are preserved; an operator may raise
    ``case["budgets"]["patch_limit"]`` before resuming to grant more budget.
    The case is refused when the recipe contract drifted since the case was
    opened, because patches authorized under the old contract must not land.
    """
    if isinstance(saved, (str, Path)):
        case = load_repair_case(Path(saved))
    else:
        case = saved
    if case.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            "repair_case_schema_mismatch: expected "
            f"{SCHEMA_VERSION}, got {case.get('schema_version')!r}"
        )
    baseline = case.get("baseline")
    draft = case.get("draft")
    budgets = case.get("budgets")
    if not isinstance(baseline, dict) or not isinstance(draft, dict):
        raise ValueError("repair_case_invalid: missing baseline or draft object")
    if not isinstance(budgets, dict) or not isinstance(budgets.get("rounds_used"), int):
        raise ValueError("repair_case_invalid: missing deterministic round counter")
    for label, record in (("baseline", baseline), ("draft", draft)):
        if (
            record.get("candidate_digest_algorithm") != DIGEST_ALGORITHM
            or record.get("candidate_digest_scope") != DIGEST_SCOPE_CANONICAL_JSON
            or record.get("candidate_digest") != _digest(record.get("candidate"))
        ):
            raise ValueError(
                f"repair_case_digest_mismatch: {label} candidate or digest metadata changed"
            )
    case_contract = case["contracts"]["recipe_contract_version"]
    if case_contract != recipe_contract_version():
        raise ValueError(
            "contract_mismatch: repair case was opened under recipe contract "
            f"{case_contract!r}, current is {recipe_contract_version()!r}"
        )
    current_handlers = {
        WRAPPER_RECIPE_HANDLER_ID: WRAPPER_RECIPE_HANDLER_VERSION,
        TARGET_RECIPE_HANDLER_ID: TARGET_RECIPE_HANDLER_VERSION,
        "deterministic.recipe_scalar_adapter": CONTRACT_MASS_FIELD_ADAPTER_VERSION,
        MATERIAL_EPISODE_EVIDENCE_HANDLER_ID: (
            MATERIAL_EPISODE_EVIDENCE_HANDLER_VERSION
        ),
        MATERIAL_RELATIONSHIP_COMPILER_HANDLER_ID: (
            MATERIAL_RELATIONSHIP_COMPILER_HANDLER_VERSION
        ),
    }
    if case["contracts"].get("repair_handlers") != current_handlers:
        raise ValueError(
            "contract_mismatch: repair handler versions changed since the case "
            "was opened"
        )
    material_inputs = _validate_material_repair_inputs(case)
    if material_inputs:
        expected_truth = str(
            material_inputs.get("workstation_truth_digest") or ""
        ).strip()
        if expected_truth != str(workstation_truth_digest or "").strip():
            raise ValueError(
                "contract_mismatch: workstation truth changed since the material "
                "repair case was opened"
            )
        expected_audit_context = str(
            material_inputs.get("full_plan_audit_context_digest") or ""
        ).strip()
        if expected_audit_context != str(
            full_plan_audit_context_digest or ""
        ).strip():
            raise ValueError(
                "contract_mismatch: full Plan audit context changed since the "
                "material repair case was opened"
            )
    draft = case["draft"]
    _log_round(
        case,
        {
            "kind": "resume",
            "baseline_digest": case["baseline"]["candidate_digest"],
            "draft_version": draft.get("version"),
            "patches_used": case["budgets"]["patches_used"],
            "remaining_findings": len(draft.get("diagnosis") or []),
            "research_authority_revalidation_mode": (
                research_authority_revalidation.get("validation_mode")
                if isinstance(research_authority_revalidation, dict)
                else None
            ),
            "research_authority_revalidation_source_kind": (
                research_authority_revalidation.get("source_kind")
                if isinstance(research_authority_revalidation, dict)
                else None
            ),
            "frozen_research_authority_validation_mode": (
                material_inputs.get("research_authority_validation", {}).get(
                    "validation_mode"
                )
                if material_inputs
                and isinstance(
                    material_inputs.get("research_authority_validation"), dict
                )
                else None
            ),
            "full_plan_audit_context_digest": (
                material_inputs.get("full_plan_audit_context_digest")
                if material_inputs
                else None
            ),
        },
    )
    final_status, stop_reason = _drive_repair_loop(
        case,
        auditor,
        resolve_workstation=resolve_workstation,
        workstation_truth_digest=workstation_truth_digest,
    )
    return _finalize_repair_case(case, final_status, stop_reason, case_dir)


# ---------------------------------------------------------------------------
# CLI: offline replay against a saved device state
# ---------------------------------------------------------------------------


class _OfflineNoModel:
    def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("offline plan-repair replay forbids model invocation")

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("offline plan-repair replay forbids model invocation")


def _offline_full_plan_context(
    device_state_path: Path,
    research_authority: Dict[str, Any],
    relationship_binding_authority: Optional[Dict[str, Any]] = None,
) -> Tuple[
    _AUDITOR,
    Callable[[str], str],
    str,
    str,
    Callable[[Dict[str, Any]], Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]],
]:
    """Rehydrate only the deterministic state required by the real Plan audit."""

    from dataclasses import fields

    saved = json.loads(device_state_path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict):
        raise ValueError("device-state must contain a JSON object")
    package, authority_validation = _validated_research_v2_authority(
        research_authority
    )
    if package is None:
        raise ValueError("full-plan audit requires research_action_package_v2")
    saved_handoff = saved.get("research_handoff")
    saved_handoff = copy.deepcopy({} if saved_handoff is None else saved_handoff)
    if not isinstance(saved_handoff, dict):
        raise ValueError("device-state research_handoff must be an object")
    raw_mirror_paths = authority_validation.get("raw_mirror_paths")
    authority_has_raw_semantics = bool(
        isinstance(raw_mirror_paths, list) and raw_mirror_paths
    )
    if authority_has_raw_semantics:
        from chem_agent_contracts.v2 import canonicalize_v2_device_handoff

        validation_authority = research_authority
        if all(
            isinstance(path, str) and path.startswith("research_handoff.")
            for path in raw_mirror_paths
        ):
            validation_authority = research_authority.get("research_handoff")
            if not isinstance(validation_authority, dict):
                raise ValueError(
                    "Research handoff-only authority requires an object-valued "
                    "research_handoff wrapper"
                )

        if __package__ in (None, ""):
            from run_from_research_state import (  # type: ignore
                build_device_agent_input_package,
                extract_macro_plan,
                validate_v2_research_handoff_consistency,
            )
        else:
            from .run_from_research_state import (
                build_device_agent_input_package,
                extract_macro_plan,
                validate_v2_research_handoff_consistency,
            )

        try:
            authoritative_macro_plan = extract_macro_plan(validation_authority)
            revalidated_package = validate_v2_research_handoff_consistency(
                validation_authority,
                authoritative_macro_plan,
            )
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        if _digest(revalidated_package) != _digest(package):
            raise ValueError(
                "validated Research authority changed while constructing the full Plan audit context"
            )
        normalized_device_handoff = build_device_agent_input_package(
            validation_authority,
            authoritative_macro_plan,
            canonical_v2_package=package,
        )
        # Rebuild the audit handoff through the same strict projection used by
        # the normal V2 Device entry point.  Do not overlay arbitrary saved
        # Research-state fields: only digest-verified raw steps/observations and
        # canonical package projections may reach the Device auditor.
        try:
            handoff = canonicalize_v2_device_handoff(
                normalized_device_handoff,
                package=package,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "rebuilt full Plan Research handoff failed canonical Device "
                f"projection: {exc}"
            ) from exc
        if _digest(handoff.get("research_action_package_v2")) != _digest(package):
            raise ValueError(
                "rebuilt full Plan Research handoff does not match the validated authority"
            )
    else:
        raise ValueError(
            "full Plan audit requires a complete validated Research state or "
            "handoff with raw macro-action semantics; package-only authority "
            "cannot reproduce an equivalent audit context"
        )

    state_fields = {item.name for item in fields(_single_agent.SingleDeviceAgentState)}
    state_payload = {
        key: copy.deepcopy(value)
        for key, value in saved.items()
        if key in state_fields
    }
    state_payload["research_handoff"] = handoff
    state_payload.setdefault("exp_id", str(saved.get("exp_id") or "offline-replay"))
    saved_contract_version = saved.get("contract_version")
    if saved_contract_version is None:
        contract_version = "v2"
    elif not isinstance(saved_contract_version, str):
        raise ValueError("device-state contract_version must be a string")
    else:
        contract_version = saved_contract_version.strip().lower()
    if contract_version not in {"v1", "v2"}:
        raise ValueError(
            f"device-state contract_version is unsupported: {contract_version!r}"
        )
    if contract_version != "v2":
        raise ValueError(
            "full Plan Research-authority replay requires a V2 Device state"
        )
    state_payload["contract_version"] = contract_version
    if isinstance(handoff.get("contract_resolution"), dict):
        state_payload["contract_resolution"] = copy.deepcopy(
            handoff["contract_resolution"]
        )
    state = _single_agent.SingleDeviceAgentState(**state_payload)
    saved_semantic_analysis = saved.get("semantic_analysis")
    active_semantic_analysis = copy.deepcopy(
        {} if saved_semantic_analysis is None else saved_semantic_analysis
    )
    if not isinstance(active_semantic_analysis, dict):
        raise ValueError("device-state semantic_analysis must be an object")
    if active_semantic_analysis:
        semantic_errors = _single_agent.SingleDeviceAgent._semantic_analysis_errors(
            handoff,
            active_semantic_analysis,
        )
        if semantic_errors:
            raise ValueError(
                "saved Device semantic_analysis is incompatible with the "
                "rebuilt Research handoff: "
                + "; ".join(semantic_errors)
            )
    full_plan_audit_context_digest_value = full_plan_audit_context_digest(
        state_payload,
        contract_version=contract_version,
        active_semantic_analysis=active_semantic_analysis,
    )
    agent = _single_agent.SingleDeviceAgent(
        _OfflineNoModel(),
        contract_version=contract_version,
    )
    agent._active_semantic_analysis = active_semantic_analysis
    agent._assert_workstation_snapshot_current(state)
    truth_digest = str(state.device_truth_sha256 or "").strip()
    if not truth_digest:
        raise ValueError("device-state lacks a bound workstation truth digest")
    frozen_binding_authority = copy.deepcopy(
        {}
        if relationship_binding_authority is None
        else relationship_binding_authority
    )
    if not isinstance(frozen_binding_authority, dict):
        raise ValueError("relationship binding authority must be an object")

    def finalize_for_pre_certificate(
        candidate: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        pre_findings = agent._plan_level_findings(
            state,
            candidate,
            relationship_binding_authority=frozen_binding_authority,
            workstation_truth_digest=truth_digest,
        )
        normalized = agent._normalize_plan_handoff_steps(
            state, copy.deepcopy(candidate)
        )
        normalized = agent._normalize_quantity_contract(
            normalized,
            research_handoff=state.research_handoff,
        )
        normalized["requires_scientific_review"] = (
            agent._scientific_review_required(
                normalized,
                feasibility_progress=state.feasibility_progress,
            )
        )
        post_findings = agent._plan_level_findings(
            state,
            normalized,
            relationship_binding_authority=frozen_binding_authority,
            workstation_truth_digest=truth_digest,
        )
        return normalized, pre_findings, post_findings

    def full_plan_auditor(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Acceptance audits the exact post-normalization object, matching
        # _accept_feasibility_plan.  The CLI report separately preserves the
        # pre-normalization verdict and both candidate digests.
        _, _, post_findings = finalize_for_pre_certificate(candidate)
        return post_findings

    return (
        full_plan_auditor,
        agent._truth_workstation_code,
        truth_digest,
        full_plan_audit_context_digest_value,
        finalize_for_pre_certificate,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-state", help="Saved device state JSON")
    parser.add_argument(
        "--progress-index",
        type=int,
        help="feasibility_progress index whose candidate should be repaired",
    )
    parser.add_argument(
        "--candidate-json",
        help=(
            "Import a raw candidate JSON directly as an UNVERIFIED draft. "
            "The import is not a checkpoint hit and inherits no pass marks."
        ),
    )
    parser.add_argument(
        "--auditor", choices=["recipe", "full-plan"], default="recipe"
    )
    parser.add_argument(
        "--research-authority",
        "--research-state",
        dest="research_authority",
        help=(
            "Revised Research state/package containing the explicit "
            "research_action_package_v2 used as material authority"
        ),
    )
    parser.add_argument(
        "--relationship-bindings",
        help=(
            "Versioned evidence-bound relationship authority JSON; bare "
            "relation-to-step maps are rejected"
        ),
    )
    parser.add_argument(
        "--assert-no-model",
        action="store_true",
        help="Fail if whole-plan model regeneration is enabled",
    )
    parser.add_argument(
        "--replay-twice",
        action="store_true",
        help="Replay the deterministic repair a second time and compare digests",
    )
    parser.add_argument(
        "--resume-case",
        help=(
            "Resume an interrupted repair_case.json from its current draft "
            "instead of importing a candidate again"
        ),
    )
    parser.add_argument("--case-dir", help="Directory for repair_case.json")
    parser.add_argument("--output", help="Optional JSON report path")
    parser.add_argument(
        "--candidate-output",
        help="Optional path for the final offline candidate (never dispatched)",
    )
    args = parser.parse_args(argv)

    if args.auditor == "full-plan" and not args.assert_no_model:
        raise SystemExit("--auditor full-plan requires --assert-no-model")
    if (args.assert_no_model or args.auditor == "full-plan") and os.getenv(
        "CHEM_DEVICE_ALLOW_PLAN_REGEN", ""
    ).strip().lower() in {"1", "true", "yes"}:
        raise SystemExit(
            "--assert-no-model refused: CHEM_DEVICE_ALLOW_PLAN_REGEN is enabled"
        )

    state_path = Path(args.device_state).expanduser() if args.device_state else None
    research_authority: Optional[Dict[str, Any]] = None
    if args.research_authority:
        research_authority = json.loads(
            Path(args.research_authority).expanduser().read_text(encoding="utf-8")
        )
        if not isinstance(research_authority, dict):
            raise SystemExit("research-authority must contain a JSON object")
    relationship_bindings: Dict[str, Any] = {}
    relationship_bindings_provided = bool(args.relationship_bindings)
    if args.relationship_bindings:
        relationship_bindings = json.loads(
            Path(args.relationship_bindings).expanduser().read_text(encoding="utf-8")
        )
        if not isinstance(relationship_bindings, dict):
            raise SystemExit("relationship-bindings must contain a JSON object")
    active_relationship_bindings = copy.deepcopy(relationship_bindings)

    auditor: _AUDITOR = recipe_auditor
    resolve_workstation: Optional[Callable[[str], str]] = None
    workstation_truth_digest = ""
    full_plan_audit_context_digest = ""
    finalize_full_plan: Optional[
        Callable[
            [Dict[str, Any]],
            Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]],
        ]
    ] = None

    def configure_full_plan(
        authority: Optional[Dict[str, Any]],
        binding_authority: Optional[Dict[str, Any]] = None,
    ) -> None:
        nonlocal auditor, resolve_workstation, workstation_truth_digest
        nonlocal full_plan_audit_context_digest
        nonlocal finalize_full_plan
        if args.auditor != "full-plan":
            return
        if state_path is None:
            raise SystemExit("--auditor full-plan requires --device-state")
        if authority is None:
            raise SystemExit("--auditor full-plan requires --research-authority")
        (
            auditor,
            resolve_workstation,
            workstation_truth_digest,
            full_plan_audit_context_digest,
            finalize_full_plan,
        ) = (
            _offline_full_plan_context(
                state_path,
                authority,
                relationship_binding_authority=binding_authority,
            )
        )

    case_base = Path(args.case_dir).expanduser() if args.case_dir else None
    if args.resume_case:
        case_path = Path(args.resume_case).expanduser()
        saved_case = load_repair_case(case_path)
        saved_material_value = saved_case.get("material_relationship_repair")
        saved_material = (
            {} if saved_material_value is None else saved_material_value
        )
        if not isinstance(saved_material, dict):
            raise SystemExit(
                "resume repair case material_relationship_repair must be an object"
            )
        resume_authority = research_authority
        if resume_authority is None and isinstance(saved_material, dict) and isinstance(
            saved_material.get("research_action_package_v2"), dict
        ):
            resume_authority = {
                "research_action_package_v2": copy.deepcopy(
                    saved_material["research_action_package_v2"]
                )
            }
        resume_authority_revalidation: Dict[str, Any] = {}
        if resume_authority is not None:
            resumed_package, resume_authority_revalidation = (
                _validated_research_v2_authority(resume_authority)
            )
            frozen_package = (
                saved_material.get("research_action_package_v2")
                if isinstance(saved_material, dict)
                else None
            )
            if not isinstance(resumed_package, dict) or not isinstance(
                frozen_package, dict
            ):
                raise SystemExit(
                    "resume material repair requires a validated frozen Research "
                    "V2 package"
                )
            if _digest(resumed_package) != _digest(frozen_package):
                raise SystemExit(
                    "resume Research authority differs from the package frozen in "
                    "the repair case"
                )
        resume_binding_authority = (
            copy.deepcopy(saved_material.get("relationship_bindings"))
            if isinstance(saved_material, dict)
            and isinstance(saved_material.get("relationship_bindings"), dict)
            else {}
        )
        frozen_binding_digest = (
            str(saved_material.get("relationship_bindings_digest") or "")
            if isinstance(saved_material, dict)
            else ""
        )
        if relationship_bindings_provided:
            if not frozen_binding_digest or _digest(relationship_bindings) != (
                frozen_binding_digest
            ):
                raise SystemExit(
                    "resume relationship binding authority differs from the "
                    "authority frozen in the repair case"
                )
            active_relationship_bindings = copy.deepcopy(relationship_bindings)
        else:
            active_relationship_bindings = resume_binding_authority
        configure_full_plan(resume_authority, active_relationship_bindings)
        result = resume_repair_loop(
            saved_case,
            auditor,
            case_dir=case_base,
            resolve_workstation=resolve_workstation,
            workstation_truth_digest=workstation_truth_digest,
            full_plan_audit_context_digest=full_plan_audit_context_digest,
            research_authority_revalidation=resume_authority_revalidation,
        )
        provenance = result["case"].get("provenance") or {}
    else:
        provenance: Dict[str, Any]
        if args.candidate_json:
            candidate_path = Path(args.candidate_json).expanduser()
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            if not isinstance(candidate, dict) or not candidate.get("device_plan"):
                raise SystemExit(
                    "candidate-json must carry a non-empty device_plan list"
                )
            provenance = {
                "channel": "candidate_json_import",
                "source_path": str(candidate_path),
                "imported_at": _utc_now(),
                "imported_as": "unverified_draft",
                "verification_status": "not_reverified",
            }
        else:
            if state_path is None or args.progress_index is None:
                raise SystemExit(
                    "either --candidate-json or both --device-state and "
                    "--progress-index are required"
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            entries = state.get("feasibility_progress") or []
            if not (0 <= args.progress_index < len(entries)):
                raise SystemExit(
                    f"progress index {args.progress_index} out of range "
                    f"({len(entries)} entries)"
                )
            candidate = entries[args.progress_index].get("candidate")
            if not isinstance(candidate, dict) or not candidate.get("device_plan"):
                raise SystemExit(
                    "selected progress entry carries no candidate device_plan"
                )
            provenance = {
                "channel": "device_state_progress",
                "source_path": str(state_path),
                "progress_index": args.progress_index,
                "imported_as": "unverified_draft",
                "verification_status": "not_reverified",
            }

        configure_full_plan(research_authority, active_relationship_bindings)
        first_case_dir = (
            case_base / "replay_1" if case_base and args.replay_twice else case_base
        )
        result = run_repair_loop(
            candidate,
            auditor,
            case_dir=first_case_dir,
            provenance=provenance,
            research_authority=research_authority,
            relationship_bindings=active_relationship_bindings,
            resolve_workstation=resolve_workstation,
            workstation_truth_digest=workstation_truth_digest,
            full_plan_audit_context_digest=full_plan_audit_context_digest,
        )

    delivered_candidate = copy.deepcopy(result["final_candidate"])
    normalization_report: Dict[str, Any] = {"performed": False}
    if finalize_full_plan is not None:
        normalized, pre_normalization_findings, post_normalization_findings = (
            finalize_full_plan(delivered_candidate)
        )
        expected_findings = result["case"].get("remaining_findings") or []
        if _findings_signature(post_normalization_findings) != _findings_signature(
            expected_findings
        ):
            raise SystemExit(
                "post-normalization full Plan audit differs from the repair-case verdict"
            )
        normalization_report = {
            "performed": True,
            "pre_normalization_candidate_digest": _digest(delivered_candidate),
            "post_normalization_candidate_digest": _digest(normalized),
            "pre_normalization_finding_count": len(pre_normalization_findings),
            "post_normalization_finding_count": len(post_normalization_findings),
            "pre_normalization_findings": copy.deepcopy(
                pre_normalization_findings
            ),
            "post_normalization_findings": copy.deepcopy(
                post_normalization_findings
            ),
        }
        delivered_candidate = normalized

    replay_report: Dict[str, Any] = {}
    if args.replay_twice:
        if research_authority is None:
            saved_material = result["case"].get("material_relationship_repair") or {}
            if isinstance(saved_material.get("research_action_package_v2"), dict):
                research_authority = {
                    "research_action_package_v2": copy.deepcopy(
                        saved_material["research_action_package_v2"]
                    )
                }
        second_case_dir = case_base / "replay_2" if case_base else None
        second = run_repair_loop(
            delivered_candidate,
            auditor,
            case_dir=second_case_dir,
            provenance={
                "channel": "deterministic_replay",
                "source_candidate_digest": _digest(delivered_candidate),
            },
            research_authority=research_authority,
            relationship_bindings=active_relationship_bindings,
            resolve_workstation=resolve_workstation,
            workstation_truth_digest=workstation_truth_digest,
            full_plan_audit_context_digest=full_plan_audit_context_digest,
        )
        replay_report = {
            "performed": True,
            "second_status": second["status"],
            "second_stop_reason": second["stop_reason"],
            "second_repair_candidate_digest": second["final_candidate_digest"],
            "second_material_blocker_summary": copy.deepcopy(
                second.get("material_blocker_summary") or {}
            ),
        }
        second_delivered = copy.deepcopy(second["final_candidate"])
        if finalize_full_plan is not None:
            second_delivered, _, _ = finalize_full_plan(second_delivered)
        replay_report["second_final_candidate_digest"] = _digest(second_delivered)
        replay_report["candidate_digest_equal"] = (
            _digest(second_delivered) == _digest(delivered_candidate)
        )
        if not replay_report["candidate_digest_equal"]:
            raise SystemExit("deterministic replay changed the final candidate digest")

    report = {
        "device_state": str(state_path) if state_path else "",
        "provenance": provenance,
        "auditor": args.auditor,
        "no_model": True,
        "real_dispatch": False,
        "progress_index": args.progress_index,
        "status": result["status"],
        "stop_reason": result["stop_reason"],
        "draft_status": result["draft_status"],
        "draft_version": result["case"]["draft"]["version"],
        "patches_used": result["case"]["budgets"]["patches_used"],
        "rounds": len(result["case"]["log"]),
        "remaining_findings": len(result["case"].get("remaining_findings") or []),
        "remaining_finding_records": copy.deepcopy(
            result["case"].get("remaining_findings") or []
        ),
        "progress": result["case"]["progress"],
        "material_blocker_summary": copy.deepcopy(
            result.get("material_blocker_summary") or {}
        ),
        "repair_candidate_digest": result["final_candidate_digest"],
        "final_candidate_digest": _digest(delivered_candidate),
        "baseline_digest": result["case"]["baseline"]["candidate_digest"],
        "pre_certificate_normalization": normalization_report,
        "replay": replay_report,
    }
    if args.output:
        _atomic_write_json(Path(args.output).expanduser(), report)
    if args.candidate_output:
        _atomic_write_json(
            Path(args.candidate_output).expanduser(), delivered_candidate
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
