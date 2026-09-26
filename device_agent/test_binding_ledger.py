"""Focused regressions for generic structured binding-ledger construction."""

from __future__ import annotations

import copy

import pytest

from binding_ledger import (
    build_instance_registry,
    ensure_binding_ledger,
    resolve_frozen_sample_binding,
)
from macro_identity import macro_id_key
from single_agent import SingleDeviceAgent, SingleDeviceAgentState


def semantic_contract(macro_id="phase-alpha", resource_id="feed-A"):
    return {
        "material_identity_registry": [
            {
                "identity_id": resource_id,
                "roles": ["reagent"],
                "canonical_name_variants": ["Feed A"],
            },
            {
                "identity_id": "product-A",
                "roles": ["sample"],
                "canonical_name_variants": ["Product A"],
            },
        ],
        "macro_step_assessments": [
            {
                "source_macro_step": macro_id,
                "required_capabilities": [{"category": "reaction"}],
                "material_identities": [
                    {"identity_id": resource_id},
                    {"identity_id": "product-A"},
                ],
                "evidence_refs": ["macro_action_steps[0].structured_materials"],
            }
        ],
    }


def candidate(
    *,
    macro_id="phase-alpha",
    sample_id="specimen-42",
    group_id="cohort-red",
    resource_id="feed-A",
):
    return {
        "device_plan": [
            {
                "plan_step": 1,
                "workstation": "feed station",
                "containers": {"容器编号": [1]},
                "source_macro_steps": [macro_id],
                "source_material_identity_ids": [resource_id],
                "material_event_kind": "none",
            },
            {
                "plan_step": 2,
                "workstation": "reactor",
                "containers": {"容器编号": [1]},
                "source_macro_steps": [macro_id],
                "source_material_identity_ids": ["product-A"],
                "material_event_kind": "state_change",
            },
        ],
        "container_plan": [
            {
                "容器类型": "arbitrary-reactor-vessel",
                "容器编号": 1,
                "sample_id": sample_id,
                "group_id": group_id,
                "material_identity_id": "product-A",
                # Display prose is deliberately non-authoritative.
                "用途": "arbitrary display text",
            }
        ],
        "sample_control_matrix": [
            {"sample_id": sample_id, "group_id": group_id}
        ],
        "offline_handoffs": [
            {
                "semantic_classification": "input_boundary",
                "source_macro_steps": [macro_id],
                "source_material_identity_ids": [],
            }
        ],
    }


def current_handoff(sample_id="specimen-42", group_id="cohort-red"):
    group = {"sample_id": sample_id, "group_id": group_id}
    return {
        "macro_action": {"experiment_group": copy.deepcopy(group)},
        "research_action_package_v2": {
            "macro_action": {"experiment_group": copy.deepcopy(group)},
            "macro_steps": [copy.deepcopy(group)],
        },
        # These are historical/diagnostic and must not pollute current binding.
        "observations": [
            {"sample_id": "old-sample", "group_id": "old-group"}
        ],
        "history": {"sample_id": "older-sample", "group_id": "older-group"},
    }


def category(station: str):
    return "reaction" if station == "reactor" else "liquid_handling"


def test_current_typed_binding_accepts_arbitrary_ids_and_ignores_history():
    sample_id, group_id, issues = resolve_frozen_sample_binding(
        current_handoff()
    )
    assert issues == []
    assert sample_id == "specimen-42"
    assert group_id == "cohort-red"


def test_conflicting_current_structures_fail_closed_without_legacy_fallback():
    handoff = current_handoff()
    handoff["research_action_package_v2"]["macro_action"][
        "experiment_group"
    ] = {"sample_id": "different-sample", "group_id": "different-group"}
    handoff["sample_control_matrix"] = [
        {"sample_id": "specimen-42", "group_id": "cohort-red"}
    ]

    sample_id, group_id, issues = resolve_frozen_sample_binding(handoff)

    assert sample_id is None and group_id is None
    assert {issue["code"] for issue in issues} == {"sample_binding_conflict"}


