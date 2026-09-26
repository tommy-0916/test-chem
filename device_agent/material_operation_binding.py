"""Deterministic Research-material to Device-operation binding.

This resolver is intentionally evidence-only.  It never matches on a macro
ordinal, a material name, free-form operation prose, or a workstation category.
Research declares an ordered sequence of abstract machine-readable capability
roles without choosing physical workstations.  Device references the exact
Research operation segment and role ID, repeats its immutable capability ID,
and selects an exact Skill operation.  The resolver first verifies the
segment/role/capability tuple and then verifies the selected operation against
workstation truth.  A relation is emitted only when every role has one
unambiguous Device step and one role is explicitly designated as the
material-state commit point.

The returned draft records are consumed by
``material_relationship_compiler.seal_relationship_binding_authority``.  They
retain ``plan_step`` for the v1 compiler boundary while also carrying the v2
``implementation_steps``/``commit_step`` scope.  ``plan_step`` is always equal
to ``commit_step``; non-commit steps are never allowed to publish a duplicate
material transition.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from chem_agent_contracts.identity import json_scalar_identity_key

try:
    from .macro_identity import (
        MacroIdentityError,
        extract_source_macro_ids,
        macro_id_key,
        semantic_macro_id,
    )
except ImportError:  # Direct-script compatibility.
    from macro_identity import (  # type: ignore
        MacroIdentityError,
        extract_source_macro_ids,
        macro_id_key,
        semantic_macro_id,
    )


RULE_ID = "material-operation-binding-resolver/v2"
BINDING_SCOPE_VERSION = 3
SKILL_OPERATION_CONTRACT_DIGEST_SCOPE = (
    "workstation-capability-index/station+capability+operation/"
    "canonical_json_utf8_sorted_compact/v2"
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


class MaterialOperationBindingIssue(dict):
    """One fail-closed deterministic binding diagnostic."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        blocker_class: str,
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


