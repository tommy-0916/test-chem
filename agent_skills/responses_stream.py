"""Fail-closed Responses streaming shared by Research, Device and native tools.

Only a validated ``response.completed`` snapshot is returned.  The terminal
event ends consumption immediately, so a compatible gateway is not required to
send ``[DONE]`` or close the HTTP body after the authoritative response.  A
read-idle watchdog and the shared logical-call deadline bound blocked streams.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable

from agent_skills.responses_diagnostics import (
    attach_responses_diagnostics,
    build_response_diagnostics,
    get_responses_diagnostics,
)
from agent_skills.llm_retry import (
    LogicalCallDeadlineExceeded,
    NonRetryableGatewayError,
    current_attempt,
    logical_deadline,
    positive_timeout_env,
)


logger = logging.getLogger(__name__)


class ResponsesReadIdleTimeout(TimeoutError):
    """No decoded event or raw SSE activity arrived inside the idle budget."""


class ResponsesStreamError(RuntimeError):
    """A Responses result was not safely completed (not a transport timeout)."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        if diagnostics is not None:
            attach_responses_diagnostics(self, diagnostics)

    def __str__(self) -> str:
        message = super().__str__()
        diagnostic = get_responses_diagnostics(self)
        if diagnostic:
            message += " [responses_diagnostics=" + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")) + "]"
        return message


class ResponsesProtocolError(ResponsesStreamError, NonRetryableGatewayError):
    """Missing, malformed or conflicting terminal protocol state."""


class ResponsesTerminalError(ResponsesStreamError):
    """The provider explicitly returned an unsuccessful terminal result."""


