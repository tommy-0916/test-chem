from __future__ import annotations

import copy

import pytest

from reagent_slot_identity import (
    RULE_ID,
    build_reagent_slot_registry,
    enrich_legacy_reagent_slot_plan,
    project_reagent_slot_identities,
    validate_reagent_slot_identity_roots,
    validate_reagent_slot_identities,
)


STATIONS = {
    "small liquid handler": "Liquid_Handler_Small_V1",
    "large liquid handler": "Liquid_Handler_Large_V1",
    "Liquid_Handler_Small_V1": "Liquid_Handler_Small_V1",
    "Liquid_Handler_Large_V1": "Liquid_Handler_Large_V1",
}


def resolve_station(value):
    return STATIONS.get(str(value))


def slot(station, number, identity, name):
    return {
        "工作站": station,
        "原液编号": number,
        "material_identity_id": identity,
        "名称": name,
    }


def workflow_step(station, source_plan_step, parameters):
    return {
        "workstation": station,
        "source_plan_step": source_plan_step,
        "parameters": parameters,
    }


def test_station_scoped_projection_is_pure_structured_and_idempotent():
    plan = [
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
        slot("large liquid handler", 1, "MAT_B", "Canonical B"),
    ]
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                10,
                {"加样方案": [{"1号原液瓶": {"配料名称": "alias A", "原液用量": 1}}]},
            ),
            workflow_step(
                "large liquid handler",
                20,
                {"加样方案": [{"1号原液瓶": {"配料名称": "alias B", "原液用量": 2}}]},
            ),
        ]
    }
    original = copy.deepcopy(workflow)
    plan_steps = [
        {"plan_step": 10, "source_material_identity_ids": ["MAT_A"]},
        {"plan_step": 20, "source_material_identity_ids": ["MAT_B"]},
    ]

    result = project_reagent_slot_identities(
        workflow, plan, station_resolver=resolve_station, plan_steps=plan_steps
    )

    assert result.ok
    assert workflow == original
    assert result.workflow["steps"][0]["parameters"]["加样方案"][0]["1号原液瓶"]["配料名称"] == "Canonical A"
    assert result.workflow["steps"][1]["parameters"]["加样方案"][0]["1号原液瓶"]["配料名称"] == "Canonical B"
    assert len(result.events) == 2
    assert result.events[0] == {
        "rule_id": RULE_ID,
        "event": "canonical_name_projected",
        "json_pointer": "/steps/0/parameters/加样方案/0/1号原液瓶/配料名称",
        "old": "alias A",
        "new": "Canonical A",
        "material_identity_id": "MAT_A",
        "station_code": "Liquid_Handler_Small_V1",
        "slot_number": 1,
    }

    again = project_reagent_slot_identities(
        result.workflow, plan, station_resolver=resolve_station, plan_steps=plan_steps
    )
    assert again.ok
    assert again.workflow == result.workflow
    assert again.events == ()


def test_plan_authorization_preserves_all_typed_scalar_identities():
    typed_ids = [0, 1, "1", "001", 1.0, "phase-alpha"]
    plan = [
        slot("small liquid handler", index, f"MAT_{index}", f"Canonical {index}")
        for index in range(1, len(typed_ids) + 1)
    ]
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                plan_id,
                {
                    f"{index}号原液瓶": {
                        "配料名称": f"old {index}",
                        "原液用量": 1,
                    }
                },
            )
            for index, plan_id in enumerate(typed_ids, start=1)
        ]
    }
    plan_steps = [
        {
            "plan_step": plan_id,
            "source_material_identity_ids": [f"MAT_{index}"],
        }
        for index, plan_id in enumerate(typed_ids, start=1)
    ]

    result = project_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=plan_steps,
    )

    assert result.ok
    assert [
        step["source_plan_step"] for step in result.workflow["steps"]
    ] == typed_ids
    assert [
        step["parameters"][f"{index}号原液瓶"]["配料名称"]
        for index, step in enumerate(result.workflow["steps"], start=1)
    ] == [f"Canonical {index}" for index in range(1, len(typed_ids) + 1)]


