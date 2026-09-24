"""Offline context-budget recovery without changing the frozen device plan."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_skills.native_tools import NativeToolConfigurationError
from single_agent import SingleDeviceAgent, SingleDeviceAgentState
from utils.llm_factory import _sanitized_gateway_exception


class ContextLimitError(RuntimeError):
    code = "context_length_exceeded"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Network is disabled in translation tests")
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


def make_plan(count=6):
    return {
        "device_plan": [{
            "plan_step": index * 2 + 11, "workstation": f"Station{index}",
            "source_macro_step": index + 1, "source_macro_steps": [index + 1, index + 101],
        } for index in range(count)],
        "container_plan": [{"sample_id": "six-samples", "容器编号": [1, 2, 3, 4, 5, 6]}],
        "reagent_slot_plan": [], "temporal_adaptations": [{"schedule": "frozen"}],
        "offline_handoffs": [{"name": "keep this handoff"}],
    }


class ContextBoundModel:
    """Read real translation prompts and fail only the oversized ranges."""

    def __init__(self, plan, threshold=2):
        self.plan = copy.deepcopy(plan)
        self.threshold = threshold
        self.calls = []
        self.contract = "COMPLETE_CONTRACT_START\n" + "detail\n" * 900 + "COMPLETE_CONTRACT_END"

    def invoke(self, messages):
        prompt = messages[-1].content
        ids = json.loads(re.search(r"plan_step 编号属于 (\[[^\]]*\])", prompt).group(1))
        plan_text = prompt.split("## 完整 device_plan", 1)[1].split("\n", 1)[1].split("## 本次只需翻译的范围", 1)[0]
        plan_view = json.loads(plan_text.strip())
        assert plan_view["device_plan"] == self.plan["device_plan"]
        assert plan_view["container_plan"] == self.plan["container_plan"]
        assert self.contract in prompt
        assert "KEEP_REPAIR_INSTRUCTION" in prompt
        carry_text = prompt.split("## 前序步骤结束时的容器状态", 1)[1].split("\n", 1)[1].split("## 各工作站参数表", 1)[0].strip()
        carry = json.loads(carry_text) if carry_text.startswith("{") else {}
        self.calls.append({"ids": ids, "carryover": carry})
        if len(ids) > self.threshold:
            raise ContextLimitError("provider rejected the input token budget")
        by_id = {step["plan_step"]: step for step in self.plan["device_plan"]}
        steps = [{
            "step_number": 1, "workstation": by_id[identifier]["workstation"],
            "operation": "开盖" if identifier == 11 else "关盖",
            "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
            "source_plan_step": identifier,
            "source_macro_step": by_id[identifier]["source_macro_step"],
            "source_macro_steps": by_id[identifier]["source_macro_steps"],
        } for identifier in ids]
        return SimpleNamespace(content=json.dumps({
            "workflow_json": {"steps": steps},
            "workflow_txt": "\n".join(f"第1步 plan {identifier}" for identifier in ids),
        }, ensure_ascii=False))


def agent_with_model(plan, threshold=2):
    # No loader, provider client, output directory or hardware setup is needed.
    agent = object.__new__(SingleDeviceAgent)
    model = ContextBoundModel(plan, threshold)
    agent._model = model
    agent._assert_workstation_snapshot_current = lambda state: None
    agent._station_parameter_tables = lambda stations: model.contract
    agent._truth_workstation_code = lambda name: name
    agent._translation_chunk_size = lambda: 6
    state = SingleDeviceAgentState(research_handoff={"macro_action": {"id": "frozen"}}, exp_id="offline-context-budget")
    return agent, model, state


def test_recursive_split_preserves_full_contracts_plan_sources_and_carryover():
    plan = make_plan()
    frozen = copy.deepcopy(plan)
    agent, model, state = agent_with_model(plan)
    carry = {"进样瓶#1": {"lid": "有盖", "sample_id": "A"}, "进样瓶#2": {"lid": "无盖"}}
    original_carry = copy.deepcopy(carry)
    result = agent._invoke_translation_chunk_bounded(
        state, plan, plan["device_plan"], 0, 1, carry,
        extra_instruction="KEEP_REPAIR_INSTRUCTION",
    )
    steps = result["workflow_json"]["steps"]
    assert [step["source_plan_step"] for step in steps] == [step["plan_step"] for step in plan["device_plan"]]
    assert [step["source_macro_steps"] for step in steps] == [step["source_macro_steps"] for step in plan["device_plan"]]
    after_first = next(call for call in model.calls if call["ids"] == [13, 15])
    assert after_first["carryover"]["进样瓶#1"] == {"lid": "无盖", "sample_id": "A"}
    right_half = next(call for call in model.calls if call["ids"] == [17, 19, 21])
    assert right_half["carryover"]["进样瓶#1"] == {"lid": "有盖", "sample_id": "A"}
    assert right_half["carryover"]["进样瓶#2"] == {"lid": "无盖"}
    assert plan == frozen and carry == original_carry


def test_split_outputs_keep_original_logical_chunk_and_targeted_repair_cache():
    plan = make_plan(12)
    agent, model, state = agent_with_model(plan)
    cache = {}
    kwargs = {"feedback_by_chunk": {0: "KEEP_REPAIR_INSTRUCTION", 1: "KEEP_REPAIR_INSTRUCTION"}}
    workflow, text, owners, groups = agent._translate_plan_in_chunks(state, plan, cache, **kwargs)
    assert set(cache) == {0, 1} and list(map(len, groups)) == [6, 6]
    assert [step["step_number"] for step in workflow["steps"]] == list(range(1, 13))
    assert owners == {index: (index - 1) // 6 for index in range(1, 13)}
    assert re.findall(r"第(\d+)步", text) == list(map(str, range(1, 13)))
    assert workflow["offline_handoffs"] == plan["offline_handoffs"]
    first_cached = copy.deepcopy(cache[0])
    model.calls.clear()
    agent._translate_plan_in_chunks(state, plan, cache, only_chunks={1}, **kwargs)
    assert cache[0] == first_cached
    assert all(min(call["ids"]) >= 23 for call in model.calls)
    assert model.calls[0]["carryover"]["进样瓶#1"]["lid"] == "有盖"


def test_binary_split_is_bounded_by_step_count():
    plan = make_plan()
    agent, model, state = agent_with_model(plan, threshold=1)
    agent._invoke_translation_chunk_bounded(
        state, plan, plan["device_plan"], 0, 1, {}, extra_instruction="KEEP_REPAIR_INSTRUCTION",
    )
    assert len(model.calls) == 2 * len(plan["device_plan"]) - 1


def test_single_step_overflow_fails_clearly_without_further_retry():
    plan = make_plan(1)
    agent, model, state = agent_with_model(plan, threshold=0)
    with pytest.raises(RuntimeError, match=r"indivisible plan_step IDs \[11\].*full contracts") as error:
        agent._invoke_translation_chunk_bounded(
            state, plan, plan["device_plan"], 0, 1, {}, extra_instruction="KEEP_REPAIR_INSTRUCTION",
        )
    assert isinstance(error.value.__cause__, ContextLimitError)
    assert len(model.calls) == 1


@pytest.mark.parametrize("error", [
    TimeoutError("API timed out"), RuntimeError("rate limit exceeded for tokens"),
    RuntimeError("max_tokens exceeds output limit"), RuntimeError("401 invalid API key"),
    ValueError("invalid JSON; context_length_exceeded"),
    NativeToolConfigurationError("context window exceeded; unsupported configuration"),
])
def test_other_failures_never_trigger_batch_splitting(error):
    plan = make_plan()
    agent, _, state = agent_with_model(plan)
    agent._invoke_translation_chunk = Mock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        agent._invoke_translation_chunk_bounded(state, plan, plan["device_plan"], 0, 1, {})
    assert caught.value is error
    assert agent._invoke_translation_chunk.call_count == 1


@pytest.mark.parametrize("error", [
    RuntimeError("This model's maximum context length is 8192 tokens. However, you requested 9000 tokens."),
    RuntimeError("prompt is too long: 220000 tokens > 200000 maximum"),
    RuntimeError("Your input exceeds the context window of this model. Please adjust your input."),
    RuntimeError("The input token count (20000) exceeds the maximum number of tokens allowed (10000)."),
])
def test_explicit_provider_context_messages_are_recognized(error):
    assert SingleDeviceAgent._is_translation_context_limit_error(error)


def test_structured_provider_error_code_is_recognized_without_message_guessing():
    error = RuntimeError("Bad Request")
    error.body = {"error": {"code": "context_length_exceeded"}}
    assert SingleDeviceAgent._is_translation_context_limit_error(error)


def test_sanitized_chat_context_limit_still_triggers_local_bisection():
    class PrivateContextError(RuntimeError):
        status_code = 400
        body = {
            "error": {
                "code": "context_length_exceeded",
                "message": "PRIVATE_CONTEXT_BODY",
            }
        }

    safe_error = _sanitized_gateway_exception(
        PrivateContextError("PRIVATE_CONTEXT_BODY")
    )
    assert "PRIVATE_CONTEXT_BODY" not in str(safe_error)
    assert SingleDeviceAgent._is_translation_context_limit_error(safe_error)

    plan = make_plan(2)
    agent, _, state = agent_with_model(plan)
    half = {"workflow_json": {"steps": []}, "workflow_txt": ""}
    agent._invoke_translation_chunk = Mock(
        side_effect=[safe_error, copy.deepcopy(half), copy.deepcopy(half)]
    )

    result = agent._invoke_translation_chunk_bounded(
        state, plan, plan["device_plan"], 0, 1, {}
    )

    assert result == half
    assert agent._invoke_translation_chunk.call_count == 3


def test_partial_split_failure_never_replaces_the_logical_chunk_cache():
    plan = make_plan()
    agent, _, state = agent_with_model(plan)
    old = {"steps": [{"operation": "cached"}], "txt": "prior candidate"}
    cache = {0: copy.deepcopy(old)}
    agent._invoke_translation_chunk = Mock(side_effect=[
        ContextLimitError("too many input tokens"),
        {"workflow_json": {"steps": []}, "workflow_txt": ""},
        TimeoutError("provider unavailable on right half"),
    ])
    with pytest.raises(TimeoutError):
        agent._translate_plan_in_chunks(state, plan, cache)
    assert cache == {0: old}
    assert agent._invoke_translation_chunk.call_count == 3
