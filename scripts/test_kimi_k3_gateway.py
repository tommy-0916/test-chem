"""Retry-ownership tests for the local Kimi transport gateway."""

from __future__ import annotations

import json
from io import BytesIO
import threading
import time
import urllib.error

import pytest

from scripts.kimi_k3_gateway import (
    GatewayHandler,
    MAX_UPSTREAM_ATTEMPTS,
    StreamAggregationError,
    UPSTREAM_WALL_TIMEOUT_SECONDS,
    _usage_log_note,
    consume_stream,
    consume_stream_bounded,
    sanitize_upstream_http_error,
)


def _sse(*payloads: str) -> BytesIO:
    return BytesIO("".join(f"data: {payload}\n\n" for payload in payloads).encode())


def test_stream_requires_explicit_done_and_terminal_choice():
    partial = json.dumps({
        "id": "chat-1",
        "choices": [{"index": 0, "delta": {"content": '{"partial":true}'}}],
    })
    with pytest.raises(StreamAggregationError, match=r"\[DONE\]"):
        consume_stream(_sse(partial))

    with pytest.raises(StreamAggregationError, match="finish_reason"):
        consume_stream(_sse(partial, "[DONE]"))


def test_stream_accepts_only_completed_choice_followed_by_done():
    delta = json.dumps({
        "id": "chat-1",
        "model": "k3",
        "choices": [{"index": 0, "delta": {"content": "{}"}}],
    })
    terminal = json.dumps({
        "id": "chat-1",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    result = consume_stream(_sse(delta, terminal, "[DONE]"))
    assert result["choices"][0]["message"]["content"] == "{}"
    assert result["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_stream_rejects_non_success_finish_reason(finish_reason):
    terminal = json.dumps({
        "id": "chat-1",
        "choices": [{
            "index": 0,
            "delta": {"content": '{"apparently":"complete"}'},
            "finish_reason": finish_reason,
        }],
    })
    with pytest.raises(StreamAggregationError, match="non-success finish_reason"):
        consume_stream(_sse(terminal, "[DONE]"))


def test_byte_active_nonterminal_stream_is_closed_at_absolute_deadline():
    class EndlessHeartbeatStream:
        def __init__(self):
            self.closed = threading.Event()

        def __iter__(self):
            while not self.closed.is_set():
                yield b": heartbeat\n"
                time.sleep(0.002)

        def close(self):
            self.closed.set()

    stream = EndlessHeartbeatStream()
    started = time.monotonic()
    with pytest.raises(StreamAggregationError, match="absolute wall deadline"):
        consume_stream_bounded(stream, 0.03)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert stream.closed.wait(0.2)


def test_header_and_stream_phases_share_one_absolute_deadline(monkeypatch):
    terminal = json.dumps({
        "id": "chat-1",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })

    class SlowResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            time.sleep(0.04)
            yield f"data: {terminal}\n\n".encode()
            yield b"data: [DONE]\n\n"

        def close(self):
            return None

    def delayed_headers(*_args, **_kwargs):
        time.sleep(0.04)
        return SlowResponse()

    monkeypatch.setattr(
        "scripts.kimi_k3_gateway.UPSTREAM_WALL_TIMEOUT_SECONDS", 0.06
    )
    monkeypatch.setattr(
        "scripts.kimi_k3_gateway.urllib.request.urlopen", delayed_headers
    )
    handler = object.__new__(GatewayHandler)
    started = time.monotonic()

    status, raw, _ = handler._upstream_once(b"{}", None)

    assert status == 502
    assert time.monotonic() - started < 0.1
    assert json.loads(raw)["error"]["code"] == "gateway_upstream_error"


def test_default_absolute_deadline_supports_device_long_generation_budget():
    assert UPSTREAM_WALL_TIMEOUT_SECONDS >= 600


@pytest.mark.parametrize(
    "frame,expected",
    [
        ("not-json PRIVATE_PROMPT", "undecodable SSE chunk"),
        (
            json.dumps({"error": {"message": "PRIVATE_PROMPT", "token": "PRIVATE_KEY"}}),
            "upstream stream error",
        ),
    ],
)
def test_stream_errors_never_echo_upstream_payload(frame, expected):
    with pytest.raises(StreamAggregationError) as captured:
        consume_stream(_sse(frame))
    assert str(captured.value) == expected
    assert "PRIVATE" not in str(captured.value)


@pytest.mark.parametrize(
    ("chunk", "expected"),
    [
        (
            {
                "created": "PRIVATE_CREATED",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            "invalid created metadata",
        ),
        (
            {
                "choices": [
                    {"index": "PRIVATE_INDEX", "delta": {}, "finish_reason": "stop"}
                ],
            },
            "invalid choice index metadata",
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": "PRIVATE_TOOL_INDEX", "function": {}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "invalid tool-call index metadata",
        ),
        (
            {
                "created": 1 << 64,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            "invalid created metadata",
        ),
        (
            {
                "created": True,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            "invalid created metadata",
        ),
    ],
)
def test_stream_rejects_malformed_numeric_metadata_without_echo(chunk, expected):
    with pytest.raises(StreamAggregationError) as captured:
        consume_stream(_sse(json.dumps(chunk), "[DONE]"))

    assert str(captured.value) == expected
    assert "PRIVATE" not in str(captured.value)


def test_usage_log_note_accepts_only_bounded_json_integers():
    raw = json.dumps(
        {
            "usage": {
                "prompt_tokens": "7\nPRIVATE_LOG_INJECTION",
                "completion_tokens": 11,
                "total_tokens": 1 << 64,
            }
        }
    ).encode()

    note = _usage_log_note(raw)

    assert note == " completion_tokens=11"
    assert "PRIVATE" not in note


@pytest.mark.parametrize(
    "value", [True, -1, 1 << 64, 1.5, [7], {"value": 7}]
)
def test_usage_log_note_omits_non_integer_or_out_of_range_counts(value):
    raw = json.dumps(
        {"usage": {"prompt_tokens": value, "completion_tokens": value}}
    ).encode()
    assert _usage_log_note(raw) == ""


def test_http_error_keeps_code_but_never_echoes_upstream_body(monkeypatch):
    raw = json.dumps({
        "error": {
            "code": "insufficient_quota",
            "type": "rate_limit_error",
            "message": "PRIVATE_PROMPT",
            "token": "PRIVATE_KEY",
        }
    }).encode()
    error = urllib.error.HTTPError(
        "https://provider.invalid/private?key=PRIVATE_KEY",
        429,
        "PRIVATE_PROMPT",
        {"Retry-After": "60"},
        BytesIO(raw),
    )
    monkeypatch.setattr(
        "scripts.kimi_k3_gateway.urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    handler = object.__new__(GatewayHandler)

    status, sanitized, retry_after = handler._upstream_once(b"{}", None)

    assert status == 429
    assert retry_after == "60"
    assert b"PRIVATE" not in sanitized
    payload = json.loads(sanitized)
    assert payload["error"]["code"] == "insufficient_quota"
    assert payload["error"]["type"] == "rate_limit_error"


def test_http_error_rejects_secret_shaped_code_and_type():
    sanitized = sanitize_upstream_http_error(
        500,
        b'{"error":{"code":"PRIVATE SECRET VALUE","type":"bad/value"}}',
    )
    assert b"PRIVATE" not in sanitized
    payload = json.loads(sanitized)
    assert payload["error"]["code"] == "upstream_http_error"
    assert payload["error"]["type"] == "http_error"


def test_http_error_rejects_single_token_secret_in_code_and_type():
    canary = "sk-kimi-CANARY_PRIVATE_TOKEN"
    sanitized = sanitize_upstream_http_error(
        500,
        json.dumps({"error": {"code": canary, "type": canary}}).encode(),
    )
    assert canary.encode() not in sanitized
    payload = json.loads(sanitized)
    assert payload["error"] == {
        "code": "upstream_http_error",
        "type": "http_error",
        "message": "upstream returned HTTP 500",
    }


@pytest.mark.parametrize("status", [401, 403, 429, 502, 524])
def test_gateway_forwards_upstream_failure_after_one_attempt(monkeypatch, status):
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
        return status, b'{"error":{"message":"upstream failure"}}', "60"

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
    assert f" {status} ".encode() in handler.wfile.getvalue().split(b"\r\n", 1)[0]
