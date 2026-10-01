"""Compile explicit Research material relationships into Device plan ledgers.

This module is deliberately narrower than the legacy binding-ledger builder.
It never derives a material event from a macro ordinal, workstation category,
operation label, or material name.  Its only scientific authority is the
accepted Research V2 ``material_relations`` graph.  A candidate step's
``research_material_relation_ids`` are only untrusted claims.  A relationship
is bound to an actual Device step only by a versioned, evidence-bound sidecar
whose Research, Device-plan and workstation-truth snapshots still match.

The compiler materializes three deliberately distinct planning forms:

* ``whole_batch`` records symbolic 1 -> 1 lineage without numeric inventory;
* ``conserved_inventory`` records Research-authorized planned allocations and
  checks their conservation without claiming that execution has occurred; and
* ``runtime_measurement_required`` records pending measurement obligations and
  consumption gates, never a fabricated observation or available balance.

All generated IDs are content-stable.  The generated graph is replaced as one
unit, so replay is idempotent and any invalid relation returns the original
candidate without a partial ledger update.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from chem_agent_contracts.identity import decode_package_identity

try:
    from .macro_identity import (
        MacroIdentityError,
        extract_source_macro_ids,
        macro_id_key,
        normalize_macro_id,
    )
except ImportError:  # Direct-script compatibility.
    from macro_identity import (  # type: ignore
        MacroIdentityError,
        extract_source_macro_ids,
        macro_id_key,
        normalize_macro_id,
    )


RULE_ID = "material-relationship-compiler/v1"
SCHEMA_VERSION = 1
RUNTIME_MEASUREMENT_SCHEMA = "chem-runtime-material-measurement-obligation/1"
RUNTIME_MEASUREMENT_SCHEMA_VERSION = 1
RUNTIME_QUANTITY_MODE = "runtime_measurement_required"
SUPPORTED_QUANTITY_BASES = frozenset(
    {"whole_batch", "conserved_inventory", RUNTIME_QUANTITY_MODE}
)
BINDING_SCOPE_VERSION = 3
SKILL_OPERATION_CONTRACT_DIGEST_SCOPE = (
    "workstation-capability-index/station+capability+operation/"
    "canonical_json_utf8_sorted_compact/v2"
)
BINDING_AUTHORITY_SCHEMA = "chem-material-relationship-binding-authority/1"
BINDING_AUTHORITY_SCHEMA_VERSION = 1
MANUAL_SUPPORT_SCHEMA = "chem-material-operation-binding-review/1"
MANUAL_SUPPORT_SCHEMA_VERSION = 1
MANUAL_SUPPORT_CONCLUSION = (
    "manual_evidence_accepts_relation_to_device_operation"
)
BINDING_AUTHORING_MODES = frozenset(
    {"manual_evidence_bound", "automated_evidence_bound"}
)
DEVICE_STEP_DIGEST_SCOPE = (
    "device_step_without_material_relationship_compiler_fields/"
    "canonical_json_utf8_sorted_compact/v1"
)
CANDIDATE_BINDING_DIGEST_SCOPE = (
    "device_plan_without_material_relationship_compiler_fields/"
    "canonical_json_utf8_sorted_compact/v1"
)
_COMPILER_MANAGED_STEP_FIELDS = frozenset(
    {
        "logical_container_bindings",
        "material_event_kind",
        "material_transition_ids",
        "research_material_relation_ids",
        "runtime_measurement_obligation_ids",
    }
)
ALLOWED_EVENT_KINDS = frozenset(
    {
        "none",
        "collect_same_material",
        "state_change",
        "process_same_material",
        "split_same_material",
        "replicate_same_material",
    }
)
MATERIAL_EFFECTS = frozenset(
    {
        "none",
        "register_existing_input",
        "observe_without_material_change",
        "consume_material",
        "produce_material",
        "transform_material",
        "transfer_material",
        "split_material",
        "merge_material",
        "unknown",
    }
)
NON_PROCESSING_MATERIAL_EFFECTS = frozenset(
    {"none", "register_existing_input", "observe_without_material_change"}
)
STATUS_VALUES = frozenset({"declared", "not_applicable", "unresolved"})
STATUS_FIELDS = (
    "material_inputs",
    "material_intermediates",
    "material_outputs",
    "logical_containers",
    "material_relations",
)
_RUNTIME_MEASUREMENT_UNIT_DIMENSIONS = {
    "mol": "amount",
    "mmol": "amount",
    "umol": "amount",
    "nmol": "amount",
    "g": "mass",
    "mg": "mass",
    "ug": "mass",
    "kg": "mass",
    "l": "volume",
    "ml": "volume",
    "ul": "volume",
    "nl": "volume",
}


class RelationshipCompileIssue(dict):
    """One fail-closed compiler diagnostic with an explicit blocker class.

    ``blocker_class`` identifies the layer that must change.  Keeping it
    separate from ``code`` prevents a deterministic compiler limitation from
    being reported as missing Research evidence, or a missing Device operation
    from being reported as an unsupported scientific contract.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        blocker_class: str = "contract_invalid",
        **context: Any,
    ) -> None:
        super().__init__(
            code=code,
            blocker_class=blocker_class,
            message=message,
            context=context,
        )


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    readable = re.sub(r"[^0-9A-Za-z]+", "_", str(parts[-1] or "item")).strip("_")
    readable = (readable[:32] or "item").lower()
    digest = _canonical_digest(list(parts))[:12]
    return f"{prefix}_{readable}_{digest}"


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def normalized_device_step_for_binding(step: Dict[str, Any]) -> Dict[str, Any]:
    """Return the operation content covered by a binding-sidecar digest.

    The relationship compiler writes the fields excluded here.  Omitting
    only those fields makes the digest stable on deterministic replay while
    retaining every pre-existing operation, parameter, workstation, container,
    lineage and provenance field supplied by the Device planner.
    """

    if not isinstance(step, dict):
        raise TypeError("Device step must be an object")
    return {
        key: copy.deepcopy(value)
        for key, value in step.items()
        if key not in _COMPILER_MANAGED_STEP_FIELDS
    }


def device_step_binding_sha256(step: Dict[str, Any]) -> str:
    """Digest the normalized, non-compiler-owned Device operation content."""

    return _canonical_digest(normalized_device_step_for_binding(step))


def candidate_binding_sha256(candidate: Dict[str, Any]) -> str:
    """Digest the ordered Device plan covered by relationship bindings.

    Material-ledger output and audit annotations are deliberately outside this
    authority: a sidecar binds Research relationships to concrete Device
    operations, not to transient audit state.  Every operation field except
    compiler-owned relationship annotations remains covered.
    """

    raw_plan = candidate.get("device_plan") if isinstance(candidate, dict) else None
    if not isinstance(raw_plan, list) or any(
        not isinstance(step, dict) for step in raw_plan
    ):
        raise TypeError("candidate.device_plan must be an array of objects")
    projection = {
        "device_plan": [normalized_device_step_for_binding(step) for step in raw_plan]
    }
    return _canonical_digest(projection)


def _package_from_authority(authority: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(authority, dict):
        return None
    nested = authority.get("research_action_package_v2")
    if isinstance(nested, dict):
        return nested
    if isinstance(authority.get("macro_steps"), list):
        return authority
    return None


def _typed_scalar_key(value: Any, path: str) -> Tuple[str, Any]:
    return macro_id_key(value, path)


def _plan_index(
    candidate: Dict[str, Any],
) -> Tuple[
    List[Dict[str, Any]],
    Dict[Tuple[str, Any], Dict[str, Any]],
    Dict[Tuple[str, Any], Any],
    List[RelationshipCompileIssue],
]:
    issues: List[RelationshipCompileIssue] = []
    raw_plan = candidate.get("device_plan")
    if not isinstance(raw_plan, list):
        return [], {}, {}, [
            RelationshipCompileIssue(
                "invalid_device_plan",
                "device_plan must be an array before relationship compilation",
                path="device_plan",
            )
        ]
    plan: List[Dict[str, Any]] = []
    by_id: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    value_by_id: Dict[Tuple[str, Any], Any] = {}
    for index, raw in enumerate(raw_plan):
        if not isinstance(raw, dict):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_device_plan_step",
                    "device_plan entries must be objects",
                    path=f"device_plan[{index}]",
                )
            )
            continue
        plan.append(raw)
        try:
            key = _typed_scalar_key(
                raw.get("plan_step"), f"device_plan[{index}].plan_step"
            )
        except MacroIdentityError as exc:
            issues.append(
                RelationshipCompileIssue(
                    exc.code.lower(), str(exc), path=exc.path
                )
            )
            continue
        if key in by_id:
            issues.append(
                RelationshipCompileIssue(
                    "duplicate_plan_step_id",
                    "relationship bindings require unique typed plan_step identities",
                    path=f"device_plan[{index}].plan_step",
                    plan_step=copy.deepcopy(raw.get("plan_step")),
                )
            )
            continue
        by_id[key] = raw
        value_by_id[key] = copy.deepcopy(raw.get("plan_step"))
    return plan, by_id, value_by_id, issues


def _physical_container_ids(step: Dict[str, Any]) -> List[Any]:
    raw = step.get("containers")
    values: Any = None
    if isinstance(raw, dict):
        for key in (
            "container_ids",
            "container_id",
            "容器编号",
            "vial_ids",
            "vial_id",
            "瓶号",
        ):
            if key in raw:
                values = raw[key]
                break
    elif isinstance(raw, list):
        values = raw
    if values is None:
        for key in ("container_ids", "container_id", "容器编号"):
            if key in step:
                values = step[key]
                break
    # Work on a detached list.  ``sample_lineage`` endpoints are additional
    # lookup candidates; appending them must never mutate the candidate's
    # containers in an audit-only or failed atomic compile.
    result = (
        copy.deepcopy(values)
        if isinstance(values, list)
        else ([] if values is None else [copy.deepcopy(values)])
    )
    # A transfer step's exact physical endpoints are represented by the
    # structured sample_lineage contract, not necessarily duplicated in
    # step.containers.  These are explicit IDs, so admitting them adds no
    # key-value/name/category inference.
    lineage = step.get("sample_lineage")
    if isinstance(lineage, dict):
        for endpoint in ("source_container", "destination_container"):
            record = lineage.get(endpoint)
            if isinstance(record, dict):
                result.append(record.get("container_id"))
    normalized: List[Any] = []
    seen: set[Tuple[str, str]] = set()
    for value in result:
        if value is None or isinstance(value, (dict, list, bool)):
            continue
        token = _container_token(value)
        if token in seen:
            continue
        seen.add(token)
        normalized.append(copy.deepcopy(value))
    return normalized


def _container_token(value: Any) -> Tuple[str, str]:
    return (
        type(value).__name__,
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
    )


def _binding_record(raw: Any) -> Tuple[Any, Any]:
    if isinstance(raw, dict):
        logical_bindings = raw.get("logical_container_bindings")
        return raw.get("plan_step"), copy.deepcopy(
            {} if logical_bindings is None else logical_bindings
        )
    return None, {}


def _automated_binding_scope_issues(
    raw_binding: Any,
    *,
    relationship_id: str,
    relation: Dict[str, Any],
    plan_by_id: Dict[Tuple[str, Any], Dict[str, Any]],
    plan_values: Dict[Tuple[str, Any], Any],
    resolve_workstation: Optional[Callable[[str], str]],
) -> Tuple[List[Tuple[str, Any]], List[RelationshipCompileIssue]]:
    """Validate the complete Device implementation scope for one relation.

    The material transition is committed by exactly one step, but the Research
    operation segment may require several ordered Device capabilities.  This
    check binds every role to a concrete step and its frozen Skill-contract
    digest.  It deliberately does not infer a role from a workstation name or
    free text.
    """

    issues: List[RelationshipCompileIssue] = []
    if not isinstance(raw_binding, dict):
        return [], [
            RelationshipCompileIssue(
                "relationship_binding_scope_missing",
                "automated binding requires a structured implementation scope",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        ]
    if raw_binding.get("binding_scope_version") != BINDING_SCOPE_VERSION:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_scope_version_invalid",
                "automated binding uses an unsupported implementation-scope version",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                expected=BINDING_SCOPE_VERSION,
                actual=copy.deepcopy(raw_binding.get("binding_scope_version")),
            )
        )

    operation_segment = relation.get("operation_segment")
    implementation = (
        operation_segment.get("device_implementation")
        if isinstance(operation_segment, dict)
        else None
    )
    raw_roles = (
        implementation.get("ordered_steps")
        if isinstance(implementation, dict)
        else None
    )
    research_commit_role = (
        str(implementation.get("commit_role_id") or "").strip()
        if isinstance(implementation, dict)
        else ""
    )
    roles: List[Dict[str, str]] = []
    if not isinstance(raw_roles, list) or not raw_roles or not research_commit_role:
        issues.append(
            RelationshipCompileIssue(
                "research_device_implementation_invalid",
                "automated binding requires ordered capability roles and a commit role",
                blocker_class="contract_invalid",
                relationship_id=relationship_id,
            )
        )
    else:
        for role_index, raw_role in enumerate(raw_roles):
            role = (
                {
                    "role_id": str(raw_role.get("role_id") or "").strip(),
                    "capability_id": str(
                        raw_role.get("capability_id") or ""
                    ).strip(),
                }
                if isinstance(raw_role, dict)
                else {"role_id": "", "capability_id": ""}
            )
            if not role["role_id"] or not role["capability_id"]:
                issues.append(
                    RelationshipCompileIssue(
                        "research_device_implementation_role_invalid",
                        "every implementation role requires immutable role and capability IDs",
                        blocker_class="contract_invalid",
                        relationship_id=relationship_id,
                        role_index=role_index,
                    )
                )
            roles.append(role)
        role_ids = [role["role_id"] for role in roles]
        if (
            len(role_ids) != len(set(role_ids))
            or research_commit_role not in set(role_ids)
        ):
            issues.append(
                RelationshipCompileIssue(
                    "research_device_implementation_commit_invalid",
                    "implementation role IDs must be unique and select one commit role",
                    blocker_class="contract_invalid",
                    relationship_id=relationship_id,
                )
            )

    implementation_steps = raw_binding.get("implementation_steps")
    implementation_evidence = raw_binding.get("implementation_step_evidence")
    commit_step = raw_binding.get("commit_step")
    plan_step = raw_binding.get("plan_step")
    commit_role_id = str(raw_binding.get("commit_role_id") or "").strip()
    if (
        not isinstance(implementation_steps, list)
        or not implementation_steps
        or not isinstance(implementation_evidence, list)
        or len(implementation_evidence) != len(implementation_steps)
        or len(implementation_steps) != len(roles)
    ):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_scope_shape_invalid",
                "implementation steps and evidence must cover every Research capability role exactly once",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )
        return [], issues
    if commit_step != plan_step or commit_role_id != research_commit_role:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_commit_mismatch",
                "plan_step must equal commit_step and commit_role_id must match Research authority",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )

    step_keys: List[Tuple[str, Any]] = []
    for scope_index, (scope_step, evidence, role) in enumerate(
        zip(implementation_steps, implementation_evidence, roles)
    ):
        try:
            step_key = macro_id_key(
                scope_step,
                f"bindings.{relationship_id}.implementation_steps[{scope_index}]",
            )
        except MacroIdentityError as exc:
            issues.append(
                RelationshipCompileIssue(
                    exc.code.lower(),
                    str(exc),
                    blocker_class="plan_binding_invalid",
                    path=exc.path,
                    relationship_id=relationship_id,
                )
            )
            continue
        step_keys.append(step_key)
        step = plan_by_id.get(step_key)
        if step is None:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_scope_step_missing",
                    "implementation scope names a Device step absent from the candidate",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    plan_step=copy.deepcopy(scope_step),
                )
            )
            continue
        if not isinstance(evidence, dict):
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_scope_evidence_invalid",
                    "every implementation step requires structured capability evidence",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    scope_index=scope_index,
                )
            )
            continue
        raw_workstation = str(step.get("workstation") or "").strip()
        resolved_workstation = (
            str(resolve_workstation(raw_workstation) or "").strip()
            if resolve_workstation is not None
            else ""
        )
        mismatches: List[str] = []
        source_operation_ref = str(
            relation.get("source_operation_ref") or ""
        ).strip()
        if evidence.get("source_operation_ref") != source_operation_ref:
            mismatches.append("source_operation_ref")
        if evidence.get("role_id") != role["role_id"]:
            mismatches.append("role_id")
        if evidence.get("plan_step") != scope_step:
            mismatches.append("plan_step")
        if evidence.get("device_step_sha256") != device_step_binding_sha256(step):
            mismatches.append("device_step_sha256")
        if evidence.get("station_code") != resolved_workstation:
            mismatches.append("station_code")
        if evidence.get("capability_id") != role["capability_id"]:
            mismatches.append("capability_id")
        if not isinstance(evidence.get("skill_operation_name"), str) or not str(
            evidence.get("skill_operation_name") or ""
        ).strip():
            mismatches.append("skill_operation_name")
        if evidence.get("skill_contract_digest_scope") != (
            SKILL_OPERATION_CONTRACT_DIGEST_SCOPE
        ):
            mismatches.append("skill_contract_digest_scope")
        if not _is_sha256(evidence.get("skill_contract_sha256")):
            mismatches.append("skill_contract_sha256")
        if not isinstance(evidence.get("support_evidence_ref"), str):
            mismatches.append("support_evidence_ref")

        raw_claims = step.get("operation_capabilities")
        exact_claims = []
        if isinstance(raw_claims, list):
            exact_claims = [
                claim
                for claim in raw_claims
                if isinstance(claim, dict)
                and str(claim.get("source_operation_ref") or "").strip()
                == source_operation_ref
                and str(claim.get("role_id") or "").strip()
                == role["role_id"]
                and str(claim.get("capability_id") or "").strip()
                == role["capability_id"]
                and str(claim.get("skill_operation_name") or "").strip()
                == str(evidence.get("skill_operation_name") or "").strip()
            ]
        if len(exact_claims) != 1:
            mismatches.append("device_operation_capability_claim")
        if mismatches:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_scope_evidence_mismatch",
                    "implementation-step evidence does not match the current Device step, Research role, or frozen Skill contract",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    scope_index=scope_index,
                    plan_step=copy.deepcopy(plan_values.get(step_key, scope_step)),
                    mismatched_fields=mismatches,
                )
            )

        raw_assignments = step.get("logical_container_assignments")
        if raw_assignments is None:
            raw_assignments = []
        if not isinstance(raw_assignments, list):
            issues.append(
                RelationshipCompileIssue(
                    "device_logical_container_assignments_invalid",
                    "logical_container_assignments must be an array",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    plan_step=copy.deepcopy(scope_step),
                )
            )
        else:
            step_physical_tokens = {
                _container_token(value) for value in _physical_container_ids(step)
            }
            for assignment_index, assignment in enumerate(raw_assignments):
                physical_values = (
                    assignment.get("physical_container_ids")
                    if isinstance(assignment, dict)
                    else None
                )
                if not isinstance(physical_values, list) or not physical_values:
                    issues.append(
                        RelationshipCompileIssue(
                            "device_logical_container_assignment_invalid",
                            "each logical-container assignment requires a nonempty physical_container_ids array",
                            blocker_class="plan_binding_invalid",
                            relationship_id=relationship_id,
                            plan_step=copy.deepcopy(scope_step),
                            assignment_index=assignment_index,
                        )
                    )
                    continue
                out_of_scope = [
                    copy.deepcopy(value)
                    for value in physical_values
                    if _container_token(value) not in step_physical_tokens
                ]
                if out_of_scope:
                    issues.append(
                        RelationshipCompileIssue(
                            "logical_container_assignment_not_used_by_declaring_step",
                            "a logical-container assignment may name only physical containers used by the exact Device step that declares it",
                            blocker_class="plan_binding_invalid",
                            relationship_id=relationship_id,
                            plan_step=copy.deepcopy(scope_step),
                            logical_container_id=(
                                assignment.get("logical_container_id")
                                if isinstance(assignment, dict)
                                else None
                            ),
                            out_of_scope=out_of_scope,
                        )
                    )

    if len(step_keys) != len(set(step_keys)):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_scope_reuses_step",
                "one Device step cannot satisfy multiple roles in one implementation scope",
                blocker_class="plan_binding_ambiguous",
                relationship_id=relationship_id,
            )
        )
    plan_order = {key: index for index, key in enumerate(plan_by_id)}
    scope_order = [plan_order[key] for key in step_keys if key in plan_order]
    if len(scope_order) == len(step_keys) and scope_order != sorted(scope_order):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_scope_order_mismatch",
                "implementation scope order must match both Research role order and Device-plan order",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )
    if commit_step in implementation_steps and roles:
        commit_index = next(
            (
                index
                for index, role in enumerate(roles)
                if role["role_id"] == research_commit_role
            ),
            -1,
        )
        if commit_index < 0 or implementation_steps[commit_index] != commit_step:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_commit_position_invalid",
                    "commit_step must occupy the Research-designated commit role",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                )
            )
    else:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_commit_out_of_scope",
                "commit_step must be one of the implementation steps",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )

    operation_evidence = raw_binding.get("operation_evidence")
    commit_evidence = next(
        (
            evidence
            for evidence in implementation_evidence
            if isinstance(evidence, dict)
            and evidence.get("role_id") == research_commit_role
        ),
        None,
    )
    if not isinstance(operation_evidence, dict) or not isinstance(
        commit_evidence, dict
    ):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_commit_evidence_missing",
                "commit operation evidence must derive from the designated implementation-step evidence",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )
    else:
        commit_fields = {
            "machine_readable_capability_id": "capability_id",
            "skill_contract_sha256": "skill_contract_sha256",
            "skill_contract_digest_scope": "skill_contract_digest_scope",
            "support_evidence_ref": "support_evidence_ref",
        }
        mismatches = [
            field
            for field, evidence_field in commit_fields.items()
            if operation_evidence.get(field) != commit_evidence.get(evidence_field)
        ]
        if mismatches:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_commit_evidence_mismatch",
                    "commit operation evidence must exactly match its implementation-step evidence",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    mismatched_fields=mismatches,
                )
            )
    if str(relation.get("quantity_basis") or "") == RUNTIME_QUANTITY_MODE:
        runtime_support = [
            evidence.get("runtime_quantity_support")
            for evidence in implementation_evidence
            if isinstance(evidence, dict)
            and isinstance(evidence.get("runtime_quantity_support"), dict)
        ]
        measurement_support = [
            support
            for support in runtime_support
            if support.get("actual_inventory_measurement_declared") is True
            and isinstance(support.get("reported_measurements"), list)
            and bool(support.get("reported_measurements"))
            and all(
                isinstance(value, str) and value.strip()
                for value in support.get("reported_measurements")
            )
            and isinstance(
                support.get("reported_measurement_contracts"), list
            )
            and bool(support.get("reported_measurement_contracts"))
        ]
        stop_support = [
            support
            for support in runtime_support
            if support.get("insufficient_quantity_stop_declared") is True
            and support.get("insufficient_quantity_stop_policy")
            == "stop_before_material_consumption"
        ]
        if len(measurement_support) != 1 or len(stop_support) != 1:
            issues.append(
                RelationshipCompileIssue(
                    "runtime_quantity_skill_support_unresolved",
                    "runtime quantity compilation requires exactly one truth-bound actual-inventory reporter and one stop-before-consumption controller",
                    blocker_class=(
                        "plan_binding_ambiguous"
                        if len(measurement_support) > 1 or len(stop_support) > 1
                        else "software_unsupported"
                    ),
                    relationship_id=relationship_id,
                    missing_support=[
                        name
                        for present, name in (
                            (measurement_support, "actual_inventory_measurement"),
                            (stop_support, "insufficient_quantity_stop"),
                        )
                        if not present
                    ],
                    ambiguous_support=[
                        name
                        for values, name in (
                            (measurement_support, "actual_inventory_measurement"),
                            (stop_support, "insufficient_quantity_stop"),
                        )
                        if len(values) > 1
                    ],
                )
            )
    return step_keys, issues


