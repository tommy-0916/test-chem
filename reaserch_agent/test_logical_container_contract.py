"""Offline regression for Research logical-container validation and repair.

The fixture reproduces the failing eighth-step XRD carrier shape without
depending on ignored experiment results or calling a model/provider.
"""

from __future__ import annotations

from copy import deepcopy
import socket
import tempfile
import unittest
from unittest.mock import patch

from reaserch_agent.prompts.task_prompts import MACRO_STEP_CONTRACT_PROMPT
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


ERROR_POINTER = "/macro_plan/7/container_requirements/2/lid_state"
MACRO_STEP_ID = "MS_MA_S01_R00_008"
CONTAINER_ID = "A02-XRD-CARRIER"


def synthetic_plan(lid_state="none"):
    """A minimal V2-valid plan except for the explicitly varied carrier lid."""
    plan = []
    for number in range(1, 9):
        plan.append({
            "步骤序号": number,
            "macro_step_id": f"MS_MA_S01_R00_{number:03d}",
            "操作": "搅拌" if number < 8 else "XRD滴液检测全流程",
            "试剂/对象": "NiFe LDH 乙醇悬浊液",
            "参数": "室温搅拌 10 min" if number < 8 else "扫描 10 min，步长 0.02°",
            "provenance": {
                "kind": "agent_inferred",
                "rationale": "仅用于离线接口回归测试的固定参数，不是实验执行方案。",
            },
            "material_inputs": [],
            "material_outputs": [],
            "container_requirements": [],
            "intermediate_returns": [],
        })
    plan[-1]["container_requirements"] = [
        {
            "logical_container_id": "A02-XRD-SAMPLE",
            "container_type": "进样瓶",
            "count": 1,
            "capacity_ml": 10.0,
            "lid_state": "open",
        },
        {
            "logical_container_id": "A02-XRD-SOLVENT",
            "container_type": "50mL耐热瓶",
            "count": 1,
            "capacity_ml": 50.0,
            "lid_state": "open",
        },
        {
            "logical_container_id": CONTAINER_ID,
            "container_type": "XRD基底片",
            "count": 1,
            "capacity_ml": None,
            "lid_state": lid_state,
        },
    ]
    return plan


class LogicalContainerContractTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, "connect", side_effect=AssertionError("offline test")).start()
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline test")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("offline test")).start()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # Passing an inert model avoids discovering a configured real provider.
        self.agent = ResearchAgent(
            model=object(), use_llm=True, knowledge_base_dir=directory.name,
            memory_dir=directory.name, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )
        self.state = ResearchAgentState(
            event=ResearchEvent(
                "bootstrap", "分析 NiFe LDH 的未反应基线 XRD", {"device_context": {}},
            ),
            contract_version="v2", stage_route=["XRD表征"],
            current_stage="XRD表征", current_stage_plan="采集基线 XRD",
            branch_history=["B1"],
        )

    def _result(self, lid_state="none"):
        return {"current_stage_plan": "采集基线 XRD", "macro_plan": synthetic_plan(lid_state)}

    def _assert_error_location(self, text):
        for value in (ERROR_POINTER, MACRO_STEP_ID, CONTAINER_ID, "not_applicable"):
            self.assertIn(value, text)
        for value in ("open", "closed", "none", "unknown"):
            self.assertIn(value, text)

    def _call_entrypoint(self, name):
        method = getattr(self.agent, name)
        if name == "_step_device_adaptation_macro_plan_design":
            return method(self.state, "采集基线 XRD")
        return method(self.state)

    def _exercise_entrypoint(self, name, scenario):
        invalid = self._result("not_applicable")
        valid = self._result("none")
        if scenario == "valid":
            responses = [valid]
        elif scenario == "repaired":
            responses = [invalid, valid]
        else:
            responses = [invalid, deepcopy(invalid)]
        originals = deepcopy(responses)
        with patch.object(self.agent, "_step_macro_action_design") as action_design, \
                patch.object(self.agent, "_device_context_macro_step_markers", return_value=[]), \
                patch.object(self.agent, "_device_adaptation_macro_plan_issues", return_value=[]), \
                patch.object(self.agent, "_invoke_state_json", side_effect=responses) as invoke, \
                patch.object(self.agent, "_publish_v2_contract") as publish:
            if scenario == "exhausted":
                with self.assertRaises(RuntimeError) as failure:
                    self._call_entrypoint(name)
                self._assert_error_location(str(failure.exception))
                self.assertEqual(self.state.macro_plan, [])
                self.assertEqual(self.state.research_action_package_v2, {})
                publish.assert_not_called()
            else:
                result = self._call_entrypoint(name)
                carrier = result["macro_plan"][7]["container_requirements"][2]
                self.assertEqual(carrier["lid_state"], "none")
                self.assertEqual(carrier["logical_container_id"], CONTAINER_ID)
                self.assertEqual(result["macro_plan"][7]["macro_step_id"], MACRO_STEP_ID)
                self.assertEqual(self.agent._macro_plan_quality_issues(
                    result["macro_plan"], self.state.event.query, state=self.state,
                ), [])
            self.assertEqual(invoke.call_count, 1 if scenario == "valid" else 2)
            # Only the current macro plan is repaired; action/research setup
            # must not be restarted for an invalid lid field.
            action_design.assert_called_once()
            if scenario != "valid":
                self._assert_error_location(invoke.call_args_list[1].args[2])
        self.assertEqual(responses, originals, "validation must not silently rewrite raw model JSON")
        if scenario != "exhausted":
            # Exercise the real final adapter on the accepted candidate too:
            # a successful repair must produce a usable V2 handoff, not merely
            # satisfy an earlier string-based quality check.
            self.state.macro_plan = result["macro_plan"]
            self.agent._publish_v2_contract(self.state)
            containers = self.state.research_action_package_v2["macro_steps"][7]["logical_containers"]
            self.assertEqual([container["lid_state"] for container in containers], ["open", "open", "none"])
        if scenario != "valid":
            first_output = self.state.raw_llm_outputs[name.removeprefix("_step_")]
            self.assertEqual(first_output["macro_plan"][7]["container_requirements"][2]["lid_state"], "not_applicable")

    def test_bootstrap_accepts_first_valid_candidate_without_retry(self):
        self._exercise_entrypoint("_step_macro_plan_design", "valid")

    def test_bootstrap_repairs_invalid_lid_once(self):
        self._exercise_entrypoint("_step_macro_plan_design", "repaired")

    def test_bootstrap_exhaustion_does_not_publish(self):
        self._exercise_entrypoint("_step_macro_plan_design", "exhausted")

    def test_post_observation_accepts_first_valid_candidate_without_retry(self):
        self._exercise_entrypoint("_step_post_observation_macro_plan_design", "valid")

    def test_post_observation_repairs_invalid_lid_once(self):
        self._exercise_entrypoint("_step_post_observation_macro_plan_design", "repaired")

    def test_post_observation_exhaustion_does_not_publish(self):
        self._exercise_entrypoint("_step_post_observation_macro_plan_design", "exhausted")

    def test_device_adaptation_accepts_first_valid_candidate_without_retry(self):
        self._exercise_entrypoint("_step_device_adaptation_macro_plan_design", "valid")

    def test_device_adaptation_repairs_invalid_lid_once(self):
        self._exercise_entrypoint("_step_device_adaptation_macro_plan_design", "repaired")

    def test_device_adaptation_exhaustion_does_not_publish(self):
        self._exercise_entrypoint("_step_device_adaptation_macro_plan_design", "exhausted")

    def test_quality_gate_locates_original_step_and_container_without_mutation(self):
        plan = synthetic_plan("not_applicable")
        before = deepcopy(plan)
        issues = self.agent._macro_plan_quality_issues(plan, self.state.event.query, state=self.state)
        self.assertEqual(len(issues), 1)
        self._assert_error_location(issues[0])
        self.assertEqual(plan, before)

    def test_normalization_preserves_invalid_lid_for_quality_feedback(self):
        plan = synthetic_plan("not_applicable")
        normalized = self.agent._normalize_macro_plan(plan)
        self.assertEqual(normalized[7]["container_requirements"][2]["lid_state"], "not_applicable")
        self.assertEqual(normalized[7]["macro_step_id"], MACRO_STEP_ID)
        self._assert_error_location("; ".join(self.agent._macro_plan_quality_issues(
            normalized, self.state.event.query, state=self.state,
        )))
        normalized[7]["container_requirements"][2]["lid_state"] = "none"
        self.assertEqual(plan[7]["container_requirements"][2]["lid_state"], "not_applicable")

    def test_normalization_preserves_malformed_container_list_for_rejection(self):
        for malformed in ({"logical_container_id": CONTAINER_ID}, "not-an-array", 3, False, None):
            with self.subTest(value=malformed):
                plan = synthetic_plan()
                plan[7]["container_requirements"] = deepcopy(malformed)
                normalized = self.agent._normalize_macro_plan(plan)
                self.assertEqual(normalized[7]["container_requirements"], malformed)
                issues = self.agent._macro_plan_quality_issues(
                    normalized, self.state.event.query, state=self.state,
                )
                self.assertTrue(issues, "malformed container_requirements must not become an empty valid list")
                self.assertIn("/macro_plan/7/container_requirements", "; ".join(issues))
                self.assertIn(MACRO_STEP_ID, "; ".join(issues))

    def test_missing_container_requirements_remains_a_supported_empty_default(self):
        plan = synthetic_plan()
        del plan[0]["container_requirements"]
        normalized = self.agent._normalize_macro_plan(plan)
        self.assertEqual(normalized[0]["container_requirements"], [])
        self.assertEqual(self.agent._macro_plan_quality_issues(
            normalized, self.state.event.query, state=self.state,
        ), [])

    def test_final_publication_rejects_invalid_lid_with_original_context(self):
        self.state.macro_plan = synthetic_plan("not_applicable")
        self.state.research_action_package_v2 = {"stale_previous_package": True}
        before = deepcopy(self.state.macro_plan)
        with self.assertRaises(ValueError) as failure:
            self.agent._publish_v2_contract(self.state)
        self._assert_error_location(str(failure.exception))
        self.assertEqual(self.state.research_action_package_v2, {})
        self.assertEqual(self.state.macro_plan, before)

    def test_valid_none_carrier_can_be_published_without_changing_bottle_lids(self):
        self.state.macro_plan = synthetic_plan("none")
        self.agent._publish_v2_contract(self.state)
        self.assertTrue(self.state.research_action_package_v2)
        containers = self.state.research_action_package_v2["macro_steps"][7]["logical_containers"]
        self.assertEqual([container["lid_state"] for container in containers], ["open", "open", "none"])
        self.assertEqual(containers[2]["logical_container_id"], CONTAINER_ID)

    def test_prompt_lists_lid_values_and_no_lid_vs_unknown_semantics(self):
        self.assertIn("lid_state", MACRO_STEP_CONTRACT_PROMPT)
        for value in ("open", "closed", "none", "unknown"):
            self.assertIn(value, MACRO_STEP_CONTRACT_PROMPT)
        self.assertIn("不是未知盖状态", MACRO_STEP_CONTRACT_PROMPT)
        self.assertIn("不表示已开盖", MACRO_STEP_CONTRACT_PROMPT)
        self.assertIn("不能替代真实进样瓶", MACRO_STEP_CONTRACT_PROMPT)


if __name__ == "__main__":
    unittest.main()
