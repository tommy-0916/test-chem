"""Loss-aware V1/V2 compatibility adapters.

The adapters make V2 canonical without forcing every existing CLI, test fixture
and frontend reader to change in one commit.  They never mutate their inputs.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Optional

from .container_requirements import parse_logical_container_requirements
from .v2 import (
    CONTRACT_VERSION_V2,
    DeviceStepV2,
    DeviceWorkflowPackageV2,
    EvidenceBundleV2,
    EvidenceItemV2,
    ExperimentGroupV2,
    MacroActionV2,
    MacroStepV2,
    MaterialPortV2,
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


def _slug(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
    return text[:48] or fallback


def _source(value: Any, *, fallback_reason: str) -> ProvenanceV2:
    if isinstance(value, dict):
        kind = str(value.get("kind") or value.get("source_type") or "agent_inferred")
        if kind not in {"user", "paper", "agent_inferred", "runtime", "device_skill"}:
            kind = "agent_inferred"
        return ProvenanceV2(
            kind=kind,
            reference=str(value.get("reference") or value.get("source") or ""),
            rationale=str(value.get("rationale") or value.get("reason") or fallback_reason),
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
        if mode in {"all_available", "runtime_measured"}:
            return QuantityV2(mode=mode)
        value = raw.get("value")
        unit = str(raw.get("unit") or "")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and unit:
            return QuantityV2(value=float(value), unit=unit)
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
) -> List[MaterialPortV2]:
    result: List[MaterialPortV2] = []
    requirements = _items(step.get("quantity_requirements"))
    for index, raw in enumerate(_items(raw_items), start=1):
        name = str(raw.get("name") or raw.get("material") or "").strip()
        if not name:
            continue
        quantity = _quantity(raw.get("quantity"))
        if quantity is None:
            for requirement in requirements:
                material = str(requirement.get("material") or "")
                if material and (material in name or name in material):
                    quantity = _quantity(requirement)
                    if quantity is not None:
                        break
        if direction == "output" and quantity is None:
            quantity = QuantityV2(mode="all_available")
        result.append(
            MaterialPortV2(
                material_id=str(raw.get("material_id") or f"MAT_{step_id}_{direction}_{index}"),
                name=name,
                state=str(raw.get("state") or "unknown"),
                quantity=quantity,
                concentration_value=(
                    float(raw["concentration_value"])
                    if isinstance(raw.get("concentration_value"), (int, float))
                    else None
                ),
                concentration_unit=str(raw.get("concentration_unit") or ""),
                provenance=_source(
                    raw.get("provenance") or raw.get("source") or step.get("provenance") or step.get("来源"),
                    fallback_reason="V1 material port was normalized into the V2 contract",
                ),
            )
        )
    return result


def _parameters(step: Dict[str, Any]) -> List[ScientificParameterV2]:
    provenance = _source(
        step.get("provenance") or step.get("来源"),
        fallback_reason="Research supplied this explicit experimental parameter",
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
                evidence_id=str(item.get("paper_id") or item.get("doi") or f"EV_{action_id}_{index}"),
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
    macro_steps = _items(
        source.get("macro_plan")
        or handoff.get("待执行 macro plan")
        or source.get("macro_action_steps")
    )
    if not macro_steps:
        raise ValueError("ResearchActionPackageV2 requires at least one macro step")
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
    action_id = str(
        action_raw.get("macro_action_id")
        or macro_steps[0].get("macro_action_id")
        or f"MA_{stage_id}_R{len(source.get('observations', []) or []):02d}"
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
        step_id = str(
            step.get("macro_step_id")
            or step.get("logical_step_id")
            or f"MS_{_slug(action_id, 'ACTION')}_{sequence:03d}"
        )
        logical_containers = parse_logical_container_requirements(
            step, sequence=sequence, step_id=step_id,
        )
        provenance = _source(
            step.get("provenance") or step.get("来源") or step.get("source"),
            fallback_reason="Research generated this concrete macro step",
        )
        converted.append(
            MacroStepV2(
                macro_step_id=step_id,
                macro_action_id=action_id,
                sequence=sequence,
                operation=str(step.get("操作") or step.get("operation") or "实验操作"),
                sample_id=sample_id,
                material_inputs=_material_ports(
                    step.get("material_inputs"), step=step, direction="input", step_id=step_id
                ),
                material_outputs=_material_ports(
                    step.get("material_outputs"), step=step, direction="output", step_id=step_id
                ),
                parameters=_parameters(step),
                logical_containers=logical_containers,
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
        experiment_group=experiment_group,
        objective=str(action_raw.get("objective") or current_stage),
        planned_operations=[
            str(item)
            for item in action_raw.get("planned_operations", []) or [step.operation for step in converted]
        ],
        expected_observation=str(action_raw.get("expected_observation") or observation),
        completion_condition=completion,
    )
    return ResearchActionPackageV2(
        campaign_id=str(source.get("campaign_id") or handoff.get("campaign_id") or ""),
        capability_snapshot_id=capability_snapshot,
        stage=stage,
        macro_action=action,
        macro_steps=converted,
        evidence_bundle=_evidence_bundle(source, scope="macro_action", action_id=action_id),
    )


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
    return str(
        step.get("device_step_id")
        or step.get("step_id")
        or f"DS_{index:04d}"
    )


def _source_macro_id(step: Dict[str, Any], fallback: str) -> str:
    raw = step.get("source_macro_step_id") or step.get("source_macro_step")
    if raw:
        return str(raw)
    raw_many = step.get("source_macro_steps")
    if isinstance(raw_many, list) and raw_many:
        return str(raw_many[0])
    return fallback


def _validation_issues(result: Dict[str, Any], step_ids: List[str]) -> List[ValidationIssueV2]:
    report = _mapping(result.get("dispatch_validation"))
    errors = [str(item) for item in report.get("errors", []) or []]
    issues: List[ValidationIssueV2] = []
    for index, message in enumerate(errors, start=1):
        device_step_id = ""
        macro_step_id = ""
        for candidate in step_ids:
            if candidate and candidate in message:
                device_step_id = candidate
                break
        macro_match = re.search(r"(?:macro(?:_| )step|source_macro_step)[^A-Za-z0-9_-]*([A-Za-z0-9_-]+)", message, re.I)
        if macro_match:
            macro_step_id = macro_match.group(1)
        issues.append(
            ValidationIssueV2(
                issue_id=f"VI_{index:04d}",
                validator=str(report.get("assessment_source") or "device_contract_validator"),
                macro_step_id=macro_step_id,
                device_step_id=device_step_id,
                rule=message,
                repair_scope="device_step" if device_step_id else "macro_step",
            )
        )
    return issues


def _terminal_macro_ids(result: Dict[str, Any], research: ResearchActionPackageV2) -> List[str]:
    text = json.dumps(result.get("error_package") or result, ensure_ascii=False)
    selected = [step.macro_step_id for step in research.macro_steps if step.macro_step_id in text]
    if selected:
        return selected
    numeric = set(re.findall(r"macro step(?:\(s\))?\s*\[['\"]?(\d+)", text, re.I))
    numeric.update(re.findall(r'"(?:macro_steps|source_macro_steps)"\s*:\s*\[\s*"?(\d+)', text))
    if numeric:
        return [step.macro_step_id for step in research.macro_steps if str(step.sequence) in numeric]
    # Fail closed: an unmappable verdict without a precise source binding is
    # not allowed to masquerade as an exact terminal diagnosis.
    return []


def device_result_to_v2(
    result: Dict[str, Any],
    research: ResearchActionPackageV2,
    *,
    capability_snapshot_id: str = "",
) -> DeviceWorkflowPackageV2:
    workflow = _mapping(result.get("workflow_json"))
    raw_steps = _items(workflow.get("steps"))
    research_ids = [step.macro_step_id for step in research.macro_steps]
    sequence_to_id = {str(step.sequence): step.macro_step_id for step in research.macro_steps}
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
    for plan_step in _items(result.get("device_plan")):
        raw_source = _source_macro_id(plan_step, "")
        source_macro = sequence_to_id.get(raw_source, raw_source)
        task_id = f"WT_{source_macro}"
        station = str(plan_step.get("station_code") or plan_step.get("workstation") or "")
        if task_id in requirements and station:
            candidates = requirements[task_id].candidate_station_codes
            if station not in candidates:
                candidates.append(station)
    device_steps: List[DeviceStepV2] = []
    skill_by_code = {
        str(item.get("station_code") or ""): item
        for item in _items(result.get("loaded_workstation_skills"))
    }
    for index, step in enumerate(raw_steps, start=1):
        source_macro = _source_macro_id(
            step,
            research_ids[min(index - 1, len(research_ids) - 1)],
        )
        source_macro = sequence_to_id.get(source_macro, source_macro)
        device_step_id = _step_id_from_workflow(step, index)
        station_code = str(
            step.get("station_code") or step.get("workstation") or "unknown_station"
        )
        task_id = f"WT_{source_macro}"
        requirements.setdefault(
            task_id,
            WorkstationRequirementV2(
                workstation_task_id=task_id,
                macro_step_id=source_macro,
                functional_role=str(step.get("operation") or "device operation"),
                required_capabilities=[str(step.get("operation") or "")],
                candidate_station_codes=[station_code],
            ),
        )
        if station_code not in requirements[task_id].candidate_station_codes:
            requirements[task_id].candidate_station_codes.append(station_code)
        raw_id = step.get("id") or step.get("station_id")
        station_id = int(raw_id) if isinstance(raw_id, (int, float)) else None
        manifest = skill_by_code.get(station_code, {})
        version_match = re.search(r"(?:_|-)(V\d+)$", station_code, re.I)
        device_steps.append(
            DeviceStepV2(
                device_step_id=device_step_id,
                source_macro_step_id=source_macro,
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
    repair = _mapping(result.get("workflow_repair_cycle"))
    rounds_used = min(10, int(repair.get("modification_count") or 0))
    step_hashes = {
        step.device_step_id: canonical_digest(step, prefix="device_step")
        for step in device_steps
    }
    issues = _validation_issues(result, list(step_hashes))
    terminal_ids = _terminal_macro_ids(result, research) if status == "terminal_unmappable" else []
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


def attach_device_v2_contract(
    result: Dict[str, Any], research: ResearchActionPackageV2
) -> Dict[str, Any]:
    updated = copy.deepcopy(result)
    package = device_result_to_v2(updated, research)
    payload = package.model_dump(mode="json", exclude_none=True)
    updated["contract_version"] = "v2"
    updated["device_workflow_package_v2"] = payload
    updated["device_package_id"] = package.device_package_id
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
        error = _mapping(updated.get("error_package"))
        error["type"] = "terminal_unmappable"
        error["macro_step_ids"] = package.terminal_macro_step_ids
        updated["error_package"] = error
    elif package.status == "human_review_required":
        updated["status"] = "manual_required"
        updated["feedback_type"] = "human_review_required"
        updated["feedback_route"] = "human"
        updated.setdefault("failure_scope", "device_workflow")
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
