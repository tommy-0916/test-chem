"""Focused integration tests for Device terminal-package normalization."""

from __future__ import annotations

import copy
import hashlib
import json

from single_agent import SingleDeviceAgent, SingleDeviceAgentState
from utils.workstation_loader import WorkstationLoader


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def agent() -> SingleDeviceAgent:
    value = SingleDeviceAgent(
        model=object(),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )
    value._contract_gate_enabled = lambda: True
    return value


def liquid_step(*, bottle=1, name="legacy label", amount="0.5 mL") -> dict:
    return {
        "step_number": 2,
        "source_plan_step": 7,
        "source_macro_step": 1,
        "source_macro_steps": [1],
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
                        "原液用量": amount,
                    },
                }
            ],
        },
    }


def stirring_step() -> dict:
    return {
        "step_number": 1,
        "source_plan_step": 6,
        "source_macro_step": 1,
        "source_macro_steps": [1],
        "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
        "operation": "开始搅拌",
        "parameters": {
            "容器类型": "进样瓶",
            "容器数量": 1,
            "容器编号": [1],
            "搅拌速度": "600 rpm",
            "搅拌时间": "2 min",
        },
    }


def test_workflow_text_renderer_is_read_only_and_uses_declared_number():
    workflow = {
        "steps": [
            {
                "step_number": 9,
                "workstation": "Fixture",
                "operation": "Observe",
                "parameters": {"x": 1},
            }
        ]
    }
    original = copy.deepcopy(workflow)

    rendered = SingleDeviceAgent._workflow_txt_from_json(workflow)

    assert workflow == original
    assert rendered.startswith("第9步 Fixture：Observe")


def test_legacy_slot_plan_and_workflow_are_migrated_from_trusted_roots():
    slot_plan = [
        {
            "工作站": "Liquid_Handling_Station_1ml_V1",
            "原液编号": 1,
            "名称": "short alias",
            "来源": "validated root ROOT_WATER",
        }
    ]
    workflow = {"steps": [liquid_step()]}
    result = {
        "reagent_slot_plan": copy.deepcopy(slot_plan),
        "batch_plan": [
            {
                "is_root_batch": True,
                "material_identity_id": "ROOT_WATER",
                "material_id": "Water canonical",
            }
        ],
        "device_plan": [
            {
                "plan_step": 7,
                "workstation": "Liquid_Handling_Station_1ml_V1",
                "operation_intent": "加液_物料绑定",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": ["ROOT_WATER"],
            }
        ],
        "workflow_json": workflow,
        "workflow_txt": "stale",
    }
    source_workflow_hash = digest(workflow)
    source_plan_hash = digest(slot_plan)

    value = agent()
    value._run_full_checks(result)

    migrated_slot = result["reagent_slot_plan"][0]
    bottle = result["workflow_json"]["steps"][0]["parameters"]["加样方案"][0]["1号原液瓶"]
    assert migrated_slot["material_identity_id"] == "ROOT_WATER"
    assert migrated_slot["canonical_name"] == "Water canonical"
    assert migrated_slot["名称"] == "Water canonical"
    assert bottle == {"配料名称": "Water canonical", "原液用量": 0.5}
    assert {event["rule_id"] for event in result["normalization_events"]} == {
        "reagent_slot_identity/v1",
        "skill_station_id/v1",
        "contract_unit_scalar/v1",
    }
    assert all(
        "old" not in event and "new" not in event
        for event in result["normalization_events"]
    )
    audit = result["normalization_audit"]
    assert audit["source_workflow_sha256"] == source_workflow_hash
    assert audit["source_reagent_slot_plan_sha256"] == source_plan_hash
    assert audit["normalized_workflow_sha256"] == digest(result["workflow_json"])
    assert audit["normalized_reagent_slot_plan_sha256"] == digest(
        result["reagent_slot_plan"]
    )
    assert audit["event_count"] == len(result["normalization_events"])
    assert result["workflow_txt"] == SingleDeviceAgent._workflow_txt_from_json(
        result["workflow_json"]
    )
    evidence = copy.deepcopy(
        {
            key: result[key]
            for key in (
                "normalization_events",
                "normalization_audit",
                "normalization_notes",
            )
        }
    )
    value._run_full_checks(result)
    assert {key: result[key] for key in evidence} == evidence


