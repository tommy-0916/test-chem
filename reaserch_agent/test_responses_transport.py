"""Tests for Research Agent's tool-free direct Responses transport."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reaserch_agent.utils.llm_factory import (
    CodexResponsesModel,
    configured_max_retries,
    normalize_openai_base_url,
)


class _FakeResponses:
    def __init__(self) -> None:
        self.payloads = []

    def create(self, **payload):
        self.payloads.append(payload)
        return {
            "status": "completed",
            "output": [
                {"content": [{"type": "output_text", "text": '{"ok":true}'}]}
            ],
        }


class DirectResponsesTransportTest(unittest.TestCase):
    def test_transport_clients_use_eight_retries(self) -> None:
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "8"}):
            model = CodexResponsesModel(
                model="gpt-5.6-sol",
                api_key="test-key",
                base_url="https://provider.invalid",
            )
            self.assertEqual(configured_max_retries(), 8)
            self.assertEqual(model._client.max_retries, 8)

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
        self.assertNotIn("tools", payload)

    def test_cli_transport_is_stateless_and_isolated(self) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            codex_home = Path(kwargs["env"]["CODEX_HOME"])
            captured.update(
                {
                    "cmd": cmd,
                    "config_mode": (codex_home / "config.toml").stat().st_mode
                    & 0o777,
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
        self.assertEqual(captured["config_mode"], 0o600)
        self.assertEqual(captured["auth_mode"], 0o600)
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("REFINER_LLM_API_KEY", captured["env"])


if __name__ == "__main__":
    unittest.main()
