from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from chem_agent_contracts.adapters import (
    attach_device_v2_contract,
    research_state_to_v2,
)
from orchestrator.execution_adapters import MockExecutionAdapter
from orchestrator.runner import (
    STOP_DEVICE_ERROR,
    STOP_READY_FOR_DISPATCH,
    STOP_TERMINAL_UNMAPPABLE,
    CampaignConfig,
    CampaignRunner,
    feedback_route,
)
from orchestrator.test_runner import (
    V2_PACKAGE_CONTRACT,
    _valid_v2_feasibility_certificate,
)


def _certified_forward_only_package():
    package = {
        **V2_PACKAGE_CONTRACT,
        "status": "ready_for_dispatch",
        "feedback_route": "success",
        "workflow_json": {"steps": [{"device_step_id": "DS_1"}]},
        "feasibility_accepted": True,
    }
    package["feasibility_certificate"] = _valid_v2_feasibility_certificate(
        package
    )
    return package


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
    assert feedback_route(
        {"status": "ready_for_dispatch", "feedback_route": "success"}
    ) == "success"
    assert feedback_route({"status": "success"}) == "success"
    assert feedback_route(
        {"status": "success", "feedback_route": "none"}
    ) == "success"
    assert feedback_route(
        {"status": "terminal_unmappable", "feedback_route": "terminal"}
    ) == "terminal"


def test_success_envelopes_fail_closed_on_conflicting_terminal_fields():
    cases = (
        (
            {"status": "success", "feedback_route": "human"},
            "human",
        ),
        (
            {"status": "success", "feedback_type": "human_review_required"},
            "human",
        ),
        (
            {"status": "success", "failure_scope": "human_review_required"},
            "human",
        ),
        (
            {"status": "success", "feedback_route": "device"},
            "device",
        ),
        (
            {"status": "success", "failure_scope": "device_workflow"},
            "device",
        ),
        (
            {
                "status": "ready_for_dispatch",
                "feedback_route": "success",
                "feedback_type": "device_internal_error",
            },
            "device",
        ),
        (
            {"status": "success", "feedback_route": "terminal"},
            "terminal",
        ),
        (
            {
                "status": "ready_for_dispatch",
                "feedback_route": "success",
                "feedback_type": "terminal_unmappable",
            },
            "terminal",
        ),
        (
            {
                "status": "terminal_unmappable",
                "feedback_route": "terminal",
                "feedback_type": "human_review_required",
            },
            "human",
        ),
        ({"status": "ready_for_dispatch"}, "device"),
        ({"status": "success", "feedback_route": "success"}, "device"),
    )
    for package, expected in cases:
        assert feedback_route(package) == expected


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


def test_forward_only_stops_after_first_validated_device_workflow():
    calls = []

    def research_step(event_type, **kwargs):
        calls.append(event_type)
        result = {"status": "completed", "macro_plan": [{"步骤序号": 1}]}
        path = kwargs["iteration_dir"] / "research_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
        return result

    def device_step(state_path, iteration_dir, **kwargs):
        return _certified_forward_only_package()

    class _NoExecutionAdapter(MockExecutionAdapter):
        def execute(self, package, iteration_dir):
            raise AssertionError("forward-only mode must not execute the workflow")

    with tempfile.TemporaryDirectory() as temporary:
        runner = CampaignRunner(
            CampaignConfig(
                query="q",
                campaign_id="CMP_FORWARD_ONLY",
                campaigns_root=Path(temporary),
                forward_only=True,
            ),
            _NoExecutionAdapter(),
            research_step=research_step,
            device_step=device_step,
        )
        with patch(
            "orchestrator.runner.check_dispatch",
            return_value={
                "status": "passed",
                "dispatchable": True,
                "findings": [],
                "input_sha256": "forward-only-fixture",
            },
        ) as check, patch(
            "orchestrator.runner.write_check_report", return_value={}
        ):
            result = runner.run()

    assert result.stop_reason == STOP_READY_FOR_DISPATCH
    assert result.iterations_run == 1
    assert calls == ["bootstrap"]
    check.assert_called_once()
    assert check.call_args.kwargs["require_payload"] is True


def test_forward_only_rejected_workflow_is_not_ready_for_dispatch():
    calls = []

    def research_step(event_type, **kwargs):
        calls.append(event_type)
        result = {"status": "completed", "macro_plan": [{"步骤序号": 1}]}
        path = kwargs["iteration_dir"] / "research_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
        return result

    def device_step(state_path, iteration_dir, **kwargs):
        return _certified_forward_only_package()

    class _NoExecutionAdapter(MockExecutionAdapter):
        def execute(self, package, iteration_dir):
            raise AssertionError("a rejected workflow must not execute")

    rejected = {
        "status": "rejected",
        "dispatchable": False,
        "findings": [{"severity": "error", "code": "invalid_contract"}],
        "input_sha256": "rejected-forward-only-fixture",
    }
    with tempfile.TemporaryDirectory() as temporary:
        runner = CampaignRunner(
            CampaignConfig(
                query="q",
                campaign_id="CMP_FORWARD_ONLY_REJECTED",
                campaigns_root=Path(temporary),
                forward_only=True,
            ),
            _NoExecutionAdapter(),
            research_step=research_step,
            device_step=device_step,
        )
        with patch(
            "orchestrator.runner.check_dispatch", return_value=rejected
        ) as check, patch(
            "orchestrator.runner.write_check_report", return_value={}
        ):
            result = runner.run()

    assert result.stop_reason == STOP_DEVICE_ERROR
    assert result.iterations_run == 1
    assert calls == ["bootstrap"]
    check.assert_called_once()


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
