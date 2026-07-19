"""Tests for the simplified single device agent."""

from __future__ import annotations

import json
import types

from feasibility_rules import classify_constraint_text
from single_agent import (
    SingleDeviceAgent,
    _soft_temporal_mapping_error,
)
from utils.workstation_loader import WorkstationLoader
from workflow_validator import WorkflowValidator


class FakeModel:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return types.SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


BATCHED_SUCCESS_PAYLOAD = {
    "status": "success",
    "feasibility": {
        "is_feasible": True,
        "blocking_constraints": [],
        "device_layer_adaptations": ["同一进样瓶上采用交替分批节拍"],
    },
    "device_self_check": {
        "container_continuity": "pass",
        "volume_and_capacity": "pass",
        "centrifuge_balancing": "not_applicable",
        "addition_mode_preserved": "pass",
        "workstation_constraints": "pass",
    },
    "workflow_txt": "分批加液与批次间搅拌",
    "workflow_json": {
        "steps": [
            {
                "step_number": 1,
                "workstation": "液体进样站",
                "operation": "加液",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "原液编号": [1],
                    "原液量": [[1.0]],
                },
            },
            {
                "step_number": 2,
                "workstation": "磁力搅拌工作站",
                "operation": "磁力搅拌",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "搅拌速度": 700,
                    "搅拌时间（分钟）": 10,
                },
            },
            {
                "step_number": 3,
                "workstation": "液体进样站",
                "operation": "加液",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "原液编号": [1],
                    "原液量": [[1.0]],
                },
            },
        ],
        "temporal_adaptations": [
            {
                "original_requirement": "边滴入边搅拌",
                "execution_fidelity": "approximated",
                "adaptation_schedule": "加液 -> 搅拌 -> 加液 -> 搅拌",
                "requires_scientific_review": True,
            }
        ],
    },
}