def test_integer_and_string_plan_ids_do_not_cross_authorize_slots():
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                "1",
                {"1号原液瓶": {"配料名称": "old", "原液用量": 1}},
            )
        ]
    }
    original = copy.deepcopy(workflow)

    result = project_reagent_slot_identities(
        workflow,
        [slot("small liquid handler", 1, "MAT_STRING", "String material")],
        station_resolver=resolve_station,
        plan_steps=[
            {"plan_step": 1, "source_material_identity_ids": ["MAT_STRING"]}
        ],
    )

    assert not result.ok
    assert result.workflow == original
    assert result.events == ()
    assert {error["code"] for error in result.errors} == {
        "unknown_source_plan_step"
    }


@pytest.mark.parametrize("invalid_id", [None, True, False, [], {}])
def test_invalid_plan_ids_fail_closed_without_mapping_or_position_fallback(invalid_id):
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                0,
                {"1号原液瓶": {"配料名称": "old", "原液用量": 1}},
            )
        ]
    }
    original = copy.deepcopy(workflow)

    result = project_reagent_slot_identities(
        workflow,
        [slot("small liquid handler", 1, "MAT_A", "Canonical A")],
        station_resolver=resolve_station,
        plan_steps={
            "fallback-key": {
                "plan_step": invalid_id,
                "source_material_identity_ids": ["MAT_A"],
            }
        },
    )

    assert not result.ok
    assert result.workflow == original
    assert result.events == ()
    assert "invalid_plan_step_id" in {
        error["code"] for error in result.errors
    }


def test_duplicate_and_conflicting_plan_id_aliases_fail_closed_by_typed_key():
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                1,
                {"1号原液瓶": {"配料名称": "old", "原液用量": 1}},
            )
        ]
    }
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]

    duplicate = project_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[
            {"plan_step": 1, "source_material_identity_ids": ["MAT_A"]},
            {"plan_step": 1, "source_material_identity_ids": ["MAT_A"]},
        ],
    )
    assert not duplicate.ok
    assert duplicate.workflow == workflow
    assert "duplicate_plan_step_id" in {
        error["code"] for error in duplicate.errors
    }

    conflict = project_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[
            {
                "plan_step": 1,
                "step_id": "1",
                "source_material_identity_ids": ["MAT_A"],
            }
        ],
    )
    assert not conflict.ok
    assert conflict.workflow == workflow
    assert "conflicting_plan_step_aliases" in {
        error["code"] for error in conflict.errors
    }


def test_conflicting_source_identity_list_aliases_fail_closed():
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                0,
                {"1号原液瓶": {"配料名称": "old", "原液用量": 1}},
            )
        ]
    }
    result = project_reagent_slot_identities(
        workflow,
        [slot("small liquid handler", 1, "MAT_A", "Canonical A")],
        station_resolver=resolve_station,
        plan_steps=[
            {
                "plan_step": 0,
                "source_material_identity_ids": ["MAT_A"],
                "源物料身份IDs": ["MAT_B"],
            }
        ],
    )

    assert not result.ok
    assert result.workflow == workflow
    assert result.events == ()
    assert "conflicting_source_material_identity_ids_aliases" in {
        error["code"] for error in result.errors
    }


def test_reordered_multi_source_identity_aliases_are_equivalent_sets():
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                0,
                {"1号原液瓶": {"配料名称": "old", "原液用量": 1}},
            )
        ]
    }
    result = project_reagent_slot_identities(
        workflow,
        [slot("small liquid handler", 1, "MAT_A", "Canonical A")],
        station_resolver=resolve_station,
        plan_steps=[
            {
                "plan_step": 0,
                "source_material_identity_ids": ["MAT_A", "MAT_B"],
                "源物料身份IDs": ["MAT_B", "MAT_A"],
            }
        ],
    )

    assert result.ok
    assert result.workflow["steps"][0]["parameters"]["1号原液瓶"][
        "配料名称"
    ] == "Canonical A"


