"""Tests for the tool-free direct Responses transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.llm_factory import (
    CodexResponsesModel,
    OpenAICompatChatModel,
    configured_max_retries,
    normalize_openai_base_url,
)


class _FakeResponses:
    def __init__(self):
        self.payloads = []

    def create(self, **payload):
        self.payloads.append(payload)
        return SimpleNamespace(
            output_text='{"status":"ok"}',
            status="completed",
        )


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
    assert "tools" not in payload
    assert "Return JSON." in payload["input"]


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


def test_transport_clients_use_eight_retries(monkeypatch):
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
    assert response_model._client.max_retries == 8
    assert chat_model._client.max_retries == 8


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
        captured.update({"cmd": cmd, **kwargs})
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