class TemporalRetryModel:
    """First reports the historical false negative, then accepts batching."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        prompt = "\n\n".join(getattr(message, "content", "") for message in messages)
        self.calls.append(prompt)
        if len(self.calls) == 1:
            payload = {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "feasibility": {
                    "is_feasible": False,
                    "blocking_constraints": ["设备无法在同一工作站同时边滴入边搅拌"],
                },
            }
        else:
            payload = BATCHED_SUCCESS_PAYLOAD
        return types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class GenericComplaintRetryModel:
    """A NON-temporal wording of a soft complaint, then success on retry.

    Regression for the review finding: any phrasing without hard capability
    evidence must trigger the adaptation retry, not just the temporal
    keyword special case.
    """

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        prompt = "\n\n".join(getattr(message, "content", "") for message in messages)
        self.calls.append(prompt)
        if len(self.calls) == 1:
            payload = {
                "feedback_type": "device_feasibility_error",
                "status": "feasibility_error",
                "feasibility": {
                    "is_feasible": False,
                    "blocking_constraints": [
                        "宏观计划要求的加料节奏在真源操作列表中没有单一对应操作"
                    ],
                },
            }
        else:
            payload = BATCHED_SUCCESS_PAYLOAD
        return types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


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


def test_temporal_addition_stirring_soft_error_retries_as_batches():
    model = TemporalRetryModel()
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())
    handoff = {
        "task": {"query": "共沉淀", "current_stage": "合成"},
        "macro_action_steps": [
            {
                "步骤序号": 1,
                "操作": "边滴入边搅拌加入 B 液",
                "试剂/对象": "B 液",
                "参数": "10 mL，30 min",
            }
        ],
    }

    state = agent.run_state(handoff, exp_id="temporal_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    assert state.terminal_package["temporal_adaptations"][0]["execution_fidelity"] == "approximated"
    assert state.terminal_package["requires_scientific_review"] is True
    assert len(model.calls) == 2
    assert "强制重试要求" in model.calls[1]


def test_generic_soft_complaint_also_retries():
    """Review finding 1: a NON-keyword complaint without hard capability
    evidence must trigger the adaptation retry, not physical_infeasible."""
    model = GenericComplaintRetryModel()
    agent = SingleDeviceAgent(model=model, workstation_loader=FakeWorkstationLoader())

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="generic_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    assert len(model.calls) == 2
    assert "强制重试要求" in model.calls[1]


def test_unverifiable_condition_becomes_human_review_not_infeasible():
    """Review finding 1: 'no identically named dispatch field' complaints go
    to human review instead of being reported as physically infeasible."""
    payload = {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "feasibility": {
            "is_feasible": False,
            "blocking_constraints": [
                "XRD_V1 的可下发参数中没有辐射源字段，无法保证 Cu Kα 与 2θ=5-80° 扫描范围"
            ],
        },
    }
    agent = build_agent(payload)

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="unverifiable_exp")

    assert state.status == "feasibility_error"
    package = state.terminal_package
    assert package["error_package"]["type"] == "needs_human_review"
    assert package["error_package"]["type"] != "physical_infeasible"
    assert package["requires_scientific_review"] is True
    assert package["error_package"]["constraint_classification"]["unverifiable"]
    # the non-hard complaint got one adaptation retry before the verdict
    assert len(agent._model.calls) == 2


def test_hard_capability_gap_stays_physical_infeasible():
    result = {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "feasibility": {
            "blocking_constraints": ["必须以恒定流速连续流滴加，不能分批"],
        },
    }
    handoff = {"macro_action_steps": [{"参数": "边滴入边搅拌"}]}
    assert not _soft_temporal_mapping_error(result, handoff)
    assert classify_constraint_text("必须以恒定流速连续流滴加，不能分批") == "hard"


def test_missing_required_station_is_not_softened():
    result = {
        "feedback_type": "device_feasibility_error",
        "status": "feasibility_error",
        "feasibility": {
            "blocking_constraints": ["磁力搅拌工作站缺失，无法执行边滴入边搅拌"],
        },
    }
    handoff = {"macro_action_steps": [{"参数": "边滴入边搅拌"}]}
    assert not _soft_temporal_mapping_error(result, handoff)


def test_constraint_text_classification_buckets():
    assert classify_constraint_text("缺少反应釜") == "hard"
    assert classify_constraint_text("容器不兼容：进样瓶无法进入马弗炉") == "hard"
    assert classify_constraint_text("单容器体积超过离心上限") == "hard"
    assert classify_constraint_text("工作站离线维修") == "hard"
    assert classify_constraint_text("设备无法在同一工作站同时边滴入边搅拌") == "adaptable"
    assert classify_constraint_text("缓慢滴加无法通过现有加液操作实现") == "adaptable"
    assert (
        classify_constraint_text("工作站参数 schema 只包含步长和扫描速度，无法设置扫描范围")
        == "unverifiable"
    )
    assert (
        classify_constraint_text("macro plan 中的参数在设备可下发参数中没有同名字段")
        == "unverifiable"
    )
    # Regression: `设备…参数中不存在字段` must not read as "设备不存在" (the
    # equipment-gap suffix rule once misfired hard on this A02 phrasing).
    assert (
        classify_constraint_text("论文要求的扫描起止角在设备可下发参数中不存在同名字段")
        == "unverifiable"
    )
    assert classify_constraint_text("真源中不存在球磨工作站") == "hard"


def test_unconfirmable_precondition_station_is_human_review_not_hard():
    """Issue-philosophy regression: 'cannot confirm an existing station is
    equivalent to the required precondition' is unprovable → human review,
    even when phrased as 没有提供X工作站; but a genuinely missing station or a
    no-transfer-path claim stays hard."""
    hedged = (
        "真源提供了谱学磁力搅拌工作站，但没有提供谱学置物工作站。"
        "Container_storaging_Station_V1 仅声明一般容器静置和物料放置，"
        "没有规则证明其等价于谱学置物工作站。"
    )
    assert classify_constraint_text(hedged) == "unverifiable"
    assert (
        classify_constraint_text("未提供可被确认等价于谱学置物工作站的工作站、操作或审核规则")
        == "unverifiable"
    )
    # genuine hard blockers must NOT be softened by the hedge rule
    assert (
        classify_constraint_text(
            "设备真源没有聚四氟乙烯内衬高压反应釜，也没有从进样瓶向10ml耐压反应管"
            "转移混合样品的受支持操作，因此不存在连续合法的容器路径"
        )
        == "hard"
    )
    assert classify_constraint_text("设备清单中没有球磨工作站、球磨操作、球磨罐") == "hard"


def test_liquid_query_keeps_all_ranges_and_stirrer_visible():
    loader = WorkstationLoader(use_new_format=True)
    selected = set(loader._select_relevant_station_codes("边滴入边搅拌 10 mL"))
    assert "Liquid_Handling_Station_5ml_V1" in selected
    assert "Liquid_Handling_Station_5ml_V2" in selected
    assert "Room_Temperture_Magnetic_Stirrer_Workstation_V1" in selected


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
    # issue 7: the package carries a platform-form dispatch payload alongside
    # the semantic workflow_json (planning layer stays untouched).
    dispatch = state.terminal_package.get("dispatch_payload") or {}
    dispatch_steps = dispatch.get("experiment_steps", {}).get("steps", [])
    assert dispatch_steps and dispatch_steps[0]["workstation"] == "303物料站"
    formatting = state.terminal_package.get("dispatch_formatting") or {}
    assert formatting.get("mapped_steps") == 1
    assert state.workflow_json["steps"][0]["workstation"] == "物料站"


def test_deterministic_completion_avoids_llm_repair_round():
    """Eval fix: a SKILL-form workflow missing only mechanically-derivable
    required fields (容器数量, 开盖编号, 保留瓶盖) must be completed deterministically
    and pass validation WITHOUT burning an LLM self-repair round (model called once)."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)
    payload = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "macro_plan_summary": "映射成功",
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取\n第2步 Liquid_Handling_Station_1ml_V2：开盖",
        "workflow_json": {
            "steps": [
                {"step_number": 1, "workstation": "General_Material_Station_V1",
                 "operation": "物料拿取",
                 "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2, 3]}},
                {"step_number": 2, "workstation": "Liquid_Handling_Station_1ml_V2",
                 "operation": "开盖",
                 "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2, 3]}},
            ],
            "offline_handoffs": [],
        },
    }
    model = FakeModel(payload)
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="complete_exp")

    assert state.status == "completed"
    assert state.terminal_package["status"] == "success"
    # deterministic completion filled the mechanical fields, so NO self-repair
    # LLM round was needed — the model was invoked exactly once.
    assert len(model.calls) == 1
    completion = state.terminal_package.get("dispatch_completion") or {}
    filled_params = {e["param"] for e in completion.get("filled", [])}
    assert {"容器数量", "开盖编号", "保留瓶盖"} <= filled_params
    # the completed workflow_json carries the filled values
    p1 = state.workflow_json["steps"][0]["parameters"]
    assert p1["容器数量"] == 3


