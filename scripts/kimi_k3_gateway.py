#!/usr/bin/env python3
"""Local OpenAI-compatible gateway adapting chem-agent chat payloads to Kimi k3.

Harness-side transport adapter for the chem-agent-eval suite. It forwards
POST /v1/chat/completions to the Kimi coding endpoint after applying the
wire constraints that Kimi k3 mandates:

- ``temperature`` is forced to 1 (k3 in thinking mode rejects any other
  value, while both chem-agent layers default to temperature 0);
- qwen-style ``thinking`` / ``do_sample`` fields are stripped so k3 stays in
  thinking mode (the device agent's disable-thinking extra_body would
  otherwise switch k3 to its non-thinking variant, which in turn demands
  temperature 0.6);
- ``reasoning_effort`` is set to ``high`` (the chat code paths do not plumb
  the repository's REFINER_LLM_REASONING_EFFORT setting through);
- ``max_tokens`` is raised to at least 32768 so k3's reasoning tokens do not
  truncate the JSON content the agents must emit;
- the upstream call is made with ``stream: true`` and the SSE chunks are
  aggregated back into an ordinary non-streaming response. Kimi's edge
  proxy closes any connection that produces no bytes for ~300s, which made
  every prompt above ~100KB fail with 504 in high-thinking mode; streaming
  keeps bytes flowing for the whole generation;
- upstream failures are returned after one request. Chem Agent's shared
  transport layer owns retries and waits exactly 10 seconds, so this proxy
  cannot multiply attempts or apply an upstream ``Retry-After`` delay.

Successful request and response content is otherwise preserved. Provider
failure bodies are reduced to an allowlisted classification, and malformed
stream metadata is rejected without echoing provider-controlled values. The
Authorization header is forwarded verbatim and no secrets are stored here.
Only bounded transport-level metadata (byte counts, status, integer token
usage) is logged.
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_URL = "https://api.kimi.com/coding/v1/chat/completions"
UPSTREAM_TIMEOUT_SECONDS = 300
# Socket read-idle remains 300s. The independent total deadline must be at
# least as large as Device's supported long-generation budget (600s), or an
# otherwise healthy byte-active stream is cut off halfway through reasoning.
UPSTREAM_WALL_TIMEOUT_SECONDS = 600
MIN_MAX_TOKENS = 32768
REASONING_EFFORT = "high"
STRIPPED_FIELDS = ("thinking", "do_sample")
MAX_UPSTREAM_ATTEMPTS = 1
SUCCESS_FINISH_REASONS = {"stop", "tool_calls", "function_call"}
MAX_SAFE_METADATA_INTEGER = (1 << 63) - 1
SAFE_UPSTREAM_ERROR_CODES = {
    "access_terminated_error",
    "authentication_error",
    "gateway_error",
    "insufficient_quota",
    "internal_error",
    "invalid_api_key",
    "overloaded",
    "permission_denied",
    "rate_limit_error",
    "rate_limit_exceeded",
    "server_error",
    "service_unavailable",
    "upstream_error",
}
SAFE_UPSTREAM_ERROR_TYPES = SAFE_UPSTREAM_ERROR_CODES | {
    "api_error",
    "http_error",
    "invalid_request_error",
}


def adapt_payload(body: bytes) -> bytes:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        return body
    payload["temperature"] = 1
    for field in STRIPPED_FIELDS:
        payload.pop(field, None)
    payload["reasoning_effort"] = REASONING_EFFORT
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, (int, float)) and max_tokens < MIN_MAX_TOKENS:
        payload["max_tokens"] = MIN_MAX_TOKENS
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


class StreamAggregationError(RuntimeError):
    pass


def _bounded_nonnegative_int(value, field: str, *, default: int = 0) -> int:
    """Accept only JSON integers that are safe to retain as metadata."""
    if value is None:
        return default
    # bool is an int subclass in Python, but is never valid numeric metadata.
    if type(value) is not int or not 0 <= value <= MAX_SAFE_METADATA_INTEGER:
        raise StreamAggregationError(f"invalid {field} metadata")
    return value


def _usage_log_note(raw: bytes) -> str:
    """Return a bounded, injection-safe token-usage suffix for gateway logs."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    usage = parsed.get("usage") if isinstance(parsed, dict) else None
    if not isinstance(usage, dict):
        return ""

    fields = []
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name)
        if type(value) is int and 0 <= value <= MAX_SAFE_METADATA_INTEGER:
            fields.append(f"{name}={value}")
    return " " + " ".join(fields) if fields else ""


