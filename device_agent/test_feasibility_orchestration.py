"""Offline regressions for scoped planning, atomic progress and failure routing."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from device_agent.single_agent import SingleDeviceAgent, SingleDeviceAgentState
from agent_skills.responses_stream import ResponsesTerminalError


def fragment(source, number, matrix):
    return {
        "status": "device_plan",
        "feasibility": {"is_feasible": True, "blocking_constraints": []},
        "sample_control_matrix": copy.deepcopy(matrix),
        "device_plan": [{
            "plan_step": number, "workstation": "Alpha",
            "source_macro_step": source, "source_macro_steps": [source],
            "objective": "read-only fake operation",
        }],
    }


def fixture_agent():
    agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
    agent._contract_version = "v2"
    agent._active_semantic_analysis = {"status": "semantic_analysis", "immutable": True}
    agent._assert_workstation_snapshot_current = Mock()
    agent._sync_skill_load_state = Mock()
    handoff = {
        "macro_action_steps": [
            {"步骤序号": 7, "参数": "original-first", "macro_step_id": "MS_A"},
            {"步骤序号": 11, "参数": "original-second", "macro_step_id": "MS_B"},
        ],
        "sample_control_matrix": [{"sample_id": "A", "condition": "fixed"}],
        "research_context": {"survey": "UNRELATED_RESEARCH_BULK_" * 500},
        "observations": [{
            "observation_id": "obs-1", "sample_id": "A", "actual_mass": "2 mg",
        }],
    }
    return agent, SingleDeviceAgentState(research_handoff=handoff, exp_id="offline")


def test_fragment_scope_keeps_full_handoff_and_stable_original_ids(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    agent, state = fixture_agent()
    original = copy.deepcopy(state.research_handoff)
    calls = []

    def request(current_state, **kwargs):
        assert current_state.research_handoff == original
        calls.append(kwargs)
        source = [7, 11][len(calls) - 1]
        return fragment(source, len(calls), original["sample_control_matrix"])

    agent._invoke_feasibility_request = request
    result = agent._invoke_feasibility_plan(state)
    assert [row["source_macro_step"] for row in result["device_plan"]] == [7, 11]
    assert [call["step_name"] for call in calls] == [
        "feasibility_device_plan_chunk_1_of_2", "feasibility_device_plan_chunk_2_of_2",
    ]
    assert [call["fragment_context"]["current_macro_id"] for call in calls] == ["7", "11"]
    assert "UNRELATED_RESEARCH_BULK" not in json.dumps(
        [call["fragment_context"] for call in calls], ensure_ascii=False,
    )
    prefix_step = calls[1]["fragment_context"]["accepted_prefix_symbols"][
        "device_plan"
    ][0]
    assert prefix_step["plan_step"] == 1
    assert prefix_step["workstation"] == "Alpha"
    assert prefix_step["source_macro_steps"] == [7]
    assert prefix_step["objective"] == "read-only fake operation"
    assert prefix_step["record_sha256"]
    assert calls[0]["fragment_context"]["observation_evidence_catalog"]
    assert calls[0]["fragment_context"]["observation_evidence_catalog"][0][
        "observation_id"
    ] == "obs-1"
    assert state.feasibility_progress[-1]["status"] == "assembled_pending_global_audit"
    assert state.feasibility_accepted is False
    assert state.workflow_json == {}
    assert state.research_handoff == original


def test_fragment_request_prompt_never_reinjects_full_handoff_or_semantics(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_REASONING_EFFORT", raising=False)
    agent, state = fixture_agent()
    agent._model = object()
    agent._active_semantic_analysis = {
        "status": "semantic_analysis",
        "macro_step_assessments": [{
            "source_macro_step": "7", "reason": "current-only",
        }],
        "unrelated_semantic_bulk": "UNRELATED_SEMANTIC_BULK_" * 500,
    }
    context = {
        "context_contract": "feasibility_fragment_context_v1",
        "current_macro_id": "7",
        "current_macro": {"步骤序号": 7, "参数": "original-first"},
        "semantic_assessment": [{
            "source_macro_step": "7", "reason": "current-only",
        }],
        "material_identity_registry": [],
        "semantic_analysis_sha256": "semantic-digest",
        "accepted_prefix_symbols": {"candidate_sha256": "prefix-digest"},
    }
    agent._invoke_json_object_with_format_retry = Mock(
        return_value={"status": "device_plan", "device_plan": []}
    )

    agent._invoke_feasibility_request(
        state,
        step_name="feasibility_device_plan_chunk_1_of_2",
        fragment_context=context,
    )

    messages = agent._invoke_json_object_with_format_retry.call_args.args[1]
    prompt = messages[1].content
    assert "UNRELATED_RESEARCH_BULK" not in prompt
    assert "UNRELATED_SEMANTIC_BULK" not in prompt
    assert "original-first" in prompt
    assert "semantic-digest" in prompt


def test_fragment_contract_retry_only_repeats_current_chunk(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    agent, state = fixture_agent()
    matrix = state.research_handoff["sample_control_matrix"]
    agent._invoke_feasibility_request = Mock(side_effect=[
        fragment(7, 1, matrix),
        fragment(11, 1, matrix),  # collision; immutable prefix must survive
        fragment(11, 2, matrix),
    ])
    result = agent._invoke_feasibility_plan(state)
    names = [call.kwargs["step_name"] for call in agent._invoke_feasibility_request.call_args_list]
    assert names == ["feasibility_device_plan_chunk_1_of_2"] + ["feasibility_device_plan_chunk_2_of_2"] * 2
    assert len(result["device_plan"]) == 2
    assert len(state.feasibility_progress[0]["completed_chunks"]) == 2


def test_later_failure_preserves_diagnostic_prefix_without_acceptance(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    agent, state = fixture_agent()
    error = ResponsesTerminalError("upstream failed", diagnostics={
        "event_type": "response.failed", "response_status": "failed",
        "error": {"code": "upstream_error"},
    })
    agent._invoke_feasibility_request = Mock(side_effect=[
        fragment(7, 1, state.research_handoff["sample_control_matrix"]), error,
    ])
    with pytest.raises(ResponsesTerminalError):
        agent._invoke_feasibility_plan(state)
    progress = state.feasibility_progress[0]
    assert progress["status"] == "failed"
    assert progress["active_macro_id"] == "11"
    assert [row["plan_step"] for row in progress["candidate"]["device_plan"]] == [1]
    assert state.feasibility_accepted is False
    assert state.feasibility_certificate == {}
    assert state.workflow_json == {}


def test_runtime_later_failure_never_calls_approval_or_translation(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    agent, initial = fixture_agent()
    agent._txt_format_reference = ""
    agent._json_format_reference = ""
    agent._skill_session = None
    agent._refresh_workstation_snapshot = Mock()
    agent._workstation_skill_session = Mock(return_value=SimpleNamespace(
        discovery_context=lambda: "offline", truth_digest=lambda: "frozen",
    ))
    agent._llm_semantic_analysis_enabled = lambda: False
    agent._verified_stage1_core_route_gap_result = lambda *args: None
    agent._device_snapshot_id = lambda: "offline"
    agent._accept_feasibility_plan = Mock()
    agent._run_accepted_device_plan = Mock()
    agent._invoke_feasibility_request = Mock(side_effect=[
        fragment(7, 1, initial.research_handoff["sample_control_matrix"]), RuntimeError("bounded failure"),
    ])
    state = agent.run_state(initial.research_handoff, exp_id="offline")
    assert state.status == "failed"
    assert state.terminal_package["workflow_json"] == {}
    assert state.feasibility_progress[0]["status"] == "failed"
    agent._accept_feasibility_plan.assert_not_called()
    agent._run_accepted_device_plan.assert_not_called()


def test_single_mode_preserves_legacy_call_shape(monkeypatch):
    monkeypatch.setenv("CHEM_DEVICE_FEASIBILITY_MODE", "single")
    agent, state = fixture_agent()
    agent._invoke_feasibility_request = Mock(return_value={"status": "device_plan"})
    assert agent._invoke_feasibility_plan(state) == {"status": "device_plan"}
    agent._invoke_feasibility_request.assert_called_once_with(state, extra_instruction="")
    assert state.feasibility_progress == []


@pytest.mark.parametrize("status", ["feasibility_error", "terminal_unmappable", "device_internal_error", "failed"])
def test_hard_terminal_fragment_stops_before_next_macro(monkeypatch, status):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    agent, state = fixture_agent()
    agent._invoke_feasibility_request = Mock(return_value={"status": status})
    result = agent._invoke_feasibility_plan(state)
    assert result["status"] == status
    agent._invoke_feasibility_request.assert_called_once()
    assert state.feasibility_progress[-1]["status"] == "blocked"


def test_manual_fragment_keeps_review_while_remaining_macros_are_planned(monkeypatch):
    monkeypatch.delenv("CHEM_DEVICE_FEASIBILITY_MODE", raising=False)
    agent, state = fixture_agent()
    first = fragment(7, 1, state.research_handoff["sample_control_matrix"])
    first.update(status="manual_required", feedback_type="human_review_required",
                 quantity_audit={"status": "human_review_required", "issues": [{"code": "unknown_yield"}]})
    agent._invoke_feasibility_request = Mock(side_effect=[
        first, fragment(11, 2, state.research_handoff["sample_control_matrix"]),
    ])
    result = agent._invoke_feasibility_plan(state)
    assert result["status"] == "manual_required"
    assert len(result["device_plan"]) == 2
    assert result["quantity_audit"]["issues"] == [{"code": "unknown_yield"}]
    assert state.feasibility_accepted is False


def test_feasibility_native_retry_metadata_and_step_scope(monkeypatch):
    agent, state = fixture_agent()
    agent._model = object()
    agent._workstation_skill_session = lambda: SimpleNamespace(
        codes=set(), discovery_context=lambda: "offline", assert_current=lambda: None,
        tool=lambda: object(), referenced_codes=lambda result: [],
    )
    diagnostics = {"event_type": "response.failed", "response_status": "failed", "error": {"code": "upstream_error"}}

    def native(*args, **kwargs):
        assert kwargs["retry_upstream_errors"] is True
        assert kwargs["max_upstream_retries"] == 1
        kwargs["on_model_attempt"]({
            "event": "model_attempt_failed", "model_turn": 3, "attempt": 1,
            "retry_scheduled": True, "exception_type": "ResponsesTerminalError",
            "responses_diagnostics": diagnostics,
        })
        return SimpleNamespace(content=json.dumps({"status": "device_plan", "device_plan": []}))

    with patch("device_agent.single_agent.invoke_with_tools", side_effect=native):
        agent._invoke_json_object_with_format_retry(
            state, [], step_name="feasibility_device_plan_chunk_2_of_7", workstation_tools=True,
        )
    assert state.llm_request_attempts[0]["model_turn"] == 3
    assert state.llm_diagnostics[0]["task_name"] == "feasibility_device_plan_chunk_2_of_7"
    assert state.llm_diagnostics[0]["retry_scheduled"] is True
    with patch("device_agent.single_agent.invoke_with_tools", return_value=SimpleNamespace(content='{"status":"ok"}')) as native:
        agent._invoke_json_object_with_format_retry(state, [], step_name="workflow_translation", workstation_tools=True)
    assert "retry_upstream_errors" not in native.call_args.kwargs


def test_injected_device_model_emits_metadata_only_timing(tmp_path, monkeypatch):
    timing_path = tmp_path / "device-timing.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(timing_path))
    agent, state = fixture_agent()
    agent._model = SimpleNamespace(
        model_name="offline-model",
        invoke=lambda messages: SimpleNamespace(content='{"status":"ok"}'),
        _chem_gateway_max_retries=0,
    )

    result = agent._invoke_json_object_with_format_retry(
        state,
        [{"role": "user", "content": "PRIVATE_DEVICE_PROMPT"}],
        step_name="offline_device_timing",
    )

    assert result == {"status": "ok"}
    raw = timing_path.read_text()
    assert "PRIVATE_DEVICE_PROMPT" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert [event["status"] for event in events] == ["started", "success"]