def _package_from_authority(authority: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(authority, dict):
        return None
    nested = authority.get("research_action_package_v2")
    if isinstance(nested, dict):
        return nested
    if isinstance(authority.get("macro_steps"), list):
        return authority
    return None


def _normalized_step_for_digest(step: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in step.items()
        if key not in _COMPILER_MANAGED_STEP_FIELDS
    }


def _device_step_digest(step: Dict[str, Any]) -> str:
    return _canonical_digest(_normalized_step_for_digest(step))


def _json_scalar_key(value: Any, path: str) -> Tuple[str, Any]:
    """Validate a physical-container scalar without lossy string coercion."""

    try:
        return json_scalar_identity_key(value, path)
    except Exception as exc:
        raise ValueError(f"{path} must be a non-boolean JSON scalar") from exc


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
    detached = (
        copy.deepcopy(values)
        if isinstance(values, list)
        else ([] if values is None else [copy.deepcopy(values)])
    )
    lineage = step.get("sample_lineage")
    if isinstance(lineage, dict):
        for endpoint in ("source_container", "destination_container"):
            record = lineage.get(endpoint)
            if isinstance(record, dict):
                detached.append(copy.deepcopy(record.get("container_id")))

    result: List[Any] = []
    seen: set[Tuple[str, Any]] = set()
    for index, value in enumerate(detached):
        try:
            key = _json_scalar_key(value, f"containers[{index}]")
        except ValueError:
            continue
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _research_relation_index(
    research_authority: Any,
) -> Tuple[List[Dict[str, Any]], List[MaterialOperationBindingIssue]]:
    package = _package_from_authority(research_authority)
    if package is None:
        return [], [
            MaterialOperationBindingIssue(
                "research_v2_authority_missing",
                "automated operation binding requires an accepted Research V2 package",
                blocker_class="contract_invalid",
            )
        ]
    raw_macros = package.get("macro_steps")
    if not isinstance(raw_macros, list):
        return [], [
            MaterialOperationBindingIssue(
                "research_macro_steps_invalid",
                "Research V2 macro_steps must be an array",
                blocker_class="contract_invalid",
            )
        ]

    records: List[Dict[str, Any]] = []
    issues: List[MaterialOperationBindingIssue] = []
    relation_ids: set[str] = set()
    global_segment_paths: Dict[str, str] = {}
    for macro_index, macro in enumerate(raw_macros):
        path = f"macro_steps[{macro_index}]"
        if not isinstance(macro, dict):
            issues.append(
                MaterialOperationBindingIssue(
                    "research_macro_step_invalid",
                    "Research macro step must be an object",
                    blocker_class="contract_invalid",
                    path=path,
                )
            )
            continue
        try:
            macro_id = semantic_macro_id(macro, path, required=True)
            macro_key = macro_id_key(macro_id, f"{path}.macro_step_id")
        except MacroIdentityError as exc:
            issues.append(
                MaterialOperationBindingIssue(
                    exc.code.lower(),
                    str(exc),
                    blocker_class="contract_invalid",
                    path=exc.path,
                )
            )
            continue

        raw_segments = macro.get("operation_segments")
        if not isinstance(raw_segments, list):
            issues.append(
                MaterialOperationBindingIssue(
                    "research_operation_segments_invalid",
                    "operation_segments must be an array",
                    blocker_class="contract_invalid",
                    path=f"{path}.operation_segments",
                )
            )
            continue
        segments: Dict[str, Dict[str, Any]] = {}
        for segment_index, segment in enumerate(raw_segments):
            segment_path = f"{path}.operation_segments[{segment_index}]"
            if not isinstance(segment, dict):
                issues.append(
                    MaterialOperationBindingIssue(
                        "research_operation_segment_invalid",
                        "operation segment must be an object",
                        blocker_class="contract_invalid",
                        path=segment_path,
                    )
                )
                continue
            segment_id = str(segment.get("segment_id") or "").strip()
            if not segment_id or segment_id in segments:
                issues.append(
                    MaterialOperationBindingIssue(
                        "research_operation_segment_id_invalid",
                        "operation segment IDs must be unique and nonempty",
                        blocker_class="contract_invalid",
                        path=f"{segment_path}.segment_id",
                    )
                )
                continue
            prior_segment_path = global_segment_paths.get(segment_id)
            if prior_segment_path is not None:
                issues.append(
                    MaterialOperationBindingIssue(
                        "research_operation_segment_id_not_globally_unique",
                        "operation segment IDs must be globally unique within the Research package",
                        blocker_class="contract_invalid",
                        path=f"{segment_path}.segment_id",
                        segment_id=segment_id,
                        first_path=prior_segment_path,
                    )
                )
            else:
                global_segment_paths[segment_id] = f"{segment_path}.segment_id"
            segments[segment_id] = segment

        raw_relations = macro.get("material_relations")
        if not isinstance(raw_relations, list):
            issues.append(
                MaterialOperationBindingIssue(
                    "research_material_relations_invalid",
                    "material_relations must be an array",
                    blocker_class="contract_invalid",
                    path=f"{path}.material_relations",
                )
            )
            continue
        for relation_index, relation in enumerate(raw_relations):
            relation_path = f"{path}.material_relations[{relation_index}]"
            if not isinstance(relation, dict):
                issues.append(
                    MaterialOperationBindingIssue(
                        "research_material_relation_invalid",
                        "material relation must be an object",
                        blocker_class="contract_invalid",
                        path=relation_path,
                    )
                )
                continue
            relationship_id = str(relation.get("relation_id") or "").strip()
            if not relationship_id or relationship_id in relation_ids:
                issues.append(
                    MaterialOperationBindingIssue(
                        "research_material_relation_id_invalid",
                        "material relation IDs must be globally unique and nonempty",
                        blocker_class="contract_invalid",
                        path=f"{relation_path}.relation_id",
                    )
                )
                continue
            relation_ids.add(relationship_id)
            if str(relation.get("event_kind") or "").strip() == "none":
                continue
            segment_id = str(relation.get("source_operation_ref") or "").strip()
            segment = segments.get(segment_id)
            if segment is None:
                issues.append(
                    MaterialOperationBindingIssue(
                        "research_relation_operation_segment_missing",
                        "material relation must reference an operation segment in the same macro",
                        blocker_class="contract_invalid",
                        relationship_id=relationship_id,
                        source_operation_ref=segment_id,
                    )
                )
                continue
            records.append(
                {
                    "relationship_id": relationship_id,
                    "macro_id": copy.deepcopy(macro_id),
                    "macro_key": macro_key,
                    "relation": relation,
                    "operation_segment": segment,
                }
            )
    return records, issues


def _plan_index(
    candidate: Any,
) -> Tuple[List[Dict[str, Any]], List[MaterialOperationBindingIssue]]:
    raw_plan = candidate.get("device_plan") if isinstance(candidate, dict) else None
    if not isinstance(raw_plan, list):
        return [], [
            MaterialOperationBindingIssue(
                "device_plan_invalid",
                "candidate.device_plan must be an array",
                blocker_class="contract_invalid",
            )
        ]
    records: List[Dict[str, Any]] = []
    issues: List[MaterialOperationBindingIssue] = []
    seen_steps: set[Tuple[str, Any]] = set()
    for index, step in enumerate(raw_plan):
        path = f"device_plan[{index}]"
        if not isinstance(step, dict):
            issues.append(
                MaterialOperationBindingIssue(
                    "device_plan_step_invalid",
                    "Device plan step must be an object",
                    blocker_class="contract_invalid",
                    path=path,
                )
            )
            continue
        try:
            step_key = macro_id_key(step.get("plan_step"), f"{path}.plan_step")
            source_ids = extract_source_macro_ids(step, path, required=True)
            source_keys = {
                macro_id_key(value, f"{path}.source_macro_steps")
                for value in source_ids
            }
        except MacroIdentityError as exc:
            issues.append(
                MaterialOperationBindingIssue(
                    exc.code.lower(),
                    str(exc),
                    blocker_class="contract_invalid",
                    path=exc.path,
                )
            )
            continue
        if step_key in seen_steps:
            issues.append(
                MaterialOperationBindingIssue(
                    "duplicate_plan_step_id",
                    "automated operation binding requires unique typed plan_step IDs",
                    blocker_class="contract_invalid",
                    path=f"{path}.plan_step",
                )
            )
            continue
        seen_steps.add(step_key)

        raw_claims = step.get("operation_capabilities")
        claims: List[Tuple[str, str, str, str]] = []
        if raw_claims is not None:
            if not isinstance(raw_claims, list):
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_operation_capabilities_invalid",
                        "operation_capabilities must be an array of exact capability selectors",
                        blocker_class="plan_binding_invalid",
                        plan_step=copy.deepcopy(step.get("plan_step")),
                    )
                )
            else:
                for claim_index, claim in enumerate(raw_claims):
                    if not isinstance(claim, dict):
                        issues.append(
                            MaterialOperationBindingIssue(
                                "device_operation_capability_invalid",
                                "operation capability claim must be an object",
                                blocker_class="plan_binding_invalid",
                                plan_step=copy.deepcopy(step.get("plan_step")),
                                claim_index=claim_index,
                            )
                        )
                        continue
                    source_operation_ref = str(
                        claim.get("source_operation_ref") or ""
                    ).strip()
                    role_id = str(claim.get("role_id") or "").strip()
                    capability_id = str(claim.get("capability_id") or "").strip()
                    operation_name = str(
                        claim.get("skill_operation_name") or ""
                    ).strip()
                    if (
                        not source_operation_ref
                        or not role_id
                        or not capability_id
                        or not operation_name
                    ):
                        issues.append(
                            MaterialOperationBindingIssue(
                                "device_operation_capability_incomplete",
                                "operation capability claim requires source_operation_ref, role_id, capability_id, and skill_operation_name",
                                blocker_class="plan_binding_invalid",
                                plan_step=copy.deepcopy(step.get("plan_step")),
                                claim_index=claim_index,
                            )
                        )
                        continue
                    claims.append(
                        (
                            source_operation_ref,
                            role_id,
                            capability_id,
                            operation_name,
                        )
                    )
                if len(claims) != len(set(claims)):
                    issues.append(
                        MaterialOperationBindingIssue(
                            "device_operation_capability_duplicate",
                            "operation_capabilities must not contain duplicate selectors",
                            blocker_class="plan_binding_invalid",
                            plan_step=copy.deepcopy(step.get("plan_step")),
                        )
                    )
                role_selectors = [
                    (source_operation_ref, role_id)
                    for source_operation_ref, role_id, _, _ in claims
                ]
                if len(role_selectors) != len(set(role_selectors)):
                    issues.append(
                        MaterialOperationBindingIssue(
                            "device_operation_role_claim_duplicate",
                            "one Device step may claim a Research operation-segment role only once",
                            blocker_class="plan_binding_invalid",
                            plan_step=copy.deepcopy(step.get("plan_step")),
                        )
                    )
        records.append(
            {
                "index": index,
                "step": step,
                "step_key": step_key,
                "plan_step": copy.deepcopy(step.get("plan_step")),
                "source_keys": source_keys,
                "claims": set(claims),
            }
        )
    return records, issues