def test_legacy_multi_sample_or_group_matrix_is_explicitly_ambiguous():
    sample_id, group_id, issues = resolve_frozen_sample_binding(
        {
            "sample_control_matrix": [
                {"sample_id": "sample-a", "group_id": "group-a"},
                {"control_id": "sample-b", "group_id": "group-b"},
            ]
        }
    )
    assert sample_id is None and group_id is None
    assert issues[0]["code"] == "sample_binding_unresolved"


def test_current_multi_sample_or_group_binding_fails_closed():
    handoff = current_handoff()
    handoff["research_action_package_v2"]["macro_steps"].append(
        {"sample_id": "specimen-other", "group_id": "cohort-other"}
    )

    sample_id, group_id, issues = resolve_frozen_sample_binding(handoff)

    assert sample_id is None and group_id is None
    assert {issue["code"] for issue in issues} == {"sample_binding_conflict"}


def test_non_numeric_macro_and_non_one_boundary_use_structured_resources():
    source = candidate()
    matrix_before = copy.deepcopy(source["sample_control_matrix"])

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert issues == []
    assert any(item.startswith("handoff_add:0:feed-A") for item in applied)
    assert updated["offline_handoffs"][0]["source_material_identity_ids"] == [
        "feed-A"
    ]
    transition = updated["material_transitions"][0]
    assert transition["source_macro_steps"] == ["phase-alpha"]
    derived = next(
        batch for batch in updated["batch_plan"] if not batch["is_root_batch"]
    )
    assert derived["sample_id"] == "specimen-42"
    assert derived["group_id"] == "cohort-red"
    assert all(
        entry.get("material_identity_id")
        for entry in updated["material_ledger"]["entries"]
    )
    assert updated["sample_control_matrix"] == matrix_before

    again, again_issues, again_applied = ensure_binding_ledger(
        updated,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )
    assert again_issues == []
    assert again == updated
    assert again_applied == []


def test_numeric_macro_one_boundary_keeps_generic_legacy_compatibility():
    updated, issues, _ = ensure_binding_ledger(
        candidate(macro_id=1, sample_id="SAMPLE-X", group_id="GRP-X", resource_id="H2O_DI"),
        current_handoff("SAMPLE-X", "GRP-X"),
        semantic_contract(macro_id=1, resource_id="H2O_DI"),
        category_of_workstation=category,
        frozen_sample_id="SAMPLE-X",
        frozen_group_id="GRP-X",
    )
    assert issues == []
    assert updated["offline_handoffs"][0]["source_material_identity_ids"] == [
        "H2O_DI"
    ]
    assert updated["material_transitions"][0]["source_macro_steps"] == [1]
    assert isinstance(
        updated["material_transitions"][0]["source_macro_steps"][0], int
    )


def test_integer_and_string_macro_ids_are_distinct_typed_identities():
    source = candidate(macro_id=1, resource_id="feed-int")
    source["offline_handoffs"][0]["source_macro_steps"] = ["1"]
    semantic = semantic_contract(macro_id=1, resource_id="feed-int")
    semantic["material_identity_registry"].append(
        {
            "identity_id": "feed-string",
            "roles": ["reagent"],
            "canonical_name_variants": ["String feed"],
        }
    )
    semantic["macro_step_assessments"].append(
        {
            "source_macro_step": "1",
            "required_capabilities": [],
            "material_identities": [{"identity_id": "feed-string"}],
        }
    )

    updated, issues, _ = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert issues == []
    assert updated["offline_handoffs"][0]["source_material_identity_ids"] == []
    assert updated["material_transitions"][0]["source_macro_steps"] == [1]


