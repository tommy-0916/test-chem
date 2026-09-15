"""Tests for the tool-free direct Responses transport."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_skills.llm_retry import RetryableGatewayError, is_terminal_gateway_error
from agent_skills.responses_stream import ResponsesProtocolError, ResponsesTerminalError
from utils.llm_factory import (
    CodexResponsesModel,
    ModelPoolChatModel,
    OpenAICompatChatModel,
    configured_max_retries,
    normalize_openai_base_url,
)


class _FakeResponses:
    def __init__(self):
        self.payloads = []

    def create(self, **payload):
        self.payloads.append(payload)
        response = SimpleNamespace(
            output_text='{"status":"ok"}',
            status="completed",
        )
        if payload.get("stream"):
            self.stream = _FakeStream(
                [SimpleNamespace(type="response.completed", response=response)]
            )
            return self.stream
        return response


class _RetryableGatewayError(RuntimeError):
    status_code = 502


class _FakeStream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


def test_direct_responses_transport_has_no_agent_tools():
    responses = _FakeResponses()
    client = SimpleNamespace(responses=responses)
    previous_transport = os.environ.get("REFINER_RESPONSES_TRANSPORT")
    previous_fallback = os.environ.get("REFINER_RESPONSES_CLI_FALLBACK")
    os.environ["REFINER_RESPONSES_TRANSPORT"] = "direct"
    os.environ["REFINER_RESPONSES_CLI_FALLBACK"] = "0"
    try:
        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://provider.invalid",
            reasoning_effort="xhigh",
            timeout=12,
            max_output_tokens=32768,
            client=client,
        )
        response = model.invoke([
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Do the task."},
        ])
    finally:
        if previous_transport is None:
            os.environ.pop("REFINER_RESPONSES_TRANSPORT", None)
        else:
            os.environ["REFINER_RESPONSES_TRANSPORT"] = previous_transport
        if previous_fallback is None:
            os.environ.pop("REFINER_RESPONSES_CLI_FALLBACK", None)
        else:
            os.environ["REFINER_RESPONSES_CLI_FALLBACK"] = previous_fallback

    assert response.content == '{"status":"ok"}'
    payload = responses.payloads[0]
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["max_output_tokens"] == 32768
    assert payload["stream"] is True
    assert responses.stream.closed is True
    assert "tools" not in payload
    assert "Return JSON." in payload["input"]


def test_direct_responses_request_emits_metadata_only_timing(tmp_path, monkeypatch):
    timing_path = tmp_path / "device-timing.jsonl"
    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(timing_path))
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "direct")
    monkeypatch.setenv("REFINER_RESPONSES_CLI_FALLBACK", "0")
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="PRIVATE_KEY",
        base_url="https://provider.invalid",
        client=SimpleNamespace(responses=_FakeResponses()),
    )

    model.invoke([{"role": "user", "content": "PRIVATE_PROMPT"}])

    raw = timing_path.read_text()
    assert "PRIVATE" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert events[0]["status"] == "started"
    assert events[-1]["status"] == "success"
    assert events[-1]["last_event_type"] == "response.completed"


def test_direct_responses_streaming_aggregates_text(monkeypatch):
    final = SimpleNamespace(status="completed", output_text='{"status":"ok"}')
    stream = _FakeStream([
        SimpleNamespace(type="response.output_text.delta", delta='{"status":"'),
        SimpleNamespace(type="response.output_text.delta", delta='ok"}'),
        SimpleNamespace(type="response.completed", response=final),
    ])
    responses = SimpleNamespace(create=lambda **payload: stream)
    client = SimpleNamespace(responses=responses)
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "direct")
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    monkeypatch.setenv("REFINER_RESPONSES_CLI_FALLBACK", "0")
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        client=client,
    )

    response = model.invoke_json_object([
        {"role": "user", "content": "Return JSON."}
    ])

    assert response.content == '{"status":"ok"}'
    assert response.raw_response is final
    assert stream.closed is True


def test_openai_base_url_adds_v1_once():
    assert normalize_openai_base_url("https://api.aigateway.qzz.io") == (
        "https://api.aigateway.qzz.io/v1"
    )
    assert normalize_openai_base_url("https://api.aigateway.qzz.io/v1/") == (
        "https://api.aigateway.qzz.io/v1"
    )
    assert normalize_openai_base_url("https://provider.example/openai/v1") == (
        "https://provider.example/openai/v1"
    )


def test_responses_adapter_owns_retries_while_sdk_retries_are_disabled(monkeypatch):
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "8")
    response_model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    chat_model = OpenAICompatChatModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        timeout=12,
        temperature=0,
    )
    assert configured_max_retries() == 8
    assert response_model._transport_max_retries == 8
    assert response_model._client.max_retries == 0
    assert chat_model._transport_max_retries == 8
    assert chat_model._client.max_retries == 0


def test_responses_retry_wait_is_ten_seconds(monkeypatch):
    responses = _FakeResponses()
    successful = responses.create(stream=True)
    calls = iter([_RetryableGatewayError(), successful])

    payloads = []

    def create(**payload):
        payloads.append(payload)
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    responses.create = create
    client = SimpleNamespace(responses=responses)
    sleeps = []
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "direct")
    monkeypatch.setenv("REFINER_RESPONSES_CLI_FALLBACK", "0")
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", sleeps.append)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        client=client,
    )

    response = model.invoke_json_object(
        [{"role": "user", "content": "Return JSON."}]
    )

    assert response.content == '{"status":"ok"}'
    assert len(payloads) == 2
    assert sleeps == [10.0]


def test_retryable_direct_failure_never_replays_through_cli(monkeypatch):
    responses = SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(
        _RetryableGatewayError("bad gateway")
    ))
    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "direct")
    monkeypatch.setenv("REFINER_RESPONSES_CLI_FALLBACK", "1")
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        client=SimpleNamespace(responses=responses),
    )
    model._transport_max_retries = 0
    cli_calls = []
    model._invoke_cli = lambda prompt: cli_calls.append(prompt)

    # The adapter keeps retry semantics but strips provider-controlled text
    # before the exception crosses the backend boundary.
    with pytest.raises(RetryableGatewayError):
        model.invoke([{"role": "user", "content": "Plan."}])

    assert cli_calls == []


def test_explicit_cli_transport_uses_fixed_retry(monkeypatch):
    outcomes = iter(
        [
            _RetryableGatewayError("bad gateway"),
            SimpleNamespace(content='{"status":"ok"}'),
        ]
    )
    calls = []
    sleeps = []
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", sleeps.append)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    model._transport_max_retries = 1

    def invoke_cli(prompt):
        calls.append(prompt)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    model._invoke_cli = invoke_cli
    response = model.invoke([{"role": "user", "content": "Plan."}])

    assert response.content == '{"status":"ok"}'
    assert len(calls) == 2
    assert sleeps == [10.0]


def test_explicit_cli_timeout_preserves_cause_and_retries_after_ten_seconds(
    monkeypatch,
):
    calls = []
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text('{"status":"ok"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", sleeps.append)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        timeout=12,
    )
    model._transport_max_retries = 1

    response = model.invoke([{"role": "user", "content": "Plan."}])

    assert response.content == '{"status":"ok"}'
    assert len(calls) == 2
    assert sleeps == [10.0]


@pytest.mark.parametrize(
    ("failure_stream", "failure_text"),
    [
        (
            "stderr",
            "request failed: unexpected status 524 from the upstream gateway",
        ),
        (
            "stdout",
            '{"type":"error","message":"HTTP 429 Too Many Requests"}',
        ),
    ],
)
def test_cli_nonzero_gateway_status_retries_once(
    monkeypatch,
    failure_stream,
    failure_text,
):
    calls = []
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout=failure_text if failure_stream == "stdout" else "",
                stderr=failure_text if failure_stream == "stderr" else "",
            )
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text('{"status":"ok"}', encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", sleeps.append)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    model._transport_max_retries = 1

    response = model.invoke([{"role": "user", "content": "Plan."}])

    assert response.content == '{"status":"ok"}'
    assert len(calls) == 2
    assert sleeps == [10.0]


def test_cli_nonzero_401_is_not_retried_and_raw_output_is_redacted(
    monkeypatch,
    caplog,
):
    secret = "provider-secret-response-body"
    calls = []
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=f'{{"error":"HTTP status 401 {secret}"}}',
            stderr=f"authorization rejected: {secret}",
        )

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", sleeps.append)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    model._transport_max_retries = 3

    with pytest.raises(RuntimeError) as exc_info:
        model.invoke([{"role": "user", "content": "Plan."}])

    diagnostic = str(exc_info.value)
    assert len(calls) == 1
    assert sleeps == []
    assert secret not in diagnostic
    assert secret not in caplog.text
    assert "stdout_len=" in diagnostic
    assert "stderr_len=" in diagnostic
    assert diagnostic.count("sha256=") == 2


def test_cli_retryable_failure_exhaustion_does_not_leak_raw_output(
    monkeypatch,
    caplog,
):
    secret = "sensitive-upstream-response"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout=f"request failed: error 524 {secret}",
            stderr=f"upstream body: {secret}",
        )

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    model._transport_max_retries = 0

    with pytest.raises(RuntimeError) as exc_info:
        model.invoke([{"role": "user", "content": "Plan."}])

    assert secret not in str(exc_info.value)
    assert secret not in caplog.text
    assert "stdout_len=" in str(exc_info.value)
    assert "stderr_len=" in str(exc_info.value)


def test_cli_timing_records_each_subprocess_retry_attempt(monkeypatch, tmp_path):
    timing_path = tmp_path / "llm-timing.jsonl"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="unexpected status 524 from upstream",
            )
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text('{"status":"ok"}', encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setenv("CHEM_LLM_TIMING_JSONL", str(timing_path))
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", lambda _: None)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    model._transport_max_retries = 1

    response = model.invoke([{"role": "user", "content": "Plan."}])

    events = [
        json.loads(line)
        for line in timing_path.read_text(encoding="utf-8").splitlines()
    ]
    request_events = [
        event
        for event in events
        if event.get("transport") == "device_codex_cli"
    ]
    completed_events = [
        event
        for event in request_events
        if event["status"] in {"failed", "success"}
    ]
    assert response.content == '{"status":"ok"}'
    assert len(calls) == 2
    assert [event["status"] for event in completed_events] == ["failed", "success"]
    assert completed_events[0]["error_type"] == "RetryableGatewayError"
    assert completed_events[0]["counted_llm_seconds"] == 0.0


@pytest.mark.parametrize(
    "subprocess_error",
    [
        subprocess.TimeoutExpired(["codex"], timeout=12),
        OSError("cannot launch"),
    ],
)
def test_cli_subprocess_wrapper_preserves_cause(monkeypatch, subprocess_error):
    def fake_run(cmd, **kwargs):
        raise subprocess_error

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    model._transport_max_retries = 0

    with pytest.raises(RuntimeError) as exc_info:
        model.invoke([{"role": "user", "content": "Plan."}])

    assert exc_info.value.__cause__ is subprocess_error


def test_chat_gateway_failure_does_not_fall_through_to_plain_payload(monkeypatch):
    calls = []

    def create(**payload):
        calls.append(payload)
        raise _RetryableGatewayError("bad gateway")

    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "1")
    monkeypatch.setattr("agent_skills.llm_retry.time.sleep", lambda seconds: None)
    model = OpenAICompatChatModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        timeout=12,
        temperature=0,
    )
    model._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    # Provider-controlled exception text is redacted after the configured
    # retry budget is exhausted.
    with pytest.raises(RetryableGatewayError):
        model.invoke([{"role": "user", "content": "Plan."}])

    assert len(calls) == 2
    assert all("extra_body" in payload for payload in calls)


@pytest.mark.parametrize(
    "network_error",
    [ConnectionError("offline"), TimeoutError("timed out")],
)
def test_chat_network_failure_keeps_compat_payload(monkeypatch, network_error):
    calls = []

    def create(**payload):
        calls.append(payload)
        raise network_error

    monkeypatch.setenv("REFINER_LLM_MAX_RETRIES", "0")
    model = OpenAICompatChatModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
        timeout=12,
        temperature=0,
    )
    model._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    # Connection exception messages can contain provider URLs or response
    # fragments, so the public boundary returns the sanitized retryable type.
    with pytest.raises(RetryableGatewayError):
        model.invoke([{"role": "user", "content": "Plan."}])

    assert len(calls) == 1
    assert "extra_body" in calls[0]


def test_chat_gateway_error_does_not_leak_provider_body_or_url(
    monkeypatch, caplog,
):
    class PrivateForbidden(RuntimeError):
        status_code = 403
        body = {
            "error": {
                "code": "access_terminated_error",
                "message": "PRIVATE_PROMPT PRIVATE_KEY",
            }
        }

        def __str__(self):
            return "PRIVATE_PROMPT https://provider.invalid/?key=PRIVATE_KEY"

    def fail(**kwargs):
        raise PrivateForbidden()

    model = OpenAICompatChatModel(
        model="gpt-5.6-sol",
        api_key="unit-test-key",
        base_url="https://provider.invalid",
        timeout=12,
        temperature=0,
    )
    model._transport_max_retries = 0
    model._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail))
    )
    with caplog.at_level(logging.WARNING), pytest.raises(Exception) as captured:
        model.invoke([{"role": "user", "content": "test"}])

    assert "PRIVATE" not in str(captured.value)
    assert "PRIVATE" not in caplog.text
    assert getattr(captured.value, "status_code", None) == 403
    assert is_terminal_gateway_error(captured.value)


def test_model_pool_does_not_replay_backend_transport_budget(monkeypatch):
    class FailingBackend:
        name = "only"
        handles_transport_retries = True

        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            raise _RetryableGatewayError("exhausted")

    backend = FailingBackend()
    sleeps = []
    monkeypatch.setattr("utils.llm_factory.time.sleep", sleeps.append)
    pool = ModelPoolChatModel([backend], max_rounds=8)

    with pytest.raises(RuntimeError, match="after 1 pool round"):
        pool.invoke([{"role": "user", "content": "Plan."}])

    assert backend.calls == 1
    assert sleeps == []


def test_response_and_chat_models_normalize_host_only_base_url():
    response_model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://api.aigateway.qzz.io",
        client=SimpleNamespace(responses=_FakeResponses()),
    )
    assert response_model.base_url == "https://api.aigateway.qzz.io/v1"

    chat_model = OpenAICompatChatModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://api.aigateway.qzz.io",
        timeout=12,
        temperature=0,
    )
    assert chat_model.base_url == "https://api.aigateway.qzz.io/v1"


def test_json_object_direct_transport_keeps_plain_responses_payload(monkeypatch):
    responses = _FakeResponses()
    client = SimpleNamespace(responses=responses)
    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "direct")
    monkeypatch.setenv("REFINER_RESPONSES_CLI_FALLBACK", "0")
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="unit-test-secret",
        base_url="https://provider.invalid",
        client=client,
    )

    response = model.invoke_json_object(
        [{"role": "user", "content": "Return one JSON object."}]
    )

    assert response.content == '{"status":"ok"}'
    payload = responses.payloads[0]
    assert "text" not in payload
    assert "response_format" not in payload
    assert "tools" not in payload


def test_cli_transport_is_stateless_and_isolated(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        config_text = (Path(kwargs["cwd"]) / "config.toml").read_text(
            encoding="utf-8"
        )
        captured.update({"cmd": cmd, "config_text": config_text, **kwargs})
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text('{"status":"ok"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://provider.invalid",
    )
    response = model.invoke([{"role": "user", "content": "Return JSON."}])

    assert response.content == '{"status":"ok"}'
    assert "--ephemeral" in captured["cmd"]
    assert "--output-schema" not in captured["cmd"]
    sandbox_index = captured["cmd"].index("--sandbox")
    assert captured["cmd"][sandbox_index + 1] == "read-only"
    assert "Do not call tools" in captured["input"]
    assert "Return JSON." in captured["input"]
    assert Path(captured["cwd"]).name.startswith("device-codex-")
    assert "request_max_retries = 0" in captured["config_text"]
    assert "stream_max_retries = 0" in captured["config_text"]


def test_cli_json_object_uses_envelope_unwraps_and_cleans_temp(monkeypatch):
    captured = {}
    inner_payload = '{"status":"ok","items":[1,2]}'
    test_key = "unit-test-secret"

    def fake_run(cmd, **kwargs):
        tmp_path = Path(kwargs["cwd"])
        schema_path = Path(cmd[cmd.index("--output-schema") + 1])
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        captured.update(
            {
                "cmd": list(cmd),
                "input": kwargs["input"],
                "tmp_path": tmp_path,
                "schema_path": schema_path,
                "schema": schema,
                "schema_mode": schema_path.stat().st_mode & 0o777,
                "auth_mode": (tmp_path / "auth.json").stat().st_mode & 0o777,
                "env": dict(kwargs["env"]),
            }
        )
        output_path.write_text(
            json.dumps({"payload_json": inner_payload}),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key=test_key,
        base_url="https://provider.invalid",
    )

    response = model.invoke_json_object(
        [{"role": "user", "content": "Return the Device decision."}]
    )

    assert response.content == inner_payload
    assert "--json" in captured["cmd"]
    assert "--output-schema" in captured["cmd"]
    assert captured["schema"] == {
        "type": "object",
        "properties": {
            "payload_json": {
                "type": "string",
                "description": (
                    "The exact requested Device response serialized as one JSON "
                    "object string, with no Markdown or surrounding prose."
                ),
            }
        },
        "required": ["payload_json"],
        "additionalProperties": False,
    }
    if os.name != "nt":
        assert captured["schema_mode"] == 0o600
        assert captured["auth_mode"] == 0o600
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "REFINER_LLM_API_KEY" not in captured["env"]
    assert test_key not in " ".join(captured["cmd"])
    assert test_key not in captured["input"]
    assert test_key not in json.dumps(captured["schema"])
    assert not captured["tmp_path"].exists()
    assert not captured["schema_path"].exists()


def test_cli_json_object_schema_rejection_falls_back_once_and_caches(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        has_schema = "--output-schema" in cmd
        calls.append(has_schema)
        if has_schema:
            return SimpleNamespace(
                returncode=2,
                stdout="",
                stderr="error: unexpected argument '--output-schema'",
            )
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text('{"status":"ok"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="unit-test-secret",
        base_url="https://provider.invalid",
    )

    first = model.invoke_json_object([{"role": "user", "content": "JSON."}])
    second = model.invoke_json_object([{"role": "user", "content": "JSON."}])

    assert first.content == '{"status":"ok"}'
    assert second.content == '{"status":"ok"}'
    assert calls == [True, False, False]


def test_cli_json_object_nonconforming_envelope_is_not_unwrapped(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        has_schema = "--output-schema" in cmd
        calls.append(has_schema)
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        if has_schema:
            output_path.write_text('{"payload_json":7}', encoding="utf-8")
        else:
            output_path.write_text('{"status":"fallback"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="unit-test-secret",
        base_url="https://provider.invalid",
    )

    response = model.invoke_json_object([{"role": "user", "content": "JSON."}])

    assert response.content == '{"status":"fallback"}'
    assert calls == [True, False]


def test_cli_json_object_missing_schema_output_falls_back_once(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        has_schema = "--output-schema" in cmd
        calls.append(has_schema)
        if not has_schema:
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text('{"status":"fallback"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key="unit-test-secret",
        base_url="https://provider.invalid",
    )

    response = model.invoke_json_object([{"role": "user", "content": "JSON."}])

    assert response.content == '{"status":"fallback"}'
    assert calls == [True, False]


def test_cli_json_object_error_is_bounded_redacted_and_cleans_temp(monkeypatch):
    test_key = "unit-test-secret"
    calls = []
    temp_paths = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        temp_paths.append(Path(kwargs["cwd"]))
        return SimpleNamespace(
            returncode=1,
            stdout=f"provider echoed {test_key}",
            stderr=f"authorization failed for {test_key}",
        )

    monkeypatch.setenv("REFINER_RESPONSES_TRANSPORT", "cli")
    monkeypatch.setattr("utils.llm_factory.subprocess.run", fake_run)
    model = CodexResponsesModel(
        model="gpt-5.6-sol",
        api_key=test_key,
        base_url="https://provider.invalid",
    )

    with pytest.raises(RuntimeError) as exc_info:
        model.invoke_json_object([{"role": "user", "content": "JSON."}])

    assert len(calls) == 1
    assert test_key not in str(exc_info.value)
    assert "stdout_len=" in str(exc_info.value)
    assert "stderr_len=" in str(exc_info.value)
    assert test_key not in " ".join(calls[0])
    assert all(not path.exists() for path in temp_paths)


if __name__ == "__main__":
    test_direct_responses_transport_has_no_agent_tools()
    print("device Responses transport tests passed")