def _truth_station_index(
    workstation_capability_index: Any,
) -> Tuple[Dict[str, Dict[str, Any]], List[MaterialOperationBindingIssue]]:
    if not isinstance(workstation_capability_index, dict) or not isinstance(
        workstation_capability_index.get("workstations"), list
    ):
        return {}, [
            MaterialOperationBindingIssue(
                "workstation_capability_index_invalid",
                "resolver requires the complete workstation capability index object",
                blocker_class="contract_invalid",
            )
        ]
    stations: Dict[str, Dict[str, Any]] = {}
    issues: List[MaterialOperationBindingIssue] = []
    for index, station in enumerate(workstation_capability_index["workstations"]):
        if not isinstance(station, dict):
            issues.append(
                MaterialOperationBindingIssue(
                    "workstation_truth_station_invalid",
                    "workstation record must be an object",
                    blocker_class="contract_invalid",
                    station_index=index,
                )
            )
            continue
        station_code = str(station.get("station_code") or "").strip()
        if not station_code or station_code in stations:
            issues.append(
                MaterialOperationBindingIssue(
                    "workstation_truth_station_code_invalid",
                    "workstation station_code values must be unique and nonempty",
                    blocker_class="contract_invalid",
                    station_index=index,
                )
            )
            continue
        stations[station_code] = station
    return stations, issues


def _explicit_capability_operations(
    station: Dict[str, Any],
    capability: Dict[str, Any],
) -> Optional[set[str]]:
    """Read only explicit truth mappings; never infer from labels or prose."""

    for key in ("skill_operation_names", "operation_names", "operations"):
        raw = capability.get(key)
        if isinstance(raw, list) and all(
            isinstance(value, str) and value.strip() for value in raw
        ):
            return {value.strip() for value in raw}

    station_bindings = station.get("capability_operation_bindings")
    capability_id = str(capability.get("id") or "").strip()
    if isinstance(station_bindings, dict):
        raw = station_bindings.get(capability_id)
        if isinstance(raw, list) and all(
            isinstance(value, str) and value.strip() for value in raw
        ):
            return {value.strip() for value in raw}

    operations = station.get("operations")
    if isinstance(operations, list):
        explicitly_linked = {
            str(operation.get("name") or "").strip()
            for operation in operations
            if isinstance(operation, dict)
            and capability_id
            in {
                str(value).strip()
                for value in (operation.get("capability_ids") or [])
                if isinstance(value, str)
            }
        }
        explicitly_linked.discard("")
        if explicitly_linked:
            return explicitly_linked
    return None


