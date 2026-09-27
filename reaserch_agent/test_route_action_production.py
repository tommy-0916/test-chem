"""Task intent is produced without rewriting a selected route's facts."""

from __future__ import annotations

import unittest

from chem_agent_contracts.v2 import canonical_digest

from reaserch_agent.route_action_production import (
    action_for_selected_stage_arm, finalize_selected_route_action_intent,
)
from reaserch_agent.stage_task_coverage import (
    StageArmActionIntentV1, StageArmV1, StageTaskRequirementsV1,
)
from reaserch_agent.test_route_publish_handoff import QUERY, _fixture


class RouteActionProductionTest(unittest.TestCase):
    def _requirements(self, decision, stage):
        return StageTaskRequirementsV1(
            query_digest=canonical_digest(QUERY),
            observation_point=stage.observation_point,
            stage=stage,
            arms=[StageArmV1(
                arm_id="arm-G7", label="stirred solution", role="experimental",
                origin="task_required", task_excerpt=QUERY,
                task_term="stirred nickel salt solution",
                route_goal=decision.goal,
                action_intent=StageArmActionIntentV1(
                    group_label="stirred solution",
                    objective="stir nickel salt solution",
                    expected_observation="uniform solution",
                    completion_condition="stirred solution available",
                ),
            )],
            observation_protocol_status="new_design_pending",
        )

    def test_selected_graph_produces_intent_that_reaches_existing_publish_gate(self):
        agent, state, result, existing_intent, _ = _fixture()
        decision = result.decision
        stage = existing_intent["stage"]
        from chem_agent_contracts.v2 import StageV2
        requirements = self._requirements(decision, StageV2.model_validate(stage))

        produced_stage, action = action_for_selected_stage_arm(
            requirements, "arm-G7", decision,
        )
        self.assertEqual(action.macro_action_id, "MA-G7")
        self.assertEqual(action.experiment_group.sample_id, "sample-G7")
        self.assertEqual(action.planned_operations, ["stir"])
        self.assertEqual(action.experiment_group.group_id, "arm-G7")
        bundle = agent._current_route_evidence_bundle_v2(state, result, action)
        intent = finalize_selected_route_action_intent(
            decision=decision, evidence_bundle=bundle,
            stage=produced_stage, macro_action=action,
        )
        agent._bind_selected_route(
            state, result=result, intent_raw=intent.model_dump(mode="json"),
            branch="B1",
        )
        self.assertEqual(state.route_binding_status_v1, "publishable")
        self.assertEqual(state.research_action_package_v2["macro_action"]["experiment_group"]["group_id"], "arm-G7")

    def test_different_arm_goal_cannot_consume_selected_decision(self):
        _agent, _state, result, existing_intent, _ = _fixture()
        from chem_agent_contracts.v2 import StageV2
        req = self._requirements(
            result.decision, StageV2.model_validate(existing_intent["stage"]),
        )
        changed = req.model_copy(deep=True)
        changed.arms[0].route_goal.goal_id = "other-goal"
        with self.assertRaisesRegex(ValueError, "goal differs"):
            action_for_selected_stage_arm(changed, "arm-G7", result.decision)


if __name__ == "__main__":
    unittest.main()
