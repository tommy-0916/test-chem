"""Offline native binding/failover tests; no real provider or hardware calls."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage
from agent_skills.native_tools import NativeToolConfigurationError, invoke_with_tools
from utils.llm_factory import CodexResponsesModel, ModelPoolChatModel, OpenAICompatChatModel


class NativeDeviceTransportTests(unittest.TestCase):
    def backend(self):
        return OpenAICompatChatModel(model="test", api_key="fake", base_url="https://example.test/v1",
                                     timeout=2, temperature=0)

    def test_device_chat_native_uses_declared_wire_even_in_pool(self):
        backend = self.backend()
        native = Mock()
        native.bind_tools.return_value.invoke.return_value = AIMessage(content="done")
        with patch.dict(os.environ, {"REFINER_LLM_WIRE_API": "codex_responses", "REFINER_RESPONSES_TRANSPORT": "direct"}), \
             patch("agent_skills.native_tools.make_native_openai_model", return_value=native) as make, \
             patch.object(backend, "invoke") as text:
            result = ModelPoolChatModel([backend]).bind_tools([], tool_choice="none").invoke([])
            self.assertEqual(result.content, "done")
            self.assertTrue(make.call_args.kwargs["use_responses_api"])
            text.assert_not_called()

    def test_device_responses_native_failure_never_invokes_cli(self):
        backend = CodexResponsesModel(model="test", api_key="fake", base_url="https://example.test/v1", client=Mock())
        native = Mock()
        native.bind_tools.return_value.invoke.side_effect = ConnectionError("offline")
        with patch.dict(os.environ, {"REFINER_RESPONSES_TRANSPORT": "direct", "REFINER_RESPONSES_CLI_FALLBACK": "1"}), \
             patch("agent_skills.native_tools.make_native_openai_model", return_value=native), \
             patch.object(backend, "_invoke_cli") as cli:
            with self.assertRaises(ConnectionError):
                backend.bind_tools([]).invoke([])
            cli.assert_not_called()

    def test_device_explicit_cli_is_configuration_error(self):
        backend = self.backend()
        with patch.dict(os.environ, {"REFINER_LLM_WIRE_API": "codex_responses", "REFINER_RESPONSES_TRANSPORT": "cli"}):
            with self.assertRaises(NativeToolConfigurationError):
                backend.bind_tools([])

    def test_failover_retains_native_binding_and_message_identity(self):
        first, second = Mock(name="first"), Mock(name="second")
        first.bind_tools.return_value.invoke.side_effect = ConnectionError("offline")
        answer = AIMessage(content="", tool_calls=[{"name": "load_workstation_skill", "args": {"station_code": "XRD_V1"}, "id": "call"}])
        second.bind_tools.return_value.invoke.return_value = answer
        tools = [{"type": "function", "function": {"name": "load_workstation_skill", "parameters": {"type": "object", "properties": {}}}}]
        messages = [AIMessage(content="previous")]
        pool = ModelPoolChatModel([first, second], max_rounds=1)
        result = pool.bind_tools(tools, tool_choice="auto").invoke(messages)
        self.assertIs(result, answer)
        self.assertIs(second.bind_tools.return_value.invoke.call_args.args[0], messages)
        second.bind_tools.assert_called_once_with(tools, tool_choice="auto")
        first.invoke.assert_not_called()
        second.invoke.assert_not_called()
        self.assertEqual(pool._preferred_backend_index, 1)

    def test_native_pool_uses_child_retry_once_without_pool_replay(self):
        backend = Mock(name="backend")
        backend._transport_max_retries = 1
        answer = AIMessage(content="done")
        backend.bind_tools.return_value.invoke.side_effect = [
            ConnectionError("offline"),
            answer,
        ]
        messages = [AIMessage(content="previous")]
        pool = ModelPoolChatModel([backend], max_rounds=8)

        with patch("agent_skills.llm_retry.time.sleep") as sleep:
            result = pool.bind_tools([]).invoke(messages)

        self.assertIs(result, answer)
        self.assertEqual(backend.bind_tools.return_value.invoke.call_count, 2)
        sleep.assert_called_once_with(10.0)

    def test_native_pool_times_each_child_attempt_without_outer_pool_event(self):
        backend = Mock(name="backend")
        backend.model_name = "child-model"
        backend._transport_max_retries = 1
        backend.bind_tools.return_value.invoke.side_effect = [
            ConnectionError("offline"),
            AIMessage(content="done"),
        ]
        pool = ModelPoolChatModel([backend], max_rounds=8)

        with tempfile.TemporaryDirectory() as tmp:
            timing_path = Path(tmp) / "native-timing.jsonl"
            with patch.dict(
                os.environ,
                {"CHEM_LLM_TIMING_JSONL": str(timing_path)},
            ), patch("agent_skills.llm_retry.time.sleep"):
                result = invoke_with_tools(pool, [], [])
            events = [
                json.loads(line)
                for line in timing_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result.content, "done")
        request_events = [
            event
            for event in events
            if event.get("transport") == "device_native_pool"
        ]
        terminal_events = [
            event
            for event in request_events
            if event["status"] in {"failed", "success"}
        ]
        retry_events = [
            event
            for event in events
            if event["status"] in {"retry_sleep_started", "retry_sleep"}
        ]
        self.assertEqual(
            [event["status"] for event in request_events],
            ["started", "failed", "started", "success"],
        )
        self.assertEqual(
            [event["status"] for event in terminal_events],
            ["failed", "success"],
        )
        self.assertEqual(len(retry_events), 2)
        self.assertTrue(
            all(event["component"] == "device" for event in request_events)
        )

    def test_all_unsupported_fail_fast_without_round_retries(self):
        backends = [Mock(), Mock()]
        for backend in backends:
            backend.bind_tools.return_value.invoke.side_effect = ValueError("tools are not supported")
        with patch("utils.llm_factory.time.sleep") as sleep:
            with self.assertRaises(NativeToolConfigurationError):
                ModelPoolChatModel(backends, max_rounds=3).bind_tools([]).invoke([])
            sleep.assert_not_called()
        for backend in backends:
            backend.bind_tools.return_value.invoke.assert_called_once()
            backend.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
