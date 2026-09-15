from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_skills.llm_retry import RetryableGatewayError
from agent_skills.responses_stream import (
    consume_responses_stream,
    responses_streaming_enabled,
)


class _Stream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


def test_streaming_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("REFINER_RESPONSES_STREAM", raising=False)
    assert responses_streaming_enabled() is False
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "true")
    assert responses_streaming_enabled() is True


def test_consume_stream_joins_text_and_keeps_final_response():
    final = SimpleNamespace(status="completed", output_text='{"ok":true}')
    stream = _Stream([
        {"type": "response.output_text.delta", "delta": "{\"ok\":"},
        SimpleNamespace(type="response.output_text.delta", delta="true}"),
        SimpleNamespace(type="response.output_text.done", text='{"ok":true}'),
        SimpleNamespace(type="response.completed", response=final),
    ])

    text, response = consume_responses_stream(stream)

    assert text == '{"ok":true}'
    assert response is final
    assert stream.closed is True


def test_incomplete_or_missing_terminal_event_fails_closed():
    incomplete = _Stream([
        SimpleNamespace(
            type="response.incomplete",
            response=SimpleNamespace(
                incomplete_details=SimpleNamespace(reason="max_output_tokens")
            ),
        )
    ])
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        consume_responses_stream(incomplete)
    assert incomplete.closed is True

    truncated = _Stream([
        SimpleNamespace(type="response.output_text.delta", delta="partial")
    ])
    with pytest.raises(RetryableGatewayError, match="without response.completed"):
        consume_responses_stream(truncated)
    assert truncated.closed is True


def test_retryable_stream_failure_is_sanitized_and_closed():
    stream = _Stream([
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "server_error",
                    "message": "private provider detail",
                }
            },
        }
    ])

    with pytest.raises(RetryableGatewayError) as caught:
        consume_responses_stream(stream)

    assert "private provider detail" not in str(caught.value)
    assert stream.closed is True
