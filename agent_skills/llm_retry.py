"""Shared retry policy for OpenAI-compatible gateway calls.

The OpenAI SDK honors a provider ``Retry-After`` header before applying its
own backoff.  Some compatible gateways return 60 seconds for transient 5xx
responses, which makes one failed request stall the whole campaign.  Chem
Agent disables SDK retries and retries explicitly here so every attempt is
observable by the benchmark timer and the delay is bounded and predictable.
"""

from __future__ import annotations

import logging
import contextvars
import math
import os
import queue
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Any, Callable, TypeVar


DEFAULT_GATEWAY_RETRY_DELAY_SECONDS = 10.0
_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_ERROR_CODES = {
    "upstream_error",
    "server_error",
    "internal_error",
    "internal_server_error",
    "stream_read_error",
    "overloaded",
    "service_unavailable",
    "service_temporarily_unavailable",
    "temporarily_unavailable",
    "gateway_error",
    "rate_limit_exceeded",
    "rate_limit_error",
}
_TERMINAL_ERROR_CODES = {
    "access_terminated_error",
    "insufficient_quota",
    "authentication_error",
    "permission_denied",
    "invalid_api_key",
}
_SAFE_SEMANTIC_ERROR_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "max_context_length_exceeded",
}
_ResultT = TypeVar("_ResultT")


class LogicalCallDeadlineExceeded(TimeoutError):
    """The shared wall budget is exhausted; another retry cannot help."""


class NonRetryableGatewayError(RuntimeError):
    """A classified terminal protocol failure is not a network retry."""


@dataclass
class AttemptContext:
    logical_call_id: str
    attempt_id: str
    deadline: float
    on_timeout: list[Callable[[BaseException], None]] = field(default_factory=list)
    cancelled: bool = False


_ATTEMPT: contextvars.ContextVar[AttemptContext | None] = contextvars.ContextVar("chem_llm_attempt", default=None)
_CALL_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("chem_llm_deadline", default=None)


@contextmanager
def logical_call_budget(timeout: float = 240.0):
    """Keep failover and compatibility variants within one logical deadline."""
    token = _CALL_DEADLINE.set(logical_deadline(timeout))
    try:
        yield
    finally:
        _CALL_DEADLINE.reset(token)


@contextmanager
def absolute_call_budget(deadline: float):
    """Clamp nested retry owners to a precomputed absolute deadline."""

    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    ambient = _CALL_DEADLINE.get()
    effective = min(deadline, ambient) if ambient is not None else deadline
    token = _CALL_DEADLINE.set(effective)
    try:
        yield
    finally:
        _CALL_DEADLINE.reset(token)


def current_attempt() -> AttemptContext | None:
    return _ATTEMPT.get()