def _binding_status(raw: Any) -> Tuple[str, str]:
    """Read an optional explicit positive/negative sidecar disposition."""

    if not isinstance(raw, dict):
        return "invalid", ""
    raw_status = raw.get("binding_status")
    raw_disposition = raw.get("disposition")
    raw_required_capability = raw.get("required_capability")
    if raw_status is not None and not isinstance(raw_status, str):
        return "invalid", ""
    if raw_disposition is not None and not isinstance(raw_disposition, str):
        return "invalid", ""
    if raw_required_capability is not None and not isinstance(
        raw_required_capability, str
    ):
        return "invalid", ""
    binding_status = (raw_status or "").strip()
    disposition = (raw_disposition or "").strip()
    if binding_status and disposition and binding_status != disposition:
        return "conflict", ""
    status = binding_status or disposition or "bound"
    required_capability = (raw_required_capability or "").strip()
    return status, required_capability


def relationship_binding_records(authority: Any) -> Dict[str, Any]:
    """Return records only from the versioned binding-authority envelope.

    This helper is intentionally strict.  Callers may use it to discover the
    records carried by a frozen repair case, but an old bare ``relation ->
    step`` map never silently becomes authoritative.
    """

    if not isinstance(authority, dict):
        return {}
    if authority.get("schema") != BINDING_AUTHORITY_SCHEMA:
        return {}
    records = authority.get("bindings")
    return copy.deepcopy(records) if isinstance(records, dict) else {}


def _expected_manual_support_record(
    *,
    relationship_id: str,
    plan_step: Any,
    source_operation_ref: str,
    operation_segment: Dict[str, Any],
    step: Dict[str, Any],
    resolved_workstation: str,
    workstation_truth_digest: str,
) -> Dict[str, Any]:
    """Return fields a reviewed manual operation-binding record must freeze.

    The record is evidence of an explicit human/maintainer decision, not a
    machine-derived capability result.  Extra reviewer rationale is allowed in
    the source document and is covered by the record digest.
    """

    return {
        "review_id": f"manual-binding:{relationship_id}",
        "relationship_id": relationship_id,
        "binding_status": "bound",
        "plan_step": copy.deepcopy(plan_step),
        "source_operation_ref": source_operation_ref,
        "research_operation_segment_sha256": _canonical_digest(
            operation_segment
        ),
        "device_step_sha256": device_step_binding_sha256(step),
        "workstation_truth_sha256": workstation_truth_digest,
        "operation_selector": {
            "plan_step": copy.deepcopy(plan_step),
            "workstation": str(step.get("workstation") or "").strip(),
            "resolved_workstation": resolved_workstation,
            "operation_intent": str(step.get("operation_intent") or "").strip(),
        },
        "support_conclusion": MANUAL_SUPPORT_CONCLUSION,
        "automation_claim": False,
    }


def _validate_binding_authority(
    authority: Any,
    *,
    candidate: Dict[str, Any],
    package: Dict[str, Any],
    workstation_truth_digest: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[RelationshipCompileIssue]]:
    """Validate the versioned evidence envelope before reading any binding."""

    if authority in (None, {}):
        return {}, {}, []
    if not isinstance(authority, dict):
        return {}, {}, [
            RelationshipCompileIssue(
                "invalid_relationship_binding_authority",
                "relationship binding authority must be an object",
                blocker_class="contract_invalid",
            )
        ]
    if authority.get("schema") != BINDING_AUTHORITY_SCHEMA:
        return {}, {}, [
            RelationshipCompileIssue(
                "unversioned_relationship_binding_authority",
                "a nonempty relationship sidecar must use the versioned evidence-bound authority schema",
                blocker_class="contract_invalid",
                expected_schema=BINDING_AUTHORITY_SCHEMA,
            )
        ]
    if authority.get("schema_version") != BINDING_AUTHORITY_SCHEMA_VERSION:
        return {}, {}, [
            RelationshipCompileIssue(
                "relationship_binding_authority_version_mismatch",
                "relationship binding authority schema version is unsupported",
                blocker_class="contract_invalid",
                expected_schema_version=BINDING_AUTHORITY_SCHEMA_VERSION,
                actual_schema_version=copy.deepcopy(authority.get("schema_version")),
            )
        ]

    issues: List[RelationshipCompileIssue] = []
    authoring_mode = str(authority.get("authoring_mode") or "").strip()
    automation_claim = authority.get("automation_claim")
    if authoring_mode not in BINDING_AUTHORING_MODES:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_authoring_mode_invalid",
                "binding authority requires an explicit supported authoring mode",
                blocker_class="contract_invalid",
                authoring_mode=authoring_mode or "<missing>",
            )
        )
    if not isinstance(automation_claim, bool) or automation_claim != (
        authoring_mode == "automated_evidence_bound"
    ):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_automation_claim_invalid",
                "automation_claim must be false for manual evidence and true for automated evidence",
                blocker_class="contract_invalid",
                authoring_mode=authoring_mode or "<missing>",
                automation_claim=copy.deepcopy(automation_claim),
            )
        )

    evidence_sources = authority.get("evidence_sources")
    required_sources = [
        "research_authority",
        "device_candidate",
        "workstation_truth",
    ]
    if authoring_mode == "manual_evidence_bound":
        required_sources.append("manual_support")
    if not isinstance(evidence_sources, dict) or any(
        not isinstance(evidence_sources.get(key), str)
        or not evidence_sources[key].strip()
        for key in required_sources
    ):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_evidence_sources_invalid",
                "binding authority requires every evidence source for its authoring mode",
                blocker_class="contract_invalid",
                required_sources=list(required_sources),
            )
        )
    elif authoring_mode == "manual_evidence_bound":
        manual_support_source = evidence_sources["manual_support"].strip()
        other_sources = {
            evidence_sources[key].strip()
            for key in ("research_authority", "device_candidate", "workstation_truth")
        }
        if manual_support_source in other_sources:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_source_not_independent",
                    "manual support must name a distinct reviewed evidence source, not the Research package, Device candidate, or workstation snapshot",
                    blocker_class="contract_invalid",
                )
            )
        if authority.get("manual_support_schema") != MANUAL_SUPPORT_SCHEMA:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_schema_invalid",
                    "manual binding authority must identify the reviewed support-document schema",
                    blocker_class="contract_invalid",
                    expected=MANUAL_SUPPORT_SCHEMA,
                    actual=copy.deepcopy(authority.get("manual_support_schema")),
                )
            )
        if not _is_sha256(authority.get("manual_support_document_sha256")):
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_digest_invalid",
                    "manual binding authority must freeze the reviewed support document digest",
                    blocker_class="contract_invalid",
                )
            )
        manual_record_digests = authority.get("manual_support_record_sha256")
        if not isinstance(manual_record_digests, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or not _is_sha256(value)
            for key, value in (
                manual_record_digests.items()
                if isinstance(manual_record_digests, dict)
                else []
            )
        ):
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_record_digests_invalid",
                    "manual binding authority must freeze each reviewed operation-binding record digest",
                    blocker_class="contract_invalid",
                )
            )
        manual_support_records = authority.get("manual_support_records")
        if not isinstance(manual_support_records, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, dict)
            for key, value in (
                manual_support_records.items()
                if isinstance(manual_support_records, dict)
                else []
            )
        ):
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_records_invalid",
                    "manual binding authority must embed each reviewed operation-binding record",
                    blocker_class="contract_invalid",
                )
            )
        elif isinstance(manual_record_digests, dict):
            digest_mismatches = sorted(
                relationship_id
                for relationship_id, record in manual_support_records.items()
                if manual_record_digests.get(relationship_id)
                != _canonical_digest(record)
            )
            if digest_mismatches:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_binding_manual_support_record_digest_mismatch",
                        "embedded manual support records do not match their frozen digests",
                        blocker_class="contract_invalid",
                        relationship_ids=digest_mismatches,
                    )
                )

    expected_research_digest = _canonical_digest(package)
    if authority.get("research_authority_sha256") != expected_research_digest:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_research_digest_mismatch",
                "binding authority does not match the accepted Research package",
                blocker_class="plan_binding_invalid",
                expected=expected_research_digest,
                actual=copy.deepcopy(authority.get("research_authority_sha256")),
            )
        )
    if authority.get("candidate_digest_scope") != CANDIDATE_BINDING_DIGEST_SCOPE:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_candidate_digest_scope_invalid",
                "binding authority candidate digest uses an unsupported scope",
                blocker_class="contract_invalid",
                expected=CANDIDATE_BINDING_DIGEST_SCOPE,
                actual=copy.deepcopy(authority.get("candidate_digest_scope")),
            )
        )
    try:
        expected_candidate_digest = candidate_binding_sha256(candidate)
    except (TypeError, ValueError) as exc:
        expected_candidate_digest = ""
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_candidate_invalid",
                str(exc),
                blocker_class="contract_invalid",
            )
        )
    if expected_candidate_digest and authority.get("candidate_sha256") != (
        expected_candidate_digest
    ):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_candidate_digest_mismatch",
                "binding authority does not match the current Device-plan operation content",
                blocker_class="plan_binding_invalid",
                expected=expected_candidate_digest,
                actual=copy.deepcopy(authority.get("candidate_sha256")),
            )
        )
    frozen_truth = str(authority.get("workstation_truth_sha256") or "").strip()
    current_truth = str(workstation_truth_digest or "").strip()
    if not frozen_truth or not current_truth:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_workstation_truth_missing",
                "binding authority and compiler call both require a workstation truth digest",
                blocker_class="contract_invalid",
            )
        )
    elif frozen_truth != current_truth:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_workstation_truth_mismatch",
                "binding authority was authored against a different workstation truth snapshot",
                blocker_class="plan_binding_invalid",
                expected=frozen_truth,
                actual=current_truth,
            )
        )

    records = authority.get("bindings")
    if not isinstance(records, dict):
        issues.append(
            RelationshipCompileIssue(
                "invalid_relationship_bindings",
                "binding authority bindings must be an object keyed by relationship_id",
                blocker_class="contract_invalid",
            )
        )
        records = {}
    if authoring_mode == "manual_evidence_bound" and isinstance(
        authority.get("manual_support_record_sha256"), dict
    ):
        positive_record_ids = {
            str(relationship_id)
            for relationship_id, record in records.items()
            if _binding_status(record)[0] == "bound"
        }
        digest_record_ids = set(authority["manual_support_record_sha256"])
        if digest_record_ids != positive_record_ids:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_record_set_mismatch",
                    "manual support record digests must cover exactly the positive binding records",
                    blocker_class="contract_invalid",
                    missing=sorted(positive_record_ids - digest_record_ids),
                    unexpected=sorted(digest_record_ids - positive_record_ids),
                )
            )
        embedded_record_ids = (
            set(authority["manual_support_records"])
            if isinstance(authority.get("manual_support_records"), dict)
            else set()
        )
        if embedded_record_ids != positive_record_ids:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_embedded_record_set_mismatch",
                    "embedded manual support records must cover exactly the positive binding records",
                    blocker_class="contract_invalid",
                    missing=sorted(positive_record_ids - embedded_record_ids),
                    unexpected=sorted(embedded_record_ids - positive_record_ids),
                )
            )
    return copy.deepcopy(authority), copy.deepcopy(records), issues


def _positive_binding_evidence_issues(
    raw_binding: Any,
    *,
    envelope: Dict[str, Any],
    relationship_id: str,
    relation: Dict[str, Any],
    step: Dict[str, Any],
    plan_step: Any,
    resolved_workstation: str,
) -> List[RelationshipCompileIssue]:
    """Verify the evidence that authorizes one positive operation binding."""

    issues: List[RelationshipCompileIssue] = []
    evidence = (
        raw_binding.get("operation_evidence")
        if isinstance(raw_binding, dict)
        else None
    )
    if not isinstance(evidence, dict):
        return [
            RelationshipCompileIssue(
                "relationship_binding_operation_evidence_missing",
                "a positive binding requires structured operation_evidence",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                plan_step=copy.deepcopy(plan_step),
            )
        ]

    if evidence.get("schema_version") != 1:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_operation_evidence_version_invalid",
                "operation evidence schema_version must be 1",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                actual=copy.deepcopy(evidence.get("schema_version")),
            )
        )

    authoring_mode = str(envelope.get("authoring_mode") or "").strip()
    automation_claim = envelope.get("automation_claim")
    if evidence.get("evidence_mode") != authoring_mode or evidence.get(
        "automation_claim"
    ) is not automation_claim:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_evidence_attribution_mismatch",
                "operation evidence attribution must match the binding authority envelope",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )
    envelope_sources = envelope.get("evidence_sources")
    expected_evidence_source = (
        {
            "research_authority_ref": (
                f"{envelope_sources['research_authority']}"
                f"#material_relation:{relationship_id}"
            ),
            "device_candidate_ref": (
                f"{envelope_sources['device_candidate']}"
                f"#plan_step:{json.dumps(plan_step, ensure_ascii=False, separators=(',', ':'))}"
            ),
        }
        if isinstance(envelope_sources, dict)
        and isinstance(envelope_sources.get("research_authority"), str)
        and isinstance(envelope_sources.get("device_candidate"), str)
        else None
    )
    evidence_source = evidence.get("evidence_source")
    if evidence_source != expected_evidence_source:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_operation_evidence_source_mismatch",
                "operation evidence source references must match the frozen authority sources",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                expected=expected_evidence_source,
                actual=copy.deepcopy(evidence_source),
            )
        )
    if evidence.get("source_operation_ref") != relation["source_operation_ref"]:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_source_operation_ref_mismatch",
                "operation evidence must freeze the exact Research source_operation_ref",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                expected=relation["source_operation_ref"],
                actual=copy.deepcopy(evidence.get("source_operation_ref")),
            )
        )
    operation_segment = relation.get("operation_segment")
    expected_segment_evidence = (
        {
            "segment_id": relation["source_operation_ref"],
            "material_effect": copy.deepcopy(
                operation_segment.get("material_effect")
            ),
            "source_operation_ref": copy.deepcopy(
                operation_segment.get("source_operation_ref")
            ),
            "segment_sha256": _canonical_digest(operation_segment),
        }
        if isinstance(operation_segment, dict)
        else None
    )
    if evidence.get("research_operation_segment") != expected_segment_evidence:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_operation_segment_mismatch",
                "operation evidence must freeze the exact Research segment and material effect",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                expected=expected_segment_evidence,
                actual=copy.deepcopy(evidence.get("research_operation_segment")),
            )
        )
    if evidence.get("device_step_digest_scope") != DEVICE_STEP_DIGEST_SCOPE:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_device_step_digest_scope_invalid",
                "operation evidence uses an unsupported Device-step digest scope",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                expected=DEVICE_STEP_DIGEST_SCOPE,
                actual=copy.deepcopy(evidence.get("device_step_digest_scope")),
            )
        )
    expected_step_digest = device_step_binding_sha256(step)
    if evidence.get("device_step_sha256") != expected_step_digest:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_device_step_digest_mismatch",
                "the bound Device operation content changed after evidence was authored",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                plan_step=copy.deepcopy(plan_step),
                expected=expected_step_digest,
                actual=copy.deepcopy(evidence.get("device_step_sha256")),
            )
        )

    workstation = str(step.get("workstation") or "").strip()
    operation_intent = str(step.get("operation_intent") or "").strip()
    selector = evidence.get("operation_selector")
    expected_selector = {
        "plan_step": copy.deepcopy(plan_step),
        "workstation": workstation,
        "resolved_workstation": resolved_workstation,
        "operation_intent": operation_intent,
    }
    if selector != expected_selector:
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_operation_selector_mismatch",
                "structured operation selector does not match the current Device step and workstation truth",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
                expected=expected_selector,
                actual=copy.deepcopy(selector),
            )
        )
    if evidence.get("workstation_truth_sha256") != envelope.get(
        "workstation_truth_sha256"
    ):
        issues.append(
            RelationshipCompileIssue(
                "relationship_binding_operation_truth_mismatch",
                "operation evidence must bind the envelope workstation truth snapshot",
                blocker_class="plan_binding_invalid",
                relationship_id=relationship_id,
            )
        )

    support_verification = str(
        evidence.get("support_verification") or ""
    ).strip()
    capability_id = evidence.get("machine_readable_capability_id")
    skill_contract_sha256 = evidence.get("skill_contract_sha256")
    if authoring_mode == "manual_evidence_bound":
        support_evidence_ref = str(
            evidence.get("support_evidence_ref") or ""
        ).strip()
        manual_support_source = (
            str(envelope_sources.get("manual_support") or "").strip()
            if isinstance(envelope_sources, dict)
            else ""
        )
        manual_record_id = str(
            evidence.get("manual_support_record_id") or ""
        ).strip()
        manual_record_digest = str(
            evidence.get("manual_support_record_sha256") or ""
        ).strip()
        frozen_record_digests = envelope.get("manual_support_record_sha256")
        embedded_records = envelope.get("manual_support_records")
        embedded_record = (
            embedded_records.get(relationship_id)
            if isinstance(embedded_records, dict)
            else None
        )
        expected_manual_record = _expected_manual_support_record(
            relationship_id=relationship_id,
            plan_step=plan_step,
            source_operation_ref=relation["source_operation_ref"],
            operation_segment=relation["operation_segment"],
            step=step,
            resolved_workstation=resolved_workstation,
            workstation_truth_digest=str(
                envelope.get("workstation_truth_sha256") or ""
            ).strip(),
        )
        embedded_record_mismatches = (
            [
                key
                for key, expected_value in expected_manual_record.items()
                if embedded_record.get(key) != expected_value
            ]
            if isinstance(embedded_record, dict)
            else list(expected_manual_record)
        )
        expected_support_ref = (
            f"{manual_support_source}#manual-binding:{relationship_id}"
            if manual_support_source
            else ""
        )
        if (
            support_verification != "manual_evidence_bound"
            or support_evidence_ref != expected_support_ref
            or not manual_support_source
            or manual_record_id != relationship_id
            or not isinstance(frozen_record_digests, dict)
            or frozen_record_digests.get(relationship_id)
            != manual_record_digest
            or not _is_sha256(manual_record_digest)
            or not isinstance(embedded_record, dict)
            or _canonical_digest(embedded_record) != manual_record_digest
            or bool(embedded_record_mismatches)
        ):
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_manual_support_evidence_invalid",
                    "manual operation support must resolve to the frozen reviewed record and digest",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    expected_support_evidence_ref=expected_support_ref,
                    embedded_record_mismatches=embedded_record_mismatches,
                )
            )
    elif authoring_mode == "automated_evidence_bound":
        if (
            support_verification != "machine_readable_skill_contract"
            or not isinstance(capability_id, str)
            or not capability_id.strip()
            or not _is_sha256(skill_contract_sha256)
            or evidence.get("skill_contract_digest_scope")
            != SKILL_OPERATION_CONTRACT_DIGEST_SCOPE
            or not isinstance(evidence.get("support_evidence_ref"), str)
        ):
            issues.append(
                RelationshipCompileIssue(
                    "relationship_binding_automated_support_evidence_invalid",
                    "automated operation support requires a capability ID and a scoped Skill contract SHA-256 digest",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                )
            )
    return issues