def _runtime_quantity_support(operation: Dict[str, Any]) -> Dict[str, Any]:
    """Project only explicit runtime-quantity guarantees from one Skill.

    Target/setpoint inputs are deliberately ignored.  A Skill is measurement
    capable only when its operation contract declares numeric inventory output
    fields.  Safe downstream consumption additionally needs an executable
    stop-before-consumption contract, not a prose note or a Device-plan claim.
    """

    quantity_semantics = operation.get("quantity_semantics")
    if not isinstance(quantity_semantics, dict):
        quantity_semantics = {}
    reported_measurements = quantity_semantics.get("reported_measurements")
    if not isinstance(reported_measurements, list) or any(
        not isinstance(value, str) or not value.strip()
        for value in reported_measurements
    ):
        reported_measurements = []
    reported_measurements = [value.strip() for value in reported_measurements]
    raw_measurement_contracts = quantity_semantics.get(
        "reported_measurement_contracts"
    )
    measurement_contracts: List[Dict[str, Any]] = []
    if isinstance(raw_measurement_contracts, list):
        for raw_contract in raw_measurement_contracts:
            if not isinstance(raw_contract, dict):
                continue
            field_name = str(raw_contract.get("field_name") or "").strip()
            dimension = str(raw_contract.get("dimension") or "").strip()
            allowed_units = raw_contract.get("allowed_units")
            if (
                field_name
                and field_name in reported_measurements
                and dimension in {"amount", "mass", "volume"}
                and isinstance(allowed_units, list)
                and bool(allowed_units)
                and all(
                    isinstance(value, str) and value.strip()
                    for value in allowed_units
                )
            ):
                measurement_contracts.append(
                    {
                        "field_name": field_name,
                        "dimension": dimension,
                        "allowed_units": [value.strip() for value in allowed_units],
                    }
                )
    stop_contract = quantity_semantics.get("insufficient_quantity_stop")
    stop_contract = stop_contract if isinstance(stop_contract, dict) else {}
    measurement_declared = bool(
        quantity_semantics.get("actual_inventory_measurement_declared") is True
        and quantity_semantics.get("can_report_actual_inventory") is True
        and reported_measurements
        and measurement_contracts
    )
    stop_declared = bool(
        stop_contract.get("declared") is True
        and stop_contract.get("policy") == "stop_before_material_consumption"
    )
    return {
        "actual_inventory_measurement_declared": measurement_declared,
        "reported_measurements": reported_measurements,
        "reported_measurement_contracts": measurement_contracts,
        "insufficient_quantity_stop_declared": stop_declared,
        "insufficient_quantity_stop_policy": (
            "stop_before_material_consumption" if stop_declared else ""
        ),
    }


def _verified_truth_contract(
    *,
    index: Dict[str, Any],
    stations: Dict[str, Dict[str, Any]],
    station_code: str,
    capability_id: str,
    operation_name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[MaterialOperationBindingIssue]]:
    station = stations.get(station_code)
    if station is None:
        return None, MaterialOperationBindingIssue(
            "device_workstation_not_in_truth",
            "Device workstation does not resolve to a canonical truth record",
            blocker_class="plan_binding_invalid",
            station_code=station_code,
        )
    capability_matches = [
        item
        for item in (station.get("experiment_capabilities") or [])
        if isinstance(item, dict)
        and str(item.get("id") or "").strip() == capability_id
    ]
    if len(capability_matches) != 1:
        return None, MaterialOperationBindingIssue(
            "skill_capability_not_uniquely_supported",
            "workstation truth must contain one exact capability record",
            blocker_class="plan_binding_invalid",
            station_code=station_code,
            capability_id=capability_id,
            match_count=len(capability_matches),
        )
    capability = capability_matches[0]
    if str(capability.get("support_status") or "").strip() != "supported":
        return None, MaterialOperationBindingIssue(
            "skill_capability_not_supported",
            "workstation truth does not mark the capability as supported",
            blocker_class="plan_binding_invalid",
            station_code=station_code,
            capability_id=capability_id,
            support_status=copy.deepcopy(capability.get("support_status")),
        )

    operation_matches = [
        item
        for item in (station.get("operations") or [])
        if isinstance(item, dict)
        and str(item.get("name") or "").strip() == operation_name
    ]
    if len(operation_matches) != 1:
        return None, MaterialOperationBindingIssue(
            "skill_operation_not_unique",
            "workstation truth must contain one exact Skill operation",
            blocker_class="plan_binding_invalid",
            station_code=station_code,
            operation_name=operation_name,
            match_count=len(operation_matches),
        )
    allowed_operations = _explicit_capability_operations(station, capability)
    if allowed_operations is None:
        return None, MaterialOperationBindingIssue(
            "skill_capability_operation_mapping_missing",
            "workstation truth does not explicitly map this capability to a Skill operation",
            blocker_class="software_unsupported",
            station_code=station_code,
            capability_id=capability_id,
            operation_name=operation_name,
        )
    if operation_name not in allowed_operations:
        return None, MaterialOperationBindingIssue(
            "skill_capability_operation_mismatch",
            "the requested Skill operation is not authorized for this capability",
            blocker_class="plan_binding_invalid",
            station_code=station_code,
            capability_id=capability_id,
            operation_name=operation_name,
            allowed_operations=sorted(allowed_operations),
        )

    operation = operation_matches[0]
    station_bindings = station.get("capability_operation_bindings")
    reviewed_capability_operation_mapping = (
        copy.deepcopy(station_bindings.get(capability_id))
        if isinstance(station_bindings, dict)
        else None
    )
    projection = {
        "digest_scope": SKILL_OPERATION_CONTRACT_DIGEST_SCOPE,
        "index_schema_version": copy.deepcopy(index.get("schema_version")),
        "index_projection_version": copy.deepcopy(index.get("projection_version")),
        "index_semantic_mapping_digest": copy.deepcopy(
            index.get("semantic_mapping_digest")
        ),
        "index_source_digest_sha256": copy.deepcopy(
            index.get("source_digest_sha256")
        ),
        "station_code": station_code,
        "capability": copy.deepcopy(capability),
        "reviewed_capability_operation_mapping": (
            reviewed_capability_operation_mapping
        ),
        "operation": copy.deepcopy(operation),
    }
    capability_evidence = capability.get("evidence")
    support_ref = ""
    if isinstance(capability_evidence, dict):
        evidence_path = str(capability_evidence.get("path") or "").strip()
        evidence_line = capability_evidence.get("line")
        if evidence_path:
            support_ref = (
                f"{evidence_path}#L{evidence_line}"
                if isinstance(evidence_line, int) and not isinstance(evidence_line, bool)
                else evidence_path
            )
    return {
        "station_code": station_code,
        "capability_id": capability_id,
        "skill_operation_name": operation_name,
        "skill_contract_digest_scope": SKILL_OPERATION_CONTRACT_DIGEST_SCOPE,
        "skill_contract_sha256": _canonical_digest(projection),
        "support_evidence_ref": support_ref,
        "runtime_quantity_support": _runtime_quantity_support(operation),
    }, None