def test_invalid_or_conflicting_workflow_source_plan_id_fails_closed():
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]
    plan_steps = [
        {"plan_step": 1, "source_material_identity_ids": ["MAT_A"]}
    ]
    invalid = {
        "steps": [
            workflow_step(
                "small liquid handler",
                True,
                {"1号原液瓶": {"配料名称": "old", "原液用量": 1}},
            )
        ]
    }
    invalid_result = project_reagent_slot_identities(
        invalid,
        plan,
        station_resolver=resolve_station,
        plan_steps=plan_steps,
    )
    assert not invalid_result.ok
    assert invalid_result.workflow == invalid
    assert "invalid_source_plan_step_id" in {
        error["code"] for error in invalid_result.errors
    }

    conflicting = copy.deepcopy(invalid)
    conflicting["steps"][0]["source_plan_step"] = 1
    conflicting["steps"][0]["来源计划步骤"] = "1"
    conflict_result = project_reagent_slot_identities(
        conflicting,
        plan,
        station_resolver=resolve_station,
        plan_steps=plan_steps,
    )
    assert not conflict_result.ok
    assert conflict_result.workflow == conflicting
    assert "conflicting_source_plan_step_aliases" in {
        error["code"] for error in conflict_result.errors
    }


def test_registry_builder_and_read_only_validation_share_projection_rules():
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]
    registry = build_reagent_slot_registry(plan, station_resolver=resolve_station)
    assert registry.ok
    binding = registry.bindings[("Liquid_Handler_Small_V1", 1)]
    assert binding.material_identity_id == "MAT_A"

    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler", 1, {"1号原液瓶": {"配料名称": "alias"}}
            )
        ]
    }
    checked = validate_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[{"plan_step": 1, "source_material_identity_ids": ["MAT_A"]}],
    )
    assert not checked.ok
    assert checked.errors[0]["code"] == "noncanonical_reagent_name"
    assert workflow["steps"][0]["parameters"]["1号原液瓶"]["配料名称"] == "alias"

    normalized = project_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[{"plan_step": 1, "source_material_identity_ids": ["MAT_A"]}],
    )
    rechecked = validate_reagent_slot_identities(
        normalized.workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[{"plan_step": 1, "source_material_identity_ids": ["MAT_A"]}],
    )
    assert rechecked.ok


def test_slot_identity_roots_require_independent_structured_membership():
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]

    unanchored = validate_reagent_slot_identity_roots(plan)
    assert not unanchored.anchored
    assert unanchored.claimed_identity_ids == frozenset({"MAT_A"})
    assert unanchored.untrusted_identity_ids == frozenset({"MAT_A"})

    anchored = validate_reagent_slot_identity_roots(
        plan,
        batch_plan=[
            {
                "is_root_batch": True,
                "material_identity_id": "MAT_A",
            }
        ],
    )
    assert anchored.anchored
    assert anchored.untrusted_identity_ids == frozenset()
    assert anchored.evidence_sources == ("batch_plan.root",)


def test_names_and_nonroot_batches_cannot_bootstrap_identity_trust():
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]
    original = copy.deepcopy(plan)

    result = validate_reagent_slot_identity_roots(
        plan,
        batch_plan=[
            {
                "is_root_batch": False,
                "material_identity_id": "MAT_A",
                "material_id": "Canonical A",
            }
        ],
        material_identity_registry=[
            {"canonical_name": "Canonical A", "aliases": ["MAT_A"]}
        ],
    )

    assert not result.anchored
    assert result.trusted_identity_ids == frozenset()
    assert plan == original


def test_registry_and_ledger_membership_are_valid_identity_roots():
    plan = [
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
        slot("small liquid handler", 2, "MAT_B", "Canonical B"),
    ]

    result = validate_reagent_slot_identity_roots(
        plan,
        material_identity_registry=[{"identity_id": "MAT_A"}],
        material_ledger={
            "entries": [{"material_identity_id": "MAT_B"}]
        },
    )

    assert result.anchored
    assert result.trusted_identity_ids == frozenset({"MAT_A", "MAT_B"})
    assert result.evidence_sources == (
        "material_ledger.entries",
        "material_identity_registry",
    )


