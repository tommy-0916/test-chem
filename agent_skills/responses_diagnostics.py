"""Small, allowlisted and redacted diagnostics for Responses failures.

Never serialize a response/event wholesale: prompts, output, tool arguments and
headers are deliberately outside this module's diagnostic contract. Redaction
is defense in depth, not an invitation to attach arbitrary request payloads.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_ATTRIBUTE = "responses_diagnostics"
_REDACTED = "[REDACTED]"
_STRING_FIELDS = (
    "event_type", "response_id", "request_id", "response_status",
    "incomplete_reason", "model", "phase",
)
_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
    "cached_tokens",
)
_RESPONSE_EVENT_TYPES = {
    "error",
    "response.audio.delta",
    "response.audio.done",
    "response.audio.transcript.delta",
    "response.audio.transcript.done",
    "response.cancelled",
    "response.code_interpreter_call.code.delta",
    "response.code_interpreter_call.code.done",
    "response.code_interpreter_call.completed",
    "response.code_interpreter_call.in_progress",
    "response.code_interpreter_call.interpreting",
    "response.completed",
    "response.content_part.added",
    "response.content_part.done",
    "response.created",
    "response.error",
    "response.failed",
    "response.file_search_call.completed",
    "response.file_search_call.in_progress",
    "response.file_search_call.searching",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.in_progress",
    "response.incomplete",
    "response.output_item.added",
    "response.output_item.done",
    "response.output_text.annotation.added",
    "response.output_text.delta",
    "response.output_text.done",
    "response.queued",
    "response.reasoning_text.delta",
    "response.reasoning_text.done",
    "response.refusal.delta",
    "response.refusal.done",
    "response.web_search_call.completed",
    "response.web_search_call.in_progress",
    "response.web_search_call.searching",
}
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;\"'<>]+", re.IGNORECASE)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]+", re.IGNORECASE)
_ASSIGNMENT_RE = re.compile(
    r"(?P<label>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|password|passwd|secret|authorization|auth)\b[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\}\]]+)",
    re.IGNORECASE,
)


def _field(value: Any, name: str) -> Any:
    """Read a single known SDK field without serializing or stringifying it."""
    try:
        return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
    except Exception:
        # Diagnostics must not replace an original provider/transport failure.
        return None


def _redact_url(match: re.Match[str]) -> str:
    try:
        parsed = urlsplit(match.group(0))
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        if "@" in parsed.netloc:
            netloc = _REDACTED + "@" + netloc
        return urlunsplit((
            parsed.scheme, netloc, parsed.path,
            _REDACTED if parsed.query else "",
            _REDACTED if parsed.fragment else "",
        ))
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def _safe_text(value: Any, *, limit: int = 256, api_key: Any = None) -> str | None:
    if not isinstance(value, str):
        return None
    # Redact before truncation, including explicitly supplied keys that do not
    # follow a known provider prefix. Never read credentials from the environment.
    if isinstance(api_key, str) and api_key:
        value = value.replace(api_key, _REDACTED)
    value = _URL_RE.sub(_redact_url, value)
    value = _BEARER_RE.sub("Bearer " + _REDACTED, value)
    value = _SK_RE.sub(_REDACTED, value)
    value = _ASSIGNMENT_RE.sub(lambda match: match.group("label") + _REDACTED, value)
    value = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in value
    )
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def safe_response_event_type(value: Any) -> str:
    """Return a fixed Responses event label for metadata-only telemetry."""

    return value if isinstance(value, str) and value in _RESPONSE_EVENT_TYPES else "response.unknown"


def _nonnegative_int(value: Any) -> int | None:
    # bool is an int subclass; floats/strings and implausibly huge integers are
    # not accepted as trustworthy token counts or formatter-safe metadata.
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _safe_copy(source: Any, *, api_key: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": 1}
    for name in _STRING_FIELDS:
        raw_value = _field(source, name)
        value = (
            safe_response_event_type(raw_value)
            if name == "event_type" and isinstance(raw_value, str)
            else _safe_text(raw_value, api_key=api_key)
        )
        if value is not None:
            result[name] = value
    for name in ("max_output_tokens", "http_status"):
        value = _nonnegative_int(_field(source, name))
        if value is not None and (name != "http_status" or 100 <= value <= 599):
            result[name] = value
    error = _field(source, "error")
    safe_error: dict[str, str] = {}
    for name in ("code", "message", "type", "param"):
        value = _safe_text(_field(error, name), limit=1000 if name == "message" else 256, api_key=api_key)
        if value is not None:
            safe_error[name] = value
    if safe_error:
        result["error"] = safe_error
    usage = _field(source, "usage")
    safe_usage = {}
    for name in _USAGE_FIELDS:
        value = _nonnegative_int(_field(usage, name))
        if value is not None:
            safe_usage[name] = value
    if safe_usage:
        result["usage"] = safe_usage
    return result


def build_response_diagnostics(
    *,
    response: Any = None,
    event: Any = None,
    request_id: Any = None,
    http_status: Any = None,
    phase: Any = None,
    model: Any = None,
    max_output_tokens: Any = None,
    api_key: Any = None,
) -> dict[str, Any]:
    """Extract only known fields from SDK objects or mappings; never infer data."""
    if response is None:
        response = _field(event, "response")
    error = _field(response, "error")
    if error is None:
        error = _field(event, "error")
    event_type = _field(event, "type")
    if error is None and isinstance(event_type, str) and event_type in {"error", "response.error"}:
        # SDK ErrorEvent keeps these fields directly on the event. Its ``type``
        # denotes the event, not an independently supplied provider error type.
        error = {name: _field(event, name) for name in ("code", "message", "param")}
    usage = _field(response, "usage")
    usage_fields = {name: _field(usage, name) for name in _USAGE_FIELDS[:3]}
    usage_fields["reasoning_tokens"] = _field(_field(usage, "output_tokens_details"), "reasoning_tokens")
    usage_fields["cached_tokens"] = _field(_field(usage, "input_tokens_details"), "cached_tokens")
    if request_id is None:
        request_id = _field(response, "_request_id")
    if request_id is None:
        request_id = _field(response, "request_id")
    if request_id is None:
        request_id = _field(event, "request_id")
    return _safe_copy({
        "event_type": event_type,
        "response_id": _field(response, "id"),
        "request_id": request_id,
        "response_status": _field(response, "status"),
        "error": error,
        "incomplete_reason": _field(_field(response, "incomplete_details"), "reason"),
        "usage": usage_fields,
        "model": model if model is not None else _field(response, "model"),
        "max_output_tokens": max_output_tokens if max_output_tokens is not None else _field(response, "max_output_tokens"),
        "phase": phase,
        "http_status": http_status,
    }, api_key=api_key)


def attach_responses_diagnostics(exc: BaseException, diagnostics: Any) -> None:
    """Attach a separately owned, sanitized allowlist without altering its type."""
    try:
        setattr(exc, _ATTRIBUTE, _safe_copy(diagnostics))
    except Exception:
        # An unusual immutable exception must not hide the original failure.
        return


def get_responses_diagnostics(exc: BaseException) -> dict[str, Any] | None:
    """Find diagnostics on self/cause/context, cycle-safe and at most 8 nodes."""
    pending = [exc]
    visited: set[int] = set()
    while pending and len(visited) < 8:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in visited:
            continue
        visited.add(id(current))
        diagnostic = _field(current, _ATTRIBUTE)
        if isinstance(diagnostic, Mapping):
            return _safe_copy(diagnostic)
        for next_error in (_field(current, "__cause__"), _field(current, "__context__")):
            if isinstance(next_error, BaseException):
                pending.append(next_error)
    return None


def format_responses_failure(exc: BaseException) -> str | None:
    """Return one bounded, safe line, never the exception's arbitrary message."""
    diagnostic = get_responses_diagnostics(exc)
    if diagnostic is None:
        return None
    text = "Responses failure: " + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= 4096 else text[:4093] + "..."