def configured_responses_streaming() -> bool:
    """Stream by default; explicit ``REFINER_RESPONSES_STREAM=0`` opts out."""
    value = os.getenv("REFINER_RESPONSES_STREAM", "1").strip().lower()
    if value in {"", "1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("REFINER_RESPONSES_STREAM must be 1/0, true/false, yes/no or on/off")


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if callable(getattr(value, "model_dump", None)):
        return _plain(value.model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def validate_completed_response(response: Any, **diagnostic_context: Any) -> Any:
    """Check terminal status without discarding SDK output/tool/usage fields."""
    def fail(error_type: type[ResponsesStreamError], message: str) -> None:
        raise error_type(message, diagnostics=build_response_diagnostics(
            response=response, phase="nonstream_response", **diagnostic_context,
        ))

    status = _field(response, "status")
    if status in {"failed", "incomplete", "cancelled"}:
        fail(ResponsesTerminalError, f"Responses API returned terminal status={status}")
    if status != "completed":
        fail(ResponsesProtocolError, "Responses API did not return status=completed")
    if _field(response, "error") or _field(response, "incomplete_details"):
        fail(ResponsesProtocolError, "Completed Responses result contains error/incomplete details")
    output = _field(response, "output")
    if isinstance(output, (list, tuple)):
        for item in output:
            item_status = _field(item, "status")
            # SDK/provider variants may omit item status, but an explicit
            # unfinished message/tool item contradicts the completed envelope.
            if item_status is not None and item_status != "completed":
                fail(ResponsesProtocolError, "Completed Responses result contains an unfinished output item")
    return response


class ResponsesStreamState:
    """Consume decoded SDK events and retain only a validated terminal result."""

    def __init__(self, **diagnostic_context: Any) -> None:
        self._response: Any = None
        self._response_fingerprint: str | None = None
        self._response_id: str | None = None
        self._failure: ResponsesStreamError | None = None
        self._diagnostic_context = diagnostic_context
        self._observed_diagnostics: dict[str, Any] = {}

    def diagnostics(self, *, event: Any = None, phase: str = "stream_read") -> dict[str, Any]:
        return {**self._observed_diagnostics, **build_response_diagnostics(
            event=event, phase=phase, **self._diagnostic_context,
        )}

    def _fail(self, error: ResponsesStreamError, event: Any = None, *, phase: str = "stream_event") -> None:
        attach_responses_diagnostics(error, {
            **self.diagnostics(event=event, phase=phase),
            **(get_responses_diagnostics(error) or {}),
            "phase": phase,
        })
        self._failure = error
        raise error

    @property
    def completed(self) -> bool:
        return self._response is not None and self._failure is None

    def consume(self, event: Any) -> None:
        if self._failure is not None:
            raise self._failure
        event_type = _field(event, "type")
        response = _field(event, "response")
        if response is not None:
            # Retain only bounded metadata, never a partial text/tool payload.
            # Delta events do not trigger extra per-token logging/serialization.
            self._observed_diagnostics.update(build_response_diagnostics(
                response=response, event=event, **self._diagnostic_context,
            ))
        if not isinstance(event_type, str) or not event_type:
            self._fail(ResponsesProtocolError("Responses stream event has no type"), event)
        if event_type in {"error", "response.error", "response.failed", "response.incomplete", "response.cancelled"}:
            self._fail(ResponsesTerminalError(f"Responses stream emitted {event_type}"), event)

        response_status = _field(response, "status")
        if response_status in {"failed", "incomplete", "cancelled"}:
            self._fail(ResponsesTerminalError(f"Responses stream contains status={response_status}"), event)
        response_id = _field(response, "id")
        if response_id:
            if self._response_id is not None and response_id != self._response_id:
                self._fail(ResponsesProtocolError("Responses stream contains conflicting response IDs"), event)
            self._response_id = response_id
        if event_type == "response.completed":
            try:
                validate_completed_response(response, **self._diagnostic_context)
                fingerprint = json.dumps(_plain(response), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except ResponsesStreamError as exc:
                self._fail(exc, event)
            except (TypeError, ValueError) as exc:
                self._fail(ResponsesProtocolError("Completed Responses result is not serializable"), event)
            if self._response is not None and fingerprint != self._response_fingerprint:
                self._fail(ResponsesProtocolError("Responses stream contains conflicting completed results"), event)
            self._response = response
            self._response_fingerprint = fingerprint
        elif self._response is not None and event_type.startswith("response."):
            self._fail(ResponsesProtocolError("Responses stream continued with data after completion"), event)

    def finish(self) -> Any:
        if self._failure is not None:
            raise self._failure
        if self._response is None:
            self._fail(ResponsesProtocolError("Responses stream ended without response.completed"), phase="stream_eof")
        return self._response


def _http_context(response: Any) -> dict[str, Any]:
    """Read just the correlation header, not the request or full headers."""
    try:
        headers = getattr(response, "headers", None)
        request_id = headers.get("x-request-id") if callable(getattr(headers, "get", None)) else None
        return {"request_id": request_id, "http_status": getattr(response, "status_code", None)}
    except Exception:
        return {}


def _request_context(client: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"model": payload.get("model"), "max_output_tokens": payload.get("max_output_tokens"),
            "api_key": getattr(client, "api_key", None)}


def _annotate_exception(exc: BaseException, context: dict[str, Any], *, phase: str,
                        state: ResponsesStreamState | None = None) -> None:
    """Attach safe metadata without changing exception type or retry policy."""
    try:
        if get_responses_diagnostics(exc):
            return
        observed = state.diagnostics(phase=phase) if state else {}
        http = _http_context(getattr(exc, "response", None))
        combined = {**context, **{key: value for key, value in http.items() if value is not None}}
        if not combined.get("request_id"):
            combined["request_id"] = getattr(exc, "request_id", None)
        body = getattr(exc, "body", None)
        # OpenAI may raise APIError before exposing an SSE error event, and its
        # .body can already be the nested error object. Never stringify it.
        event = body if isinstance(body, Mapping) else None
        body_response = None
        if event is not None:
            event_type = event.get("type")
            is_event = (isinstance(event_type, str) and
                        (event_type == "error" or event_type.startswith("response.")))
            if (not is_event and isinstance(event.get("id"), str)
                    and event.get("status") in ("completed", "failed", "incomplete", "cancelled", "in_progress", "queued")):
                body_response, event = event, None
            elif (not is_event and not isinstance(event.get("response"), Mapping)
                    and not isinstance(event.get("error"), Mapping)):
                event = {"error": body, "request_id": body.get("request_id")}
        details = build_response_diagnostics(response=body_response, event=event, phase=phase, **combined)
        attach_responses_diagnostics(exc, {**observed, **details})
    except Exception:
        # A malformed metadata property must never mask the original failure.
        return


def _stream_limits() -> tuple[float, float]:
    """Return one absolute wall deadline and the read-idle interval."""
    active = current_attempt()
    deadline = active.deadline if active is not None else logical_deadline(240.0)
    idle = positive_timeout_env("REFINER_LLM_READ_IDLE_TIMEOUT_SECONDS", 240.0)
    return deadline, idle


def _install_raw_activity_probe(response: Any, activity: list[float]) -> None:
    """Count SSE heartbeat bytes when the installed httpx transport exposes them."""
    raw_response = getattr(response, "response", None)
    try:
        import httpx

        if not isinstance(raw_response, httpx.Response):
            return
        original_stream = raw_response.stream
        if not isinstance(original_stream, httpx.SyncByteStream):
            return

        class ActivityStream(httpx.SyncByteStream):
            def __iter__(self):
                for chunk in original_stream:
                    activity[0] = time.monotonic()
                    yield chunk

            def close(self) -> None:
                original_stream.close()

        raw_response.stream = ActivityStream()
    except Exception:
        # The watchdog still observes decoded events and the SDK socket timeout.
        return


def _close_sync_stream(response: Any) -> None:
    """Close a stream without letting a broken provider block the caller."""
    close = getattr(response, "close", None)
    if not callable(close):
        return
    finished = threading.Event()

    def run() -> None:
        try:
            close()
        except Exception as exc:
            logger.warning("RESPONSES_STREAM_CLOSE_FAILED error_type=%s", type(exc).__name__)
        finally:
            finished.set()

    threading.Thread(target=run, name="responses-close", daemon=True).start()
    if not finished.wait(0.05):
        logger.warning("RESPONSES_STREAM_CLOSE_PENDING")


def _consume_sync_stream(
    response: Any,
    state: ResponsesStreamState,
    on_event: Callable[[str], None] | None = None,
) -> Any:
    deadline, read_idle = _stream_limits()
    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    stopped = threading.Event()
    activity = [time.monotonic()]
    _install_raw_activity_probe(response, activity)

    def read() -> None:
        try:
            for event in response:
                if stopped.is_set():
                    return
                events.put(("event", event))
            events.put(("eof", None))
        except BaseException as exc:
            events.put(("error", exc))

    context = contextvars.copy_context()
    threading.Thread(
        target=lambda: context.run(read),
        name="responses-reader",
        daemon=True,
    ).start()
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise LogicalCallDeadlineExceeded(
                    "Responses exceeded its absolute wall deadline"
                )
            idle_remaining = activity[0] + read_idle - now
            if idle_remaining <= 0:
                raise ResponsesReadIdleTimeout(
                    "Responses exceeded its read-idle timeout"
                )
            try:
                kind, value = events.get(
                    timeout=min(deadline - now, idle_remaining)
                )
            except queue.Empty:
                continue
            if kind == "error":
                raise value
            if kind == "eof":
                return state.finish()
            activity[0] = time.monotonic()
            event_type = _field(value, "type")
            if on_event is not None and isinstance(event_type, str):
                on_event(event_type)
            state.consume(value)
            if state.completed:
                return state.finish()
    finally:
        stopped.set()


def invoke_responses(
    client: Any,
    payload: Mapping[str, Any],
    *,
    on_event: Callable[[str], None] | None = None,
) -> Any:
    """Invoke once, close the stream on every exit, and return only completion."""
    streaming = configured_responses_streaming()
    context = _request_context(client, payload)
    try:
        response = client.responses.create(**{**payload, "stream": streaming})
    except BaseException as exc:
        _annotate_exception(exc, context, phase="request")
        raise
    if not streaming:
        validated = validate_completed_response(response, **context)
        if on_event is not None:
            on_event("response.completed")
        return validated
    context.update(_http_context(getattr(response, "response", None)))
    state = ResponsesStreamState(**context)
    try:
        return _consume_sync_stream(response, state, on_event=on_event)
    except BaseException as exc:
        _annotate_exception(exc, context, phase="stream_read", state=state)
        raise
    finally:
        _close_sync_stream(response)


async def invoke_responses_async(
    client: Any,
    payload: Mapping[str, Any],
    *,
    on_event: Callable[[str], None] | None = None,
) -> Any:
    """Async counterpart preserving the same terminal and close guarantees."""
    streaming = configured_responses_streaming()
    context = _request_context(client, payload)
    try:
        response = await client.responses.create(**{**payload, "stream": streaming})
    except BaseException as exc:
        _annotate_exception(exc, context, phase="request")
        raise
    if not streaming:
        validated = validate_completed_response(response, **context)
        if on_event is not None:
            on_event("response.completed")
        return validated
    context.update(_http_context(getattr(response, "response", None)))
    state = ResponsesStreamState(**context)
    try:
        deadline, read_idle = _stream_limits()
        iterator = response.__aiter__()
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise LogicalCallDeadlineExceeded(
                    "Responses exceeded its absolute wall deadline"
                )
            timeout = min(deadline - now, read_idle)
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return state.finish()
            except asyncio.TimeoutError as exc:
                if time.monotonic() >= deadline:
                    raise LogicalCallDeadlineExceeded(
                        "Responses exceeded its absolute wall deadline"
                    ) from exc
                raise ResponsesReadIdleTimeout(
                    "Responses exceeded its read-idle timeout"
                ) from exc
            event_type = _field(event, "type")
            if on_event is not None and isinstance(event_type, str):
                on_event(event_type)
            state.consume(event)
            if state.completed:
                return state.finish()
    except BaseException as exc:
        _annotate_exception(exc, context, phase="stream_read", state=state)
        raise
    finally:
        close = getattr(response, "aclose", None) or getattr(response, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=0.05)
            except asyncio.TimeoutError:
                logger.warning("RESPONSES_STREAM_CLOSE_PENDING")
            except BaseException as exc:
                logger.warning(
                    "RESPONSES_STREAM_CLOSE_FAILED error_type=%s",
                    type(exc).__name__,
                )
