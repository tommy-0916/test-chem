"""Zero-network tests for SSRF and redirect protections."""

from __future__ import annotations

import socket
import unittest
import urllib.request
from unittest import mock

from reaserch_agent.tools.network_safety import (
    SafeRedirectHandler,
    UnsafeRemoteUrl,
    validate_public_http_url,
)
from reaserch_agent.tools.web_search import WebSearchClient


def _resolver_for(address: str):
    def resolve(host: str, port: int, *, type: int):
        return [(socket.AF_INET, type, 6, "", (address, port))]

    return resolve


class NetworkSafetyTest(unittest.TestCase):
    def test_rejects_literal_and_dns_resolved_private_addresses(self) -> None:
        for url in (
            "http://127.0.0.1/secret",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/secret",
            "http://localhost/secret",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeRemoteUrl):
                validate_public_http_url(url)

        with self.assertRaises(UnsafeRemoteUrl):
            validate_public_http_url(
                "https://metadata.example/secret",
                resolver=_resolver_for("10.0.0.8"),
            )

    def test_accepts_hostname_when_all_addresses_are_public(self) -> None:
        validate_public_http_url(
            "https://papers.example/article",
            resolver=_resolver_for("93.184.216.34"),
        )

    def test_web_page_fetch_rejects_private_url_before_transport(self) -> None:
        opener = mock.Mock(side_effect=AssertionError("transport must not run"))
        client = WebSearchClient(opener=opener)

        self.assertEqual(client.fetch_page("http://127.0.0.1/secret"), "")
        self.assertFalse(opener.called)
        self.assertIn("unsafe_url", client.last_errors[0])

    def test_redirect_handler_revalidates_destination(self) -> None:
        validator = mock.Mock(side_effect=UnsafeRemoteUrl("private redirect"))
        handler = SafeRedirectHandler(validator)
        request = urllib.request.Request("https://public.example/start")

        with self.assertRaises(UnsafeRemoteUrl):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1/secret",
            )

        validator.assert_called_once_with("http://127.0.0.1/secret")

    def test_cross_origin_redirect_strips_credentials(self) -> None:
        handler = SafeRedirectHandler(lambda url: None)
        request = urllib.request.Request(
            "https://source.example/start",
            headers={
                "Authorization": "Bearer secret",
                "X-API-KEY": "secret-key",
                "Cookie": "session=secret",
                "User-Agent": "chemagent-test",
            },
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example/end",
        )

        headers = {name.lower(): value for name, value in redirected.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("x-api-key", headers)
        self.assertNotIn("cookie", headers)
        self.assertEqual(headers["user-agent"], "chemagent-test")

    def test_same_origin_redirect_keeps_credentials(self) -> None:
        handler = SafeRedirectHandler(lambda url: None)
        request = urllib.request.Request(
            "https://source.example/start",
            headers={"Authorization": "Bearer secret"},
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://source.example/next",
        )

        headers = {name.lower(): value for name, value in redirected.header_items()}
        self.assertEqual(headers["authorization"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
