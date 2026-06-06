"""Pre-feasibility gate tests for macro plan/device compatibility."""

import sys
import types

dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv_stub)

from workflow import MainWorkflow
from state import WorkflowState


class DummyModel:
    def invoke(self, messages):
        raise AssertionError("Pre-feasibility gate tests should not call the LLM")


class FakeFeasibilityModel:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return types.SimpleNamespace(content=self.content)


WORKSTATIONS = [
    {
        "station_name": "material-workstation",
        "usage_content": "容器类型：进样瓶、西林瓶、50ml耐热瓶",
        "audit_rules_content": "",
        "skill_content": "",
    },
    {
        "station_name": "liquid-dispensing-workstation",
        "usage_content": "操作：开盖、加液、关盖；容器类型：进样瓶、西林瓶、50ml耐热瓶",
        "audit_rules_content": "",
        "skill_content": "",
    },
    {
        "station_name": "magnetic-stirring-workstation",
        "usage_content": "操作：磁力搅拌；容器类型：进样瓶",
        "audit_rules_content": "",
        "skill_content": "",
    },
    {
        "station_name": "pure-workstation",
        "usage_content": "操作：离心；容器类型：进样瓶、留样瓶",
        "audit_rules_content": "",
        "skill_content": "",
    },
    {
        "station_name": "dryer-workstation",
        "usage_content": "操作：静置烘干；容器类型：进样瓶、50ml耐热瓶",
        "audit_rules_content": "",
        "skill_content": "",
    },
]


def build_workflow() -> MainWorkflow:
    workflow = object.__new__(MainWorkflow)
    workflow._workstation_loader = None
    workflow._model = DummyModel()
    return workflow


def build_state(macro_plan: str) -> WorkflowState:
    return WorkflowState(
        final_goal=macro_plan,
        macro_plan=macro_plan,
        exp_id="test",
        exp_log_path="/tmp/test/exp_log.json",
        workstation_descriptions=WORKSTATIONS,
    )


def test_pre_feasibility_gate_rejects_autoclave_macro_plan():
    workflow = build_workflow()
    fake_model = FakeFeasibilityModel(
        """
        {
          "is_feasible": false,
          "status": "unsupported",
          "blocking_constraints": [
            "当前设备描述中没有聚四氟乙烯内衬高压反应釜或溶剂热反应能力。",
            "当前设备描述中没有 XRD/PXRD 在线表征工作站。"
          ],
          "unsupported_items": [
            {
              "macro_step": "溶剂热反应",
              "requirement": "聚四氟乙烯内衬高压反应釜",
              "reason": "设备描述中没有该容器或高压密封加热能力",
              "missing_device_capability": "反应釜/高压釜/溶剂热",
              "suggested_research_revision": "改写为当前容器和温控能力支持的瓶内反应。"
            }
          ],
          "supported_parts": ["离心洗涤", "烘干"],
          "device_capability_summary": {
            "supported_containers": ["进样瓶", "西林瓶", "50ml耐热瓶", "留样瓶"],
            "supported_workstations": ["material-workstation", "pure-workstation", "dryer-workstation"],
            "not_supported": ["反应釜", "XRD/PXRD"]
          },
          "recommendation_to_research_agent": "请重新规划为当前设备支持的容器和离线表征路线。"
        }
        """
    )
    workflow._model = fake_model
    state = build_state(
        "将前驱体溶液转移至聚四氟乙烯内衬高压反应釜中，80 C 溶剂热反应 24 h，随后进行 XRD 表征。"
    )

    result = workflow._step_pre_feasibility_gate(state)

    assert len(fake_model.calls) == 1
    assert result.status == "feasibility_error"
    assert result.verification_category == "physical_infeasible"
    assert result.terminal_package["status"] == "feasibility_error"
    assert result.terminal_package["error_package"]["assessment_source"] == "llm"
    assert result.pre_feasibility_report["source"] == "llm"
    blocking_text = " ".join(result.blocking_constraints)
    assert "反应釜" in blocking_text
    assert "XRD" in blocking_text


def test_pre_feasibility_gate_allows_supported_vial_route():
    workflow = build_workflow()
    fake_model = FakeFeasibilityModel(
        """
        {
          "is_feasible": true,
          "status": "supported",
          "blocking_constraints": [],
          "unsupported_items": [],
          "supported_parts": ["进样瓶加液", "磁力搅拌", "留固离心洗涤", "静置烘干"],
          "device_capability_summary": {
            "supported_containers": ["进样瓶", "留样瓶"],
            "supported_workstations": ["liquid-dispensing-workstation", "magnetic-stirring-workstation", "pure-workstation", "dryer-workstation"],
            "not_supported": []
          },
          "recommendation_to_research_agent": "当前 macro_plan 可进入设备动作生成。"
        }
        """
    )
    workflow._model = fake_model
    state = build_state(
        "使用 2 个进样瓶完成瓶内水相共沉淀；通过液体进样站加液，磁力搅拌 120 min，"
        "随后用纯化工作站留固离心洗涤，并在烘干机 60 C 干燥 12 h。"
    )

    result = workflow._step_pre_feasibility_gate(state)

    assert len(fake_model.calls) == 1
    assert result.status == "running"
    assert result.verification_category == ""
    assert result.terminal_package is None
    assert result.pre_feasibility_report["source"] == "llm"


def test_pre_feasibility_gate_fails_when_llm_fails():
    workflow = build_workflow()
    workflow._model = DummyModel()
    state = build_state(
        "将前驱体溶液转移至聚四氟乙烯内衬高压反应釜中，80 C 溶剂热反应 24 h，随后进行 XRD 表征。"
    )

    try:
        workflow._step_pre_feasibility_gate(state)
    except RuntimeError as exc:
        assert "without fallback" in str(exc)
    else:
        raise AssertionError("LLM failure should stop the pre-feasibility gate")


if __name__ == "__main__":
    test_pre_feasibility_gate_rejects_autoclave_macro_plan()
    test_pre_feasibility_gate_allows_supported_vial_route()
    test_pre_feasibility_gate_fails_when_llm_fails()
    print("pre_feasibility_gate tests passed")
