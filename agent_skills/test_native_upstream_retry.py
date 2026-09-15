"""Offline coverage of opt-in retries without replaying native application tools."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent_skills.native_tools import NativeToolBudgetExceeded, invoke_with_tools
from agent_skills.responses_stream import ResponsesProtocolError, ResponsesTerminalError


def upstream_failure(**updates):
    diagnostics = {
        "event_type": "response.failed",
        "response_status": "failed",
        "response_id": "resp_failed",
        "http_status": 200,
        "error": {"code": "upstream_error", "message": "Upstream request failed"},
        **updates,
    }
    return ResponsesTerminalError("Response failed", diagnostics=diagnostics)


class ScriptedModel:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.histories = []
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append(kwargs)
        return self

    def invoke(self, messages):
        self.histories.append(deepcopy(messages))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(messages) if callable(outcome) else outcome


class NativeUpstreamRetryTests(unittest.TestCase):
    def setUp(self):
        sleep_patch = patch("agent_skills.llm_retry.time.sleep", return_value=None)
        self.addCleanup(sleep_patch.stop)
        sleep_patch.start()
        self.executed = []

        @tool
        def lookup(value: int) -> dict:
            """Load one mock device skill."""
            self.executed.append(value)
            return {"status": "ok", "value": value, "skill": "fully loaded mock skill"}

        self.lookup = lookup
        self.input = [HumanMessage(content="private planning input")]

    @staticmethod
    def tool_call(call_id="skill_one", value=1):
        return AIMessage(content="", tool_calls=[
            {"name": "lookup", "args": {"value": value}, "id": call_id},
        ])

    def test_default_behavior_does_not_retry(self):
        failure = upstream_failure()
        model = ScriptedModel([failure, AIMessage(content="unused")])
        with self.assertRaises(ResponsesTerminalError) as caught:
            invoke_with_tools(model, self.input, [])
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(model.histories), 1)

    def test_generic_native_request_is_timed_without_prompt_content(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        model = ScriptedModel([AIMessage(content="complete plan")])
        with tempfile.TemporaryDirectory() as directory:
            timing_path = Path(directory) / "timing.jsonl"
            with patch.dict(
                os.environ,
                {"CHEM_LLM_TIMING_JSONL": str(timing_path)},
            ):
                result = invoke_with_tools(model, self.input, [])
            raw = timing_path.read_text(encoding="utf-8")

        self.assertEqual(result.content, "complete plan")
        events = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(
            [event["status"] for event in events], ["started", "success"]
        )
        self.assertEqual(events[-1]["transport"], "chat_native_tools")
        self.assertNotIn("private planning input", raw)

    def test_opt_in_recovers_with_one_retry_and_metadata_only_events(self):
        failure = upstream_failure(error={
            "code": "upstream_error", "message": "Authorization: Bearer sk-unit-secret",
        }, private_prompt="private planning input")
        model = ScriptedModel([failure, AIMessage(content="complete plan")])
        events = []
        result = invoke_with_tools(model, self.input, [], retry_upstream_errors=True,
                                   on_model_attempt=events.append)
        self.assertEqual(result.content, "complete plan")
        self.assertEqual(model.histories[0], model.histories[1])
        self.assertEqual([event["event"] for event in events], [
            "model_attempt_start", "model_attempt_failed",
            "model_attempt_start", "model_attempt_completed",
        ])
        self.assertTrue(events[1]["retry_scheduled"])
        self.assertEqual(events[1]["exception_type"], "ResponsesTerminalError")
        self.assertEqual(events[-1]["model_turn"], 1)
        self.assertEqual(events[-1]["attempt"], 2)
        self.assertEqual(events[-1]["upstream_retries_used"], 1)
        self.assertNotIn("private planning input", json.dumps(events))
        self.assertNotIn("sk-unit-secret", json.dumps(events))

    def test_exhaustion_propagates_last_failure_and_marks_no_further_retry(self):
        first, last = upstream_failure(), upstream_failure(response_id="resp_last")
        model = ScriptedModel([first, last, AIMessage(content="unused")])
        events = []
        with self.assertRaises(ResponsesTerminalError) as caught:
            invoke_with_tools(model, self.input, [], retry_upstream_errors=True,
                              on_model_attempt=events.append)
        self.assertIs(caught.exception, last)
        self.assertEqual(len(model.histories), 2)
        self.assertFalse(events[-1]["retry_scheduled"])
        self.assertEqual(events[-1]["attempt"], 2)
        self.assertEqual(events[-1]["responses_diagnostics"]["response_id"], "resp_last")

    def test_completed_tools_are_kept_and_not_executed_again(self):
        model = ScriptedModel([self.tool_call(), upstream_failure(), AIMessage(content="plan")])
        results, events = [], []
        result = invoke_with_tools(
            model, self.input, [self.lookup], max_rounds=1, retry_upstream_errors=True,
            on_tool_result=lambda request, output: results.append((request, output)),
            on_model_attempt=events.append,
        )
        self.assertEqual(result.content, "plan")
        self.assertEqual(self.executed, [1])
        self.assertEqual(len(results), 1)
        self.assertEqual(model.histories[1], model.histories[2])
        self.assertEqual(model.histories[2][-1].tool_call_id, "skill_one")
        self.assertIn("fully loaded mock skill", model.histories[2][-1].content)
        self.assertEqual(model.bindings, [{"tool_choice": "auto"}, {"tool_choice": "none"}])
        self.assertEqual(events[-1]["model_turn"], 2)
        self.assertEqual(events[-1]["attempt"], 2)
        self.assertTrue(events[-1]["tools_disabled"])

    def test_retry_budget_is_shared_across_model_turns(self):
        last = upstream_failure(response_id="last")
        model = ScriptedModel([upstream_failure(), self.tool_call(), last, AIMessage(content="unused")])
        with self.assertRaises(ResponsesTerminalError) as caught:
            invoke_with_tools(model, self.input, [self.lookup], max_rounds=2,
                              retry_upstream_errors=True)
        self.assertIs(caught.exception, last)
        self.assertEqual(len(model.histories), 3)
        self.assertEqual(self.executed, [1])

    def test_explicit_zero_retry_budget(self):
        model = ScriptedModel([upstream_failure(), AIMessage(content="unused")])
        with self.assertRaises(ResponsesTerminalError):
            invoke_with_tools(model, [], [], retry_upstream_errors=True, max_upstream_retries=0)
        self.assertEqual(len(model.histories), 1)

    def test_explicit_retry_budget_can_recover_after_two_failures(self):
        model = ScriptedModel([upstream_failure(), upstream_failure(), AIMessage(content="plan")])
        self.assertEqual(invoke_with_tools(model, [], [], retry_upstream_errors=True,
                                          max_upstream_retries=2).content, "plan")
        self.assertEqual(len(model.histories), 3)

    def test_configuration_rejects_coercion_and_unbounded_budgets_before_model_call(self):
        invalid = [
            {"retry_upstream_errors": value} for value in (1, 0, "false", None)
        ] + [{"max_upstream_retries": value} for value in (-1, 4, 10000, True, False, 1.0, "1", None)]
        for options in invalid:
            with self.subTest(options=options):
                model = ScriptedModel([AIMessage(content="unused")])
                with self.assertRaises(ValueError):
                    invoke_with_tools(model, [], [], **options)
                self.assertEqual(model.histories, [])
                self.assertEqual(model.bindings, [])

    def test_terminal_and_protocol_failures_are_not_retried(self):
        failures = [
            upstream_failure(event_type="response.incomplete", response_status="incomplete"),
            upstream_failure(event_type="response.cancelled", response_status="cancelled"),
            upstream_failure(event_type="response.failed", response_status="incomplete"),
            upstream_failure(error={"code": "authentication_error"}),
            upstream_failure(error={"message": "upstream_error"}),
            upstream_failure(http_status=401),
            ResponsesProtocolError("Conflicting terminal", diagnostics={
                "event_type": "response.failed", "response_status": "failed",
                "error": {"code": "upstream_error"},
            }),
            PermissionError("Invalid credential"),
        ]
        for failure in failures:
            with self.subTest(failure=str(failure)):
                model = ScriptedModel([failure, AIMessage(content="unused")])
                events = []
                with self.assertRaises(type(failure)) as caught:
                    invoke_with_tools(model, [], [], retry_upstream_errors=True,
                                      on_model_attempt=events.append)
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(model.histories), 1)
                self.assertFalse(events[-1]["retry_scheduled"])

    def test_transient_network_http_and_upstream_failures_are_retried(self):
        class HttpFailure(RuntimeError):
            status_code = 502

        failures = [
            upstream_failure(event_type="error"),
            upstream_failure(event_type=None),
            upstream_failure(
                error={"code": "server_error", "type": "upstream_error"}
            ),
            TimeoutError("Read timeout"),
            HttpFailure("provider body must not matter"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                model = ScriptedModel([failure, AIMessage(content="recovered")])
                events = []
                result = invoke_with_tools(
                    model,
                    [],
                    [],
                    retry_upstream_errors=True,
                    on_model_attempt=events.append,
                )
                self.assertEqual(result.content, "recovered")
                self.assertEqual(len(model.histories), 2)
                self.assertTrue(events[1]["retry_scheduled"])

    def test_failed_wrapper_cannot_mutate_checkpoint_or_supply_partial_output(self):
        def mutate_then_fail(history):
            history[0].content = "mutated input"
            history.append(self.tool_call("partial_call", 2))
            failure = upstream_failure()
            failure.partial_response = self.tool_call("partial_error_call", 3)
            raise failure

        model = ScriptedModel([self.tool_call(), mutate_then_fail, AIMessage(content="plan")])
        invoke_with_tools(model, self.input, [self.lookup], max_rounds=1, retry_upstream_errors=True)
        self.assertEqual(model.histories[1], model.histories[2])
        self.assertEqual(self.input[0].content, "private planning input")
        self.assertEqual(self.executed, [1])

    def test_tool_execution_failure_does_not_trigger_model_retry(self):
        @tool
        def lookup(value: int) -> dict:
            """Fail once while loading the mock skill."""
            self.executed.append(value)
            raise upstream_failure()

        model = ScriptedModel([self.tool_call(), AIMessage(content="handled tool failure")])
        events = []
        invoke_with_tools(model, [], [lookup], max_rounds=1, retry_upstream_errors=True,
                          on_model_attempt=events.append)
        self.assertEqual(self.executed, [1])
        self.assertEqual(len(model.histories), 2)
        self.assertIsInstance(model.histories[-1][-1], ToolMessage)
        self.assertEqual(model.histories[-1][-1].status, "error")
        self.assertFalse(any(event["event"] == "model_attempt_failed" for event in events))

    def test_retry_cannot_bypass_exhausted_tool_budget(self):
        model = ScriptedModel([self.tool_call(), upstream_failure(), self.tool_call("extra", 2)])
        with self.assertRaises(NativeToolBudgetExceeded):
            invoke_with_tools(model, [], [self.lookup], max_rounds=1, retry_upstream_errors=True)
        self.assertEqual(self.executed, [1])
        self.assertEqual(model.bindings[-1]["tool_choice"], "none")

    def test_callback_failure_stops_before_retry_or_tool_execution(self):
        for event_type, outcomes, call_count in [
            ("model_attempt_start", [self.tool_call()], 0),
            ("model_attempt_failed", [upstream_failure(), self.tool_call()], 1),
            ("model_attempt_completed", [self.tool_call(), AIMessage(content="unused")], 1),
        ]:
            with self.subTest(event_type=event_type):
                model = ScriptedModel(outcomes)

                def callback(event):
                    if event["event"] == event_type:
                        raise AssertionError("Cannot persist attempt metadata")

                with self.assertRaisesRegex(AssertionError, "Cannot persist"):
                    invoke_with_tools(model, [], [self.lookup], retry_upstream_errors=True,
                                      on_model_attempt=callback)
                self.assertEqual(len(model.histories), call_count)
                self.assertEqual(self.executed, [])

    def test_terminal_tool_call_is_authoritative_over_late_stream_failure(self):
        import httpx
        from unittest.mock import patch

        from agent_skills.test_native_responses_stream import completed, done_event, sse, sync_model

        payloads = []

        def handle(request):
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                events = [done_event(completed(tools=True)), {
                    "type": "response.failed", "response": {
                        "id": "resp_one", "status": "failed", "error": {"code": "upstream_error"},
                    },
                }]
            else:
                events = [done_event(completed(response_id="resp_retry"))]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))

        with patch.dict("os.environ", {"REFINER_RESPONSES_STREAM": "1", "LANGSMITH_TRACING": "false",
                                       "LANGCHAIN_TRACING_V2": "false"}):
            with sync_model(handle) as model:
                invoke_with_tools(model, self.input, [self.lookup], max_rounds=1,
                                  retry_upstream_errors=True)
        self.assertEqual(self.executed, [7])
        self.assertEqual(len(payloads), 2)
        self.assertNotEqual(payloads[0]["input"], payloads[1]["input"])
        self.assertTrue(all(payload["stream"] for payload in payloads))
        self.assertTrue(all("previous_response_id" not in payload for payload in payloads))


if __name__ == "__main__":
    unittest.main()