def test_slot_error_does_not_rollback_independent_scalar_repairs():
    result = {
        "reagent_slot_plan": [
            {
                "工作站": "Liquid_Handling_Station_1ml_V1",
                "原液编号": 1,
                "material_identity_id": "MAT_WATER",
                "canonical_name": "Water canonical",
            }
        ],
        "batch_plan": [
            {
                "is_root_batch": True,
                "material_identity_id": "MAT_WATER",
                "material_id": "Water canonical",
            }
        ],
        "device_plan": [
            {
                "plan_step": 6,
                "workstation": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
                "operation_intent": "开始搅拌",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": [],
            },
            {
                "plan_step": 7,
                "workstation": "Liquid_Handling_Station_1ml_V1",
                "operation_intent": "加液_物料绑定",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": ["MAT_WATER"],
            },
        ],
        "workflow_json": {
            "steps": [stirring_step(), liquid_step(bottle=2)]
        },
        "workflow_txt": "stale",
    }

    report = agent()._run_full_checks(result)

    stir = result["workflow_json"]["steps"][0]["parameters"]
    source = result["workflow_json"]["steps"][1]["parameters"]["加样方案"][0]["2号原液瓶"]
    assert stir["搅拌速度"] == 600
    assert stir["搅拌时间"] == 2
    assert source["配料名称"] == "legacy label"
    assert any(
        issue["code"] == "missing_slot_binding"
        for issue in result["normalization_issues"]
    )
    assert any(
        "第 2 步" in error and "missing_slot_binding" in error
        for error in report["errors"]
    )
    assert any(
        event["rule_id"] == "contract_unit_scalar/v1"
        for event in result["normalization_events"]
    )
    assert result["normalization_audit"]["normalized_workflow_sha256"] == digest(
        result["workflow_json"]
    )


def test_nested_registry_trust_is_used_by_full_checks_and_audit_replay():
    result = {
        "reagent_slot_plan": [
            {
                "工作站": "Liquid_Handling_Station_1ml_V1",
                "原液编号": 1,
                "material_identity_id": "DERIVED_X",
                "canonical_name": "Derived X canonical",
            }
        ],
        "research_handoff": {
            "semantic_analysis": {
                "material_identity_registry": [
                    {
                        "identity_id": "DERIVED_X",
                        "canonical_name": "Derived X canonical",
                    }
                ]
            }
        },
        "device_plan": [
            {
                "plan_step": 7,
                "workstation": "Liquid_Handling_Station_1ml_V1",
                "operation_intent": "加液_物料绑定",
                "source_macro_step": 1,
                "source_macro_steps": [1],
                "source_material_identity_ids": ["DERIVED_X"],
            }
        ],
        "workflow_json": {"steps": [liquid_step()]},
        "workflow_txt": "stale",
    }
    value = agent()

    value._run_full_checks(result)

    assert not any(
        issue.get("code") in {
            "reagent_slot_identity_unanchored",
            "untrusted_reagent_slot_identity",
        }
        for issue in result.get("normalization_issues", [])
    )
    bottle = result["workflow_json"]["steps"][0]["parameters"]["加样方案"][0][
        "1号原液瓶"
    ]
    assert bottle["配料名称"] == "Derived X canonical"
    first_evidence = copy.deepcopy(
        {
            key: result[key]
            for key in (
                "normalization_events",
                "normalization_audit",
                "normalization_notes",
            )
        }
    )

    value._run_full_checks(result)

    assert {key: result[key] for key in first_evidence} == first_evidence