def test_completion_applies_to_self_repair_output():
    """Regression: deterministic completion must run on the self-repair
    (attempt-2) output too. A bug once left attempt-2 uncompleted, so a fresh
    LLM output could reintroduce mechanical omissions (缺 保留瓶盖) that the
    harness can fill. Here attempt-1 has a structural error forcing a repair;
    the repaired output omits 保留瓶盖 (mechanical) and must be completed."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    # attempt-1: 加样方案 as string → type_mismatch (structural, forces repair)
    attempt1 = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "x",
        "workflow_json": {"steps": [
            {"step_number": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
             "operation": "加液_物料绑定",
             "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                            "加样方案": "一段自然语言"}}],
            "offline_handoffs": []},
    }
    # attempt-2 (repair): fixes 加样方案 structure but OMITS 保留瓶盖 on an 开盖 step
    attempt2 = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "x",
        "workflow_json": {"steps": [
            {"step_number": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
             "operation": "开盖",
             "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                            "开盖编号": [1]}}],  # 保留瓶盖 missing → completion fills =1
            "offline_handoffs": []},
    }

    class SequencedModel:
        def __init__(self, payloads):
            self.payloads = payloads
            self.calls = []

        def invoke(self, messages):
            idx = min(len(self.calls), len(self.payloads) - 1)
            self.calls.append(messages)
            return types.SimpleNamespace(
                content=json.dumps(self.payloads[idx], ensure_ascii=False))

    model = SequencedModel([attempt1, attempt2])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="repair_complete_exp")

    # self-repair ran (2 mapping calls), and completion filled 保留瓶盖 on attempt-2
    assert len(model.calls) == 2
    filled = {e["param"] for e in
              (state.terminal_package.get("dispatch_completion") or {}).get("filled", [])}
    assert "保留瓶盖" in filled
    # attempt-2 completed → passes validation → success
    assert state.terminal_package["status"] == "success"


class _SequencedModel:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []
        self.prompts = []

    def invoke(self, messages):
        idx = min(len(self.calls), len(self.payloads) - 1)
        self.calls.append(messages)
        self.prompts.append(
            "\n".join(
                getattr(m, "content", None) or m.get("content", "")
                for m in messages
            )
        )
        return types.SimpleNamespace(
            content=json.dumps(self.payloads[idx], ensure_ascii=False))


def _good_material_step():
    return {
        "step_number": 1,
        "workstation": "General_Material_Station_V1",
        "operation": "物料拿取",
        "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]},
    }


def test_weighing_handoff_triggers_audit_repair_and_route_note():
    """Issue #9: a manual-weighing offline_handoff must fail the deterministic
    checks, the repair prompt must carry unnecessary_offline_handoff + the
    canonical solid-weighing route, and a compliant repair round succeeds with
    capability_audit=clean in the terminal package."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    attempt1 = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {
            "steps": [_good_material_step()],
            "offline_handoffs": [{
                "name": "XRD定量称样与换瓶",
                "instructions": ["分别称取5.0 mg粉末并对应装入无盖进样瓶21-30"],
            }],
        },
    }
    attempt2 = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [_good_material_step()], "offline_handoffs": []},
    }
    model = _SequencedModel([attempt1, attempt2])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="audit_repair_exp")

    assert len(model.calls) == 2
    repair_prompt = model.prompts[1]
    assert "unnecessary_offline_handoff" in repair_prompt or "mg 级固体定量称量" in repair_prompt
    assert "固体样品转移" in repair_prompt  # canonical route note attached
    package = state.terminal_package
    assert package["status"] == "success"
    assert package["capability_audit"]["status"] == "clean"