def sanitize_upstream_http_error(status: int, raw: bytes) -> bytes:
    """Preserve retry classification without echoing provider-controlled text."""

    def safe_symbol(value, fallback: str, allowlist: set[str]) -> str:
        if isinstance(value, str) and value in allowlist:
            return value
        return fallback

    code = "upstream_http_error"
    error_type = "http_error"
    try:
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict):
            error = payload.get("error")
            source = error if isinstance(error, dict) else payload
            code = safe_symbol(
                source.get("code"), code, SAFE_UPSTREAM_ERROR_CODES
            )
            error_type = safe_symbol(
                source.get("type"), error_type, SAFE_UPSTREAM_ERROR_TYPES
            )
    except (UnicodeDecodeError, ValueError):
        pass
    return json.dumps(
        {
            "error": {
                "code": code,
                "type": error_type,
                "message": f"upstream returned HTTP {int(status)}",
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")


def consume_stream(response) -> dict:
    """Aggregate SSE chat.completion.chunk frames into one response object."""
    choices: dict[int, dict] = {}
    saw_done = False
    usage: dict | None = None
    response_id = ""
    created = 0
    model = ""
    fingerprint = ""
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            saw_done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise StreamAggregationError("undecodable SSE chunk") from exc
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("error"), dict):
            raise StreamAggregationError("upstream stream error")
        response_id = response_id or str(chunk.get("id") or "")
        chunk_created = _bounded_nonnegative_int(
            chunk.get("created"), "created", default=0
        )
        created = created or chunk_created
        model = model or str(chunk.get("model") or "")
        fingerprint = str(chunk.get("system_fingerprint") or fingerprint)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = _bounded_nonnegative_int(
                choice.get("index"), "choice index", default=0
            )
            slot = choices.setdefault(
                index,
                {
                    "role": "assistant",
                    "content": [],
                    "reasoning": [],
                    "tool_calls": {},
                    "finish_reason": None,
                },
            )
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
            if delta.get("role"):
                slot["role"] = str(delta["role"])
            if isinstance(delta.get("content"), str):
                slot["content"].append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                slot["reasoning"].append(delta["reasoning_content"])
            for tool_call in delta.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                tc_index = _bounded_nonnegative_int(
                    tool_call.get("index"), "tool-call index", default=0
                )
                stored = slot["tool_calls"].setdefault(
                    tc_index,
                    {"id": "", "type": "function",
                     "function": {"name": "", "arguments": ""}},
                )
                if tool_call.get("id"):
                    stored["id"] = str(tool_call["id"])
                if tool_call.get("type"):
                    stored["type"] = str(tool_call["type"])
                function = tool_call.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        stored["function"]["name"] += str(function["name"])
                    if function.get("arguments"):
                        stored["function"]["arguments"] += str(function["arguments"])
            if isinstance(choice.get("usage"), dict):
                usage = choice["usage"]
            if choice.get("finish_reason"):
                slot["finish_reason"] = str(choice["finish_reason"])

    if not choices:
        raise StreamAggregationError("stream ended without any choice chunks")
    if not saw_done:
        raise StreamAggregationError("stream ended before the [DONE] sentinel")
    unfinished = [
        index for index, slot in choices.items()
        if not slot.get("finish_reason")
    ]
    if unfinished:
        raise StreamAggregationError(
            "stream ended without a terminal finish_reason for choices: "
            + ",".join(str(index) for index in sorted(unfinished))
        )
    rejected = {
        index: slot["finish_reason"]
        for index, slot in choices.items()
        if slot.get("finish_reason") not in SUCCESS_FINISH_REASONS
    }
    if rejected:
        raise StreamAggregationError(
            "stream ended with a non-success finish_reason for choices: "
            + ",".join(
                f"{index}={reason}" for index, reason in sorted(rejected.items())
            )
        )

    result_choices = []
    for index in sorted(choices):
        slot = choices[index]
        message: dict = {
            "role": slot["role"],
            "content": "".join(slot["content"]),
        }
        if slot["reasoning"]:
            message["reasoning_content"] = "".join(slot["reasoning"])
        if slot["tool_calls"]:
            message["tool_calls"] = [
                slot["tool_calls"][key] for key in sorted(slot["tool_calls"])
            ]
        result_choices.append(
            {
                "index": index,
                "message": message,
                "finish_reason": slot["finish_reason"],
            }
        )

    result: dict = {
        "id": response_id or "chatcmpl-gateway",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model or "k3",
        "choices": result_choices,
    }
    if usage:
        result["usage"] = usage
    if fingerprint:
        result["system_fingerprint"] = fingerprint
    return result


def consume_stream_bounded(response, wall_timeout_seconds: float) -> dict:
    """Preempt even a byte-active stream that never reaches a terminal."""
    if wall_timeout_seconds <= 0:
        raise ValueError("wall_timeout_seconds must be positive")
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, consume_stream(response)))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(
        target=run,
        name="kimi-gateway-stream-reader",
        daemon=True,
    ).start()
    try:
        ok, value = result.get(timeout=wall_timeout_seconds)
    except queue.Empty:
        close = getattr(response, "close", None)
        if callable(close):
            threading.Thread(
                target=close,
                name="kimi-gateway-stream-close",
                daemon=True,
            ).start()
        raise StreamAggregationError(
            "upstream stream exceeded its absolute wall deadline"
        ) from None
    if not ok:
        raise value
    return value


