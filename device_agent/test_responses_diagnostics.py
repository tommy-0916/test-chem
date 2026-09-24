"""Offline Device error-package and step-attribution regression tests."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_skills.llm_retry import RetryableGatewayError
from device_agent.single_agent import SingleDeviceAgent, SingleDeviceAgentState


def provider_failure() -> ConnectionError:
    error = ConnectionError("raw-private-body sk-private-credential")
    error.responses_diagnostics = {
        "phase": "stream_event",
        "event_type": "response.failed",
        "response_status": "failed",
        "response_id": "resp_device_offline",
        "request_id": "req_device_offline",
        "error": {"code": "server_error", "message": "Bearer secret-token rejected"},
        "output": "private output must never persist",
    }
    return error


class DeviceResponsesDiagnosticsTests(unittest.TestCase):
    def agent(self, side_effect):
        agent = SingleDeviceAgent.__new__(SingleDeviceAgent)
        agent._model = Mock(spec=["invoke"])
        agent._model.invoke.side_effect = side_effect
        agent._assert_workstation_snapshot_current = Mock()
        return agent

    def state(self):
        return SingleDeviceAgentState(research_handoff={}, exp_id="offline")

    def prepare_runtime(self, agent):
        agent._skill_session = None
        agent._txt_format_reference = "offline"
        agent._json_format_reference = "offline"
        agent._strip_untrusted_approval_fields = lambda value: (value, [])
        agent._refresh_workstation_snapshot = Mock()
        agent._workstation_skill_session = Mock(return_value=SimpleNamespace(
            discovery_context=lambda: "offline", truth_digest=lambda: "offline",
        ))
        agent._sync_skill_load_state = Mock()
        agent._device_snapshot_id = Mock(return_value="offline")
        agent._llm_semantic_analysis_enabled = Mock(return_value=True)
        agent._invoke_semantic_analysis = lambda state: agent._invoke_json_object_with_format_retry(
            state, [], step_name="device_semantic_analysis",
        )

    def test_text_failure_keeps_exception_identity_and_no_format_retry(self):
        error = provider_failure()
        agent = self.agent(error)
        state = self.state()
        with self.assertRaises(ConnectionError) as caught:
            agent._invoke_json_object_with_format_retry(state, [], step_name="device_plan")
        self.assertIs(caught.exception, error)
        agent._model.invoke.assert_called_once()
        self.assertEqual(len(state.llm_diagnostics), 1)
        record = state.llm_diagnostics[0]
        self.assertEqual(record["task_name"], "device_plan")
        self.assertEqual(record["exception_type"], "ConnectionError")
        self.assertEqual(record["diagnostics"]["phase"], "stream_event")
        self.assertNotIn("output", record["diagnostics"])

    def test_native_failure_has_same_step_attribution_without_extra_call(self):
        error = provider_failure()
        agent = self.agent([])
        agent._workstation_skill_session = Mock(return_value=SimpleNamespace(
            codes=set(), discovery_context=lambda: "offline", assert_current=lambda: None,
            tool=lambda: object(),
        ))
        state = self.state()
        with patch("device_agent.single_agent.invoke_with_tools", side_effect=error) as invoke:
            with self.assertRaises(ConnectionError) as caught:
                agent._invoke_json_object_with_format_retry(
                    state, [], step_name="device_translation_chunk_2", workstation_tools=True,
                )
        self.assertIs(caught.exception, error)
        invoke.assert_called_once()
        agent._model.invoke.assert_not_called()
        self.assertEqual(state.llm_diagnostics[0]["task_name"], "device_translation_chunk_2")

    def test_failed_package_persists_structured_detail_and_safe_logs(self):
        error = provider_failure()
        agent = self.agent(error)
        self.prepare_runtime(agent)
        with self.assertLogs("device_agent.single_agent", level="ERROR") as logs:
            state = agent.run_state({}, exp_id="offline")
        package = state.terminal_package
        self.assertEqual(package["feedback_type"], "device_internal_error")
        self.assertEqual(package["feedback_route"], "device")
        self.assertEqual(package["failure_scope"], "device_internal")
        self.assertEqual(package["workflow_json"], {})
        self.assertEqual(state.status, "failed")
        diagnostic = package["error_package"]["llm_diagnostics"]
        self.assertEqual(diagnostic["error"]["code"], "server_error")
        self.assertEqual(diagnostic["request_id"], "req_device_offline")
        self.assertEqual(package["error_package"]["llm_failure_step"], "device_semantic_analysis")
        self.assertEqual(package["error_package"]["llm_exception_type"], "ConnectionError")
        self.assertEqual(len(state.llm_diagnostics), 1)
        agent._model.invoke.assert_called_once()
        rendered = json.dumps(state.to_dict()) + " ".join(logs.output)
        for secret in ("raw-private-body", "sk-private-credential", "secret-token", "private output"):
            self.assertNotIn(secret, rendered)
        self.assertIsNone(logs.records[0].exc_info)
        diagnostic["error"]["code"] = "changed"
        self.assertEqual(state.llm_diagnostics[0]["diagnostics"]["error"]["code"], "server_error")

    def test_native_review_failure_is_returned_as_safe_internal_result(self):
        error = provider_failure()
        agent = self.agent([])
        agent._invoke_json_object_with_format_retry = Mock(side_effect=error)
        agent._workstation_skill_session = Mock(return_value=SimpleNamespace(referenced_codes=lambda _: []))
        with patch("builtins.print"):
            result = agent._invoke_workflow_skill_review(
                self.state(), {}, {}, allow_rewrite=False, round_number=2,
            )
        self.assertTrue(result["_device_internal_error"])
        self.assertEqual(result["verdict"], "not_executable")
        self.assertEqual(result["llm_failure_step"], "workflow_skill_review_round_2")
        self.assertEqual(result["llm_diagnostics"]["error"]["code"], "server_error")
        self.assertNotIn("raw-private-body", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))

    def test_consumed_review_failure_is_carried_into_final_package(self):
        agent = self.agent([])
        self.prepare_runtime(agent)
        agent._llm_semantic_analysis_enabled.return_value = False
        diagnostic = {"phase": "stream_event", "event_type": "response.failed"}
        result = {
            "llm_diagnostics": diagnostic,
            "llm_failure_step": "workflow_skill_review_round_2",
            "llm_exception_type": "ConnectionError",
        }
        agent._verified_stage1_core_route_gap_result = Mock(return_value=result)
        agent._plan_is_accepted = Mock(return_value=False)
        agent._stage1_device_local_manual_result = Mock(return_value=None)
        agent._normalize_terminal_package = Mock(return_value={
            "status": "failed", "feedback_type": "device_internal_error",
            "feedback_route": "device", "error_package": {"type": "device_internal_error"},
        })
        state = agent.run_state({}, exp_id="offline")
        package = state.terminal_package["error_package"]
        self.assertEqual(state.status, "failed")
        self.assertEqual(package["llm_failure_step"], "workflow_skill_review_round_2")
        self.assertEqual(package["llm_diagnostics"], diagnostic)
        package["llm_diagnostics"]["phase"] = "changed"
        self.assertEqual(diagnostic["phase"], "stream_event")
        agent._model.invoke.assert_not_called()

    def test_non_responses_error_keeps_legacy_failed_package(self):
        agent = self.agent(ValueError("legacy failure"))
        self.prepare_runtime(agent)
        with self.assertLogs("device_agent.single_agent", level="ERROR"):
            state = agent.run_state({}, exp_id="offline")
        package = state.terminal_package
        self.assertEqual(package["feedback_type"], "device_internal_error")
        self.assertEqual(package["error_package"]["blocking_constraints"], ["ValueError: legacy failure"])
        self.assertNotIn("llm_diagnostics", package["error_package"])
        self.assertEqual(state.llm_diagnostics, [])

    def test_raw_chat_http_error_uses_metadata_only_log_and_state(self):
        class PrivateChatError(RuntimeError):
            status_code = 403
            body = {
                "error": {
                    "code": "access_terminated_error",
                    "message": "PRIVATE_CHAT_BODY",
                }
            }

            def __str__(self):
                return (
                    "PRIVATE_CHAT_BODY "
                    "https://provider.invalid/?key=sk-private-canary"
                )

        agent = self.agent(PrivateChatError())
        self.prepare_runtime(agent)
        with self.assertLogs("device_agent.single_agent", level="ERROR") as logs:
            state = agent.run_state({}, exp_id="offline-chat-error")

        rendered = json.dumps(state.to_dict()) + " ".join(logs.output)
        self.assertNotIn("PRIVATE_CHAT_BODY", rendered)
        self.assertNotIn("sk-private-canary", rendered)
        self.assertNotIn("provider.invalid", rendered)
        self.assertIn("http_status=403", rendered)
        self.assertIsNone(logs.records[0].exc_info)
        agent._model.invoke.assert_called_once()

    def test_success_does_not_add_diagnostics(self):
        agent = self.agent([SimpleNamespace(content='{"ok":true}')])
        state = self.state()
        result = agent._invoke_json_object_with_format_retry(state, [], step_name="device_plan")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(state.llm_diagnostics, [])
        agent._model.invoke.assert_called_once()

    def test_transient_direct_failure_never_switches_to_cli_transport(self):
        from device_agent.utils.llm_factory import CodexResponsesModel

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
            # The direct adapter must preserve retry classification while
            # stripping the provider-controlled body from the public error.
            with self.assertRaises(RetryableGatewayError):
                model.invoke([])
        direct.assert_called_once()
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