def test_generic_n_bottle_list_and_missing_display_name_are_projected():
    plan = [slot("small liquid handler", 2, "MAT_B", "Canonical B")]
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                "p1",
                {
                    "加样方案": [
                        {
                            "N号原液瓶": [
                                {"瓶号": 2, "配料名称": "old", "原液用量": 1},
                                {"瓶号": "2", "原液用量": 2},
                            ]
                        }
                    ]
                },
            )
        ]
    }
    result = project_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[{"step_id": "p1", "source_material_identity_ids": ["MAT_B"]}],
    )
    entries = result.workflow["steps"][0]["parameters"]["加样方案"][0]["N号原液瓶"]
    assert result.ok
    assert [entry["配料名称"] for entry in entries] == ["Canonical B", "Canonical B"]
    assert result.events[1]["old"] is None
    assert result.events[1]["json_pointer"].endswith("/N号原液瓶/1/配料名称")


def test_chinese_identity_alias_is_supported_without_name_inference():
    plan = [
        {
            "工作站编码": "Liquid_Handler_Small_V1",
            "原液瓶号": "01",
            "物料身份ID": "MAT_A",
            "规范名称": "Canonical A",
        }
    ]
    workflow = {
        "steps": [
            {
                "station_code": "Liquid_Handler_Small_V1",
                "parameters": {"01号原液瓶": {"配料名称": "unrelated text"}},
            }
        ]
    }
    result = project_reagent_slot_identities(
        workflow, plan, station_resolver=resolve_station
    )
    assert result.ok
    assert result.workflow["steps"][0]["parameters"]["01号原液瓶"]["配料名称"] == "Canonical A"


def test_missing_identity_fails_closed_and_preserves_all_workflow_values():
    plan = [{"工作站": "small liquid handler", "原液编号": 1, "名称": "Canonical A"}]
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler", 1, {"1号原液瓶": {"配料名称": "old"}}
            )
        ]
    }
    result = project_reagent_slot_identities(
        workflow, plan, station_resolver=resolve_station
    )
    assert not result.ok
    assert result.workflow == workflow
    assert result.events == ()
    assert "missing_material_identity_id" in {error["code"] for error in result.errors}


def test_duplicate_and_ambiguous_bindings_are_rejected():
    workflow = {"steps": []}
    duplicate = [
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
    ]
    duplicate_result = project_reagent_slot_identities(
        workflow, duplicate, station_resolver=resolve_station
    )
    assert "duplicate_slot_binding" in {error["code"] for error in duplicate_result.errors}

    ambiguous = [
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
        slot("small liquid handler", 1, "MAT_B", "Canonical B"),
    ]
    ambiguous_result = project_reagent_slot_identities(
        workflow, ambiguous, station_resolver=resolve_station
    )
    assert "ambiguous_slot_binding" in {error["code"] for error in ambiguous_result.errors}


def test_one_identity_cannot_have_two_canonical_names():
    plan = [
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
        slot("large liquid handler", 2, "MAT_A", "Different label"),
    ]
    result = project_reagent_slot_identities(
        {"steps": []}, plan, station_resolver=resolve_station
    )
    assert "identity_canonical_name_conflict" in {
        error["code"] for error in result.errors
    }


def test_missing_slot_binding_fails_closed():
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler", 1, {"2号原液瓶": {"配料名称": "old"}}
            )
        ]
    }
    result = project_reagent_slot_identities(
        workflow,
        [slot("small liquid handler", 1, "MAT_A", "Canonical A")],
        station_resolver=resolve_station,
    )
    assert not result.ok
    assert result.workflow == workflow
    assert result.errors[0]["code"] == "missing_slot_binding"


