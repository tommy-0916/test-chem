"""Shared retry policy for OpenAI-compatible gateway calls.

The OpenAI SDK honors a provider ``Retry-After`` header before applying its
own backoff.  Some compatible gateways return 60 seconds for transient 5xx
responses, which makes one failed request stall the whole campaign.  Chem
Agent disables SDK retries and retries explicitly here so every attempt is
observable by the benchmark timer and the delay is bounded and predictable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar


DEFAULT_GATEWAY_RETRY_DELAY_SECONDS = 10.0
_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_ResultT = TypeVar("_ResultT")


class RetryableGatewayError(RuntimeError):
    """Mark a gateway failure retryable without retaining provider payloads.

    Callers should pass only a sanitized diagnostic as the exception message.
    The explicit type lets transports classify private response details first
    and discard them before the shared retry boundary sees the failure.
    """


def configured_gateway_retry_delay_seconds() -> float:
    """Return the fixed delay between retryable gateway attempts."""

    return DEFAULT_GATEWAY_RETRY_DELAY_SECONDS


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    if value is None:
        value = getattr(exc, "code", None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable_gateway_error(exc: BaseException) -> bool:
    """Return whether an exception chain contains a transient network error."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RetryableGatewayError):
            return True
        status_code = _status_code(current)
        if status_code is not None:
            if status_code in _RETRYABLE_STATUS_CODES or status_code >= 500:
                return True
            # A concrete non-retryable HTTP status outranks vague class names
            # or messages attached by an SDK wrapper.
            return False

        if isinstance(current, (ConnectionError, TimeoutError)):
            return True

        type_names = {
            base.__name__.lower() for base in type(current).__mro__
        }
        if any(
            marker in name
            for name in type_names
            for marker in (
                "connectionerror",
                "timeout",
                "transporterror",
                "protocolerror",
                "networkerror",
            )
        ):
            return True

        message = str(current).lower()
        if any(
            marker in message
            for marker in (
                "bad gateway",
                "gateway timeout",
                "origin_bad_gateway",
                "connection reset",
                "connection refused",
                "connection timed out",
                "read timed out",
                "write timed out",
                "pool timeout",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def call_with_gateway_retry(
    operation: Callable[[], _ResultT],
    *,
    max_retries: int,
    logger: logging.Logger | None = None,
    operation_name: str = "LLM gateway request",
    sleep: Callable[[float], Any] | None = None,
) -> _ResultT:
    """Call ``operation`` and retry transient failures after a fixed delay.

    ``max_retries`` counts retries after the initial request.  Keeping the
    loop outside the SDK also means request-level timing records each failed
    or successful attempt separately.
    """

    retry_budget = max(0, int(max_retries))
    sleep_for = sleep or time.sleep
    for retry_index in range(retry_budget + 1):
        try:
            return operation()
        except Exception as exc:
            if retry_index >= retry_budget or not is_retryable_gateway_error(exc):
                raise
            delay = configured_gateway_retry_delay_seconds()
            if logger is not None:
                logger.warning(
                    "%s failed with %s; retrying in %.3gs (%s/%s)",
                    operation_name,
                    type(exc).__name__,
                    delay,
                    retry_index + 1,
                    retry_budget,
                )
            sleep_for(delay)

    raise RuntimeError("unreachable gateway retry state")
