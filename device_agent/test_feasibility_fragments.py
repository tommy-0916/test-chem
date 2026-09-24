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
    build_fragment_field_contract,
    build_fragment_instruction,
    build_fragment_repair_instruction,
    build_fragment_request_context,
    build_prefix_symbol_table,
    diagnose_fragment,
    merge_fragment,
    _ALLOWED,
    _FORBIDDEN,
)


MACROS = [10, 20, 30]
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
    return merge_fragment(previous, new, source, MACROS, MATRIX)


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
    assert result["_feasibility_fragments"]["completed_macro_ids"] == [10, 20]
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

    instruction = build_fragment_instruction(prefix, 20, MACROS, MATRIX)
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
                }] if identifier == 20 else []),
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
        handoff, semantic, {}, 10, MACROS, MATRIX,
    )
    serialized = json.dumps(context, ensure_ascii=False)

    assert "UNRELATED_SURVEY_BULK" not in serialized
    assert context["current_macro"]["macro_step_id"] == 10
    assert context["semantic_assessment"] == [{
        "source_macro_step": 10,
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


@pytest.mark.parametrize(
    "current,declared",
    [
        (1, 1.0),
        (1.0, 1),
        (1, "1"),
        ("1", 1),
    ],
)
def test_offline_handoff_coverage_requires_exact_typed_macro_identity(
    current, declared
):
    handoff = {
        "name": "offline observation",
        "source_macro_step": declared,
        "source_macro_steps": [declared],
        "semantic_classification": "observation_data_return",
        "required_return_data": ["result"],
    }

    with pytest.raises(FeasibilityFragmentError, match="coverage"):
        merge_fragment(
            {},
            {
                "status": "device_plan",
                "device_plan": [],
                "offline_handoffs": [handoff],
            },
            current,
            [current, declared],
            MATRIX,
        )


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
    stock = {
        "工作站": "Station_A",
        "原液编号": 1,
        "material_identity_id": "SOL_NI",
        "canonical_name": "Ni stock",
    }
    prefix = merge({}, fragment(10, 1, reagent_slot_plan=[stock]), 10)
    result = merge(prefix, fragment(20, 2, reagent_slot_plan=[dict(stock, 工作站="Station_B")]), 20)
    assert len(result["reagent_slot_plan"]) == 2
    with pytest.raises(FeasibilityFragmentError, match="conflicting"):
        merge(
            prefix,
            fragment(
                20,
                2,
                reagent_slot_plan=[
                    dict(
                        stock,
                        material_identity_id="SOL_FE",
                        canonical_name="Fe stock",
                    )
                ],
            ),
            20,
        )


def test_slot_identity_requires_stable_id_and_canonical_name():
    base = {"工作站": "Station_A", "原液编号": 1}
    with pytest.raises(FeasibilityFragmentError) as missing_id:
        merge(
            {},
            fragment(10, 1, reagent_slot_plan=[dict(base, canonical_name="stock")]),
            10,
        )
    assert missing_id.value.code == "MISSING_MATERIAL_IDENTITY_ID"

    with pytest.raises(FeasibilityFragmentError) as missing_name:
        merge(
            {},
            fragment(
                10,
                1,
                reagent_slot_plan=[dict(base, material_identity_id="SOL_A")],
            ),
            10,
        )
    assert missing_name.value.code == "MISSING_CANONICAL_NAME"


def _legacy_reagent_slot_prefix():
    """Build an accepted prefix, then represent a pre-identity checkpoint."""
    slot = {
        "工作站": "Station_A",
        "原液编号": 1,
        "material_identity_id": "SOL_LEGACY",
        "canonical_name": "Legacy stock",
        "配料名称": "historical presentation only",
    }
    prefix = merge({}, fragment(10, 1, reagent_slot_plan=[slot]), 10)
    prefix["reagent_slot_plan"][0].pop("material_identity_id")
    prefix["reagent_slot_plan"][0].pop("canonical_name")
    return prefix


def test_untouched_legacy_slot_checkpoint_remains_resumable_without_fabrication():
    prefix = _legacy_reagent_slot_prefix()
    frozen_prefix = copy.deepcopy(prefix)

    result = merge(prefix, fragment(20, 2), 20)

    assert result["reagent_slot_plan"] == frozen_prefix["reagent_slot_plan"]
    assert "material_identity_id" not in result["reagent_slot_plan"][0]
    assert "canonical_name" not in result["reagent_slot_plan"][0]
    assert prefix == frozen_prefix


@pytest.mark.parametrize(
    ("incoming", "expected_code"),
    [
        (
            {"工作站": "Station_B", "原液编号": 2, "canonical_name": "New stock"},
            "MISSING_MATERIAL_IDENTITY_ID",
        ),
        (
            {"工作站": "Station_B", "原液编号": 2, "material_identity_id": "SOL_NEW"},
            "MISSING_CANONICAL_NAME",
        ),
        (
            {"工作站": "Station_A", "原液编号": 1, "canonical_name": "Attempted repair"},
            "MISSING_MATERIAL_IDENTITY_ID",
        ),
    ],
)
def test_new_or_modified_slot_in_legacy_resume_must_satisfy_identity_contract(incoming, expected_code):
    prefix = _legacy_reagent_slot_prefix()
    frozen_prefix = copy.deepcopy(prefix)

    with pytest.raises(FeasibilityFragmentError) as error:
        merge(prefix, fragment(20, 2, reagent_slot_plan=[incoming]), 20)

    assert error.value.code == expected_code
    assert prefix == frozen_prefix


def test_legacy_resume_accepts_complete_new_slot_but_never_rebinds_existing_slot():
    prefix = _legacy_reagent_slot_prefix()
    new_slot = {
        "工作站": "Station_B",
        "原液编号": 2,
        "material_identity_id": "SOL_NEW",
        "canonical_name": "New stock",
    }

    result = merge(prefix, fragment(20, 2, reagent_slot_plan=[new_slot]), 20)
    assert result["reagent_slot_plan"] == prefix["reagent_slot_plan"] + [new_slot]

    replacement = {
        "工作站": "Station_A",
        "原液编号": 1,
        "material_identity_id": "SOL_REBOUND",
        "canonical_name": "Rebound stock",
    }
    with pytest.raises(FeasibilityFragmentError) as conflict:
        merge(prefix, fragment(20, 2, reagent_slot_plan=[replacement]), 20)
    assert conflict.value.code == "CONFLICTING_RECORD"


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


@pytest.mark.parametrize("macro_id", [1, "1"])
def test_macro_source_ref_resolves_one_numeric_or_string_typed_id(macro_id):
    payload = fragment(
        macro_id,
        1,
        material_ledger={
            "entries": [
                {"entry_id": "research-source", "source_refs": ["macro_step:1"]}
            ]
        },
    )

    result = merge_fragment({}, payload, macro_id, [macro_id], MATRIX)

    assert result["material_ledger"]["entries"][0]["source_refs"] == [
        "macro_step:1"
    ]


def test_macro_source_ref_rejects_unknown_numeric_id():
    payload = fragment(
        1,
        1,
        material_ledger={
            "entries": [
                {"entry_id": "unknown-source", "source_refs": ["macro_step:2"]}
            ]
        },
    )

    with pytest.raises(FeasibilityFragmentError) as caught:
        merge_fragment({}, payload, 1, [1], MATRIX)

    assert caught.value.code == "DANGLING_REFERENCE"
    assert caught.value.path.endswith("source_refs")


def test_macro_source_ref_rejects_ambiguous_numeric_and_string_alias():
    payload = fragment(
        1,
        1,
        material_ledger={
            "entries": [
                {"entry_id": "ambiguous-source", "source_refs": ["macro_step:1"]}
            ]
        },
    )

    with pytest.raises(FeasibilityFragmentError) as caught:
        merge_fragment({}, payload, 1, [1, "1"], MATRIX)

    assert caught.value.code == "AMBIGUOUS_REFERENCE"
    assert caught.value.path.endswith("source_refs")
    assert caught.value.details["typed_candidates"] == [
        ("integer", 1),
        ("string", "1"),
    ]


@pytest.mark.parametrize("reference", ["1", 0])
def test_source_plan_step_references_are_typed_and_preserve_zero(reference):
    payload = fragment(
        10,
        1,
        offline_handoffs=[
            {
                "handoff_id": "typed-plan-ref",
                "source_macro_step": 10,
                "source_macro_steps": [10],
                "source_plan_step": reference,
            }
        ],
    )
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge({}, payload, 10)
    assert caught.value.code == "DANGLING_REFERENCE"
    assert caught.value.path.endswith("source_plan_step")
    assert repr(reference) in str(caught.value)


def test_source_plan_steps_string_does_not_alias_integer_plan_step():
    payload = fragment(
        10,
        1,
        batch_plan=[{"batch_id": "typed-ref", "source_plan_steps": ["1"]}],
    )
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge({}, payload, 10)
    assert caught.value.code == "DANGLING_REFERENCE"
    assert caught.value.path.endswith("source_plan_steps")


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
    assert result["_feasibility_fragments"]["completed_macro_ids"] == [10, 20]
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
    assert result["_feasibility_fragments"]["completed_macro_ids"] == [10]
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
    instruction = build_fragment_instruction(prefix, 20, MACROS, MATRIX)
    context = json.loads(instruction[instruction.index('{"accepted_prefix_symbols"'):])
    assert context["accepted_prefix_symbols"]["candidate_sha256"]
    assert context["accepted_prefix_symbols"]["device_plan"][0]["plan_step"] == 1
    assert context["accepted_prefix_symbols"] != prefix
    assert context["first_new_plan_step"] == 2
    assert context["current_macro_id"] == 20
    assert context["all_macro_ids"] == MACROS
    assert context["frozen_sample_control_matrix"] == MATRIX
    assert "prior_record_updates" in instruction and "reused_plan_steps" in instruction
    assert prefix == frozen
    with pytest.raises(FeasibilityFragmentError, match="Research order"):
        build_fragment_instruction(prefix, 30, MACROS, MATRIX)


def test_unknown_output_fields_carry_machine_readable_code_and_details():
    rejected = fragment(
        10, 1, workstation_skill_reviewed=True, workstation_skill_used=["XPS"],
    )
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge({}, rejected, 10)
    exc = caught.value
    assert exc.code == "UNKNOWN_FIELDS"
    assert exc.path == "fragment"
    assert exc.details == {
        "fields": ["workstation_skill_reviewed", "workstation_skill_used"]
    }
    assert "workstation_skill_reviewed" in str(exc)


def test_dispatch_fields_report_forbidden_code():
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge({}, fragment(10, 1, workflow_json={}), 10)
    assert caught.value.code == "FORBIDDEN_FIELDS"
    assert caught.value.details["fields"] == ["workflow_json"]


def test_unknown_field_with_veto_semantics_is_never_stripped_to_pass():
    # A field name may look like noise while carrying "do not execute" meaning.
    # Unknown fields are rejected whole; nothing is filtered out to make the
    # remaining object pass.
    veto = fragment(10, 1, unverified_parameter_note="dose not confirmed")
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge({}, veto, 10)
    assert caught.value.code == "UNKNOWN_FIELDS"


def test_frozen_matrix_mutation_has_dedicated_code_and_changes_nothing():
    prefix = merge({}, fragment(10, 1), 10)
    frozen = copy.deepcopy(prefix)
    mutated = copy.deepcopy(MATRIX)
    mutated[0]["variables"]["activation_minutes"] = 999  # nested drift
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge(prefix, fragment(20, 2, sample_control_matrix=mutated), 20)
    assert caught.value.code == "FROZEN_MATRIX_MISMATCH"
    assert caught.value.path == "sample_control_matrix"
    assert prefix == frozen


def test_field_contract_covers_exactly_the_enforced_sets():
    contract = build_fragment_field_contract()
    for field in sorted(_ALLOWED | _FORBIDDEN):
        assert field in contract
    assert "禁止自造字段" in contract
    prefix = merge({}, fragment(10, 1), 10)
    instruction = build_fragment_instruction(prefix, 20, MACROS, MATRIX)
    assert contract in instruction
    # The machine-readable context remains the trailing JSON payload.
    context = json.loads(instruction[instruction.index('{"accepted_prefix_symbols"'):])
    assert context["current_macro_id"] == 20
    assert context["first_new_plan_step"] == 2


def test_repair_instruction_bounds_scope_and_marks_rejected_fragment_as_data():
    rejected = fragment(10, 1, parameter_disposition={"note": "self-proof"})
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge({}, rejected, 10)
    repair = build_fragment_repair_instruction(
        rejected, caught.value, attempt=1, max_attempts=2,
    )
    assert "修复模式" in repair and "第 1/2 次修复" in repair
    assert "[UNKNOWN_FIELDS]" in repair and "parameter_disposition" in repair
    assert "禁止操作" in repair and "冻结" in repair
    assert "待处理数据" in repair
    assert "self-proof" in repair  # rejected fragment embedded as data
    assert build_fragment_field_contract() in repair


def whole_batch_root(consumers):
    return {
        "batch_id": "whole_root", "quantity_mode": "whole_batch",
        "material_identity_id": "frozen_identity", "sample_id": "NiFe_control",
        "is_root_batch": True, "consumer_ids": consumers,
        "source_macro_steps": [10], "source_plan_steps": [1],
    }


def test_whole_batch_conflict_error_carries_consumer_identities():
    prefix = merge({}, fragment(10, 1, batch_plan=[whole_batch_root(["op_a"])]), 10)
    update = {"table": "batch_plan", "id": "whole_root", "consumer_ids": ["op_b"]}
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge(prefix, fragment(20, 2, prior_record_updates=[update]), 20)
    assert caught.value.code == "CONFLICTING_RECORD"
    assert caught.value.details == {
        "batch_id": "whole_root",
        "existing_consumers": ["op_a"],
        "added_consumers": ["op_b"],
    }


def test_merge_rejection_never_mutates_prefix_for_each_error_class():
    prefix = merge({}, fragment(10, 1), 10)
    frozen_prefix = copy.deepcopy(prefix)
    with pytest.raises(FeasibilityFragmentError, match="unsupported/forbidden"):
        merge(prefix, fragment(20, 2, workstation_skill_reviewed=True), 20)
    tampered = fragment(20, 2)
    tampered["sample_control_matrix"] = [{"sample_id": "MUTATED"}]
    with pytest.raises(FeasibilityFragmentError, match="frozen Research matrix"):
        merge(prefix, tampered, 20)
    with_root = merge(prefix, fragment(20, 2, batch_plan=[whole_batch_root([])]), 20)
    conflict = {"table": "batch_plan", "id": "whole_root", "consumer_ids": ["op_a", "op_b"]}
    with pytest.raises(FeasibilityFragmentError, match="at most one total consumer"):
        merge(with_root, fragment(30, 3, prior_record_updates=[conflict]), 30)
    # Every rejection above must leave the accepted prefix byte-identical:
    # failures are diagnostics, never partially applied business state.
    assert prefix == frozen_prefix
    assert with_root["batch_plan"][0]["consumer_ids"] == []


def test_diagnose_fragment_reports_masked_consumer_conflict():
    # An early field-contract failure must not hide a later consumer conflict:
    # the merge raises on unknown fields before ever reaching _apply_updates.
    prefix = merge({}, fragment(10, 1, batch_plan=[whole_batch_root(["op_a"])]), 10)
    bad = fragment(20, 2, prior_record_updates=[{
        "table": "batch_plan", "id": "whole_root", "consumer_ids": ["op_b"],
    }])
    bad["workstation_skill_reviewed"] = True
    report = diagnose_fragment(prefix, bad, 20, MACROS, MATRIX)
    codes = {(finding["stage"], finding["code"]) for finding in report["findings"]}
    assert ("fields", "UNKNOWN_FIELDS") in codes
    conflict = [
        finding for finding in report["findings"]
        if finding["stage"] == "prior_record_updates" and finding["code"] == "CONFLICTING_RECORD"
    ]
    assert len(conflict) == 1
    assert conflict[0]["details"] == {
        "batch_id": "whole_root",
        "existing_consumers": ["op_a"],
        "added_consumers": ["op_b"],
    }
    stages = {finding["stage"]: finding["status"] for finding in report["findings"]}
    assert stages["status"] == "passed"
    assert stages["frozen_matrix"] == "passed"
    # Checks that need the merged candidate are honestly reported, not "passed".
    assert stages["references"] == "not_executed"


def test_diagnose_fragment_never_marks_unexecuted_stages_as_passed():
    report = diagnose_fragment({}, ["not", "an", "object"], 10, MACROS, MATRIX)
    assert report["findings"][0]["stage"] == "fields"
    assert report["findings"][0]["code"] == "NOT_AN_OBJECT"
    assert all(finding["status"] == "not_executed" for finding in report["findings"][1:])


def test_diagnose_fragment_reports_no_prior_updates_when_absent():
    prefix = merge({}, fragment(10, 1), 10)
    report = diagnose_fragment(prefix, fragment(20, 2), 20, MACROS, MATRIX)
    stages = {finding["stage"]: finding["status"] for finding in report["findings"]}
    assert stages["fields"] == "passed"
    assert stages["status"] == "passed"
    assert stages["frozen_matrix"] == "passed"
    assert stages["prior_record_updates"] == "not_executed"


def test_terminal_failure_exit_variants_merge_and_classify_without_model():
    prefix = merge({}, fragment(10, 1), 10)
    # 终止报告通道：终态 status + error_package。
    exit_report = {
        "status": "feasibility_error",
        "feedback_type": "device_feasibility_error", "feedback_route": "device",
        "failure_scope": "device", "failure_stage": "route_feasibility",
        "error_package": {
            "type": "device_feasibility_error",
            "blocking_constraints": ["no legal route between stations"],
            "message": "minimal hand-constructed legal failure result",
        },
    }
    result = merge(prefix, exit_report, 20)
    assert result["status"] == "feasibility_error"
    assert result["error_package"]["type"] == "device_feasibility_error"
    assert result["_feasibility_fragments"]["completed_macro_ids"] == [10]
    # 语义阻塞通道：继续态 + 顶层 blocking_constraints，自动升格。
    exit_blockers = dict(fragment(20, 2), blocking_constraints=["station cannot meet atmosphere"])
    blocked = merge(prefix, exit_blockers, 20)
    assert blocked["status"] == "feasibility_error"
    assert blocked["blocking_constraints"] == ["station cannot meet atmosphere"]
    assert blocked["_feasibility_fragments"]["completed_macro_ids"] == [10]
    # 硬终止候选不可被后续块续跑。
    with pytest.raises(FeasibilityFragmentError, match="hard-terminal"):
        merge(result, fragment(20, 2), 20)


def test_material_state_digest_states_whole_batch_facts_and_allowed_ids():
    from feasibility_fragments import build_material_state_digest
    root = whole_batch_root(["material_transition:dry"])
    transition = {
        "transition_id": "dry", "quantity_basis": "whole_batch",
        "parent_batch_ids": ["whole_root"], "child_batch_ids": [],
        "source_macro_steps": [10], "source_plan_steps": [1],
    }
    prefix = merge({}, fragment(10, 1, batch_plan=[root], material_transitions=[transition]), 10)
    digest = build_material_state_digest(prefix, MACROS)
    assert digest["allowed_reference_ids"] == {
        "plan_steps": [1],
        "batch_ids": ["whole_root"],
        "transition_ids": ["dry"],
        "macro_ids": MACROS,
    }
    entry = digest["batch_plan"][0]
    assert entry["existing_consumers"] == ["material_transition:dry"]
    assert "唯一总消费者已是 material_transition:dry" in entry["current_fact"]
    assert "已接受的宏步骤 [10]" in entry["current_fact"]
    # A whole_batch record without consumers offers exactly one new binding.
    empty_digest = build_material_state_digest(
        merge({}, fragment(10, 1, batch_plan=[whole_batch_root([])]), 10), MACROS
    )
    assert "当前没有消费者" in empty_digest["batch_plan"][0]["current_fact"]


def test_fragment_request_context_includes_material_state_digest():
    handoff = {
        "task": {"query": "NiFe LDH", "current_stage": "synthesis"},
        "macro_action_steps": [
            {"步骤序号": identifier, "操作": f"operation-{identifier}", "sample_id": "S"}
            for identifier in MACROS
        ],
        "device_agent_contract": {"contract_version": "v2"},
    }
    semantic = {"macro_step_assessments": [], "material_identity_registry": []}
    context = build_fragment_request_context(handoff, semantic, {}, 10, MACROS, MATRIX)
    digest = context["material_state_digest"]
    assert digest["batch_plan"] == []
    assert digest["allowed_reference_ids"]["plan_steps"] == []
    assert digest["allowed_reference_ids"]["macro_ids"] == MACROS


@pytest.mark.parametrize("macro_id", ["001", "1,2", "phase-alpha"])
def test_opaque_string_macro_ids_survive_fragment_merge_end_to_end(macro_id):
    candidate = merge_fragment(
        {}, fragment(macro_id, 1), macro_id, [macro_id], MATRIX
    )

    assert candidate["device_plan"][0]["source_macro_step"] == macro_id
    assert candidate["device_plan"][0]["source_macro_steps"] == [macro_id]
    assert candidate["_feasibility_fragments"]["completed_macro_ids"] == [macro_id]


def test_typed_macro_mismatch_is_rejected_instead_of_coerced():
    with pytest.raises(FeasibilityFragmentError, match="unknown Research macro ID"):
        merge_fragment({}, fragment("1", 1), 1, [1], MATRIX)


def test_fragment_source_id_mirror_is_preserved_and_conflicts_fail_closed():
    source_id_only = fragment(10, 1)
    source_id_only["device_plan"][0].pop("source_macro_step")
    source_id_only["device_plan"][0].pop("source_macro_steps")
    source_id_only["device_plan"][0]["source_macro_step_id"] = 10
    source_id_only["quantity_requirement_dispositions"] = [
        {
            "source_macro_step_id": 10,
            "requirement_index": 0,
            "decision": "preserve typed source identity",
        }
    ]

    merged = merge_fragment({}, source_id_only, 10, MACROS, MATRIX)
    assert merged["device_plan"][0]["source_macro_step_id"] == 10
    assert build_prefix_symbol_table(merged)["device_plan"][0][
        "source_macro_step_id"
    ] == 10
    assert build_prefix_symbol_table(merged)[
        "quantity_requirement_dispositions"
    ][0]["source_macro_step_id"] == 10

    conflicting = fragment(10, 1)
    conflicting["device_plan"][0]["source_macro_step_id"] = "10"
    with pytest.raises(FeasibilityFragmentError) as caught:
        merge_fragment({}, conflicting, 10, MACROS, MATRIX)
    assert caught.value.code == "CONFLICTING_MACRO_SOURCE"
    assert caught.value.path == "device_plan[0]"


def test_fragment_context_missing_semantic_macro_id_fails_closed():
    handoff = {
        "task": {"query": "missing ID must not use array position"},
        "macro_action_steps": [{"操作": "first"}],
    }

    with pytest.raises(FeasibilityFragmentError) as caught:
        build_fragment_request_context(handoff, {}, {}, "001", ["001"], MATRIX)

    assert caught.value.code == "MISSING_MACRO_ID"
    assert caught.value.path == "research_handoff.macro_action_steps[0]"


def test_fragment_context_conflicting_step_identity_mirrors_fail_closed():
    handoff = {
        "task": {"query": "typed alias conflict must block planning"},
        "macro_action_steps": [
            {
                "macro_step_id": 1,
                "logical_step_id": "1",
                "macro_action_id": "MA_SHARED",
                "步骤序号": 1,
                "操作": "first",
            }
        ],
    }

    with pytest.raises(FeasibilityFragmentError) as caught:
        build_fragment_request_context(handoff, {}, {}, 1, [1], MATRIX)

    assert caught.value.code == "CONFLICTING_MACRO_ID_MIRRORS"
    assert caught.value.path == "research_handoff.macro_action_steps[0]"