def read_body_bounded(response, wall_timeout_seconds: float) -> bytes:
    """Read a small error body without escaping the request's wall deadline."""
    if wall_timeout_seconds <= 0:
        raise StreamAggregationError("upstream request exhausted its wall deadline")
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, response.read()))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(
        target=run,
        name="kimi-gateway-error-reader",
        daemon=True,
    ).start()
    try:
        ok, value = result.get(timeout=wall_timeout_seconds)
    except queue.Empty:
        close = getattr(response, "close", None)
        if callable(close):
            threading.Thread(
                target=close,
                name="kimi-gateway-error-close",
                daemon=True,
            ).start()
        raise StreamAggregationError(
            "upstream error body exceeded its absolute wall deadline"
        ) from None
    if not ok:
        raise value
    return value if isinstance(value, bytes) else bytes(value)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib name
        return

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if self.path.rstrip("/") in {"/v1/models", "/models"}:
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "k3", "object": "model", "owned_by": "kimi"}],
                },
            )
            return
        self._send_json(404, {"error": {"message": f"unsupported path: {self.path}"}})

    def _upstream_once(self, upstream_body: bytes, authorization: str | None):
        """Single upstream attempt. Returns (status, raw_body, retry_after)."""
        wall_deadline = time.monotonic() + UPSTREAM_WALL_TIMEOUT_SECONDS

        def remaining_wall() -> float:
            remaining = wall_deadline - time.monotonic()
            if remaining <= 0:
                raise StreamAggregationError(
                    "upstream request exceeded its absolute wall deadline"
                )
            return remaining

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(
            UPSTREAM_URL,
            data=upstream_body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=min(
                    UPSTREAM_TIMEOUT_SECONDS, remaining_wall(),
                ),
            ) as response:
                status = response.status
                if status == 200:
                    aggregated = consume_stream_bounded(
                        response, remaining_wall(),
                    )
                    raw = json.dumps(aggregated, ensure_ascii=False).encode("utf-8")
                else:
                    raw = sanitize_upstream_http_error(
                        status, read_body_bounded(response, remaining_wall())
                    )
                return status, raw, None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                error_body = read_body_bounded(exc, remaining_wall())
            except StreamAggregationError:
                error_body = b""
            return (
                exc.code,
                sanitize_upstream_http_error(exc.code, error_body),
                retry_after,
            )
        except (urllib.error.URLError, TimeoutError, OSError, StreamAggregationError) as exc:
            raw = json.dumps(
                {
                    "error": {
                        "code": "gateway_upstream_error",
                        "message": "gateway upstream request failed",
                        "error_type": type(exc).__name__,
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")
            return 502, raw, None

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in {"/v1/chat/completions", "/chat/completions"}:
            self._send_json(404, {"error": {"message": f"unsupported path: {self.path}"}})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            upstream_body = adapt_payload(body)
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": {"message": f"invalid JSON payload: {exc}"}})
            return

        authorization = self.headers.get("Authorization")

        started = time.monotonic()
        attempt_started = time.monotonic()
        status, raw, _retry_after = self._upstream_once(
            upstream_body, authorization
        )
        log(
            f"POST {path} attempt 1/{MAX_UPSTREAM_ATTEMPTS} -> {status} "
            f"in {time.monotonic() - attempt_started:.1f}s req={len(upstream_body)}B"
        )

        elapsed = time.monotonic() - started
        usage_note = _usage_log_note(raw)
        log(
            f"POST {path} -> {status} in {elapsed:.1f}s "
            f"req={len(upstream_body)}B resp={len(raw)}B{usage_note}"
        )

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    global UPSTREAM_WALL_TIMEOUT_SECONDS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument(
        "--upstream-wall-timeout",
        type=float,
        default=UPSTREAM_WALL_TIMEOUT_SECONDS,
        help=(
            "absolute seconds allowed for one byte-active upstream stream "
            "(default: 600; socket read-idle remains 300)"
        ),
    )
    args = parser.parse_args()
    if (
        not math.isfinite(args.upstream_wall_timeout)
        or args.upstream_wall_timeout <= 0
    ):
        parser.error("--upstream-wall-timeout must be finite and positive")
    UPSTREAM_WALL_TIMEOUT_SECONDS = args.upstream_wall_timeout
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.daemon_threads = True
    log(f"kimi k3 gateway listening on http://{args.host}:{args.port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
