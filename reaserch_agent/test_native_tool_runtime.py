"""Offline protocol tests for the shared LangChain tool runtime."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent_skills.native_tools import (
    NativeToolBudgetExceeded,
    NativeToolConfigurationError,
    NativeToolProtocolError,
    invoke_with_tools,
    make_native_openai_model,
)
from reaserch_agent.utils.llm_factory import CodexResponsesModel


class ScriptedNativeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.bindings = []
        self.messages = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return self

    def invoke(self, messages):
        self.messages.append(list(messages))
        return next(self.responses)


class RetryableNativeModel(ScriptedNativeModel):
    _transport_max_retries = 1

    def __init__(self):
        super().__init__([AIMessage(content="done")])
        self.attempts = 0

    def invoke(self, messages):
        self.attempts += 1
        if self.attempts == 1:
            error = RuntimeError("bad gateway")
            error.status_code = 502
            raise error
        return super().invoke(messages)


class NativeToolRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.executed = []

        @tool
        def lookup(value: int) -> dict:
            """Read one test value."""
            self.executed.append(value)
            return {"status": "ok", "value": value}

        self.lookup = lookup

    def test_empty_text_native_call_gets_matching_tool_result(self):
        model = ScriptedNativeModel([
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"value": 7}, "id": "c1"}]),
            AIMessage(content='{"done":true}'),
        ])
        recorded = []
        response = invoke_with_tools(model, [HumanMessage(content="read")], [self.lookup],
                                     on_tool_result=lambda request, output: recorded.append((request, output)))
        self.assertEqual(self.executed, [7])
        self.assertEqual(response.content, '{"done":true}')
        result = model.messages[1][-1]
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.tool_call_id, "c1")
        self.assertEqual(recorded[0][0], {"name": "lookup", "args": {"value": 7}, "id": "c1"})

    def test_text_only_model_fails_before_invocation(self):
        model = type("TextOnly", (), {"invoke": Mock()})()
        with self.assertRaises(NativeToolConfigurationError):
            invoke_with_tools(model, [], [self.lookup])
        model.invoke.assert_not_called()

    def test_native_gateway_failure_retries_after_ten_seconds(self):
        model = RetryableNativeModel()
        with patch("agent_skills.llm_retry.time.sleep") as sleep:
            response = invoke_with_tools(model, [], [self.lookup])
        self.assertEqual(response.content, "done")
        self.assertEqual(model.attempts, 2)
        sleep.assert_called_once_with(10.0)

    def test_legacy_text_request_is_never_executed(self):
        text = '{"tool_request":{"tool":"lookup","value":4}}'
        model = ScriptedNativeModel([AIMessage(content=text)])
        self.assertEqual(invoke_with_tools(model, [], [self.lookup]).content, text)
        self.assertEqual(self.executed, [])

    def test_parallel_requests_share_budget(self):
        model = ScriptedNativeModel([
            AIMessage(content="", tool_calls=[
                {"name": "lookup", "args": {"value": n}, "id": f"c{n}"} for n in (1, 2, 3)
            ]), AIMessage(content="done"),
        ])
        invoke_with_tools(model, [], [self.lookup], max_rounds=1)
        self.assertEqual(self.executed, [1])
        self.assertEqual(model.bindings[-1][1]["tool_choice"], "none")
        returned = [m for m in model.messages[-1] if isinstance(m, ToolMessage)]
        self.assertEqual(len(returned), 3)
        self.assertEqual(json.loads(returned[-1].content)["error"], "tool_budget_exhausted")

    def test_invalid_and_unknown_calls_return_errors_without_execution(self):
        model = ScriptedNativeModel([
            AIMessage(content="", tool_calls=[
                {"name": "lookup", "args": {"value": "not an integer"}, "id": "c1"},
                {"name": "not_registered", "args": {}, "id": "c2"},
            ]), AIMessage(content="done"),
        ])
        invoke_with_tools(model, [], [self.lookup])
        self.assertEqual(self.executed, [])
        self.assertTrue(all(m.status == "error" for m in model.messages[-1] if isinstance(m, ToolMessage)))

    def test_repeated_call_id_does_not_reexecute(self):
        call = AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"value": 1}, "id": "same"}])
        with self.assertRaises(NativeToolProtocolError):
            invoke_with_tools(ScriptedNativeModel([call, call]), [], [self.lookup])
        self.assertEqual(self.executed, [1])

    def test_final_turn_cannot_bypass_budget(self):
        call = AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"value": 1}, "id": "c1"}])
        with self.assertRaises(NativeToolBudgetExceeded):
            invoke_with_tools(ScriptedNativeModel([call]), [], [self.lookup], max_rounds=0)
        self.assertEqual(self.executed, [])

    def test_cli_transport_rejected_before_building_native_client(self):
        adapter = CodexResponsesModel(model="test", api_key="fake", base_url="https://example.test", client=Mock())
        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "cli"}), patch.object(adapter, "_invoke_cli") as cli:
            with self.assertRaises(NativeToolConfigurationError):
                adapter.bind_tools([self.lookup])
            cli.assert_not_called()

    def test_responses_adapter_delegates_native_and_never_falls_back(self):
        adapter = CodexResponsesModel(model="test", api_key="fake", base_url="https://example.test", client=Mock())
        native = Mock()
        native.bind_tools.return_value.invoke.side_effect = ConnectionError("offline test")
        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct", "REFINER_RESPONSES_CLI_FALLBACK": "1"}), \
             patch("agent_skills.native_tools.make_native_openai_model", return_value=native) as factory, \
             patch.object(adapter, "_invoke_cli") as cli:
            with self.assertRaises(ConnectionError):
                invoke_with_tools(adapter, [], [self.lookup])
            self.assertTrue(factory.call_args.kwargs["use_responses_api"])
            cli.assert_not_called()

    def test_chat_wire_schema_and_call_id_with_real_langchain_conversion(self):
        payloads = []

        def handle(request):
            payload = json.loads(request.content)
            payloads.append(payload)
            message = ({"role": "assistant", "content": None, "tool_calls": [
                {"id": "wire_call", "type": "function", "function": {"name": "lookup", "arguments": '{"value":9}'}}
            ]} if len(payloads) == 1 else {"role": "assistant", "content": "done"})
            return httpx.Response(200, json={"id": "completion_test", "object": "chat.completion", "created": 0,
                                           "model": "test", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})

        from langchain_openai import ChatOpenAI
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            model = ChatOpenAI(model="test", api_key="fake", base_url="https://example.test/v1", http_client=client,
                               use_responses_api=False, max_retries=0)
            invoke_with_tools(model, [HumanMessage(content="read")], [self.lookup], max_rounds=1)
        self.assertEqual(self.executed, [9])
        self.assertEqual(payloads[0]["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(payloads[1]["messages"][-1]["tool_call_id"], "wire_call")

    def test_responses_wire_uses_function_outputs_without_state_storage(self):
        payloads = []

        def handle(request):
            payloads.append(json.loads(request.content))
            output = ([{"type": "function_call", "name": "lookup", "arguments": '{"value":8}',
                        "call_id": "responses_call", "id": "fc_1", "status": "completed"}]
                      if len(payloads) == 1 else [{"type": "message", "id": "msg_1", "role": "assistant",
                                                   "status": "completed", "content": [{"type": "output_text", "text": "done", "annotations": []}]}])
            return httpx.Response(200, json={"id": "resp_test", "object": "response", "created_at": 0,
                                           "model": "test", "status": "completed", "output": output})

        from langchain_openai import ChatOpenAI
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            model = ChatOpenAI(model="test", api_key="fake", base_url="https://example.test/v1", http_client=client,
                               use_responses_api=True, store=False, use_previous_response_id=False, max_retries=0)
            invoke_with_tools(model, [HumanMessage(content="read")], [self.lookup], max_rounds=1)
        self.assertEqual(self.executed, [8])
        self.assertEqual(payloads[0]["tools"][0]["type"], "function")
        self.assertFalse(payloads[0]["store"])
        self.assertNotIn("previous_response_id", payloads[1])
        outputs = [item for item in payloads[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(outputs[0]["call_id"], "responses_call")

    def test_model_builder_keeps_explicit_wire_options(self):
        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct"}):
            model = make_native_openai_model(model="test", api_key="fake", base_url="https://example.test/v1",
                                            timeout=3, use_responses_api=True)
        self.assertTrue(model.use_responses_api)
        self.assertFalse(model.store)
        self.assertFalse(model.use_previous_response_id)

    def test_model_builder_enables_native_responses_streaming(self):
        with patch.dict(
            os.environ,
            {
                "REFINER_RESPONSES_TRANSPORT": "direct",
                "REFINER_RESPONSES_STREAM": "1",
            },
        ):
            model = make_native_openai_model(
                model="test",
                api_key="fake",
                base_url="https://example.test/v1",
                timeout=3,
                use_responses_api=True,
            )
        self.assertTrue(model.streaming)


if __name__ == "__main__":
    unittest.main()
