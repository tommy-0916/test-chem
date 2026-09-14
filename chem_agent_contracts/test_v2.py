from __future__ import annotations

import unittest

from chem_agent_contracts.adapters import (
    build_observation_event_v2,
    device_result_to_v2,
    research_state_to_v2,
)


def research_fixture():
    return research_state_to_v2(
        {
            "campaign_id": "CMP_V2",
            "current_stage": "制备并观察晶相",
            "macro_action": {
                "macro_action_id": "MA_001",
                "objective": "制备单一样品",
                "planned_operations": ["加液", "搅拌"],
                "expected_observation": "XRD",
                "completion_condition": "获得有效 XRD",
                "experiment_group": {
                    "group_id": "G_001",
                    "sample_id": "S_001",
                    "role": "experimental",
                },
            },
            "macro_plan": [
                {
                    "步骤序号": 1,
                    "macro_step_id": "MS_001",
                    "操作": "加液",
                    "试剂/对象": "水",
                    "参数": "加入 10 mL 水",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "按 10 mL 反应规模设置",
                    },
                    "material_inputs": [
                        {
                            "name": "水",
                            "state": "liquid",
                            "quantity": {"value": 10, "unit": "mL"},
                            "provenance": {
                                "kind": "agent_inferred",
                                "rationale": "按 10 mL 反应规模设置",
                            },
                        }
                    ],
                    "material_outputs": [
                        {
                            "name": "混合液",
                            "state": "solution",
                            "quantity": {"mode": "all_available"},
                        }
                    ],
                },
                {
                    "步骤序号": 2,
                    "macro_step_id": "MS_002",
                    "操作": "搅拌",
                    "试剂/对象": "混合液",
                    "参数": "500 rpm 搅拌 10 min",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "保证小体积混合均匀",
                    },
                },
            ],
            "current_evidence_bundle": {
                "bundle_id": "EV_CURRENT",
                "query": "test",
                "retrieval_status": "empty",
                "results": [],
            },
        }
    )


class V2ContractTest(unittest.TestCase):
    def test_research_contract_is_hashed_and_one_group(self):
        research = research_fixture()
        self.assertTrue(research.research_contract_hash.startswith("research_v2_"))
        self.assertEqual({step.sample_id for step in research.macro_steps}, {"S_001"})
        self.assertEqual(research.macro_steps[0].material_inputs[0].quantity.value, 10)

    def test_mapping_preserves_macro_ids_and_exact_binding(self):
        research = research_fixture()
        result = {
            "status": "success",
            "device_snapshot_id": "SNAP_1",
            "workflow_json": {
                "steps": [
                    {
                        "device_step_id": "DS_1",
                        "source_macro_step_id": "MS_001",
                        "station_code": "Liquid_Handling_Station_1ml_V2",
                        "station_version": "V2",
                        "platform_name": "移液平台1ml_V2",
                        "workstation": "移液平台1ml_V2",
                        "id": 36,
                        "operation": "加液_物料绑定",
                        "parameters": {"体积(mL)": 1.0},
                    }
                ]
            },
            "dispatch_payload": {"experiment_steps": {"steps": []}},
            "dispatch_validation": {"status": "passed", "errors": []},
        }
        package = device_result_to_v2(result, research)
        self.assertEqual(package.status, "ready_for_dispatch")
        step = package.workstation_mapping.device_steps[0]
        self.assertEqual(step.source_macro_step_id, "MS_001")
        self.assertEqual(step.station_version, "V2")
        self.assertEqual(step.station_id, 36)
        self.assertEqual(
            {item.macro_step_id for item in package.workstation_mapping.requirements},
            {"MS_001", "MS_002"},
        )

    def test_terminal_requires_exact_macro_step_id(self):
        research = research_fixture()
        exact = device_result_to_v2(
            {
                "status": "feasibility_error",
                "error_package": {
                    "blocking_constraints": ["MS_002 has no compatible workstation"]
                },
            },
            research,
        )
        self.assertEqual(exact.status, "terminal_unmappable")
        self.assertEqual(exact.terminal_macro_step_ids, ["MS_002"])

        ambiguous = device_result_to_v2(
            {"status": "feasibility_error", "error_package": {"blocking_constraints": ["gap"]}},
            research,
        )
        self.assertEqual(ambiguous.status, "human_review_required")

    def test_observation_has_planned_setpoint_actual_and_deviation(self):
        research = research_fixture()
        device = device_result_to_v2(
            {
                "status": "success",
                "workflow_json": {
                    "steps": [
                        {
                            "device_step_id": "DS_1",
                            "source_macro_step_id": "MS_001",
                            "station_code": "Liquid_Handling_Station_1ml_V2",
                            "platform_name": "移液平台1ml_V2",
                            "id": 36,
                            "operation": "加液_物料绑定",
                            "parameters": {"numeric_parameter_1": 9.8},
                        }
                    ]
                },
                "dispatch_validation": {"status": "passed", "errors": []},
            },
            research,
        )
        event = build_observation_event_v2(
            {
                "status": "completed",
                "device_parameter_trace": [
                    {
                        "device_step_id": "DS_1",
                        "name": "numeric_parameter_1",
                        "actual_value": 9.7,
                        "unit": "mL",
                    }
                ],
            },
            research,
            device,
        )
        trace = event.device_parameter_trace[0]
        self.assertEqual(trace.planned_value, 10)
        self.assertEqual(trace.device_setpoint, 9.8)
        self.assertEqual(trace.actual_value, 9.7)
        self.assertAlmostEqual(trace.deviation, -0.3)
        self.assertTrue(event.macro_parameter_summary)


if __name__ == "__main__":
    unittest.main()
