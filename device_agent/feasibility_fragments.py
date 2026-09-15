"""Assemble bounded feasibility outputs without changing model chemistry decisions.

This module only validates and joins fragments.  A joined ``device_plan`` is
still an unapproved candidate: the caller must run the ordinary full-plan and
quantity gates before translation.  No partial plan is made dispatchable here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


class FeasibilityFragmentError(ValueError):
    """A planning fragment cannot be joined without losing or changing evidence."""


_PROGRESS = "_feasibility_fragments"
_TABLE_KEYS = {
    # Container numbers are allocated independently within each container type.
    # Treating the numeric slot alone as global incorrectly aliases, for example,
    # 进样瓶 #1 with 50ml耐热瓶 #1.
    "container_plan": ("容器类型", "容器编号"),
    "reagent_slot_plan": ("工作站", "原液编号"),
    "quantity_adjustments": ("adjustment_id",),
    "quantity_requirement_dispositions": ("source_macro_step", "requirement_index"),
    "batch_plan": ("batch_id",),
    "material_transitions": ("transition_id",),
}
_LIST_FIELDS = {"device_plan", "temporal_adaptations", "offline_handoffs"}
_ACCEPTED = {"device_plan", "success"}
_MANUAL = {"manual_required", "human_review_required"}
_CONTINUABLE = _ACCEPTED | _MANUAL
_TERMINAL = {
    "feasibility_error", "manual_required", "human_review_required", "failed",
    "device_internal_error", "terminal_unmappable",
}
_ALLOWED = set(_TABLE_KEYS) | _LIST_FIELDS | {
    "status", "feasibility", "macro_plan_summary", "sample_control_matrix",
    "device_self_check", "material_ledger", "requires_scientific_review",
    "quantity_contract_required", "quantity_audit", "pending_quantity_human_review",
    "reused_plan_steps", "prior_record_updates", "feedback_type", "feedback_route",
    "failure_scope", "failure_stage", "device_capability_summary",
    "recommendation_to_research_agent", "error_package", "blocking_constraints",
}
_FORBIDDEN = {
    "workflow", "workflow_json", "workflow_txt", "dispatch_payload",
    "dispatch_validation", "feasibility_accepted", "feasibility_certificate",
    "trusted_human_quantity_approvals", "human_quantity_approvals",
}


def _fail(path: str, message: str) -> None:
    raise FeasibilityFragmentError(f"{path}: {message}")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FeasibilityFragmentError("fragment must contain finite JSON values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _same(left: Any, right: Any) -> bool:
    # Python equality would otherwise accept True as the integer 1.
    return _json(left) == _json(right)


def _scalar(value: Any, path: str, *, zero: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail(path, "expected a scalar string/integer identifier")
    key = str(value).strip()
    if not key or (isinstance(value, int) and value < (0 if zero else 1)):
        _fail(path, "expected a nonempty valid identifier")
    return key


def _records(value: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail(path, "expected an array")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(f"{path}[{index}]", "expected an object")
    return value


def _append_unique(previous: list[Any], incoming: list[Any]) -> list[Any]:
    output = copy.deepcopy(previous)
    seen = {_json(item) for item in output}
    for item in incoming:
        key = _json(item)
        if key not in seen:
            output.append(copy.deepcopy(item))
            seen.add(key)
    return output


def _key(record: dict[str, Any], fields: tuple[str, ...], path: str) -> tuple[str, ...]:
    if "requirement_index" in fields:
        index = record.get("requirement_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            _fail(path + ".requirement_index", "expected a nonnegative integer")
    return tuple(_scalar(record.get(field), f"{path}.{field}", zero=field == "requirement_index") for field in fields)


def _merge_table(previous: Any, incoming: Any, fields: tuple[str, ...], path: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    indexed: dict[tuple[str, ...], dict[str, Any]] = {}
    for origin, records in (("prior", previous), ("fragment", incoming)):
        for index, record in enumerate(_records(records, path)):
            key = _key(record, fields, f"{path}[{index}]")
            if key in indexed:
                if not _same(indexed[key], record):
                    _fail(path, f"conflicting {origin} record for identifier {key}; no overwrite allowed")
                continue
            item = copy.deepcopy(record)
            indexed[key] = item
            output.append(item)
    return output


def _sources(record: dict[str, Any], path: str, macro_ids: list[str]) -> list[str]:
    primary = record.get("source_macro_step")
    many = record.get("source_macro_steps")
    if primary is None and many is None:
        _fail(path, "missing source_macro_step/source_macro_steps")
    sources = [] if many is None else many
    if not isinstance(sources, list):
        _fail(f"{path}.source_macro_steps", "expected an array")
    sources = [_scalar(value, f"{path}.source_macro_steps") for value in sources]
    if primary is not None:
        scalar = _scalar(primary, f"{path}.source_macro_step")
        if not sources:
            sources = [scalar]
        elif sources[0] != scalar:
            _fail(path, "primary source must equal the first source_macro_steps entry")
    if not sources or len(set(sources)) != len(sources):
        _fail(path, "source list must be nonempty and contain unique IDs")
    if any(source not in macro_ids for source in sources):
        _fail(path, "source references an unknown Research macro ID")
    if sources != sorted(sources, key=macro_ids.index):
        _fail(path, "source_macro_steps must follow frozen Research order")
    return sources


def _validate_plan_steps(steps: Any, macro_ids: list[str]) -> list[dict[str, Any]]:
    records = _records(steps, "device_plan")
    for index, step in enumerate(records, start=1):
        identifier = step.get("plan_step")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier != index:
            _fail(f"device_plan[{index - 1}].plan_step", f"expected global sequential integer {index}")
        _sources(step, f"device_plan[{index - 1}]", macro_ids)
    return records


def _merge_metadata(previous: Any, incoming: Any, path: str) -> dict[str, Any]:
    if not isinstance(previous, dict) or not isinstance(incoming, dict):
        _fail(path, "expected an object")
    output = copy.deepcopy(previous)
    for key, value in incoming.items():
        if key not in output or _same(output[key], value):
            output[key] = copy.deepcopy(value)
        elif isinstance(value, list) and isinstance(output[key], list):
            output[key] = _append_unique(output[key], value)
        elif isinstance(value, bool) and isinstance(output[key], bool):
            # Feasibility cannot become true by a later fragment; review cannot disappear.
            output[key] = output[key] and value if key == "is_feasible" else output[key] or value
        elif isinstance(value, str) and isinstance(output[key], str):
            output[key] = output[key] + "\n" + value
        else:
            _fail(f"{path}.{key}", "conflicting metadata cannot be overwritten")
    return output


def _apply_updates(candidate: dict[str, Any], updates: Any) -> None:
    for index, update in enumerate(_records(updates, "prior_record_updates")):
        path = f"prior_record_updates[{index}]"
        if set(update) - {"table", "id", "consumer_ids", "allocation"}:
            _fail(path, "only additive batch consumer_ids/allocation may be updated")
        if update.get("table") != "batch_plan":
            _fail(path, "only batch_plan updates are supported; frozen records cannot be replaced")
        identifier = _scalar(update.get("id"), path + ".id")
        matches = [item for item in candidate.get("batch_plan", []) if _scalar(item.get("batch_id"), "batch_id") == identifier]
        if len(matches) != 1:
            _fail(path, "update must reference exactly one prior batch")
        record = matches[0]
        current_consumers = record.get("consumer_ids", [])
        current_allocations = record.get("allocation", {})
        additions = update.get("consumer_ids", [])
        allocations = update.get("allocation", {})
        if not isinstance(current_consumers, list) or not isinstance(additions, list):
            _fail(path, "consumer_ids must be arrays")
        existing_ids = [_scalar(item, path + ".consumer_ids") for item in current_consumers]
        added_ids = [_scalar(item, path + ".consumer_ids") for item in additions]
        if len(set(existing_ids)) != len(existing_ids) or len(set(added_ids)) != len(added_ids):
            _fail(path, "duplicate consumer identifiers")
        if record.get("quantity_mode") == "whole_batch":
            if current_allocations not in (None, {}, []) or allocations not in (None, {}, []):
                _fail(path, "whole_batch cannot carry numeric allocations")
            if len(set(existing_ids + added_ids)) > 1:
                _fail(path, "whole_batch may have at most one total consumer")
            record["consumer_ids"] = _append_unique(current_consumers, additions)
            record.pop("allocation", None)
            continue
        if not isinstance(current_allocations, dict) or not isinstance(allocations, dict):
            _fail(path, "additive updates require dictionary allocation records")
        if set(added_ids) != set(allocations):
            _fail(path, "new consumer_ids must exactly match supplied allocation keys")
        if set(existing_ids) != set(current_allocations):
            _fail(path, "prior batch consumers and allocation keys disagree")
        for consumer, allocation in allocations.items():
            if consumer in current_allocations and not _same(current_allocations[consumer], allocation):
                _fail(path, f"cannot change existing allocation for {consumer}")
            current_allocations[consumer] = copy.deepcopy(allocation)
        record["consumer_ids"] = _append_unique(current_consumers, additions)
        record["allocation"] = current_allocations


def _check_references(candidate: dict[str, Any], macro_ids: list[str]) -> None:
    plan_ids = {str(item["plan_step"]) for item in candidate["device_plan"]}
    batches = {str(item["batch_id"]) for item in candidate.get("batch_plan", [])}
    transitions = {str(item["transition_id"]) for item in candidate.get("material_transitions", [])}
    adjustments = {str(item["adjustment_id"]) for item in candidate.get("quantity_adjustments", [])}
    list_refs = {
        "source_plan_steps": plan_ids, "processing_step_refs": plan_ids,
        "source_macro_steps": set(macro_ids), "parent_batch_ids": batches,
        "child_batch_ids": batches, "material_transition_ids": transitions,
    }
    scalar_refs = {
        "source_plan_step": plan_ids, "plan_step": plan_ids,
        "source_macro_step": set(macro_ids), "batch_id": batches,
        "parent_batch_id": batches,
    }

    def walk(value: Any, path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}"
                if key in list_refs:
                    if not isinstance(item, list):
                        _fail(child_path, "expected a reference array")
                    for reference in item:
                        if _scalar(reference, child_path) not in list_refs[key]:
                            _fail(child_path, f"dangling reference {reference!r}")
                elif key in scalar_refs and item not in (None, ""):
                    if _scalar(item, child_path) not in scalar_refs[key]:
                        _fail(child_path, f"dangling reference {item!r}")
                elif key in {"source_refs", "consumer_id", "justification_evidence_refs"}:
                    refs = item if isinstance(item, list) else [item]
                    for reference in refs:
                        if not isinstance(reference, str):
                            continue
                        for prefix, allowed in (("material_transition:", transitions), ("quantity_adjustment:", adjustments), ("macro_step:", set(macro_ids))):
                            if reference.startswith(prefix) and reference[len(prefix):] not in allowed:
                                _fail(child_path, f"dangling reference {reference!r}")
                walk(item, child_path)

    # Scientific matrix/provenance content is not interpreted as Device references.
    for field in (*_TABLE_KEYS, "device_plan", "offline_handoffs", "material_ledger"):
        walk(candidate.get(field, []), field)


def _any_review(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("requires_scientific_review") is True or any(_any_review(item) for item in value.values())
    return isinstance(value, list) and any(_any_review(item) for item in value)


def _check_arguments(aggregate: Any, current_macro_id: Any, all_macro_ids: Any) -> tuple[str, list[str]]:
    if not isinstance(aggregate, dict) or not isinstance(all_macro_ids, list):
        _fail("arguments", "aggregate must be an object and macro IDs an array")
    macros = [_scalar(item, "all_macro_ids") for item in all_macro_ids]
    if not macros or len(set(macros)) != len(macros):
        _fail("all_macro_ids", "expected unique frozen Research IDs")
    current = _scalar(current_macro_id, "current_macro_id")
    progress = aggregate.get(_PROGRESS, {})
    if not isinstance(progress, dict) or not isinstance(progress.get("completed_macro_ids", []), list):
        _fail(_PROGRESS, "expected completed_macro_ids array")
    completed = progress.get("completed_macro_ids", [])
    if completed != macros[:len(completed)] or len(completed) >= len(macros) or macros[len(completed)] != current:
        _fail("current_macro_id", "fragments must follow frozen Research order exactly once")
    if aggregate and aggregate.get("status") not in _CONTINUABLE:
        _fail("aggregate.status", "a hard-terminal candidate cannot be resumed")
    return current, macros


def _compact_record(
    record: dict[str, Any],
    fields: tuple[str, ...],
    *,
    exact_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    def bounded(value: Any) -> Any:
        encoded = _json(value).encode("utf-8")
        if len(encoded) <= 4096:
            return copy.deepcopy(value)
        return {
            "oversize_value_omitted": True,
            "value_sha256": hashlib.sha256(encoded).hexdigest(),
            "json_bytes": len(encoded),
        }

    summary = {
        field: (
            copy.deepcopy(record[field])
            if field in exact_fields
            else bounded(record[field])
        )
        for field in fields
        if field in record
    }
    summary["record_sha256"] = _digest(record)
    return summary


def build_prefix_symbol_table(aggregate: dict[str, Any]) -> dict[str, Any]:
    """Expose immutable identifiers without resending the growing candidate.

    The full prefix stays local and is still passed to ``merge_fragment`` for
    deterministic reference and conservation checks.  The model only needs a
    symbol table in order to reference prior objects without redefining them.
    """
    if not isinstance(aggregate, dict):
        _fail("aggregate", "expected an object")
    plan_fields = (
        "plan_step", "workstation", "operation", "操作", "sample_id",
        "source_macro_step", "source_macro_steps", "batch_id",
        "container_type", "container_id", "容器类型", "容器编号",
        "objective", "parameters", "source_reagent_identity",
        "material_identity_id", "input_batch_ids", "output_batch_ids",
        "material_event_kind", "material_transition_ids",
        "operation_intent", "key_values", "containers", "dependencies",
        "source_material_identity_ids", "sample_ids", "batch_ids",
    )
    table_fields = {
        "container_plan": (
            "容器类型", "容器编号", "sample_id", "batch_id", "用途",
            "lid_state", "盖状态", "source_macro_step", "source_macro_steps",
            "lifecycle", "生命周期", "capacity", "max_volume",
            "material_identity_id",
        ),
        "reagent_slot_plan": (
            "工作站", "原液编号", "试剂", "试剂名称", "sample_id",
            "source_macro_step", "source_macro_steps", "material_identity_id",
            "concentration", "volume", "quantity",
        ),
        "quantity_adjustments": (
            "adjustment_id", "source_macro_step", "requirement_index",
            "before", "after", "formula", "source", "reason",
            "requires_scientific_review",
        ),
        "quantity_requirement_dispositions": (
            "source_macro_step", "requirement_index", "disposition",
            "decision", "plan_step", "workstation", "operation", "parameter",
            "evidence_refs", "requires_scientific_review",
        ),
        "batch_plan": (
            "batch_id", "sample_id", "quantity_mode", "consumer_ids",
            "source_macro_step", "source_macro_steps",
            "material_id", "material_identity_id", "research_material_identity_id",
            "is_root_batch", "parent_batch_id", "parent_batch_ids",
            "total_quantity", "parent_quantity", "per_batch_quantity",
            "allocation", "quantity_scope", "multiplicity", "multiplicity_ref",
            "operation_repeat_ref", "operation_repeat_count", "transition_kind",
            "source_plan_steps", "research_source_refs",
        ),
        "material_transitions": (
            "transition_id", "sample_id", "parent_batch_id", "child_batch_ids",
            "source_macro_step", "source_macro_steps",
            "parent_batch_ids", "material_id", "material_identity_id",
            "transition_kind", "source_plan_steps", "quantity_basis",
            "input_allocations", "output_allocations",
        ),
    }
    symbols: dict[str, Any] = {
        "candidate_sha256": _digest(aggregate),
        "completed_macro_ids": copy.deepcopy(
            aggregate.get(_PROGRESS, {}).get("completed_macro_ids", [])
            if isinstance(aggregate.get(_PROGRESS), dict)
            else []
        ),
        "device_plan": [
            _compact_record(
                record,
                plan_fields,
                exact_fields=(
                    "objective", "parameters", "source_reagent_identity",
                    "material_identity_id", "input_batch_ids", "output_batch_ids",
                    "material_event_kind", "material_transition_ids",
                    "operation_intent", "key_values", "containers", "dependencies",
                    "source_material_identity_ids", "sample_ids", "batch_ids",
                ),
            )
            for record in aggregate.get("device_plan", [])
            if isinstance(record, dict)
        ],
    }
    conservation_fields = {
        "container_plan": (
            "lid_state", "盖状态", "lifecycle", "生命周期", "capacity",
            "max_volume", "material_identity_id",
        ),
        "reagent_slot_plan": (
            "material_identity_id", "concentration", "volume", "quantity",
        ),
        "quantity_adjustments": (
            "before", "after", "formula", "source",
        ),
        "quantity_requirement_dispositions": (
            "decision", "disposition", "parameter", "evidence_refs",
        ),
        "batch_plan": (
            "total_quantity", "parent_quantity", "per_batch_quantity",
            "allocation", "consumer_ids", "quantity_scope", "multiplicity",
            "multiplicity_ref", "operation_repeat_ref", "operation_repeat_count",
        ),
        "material_transitions": (
            "quantity_basis", "input_allocations", "output_allocations",
        ),
    }
    for table, fields in table_fields.items():
        symbols[table] = [
            _compact_record(
                record,
                fields,
                exact_fields=conservation_fields.get(table, ()),
            )
            for record in aggregate.get(table, [])
            if isinstance(record, dict)
        ]
    ledger = aggregate.get("material_ledger", {})
    ledger_entries = ledger.get("entries", []) if isinstance(ledger, dict) else []
    ledger_fields = (
        "entry_id", "batch_id", "sample_id", "material_id",
        "material_identity_id", "consumer_id", "produced", "consumed",
        "remaining", "allocation", "source_refs", "processing_step_refs",
        "source_macro_step", "source_macro_steps",
    )
    symbols["material_ledger"] = {
        "entries": [
            _compact_record(
                record,
                ledger_fields,
                exact_fields=(
                    "produced", "consumed", "remaining", "allocation",
                    "source_refs", "processing_step_refs",
                ),
            )
            for record in ledger_entries
            if isinstance(record, dict)
        ]
    }
    offline_fields = (
        "handoff_id", "offline_handoff_id", "name", "operation",
        "measurement", "instrument", "semantic_classification",
        "sample_id", "sample_ids", "material_id", "material_identity_id",
        "source_macro_step", "source_macro_steps", "source_plan_step",
        "source_plan_steps", "input", "inputs", "input_materials",
        "output", "outputs", "output_materials", "required_return_data",
        "required_returns", "intermediate_returns", "return_contract",
        "return_to", "dependencies", "depends_on", "dependency_refs",
        "reason", "requires_scientific_review",
    )
    symbols["offline_handoffs"] = [
        _compact_record(record, offline_fields, exact_fields=offline_fields)
        for record in aggregate.get("offline_handoffs", [])
        if isinstance(record, dict)
    ]
    temporal_fields = (
        "adaptation_id", "source_macro_step", "source_macro_steps",
        "source_plan_step", "source_plan_steps", "sample_id", "sample_ids",
        "batch_id", "batch_ids", "material_id", "material_identity_id",
        "workstation", "operation", "original_requirement",
        "adaptation_schedule", "schedule", "timing", "duration",
        "dependencies", "depends_on", "dependency_refs", "reason",
        "execution_fidelity", "requires_scientific_review",
    )
    symbols["temporal_adaptations"] = [
        _compact_record(record, temporal_fields, exact_fields=temporal_fields)
        for record in aggregate.get("temporal_adaptations", [])
        if isinstance(record, dict)
    ]
    return symbols


def build_fragment_request_context(
    research_handoff: dict[str, Any],
    semantic_analysis: dict[str, Any],
    aggregate: dict[str, Any],
    current_macro_id: str,
    all_macro_ids: list[str],
    matrix: Any,
) -> dict[str, Any]:
    """Build the bounded scientific context for one feasibility fragment."""
    current, macros = _check_arguments(aggregate, current_macro_id, all_macro_ids)
    steps = research_handoff.get("macro_action_steps")
    if not isinstance(steps, list):
        _fail("research_handoff.macro_action_steps", "expected an array")

    def macro_id(step: dict[str, Any], index: int) -> str:
        # Keep this identical to SingleDeviceAgent._semantic_macro_id so the
        # prompt view, semantic contract and deterministic merger share one
        # namespace even when Research also supplies descriptive IDs.
        return str(step.get("步骤序号", step.get("step", index))).strip()

    indexed = {
        macro_id(step, index): step
        for index, step in enumerate(steps, start=1)
        if isinstance(step, dict)
    }
    if current not in indexed:
        _fail("current_macro_id", "not found in research_handoff")
    outline_fields = (
        "macro_step_id", "logical_step_id", "macro_action_id", "步骤序号", "step",
        "操作", "operation", "sample_id", "observation_point_id",
        "container_requirements", "material_inputs", "material_outputs",
    )
    outline = [
        _compact_record(indexed[identifier], outline_fields)
        for identifier in macros
        if identifier in indexed
    ]
    assessments = semantic_analysis.get("macro_step_assessments", [])
    relevant_assessments = [
        copy.deepcopy(item)
        for item in assessments
        if isinstance(item, dict)
        and str(item.get("source_macro_step") or "").strip() == current
    ] if isinstance(assessments, list) else []
    remaining_ids = macros[macros.index(current):]
    dependency_fields = outline_fields + (
        "试剂/对象", "parameters", "参数", "quantity_requirements",
        "intermediate_returns", "required_returns", "dependencies",
        "depends_on", "source_path", "来源",
    )
    remaining_dependencies = [
        _compact_record(
            indexed[identifier],
            dependency_fields,
            exact_fields=(
                "参数", "parameters", "quantity_requirements",
                "intermediate_returns", "required_returns", "dependencies",
                "depends_on", "container_requirements", "material_inputs",
                "material_outputs",
            ),
        )
        for identifier in remaining_ids
        if identifier in indexed
    ]
    semantic_dependency_fields = (
        "source_macro_step", "required_capabilities", "quantity_semantics",
        "material_identities", "core_chemistry", "observation_only",
        "joint_requirements",
    )
    remaining_semantic_dependencies = [
        _compact_record(
            item,
            semantic_dependency_fields,
            exact_fields=(
                "required_capabilities", "quantity_semantics",
                "material_identities", "joint_requirements",
            ),
        )
        for item in assessments
        if isinstance(item, dict)
        and str(item.get("source_macro_step") or "").strip() in remaining_ids
    ] if isinstance(assessments, list) else []
    task = research_handoff.get("task")
    task_view = {
        field: copy.deepcopy(task[field])
        for field in ("query", "stage_route", "current_stage", "current_stage_plan")
        if isinstance(task, dict) and field in task
    }
    context = {
        "context_contract": "feasibility_fragment_context_v1",
        "current_macro_id": current,
        "task": task_view,
        "current_macro": copy.deepcopy(indexed[current]),
        "macro_outline": outline,
        "remaining_dependency_view": remaining_dependencies,
        "frozen_sample_control_matrix": copy.deepcopy(matrix),
        "semantic_assessment": relevant_assessments,
        "remaining_semantic_dependency_view": remaining_semantic_dependencies,
        "material_identity_registry": copy.deepcopy(
            semantic_analysis.get("material_identity_registry", [])
        ),
        "semantic_analysis_sha256": _digest(semantic_analysis),
        "device_agent_contract": copy.deepcopy(
            research_handoff.get("device_agent_contract", {})
        ),
        "accepted_prefix_symbols": build_prefix_symbol_table(aggregate),
        "research_handoff_sha256": _digest(research_handoff),
    }
    return context


def build_fragment_instruction(aggregate: dict[str, Any], current_macro_id: str, all_macro_ids: list[str], matrix: Any) -> str:
    """Return the bounded-output directive for one macro fragment."""
    current, macros = _check_arguments(aggregate, current_macro_id, all_macro_ids)
    first_step = len(aggregate.get("device_plan", [])) + 1
    context = {
        "current_macro_id": current,
        "all_macro_ids": macros,
        "first_new_plan_step": first_step,
        "frozen_sample_control_matrix": matrix,
        "accepted_prefix_symbols": build_prefix_symbol_table(aggregate),
    }
    return (
        "## 分段可行性输出合同（本次只规划一个宏步骤）\n"
        "当前宏步骤的完整 Research 字段、全局宏步骤提纲、当前语义合同与工作站真源仍是依据；"
        "完整输入的摘要用于绑定与防漂移，不得重编号 Research 或修改 source_path。"
        "只输出当前 macro 的新增 device_plan 步骤及必需 sidecar，不重复输出之前的计划。"
        f"新增 plan_step 从 {first_step} 开始连续整数；source_macro_step 必须为 {current}，"
        "source_macro_steps 按 Research 顺序，可包含同一物理操作同时覆盖的后续 macro。"
        "如之前物理步骤已覆盖当前 macro，device_plan 可为空，必须用 reused_plan_steps 数组"
        "明确引用那些既存 plan_step；不得复制执行。离线观测可用带正确来源的 offline_handoffs 覆盖。\n"
        "每个 sidecar ID 全局唯一；重复 ID 只接受逐字段相同记录，禁止改写既有数量、物料、样品、"
        "来源、容器身份。所有 plan/batch/transition 引用必须指向前序或本次定义的记录。"
        "提前考虑下游容器兼容性、整批处理与消费边界，不得用重复 root 批次创造库存。"
        "sample_control_matrix 若输出，必须完整逐字回显冻结矩阵。\n"
        "必要时可通过 prior_record_updates 只追加既有 batch 的消费者："
        '[{"table":"batch_plan","id":"已有 batch_id","consumer_ids":["new_consumer"],'
        '"allocation":{"new_consumer":{"value":1,"unit":"mL"}}}]。'
        "必须保持已有分配不变，禁止改产量、来源、身份或 root 标记；程序合并后做全局守恒检查。"
        "quantity_mode=whole_batch 的既有 batch 只允许追加一个总消费者，更新时省略 allocation，"
        "禁止虚构质量、体积或拆给多个消费者。"
        "容器 lifecycle 等字段如需贯穿后续步骤，请首次声明完整生命周期，不允许后续覆盖。\n"
        "需科学复核的问题必须保留 requires_scientific_review/pending_quantity_human_review；"
        "硬阻塞或人工判断仍用原始终止 status，禁止将不完整路线宣称成功。"
        "不得输出 workflow_json/workflow_txt/dispatch_payload/certificate。"
        "这只是未批准的计划片段；只有所有片段通过现有全局检查才可翻译。\n"
        + _json(context)
    )


def merge_fragment(aggregate: dict[str, Any], fragment: dict[str, Any], current_macro_id: str, all_macro_ids: list[str], matrix: Any) -> dict[str, Any]:
    """Join one fragment atomically; raise on ambiguity without mutating inputs.

    Negative/manual status stays nonaccepted.  A manual quantity candidate may
    collect remaining fragments, but cannot silently become an accepted plan.
    """
    current, macros = _check_arguments(aggregate, current_macro_id, all_macro_ids)
    if not isinstance(fragment, dict):
        _fail("fragment", "expected an object")
    _json(fragment)
    forbidden = set(fragment) & _FORBIDDEN
    unknown = set(fragment) - _ALLOWED
    if forbidden or unknown:
        _fail("fragment", f"unsupported/forbidden output fields: {sorted(forbidden or unknown)}")
    status = fragment.get("status")
    if not isinstance(status, str) or status not in _ACCEPTED | _TERMINAL:
        _fail("status", "expected an explicit supported feasibility status")
    if "sample_control_matrix" in fragment and not _same(fragment["sample_control_matrix"], matrix):
        _fail("sample_control_matrix", "differs from frozen Research matrix")
    if aggregate and not _same(aggregate.get("sample_control_matrix"), matrix):
        _fail("aggregate.sample_control_matrix", "differs from frozen Research matrix")
    candidate = copy.deepcopy(aggregate)
    candidate.setdefault("device_plan", [])
    candidate["sample_control_matrix"] = copy.deepcopy(matrix)
    candidate["status"] = "device_plan" if status in _ACCEPTED else status
    if status in _ACCEPTED and aggregate.get("status") in _MANUAL:
        candidate["status"] = aggregate["status"]
    _validate_plan_steps(candidate["device_plan"], macros)
    if status in _CONTINUABLE and "device_plan" not in fragment:
        _fail("device_plan", "continuing fragment must explicitly supply an array")
    new_steps = _records(fragment.get("device_plan", []), "device_plan")
    previous_count = len(candidate["device_plan"])
    for index, step in enumerate(new_steps):
        sources = _sources(step, f"device_plan[{index}]", macros)
        if sources[0] != current:
            _fail(f"device_plan[{index}].source_macro_step", "new physical step must have the current primary source")
    candidate["device_plan"].extend(copy.deepcopy(new_steps))
    _validate_plan_steps(candidate["device_plan"], macros)
    reused = fragment.get("reused_plan_steps", [])
    if not isinstance(reused, list):
        _fail("reused_plan_steps", "expected an array")
    reused_ids: set[int] = set()
    for identifier in reused:
        if isinstance(identifier, bool) or not isinstance(identifier, int) or not 1 <= identifier <= previous_count:
            _fail("reused_plan_steps", "must reference a prior global integer plan_step")
        if identifier in reused_ids:
            _fail("reused_plan_steps", "duplicate prior step reference")
        reused_ids.add(identifier)
        if current not in _sources(candidate["device_plan"][identifier - 1], "reused_plan_steps", macros):
            _fail("reused_plan_steps", "prior physical step does not cover the current macro")
    _apply_updates(candidate, fragment.get("prior_record_updates", []))
    for field, fields in _TABLE_KEYS.items():
        if field in fragment or field in candidate:
            candidate[field] = _merge_table(candidate.get(field, []), fragment.get(field, []), fields, field)
    if "material_ledger" in fragment or "material_ledger" in candidate:
        previous = candidate.get("material_ledger", {})
        incoming = fragment.get("material_ledger", {})
        if not isinstance(previous, dict) or not isinstance(incoming, dict):
            _fail("material_ledger", "expected an object")
        if set(incoming) - {"entries"}:
            _fail("material_ledger", "only entries may be emitted")
        candidate["material_ledger"] = {"entries": _merge_table(previous.get("entries", []), incoming.get("entries", []), ("entry_id",), "material_ledger.entries")}
    for field in ("offline_handoffs", "temporal_adaptations"):
        if field in fragment or field in candidate:
            candidate[field] = _append_unique(_records(candidate.get(field, []), field), _records(fragment.get(field, []), field))
    for index, handoff in enumerate(candidate.get("offline_handoffs", [])):
        _sources(handoff, f"offline_handoffs[{index}]", macros)
    covered = bool(new_steps or reused_ids) or any(current in _sources(item, "offline_handoffs", macros) for item in candidate.get("offline_handoffs", []))
    if status in _CONTINUABLE and not covered:
        _fail("coverage", "current macro has no new/reused physical step or sourced offline handoff")
    for field in ("feasibility", "device_self_check", "device_capability_summary"):
        if field in fragment:
            candidate[field] = _merge_metadata(candidate.get(field, {}), fragment[field], field)
    feasibility = candidate.get("feasibility", {})
    if "blocking_constraints" in feasibility and not isinstance(feasibility["blocking_constraints"], list):
        _fail("feasibility.blocking_constraints", "expected an array")
    if "is_feasible" in feasibility and not isinstance(feasibility["is_feasible"], bool):
        _fail("feasibility.is_feasible", "expected a boolean")
    if candidate["status"] in _CONTINUABLE and (feasibility.get("is_feasible") is False or feasibility.get("blocking_constraints")):
        candidate["status"] = "feasibility_error"
        candidate["feedback_type"] = "device_feasibility_error"
    for field in ("requires_scientific_review", "quantity_contract_required"):
        if field in fragment and not isinstance(fragment[field], bool):
            _fail(field, "expected a boolean")
        if field in fragment or field in candidate:
            candidate[field] = bool(candidate.get(field, False) or fragment.get(field, False))
    candidate["requires_scientific_review"] = bool(candidate.get("requires_scientific_review") or _any_review(fragment))
    for field in ("quantity_audit", "pending_quantity_human_review"):
        if field in fragment:
            incoming = fragment[field]
            if not isinstance(incoming, dict):
                _fail(field, "expected an object")
            previous = candidate.get(field, {})
            merged = _merge_metadata({key: value for key, value in previous.items() if key != "status"}, {key: value for key, value in incoming.items() if key != "status"}, field)
            ranks = {"passed": 0, "not_required": 0, "human_review_required": 1, "failed": 2}
            statuses = [item["status"] for item in (previous, incoming) if "status" in item]
            if any(value not in ranks for value in statuses):
                _fail(field + ".status", "unsupported quantity-audit status")
            if statuses:
                merged["status"] = max(statuses, key=ranks.__getitem__)
            candidate[field] = merged
    # Both existing downstream entry points consume one of these fields, and
    # Case-C promotion historically copies quantity_audit onto pending review.
    # Keep one canonical issue set in both locations so neither path loses an
    # earlier human finding when another chunk contributes a different issue.
    audits = [candidate[field] for field in ("quantity_audit", "pending_quantity_human_review") if isinstance(candidate.get(field), dict)]
    if "pending_quantity_human_review" in candidate or any(audit.get("status") == "human_review_required" for audit in audits):
        canonical: dict[str, Any] = {}
        statuses: list[str] = []
        ranks = {"passed": 0, "not_required": 0, "human_review_required": 1, "failed": 2}
        for audit in audits:
            canonical = _merge_metadata(canonical, {key: value for key, value in audit.items() if key != "status"}, "quantity_review")
            if "status" in audit:
                statuses.append(audit["status"])
        if statuses:
            canonical["status"] = max(statuses, key=ranks.__getitem__)
        candidate["quantity_audit"] = copy.deepcopy(canonical)
        candidate["pending_quantity_human_review"] = copy.deepcopy(canonical)
    if "macro_plan_summary" in fragment:
        if not isinstance(fragment["macro_plan_summary"], str):
            _fail("macro_plan_summary", "expected a string")
        previous = candidate.get("macro_plan_summary", "")
        candidate["macro_plan_summary"] = "\n".join(value for value in (previous, fragment["macro_plan_summary"]) if value)
    if "blocking_constraints" in fragment:
        if not isinstance(fragment["blocking_constraints"], list):
            _fail("blocking_constraints", "expected an array")
        candidate["blocking_constraints"] = _append_unique(candidate.get("blocking_constraints", []), fragment["blocking_constraints"])
        if candidate["blocking_constraints"] and candidate["status"] in _CONTINUABLE:
            candidate["status"] = "feasibility_error"
    if status not in _ACCEPTED:
        for field in ("feedback_type", "feedback_route", "failure_scope", "failure_stage", "recommendation_to_research_agent", "error_package"):
            if field in fragment:
                candidate[field] = copy.deepcopy(fragment[field])
    _check_references(candidate, macros)
    completed = list(aggregate.get(_PROGRESS, {}).get("completed_macro_ids", []))
    if candidate["status"] in _CONTINUABLE:
        completed.append(current)
    candidate[_PROGRESS] = {"completed_macro_ids": completed}
    return candidate