def test_binding_ledger_preserves_typed_plan_ids_and_uses_list_order_only():
    typed_plan_ids = [0, 1, "1", "001", 1.0, "phase-alpha"]
    source = candidate()
    source["device_plan"] = [
        {
            "plan_step": "feed-input",
            "workstation": "feed station",
            "containers": {"容器编号": [1]},
            "source_macro_steps": ["phase-alpha"],
            "source_material_identity_ids": ["feed-A"],
            "material_event_kind": "none",
        },
        *[
            {
                "plan_step": plan_id,
                "workstation": "reactor",
                "containers": {"容器编号": [1]},
                "source_macro_steps": ["phase-alpha"],
                "source_material_identity_ids": ["product-A"],
                "material_event_kind": "state_change",
            }
            for plan_id in typed_plan_ids
        ],
    ]

    updated, issues, _ = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert issues == []
    transition_refs = [
        transition["source_plan_steps"][0]
        for transition in updated["material_transitions"]
    ]
    assert [macro_id_key(value) for value in transition_refs] == [
        macro_id_key(value) for value in typed_plan_ids
    ]
    assert [
        type(step["plan_step"])
        for step in updated["device_plan"][1:]
    ] == [type(value) for value in typed_plan_ids]
    assert all(
        step.get("material_transition_ids")
        for step in updated["device_plan"][1:]
    )


def test_duplicate_plan_ids_fail_closed_but_typed_lookalikes_do_not_collide():
    source = candidate()
    source["device_plan"].append(
        {
            **copy.deepcopy(source["device_plan"][1]),
            "plan_step": 1,
        }
    )
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert applied == []
    assert [issue["code"] for issue in issues] == ["duplicate_plan_step_id"]

    source["device_plan"][-1]["plan_step"] = "1"
    updated, issues, _ = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )
    assert issues == []
    assert updated["device_plan"][1]["plan_step"] == 2
    assert updated["device_plan"][2]["plan_step"] == "1"


@pytest.mark.parametrize("invalid_id", [None, True, False, [], {}, float("inf")])
def test_invalid_plan_id_fails_closed_without_zero_or_position_fallback(invalid_id):
    source = candidate()
    source["device_plan"][0]["plan_step"] = invalid_id
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert applied == []
    assert [issue["code"] for issue in issues] == ["invalid_plan_step_id"]


def test_unknown_and_duplicate_explicit_plan_references_fail_closed():
    source = candidate()
    source["batch_plan"] = [
        {
            "batch_id": "legacy",
            "source_plan_steps": ["missing", "missing"],
        }
    ]
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert applied == []
    codes = [issue["code"] for issue in issues]
    assert codes.count("unknown_plan_step_reference") == 2
    assert "duplicate_plan_step_reference" in codes


def test_multiple_input_boundaries_for_same_macro_are_not_guessed():
    source = candidate()
    source["offline_handoffs"].append(
        copy.deepcopy(source["offline_handoffs"][0])
    )
    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )
    assert any(
        issue["code"] == "ambiguous_input_boundary_handoffs"
        for issue in issues
    )
    assert updated == source
    assert applied == []


def test_guarded_apply_conflict_is_visible_and_atomic():
    source = candidate()
    source["batch_plan"] = [
        {
            "batch_id": "RB_feed_a",
            "material_identity_id": "feed-A",
            "unexpected": "conflicting existing record",
        }
    ]
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert [issue["code"] for issue in issues] == [
        "binding_ledger_apply_conflict"
    ]
    assert any(item.startswith("conflict:batch:RB_feed_a") for item in applied)


def test_alias_or_purpose_text_cannot_create_container_identity():
    registry = {
        "product-A": {
            "identity_id": "product-A",
            "roles": ["sample"],
            "aliases": ["magic alias"],
        }
    }
    entries, issues = build_instance_registry(
        {
            "container_plan": [
                {
                    "容器类型": "进样瓶",
                    "容器编号": 1,
                    "sample_id": "specimen-42 magic alias",
                    "用途": "contains magic alias",
                }
            ]
        },
        registry,
        frozen_sample_ids=("specimen-42",),
    )
    assert entries == []
    assert issues[0]["code"] == "container_identity_unresolved"


