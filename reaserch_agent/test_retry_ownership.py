"""Offline regression tests for Research retry ownership."""

from __future__ import annotations

import os
import sys
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_skills.llm_retry import RetryableGatewayError, call_with_gateway_retry
from reaserch_agent.core import BaseAgent
from reaserch_agent.utils.llm_factory import LLMFactory, _SingleAttemptGeminiClient


class _CountingModel:
    def __init__(self, side_effects, *, owns_transport: bool = False) -> None:
        self.side_effects = list(side_effects)
        self.calls = 0
        self.messages = []
        self.handles_transport_retries = owns_transport

    def invoke(self, messages):
        self.calls += 1
        self.messages.append(messages)
        value = self.side_effects.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(content=value)


class _InternallyRetriedModel:
    handles_transport_retries = True

    def __init__(self) -> None:
        self.logical_calls = 0
        self.wire_calls = 0

    def invoke(self, messages):
        self.logical_calls += 1

        def wire_call():
            self.wire_calls += 1
            raise RetryableGatewayError("sanitized upstream error")

        return call_with_gateway_retry(
            wire_call,
            max_retries=8,
            wall_timeout_seconds=30,
            sleep=lambda _seconds: None,
        )


class ResearchRetryOwnershipTests(unittest.TestCase):
    def test_403_is_one_wire_call_even_with_eight_retry_budget(self) -> None:
        error = RuntimeError("sanitized")
        error.status_code = 403
        model = _CountingModel([error])
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "8"}), patch(
            "agent_skills.llm_retry.time.sleep"
        ) as sleep:
            agent = BaseAgent(model=model)
            with self.assertRaises(RuntimeError):
                agent.invoke_text("system", "task")

        self.assertEqual(model.calls, 1)
        sleep.assert_not_called()

    def test_chat_429_insufficient_quota_is_one_base_agent_call(self) -> None:
        error = RuntimeError(
            "canary-body sk-secret https://gateway.invalid/private?token=canary"
        )
        error.status_code = 429
        error.body = {
            "error": {
                "type": "rate_limit_error",
                "code": "insufficient_quota",
            }
        }
        model = _CountingModel([error])
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "8"}), patch(
            "agent_skills.llm_retry.time.sleep"
        ) as sleep:
            with self.assertRaises(RuntimeError) as caught:
                BaseAgent(model=model).invoke_text("system", "task")

        self.assertEqual(model.calls, 1)
        sleep.assert_not_called()
        rendered = str(caught.exception) + "".join(
            traceback.format_exception(caught.exception)
        )
        self.assertIn("status=429", rendered)
        self.assertIn("code=insufficient_quota", rendered)
        self.assertNotIn("canary-body", rendered)
        self.assertNotIn("sk-secret", rendered)
        self.assertNotIn("gateway.invalid", rendered)
        self.assertIs(caught.exception.__context__, error)

    def test_model_owned_transient_retry_is_not_multiplied_by_base_agent(self) -> None:
        model = _InternallyRetriedModel()
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "8"}):
            agent = BaseAgent(model=model)
            with self.assertRaises(RuntimeError):
                agent.invoke_text("system", "task")

        self.assertEqual(model.logical_calls, 1)
        self.assertEqual(model.wire_calls, 9)

    def test_json_format_repair_is_separate_from_transport_retry(self) -> None:
        model = _CountingModel(["not-json", '{"ok": true}'])
        with patch.dict(os.environ, {
            "REFINER_LLM_MAX_RETRIES": "8",
            "RESEARCH_JSON_FORMAT_MAX_RETRIES": "2",
        }):
            result = BaseAgent(model=model).invoke_json("system", "task")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(model.calls, 2)
        repair_message = model.messages[1][-1]
        repair_content = (
            repair_message["content"]
            if isinstance(repair_message, dict)
            else repair_message.content
        )
        self.assertIn("Serialization repair only", repair_content)

    def test_chat_factory_forces_sdk_retry_zero_and_records_shared_budget(self) -> None:
        captured = {}

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch.dict(os.environ, {
            "REFINER_LLM_MAX_RETRIES": "8",
            "REFINER_LLM_TIMEOUT_SECONDS": "60",
        }), patch("langchain_openai.ChatOpenAI", FakeChatOpenAI):
            model = LLMFactory._create_openai_compatible_model(
                provider_model="offline",
                provider_key="fake",
                provider_url="https://provider.invalid/v1",
                temperature=0.0,
                max_retries=99,
            )

        self.assertEqual(captured["max_retries"], 0)
        self.assertEqual(model._chem_gateway_max_retries, 8)
        self.assertEqual(model._chem_wall_timeout_seconds, 60.0)

    def test_gemini_factory_disables_langchain_and_gapic_retries(self) -> None:
        captured = {}

        class RawGeminiClient:
            def generate_content(self, *args, **kwargs):
                captured["generate_kwargs"] = kwargs
                return "ok"

        class FakeGeminiModel:
            def __init__(self, **kwargs):
                captured["model_kwargs"] = kwargs
                self.client = RawGeminiClient()

        fake_module = SimpleNamespace(ChatGoogleGenerativeAI=FakeGeminiModel)
        with patch.dict(os.environ, {"REFINER_LLM_MAX_RETRIES": "8"}), patch.dict(
            sys.modules, {"langchain_google_genai": fake_module}
        ):
            model = LLMFactory._create_gemini_model(
                provider_model="gemini-offline",
                provider_key="fake",
                provider_url=None,
                temperature=0.0,
                max_retries=99,
            )

        self.assertEqual(captured["model_kwargs"]["max_retries"], 0)
        self.assertEqual(model._chem_gateway_max_retries, 8)
        self.assertIsInstance(model.client, _SingleAttemptGeminiClient)
        self.assertEqual(model.client.generate_content("offline"), "ok")
        self.assertIsNone(captured["generate_kwargs"]["retry"])


if __name__ == "__main__":
    unittest.main()
