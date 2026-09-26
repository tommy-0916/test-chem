"""Offline checks for official Kimi K3 Chat Completions configuration."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from types import ModuleType
from unittest.mock import Mock, patch

from reaserch_agent.utils.llm_factory import LLMFactory


class ResearchKimiChatCompatibilityTests(unittest.TestCase):
    def fake_langchain(self) -> ModuleType:
        module = ModuleType("langchain_openai")
        module.ChatOpenAI = Mock(return_value=SimpleNamespace())
        return module

    def test_official_k3_omits_temperature_without_changing_retry_owner(self) -> None:
        cases = [
            ("https://api.moonshot.ai/v1", "kimi-k3"),
            ("https://api.kimi.ai/coding/v1", "k3"),
            ("https://api.kimi.com/coding/v1", "k3-256k"),
        ]
        module = self.fake_langchain()
        with patch.dict(sys.modules, {"langchain_openai": module}):
            for base_url, model in cases:
                with self.subTest(base_url=base_url, model=model):
                    LLMFactory._create_openai_compatible_model(
                        provider_model=model,
                        provider_key="fake",
                        provider_url=base_url,
                        temperature=0.0,
                    )
                    options = module.ChatOpenAI.call_args.kwargs
                    self.assertNotIn("temperature", options)
                    self.assertEqual(options["max_retries"], 0)
                    self.assertFalse(options["use_responses_api"])

    def test_other_chat_backends_keep_temperature(self) -> None:
        cases = [
            ("https://example.test/v1", "k3"),
            ("https://api.kimi.ai.example.test/coding/v1", "k3"),
            ("https://api.kimi.ai/coding/v1", "other-model"),
        ]
        module = self.fake_langchain()
        with patch.dict(sys.modules, {"langchain_openai": module}):
            for base_url, model in cases:
                with self.subTest(base_url=base_url, model=model):
                    LLMFactory._create_openai_compatible_model(
                        provider_model=model,
                        provider_key="fake",
                        provider_url=base_url,
                        temperature=0.0,
                    )
                    self.assertEqual(module.ChatOpenAI.call_args.kwargs["temperature"], 0.0)


if __name__ == "__main__":
    unittest.main()
