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
- transient upstream failures (connection errors, stalls, and HTTP
  408/409/425/429/500/502/503/504) are retried inside the gateway with
  backoff, so one client call is carried to a completed response whenever
  the upstream can produce one. Deterministic rejections (400/401/403 and
  other 4xx) are forwarded immediately without retry.

No other request or response content is modified; the Authorization header
is forwarded verbatim and no secrets are stored here. Only transport-level
metadata (byte counts, status, token usage) is logged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_URL = "https://api.kimi.com/coding/v1/chat/completions"
UPSTREAM_TIMEOUT_SECONDS = 300
MIN_MAX_TOKENS = 32768
REASONING_EFFORT = "high"
STRIPPED_FIELDS = ("thinking", "do_sample")
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
QUOTA_STATUS = {401, 403}
MAX_UPSTREAM_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = [2, 5, 10]
QUOTA_BACKOFF_SECONDS = [20, 40, 60]


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


def consume_stream(response) -> dict:
    """Aggregate SSE chat.completion.chunk frames into one response object."""
    choices: dict[int, dict] = {}
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
            break
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise StreamAggregationError(f"undecodable SSE chunk: {data[:200]}") from exc
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("error"), dict):
            raise StreamAggregationError(
                f"upstream stream error: {str(chunk['error'])[:300]}"
            )
        response_id = response_id or str(chunk.get("id") or "")
        created = created or int(chunk.get("created") or 0)
        model = model or str(chunk.get("model") or "")
        fingerprint = str(chunk.get("system_fingerprint") or fingerprint)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = int(choice.get("index") or 0)
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
                tc_index = int(tool_call.get("index") or 0)
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
                "finish_reason": slot["finish_reason"] or "stop",
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
                request, timeout=UPSTREAM_TIMEOUT_SECONDS
            ) as response:
                status = response.status
                if status == 200:
                    aggregated = consume_stream(response)
                    raw = json.dumps(aggregated, ensure_ascii=False).encode("utf-8")
                else:
                    raw = response.read()
                return status, raw, None
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return exc.code, exc.read(), retry_after
        except (urllib.error.URLError, TimeoutError, OSError, StreamAggregationError) as exc:
            raw = json.dumps(
                {"error": {"message": f"gateway upstream error: {exc}"}},
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
        status = 0
        raw = b""
        for attempt in range(1, MAX_UPSTREAM_ATTEMPTS + 1):
            attempt_started = time.monotonic()
            status, raw, retry_after = self._upstream_once(upstream_body, authorization)
            log(
                f"POST {path} attempt {attempt}/{MAX_UPSTREAM_ATTEMPTS} -> {status} "
                f"in {time.monotonic() - attempt_started:.1f}s req={len(upstream_body)}B"
            )
            if status == 200 or (
                status not in RETRYABLE_STATUS and status not in QUOTA_STATUS
            ):
                break
            if attempt < MAX_UPSTREAM_ATTEMPTS:
                if status in QUOTA_STATUS:
                    delay = QUOTA_BACKOFF_SECONDS[min(attempt - 1, len(QUOTA_BACKOFF_SECONDS) - 1)]
                else:
                    delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                if retry_after:
                    try:
                        delay = max(delay, min(float(retry_after), 60.0))
                    except ValueError:
                        pass
                log(f"retrying upstream in {delay:.0f}s (status {status})")
                time.sleep(delay)

        elapsed = time.monotonic() - started
        usage_note = ""
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            usage = parsed.get("usage") if isinstance(parsed, dict) else None
            if isinstance(usage, dict):
                usage_note = (
                    f" prompt_tokens={usage.get('prompt_tokens')}"
                    f" completion_tokens={usage.get('completion_tokens')}"
                )
        except ValueError:
            pass
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
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
