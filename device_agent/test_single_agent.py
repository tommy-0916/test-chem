"""Tests for the simplified single device agent."""

from __future__ import annotations

import json
import types

from single_agent import SingleDeviceAgent


class FakeModel:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return types.SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


class FakeWorkstationLoader:
    def format_for_prompt(self):
        return """
## material-workstation
### USAGE
支持进样瓶。
### AUDIT-RULES
容器类型必须来自支持列表。

## liquid-dispensing-workstation
### USAGE
支持开盖、加液、关盖。

## dryer-workstation
### USAGE
支持静置烘干，温度 26-210 C。
""".strip()


RESEARCH_HANDOFF = {
    "task": {"query": "测试 query", "current_stage": "合成并 XRD"},
    "macro_action_steps": [
        {
            "步骤序号": 1,
            "操作": "配制 A 液",
            "试剂/对象": "盐、水",
            "参数": "10 mL water",
        }
    ],
}


def build_agent(payload):
    return SingleDeviceAgent(
        model=FakeModel(payload),
        workstation_loader=FakeWorkstationLoader(),
    )


def test_single_device_agent_success_package():
    agent = build_agent(
        {
            "status": "success",
            "feasibility": {
                "is_feasible": True,
                "blocking_constraints": [],
                "device_layer_adaptations": ["补全容器编号"],
            },
            "macro_plan_summary": "映射成功",
            "reagent_slot_plan": [{"原液编号": 1, "名称": "水"}],
            "container_plan": [{"容器编号": 1, "容器类型": "进样瓶", "用途": "反应"}],
            "workflow_txt": "1. 第1步 物料站：\n   - 操作：物料拿取",
            "workflow_json": {
                "steps": [
                    {
                        "step_number": 1,
                        "workstation": "物料站",
                        "operation": "物料拿取",
                        "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
                    }
                ],
                "offline_handoffs": [],
            },
        }
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="test_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    assert state.terminal_package["agent_mode"] == "single_device_agent"
    assert state.workflow_json["steps"]
    assert len(agent._model.calls) == 1


def test_single_device_agent_feasibility_error_package():
    agent = build_agent(
        {
            "feedback_type": "device_feasibility_error",
            "status": "feasibility_error",
            "feasibility": {
                "is_feasible": False,
                "blocking_constraints": ["缺少反应釜"],
                "unsupported_items": [
                    {
                        "macro_step": "溶剂热",
                        "requirement": "反应釜",
                        "reason": "真源未支持",
                        "missing_device_capability": "高压釜",
                        "suggested_research_revision": "改为常压路线",
                    }
                ],
            },
            "device_capability_summary": {
                "supported_containers": ["进样瓶"],
                "supported_workstations": ["material-workstation"],
                "not_supported": ["反应釜"],
            },
            "recommendation_to_research_agent": "改为常压路线",
        }
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="test_exp")

    assert state.status == "feasibility_error"
    package = state.terminal_package
    assert package["feedback_type"] == "device_feasibility_error"
    assert package["error_package"]["blocking_constraints"] == ["缺少反应釜"]
    assert package["error_package"]["assessment_source"] == "single_device_agent_llm"


if __name__ == "__main__":
    test_single_device_agent_success_package()
    test_single_device_agent_feasibility_error_package()
    print("single device agent tests passed")
