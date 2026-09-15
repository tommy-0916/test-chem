"""Device Responses stream integration tests with no pytest or network."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_skills.responses_stream import ResponsesProtocolError, ResponsesTerminalError
from utils.llm_factory import CodexResponsesModel, ModelPoolChatModel


class FakeStream:
    def __init__(self, events, error=None):
        self.events, self.error, self.closed = events, error, False

    def __iter__(self):
        yield from self.events
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class DeviceResponsesStreamTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "REFINER_RESPONSES_TRANSPORT": "direct", "REFINER_RESPONSES_STREAM": "1",
            "REFINER_RESPONSES_CLI_FALLBACK": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def model(self, result):
        create = Mock(return_value=result)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        model = CodexResponsesModel(model="test", api_key="fake", base_url="https://provider.invalid",
                                    reasoning_effort="high", max_output_tokens=1024, client=client)
        return model, create

    def terminal(self):
        return SimpleNamespace(id="resp_test", status="completed", output_text='{"status":"ok"}')

    def test_default_direct_stream_preserves_payload_and_terminal_identity(self):
        terminal = self.terminal()
        stream = FakeStream([SimpleNamespace(type="response.completed", response=terminal)])
        model, create = self.model(stream)
        response = model.invoke([{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "test"}])
        self.assertEqual(response.content, '{"status":"ok"}')
        self.assertIs(response.raw_response, terminal)
        create.assert_called_once()
        payload = create.call_args.kwargs
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["max_output_tokens"], 1024)
        self.assertIn("Return JSON.", payload["input"])
        self.assertNotIn("tools", payload)
        self.assertTrue(stream.closed)

    def test_json_object_path_also_streams_without_cli_envelope(self):
        stream = FakeStream([SimpleNamespace(type="response.completed", response=self.terminal())])
        model, create = self.model(stream)
        result = model.invoke_json_object([{"role": "user", "content": "JSON"}])
        self.assertEqual(result.content, '{"status":"ok"}')
        self.assertTrue(create.call_args.kwargs["stream"])
        for key in ("text", "response_format", "tools"):
            self.assertNotIn(key, create.call_args.kwargs)
        self.assertTrue(stream.closed)

    def test_preterminal_stream_failures_do_not_fallback_to_cli(self):
        cases = [
            ([SimpleNamespace(type="response.output_text.delta", delta="partial")], None, ResponsesProtocolError),
            ([SimpleNamespace(type="response.incomplete", response=SimpleNamespace(status="incomplete"))], None, ResponsesTerminalError),
            ([SimpleNamespace(type="error")], None, ResponsesTerminalError),
        ]
        for json_task in (False, True):
            for events, error, expected in cases:
                with self.subTest(json_task=json_task, expected=expected, event=events[-1].type):
                    stream = FakeStream(events, error)
                    model, create = self.model(stream)
                    with patch.object(model, "_invoke_cli") as cli, patch.object(model, "_invoke_cli_json_object") as cli_json:
                        with self.assertRaises(expected):
                            (model.invoke_json_object if json_task else model.invoke)([{"role": "user", "content": "test"}])
                        cli.assert_not_called()
                        cli_json.assert_not_called()
                    create.assert_called_once()
                    self.assertTrue(stream.closed)

    def test_completed_terminal_does_not_wait_for_late_failure_or_eof_error(self):
        done = SimpleNamespace(type="response.completed", response=self.terminal())
        cases = [
            FakeStream([done, SimpleNamespace(type="response.failed")]),
            FakeStream([done], ConnectionError("test interrupted stream")),
        ]
        for json_task in (False, True):
            for stream in cases:
                with self.subTest(json_task=json_task, stream=stream):
                    model, create = self.model(stream)
                    with patch.object(model, "_invoke_cli") as cli, patch.object(
                        model, "_invoke_cli_json_object"
                    ) as cli_json:
                        response = (model.invoke_json_object if json_task else model.invoke)(
                            [{"role": "user", "content": "test"}]
                        )
                        self.assertEqual(response.content, '{"status":"ok"}')
                        cli.assert_not_called()
                        cli_json.assert_not_called()
                    create.assert_called_once()
                    self.assertTrue(stream.closed)

    def test_nonstream_optout_requires_completed_status(self):
        os.environ["REFINER_RESPONSES_STREAM"] = "0"
        os.environ["REFINER_RESPONSES_CLI_FALLBACK"] = "0"
        model, create = self.model(self.terminal())
        self.assertEqual(model.invoke([{"role": "user", "content": "test"}]).content, '{"status":"ok"}')
        self.assertFalse(create.call_args.kwargs["stream"])
        create.return_value = SimpleNamespace(status="incomplete", output_text="partial")
        with self.assertRaises(ResponsesTerminalError):
            model.invoke([{"role": "user", "content": "test"}])

    def test_explicit_cli_transport_keeps_legacy_entrypoint(self):
        os.environ["REFINER_RESPONSES_TRANSPORT"] = "cli"
        model, create = self.model(None)
        sentinel = object()
        with patch.object(model, "_invoke_cli", return_value=sentinel) as cli:
            self.assertIs(model.invoke([{"role": "user", "content": "test"}]), sentinel)
            cli.assert_called_once()
        create.assert_not_called()

    def test_native_pool_does_not_replay_protocol_failure(self):
        for error in (ResponsesProtocolError, ResponsesTerminalError):
            with self.subTest(error=error):
                first, second = Mock(), Mock()
                first.bind_tools.return_value.invoke.side_effect = error("invalid completion")
                pool = ModelPoolChatModel([first, second], max_rounds=3)
                with patch("utils.llm_factory.time.sleep") as sleep:
                    with self.assertRaises(error):
                        pool.bind_tools([]).invoke([])
                    sleep.assert_not_called()
                first.bind_tools.return_value.invoke.assert_called_once()
                second.bind_tools.return_value.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
