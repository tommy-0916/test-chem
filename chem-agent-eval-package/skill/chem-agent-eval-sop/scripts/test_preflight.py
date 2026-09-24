from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts.preflight import api_endpoint, probe_api


class FakeResponse:
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "chem-agent-eval-ready"}
                        ],
                    }
                ]
            }
        ).encode("utf-8")


class PreflightTest(unittest.TestCase):
    @patch("scripts.preflight.urllib.request.urlopen")
    def test_responses_probe_requires_nonempty_real_text(self, urlopen: object) -> None:
        urlopen.return_value = FakeResponse()
        result = probe_api(
            endpoint="https://provider.invalid/v1",
            api_key="secret-value",
            model="test-model",
            wire_api="codex_responses",
            reasoning_effort="xhigh",
            timeout=5,
        )
        request = urlopen.call_args.args[0]
        self.assertTrue(result["response_nonempty"])
        self.assertEqual(request.full_url, "https://provider.invalid/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
        self.assertNotIn("secret-value", json.dumps(result))

    def test_endpoint_does_not_duplicate_suffix(self) -> None:
        self.assertEqual(
            api_endpoint("https://provider.invalid/v1/responses", "codex_responses"),
            "https://provider.invalid/v1/responses",
        )


if __name__ == "__main__":
    unittest.main()
