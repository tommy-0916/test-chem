"""Offline regression tests for Research request-boundary timing."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from agent_skills.llm_timing import measure_llm_request
from reaserch_agent.core import BaseAgent
from reaserch_agent.utils.llm_factory import CodexResponsesModel


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _ChatModel:
    model_name = "offline-chat"

    def __init__(self, prompt_canary: str, key_canary: str) -> None:
        self.prompt_canary = prompt_canary
        self.api_key = key_canary
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        assert any(
            self.prompt_canary in str(getattr(message, "content", message))
            for message in messages
        )
        return SimpleNamespace(content="ok")


class _RetryingChatModel(_ChatModel):
    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("offline timeout")
        return SimpleNamespace(content="ok")


class _SelfTimedChatModel(_ChatModel):
    handles_request_timing = True

    def invoke(self, messages):
        with measure_llm_request(
            component="research",
            model=self.model_name,
            transport="self_timed_chat",
        ):
            return super().invoke(messages)


class _FakeResponses:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **payload):
        self.calls += 1
        response = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
        }
        if payload.get("stream"):
            return _FakeStream(
                [{"type": "response.completed", "response": response}]
            )
        return response


class _FakeStream:
    def __init__(self, events) -> None:
        self.events = events
        self.closed = False

    def __iter__(self):
        yield from self.events

    def close(self):
        self.closed = True


def test_base_agent_times_each_plain_chat_wire_request_once(
    tmp_path, monkeypatch, caplog,
):
    output = tmp_path / "research-chat-timing.jsonl"
    prompt_canary = "PRIVATE_RESEARCH_PROMPT_CANARY"
    key_canary = "sk-private-research-key-canary"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "0")
    caplog.set_level(logging.DEBUG)

    model = _ChatModel(prompt_canary, key_canary)
    result = BaseAgent(model=model).invoke_text("system", prompt_canary)

    assert result == "ok"
    assert model.calls == 1
    events = _events(output)
    assert [event["status"] for event in events] == ["started", "success"]
    assert events[-1]["component"] == "research"
    assert events[-1]["model"] == "offline-chat"
    assert events[-1]["transport"] == "research_chat"
    rendered = output.read_text(encoding="utf-8") + caplog.text
    assert prompt_canary not in rendered
    assert key_canary not in rendered


def test_base_agent_does_not_double_count_a_self_timed_model(
    tmp_path, monkeypatch,
):
    output = tmp_path / "research-self-timed.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "0")
    model = _SelfTimedChatModel("PROMPT_CANARY", "KEY_CANARY")

    assert BaseAgent(model=model).invoke_text("system", "PROMPT_CANARY") == "ok"

    assert model.calls == 1
    events = _events(output)
    assert [event["status"] for event in events] == ["started", "success"]
    assert {event["transport"] for event in events} == {"self_timed_chat"}


def test_base_agent_records_each_retried_chat_wire_request_separately(
    tmp_path, monkeypatch,
):
    output = tmp_path / "research-chat-retry-timing.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "1")
    model = _RetryingChatModel("PROMPT_CANARY", "KEY_CANARY")
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", lambda _seconds: None)

    assert BaseAgent(model=model).invoke_text("system", "PROMPT_CANARY") == "ok"

    assert model.calls == 2
    events = _events(output)
    request_events = [
        event for event in events
        if event["status"] in {"started", "failed", "success"}
    ]
    assert [event["status"] for event in request_events] == [
        "started", "failed", "started", "success",
    ]
    assert len({event["attempt_id"] for event in request_events}) == 2
    assert len({event["logical_call_id"] for event in request_events}) == 1


def test_direct_responses_times_the_true_request_boundary_once_and_is_secret_free(
    tmp_path, monkeypatch, caplog,
):
    output = tmp_path / "research-responses-timing.jsonl"
    prompt_canary = "PRIVATE_RESPONSES_PROMPT_CANARY"
    key_canary = "sk-private-responses-key-canary"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(output))
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "direct")
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    monkeypatch.setenv("REFINER_RESPONSES_CLI_FALLBACK", "0")
    caplog.set_level(logging.DEBUG)
    responses = _FakeResponses()
    model = CodexResponsesModel(
        model="offline-responses",
        api_key=key_canary,
        base_url="https://provider.invalid",
        client=SimpleNamespace(responses=responses),
    )

    result = BaseAgent(model=model).invoke_text("system", prompt_canary)

    assert result == "ok"
    assert responses.calls == 1
    events = _events(output)
    assert [event["status"] for event in events].count("started") == 1
    assert [event["status"] for event in events].count("success") == 1
    assert events[-1]["last_event_type"] == "response.completed"
    assert {event["transport"] for event in events} == {"research_responses"}
    rendered = output.read_text(encoding="utf-8") + caplog.text
    assert prompt_canary not in rendered
    assert key_canary not in rendered
