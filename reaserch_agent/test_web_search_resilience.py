"""Focused zero-network tests for resilient general web search."""

from __future__ import annotations

import unittest
from unittest import mock

from reaserch_agent.tools.web_search import WebSearchClient


DDG_HTML = """
<a class="result__a" href="https://example.org/ddg">Fallback result</a>
<a class="result__snippet">DuckDuckGo fallback content</a>
"""


class RotatingTavilyClient(WebSearchClient):
    def __init__(self, responses, **kwargs) -> None:
        super().__init__(**kwargs)
        self.responses = dict(responses)
        self.used_keys = []

    def _post_json(self, url, payload, *, headers=None):
        api_key = payload["api_key"]
        self.used_keys.append(api_key)
        response = self.responses[api_key]
        if isinstance(response, Exception):
            raise response
        return response

    def _get_text(self, url, *, headers=None):
        if "duckduckgo.com/html" in url:
            return DDG_HTML
        raise AssertionError(f"unexpected GET {url}")


class WebSearchResilienceTest(unittest.TestCase):
    def test_env_keys_rotate_until_tavily_succeeds(self) -> None:
        responses = {
            "first-key": RuntimeError("quota exhausted for first-key"),
            "second-key": {
                "results": [
                    {
                        "title": "Working result",
                        "url": "https://example.org/paper",
                        "content": "result content",
                    }
                ]
            },
        }
        with mock.patch.dict(
            "os.environ",
            {"TAVILY_API_KEY": " first-key, second-key, first-key "},
            clear=False,
        ):
            client = RotatingTavilyClient(responses)

        results = client.search("electrochemical synthesis")

        self.assertEqual(client.tavily_api_keys, ("first-key", "second-key"))
        self.assertEqual(client.used_keys, ["first-key", "second-key"])
        self.assertEqual(results[0].engine, "tavily")
        self.assertEqual(
            [attempt.status for attempt in client.last_attempts],
            ["error", "success"],
        )
        self.assertEqual(
            [attempt.credential_index for attempt in client.last_attempts],
            [1, 2],
        )
        self.assertNotIn("first-key", " ".join(client.last_errors))
        self.assertIn("[REDACTED]", client.last_errors[0])

    def test_all_tavily_keys_fail_then_duckduckgo_runs(self) -> None:
        client = RotatingTavilyClient(
            {
                "key-a": ConnectionError("provider unavailable"),
                "key-b": TimeoutError("provider timeout"),
            },
            tavily_api_key="key-a,key-b",
        )

        results = client.search("nickel catalyst")

        self.assertEqual(results[0].engine, "duckduckgo")
        self.assertEqual(
            [attempt.status for attempt in client.last_attempts],
            ["error", "error", "success"],
        )
        self.assertEqual(client.last_attempts[-1].engine, "duckduckgo")

    def test_results_are_deduplicated_by_normalized_url(self) -> None:
        client = RotatingTavilyClient(
            {
                "key-a": {
                    "results": [
                        {"title": "First", "url": "https://EXAMPLE.org/item"},
                        {
                            "title": "Duplicate",
                            "url": "https://example.org/item/#section",
                        },
                        {"title": "Second", "url": "https://example.org/other"},
                        {"title": "Invalid", "url": "javascript:void(0)"},
                    ]
                }
            },
            tavily_api_key="key-a",
        )

        results = client.search("query", max_results=5)

        self.assertEqual([result.title for result in results], ["First", "Second"])
        self.assertEqual(client.last_attempts[0].result_count, 2)
        self.assertEqual(client.last_attempts[0].to_dict()["status"], "success")
        self.assertEqual(results[0].to_dict()["engine"], "tavily")

    def test_transport_uses_configured_timeout(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"response body"
        opener = mock.Mock(return_value=response)
        client = WebSearchClient(
            timeout_seconds=7,
            opener=opener,
            url_validator=lambda url: None,
        )

        body = client._get_text("https://example.org")

        self.assertEqual(body, "response body")
        self.assertEqual(opener.call_args.kwargs["timeout"], 7)


if __name__ == "__main__":
    unittest.main()