def test_structured_container_binding_without_container_id_fails_closed():
    entries, issues = build_instance_registry(
        {
            "container_plan": [
                {
                    "容器类型": "any-vessel",
                    "sample_id": "specimen-42",
                    "material_identity_id": "product-A",
                }
            ]
        },
        {
            "product-A": {
                "identity_id": "product-A",
                "roles": ["sample"],
            }
        },
        frozen_sample_ids=("specimen-42",),
    )

    assert entries == []
    assert [issue["code"] for issue in issues] == ["container_id_missing"]


def test_single_agent_surfaces_binding_resolution_issue_without_mutation():
    handoff = current_handoff()
    handoff["research_action_package_v2"]["macro_action"][
        "experiment_group"
    ] = {"sample_id": "conflict", "group_id": "conflict"}
    state = SingleDeviceAgentState(research_handoff=handoff, exp_id="binding")
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._active_semantic_analysis = semantic_contract()
    source = {"device_plan": [{"plan_step": 1}]}
    original = copy.deepcopy(source)

    result = agent._ensure_binding_ledger_filled(state, source)

    assert result == original
    assert source == original
    assert state.binding_ledger_issues
    assert state.binding_ledger_issues[0]["code"] == "sample_binding_conflict"


def test_invalid_macro_source_becomes_ledger_issue_and_is_atomic():
    source = candidate()
    source["device_plan"][0]["source_macro_steps"] = [{"not": "a scalar"}]
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert any(issue["code"] == "invalid_macro_id" for issue in issues)


def test_string_source_material_identity_array_fails_closed_without_character_split():
    source = candidate()
    source["offline_handoffs"][0]["source_material_identity_ids"] = "already"
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "invalid_source_material_identity_ids"
    ]


def test_string_material_transition_ids_fails_closed_without_character_split():
    source = candidate()
    source["device_plan"][1]["material_transition_ids"] = "legacy-id"
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "invalid_material_transition_ids"
    ]


@pytest.mark.parametrize("container_id", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_container_identity_is_rejected(container_id):
    entries, issues = build_instance_registry(
        {
            "container_plan": [
                {
                    "容器编号": container_id,
                    "sample_id": "specimen-42",
                    "material_identity_id": "product-A",
                }
            ]
        },
        {
            "product-A": {
                "identity_id": "product-A",
                "roles": ["sample"],
            }
        },
        frozen_sample_ids=("specimen-42",),
    )

    assert entries == []
    assert [issue["code"] for issue in issues] == ["container_id_missing"]


def test_duplicate_container_identity_conflict_fails_closed():
    source = candidate()
    source["container_plan"].append(
        {
            **copy.deepcopy(source["container_plan"][0]),
            "material_identity_id": "other-product",
        }
    )
    original = copy.deepcopy(source)
    semantic = semantic_contract()
    semantic["material_identity_registry"].append(
        {
            "identity_id": "other-product",
            "roles": ["sample"],
            "canonical_name_variants": ["Other product"],
        }
    )

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "duplicate_container_identity_conflict"
    ]


def test_zero_instance_ref_is_not_replaced_by_truthiness_default():
    source = candidate()
    source["container_plan"][0]["instance_ref"] = 0

    updated, issues, _ = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert issues == []
    derived = next(
        batch for batch in updated["batch_plan"] if not batch["is_root_batch"]
    )
    assert derived["instance_ref"] == 0


def test_numeric_source_material_identity_cannot_alias_string_registry_id():
    source = candidate(resource_id="1")
    source["device_plan"][0]["source_material_identity_ids"] = [1]
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(resource_id="1"),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "invalid_source_material_identity_id"
    ]


