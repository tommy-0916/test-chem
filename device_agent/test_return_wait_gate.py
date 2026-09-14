"""Offline regression tests for frozen Research external-feedback barriers."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_DEVICE_ROOT = str(Path(__file__).resolve().parent)
if _DEVICE_ROOT not in sys.path:
    sys.path.insert(0, _DEVICE_ROOT)

import single_agent as agent_module
from single_agent import SingleDeviceAgent, SingleDeviceAgentState


WAIT = "等待真实 XRD 数据和人工晶相分析后再决定下一批加热条件"


def handoff(*, terminal=False):
    steps = [
        {"步骤序号": "xrd", "操作": "XRD", "试剂/对象": "催化剂", "参数": "采集图谱"},
        {"步骤序号": "heat", "操作": "加热", "试剂/对象": "催化剂", "参数": "80 ℃，10 min"},
    ]
    if terminal:
        steps.reverse()
    steps[-1 if terminal else 0]["intermediate_returns"] = [{
        "name": "晶相人工判断",
        "availability": "undeclared",
        "required_for_next_step": True,
        "feedback_kind": "intermediate_feedback",
        "delivery_mode": "observation",
        "wait_for": WAIT,
    }]
    return {"macro_action_steps": steps}


def candidate(*, terminal=False):
    order = ["heat", "xrd"] if terminal else ["xrd", "heat"]
    plan = [{
        "plan_step": index,
        "workstation": "XRD_V1" if source == "xrd" else "Heating_V1",
        "operation_intent": "XRD" if source == "xrd" else "加热",
        "source_macro_step": source,
    } for index, source in enumerate(order, 1)]
    return {
        "status": "device_plan",
        "device_plan": plan,
        "workflow_txt": "unapproved full workflow",
        "workflow_json": {"steps": [{
            "step_number": item["plan_step"],
            "source_plan_step": item["plan_step"],
            "source_macro_step": item["source_macro_step"],
            "workstation": item["workstation"],
            "operation": item["operation_intent"],
            "parameters": {},
        } for item in plan]},
        "dispatch_validation": {"status": "passed", "errors": []},
        "dispatch_payload": {"untrusted_precomputed_payload": True},
        "offline_handoffs": [],
    }


@pytest.fixture
def agent(monkeypatch):
    # Exercise the actual deterministic methods without loading equipment,
    # configuring a model, generating files, or dispatching anything.
    value = SingleDeviceAgent.__new__(SingleDeviceAgent)
    value._active_semantic_analysis = {}
    value._dispatch_catalog = object()
    monkeypatch.setattr(value, "_device_snapshot_id", lambda: "test-snapshot")
    monkeypatch.setattr(value, "_full_device_truth_digest", lambda: "test-full-truth")
    monkeypatch.setattr(value, "_verified_stage1_core_route_gap_result", lambda *_: None)
    monkeypatch.setattr(value, "_normalize_plan_handoff_steps", lambda _, plan: copy.deepcopy(plan))
    return value


def current(*, terminal=False):
    return SingleDeviceAgentState(research_handoff=handoff(terminal=terminal), exp_id="return-wait")


def assert_waiting(result, state):
    assert result["status"] == "manual_required"
    assert result["feedback_route"] == "human"
    assert result["error_package"]["type"] == "device_external_return_wait_required"
    assert result["error_package"]["pending_returns"][0]["source_macro_step"] == "xrd"
    assert result["error_package"]["pending_returns"][0]["wait_for"] == WAIT
    assert result["workflow_json"] == {}
    assert result["dispatch_payload"] == {}
    assert result["feasibility_accepted"] is False
    assert result["feasibility_certificate"] == {}
    assert state.feasibility_accepted is False
    assert state.feasibility_certificate == {}
    assert len(result["device_plan"]) == 2
    assert result["macro_plan"] == state.research_handoff


def test_candidate_omitting_qualitative_xrd_wait_cannot_receive_certificate(agent):
    state = current()
    proposed = candidate()
    before = copy.deepcopy(proposed)
    result = agent._accept_feasibility_plan(state, proposed)
    assert_waiting(result, state)
    assert proposed == before
    assert result["manual_repair_context"]["last_workflow"] == before["workflow_json"]
    with pytest.raises(ValueError, match="external return wait"):
        agent._build_feasibility_certificate(state, proposed)


def test_terminal_xrd_return_keeps_normal_success_and_dispatch(agent, monkeypatch):
    state = current(terminal=True)
    proposed = candidate(terminal=True)
    assert agent._external_return_wait_findings(state.research_handoff, proposed) == []
    state.feasibility_certificate = agent._build_feasibility_certificate(state, proposed)
    state.feasibility_accepted = True
    monkeypatch.setattr(agent_module, "format_dispatch_payload", lambda workflow, *_args, **_kwargs: {
        "payload": {"experiment_steps": {"steps": copy.deepcopy(workflow["steps"])}},
        "mapped_steps": len(workflow["steps"]), "unmapped_steps": 0,
    })
    result = agent._normalize_terminal_package(state, proposed)
    assert result["status"] == "success"
    assert len(result["dispatch_payload"]["experiment_steps"]["steps"]) == 2
    assert result["feasibility_certificate"]["external_return_contracts"][0]["wait_for"] == WAIT


@pytest.mark.parametrize("kind", ["legacy", "optional", "declared"])
def test_legacy_optional_and_declared_returns_do_not_trigger_external_wait_gate(agent, kind):
    research = handoff()
    feedback = research["macro_action_steps"][0]["intermediate_returns"][0]
    if kind == "legacy":
        research["macro_action_steps"][0].pop("intermediate_returns")
    elif kind == "optional":
        feedback["required_for_next_step"] = False
    else:
        feedback["availability"] = "declared"
    assert agent._external_return_wait_findings(research, candidate()) == []
    state = SingleDeviceAgentState(research_handoff=research, exp_id="compatible")
    certificate = agent._build_feasibility_certificate(state, candidate())
    assert agent._validate_feasibility_certificate(state, certificate, require_snapshot_match=True) == []


def test_plan_repair_cannot_ask_model_to_remove_or_satisfy_wait(agent):
    state = current()
    result = agent._repair_plan_level_findings(state, candidate())
    assert_waiting(result, state)
    assert result["device_plan"] == candidate()["device_plan"]


def test_nested_previous_certificates_cannot_unlock_waiting_package(agent):
    state = current()
    proposed = candidate()
    accepted = {"feasibility_accepted": True, "feasibility_certificate": {"accepted": True}}
    proposed["manual_repair_context"] = {"previous": copy.deepcopy(accepted)}
    proposed["history"] = [{"wrapper": {**copy.deepcopy(accepted), "dispatch_payload": {"steps": [1]}}}]
    result = agent._normalize_terminal_package(state, proposed)
    assert_waiting(result, state)
    for nested in (result["manual_repair_context"]["previous"], result["history"][0]["wrapper"]):
        assert nested["feasibility_accepted"] is False
        assert nested["feasibility_certificate"]["accepted"] is False
    assert result["history"][0]["wrapper"]["dispatch_payload"] == {}
    assert proposed["manual_repair_context"]["previous"]["feasibility_accepted"] is True


def test_restored_accepted_plan_cannot_skip_wait(agent):
    state = current()
    state.feasibility_accepted = True
    state.feasibility_certificate = {"accepted": True, "certificate_id": "old-pre-gate"}
    result = agent._run_accepted_device_plan(
        state, candidate(), allow_plan_rewrite=False, resumed_from_manual=True,
    )
    assert_waiting(result, state)


def test_final_workflow_rewrite_cannot_bypass_wait_with_false_satisfaction_claim(agent):
    state = current()
    state.feasibility_accepted = True
    state.feasibility_certificate = {"accepted": True, "certificate_id": "old"}
    proposed = candidate()
    proposed.update({"status": "success", "pending_returns": [], "wait_satisfied": True})
    # Translation omitted the later plan source, but its executable workflow
    # still references the frozen downstream Research macro step.
    proposed["device_plan"][1]["source_macro_step"] = "xrd"
    result = agent._normalize_terminal_package(state, proposed)
    assert_waiting(result, state)
    normalized_again = agent._normalize_terminal_package(state, result)
    assert normalized_again["status"] == "manual_required"
    assert normalized_again["workflow_json"] == {}


def test_workflow_cannot_hide_later_source_by_lying_about_macro_trace(agent):
    state = current()
    proposed = candidate()
    for step in proposed["workflow_json"]["steps"]:
        step["source_macro_step"] = "xrd"
    findings = agent._external_return_wait_findings(state.research_handoff, proposed)
    assert any(item["kind"] == "workflow" for item in findings[0]["blocked_machine_steps"])


def test_return_contract_change_invalidates_old_certificate_even_for_terminal_return(agent):
    state = current(terminal=True)
    certificate = agent._build_feasibility_certificate(state, candidate(terminal=True))
    assert agent._validate_feasibility_certificate(state, certificate, require_snapshot_match=True) == []
    state.research_handoff["macro_action_steps"][-1]["intermediate_returns"][0]["wait_for"] = "changed"
    assert any("返回等待合同" in error for error in agent._validate_feasibility_certificate(
        state, certificate, require_snapshot_match=True,
    ))


def test_pre_gate_certificate_cannot_resume_new_wait_contract(agent):
    state = current(terminal=True)
    old_state = copy.deepcopy(state)
    old_state.research_handoff["macro_action_steps"][-1].pop("intermediate_returns")
    certificate = agent._build_feasibility_certificate(old_state, candidate(terminal=True))
    assert "external_return_contracts" not in certificate
    assert any("返回等待合同" in error for error in agent._validate_feasibility_certificate(
        state, certificate, require_snapshot_match=True,
    ))
