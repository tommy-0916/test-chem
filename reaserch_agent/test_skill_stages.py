"""Offline contracts for progressive Research skills and planning order."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.test_research_agent import CapturingModel
from reaserch_agent.workflow import ResearchAgent


CONTEXT = {
    "source": "fixture",
    "compact_workstation_capabilities": "LEGACY_SECRET_IO",
    "workstations": [{
        "station_code": "XRD_V1", "display_name": "衍射表征", "availability": "busy",
        "experiment_capabilities": [{"id": "xrd", "name": "XRD", "support_status": "supported"}],
        "operations": [{
            "name": "XRD测试", "input": {"container": "SECRET_IO"},
            "output": {"sample": "粉末"},
            "container_contract": {"compatible": ["测试载台"]},
            "feedback_contract": {"intermediate_feedback": {"status": "unknown"}},
        }],
    }],
}


class SkillStageTests(unittest.TestCase):
    def setUp(self):
        self.agent = ResearchAgent(
            model=None, use_llm=False,
            enable_online_literature=False, enable_web_search=False,
        )
        self.state = ResearchAgentState(
            event=ResearchEvent("bootstrap", "制备样品并完成 XRD", {"device_context": deepcopy(CONTEXT)}),
            stage_route=["完成 XRD 观察"], current_stage="完成 XRD 观察",
        )

    def test_task_tiers_include_all_retry_and_repair_paths(self):
        expected = {
            "survey_query_generate": "experiment",
            "abnormal_observation_survey_query_generate": "experiment",
            "stage_design": "experiment", "stage_route_repair_assess": "experiment",
            "new_route_stage_design": "experiment",
            "macro_action_design_bootstrap": "operation",
            "macro_action_design_device_adaptation": "operation",
            "macro_plan_design_retry_1": "step",
            "post_observation_macro_plan_design_retry_1": "step",
            "device_adaptation_macro_plan_design_quality_feedback_retry": "step",
        }
        for task, tier in expected.items():
            with self.subTest(task=task):
                self.assertEqual(self.agent._device_tier_for_task(task), tier)

    def test_state_context_has_no_deeper_skill_or_history_leak(self):
        self.state.raw_llm_outputs = {"old_tool_output": {"device_context": "HISTORY_SECRET_IO"}}
        self.state.previous_macro_plan = [{"container_requirements": [{"name": "PREVIOUS_SECRET_IO"}]}]
        self.state.latest_observation = {"device_context": deepcopy(CONTEXT), "skill_content": "FEEDBACK_SECRET_IO"}
        for task in ("survey_query_generate", "stage_design", "macro_action_design_bootstrap"):
            with self.subTest(task=task):
                text = self.agent._compact_state_context(self.state, task)
                self.assertNotIn("SECRET_IO", text)
                self.assertIn("busy", text)
        text = self.agent._compact_state_context(self.state, "macro_plan_design_retry_1")
        self.assertIn("SECRET_IO", text)
        self.assertNotIn("LEGACY_SECRET_IO", text)
        self.assertNotIn("HISTORY_SECRET_IO", text)

    def test_bootstrap_designs_action_before_steps(self):
        model = CapturingModel()
        agent = ResearchAgent(
            model=model, use_llm=True, enable_online_literature=False, enable_web_search=False,
            knowledge_base_dir=str(Path(__file__).resolve().parents[1] / "structured_outputs"),
            max_survey_rounds=1,
        )
        result = agent.run("bootstrap", "合成普鲁士蓝并通过 XRD 确认物相", constraints={"device_context": CONTEXT})
        self.assertEqual(result.status, "completed", result.errors)
        action_index = next(i for i, prompt in enumerate(model.prompts) if "## 任务名称\nmacro action design" in prompt)
        step_index = next(i for i, prompt in enumerate(model.prompts) if "## 任务名称\nmacro plan design" in prompt)
        self.assertLess(action_index, step_index)
        self.assertNotIn("SECRET_IO", model.prompts[action_index])
        self.assertIn("SECRET_IO", model.prompts[step_index])
        self.assertEqual(model.prompts[step_index].count("SECRET_IO"), 1)
        self.assertIn("planned_operations", model.prompts[step_index])
        self.assertTrue(result.macro_action["planned_operations"])
        self.assertEqual(result.pending_macro_action, {})
        self.assertTrue(all(step["macro_action_id"] == result.macro_action["macro_action_id"] for step in result.macro_plan))

    def test_new_action_is_not_published_before_old_outcome(self):
        self.state.macro_action = {"macro_action_id": "previous", "objective": "old"}
        designed = self.agent._step_macro_action_design(self.state, "post_observation")
        self.assertEqual(self.state.macro_action["macro_action_id"], "previous")
        self.assertTrue(designed["planned_operations"])
        self.state.macro_plan = [{"步骤序号": 1, "操作": "XRD"}]
        self.agent._build_macro_action_view(self.state)
        self.assertNotEqual(self.state.macro_action["macro_action_id"], "previous")
        self.assertEqual(self.state.macro_action["planned_operations"], designed["planned_operations"])

    def test_step_contract_round_trips_through_normalization_state_and_handoff(self):
        step = {
            "步骤序号": 7, "操作": "混合", "试剂/对象": "A液", "参数": "5 mL，室温 10 min",
            "macro_action_id": "MA", "observation_point_id": "OP", "来源": "agent补全",
            "material_inputs": [{"name": "A液", "state": "液体"}],
            "material_outputs": [{"name": "反应液", "state": "液体"}],
            "container_requirements": [{"logical_container_id": "reaction_batch", "container_type": "进样瓶", "count": 6}],
            "intermediate_returns": [{"name": "实际质量", "availability": "undeclared", "required_for_next_step": False}],
        }
        self.state.macro_plan = self.agent._normalize_macro_plan([step])
        restored = self.agent._state_from_dict(self.state.to_dict())
        outgoing = restored.device_adaptation_external_handoff()["待执行 macro plan"][0]
        for key in ("macro_action_id", "observation_point_id", "material_inputs", "material_outputs", "container_requirements", "intermediate_returns"):
            self.assertEqual(outgoing[key], step[key])
        self.assertEqual(outgoing["步骤序号"], 1)
        self.assertNotIn("瓶号", json.dumps(outgoing, ensure_ascii=False))

    def test_legacy_state_has_empty_pending_action(self):
        restored = self.agent._state_from_dict({"event": {"event_type": "bootstrap"}, "macro_plan": []})
        self.assertEqual(restored.pending_macro_action, {})

    def test_step_quality_rejects_entity_allocation_and_unsourced_returns(self):
        step = {
            "操作": "混合", "试剂/对象": "A液", "参数": "5 mL，室温 10 min",
            "container_requirements": [{"bottle_id": "actual_6", "count": 0}],
            "intermediate_returns": [{"name": "实际质量", "availability": "declared"}],
        }
        issues = self.agent._macro_plan_quality_issues([step], "制备样品")
        self.assertTrue(any("实体" in issue for issue in issues))
        self.assertTrue(any("count" in issue for issue in issues))
        self.assertTrue(any("source" in issue for issue in issues))

    def test_both_automatic_acquisition_paths_use_unified_service(self):
        self.agent._online_literature = True
        service = SimpleNamespace(run=mock.Mock(return_value={"seeds": [], "errors": []}))
        with mock.patch.object(self.agent, "_online_research_service", return_value=service):
            self.agent._maybe_acquire_literature(self.state)
            self.agent._maybe_acquire_repair_literature(self.state, ["NiFe XRD"])
        calls = service.run.call_args_list
        self.assertEqual([call.kwargs["mode"] for call in calls], ["bootstrap", "repair"])
        self.assertEqual(calls[1].kwargs["stage"], self.state.current_stage)

    def test_unsupported_native_configuration_does_not_become_missing_evidence(self):
        from agent_skills.native_tools import NativeToolConfigurationError
        self.agent._online_literature = True
        error = NativeToolConfigurationError("native tools unsupported")
        service = SimpleNamespace(run=mock.Mock(side_effect=error))
        with mock.patch.object(self.agent, "_online_research_service", return_value=service):
            with self.assertRaises(NativeToolConfigurationError):
                self.agent._maybe_acquire_literature(self.state)
            with self.assertRaises(NativeToolConfigurationError):
                self.agent._maybe_acquire_repair_literature(self.state, ["XRD"])
        self.assertEqual(self.agent._classify_failure(error), "configuration_error")

    def test_current_xrd_capability_does_not_force_offline_or_universal_drying(self):
        self.state.latest_observation = {"summary": "旧设备曾不支持 XRD；当前设备已更新"}
        plan = [{"操作": "XRD 工作站表征", "试剂/对象": "可用测试样品", "参数": "按已有样品状态采集数据"}]
        issues = self.agent._device_adaptation_macro_plan_issues(self.state, plan)
        self.assertFalse(any("XRD" in issue or "drying" in issue for issue in issues))

    def test_declared_weighing_has_no_legacy_blanket_ban(self):
        self.state.event.constraints["device_context"]["workstations"][0]["operations"].append({"name": "固体进样"})
        plan = [{"操作": "称取前驱体配制", "试剂/对象": "NaCl", "参数": "称取 1 g 溶于 20 mL 水"}]
        issues = self.agent._device_context_macro_quality_issues(self.state, plan)
        self.assertFalse(any("固体称量" in issue or "体积偏大" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
