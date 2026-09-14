"""Offline behavioral checks for source-grounded Research return contracts."""

from __future__ import annotations

from copy import deepcopy
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from agent_skills.capabilities import project_device_context
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


class ReturnContractTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, "connect", side_effect=AssertionError("offline test")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("offline test")).start()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.agent = ResearchAgent(
            model=None, use_llm=False, knowledge_base_dir=directory.name,
            enable_online_literature=False, enable_web_search=False,
        )
        self.state = ResearchAgentState(
            event=ResearchEvent("bootstrap", "研究6个样品的加热处理与后续XRD表征", {"device_context": {}}),
            stage_route=["XRD表征"], current_stage="XRD表征", branch_history=["B1"],
        )
        self.feedback = {
            "name": "实时晶相转化率", "availability": "declared",
            "required_for_next_step": True, "feedback_kind": "intermediate_feedback",
            "delivery_mode": "automatic",
            "source": {"station_code": "XRD_V1", "operation": "XRD滴液检测全流程"},
        }
        self.plan = [{
            "步骤序号": 1, "操作": "XRD滴液检测全流程", "试剂/对象": "6个乙醇悬浊液样品",
            "参数": "每个样品4 mL，步长0.02°，扫描速度2°/min",
            "container_requirements": [{"logical_container_id": "six_samples", "container_type": "进样瓶", "count": 6}],
            "intermediate_returns": [self.feedback],
        }]

    def issues(self):
        return self.agent._macro_plan_quality_issues(self.plan, self.state.event.query, state=self.state)

    def declare_custom_return(self, kind="intermediate_feedback"):
        self.state.event.constraints["device_context"] = {"workstations": [{
            "station_code": "XRD_V1", "operations": [{
                "name": "XRD滴液检测全流程", "feedback_contract": {
                    kind: {"status": "supported", "fields": ["实时晶相转化率"]},
                },
            }],
        }]}

    def test_default_xrd_cannot_forge_a_declared_field_or_string_source(self):
        xrd = next(item for item in project_device_context({}, "step")["operation_contracts"]
                   if item["station_code"] == "XRD_V1")
        self.assertEqual(xrd["feedback_contract"]["intermediate_feedback"]["status"], "unknown")
        self.assertTrue(any("当前真源未" in issue for issue in self.issues()))
        self.feedback["source"] = "XRD_V1/SKILL.md"
        self.assertTrue(any("source.station_code" in issue for issue in self.issues()))

    def test_custom_declared_field_matches_operation_and_return_timing(self):
        self.declare_custom_return()
        self.assertEqual(self.issues(), [])
        self.feedback["source"]["operation"] = "不存在的操作"
        self.assertTrue(self.issues())
        self.feedback["source"]["operation"] = "XRD滴液检测全流程"
        self.declare_custom_return("returned_data")
        self.assertTrue(any("当成中间反馈" in issue for issue in self.issues()))
        self.feedback["feedback_kind"] = "returned_data"
        self.assertEqual(self.issues(), [])

    def test_declared_control_or_unsupported_return_does_not_count_as_feedback(self):
        self.declare_custom_return()
        operation = self.state.event.constraints["device_context"]["workstations"][0]["operations"][0]
        operation["parameter_contracts"] = [{"name": "实时晶相转化率", "role": "process_control"}]
        operation["feedback_contract"]["intermediate_feedback"]["status"] = "unsupported"
        self.assertTrue(self.issues())

    def test_unknown_required_reading_needs_explicit_external_wait(self):
        self.feedback["availability"] = "undeclared"
        self.assertTrue(any("自动反馈闭环" in issue for issue in self.issues()))
        for mode in ("observation", "manual_handoff"):
            with self.subTest(mode=mode):
                self.feedback["delivery_mode"] = mode
                self.feedback.pop("wait_for", None)
                self.assertTrue(self.issues())
                self.feedback["wait_for"] = "等待真实XRD数据和人工晶相分析后再决定下一批加热条件"
                self.assertEqual(self.issues(), [])
        self.feedback["delivery_mode"] = "automatic"
        self.assertTrue(self.issues())

    def test_optional_unknown_reading_is_not_an_automatic_dependency(self):
        self.feedback.update(availability="undeclared", required_for_next_step=False)
        self.assertEqual(self.issues(), [])

    def test_wait_and_source_fields_survive_normalization_state_and_handoff(self):
        self.feedback.update(
            availability="undeclared", delivery_mode="manual_handoff",
            wait_for="人工上传六个样品的XRD观察结果",
        )
        self.state.macro_plan = self.agent._normalize_macro_plan(self.plan)
        self.agent._build_macro_action_view(self.state)
        restored = self.agent._state_from_dict(self.state.to_dict())
        outgoing = restored.device_adaptation_external_handoff()["待执行 macro plan"][0]
        self.assertEqual(outgoing["intermediate_returns"], [self.feedback])
        self.assertEqual(outgoing["container_requirements"], self.plan[0]["container_requirements"])

    def test_final_publication_and_b2_completion_do_not_bypass_feedback_gate(self):
        self.state.macro_plan = deepcopy(self.plan)
        with self.assertRaisesRegex(ValueError, "feedback contract check failed"):
            self.agent._build_macro_action_view(self.state)
        self.assertEqual(self.state.macro_action, {})
        self.assertEqual(self.agent._classify_failure("macro plan feedback contract check failed"), "macro_quality_error")
        with patch.object(self.agent, "_record_macro_action_outcome"):
            with self.assertRaisesRegex(ValueError, "feedback contract check failed"):
                self.agent._complete_b2(self.state, "completed")
        with self.assertRaisesRegex(ValueError, "feedback contract check failed"):
            self.agent._complete_b2_ignored_device_feedback(self.state, {})

    def test_each_task_loads_only_its_selected_skill_once(self):
        for task, tier in (
            ("stage_design", "experiment"),
            ("macro_action_design_bootstrap", "operation"),
            ("macro_plan_design", "step"),
            ("device_adaptation_macro_plan_design", "step"),
        ):
            with self.subTest(task=task):
                text = self.agent._compact_state_context(self.state, task)
                context = json.loads(text)["constraints"]["device_context"]
                self.assertEqual(context["tier"], tier)
                self.assertTrue(context["instructions"])
                self.assertEqual(len(context["instructions_digest_sha256"]), 64)
                self.assertEqual(text.count(json.dumps(context["instructions"], ensure_ascii=False)), 1)

    def test_bootstrap_post_observation_and_adaptation_retry_invalid_returns(self):
        self.agent._use_llm = True
        invalid = {"current_stage_plan": "加热后XRD观察", "macro_plan": self.plan}
        for method, args in (
            (self.agent._step_macro_plan_design, (self.state,)),
            (self.agent._step_post_observation_macro_plan_design, (self.state,)),
            (self.agent._step_device_adaptation_macro_plan_design, (self.state, "加热后XRD观察")),
        ):
            with self.subTest(method=method.__name__), \
                    patch.object(self.agent, "_step_macro_action_design"), \
                    patch.object(self.agent, "_invoke_state_json", return_value=invalid) as invoke:
                with self.assertRaises(RuntimeError):
                    method(*args)
                self.assertEqual(invoke.call_count, 2)

    def test_all_heuristic_entrypoints_reject_unresolved_return_claims(self):
        with patch.object(self.agent, "_step_macro_action_design"), \
                patch.object(self.agent, "_heuristic_macro_plan_design", return_value={"current_stage_plan": "XRD", "macro_plan": self.plan}), \
                patch.object(self.agent, "_best_structured_reference_macro_plan", return_value=[]):
            with self.assertRaisesRegex(ValueError, "feedback contract check failed"):
                self.agent._step_macro_plan_design(self.state)
        with patch.object(self.agent, "_next_macro_plan_from_reference", return_value=self.plan), \
                patch.object(self.agent, "_best_structured_reference_macro_plan", return_value=[]):
            with self.assertRaisesRegex(ValueError, "feedback contract check failed"):
                self.agent._heuristic_post_observation_macro_plan_design(self.state)
        with patch.object(self.agent, "_device_feasible_repair_macro_plan", return_value=self.plan):
            with self.assertRaisesRegex(ValueError, "feedback contract check failed"):
                self.agent._device_feasible_repair_design_for_non_llm_mode(self.state, "XRD")


if __name__ == "__main__":
    unittest.main()
