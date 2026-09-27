"""B1/B2 opt-in route boundary does not publish an unbound route."""

from __future__ import annotations

from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


GOAL = {
    "goal_id": "goal-1",
    "target": {
        "material": "product", "desired_state": "powder", "objective": "prepare product",
    },
    "constraint": "open",
}


def _fake_result(status: str):
    return SimpleNamespace(decision=SimpleNamespace(
        status=status,
        decision_reasons=["required_evidence_absent"],
        selected_route_id="route-1" if status == "selected_for_planning" else None,
    ))


class RouteWorkflowHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ResearchAgent(
            model=object(), use_llm=False, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )

    def _bootstrap(self, constraints: dict):
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap", query="prepare product", constraints=constraints,
            ),
            contract_version="v2",
        )
        with patch.object(self.agent, "_step_survey_query_generate", return_value=["q"]), patch.object(
            self.agent, "_maybe_acquire_literature"
        ), patch.object(
            self.agent._knowledge_query, "search", return_value=[]
        ), patch.object(
            self.agent, "_step_survey_expansion",
            return_value={"continue_research": False, "new_queries": []},
        ), patch.object(
            self.agent, "_step_paper_protocol_extract", return_value=[]
        ), patch.object(
            self.agent, "_attach_protocol_provenance"
        ), patch.object(
            self.agent, "_step_macro_plan_design",
            return_value={"current_stage_plan": "plan", "macro_plan": [{"操作": "mix"}]},
        ) as planner, patch.object(
            self.agent, "_publish_v2_contract"
        ) as publish, patch.object(
            self.agent, "_record_plan_revision"
        ), patch.object(
            self.agent, "_annotate_macro_plan_sources"
        ), patch.object(
            self.agent, "_build_macro_action_view"
        ), patch.object(
            self.agent, "_step_survey_report_generate", return_value={}
        ), patch.object(
            self.agent, "_step_stage_design", return_value={
                "stage_route": ["synthesis"], "current_stage": "synthesis",
                "stage_route_reason": "reason", "current_stage_reason": "reason",
            },
        ):
            result = self.agent._run_b1(state)
        return result, planner, publish

    def test_unresolved_decision_blocks_b1_before_action(self) -> None:
        with patch.object(
            self.agent, "evaluate_route_decision_v1",
            return_value=_fake_result("unresolved"),
        ) as evaluate:
            state, planner, publish = self._bootstrap({"route_decision_goal_v1": GOAL})
        evaluate.assert_called_once()
        planner.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(state.status, "manual_required")
        self.assertEqual(state.next_branch, "B8")
        self.assertEqual(state.macro_plan, [])
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.device_adaptation_handoff, {})
        self.assertEqual(state.route_binding_status_v1, "decision_unresolved")

    def test_real_empty_kb_bootstrap_abstains_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as kb_dir:
            agent = ResearchAgent(
                model=object(), use_llm=False, enable_memory=False,
                enable_online_literature=False, enable_web_search=False,
                contract_version="v2", knowledge_base_dir=kb_dir,
                memory_dir=kb_dir, max_survey_rounds=1,
            )
            state = agent.run(
                "bootstrap", query="prepare product",
                constraints={"route_decision_goal_v1": GOAL},
            )
        self.assertEqual(state.status, "manual_required")
        self.assertEqual(state.route_decision_v1["status"], "unresolved")
        self.assertEqual(state.macro_plan, [])
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.device_adaptation_handoff, {})

    def test_selected_decision_still_requires_lossless_plan_binding(self) -> None:
        with patch.object(
            self.agent, "evaluate_route_decision_v1",
            return_value=_fake_result("selected_for_planning"),
        ):
            state, planner, publish = self._bootstrap({"route_decision_goal_v1": GOAL})
        planner.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(state.route_binding_status_v1, "selected_unbound")
        self.assertIn("route_plan_binding_unavailable", state.manual_handoff)
        self.assertEqual(state.research_action_package_v2, {})

    def test_malformed_opt_in_goal_abstains_without_legacy_fallback(self) -> None:
        with patch.object(self.agent, "evaluate_route_decision_v1") as evaluate:
            state, planner, publish = self._bootstrap({"route_decision_goal_v1": None})
        evaluate.assert_not_called()
        planner.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(state.route_binding_status_v1, "decision_error")
        self.assertEqual(state.failure_category, "route_decision_error")
        self.assertEqual(state.device_adaptation_handoff, {})

    def test_legacy_path_still_reaches_existing_publication(self) -> None:
        with patch.object(self.agent, "evaluate_route_decision_v1") as evaluate:
            state, planner, publish = self._bootstrap({})
        evaluate.assert_not_called()
        planner.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.route_decision_enabled_v1)

    def test_b2_sticky_route_goal_rechecks_and_blocks_new_action(self) -> None:
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="new_observation", query="prepare product",
                payload={"observation": {"summary": "recovered solid"}},
            ),
            contract_version="v2", stage_route=["synthesis"], current_stage="synthesis",
            route_decision_enabled_v1=True, route_decision_goal_v1=dict(GOAL),
            macro_plan=[{"操作": "mix", "试剂/对象": "salt", "参数": "2 mmol"}],
            research_action_package_v2={"old": "package"},
        )
        with patch.object(
            self.agent, "evaluate_route_decision_v1", return_value=_fake_result("unresolved"),
        ) as evaluate, patch.object(
            self.agent, "_step_observation_stage_fit_judge"
        ) as fit, patch.object(
            self.agent, "_is_unauthorized_device_feedback", return_value=False,
        ) as device_feedback, patch.object(
            self.agent, "_complete_b2_ignored_device_feedback",
        ) as ignored:
            result = self.agent._run_b2(state)
        evaluate.assert_called_once()
        fit.assert_not_called()
        device_feedback.assert_called_once()
        ignored.assert_not_called()
        self.assertEqual(len(result.previous_macro_plan), 1)
        self.assertEqual(result.macro_plan, [])
        self.assertEqual(result.research_action_package_v2, {})
        self.assertEqual(result.device_adaptation_handoff, {})
        self.assertEqual(result.status, "manual_required")

    def test_device_local_feedback_cannot_trigger_route_replan(self) -> None:
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="new_observation", query="prepare product",
                payload={"observation": {"summary": "device local error"}},
            ),
            contract_version="v2", stage_route=["synthesis"], current_stage="synthesis",
            route_decision_enabled_v1=True, route_decision_goal_v1=dict(GOAL),
            macro_plan=[{"操作": "mix", "试剂/对象": "salt", "参数": "2 mmol"}],
            research_action_package_v2={"old": "package"},
        )
        with patch.object(
            self.agent, "evaluate_route_decision_v1"
        ) as evaluate, patch.object(
            self.agent, "_is_unauthorized_device_feedback", return_value=True,
        ), patch.object(
            self.agent, "_complete_b2_ignored_device_feedback", return_value=state,
        ) as ignored:
            result = self.agent._run_b2(state)
        evaluate.assert_not_called()
        ignored.assert_called_once()
        self.assertEqual(result.research_action_package_v2, {"old": "package"})


if __name__ == "__main__":
    unittest.main()
