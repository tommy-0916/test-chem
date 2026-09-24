"""Regression tests for shared, pre-gate workflow representation rules."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from device_agent.workflow_normalizer import SkillContractEngine


CONTRACT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "chem_resources"
    / "lab-design-all"
    / "skills"
    / "chemistry-experiment-workstation"
)


def engine() -> SkillContractEngine:
    return SkillContractEngine(CONTRACT_ROOT)


def stirring_step(speed="600 rpm", duration="2 min") -> dict:
    return {
        "step_number": 1,
        "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "operation": "开始搅拌",
        "parameters": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [1],
            "搅拌速度": speed,
            "搅拌时间": duration,
        },
    }


def liquid_step(name="legacy label", bottle=1) -> dict:
    return {
        "step_number": 2,
        "source_plan_step": 7,
        "workstation": "Liquid_Handling_Station_1ml_V1",
        "operation": "加液_物料绑定",
        "parameters": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [1],
            "加样方案": [
                {
                    "加样瓶号": "1",
                    f"{bottle}号原液瓶": {
                        "配料名称": name,
                        "原液用量": "0.5 mL",
                    },
                }
            ],
        },
    }


def test_unit_bearing_contract_scalars_are_normalized_and_audited():
    workflow = {"steps": [stirring_step()]}
    events: list[dict] = []

    notes = engine().normalize_workflow(
        workflow, normalization_events=events
    )

    parameters = workflow["steps"][0]["parameters"]
    assert parameters["搅拌速度"] == 600
    assert parameters["搅拌时间"] == 2
    scalar_events = [
        item for item in events
        if item["rule_id"] == "contract_unit_scalar/v1"
    ]
    assert len(scalar_events) == 2
    assert {item["original_value"] for item in scalar_events} == {
        "600 rpm",
        "2 min",
    }
    assert {item["new_value"] for item in scalar_events} == {600, 2}
    assert any("coerced 2 parameter value" in note for note in notes)


def test_wrong_unit_is_not_converted_or_silently_accepted():
    workflow = {"steps": [stirring_step(speed="600 min")]}
    events: list[dict] = []

    engine().normalize_workflow(workflow, normalization_events=events)

    assert workflow["steps"][0]["parameters"]["搅拌速度"] == "600 min"
    assert not any(
        item.get("json_pointer", "").endswith("/搅拌速度")
        and item.get("rule_id") == "contract_unit_scalar/v1"
        for item in events
    )
    errors = engine().contract_audit_errors(workflow)
    assert any("type_mismatch" in item for item in errors)


def test_identity_projection_and_numeric_normalization_share_one_audit_schema():
    workflow = {"steps": [liquid_step()]}
    slot_plan = [
        {
            "工作站": "Liquid_Handling_Station_1ml_V1",
            "原液编号": 1,
            "material_identity_id": "MAT_WATER",
            "canonical_name": "Water canonical",
        }
    ]
    plan_steps = [
        {"plan_step": 7, "source_material_identity_ids": ["MAT_WATER"]}
    ]
    events: list[dict] = []
    issues: list[dict] = []

    engine().normalize_workflow(
        workflow,
        reagent_slot_plan=slot_plan,
        plan_steps=plan_steps,
        batch_plan=[
            {"is_root_batch": True, "material_identity_id": "MAT_WATER"}
        ],
        normalization_events=events,
        normalization_issues=issues,
    )

    bottle = workflow["steps"][0]["parameters"]["加样方案"][0]["1号原液瓶"]
    assert issues == []
    assert bottle["配料名称"] == "Water canonical"
    assert bottle["原液用量"] == 0.5
    identity_event = next(
        item for item in events
        if item["rule_id"] == "reagent_slot_identity/v1"
    )
    assert identity_event["original_value"] == "legacy label"
    assert identity_event["new_value"] == "Water canonical"
    assert "old" not in identity_event and "new" not in identity_event


def test_slot_projection_failure_is_atomic_but_does_not_rollback_scalars():
    workflow = {"steps": [stirring_step(), liquid_step(bottle=2)]}
    original_liquid = copy.deepcopy(workflow["steps"][1])
    events: list[dict] = []
    issues: list[dict] = []

    engine().normalize_workflow(
        workflow,
        reagent_slot_plan=[
            {
                "工作站": "Liquid_Handling_Station_1ml_V1",
                "原液编号": 1,
                "material_identity_id": "MAT_WATER",
                "canonical_name": "Water canonical",
            }
        ],
        plan_steps=[
            {"plan_step": 7, "source_material_identity_ids": ["MAT_WATER"]}
        ],
        batch_plan=[
            {"is_root_batch": True, "material_identity_id": "MAT_WATER"}
        ],
        normalization_events=events,
        normalization_issues=issues,
    )

    assert any(item["code"] == "missing_slot_binding" for item in issues)
    assert workflow["steps"][0]["parameters"]["搅拌速度"] == 600
    assert workflow["steps"][0]["parameters"]["搅拌时间"] == 2
    assert (
        workflow["steps"][1]["parameters"]["加样方案"][0]["2号原液瓶"]["配料名称"]
        == original_liquid["parameters"]["加样方案"][0]["2号原液瓶"]["配料名称"]
    )
    assert not any(
        item["rule_id"] == "reagent_slot_identity/v1" for item in events
    )
    assert sum(
        item["rule_id"] == "contract_unit_scalar/v1" for item in events
    ) >= 2


def test_unanchored_slot_ids_block_only_identity_projection():
    workflow = {"steps": [stirring_step(), liquid_step()]}
    events: list[dict] = []
    issues: list[dict] = []

    engine().normalize_workflow(
        workflow,
        reagent_slot_plan=[
            {
                "工作站": "Liquid_Handling_Station_1ml_V1",
                "原液编号": 1,
                "material_identity_id": "SELF_ASSERTED_WATER",
                "canonical_name": "Water canonical",
            }
        ],
        plan_steps=[
            {
                "plan_step": 7,
                "source_material_identity_ids": ["SELF_ASSERTED_WATER"],
            }
        ],
        normalization_events=events,
        normalization_issues=issues,
    )

    assert issues == [
        {
            "rule_id": "reagent_slot_identity/v1",
            "code": "reagent_slot_identity_unanchored",
            "json_pointer": "/reagent_slot_plan",
            "message": (
                "Slot material identities have no independent root in "
                "batch_plan, material_identity_registry, or material_ledger"
            ),
            "material_identity_ids": ["SELF_ASSERTED_WATER"],
        }
    ]
    assert (
        workflow["steps"][1]["parameters"]["加样方案"][0]["1号原液瓶"][
            "配料名称"
        ]
        == "legacy label"
    )
    assert workflow["steps"][0]["parameters"]["搅拌速度"] == 600
    assert workflow["steps"][0]["parameters"]["搅拌时间"] == 2
    assert any(
        event["rule_id"] == "contract_unit_scalar/v1" for event in events
    )
    assert not any(
        event["rule_id"] == "reagent_slot_identity/v1" for event in events
    )


def test_successful_normalization_is_idempotent_and_json_roundtrip_stable():
    workflow = {"steps": [stirring_step()]}
    normalizer = engine()
    first_events: list[dict] = []
    normalizer.normalize_workflow(workflow, normalization_events=first_events)
    once = json.loads(json.dumps(workflow, ensure_ascii=False))
    second_events: list[dict] = []

    normalizer.normalize_workflow(workflow, normalization_events=second_events)

    assert workflow == once
    assert second_events == []