def _implementation_contract(
    relationship: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[MaterialOperationBindingIssue]]:
    relationship_id = relationship["relationship_id"]
    segment = relationship["operation_segment"]
    implementation = segment.get("device_implementation")
    if not isinstance(implementation, dict):
        return None, MaterialOperationBindingIssue(
            "research_device_implementation_missing",
            "automated binding requires an evidence-bound Device implementation contract",
            blocker_class="research_contract_missing",
            relationship_id=relationship_id,
            source_operation_ref=copy.deepcopy(
                relationship["relation"].get("source_operation_ref")
            ),
        )
    raw_roles = implementation.get("ordered_steps")
    commit_role_id = str(implementation.get("commit_role_id") or "").strip()
    if not isinstance(raw_roles, list) or not raw_roles or not commit_role_id:
        return None, MaterialOperationBindingIssue(
            "research_device_implementation_invalid",
            "Device implementation requires ordered_steps and commit_role_id",
            blocker_class="contract_invalid",
            relationship_id=relationship_id,
        )
    roles: List[Dict[str, str]] = []
    for role_index, role in enumerate(raw_roles):
        if not isinstance(role, dict):
            return None, MaterialOperationBindingIssue(
                "research_device_implementation_role_invalid",
                "Device implementation role must be an object",
                blocker_class="contract_invalid",
                relationship_id=relationship_id,
                role_index=role_index,
            )
        normalized = {
            "role_id": str(role.get("role_id") or "").strip(),
            "capability_id": str(role.get("capability_id") or "").strip(),
        }
        if any(not value for value in normalized.values()):
            return None, MaterialOperationBindingIssue(
                "research_device_implementation_role_incomplete",
                "each Device implementation role requires exact role and capability IDs",
                blocker_class="contract_invalid",
                relationship_id=relationship_id,
                role_index=role_index,
            )
        roles.append(normalized)
    role_ids = [role["role_id"] for role in roles]
    if len(role_ids) != len(set(role_ids)) or commit_role_id not in set(role_ids):
        return None, MaterialOperationBindingIssue(
            "research_device_implementation_commit_invalid",
            "implementation role IDs must be unique and commit_role_id must select one role",
            blocker_class="contract_invalid",
            relationship_id=relationship_id,
        )
    return {"ordered_steps": roles, "commit_role_id": commit_role_id}, None


