"""Network-free tests for shared sync/async Responses terminal guards."""

from __future__ import annotations

import copy
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_skills.responses_stream import (
    ResponsesProtocolError,
    ResponsesReadIdleTimeout,
    ResponsesStreamError,
    ResponsesStreamState,
    ResponsesTerminalError,
    configured_responses_streaming,
    invoke_responses,
    invoke_responses_async,
)


def completed(text='{"ok":true}', **updates):
    return {
        "id": "resp_test", "object": "response", "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        **updates,
    }


def terminal(response=None, kind="response.completed"):
    return {"type": kind, "response": response if response is not None else completed()}


class FakeStream:
    def __init__(self, events, error=None, close_error=None):
        self.events = events
        self.error = error
        self.close_error = close_error
        self.closed = False

    def __iter__(self):
        yield from self.events
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeAsyncStream(FakeStream):
    async def __aiter__(self):
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error

    async def close(self):
        super().close()


class FakeResponses:
    def __init__(self, result):
        self.result = result
        self.payloads = []

    def create(self, **payload):
        self.payloads.append(payload)
        return self.result


class ResponsesStreamTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, stream, payload=None):
        self.responses = FakeResponses(stream)
        return invoke_responses(SimpleNamespace(responses=self.responses), payload or {"model": "test"})

    def test_default_on_and_explicit_opt_out(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(configured_responses_streaming())
        for value in ["0", "false", "OFF", "no"]:
            with self.subTest(value=value), patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": value}):
                self.assertFalse(configured_responses_streaming())
        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "typo"}):
            with self.assertRaises(ValueError):
                configured_responses_streaming()

    def test_complete_preserves_payload_and_response_and_closes(self):
        result = completed(output=[{"type": "function_call", "name": "lookup", "arguments": "{}", "call_id": "call_1"}], usage={"output_tokens": 10})
        stream = FakeStream([{"type": "response.function_call_arguments.delta", "delta": "{"}, terminal(result)])
        payload = {"model": "test", "input": [{"role": "user", "content": "test"}], "store": False,
                   "reasoning": {"effort": "xhigh"}, "max_output_tokens": 32768, "tools": [{"type": "function", "name": "lookup"}]}
        original = copy.deepcopy(payload)
        self.assertIs(self.invoke(stream, payload), result)
        self.assertEqual(payload, original)
        self.assertEqual(self.responses.payloads, [{**payload, "stream": True}])
        self.assertTrue(stream.closed)

    def test_no_terminal_never_returns_deltas(self):
        stream = FakeStream([{"type": "response.output_text.delta", "delta": '{"ok":true}'}])
        with self.assertRaises(ResponsesProtocolError):
            self.invoke(stream)
        self.assertTrue(stream.closed)

    def test_failed_incomplete_and_error_never_return_partial(self):
        for kind in ["error", "response.failed", "response.incomplete", "response.cancelled"]:
            with self.subTest(kind=kind):
                stream = FakeStream([{"type": "response.output_text.delta", "delta": "partial"}, terminal(completed(), kind=kind)])
                with self.assertRaises(ResponsesTerminalError):
                    self.invoke(stream)
                self.assertTrue(stream.closed)

    def test_completed_status_and_error_details_are_checked(self):
        for result in [completed(status="in_progress"), completed(status="incomplete"), completed(error={"code": "bad"}), completed(incomplete_details={"reason": "max_output_tokens"}), {}]:
            with self.subTest(result=result):
                stream = FakeStream([terminal(result)])
                with self.assertRaises(ResponsesStreamError):
                    self.invoke(stream)
                self.assertTrue(stream.closed)

    def test_completed_is_authoritative_and_late_events_are_not_waited_for(self):
        for later in [terminal(completed("changed")), terminal(kind="response.failed"), {"type": "error"}, {"type": "response.output_text.delta", "delta": "late"}]:
            with self.subTest(later=later):
                self.assertEqual(self.invoke(FakeStream([terminal(), later])), completed())

    def test_completed_envelope_cannot_hide_unfinished_tool_item(self):
        for status in ["in_progress", "incomplete", "failed", "cancelled"]:
            with self.subTest(status=status):
                result = completed(output=[{"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{", "status": status}])
                stream = FakeStream([terminal(result)])
                with self.assertRaises(ResponsesProtocolError):
                    self.invoke(stream)
                self.assertTrue(stream.closed)

    def test_completed_item_status_may_be_missing_or_null(self):
        for status_fields in [{}, {"status": None}, {"status": "completed"}]:
            with self.subTest(fields=status_fields):
                result = completed(output=[{"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}", **status_fields}])
                self.assertIs(self.invoke(FakeStream([terminal(result)])), result)

    def test_identical_duplicate_terminal_is_consistent(self):
        result = completed()
        self.assertEqual(self.invoke(FakeStream([terminal(result), terminal(copy.deepcopy(result))])), result)

    def test_response_id_conflict_is_rejected(self):
        stream = FakeStream([{"type": "response.created", "response": {"id": "other", "status": "in_progress"}}, terminal()])
        with self.assertRaises(ResponsesProtocolError):
            self.invoke(stream)

    def test_timeout_after_completed_is_not_waited_for_and_stream_closes(self):
        failure = TimeoutError("transport timed out")
        stream = FakeStream([terminal()], error=failure, close_error=RuntimeError("close failed"))
        self.assertEqual(self.invoke(stream), completed())
        self.assertTrue(stream.closed)

    def test_close_failure_does_not_discard_a_valid_terminal(self):
        self.assertEqual(
            self.invoke(FakeStream([terminal()], close_error=RuntimeError("close failed"))),
            completed(),
        )

    def test_silent_stream_is_bounded_by_read_idle_timeout(self):
        blocker = threading.Event()

        class SilentStream(FakeStream):
            def __iter__(self):
                blocker.wait(1)
                return
                yield  # pragma: no cover

        with patch.dict(os.environ, {"REFINER_LLM_READ_IDLE_TIMEOUT_SECONDS": "0.02"}):
            with self.assertRaises(ResponsesReadIdleTimeout):
                self.invoke(SilentStream([]))

    def test_non_stream_opt_out_validates_final_status(self):
        result = completed()
        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "0"}):
            self.assertIs(self.invoke(result), result)
            self.assertFalse(self.responses.payloads[0]["stream"])
            with self.assertRaises(ResponsesTerminalError):
                self.invoke(completed(status="incomplete"))

    def test_failed_state_cannot_later_finish_successfully(self):
        state = ResponsesStreamState()
        state.consume(terminal())
        with self.assertRaises(ResponsesTerminalError):
            state.consume({"type": "error"})
        with self.assertRaises(ResponsesTerminalError):
            state.finish()

    def test_sdk_mock_http_fragmented_unicode_stream(self):
        import httpx
        from openai import OpenAI

        body = ("data: " + json.dumps(terminal(completed('你好')), ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()

        class Bytes(httpx.SyncByteStream):
            def __iter__(self):
                for index in range(0, len(body), 7):
                    yield body[index:index + 7]

        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes())

        with OpenAI(api_key="fake", base_url="https://provider.invalid/v1", http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0) as client:
            response = invoke_responses(client, {"model": "test", "input": "test"})
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.output[0].content[0].text, "你好")
        self.assertTrue(requests[0]["stream"])

    def test_sdk_wire_eof_and_late_failure_are_rejected(self):
        import httpx
        from openai import OpenAI

        def event(value):
            return "data: " + json.dumps(value) + "\n\n"

        for body in [
            event({"type": "response.output_text.delta", "delta": "partial"}),
            "data: [DONE]\n\n",
            json.dumps(completed()),
        ]:
            with self.subTest(body=body):
                def respond(request):
                    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
                with OpenAI(api_key="fake", base_url="https://provider.invalid/v1", http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0) as client:
                    with self.assertRaises(ResponsesStreamError):
                        invoke_responses(client, {"model": "test", "input": "test"})

        body = event(terminal()) + event(
            terminal(completed(status="failed"), "response.failed")
        )
        def respond_after_terminal(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )
        with OpenAI(
            api_key="fake",
            base_url="https://provider.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(respond_after_terminal)),
            max_retries=0,
        ) as client:
            self.assertEqual(
                invoke_responses(client, {"model": "test", "input": "test"}).status,
                "completed",
            )


class AsyncResponsesStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_sdk_mock_http_preserves_terminal_tool_fields(self):
        import httpx
        from openai import AsyncOpenAI

        result = completed(output=[{"type": "function_call", "name": "lookup", "arguments": "{}", "call_id": "call_1", "status": "completed"}])
        body = ("data: " + json.dumps(terminal(result)) + "\n\ndata: [DONE]\n\n").encode()

        class Bytes(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                for index in range(0, len(body), 11):
                    yield body[index:index + 11]

            async def aclose(self):
                self.closed = True

        wire = Bytes()
        async def respond(request):
            self.assertTrue(json.loads(request.content)["stream"])
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"}):
            async with AsyncOpenAI(api_key="fake", base_url="https://provider.invalid/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=0) as client:
                response = await invoke_responses_async(client, {"model": "test", "input": "test"})
                self.assertTrue(wire.closed)
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.output[0].call_id, "call_1")
        self.assertEqual(response.output[0].arguments, "{}")

    async def test_async_completed_close_and_payload(self):
        result = completed()
        stream = FakeAsyncStream([terminal(result)])
        create = AsyncMock(return_value=stream)
        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"}):
            actual = await invoke_responses_async(SimpleNamespace(responses=SimpleNamespace(create=create)), {"model": "test"})
        self.assertIs(actual, result)
        create.assert_awaited_once_with(model="test", stream=True)
        self.assertTrue(stream.closed)

    async def test_async_eof_and_incomplete_fail_before_terminal(self):
        for stream, error_type in [(FakeAsyncStream([]), ResponsesProtocolError),
                                   (FakeAsyncStream([terminal(kind="response.incomplete")]), ResponsesTerminalError)]:
            with self.subTest(error=error_type), patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"}):
                client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=stream)))
                with self.assertRaises(error_type):
                    await invoke_responses_async(client, {"model": "test"})
                self.assertTrue(stream.closed)

    async def test_async_terminal_does_not_wait_for_late_conflict_or_timeout(self):
        for stream in [
            FakeAsyncStream([terminal(), terminal(completed("other"))]),
            FakeAsyncStream([terminal()], error=TimeoutError("read")),
        ]:
            with self.subTest(stream=stream), patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"}):
                client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=stream)))
                response = await invoke_responses_async(client, {"model": "test"})
                self.assertEqual(response, completed())
                self.assertTrue(stream.closed)

    async def test_async_nonstream_opt_out(self):
        result = completed()
        create = AsyncMock(return_value=result)
        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "0"}):
            actual = await invoke_responses_async(SimpleNamespace(responses=SimpleNamespace(create=create)), {"model": "test"})
        self.assertIs(actual, result)
        create.assert_awaited_once_with(model="test", stream=False)

    async def test_async_cancellation_closes_stream(self):
        import asyncio

        stream = FakeAsyncStream([], error=asyncio.CancelledError())
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=stream)))
        with patch.dict(os.environ, {"REFINER_RESPONSES_STREAM": "1"}):
            with self.assertRaises(asyncio.CancelledError):
                await invoke_responses_async(client, {"model": "test"})
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
