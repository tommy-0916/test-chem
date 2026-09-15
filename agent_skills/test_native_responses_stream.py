"""Real SDK/LangChain encoding on fake HTTP streams; no provider requests.

The lifecycle assertions cover SDK-decoded events through its EOF boundary.
They do not claim raw-byte inspection after the SDK has consumed [DONE].
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from openai import AsyncOpenAI, OpenAI

from agent_skills.native_tools import invoke_with_tools, make_native_openai_model
from agent_skills.responses_stream import ResponsesProtocolError, ResponsesTerminalError


def completed(*, tools: bool = False, response_id: str = "resp_one") -> dict:
    output = [{"type": "function_call", "id": "fc_one", "call_id": "call_one", "name": "lookup",
               "arguments": '{"value":7}', "status": "completed"}] if tools else [
        {"type": "message", "id": "msg_one", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "done", "annotations": [], "logprobs": []}]}]
    return {
        "id": response_id, "object": "response", "created_at": 0, "model": "test",
        "status": "completed", "error": None, "incomplete_details": None,
        "output": output, "parallel_tool_calls": True, "tools": [], "tool_choice": "auto",
        "temperature": 1, "top_p": 1, "store": False, "metadata": {}, "service_tier": "default",
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        "text": {"format": {"type": "text"}}, "reasoning": {"effort": None, "summary": None},
    }


def sse(events: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode("utf-8")


def done_event(response: dict) -> dict:
    return {"type": "response.completed", "sequence_number": 1, "response": response}


def factory():
    return make_native_openai_model(model="test", api_key="fake", base_url="https://provider.invalid/v1",
                                    timeout=2, max_retries=0, use_responses_api=True)


@contextmanager
def sync_model(handler):
    with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct"}):
        model = factory()
    original_sync, original_async = model.root_client, model.root_async_client
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        model.root_client = OpenAI(api_key="fake", base_url="https://provider.invalid/v1", http_client=http, max_retries=0)
        try:
            yield model
        finally:
            model.root_client.close()
            original_sync.close()
            asyncio.run(original_async.close())


class Recorder(BaseCallbackHandler):
    def __init__(self):
        self.tokens = []
        self.ends = []

    def on_llm_new_token(self, token, **kwargs):
        self.tokens.append(token)

    def on_llm_end(self, response, **kwargs):
        self.ends.append(response)


def _case_terminal_only_tools_preserve_call_ids_and_execute_once(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    payloads, executed, responses = [], [], []

    @tool
    def lookup(value: int) -> dict:
        """Look up one mock value."""
        executed.append(value)
        return {"value": value}

    def handle(request):
        payloads.append(json.loads(request.content))
        response = completed(tools=len(payloads) == 1, response_id=f"resp_{len(payloads)}")
        reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([done_event(response)]))
        responses.append(reply)
        return reply

    with sync_model(handle) as model:
        assert model.streaming is True
        response = invoke_with_tools(model, [HumanMessage(content="test")], [lookup], max_rounds=1)
    assert response.content[0]["text"] == "done"
    assert executed == [7]
    assert len(payloads) == 2
    assert all(payload["stream"] is True for payload in payloads)
    assert all(payload["store"] is False for payload in payloads)
    assert all("previous_response_id" not in payload for payload in payloads)
    history = payloads[1]["input"]
    call = next(item for item in history if item.get("type") == "function_call")
    output = next(item for item in history if item.get("type") == "function_call_output")
    assert call["id"] == "fc_one"
    assert call["call_id"] == output["call_id"] == "call_one"
    assert json.loads(call["arguments"]) == {"value": 7}
    assert json.loads(output["output"]) == {"value": 7}
    assert all(item.is_closed for item in responses)


def _case_incomplete_deltas_use_authoritative_terminal_and_preserve_reasoning(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    terminal = completed(tools=True)
    terminal["output"].insert(0, {"type": "reasoning", "id": "rs_one", "summary": [], "encrypted_content": "opaque-test"})
    events = [{"type": "response.function_call_arguments.delta", "sequence_number": 0,
               "output_index": 1, "item_id": "fc_one", "delta": '{"value":'} , done_event(terminal)]
    with sync_model(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))) as model:
        response = model.bind_tools([]).invoke([HumanMessage(content="test")])
    assert response.tool_calls == [{"name": "lookup", "args": {"value": 7}, "id": "call_one", "type": "tool_call"}]
    block = next(item for item in response.content if item["type"] == "reasoning")
    assert block["id"] == "rs_one" and block["encrypted_content"] == "opaque-test"
    assert response.id == "resp_one"
    assert response.usage_metadata["total_tokens"] == 8


BAD_STREAM_CASES = [
    ([{"type": "response.output_text.delta", "delta": "partial", "output_index": 0, "content_index": 0}], ResponsesProtocolError),
    ([{"type": "response.incomplete", "response": {**completed(tools=True), "status": "incomplete"}}], ResponsesTerminalError),
    ([{"type": "error", "code": "broken", "message": "test"}], ResponsesTerminalError),
]


def _case_terminal_is_authoritative_without_done_or_eof(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    events = [
        done_event(completed()),
        {"type": "response.failed", "response": {"id": "resp_one", "status": "failed"}},
    ]
    with sync_model(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(events),
        )
    ) as model:
        response = model.bind_tools([]).invoke([HumanMessage(content="test")])
    assert response.content[0]["text"] == "done"


def _case_bad_stream_exposes_no_callback_success(monkeypatch, events, expected):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    requests, replies = [], []

    def handle(request):
        requests.append(request)
        reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))
        replies.append(reply)
        return reply

    recorder = Recorder()
    with sync_model(handle) as model:
        with unittest.TestCase().assertRaises(expected):
            model.bind_tools([]).invoke([HumanMessage(content="test")], config={"callbacks": [recorder]})
    assert not recorder.tokens
    assert not recorder.ends
    assert len(requests) == 1
    assert replies[0].is_closed


def _case_late_failure_after_terminal_cannot_cancel_tool_runtime(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    requests, executed, replies = [], [], []

    @tool
    def lookup(value: int) -> dict:
        """Read one mock value."""
        executed.append(value)
        return {"value": value}

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            events = [done_event(completed(tools=True)),
                      {"type": "response.failed", "response": {"id": "resp_one", "status": "failed"}}]
        else:
            events = [done_event(completed(response_id="resp_two"))]
        reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))
        replies.append(reply)
        return reply

    with sync_model(handle) as model:
        result = invoke_with_tools(model, [HumanMessage(content="test")], [lookup], max_rounds=1)
    assert result.content[0]["text"] == "done"
    assert executed == [7]
    assert len(requests) == 2
    assert all(reply.is_closed for reply in replies)


def _case_stream_method_emits_exactly_one_complete_chunk(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    events = [{"type": "response.output_text.delta", "delta": "unfinished", "output_index": 0, "content_index": 0},
              done_event(completed(tools=True))]
    with sync_model(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))) as model:
        chunks = list(model.bind_tools([]).stream([HumanMessage(content="test")]))
    assert len(chunks) == 1
    assert chunks[0].tool_calls[0]["id"] == "call_one"
    assert chunks[0].tool_calls[0]["args"] == {"value": 7}


def _case_optout_and_per_call_stream_false_still_validate_terminal(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "0")
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completed(tools=True))

    with sync_model(handle) as model:
        assert not model.streaming
        response = model.bind_tools([]).invoke([HumanMessage(content="test")], stream=False)
    assert response.tool_calls[0]["id"] == "call_one"
    assert payloads[0]["stream"] is False
    incomplete = {**completed(tools=True), "status": "incomplete"}
    with sync_model(lambda _: httpx.Response(200, json=incomplete)) as model:
        with unittest.TestCase().assertRaises(ResponsesTerminalError):
            model.bind_tools([]).invoke([HumanMessage(content="test")], stream=False)


def _case_per_call_stream_false_does_not_silently_disable_configured_wire_stream(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([done_event(completed())]))

    with sync_model(handle) as model:
        model.bind_tools([]).invoke([HumanMessage(content="test")], stream=False)
    assert payloads[0]["stream"] is True


def _case_network_failure_after_completed_keeps_authoritative_success(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")

    class BrokenStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield sse([done_event(completed(tools=True))])
            raise httpx.ReadError("test connection cut")

        def close(self):
            self.closed = True

    stream = BrokenStream()
    recorder = Recorder()
    with sync_model(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)) as model:
        response = model.bind_tools([]).invoke(
            [HumanMessage(content="test")], config={"callbacks": [recorder]}
        )
    assert stream.closed
    assert response.tool_calls[0]["id"] == "call_one"
    assert recorder.ends


def _case_async_invoke_and_astream_validate_and_preserve_calls(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")

    async def exercise():
        payloads, replies = [], []

        def handle(request):
            payloads.append(json.loads(request.content))
            reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([done_event(completed(tools=True))]))
            replies.append(reply)
            return reply

        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct"}):
            model = factory()
        original_sync, original_async = model.root_client, model.root_async_client
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            model.root_async_client = AsyncOpenAI(api_key="fake", base_url="https://provider.invalid/v1", http_client=http, max_retries=0)
            try:
                bound = model.model_copy().bind_tools([])
                response = await bound.ainvoke([HumanMessage(content="test")])
                assert response.tool_calls[0]["id"] == "call_one"
                chunks = [item async for item in bound.astream([HumanMessage(content="test")])]
                assert len(chunks) == 1 and chunks[0].tool_calls[0]["args"] == {"value": 7}
                assert all(item["stream"] is True for item in payloads)
                assert all(item.is_closed for item in replies)
            finally:
                await model.root_async_client.close()
                original_sync.close()
                await original_async.close()

    asyncio.run(exercise())


def _case_async_missing_terminal_has_no_success_callbacks_and_closes(monkeypatch):
    monkeypatch.setenv("REFINER_RESPONSES_STREAM", "1")

    async def exercise():
        replies = []

        def handle(_):
            reply = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                   content=sse([{"type": "response.output_text.delta", "delta": "partial", "output_index": 0, "content_index": 0}]))
            replies.append(reply)
            return reply

        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct"}):
            model = factory()
        original_sync, original_async = model.root_client, model.root_async_client
        recorder = Recorder()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            model.root_async_client = AsyncOpenAI(api_key="fake", base_url="https://provider.invalid/v1", http_client=http, max_retries=0)
            try:
                with unittest.TestCase().assertRaises(ResponsesProtocolError):
                    await model.bind_tools([]).ainvoke([HumanMessage(content="test")], config={"callbacks": [recorder]})
                assert not recorder.tokens and not recorder.ends
                assert replies[0].is_closed
            finally:
                await model.root_async_client.close()
                original_sync.close()
                await original_async.close()

    asyncio.run(exercise())


class NativeResponsesStreamTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)

    def setenv(self, key, value):
        os.environ[key] = value

    def test_terminal_only_tools_preserve_call_ids_and_execute_once(self):
        _case_terminal_only_tools_preserve_call_ids_and_execute_once(self)

    def test_incomplete_deltas_use_authoritative_terminal_and_preserve_reasoning(self):
        _case_incomplete_deltas_use_authoritative_terminal_and_preserve_reasoning(self)

    def test_terminal_is_authoritative_without_done_or_eof(self):
        _case_terminal_is_authoritative_without_done_or_eof(self)

    def test_bad_stream_exposes_no_callback_success(self):
        for events, expected in BAD_STREAM_CASES:
            with self.subTest(expected=expected, last_event=events[-1]["type"]):
                _case_bad_stream_exposes_no_callback_success(self, events, expected)

    def test_late_failure_after_terminal_cannot_cancel_tool_runtime(self):
        _case_late_failure_after_terminal_cannot_cancel_tool_runtime(self)

    def test_stream_method_emits_exactly_one_complete_chunk(self):
        _case_stream_method_emits_exactly_one_complete_chunk(self)

    def test_optout_and_per_call_stream_false_still_validate_terminal(self):
        _case_optout_and_per_call_stream_false_still_validate_terminal(self)

    def test_per_call_stream_false_keeps_configured_wire_stream(self):
        _case_per_call_stream_false_does_not_silently_disable_configured_wire_stream(self)

    def test_network_failure_after_completed_keeps_authoritative_success(self):
        _case_network_failure_after_completed_keeps_authoritative_success(self)

    def test_async_invoke_and_astream_validate_and_preserve_calls(self):
        _case_async_invoke_and_astream_validate_and_preserve_calls(self)

    def test_async_missing_terminal_has_no_success_callbacks_and_closes(self):
        _case_async_missing_terminal_has_no_success_callbacks_and_closes(self)


if __name__ == "__main__":
    unittest.main()