def seal_relationship_binding_authority(
    candidate: Dict[str, Any],
    research_authority: Dict[str, Any],
    draft_bindings: Dict[str, Any],
    *,
    authoring_mode: str,
    automation_claim: bool,
    evidence_sources: Dict[str, str],
    workstation_truth_digest: str,
    resolve_workstation: Callable[[str], str],
    manual_support_document: Optional[Dict[str, Any]] = None,
    workstation_capability_index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Seal explicit binding decisions against immutable authority snapshots.

    Manual mode converts an independently reviewed decision into a verifiable
    sidecar.  Automated mode never trusts caller-authored drafts: it resolves
    the canonical records again from the current Research package, Device
    candidate and complete capability index, then requires an exact match.
    """

    package = _package_from_authority(research_authority)
    if package is None:
        raise ValueError("accepted Research V2 package is required")
    if authoring_mode not in BINDING_AUTHORING_MODES:
        raise ValueError("unsupported binding authoring_mode")
    if not isinstance(automation_claim, bool) or automation_claim != (
        authoring_mode == "automated_evidence_bound"
    ):
        raise ValueError("automation_claim conflicts with authoring_mode")
    required_sources = [
        "research_authority",
        "device_candidate",
        "workstation_truth",
    ]
    if authoring_mode == "manual_evidence_bound":
        required_sources.append("manual_support")
    if not isinstance(evidence_sources, dict) or any(
        not isinstance(evidence_sources.get(key), str)
        or not evidence_sources[key].strip()
        for key in required_sources
    ):
        raise ValueError(
            "evidence_sources must identify every source required by the authoring mode"
        )
    if authoring_mode == "manual_evidence_bound":
        manual_support_source = evidence_sources["manual_support"].strip()
        other_sources = {
            evidence_sources[key].strip()
            for key in ("research_authority", "device_candidate", "workstation_truth")
        }
        if manual_support_source in other_sources:
            raise ValueError(
                "manual_support must be distinct from Research, Device-candidate, and workstation-truth sources"
            )
    if not isinstance(draft_bindings, dict):
        raise ValueError("draft_bindings must be an object")
    frozen_truth = str(workstation_truth_digest or "").strip()
    if not frozen_truth:
        raise ValueError("workstation_truth_digest is required")
    research_authority_digest = _canonical_digest(package)
    frozen_candidate_digest = candidate_binding_sha256(candidate)

    if authoring_mode == "automated_evidence_bound":
        if not isinstance(workstation_capability_index, dict):
            raise ValueError(
                "automated_evidence_bound sealing requires the current complete workstation capability index"
            )
        try:
            try:
                from .material_operation_binding import (
                    resolve_material_operation_bindings,
                )
            except ImportError:  # Direct-script compatibility.
                from material_operation_binding import (  # type: ignore
                    resolve_material_operation_bindings,
                )
            canonical_drafts, resolver_issues = resolve_material_operation_bindings(
                candidate,
                research_authority,
                workstation_capability_index,
                resolve_workstation=resolve_workstation,
            )
        except Exception as exc:
            raise ValueError(
                "automated relationship binding could not be recomputed from current truth: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if resolver_issues:
            raise ValueError(
                "automated relationship binding resolver rejected the current candidate: "
                + str([dict(issue) for issue in resolver_issues])
            )
        if canonical_drafts != draft_bindings:
            raise ValueError(
                "automated relationship binding drafts do not exactly match the current deterministic resolver output"
            )
        draft_bindings = canonical_drafts

    manual_support_records: Dict[str, Any] = {}
    manual_support_document_digest = ""
    if authoring_mode == "manual_evidence_bound":
        if not isinstance(manual_support_document, dict):
            raise ValueError(
                "manual_evidence_bound sealing requires the reviewed manual support document"
            )
        if (
            manual_support_document.get("operation_binding_review_schema")
            != MANUAL_SUPPORT_SCHEMA
            or manual_support_document.get("operation_binding_review_schema_version")
            != MANUAL_SUPPORT_SCHEMA_VERSION
            or manual_support_document.get("operation_binding_review_authoring_mode")
            != "manual_evidence_bound"
            or manual_support_document.get("operation_binding_review_automation_claim")
            is not False
        ):
            raise ValueError(
                "manual support document lacks the required manual operation-binding review schema and attribution"
            )
        if manual_support_document.get(
            "operation_binding_review_research_authority_sha256"
        ) != research_authority_digest:
            raise ValueError(
                "manual support document was reviewed against a different Research authority"
            )
        if manual_support_document.get(
            "operation_binding_review_candidate_digest_scope"
        ) != CANDIDATE_BINDING_DIGEST_SCOPE or manual_support_document.get(
            "operation_binding_review_candidate_sha256"
        ) != frozen_candidate_digest:
            raise ValueError(
                "manual support document was reviewed against different Device-plan operation content"
            )
        if manual_support_document.get(
            "operation_binding_review_workstation_truth_sha256"
        ) != frozen_truth:
            raise ValueError(
                "manual support document was reviewed against a different workstation truth snapshot"
            )
        manual_support_records = manual_support_document.get(
            "operation_binding_reviews"
        )
        if not isinstance(manual_support_records, dict):
            raise ValueError(
                "manual support document must contain operation_binding_reviews"
            )
        manual_support_document_digest = _canonical_digest(
            manual_support_document
        )

    plan, plan_by_id, plan_values, plan_issues = _plan_index(candidate)
    if plan_issues:
        raise ValueError(str([dict(issue) for issue in plan_issues]))
    del plan

    relations: Dict[str, Dict[str, Any]] = {}
    raw_macros = package.get("macro_steps")
    if not isinstance(raw_macros, list):
        raise ValueError("Research V2 macro_steps must be an array")
    global_segment_ids: set[str] = set()
    for macro in raw_macros:
        if not isinstance(macro, dict):
            raise ValueError("Research V2 macro steps must be objects")
        raw_segments = macro.get("operation_segments")
        if not isinstance(raw_segments, list):
            raise ValueError("Research operation_segments must be arrays")
        segments: Dict[str, Dict[str, Any]] = {}
        for segment in raw_segments:
            if not isinstance(segment, dict):
                raise ValueError("Research operation segments must be objects")
            segment_id = str(segment.get("segment_id") or "").strip()
            if (
                not segment_id
                or segment_id in segments
                or segment_id in global_segment_ids
            ):
                raise ValueError(
                    "Research operation segment IDs must be globally unique"
                )
            global_segment_ids.add(segment_id)
            segments[segment_id] = segment
        raw_relations = macro.get("material_relations")
        if not isinstance(raw_relations, list):
            raise ValueError("Research material_relations must be arrays")
        for relation in raw_relations:
            if not isinstance(relation, dict):
                raise ValueError("Research material relations must be objects")
            relationship_id = str(relation.get("relation_id") or "").strip()
            if not relationship_id or relationship_id in relations:
                raise ValueError("Research relationship IDs must be unique and nonempty")
            segment_id = str(relation.get("source_operation_ref") or "").strip()
            segment = segments.get(segment_id)
            if segment is None:
                raise ValueError(
                    f"Research relationship {relationship_id} references an unknown operation segment"
                )
            relations[relationship_id] = {
                "relation": relation,
                "operation_segment": segment,
            }

    sealed_bindings: Dict[str, Any] = {}
    sealed_manual_records: Dict[str, Dict[str, Any]] = {}
    sealed_manual_record_digests: Dict[str, str] = {}
    for relationship_id, raw_binding in draft_bindings.items():
        if relationship_id not in relations:
            raise ValueError(
                f"binding references unknown Research relationship: {relationship_id}"
            )
        relation_record = relations[relationship_id]
        relation = relation_record["relation"]
        if str(relation.get("event_kind") or "").strip() == "none":
            raise ValueError(
                f"event_kind=none relationship {relationship_id} may not have a Device binding record"
            )
        if str(relation.get("event_kind") or "").strip() == "collect_same_material":
            raise ValueError(
                f"collect_same_material registration {relationship_id} is a non-executing assertion and may not have a Device binding record"
            )
        if not isinstance(raw_binding, dict):
            raise ValueError(
                f"binding {relationship_id} must be a structured object"
            )
        binding_status, required_capability = _binding_status(raw_binding)
        if binding_status not in {"bound", "missing_operation"}:
            raise ValueError(
                f"binding {relationship_id} has invalid binding_status"
            )
        if binding_status == "bound" and required_capability:
            raise ValueError(
                f"bound relationship {relationship_id} may not declare required_capability"
            )
        sealed = copy.deepcopy(raw_binding)
        sealed["binding_status"] = binding_status
        sealed.pop("disposition", None)
        if binding_status == "missing_operation":
            plan_step, logical_bindings = _binding_record(raw_binding)
            if (
                plan_step is not None
                or not isinstance(logical_bindings, dict)
                or logical_bindings
                or not required_capability
            ):
                raise ValueError(
                    f"missing_operation binding {relationship_id} is malformed"
                )
            sealed.pop("operation_evidence", None)
            sealed_bindings[relationship_id] = sealed
            continue

        plan_step, logical_bindings = _binding_record(raw_binding)
        if plan_step is None:
            raise ValueError(f"bound relationship {relationship_id} lacks plan_step")
        if not isinstance(logical_bindings, dict):
            raise ValueError(
                f"bound relationship {relationship_id} logical_container_bindings must be an object"
            )
        try:
            step_key = macro_id_key(
                plan_step, f"draft_bindings.{relationship_id}.plan_step"
            )
        except MacroIdentityError as exc:
            raise ValueError(str(exc)) from exc
        step = plan_by_id.get(step_key)
        if step is None:
            raise ValueError(
                f"bound relationship {relationship_id} names an absent Device step"
            )
        operation_segment = relation_record["operation_segment"]
        source_operation_ref = str(
            relation.get("source_operation_ref") or ""
        ).strip()
        if not source_operation_ref:
            raise ValueError(
                f"Research relationship {relationship_id} lacks source_operation_ref"
            )
        workstation = str(step.get("workstation") or "").strip()
        resolved_workstation = str(resolve_workstation(workstation) or "").strip()
        operation_intent = str(step.get("operation_intent") or "").strip()
        if not workstation or not resolved_workstation or not operation_intent:
            raise ValueError(
                f"Device step for {relationship_id} lacks a resolvable structured operation selector"
            )

        if authoring_mode == "automated_evidence_bound":
            _, scope_issues = _automated_binding_scope_issues(
                raw_binding,
                relationship_id=relationship_id,
                relation={
                    **copy.deepcopy(relation),
                    "operation_segment": copy.deepcopy(operation_segment),
                },
                plan_by_id=plan_by_id,
                plan_values=plan_values,
                resolve_workstation=resolve_workstation,
            )
            if scope_issues:
                raise ValueError(str([dict(issue) for issue in scope_issues]))

        existing_evidence = raw_binding.get("operation_evidence")
        existing_evidence = (
            existing_evidence if isinstance(existing_evidence, dict) else {}
        )
        capability_id = existing_evidence.get("machine_readable_capability_id")
        skill_contract_sha256 = existing_evidence.get("skill_contract_sha256")
        if authoring_mode == "automated_evidence_bound" and (
            not isinstance(capability_id, str)
            or not capability_id.strip()
            or not isinstance(skill_contract_sha256, str)
            or not skill_contract_sha256.strip()
        ):
            raise ValueError(
                f"automated binding {relationship_id} lacks Skill capability evidence"
            )
        if authoring_mode == "manual_evidence_bound":
            support_verification = "manual_evidence_bound"
            support_evidence_ref = str(
                existing_evidence.get("support_evidence_ref") or ""
            ).strip()
            expected_support_evidence_ref = (
                f"{manual_support_source}"
                f"#manual-binding:{relationship_id}"
            )
            if support_evidence_ref != expected_support_evidence_ref:
                raise ValueError(
                    f"manual binding {relationship_id} requires the explicit reviewed support_evidence_ref {expected_support_evidence_ref!r}"
                )
            manual_support_record = manual_support_records.get(relationship_id)
            if not isinstance(manual_support_record, dict):
                raise ValueError(
                    f"manual support document has no review record for {relationship_id}"
                )
            expected_manual_record = _expected_manual_support_record(
                relationship_id=relationship_id,
                plan_step=plan_step,
                source_operation_ref=source_operation_ref,
                operation_segment=operation_segment,
                step=step,
                resolved_workstation=resolved_workstation,
                workstation_truth_digest=frozen_truth,
            )
            mismatched_fields = [
                key
                for key, expected_value in expected_manual_record.items()
                if manual_support_record.get(key) != expected_value
            ]
            if mismatched_fields:
                raise ValueError(
                    f"manual support record {relationship_id} does not match the frozen Research relation and Device operation: {mismatched_fields}"
                )
            manual_support_record_digest = _canonical_digest(
                manual_support_record
            )
            sealed_manual_record_digests[
                relationship_id
            ] = manual_support_record_digest
            sealed_manual_records[relationship_id] = copy.deepcopy(
                manual_support_record
            )
        else:
            support_verification = "machine_readable_skill_contract"
            support_evidence_ref = str(
                existing_evidence.get("support_evidence_ref") or ""
            ).strip()
            manual_support_record_digest = ""

        sealed["operation_evidence"] = {
            "schema_version": 1,
            "evidence_mode": authoring_mode,
            "automation_claim": automation_claim,
            "evidence_source": {
                "research_authority_ref": (
                    f"{evidence_sources['research_authority']}"
                    f"#material_relation:{relationship_id}"
                ),
                "device_candidate_ref": (
                    f"{evidence_sources['device_candidate']}"
                    f"#plan_step:{json.dumps(plan_step, ensure_ascii=False, separators=(',', ':'))}"
                ),
            },
            "source_operation_ref": source_operation_ref,
            "research_operation_segment": {
                "segment_id": source_operation_ref,
                "material_effect": copy.deepcopy(
                    operation_segment.get("material_effect")
                ),
                "source_operation_ref": copy.deepcopy(
                    operation_segment.get("source_operation_ref")
                ),
                "segment_sha256": _canonical_digest(operation_segment),
            },
            "device_step_digest_scope": DEVICE_STEP_DIGEST_SCOPE,
            "device_step_sha256": device_step_binding_sha256(step),
            "workstation_truth_sha256": frozen_truth,
            "operation_selector": {
                "plan_step": copy.deepcopy(plan_step),
                "workstation": workstation,
                "resolved_workstation": resolved_workstation,
                "operation_intent": operation_intent,
            },
            "support_verification": support_verification,
            "support_evidence_ref": support_evidence_ref,
            "manual_support_record_id": (
                relationship_id
                if authoring_mode == "manual_evidence_bound"
                else None
            ),
            "manual_support_record_sha256": (
                manual_support_record_digest
                if authoring_mode == "manual_evidence_bound"
                else None
            ),
            "machine_readable_capability_id": copy.deepcopy(capability_id),
            "skill_contract_sha256": copy.deepcopy(skill_contract_sha256),
            "skill_contract_digest_scope": copy.deepcopy(
                existing_evidence.get("skill_contract_digest_scope")
            ),
        }
        sealed_bindings[relationship_id] = sealed

    return {
        "schema": BINDING_AUTHORITY_SCHEMA,
        "schema_version": BINDING_AUTHORITY_SCHEMA_VERSION,
        "authoring_mode": authoring_mode,
        "automation_claim": automation_claim,
        "research_authority_sha256": research_authority_digest,
        "candidate_digest_scope": CANDIDATE_BINDING_DIGEST_SCOPE,
        "candidate_sha256": frozen_candidate_digest,
        "workstation_truth_sha256": frozen_truth,
        "evidence_sources": copy.deepcopy(evidence_sources),
        "manual_support_schema": (
            MANUAL_SUPPORT_SCHEMA
            if authoring_mode == "manual_evidence_bound"
            else None
        ),
        "manual_support_document_sha256": (
            manual_support_document_digest
            if authoring_mode == "manual_evidence_bound"
            else None
        ),
        "manual_support_record_sha256": (
            sealed_manual_record_digests
            if authoring_mode == "manual_evidence_bound"
            else {}
        ),
        "manual_support_records": (
            sealed_manual_records
            if authoring_mode == "manual_evidence_bound"
            else {}
        ),
        "bindings": sealed_bindings,
    }


def _normalize_logical_binding_values(raw: Any) -> List[Any]:
    values = raw if isinstance(raw, list) else [raw]
    return [
        copy.deepcopy(value)
        for value in values
        if value is not None and not isinstance(value, (dict, list, bool))
    ]


def _quantity_mode(port: Dict[str, Any]) -> str:
    quantity = port.get("quantity")
    return str(quantity.get("mode") or "").strip() if isinstance(quantity, dict) else ""


def _quantity_semantic(port: Dict[str, Any]) -> str:
    quantity = port.get("quantity")
    return (
        str(quantity.get("semantic") or "").strip()
        if isinstance(quantity, dict)
        else ""
    )


def _runtime_measurement_unit_contract(
    port: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Freeze an auditable report unit for a future runtime observation.

    The Research contract may provide either a planned exact target or a
    ``runtime_measured`` placeholder.  Both must name the expected reporting
    unit.  The compiler accepts only extensive inventory dimensions and never
    invents a default or widens the unit set.
    """

    quantity = port.get("quantity")
    if not isinstance(quantity, dict):
        return None
    mode = str(quantity.get("mode") or "").strip()
    semantic = str(quantity.get("semantic") or "").strip()
    value = quantity.get("value")
    unit = str(quantity.get("unit") or "").strip()
    if mode == "runtime_measured":
        if (
            semantic != "runtime_measurement_required"
            or value is not None
            or not unit
        ):
            return None
    elif mode == "exact":
        if (
            semantic not in {"planned_target", "planning_estimate"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not unit
        ):
            return None
    else:
        return None
    normalized_unit = unit.replace("μ", "u").replace("µ", "u").lower()
    dimension = _RUNTIME_MEASUREMENT_UNIT_DIMENSIONS.get(normalized_unit)
    if dimension is None:
        return None
    return {
        "expected_quantity_dimension": dimension,
        "allowed_report_units": [unit],
        "unit_match_policy": "exact_research_declared_unit",
    }


def _planned_quantity(port: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return one explicit Research planning quantity, never an observation.

    The V2 model already distinguishes planned targets/estimates from runtime
    measurements.  Keep that distinction in every Device record and strip the
    Research-only ``mode``/``semantic`` wrapper from arithmetic quantities.
    """

    quantity = port.get("quantity")
    if not isinstance(quantity, dict) or str(quantity.get("mode") or "") != "exact":
        return None
    semantic = str(quantity.get("semantic") or "").strip()
    value = quantity.get("value")
    unit = str(quantity.get("unit") or "").strip()
    if (
        semantic not in {"planned_target", "planning_estimate"}
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or not unit
    ):
        return None
    return {
        "value": copy.deepcopy(value),
        "unit": unit,
        "semantic": semantic,
        "evidence_kind": "research_planning_quantity_not_execution_fact",
    }


def _allocation_records(
    raw_relation: Dict[str, Any],
    *,
    field: str,
    expected_instance_ids: List[str],
    relationship_id: str,
) -> Tuple[List[Dict[str, Any]], List[RelationshipCompileIssue]]:
    """Validate and normalize Research allocation records defensively.

    Pydantic normally guarantees this shape, but the compiler also accepts a
    persisted dictionary authority.  Rechecking here prevents a malformed
    hand-edited package from being promoted into a numeric Device ledger.
    """

    raw_allocations = raw_relation.get(field)
    if not isinstance(raw_allocations, list):
        return [], [
            RelationshipCompileIssue(
                "invalid_material_relation_allocations",
                f"{field} must be an array",
                relationship_id=relationship_id,
                field=field,
            )
        ]
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    local_issues: List[RelationshipCompileIssue] = []
    for index, raw in enumerate(raw_allocations):
        instance_id = (
            str(raw.get("material_instance_id") or "").strip()
            if isinstance(raw, dict)
            else ""
        )
        quantity = raw.get("quantity") if isinstance(raw, dict) else None
        mode = str(quantity.get("mode") or "").strip() if isinstance(quantity, dict) else ""
        semantic = (
            str(quantity.get("semantic") or "").strip()
            if isinstance(quantity, dict)
            else ""
        )
        value = quantity.get("value") if isinstance(quantity, dict) else None
        unit = str(quantity.get("unit") or "").strip() if isinstance(quantity, dict) else ""
        if not instance_id or instance_id in seen:
            local_issues.append(
                RelationshipCompileIssue(
                    "duplicate_or_missing_material_allocation_instance",
                    "material allocation requires one unique material_instance_id",
                    relationship_id=relationship_id,
                    field=field,
                    allocation_index=index,
                    material_instance_id=instance_id or "<missing>",
                )
            )
            continue
        seen.add(instance_id)
        if (
            mode != "exact"
            or semantic not in {"planned_target", "planning_estimate"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            or not unit
        ):
            local_issues.append(
                RelationshipCompileIssue(
                    "invalid_material_allocation_quantity",
                    "material allocation must be a finite positive planned exact quantity",
                    relationship_id=relationship_id,
                    field=field,
                    allocation_index=index,
                    material_instance_id=instance_id,
                )
            )
            continue
        normalized.append(
            {
                "material_instance_id": instance_id,
                "quantity": {
                    "value": copy.deepcopy(value),
                    "unit": unit,
                },
                "quantity_semantic": semantic,
            }
        )
    if set(seen) != set(expected_instance_ids):
        local_issues.append(
            RelationshipCompileIssue(
                "material_allocation_endpoint_mismatch",
                "material allocations must cover exactly the relation endpoints",
                relationship_id=relationship_id,
                field=field,
                missing=sorted(set(expected_instance_ids) - seen),
                extra=sorted(seen - set(expected_instance_ids)),
            )
        )
    return normalized, local_issues


def _quantity_without_planning_metadata(quantity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "value": copy.deepcopy(quantity.get("value")),
        "unit": str(quantity.get("unit") or "").strip(),
    }


def _material_dimension_contract_issues(
    macro: Dict[str, Any],
    status: Dict[str, Any],
    *,
    path: str,
    macro_id: Any,
) -> List[RelationshipCompileIssue]:
    """Validate every explicit material dimension before skipping any edge.

    ``material_relations=not_applicable`` means that no transition edge is
    compiled.  It does not waive the independent contract for declared input,
    intermediate, output, or logical-container records.  This guard therefore
    runs before the relation-disposition branch and uses only structured V2
    fields; it never consults macro text, sequence, workstation category, or a
    physical-container name.
    """

    issues: List[RelationshipCompileIssue] = []
    collections: Dict[str, List[Any]] = {}
    for field in STATUS_FIELDS:
        raw = macro.get(field)
        if not isinstance(raw, list):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_material_contract_array",
                    f"{field} must be an array",
                    path=f"{path}.{field}",
                    macro_step_id=copy.deepcopy(macro_id),
                    dimension=field,
                )
            )
            continue
        collections[field] = raw
        disposition = str(status.get(field) or "")
        if disposition == "declared" and not raw:
            issues.append(
                RelationshipCompileIssue(
                    "declared_material_dimension_empty",
                    f"{field}=declared requires a nonempty explicit collection",
                    path=f"{path}.{field}",
                    macro_step_id=copy.deepcopy(macro_id),
                    dimension=field,
                )
            )
        elif disposition == "not_applicable" and raw:
            issues.append(
                RelationshipCompileIssue(
                    "not_applicable_material_dimension_not_empty",
                    f"{field}=not_applicable requires an empty collection",
                    path=f"{path}.{field}",
                    macro_step_id=copy.deepcopy(macro_id),
                    dimension=field,
                )
            )

    # If an array itself is malformed, do not iterate or infer a replacement.
    if len(collections) != len(STATUS_FIELDS):
        return issues

    instance_ids: set[str] = set()
    for field in ("material_inputs", "material_intermediates", "material_outputs"):
        if status.get(field) != "declared":
            continue
        for item_index, item in enumerate(collections[field]):
            item_path = f"{path}.{field}[{item_index}]"
            if not isinstance(item, dict):
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_material_port",
                        "declared material records must be objects",
                        path=item_path,
                        macro_step_id=copy.deepcopy(macro_id),
                        dimension=field,
                    )
                )
                continue
            material_id = str(item.get("material_id") or "").strip()
            instance_id = str(item.get("material_instance_id") or "").strip()
            if not material_id or not instance_id:
                issues.append(
                    RelationshipCompileIssue(
                        "declared_material_identity_missing",
                        "declared material records require material_id and material_instance_id",
                        path=item_path,
                        macro_step_id=copy.deepcopy(macro_id),
                        dimension=field,
                    )
                )
            elif instance_id in instance_ids:
                issues.append(
                    RelationshipCompileIssue(
                        "material_instance_namespace_collision",
                        "material instance IDs must be unique across input, intermediate, and output dimensions",
                        path=item_path,
                        macro_step_id=copy.deepcopy(macro_id),
                        material_instance_id=instance_id,
                    )
                )
            else:
                instance_ids.add(instance_id)

            quantity = item.get("quantity")
            mode = str(quantity.get("mode") or "").strip() if isinstance(quantity, dict) else ""
            semantic = (
                str(quantity.get("semantic") or "").strip()
                if isinstance(quantity, dict)
                else ""
            )
            expected_semantics = {
                "exact": {"planned_target", "planning_estimate"},
                "all_available": {"whole_batch_unspecified"},
                "runtime_measured": {"runtime_measurement_required"},
            }
            if mode not in expected_semantics or semantic not in expected_semantics.get(
                mode, set()
            ):
                issues.append(
                    RelationshipCompileIssue(
                        "declared_material_quantity_semantics_invalid",
                        "declared material quantity requires explicit mode-consistent planning semantics",
                        path=f"{item_path}.quantity",
                        macro_step_id=copy.deepcopy(macro_id),
                        dimension=field,
                        quantity_mode=mode or "<missing>",
                        quantity_semantic=semantic or "<missing>",
                    )
                )
            elif isinstance(quantity, dict):
                value = quantity.get("value")
                unit = str(quantity.get("unit") or "").strip()
                exact_value_valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(float(value))
                    and float(value) >= 0
                )
                invalid_quantity_shape = bool(
                    (mode == "exact" and (not exact_value_valid or not unit))
                    or (
                        mode == "all_available"
                        and (value is not None or bool(unit))
                    )
                    or (
                        mode == "runtime_measured"
                        and (value is not None or not unit)
                    )
                )
                if invalid_quantity_shape:
                    issues.append(
                        RelationshipCompileIssue(
                            "declared_material_quantity_shape_invalid",
                            "exact targets require value+unit; all_available carries neither; runtime_measured carries an expected unit but no value",
                            path=f"{item_path}.quantity",
                            macro_step_id=copy.deepcopy(macro_id),
                            dimension=field,
                            quantity_mode=mode,
                        )
                    )

            parent_refs = item.get("parent_output_refs")
            if parent_refs is None:
                parent_refs = []
            origin = str(item.get("material_origin") or "").strip()
            if field == "material_inputs":
                if origin not in {
                    "external_inventory",
                    "upstream_output",
                    "same_step_relation",
                }:
                    issues.append(
                        RelationshipCompileIssue(
                            "declared_material_origin_invalid",
                            "declared input material requires an explicit supported material_origin",
                            path=f"{item_path}.material_origin",
                            macro_step_id=copy.deepcopy(macro_id),
                            material_instance_id=instance_id,
                        )
                    )
                if not isinstance(parent_refs, list) or (
                    origin == "upstream_output" and not parent_refs
                ) or (origin != "upstream_output" and bool(parent_refs)):
                    issues.append(
                        RelationshipCompileIssue(
                            "declared_parent_output_refs_invalid",
                            "parent_output_refs must be nonempty only for upstream_output inputs",
                            path=f"{item_path}.parent_output_refs",
                            macro_step_id=copy.deepcopy(macro_id),
                            material_instance_id=instance_id,
                            material_origin=origin or "<missing>",
                        )
                    )
                elif isinstance(parent_refs, list):
                    seen_parent_refs: set[Tuple[str, str]] = set()
                    for parent_index, parent_ref in enumerate(parent_refs):
                        ref_path = f"{item_path}.parent_output_refs[{parent_index}]"
                        parent_macro = (
                            str(parent_ref.get("macro_step_id") or "").strip()
                            if isinstance(parent_ref, dict)
                            else ""
                        )
                        parent_instance = (
                            str(parent_ref.get("material_instance_id") or "").strip()
                            if isinstance(parent_ref, dict)
                            else ""
                        )
                        ref_key = (parent_macro, parent_instance)
                        if not parent_macro or not parent_instance:
                            issues.append(
                                RelationshipCompileIssue(
                                    "declared_parent_output_ref_identity_invalid",
                                    "parent output references require macro_step_id and material_instance_id",
                                    path=ref_path,
                                    macro_step_id=copy.deepcopy(macro_id),
                                    material_instance_id=instance_id,
                                )
                            )
                        elif ref_key in seen_parent_refs:
                            issues.append(
                                RelationshipCompileIssue(
                                    "declared_parent_output_ref_duplicate",
                                    "parent output references must be unique",
                                    path=ref_path,
                                    macro_step_id=copy.deepcopy(macro_id),
                                    material_instance_id=instance_id,
                                )
                            )
                        else:
                            seen_parent_refs.add(ref_key)
            elif field == "material_intermediates" and origin != "same_step_relation":
                issues.append(
                    RelationshipCompileIssue(
                        "declared_intermediate_origin_invalid",
                        "declared intermediate material requires material_origin=same_step_relation",
                        path=f"{item_path}.material_origin",
                        macro_step_id=copy.deepcopy(macro_id),
                        material_instance_id=instance_id,
                    )
                )

    if status.get("logical_containers") == "declared":
        logical_ids: set[str] = set()
        for item_index, item in enumerate(collections["logical_containers"]):
            item_path = f"{path}.logical_containers[{item_index}]"
            logical_id = (
                str(item.get("logical_container_id") or "").strip()
                if isinstance(item, dict)
                else ""
            )
            if not logical_id or logical_id in logical_ids:
                issues.append(
                    RelationshipCompileIssue(
                        "declared_logical_container_identity_invalid",
                        "declared logical containers require unique nonempty logical_container_id values",
                        path=item_path,
                        macro_step_id=copy.deepcopy(macro_id),
                    )
                )
            else:
                logical_ids.add(logical_id)
    return issues


def _source_macro_keys(step: Dict[str, Any], path: str) -> set[Tuple[str, Any]]:
    return {
        macro_id_key(value, path)
        for value in extract_source_macro_ids(step, path, required=True)
    }


def _research_source_ref(
    macro_index: int,
    input_index: int,
    macro_id: Any,
    port: Dict[str, Any],
) -> Dict[str, Any]:
    path = (
        "research_action_package_v2.macro_steps"
        f"[{macro_index}].material_inputs[{input_index}]"
    )
    result = {
        "source_path": path,
        "source_macro_step": copy.deepcopy(macro_id),
        "source_field": "material_inputs",
        "material_id": str(port.get("material_id") or ""),
        "material_identity": str(port.get("name") or port.get("material_id") or ""),
        "material_identity_id": str(port.get("material_id") or ""),
        "material_instance_id": str(port.get("material_instance_id") or ""),
        "provenance": copy.deepcopy(port.get("provenance") or {}),
    }
    planned = _planned_quantity(port)
    if planned is not None:
        result["quantity"] = _quantity_without_planning_metadata(planned)
        result["quantity_semantic"] = planned["semantic"]
        result["quantity_scope"] = "per_material_instance"
    return result


def _managed_records(values: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    managed: List[Dict[str, Any]] = []
    preserved: List[Dict[str, Any]] = []
    for value in values or []:
        if not isinstance(value, dict):
            preserved.append(copy.deepcopy(value))
        elif str(value.get("construction_rule") or "") == RULE_ID:
            managed.append(copy.deepcopy(value))
        else:
            preserved.append(copy.deepcopy(value))
    return managed, preserved


def compile_material_relationships(
    candidate: Dict[str, Any],
    research_authority: Dict[str, Any],
    *,
    relationship_bindings: Optional[Dict[str, Any]] = None,
    resolve_workstation: Optional[Callable[[str], str]] = None,
    workstation_truth_digest: str = "",
    workstation_capability_index: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[RelationshipCompileIssue], List[str]]:
    """Atomically compile an explicit Research relationship graph.

    A nonempty ``relationship_bindings`` value must be a versioned binding
    authority produced by :func:`seal_relationship_binding_authority` (or an
    equivalent implementation of that schema).  Device-step relation IDs do
    not authorize themselves.  Multi-container relationships always need
    explicit logical-to-physical bindings.

    On any issue the original candidate is returned byte-for-byte (deep copied)
    and ``applied`` is empty.  A successful replay over an already compiled
    candidate returns an identical object and an empty ``applied`` list.
    """

    original = copy.deepcopy(candidate)
    if not isinstance(candidate, dict):
        return original, [
            RelationshipCompileIssue(
                "invalid_candidate", "relationship compiler requires an object"
            )
        ], []
    package = _package_from_authority(research_authority)
    if package is None:
        return original, [
            RelationshipCompileIssue(
                "missing_research_v2_authority",
                "accepted Research V2 package is required for relationship compilation",
            )
        ], []
    raw_macro_steps = package.get("macro_steps")
    if not isinstance(raw_macro_steps, list):
        return original, [
            RelationshipCompileIssue(
                "invalid_research_macro_steps",
                "Research V2 macro_steps must be an array",
            )
        ], []
    # Device operation claims identify a Research segment by
    # ``source_operation_ref`` without a second macro discriminator.  Reject
    # cross-macro collisions at the compiler boundary before any binding
    # authority is consumed, including for unresolved/N/A macro contracts.
    global_segment_paths: Dict[str, str] = {}
    global_segment_issues: List[RelationshipCompileIssue] = []
    for macro_index, raw_macro in enumerate(raw_macro_steps):
        if not isinstance(raw_macro, dict):
            continue
        raw_segments = raw_macro.get("operation_segments")
        if not isinstance(raw_segments, list):
            continue
        for segment_index, raw_segment in enumerate(raw_segments):
            if not isinstance(raw_segment, dict):
                continue
            segment_id = str(raw_segment.get("segment_id") or "").strip()
            if not segment_id:
                continue
            segment_path = (
                "research_action_package_v2.macro_steps"
                f"[{macro_index}].operation_segments[{segment_index}].segment_id"
            )
            prior_path = global_segment_paths.get(segment_id)
            if prior_path is not None:
                global_segment_issues.append(
                    RelationshipCompileIssue(
                        "research_operation_segment_id_not_globally_unique",
                        "operation segment IDs must be globally unique within the Research package",
                        blocker_class="contract_invalid",
                        path=segment_path,
                        segment_id=segment_id,
                        first_path=prior_path,
                    )
                )
            else:
                global_segment_paths[segment_id] = segment_path
    if global_segment_issues:
        return original, global_segment_issues, []
    plan, plan_by_id, plan_values, issues = _plan_index(candidate)
    if issues:
        return original, issues, []
    binding_envelope, bindings, binding_authority_issues = (
        _validate_binding_authority(
            relationship_bindings,
            candidate=candidate,
            package=package,
            workstation_truth_digest=workstation_truth_digest,
        )
    )
    if binding_authority_issues:
        return original, binding_authority_issues, []

    if (
        str(binding_envelope.get("authoring_mode") or "").strip()
        == "automated_evidence_bound"
    ):
        if not isinstance(workstation_capability_index, dict):
            return original, [
                RelationshipCompileIssue(
                    "automated_binding_current_capability_index_missing",
                    "automated binding authority must be recomputed from the current complete workstation capability index",
                    blocker_class="contract_invalid",
                )
            ], []
        if resolve_workstation is None:
            return original, [
                RelationshipCompileIssue(
                    "automated_binding_workstation_resolver_missing",
                    "automated binding authority recomputation requires the current workstation resolver",
                    blocker_class="contract_invalid",
                )
            ], []
        try:
            try:
                from .material_operation_binding import (
                    resolve_and_seal_material_operation_binding_authority,
                )
            except ImportError:  # Direct-script compatibility.
                from material_operation_binding import (  # type: ignore
                    resolve_and_seal_material_operation_binding_authority,
                )
            recomputed_authority, raw_resolver_issues = (
                resolve_and_seal_material_operation_binding_authority(
                    candidate,
                    package,
                    workstation_capability_index,
                    evidence_sources=copy.deepcopy(
                        binding_envelope.get("evidence_sources") or {}
                    ),
                    workstation_truth_digest=workstation_truth_digest,
                    resolve_workstation=resolve_workstation,
                )
            )
        except Exception as exc:
            return original, [
                RelationshipCompileIssue(
                    "automated_binding_recomputation_failed",
                    "automated binding authority could not be recomputed from current truth",
                    blocker_class="software_internal_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            ], []
        if raw_resolver_issues:
            recomputation_issues: List[RelationshipCompileIssue] = []
            for raw_issue in raw_resolver_issues:
                raw_issue = raw_issue if isinstance(raw_issue, dict) else {}
                raw_context = raw_issue.get("context")
                context = (
                    copy.deepcopy(raw_context)
                    if isinstance(raw_context, dict)
                    else {}
                )
                recomputation_issues.append(
                    RelationshipCompileIssue(
                        str(raw_issue.get("code") or "automated_binding_rejected"),
                        str(
                            raw_issue.get("message")
                            or "current truth rejected the automated binding authority"
                        ),
                        blocker_class=str(
                            raw_issue.get("blocker_class")
                            or "plan_binding_invalid"
                        ),
                        **context,
                    )
                )
            return original, recomputation_issues, []
        if recomputed_authority != binding_envelope:
            return original, [
                RelationshipCompileIssue(
                    "automated_binding_authority_not_canonical",
                    "supplied automated binding authority does not exactly match a fresh resolution against current Research, Device, and Skill truth",
                    blocker_class="plan_binding_invalid",
                    supplied_sha256=_canonical_digest(binding_envelope),
                    recomputed_sha256=_canonical_digest(recomputed_authority),
                )
            ], []
        binding_envelope = copy.deepcopy(recomputed_authority)
        bindings = relationship_binding_records(recomputed_authority)

    identity_encoding = package.get("identity_encoding")
    macro_records: List[Dict[str, Any]] = []
    output_ports_global: Dict[Tuple[str, str], Dict[str, Any]] = {}
    relationship_ids: set[str] = set()
    non_bindable_none_relationship_ids: set[str] = set()
    non_bindable_collection_relationship_ids: set[str] = set()
    collection_registrations: List[Dict[str, Any]] = []

    for macro_index, raw_macro in enumerate(raw_macro_steps):
        path = f"research_action_package_v2.macro_steps[{macro_index}]"
        if not isinstance(raw_macro, dict):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_research_macro_step",
                    "Research macro step must be an object",
                    path=path,
                )
            )
            continue
        raw_macro_id = raw_macro.get("macro_step_id")
        try:
            macro_id = decode_package_identity(
                raw_macro_id, identity_encoding, f"{path}.macro_step_id"
            )
            macro_key = macro_id_key(macro_id, f"{path}.macro_step_id")
        except Exception as exc:
            issues.append(
                RelationshipCompileIssue(
                    "invalid_research_macro_id", str(exc), path=f"{path}.macro_step_id"
                )
            )
            continue

        status = raw_macro.get("material_contract_status")
        # Historical V2 and legacy-upconverted packages have no explicit
        # completeness declaration.  They are unresolved, not malformed, and
        # therefore provide no authority to this compiler.
        if status is None:
            continue
        if not isinstance(status, dict) or any(
            str(status.get(field) or "") not in STATUS_VALUES for field in STATUS_FIELDS
        ):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_material_contract_status",
                    "all five material contract dimensions require an explicit status",
                    path=f"{path}.material_contract_status",
                    macro_step_id=copy.deepcopy(macro_id),
                )
            )
            continue
        dimension_issues = _material_dimension_contract_issues(
            raw_macro,
            status,
            path=path,
            macro_id=macro_id,
        )
        if dimension_issues:
            issues.extend(dimension_issues)
            continue
        relationship_status = str(status.get("material_relations") or "")
        raw_relations = raw_macro.get("material_relations")
        # The per-dimension guard above has already proved this is an array.
        assert isinstance(raw_relations, list)
        if relationship_status == "unresolved":
            continue
        if relationship_status == "not_applicable":
            if raw_relations:
                issues.append(
                    RelationshipCompileIssue(
                        "not_applicable_relationships_not_empty",
                        "not_applicable material relationships must be empty",
                        macro_step_id=copy.deepcopy(macro_id),
                    )
                )
            continue
        if not raw_relations:
            issues.append(
                RelationshipCompileIssue(
                    "declared_relationships_empty",
                    "declared material relationships require at least one record",
                    macro_step_id=copy.deepcopy(macro_id),
                )
            )
            continue

        inputs = raw_macro.get("material_inputs")
        intermediates = raw_macro.get("material_intermediates")
        outputs = raw_macro.get("material_outputs")
        logical_containers = raw_macro.get("logical_containers")
        if not all(
            isinstance(value, list)
            for value in (inputs, intermediates, outputs, logical_containers)
        ):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_material_contract_arrays",
                    "material ports and logical containers must be arrays",
                    macro_step_id=copy.deepcopy(macro_id),
                )
            )
            continue

        def indexed(items: Iterable[Any], field: str) -> Dict[str, Dict[str, Any]]:
            result: Dict[str, Dict[str, Any]] = {}
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    issues.append(
                        RelationshipCompileIssue(
                            f"invalid_{field}_record",
                            f"{field} entries must be objects",
                            path=f"{path}.{field}[{item_index}]",
                        )
                    )
                    continue
                raw_id = (
                    item.get("material_instance_id")
                    if field != "logical_containers"
                    else item.get("logical_container_id")
                )
                item_id = str(raw_id or "").strip()
                if not item_id:
                    issues.append(
                        RelationshipCompileIssue(
                            f"missing_{field}_id",
                            f"{field} entries require a stable instance identifier",
                            path=f"{path}.{field}[{item_index}]",
                        )
                    )
                    continue
                if item_id in result:
                    issues.append(
                        RelationshipCompileIssue(
                            f"duplicate_{field}_id",
                            f"duplicate {field} identifier {item_id!r}",
                            path=f"{path}.{field}[{item_index}]",
                        )
                    )
                    continue
                enriched = copy.deepcopy(item)
                enriched["_index"] = item_index
                result[item_id] = enriched
            return result

        input_by_id = indexed(inputs, "material_inputs")
        intermediate_by_id = indexed(intermediates, "material_intermediates")
        output_by_id = indexed(outputs, "material_outputs")
        container_by_id = indexed(logical_containers, "logical_containers")
        overlapping_instances = (
            (set(input_by_id) & set(intermediate_by_id))
            | (set(input_by_id) & set(output_by_id))
            | (set(intermediate_by_id) & set(output_by_id))
        )
        if overlapping_instances:
            issues.append(
                RelationshipCompileIssue(
                    "material_instance_namespace_collision",
                    "input, intermediate, and output instance IDs must be disjoint",
                    macro_step_id=copy.deepcopy(macro_id),
                    material_instance_ids=sorted(overlapping_instances),
                )
            )
        for instance_id, port in output_by_id.items():
            global_key = (str(raw_macro_id), instance_id)
            if global_key in output_ports_global:
                issues.append(
                    RelationshipCompileIssue(
                        "duplicate_output_instance",
                        "output material instances must be unique within a macro step",
                        macro_step_id=copy.deepcopy(macro_id),
                        material_instance_id=instance_id,
                    )
                )
            output_ports_global[global_key] = port

        raw_operation_segments = raw_macro.get("operation_segments")
        if not isinstance(raw_operation_segments, list) or not raw_operation_segments:
            issues.append(
                RelationshipCompileIssue(
                    "research_operation_segments_missing",
                    "an explicit material contract requires evidence-bound operation_segments",
                    blocker_class="evidence_insufficient",
                    macro_step_id=copy.deepcopy(macro_id),
                )
            )
            continue
        operation_segments: Dict[str, Dict[str, Any]] = {}
        for segment_index, raw_segment in enumerate(raw_operation_segments):
            segment_path = f"{path}.operation_segments[{segment_index}]"
            if not isinstance(raw_segment, dict):
                issues.append(
                    RelationshipCompileIssue(
                        "research_operation_segment_invalid",
                        "operation segment must be an object",
                        blocker_class="contract_invalid",
                        path=segment_path,
                    )
                )
                continue
            segment_id = str(raw_segment.get("segment_id") or "").strip()
            material_effect = str(
                raw_segment.get("material_effect") or ""
            ).strip()
            segment_source_ref = str(
                raw_segment.get("source_operation_ref") or ""
            ).strip()
            if (
                not segment_id
                or segment_id in operation_segments
                or material_effect not in MATERIAL_EFFECTS
                or not segment_source_ref
                or not isinstance(raw_segment.get("provenance"), dict)
            ):
                issues.append(
                    RelationshipCompileIssue(
                        "research_operation_segment_invalid",
                        "operation segment requires a unique segment_id, supported material_effect, evidence-facing source_operation_ref and provenance",
                        blocker_class="contract_invalid",
                        path=segment_path,
                        segment_id=segment_id or "<missing>",
                        material_effect=material_effect or "<missing>",
                    )
                )
                continue
            operation_segments[segment_id] = copy.deepcopy(raw_segment)

        relations: List[Dict[str, Any]] = []
        for relation_index, raw_relation in enumerate(raw_relations):
            relation_path = f"{path}.material_relations[{relation_index}]"
            if not isinstance(raw_relation, dict):
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_material_relationship",
                        "material relationship must be an object",
                        path=relation_path,
                    )
                )
                continue
            relationship_id = str(raw_relation.get("relation_id") or "").strip()
            if not relationship_id or relationship_id in relationship_ids:
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_or_duplicate_relationship_id",
                        "relation_id must be nonempty and globally unique",
                        path=f"{relation_path}.relation_id",
                        relationship_id=relationship_id,
                    )
                )
                continue
            relationship_ids.add(relationship_id)
            event_kind = str(raw_relation.get("event_kind") or "").strip()
            if event_kind not in ALLOWED_EVENT_KINDS:
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_material_event_kind",
                        f"unsupported explicit material event {event_kind!r}",
                        relationship_id=relationship_id,
                    )
                )
                continue
            input_ids = raw_relation.get("input_material_instance_ids")
            output_ids = raw_relation.get("output_material_instance_ids")
            logical_ids = raw_relation.get("logical_container_ids")
            if not all(isinstance(value, list) for value in (input_ids, output_ids, logical_ids)):
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_relationship_endpoint_lists",
                        "relationship input/output/container references must be arrays",
                        relationship_id=relationship_id,
                    )
                )
                continue
            input_ids = [str(value).strip() for value in input_ids if str(value).strip()]
            output_ids = [str(value).strip() for value in output_ids if str(value).strip()]
            logical_ids = [str(value).strip() for value in logical_ids if str(value).strip()]
            if event_kind == "none":
                non_bindable_none_relationship_ids.add(relationship_id)
                if input_ids or output_ids or logical_ids:
                    issues.append(
                        RelationshipCompileIssue(
                            "none_relationship_has_endpoints",
                            "event_kind=none cannot carry material or container endpoints",
                            relationship_id=relationship_id,
                        )
                    )
                continue
            if event_kind == "collect_same_material":
                # A collection registration is a Device-visible NO-OP: it is
                # preserved as package metadata, never bound to a workstation
                # action, never measured, and never transitions material.
                # Because this branch skips the generic endpoint-resolution
                # checks below, it validates its own endpoints fail-closed:
                # the registration must reference exactly one logical
                # container declared in the same macro step, that container
                # must be a collected_set whose members equal the registered
                # member set, and the quantity basis must stay
                # runtime_measurement_required.
                non_bindable_collection_relationship_ids.add(relationship_id)
                registration_issues: List[RelationshipCompileIssue] = []
                if not input_ids or set(input_ids) != set(output_ids):
                    registration_issues.append(
                        RelationshipCompileIssue(
                            "collect_registration_endpoint_mismatch",
                            "collect_same_material registration requires a nonempty member set with identical input and output instance IDs",
                            relationship_id=relationship_id,
                            input_material_instance_ids=list(input_ids),
                            output_material_instance_ids=list(output_ids),
                        )
                    )
                if not logical_ids:
                    registration_issues.append(
                        RelationshipCompileIssue(
                            "collect_registration_missing_logical_container",
                            "collect_same_material registration requires the collected_set logical container endpoint",
                            relationship_id=relationship_id,
                        )
                    )
                else:
                    distinct_container_ids = list(dict.fromkeys(logical_ids))
                    unresolved_containers = [
                        value
                        for value in distinct_container_ids
                        if value not in container_by_id
                    ]
                    if unresolved_containers:
                        registration_issues.append(
                            RelationshipCompileIssue(
                                "collect_registration_unresolved_logical_container",
                                "collect_same_material registration must reference a logical container declared in the same macro step",
                                relationship_id=relationship_id,
                                missing_logical_containers=unresolved_containers,
                            )
                        )
                    if len(distinct_container_ids) != 1:
                        registration_issues.append(
                            RelationshipCompileIssue(
                                "collect_registration_logical_container_not_unique",
                                "collect_same_material registration requires exactly one collected_set logical container endpoint",
                                relationship_id=relationship_id,
                                logical_container_ids=distinct_container_ids,
                            )
                        )
                    for container_id in distinct_container_ids:
                        container = container_by_id.get(container_id)
                        if container is None:
                            continue
                        if (
                            str(container.get("container_type") or "").strip()
                            != "collected_set"
                        ):
                            registration_issues.append(
                                RelationshipCompileIssue(
                                    "collect_registration_container_type_mismatch",
                                    "collect_same_material registration must reference a collected_set logical container",
                                    relationship_id=relationship_id,
                                    logical_container_id=container_id,
                                    container_type=str(
                                        container.get("container_type") or ""
                                    ).strip(),
                                )
                            )
                            continue
                        raw_members = container.get("member_material_instance_ids")
                        container_members = [
                            str(value).strip()
                            for value in (raw_members if isinstance(raw_members, list) else [])
                            if str(value).strip()
                        ]
                        if set(container_members) != set(input_ids):
                            registration_issues.append(
                                RelationshipCompileIssue(
                                    "collect_registration_member_mismatch",
                                    "collected_set container members must equal the registered member set",
                                    relationship_id=relationship_id,
                                    logical_container_id=container_id,
                                    container_member_material_instance_ids=container_members,
                                    member_material_instance_ids=list(input_ids),
                                )
                            )
                quantity_basis_value = str(
                    raw_relation.get("quantity_basis") or ""
                ).strip()
                if quantity_basis_value != RUNTIME_QUANTITY_MODE:
                    registration_issues.append(
                        RelationshipCompileIssue(
                            "collect_registration_quantity_basis_unsupported",
                            "collect_same_material registration requires quantity_basis runtime_measurement_required (a registration carries no allocated quantity)",
                            relationship_id=relationship_id,
                            quantity_basis=quantity_basis_value,
                        )
                    )
                if raw_relation.get("input_allocations") or raw_relation.get(
                    "output_allocations"
                ):
                    registration_issues.append(
                        RelationshipCompileIssue(
                            "collect_registration_with_allocations",
                            "collect_same_material registration is a non-executing assertion and may not carry quantity allocations",
                            relationship_id=relationship_id,
                        )
                    )
                if registration_issues:
                    issues.extend(registration_issues)
                else:
                    collection_registrations.append(
                        {
                            "relationship_id": relationship_id,
                            "member_material_instance_ids": list(input_ids),
                            "logical_container_ids": list(logical_ids),
                            "quantity_basis": str(
                                raw_relation.get("quantity_basis") or ""
                            ).strip(),
                            "source_operation_ref": str(
                                raw_relation.get("source_operation_ref") or ""
                            ).strip(),
                            "provenance": copy.deepcopy(
                                raw_relation.get("provenance") or {}
                            ),
                        }
                    )
                continue
            input_nodes = {**input_by_id, **intermediate_by_id}
            output_nodes = {**intermediate_by_id, **output_by_id}
            required_dimensions = {
                "material_inputs"
                for value in input_ids
                if value in input_by_id
            } | {
                "material_intermediates"
                for value in input_ids + output_ids
                if value in intermediate_by_id
            } | {
                "material_outputs"
                for value in output_ids
                if value in output_by_id
            } | {"logical_containers"}
            if any(status.get(field) != "declared" for field in required_dimensions):
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_dimension_not_declared",
                        "non-none relationships require declared input/output/container dimensions",
                        relationship_id=relationship_id,
                        material_contract_status=copy.deepcopy(status),
                    )
                )
                continue
            missing_inputs = [value for value in input_ids if value not in input_nodes]
            missing_outputs = [value for value in output_ids if value not in output_nodes]
            missing_containers = [value for value in logical_ids if value not in container_by_id]
            if missing_inputs or missing_outputs or missing_containers:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_endpoint_unresolved",
                        "relationship endpoints must resolve inside the same Research macro step",
                        relationship_id=relationship_id,
                        missing_inputs=missing_inputs,
                        missing_outputs=missing_outputs,
                        missing_logical_containers=missing_containers,
                    )
                )
                continue
            quantity_basis = str(raw_relation.get("quantity_basis") or "").strip()
            compilation_support_issues: List[Dict[str, Any]] = []
            normalized_input_allocations: List[Dict[str, Any]] = []
            normalized_output_allocations: List[Dict[str, Any]] = []
            runtime_measurement_contracts: Dict[str, Dict[str, Dict[str, Any]]] = {
                "input": {},
                "output": {},
            }
            if quantity_basis not in SUPPORTED_QUANTITY_BASES:
                compilation_support_issues.append(
                    {
                        "code": "relationship_quantity_basis_not_compilable",
                        "message": (
                            "the deterministic compiler supports whole_batch, "
                            "conserved_inventory, and pending runtime measurement; "
                            "this quantity basis remains unsupported"
                        ),
                        "quantity_basis": quantity_basis or "<missing>",
                    }
                )
            elif quantity_basis == "whole_batch" and (
                event_kind in {"split_same_material", "replicate_same_material"}
                or len(input_ids) != 1
                or len(output_ids) != 1
            ):
                issues.append(
                    RelationshipCompileIssue(
                        "whole_batch_relationship_requires_one_to_one_lineage",
                        "whole_batch may only represent one input and one output",
                        relationship_id=relationship_id,
                        event_kind=event_kind,
                        input_count=len(input_ids),
                        output_count=len(output_ids),
                    )
                )
                continue
            elif quantity_basis == "conserved_inventory":
                normalized_input_allocations, input_allocation_issues = (
                    _allocation_records(
                        raw_relation,
                        field="input_allocations",
                        expected_instance_ids=input_ids,
                        relationship_id=relationship_id,
                    )
                )
                normalized_output_allocations, output_allocation_issues = (
                    _allocation_records(
                        raw_relation,
                        field="output_allocations",
                        expected_instance_ids=output_ids,
                        relationship_id=relationship_id,
                    )
                )
                allocation_issues = input_allocation_issues + output_allocation_issues
                if allocation_issues:
                    issues.extend(allocation_issues)
                    continue
                units = {
                    str(item["quantity"].get("unit") or "").strip()
                    for item in normalized_input_allocations
                    + normalized_output_allocations
                }
                input_total = math.fsum(
                    float(item["quantity"]["value"])
                    for item in normalized_input_allocations
                )
                output_total = math.fsum(
                    float(item["quantity"]["value"])
                    for item in normalized_output_allocations
                )
                if len(units) != 1 or not math.isclose(
                    input_total, output_total, rel_tol=1e-9, abs_tol=1e-12
                ):
                    issues.append(
                        RelationshipCompileIssue(
                            "conserved_inventory_allocation_mismatch",
                            "conserved_inventory allocations require one unit and equal aggregate input/output totals",
                            relationship_id=relationship_id,
                            units=sorted(units),
                            input_total=input_total,
                            output_total=output_total,
                        )
                    )
                    continue
            elif quantity_basis == RUNTIME_QUANTITY_MODE:
                # Optional allocations remain planning hints only.  They are
                # copied into the pending obligation and never materialized as
                # produced/consumed execution quantities.
                if raw_relation.get("input_allocations"):
                    normalized_input_allocations, allocation_issues = (
                        _allocation_records(
                            raw_relation,
                            field="input_allocations",
                            expected_instance_ids=input_ids,
                            relationship_id=relationship_id,
                        )
                    )
                    if allocation_issues:
                        issues.extend(allocation_issues)
                        continue
                runtime_contract_issues: List[RelationshipCompileIssue] = []
                for role, endpoint_ids, node_map in (
                    ("input", input_ids, input_nodes),
                    ("output", output_ids, output_nodes),
                ):
                    for instance_id in endpoint_ids:
                        measurement_contract = _runtime_measurement_unit_contract(
                            node_map[instance_id]
                        )
                        if measurement_contract is None:
                            runtime_contract_issues.append(
                                RelationshipCompileIssue(
                                    "runtime_measurement_unit_contract_missing",
                                    "every runtime-measured endpoint requires a Research-declared extensive measurement unit; no default unit may be invented",
                                    blocker_class="research_contract_missing",
                                    relationship_id=relationship_id,
                                    role=role,
                                    material_instance_id=instance_id,
                                )
                            )
                        else:
                            runtime_measurement_contracts[role][instance_id] = (
                                measurement_contract
                            )
                if runtime_contract_issues:
                    issues.extend(runtime_contract_issues)
                    continue
                if len(input_ids) != 1 or len(output_ids) != 1:
                    compilation_support_issues.append(
                        {
                            "code": "runtime_quantity_multi_endpoint_reservation_unsupported",
                            "message": (
                                "runtime-measured split/merge requires an atomic measured-allocation "
                                "reservation and conservation executor; metadata-only gates are insufficient"
                            ),
                            "input_count": len(input_ids),
                            "output_count": len(output_ids),
                        }
                    )
                if raw_relation.get("output_allocations"):
                    normalized_output_allocations, allocation_issues = (
                        _allocation_records(
                            raw_relation,
                            field="output_allocations",
                            expected_instance_ids=output_ids,
                            relationship_id=relationship_id,
                        )
                    )
                    if allocation_issues:
                        issues.extend(allocation_issues)
                        continue
            if not logical_ids:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_missing_logical_container",
                        "non-none relationship requires explicit logical container endpoints",
                        relationship_id=relationship_id,
                    )
                )
                continue
            source_operation_ref = str(
                raw_relation.get("source_operation_ref") or ""
            ).strip()
            if not source_operation_ref:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_missing_operation_ref",
                        "relationship must point to an authorized Research operation",
                        relationship_id=relationship_id,
                    )
                )
                continue
            operation_segment = operation_segments.get(source_operation_ref)
            if operation_segment is None:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_operation_segment_unresolved",
                        "relationship source_operation_ref must resolve to an operation segment in the same Research macro",
                        blocker_class="contract_invalid",
                        relationship_id=relationship_id,
                        source_operation_ref=source_operation_ref,
                    )
                )
                continue
            segment_effect = str(
                operation_segment.get("material_effect") or ""
            ).strip()
            if segment_effect in NON_PROCESSING_MATERIAL_EFFECTS or (
                segment_effect == "unknown"
            ):
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_operation_segment_effect_invalid",
                        "a material relationship must reference an explicit material-processing operation segment",
                        blocker_class=(
                            "evidence_insufficient"
                            if segment_effect == "unknown"
                            else "contract_invalid"
                        ),
                        relationship_id=relationship_id,
                        source_operation_ref=source_operation_ref,
                        material_effect=segment_effect,
                    )
                )
                continue
            relations.append(
                {
                    "relationship_id": relationship_id,
                    "event_kind": event_kind,
                    "input_ids": input_ids,
                    "output_ids": output_ids,
                    "logical_ids": logical_ids,
                    "source_operation_ref": source_operation_ref,
                    "operation_segment": copy.deepcopy(operation_segment),
                    "provenance": copy.deepcopy(raw_relation.get("provenance") or {}),
                    "quantity_basis": quantity_basis,
                    "input_allocations": normalized_input_allocations,
                    "output_allocations": normalized_output_allocations,
                    "runtime_measurement_contracts": runtime_measurement_contracts,
                    "compilation_support_issues": compilation_support_issues,
                    "raw": copy.deepcopy(raw_relation),
                }
            )
        macro_records.append(
            {
                "index": macro_index,
                "raw_id": str(raw_macro_id),
                "macro_id": macro_id,
                "macro_key": macro_key,
                "sample_id": str(raw_macro.get("sample_id") or "").strip(),
                "inputs": input_by_id,
                "intermediates": intermediate_by_id,
                "outputs": output_by_id,
                "input_nodes": {**input_by_id, **intermediate_by_id},
                "output_nodes": {**intermediate_by_id, **output_by_id},
                "all_nodes": {**input_by_id, **intermediate_by_id, **output_by_id},
                "containers": container_by_id,
                "operation_segments": operation_segments,
                "relations": relations,
            }
        )

    unknown_bindings = sorted(set(bindings) - relationship_ids)
    if unknown_bindings:
        issues.append(
            RelationshipCompileIssue(
                "unknown_relationship_binding",
                "binding authority references relationships absent from Research authority",
                blocker_class="plan_binding_invalid",
                relationship_ids=unknown_bindings,
            )
        )
    unexpected_none_bindings = sorted(
        set(bindings) & non_bindable_none_relationship_ids
    )
    if unexpected_none_bindings:
        issues.append(
            RelationshipCompileIssue(
                "unexpected_binding_for_none_relation",
                "event_kind=none is a non-operation assertion and may not have a Device binding record",
                blocker_class="plan_binding_invalid",
                relationship_ids=unexpected_none_bindings,
            )
        )
    unexpected_collection_bindings = sorted(
        set(bindings) & non_bindable_collection_relationship_ids
    )
    if unexpected_collection_bindings:
        issues.append(
            RelationshipCompileIssue(
                "unexpected_binding_for_collect_registration",
                "collect_same_material is a non-executing registration and may not have a Device binding record",
                blocker_class="plan_binding_invalid",
                relationship_ids=unexpected_collection_bindings,
            )
        )

    previous_manifest = candidate.get("material_relationship_compiler")
    previous_relation_transitions = (
        previous_manifest.get("relationship_transition_ids")
        if isinstance(previous_manifest, dict)
        and str(previous_manifest.get("construction_rule") or "") == RULE_ID
        else {}
    )
    previous_relation_transitions = (
        previous_relation_transitions
        if isinstance(previous_relation_transitions, dict)
        else {}
    )
    previous_compiler_step_keys: set[Tuple[str, Any]] = set()
    candidate_claim_steps: Dict[str, List[Tuple[str, Any]]] = {}
    for step_index, step in enumerate(plan):
        direct = step.get("research_material_relation_ids")
        if direct is None:
            continue
        if not isinstance(direct, list) or any(
            not isinstance(value, str) or not value.strip() for value in direct
        ):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_step_relationship_ids",
                    "research_material_relation_ids must be nonempty strings",
                    blocker_class="plan_binding_invalid",
                    path=f"device_plan[{step_index}].research_material_relation_ids",
                )
            )
            continue
        normalized_direct = [value.strip() for value in direct]
        if len(set(normalized_direct)) != len(normalized_direct):
            issues.append(
                RelationshipCompileIssue(
                    "duplicate_step_relationship_id",
                    "a Device step cannot claim the same Research relationship more than once",
                    blocker_class="plan_binding_invalid",
                    path=f"device_plan[{step_index}].research_material_relation_ids",
                    plan_step=copy.deepcopy(step.get("plan_step")),
                )
            )
            continue
        step_key = macro_id_key(
            step.get("plan_step"), f"device_plan[{step_index}].plan_step"
        )
        old_transition_ids = step.get("material_transition_ids")
        expected_old_transition_ids = [
            previous_relation_transitions.get(relationship_id)
            for relationship_id in normalized_direct
        ]
        if (
            normalized_direct
            and all(
                isinstance(value, str) and value.strip()
                for value in expected_old_transition_ids
            )
            and isinstance(old_transition_ids, list)
            and old_transition_ids == expected_old_transition_ids
        ):
            # These claims were written by the previous compiler manifest.
            # Ignore them as fresh Device authority so an authorized relation
            # may move or disappear on an evolution replay.
            previous_compiler_step_keys.add(step_key)
            continue
        for relationship_id in normalized_direct:
            candidate_claim_steps.setdefault(relationship_id, []).append(step_key)
    unknown_candidate_claims = sorted(set(candidate_claim_steps) - relationship_ids)
    if unknown_candidate_claims:
        issues.append(
            RelationshipCompileIssue(
                "unknown_candidate_relationship_binding",
                "Device steps claim relationship IDs absent from Research authority",
                blocker_class="plan_binding_invalid",
                relationship_ids=unknown_candidate_claims,
            )
        )
    claimed_none_relationships = sorted(
        set(candidate_claim_steps) & non_bindable_none_relationship_ids
    )
    if claimed_none_relationships:
        issues.append(
            RelationshipCompileIssue(
                "candidate_binding_for_none_relation",
                "Device steps may not claim event_kind=none Research relationships",
                blocker_class="plan_binding_invalid",
                relationship_ids=claimed_none_relationships,
            )
        )
    claimed_collection_registrations = sorted(
        set(candidate_claim_steps) & non_bindable_collection_relationship_ids
    )
    if claimed_collection_registrations:
        issues.append(
            RelationshipCompileIssue(
                "candidate_binding_for_collect_registration",
                "Device steps may not claim collect_same_material Research registrations",
                blocker_class="plan_binding_invalid",
                relationship_ids=claimed_collection_registrations,
            )
        )
    if issues:
        return original, issues, []

    relation_jobs: List[Dict[str, Any]] = []
    target_step_relation_ids: Dict[Tuple[str, Any], List[str]] = {}
    target_step_kinds: Dict[Tuple[str, Any], set[str]] = {}
    target_step_container_bindings: Dict[Tuple[str, Any], Dict[str, List[Any]]] = {}

    for macro in macro_records:
        for relation in macro["relations"]:
            relationship_id = relation["relationship_id"]
            direct_steps = list(candidate_claim_steps.get(relationship_id, []))
            manifest_step: Any = None
            manifest_container_bindings: Dict[str, Any] = {}
            manifest_status = ""
            required_capability = ""
            manifest_raw: Any = None
            if relationship_id in bindings:
                manifest_raw = bindings[relationship_id]
                manifest_step, manifest_container_bindings = _binding_record(
                    manifest_raw
                )
                manifest_status, required_capability = _binding_status(manifest_raw)
                if (
                    not isinstance(manifest_raw, dict)
                    or not isinstance(manifest_raw.get("binding_status"), str)
                    or not manifest_raw["binding_status"].strip()
                    or "disposition" in manifest_raw
                ):
                    issues.append(
                        RelationshipCompileIssue(
                            "invalid_relationship_binding_status",
                            "versioned binding records require an explicit binding_status and may not carry a legacy disposition",
                            blocker_class="contract_invalid",
                            relationship_id=relationship_id,
                        )
                    )
                    continue
                if not isinstance(manifest_container_bindings, dict):
                    issues.append(
                        RelationshipCompileIssue(
                            "invalid_relationship_binding_containers",
                            "logical_container_bindings must be an object",
                            blocker_class="contract_invalid",
                            relationship_id=relationship_id,
                        )
                    )
                    continue
                if manifest_status not in {"bound", "missing_operation"}:
                    issues.append(
                        RelationshipCompileIssue(
                            "invalid_relationship_binding_status",
                            "relationship binding status must be bound or missing_operation",
                            blocker_class="contract_invalid",
                            relationship_id=relationship_id,
                            binding_status=manifest_status or "<missing>",
                        )
                    )
                    continue
                if manifest_status == "bound" and required_capability:
                    issues.append(
                        RelationshipCompileIssue(
                            "invalid_bound_relationship_capability",
                            "a bound relationship may not also declare a missing required_capability",
                            blocker_class="contract_invalid",
                            relationship_id=relationship_id,
                        )
                    )
                    continue
                if manifest_status == "missing_operation":
                    if (
                        manifest_step is not None
                        or bool(manifest_container_bindings)
                        or not required_capability
                    ):
                        issues.append(
                            RelationshipCompileIssue(
                                "invalid_missing_operation_binding",
                                "missing_operation requires plan_step=null, no container bindings, and a nonempty required_capability",
                                blocker_class="contract_invalid",
                                relationship_id=relationship_id,
                            )
                        )
                        continue
                    if direct_steps:
                        issues.append(
                            RelationshipCompileIssue(
                                "missing_operation_binding_conflicts_with_device_step",
                                "sidecar declares a missing operation but the candidate directly binds the relation",
                                blocker_class="plan_binding_invalid",
                                relationship_id=relationship_id,
                                direct_binding_count=len(direct_steps),
                                required_capability=required_capability,
                            )
                        )
                        continue
                    issues.append(
                        RelationshipCompileIssue(
                            "relationship_operation_explicitly_missing",
                            "authorized sidecar records that no current Device operation implements this relation",
                            blocker_class="plan_missing_operation",
                            relationship_id=relationship_id,
                            required_capability=required_capability,
                        )
                    )
                    continue
                if manifest_step is None:
                    issues.append(
                        RelationshipCompileIssue(
                            "relationship_binding_missing_plan_step",
                            "bound relationship sidecar record requires an exact plan_step",
                            blocker_class="plan_binding_missing",
                            relationship_id=relationship_id,
                        )
                    )
                    continue
            else:
                if direct_steps:
                    issues.append(
                        RelationshipCompileIssue(
                            "unauthorized_candidate_relationship_binding",
                            "candidate research_material_relation_ids are claims and require a matching evidence-bound sidecar record",
                            blocker_class="plan_binding_invalid",
                            relationship_id=relationship_id,
                            claimed_plan_steps=[
                                copy.deepcopy(plan_values[key]) for key in direct_steps
                            ],
                        )
                    )
                    continue

            selected_keys: set[Tuple[str, Any]] = set()
            if manifest_step is not None:
                try:
                    manifest_key = macro_id_key(
                        manifest_step,
                        f"relationship_bindings.bindings.{relationship_id}.plan_step",
                    )
                    selected_keys.add(manifest_key)
                except MacroIdentityError as exc:
                    issues.append(
                        RelationshipCompileIssue(
                            exc.code.lower(), str(exc), path=exc.path
                        )
                    )
                    continue
                conflicting_claims = [
                    key for key in direct_steps if key != manifest_key
                ]
                if conflicting_claims:
                    issues.append(
                        RelationshipCompileIssue(
                            "candidate_relationship_binding_conflicts_with_authority",
                            "candidate relation claims conflict with the evidence-bound sidecar",
                            blocker_class="plan_binding_invalid",
                            relationship_id=relationship_id,
                            authorized_plan_step=copy.deepcopy(manifest_step),
                            conflicting_plan_steps=[
                                copy.deepcopy(plan_values[key])
                                for key in conflicting_claims
                            ],
                        )
                    )
                    continue
            if len(selected_keys) != 1:
                mapped_macro_steps: List[Any] = []
                for candidate_step in plan:
                    try:
                        candidate_sources = _source_macro_keys(
                            candidate_step,
                            "device_plan.relationship_binding_candidate",
                        )
                    except MacroIdentityError:
                        continue
                    candidate_workstation = str(
                        candidate_step.get("workstation") or ""
                    ).strip()
                    candidate_operation_intent = str(
                        candidate_step.get("operation_intent") or ""
                    ).strip()
                    resolved_candidate_workstation = (
                        str(resolve_workstation(candidate_workstation) or "").strip()
                        if resolve_workstation is not None
                        else candidate_workstation
                    )
                    if (
                        macro["macro_key"] in candidate_sources
                        and candidate_workstation
                        and resolved_candidate_workstation
                        and candidate_operation_intent
                    ):
                        mapped_macro_steps.append(
                            copy.deepcopy(candidate_step.get("plan_step"))
                        )
                if not selected_keys:
                    blocker_class = (
                        "plan_binding_missing"
                        if mapped_macro_steps
                        else "plan_missing_operation"
                    )
                else:
                    blocker_class = "plan_binding_ambiguous"
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_step_binding_missing_or_ambiguous",
                        "each relationship must bind to exactly one actual Device step",
                        blocker_class=blocker_class,
                        relationship_id=relationship_id,
                        direct_binding_count=len(direct_steps),
                        manifest_binding_present=False,
                        mapped_macro_steps=mapped_macro_steps,
                    )
                )
                continue
            step_key = next(iter(selected_keys))
            step = plan_by_id.get(step_key)
            if step is None:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_step_not_found",
                        "relationship binding names a Device step that does not exist",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(manifest_step),
                    )
                )
                continue
            implementation_step_keys = [step_key]
            if (
                relation["quantity_basis"] == RUNTIME_QUANTITY_MODE
                and str(binding_envelope.get("authoring_mode") or "").strip()
                != "automated_evidence_bound"
            ):
                issues.append(
                    RelationshipCompileIssue(
                        "runtime_quantity_requires_automated_skill_scope",
                        "manual binding evidence cannot prove runtime measurement or executable insufficient-quantity stopping support",
                        blocker_class="software_unsupported",
                        relationship_id=relationship_id,
                    )
                )
                continue
            if (
                str(binding_envelope.get("authoring_mode") or "").strip()
                == "automated_evidence_bound"
            ):
                implementation_step_keys, scope_issues = (
                    _automated_binding_scope_issues(
                        manifest_raw,
                        relationship_id=relationship_id,
                        relation=relation,
                        plan_by_id=plan_by_id,
                        plan_values=plan_values,
                        resolve_workstation=resolve_workstation,
                    )
                )
                if scope_issues:
                    issues.extend(scope_issues)
                    continue
                if step_key not in implementation_step_keys:
                    issues.append(
                        RelationshipCompileIssue(
                            "relationship_commit_step_not_in_scope",
                            "the material commit step must belong to the authorized implementation scope",
                            blocker_class="plan_binding_invalid",
                            relationship_id=relationship_id,
                            plan_step=copy.deepcopy(plan_values[step_key]),
                        )
                    )
                    continue
            try:
                source_keys = _source_macro_keys(
                    step, f"device_plan[step={plan_values[step_key]!r}]"
                )
            except MacroIdentityError as exc:
                issues.append(
                    RelationshipCompileIssue(exc.code.lower(), str(exc), path=exc.path)
                )
                continue
            if macro["macro_key"] not in source_keys:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_step_macro_mismatch",
                        "bound Device step does not reference the relationship's Research macro",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(plan_values[step_key]),
                        macro_step_id=copy.deepcopy(macro["macro_id"]),
                    )
                )
                continue
            workstation = str(step.get("workstation") or "").strip()
            if resolve_workstation is None:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_binding_workstation_resolver_missing",
                        "positive evidence-bound relationships require the bound workstation truth resolver",
                        blocker_class="contract_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(plan_values[step_key]),
                    )
                )
                continue
            resolved_workstation = (
                str(resolve_workstation(workstation) or "").strip()
            )
            if not workstation or not resolved_workstation:
                issues.append(
                    RelationshipCompileIssue(
                        "relationship_without_real_device_operation",
                        "bound relationship requires an existing truth-source workstation",
                        blocker_class="plan_missing_operation",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(plan_values[step_key]),
                        workstation=workstation,
                    )
                )
                continue
            operation_evidence_issues = _positive_binding_evidence_issues(
                manifest_raw,
                envelope=binding_envelope,
                relationship_id=relationship_id,
                relation=relation,
                step=step,
                plan_step=plan_values[step_key],
                resolved_workstation=resolved_workstation,
            )
            if operation_evidence_issues:
                issues.extend(operation_evidence_issues)
                continue
            existing_kind = str(step.get("material_event_kind") or "none").strip()
            if existing_kind not in {"none", relation["event_kind"]}:
                issues.append(
                    RelationshipCompileIssue(
                        "material_event_kind_conflict",
                        "Device step declares an event different from Research authority",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(plan_values[step_key]),
                        expected=relation["event_kind"],
                        actual=existing_kind,
                    )
                )
                continue
            declared_logical_bindings = step.get("logical_container_bindings")
            if declared_logical_bindings is None:
                declared_logical_bindings = {}
            if not isinstance(declared_logical_bindings, dict):
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_step_logical_container_bindings",
                        "logical_container_bindings must be an object",
                        blocker_class="plan_binding_invalid",
                        plan_step=copy.deepcopy(plan_values[step_key]),
                    )
                )
                continue
            combined_bindings = copy.deepcopy(declared_logical_bindings)
            for logical_id, physical in manifest_container_bindings.items():
                manifest_values = _normalize_logical_binding_values(physical)
                if logical_id in combined_bindings:
                    existing_values = _normalize_logical_binding_values(
                        combined_bindings[logical_id]
                    )
                    if [
                        _container_token(value) for value in existing_values
                    ] != [
                        _container_token(value) for value in manifest_values
                    ]:
                        issues.append(
                            RelationshipCompileIssue(
                                "logical_container_binding_conflict",
                                "Device declaration conflicts with repair authorization manifest",
                                blocker_class="plan_binding_ambiguous",
                                relationship_id=relationship_id,
                                logical_container_id=logical_id,
                            )
                        )
                # Store one canonical representation so a scalar authorization
                # and the compiler-emitted one-item array compare identically
                # on deterministic replay.
                combined_bindings[logical_id] = manifest_values
            physical_ids: List[Any] = []
            for implementation_key in implementation_step_keys:
                physical_ids.extend(
                    _physical_container_ids(plan_by_id[implementation_key])
                )
            physical_tokens = {_container_token(value) for value in physical_ids}
            resolved_bindings: Dict[str, List[Any]] = {}
            for logical_id in relation["logical_ids"]:
                logical = macro["containers"][logical_id]
                count = logical.get("count", 1)
                if isinstance(count, bool) or not isinstance(count, int) or count != 1:
                    issues.append(
                        RelationshipCompileIssue(
                            "logical_container_requires_instance_binding",
                            "relationship compiler requires count=1 logical instances; repeated containers need separate material instances and explicit bindings",
                            blocker_class="software_unsupported",
                            relationship_id=relationship_id,
                            logical_container_id=logical_id,
                            count=count,
                        )
                    )
                    continue
                if logical_id in combined_bindings:
                    values = _normalize_logical_binding_values(combined_bindings[logical_id])
                elif len(relation["logical_ids"]) == 1 and len(physical_ids) == 1:
                    values = [copy.deepcopy(physical_ids[0])]
                else:
                    values = []
                if len(values) != 1:
                    issues.append(
                        RelationshipCompileIssue(
                            "logical_container_binding_missing_or_ambiguous",
                            "each count=1 logical container needs exactly one physical binding",
                            blocker_class="plan_binding_missing",
                            relationship_id=relationship_id,
                            logical_container_id=logical_id,
                        )
                    )
                    continue
                if _container_token(values[0]) not in physical_tokens:
                    issues.append(
                        RelationshipCompileIssue(
                            "logical_container_not_used_by_step",
                            "logical container binding must name a container actually used by the bound Device step",
                            blocker_class="plan_binding_invalid",
                            relationship_id=relationship_id,
                            logical_container_id=logical_id,
                            physical_container=copy.deepcopy(values[0]),
                        )
                    )
                    continue
                resolved_bindings[logical_id] = values
            if len(resolved_bindings) != len(relation["logical_ids"]):
                continue
            # A software support verdict is meaningful only after the
            # Research relation is bound to one real Device operation and all
            # of its logical containers are bound to containers used there.
            # This ordering keeps missing plan work from being mislabeled as a
            # compiler limitation.
            support_issues = relation.get("compilation_support_issues") or []
            if support_issues:
                for support_issue in support_issues:
                    issues.append(
                        RelationshipCompileIssue(
                            str(support_issue.get("code") or "software_unsupported"),
                            str(
                                support_issue.get("message")
                                or "material relation form is not implemented"
                            ),
                            blocker_class="software_unsupported",
                            relationship_id=relationship_id,
                            **{
                                key: copy.deepcopy(value)
                                for key, value in support_issue.items()
                                if key not in {"code", "message"}
                            },
                        )
                    )
                continue
            previous_ids = target_step_relation_ids.setdefault(step_key, [])
            if relationship_id not in previous_ids:
                previous_ids.append(relationship_id)
            target_step_kinds.setdefault(step_key, set()).add(relation["event_kind"])
            target_step_container_bindings.setdefault(step_key, {}).update(
                copy.deepcopy(resolved_bindings)
            )
            runtime_capability_evidence: Dict[str, Dict[str, Any]] = {}
            runtime_report_field_contracts: Dict[str, Dict[str, Dict[str, Any]]] = {
                "input": {},
                "output": {},
            }
            if relation["quantity_basis"] == RUNTIME_QUANTITY_MODE:
                scoped_evidence = manifest_raw.get("implementation_step_evidence")
                scoped_evidence = (
                    scoped_evidence if isinstance(scoped_evidence, list) else []
                )
                measurement_evidence = [
                    item
                    for item in scoped_evidence
                    if isinstance(item, dict)
                    and isinstance(item.get("runtime_quantity_support"), dict)
                    and item["runtime_quantity_support"].get(
                        "actual_inventory_measurement_declared"
                    )
                    is True
                    and bool(
                        item["runtime_quantity_support"].get(
                            "reported_measurements"
                        )
                    )
                ]
                stop_evidence = [
                    item
                    for item in scoped_evidence
                    if isinstance(item, dict)
                    and isinstance(item.get("runtime_quantity_support"), dict)
                    and item["runtime_quantity_support"].get(
                        "insufficient_quantity_stop_declared"
                    )
                    is True
                    and item["runtime_quantity_support"].get(
                        "insufficient_quantity_stop_policy"
                    )
                    == "stop_before_material_consumption"
                ]
                # The scope validator above has already required exactly one of
                # each.  Keep only immutable identifiers and Skill digests in
                # generated obligations; never copy a free-text assertion.
                runtime_capability_evidence = {
                    "measurement": copy.deepcopy(measurement_evidence[0]),
                    "insufficient_quantity_stop": copy.deepcopy(stop_evidence[0]),
                }
                skill_measurement_contracts = measurement_evidence[0][
                    "runtime_quantity_support"
                ].get("reported_measurement_contracts")
                skill_measurement_contracts = (
                    skill_measurement_contracts
                    if isinstance(skill_measurement_contracts, list)
                    else []
                )
                report_contract_invalid = False
                for role, endpoint_contracts in relation[
                    "runtime_measurement_contracts"
                ].items():
                    for instance_id, endpoint_contract in endpoint_contracts.items():
                        allowed_units = set(endpoint_contract["allowed_report_units"])
                        matching_contracts = [
                            contract
                            for contract in skill_measurement_contracts
                            if isinstance(contract, dict)
                            and contract.get("dimension")
                            == endpoint_contract["expected_quantity_dimension"]
                            and isinstance(contract.get("allowed_units"), list)
                            and allowed_units.issubset(
                                {
                                    str(value).strip()
                                    for value in contract["allowed_units"]
                                    if isinstance(value, str) and value.strip()
                                }
                            )
                            and isinstance(contract.get("field_name"), str)
                            and str(contract.get("field_name") or "").strip()
                        ]
                        if len(matching_contracts) != 1:
                            issues.append(
                                RelationshipCompileIssue(
                                    "runtime_measurement_skill_field_unresolved",
                                    "the bound Skill must expose exactly one reported field matching each endpoint's frozen quantity dimension and unit",
                                    blocker_class=(
                                        "plan_binding_ambiguous"
                                        if len(matching_contracts) > 1
                                        else "software_unsupported"
                                    ),
                                    relationship_id=relationship_id,
                                    role=role,
                                    material_instance_id=instance_id,
                                    expected_quantity_dimension=endpoint_contract[
                                        "expected_quantity_dimension"
                                    ],
                                    allowed_report_units=sorted(allowed_units),
                                    match_count=len(matching_contracts),
                                )
                            )
                            report_contract_invalid = True
                            continue
                        runtime_report_field_contracts[role][instance_id] = (
                            copy.deepcopy(matching_contracts[0])
                        )
                if report_contract_invalid:
                    continue
            relation_jobs.append(
                {
                    **copy.deepcopy(relation),
                    "macro": macro,
                    "step_key": step_key,
                    "step_id": copy.deepcopy(plan_values[step_key]),
                    "implementation_step_ids": [
                        copy.deepcopy(plan_values[key])
                        for key in implementation_step_keys
                    ],
                    "container_bindings": resolved_bindings,
                    "runtime_capability_evidence": runtime_capability_evidence,
                    "runtime_report_field_contracts": (
                        runtime_report_field_contracts
                    ),
                }
            )

    if any(len(kinds) != 1 for kinds in target_step_kinds.values()):
        for step_key, kinds in target_step_kinds.items():
            if len(kinds) > 1:
                issues.append(
                    RelationshipCompileIssue(
                        "multiple_event_kinds_on_device_step",
                        "one Device operation cannot implement different material event kinds",
                        plan_step=copy.deepcopy(plan_values[step_key]),
                        event_kinds=sorted(kinds),
                    )
                )
    if issues:
        return original, issues, []

    # Build a fresh graph independently of pre-existing compiler-owned records.
    # No mutation reaches ``candidate`` until every relation has compiled.
    generated_batches: Dict[str, Dict[str, Any]] = {}
    generated_transitions: Dict[str, Dict[str, Any]] = {}
    generated_runtime_obligations: Dict[str, Dict[str, Any]] = {}
    generated_consumption_events: Dict[str, Dict[str, Any]] = {}
    batch_for_port: Dict[Tuple[str, str, str], str] = {}
    batch_consumers: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    batch_runtime_dependencies: Dict[str, set[str]] = {}
    batch_measurement_events: Dict[str, set[str]] = {}
    whole_batch_consumed_by: Dict[str, str] = {}
    transition_by_relationship: Dict[str, str] = {}

    def batch_mode_for_port(port: Dict[str, Any]) -> str:
        mode = _quantity_mode(port)
        if mode == "all_available":
            return "whole_batch"
        if mode == "runtime_measured":
            return RUNTIME_QUANTITY_MODE
        return "numeric_inventory"

    def external_batch(macro: Dict[str, Any], instance_id: str) -> Optional[str]:
        key = (macro["raw_id"], "input", instance_id)
        if key in batch_for_port:
            return batch_for_port[key]
        port = macro["input_nodes"][instance_id]
        if str(port.get("material_origin") or "") != "external_inventory":
            return None
        batch_id = _stable_id("MRB", macro["raw_id"], "input", instance_id)
        source_ref = _research_source_ref(
            macro["index"], int(port["_index"]), macro["macro_id"], port
        )
        quantity_mode = batch_mode_for_port(port)
        batch: Dict[str, Any] = {
            "batch_id": batch_id,
            "quantity_mode": quantity_mode,
            "material_id": str(port.get("material_id") or ""),
            "material_identity_id": str(port.get("material_id") or ""),
            "research_material_instance_id": instance_id,
            "is_root_batch": True,
            "source_kind": "research",
            "source_refs": [source_ref["source_path"]],
            "research_source_refs": [source_ref],
            "source_macro_steps": [copy.deepcopy(macro["macro_id"])],
            "sample_id": macro["sample_id"],
            "state_label": str(port.get("state") or "unknown"),
            "logical_container_id": str(port.get("logical_container_id") or ""),
            "record_phase": "planned",
            "quantity_assertion": "planned_only",
            "execution_fact": False,
            "construction_rule": RULE_ID,
        }
        planned = _planned_quantity(port)
        if quantity_mode == "numeric_inventory" and planned is not None:
            batch["total_quantity"] = _quantity_without_planning_metadata(planned)
            batch["quantity_evidence_kind"] = planned["evidence_kind"]
            batch["availability_status"] = "planned_unverified"
            batch["calculation"] = (
                "Research planned quantity; execution must still verify availability."
            )
        elif quantity_mode == RUNTIME_QUANTITY_MODE:
            batch["measurement_status"] = "pending"
            batch["calculation"] = (
                "Research requires a future runtime measurement; no inventory value exists yet."
            )
        else:
            batch["calculation"] = (
                "Research whole-batch identity boundary; no numeric inventory is asserted."
            )
        generated_batches[batch_id] = batch
        batch_for_port[key] = batch_id
        batch_consumers.setdefault(batch_id, {})
        batch_runtime_dependencies.setdefault(batch_id, set())
        batch_measurement_events.setdefault(batch_id, set())
        return batch_id

    def resolve_parent_batch(
        macro: Dict[str, Any], instance_id: str
    ) -> Tuple[Optional[str], bool]:
        port = macro["input_nodes"][instance_id]
        origin = str(port.get("material_origin") or "").strip()
        if origin == "external_inventory":
            return external_batch(macro, instance_id), False
        if origin == "upstream_output":
            upstream_refs = port.get("parent_output_refs")
            if not isinstance(upstream_refs, list) or len(upstream_refs) != 1:
                issues.append(
                    RelationshipCompileIssue(
                        "missing_or_ambiguous_upstream_output_ref",
                        "each relation input instance must identify exactly one upstream output batch",
                        material_instance_id=instance_id,
                        parent_output_ref_count=(
                            len(upstream_refs) if isinstance(upstream_refs, list) else 0
                        ),
                    )
                )
                return None, False
            upstream = upstream_refs[0]
            if not isinstance(upstream, dict):
                issues.append(
                    RelationshipCompileIssue(
                        "invalid_upstream_output_ref",
                        "parent_output_refs entries must be objects",
                        material_instance_id=instance_id,
                    )
                )
                return None, False
            upstream_macro = str(upstream.get("macro_step_id") or "").strip()
            upstream_instance = str(upstream.get("material_instance_id") or "").strip()
            if (upstream_macro, upstream_instance) not in output_ports_global:
                issues.append(
                    RelationshipCompileIssue(
                        "upstream_output_not_found",
                        "upstream output reference does not resolve in Research authority",
                        material_instance_id=instance_id,
                        upstream_output_ref=copy.deepcopy(upstream),
                    )
                )
                return None, False
            parent_batch = batch_for_port.get(
                (upstream_macro, "output", upstream_instance)
            )
            if parent_batch is None:
                return None, True
            batch_for_port[(macro["raw_id"], "input", instance_id)] = parent_batch
            return parent_batch, False
        if origin == "same_step_relation":
            parent_batch = batch_for_port.get(
                (macro["raw_id"], "output", instance_id)
            )
            return parent_batch, parent_batch is None
        issues.append(
            RelationshipCompileIssue(
                "invalid_material_origin",
                "material input origin must be external_inventory, upstream_output, or same_step_relation",
                material_instance_id=instance_id,
                material_origin=origin,
            )
        )
        return None, False

    def promote_batch_to_runtime_pending(batch_id: str) -> None:
        batch = generated_batches[batch_id]
        previous_mode = str(batch.get("quantity_mode") or "")
        if previous_mode == RUNTIME_QUANTITY_MODE:
            return
        planned = batch.pop("total_quantity", None)
        if isinstance(planned, dict):
            batch["planned_quantity"] = copy.deepcopy(planned)
        batch["declared_quantity_mode"] = previous_mode
        batch["quantity_mode"] = RUNTIME_QUANTITY_MODE
        batch["measurement_status"] = "pending"
        batch["availability_status"] = "pending_runtime_measurement"
        batch.pop("allocation", None)

    def attach_container(
        *,
        job: Dict[str, Any],
        batch_id: str,
        logical_id: str,
    ) -> bool:
        if not logical_id or logical_id not in job["container_bindings"]:
            issues.append(
                RelationshipCompileIssue(
                    "port_container_binding_missing",
                    "every material endpoint container must resolve to one physical binding",
                    relationship_id=job["relationship_id"],
                    logical_container_id=logical_id or "<missing>",
                )
            )
            return False
        physical = copy.deepcopy(job["container_bindings"][logical_id][0])
        batch = generated_batches[batch_id]
        existing = batch.get("container_ref")
        if existing is not None and _container_token(existing) != _container_token(physical):
            issues.append(
                RelationshipCompileIssue(
                    "implicit_container_transfer",
                    "a material batch changed physical containers without an explicit transfer relation",
                    relationship_id=job["relationship_id"],
                    batch_id=batch_id,
                    previous_container=copy.deepcopy(existing),
                    current_container=copy.deepcopy(physical),
                )
            )
            return False
        batch["container_ref"] = physical
        batch["logical_container_id"] = logical_id
        return True

    pending = list(relation_jobs)
    while pending and not issues:
        progressed = False
        remaining: List[Dict[str, Any]] = []
        for job in pending:
            macro = job["macro"]
            parent_batches: List[str] = []
            unresolved_parent = False
            for input_id in job["input_ids"]:
                parent_batch, wait_for_upstream = resolve_parent_batch(macro, input_id)
                if issues:
                    break
                if wait_for_upstream:
                    unresolved_parent = True
                    break
                if parent_batch is None:
                    issues.append(
                        RelationshipCompileIssue(
                            "material_input_batch_unresolved",
                            "material input did not resolve to an external or upstream batch",
                            relationship_id=job["relationship_id"],
                            material_instance_id=input_id,
                        )
                    )
                    break
                parent_batches.append(parent_batch)
            if issues:
                break
            if unresolved_parent:
                remaining.append(job)
                continue
            if len(set(parent_batches)) != len(parent_batches):
                issues.append(
                    RelationshipCompileIssue(
                        "duplicate_parent_batch_alias",
                        "one relation may not consume the same batch through multiple material instance aliases",
                        relationship_id=job["relationship_id"],
                        parent_batch_ids=parent_batches,
                    )
                )
                break

            for input_id, parent_batch in zip(job["input_ids"], parent_batches):
                input_port = macro["input_nodes"][input_id]
                input_logical = str(input_port.get("logical_container_id") or "").strip()
                if not attach_container(
                    job=job, batch_id=parent_batch, logical_id=input_logical
                ):
                    break
            if issues:
                break

            quantity_basis = job["quantity_basis"]
            transition_id = _stable_id(
                "MRT", macro["raw_id"], job["relationship_id"]
            )
            transition_consumer_id = f"material_transition:{transition_id}"
            input_allocation_by_id = {
                item["material_instance_id"]: item
                for item in job.get("input_allocations") or []
            }
            output_allocation_by_id = {
                item["material_instance_id"]: item
                for item in job.get("output_allocations") or []
            }
            inherited_runtime_dependencies = set().union(
                *(batch_runtime_dependencies.get(batch_id, set()) for batch_id in parent_batches)
            ) if parent_batches else set()
            inherited_measurement_events = set().union(
                *(batch_measurement_events.get(batch_id, set()) for batch_id in parent_batches)
            ) if parent_batches else set()

            if quantity_basis == "whole_batch":
                parent_batch = parent_batches[0]
                parent_mode = str(generated_batches[parent_batch].get("quantity_mode") or "")
                if parent_batch in whole_batch_consumed_by:
                    issues.append(
                        RelationshipCompileIssue(
                            "whole_batch_consumed_more_than_once",
                            "a symbolic whole batch cannot feed multiple downstream consumers",
                            batch_id=parent_batch,
                            first_relationship=whole_batch_consumed_by[parent_batch],
                            second_relationship=job["relationship_id"],
                        )
                    )
                    break
                if _quantity_mode(macro["input_nodes"][job["input_ids"][0]]) != "all_available" or _quantity_mode(
                    macro["output_nodes"][job["output_ids"][0]]
                ) != "all_available":
                    issues.append(
                        RelationshipCompileIssue(
                            "whole_batch_port_quantity_mismatch",
                            "whole_batch lineage requires all_available input and output ports",
                            relationship_id=job["relationship_id"],
                        )
                    )
                    break
                if parent_mode not in {"whole_batch", RUNTIME_QUANTITY_MODE}:
                    issues.append(
                        RelationshipCompileIssue(
                            "whole_batch_parent_has_numeric_inventory",
                            "numeric inventory cannot be silently downgraded to whole_batch",
                            relationship_id=job["relationship_id"],
                            batch_id=parent_batch,
                        )
                    )
                    break
                whole_batch_consumed_by[parent_batch] = job["relationship_id"]
            elif quantity_basis == "conserved_inventory":
                for input_id, parent_batch in zip(job["input_ids"], parent_batches):
                    allocation = input_allocation_by_id[input_id]["quantity"]
                    parent = generated_batches[parent_batch]
                    parent_mode = str(parent.get("quantity_mode") or "")
                    if parent_mode == "whole_batch":
                        issues.append(
                            RelationshipCompileIssue(
                                "conserved_inventory_source_requires_measurement",
                                "a whole-batch source cannot fund a numeric allocation without a runtime measurement obligation",
                                blocker_class="software_unsupported",
                                relationship_id=job["relationship_id"],
                                batch_id=parent_batch,
                            )
                        )
                        break
                    if parent_mode == RUNTIME_QUANTITY_MODE:
                        issues.append(
                            RelationshipCompileIssue(
                                "conserved_inventory_source_pending_measurement",
                                "a conserved allocation cannot spend a pending runtime quantity before its observation is verified",
                                blocker_class="software_unsupported",
                                relationship_id=job["relationship_id"],
                                batch_id=parent_batch,
                            )
                        )
                        break
                    if parent_mode == "numeric_inventory":
                        total = parent.get("total_quantity")
                        if not isinstance(total, dict) or str(total.get("unit") or "").strip() != str(
                            allocation.get("unit") or ""
                        ).strip():
                            issues.append(
                                RelationshipCompileIssue(
                                    "conserved_inventory_parent_unit_mismatch",
                                    "numeric parent total and allocation must use the same explicit unit",
                                    relationship_id=job["relationship_id"],
                                    batch_id=parent_batch,
                                )
                            )
                            break
                if issues:
                    break
            elif quantity_basis == RUNTIME_QUANTITY_MODE:
                for parent_batch in parent_batches:
                    promote_batch_to_runtime_pending(parent_batch)

            child_batches: List[str] = []
            for output_id in job["output_ids"]:
                output_port = macro["output_nodes"][output_id]
                child_batch = _stable_id(
                    "MRB", macro["raw_id"], "output", output_id
                )
                if child_batch in generated_batches:
                    issues.append(
                        RelationshipCompileIssue(
                            "output_instance_produced_more_than_once",
                            "one material output instance must have exactly one producer",
                            relationship_id=job["relationship_id"],
                            material_instance_id=output_id,
                        )
                    )
                    break
                if quantity_basis == "conserved_inventory":
                    child_mode = "numeric_inventory"
                elif quantity_basis == RUNTIME_QUANTITY_MODE or inherited_runtime_dependencies:
                    child_mode = RUNTIME_QUANTITY_MODE
                else:
                    child_mode = "whole_batch"
                output_logical = str(output_port.get("logical_container_id") or "").strip()
                if not output_logical or output_logical not in job["container_bindings"]:
                    issues.append(
                        RelationshipCompileIssue(
                            "port_container_binding_missing",
                            "every material endpoint container must resolve to one physical binding",
                            relationship_id=job["relationship_id"],
                            logical_container_id=output_logical or "<missing>",
                        )
                    )
                    break
                child: Dict[str, Any] = {
                    "batch_id": child_batch,
                    "quantity_mode": child_mode,
                    "material_id": str(output_port.get("material_id") or ""),
                    "material_identity_id": str(output_port.get("material_id") or ""),
                    "research_material_instance_id": output_id,
                    "is_root_batch": False,
                    "source_kind": "derived",
                    "source_refs": [f"material_transition:{transition_id}"],
                    "transition_kind": job["event_kind"],
                    "parent_batch_ids": list(parent_batches),
                    "sample_id": macro["sample_id"],
                    "source_plan_steps": [copy.deepcopy(job["step_id"])],
                    "implementation_plan_steps": copy.deepcopy(
                        job["implementation_step_ids"]
                    ),
                    "source_macro_steps": [copy.deepcopy(macro["macro_id"])],
                    "logical_container_id": output_logical,
                    "container_ref": copy.deepcopy(
                        job["container_bindings"][output_logical][0]
                    ),
                    "state_label": str(output_port.get("state") or "unknown"),
                    "record_phase": "planned",
                    "quantity_assertion": "planned_only",
                    "execution_fact": False,
                    "construction_rule": RULE_ID,
                }
                if child_mode == "numeric_inventory":
                    allocation = output_allocation_by_id[output_id]
                    child["total_quantity"] = copy.deepcopy(allocation["quantity"])
                    child["quantity_evidence_kind"] = (
                        "research_conserved_planning_allocation_not_execution_fact"
                    )
                    child["availability_status"] = "planned_unverified"
                    child["calculation"] = (
                        "Research conserved allocation; planned quantity only."
                    )
                elif child_mode == RUNTIME_QUANTITY_MODE:
                    child["measurement_status"] = "pending"
                    child["availability_status"] = "pending_runtime_measurement"
                    child["calculation"] = (
                        "Quantity remains unavailable until the bound runtime observation is verified."
                    )
                else:
                    child["calculation"] = (
                        "Whole-batch planned lineage; no numeric quantity is asserted."
                    )
                generated_batches[child_batch] = child
                batch_for_port[(macro["raw_id"], "output", output_id)] = child_batch
                batch_consumers.setdefault(child_batch, {})
                batch_runtime_dependencies[child_batch] = set(
                    inherited_runtime_dependencies
                )
                batch_measurement_events[child_batch] = set(
                    inherited_measurement_events
                )
                child_batches.append(child_batch)
            if issues:
                break

            input_edges: List[Dict[str, Any]] = []
            output_edges: List[Dict[str, Any]] = []
            if quantity_basis == "conserved_inventory":
                for input_id, parent_batch in zip(job["input_ids"], parent_batches):
                    input_edges.append(
                        {
                            "batch_id": parent_batch,
                            "quantity": copy.deepcopy(
                                input_allocation_by_id[input_id]["quantity"]
                            ),
                            "research_material_instance_id": input_id,
                        }
                    )
                for output_id, child_batch in zip(job["output_ids"], child_batches):
                    output_edges.append(
                        {
                            "batch_id": child_batch,
                            "quantity": copy.deepcopy(
                                output_allocation_by_id[output_id]["quantity"]
                            ),
                            "research_material_instance_id": output_id,
                        }
                    )

            obligation_id = ""
            required_input_measurements: List[Dict[str, Any]] = []
            required_output_measurements: List[Dict[str, Any]] = []
            if quantity_basis == RUNTIME_QUANTITY_MODE:
                measurement_evidence = job["runtime_capability_evidence"][
                    "measurement"
                ]
                stop_evidence = job["runtime_capability_evidence"][
                    "insufficient_quantity_stop"
                ]
                obligation_id = _stable_id(
                    "MRO", macro["raw_id"], job["relationship_id"]
                )
                for role, instance_ids, batch_ids, node_map, destination in (
                    ("input", job["input_ids"], parent_batches, macro["input_nodes"], required_input_measurements),
                    ("output", job["output_ids"], child_batches, macro["output_nodes"], required_output_measurements),
                ):
                    for instance_id, batch_id in zip(instance_ids, batch_ids):
                        port = node_map[instance_id]
                        event_id = _stable_id(
                            "MRM", macro["raw_id"], job["relationship_id"], role, instance_id
                        )
                        planned = _planned_quantity(port)
                        event: Dict[str, Any] = {
                            "measurement_event_id": event_id,
                            "role": role,
                            "status": "pending",
                            "batch_id": batch_id,
                            "material_id": str(port.get("material_id") or ""),
                            "material_instance_id": instance_id,
                            "sample_id": macro["sample_id"],
                            "plan_step": copy.deepcopy(job["step_id"]),
                            "reported_quantity_field_required": True,
                            "reported_unit_field_required": True,
                            "observation_id_required": True,
                            "artifact_digest_required": True,
                            **copy.deepcopy(
                                job["runtime_measurement_contracts"][role][
                                    instance_id
                                ]
                            ),
                            "report_source_contract": {
                                "source_role_id": measurement_evidence["role_id"],
                                "source_plan_step": copy.deepcopy(
                                    measurement_evidence["plan_step"]
                                ),
                                "station_code": measurement_evidence["station_code"],
                                "capability_id": measurement_evidence["capability_id"],
                                "skill_operation_name": measurement_evidence[
                                    "skill_operation_name"
                                ],
                                "skill_contract_digest_scope": measurement_evidence[
                                    "skill_contract_digest_scope"
                                ],
                                "skill_contract_sha256": measurement_evidence[
                                    "skill_contract_sha256"
                                ],
                                "reported_measurement_contract": copy.deepcopy(
                                    job["runtime_report_field_contracts"][role][
                                        instance_id
                                    ]
                                ),
                            },
                            "observation_contract": {
                                "observation_id_required": True,
                                "observation_id_uniqueness_scope": (
                                    "material_runtime_measurement_obligations"
                                ),
                                "artifact_digest_required": True,
                                "artifact_digest_algorithm": "sha256",
                                "report_source_match_required": True,
                            },
                            "record_phase": "planned",
                            "quantity_assertion": "planned_only",
                            "execution_fact": False,
                        }
                        if planned is not None:
                            event["planned_target"] = _quantity_without_planning_metadata(
                                planned
                            )
                            event["planned_target_semantic"] = planned["semantic"]
                        destination.append(event)
                        batch_measurement_events.setdefault(batch_id, set()).add(event_id)
                blocked_event_ids = sorted(
                    {
                        item["measurement_event_id"]
                        for item in required_input_measurements
                    }
                    | inherited_measurement_events
                )
                obligation = {
                    "schema": RUNTIME_MEASUREMENT_SCHEMA,
                    "schema_version": RUNTIME_MEASUREMENT_SCHEMA_VERSION,
                    "obligation_id": obligation_id,
                    "status": "pending",
                    "record_phase": "planned",
                    "quantity_assertion": "planned_only",
                    "execution_fact": False,
                    "research_material_relationship_id": job["relationship_id"],
                    "transition_id": transition_id,
                    "plan_step": copy.deepcopy(job["step_id"]),
                    "implementation_plan_steps": copy.deepcopy(
                        job["implementation_step_ids"]
                    ),
                    "source_macro_steps": [copy.deepcopy(macro["macro_id"])],
                    "input_batch_ids": list(parent_batches),
                    "output_batch_ids": list(child_batches),
                    "required_input_measurements": required_input_measurements,
                    "required_output_measurements": required_output_measurements,
                    "depends_on_obligation_ids": sorted(inherited_runtime_dependencies),
                    "pre_consumption_gate": {
                        "policy": "all_measurements_verified_and_quantity_sufficient",
                        "blocked_until_measurement_event_ids": blocked_event_ids,
                        "on_insufficient_quantity": "stop_before_material_consumption",
                        "stop_controller": {
                            "source_role_id": stop_evidence["role_id"],
                            "source_plan_step": copy.deepcopy(
                                stop_evidence["plan_step"]
                            ),
                            "station_code": stop_evidence["station_code"],
                            "capability_id": stop_evidence["capability_id"],
                            "skill_operation_name": stop_evidence[
                                "skill_operation_name"
                            ],
                            "skill_contract_digest_scope": stop_evidence[
                                "skill_contract_digest_scope"
                            ],
                            "skill_contract_sha256": stop_evidence[
                                "skill_contract_sha256"
                            ],
                        },
                    },
                    "downstream_consumption_gate": {
                        "policy": "all_output_measurements_verified",
                        "blocked_until_measurement_event_ids": sorted(
                            item["measurement_event_id"]
                            for item in required_output_measurements
                        ),
                    },
                    "planning_input_allocations": copy.deepcopy(
                        job.get("input_allocations") or []
                    ),
                    "planning_output_allocations": copy.deepcopy(
                        job.get("output_allocations") or []
                    ),
                    "actual_measurements": [],
                    "construction_rule": RULE_ID,
                }
                generated_runtime_obligations[obligation_id] = obligation
                for child_batch in child_batches:
                    batch_runtime_dependencies[child_batch].add(obligation_id)
                    generated_batches[child_batch]["runtime_measurement_obligation_ids"] = sorted(
                        batch_runtime_dependencies[child_batch]
                    )

            transition: Dict[str, Any] = {
                "transition_id": transition_id,
                "transition_kind": job["event_kind"],
                "parent_batch_ids": list(parent_batches),
                "child_batch_ids": list(child_batches),
                "source_plan_steps": [copy.deepcopy(job["step_id"])],
                "implementation_plan_steps": copy.deepcopy(
                    job["implementation_step_ids"]
                ),
                "source_macro_steps": [copy.deepcopy(macro["macro_id"])],
                "research_material_relationship_id": job["relationship_id"],
                "research_input_material_instance_ids": list(job["input_ids"]),
                "research_output_material_instance_ids": list(job["output_ids"]),
                "logical_container_bindings": copy.deepcopy(job["container_bindings"]),
                "quantity_basis": quantity_basis,
                "before_material_states": [
                    str(macro["input_nodes"][value].get("state") or "unknown")
                    for value in job["input_ids"]
                ],
                "after_material_states": [
                    str(macro["output_nodes"][value].get("state") or "unknown")
                    for value in job["output_ids"]
                ],
                "source_operation_ref": job["source_operation_ref"],
                "provenance": copy.deepcopy(job["provenance"]),
                "record_phase": "planned",
                "quantity_assertion": "planned_only",
                "execution_fact": False,
                "construction_rule": RULE_ID,
            }
            if len(job["input_ids"]) == 1:
                transition["before_material_state"] = transition["before_material_states"][0]
            if len(job["output_ids"]) == 1:
                transition["after_material_state"] = transition["after_material_states"][0]
            if quantity_basis == "conserved_inventory":
                transition["input_allocations"] = input_edges
                transition["output_allocations"] = output_edges
                transition["calculation_or_basis"] = (
                    "Research-authorized conserved planning allocations; not executed consumption."
                )
            elif quantity_basis == RUNTIME_QUANTITY_MODE:
                transition["runtime_measurement_obligation_id"] = obligation_id
                transition["execution_status"] = "awaiting_runtime_measurement"
                transition["input_allocations"] = []
                transition["output_allocations"] = []
                transition["calculation_or_basis"] = (
                    "Pending runtime measurement obligation; no produced, consumed, or available quantity is asserted."
                )
            else:
                transition["calculation_or_basis"] = (
                    "Explicit Research material relationship; whole_batch records planned 1-to-1 lineage only."
                )
                if inherited_runtime_dependencies:
                    transition["depends_on_runtime_measurement_obligation_ids"] = sorted(
                        inherited_runtime_dependencies
                    )
                    transition["execution_status"] = "blocked_pending_measurement"
            generated_transitions[transition_id] = transition
            transition_by_relationship[job["relationship_id"]] = transition_id

            for input_id, parent_batch in zip(job["input_ids"], parent_batches):
                planned_allocation: Optional[Dict[str, Any]] = None
                if quantity_basis == "conserved_inventory":
                    planned_allocation = copy.deepcopy(
                        input_allocation_by_id[input_id]["quantity"]
                    )
                event_id = _stable_id(
                    "MRC", macro["raw_id"], job["relationship_id"], input_id, parent_batch
                )
                event: Dict[str, Any] = {
                    "consumption_event_id": event_id,
                    "status": "pending_execution",
                    "record_phase": "planned",
                    "quantity_assertion": "planned_only",
                    "execution_fact": False,
                    "consumer_id": transition_consumer_id,
                    "transition_id": transition_id,
                    "research_material_relationship_id": job["relationship_id"],
                    "batch_id": parent_batch,
                    "material_instance_id": input_id,
                    "plan_step": copy.deepcopy(job["step_id"]),
                    "implementation_plan_steps": copy.deepcopy(
                        job["implementation_step_ids"]
                    ),
                    "actual_quantity": None,
                    "construction_rule": RULE_ID,
                }
                if planned_allocation is not None:
                    event["planned_quantity"] = planned_allocation
                elif quantity_basis == RUNTIME_QUANTITY_MODE:
                    planned = _planned_quantity(macro["input_nodes"][input_id])
                    if planned is not None:
                        event["planned_quantity"] = _quantity_without_planning_metadata(
                            planned
                        )
                    event["blocked_until_measurement_event_ids"] = sorted(
                        item["measurement_event_id"]
                        for item in required_input_measurements
                        if item["batch_id"] == parent_batch
                    )
                generated_consumption_events[event_id] = event
                batch_consumers.setdefault(parent_batch, {})[
                    transition_consumer_id
                ] = planned_allocation
                generated_batches[parent_batch].setdefault(
                    "consumption_event_ids", []
                ).append(event_id)
            progressed = True
        if issues:
            break
        if not progressed and remaining:
            issues.append(
                RelationshipCompileIssue(
                    "material_relationship_cycle_or_forward_gap",
                    "relationship graph cannot resolve its upstream output dependencies",
                    relationship_ids=[job["relationship_id"] for job in remaining],
                )
            )
            break
        pending = remaining

    if issues:
        return original, issues, []

    # Finalize planned consumers only after the complete graph exists.  Numeric
    # totals and allocations remain planning facts; pending batches carry no
    # arithmetic fields at all.
    for batch_id, batch in generated_batches.items():
        consumers = batch_consumers.get(batch_id, {})
        batch["consumer_ids"] = sorted(consumers)
        quantity_mode = str(batch.get("quantity_mode") or "")
        if quantity_mode == "numeric_inventory":
            numeric_consumers = {
                consumer_id: copy.deepcopy(quantity)
                for consumer_id, quantity in consumers.items()
                if isinstance(quantity, dict)
            }
            if len(numeric_consumers) != len(consumers):
                issues.append(
                    RelationshipCompileIssue(
                        "numeric_inventory_consumer_quantity_pending",
                        "numeric inventory cannot be consumed without a planned allocation; use a runtime measurement obligation",
                        blocker_class="software_unsupported",
                        batch_id=batch_id,
                    )
                )
                continue
            if numeric_consumers:
                batch["allocation"] = numeric_consumers
            else:
                batch["terminal_material"] = True
        elif quantity_mode == RUNTIME_QUANTITY_MODE:
            if len(consumers) > 1:
                issues.append(
                    RelationshipCompileIssue(
                        "runtime_quantity_multi_consumer_reservation_unsupported",
                        "one pending measured batch cannot fund multiple consumers until an atomic runtime reservation ledger is implemented",
                        blocker_class="software_unsupported",
                        batch_id=batch_id,
                        consumer_ids=sorted(consumers),
                    )
                )
                continue
            batch["measurement_status"] = "pending"
            batch["runtime_measurement_obligation_ids"] = sorted(
                batch_runtime_dependencies.get(batch_id, set())
            )
            batch["measurement_event_ids"] = sorted(
                batch_measurement_events.get(batch_id, set())
            )
            batch.pop("total_quantity", None)
            batch.pop("allocation", None)
    if issues:
        return original, issues, []

    generated_ledger: Dict[str, Dict[str, Any]] = {}
    for batch_id, batch in generated_batches.items():
        quantity_mode = str(batch.get("quantity_mode") or "")
        entry: Dict[str, Any] = {
            "entry_id": f"ml_{batch_id}",
            "batch_id": batch_id,
            "material_id": batch["material_id"],
            "material_identity_id": batch["material_identity_id"],
            "research_material_instance_id": batch["research_material_instance_id"],
            "quantity_mode": quantity_mode,
            "sample_id": batch["sample_id"],
            "source_kind": batch["source_kind"],
            "source_refs": list(batch.get("source_refs") or []),
            "consumer_ids": list(batch.get("consumer_ids") or []),
            "record_phase": "planned",
            "quantity_assertion": "planned_only",
            "execution_fact": False,
            "construction_rule": RULE_ID,
        }
        if not batch.get("is_root_batch"):
            entry["processing_step_refs"] = list(batch.get("source_plan_steps") or [])
            # R2: one stable production-event identity per batch, referenced
            # from every derived view (ledger, relation graph, transitions).
            entry["production_event_id"] = _stable_id("MRPE", batch_id)
        if quantity_mode == "numeric_inventory":
            total = copy.deepcopy(batch.get("total_quantity"))
            entry["planned_total"] = total
            consumer_rows = []
            allocated_total = 0.0
            unit = str(total.get("unit") or "") if isinstance(total, dict) else ""
            for consumer_id, quantity in sorted(
                (batch.get("allocation") or {}).items()
            ):
                if str(quantity.get("unit") or "") != unit:
                    issues.append(
                        RelationshipCompileIssue(
                            "numeric_inventory_allocation_unit_mismatch",
                            "compiler-generated allocations for one batch must share its total unit",
                            batch_id=batch_id,
                            consumer_id=consumer_id,
                        )
                    )
                    break
                allocated_total += float(quantity["value"])
                consumption_event_id = next(
                    (
                        event_id
                        for event_id, event in generated_consumption_events.items()
                        if event["batch_id"] == batch_id
                        and event["consumer_id"] == consumer_id
                    ),
                    "",
                )
                consumer_rows.append(
                    {
                        "consumer_id": consumer_id,
                        "allocation_id": consumption_event_id,
                        "planned_quantity": copy.deepcopy(quantity),
                    }
                )
            if issues:
                break
            total_value = float(total["value"])
            tolerance = max(1e-12, abs(total_value) * 1e-9)
            if allocated_total > total_value + tolerance:
                issues.append(
                    RelationshipCompileIssue(
                        "planned_numeric_consumption_exceeds_batch",
                        "planned consumer allocations exceed the Research planning quantity",
                        batch_id=batch_id,
                        total=total_value,
                        allocated=allocated_total,
                        unit=unit,
                    )
                )
                break
            if consumer_rows:
                entry["planned_consumer_allocations"] = consumer_rows
                entry["planned_allocated"] = {
                    "value": allocated_total,
                    "unit": unit,
                }
            entry["planned_balance"] = {
                "value": total_value - allocated_total,
                "unit": unit,
            }
            entry["quantity_evidence_kind"] = batch.get("quantity_evidence_kind")
            entry["availability_status"] = batch.get("availability_status")
            entry["calculation"] = (
                "Planned Research allocation ledger; planned_* values are not executed measurements or consumption facts."
            )
        elif quantity_mode == RUNTIME_QUANTITY_MODE:
            entry["measurement_status"] = "pending"
            entry["runtime_measurement_obligation_ids"] = list(
                batch.get("runtime_measurement_obligation_ids") or []
            )
            entry["measurement_event_ids"] = list(
                batch.get("measurement_event_ids") or []
            )
            entry["calculation"] = (
                "Pending runtime quantity; produced/consumed/reserved/balance are intentionally absent."
            )
        else:
            entry["calculation"] = (
                "Explicit whole-batch relationship binding; no numeric amount, yield, or executed consumption is asserted."
            )
        generated_ledger[entry["entry_id"]] = entry
    if issues:
        return original, issues, []

    _, preserved_batches = _managed_records(candidate.get("batch_plan") or [])
    _, preserved_transitions = _managed_records(
        candidate.get("material_transitions") or []
    )
    raw_runtime_obligations = candidate.get("material_runtime_measurement_obligations")
    if raw_runtime_obligations is None:
        raw_runtime_obligations = []
    raw_consumption_events = candidate.get("material_consumption_events")
    if raw_consumption_events is None:
        raw_consumption_events = []
    if not isinstance(raw_runtime_obligations, list) or not isinstance(
        raw_consumption_events, list
    ):
        return original, [
            RelationshipCompileIssue(
                "invalid_material_execution_obligation_collection",
                "runtime measurement obligations and consumption events must be arrays",
            )
        ], []
    _, preserved_runtime_obligations = _managed_records(raw_runtime_obligations)
    _, preserved_consumption_events = _managed_records(raw_consumption_events)
    ledger = candidate.get("material_ledger")
    if ledger is None:
        ledger = {"entries": []}
    if not isinstance(ledger, dict) or not isinstance(ledger.get("entries", []), list):
        return original, [
            RelationshipCompileIssue(
                "invalid_material_ledger",
                "material_ledger must be an object with an entries array",
            )
        ], []
    _, preserved_ledger = _managed_records(ledger.get("entries") or [])

    def reject_id_collisions(
        preserved: List[Dict[str, Any]], generated: Dict[str, Dict[str, Any]], field: str
    ) -> None:
        existing = {
            str(item.get(field) or "") for item in preserved if isinstance(item, dict)
        }
        collisions = sorted(existing & set(generated))
        if collisions:
            issues.append(
                RelationshipCompileIssue(
                    "relationship_record_id_conflict",
                    "compiler-owned record ID collides with an unmanaged record",
                    field=field,
                    ids=collisions,
                )
            )

    reject_id_collisions(preserved_batches, generated_batches, "batch_id")
    reject_id_collisions(
        preserved_transitions, generated_transitions, "transition_id"
    )
    reject_id_collisions(preserved_ledger, generated_ledger, "entry_id")
    reject_id_collisions(
        preserved_runtime_obligations,
        generated_runtime_obligations,
        "obligation_id",
    )
    reject_id_collisions(
        preserved_consumption_events,
        generated_consumption_events,
        "consumption_event_id",
    )
    if issues:
        return original, issues, []

    previous_owned_transition_ids = {
        str(item.get("transition_id") or "")
        for item in candidate.get("material_transitions") or []
        if isinstance(item, dict)
        and str(item.get("construction_rule") or "") == RULE_ID
    }
    updated = copy.deepcopy(candidate)
    updated["batch_plan"] = preserved_batches + list(generated_batches.values())
    updated["material_transitions"] = preserved_transitions + list(
        generated_transitions.values()
    )
    updated_ledger = copy.deepcopy(ledger)
    updated_ledger["entries"] = preserved_ledger + list(generated_ledger.values())
    updated_ledger["record_phase"] = "planned"
    updated["material_ledger"] = updated_ledger
    updated["material_runtime_measurement_obligations"] = (
        preserved_runtime_obligations
        + list(generated_runtime_obligations.values())
    )
    updated["material_consumption_events"] = (
        preserved_consumption_events + list(generated_consumption_events.values())
    )

    # R2 canonical relation/event graph: batch_plan, material_ledger and
    # material_transitions are three views over this single graph.  Nodes are
    # batches; edges are transitions carrying stable production/consumption
    # event ids so every view references the same event identities.
    relation_graph: Dict[str, Any] = {
        "schema": "material-relation-graph/v1",
        "nodes": [
            {
                "batch_id": batch_id,
                "material_id": batch.get("material_id") or "",
                "sample_id": batch.get("sample_id") or "",
                "quantity_mode": batch.get("quantity_mode") or "",
                "is_root_batch": bool(batch.get("is_root_batch")),
                "quantity_status": (
                    "pending_measurement"
                    if batch.get("quantity_mode") == RUNTIME_QUANTITY_MODE
                    else (
                        "known"
                        if batch.get("quantity_mode") == "numeric_inventory"
                        else "unknown"
                    )
                ),
                "production_event_id": (
                    ""
                    if batch.get("is_root_batch")
                    else _stable_id("MRPE", batch_id)
                ),
            }
            for batch_id, batch in generated_batches.items()
        ],
        "edges": [
            {
                "transition_id": transition.get("transition_id") or "",
                "transition_kind": transition.get("transition_kind") or "",
                "parent_batch_ids": list(transition.get("parent_batch_ids") or []),
                "child_batch_ids": list(transition.get("child_batch_ids") or []),
                "source_plan_steps": copy.deepcopy(
                    transition.get("source_plan_steps") or []
                ),
                "production_event_ids": [
                    _stable_id("MRPE", child_id)
                    for child_id in (transition.get("child_batch_ids") or [])
                ],
                "consumption_event_ids": [
                    event["consumption_event_id"]
                    for event in generated_consumption_events.values()
                    if event.get("transition_id") == transition.get("transition_id")
                ],
            }
            for transition in generated_transitions.values()
        ],
    }
    updated["material_relation_graph"] = relation_graph

    updated_plan_by_key = {
        macro_id_key(step.get("plan_step")): step
        for step in updated.get("device_plan") or []
        if isinstance(step, dict)
    }
    for previous_step_key in previous_compiler_step_keys:
        previous_step = updated_plan_by_key.get(previous_step_key)
        if not isinstance(previous_step, dict):
            continue
        for field_name in _COMPILER_MANAGED_STEP_FIELDS:
            previous_step.pop(field_name, None)
    for step_key, relation_ids in target_step_relation_ids.items():
        step = updated_plan_by_key[step_key]
        old_refs = step.get("material_transition_ids")
        if old_refs is None:
            old_refs = []
        if not isinstance(old_refs, list) or any(
            not isinstance(value, str) or not value.strip() for value in old_refs
        ):
            issues.append(
                RelationshipCompileIssue(
                    "invalid_material_transition_ids",
                    "material_transition_ids must be an array of nonempty strings",
                    plan_step=copy.deepcopy(step.get("plan_step")),
                )
            )
            continue
        unmanaged_refs = [
            value for value in old_refs if value not in previous_owned_transition_ids
        ]
        if unmanaged_refs:
            issues.append(
                RelationshipCompileIssue(
                    "device_step_has_unmanaged_material_transition",
                    "relationship compiler will not append to an independently managed material event",
                    plan_step=copy.deepcopy(step.get("plan_step")),
                    transition_ids=unmanaged_refs,
                )
            )
            continue
        step_transition_ids = [
            transition_by_relationship[relationship_id]
            for relationship_id in relation_ids
        ]
        event_kind = next(iter(target_step_kinds[step_key]))
        step["material_event_kind"] = event_kind
        step["material_transition_ids"] = step_transition_ids
        step["research_material_relation_ids"] = list(relation_ids)
        step["logical_container_bindings"] = copy.deepcopy(
            target_step_container_bindings.get(step_key, {})
        )
        step_obligation_ids = sorted(
            obligation_id
            for obligation_id, obligation in generated_runtime_obligations.items()
            if obligation.get("research_material_relationship_id") in relation_ids
        )
        if step_obligation_ids:
            step["runtime_measurement_obligation_ids"] = step_obligation_ids
        else:
            step.pop("runtime_measurement_obligation_ids", None)

    if issues:
        return original, issues, []

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "construction_rule": RULE_ID,
        "record_phase": "planned",
        "research_authority_sha256": _canonical_digest(package),
        "binding_authority_schema": binding_envelope.get("schema"),
        "binding_authority_schema_version": binding_envelope.get("schema_version"),
        "binding_authority_sha256": (
            _canonical_digest(binding_envelope) if binding_envelope else ""
        ),
        "relationship_bindings_sha256": _canonical_digest(bindings),
        "binding_authoring_mode": binding_envelope.get("authoring_mode"),
        "binding_automation_claim": binding_envelope.get("automation_claim"),
        "manual_support_schema": binding_envelope.get("manual_support_schema"),
        "manual_support_document_sha256": binding_envelope.get(
            "manual_support_document_sha256"
        ),
        "manual_support_record_sha256": copy.deepcopy(
            binding_envelope.get("manual_support_record_sha256") or {}
        ),
        "binding_candidate_digest_scope": binding_envelope.get(
            "candidate_digest_scope"
        ),
        "binding_candidate_sha256": binding_envelope.get("candidate_sha256"),
        "workstation_truth_sha256": binding_envelope.get(
            "workstation_truth_sha256"
        ),
        "operation_support_attribution": (
            "manual_evidence_bound"
            if binding_envelope.get("authoring_mode") == "manual_evidence_bound"
            else "machine_readable_skill_contract"
            if binding_envelope.get("authoring_mode")
            == "automated_evidence_bound"
            else None
        ),
        "managed_batch_ids": sorted(generated_batches),
        "managed_transition_ids": sorted(generated_transitions),
        "managed_ledger_entry_ids": sorted(generated_ledger),
        "managed_runtime_measurement_obligation_ids": sorted(
            generated_runtime_obligations
        ),
        "managed_consumption_event_ids": sorted(generated_consumption_events),
        "relationship_ids": sorted(relationship_ids),
        "collection_registrations": copy.deepcopy(collection_registrations),
        "relationship_transition_ids": {
            relationship_id: transition_by_relationship[relationship_id]
            for relationship_id in sorted(transition_by_relationship)
        },
        "consumption_event_ids": sorted(generated_consumption_events),
        "runtime_measurement_obligation_ids": sorted(
            generated_runtime_obligations
        ),
        "pending_runtime_measurement": bool(generated_runtime_obligations),
    }
    updated["material_relationship_compiler"] = manifest

    if updated == original:
        return updated, [], []
    applied = [
        "replace:material_relationship_graph",
        *(
            f"bind:relationship:{relationship_id}"
            for relationship_id in sorted(transition_by_relationship)
        ),
    ]
    return updated, [], applied


# Research -> device bridge (scientific semantics Phase 2): material-state
# vocabulary synonyms only.  The quantity mapping above is unchanged.
_FALLBACK_RESEARCH_STATE_SYNONYMS: Dict[str, str] = {
    "powder": "粉末",
    "suspension": "悬浊液",
    "solution": "纯液态",
    "retained_wet_solid": "固体",
    "washed_wet_solid": "固体",
    "dry_solid": "固体",
    "supernatant": "纯液态",
    "filtrate": "纯液态",
    "gas": "不限",
    "unknown": "unknown",
}
_RESEARCH_STATE_SYNONYMS_CACHE: Optional[Dict[str, str]] = None


def research_state_device_synonyms() -> Dict[str, str]:
    """Map Research material-state tokens to device sample-state vocabulary.

    Reads chem_resources/material_states/v1.json when present; falls back to
    the embedded table so the compiler never hard-fails on a missing resource.
    """

    global _RESEARCH_STATE_SYNONYMS_CACHE
    if _RESEARCH_STATE_SYNONYMS_CACHE is not None:
        return _RESEARCH_STATE_SYNONYMS_CACHE
    synonyms = dict(_FALLBACK_RESEARCH_STATE_SYNONYMS)
    try:
        resource_path = Path(__file__).resolve().parents[1] / "chem_resources" / "material_states" / "v1.json"
        payload = json.loads(resource_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if (
        isinstance(payload, dict)
        and str(payload.get("schema") or "").strip() == "material-states/v1"
        and isinstance(payload.get("device_sample_state_map"), dict)
    ):
        for key, value in payload["device_sample_state_map"].items():
            if isinstance(key, str) and isinstance(value, str) and key.strip():
                synonyms[key.strip()] = value.strip()
    _RESEARCH_STATE_SYNONYMS_CACHE = synonyms
    return synonyms


__all__ = [
    "ALLOWED_EVENT_KINDS",
    "BINDING_AUTHORITY_SCHEMA",
    "BINDING_SCOPE_VERSION",
    "CANDIDATE_BINDING_DIGEST_SCOPE",
    "DEVICE_STEP_DIGEST_SCOPE",
    "MANUAL_SUPPORT_CONCLUSION",
    "MANUAL_SUPPORT_SCHEMA",
    "MANUAL_SUPPORT_SCHEMA_VERSION",
    "RULE_ID",
    "RUNTIME_MEASUREMENT_SCHEMA",
    "RUNTIME_MEASUREMENT_SCHEMA_VERSION",
    "RUNTIME_QUANTITY_MODE",
    "SKILL_OPERATION_CONTRACT_DIGEST_SCOPE",
    "SUPPORTED_QUANTITY_BASES",
    "RelationshipCompileIssue",
    "candidate_binding_sha256",
    "compile_material_relationships",
    "device_step_binding_sha256",
    "normalized_device_step_for_binding",
    "relationship_binding_records",
    "seal_relationship_binding_authority",
]
