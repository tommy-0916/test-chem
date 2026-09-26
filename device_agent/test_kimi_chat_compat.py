"""Offline checks for official Kimi K3 Device chat payloads."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.llm_factory import OpenAICompatChatModel


class DeviceKimiChatCompatibilityTests(unittest.TestCase):
    def build_backend(self, base_url: str, model: str) -> OpenAICompatChatModel:
        with patch("utils.llm_factory.OpenAI"):
            return OpenAICompatChatModel(
                model=model,
                api_key="fake",
                base_url=base_url,
                timeout=30,
                temperature=0,
            )

    def test_official_k3_omits_fixed_and_unsupported_fields(self) -> None:
        cases = [
            ("https://api.moonshot.ai/v1", "kimi-k3"),
            ("https://api.kimi.ai/coding/v1", "k3"),
            ("https://api.kimi.com/coding/v1", "k3-256k"),
        ]
        for base_url, model in cases:
            with self.subTest(base_url=base_url, model=model):
                backend = self.build_backend(base_url, model)
                variants = backend._payload_variants([{"role": "user", "content": "test"}])
                self.assertEqual(len(variants), 1)
                payload = variants[0][1]
                self.assertNotIn("temperature", payload)
                self.assertNotIn("extra_body", payload)

                native = Mock()
                with patch("agent_skills.native_tools.make_native_openai_model", return_value=native) as make:
                    backend.bind_tools([])
                self.assertIsNone(make.call_args.kwargs["temperature"])
                self.assertFalse(make.call_args.kwargs["use_responses_api"])

    def test_other_chat_backend_preserves_temperature_and_compat_body(self) -> None:
        backend = self.build_backend("https://example.test/v1", "k3")
        variants = backend._payload_variants([{"role": "user", "content": "test"}])
        self.assertEqual(variants[0][1]["temperature"], 0)
        self.assertEqual(variants[0][1]["extra_body"], {
            "thinking": {"type": "disabled"},
            "do_sample": False,
        })


if __name__ == "__main__":
    unittest.main()