def test_repeated_full_checks_preserve_one_verified_audit_without_duplication():
    result = {
        "workflow_json": {"steps": [stirring_step()]},
        "workflow_txt": "stale",
    }
    value = agent()

    value._run_full_checks(result)
    first = {
        key: copy.deepcopy(result[key])
        for key in (
            "normalization_events",
            "normalization_audit",
            "normalization_notes",
        )
    }

    value._run_full_checks(result)

    assert {
        key: result[key]
        for key in (
            "normalization_events",
            "normalization_audit",
            "normalization_notes",
        )
    } == first
    assert result["normalization_audit"]["event_count"] == len(
        result["normalization_events"]
    )
    assert "normalization_issues" not in result


def test_new_full_check_replaces_forged_or_mixed_normalization_state():
    workflow = {"steps": [stirring_step()]}
    fake_event = {
        "rule_id": "fake_rule/v1",
        "json_pointer": "/steps/0/parameters/搅拌速度",
        "original_value": "fabricated",
        "new_value": "600 rpm",
    }
    result = {
        "workflow_json": workflow,
        "workflow_txt": "stale",
        "normalization_events": [fake_event],
        "normalization_audit": {
            "rule_ids": ["fake_rule/v1"],
            "source_workflow_sha256": "0" * 64,
            "normalized_workflow_sha256": digest(workflow),
            "source_reagent_slot_plan_sha256": "1" * 64,
            "normalized_reagent_slot_plan_sha256": digest(None),
            "event_count": 1,
        },
        "normalization_notes": ["fabricated note"],
        "normalization_issues": [
            {
                "code": "fabricated_issue",
                "json_pointer": "/steps/0",
                "message": "fabricated",
            }
        ],
    }

    agent()._run_full_checks(result)

    assert all(
        event["rule_id"] != "fake_rule/v1"
        for event in result["normalization_events"]
    )
    assert result["normalization_audit"]["event_count"] == len(
        result["normalization_events"]
    )
    assert result["normalization_audit"]["normalized_workflow_sha256"] == digest(
        result["workflow_json"]
    )
    assert "fabricated note" not in result.get("normalization_notes", [])
    assert "normalization_issues" not in result


def test_already_canonical_input_drops_unverifiable_prior_audit():
    result = {
        "workflow_json": {"steps": [stirring_step()]},
        "workflow_txt": "stale",
    }
    value = agent()
    value._run_full_checks(result)
    result["normalization_events"][0]["new_value"] = "not-current"
    result["normalization_notes"] = ["stale note"]
    result["normalization_issues"] = [
        {
            "code": "stale_issue",
            "json_pointer": "/steps/0",
            "message": "stale",
        }
    ]

    value._run_full_checks(result)

    assert "normalization_events" not in result
    assert "normalization_audit" not in result
    assert "normalization_notes" not in result
    assert "normalization_issues" not in result


def test_current_hash_disguise_cannot_preserve_unknown_rule_event():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Unknown_Test_Station",
                "operation": "noop",
                "parameters": {"x": 1},
            }
        ]
    }
    source = copy.deepcopy(workflow)
    source["steps"][0]["parameters"]["x"] = 0
    result = {
        "workflow_json": workflow,
        "workflow_txt": "canonical",
        "normalization_events": [
            {
                "rule_id": "forged/v9",
                "json_pointer": "/steps/0/parameters/x",
                "original_value": 0,
                "new_value": 1,
            }
        ],
        "normalization_audit": {
            "rule_ids": ["forged/v9"],
            "source_workflow_sha256": digest(source),
            "normalized_workflow_sha256": digest(workflow),
            "source_reagent_slot_plan_sha256": digest(None),
            "normalized_reagent_slot_plan_sha256": digest(None),
            "event_count": 1,
        },
        "normalization_notes": ["forged but hash-shaped"],
    }

    agent()._run_full_checks(result)

    assert "normalization_events" not in result
    assert "normalization_audit" not in result
    assert "normalization_notes" not in result


