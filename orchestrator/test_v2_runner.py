from __future__ import annotations

import json
import tempfile
from pathlib import Path

from chem_agent_contracts.adapters import (
    attach_device_v2_contract,
    research_state_to_v2,
)
from orchestrator.execution_adapters import MockExecutionAdapter
from orchestrator.runner import (
    STOP_TERMINAL_UNMAPPABLE,
    CampaignConfig,
    CampaignRunner,
    feedback_route,
)


def _research_contract():
    return research_state_to_v2(
        {
            "campaign_id": "CMP_V2_RUNNER",
            "current_stage": "反应",
            "macro_action": {
                "macro_action_id": "MA_V2",
                "objective": "完成反应",
                "planned_operations": ["反应"],
                "expected_observation": "产物",
                "completion_condition": "产物可测",
                "experiment_group": {
                    "group_id": "G_V2",
                    "sample_id": "S_V2",
                    "role": "experimental",
                },
            },
            "macro_plan": [
                {
                    "macro_step_id": "MS_V2",
                    "步骤序号": 1,
                    "操作": "反应",
                    "试剂/对象": "样品",
                    "参数": "500 rpm 反应 10 min",
                    "provenance": {
                        "kind": "agent_inferred",
                        "rationale": "小规模筛选起点",
                    },
                }
            ],
        }
    )


def test_v2_routes_ready_and_terminal_explicitly():
    assert feedback_route({"status": "ready_for_dispatch"}) == "success"
    assert feedback_route(
        {"status": "terminal_unmappable", "feedback_route": "terminal"}
    ) == "terminal"


def test_terminal_unmappable_stops_campaign_without_research_retry():
    calls = []

    def research_step(event_type, **kwargs):
        calls.append(event_type)
        result = {"status": "completed", "macro_plan": [{"步骤序号": 1}]}
        path = kwargs["iteration_dir"] / "research_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
        return result

    def device_step(state_path, iteration_dir, **kwargs):
        return {
            "status": "terminal_unmappable",
            "feedback_route": "terminal",
            "device_workflow_package_v2": {
                "terminal_macro_step_ids": ["MS_V2"]
            },
        }

    with tempfile.TemporaryDirectory() as temporary:
        runner = CampaignRunner(
            CampaignConfig(
                query="q",
                campaign_id="CMP_STOP",
                campaigns_root=Path(temporary),
            ),
            MockExecutionAdapter(),
            research_step=research_step,
            device_step=device_step,
        )
        result = runner.run()
    assert result.stop_reason == STOP_TERMINAL_UNMAPPABLE
    assert calls == ["bootstrap"]


def test_v2_observation_trace_is_attached_for_research():
    research = _research_contract()
    legacy = {
        "status": "success",
        "workflow_json": {
            "steps": [
                {
                    "device_step_id": "DS_V2",
                    "source_macro_step_id": "MS_V2",
                    "station_code": "Room_Temperture_Magnetic_Stirrer_Workstation_V1",
                    "platform_name": "十通道磁力搅拌_V1",
                    "workstation": "十通道磁力搅拌_V1",
                    "id": 31,
                    "operation": "开始搅拌",
                    "parameters": {"numeric_parameter_1": 500},
                }
            ]
        },
        "dispatch_validation": {"status": "passed", "errors": []},
    }
    package = attach_device_v2_contract(legacy, research)
    package["macro_plan"] = {
        "research_action_package_v2": research.model_dump(
            mode="json", exclude_none=True
        )
    }
    observation = CampaignRunner._attach_actual_execution_parameters(
        {
            "status": "completed",
            "device_parameter_trace": [
                {
                    "device_step_id": "DS_V2",
                    "name": "numeric_parameter_1",
                    "actual_value": 495,
                    "unit": "rpm",
                }
            ],
        },
        package,
    )
    assert observation["observation_event_v2"]["macro_action_id"] == "MA_V2"
    trace = observation["device_parameter_trace"][0]
    assert trace["planned_value"] == 500
    assert trace["device_setpoint"] == 500
    assert trace["actual_value"] == 495
    assert trace["deviation"] == -5