def positive_timeout_env(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def configured_wall_timeout(default: float = 240.0) -> float:
    return positive_timeout_env("REFINER_LLM_WALL_TIMEOUT_SECONDS", default)


def logical_deadline(timeout: float = 240.0) -> float:
    proposed = time.monotonic() + configured_wall_timeout(timeout)
    ambient = _CALL_DEADLINE.get()
    if ambient is not None:
        proposed = min(proposed, ambient)
    active = current_attempt()
    return min(proposed, active.deadline) if active else proposed


def remaining_timeout(default: float) -> float:
    now = time.monotonic()
    active = current_attempt()
    remaining = default
    ambient = _CALL_DEADLINE.get()
    if ambient is not None:
        remaining = min(remaining, ambient - now)
    if active is not None:
        remaining = min(remaining, active.deadline - now)
    if remaining <= 0 or (active is not None and active.cancelled):
        raise LogicalCallDeadlineExceeded("LLM logical call exceeded its absolute wall deadline")
    return remaining


def _bounded_attempt(operation: Callable[[], _ResultT], attempt: AttemptContext) -> _ResultT:
    """Bound blocking SDK reads too, including SSE comments with no events.

    The caller never waits for an uncooperative iterator. Transports register
    cancellation callbacks which close the response/socket on timeout. Daemon
    workers are a last-resort containment for injected providers without a
    cancellation API; they cannot keep process shutdown waiting indefinitely.
    """
    result: queue.Queue[Any] = queue.Queue(maxsize=1)
    context = contextvars.copy_context()

    def run() -> None:
        try:
            if attempt.cancelled or time.monotonic() >= attempt.deadline:
                raise LogicalCallDeadlineExceeded("LLM attempt was cancelled before starting")
            result.put((True, context.run(operation)))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=run, name=f"llm-{attempt.attempt_id}", daemon=True).start()
    try:
        ok, value = result.get(timeout=max(0.0, attempt.deadline - time.monotonic()))
    except queue.Empty:
        error = LogicalCallDeadlineExceeded("LLM logical call exceeded its absolute wall deadline")
        attempt.cancelled = True
        for callback in list(attempt.on_timeout):
            callback(error)
        raise error from None
    if time.monotonic() >= attempt.deadline:
        error = LogicalCallDeadlineExceeded("LLM logical call exceeded its absolute wall deadline")
        attempt.cancelled = True
        for callback in list(attempt.on_timeout):
            callback(error)
        raise error
    if not ok:
        raise value
    return value


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
    if value is None:
        diagnostic = getattr(exc, "responses_diagnostics", None)
        if isinstance(diagnostic, Mapping):
            value = diagnostic.get("http_status")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _diagnostic_error_code(exc: BaseException) -> str:
    def extract(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            return ""
        nested = payload.get("error")
        # OpenAI chat exceptions expose either ``body={code,type}`` or
        # ``body={error:{code,type,message}}``; relays may also put the
        # transient class in ``type`` while ``code`` carries a wrapper-specific
        # string such as ``stream_read_error``. Inspect only the finite
        # code/type allowlist; never copy/log provider-controlled messages.
        candidates = [nested, payload]
        typed: list[str] = []
        coded: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            for name, bucket in (("type", typed), ("code", coded)):
                value = candidate.get(name)
                if isinstance(value, str) and value.strip():
                    bucket.append(value.strip().lower())
        known = _RETRYABLE_ERROR_CODES | _TERMINAL_ERROR_CODES | _SAFE_SEMANTIC_ERROR_CODES
        known_values = [value for value in coded + typed if value in known]
        # Terminal classes (quota/auth) outrank generic transient families so
        # a 429 carrying ``code=insufficient_quota, type=rate_limit_error`` is
        # never replayed; within a class, the explicit code outranks the type.
        for group in (_TERMINAL_ERROR_CODES, _SAFE_SEMANTIC_ERROR_CODES, _RETRYABLE_ERROR_CODES):
            for value in known_values:
                if value in group:
                    return value
        return (coded + typed)[0] if (coded or typed) else ""

    direct_code = getattr(exc, "_chem_gateway_error_code", None)
    if isinstance(direct_code, str) and direct_code.strip():
        return direct_code.strip().lower()
    diagnostic = getattr(exc, "responses_diagnostics", None)
    code = extract(diagnostic)
    if code:
        return code
    return extract(getattr(exc, "body", None))


def safe_gateway_error_metadata(exc: BaseException) -> tuple[str, int | None]:
    """Return provider-text-free metadata for a gateway exception.

    SDK HTTP exceptions often include the response body and request URL in
    ``str(exc)``.  Callers that cross a trust boundary can use this helper to
    replace such exceptions without copying any provider-controlled text.
    """

    error_type = type(exc).__name__
    if (
        not error_type
        or len(error_type) > 100
        or any(
            not (character.isascii() and (character.isalnum() or character == "_"))
            for character in error_type
        )
    ):
        error_type = "GatewayError"
    status_code = _status_code(exc)
    if status_code is not None and not 100 <= status_code <= 599:
        status_code = None
    return error_type, status_code


def safe_gateway_error_code(exc: BaseException) -> str | None:
    """Return only a known classification code, never arbitrary provider text."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        code = _diagnostic_error_code(current)
        if code in (
            _RETRYABLE_ERROR_CODES
            | _TERMINAL_ERROR_CODES
            | _SAFE_SEMANTIC_ERROR_CODES
        ):
            return code
        current = current.__cause__ or current.__context__
    return None


def _diagnostic_terminal_state(exc: BaseException) -> tuple[str, str]:
    diagnostic = getattr(exc, "responses_diagnostics", None)
    if not isinstance(diagnostic, Mapping):
        return "", ""
    event_type = diagnostic.get("event_type")
    response_status = diagnostic.get("response_status")
    return (
        event_type.strip().lower() if isinstance(event_type, str) else "",
        response_status.strip().lower()
        if isinstance(response_status, str)
        else "",
    )


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Traverse both explicit and implicit wrappers once, outermost first."""
    output: list[BaseException] = []
    pending = [exc]
    seen: set[int] = set()
    while pending and len(output) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        output.append(current)
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException) and id(nested) not in seen:
                pending.append(nested)
    return output


