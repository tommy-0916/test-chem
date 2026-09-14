"""Retry-ownership tests for the local Kimi transport gateway."""

from __future__ import annotations

from io import BytesIO

from scripts.kimi_k3_gateway import GatewayHandler, MAX_UPSTREAM_ATTEMPTS


def test_gateway_forwards_retryable_failure_after_one_upstream_attempt(monkeypatch):
    handler = object.__new__(GatewayHandler)
    handler.path = "/v1/chat/completions"
    handler.requestline = "POST /v1/chat/completions HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.headers = {"Content-Length": "2", "Authorization": "Bearer private"}
    handler.rfile = BytesIO(b"{}")
    handler.wfile = BytesIO()
    calls = []

    def upstream_once(body, authorization):
        calls.append((body, authorization))
        return 524, b'{"error":{"message":"temporary"}}', "60"

    def unexpected_sleep(_delay):
        raise AssertionError("proxy must not sleep")

    monkeypatch.setattr(handler, "_upstream_once", upstream_once)
    monkeypatch.setattr(
        "scripts.kimi_k3_gateway.time.sleep",
        unexpected_sleep,
    )

    handler.do_POST()

    assert MAX_UPSTREAM_ATTEMPTS == 1
    assert len(calls) == 1
    assert b"524" in handler.wfile.getvalue().split(b"\r\n", 1)[0]
