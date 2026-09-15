"""Offline state and retry tests for Responses diagnostic persistence."""

from __future__ import annotations

import json
import os
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_skills.llm_retry import (
    is_retryable_gateway_error,
    is_terminal_gateway_error,
)
from agent_skills.responses_diagnostics import get_responses_diagnostics
from reaserch_agent.core import BaseAgent
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


def provider_failure() -> ConnectionError:
    error = ConnectionError("raw-private-body sk-private-credential")
    error.responses_diagnostics = {
        "phase": "stream_event",
        "event_type": "response.failed",
        "response_status": "failed",
        "response_id": "resp_offline",
        "request_id": "req_offline",
        "error": {"code": "server_error", "message": "Bearer secret-token rejected"},
        "input": "private prompt must never persist",
    }
    return error


class ResearchResponsesDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "REFINER_LLM_MAX_RETRIES": "2",
            "RESEARCH_AGENT_VERBOSE_STEPS": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sleep = patch("agent_skills.llm_retry.time.sleep")
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def agent(self, side_effect):
        model = Mock(spec=["invoke"])
        model.invoke.side_effect = side_effect
        agent = ResearchAgent.__new__(ResearchAgent)
        BaseAgent.__init__(agent, model=model, max_retries=2)
        agent._web_tool_executor = Mock(return_value=None)
        agent._compact_state_context = Mock(return_value="{}")
        return agent, model

    def state(self):
        return ResearchAgentState(event=ResearchEvent("bootstrap", query="offline"))

    def test_each_failed_application_attempt_is_persisted_with_step(self):
        error = provider_failure()
        agent, model = self.agent([error, error, error])
        state = self.state()
        with self.assertLogs("reaserch_agent.core", level="WARNING") as logs:
            with self.assertRaises(RuntimeError) as caught:
                agent._invoke_state_json(state, "macro_plan_design", "offline")
        self.assertEqual(model.invoke.call_count, 3)
        self.assertEqual(self.sleep_mock.call_count, 2)
        self.sleep_mock.assert_any_call(10.0)
        self.assertEqual(len(state.llm_diagnostics), 3)
        self.assertEqual([record["application_attempt"] for record in state.llm_diagnostics], [1, 2, 3])
        self.assertEqual(state.llm_diagnostics[0]["task_name"], "macro_plan_design")
        self.assertEqual(state.llm_diagnostics[0]["exception_type"], "ConnectionError")
        self.assertEqual(state.llm_diagnostics[0]["diagnostics"]["phase"], "stream_event")
        output = json.dumps(state.to_dict()) + str(caught.exception) + " ".join(logs.output)
        self.assertNotIn("raw-private-body", output)
        self.assertNotIn("sk-private-credential", output)
        self.assertNotIn("secret-token", output)
        self.assertNotIn("private prompt", output)
        self.assertIsNotNone(get_responses_diagnostics(caught.exception))

    def test_success_after_retry_retains_failed_attempt_without_extra_calls(self):
        agent, model = self.agent([provider_failure(), SimpleNamespace(content='{"ok":true}')])
        state = self.state()
        result = agent._invoke_state_json(state, "stage_design", "offline")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(model.invoke.call_count, 2)
        self.assertEqual(len(state.llm_diagnostics), 1)
        self.assertEqual(state.errors, [])

    def test_native_failure_is_saved_without_application_retry(self):
        error = provider_failure()
        agent, model = self.agent([])
        agent._web_tool_executor.return_value = object()
        agent._online_research_service = Mock(return_value=SimpleNamespace(as_tool=lambda: object()))
        agent._web_tool_max_rounds = Mock(return_value=2)
        state = self.state()
        with patch("agent_skills.native_tools.invoke_with_tools", side_effect=error) as invoke:
            with self.assertRaises(RuntimeError) as caught:
                agent._invoke_state_json(state, "macro_action_design_bootstrap", "offline")
        invoke.assert_called_once()
        model.invoke.assert_not_called()
        self.sleep_mock.assert_not_called()
        self.assertEqual(len(state.llm_diagnostics), 1)
        self.assertNotIn("application_attempt", state.llm_diagnostics[0])
        self.assertEqual(state.llm_diagnostics[0]["exception_type"], "ConnectionError")
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("raw-private-body", rendered)
        self.assertNotIn("sk-private-credential", rendered)

    def test_bootstrap_failure_keeps_manual_route_and_safe_saved_errors(self):
        agent, model = self.agent([
            provider_failure(), provider_failure(), provider_failure(),
        ])
        agent._step_survey_query_generate = lambda state: agent._invoke_state_json(
            state, "survey_query_generate", "offline",
        )
        agent._record_plan_revision = Mock()
        with self.assertLogs("reaserch_agent", level="WARNING") as logs:
            state = agent._run_b1(self.state())
        self.assertEqual(state.status, "manual_required")
        self.assertEqual(state.next_branch, "B8")
        self.assertEqual(state.current_branch, "B1")
        self.assertEqual(model.invoke.call_count, 3)
        self.assertEqual(len(state.llm_diagnostics), 3)
        self.assertEqual(state.macro_plan, [])
        self.assertNotIn("raw-private-body", json.dumps(state.to_dict()) + " ".join(logs.output))

    def test_state_json_roundtrip_preserves_isolated_records(self):
        agent, _ = self.agent([provider_failure(), SimpleNamespace(content='{"ok":true}')])
        state = self.state()
        agent._invoke_state_json(state, "macro_plan_design", "offline")
        saved = json.loads(json.dumps(state.to_dict()))
        restored = agent._state_from_dict(saved)
        self.assertEqual(restored.llm_diagnostics, state.llm_diagnostics)
        restored.llm_diagnostics[0]["diagnostics"]["error"]["code"] = "changed"
        self.assertEqual(state.llm_diagnostics[0]["diagnostics"]["error"]["code"], "server_error")

    def test_non_responses_failure_uses_safe_metadata_and_is_not_retried(self):
        agent, model = self.agent([ValueError("legacy error")])
        state = self.state()
        with self.assertRaisesRegex(RuntimeError, "retry_owner=shared.*type=ValueError"):
            agent._invoke_state_json(state, "stage_design", "offline")
        self.assertEqual(model.invoke.call_count, 1)
        self.assertEqual(state.llm_diagnostics, [])

    def test_non_responses_native_failure_preserves_exception_identity(self):
        agent, _ = self.agent([])
        agent._web_tool_executor.return_value = object()
        agent._online_research_service = Mock(return_value=SimpleNamespace(as_tool=lambda: object()))
        agent._web_tool_max_rounds = Mock(return_value=2)
        error = ValueError("legacy native error")
        with patch("agent_skills.native_tools.invoke_with_tools", side_effect=error):
            with self.assertRaises(ValueError) as caught:
                agent._invoke_state_json(self.state(), "stage_design", "offline")
        self.assertIs(caught.exception, error)

    def test_native_chat_terminal_failures_are_sanitized_without_retry(self):
        class ChatHTTPError(RuntimeError):
            pass

        class TerminalNativeModel:
            _chem_gateway_max_retries = 3

            def __init__(self, error):
                self.error = error
                self.invoke_count = 0

            def bind_tools(self, tools, **kwargs):
                return self

            def invoke(self, messages):
                self.invoke_count += 1
                raise self.error

        terminal_cases = (
            (401, None),
            (403, "permission_denied"),
            (429, "insufficient_quota"),
        )
        for index, (status, code) in enumerate(terminal_cases, start=1):
            with self.subTest(status=status, code=code):
                canaries = (
                    f"private-message-canary-{index}",
                    f"private-body-canary-{index}",
                    f"private-url-canary-{index}",
                    f"sk-private-key-canary-{index}",
                )
                error = ChatHTTPError(f"{canaries[0]} {canaries[3]}")
                error.status_code = status
                error.body = {
                    "error": {
                        **({"code": code} if code else {}),
                        "message": canaries[1],
                    }
                }
                error.request = SimpleNamespace(
                    url=f"https://example.test/chat?api_key={canaries[2]}"
                )
                model = TerminalNativeModel(error)
                agent = ResearchAgent.__new__(ResearchAgent)
                BaseAgent.__init__(agent, model=model, max_retries=3)
                agent._use_llm = True
                agent._web_tool_executor = Mock(return_value=object())
                agent._compact_state_context = Mock(return_value="{}")
                agent._online_research_service = Mock(return_value=SimpleNamespace(
                    as_tool=lambda: SimpleNamespace(name="online_research")
                ))
                agent._web_tool_max_rounds = Mock(return_value=1)
                state = self.state()

                with self.assertLogs("reaserch_agent.workflow", level="WARNING") as logs:
                    with self.assertRaises(RuntimeError) as caught:
                        agent._step_survey_query_generate(state)

                self.assertEqual(model.invoke_count, 1)
                self.assertTrue(is_terminal_gateway_error(caught.exception))
                self.assertFalse(is_retryable_gateway_error(caught.exception))
                self.assertEqual(
                    ResearchAgent._classify_failure(caught.exception),
                    "network_or_retrieval_error",
                )
                self.assertEqual(len(state.llm_diagnostics), 1)
                metadata = state.llm_diagnostics[0]["gateway_failure"]
                self.assertEqual(metadata["exception_type"], "ChatHTTPError")
                self.assertEqual(metadata["http_status"], status)
                self.assertEqual(metadata["classification"], "terminal")
                if code:
                    self.assertEqual(metadata["error_code"], code)
                rendered = (
                    "".join(traceback.format_exception(caught.exception))
                    + json.dumps(state.to_dict(), ensure_ascii=False)
                    + " ".join(logs.output)
                )
                for canary in canaries:
                    self.assertNotIn(canary, rendered)

                safe_boundary = caught.exception.__context__
                self.assertIsNotNone(safe_boundary)
                self.assertIsNone(safe_boundary.__context__)
                self.assertIsNone(safe_boundary.__cause__)

    def test_completed_http_transport_with_response_failed_is_generation_failure(self):
        error = RuntimeError("response.failed")
        error.responses_diagnostics = {
            "phase": "stream_event", "http_status": 200,
            "event_type": "response.failed", "response_status": "failed",
        }
        self.assertEqual(ResearchAgent._classify_failure(error), "macro_generation_error")

    def test_http_error_and_wrapped_timeout_remain_network_failures(self):
        for status in (400, 429):
            with self.subTest(status=status):
                error = RuntimeError("provider rejected request")
                error.responses_diagnostics = {"phase": "request", "http_status": status}
                self.assertEqual(ResearchAgent._classify_failure(error), "network_or_retrieval_error")
        class ReadTimeout(Exception):
            pass
        timeout = ReadTimeout("private raw timeout details")
        timeout.responses_diagnostics = {"phase": "stream_read", "http_status": 200}
        wrapped = RuntimeError("safe failure")
        wrapped.__cause__ = timeout
        self.assertEqual(ResearchAgent._classify_failure(wrapped), "network_or_retrieval_error")

    def test_actual_retry_wrapper_preserves_timeout_classification_without_raw_trace(self):
        class ReadTimeout(Exception):
            pass
        error = ReadTimeout("raw-private-timeout-body")
        error.responses_diagnostics = {"phase": "stream_read", "http_status": 200}
        agent, model = self.agent([error, error, error])
        with self.assertRaises(RuntimeError) as caught:
            agent._invoke_state_json(self.state(), "macro_plan_design", "offline")
        self.assertEqual(model.invoke.call_count, 3)
        self.assertEqual(ResearchAgent._classify_failure(caught.exception), "network_or_retrieval_error")
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("raw-private-timeout-body", rendered)

    def test_transient_direct_failure_never_switches_to_cli_transport(self):
        from reaserch_agent.utils.llm_factory import CodexResponsesModel

        model = CodexResponsesModel(
            model="offline", api_key="fake", base_url="https://example.test/v1", client=Mock(),
        )
        model._transport_max_retries = 0
        with patch.dict(os.environ, {
            "REFINER_RESPONSES_STREAM": "0",
            "REFINER_RESPONSES_TRANSPORT": "direct",
            "REFINER_RESPONSES_CLI_FALLBACK": "1",
            "REFINER_LLM_MAX_RETRIES": "0",
        }), patch.object(model, "_invoke_direct", side_effect=provider_failure()) as direct, \
             patch.object(model, "_invoke_cli") as cli:
            with self.assertRaises(ConnectionError):
                model.invoke([])
        direct.assert_called_once()
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
