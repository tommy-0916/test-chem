"""A covered stage is a queue of selected actions, not an executed experiment."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from chem_agent_contracts.route_decision import decide_routes
from chem_agent_contracts.v2 import StageV2, canonical_digest
from reaserch_agent.route_action_production import (
    action_for_selected_stage_arm, finalize_selected_route_action_intent,
)
from reaserch_agent.observation_protocol import ObservationProtocolResolutionV1
from reaserch_agent.stage_task_coverage import (
    StageActionQueueReceiptV1, StageArmActionIntentV1, StageArmV1,
    StageComparisonV1, StageTaskRequirementsV1,
    compose_stage_action_queue, evaluate_stage_coverage,
    record_published_stage_action,
)
from reaserch_agent.test_route_publish_handoff import FIELD_PATH, QUERY, _fixture


PROTOCOL_ID = "trusted-observation-protocol-G7"
TRUSTED_COMPARISON_FIELDS = {"route-comparison": (FIELD_PATH,)}


def _second_decision(first, *, reuse_sample: bool = False):
    original = first.candidates[0]
    candidate = original.candidate.model_copy(deep=True)
    candidate.route_id = "route-G8"
    candidate.source_scope.experimental_group_id = "group-G8"
    candidate.route_signature.target_transformation = "vortex_then_stir_solution"
    for item in candidate.evidence_matrix:
        item.source_scope = candidate.source_scope.model_copy(deep=True)
    step = candidate.material_graph[0]
    step.macro_step_id = "MS-G8"
    step.macro_action_id = "MA-G8"
    step.sample_id = "sample-G7" if reuse_sample else "sample-G8"
    receipt = original.validation.model_copy(deep=True)
    receipt.route_id = candidate.route_id
    receipt.candidate_digest = canonical_digest(candidate)
    receipt.source_route_signature = candidate.route_signature.model_copy(deep=True)
    receipt.verified_graph_step_ids = [step.macro_step_id]
    goal = first.goal.model_copy(deep=True)
    goal.goal_id = "goal-G8"
    result = decide_routes(goal, [candidate], lambda _candidate: receipt)
    assert result.status == "selected_for_planning", result.decision_reasons
    return result


def _covered_stage(*, reuse_sample: bool = False):
    agent, state, result, old_intent, _ = _fixture()
    first = result.decision
    second = _second_decision(first, reuse_sample=reuse_sample)
    stage = StageV2.model_validate(old_intent["stage"], strict=True)
    requirements = StageTaskRequirementsV1(
        query_digest=canonical_digest(QUERY),
        observation_point=stage.observation_point,
        stage=stage,
        arms=[
            StageArmV1(
                arm_id="arm-A", label="primary processing", role="experimental",
                origin="task_required", task_excerpt=QUERY,
                task_term="nickel salt solution", route_goal=first.goal,
                action_intent=StageArmActionIntentV1(
                    group_label="primary processing",
                    comparison_to_arm_ids=["arm-B"],
                    objective="stir nickel salt solution",
                    expected_observation="uniform solution",
                    completion_condition="stirred solution available",
                ),
            ),
            StageArmV1(
                arm_id="arm-B", label="alternate processing", role="control",
                origin="new_design", design_rationale="test an alternate route",
                route_goal=second.goal,
                action_intent=StageArmActionIntentV1(
                    group_label="alternate processing",
                    objective="compare alternate nickel salt processing",
                    expected_observation="uniform solution",
                    completion_condition="comparable stirred solution available",
                ),
            ),
        ],
        comparisons=[StageComparisonV1(
            comparison_id="route-comparison", left_arm_id="arm-A",
            right_arm_id="arm-B", contrast="processing method",
            origin="new_design", design_rationale="compare route effect",
            held_constant_field_paths=[FIELD_PATH],
        )],
        observation_protocol_status="verified",
        observation_protocol_id=PROTOCOL_ID,
    )
    decisions = {"arm-A": first, "arm-B": second}
    coverage = evaluate_stage_coverage(
        requirements, decisions,
        verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
        trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
    )
    assert coverage.status == "covered", coverage.reason_codes
    return agent, state, result, requirements, decisions, coverage


class StageActionQueueTest(unittest.TestCase):
    def test_workflow_abstains_without_independent_comparison_contract(self):
        agent, state, first_result, req, decisions, _ = _covered_stage()
        agent._use_llm = True
        state.event.constraints["stage_task_coverage_v1"] = True
        state.event.constraints["current_observation_point_v1"] = req.observation_point

        def evaluate(_state, goal, *, route_protocols=None):
            decision = next(
                item for item in decisions.values()
                if item.goal.goal_id == goal["goal_id"]
            )
            return first_result if decision == first_result.decision else SimpleNamespace(decision=decision)

        protocol = ObservationProtocolResolutionV1(
            "verified", req.observation_point, protocol_id=PROTOCOL_ID,
        )
        with patch.object(agent, "invoke_text", return_value=json.dumps(
            req.model_dump(mode="json"),
        )), patch.object(
            agent, "_propose_attested_route_protocols", return_value=[],
        ), patch.object(
            agent, "evaluate_route_decision_v1", side_effect=evaluate,
        ), patch(
            "reaserch_agent.observation_protocol.resolve_observation_protocol",
            return_value=protocol,
        ):
            self.assertTrue(agent._route_decision_gate_before_action(state, "B1"))

        self.assertEqual(state.status, "manual_required", state.errors)
        self.assertEqual(state.stage_coverage_report_v1["status"], "incomplete")
        self.assertIn(
            "comparison_conditions_partially_checked:route-comparison",
            state.stage_coverage_report_v1["reason_codes"],
        )
        self.assertEqual(state.stage_action_queue_v1, {})
        self.assertEqual(state.device_adaptation_handoff, {})

    def test_single_arm_without_comparison_still_publishes(self):
        agent, state, result, req, _decisions, _coverage = _covered_stage()
        single = req.model_copy(deep=True)
        single.arms = [single.arms[0]]
        single.arms[0].action_intent.comparison_to_arm_ids = []
        single.comparisons = []
        agent._use_llm = True
        state.event.constraints["stage_task_coverage_v1"] = True
        state.event.constraints["current_observation_point_v1"] = single.observation_point
        protocol = ObservationProtocolResolutionV1(
            "verified", single.observation_point, protocol_id=PROTOCOL_ID,
        )
        with patch.object(agent, "invoke_text", return_value=json.dumps(
            single.model_dump(mode="json"),
        )), patch.object(
            agent, "_propose_attested_route_protocols", return_value=[],
        ), patch.object(
            agent, "evaluate_route_decision_v1", return_value=result,
        ), patch(
            "reaserch_agent.observation_protocol.resolve_observation_protocol",
            return_value=protocol,
        ):
            self.assertTrue(agent._route_decision_gate_before_action(state, "B1"))
        self.assertEqual(state.status, "completed", state.errors)
        self.assertEqual(state.stage_action_queue_v1["publication_status"], "all_actions_published")
        self.assertEqual(state.stage_action_queue_v1["execution_status"], "not_executed")

    def test_composed_queue_keeps_group_and_graph_identity_without_publication(self):
        _, _, _, req, decisions, coverage = _covered_stage()
        queue = compose_stage_action_queue(
            req, decisions, coverage,
            verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
            trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
        )
        self.assertEqual(queue.publication_status, "none_published")
        self.assertEqual(queue.execution_status, "not_executed")
        self.assertEqual([arm.arm_id for arm in queue.arms], ["arm-A", "arm-B"])
        self.assertEqual([arm.experimental_group_id for arm in queue.arms],
                         ["group-G7", "group-G8"])
        self.assertEqual([arm.sample_id for arm in queue.arms],
                         ["sample-G7", "sample-G8"])
        self.assertEqual([arm.macro_action_id for arm in queue.arms],
                         ["MA-G7", "MA-G8"])
        self.assertEqual(queue.comparisons[0].planning_status, "conditions_checked")
        self.assertEqual(queue.comparisons[0].observation_status, "not_measured")
        loaded = StageActionQueueReceiptV1.model_validate(
            json.loads(json.dumps(queue.model_dump(mode="json"))), strict=True,
        )
        self.assertEqual(loaded, queue)

    def test_equal_proposed_field_is_not_an_independent_comparison_review(self):
        _, _, _, req, decisions, _ = _covered_stage()
        unreviewed = evaluate_stage_coverage(
            req, decisions,
            verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
        )
        self.assertEqual(unreviewed.status, "incomplete")
        self.assertIn(
            "comparison_conditions_partially_checked:route-comparison",
            unreviewed.reason_codes,
        )
        self.assertEqual(unreviewed.unchecked_comparison_ids, ["route-comparison"])
        with self.assertRaisesRegex(ValueError, "complete coverage"):
            compose_stage_action_queue(
                req, decisions, unreviewed,
                verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
            )
        mismatched = evaluate_stage_coverage(
            req, decisions,
            verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
            trusted_comparison_fields_by_id={
                "route-comparison": (FIELD_PATH, "macro_steps[0].temperature"),
            },
        )
        self.assertEqual(mismatched.status, "incomplete")
        self.assertIn(
            "comparison_conditions_partially_checked:route-comparison",
            mismatched.reason_codes,
        )

    def test_stale_coverage_or_missing_trusted_protocol_cannot_compose(self):
        _, _, _, req, decisions, coverage = _covered_stage()
        with self.assertRaisesRegex(ValueError, "complete coverage"):
            compose_stage_action_queue(req, decisions, coverage)
        stale = coverage.model_copy(deep=True)
        stale.arms[0].route_id = "other-route"
        with self.assertRaisesRegex(ValueError, "complete coverage"):
            compose_stage_action_queue(
                req, decisions, stale,
                verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
                trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
            )

    def test_runtime_pending_protocol_can_be_planned_with_trusted_resolver(self):
        _, _, _, req, decisions, _ = _covered_stage()
        pending = req.model_copy(deep=True)
        pending.observation_protocol_status = "runtime_pending"
        pending.observation_resolver_paths = ["device.measure_sample_mount"]
        resolver = {PROTOCOL_ID: ["device.measure_sample_mount"]}
        coverage = evaluate_stage_coverage(
            pending, decisions, trusted_runtime_resolvers=resolver,
            trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
        )
        self.assertEqual(coverage.status, "covered", coverage.reason_codes)
        queue = compose_stage_action_queue(
            pending, decisions, coverage, trusted_runtime_resolvers=resolver,
            trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
        )
        self.assertEqual(queue.publication_status, "none_published")
        with self.assertRaisesRegex(ValueError, "complete coverage"):
            compose_stage_action_queue(pending, decisions, coverage)

    def test_distinct_arms_cannot_share_one_executable_sample(self):
        _, _, _, req, decisions, coverage = _covered_stage(reuse_sample=True)
        with self.assertRaisesRegex(ValueError, "reuses a sample ID"):
            compose_stage_action_queue(
                req, decisions, coverage,
                verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
                trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
            )

    def test_publication_receipt_marks_only_one_arm_and_never_execution(self):
        agent, state, result, req, decisions, coverage = _covered_stage()
        queue = compose_stage_action_queue(
            req, decisions, coverage,
            verified_observation_protocol_ids=frozenset({PROTOCOL_ID}),
            trusted_comparison_fields_by_id=TRUSTED_COMPARISON_FIELDS,
        )
        stage, action = action_for_selected_stage_arm(
            req, "arm-A", result.decision,
        )
        bundle = agent._current_route_evidence_bundle_v2(state, result, action)
        intent = finalize_selected_route_action_intent(
            decision=result.decision, evidence_bundle=bundle,
            stage=stage, macro_action=action,
        )
        agent._bind_selected_route(
            state, result=result, intent_raw=intent.model_dump(mode="json"),
            branch="B1",
        )
        published = record_published_stage_action(queue, "arm-A", state.to_dict())
        self.assertEqual(published.publication_status, "partially_published")
        self.assertEqual(published.execution_status, "not_executed")
        self.assertEqual(published.arms[0].publication_status, "research_published")
        self.assertEqual(published.arms[1].publication_status, "planned_only")
        self.assertEqual(published.comparisons[0].observation_status, "not_measured")
        self.assertEqual(
            record_published_stage_action(published, "arm-A", state.to_dict()),
            published,
        )
        with self.assertRaisesRegex(ValueError, "differs from queued arm"):
            record_published_stage_action(queue, "arm-B", state.to_dict())


if __name__ == "__main__":
    unittest.main()
