"""Offline tests for immutable-prefix feasibility fragment assembly."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feasibility_fragments import (
    FeasibilityFragmentError,
    build_fragment_instruction,
    build_fragment_request_context,
    build_prefix_symbol_table,
    merge_fragment,
)


MACROS = ["10", "20", "30"]
MATRIX = [
    {"sample_id": "NiFe_control", "role": "control", "variables": {"activation_minutes": 0}},
    {"sample_id": "NiFe_activated", "role": "experimental", "variables": {"activation_minutes": 30}},
]


def step(number, source, sources=None):
    return {
        "plan_step": number,
        "workstation": "Chemical_Reactor_Station_V1",
        "operation_intent": "keep frozen scientific operation",
        "source_macro_step": source,
        "source_macro_steps": sources if sources is not None else [source],
        "key_values": {"temperature": "120 C"},
        "containers": {"容器类型": "进样瓶", "容器编号": [1]},
        "source_material_identity_ids": ["frozen_identity"],
    }


def fragment(source, number, **extra):
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_plan": [step(number, source)],
        **extra,
    }


def merge(previous, new, source):
    return merge_fragment(previous, new, str(source), MACROS, MATRIX)


def test_sequential_fragments_keep_original_sources_frozen_matrix_and_inputs():
    first = fragment(10, 1, container_plan=[{"容器编号": 1, "容器类型": "进样瓶", "sample_id": "NiFe_control", "lifecycle": "receive -> react -> wash -> output"}])
    frozen_first = copy.deepcopy(first)
    prefix = merge({}, first, 10)
    frozen_prefix = copy.deepcopy(prefix)
    second = fragment(20, 2, quantity_requirement_dispositions=[{
        "source_macro_step": 20, "requirement_index": 0, "plan_step": 2,
        "decision": "retain_as_scientific_target", "reason": "frozen target",
        "evidence_refs": ["macro_action_steps[1].参数"],
    }])
    result = merge(prefix, second, 20)
    assert [item["source_macro_step"] for item in result["device_plan"]] == [10, 20]
    assert [item["plan_step"] for item in result["device_plan"]] == [1, 2]
    assert result["sample_control_matrix"] == MATRIX
    assert result["quantity_requirement_dispositions"][0]["evidence_refs"] == ["macro_action_steps[1].参数"]
    assert result["_feasibility_fragments"]["completed_macro_ids"] == ["10", "20"]
    assert prefix == frozen_prefix and first == frozen_first
    assert "feasibility_certificate" not in result and "workflow_json" not in result


def test_fragment_prompt_uses_digest_backed_prefix_symbols_not_full_candidate():
    large_value = "PREFIX_BULK_SHOULD_NOT_BE_RESENT_" * 200
    first = fragment(10, 1, container_plan=[{
        "容器编号": 1,
        "容器类型": "进样瓶",
        "sample_id": "NiFe_control",
        "lifecycle": "receive -> react -> output",
        "unrelated_diagnostic_blob": large_value,
    }])
    prefix = merge({}, first, 10)

    instruction = build_fragment_instruction(prefix, "20", MACROS, MATRIX)
    symbols = build_prefix_symbol_table(prefix)

    assert "accepted_prefix_read_only" not in instruction
    assert large_value not in instruction
    assert symbols["candidate_sha256"]
    assert symbols["container_plan"][0]["容器类型"] == "进样瓶"
    assert symbols["container_plan"][0]["容器编号"] == 1
    assert symbols["container_plan"][0]["record_sha256"]


def test_fragment_request_context_excludes_unrelated_research_bulk():
    handoff = {
        "task": {"query": "NiFe LDH", "current_stage": "synthesis"},
        "macro_action_steps": [
            {
                "步骤序号": identifier,
                "macro_step_id": identifier,
                "操作": f"operation-{identifier}",
                "sample_id": "S",
            }
            for identifier in MACROS
        ],
        "research_context": {"survey_report": "UNRELATED_SURVEY_BULK_" * 500},
        "device_agent_contract": {"contract_version": "v2"},
    }
    semantic = {
        "macro_step_assessments": [
            {
                "source_macro_step": identifier,
                "reason": f"semantic-{identifier}",
                "required_capabilities": [{"category": f"capability-{identifier}"}],
                "joint_requirements": ([{
                    "type": "temperature_atmosphere",
                    "temperature": "900 C",
                    "atmosphere": "Ar",
                }] if identifier == "20" else []),
            }
            for identifier in MACROS
        ],
        "material_identity_registry": [{"identity_id": "mat-1", "name": "NiFe LDH"}],
    }
    handoff["macro_action_steps"][1].update({
        "参数": "900 C under Ar for 2 h",
        "quantity_requirements": [{"kind": "target_dose", "value": 2, "unit": "mL"}],
        "intermediate_returns": [{"name": "actual mass", "required_for_next_step": True}],
    })

    context = build_fragment_request_context(
        handoff, semantic, {}, "10", MACROS, MATRIX,
    )
    serialized = json.dumps(context, ensure_ascii=False)

    assert "UNRELATED_SURVEY_BULK" not in serialized
    assert context["current_macro"]["macro_step_id"] == "10"
    assert context["semantic_assessment"] == [{
        "source_macro_step": "10",
        "reason": "semantic-10",
        "required_capabilities": [{"category": "capability-10"}],
        "joint_requirements": [],
    }]
    assert len(context["macro_outline"]) == 3
    downstream = context["remaining_dependency_view"][1]
    assert downstream["参数"] == "900 C under Ar for 2 h"
    assert downstream["quantity_requirements"][0]["value"] == 2
    assert downstream["intermediate_returns"][0]["required_for_next_step"] is True
    assert context["remaining_semantic_dependency_view"][1][
        "required_capabilities"
    ] == [{"category": "capability-20"}]
    assert context["remaining_semantic_dependency_view"][1][
        "joint_requirements"
    ][0]["atmosphere"] == "Ar"
    assert context["research_handoff_sha256"]


def test_prefix_symbols_preserve_conservation_fields_without_resending_bulk():
    bulk = "DO_NOT_RESEND_THIS_LIFECYCLE_BULK_" * 300
    first = fragment(
        10,
        1,
        batch_plan=[{
            "batch_id": "root",
            "quantity_mode": "numeric_inventory",
            "total_quantity": {"value": 10, "unit": "mL"},
            "consumer_ids": ["consumer-1"],
            "allocation": {"consumer-1": {"value": 4, "unit": "mL"}},
            "material_identity_id": "mat-1",
        }],
        material_ledger={"entries": [{
            "entry_id": "root-produced",
            "batch_id": "root",
            "produced": {"value": 10, "unit": "mL"},
            "processing_step_refs": [1],
        }]},
        container_plan=[{
            "容器类型": "进样瓶", "容器编号": 1,
            "lifecycle": "receive -> react -> output",
            "unrelated_diagnostic_blob": bulk,
        }],
    )
    symbols = build_prefix_symbol_table(merge({}, first, 10))

    batch = symbols["batch_plan"][0]
    assert batch["total_quantity"] == {"value": 10, "unit": "mL"}
    assert batch["allocation"] == {"consumer-1": {"value": 4, "unit": "mL"}}
    assert symbols["material_ledger"]["entries"][0]["produced"]["value"] == 10
    assert symbols["container_plan"][0]["lifecycle"] == "receive -> react -> output"
    assert bulk not in json.dumps(symbols, ensure_ascii=False)


def test_prefix_symbols_keep_large_but_execution_critical_fields_exact():
    recipe = "critical-recipe-step;" * 400
    first = fragment(
        10,
        1,
        container_plan=[{
            "容器类型": "进样瓶", "容器编号": 1,
            "lifecycle": recipe,
        }],
    )
    first["device_plan"][0]["parameters"] = {"recipe": recipe}
    symbols = build_prefix_symbol_table(merge({}, first, 10))

    assert symbols["device_plan"][0]["parameters"] == {"recipe": recipe}
    assert symbols["container_plan"][0]["lifecycle"] == recipe


def test_multisource_operation_is_reused_without_cloning_execution():
    first = fragment(10, 1)
    first["device_plan"][0]["source_macro_steps"] = [10, 20]
    prefix = merge({}, first, 10)
    result = merge(prefix, {"status": "device_plan", "device_plan": [], "reused_plan_steps": [1]}, 20)
    assert result["device_plan"] == prefix["device_plan"]
    assert len(result["device_plan"]) == 1
    with pytest.raises(FeasibilityFragmentError, match="does not cover"):
        merge(result, {"status": "device_plan", "device_plan": [], "reused_plan_steps": [1]}, 30)


def test_empty_observation_fragment_requires_a_sourced_handoff():
    prefix = merge({}, fragment(10, 1), 10)
    handoff = {"name": "offline XRD", "source_macro_step": 20, "source_macro_steps": [20], "semantic_classification": "observation_data_return", "required_return_data": ["XRD spectrum"]}
    result = merge(prefix, {"status": "device_plan", "device_plan": [], "offline_handoffs": [handoff]}, 20)
    assert result["offline_handoffs"] == [handoff]
    assert result["device_plan"] == prefix["device_plan"]
    with pytest.raises(FeasibilityFragmentError, match="coverage"):
        merge(prefix, {"status": "device_plan", "device_plan": []}, 20)


def test_prefix_exposes_cross_macro_handoffs_and_temporal_adaptations():
    handoff = {
        "handoff_id": "xps-series",
        "name": "offline XPS series",
        "source_macro_step": 10,
        "source_macro_steps": [10, 20],
        "sample_ids": ["NiFe_control", "NiFe_activated"],
        "measurement": "XPS oxidation-state comparison",
        "required_return_data": ["Ni 2p", "Fe 2p", "O 1s"],
        "dependencies": ["activation complete"],
        "requires_scientific_review": True,
    }
    temporal = {
        "adaptation_id": "activation-series",
        "source_macro_step": 10,
        "source_macro_steps": [10, 20],
        "sample_ids": ["NiFe_control", "NiFe_activated"],
        "original_requirement": "collect samples at multiple activation times",
        "adaptation_schedule": ["0 min", "30 min"],
        "execution_fidelity": "exact",
    }
    first = fragment(
        10,
        1,
        offline_handoffs=[handoff],
        temporal_adaptations=[temporal],
    )
    prefix = merge({}, first, 10)

    symbols = build_prefix_symbol_table(prefix)

    assert symbols["offline_handoffs"][0]["handoff_id"] == "xps-series"
    assert symbols["offline_handoffs"][0]["source_macro_steps"] == [10, 20]
    assert symbols["offline_handoffs"][0]["required_return_data"] == [
        "Ni 2p", "Fe 2p", "O 1s"
    ]
    assert symbols["temporal_adaptations"][0]["adaptation_schedule"] == [
        "0 min", "30 min"
    ]
    # The model is explicitly told that the accepted handoff already covers
    # macro 20, and the deterministic merger therefore accepts an empty block
    # instead of forcing a duplicate offline measurement.
    result = merge(
        prefix,
        {"status": "device_plan", "device_plan": []},
        20,
    )
    assert result["offline_handoffs"] == [handoff]


@pytest.mark.parametrize("field,bad", [
    ("device_plan", {}), ("device_plan", ["not an object"]),
    ("batch_plan", {}), ("batch_plan", [None]),
    ("offline_handoffs", ["lost coverage"]), ("material_ledger", []),
    ("material_ledger", {"entries": [7]}), ("quantity_adjustments", None),
])
def test_malformed_fragment_records_are_not_silently_dropped(field, bad):
    with pytest.raises(FeasibilityFragmentError):
        merge({}, fragment(10, 1, **{field: bad}), 10)


@pytest.mark.parametrize("field", ["workflow_json", "workflow_txt", "dispatch_payload", "feasibility_certificate", "trusted_human_quantity_approvals"])
def test_model_cannot_attach_dispatch_or_approval_fields(field):
    with pytest.raises(FeasibilityFragmentError, match="forbidden"):
        merge({}, fragment(10, 1, **{field: {}}), 10)


@pytest.mark.parametrize("identifier", [True, "1", 2, 0, -1])
def test_new_plan_step_ids_are_global_sequential_integers(identifier):
    with pytest.raises(FeasibilityFragmentError, match="global sequential integer"):
        merge({}, fragment(10, identifier), 10)


@pytest.mark.parametrize("sources", [[20, 10], [10, 999], [10, 10], [20]])
def test_new_sources_must_keep_frozen_order_known_ids_and_primary(sources):
    candidate = fragment(10, 1)
    candidate["device_plan"][0]["source_macro_steps"] = sources
    with pytest.raises(FeasibilityFragmentError):
        merge({}, candidate, 10)


def test_container_identity_allows_same_number_for_different_types():
    bottle = {"容器编号": 1, "容器类型": "进样瓶", "sample_id": "NiFe_control", "lifecycle": "whole route"}
    prefix = merge({}, fragment(10, 1, container_plan=[bottle]), 10)
    result = merge(prefix, fragment(20, 2, container_plan=[bottle]), 20)
    assert result["container_plan"] == [bottle]

    heat_resistant_bottle = dict(bottle, 容器类型="50ml耐热瓶")
    result = merge(prefix, fragment(20, 2, container_plan=[heat_resistant_bottle]), 20)
    assert result["container_plan"] == [bottle, heat_resistant_bottle]


def test_container_identity_rejects_conflicting_same_type_and_number():
    bottle = {"容器编号": 1, "容器类型": "进样瓶", "sample_id": "NiFe_control", "lifecycle": "whole route"}
    prefix = merge({}, fragment(10, 1, container_plan=[bottle]), 10)
    changed = dict(bottle, sample_id="NiFe_activated")
    with pytest.raises(FeasibilityFragmentError, match="conflicting"):
        merge(prefix, fragment(20, 2, container_plan=[changed]), 20)
    assert prefix["container_plan"] == [bottle]


def test_slot_identity_includes_workstation_and_prevents_same_station_rebinding():
    stock = {"工作站": "Station_A", "原液编号": 1, "名称": "Ni stock"}
    prefix = merge({}, fragment(10, 1, reagent_slot_plan=[stock]), 10)
    result = merge(prefix, fragment(20, 2, reagent_slot_plan=[dict(stock, 工作站="Station_B")]), 20)
    assert len(result["reagent_slot_plan"]) == 2
    with pytest.raises(FeasibilityFragmentError, match="conflicting"):
        merge(prefix, fragment(20, 2, reagent_slot_plan=[dict(stock, 名称="Fe stock")]), 20)


def test_cross_fragment_material_graph_references_are_preserved():
    first = fragment(10, 1, batch_plan=[{
        "batch_id": "root", "is_root_batch": True, "material_identity_id": "frozen_identity",
        "total_quantity": {"value": 10, "unit": "mL"}, "source_plan_steps": [1],
        "source_macro_steps": [10], "research_source_refs": [{"source_path": "macro_action_steps[0].参数", "source_macro_step": 10}],
    }], material_ledger={"entries": [{"entry_id": "root-produced", "batch_id": "root", "processing_step_refs": [1], "produced": {"value": 10, "unit": "mL"}}]})
    prefix = merge({}, first, 10)
    second = fragment(20, 2, batch_plan=[{
        "batch_id": "child", "parent_batch_id": "root", "source_macro_steps": [20],
        "source_plan_steps": [2], "material_identity_id": "frozen_identity",
    }], material_transitions=[{
        "transition_id": "reaction", "parent_batch_ids": ["root"], "child_batch_ids": ["child"],
        "source_plan_steps": [2], "source_macro_steps": [20],
        "input_allocations": [{"batch_id": "root", "quantity": {"value": 10, "unit": "mL"}}],
    }], material_ledger={"entries": [{
        "entry_id": "child-produced", "batch_id": "child", "processing_step_refs": [2],
        "source_refs": ["material_transition:reaction"],
    }]})
    second["device_plan"][0]["material_transition_ids"] = ["reaction"]
    result = merge(prefix, second, 20)
    assert result["batch_plan"][1]["parent_batch_id"] == "root"
    assert result["batch_plan"][0] == prefix["batch_plan"][0]
    assert result["material_ledger"]["entries"][0] == prefix["material_ledger"]["entries"][0]
    assert result["material_transitions"][0]["source_plan_steps"] == [2]


@pytest.mark.parametrize("field,records", [
    ("quantity_requirement_dispositions", [{"source_macro_step": 10, "requirement_index": 0, "plan_step": 99}]),
    ("batch_plan", [{"batch_id": "child", "parent_batch_id": "missing"}]),
    ("batch_plan", [{"batch_id": "batch", "source_plan_steps": [2]}]),
    ("material_transitions", [{"transition_id": "bad", "parent_batch_ids": ["missing"], "child_batch_ids": []}]),
    ("material_ledger", {"entries": [{"entry_id": "bad", "source_refs": ["material_transition:missing"]}]}),
])
def test_dangling_references_are_rejected_before_global_validation(field, records):
    with pytest.raises(FeasibilityFragmentError, match="dangling reference"):
        merge({}, fragment(10, 1, **{field: records}), 10)


def test_batch_consumer_update_is_additive_and_cannot_change_inventory():
    batch = {
        "batch_id": "root", "sample_id": "NiFe_control", "is_root_batch": True,
        "material_identity_id": "frozen_identity", "total_quantity": {"value": 10, "unit": "mL"},
        "consumer_ids": ["consumer_1"], "allocation": {"consumer_1": {"value": 4, "unit": "mL"}},
    }
    prefix = merge({}, fragment(10, 1, batch_plan=[batch]), 10)
    update = {"table": "batch_plan", "id": "root", "consumer_ids": ["consumer_2"], "allocation": {"consumer_2": {"value": 6, "unit": "mL"}}}
    result = merge(prefix, fragment(20, 2, prior_record_updates=[update]), 20)
    assert result["batch_plan"][0]["consumer_ids"] == ["consumer_1", "consumer_2"]
    assert result["batch_plan"][0]["allocation"]["consumer_1"] == {"value": 4, "unit": "mL"}
    for key in ("sample_id", "is_root_batch", "material_identity_id", "total_quantity"):
        assert result["batch_plan"][0][key] == batch[key]
    assert prefix["batch_plan"] == [batch]
    changed = dict(update, total_quantity={"value": 20, "unit": "mL"})
    with pytest.raises(FeasibilityFragmentError, match="only additive"):
        merge(prefix, fragment(20, 2, prior_record_updates=[changed]), 20)
    changed = dict(update, consumer_ids=["consumer_1"], allocation={"consumer_1": {"value": 9, "unit": "mL"}})
    with pytest.raises(FeasibilityFragmentError, match="cannot change existing"):
        merge(prefix, fragment(20, 2, prior_record_updates=[changed]), 20)
    changed = dict(update, id="unknown")
    with pytest.raises(FeasibilityFragmentError, match="prior batch"):
        merge(prefix, fragment(20, 2, prior_record_updates=[changed]), 20)


def test_whole_batch_can_bind_one_later_transition_without_numeric_inventory():
    root = {
        "batch_id": "whole_root", "quantity_mode": "whole_batch",
        "material_identity_id": "frozen_identity", "sample_id": "NiFe_control",
        "is_root_batch": True, "consumer_ids": [],
        "source_macro_steps": [10], "source_plan_steps": [1],
    }
    prefix = merge({}, fragment(10, 1, batch_plan=[root]), 10)
    child = {
        "batch_id": "whole_child", "quantity_mode": "whole_batch",
        "parent_batch_id": "whole_root", "material_identity_id": "frozen_identity",
        "sample_id": "NiFe_control", "consumer_ids": [],
        "source_macro_steps": [20], "source_plan_steps": [2],
    }
    transition = {
        "transition_id": "whole_drying", "quantity_basis": "whole_batch",
        "parent_batch_ids": ["whole_root"], "child_batch_ids": ["whole_child"],
        "source_macro_steps": [20], "source_plan_steps": [2],
    }
    update = {"table": "batch_plan", "id": "whole_root", "consumer_ids": ["material_transition:whole_drying"]}
    second = fragment(20, 2, batch_plan=[child], material_transitions=[transition], prior_record_updates=[update])
    second["device_plan"][0]["material_transition_ids"] = ["whole_drying"]
    result = merge(prefix, second, 20)
    merged_root = result["batch_plan"][0]
    assert merged_root["consumer_ids"] == ["material_transition:whole_drying"]
    assert {key: value for key, value in merged_root.items() if key != "consumer_ids"} == {key: value for key, value in root.items() if key != "consumer_ids"}
    assert "allocation" not in merged_root and "total_quantity" not in merged_root
    assert result["batch_plan"][1] == child
    assert prefix["batch_plan"] == [root]
    with pytest.raises(FeasibilityFragmentError, match="at most one total consumer"):
        merge(result, fragment(30, 3, prior_record_updates=[dict(update, consumer_ids=["extra_consumer"])]), 30)
    numeric_update = dict(update, allocation={"material_transition:whole_drying": {"value": 1, "unit": "g"}})
    with pytest.raises(FeasibilityFragmentError, match="cannot carry numeric allocations"):
        merge(prefix, fragment(20, 2, prior_record_updates=[numeric_update]), 20)


def test_manual_review_survives_later_success_and_accumulates_issues():
    first = fragment(10, 1, status="manual_required", requires_scientific_review=True,
        feedback_type="human_review_required", feedback_route="human",
        quantity_audit={"status": "human_review_required", "issues": [{"code": "unknown_yield"}]})
    prefix = merge({}, first, 10)
    second = fragment(20, 2, requires_scientific_review=False,
        quantity_audit={"status": "passed", "issues": []},
        pending_quantity_human_review={"status": "human_review_required", "issues": [{"code": "unauthorized_pooling"}]})
    result = merge(prefix, second, 20)
    assert result["status"] == "manual_required"
    assert result["requires_scientific_review"] is True
    expected_audit = {"status": "human_review_required", "issues": [{"code": "unknown_yield"}, {"code": "unauthorized_pooling"}]}
    assert result["quantity_audit"] == expected_audit
    assert result["pending_quantity_human_review"] == expected_audit
    assert result["feedback_route"] == "human"
    assert result["_feasibility_fragments"]["completed_macro_ids"] == ["10", "20"]
    from single_agent import SingleDeviceAgent
    promoted = SingleDeviceAgent._promote_feasible_quantity_human_plan(result)
    assert promoted["pending_quantity_human_review"] == expected_audit
    third = fragment(30, 3, quantity_audit={"status": "failed", "issues": [{"code": "inventory_shortage"}]})
    failed = merge(result, third, 30)
    assert failed["quantity_audit"]["status"] == "failed"
    assert failed["quantity_audit"] == failed["pending_quantity_human_review"]
    assert {issue["code"] for issue in failed["quantity_audit"]["issues"]} == {"unknown_yield", "unauthorized_pooling", "inventory_shortage"}


def test_hard_failure_preserves_nonaccepted_prefix_and_all_blockers():
    prefix = merge({}, fragment(10, 1), 10)
    blocked = {
        "status": "feasibility_error", "feedback_type": "device_feasibility_error",
        "feasibility": {"is_feasible": False, "blocking_constraints": ["station cannot meet required atmosphere"]},
    }
    result = merge(prefix, blocked, 20)
    assert result["status"] == "feasibility_error"
    assert result["device_plan"] == prefix["device_plan"]
    assert result["feasibility"]["blocking_constraints"] == blocked["feasibility"]["blocking_constraints"]
    assert result["_feasibility_fragments"]["completed_macro_ids"] == ["10"]
    with pytest.raises(FeasibilityFragmentError, match="hard-terminal"):
        merge(result, fragment(20, 2), 20)


def test_positive_status_cannot_hide_blockers_or_nested_review():
    result = merge({}, fragment(10, 1, feasibility={"is_feasible": True, "blocking_constraints": ["unresolved physical condition"]}), 10)
    assert result["status"] == "feasibility_error"
    result = merge({}, fragment(10, 1, quantity_adjustments=[{"adjustment_id": "qa_1", "requires_scientific_review": True}]), 10)
    assert result["requires_scientific_review"] is True


def test_matrix_drift_and_nonfinite_quantities_fail_atomically():
    prefix = merge({}, fragment(10, 1), 10)
    frozen = copy.deepcopy(prefix)
    with pytest.raises(FeasibilityFragmentError, match="frozen Research matrix"):
        merge(prefix, fragment(20, 2, sample_control_matrix=MATRIX[:1]), 20)
    with pytest.raises(FeasibilityFragmentError, match="finite JSON"):
        merge(prefix, fragment(20, 2, quantity_adjustments=[{"adjustment_id": "qa_bad", "after": {"value": float("nan"), "unit": "g"}}]), 20)
    assert prefix == frozen


def test_instruction_contains_compact_carryover_and_original_macro_ids():
    prefix = merge({}, fragment(10, 1), 10)
    frozen = copy.deepcopy(prefix)
    instruction = build_fragment_instruction(prefix, "20", MACROS, MATRIX)
    context = json.loads(instruction[instruction.index('{"accepted_prefix_symbols"'):])
    assert context["accepted_prefix_symbols"]["candidate_sha256"]
    assert context["accepted_prefix_symbols"]["device_plan"][0]["plan_step"] == 1
    assert context["accepted_prefix_symbols"] != prefix
    assert context["first_new_plan_step"] == 2
    assert context["current_macro_id"] == "20"
    assert context["all_macro_ids"] == MACROS
    assert context["frozen_sample_control_matrix"] == MATRIX
    assert "prior_record_updates" in instruction and "reused_plan_steps" in instruction
    assert prefix == frozen
    with pytest.raises(FeasibilityFragmentError, match="Research order"):
        build_fragment_instruction(prefix, "30", MACROS, MATRIX)