def test_two_bounded_repair_rounds_then_success():
    """Issue #12: repair → re-validate → repair → re-validate. bad, bad, good
    = 3 mapping calls and a success package."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    bad = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [{
            "step_number": 1,
            "workstation": "General_Material_Station_V1",
            "operation": "物料拿取",
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                           "不存在的危险参数": 999},
        }], "offline_handoffs": []},
    }
    good = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 General_Material_Station_V1：物料拿取",
        "workflow_json": {"steps": [_good_material_step()], "offline_handoffs": []},
    }
    model = _SequencedModel([bad, bad, good])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="two_round_exp")
    assert len(model.calls) == 3
    assert state.terminal_package["status"] == "success"


def test_lid_conflict_and_txt_mismatch_are_deterministic_errors():
    """Issue #12: lid-state continuity and txt↔json agreement are now
    deterministic checks, not LLM self-check prose."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    # 关盖 then an operation whose SKILL input demands 无盖 → proven conflict;
    # workflow_txt also has fewer 第N步 blocks than steps → txt_json_mismatch.
    bad = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "workflow_txt": "第1步 移液平台1ml_V2：关盖",
        "workflow_json": {"steps": [
            {"step_number": 1, "workstation": "Liquid_Handling_Station_1ml_V2",
             "operation": "关盖",
             "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                            "关盖编号": [1]}},
            {"step_number": 2,
             "workstation": "Multi_Channel_Solid_Weighing_Workstation_V1",
             "operation": "固体进样-文件传参-机器人",
             "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                            "上传文件": "recipe.csv"}},
        ], "offline_handoffs": []},
    }
    model = _SequencedModel([bad, bad, bad])
    agent = SingleDeviceAgent(
        model=model, workstation_loader=loader, workflow_validator=validator,
    )
    state = agent.run_state(RESEARCH_HANDOFF, exp_id="continuity_exp")
    package = state.terminal_package
    assert package["status"] == "failed"
    errors = " ".join(package["dispatch_validation"]["errors"])
    assert "lid_state_conflict" in errors
    assert "txt_json_mismatch" in errors


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
    assert package["error_package"]["type"] == "physical_infeasible"
    assert package["error_package"]["assessment_source"] == "single_device_agent_llm"
    # hard capability gap: no adaptation retry wasted
    assert len(agent._model.calls) == 1


def test_fabricated_dispatch_parameter_is_rejected():
    """Review finding 2: 不存在的危险参数=999 must not pass as success."""
    payload = {
        "status": "success",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "device_self_check": {"workstation_constraints": "pass"},
        "workflow_txt": "1. 第1步 物料站：\n   - 操作：物料拿取",
        "workflow_json": {
            "steps": [
                {
                    "step_number": 1,
                    "workstation": "物料站",
                    "operation": "物料拿取",
                    "parameters": {
                        "容器类型": "进样瓶",
                        "容器编号": [1],
                        "不存在的危险参数": 999,
                    },
                }
            ],
            "offline_handoffs": [],
        },
    }
    agent = SingleDeviceAgent(
        model=FakeModel(payload),
        workstation_loader=WorkstationLoader(use_new_format=True),
    )

    state = agent.run_state(RESEARCH_HANDOFF, exp_id="fabricated_exp")

    assert state.status == "failed"
    package = state.terminal_package
    assert package["status"] == "failed"
    assert package["failure_stage"] == "dispatch_validation"
    assert any(
        "不存在的危险参数" in error
        for error in package["dispatch_validation"]["errors"]
    )
    # two bounded self-repair rounds were attempted before giving up
    assert len(agent._model.calls) == 3


def test_workflow_validator_accepts_reference_and_rejects_bad_values():
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    from utils.paths import format_reference_path

    reference = json.loads(
        open(format_reference_path("json"), encoding="utf-8").read()
    )
    assert validator.validate(reference)["status"] == "passed"

    unknown_station = {
        "steps": [
            {"step_number": 1, "workstation": "虚构工作站X", "operation": "操作", "parameters": {}}
        ]
    }
    assert validator.validate(unknown_station)["status"] == "failed"

    out_of_range = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "烘干机",
                "operation": "静置烘干",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "恒温温度（℃）": 999,
                    "烘干时间（分钟）": 60,
                },
            }
        ]
    }
    report = validator.validate(out_of_range)
    assert report["status"] == "failed"
    assert any("value_out_of_range" in error for error in report["errors"])

    missing_required = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "磁力搅拌工作站",
                "operation": "磁力搅拌",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
            }
        ]
    }
    report = validator.validate(missing_required)
    assert report["status"] == "failed"
    assert any("missing_required_parameter" in error for error in report["errors"])

    invalid_enum = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "纯化工作站",
                "operation": "标准离心",
                "parameters": {"进样瓶编号": [1, 2], "纯化方法": "不存在的方法"},
            }
        ]
    }
    report = validator.validate(invalid_enum)
    assert report["status"] == "failed"
    assert any("invalid_enum_value" in error for error in report["errors"])