def test_known_rule_with_unreplayable_source_hash_is_not_preserved():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Unknown_Test_Station",
                "operation": "noop",
                "parameters": {"x": 1},
            }
        ]
    }
    result = {
        "workflow_json": workflow,
        "workflow_txt": "canonical",
        "normalization_events": [
            {
                "rule_id": "contract_unit_scalar/v1",
                "json_pointer": "/steps/0/parameters/x",
                "original_value": "1 rpm",
                "new_value": 1,
            }
        ],
        "normalization_audit": {
            "rule_ids": ["contract_unit_scalar/v1"],
            # Both normalized hashes are genuine, but the source workflow hash
            # is forged to equal the current state rather than the replayed
            # pre-normalization value.
            "source_workflow_sha256": digest(workflow),
            "normalized_workflow_sha256": digest(workflow),
            "source_reagent_slot_plan_sha256": digest(None),
            "normalized_reagent_slot_plan_sha256": digest(None),
            "event_count": 1,
        },
    }

    agent()._run_full_checks(result)

    assert "normalization_events" not in result
    assert "normalization_audit" not in result


def test_known_rule_with_self_consistent_hashes_must_replay_real_normalizer():
    workflow = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Unknown_Test_Station",
                "operation": "noop",
                "parameters": {"x": 1},
            }
        ]
    }
    source = copy.deepcopy(workflow)
    source["steps"][0]["parameters"]["x"] = "999 rpm"
    result = {
        "workflow_json": workflow,
        "workflow_txt": "canonical",
        "normalization_events": [
            {
                "rule_id": "contract_unit_scalar/v1",
                "json_pointer": "/steps/0/parameters/x",
                "original_value": "999 rpm",
                "new_value": 1,
            }
        ],
        "normalization_audit": {
            "rule_ids": ["contract_unit_scalar/v1"],
            "source_workflow_sha256": digest(source),
            "normalized_workflow_sha256": digest(workflow),
            "source_reagent_slot_plan_sha256": digest(None),
            "normalized_reagent_slot_plan_sha256": digest(None),
            "event_count": 1,
        },
    }

    agent()._run_full_checks(result)

    # Hashes and event chaining are internally consistent, but the real
    # normalizer has no contract for this unknown station and cannot produce
    # the claimed unit conversion.
    assert "normalization_events" not in result
    assert "normalization_audit" not in result


def test_terminal_success_and_failure_propagate_current_normalization_evidence():
    value = agent()
    result = {
        "status": "success",
        "workflow_json": {"steps": [stirring_step()]},
        "workflow_txt": "stale",
        "device_self_check": {},
    }
    state = SingleDeviceAgentState(research_handoff={}, exp_id="normalization")
    state.feasibility_accepted = True
    state.feasibility_certificate = {"accepted": True}
    # Match the normal pipeline ordering: trace/platform stamping precedes the
    # full deterministic gate, so the recorded normalized hash is terminal.
    value._stamp_device_steps_with_macro_action(result["workflow_json"], {})
    value._run_full_checks(result)
    expected = {
        key: copy.deepcopy(result[key])
        for key in (
            "normalization_events",
            "normalization_audit",
            "normalization_notes",
        )
    }

    successful = copy.deepcopy(result)
    successful["dispatch_validation"] = {
        "status": "passed",
        "errors": [],
        "warnings": [],
    }
    success_package = value._normalize_terminal_package(state, successful)
    assert success_package["status"] == "success"
    assert {
        key: success_package[key] for key in expected
    } == expected

    failed = copy.deepcopy(result)
    failed_issue = {
        "code": "current_issue",
        "json_pointer": "/steps/0",
        "message": "current",
    }
    failed["normalization_issues"] = [failed_issue]
    failed["dispatch_validation"] = {
        "status": "failed",
        "errors": ["current failure"],
        "warnings": [],
        "assessment_source": "deterministic_workstation_validator",
    }
    failure_package = value._normalize_terminal_package(state, failed)
    assert failure_package["status"] == "failed"
    assert {
        key: failure_package[key] for key in expected
    } == expected
    assert failure_package["normalization_issues"] == [failed_issue]