def test_unauthorized_identity_blocks_every_change_atomically():
    plan = [
        slot("small liquid handler", 1, "MAT_A", "Canonical A"),
        slot("small liquid handler", 2, "MAT_B", "Canonical B"),
    ]
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler", 1, {"1号原液瓶": {"配料名称": "old A"}}
            ),
            workflow_step(
                "small liquid handler", 2, {"2号原液瓶": {"配料名称": "old B"}}
            ),
        ]
    }
    result = project_reagent_slot_identities(
        workflow,
        plan,
        station_resolver=resolve_station,
        plan_steps=[
            {"plan_step": 1, "source_material_identity_ids": ["MAT_A"]},
            {"plan_step": 2, "source_material_identity_ids": ["OTHER"]},
        ],
    )
    assert not result.ok
    assert result.workflow == workflow
    assert result.events == ()
    unauthorized = next(
        error for error in result.errors if error["code"] == "unauthorized_slot_identity"
    )
    assert unauthorized["material_identity_id"] == "MAT_B"
    assert unauthorized["authorized_material_identity_ids"] == ["OTHER"]


def test_invalid_generic_bottle_number_fails_closed():
    workflow = {
        "steps": [
            workflow_step(
                "small liquid handler",
                1,
                {"N号原液瓶": [{"瓶号": "N", "配料名称": "old"}]},
            )
        ]
    }
    result = project_reagent_slot_identities(
        workflow,
        [slot("small liquid handler", 1, "MAT_A", "Canonical A")],
        station_resolver=resolve_station,
    )
    assert not result.ok
    assert result.workflow == workflow
    assert "invalid_slot_number" in {error["code"] for error in result.errors}


def test_instantiated_bottle_internal_slot_and_identity_conflicts_fail_closed():
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]
    internal_slot_conflict = {
        "steps": [
            workflow_step(
                "small liquid handler",
                1,
                {"1号原液瓶": {"瓶号": 2, "配料名称": "old"}},
            )
        ]
    }
    result = project_reagent_slot_identities(
        internal_slot_conflict, plan, station_resolver=resolve_station
    )
    assert not result.ok
    assert result.workflow == internal_slot_conflict
    assert "bottle_slot_conflict" in {error["code"] for error in result.errors}

    identity_conflict = {
        "steps": [
            workflow_step(
                "small liquid handler",
                1,
                {
                    "1号原液瓶": {
                        "material_identity_id": "MAT_B",
                        "配料名称": "old",
                    }
                },
            )
        ]
    }
    result = project_reagent_slot_identities(
        identity_conflict, plan, station_resolver=resolve_station
    )
    assert not result.ok
    assert result.workflow == identity_conflict
    assert "workflow_slot_identity_conflict" in {
        error["code"] for error in result.errors
    }


def test_scanner_only_projects_source_bottles_under_parameters():
    plan = [slot("small liquid handler", 1, "MAT_A", "Canonical A")]
    workflow = {
        "steps": [
            {
                "workstation": "small liquid handler",
                "source_plan_step": 1,
                "parameters": {},
                "provenance": {"1号原液瓶": {"配料名称": "historical evidence"}},
            }
        ]
    }
    result = project_reagent_slot_identities(
        workflow, plan, station_resolver=resolve_station
    )
    assert result.ok
    assert result.events == ()
    assert result.workflow == workflow


def test_legacy_enrichment_uses_verbatim_trusted_identity_only():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "名称": "a name that is not evidence",
            "来源": "validated source (ROOT_A)",
        }
    ]
    batch_plan = [
        {"is_root_batch": True, "material_identity_id": "ROOT_A"},
        {"is_root_batch": False, "material_identity_id": "DERIVED_A"},
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan, station_resolver=resolve_station, batch_plan=batch_plan
    )
    assert result.ok
    assert "material_identity_id" not in plan[0]
    assert result.reagent_slot_plan[0]["material_identity_id"] == "ROOT_A"
    assert result.events[0]["evidence"] == "verbatim_source_identity"


def test_structured_source_value_is_not_coerced_into_verbatim_identity_evidence():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "名称": "display",
            "来源": {"unrelated_note": "ROOT_A"},
        }
    ]

    result = enrich_legacy_reagent_slot_plan(
        plan,
        station_resolver=resolve_station,
        known_identity_ids=["ROOT_A"],
    )

    assert not result.ok
    assert result.reagent_slot_plan == plan
    assert result.events == ()
    assert result.errors[0]["code"] == "unresolved_legacy_slot_identity"