def _logical_container_bindings(
    *,
    relationship_id: str,
    relation: Dict[str, Any],
    selected_steps: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[MaterialOperationBindingIssue]]:
    required = relation.get("logical_container_ids")
    if not isinstance(required, list) or any(
        not isinstance(value, str) or not value.strip() for value in required
    ):
        return None, [
            MaterialOperationBindingIssue(
                "research_logical_container_ids_invalid",
                "relationship logical_container_ids must be an array of nonempty strings",
                blocker_class="contract_invalid",
                relationship_id=relationship_id,
            )
        ]
    required_ids = list(dict.fromkeys(value.strip() for value in required))
    if len(required_ids) != len(required):
        return None, [
            MaterialOperationBindingIssue(
                "research_logical_container_ids_duplicate",
                "relationship logical_container_ids must not contain duplicates",
                blocker_class="contract_invalid",
                relationship_id=relationship_id,
            )
        ]
    assignments: Dict[str, List[Any]] = {}
    issues: List[MaterialOperationBindingIssue] = []
    for record in selected_steps:
        step = record["step"]
        step_physical = {
            _json_scalar_key(value, "physical_container_id"): value
            for value in _physical_container_ids(step)
        }
        raw_assignments = step.get("logical_container_assignments")
        if raw_assignments is None:
            continue
        if not isinstance(raw_assignments, list):
            issues.append(
                MaterialOperationBindingIssue(
                    "device_logical_container_assignments_invalid",
                    "logical_container_assignments must be an array",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    plan_step=copy.deepcopy(record["plan_step"]),
                )
            )
            continue
        for assignment_index, assignment in enumerate(raw_assignments):
            if not isinstance(assignment, dict):
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_logical_container_assignment_invalid",
                        "logical container assignment must be an object",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(record["plan_step"]),
                        assignment_index=assignment_index,
                    )
                )
                continue
            logical_id = str(
                assignment.get("logical_container_id") or ""
            ).strip()
            physical_ids = assignment.get("physical_container_ids")
            if not logical_id or not isinstance(physical_ids, list) or not physical_ids:
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_logical_container_assignment_incomplete",
                        "logical container assignment requires an ID and nonempty physical_container_ids",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(record["plan_step"]),
                        assignment_index=assignment_index,
                    )
                )
                continue
            try:
                keys = [
                    _json_scalar_key(
                        value,
                        "logical_container_assignments.physical_container_ids",
                    )
                    for value in physical_ids
                ]
            except ValueError as exc:
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_logical_container_assignment_physical_id_invalid",
                        str(exc),
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        plan_step=copy.deepcopy(record["plan_step"]),
                    )
                )
                continue
            if len(keys) != len(set(keys)):
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_logical_container_assignment_duplicate",
                        "one logical-container assignment cannot repeat a physical ID",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        logical_container_id=logical_id,
                    )
                )
                continue
            unknown = [
                value
                for value, key in zip(physical_ids, keys)
                if key not in step_physical
            ]
            if unknown:
                issues.append(
                    MaterialOperationBindingIssue(
                        "relationship_logical_container_binding_out_of_scope",
                        "assigned physical containers must occur in the exact Device step declaring the assignment",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        source_operation_ref=copy.deepcopy(
                            relation.get("source_operation_ref")
                        ),
                        plan_step=copy.deepcopy(record["plan_step"]),
                        logical_container_id=logical_id,
                        out_of_scope=copy.deepcopy(unknown),
                    )
                )
                continue
            normalized = copy.deepcopy(physical_ids)
            existing = assignments.get(logical_id)
            if existing is not None and [
                _json_scalar_key(value, "existing_assignment") for value in existing
            ] != keys:
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_logical_container_assignment_conflict",
                        "implementation steps disagree on a logical-container assignment",
                        blocker_class="plan_binding_ambiguous",
                        relationship_id=relationship_id,
                        logical_container_id=logical_id,
                    )
                )
                continue
            assignments[logical_id] = normalized

    if issues:
        return None, issues
    missing = sorted(set(required_ids) - set(assignments))
    if missing:
        return None, [
            MaterialOperationBindingIssue(
                "relationship_logical_container_binding_missing",
                "every Research logical container requires an explicit Device assignment",
                blocker_class="plan_binding_missing",
                relationship_id=relationship_id,
                source_operation_ref=copy.deepcopy(
                    relation.get("source_operation_ref")
                ),
                implementation_steps=[
                    copy.deepcopy(record["plan_step"])
                    for record in selected_steps
                ],
                logical_container_ids=missing,
                repair_authority="research_logical_container_assignment",
            )
        ]
    return {
        logical_id: (
            assignments[logical_id][0]
            if len(assignments[logical_id]) == 1
            else copy.deepcopy(assignments[logical_id])
        )
        for logical_id in required_ids
    }, []