def test_skill_required_params_enforced_for_skill_form_stations():
    """Issue 7: params the SKILL table marks 是否必填=是 must be present when the
    step names the station in SKILL form (code/display); legacy reference-form
    payloads keep the reference.json contract (previous test). 容器数量 must
    match len(容器编号)."""
    validator = WorkflowValidator(WorkstationLoader(use_new_format=True))

    missing_count = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "General_Material_Station_V1",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2, 3]},
            }
        ]
    }
    report = validator.validate(missing_count)
    assert report["status"] == "failed"
    assert any("容器数量" in e and "missing_required_parameter" in e for e in report["errors"])

    open_lid_incomplete = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "开盖",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
            }
        ]
    }
    report = validator.validate(open_lid_incomplete)
    assert report["status"] == "failed"
    for required in ("容器数量", "开盖编号", "保留瓶盖"):
        assert any(required in e for e in report["errors"]), required

    complete = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "General_Material_Station_V1",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器数量": 3, "容器编号": [1, 2, 3]},
            },
            {
                "step_number": 2,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "开盖编号": [1],
                    "保留瓶盖": "是",
                },
            },
        ]
    }
    assert validator.validate(complete)["status"] == "passed"

    count_mismatch = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "General_Material_Station_V1",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器数量": 2, "容器编号": [1, 2, 3]},
            }
        ]
    }
    report = validator.validate(count_mismatch)
    assert report["status"] == "failed"
    assert any("container_count_mismatch" in e for e in report["errors"])