def test_legacy_enrichment_can_use_unique_structured_device_authorization():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 3,
            "名称": "display only",
            "来源": "legacy import",
        }
    ]
    device_plan = [
        {
            "plan_step": 4,
            "workstation": "small liquid handler",
            "key_values": {"原液": "3号原液瓶"},
            "source_material_identity_ids": ["ROOT_C"],
        },
        {
            "plan_step": 5,
            "workstation": "small liquid handler",
            "key_values": {"加样方案": [{"3号原液瓶(root)": "1 mL"}]},
            "source_material_identity_ids": ["ROOT_C"],
        },
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan,
        station_resolver=resolve_station,
        known_identity_ids=["ROOT_C", "ROOT_D"],
        device_plan=device_plan,
    )
    assert result.ok
    assert result.reagent_slot_plan[0]["material_identity_id"] == "ROOT_C"
    assert result.events[0]["evidence"] == "unique_device_plan_authorization"


def test_root_material_registry_unifies_legacy_names_across_stations():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "名称": "short label",
            "来源": "root (ROOT_A)",
        },
        {
            "工作站": "large liquid handler",
            "原液编号": 2,
            "名称": "another legacy label",
            "来源": "root (ROOT_A)",
        },
    ]
    batch_plan = [
        {
            "is_root_batch": True,
            "material_identity_id": "ROOT_A",
            "research_material_identity_id": "ROOT_A",
            "material_id": "Authoritative root material",
        }
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan, station_resolver=resolve_station, batch_plan=batch_plan
    )
    assert result.ok
    assert [
        record["canonical_name"] for record in result.reagent_slot_plan
    ] == ["Authoritative root material", "Authoritative root material"]
    assert [record["名称"] for record in result.reagent_slot_plan] == [
        "Authoritative root material",
        "Authoritative root material",
    ]
    assert plan[0]["名称"] == "short label"

    registry = build_reagent_slot_registry(
        result.reagent_slot_plan, station_resolver=resolve_station
    )
    assert registry.ok
    again = enrich_legacy_reagent_slot_plan(
        result.reagent_slot_plan,
        station_resolver=resolve_station,
        batch_plan=batch_plan,
    )
    assert again.ok
    assert again.reagent_slot_plan == result.reagent_slot_plan
    assert again.events == ()


def test_root_registry_canonicalizes_a_legacy_slot_with_existing_identity():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "material_identity_id": "ROOT_A",
            "名称": "short label",
        }
    ]
    batch_plan = [
        {
            "is_root_batch": True,
            "material_identity_id": "ROOT_A",
            "canonical_name": "Authoritative A",
        }
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan, station_resolver=resolve_station, batch_plan=batch_plan
    )
    assert result.ok
    assert result.reagent_slot_plan[0]["canonical_name"] == "Authoritative A"
    assert result.reagent_slot_plan[0]["名称"] == "Authoritative A"
    assert all(event["event"] != "legacy_identity_enriched" for event in result.events)


def test_conflicting_trusted_root_names_fail_closed():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "名称": "legacy",
            "来源": "ROOT_A",
        }
    ]
    batch_plan = [
        {
            "is_root_batch": True,
            "material_identity_id": "ROOT_A",
            "material_id": "Canonical A",
        },
        {
            "is_root_batch": True,
            "material_identity_id": "ROOT_A",
            "material_id": "Conflicting A",
        },
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan, station_resolver=resolve_station, batch_plan=batch_plan
    )
    assert not result.ok
    assert result.reagent_slot_plan == plan
    assert result.events == ()
    assert "conflicting_trusted_root_names" in {
        error["code"] for error in result.errors
    }


def test_legacy_enrichment_is_fail_closed_on_ambiguous_authorization():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 3,
            "名称": "display only",
        }
    ]
    device_plan = [
        {
            "workstation": "small liquid handler",
            "key_values": {"3号原液瓶(root)": "1 mL"},
            "source_material_identity_ids": ["ROOT_C", "ROOT_D"],
        }
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan,
        station_resolver=resolve_station,
        known_identity_ids=["ROOT_C", "ROOT_D"],
        device_plan=device_plan,
    )
    assert not result.ok
    assert result.reagent_slot_plan == plan
    assert result.events == ()
    assert result.errors[0]["code"] == "unresolved_legacy_slot_identity"


