"""Loss-aware V1/V2 compatibility adapters.

The adapters make V2 canonical without forcing every existing CLI, test fixture
and frontend reader to change in one commit.  They never mutate their inputs.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Optional

from .container_requirements import parse_logical_container_requirements
from .identity import (
    IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
    IdentityContractError,
    JsonScalarIdentity,
    decode_package_identity,
    encode_json_scalar_identity,
    json_scalar_identity_key,
    normalize_json_scalar_identity,
)
from .v2 import (
    CONTRACT_VERSION_V2,
    RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
    RAW_STEP_DIGEST_SCOPE_V1,
    DeviceStepV2,
    DeviceWorkflowPackageV2,
    EvidenceBundleV2,
    EvidenceItemV2,
    ExperimentGroupV2,
    MacroActionV2,
    MacroStepV2,
    MaterialApplicabilityEvidenceV2,
    MaterialContractMigrationV2,
    MaterialContractStatusV2,
    MaterialOperationSegmentV2,
    MaterialOutputRefV2,
    MaterialPortV2,
    MaterialRelationV2,
    ObservationEventV2,
    ParameterTraceV2,
    ProvenanceV2,
    QuantityV2,
    ResearchActionPackageV2,
    ScientificParameterV2,
    StageV2,
    ValidationIssueV2,
    ValidationReportV2,
    WorkstationMappingV2,
    WorkstationRequirementV2,
    canonical_digest,
    canonical_raw_observations_digest,
    canonical_raw_step_digest,
)


_QUANTITY_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*"
    r"(µL|μL|uL|mL|ml|L|µg|μg|ug|mg|g|mmol|mol|mM|µM|μM|uM|M|"
    r"°C|℃|rpm|min|h|s)(?![A-Za-z])",
    re.IGNORECASE,
)


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> List[Dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)]


_MISSING = object()


class _ContractCollectionError(ValueError):
    def __init__(self, path: str, actual: Any, message: str) -> None:
        self.path = path
        self.actual = actual
        self.message = message
        super().__init__(f"{path}: {message}")


def _contract_object_list(value: Any, path: str) -> List[Dict[str, Any]]:
    """Strict list-of-object parser for identity-bearing contract collections."""

    if value is _MISSING:
        return []
    if not isinstance(value, list):
        raise _ContractCollectionError(
            path,
            value,
            "expected an array of objects",
        )
    result: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _ContractCollectionError(
                f"{path}[{index}]",
                item,
                "expected an object; contract entries cannot be filtered or skipped",
            )
        result.append(dict(item))
    return result


def _slug(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
    return text[:48] or fallback


def _source(
    value: Any,
    *,
    fallback_reason: str,
    native_v2: bool = False,
    path: str = "provenance",
) -> ProvenanceV2:
    """Parse provenance without upgrading malformed native V2 evidence.

    V1 text fields still need a loss-aware compatibility representation.  A
    native V2 producer, however, has already opted into the structured
    contract: unknown kinds, scalar values and missing inference rationales are
    errors rather than invitations for the adapter to invent a valid-looking
    ``agent_inferred`` record.
    """

    if native_v2:
        if not isinstance(value, dict):
            raise _ContractCollectionError(
                path,
                value,
                "native V2 provenance must be an explicit object",
            )
        return ProvenanceV2.model_validate(value, strict=True)
    if isinstance(value, dict):
        kind = str(value.get("kind") or value.get("source_type") or "agent_inferred")
        if kind not in {
            "user",
            "paper",
            "agent_inferred",
            "manual_revision",
            "runtime",
            "device_skill",
        }:
            kind = "agent_inferred"
        return ProvenanceV2(
            kind=kind,
            reference=str(value.get("reference") or value.get("source") or ""),
            rationale=str(value.get("rationale") or value.get("reason") or fallback_reason),
            source_path=str(value.get("source_path") or ""),
            excerpt=str(value.get("excerpt") or ""),
            source_digest=str(value.get("source_digest") or ""),
            revision_id=str(value.get("revision_id") or ""),
            manifest_digest=str(value.get("manifest_digest") or ""),
            automation_claim=value.get("automation_claim"),
        )
    text = str(value or "").strip()
    lowered = text.lower()
    if text.startswith("protocol:") or "doi" in lowered or "paper" in lowered:
        return ProvenanceV2(kind="paper", reference=text)
    if text and ("用户" in text or "user" in lowered):
        return ProvenanceV2(kind="user", reference=text)
    return ProvenanceV2(
        kind="agent_inferred",
        reference=text,
        rationale=fallback_reason,
    )


def _quantity(raw: Any) -> Optional[QuantityV2]:
    if isinstance(raw, dict):
        mode = str(raw.get("mode") or raw.get("quantity_mode") or "exact")
        payload: Dict[str, Any] = {
            "mode": mode,
            "value": raw.get("value"),
            "unit": str(raw.get("unit") or ""),
        }
        if "semantic" in raw:
            payload["semantic"] = raw.get("semantic")
        return QuantityV2.model_validate(payload, strict=True)
    match = _QUANTITY_RE.search(str(raw or ""))
    if match:
        return QuantityV2(value=float(match.group(1)), unit=match.group(2))
    return None


def _material_ports(
    raw_items: Any,
    *,
    step: Dict[str, Any],
    direction: str,
    step_id: str,
    path: str,
    declared: bool,
    native_v2: bool,
) -> List[MaterialPortV2]:
    result: List[MaterialPortV2] = []
    requirements = _items(step.get("quantity_requirements"))
    for index, raw in enumerate(_contract_object_list(raw_items, path), start=1):
        name = str(raw.get("name") or raw.get("material") or "").strip()
        if not name:
            raise _ContractCollectionError(
                f"{path}[{index - 1}].name",
                raw.get("name"),
                "material entry requires a nonempty name",
            )
        if declared:
            for field_name in ("material_id", "material_instance_id"):
                value = raw.get(field_name)
                if not isinstance(value, str) or not value.strip():
                    raise _ContractCollectionError(
                        f"{path}[{index - 1}].{field_name}",
                        value,
                        f"declared material requires an explicit {field_name}",
                    )
            if direction == "input" and raw.get("material_origin") not in {
                "external_inventory", "upstream_output"
            }:
                raise _ContractCollectionError(
                    f"{path}[{index - 1}].material_origin",
                    raw.get("material_origin"),
                    "declared input requires external_inventory or upstream_output",
                )
            if not isinstance(raw.get("provenance"), dict):
                raise _ContractCollectionError(
                    f"{path}[{index - 1}].provenance",
                    raw.get("provenance"),
                    "declared material requires structured provenance",
                )
        raw_quantity = raw.get("quantity")
        if native_v2 and raw_quantity is not None and not isinstance(
            raw_quantity, dict
        ):
            raise _ContractCollectionError(
                f"{path}[{index - 1}].quantity",
                raw_quantity,
                "native V2 material quantity must be an explicit structured object",
            )
        quantity = _quantity(raw_quantity)
        # Only the explicit V1 compatibility path may recover a historical
        # quantity from the legacy quantity_requirements list.  A native V2
        # port must carry its own structured quantity; matching names or
        # substrings is not evidence and must never silently complete it.
        if quantity is None and not native_v2:
            for requirement in requirements:
                material = str(requirement.get("material") or "")
                if material and (material in name or name in material):
                    quantity = _quantity(requirement)
                    if quantity is not None:
                        break
        if declared and native_v2 and quantity is None:
            raise _ContractCollectionError(
                f"{path}[{index - 1}].quantity",
                raw.get("quantity"),
                "declared native V2 material requires an explicit structured quantity",
            )
        parent_refs: List[MaterialOutputRefV2] = []
        for parent_index, parent in enumerate(
            _contract_object_list(
                raw.get("parent_output_refs", []),
                f"{path}[{index - 1}].parent_output_refs",
            )
        ):
            parent_step_id = parent.get("macro_step_id")
            if parent_step_id is None:
                raise _ContractCollectionError(
                    f"{path}[{index - 1}].parent_output_refs[{parent_index}].macro_step_id",
                    None,
                    "parent output reference requires macro_step_id",
                )
            parent_refs.append(
                MaterialOutputRefV2(
                    macro_step_id=encode_json_scalar_identity(
                        parent_step_id,
                        f"{path}[{index - 1}].parent_output_refs[{parent_index}].macro_step_id",
                    ),
                    material_instance_id=str(
                        parent.get("material_instance_id") or ""
                    ),
                )
            )
        raw_concentration = raw.get("concentration_value")
        if raw_concentration is not None and (
            isinstance(raw_concentration, bool)
            or not isinstance(raw_concentration, (int, float))
        ):
            raise _ContractCollectionError(
                f"{path}[{index - 1}].concentration_value",
                raw_concentration,
                "concentration_value must be a finite number",
            )
        port_payload: Dict[str, Any] = {
            "material_id": str(
                raw.get("material_id")
                or f"MAT_{step_id}_{direction}_{index}"
            ),
            "name": name,
            "state": str(raw.get("state") or "unknown"),
            "quantity": quantity,
            "concentration_value": (
                float(raw_concentration)
                if raw_concentration is not None
                else None
            ),
            "concentration_unit": str(raw.get("concentration_unit") or ""),
            "provenance": _source(
                (
                    raw.get("provenance")
                    if native_v2
                    else raw.get("provenance")
                    or raw.get("source")
                    or step.get("provenance")
                    or step.get("来源")
                ),
                fallback_reason=(
                    "Legacy material metadata was preserved during V1 to V2 "
                    "compatibility migration; it is not measurement evidence"
                ),
                native_v2=native_v2,
                path=f"{path}[{index - 1}].provenance",
            ),
        }
        for field_name in (
            "material_instance_id", "material_origin", "logical_container_id"
        ):
            if field_name in raw and raw[field_name] is not None:
                port_payload[field_name] = raw[field_name]
        if "parent_output_refs" in raw:
            port_payload["parent_output_refs"] = parent_refs
        result.append(
            MaterialPortV2.model_validate(port_payload)
        )
    return result


def _source_contract_version(source: Dict[str, Any]) -> str:
    raw = str(source.get("contract_version") or "v1").strip().lower()
    if raw in {"v1", "1", "1.0"}:
        return "v1"
    if raw in {"v2", "2", "2.0"}:
        return "v2"
    raise ValueError(f"unsupported Research source contract_version: {raw!r}")


def _material_contract_metadata(
    step: Dict[str, Any],
    *,
    source_contract_version: str,
    source_path: str,
) -> tuple[MaterialContractStatusV2, MaterialContractMigrationV2]:
    """Read explicit completeness declarations without inferring semantics.

    A nonempty legacy array can still be partial.  Therefore collection
    presence/content never upgrades an absent status to ``declared``.
    """

    raw_status = step.get("material_contract_status", _MISSING)
    diagnostics: List[str] = []
    if raw_status is _MISSING:
        status = MaterialContractStatusV2(
            material_inputs="unresolved",
            material_intermediates="unresolved",
            material_outputs="unresolved",
            logical_containers="unresolved",
            material_relations="unresolved",
        )
        source_fields = {
            "material_inputs": "material_inputs",
            "material_intermediates": "material_intermediates",
            "material_outputs": "material_outputs",
            "logical_containers": "container_requirements",
            "material_relations": "material_relations",
        }
        for status_field, source_field in source_fields.items():
            if source_field not in step:
                detail = "source field absent"
            elif isinstance(step[source_field], list) and step[source_field]:
                detail = "source field contains unverified partial records"
            elif isinstance(step[source_field], list):
                detail = "source field is empty without an applicability declaration"
            else:
                detail = "source field has an invalid non-array value"
            diagnostics.append(f"{status_field}: {detail}; completeness unresolved")
    else:
        if not isinstance(raw_status, dict):
            raise _ContractCollectionError(
                f"{source_path}.material_contract_status",
                raw_status,
                "expected an object with five explicit dispositions",
            )
        status = MaterialContractStatusV2.model_validate(raw_status, strict=True)
        diagnostics.append("material contract dispositions were explicitly supplied by Research")
    diagnostics.append(
        "no material identities, instances, relations, quantities, or applicability "
        "were inferred from macro sequence, operation category, or prose"
    )
    return status, MaterialContractMigrationV2(
        source_contract_version=source_contract_version,
        migration_mode=(
            "native_v2"
            if source_contract_version == "v2"
            else "legacy_v1_compatibility"
        ),
        source_path=source_path,
        diagnostics=diagnostics,
    )


def _string_id_list(value: Any, path: str) -> List[str]:
    if not isinstance(value, list):
        raise _ContractCollectionError(path, value, "expected an array of string IDs")
    result: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _ContractCollectionError(
                f"{path}[{index}]", item, "expected a nonempty string ID"
            )
        result.append(item)
    return result


def _material_allocations(value: Any, path: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for index, raw in enumerate(_contract_object_list(value, path)):
        quantity = _quantity(raw.get("quantity"))
        if quantity is None:
            raise _ContractCollectionError(
                f"{path}[{index}].quantity",
                raw.get("quantity"),
                "allocation requires an explicit planning quantity",
            )
        result.append({
            "material_instance_id": raw.get("material_instance_id"),
            "quantity": quantity,
        })
    return result


def _material_relations(
    raw_items: Any,
    *,
    step: Dict[str, Any],
    path: str,
    declared: bool,
    native_v2: bool,
) -> List[MaterialRelationV2]:
    relations: List[MaterialRelationV2] = []
    for index, raw in enumerate(_contract_object_list(raw_items, path)):
        if declared and not isinstance(raw.get("provenance"), dict):
            raise _ContractCollectionError(
                f"{path}[{index}].provenance",
                raw.get("provenance"),
                "declared material relation requires structured provenance",
            )
        source_ref = raw.get("source_operation_ref")
        if source_ref is None:
            source_ref = ""
        elif source_ref != "":
            source_ref = encode_json_scalar_identity(
                source_ref,
                f"{path}[{index}].source_operation_ref",
            )
        planning_quantity = (
            _quantity(raw.get("planning_quantity"))
            if raw.get("planning_quantity") is not None
            else None
        )
        relations.append(MaterialRelationV2(
            relation_id=str(raw.get("relation_id") or ""),
            event_kind=str(raw.get("event_kind") or ""),
            input_material_instance_ids=_string_id_list(
                raw.get("input_material_instance_ids", []),
                f"{path}[{index}].input_material_instance_ids",
            ),
            output_material_instance_ids=_string_id_list(
                raw.get("output_material_instance_ids", []),
                f"{path}[{index}].output_material_instance_ids",
            ),
            logical_container_ids=_string_id_list(
                raw.get("logical_container_ids", []),
                f"{path}[{index}].logical_container_ids",
            ),
            quantity_basis=raw.get("quantity_basis"),
            planning_quantity=planning_quantity,
            input_allocations=_material_allocations(
                raw.get("input_allocations", []),
                f"{path}[{index}].input_allocations",
            ),
            output_allocations=_material_allocations(
                raw.get("output_allocations", []),
                f"{path}[{index}].output_allocations",
            ),
            source_operation_ref=str(source_ref),
            provenance=_source(
                (
                    raw.get("provenance")
                    if native_v2
                    else raw.get("provenance")
                    or raw.get("source")
                    or step.get("provenance")
                    or step.get("来源")
                ),
                fallback_reason=(
                    "Legacy relation metadata was preserved without treating "
                    "operation prose as material or measurement evidence"
                ),
                native_v2=native_v2,
                path=f"{path}[{index}].provenance",
            ),
        ))
    return relations


def _operation_segments(
    raw_items: Any,
    *,
    path: str,
    native_v2: bool,
) -> List[MaterialOperationSegmentV2]:
    segments: List[MaterialOperationSegmentV2] = []
    for index, raw in enumerate(_contract_object_list(raw_items, path)):
        item_path = f"{path}[{index}]"
        payload = dict(raw)
        payload["provenance"] = _source(
            raw.get("provenance"),
            fallback_reason=(
                "Legacy operation-segment metadata was retained only as unresolved "
                "migration context"
            ),
            native_v2=native_v2,
            path=f"{item_path}.provenance",
        )
        segments.append(MaterialOperationSegmentV2.model_validate(payload, strict=True))
    return segments


def _material_applicability(
    raw_items: Any,
    *,
    path: str,
    native_v2: bool,
) -> List[MaterialApplicabilityEvidenceV2]:
    evidence: List[MaterialApplicabilityEvidenceV2] = []
    for index, raw in enumerate(_contract_object_list(raw_items, path)):
        item_path = f"{path}[{index}]"
        payload = dict(raw)
        payload["provenance"] = _source(
            raw.get("provenance"),
            fallback_reason=(
                "Legacy applicability metadata cannot establish that a material "
                "dimension is not applicable"
            ),
            native_v2=native_v2,
            path=f"{item_path}.provenance",
        )
        evidence.append(
            MaterialApplicabilityEvidenceV2.model_validate(payload, strict=True)
        )
    return evidence


def _parameters(
    step: Dict[str, Any],
    *,
    native_v2: bool,
    path: str,
) -> List[ScientificParameterV2]:
    provenance = _source(
        (
            step.get("provenance")
            if native_v2
            else step.get("provenance") or step.get("来源")
        ),
        fallback_reason="Research supplied this explicit experimental parameter",
        native_v2=native_v2,
        path=f"{path}.provenance",
    )
    text = str(step.get("参数") or step.get("parameters") or "").strip()
    result = [
        ScientificParameterV2(
            name="research_parameter_text",
            value=text or "unspecified",
            provenance=provenance,
        )
    ]
    for index, match in enumerate(_QUANTITY_RE.finditer(text), start=1):
        result.append(
            ScientificParameterV2(
                name=f"numeric_parameter_{index}",
                value=float(match.group(1)),
                unit=match.group(2),
                provenance=provenance,
            )
        )
    return result


def _evidence_bundle(state: Dict[str, Any], *, scope: str, action_id: str) -> EvidenceBundleV2:
    raw = _mapping(state.get("current_evidence_bundle"))
    results = _items(raw.get("results"))
    evidence_items: List[EvidenceItemV2] = []
    for index, item in enumerate(results, start=1):
        evidence_items.append(
            EvidenceItemV2(
                evidence_id=str(
                    item.get("evidence_id")
                    or item.get("paper_id")
                    or item.get("doi")
                    or item.get("arxiv_id")
                    or f"EV_{action_id}_{index}"
                ),
                title=str(item.get("title") or ""),
                doi=str(item.get("doi") or ""),
                arxiv_id=str(item.get("arxiv_id") or ""),
                url=str(item.get("url") or ""),
                verification_status=str(item.get("verification_status") or "unknown"),
                full_text_status=str(item.get("full_text_status") or "unknown"),
                excerpt=str(item.get("evidence_excerpt") or ""),
            )
        )
    query = str(raw.get("query") or _mapping(state.get("event")).get("query") or "")
    bundle_seed = {
        "action_id": action_id,
        "query": query,
        "results": [item.model_dump(mode="json") for item in evidence_items],
        "tool_invocation_index": raw.get("tool_invocation_index", 0),
    }
    return EvidenceBundleV2(
        bundle_id=str(raw.get("bundle_id") or canonical_digest(bundle_seed, prefix="evidence")),
        scope="macro_action" if scope == "macro_action" else "stage",
        query=query,
        objective=str(raw.get("objective") or ""),
        retrieval_status=str(raw.get("retrieval_status") or raw.get("status") or "empty"),
        current_invocation_only=True,
        items=evidence_items,
        errors=[str(item) for item in raw.get("errors", []) or []],
    )


def research_state_to_v2(state: Dict[str, Any]) -> ResearchActionPackageV2:
    """Convert either a Research state or its legacy handoff to canonical V2."""

    source = copy.deepcopy(state)
    handoff = _mapping(source.get("device_adaptation_handoff"))
    event = _mapping(source.get("event"))
    if (
        "macro_plan" in source
        and source["macro_plan"] is not None
        and not isinstance(source["macro_plan"], list)
    ):
        raw_macro_steps = source["macro_plan"]
        macro_steps_path = "macro_plan"
    elif source.get("macro_plan"):
        raw_macro_steps = source["macro_plan"]
        macro_steps_path = "macro_plan"
    elif (
        "待执行 macro plan" in handoff
        and handoff["待执行 macro plan"] is not None
        and not isinstance(handoff["待执行 macro plan"], list)
    ):
        raw_macro_steps = handoff["待执行 macro plan"]
        macro_steps_path = "device_adaptation_handoff.待执行 macro plan"
    elif handoff.get("待执行 macro plan"):
        raw_macro_steps = handoff["待执行 macro plan"]
        macro_steps_path = "device_adaptation_handoff.待执行 macro plan"
    elif (
        "macro_action_steps" in source
        and source["macro_action_steps"] is not None
        and not isinstance(source["macro_action_steps"], list)
    ):
        raw_macro_steps = source["macro_action_steps"]
        macro_steps_path = "macro_action_steps"
    elif source.get("macro_action_steps"):
        raw_macro_steps = source["macro_action_steps"]
        macro_steps_path = "macro_action_steps"
    else:
        raw_macro_steps = []
        macro_steps_path = "macro_plan"
    macro_steps = _contract_object_list(raw_macro_steps, macro_steps_path)
    if not macro_steps:
        raise ValueError("ResearchActionPackageV2 requires at least one macro step")
    source_version = _source_contract_version(source)
    action_raw = _mapping(
        source.get("macro_action") or handoff.get("当前 macro action")
    )
    current_stage = str(
        source.get("current_stage") or handoff.get("当前 stage") or "current stage"
    )
    stage_index = 1
    route = source.get("stage_route") or handoff.get("stage 路线") or []
    if isinstance(route, list) and current_stage in route:
        stage_index = route.index(current_stage) + 1
    stage_id = str(action_raw.get("stage_id") or f"STAGE_{stage_index:02d}")
    action_id_candidates: List[tuple[JsonScalarIdentity, str]] = []
    if action_raw.get("macro_action_id") is not None:
        action_id_candidates.append(
            (
                normalize_json_scalar_identity(
                    action_raw["macro_action_id"],
                    "macro_action.macro_action_id",
                ),
                "macro_action.macro_action_id",
            )
        )
    for step_index, step in enumerate(macro_steps):
        if step.get("macro_action_id") is None:
            continue
        step_action_path = f"macro_plan[{step_index}].macro_action_id"
        action_id_candidates.append(
            (
                normalize_json_scalar_identity(
                    step["macro_action_id"],
                    step_action_path,
                ),
                step_action_path,
            )
        )
    if action_id_candidates:
        raw_action_id = action_id_candidates[0][0]
        raw_action_key = json_scalar_identity_key(raw_action_id)
        if any(
            json_scalar_identity_key(candidate) != raw_action_key
            for candidate, _ in action_id_candidates[1:]
        ):
            locations = ", ".join(path for _, path in action_id_candidates)
            raise ValueError(
                "conflicting typed macro_action_id values across " + locations
            )
    else:
        raw_action_id = f"MA_{stage_id}_R{len(source.get('observations', []) or []):02d}"
    action_id = encode_json_scalar_identity(
        raw_action_id,
        "macro_action.macro_action_id",
    )
    observation = str(
        action_raw.get("observation_point")
        or action_raw.get("expected_observation")
        or current_stage
    )
    completion = str(
        action_raw.get("completion_condition")
        or f"获得 {observation} 的有效结果并可判读"
    )
    group_raw = _mapping(action_raw.get("experiment_group"))
    group_id = str(group_raw.get("group_id") or f"GRP_{_slug(action_id, 'ACTION')}_01")
    sample_id = str(group_raw.get("sample_id") or f"SAMPLE_{_slug(group_id, 'GROUP')}_01")
    experiment_group = ExperimentGroupV2(
        group_id=group_id,
        role=str(group_raw.get("role") or "experimental"),
        sample_id=sample_id,
        hypothesis=str(group_raw.get("hypothesis") or action_raw.get("objective") or ""),
        comparison_to=[str(item) for item in group_raw.get("comparison_to", []) or []],
        variables=dict(group_raw.get("variables") or {}),
    )
    converted: List[MacroStepV2] = []
    for sequence, step in enumerate(macro_steps, start=1):
        source_step_path = f"{macro_steps_path}[{sequence - 1}]"
        native_v2 = source_version == "v2"
        raw_step_id = step.get("macro_step_id")
        raw_logical_id = step.get("logical_step_id")
        if raw_step_id is not None and raw_logical_id is not None:
            if json_scalar_identity_key(
                raw_step_id,
                f"macro_plan[{sequence - 1}].macro_step_id",
            ) != json_scalar_identity_key(
                raw_logical_id,
                f"macro_plan[{sequence - 1}].logical_step_id",
            ):
                raise ValueError(
                    f"macro_plan[{sequence - 1}] has conflicting typed "
                    "macro_step_id/logical_step_id"
                )
        if raw_step_id is None:
            raw_step_id = raw_logical_id
        if raw_step_id is None:
            raw_step_id = f"MS_{_slug(action_id, 'ACTION')}_{sequence:03d}"
        step_id = encode_json_scalar_identity(
            raw_step_id,
            f"macro_plan[{sequence - 1}].macro_step_id",
        )
        material_status, material_migration = _material_contract_metadata(
            step,
            source_contract_version=source_version,
            source_path=source_step_path,
        )
        if material_status.logical_containers == "declared":
            raw_containers = _contract_object_list(
                step.get("container_requirements", _MISSING),
                f"{source_step_path}.container_requirements",
            )
            for container_index, container in enumerate(raw_containers):
                logical_id = container.get("logical_container_id")
                if not isinstance(logical_id, str) or not logical_id.strip():
                    raise _ContractCollectionError(
                        f"{source_step_path}.container_requirements[{container_index}].logical_container_id",
                        logical_id,
                        "declared logical container requires an explicit stable ID",
                    )
        logical_containers = parse_logical_container_requirements(
            step, sequence=sequence, step_id=raw_step_id,
        )
        material_relations = _material_relations(
            step.get("material_relations", _MISSING),
            step=step,
            path=f"{source_step_path}.material_relations",
            declared=material_status.material_relations == "declared",
            native_v2=native_v2,
        )
        operation_segments = _operation_segments(
            step.get("operation_segments", _MISSING),
            path=f"{source_step_path}.operation_segments",
            native_v2=native_v2,
        )
        material_applicability = _material_applicability(
            step.get("material_applicability", _MISSING),
            path=f"{source_step_path}.material_applicability",
            native_v2=native_v2,
        )
        provenance = _source(
            (
                step.get("provenance")
                if native_v2
                else step.get("provenance") or step.get("来源") or step.get("source")
            ),
            fallback_reason="Research generated this concrete macro step",
            native_v2=native_v2,
            path=f"{source_step_path}.provenance",
        )
        quantity_requirements = (
            _contract_object_list(
                step.get("quantity_requirements", _MISSING),
                f"{source_step_path}.quantity_requirements",
            )
            if native_v2
            else _items(step.get("quantity_requirements"))
        )
        converted.append(
            MacroStepV2(
                macro_step_id=step_id,
                macro_action_id=action_id,
                sequence=sequence,
                operation=str(step.get("操作") or step.get("operation") or "实验操作"),
                reagent_or_object=str(
                    step.get("试剂/对象") or step.get("reagent_or_object") or ""
                ),
                quantity_requirements=quantity_requirements,
                raw_step_digest_scope=RAW_STEP_DIGEST_SCOPE_V1,
                raw_step_sha256=canonical_raw_step_digest(step),
                sample_id=sample_id,
                material_inputs=_material_ports(
                    step.get("material_inputs", _MISSING),
                    step=step,
                    direction="input",
                    step_id=step_id,
                    path=f"{source_step_path}.material_inputs",
                    declared=material_status.material_inputs == "declared",
                    native_v2=native_v2,
                ),
                material_intermediates=_material_ports(
                    step.get("material_intermediates", _MISSING),
                    step=step,
                    direction="intermediate",
                    step_id=step_id,
                    path=f"{source_step_path}.material_intermediates",
                    declared=material_status.material_intermediates == "declared",
                    native_v2=native_v2,
                ),
                material_outputs=_material_ports(
                    step.get("material_outputs", _MISSING),
                    step=step,
                    direction="output",
                    step_id=step_id,
                    path=f"{source_step_path}.material_outputs",
                    declared=material_status.material_outputs == "declared",
                    native_v2=native_v2,
                ),
                parameters=_parameters(
                    step,
                    native_v2=native_v2,
                    path=source_step_path,
                ),
                logical_containers=logical_containers,
                material_relations=material_relations,
                operation_segments=operation_segments,
                material_applicability=material_applicability,
                material_contract_status=material_status,
                material_contract_migration=material_migration,
                expected_return=_items(step.get("intermediate_returns")),
                provenance=provenance,
            )
        )
    capability_snapshot = str(
        source.get("device_snapshot_id")
        or handoff.get("设备能力快照ID")
        or _mapping(event.get("constraints")).get("device_snapshot_id")
        or ""
    )
    stage = StageV2(
        stage_id=stage_id,
        name=current_stage,
        objective=str(
            action_raw.get("current_stage_plan")
            or source.get("current_stage_plan")
            or current_stage
        ),
        observation_point=observation,
        completion_condition=completion,
        capability_requirements=[
            str(item) for item in action_raw.get("capability_requirements", []) or []
        ],
    )
    action = MacroActionV2(
        macro_action_id=action_id,
        stage_id=stage_id,
        observation_point_id=str(
            action_raw.get("observation_point_id")
            or next(
                (
                    step.get("observation_point_id")
                    for step in macro_steps
                    if str(step.get("observation_point_id") or "").strip()
                ),
                "",
            )
            or f"OP_{_slug(observation, 'observation')}"
        ),
        experiment_group=experiment_group,
        objective=str(action_raw.get("objective") or current_stage),
        planned_operations=[
            str(item)
            for item in action_raw.get("planned_operations", []) or [step.operation for step in converted]
        ],
        expected_observation=str(action_raw.get("expected_observation") or observation),
        completion_condition=completion,
    )
    raw_observations = source.get("observations", handoff.get("observations", []))
    if not isinstance(raw_observations, list):
        raise _ContractCollectionError(
            "observations",
            raw_observations,
            "native V2 observations must be an explicit array",
        )
    package = ResearchActionPackageV2(
        identity_encoding=IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
        campaign_id=str(source.get("campaign_id") or handoff.get("campaign_id") or ""),
        capability_snapshot_id=capability_snapshot,
        stage=stage,
        macro_action=action,
        macro_steps=converted,
        evidence_bundle=_evidence_bundle(source, scope="macro_action", action_id=action_id),
        raw_observations_digest_scope=RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
        raw_observations_sha256=canonical_raw_observations_digest(raw_observations),
    )
    # Hash the representation that actually crosses the JSON boundary.  Some
    # nested Pydantic defaults are absent from an adapter-constructed model's
    # ``model_fields_set`` but present in ``model_dump``; hashing before this
    # round trip can therefore produce a package whose stored hash fails as
    # soon as the same payload is reloaded.  Removing only the freshly computed
    # hash and validating the wire payload once makes new packages stable while
    # retaining the model's historical-package compatibility rules.
    wire_payload = package.model_dump(mode="json", exclude_none=True)
    wire_payload.pop("research_contract_hash", None)
    return ResearchActionPackageV2.model_validate(wire_payload)


def attach_research_v2_contract(state: Dict[str, Any]) -> Dict[str, Any]:
    updated = copy.deepcopy(state)
    package = research_state_to_v2(updated)
    payload = package.model_dump(mode="json", exclude_none=True)
    updated["contract_version"] = "v2"
    updated["research_action_package_v2"] = payload
    handoff = _mapping(updated.get("device_adaptation_handoff"))
    handoff["contract_version"] = "v2"
    handoff["research_action_package_v2"] = copy.deepcopy(payload)
    updated["device_adaptation_handoff"] = handoff
    return updated


def _step_id_from_workflow(step: Dict[str, Any], index: int) -> str:
    declared: List[tuple[str, str]] = []
    for field in ("device_step_id", "step_id"):
        if field not in step:
            continue
        raw = step[field]
        if not isinstance(raw, str) or not raw.strip():
            raise _ContractCollectionError(
                f"workflow_json.steps[{index - 1}].{field}",
                raw,
                "expected a nonempty string device step identity",
            )
        declared.append((raw, field))
    if len(declared) == 2 and declared[0][0] != declared[1][0]:
        raise _ContractCollectionError(
            f"workflow_json.steps[{index - 1}]",
            {field: value for value, field in declared},
            "device_step_id and step_id mirrors must agree exactly",
        )
    return declared[0][0] if declared else f"DS_{index:04d}"


class _SourceMacroReferenceError(ValueError):
    def __init__(self, code: str, path: str, actual: Any, message: str) -> None:
        self.code = code
        self.path = path
        self.actual = actual
        self.message = message
        super().__init__(f"{path}: {message} ({code.lower()})")


def _source_macro_id(
    step: Dict[str, Any],
    path: str,
) -> tuple[JsonScalarIdentity, List[tuple[JsonScalarIdentity, str]]]:
    """Read the typed primary source while validating all source mirrors.

    A physical device step may cover several macro steps.  The singular field
    is the typed primary and complete list mirrors preserve ordered coverage.
    Scalar mirrors must agree with the first list item; complete list mirrors
    must agree in full typed order.
    """

    declared_primaries: List[tuple[JsonScalarIdentity, str]] = []
    full_list_mirrors: List[
        tuple[List[tuple[JsonScalarIdentity, str]], str]
    ] = []

    def normalize(raw: Any, candidate_path: str) -> JsonScalarIdentity:
        try:
            return normalize_json_scalar_identity(raw, candidate_path)
        except IdentityContractError as exc:
            raise _SourceMacroReferenceError(
                exc.code,
                exc.path,
                raw,
                exc.message,
            ) from exc

    if "source_macro_step_id" in step:
        raw = step["source_macro_step_id"]
        if isinstance(raw, list):
            raise _SourceMacroReferenceError(
                "INVALID_MACRO_SOURCE",
                f"{path}.source_macro_step_id",
                raw,
                "source_macro_step_id must be one scalar identity",
            )
        declared_primaries.append(
            (
                normalize(raw, f"{path}.source_macro_step_id"),
                f"{path}.source_macro_step_id",
            )
        )

    if "source_macro_step" in step:
        raw = step["source_macro_step"]
        values = raw if isinstance(raw, list) else [raw]
        if not values:
            raise _SourceMacroReferenceError(
                "INVALID_MACRO_SOURCE",
                f"{path}.source_macro_step",
                raw,
                "source_macro_step array must contain a primary identity",
            )
        normalized_values = [
            (
                normalize(
                    item,
                    f"{path}.source_macro_step[{index}]"
                    if isinstance(raw, list)
                    else f"{path}.source_macro_step",
                ),
                f"{path}.source_macro_step[{index}]"
                if isinstance(raw, list)
                else f"{path}.source_macro_step",
            )
            for index, item in enumerate(values)
        ]
        declared_primaries.append((normalized_values[0][0], normalized_values[0][1]))
        if isinstance(raw, list):
            full_list_mirrors.append((normalized_values, "source_macro_step"))

    if "source_macro_steps" in step:
        raw_many = step["source_macro_steps"]
        if not isinstance(raw_many, list) or not raw_many:
            raise _SourceMacroReferenceError(
                "INVALID_MACRO_SOURCE",
                f"{path}.source_macro_steps",
                raw_many,
                "source_macro_steps must be a nonempty array of scalar identities",
            )
        normalized_many = [
            (
                normalize(item, f"{path}.source_macro_steps[{index}]"),
                f"{path}.source_macro_steps[{index}]",
            )
            for index, item in enumerate(raw_many)
        ]
        declared_primaries.append((normalized_many[0][0], normalized_many[0][1]))
        full_list_mirrors.append((normalized_many, "source_macro_steps"))

    if not declared_primaries:
        raise _SourceMacroReferenceError(
            "MISSING_MACRO_SOURCE",
            path,
            None,
            "missing source_macro_step_id/source_macro_step/source_macro_steps",
        )

    primary, _ = declared_primaries[0]
    primary_key = json_scalar_identity_key(primary)
    if any(
        json_scalar_identity_key(candidate) != primary_key
        for candidate, _ in declared_primaries[1:]
    ):
        raise _SourceMacroReferenceError(
            "CONFLICTING_MACRO_SOURCE",
            path,
            [candidate for candidate, _ in declared_primaries],
            "source mirrors must declare the same typed primary macro identity",
        )
    if len(full_list_mirrors) > 1:
        reference_keys = [
            json_scalar_identity_key(value) for value, _ in full_list_mirrors[0][0]
        ]
        for values, _ in full_list_mirrors[1:]:
            if [json_scalar_identity_key(value) for value, _ in values] != reference_keys:
                raise _SourceMacroReferenceError(
                    "CONFLICTING_MACRO_SOURCE_COVERAGE",
                    path,
                    [
                        [value for value, _ in mirror_values]
                        for mirror_values, _ in full_list_mirrors
                    ],
                    "complete source list mirrors must match in typed order",
                )
    coverage = (
        full_list_mirrors[0][0]
        if full_list_mirrors
        else [(primary, declared_primaries[0][1])]
    )
    coverage_keys = [json_scalar_identity_key(value) for value, _ in coverage]
    if len(set(coverage_keys)) != len(coverage_keys):
        raise _SourceMacroReferenceError(
            "DUPLICATE_MACRO_SOURCE_COVERAGE",
            path,
            [value for value, _ in coverage],
            "source coverage identities must be unique typed values",
        )
    return primary, coverage


def _resolve_source_macro_id(
    raw_source: JsonScalarIdentity,
    *,
    research_by_key: Dict[tuple[str, Any], str],
    sequence_to_id: Dict[int, str],
    path: str,
    source_identity_encoding: str | None = None,
) -> str:
    """Resolve exact typed identity first, then an explicit integer sequence."""

    if source_identity_encoding not in {
        None,
        IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
    }:
        raise _SourceMacroReferenceError(
            "UNSUPPORTED_IDENTITY_ENCODING",
            path,
            source_identity_encoding,
            "unsupported source identity encoding marker",
        )
    source_for_match: JsonScalarIdentity = raw_source
    if source_identity_encoding == IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1:
        if not isinstance(raw_source, str):
            raise _SourceMacroReferenceError(
                "INVALID_WIRE_IDENTITY",
                path,
                raw_source,
                "a marked source identity must use the string wire representation",
            )
        try:
            source_for_match = decode_package_identity(
                raw_source,
                source_identity_encoding,
                path,
            )
        except IdentityContractError as exc:
            raise _SourceMacroReferenceError(
                exc.code,
                exc.path,
                raw_source,
                exc.message,
            ) from exc
    key = json_scalar_identity_key(source_for_match, path)
    if key in research_by_key:
        return research_by_key[key]
    # Legacy sequence compatibility is intentionally limited to a JSON integer.
    # A string such as "1" is an opaque ID and must never mean sequence 1.
    if (
        source_identity_encoding is None
        and type(raw_source) is int
        and raw_source > 0
        and raw_source in sequence_to_id
    ):
        return sequence_to_id[raw_source]
    raise _SourceMacroReferenceError(
        "UNKNOWN_MACRO_SOURCE",
        path,
        raw_source,
        "source does not match an exact typed macro ID or a positive integer sequence",
    )


def _consistent_identity_encoding(
    declarations: Iterable[tuple[str, Dict[str, Any]]],
) -> str | None:
    declared: List[tuple[str, Any]] = [
        (f"{path}.identity_encoding", payload.get("identity_encoding"))
        for path, payload in declarations
        if "identity_encoding" in payload
    ]
    for path, value in declared:
        if value is not None and value != IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1:
            raise _SourceMacroReferenceError(
                "UNSUPPORTED_IDENTITY_ENCODING",
                path,
                value,
                "unsupported identity encoding marker",
            )
    if len({value for _, value in declared}) > 1:
        raise _SourceMacroReferenceError(
            "CONFLICTING_IDENTITY_ENCODING",
            "identity_encoding",
            {path: value for path, value in declared},
            "identity encoding declarations must agree across result sections",
        )
    return declared[0][1] if declared else None


def _validation_issues(result: Dict[str, Any], step_ids: List[str]) -> List[ValidationIssueV2]:
    report = _mapping(result.get("dispatch_validation"))
    errors = [str(item) for item in report.get("errors", []) or []]
    issues: List[ValidationIssueV2] = []
    for index, message in enumerate(errors, start=1):
        device_step_id = ""
        matched_device_ids: List[str] = []
        for candidate in step_ids:
            # Validation prose is not an identity-bearing structure.  Attribute
            # only an unambiguous safe device token with proper boundaries;
            # never manufacture a typed macro ID from a free-text regex.
            if not re.fullmatch(r"[A-Za-z0-9_-]+", candidate or ""):
                continue
            pattern = rf"(?<![A-Za-z0-9_-]){re.escape(candidate)}(?![A-Za-z0-9_-])"
            if re.search(pattern, message):
                matched_device_ids.append(candidate)
        if len(matched_device_ids) == 1:
            device_step_id = matched_device_ids[0]
        issues.append(
            ValidationIssueV2(
                issue_id=f"VI_{index:04d}",
                validator=str(report.get("assessment_source") or "device_contract_validator"),
                device_step_id=device_step_id,
                rule=message,
                repair_scope="device_step" if device_step_id else "macro_step",
            )
        )
    return issues


def _terminal_macro_ids(
    result: Dict[str, Any],
    research: ResearchActionPackageV2,
    *,
    source_identity_encoding: str | None,
) -> List[str]:
    terminal_fields = (
        "macro_step_id",
        "source_macro_step_id",
        "source_macro_step",
        "macro_step_ids",
        "source_macro_steps",
        "blocking_constraints",
        "errors",
        "error",
        "message",
        "reason",
        "failure_reason",
    )
    if "error_package" in result:
        raw_payload = result.get("error_package")
        payload = raw_payload if isinstance(raw_payload, (dict, list)) else {}
    else:
        payload = {key: result[key] for key in terminal_fields if key in result}
    research_by_key: Dict[tuple[str, Any], str] = {}
    sequence_to_id: Dict[int, str] = {}
    for index, step in enumerate(research.macro_steps):
        decoded = decode_package_identity(
            step.macro_step_id,
            research.identity_encoding,
            f"research.macro_steps[{index}].macro_step_id",
        )
        research_by_key[json_scalar_identity_key(decoded)] = step.macro_step_id
        sequence_to_id[step.sequence] = step.macro_step_id

    structured: List[tuple[JsonScalarIdentity, str]] = []
    structured_seen = False

    def collect(node: Any, path: str) -> None:
        nonlocal structured_seen
        if isinstance(node, dict):
            identity_fields = {
                "macro_step_id",
                "source_macro_step_id",
                "source_macro_step",
                "macro_step_ids",
                "source_macro_steps",
            }
            for field in ("macro_step_id", "source_macro_step_id", "source_macro_step"):
                if field not in node:
                    continue
                structured_seen = True
                raw = node[field]
                values = raw if field == "source_macro_step" and isinstance(raw, list) else [raw]
                for item_index, item in enumerate(values):
                    item_path = f"{path}.{field}"
                    if isinstance(raw, list):
                        item_path += f"[{item_index}]"
                    structured.append(
                        (normalize_json_scalar_identity(item, item_path), item_path)
                    )
            for field in ("macro_step_ids", "source_macro_steps"):
                if field not in node:
                    continue
                structured_seen = True
                raw_many = node[field]
                if not isinstance(raw_many, list):
                    raise IdentityContractError(
                        "INVALID_MACRO_SOURCE",
                        f"{path}.{field}",
                        "expected an array of scalar identities",
                    )
                for item_index, item in enumerate(raw_many):
                    item_path = f"{path}.{field}[{item_index}]"
                    structured.append(
                        (normalize_json_scalar_identity(item, item_path), item_path)
                    )
            for key, child in node.items():
                if key not in identity_fields:
                    collect(child, f"{path}.{key}")
        elif isinstance(node, list):
            for item_index, child in enumerate(node):
                collect(child, f"{path}[{item_index}]")

    try:
        collect(payload, "error_package")
        if structured_seen:
            selected: List[str] = []
            seen: set[str] = set()
            for raw_source, source_path in structured:
                resolved = _resolve_source_macro_id(
                    raw_source,
                    research_by_key=research_by_key,
                    sequence_to_id=sequence_to_id,
                    path=source_path,
                    source_identity_encoding=source_identity_encoding,
                )
                if resolved not in seen:
                    seen.add(resolved)
                    selected.append(resolved)
            return selected
    except (IdentityContractError, _SourceMacroReferenceError):
        return []

    text_fragments: List[str] = []

    def collect_text_values(node: Any) -> None:
        if isinstance(node, str):
            text_fragments.append(node)
        elif isinstance(node, dict):
            for child in node.values():
                collect_text_values(child)
        elif isinstance(node, list):
            for child in node:
                collect_text_values(child)

    collect_text_values(payload)
    text = "\n".join(text_fragments)
    decoded_string_ids: List[str] = []
    for index, step in enumerate(research.macro_steps):
        decoded = decode_package_identity(
            step.macro_step_id,
            research.identity_encoding,
            f"research.macro_steps[{index}].macro_step_id",
        )
        if isinstance(decoded, str):
            decoded_string_ids.append(decoded)
    selected = []
    for index, step in enumerate(research.macro_steps):
        decoded = decode_package_identity(
            step.macro_step_id,
            research.identity_encoding,
            f"research.macro_steps[{index}].macro_step_id",
        )
        # Free-text compatibility is limited to legacy identifier tokens such
        # as MS_002. Numeric, wire-encoded, whitespace, and punctuation-rich
        # identities require structured fields; otherwise substring matches
        # (A/B vs B, foo.bar vs bar) are not reliable identity evidence.
        if not isinstance(decoded, str):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+", decoded):
            continue
        if not re.search(r"[A-Za-z_]", decoded):
            continue
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(decoded)}(?![A-Za-z0-9_-])"
        if any(
            other != decoded and re.search(pattern, other)
            for other in decoded_string_ids
        ):
            # A safe token such as ``B`` is still ambiguous when another
            # Research ID is ``A/B``, ``foo.B`` or ``A+B``.  Such cases need a
            # structured identity field rather than containment evidence.
            continue
        if re.search(pattern, text):
            selected.append(step.macro_step_id)
    # Fail closed: an unmappable verdict without a precise source binding is
    # not allowed to masquerade as an exact terminal diagnosis.
    return selected


def device_result_to_v2(
    result: Dict[str, Any],
    research: ResearchActionPackageV2,
    *,
    capability_snapshot_id: str = "",
) -> DeviceWorkflowPackageV2:
    workflow = _mapping(result.get("workflow_json"))
    error_package = _mapping(result.get("error_package"))
    collection_errors: List[_ContractCollectionError] = []
    try:
        raw_steps = _contract_object_list(
            workflow.get("steps", _MISSING),
            "workflow_json.steps",
        )
    except _ContractCollectionError as exc:
        raw_steps = []
        collection_errors.append(exc)
    try:
        raw_plan_steps = _contract_object_list(
            result.get("device_plan", _MISSING),
            "device_plan",
        )
    except _ContractCollectionError as exc:
        raw_plan_steps = []
        collection_errors.append(exc)

    workflow_step_ids: List[str] = []
    try:
        seen_device_step_ids: Dict[str, int] = {}
        for step_index, step in enumerate(raw_steps, start=1):
            device_step_id = _step_id_from_workflow(step, step_index)
            if device_step_id in seen_device_step_ids:
                raise _ContractCollectionError(
                    f"workflow_json.steps[{step_index - 1}].device_step_id",
                    device_step_id,
                    (
                        "device_step_id must be unique; duplicates "
                        f"workflow_json.steps[{seen_device_step_ids[device_step_id]}]"
                    ),
                )
            seen_device_step_ids[device_step_id] = step_index - 1
            workflow_step_ids.append(device_step_id)
    except _ContractCollectionError as exc:
        raw_steps = []
        workflow_step_ids = []
        collection_errors.append(exc)
    identity_encoding_error: Optional[_SourceMacroReferenceError] = None
    try:
        _consistent_identity_encoding(
            (
                ("result", result),
                ("workflow_json", workflow),
                ("error_package", error_package),
            )
        )
        result_identity_encoding = (
            result.get("identity_encoding")
            if "identity_encoding" in result
            else None
        )
        workflow_identity_encoding = (
            workflow.get("identity_encoding")
            if "identity_encoding" in workflow
            else result_identity_encoding
        )
        error_identity_encoding = (
            error_package.get("identity_encoding")
            if "identity_encoding" in error_package
            else result_identity_encoding
        )
    except _SourceMacroReferenceError as exc:
        result_identity_encoding = None
        workflow_identity_encoding = None
        error_identity_encoding = None
        identity_encoding_error = exc
    research_ids = [step.macro_step_id for step in research.macro_steps]
    research_by_key: Dict[tuple[str, Any], str] = {}
    for index, research_step in enumerate(research.macro_steps):
        decoded_id = decode_package_identity(
            research_step.macro_step_id,
            research.identity_encoding,
            f"research.macro_steps[{index}].macro_step_id",
        )
        research_by_key[
            json_scalar_identity_key(
                decoded_id,
                f"research.macro_steps[{index}].macro_step_id",
            )
        ] = research_step.macro_step_id
    sequence_to_id = {
        step.sequence: step.macro_step_id for step in research.macro_steps
    }
    collection_issues: List[ValidationIssueV2] = [
        ValidationIssueV2(
            issue_id=f"VI_COLLECTION_{index:04d}",
            validator="contract_object_collection_v1",
            field_path=error.path,
            actual=error.actual,
            expected="array of objects with unique nonempty string device_step_id values",
            rule=error.message,
            source_ref="chem_agent_contracts/adapters.py",
            repair_scope="device_step",
        )
        for index, error in enumerate(collection_errors, start=1)
    ]
    source_binding_issues: List[ValidationIssueV2] = []

    def add_source_binding_issue(
        error: _SourceMacroReferenceError,
        *,
        collection: str,
        index: int,
        device_step_id: str = "",
    ) -> None:
        source_binding_issues.append(
            ValidationIssueV2(
                issue_id=f"VI_SOURCE_{len(source_binding_issues) + 1:04d}",
                validator="typed_macro_source_binding_v1",
                device_step_id=device_step_id,
                field_path=error.path,
                actual=error.actual,
                expected={
                    "exact_macro_step_ids": research_ids,
                    "legacy_sequence_numbers": sorted(sequence_to_id),
                },
                rule=(
                    f"{collection}[{index}] has an invalid typed macro identity binding: "
                    f"{error.message}"
                ),
                source_ref="chem_agent_contracts/identity.py",
                repair_scope="device_step",
            )
        )

    if identity_encoding_error is not None:
        add_source_binding_issue(
            identity_encoding_error,
            collection="device result",
            index=0,
        )

    requirements: Dict[str, WorkstationRequirementV2] = {
        f"WT_{step.macro_step_id}": WorkstationRequirementV2(
            workstation_task_id=f"WT_{step.macro_step_id}",
            macro_step_id=step.macro_step_id,
            functional_role=step.operation,
            required_capabilities=[step.operation],
            input_state=", ".join(
                sorted({item.state for item in step.material_inputs if item.state})
            ) or "unknown",
            output_state=", ".join(
                sorted({item.state for item in step.material_outputs if item.state})
            ) or "unknown",
            candidate_station_codes=[],
        )
        for step in research.macro_steps
    }
    for plan_index, plan_step in enumerate(raw_plan_steps):
        if identity_encoding_error is not None:
            continue
        try:
            raw_source, source_coverage = _source_macro_id(
                plan_step,
                f"device_plan[{plan_index}]",
            )
            resolved_coverage = [
                _resolve_source_macro_id(
                    coverage_id,
                    research_by_key=research_by_key,
                    sequence_to_id=sequence_to_id,
                    path=coverage_path,
                    source_identity_encoding=result_identity_encoding,
                )
                for coverage_id, coverage_path in source_coverage
            ]
            source_macro = resolved_coverage[0]
        except _SourceMacroReferenceError as exc:
            add_source_binding_issue(
                exc,
                collection="device_plan",
                index=plan_index,
            )
            continue
        station = str(plan_step.get("station_code") or plan_step.get("workstation") or "")
        if station:
            for covered_macro in resolved_coverage:
                task_id = f"WT_{covered_macro}"
                if task_id in requirements:
                    candidates = requirements[task_id].candidate_station_codes
                    if station not in candidates:
                        candidates.append(station)
    device_steps: List[DeviceStepV2] = []
    skill_by_code = {
        str(item.get("station_code") or ""): item
        for item in _items(result.get("loaded_workstation_skills"))
    }
    for index, step in enumerate(raw_steps, start=1):
        device_step_id = workflow_step_ids[index - 1]
        if identity_encoding_error is not None:
            continue
        try:
            raw_source, source_coverage = _source_macro_id(
                step,
                f"workflow.steps[{index - 1}]",
            )
            resolved_coverage = [
                _resolve_source_macro_id(
                    coverage_id,
                    research_by_key=research_by_key,
                    sequence_to_id=sequence_to_id,
                    path=coverage_path,
                    source_identity_encoding=workflow_identity_encoding,
                )
                for coverage_id, coverage_path in source_coverage
            ]
            source_macro = resolved_coverage[0]
        except _SourceMacroReferenceError as exc:
            add_source_binding_issue(
                exc,
                collection="workflow.steps",
                index=index - 1,
                device_step_id=device_step_id,
            )
            continue
        station_code = str(
            step.get("station_code") or step.get("workstation") or "unknown_station"
        )
        task_id = f"WT_{source_macro}"
        for covered_macro in resolved_coverage:
            covered_task_id = f"WT_{covered_macro}"
            requirements.setdefault(
                covered_task_id,
                WorkstationRequirementV2(
                    workstation_task_id=covered_task_id,
                    macro_step_id=covered_macro,
                    functional_role=str(step.get("operation") or "device operation"),
                    required_capabilities=[str(step.get("operation") or "")],
                    candidate_station_codes=[station_code],
                ),
            )
            if station_code not in requirements[covered_task_id].candidate_station_codes:
                requirements[covered_task_id].candidate_station_codes.append(station_code)
        raw_id = step.get("id") or step.get("station_id")
        station_id = int(raw_id) if isinstance(raw_id, (int, float)) else None
        manifest = skill_by_code.get(station_code, {})
        version_match = re.search(r"(?:_|-)(V\d+)$", station_code, re.I)
        device_steps.append(
            DeviceStepV2(
                device_step_id=device_step_id,
                source_macro_step_id=source_macro,
                source_macro_step_ids=resolved_coverage,
                workstation_task_id=task_id,
                station_code=station_code,
                station_version=(
                    version_match.group(1).upper()
                    if version_match
                    else str(step.get("station_version") or "versioned")
                ),
                platform_name=str(step.get("platform_name") or step.get("workstation") or station_code),
                station_id=station_id,
                operation=str(step.get("operation") or "unknown_operation"),
                parameters=dict(step.get("parameters") or {}),
                container_bindings=list(step.get("container_bindings") or []),
                material_bindings=list(step.get("material_bindings") or []),
                skill_sha256=str(manifest.get("source_sha256") or ""),
            )
        )
    raw_status = str(result.get("status") or "")
    if raw_status in {"success", "completed", "ready_for_dispatch"} and raw_steps:
        status = "ready_for_dispatch"
    elif raw_status in {"feasibility_error", "terminal_unmappable", "unsupported", "not_feasible"}:
        status = "terminal_unmappable"
    elif raw_status == "manual_required":
        status = "human_review_required"
    else:
        status = "device_internal_error"
    if collection_issues or source_binding_issues:
        status = "human_review_required"
    repair = _mapping(result.get("workflow_repair_cycle"))
    rounds_used = min(10, int(repair.get("modification_count") or 0))
    step_hashes = {
        step.device_step_id: canonical_digest(step, prefix="device_step")
        for step in device_steps
    }
    issues = (
        collection_issues
        + source_binding_issues
        + _validation_issues(result, list(step_hashes))
    )
    dispatch_report = _mapping(result.get("dispatch_validation"))
    explicit_dispatch_status = (
        str(dispatch_report.get("status") or "").strip().lower()
        if "status" in dispatch_report
        else None
    )
    if status == "ready_for_dispatch" and explicit_dispatch_status != "passed":
        raw_errors = dispatch_report.get("errors")
        has_reported_errors = isinstance(raw_errors, list) and bool(raw_errors)
        if explicit_dispatch_status is None or not has_reported_errors:
            missing_status = explicit_dispatch_status is None
            issues.append(
                ValidationIssueV2(
                    issue_id="VI_DISPATCH_STATUS_0001",
                    validator=str(
                        dispatch_report.get("assessment_source")
                        or "device_contract_validator"
                    ),
                    field_path="/dispatch_validation/status",
                    actual=dispatch_report.get("status"),
                    expected="passed",
                    rule=(
                        "ready_for_dispatch requires an explicit passed "
                        "dispatch_validation status"
                        if missing_status
                        else "dispatch_validation reported a non-passed status "
                        "without a structured error"
                    ),
                    source_ref="chem_agent_contracts/adapters.py",
                    repair_scope="device_step",
                )
            )
        status = "human_review_required"
    if issues and status == "ready_for_dispatch":
        status = "human_review_required"
    terminal_ids = (
        _terminal_macro_ids(
            result,
            research,
            source_identity_encoding=error_identity_encoding,
        )
        if status == "terminal_unmappable"
        else []
    )
    if status == "terminal_unmappable" and not terminal_ids:
        status = "human_review_required"
        issues.append(
            ValidationIssueV2(
                issue_id=f"VI_{len(issues) + 1:04d}",
                validator="terminal_mapping_guard",
                rule="terminal_unmappable requires an exact macro_step_id",
                repair_scope="terminal",
            )
        )
    return DeviceWorkflowPackageV2(
        identity_encoding=research.identity_encoding,
        status=status,
        campaign_id=research.campaign_id,
        device_package_id=str(
            result.get("device_package_id")
            or canonical_digest(
                {"research": research.research_contract_hash, "workflow": workflow},
                prefix="device_package",
            )
        ),
        capability_snapshot_id=str(
            capability_snapshot_id
            or result.get("device_snapshot_id")
            or research.capability_snapshot_id
        ),
        research_contract_hash=research.research_contract_hash,
        workstation_mapping=WorkstationMappingV2(
            requirements=list(requirements.values()),
            device_steps=device_steps,
        ),
        workflow=workflow,
        dispatch_payload=_mapping(result.get("dispatch_payload")),
        loaded_workstation_skills=_items(result.get("loaded_workstation_skills")),
        validation=ValidationReportV2(
            status="passed" if status == "ready_for_dispatch" and not issues else "failed",
            rounds_used=rounds_used,
            issues=issues,
            locked_step_hashes=step_hashes,
        ),
        terminal_macro_step_ids=terminal_ids,
    )


def _without_macro_identity_fields(value: Any) -> Any:
    """Drop raw terminal identity mirrors before writing canonical wire IDs.

    The original error package remains available in
    ``legacy_terminal_classification``.  Keeping legacy raw IDs beside newly
    encoded IDs under one marker would make a second adapter pass ambiguous.
    """

    identity_fields = {
        "macro_step_id",
        "source_macro_step_id",
        "source_macro_step",
        "macro_step_ids",
        "source_macro_steps",
        "identity_encoding",
    }
    if isinstance(value, dict):
        return {
            key: _without_macro_identity_fields(child)
            for key, child in value.items()
            if key not in identity_fields
        }
    if isinstance(value, list):
        return [_without_macro_identity_fields(child) for child in value]
    return copy.deepcopy(value)


def attach_device_v2_contract(
    result: Dict[str, Any], research: ResearchActionPackageV2
) -> Dict[str, Any]:
    updated = copy.deepcopy(result)
    package = device_result_to_v2(updated, research)
    payload = package.model_dump(mode="json", exclude_none=True)
    updated["contract_version"] = "v2"
    updated["device_workflow_package_v2"] = payload
    updated["device_package_id"] = package.device_package_id
    if updated.get("identity_encoding") is None:
        updated.pop("identity_encoding", None)
    if package.status == "ready_for_dispatch":
        updated["status"] = "ready_for_dispatch"
        updated["feedback_route"] = "success"
    elif package.status == "terminal_unmappable":
        original = {
            key: copy.deepcopy(updated.get(key))
            for key in ("status", "feedback_type", "feedback_route", "failure_scope", "error_package")
        }
        updated["legacy_terminal_classification"] = original
        updated["status"] = "terminal_unmappable"
        updated["feedback_type"] = "terminal_unmappable"
        updated["feedback_route"] = "terminal"
        updated["failure_scope"] = "macro_step_mapping"
        error = _mapping(_without_macro_identity_fields(updated.get("error_package")))
        error["type"] = "terminal_unmappable"
        error["macro_step_ids"] = package.terminal_macro_step_ids
        if package.identity_encoding is not None:
            error["identity_encoding"] = package.identity_encoding
        else:
            error.pop("identity_encoding", None)
        workflow = _mapping(updated.get("workflow_json"))
        if workflow.get("identity_encoding") is None:
            workflow.pop("identity_encoding", None)
            if "workflow_json" in updated:
                updated["workflow_json"] = workflow
        updated["error_package"] = error
    elif package.status == "human_review_required":
        updated["status"] = "manual_required"
        updated["feedback_type"] = "human_review_required"
        updated["feedback_route"] = "human"
        updated.setdefault("failure_scope", "device_workflow")
    elif package.status == "device_internal_error":
        updated["status"] = "device_internal_error"
        updated["feedback_type"] = "device_internal_error"
        updated["feedback_route"] = "device"
        updated.setdefault("failure_scope", "device_workflow")
        error = _mapping(updated.get("error_package"))
        error.setdefault("type", "device_internal_error")
        updated["error_package"] = error
    return updated


def _numeric_deviation(planned: Any, actual: Any) -> Any:
    if (
        isinstance(planned, (int, float))
        and not isinstance(planned, bool)
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
    ):
        return float(actual) - float(planned)
    return None


def build_observation_event_v2(
    observation: Dict[str, Any],
    research: ResearchActionPackageV2,
    device: DeviceWorkflowPackageV2,
) -> ObservationEventV2:
    actual_trace = _items(observation.get("device_parameter_trace"))
    actual_by_key = {
        (str(item.get("device_step_id") or ""), str(item.get("name") or "")): item
        for item in actual_trace
    }
    planned_by_macro: Dict[str, Dict[str, Any]] = {}
    for macro in research.macro_steps:
        planned_by_macro[macro.macro_step_id] = {
            parameter.name: {"value": parameter.value, "unit": parameter.unit}
            for parameter in macro.parameters
        }
    trace: List[ParameterTraceV2] = []
    for device_step in device.workstation_mapping.device_steps:
        macro_parameters = planned_by_macro.get(device_step.source_macro_step_id, {})
        for name, setpoint in device_step.parameters.items():
            actual = actual_by_key.get((device_step.device_step_id, str(name)), {})
            planned_record = macro_parameters.get(str(name), {})
            planned_value = planned_record.get("value")
            actual_value = actual.get("actual_value")
            trace.append(
                ParameterTraceV2(
                    macro_step_id=device_step.source_macro_step_id,
                    device_step_id=device_step.device_step_id,
                    name=str(name),
                    planned_value=planned_value,
                    device_setpoint=setpoint,
                    actual_value=actual_value,
                    unit=str(actual.get("unit") or planned_record.get("unit") or ""),
                    deviation=actual.get("deviation", _numeric_deviation(planned_value, actual_value)),
                    actual_status=(
                        str(actual.get("actual_status"))
                        if actual.get("actual_status") in {"reported", "unavailable", "not_applicable"}
                        else ("reported" if "actual_value" in actual else "unavailable")
                    ),
                )
            )
    macro_summary = [
        {
            "macro_step_id": macro.macro_step_id,
            "operation": macro.operation,
            "sample_id": macro.sample_id,
            "material_inputs": [item.model_dump(mode="json", exclude_none=True) for item in macro.material_inputs],
            "parameters": [item.model_dump(mode="json", exclude_none=True) for item in macro.parameters],
        }
        for macro in research.macro_steps
    ]
    return ObservationEventV2(
        identity_encoding=research.identity_encoding,
        campaign_id=research.campaign_id,
        macro_action_id=research.macro_action.macro_action_id,
        device_package_id=device.device_package_id,
        execution_status=str(observation.get("status") or "unknown"),
        summary=str(observation.get("summary") or ""),
        macro_parameter_summary=macro_summary,
        device_parameter_trace=trace,
        measurements=dict(observation.get("measurements") or observation.get("metrics") or {}),
        artifacts=_items(observation.get("artifacts")),
        material_consumption=_items(observation.get("material_consumption")),
        errors=[str(item) for item in observation.get("errors", []) or []],
    )
