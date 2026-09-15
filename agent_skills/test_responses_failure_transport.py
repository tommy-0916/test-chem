"""Exercise failure diagnostics through the real SDK with mock HTTP only."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import APIError, APIStatusError, AsyncOpenAI, OpenAI

from agent_skills.responses_stream import (
    ResponsesProtocolError,
    ResponsesTerminalError,
    get_responses_diagnostics,
    invoke_responses,
    invoke_responses_async,
)


FAKE_KEY = "unit-diagnostic-secret-not-an-sk-key"
BEARER_SECRET = "sk-unit-bearer-secret-abcdef0123456789"
INPUT_SECRET = "private-input-sentinel"
OUTPUT_SECRET = "private-generated-output-sentinel"
TOOL_SECRET = "private-tool-schema-sentinel"
HEADER_SECRET = "private-header-sentinel"
ALLOWED_KEYS = {
    "schema_version", "event_type", "response_id", "request_id", "response_status", "error",
    "incomplete_reason", "usage", "model", "max_output_tokens", "http_status", "phase",
}
ALLOWED_ERROR_KEYS = {"code", "message", "type", "param"}
ALLOWED_USAGE_KEYS = {
    "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_tokens",
}


def payload():
    return {
        "model": "mock-model", "input": INPUT_SECRET, "store": False,
        "max_output_tokens": 32768,
        "tools": [{"type": "function", "name": "lookup", "description": TOOL_SECRET,
                   "parameters": {"type": "object", "properties": {}}}],
    }


def provider_response(status="failed", **updates):
    return {
        "id": "resp_failure", "object": "response", "created_at": 0,
        "status": status, "model": "mock-model", "max_output_tokens": 32768,
        "output": [{"type": "message", "role": "assistant", "status": "incomplete",
                    "content": [{"type": "output_text", "text": OUTPUT_SECRET,
                                 "annotations": []}]}],
        "input": INPUT_SECRET, "tools": [{"name": TOOL_SECRET}],
        "headers": {"x-private": HEADER_SECRET},
        "error": {
            "code": "server_error", "type": "upstream_error", "param": "max_output_tokens",
            "message": f"Upstream stopped; key={FAKE_KEY}; Authorization: Bearer {BEARER_SECRET}",
            "debug_payload": INPUT_SECRET,
        },
        "usage": {"input_tokens": 21, "output_tokens": 13, "total_tokens": 34,
                  "input_tokens_details": {"cached_tokens": 8, "private": INPUT_SECRET},
                  "output_tokens_details": {"reasoning_tokens": 5, "private": OUTPUT_SECRET},
                  "extra": TOOL_SECRET},
        **updates,
    }


def event(kind, response=None, **updates):
    value = {"type": kind, "sequence_number": 1, **updates}
    if response is not None:
        value["response"] = response
    return value


def sse(*events, done=True):
    text = "".join("data: " + json.dumps(value) + "\n\n" for value in events)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


def stream_headers(request_id="req_mock"):
    return {"content-type": "text/event-stream", "x-request-id": request_id,
            "x-private": HEADER_SECRET, "authorization": "Bearer " + BEARER_SECRET}


class DiagnosticAssertions:
    def install_guards(self):
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("Live networking is prohibited in this test"))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def assert_safe(self, exc):
        diagnostic = get_responses_diagnostics(exc)
        self.assertIsInstance(diagnostic, dict)
        self.assertEqual(diagnostic["schema_version"], 1)
        self.assertTrue(set(diagnostic).issubset(ALLOWED_KEYS), set(diagnostic) - ALLOWED_KEYS)
        if "error" in diagnostic:
            self.assertTrue(set(diagnostic["error"]).issubset(ALLOWED_ERROR_KEYS))
        if "usage" in diagnostic:
            self.assertTrue(set(diagnostic["usage"]).issubset(ALLOWED_USAGE_KEYS))
        rendered = json.dumps(diagnostic)
        for secret in (FAKE_KEY, BEARER_SECRET, INPUT_SECRET, OUTPUT_SECRET, TOOL_SECRET, HEADER_SECRET):
            self.assertNotIn(secret, rendered)
        self.assertIsInstance(diagnostic.get("phase"), str)
        self.assertTrue(diagnostic["phase"])
        return diagnostic


class FailureDiagnosticTransportTests(DiagnosticAssertions, unittest.TestCase):
    def setUp(self):
        self.install_guards()

    def invoke_mock(self, handler):
        with OpenAI(api_key=FAKE_KEY, base_url="https://diagnostics.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            return invoke_responses(client, payload())

    def test_getter_without_diagnostics_returns_none(self):
        self.assertIsNone(get_responses_diagnostics(RuntimeError("ordinary error")))

    def test_api_error_with_complete_failed_event_body_keeps_nested_metadata(self):
        failure = APIError(
            "Provider returned a terminal event",
            request=httpx.Request("POST", "https://diagnostics.invalid/v1/responses"),
            body=event("response.failed", provider_response(), request_id="req_complete_body"),
        )
        client = SimpleNamespace(api_key=FAKE_KEY, responses=SimpleNamespace(create=Mock(side_effect=failure)))
        with self.assertRaises(APIError) as caught:
            invoke_responses(client, payload())
        self.assertIs(caught.exception, failure)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["event_type"], "response.failed")
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["request_id"], "req_complete_body")
        self.assertEqual(diagnostic["response_status"], "failed")
        self.assertEqual(diagnostic["error"]["code"], "server_error")
        self.assertEqual(diagnostic["error"]["type"], "upstream_error")
        self.assertEqual(diagnostic["usage"], {
            "input_tokens": 21, "output_tokens": 13, "total_tokens": 34,
            "reasoning_tokens": 5, "cached_tokens": 8,
        })

    def test_api_error_with_flat_event_body_does_not_invent_error_type(self):
        failure = APIError(
            "Provider returned a flat error event",
            request=httpx.Request("POST", "https://diagnostics.invalid/v1/responses"),
            body=event("error", request_id="req_flat_body", code="provider_busy",
                       message="Capacity temporarily exhausted", param="model", private=INPUT_SECRET),
        )
        client = SimpleNamespace(api_key=FAKE_KEY, responses=SimpleNamespace(create=Mock(side_effect=failure)))
        with self.assertRaises(APIError) as caught:
            invoke_responses(client, payload())
        self.assertIs(caught.exception, failure)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["event_type"], "error")
        self.assertEqual(diagnostic["request_id"], "req_flat_body")
        self.assertEqual(diagnostic["error"]["code"], "provider_busy")
        self.assertEqual(diagnostic["error"]["param"], "model")
        self.assertNotIn("type", diagnostic["error"])

    def test_api_error_with_error_object_or_complete_response_body_keeps_context(self):
        cases = (
            ("error_object", {"code": "upstream_unavailable", "type": "server_error",
                              "message": "Provider temporarily unavailable", "request_id": "req_error_object",
                              "private": INPUT_SECRET},
             {"request_id": "req_error_object", "error_code": "upstream_unavailable",
              "error_type": "server_error"}),
            ("complete_response", provider_response(request_id="req_response_object"),
             {"request_id": "req_response_object", "error_code": "server_error",
              "error_type": "upstream_error", "response_id": "resp_failure", "response_status": "failed"}),
        )
        for shape, body, expected in cases:
            with self.subTest(shape=shape):
                failure = APIError(
                    "Provider returned structured failure metadata",
                    request=httpx.Request("POST", "https://diagnostics.invalid/v1/responses"), body=body,
                )
                client = SimpleNamespace(api_key=FAKE_KEY,
                                         responses=SimpleNamespace(create=Mock(side_effect=failure)))
                with self.assertRaises(APIError) as caught:
                    invoke_responses(client, payload())
                self.assertIs(caught.exception, failure)
                diagnostic = self.assert_safe(caught.exception)
                self.assertEqual(diagnostic["request_id"], expected["request_id"])
                self.assertEqual(diagnostic["error"]["code"], expected["error_code"])
                self.assertEqual(diagnostic["error"]["type"], expected["error_type"])
                self.assertNotIn("event_type", diagnostic)
                if shape == "complete_response":
                    self.assertEqual(diagnostic["response_id"], expected["response_id"])
                    self.assertEqual(diagnostic["response_status"], expected["response_status"])
                    self.assertEqual(diagnostic["usage"], {
                        "input_tokens": 21, "output_tokens": 13, "total_tokens": 34,
                        "reasoning_tokens": 5, "cached_tokens": 8,
                    })

    def test_malformed_exception_metadata_cannot_mask_original_failure(self):
        class UnreadableBodyError(RuntimeError):
            @property
            def body(self):
                raise ValueError("Broken body metadata accessor")

        class UnreadableHeaders:
            @property
            def headers(self):
                raise ValueError("Broken headers metadata accessor")

        headers_failure = RuntimeError("Original provider failure with unusual response")
        headers_failure.response = UnreadableHeaders()
        for failure in (UnreadableBodyError("Original provider failure with unusual body"), headers_failure):
            with self.subTest(metadata=type(failure).__name__):
                client = SimpleNamespace(api_key=FAKE_KEY,
                                         responses=SimpleNamespace(create=Mock(side_effect=failure)))
                with self.assertRaises(RuntimeError) as caught:
                    invoke_responses(client, payload())
                self.assertIs(caught.exception, failure)

    def test_failed_event_retains_provider_code_usage_ids_without_sensitive_values(self):
        body = sse(event("response.failed", provider_response()))

        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer " + FAKE_KEY)
            self.assertTrue(json.loads(request.content)["stream"])
            return httpx.Response(200, headers=stream_headers(), content=body)

        with self.assertRaises(ResponsesTerminalError) as caught:
            self.invoke_mock(handler)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["event_type"], "response.failed")
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["request_id"], "req_mock")
        self.assertEqual(diagnostic["response_status"], "failed")
        self.assertEqual(diagnostic["http_status"], 200)
        self.assertEqual(diagnostic["model"], "mock-model")
        self.assertEqual(diagnostic["max_output_tokens"], 32768)
        self.assertEqual(diagnostic["error"]["code"], "server_error")
        self.assertEqual(diagnostic["error"]["type"], "upstream_error")
        self.assertEqual(diagnostic["error"]["param"], "max_output_tokens")
        self.assertIn("Upstream stopped", diagnostic["error"]["message"])
        self.assertEqual(diagnostic["usage"], {
            "input_tokens": 21, "output_tokens": 13, "total_tokens": 34,
            "reasoning_tokens": 5, "cached_tokens": 8,
        })

    def test_incomplete_event_exposes_reason(self):
        result = provider_response("incomplete", error=None,
                                   incomplete_details={"reason": "max_output_tokens", "private": INPUT_SECRET})
        with self.assertRaises(ResponsesTerminalError) as caught:
            self.invoke_mock(lambda _: httpx.Response(200, headers=stream_headers(),
                                                      content=sse(event("response.incomplete", result))))
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["incomplete_reason"], "max_output_tokens")
        self.assertEqual(diagnostic["response_status"], "incomplete")

    def test_enveloped_error_intercepted_by_sdk_still_has_diagnostics(self):
        # The SDK raises APIError before yielding an event with a top-level error.
        body = sse(event("response.created", provider_response("in_progress", error=None)),
                   event("error", error={"code": "upstream_unavailable", "type": "server_error",
                                         "message": "Temporary provider outage", "private": INPUT_SECRET}))
        with self.assertRaises(APIError) as caught:
            self.invoke_mock(lambda _: httpx.Response(200, headers=stream_headers(), content=body))
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["error"]["code"], "upstream_unavailable")
        self.assertEqual(diagnostic["request_id"], "req_mock")
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["http_status"], 200)

    def test_flat_error_event_is_not_lost(self):
        body = sse(event("error", code="invalid_request", message="Invalid option", param="temperature",
                         private=INPUT_SECRET))
        with self.assertRaises(ResponsesTerminalError) as caught:
            self.invoke_mock(lambda _: httpx.Response(200, headers=stream_headers(), content=body))
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["event_type"], "error")
        self.assertEqual(diagnostic["error"]["code"], "invalid_request")
        self.assertEqual(diagnostic["error"]["message"], "Invalid option")
        self.assertEqual(diagnostic["error"]["param"], "temperature")

    def test_nonstream_failed_retains_sdk_request_id_and_provider_error(self):
        def handler(request):
            self.assertFalse(json.loads(request.content)["stream"])
            return httpx.Response(200, headers={"x-request-id": "req_nonstream"}, json=provider_response())

        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "0"}):
            with self.assertRaises(ResponsesTerminalError) as caught:
                self.invoke_mock(handler)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["request_id"], "req_nonstream")
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["error"]["code"], "server_error")

    def test_http_400_and_429_preserve_status_exception_classes(self):
        for status, code in ((400, "invalid_request"), (429, "rate_limit_exceeded")):
            with self.subTest(status=status):
                body = {"error": {"code": code, "message": f"Request denied for {FAKE_KEY}",
                                  "type": "request_error", "param": "model", "debug": INPUT_SECRET},
                        "input": INPUT_SECRET, "output": OUTPUT_SECRET}
                with self.assertRaises(APIStatusError) as caught:
                    self.invoke_mock(lambda _: httpx.Response(status, headers={"x-request-id": "req_http"}, json=body))
                diagnostic = self.assert_safe(caught.exception)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(diagnostic["http_status"], status)
                self.assertEqual(diagnostic["request_id"], "req_http")
                self.assertEqual(diagnostic["error"]["code"], code)

    def test_midstream_real_read_timeout_preserves_identity_and_latest_ids(self):
        failure = httpx.ReadTimeout("mock idle timeout")

        class BrokenBytes(httpx.SyncByteStream):
            closed = False

            def __iter__(self):
                yield sse(event("response.created", provider_response("in_progress", error=None)), done=False)
                raise failure

            def close(self):
                self.closed = True

        stream = BrokenBytes()
        with self.assertRaises(httpx.ReadTimeout) as caught:
            self.invoke_mock(lambda _: httpx.Response(200, headers=stream_headers(), stream=stream))
        self.assertIs(caught.exception, failure)
        self.assertTrue(stream.closed)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["request_id"], "req_mock")
        self.assertEqual(diagnostic["response_status"], "in_progress")

    def test_eof_after_created_keeps_last_response_context(self):
        body = sse(event("response.created", provider_response("in_progress", error=None)), done=False)
        with self.assertRaises(ResponsesProtocolError) as caught:
            self.invoke_mock(lambda _: httpx.Response(200, headers=stream_headers(), content=body))
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["request_id"], "req_mock")
        self.assertEqual(diagnostic["response_status"], "in_progress")

    def test_diagnostic_getter_returns_independent_nested_copy(self):
        with self.assertRaises(ResponsesTerminalError) as caught:
            self.invoke_mock(lambda _: httpx.Response(200, headers=stream_headers(),
                                                      content=sse(event("response.failed", provider_response()))))
        first = self.assert_safe(caught.exception)
        first["error"]["message"] = "changed"
        first["usage"]["output_tokens"] = -1
        first["request_id"] = "changed"
        second = self.assert_safe(caught.exception)
        self.assertNotEqual(second["error"]["message"], "changed")
        self.assertEqual(second["usage"]["output_tokens"], 13)
        self.assertEqual(second["request_id"], "req_mock")

    def test_native_langchain_tool_path_preserves_failure_diagnostics(self):
        from langchain_core.messages import HumanMessage

        from agent_skills.native_tools import invoke_with_tools, make_native_openai_model

        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct",
                                     "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false"}):
            model = make_native_openai_model(model="mock-model", api_key=FAKE_KEY,
                                             base_url="https://diagnostics.invalid/v1", timeout=2,
                                             max_retries=0, use_responses_api=True)
            original_sync, original_async = model.root_client, model.root_async_client
            handler = lambda _: httpx.Response(200, headers=stream_headers("req_native"),
                                                content=sse(event("response.failed", provider_response())))
            try:
                with OpenAI(api_key=FAKE_KEY, base_url="https://diagnostics.invalid/v1", max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                    model.root_client = client
                    with self.assertRaises(ResponsesTerminalError) as caught:
                        invoke_with_tools(model, [HumanMessage(content=INPUT_SECRET)], [], max_rounds=0)
                diagnostic = self.assert_safe(caught.exception)
                self.assertEqual(diagnostic["request_id"], "req_native")
                self.assertEqual(diagnostic["error"]["code"], "server_error")
                self.assertEqual(diagnostic["response_id"], "resp_failure")
            finally:
                original_sync.close()
                asyncio.run(original_async.close())


class AsyncFailureDiagnosticTransportTests(DiagnosticAssertions, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.install_guards()

    async def invoke_mock(self, handler):
        async with AsyncOpenAI(api_key=FAKE_KEY, base_url="https://diagnostics.invalid/v1", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            return await invoke_responses_async(client, payload())

    async def test_async_failed_event_diagnostics(self):
        async def handler(_):
            return httpx.Response(200, headers=stream_headers("req_async"),
                                  content=sse(event("response.failed", provider_response())))

        with self.assertRaises(ResponsesTerminalError) as caught:
            await self.invoke_mock(handler)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["request_id"], "req_async")
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["event_type"], "response.failed")
        self.assertEqual(diagnostic["error"]["code"], "server_error")

    async def test_async_enveloped_sdk_error_keeps_stream_ids(self):
        async def handler(_):
            return httpx.Response(200, headers=stream_headers("req_async_error"),
                                  content=sse(event("response.created", provider_response("in_progress", error=None)),
                                              event("error", error={"code": "stream_error", "message": "Stream stopped"})))

        with self.assertRaises(APIError) as caught:
            await self.invoke_mock(handler)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["request_id"], "req_async_error")
        self.assertEqual(diagnostic["response_id"], "resp_failure")
        self.assertEqual(diagnostic["error"]["code"], "stream_error")

    async def test_async_http_error_preserves_api_status_error(self):
        async def handler(_):
            return httpx.Response(429, headers={"x-request-id": "req_async_http"},
                                  json={"error": {"code": "rate_limit_exceeded", "message": "Please retry later"}})

        with self.assertRaises(APIStatusError) as caught:
            await self.invoke_mock(handler)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(diagnostic["http_status"], 429)
        self.assertEqual(diagnostic["request_id"], "req_async_http")
        self.assertEqual(diagnostic["error"]["code"], "rate_limit_exceeded")

    async def test_async_midstream_real_read_timeout_keeps_identity_and_ids(self):
        failure = httpx.ReadTimeout("mock asynchronous idle timeout")

        class BrokenBytes(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield sse(event("response.created", provider_response("in_progress", error=None)), done=False)
                raise failure

            async def aclose(self):
                self.closed = True

        stream = BrokenBytes()

        async def handler(_):
            return httpx.Response(200, headers=stream_headers("req_async_timeout"), stream=stream)

        with self.assertRaises(httpx.ReadTimeout) as caught:
            await self.invoke_mock(handler)
        self.assertIs(caught.exception, failure)
        self.assertTrue(stream.closed)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["request_id"], "req_async_timeout")
        self.assertEqual(diagnostic["response_id"], "resp_failure")

    async def test_async_nonstream_incomplete_preserves_reason_and_request_id(self):
        async def handler(request):
            self.assertFalse(json.loads(request.content)["stream"])
            return httpx.Response(200, headers={"x-request-id": "req_async_nonstream"},
                                  json=provider_response("incomplete", error=None,
                                                         incomplete_details={"reason": "max_output_tokens"}))

        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "0"}):
            with self.assertRaises(ResponsesTerminalError) as caught:
                await self.invoke_mock(handler)
        diagnostic = self.assert_safe(caught.exception)
        self.assertEqual(diagnostic["request_id"], "req_async_nonstream")
        self.assertEqual(diagnostic["incomplete_reason"], "max_output_tokens")
        self.assertEqual(diagnostic["response_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