def test_unknown_device_authorization_cannot_be_filtered_into_false_unique_match():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 3,
            "名称": "display only",
        }
    ]
    device_plan = [
        {
            "workstation": "small liquid handler",
            "key_values": {"3号原液瓶(root)": "1 mL"},
            "source_material_identity_ids": ["ROOT_C", "UNKNOWN_CANDIDATE"],
        }
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan,
        station_resolver=resolve_station,
        known_identity_ids=["ROOT_C"],
        device_plan=device_plan,
    )
    assert not result.ok
    assert result.reagent_slot_plan == plan
    unknown = next(
        error
        for error in result.errors
        if error["code"] == "unknown_device_material_identity_id"
    )
    assert unknown["material_identity_ids"] == ["UNKNOWN_CANDIDATE"]


def test_known_derived_id_is_ignored_when_selecting_unique_root_authority():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 3,
            "名称": "legacy",
        }
    ]
    batch_plan = [
        {
            "is_root_batch": True,
            "material_identity_id": "ROOT_C",
            "material_id": "Canonical C",
        },
        {
            "is_root_batch": False,
            "material_identity_id": "DERIVED_SAMPLE",
            "material_id": "Derived sample",
        },
    ]
    device_plan = [
        {
            "workstation": "small liquid handler",
            "key_values": {"3号原液瓶(root)": "1 mL"},
            "source_material_identity_ids": ["ROOT_C", "DERIVED_SAMPLE"],
        }
    ]
    result = enrich_legacy_reagent_slot_plan(
        plan,
        station_resolver=resolve_station,
        batch_plan=batch_plan,
        device_plan=device_plan,
    )
    assert result.ok
    assert result.reagent_slot_plan[0]["material_identity_id"] == "ROOT_C"
    assert result.reagent_slot_plan[0]["canonical_name"] == "Canonical C"


def test_legacy_enrichment_rejects_identity_substring_and_evidence_conflict():
    plan = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "名称": "display",
            "来源": "ROOT_A2",
        }
    ]
    no_substring = enrich_legacy_reagent_slot_plan(
        plan,
        station_resolver=resolve_station,
        known_identity_ids=["ROOT_A"],
    )
    assert not no_substring.ok
    assert no_substring.errors[0]["code"] == "unresolved_legacy_slot_identity"

    conflict_device_plan = [
        {
            "workstation": "small liquid handler",
            "key_values": {"1号原液瓶(other)": "1 mL"},
            "source_material_identity_ids": ["ROOT_B"],
        }
    ]
    conflict_plan = [dict(plan[0], 来源="ROOT_A")]
    conflict = enrich_legacy_reagent_slot_plan(
        conflict_plan,
        station_resolver=resolve_station,
        known_identity_ids=["ROOT_A", "ROOT_B"],
        device_plan=conflict_device_plan,
    )
    assert not conflict.ok
    assert conflict.errors[0]["code"] == "legacy_identity_evidence_conflict"


def test_legacy_enrichment_uses_unicode_aware_identity_token_boundaries():
    embedded = [
        {
            "工作站": "small liquid handler",
            "原液编号": 1,
            "名称": "display",
            "来源": "去离子水溶液",
        }
    ]
    embedded_result = enrich_legacy_reagent_slot_plan(
        embedded,
        station_resolver=resolve_station,
        known_identity_ids=["水"],
    )
    assert not embedded_result.ok
    assert embedded_result.reagent_slot_plan == embedded
    assert embedded_result.errors[0]["code"] == "unresolved_legacy_slot_identity"

    independent = [dict(embedded[0], 来源="validated source（水）")]
    independent_result = enrich_legacy_reagent_slot_plan(
        independent,
        station_resolver=resolve_station,
        known_identity_ids=["水"],
    )
    assert independent_result.ok
    assert independent_result.reagent_slot_plan[0]["material_identity_id"] == "水"
    assert independent_result.events[0]["evidence"] == "verbatim_source_identity"


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} reagent slot identity tests passed")