def resolve_material_operation_bindings(
    candidate: Dict[str, Any],
    research_authority: Dict[str, Any],
    workstation_capability_index: Dict[str, Any],
    *,
    resolve_workstation: Optional[Callable[[str], str]] = None,
) -> Tuple[Dict[str, Any], List[MaterialOperationBindingIssue]]:
    """Resolve exact operation scopes into compiler-ready draft bindings.

    ``workstation_capability_index`` must be the complete JSON object, not a
    station-only map, because the sealed Skill contract digest also binds the
    index schema, projection, and source digest.
    """

    relations, relation_issues = _research_relation_index(research_authority)
    plan, plan_issues = _plan_index(candidate)
    stations, station_issues = _truth_station_index(workstation_capability_index)
    issues = [*relation_issues, *plan_issues, *station_issues]
    if issues:
        return {}, issues

    drafts: Dict[str, Any] = {}
    for relationship in relations:
        relationship_id = relationship["relationship_id"]
        source_operation_ref = str(
            relationship["relation"].get("source_operation_ref") or ""
        ).strip()
        implementation, implementation_issue = _implementation_contract(
            relationship
        )
        if implementation_issue is not None:
            issues.append(implementation_issue)
            continue
        assert implementation is not None

        macro_steps = [
            record
            for record in plan
            if relationship["macro_key"] in record["source_keys"]
        ]
        selected: List[Dict[str, Any]] = []
        selected_evidence: List[Dict[str, Any]] = []
        relation_blocked = False
        for role in implementation["ordered_steps"]:
            role_claiming = [
                record
                for record in macro_steps
                if any(
                    claimed_source_ref == source_operation_ref
                    and claimed_role_id == role["role_id"]
                    for claimed_source_ref, claimed_role_id, _, _ in record["claims"]
                )
            ]
            claiming = [
                record
                for record in macro_steps
                if any(
                    claimed_source_ref == source_operation_ref
                    and claimed_role_id == role["role_id"]
                    and capability_id == role["capability_id"]
                    for claimed_source_ref, claimed_role_id, capability_id, _ in record[
                        "claims"
                    ]
                )
            ]
            if role_claiming and not claiming:
                actual_capability_ids = sorted(
                    {
                        capability_id
                        for record in role_claiming
                        for claimed_source_ref, claimed_role_id, capability_id, _ in record[
                            "claims"
                        ]
                        if claimed_source_ref == source_operation_ref
                        and claimed_role_id == role["role_id"]
                    }
                )
                issues.append(
                    MaterialOperationBindingIssue(
                        "device_role_capability_mismatch",
                        "Device role claim cannot add or change the Research-required capability",
                        blocker_class="plan_binding_invalid",
                        relationship_id=relationship_id,
                        source_operation_ref=copy.deepcopy(
                            source_operation_ref
                        ),
                        role_id=role["role_id"],
                        expected_capability_id=role["capability_id"],
                        actual_capability_ids=actual_capability_ids,
                        plan_steps=[
                            copy.deepcopy(record["plan_step"])
                            for record in role_claiming
                        ],
                    )
                )
                relation_blocked = True
                continue
            verified: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            truth_failures: List[MaterialOperationBindingIssue] = []
            for record in claiming:
                step = record["step"]
                raw_workstation = str(
                    step.get("station_code") or step.get("workstation") or ""
                ).strip()
                resolved = (
                    str(resolve_workstation(raw_workstation) or "").strip()
                    if resolve_workstation is not None
                    else raw_workstation
                )
                matching_claims = sorted(
                    {
                        operation_name
                        for (
                            claimed_source_ref,
                            claimed_role_id,
                            capability_id,
                            operation_name,
                        ) in record["claims"]
                        if claimed_source_ref == source_operation_ref
                        and claimed_role_id == role["role_id"]
                        and capability_id == role["capability_id"]
                    }
                )
                if len(matching_claims) != 1:
                    truth_failures.append(
                        MaterialOperationBindingIssue(
                            "device_capability_operation_claim_ambiguous",
                            "one Device step must claim exactly one Skill operation for a required capability",
                            blocker_class="plan_binding_ambiguous",
                            relationship_id=relationship_id,
                            source_operation_ref=source_operation_ref,
                            role_id=role["role_id"],
                            plan_step=copy.deepcopy(record["plan_step"]),
                            capability_id=role["capability_id"],
                            skill_operation_names=matching_claims,
                        )
                    )
                    continue
                contract, truth_issue = _verified_truth_contract(
                    index=workstation_capability_index,
                    stations=stations,
                    station_code=resolved,
                    capability_id=role["capability_id"],
                    operation_name=matching_claims[0],
                )
                if truth_issue is not None:
                    truth_issue["context"].update(
                        relationship_id=relationship_id,
                        source_operation_ref=source_operation_ref,
                        role_id=role["role_id"],
                        plan_step=copy.deepcopy(record["plan_step"]),
                    )
                    truth_failures.append(truth_issue)
                elif contract is not None:
                    verified.append((record, contract))
            if truth_failures:
                issues.extend(truth_failures)
                relation_blocked = True
                continue
            if len(verified) != 1:
                issues.append(
                    MaterialOperationBindingIssue(
                        "relationship_operation_role_unresolved",
                        (
                            "required Device capability role has no structured candidate"
                            if not claiming
                            else "required Device capability role is ambiguous"
                        ),
                        blocker_class=(
                            "plan_missing_operation"
                            if not claiming
                            else "plan_binding_ambiguous"
                        ),
                        relationship_id=relationship_id,
                        source_operation_ref=copy.deepcopy(
                            source_operation_ref
                        ),
                        role_id=role["role_id"],
                        capability_id=role["capability_id"],
                        # Research deliberately leaves physical operation
                        # selection to Device.  Empty means "no exact
                        # Device claim was present", not a wildcard match.
                        skill_operation_name="",
                        repair_authority=(
                            "research_device_implementation"
                            if not claiming
                            else None
                        ),
                        candidate_count=len(claiming),
                        verified_count=len(verified),
                    )
                )
                relation_blocked = True
                continue
            selected.append(verified[0][0])
            selected_evidence.append(
                {
                    "source_operation_ref": source_operation_ref,
                    "role_id": role["role_id"],
                    "plan_step": copy.deepcopy(verified[0][0]["plan_step"]),
                    "device_step_sha256": _device_step_digest(
                        verified[0][0]["step"]
                    ),
                    **verified[0][1],
                }
            )
        if relation_blocked:
            continue

        if str(relationship["relation"].get("quantity_basis") or "").strip() == (
            "runtime_measurement_required"
        ):
            measurement_roles = [
                evidence["role_id"]
                for evidence in selected_evidence
                if isinstance(evidence.get("runtime_quantity_support"), dict)
                and evidence["runtime_quantity_support"].get(
                    "actual_inventory_measurement_declared"
                )
                is True
                and bool(
                    evidence["runtime_quantity_support"].get(
                        "reported_measurements"
                    )
                )
            ]
            stop_roles = [
                evidence["role_id"]
                for evidence in selected_evidence
                if isinstance(evidence.get("runtime_quantity_support"), dict)
                and evidence["runtime_quantity_support"].get(
                    "insufficient_quantity_stop_declared"
                )
                is True
                and evidence["runtime_quantity_support"].get(
                    "insufficient_quantity_stop_policy"
                )
                == "stop_before_material_consumption"
            ]
            if len(measurement_roles) != 1 or len(stop_roles) != 1:
                issues.append(
                    MaterialOperationBindingIssue(
                        "runtime_quantity_skill_support_unresolved",
                        "runtime measurement requires exactly one truth-bound actual-inventory reporter and one executable stop-before-consumption controller",
                        blocker_class=(
                            "plan_binding_ambiguous"
                            if len(measurement_roles) > 1 or len(stop_roles) > 1
                            else "software_unsupported"
                        ),
                        relationship_id=relationship_id,
                        missing_support=[
                            name
                            for present, name in (
                                (measurement_roles, "actual_inventory_measurement"),
                                (stop_roles, "insufficient_quantity_stop"),
                            )
                            if not present
                        ],
                        ambiguous_support=[
                            name
                            for values, name in (
                                (measurement_roles, "actual_inventory_measurement"),
                                (stop_roles, "insufficient_quantity_stop"),
                            )
                            if len(values) > 1
                        ],
                        measurement_role_ids=measurement_roles,
                        insufficient_quantity_stop_role_ids=stop_roles,
                    )
                )
                continue

        selected_keys = [record["step_key"] for record in selected]
        if len(selected_keys) != len(set(selected_keys)):
            issues.append(
                MaterialOperationBindingIssue(
                    "relationship_operation_scope_reuses_step",
                    "one Device step cannot satisfy two roles in the same material-operation scope",
                    blocker_class="plan_binding_ambiguous",
                    relationship_id=relationship_id,
                    implementation_steps=[
                        copy.deepcopy(record["plan_step"]) for record in selected
                    ],
                )
            )
            continue
        selected_indexes = [record["index"] for record in selected]
        if selected_indexes != sorted(selected_indexes):
            issues.append(
                MaterialOperationBindingIssue(
                    "relationship_operation_scope_order_mismatch",
                    "Device implementation order must match the Research capability-role order",
                    blocker_class="plan_binding_invalid",
                    relationship_id=relationship_id,
                    implementation_steps=[
                        copy.deepcopy(record["plan_step"]) for record in selected
                    ],
                )
            )
            continue

        commit_index = next(
            index
            for index, role in enumerate(implementation["ordered_steps"])
            if role["role_id"] == implementation["commit_role_id"]
        )
        commit_record = selected[commit_index]
        commit_evidence = selected_evidence[commit_index]
        logical_bindings, logical_issues = _logical_container_bindings(
            relationship_id=relationship_id,
            relation=relationship["relation"],
            selected_steps=selected,
        )
        if logical_issues:
            issues.extend(logical_issues)
            continue
        assert logical_bindings is not None

        drafts[relationship_id] = {
            "binding_status": "bound",
            "binding_scope_version": BINDING_SCOPE_VERSION,
            # Backward-compatible compiler commit target.  It is not the full
            # implementation scope and must always equal commit_step.
            "plan_step": copy.deepcopy(commit_record["plan_step"]),
            "implementation_steps": [
                copy.deepcopy(record["plan_step"]) for record in selected
            ],
            "commit_step": copy.deepcopy(commit_record["plan_step"]),
            "commit_role_id": implementation["commit_role_id"],
            "implementation_step_evidence": selected_evidence,
            "logical_container_bindings": logical_bindings,
            "operation_evidence": {
                "machine_readable_capability_id": commit_evidence[
                    "capability_id"
                ],
                "skill_contract_sha256": commit_evidence[
                    "skill_contract_sha256"
                ],
                "skill_contract_digest_scope": commit_evidence[
                    "skill_contract_digest_scope"
                ],
                "support_evidence_ref": commit_evidence[
                    "support_evidence_ref"
                ],
            },
            "resolver": RULE_ID,
        }

    if issues:
        return {}, issues
    return drafts, []