def is_retryable_gateway_error(exc: BaseException) -> bool:
    """Return whether an exception chain contains a transient network error."""

    chain = _exception_chain(exc)
    # Terminal evidence anywhere in a wrapper chain outranks vague outer
    # messages such as RuntimeError("upstream_error").
    for current in chain:
        if getattr(current, "_chem_no_retry", False) is True:
            return False
        if isinstance(current, (LogicalCallDeadlineExceeded, NonRetryableGatewayError)):
            return False
        error_code = _diagnostic_error_code(current)
        status_code = _status_code(current)
        if status_code in {401, 403} or error_code in _TERMINAL_ERROR_CODES:
            return False
        event_type, response_status = _diagnostic_terminal_state(current)
        if response_status in {"incomplete", "cancelled"} or event_type in {
            "response.incomplete", "response.cancelled",
        }:
            return False

    for current in chain:
        if isinstance(current, RetryableGatewayError):
            return True
        error_code = _diagnostic_error_code(current)
        status_code = _status_code(current)
        if error_code in _RETRYABLE_ERROR_CODES and (
            status_code is None
            or 200 <= status_code < 300
            or status_code in _RETRYABLE_STATUS_CODES
            or status_code >= 500
        ):
            return True
        if status_code is not None:
            if status_code in _RETRYABLE_STATUS_CODES or status_code >= 500:
                return True
            if 200 <= status_code < 300:
                # A stream/read may time out after a successful HTTP
                # handshake. Continue with exception-type classification.
                pass
            else:
                # A concrete non-retryable HTTP failure outranks vague class
                # names or messages attached by an SDK wrapper.
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
    return False


def is_terminal_gateway_error(exc: BaseException) -> bool:
    """Return whether retry/failover cannot repair authentication or quota."""
    for current in _exception_chain(exc):
        if getattr(current, "_chem_terminal_gateway_error", False) is True:
            return True
        if _status_code(current) in {401, 403}:
            return True
        if _diagnostic_error_code(current) in _TERMINAL_ERROR_CODES:
            return True
    return False


def call_with_gateway_retry(
    operation: Callable[[], _ResultT],
    *,
    max_retries: int,
    logger: logging.Logger | None = None,
    operation_name: str = "LLM gateway request",
    sleep: Callable[[float], Any] | None = None,
    wall_timeout_seconds: float | None = None,
    deadline: float | None = None,
) -> _ResultT:
    """Call ``operation`` and retry transient failures after a fixed delay.

    ``max_retries`` counts retries after the initial request.  Keeping the
    loop outside the SDK also means request-level timing records each failed
    or successful attempt separately.
    """

    retry_budget = max(0, int(max_retries))
    sleep_for = sleep or time.sleep
    active = current_attempt()
    if active is not None:
        # Nested adapters share the same attempt and never multiply retries.
        remaining_timeout(240.0)
        return operation()
    timeout = configured_wall_timeout() if wall_timeout_seconds is None else float(wall_timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("wall_timeout_seconds must be a finite positive number")
    absolute_deadline = time.monotonic() + timeout
    if deadline is not None:
        absolute_deadline = min(deadline, absolute_deadline) if wall_timeout_seconds is not None else deadline
    ambient = _CALL_DEADLINE.get()
    if ambient is not None:
        absolute_deadline = min(absolute_deadline, ambient)
    logical_call_id = uuid.uuid4().hex
    for retry_index in range(retry_budget + 1):
        if time.monotonic() >= absolute_deadline:
            raise LogicalCallDeadlineExceeded("LLM logical call exhausted its wall budget before another attempt")
        attempt = AttemptContext(logical_call_id, uuid.uuid4().hex, absolute_deadline)
        token = _ATTEMPT.set(attempt)
        try:
            return _bounded_attempt(operation, attempt)
        except Exception as exc:
            if retry_index >= retry_budget or not is_retryable_gateway_error(exc):
                raise
            delay = configured_gateway_retry_delay_seconds()
            if absolute_deadline - time.monotonic() <= delay:
                raise LogicalCallDeadlineExceeded("LLM logical call has insufficient wall budget for the fixed retry delay") from exc
            if logger is not None:
                logger.warning(
                    "%s failed with %s; retrying in %.3gs (%s/%s)",
                    operation_name,
                    type(exc).__name__,
                    delay,
                    retry_index + 1,
                    retry_budget,
                )
            from .llm_timing import record_retry_sleep
            record_retry_sleep(delay, attempt, started=True)
            sleep_started = time.monotonic()
            try:
                sleep_for(delay)
            finally:
                record_retry_sleep(time.monotonic() - sleep_started, attempt)
        finally:
            _ATTEMPT.reset(token)

    raise RuntimeError("unreachable gateway retry state")