def test_numeric_registry_identity_cannot_overwrite_string_identity():
    source = candidate(resource_id="1")
    original = copy.deepcopy(source)
    semantic = semantic_contract(resource_id="1")
    semantic["material_identity_registry"].insert(
        0,
        {
            "identity_id": 1,
            "roles": ["reagent"],
            "canonical_name_variants": ["Numeric identity"],
        },
    )

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "invalid_material_identity_id"
    ]
    assert issues[0]["context"]["actual"] == 1


def test_duplicate_string_registry_identity_fails_closed_without_overwrite():
    source = candidate()
    original = copy.deepcopy(source)
    semantic = semantic_contract()
    semantic["material_identity_registry"].append(
        {
            "identity_id": "feed-A",
            "roles": ["sample"],
            "canonical_name_variants": ["Conflicting duplicate"],
        }
    )

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "duplicate_material_identity_id"
    ]


@pytest.mark.parametrize("malformed", [None, {}, "not-an-array"])
def test_malformed_macro_step_assessments_fail_closed(malformed):
    source = candidate()
    original = copy.deepcopy(source)
    semantic = semantic_contract()
    semantic["macro_step_assessments"] = malformed

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "invalid_macro_step_assessments"
    ]


def test_conflicting_step_container_aliases_fail_closed_atomically():
    source = candidate()
    source["device_plan"][1]["containers"]["container_ids"] = [2]
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "conflicting_container_id_aliases"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_id", "different-sample"),
        ("group_id", "different-group"),
        ("instance_ref", "different-instance"),
    ],
)
def test_duplicate_container_full_binding_conflict_fails_closed(field, value):
    source = candidate()
    duplicate = copy.deepcopy(source["container_plan"][0])
    duplicate[field] = value
    source["container_plan"].append(duplicate)
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic_contract(),
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == [
        "duplicate_container_binding_conflict"
    ]


def _dual_identity_case():
    source = candidate()
    source["device_plan"][1]["source_material_identity_ids"] = [
        "product-B",
        "product-A",
    ]
    container = source["container_plan"][0]
    container.pop("material_identity_id")
    container["material_identity_ids"] = ["product-B", "product-A"]
    semantic = semantic_contract()
    semantic["material_identity_registry"].append(
        {
            "identity_id": "product-B",
            "roles": ["sample"],
            "canonical_name_variants": ["Product B"],
        }
    )
    semantic["macro_step_assessments"][0]["material_identities"].append(
        {"identity_id": "product-B"}
    )
    return source, semantic


def test_multi_hit_without_structured_primary_fails_closed():
    source, semantic = _dual_identity_case()
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == ["batch_identity_ambiguous"]
    assert issues[0]["context"]["structured_primary_identity_ids"] == []


def test_multi_hit_with_conflicting_structured_primaries_fails_closed():
    source, semantic = _dual_identity_case()
    source["container_plan"][0]["material_identity_id"] = "product-A"
    source["device_plan"][1]["material_identity_id"] = "product-B"
    original = copy.deepcopy(source)

    updated, issues, applied = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert updated == original
    assert source == original
    assert applied == []
    assert [issue["code"] for issue in issues] == ["batch_identity_ambiguous"]
    assert issues[0]["context"]["structured_primary_identity_ids"] == [
        "product-A",
        "product-B",
    ]


def test_multi_hit_uses_unique_structured_primary_not_lexical_order():
    source, semantic = _dual_identity_case()
    source["container_plan"][0]["material_identity_id"] = "product-B"

    updated, issues, _ = ensure_binding_ledger(
        source,
        current_handoff(),
        semantic,
        category_of_workstation=category,
        frozen_sample_id="specimen-42",
        frozen_group_id="cohort-red",
    )

    assert issues == []
    derived = next(
        batch for batch in updated["batch_plan"] if not batch["is_root_batch"]
    )
    assert derived["material_identity_id"] == "product-B"
    assert derived["co_material_identity_ids"] == ["product-A"]