def resolve_and_seal_material_operation_binding_authority(
    candidate: Dict[str, Any],
    research_authority: Dict[str, Any],
    workstation_capability_index: Dict[str, Any],
    *,
    evidence_sources: Dict[str, str],
    workstation_truth_digest: str,
    resolve_workstation: Callable[[str], str],
) -> Tuple[Dict[str, Any], List[MaterialOperationBindingIssue]]:
    """Resolve bindings and seal an automated compiler authority envelope."""

    drafts, issues = resolve_material_operation_bindings(
        candidate,
        research_authority,
        workstation_capability_index,
        resolve_workstation=resolve_workstation,
    )
    if issues:
        return {}, issues
    try:
        try:
            from .material_relationship_compiler import (
                seal_relationship_binding_authority,
            )
        except ImportError:  # Direct-script compatibility.
            from material_relationship_compiler import (  # type: ignore
                seal_relationship_binding_authority,
            )

        authority = seal_relationship_binding_authority(
            candidate,
            research_authority,
            drafts,
            authoring_mode="automated_evidence_bound",
            automation_claim=True,
            evidence_sources=evidence_sources,
            workstation_truth_digest=workstation_truth_digest,
            resolve_workstation=resolve_workstation,
            workstation_capability_index=workstation_capability_index,
        )
    except (TypeError, ValueError) as exc:
        return {}, [
            MaterialOperationBindingIssue(
                "automated_relationship_binding_seal_rejected",
                str(exc),
                blocker_class="software_unsupported",
            )
        ]
    return authority, []


__all__ = [
    "BINDING_SCOPE_VERSION",
    "MaterialOperationBindingIssue",
    "RULE_ID",
    "SKILL_OPERATION_CONTRACT_DIGEST_SCOPE",
    "resolve_and_seal_material_operation_binding_authority",
    "resolve_material_operation_bindings",
]
