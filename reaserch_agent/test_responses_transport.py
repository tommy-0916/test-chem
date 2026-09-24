"""Tests for Research Agent's tool-free direct Responses transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_skills.llm_retry import call_with_gateway_retry
from agent_skills.responses_stream import ResponsesProtocolError, ResponsesTerminalError
from reaserch_agent.utils.llm_factory import (
    CodexResponsesModel,
    LLMFactory,
    configured_max_retries,
    normalize_openai_base_url,
)


class _FakeResponses:
    def __init__(self) -> None:
        self.payloads = []

    def create(self, **payload):
        self.payloads.append(payload)
        response = {
            "status": "completed",
            "output": [
                {"content": [{"type": "output_text", "text": '{"ok":true}'}]}
            ],
        }
        if payload.get("stream"):
            self.stream = _FakeStream(
                [{"type": "response.completed", "response": response}]
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


class DirectResponsesTransportTest(unittest.TestCase):
    def test_direct_responses_streaming_aggregates_text(self) -> None:
        final = SimpleNamespace(status="completed", output_text='{"ok":true}')
        stream = _FakeStream([
            SimpleNamespace(type="response.output_text.delta", delta='{"ok":'),
            SimpleNamespace(type="response.output_text.delta", delta="true}"),
            SimpleNamespace(type="response.completed", response=final),
        ])
        responses = SimpleNamespace(create=Mock(return_value=stream))
        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=responses),
        )

        with patch.dict(
            os.environ,
            {
                "REFINER_RESPONSES_TRANSPORT": "direct",
                "REFINER_RESPONSES_STREAM": "1",
                "REFINER_RESPONSES_CLI_FALLBACK": "0",
            },
        ):
            response = model.invoke([{"role": "user", "content": "Plan."}])

        self.assertEqual(response.content, '{"ok":true}')
        self.assertIs(response.raw_response, final)
        self.assertTrue(stream.closed)
        self.assertIs(responses.create.call_args.kwargs["stream"], True)

    def test_truncated_stream_fails_closed_without_replay(self) -> None:
        truncated = _FakeStream([
            SimpleNamespace(type="response.output_text.delta", delta="partial")
        ])
        responses = SimpleNamespace(create=Mock(return_value=truncated))
        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=responses),
        )
        model._transport_max_retries = 1

        with patch.dict(
            os.environ,
            {
                "REFINER_RESPONSES_TRANSPORT": "direct",
                "REFINER_RESPONSES_STREAM": "1",
                "REFINER_RESPONSES_CLI_FALLBACK": "1",
            },
        ), patch("agent_skills.llm_retry.time.sleep") as sleep, patch.object(
            model, "_invoke_cli"
        ) as cli:
            with self.assertRaises(ResponsesProtocolError):
                model.invoke([{"role": "user", "content": "Plan."}])

        self.assertEqual(responses.create.call_count, 1)
        self.assertTrue(truncated.closed)
        sleep.assert_not_called()
        cli.assert_not_called()

    def test_adapter_owns_configured_retries_and_sdk_retries_are_disabled(self) -> None:
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "8"}):
            model = CodexResponsesModel(
                model="gpt-5.6-sol",
                api_key="test-key",
                base_url="https://provider.invalid",
            )
            self.assertEqual(configured_max_retries(), 8)
            self.assertEqual(model._transport_max_retries, 8)
            self.assertEqual(model._client.max_retries, 0)

    def test_retryable_gateway_failure_retries_after_ten_seconds(self) -> None:
        responses = _FakeResponses()
        original_create = responses.create
        responses.create = Mock(
            side_effect=[_RetryableGatewayError(), original_create(stream=True)]
        )
        client = SimpleNamespace(responses=responses)

        with patch.dict(
            os.environ,
            {
                "REFINER_LLM_MAX_RETRIES": "1",
                "REFINER_RESPONSES_TRANSPORT": "direct",
                "REFINER_RESPONSES_CLI_FALLBACK": "0",
            },
        ), patch("agent_skills.llm_retry.time.sleep") as sleep:
            model = CodexResponsesModel(
                model="gpt-5.6-sol",
                api_key="test-key",
                base_url="https://provider.invalid",
                client=client,
            )
            response = model.invoke([{"role": "user", "content": "Plan."}])

        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(responses.create.call_count, 2)
        sleep.assert_called_once_with(10.0)

    def test_retryable_direct_failure_never_replays_through_cli(self) -> None:
        responses = SimpleNamespace(create=Mock(side_effect=_RetryableGatewayError()))
        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=responses),
        )
        with patch.dict(
            os.environ,
            {
                "REFINER_LLM_MAX_RETRIES": "0",
                "REFINER_RESPONSES_TRANSPORT": "direct",
                "REFINER_RESPONSES_CLI_FALLBACK": "1",
            },
        ), patch.object(model, "_invoke_cli") as cli:
            model._transport_max_retries = 0
            with self.assertRaises(_RetryableGatewayError):
                model.invoke([{"role": "user", "content": "Plan."}])
        cli.assert_not_called()

    def test_explicit_cli_transport_uses_fixed_retry(self) -> None:
        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://provider.invalid",
        )
        model._transport_max_retries = 1
        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "cli"}), patch.object(
            model,
            "_invoke_cli",
            side_effect=[
                _RetryableGatewayError(),
                SimpleNamespace(content='{"ok":true}'),
            ],
        ) as cli, patch("agent_skills.llm_retry.time.sleep") as sleep:
            response = model.invoke([{"role": "user", "content": "Plan."}])
        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(cli.call_count, 2)
        sleep.assert_called_once_with(10.0)

    def test_real_cli_524_exit_retries_and_401_does_not_leak(self) -> None:
        calls = []

        def retry_then_succeed(cmd, **_kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="gateway returned 524 private-body",
                )
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text('{"ok":true}', encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://provider.invalid",
        )
        model._transport_max_retries = 1
        with patch.dict(
            os.environ, {"REFINER_RESPONSES_TRANSPORT": "cli"}
        ), patch(
            "reaserch_agent.utils.llm_factory.subprocess.run",
            side_effect=retry_then_succeed,
        ), patch("agent_skills.llm_retry.time.sleep") as sleep:
            response = model.invoke([{"role": "user", "content": "Plan."}])

        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(10.0)

        secret = "private-provider-response"
        calls.clear()
        with patch.dict(
            os.environ, {"REFINER_RESPONSES_TRANSPORT": "cli"}
        ), patch(
            "reaserch_agent.utils.llm_factory.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["codex"],
                1,
                stdout=f"HTTP status 401 {secret}",
                stderr=secret,
            ),
        ), patch("agent_skills.llm_retry.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as caught:
                model.invoke([{"role": "user", "content": "Plan."}])

        self.assertNotIn(secret, str(caught.exception))
        sleep.assert_not_called()

    def test_caller_cannot_reenable_openai_sdk_retries(self) -> None:
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "1"}):
            model = LLMFactory._create_openai_compatible_model(
                provider_model="test",
                provider_key="fake",
                provider_url="https://provider.invalid/v1",
                temperature=0,
                max_retries=60,
            )
        self.assertEqual(model.root_client.max_retries, 0)
        self.assertEqual(model._chem_gateway_max_retries, 1)

    def test_gemini_sdk_retries_are_disabled_and_shared_budget_is_attached(self) -> None:
        captured = {}

        class FakeGemini:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module = SimpleNamespace(ChatGoogleGenerativeAI=FakeGemini)
        with patch.dict(sys.modules, {"langchain_google_genai": fake_module}), patch.dict(
            os.environ, {"REFINER_LLM_MAX_RETRIES": "1"}
        ):
            model = LLMFactory._create_gemini_model(
                provider_model="gemini-test",
                provider_key="fake",
                provider_url=None,
                temperature=0,
                max_retries=60,
            )
        self.assertEqual(captured["max_retries"], 0)
        self.assertEqual(model._chem_gateway_max_retries, 1)

    def test_gemini_retry_after_is_replaced_by_fixed_ten_second_retry(self) -> None:
        calls = []

        class GeminiQuotaError(RuntimeError):
            code = 429
            retry_after = 59

        class FakeClient:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise GeminiQuotaError("private provider response")
                return "ok"

        class FakeGemini:
            def __init__(self, **kwargs):
                self.client = FakeClient()

        fake_module = SimpleNamespace(ChatGoogleGenerativeAI=FakeGemini)
        with patch.dict(sys.modules, {"langchain_google_genai": fake_module}), patch.dict(
            os.environ, {"REFINER_LLM_MAX_RETRIES": "1"}
        ), patch("agent_skills.llm_retry.time.sleep") as sleep:
            model = LLMFactory._create_gemini_model(
                provider_model="gemini-test",
                provider_key="fake",
                provider_url=None,
                temperature=0,
            )
            result = call_with_gateway_retry(
                lambda: model.client.generate_content(request="prompt"),
                max_retries=model._chem_gateway_max_retries,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [
            {"request": "prompt", "retry": None},
            {"request": "prompt", "retry": None},
        ])
        sleep.assert_called_once_with(10.0)

    def test_openai_base_url_adds_v1_once(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.aigateway.qzz.io"),
            "https://api.aigateway.qzz.io/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("https://api.aigateway.qzz.io/v1/"),
            "https://api.aigateway.qzz.io/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("https://provider.example/openai/v1"),
            "https://provider.example/openai/v1",
        )

    def test_response_model_normalizes_host_only_base_url(self) -> None:
        model = CodexResponsesModel(
            model="gpt-5.6-sol",
            api_key="test-key",
            base_url="https://api.aigateway.qzz.io",
            client=SimpleNamespace(responses=_FakeResponses()),
        )
        self.assertEqual(model._base_url, "https://api.aigateway.qzz.io/v1")

    def test_direct_transport_has_no_agent_tools(self) -> None:
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
                reasoning_effort="high",
                timeout=12,
                max_output_tokens=32768,
                client=client,
            )
            response = model.invoke([
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "Plan chemistry."},
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

        self.assertEqual(response.content, '{"ok":true}')
        payload = responses.payloads[0]
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["max_output_tokens"], 32768)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertTrue(responses.stream.closed)
        self.assertNotIn("tools", payload)

    def test_nonstream_requires_explicit_opt_out(self) -> None:
        responses = _FakeResponses()
        model = CodexResponsesModel(
            model="test",
            api_key="fake",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=responses),
        )
        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "0"}):
            result = model.invoke([{"role": "user", "content": "test"}])
        self.assertEqual(result.content, '{"ok":true}')
        self.assertFalse(responses.payloads[0]["stream"])

    def test_incomplete_stream_cannot_fall_back_to_cli(self) -> None:
        stream = _FakeStream([
            {"type": "response.output_text.delta", "delta": '{"partial":true}'},
            {
                "type": "response.incomplete",
                "response": {"status": "incomplete"},
            },
        ])
        responses = SimpleNamespace(create=lambda **payload: stream)
        model = CodexResponsesModel(
            model="test",
            api_key="fake",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=responses),
        )
        with patch.dict(
            os.environ, {"REFINER_RESPONSES_CLI_FALLBACK": "1"}
        ), patch.object(model, "_invoke_cli") as cli:
            with self.assertRaises(ResponsesTerminalError):
                model.invoke([{"role": "user", "content": "test"}])
        cli.assert_not_called()
        self.assertTrue(stream.closed)

    def test_stream_eof_cannot_return_partial_text(self) -> None:
        stream = _FakeStream([
            {"type": "response.output_text.delta", "delta": '{"partial":true}'}
        ])
        model = CodexResponsesModel(
            model="test",
            api_key="fake",
            base_url="https://provider.invalid",
            client=SimpleNamespace(
                responses=SimpleNamespace(create=lambda **payload: stream)
            ),
        )
        with self.assertRaises(ResponsesProtocolError):
            model.invoke([{"role": "user", "content": "test"}])
        self.assertTrue(stream.closed)

    def test_request_failure_cannot_silently_downgrade(self) -> None:
        responses = _FakeResponses()
        model = CodexResponsesModel(
            model="test",
            api_key="fake",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=responses),
        )
        model._transport_max_retries = 0
        failure = TimeoutError("fake transport timeout")
        with patch.dict(
            os.environ, {"REFINER_RESPONSES_CLI_FALLBACK": "1"}
        ), patch.object(
            responses, "create", side_effect=failure
        ) as create, patch.object(model, "_invoke_cli") as cli:
            with self.assertRaises(TimeoutError) as caught:
                model.invoke([{"role": "user", "content": "test"}])
        self.assertIs(caught.exception, failure)
        create.assert_called_once()
        self.assertTrue(create.call_args.kwargs["stream"])
        cli.assert_not_called()

    def test_terminal_403_cannot_fall_back_to_cli_in_nonstream_mode(self) -> None:
        model = CodexResponsesModel(
            model="test",
            api_key="fake",
            base_url="https://provider.invalid",
            client=SimpleNamespace(responses=_FakeResponses()),
        )
        model._transport_max_retries = 0
        failure = RuntimeError("private provider body")
        failure.status_code = 403
        with patch.dict(
            os.environ,
            {
                "REFINER_RESPONSES_STREAM": "0",
                "REFINER_RESPONSES_CLI_FALLBACK": "1",
            },
        ), patch.object(
            model, "_invoke_direct", side_effect=failure
        ) as direct, patch.object(model, "_invoke_cli") as cli:
            with self.assertRaises(RuntimeError) as caught:
                model.invoke([{"role": "user", "content": "test"}])

        self.assertIs(caught.exception, failure)
        direct.assert_called_once()
        cli.assert_not_called()

    def test_cli_transport_is_stateless_and_isolated(self) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            codex_home = Path(kwargs["env"]["CODEX_HOME"])
            captured.update(
                {
                    "cmd": cmd,
                    "config_mode": (codex_home / "config.toml").stat().st_mode
                    & 0o777,
                    "config_text": (codex_home / "config.toml").read_text(
                        encoding="utf-8"
                    ),
                    "auth_mode": (codex_home / "auth.json").stat().st_mode & 0o777,
                    **kwargs,
                }
            )
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text('{"ok":true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        previous_transport = os.environ.get("REFINER_RESPONSES_TRANSPORT")
        os.environ["REFINER_RESPONSES_TRANSPORT"] = "cli"
        try:
            model = CodexResponsesModel(
                model="gpt-5.6-sol",
                api_key="test-key",
                base_url="https://provider.invalid",
            )
            with patch(
                "reaserch_agent.utils.llm_factory.subprocess.run",
                side_effect=fake_run,
            ):
                response = model.invoke(
                    [{"role": "user", "content": "Plan chemistry."}]
                )
        finally:
            if previous_transport is None:
                os.environ.pop("REFINER_RESPONSES_TRANSPORT", None)
            else:
                os.environ["REFINER_RESPONSES_TRANSPORT"] = previous_transport

        self.assertEqual(response.content, '{"ok":true}')
        self.assertIn("--ephemeral", captured["cmd"])
        sandbox_index = captured["cmd"].index("--sandbox")
        self.assertEqual(captured["cmd"][sandbox_index + 1], "read-only")
        self.assertIn("Do not call tools", captured["input"])
        self.assertIn("Plan chemistry.", captured["input"])
        self.assertTrue(Path(captured["cwd"]).name.startswith("research-codex-"))
        if os.name != "nt":
            self.assertEqual(captured["config_mode"], 0o600)
            self.assertEqual(captured["auth_mode"], 0o600)
        self.assertIn("request_max_retries = 0", captured["config_text"])
        self.assertIn("stream_max_retries = 0", captured["config_text"])
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("REFINER_LLM_API_KEY", captured["env"])

    def test_cli_transport_records_one_timing_event_per_subprocess(self) -> None:
        def fake_run(cmd, **_kwargs):
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text('{"ok":true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            timing_path = Path(tmp) / "timing.jsonl"
            model = CodexResponsesModel(
                model="gpt-5.6-sol",
                api_key="test-key",
                base_url="https://provider.invalid",
            )
            with patch.dict(
                os.environ,
                {
                    "REFINER_RESPONSES_TRANSPORT": "cli",
                    "CHEM_LLM_TIMING_JSONL": str(timing_path),
                },
            ), patch(
                "reaserch_agent.utils.llm_factory.subprocess.run",
                side_effect=fake_run,
            ):
                model.invoke([{"role": "user", "content": "Plan chemistry."}])

            events = [
                json.loads(line)
                for line in timing_path.read_text(encoding="utf-8").splitlines()
            ]
        completed = [event for event in events if event["status"] == "success"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["component"], "research")
        self.assertEqual(completed[0]["transport"], "research_codex_cli")


if __name__ == "__main__":
    unittest.main()
