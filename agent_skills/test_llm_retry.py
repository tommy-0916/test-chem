"""Tests for the shared fixed-delay gateway retry policy."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from agent_skills.llm_retry import (
    RetryableGatewayError,
    call_with_gateway_retry,
    configured_gateway_retry_delay_seconds,
    is_retryable_gateway_error,
    safe_gateway_error_code,
)


class _GatewayError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"gateway status {status_code}")
        self.status_code = status_code


def test_sanitized_retryable_gateway_error_is_retryable():
    error = RetryableGatewayError(
        "gateway request failed: stderr_len=12,sha256=deadbeef"
    )

    assert is_retryable_gateway_error(error)


def test_retry_after_gateway_failure_waits_ten_seconds(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _GatewayError(502)
        return "ok"

    result = call_with_gateway_retry(
        operation,
        max_retries=1,
        sleep=sleeps.append,
    )

    assert result == "ok"
    assert calls == 2
    assert sleeps == [10.0]
    assert configured_gateway_retry_delay_seconds() == 10.0


def test_nonretryable_client_error_is_not_retried():
    calls = 0
    sleeps: list[float] = []

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise _GatewayError(401)

    with pytest.raises(_GatewayError):
        call_with_gateway_retry(operation, max_retries=4, sleep=sleeps.append)

    assert calls == 1
    assert sleeps == []


def test_status_from_response_is_retryable():
    response_error = RuntimeError("provider error")
    response_error.response = SimpleNamespace(status_code=429)

    assert is_retryable_gateway_error(response_error)


def test_status_from_google_style_code_is_retryable():
    response_error = RuntimeError("provider error")
    response_error.code = 429

    assert is_retryable_gateway_error(response_error)


def test_relay_wrapped_upstream_error_is_retryable():
    # Relays may wrap a dead stream as an SDK APIError whose ``code`` carries a
    # wrapper-specific string (``stream_read_error``) while the transient
    # class sits in ``type`` (``upstream_error``). The known-code extraction
    # must look at both fields instead of trusting the first one.
    error = RuntimeError("stream failed")
    error.body = {
        "error": {"code": "stream_read_error", "message": "stream_read_error", "type": "upstream_error"},
        "event_type": "response.in_progress",
        "http_status": 200,
    }

    assert is_retryable_gateway_error(error)
    assert safe_gateway_error_code(error) in {"upstream_error", "stream_read_error"}


def test_delay_cannot_be_overridden_by_gateway_retry_after_or_environment(monkeypatch):
    monkeypatch.setenv("REFINER_LLM_RETRY_DELAY_SECONDS", "60")
    assert configured_gateway_retry_delay_seconds() == 10.0


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("offline"),
        TimeoutError("timed out"),
        httpx.ConnectTimeout("connect timeout"),
        httpx.WriteTimeout("write timeout"),
        httpx.PoolTimeout("pool timeout"),
        httpx.RemoteProtocolError("peer disconnected"),
    ],
)
def test_common_network_exception_families_are_retryable(error):
    assert is_retryable_gateway_error(error)


def test_chained_network_error_is_retryable():
    try:
        try:
            raise ConnectionError("socket closed")
        except ConnectionError as cause:
            raise RuntimeError("provider wrapper") from cause
    except RuntimeError as error:
        assert is_retryable_gateway_error(error)
