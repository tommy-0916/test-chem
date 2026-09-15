"""Small helpers for streamed OpenAI-compatible Responses requests."""

from __future__ import annotations

import os
from typing import Any, Tuple

from .llm_retry import RetryableGatewayError


def responses_streaming_enabled() -> bool:
    """Return whether direct Responses calls should consume SSE streams."""

    return os.getenv("REFINER_RESPONSES_STREAM", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _failure_code(event: Any) -> str:
    candidates = [
        _value(event, "code"),
        _value(_value(event, "error"), "code"),
        _value(_value(_value(event, "response"), "error"), "code"),
    ]
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip().lower()
    return "unknown"


def consume_responses_stream(stream: Any) -> Tuple[str, Any]:
    """Consume a raw Responses event stream and return text plus final response.

    Only structured event types and error codes enter exceptions. Provider
    payloads, prompt text and partial model output are never copied into error
    messages.
    """

    deltas: list[str] = []
    done_texts: list[str] = []
    final_response: Any = None
    try:
        for event in stream:
            event_type = str(_value(event, "type") or "")
            if event_type == "response.output_text.delta":
                delta = _value(event, "delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            elif event_type == "response.output_text.done":
                text = _value(event, "text")
                if isinstance(text, str):
                    done_texts.append(text)
            elif event_type == "response.completed":
                final_response = _value(event, "response")
            elif event_type == "response.incomplete":
                response = _value(event, "response")
                details = _value(response, "incomplete_details")
                reason = str(_value(details, "reason") or "unknown")
                raise RuntimeError(
                    f"Responses stream incomplete (reason={reason})"
                )
            elif event_type in {"error", "response.failed"}:
                code = _failure_code(event)
                detail = f"Responses stream failed (event={event_type}, code={code})"
                if any(
                    marker in code
                    for marker in (
                        "server_error",
                        "rate_limit",
                        "timeout",
                        "overloaded",
                        "unavailable",
                        "internal",
                        "gateway",
                    )
                ):
                    raise RetryableGatewayError(detail)
                raise RuntimeError(detail)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    if final_response is None:
        raise RetryableGatewayError(
            "Responses stream ended without response.completed"
        )
    status = _value(final_response, "status")
    if status not in {None, "", "completed"}:
        raise RuntimeError(
            f"Responses stream returned unexpected final status ({status})"
        )

    text = "".join(deltas)
    if not text and done_texts:
        text = "\n".join(done_texts)
    return text.strip(), final_response