def test_label_value_enum_param_is_not_a_type_mismatch():
    """Regression: 保留瓶盖 is declared int in SKILL.md but dispatched as the
    label string 是/否; it must not be flagged type_mismatch, and an invalid
    label must still be rejected."""
    loader = WorkstationLoader(use_new_format=True)
    validator = WorkflowValidator(loader)

    valid_label = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "Liquid_Handling_Station_5ml_V2",
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "开盖瓶号": [1],
                    "保留瓶盖": "是",
                },
            }
        ]
    }
    assert validator.validate(valid_label)["status"] == "passed"

    # numeric form of the same enum also valid
    valid_value = json.loads(json.dumps(valid_label))
    valid_value["steps"][0]["parameters"]["保留瓶盖"] = 1
    assert validator.validate(valid_value)["status"] == "passed"

    invalid_label = json.loads(json.dumps(valid_label))
    invalid_label["steps"][0]["parameters"]["保留瓶盖"] = "也许"
    report = validator.validate(invalid_label)
    assert report["status"] == "failed"
    assert any("保留瓶盖" in error for error in report["errors"])


if __name__ == "__main__":
    test_temporal_addition_stirring_soft_error_retries_as_batches()
    test_generic_soft_complaint_also_retries()
    test_unverifiable_condition_becomes_human_review_not_infeasible()
    test_hard_capability_gap_stays_physical_infeasible()
    test_missing_required_station_is_not_softened()
    test_constraint_text_classification_buckets()
    test_unconfirmable_precondition_station_is_human_review_not_hard()
    test_liquid_query_keeps_all_ranges_and_stirrer_visible()
    test_single_device_agent_success_package()
    test_deterministic_completion_avoids_llm_repair_round()
    test_completion_applies_to_self_repair_output()
    test_weighing_handoff_triggers_audit_repair_and_route_note()
    test_two_bounded_repair_rounds_then_success()
    test_lid_conflict_and_txt_mismatch_are_deterministic_errors()
    test_single_device_agent_feasibility_error_package()
    test_fabricated_dispatch_parameter_is_rejected()
    test_workflow_validator_accepts_reference_and_rejects_bad_values()
    test_skill_required_params_enforced_for_skill_form_stations()
    test_label_value_enum_param_is_not_a_type_mismatch()
    print("single device agent tests passed")
