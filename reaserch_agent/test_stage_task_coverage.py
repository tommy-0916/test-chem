from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from chem_agent_contracts.v2 import canonical_digest
from reaserch_agent.stage_task_coverage import (
    StageTaskRequirementsV1, _explicit_coordinated_materials,
    evaluate_stage_coverage,
    propose_stage_task_requirements,
)
from reaserch_agent.test_route_publish_handoff import _fixture


QUERY = (
    "制备两种不同 Fe 引入方式的 NiFe 样品，并设置 Ni 基催化剂及"
    "物理混合样品作为对照，先比较 XRD，后续再测 XPS 和 OER。"
)
OTHER_QUERY = (
    "比较两种不同 Mn 引入方式的 CoOOH 样品，并设置无 Mn 的 CoOOH "
    "及 CoOOH/MnOOH 物理混合样品作为对照，当前先测 XRD。"
)


def _arm(arm_id: str, term: str, role: str, query: str) -> dict:
    return {
        "arm_id": arm_id,
        "label": term,
        "role": role,
        "origin": "task_required",
        "task_excerpt": query,
        "task_term": term,
        "route_goal": {
            "goal_id": "goal-" + arm_id,
            "target": {
                "material": term,
                "desired_state": "powder",
                "objective": "prepare " + term,
            },
            "constraint": "open",
        },
        "action_intent": {
            "group_label": term,
            "comparison_to_arm_ids": [],
            "objective": "prepare " + term,
            "expected_observation": "XRD pattern",
            "completion_condition": "interpretable XRD pattern",
        },
    }


def _proposal(query: str, *, omit_mix: bool = False) -> dict:
    arms = [
        _arm("var-a", "不同 Fe 引入方式", "experimental", query),
        _arm("var-b", "不同 Fe 引入方式", "experimental", query),
        _arm("matrix", "Ni 基催化剂", "control", query),
    ]
    if not omit_mix:
        arms.append(_arm("mix", "物理混合样品", "control", query))
    return {
        "schema_version": "stage-task-requirements/v1",
        "query_digest": canonical_digest(query),
        "observation_point": "XRD",
        "stage": {
            "stage_id": "ST-XRD",
            "name": "current XRD stage",
            "objective": "compare current samples by XRD",
            "observation_point": "XRD",
            "completion_condition": "all required arms have interpretable XRD",
            "capability_requirements": [],
        },
        "arms": arms,
        "comparisons": [
            {
                "comparison_id": "Fe-route-contrast",
                "left_arm_id": "var-a",
                "right_arm_id": "var-b",
                "contrast": "Fe introduction route",
                "origin": "task_required",
                "task_excerpt": "不同 Fe 引入方式",
                "held_constant_field_paths": [],
            },
            {
                "comparison_id": "Ni-control",
                "left_arm_id": "var-a",
                "right_arm_id": "matrix",
                "contrast": "Fe present vs absent",
                "origin": "task_required",
                "task_excerpt": "Ni 基催化剂及物理混合样品作为对照",
                "held_constant_field_paths": [],
            },
            {
                "comparison_id": "physical-mix-control",
                "left_arm_id": "var-a",
                "right_arm_id": "mix",
                "contrast": "physical mixture vs integrated sample",
                "origin": "task_required",
                "task_excerpt": "Ni 基催化剂及物理混合样品作为对照",
                "held_constant_field_paths": [],
            },
        ],
        "observation_protocol_status": "new_design_pending",
    }


class StageTaskCoverageTest(unittest.TestCase):
    def test_explicit_material_comparison_needs_task_grounded_relation_in_chinese_and_english(self):
        cases = (
            (
                "制备 NiFe-LDH 和 CoFe-LDH 样品，比较其 XRD 峰形。",
                "比较其 XRD 峰形",
            ),
            (
                "Prepare NiFe-LDH and CoFe-LDH samples; compare their XRD patterns.",
                "compare their XRD patterns",
            ),
            (
                "比较 NiFe-LDH 和 CoFe-LDH 的 XRD 峰形。",
                "比较 NiFe-LDH 和 CoFe-LDH 的 XRD 峰形",
            ),
            (
                "Compare NiFe-LDH and CoFe-LDH XRD patterns.",
                "Compare NiFe-LDH and CoFe-LDH XRD patterns",
            ),
            (
                "NiFe-LDH vs. CoFe-LDH: compare XRD patterns.",
                "compare XRD patterns",
            ),
        )
        for query, excerpt in cases:
            with self.subTest(query=query):
                proposal = _proposal(query)
                proposal["query_digest"] = canonical_digest(query)
                proposal["arms"] = [
                    _arm("nife", "NiFe-LDH", "experimental", query),
                    _arm("cofe", "CoFe-LDH", "experimental", query),
                ]
                proposal["comparisons"] = []
                missing = propose_stage_task_requirements(
                    query, "XRD", lambda _prompt: proposal,
                )
                self.assertEqual(missing.status, "incomplete")
                self.assertIn(
                    "explicit_material_comparison_missing", missing.reason_codes,
                )

                comparison = {
                    "comparison_id": "material-contrast",
                    "left_arm_id": "nife", "right_arm_id": "cofe",
                    "contrast": "XRD pattern", "origin": "task_required",
                    "task_excerpt": excerpt,
                }
                proposal["comparisons"] = [comparison]
                grounded = propose_stage_task_requirements(
                    query, "XRD", lambda _prompt: proposal,
                )
                self.assertEqual(grounded.status, "ready", grounded.reason_codes)

                proposal["comparisons"] = [{
                    **comparison, "origin": "new_design",
                    "task_excerpt": "", "design_rationale": "model-added comparison",
                }]
                self_attested = propose_stage_task_requirements(
                    query, "XRD", lambda _prompt: proposal,
                )
                self.assertIn(
                    "explicit_material_comparison_missing",
                    self_attested.reason_codes,
                )

    def test_explicit_three_material_comparison_needs_connected_relations(self):
        query = "制备 NiFe-LDH、CoFe-LDH 和 MnFe-LDH 样品，比较其 XRD 峰形。"
        proposal = _proposal(query)
        proposal["query_digest"] = canonical_digest(query)
        proposal["arms"] = [
            _arm("nife", "NiFe-LDH", "experimental", query),
            _arm("cofe", "CoFe-LDH", "experimental", query),
            _arm("mnfe", "MnFe-LDH", "experimental", query),
        ]
        proposal["comparisons"] = [{
            "comparison_id": "nife-cofe", "left_arm_id": "nife",
            "right_arm_id": "cofe", "contrast": "XRD pattern",
            "origin": "task_required", "task_excerpt": "比较其 XRD 峰形",
        }]
        partial = propose_stage_task_requirements(
            query, "XRD", lambda _prompt: proposal,
        )
        self.assertIn("explicit_material_comparison_missing", partial.reason_codes)
        proposal["comparisons"].append({
            "comparison_id": "cofe-mnfe", "left_arm_id": "cofe",
            "right_arm_id": "mnfe", "contrast": "XRD pattern",
            "origin": "task_required", "task_excerpt": "比较其 XRD 峰形",
        })
        connected = propose_stage_task_requirements(
            query, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(connected.status, "ready", connected.reason_codes)

    def test_two_material_preparation_without_comparison_does_not_invent_one(self):
        query = "制备 NiFe-LDH 和 CoFe-LDH 样品，分别记录 XRD。"
        proposal = _proposal(query)
        proposal["query_digest"] = canonical_digest(query)
        proposal["arms"] = [
            _arm("nife", "NiFe-LDH", "experimental", query),
            _arm("cofe", "CoFe-LDH", "experimental", query),
        ]
        proposal["comparisons"] = []
        produced = propose_stage_task_requirements(
            query, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(produced.status, "ready", produced.reason_codes)

    def test_bare_measurement_comparison_is_not_treated_as_material_pair(self):
        self.assertEqual(
            _explicit_coordinated_materials("比较 XRD 和 XPS 的差异。"), [],
        )

    def test_explicit_coordinated_material_cannot_disappear_from_stage(self):
        query = "制备 NiFe-LDH 和 CoFe-LDH 样品，先测 XRD。"
        proposal = _proposal(query)
        proposal["query_digest"] = canonical_digest(query)
        proposal["arms"] = [_arm("nife", "NiFe-LDH", "experimental", query)]
        proposal["comparisons"] = []
        produced = propose_stage_task_requirements(
            query, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(produced.status, "incomplete")
        self.assertIn("task_arm_inventory_unverified", produced.reason_codes)
        self.assertIn(
            "explicit_material_arm_missing:CoFe-LDH", produced.reason_codes,
        )

        complete = deepcopy(proposal)
        complete["arms"].append(_arm("cofe", "CoFe-LDH", "experimental", query))
        produced = propose_stage_task_requirements(
            query, "XRD", lambda _prompt: complete,
        )
        self.assertEqual(produced.status, "ready", produced.reason_codes)

    def test_explicit_sample_variant_needs_distinct_arm(self):
        query = "制备 NiFe 样品 A 和 B，先测 XRD。"
        proposal = _proposal(query)
        proposal["query_digest"] = canonical_digest(query)
        proposal["arms"] = [_arm("sample-A", "NiFe", "experimental", query)]
        proposal["arms"][0]["label"] = "样品 A"
        proposal["comparisons"] = []
        produced = propose_stage_task_requirements(
            query, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(produced.status, "incomplete")
        self.assertIn("explicit_variant_arm_missing:B", produced.reason_codes)

    def test_invalid_model_shape_reports_exact_schema_paths(self):
        prompts = []
        produced = propose_stage_task_requirements(
            QUERY, "XRD", lambda prompt: (
                prompts.append(prompt) or {"sample_arms": [], "comparisons": []}
            ),
        )
        self.assertEqual(produced.status, "incomplete")
        self.assertIn(
            "task_decomposition_schema:missing:arms", produced.reason_codes,
        )
        self.assertIn("JSON Schema:", prompts[0])
        self.assertIn('"schema_version"', prompts[0])
        self.assertIn("variant labels such as A/B in arm_id", prompts[0])

    def test_producer_retains_both_explicit_controls_and_only_current_observation(self):
        produced = propose_stage_task_requirements(
            QUERY, "XRD", lambda _prompt: _proposal(QUERY),
        )
        self.assertEqual(produced.status, "ready", produced.reason_codes)
        self.assertEqual(
            produced.explicit_control_terms,
            ["Ni 基催化剂", "物理混合样品"],
        )
        self.assertEqual(produced.requirements.observation_point, "XRD")
        self.assertNotIn("XPS", produced.requirements.stage.completion_condition)
        self.assertNotIn("OER", produced.requirements.stage.completion_condition)

    def test_omitted_explicit_control_is_not_silently_dropped(self):
        proposal = _proposal(QUERY, omit_mix=True)
        proposal["comparisons"] = proposal["comparisons"][:2]
        produced = propose_stage_task_requirements(QUERY, "XRD", lambda _prompt: proposal)
        self.assertEqual(produced.status, "incomplete")
        self.assertIn(
            "explicit_control_omitted:物理混合样品", produced.reason_codes,
        )

    def test_control_arm_without_comparison_is_incomplete(self):
        proposal = _proposal(QUERY)
        proposal["comparisons"] = proposal["comparisons"][:2]
        produced = propose_stage_task_requirements(QUERY, "XRD", lambda _prompt: proposal)
        self.assertEqual(produced.status, "incomplete")
        self.assertIn("control_relation_missing:mix", produced.reason_codes)

    def test_other_chemistry_control_omission_uses_same_generic_rule(self):
        proposal = _proposal(QUERY)
        proposal["query_digest"] = canonical_digest(OTHER_QUERY)
        proposal["arms"] = [
            _arm("var-a", "不同 Mn 引入方式", "experimental", OTHER_QUERY),
            _arm("var-b", "不同 Mn 引入方式", "experimental", OTHER_QUERY),
            _arm("matrix", "无 Mn 的 CoOOH", "control", OTHER_QUERY),
        ]
        proposal["comparisons"] = [{
            "comparison_id": "Mn-route-contrast",
            "left_arm_id": "var-a", "right_arm_id": "var-b",
            "contrast": "Mn introduction route", "origin": "task_required",
            "task_excerpt": "不同 Mn 引入方式",
        }]
        produced = propose_stage_task_requirements(
            OTHER_QUERY, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(produced.status, "incomplete")
        self.assertIn(
            "explicit_control_omitted:CoOOH/MnOOH 物理混合样品",
            produced.reason_codes,
        )

    def test_missing_proposer_is_engineering_gap(self):
        produced = propose_stage_task_requirements(QUERY, "XRD", None)
        self.assertEqual(produced.status, "incomplete")
        self.assertEqual(produced.reason_codes, ["task_decomposer_unavailable"])

    def test_model_cannot_self_certify_observation_protocol(self):
        proposal = _proposal(QUERY)
        proposal["observation_protocol_status"] = "verified"
        proposal["observation_protocol_id"] = "self-claimed-xrd"
        produced = propose_stage_task_requirements(
            QUERY, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(produced.status, "incomplete")
        self.assertIn(
            "observation_protocol_self_attestation_forbidden",
            produced.reason_codes,
        )

    def test_model_cannot_dismiss_observation_protocol(self):
        proposal = _proposal(QUERY)
        proposal["observation_protocol_status"] = "not_required"
        produced = propose_stage_task_requirements(
            QUERY, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(produced.status, "incomplete")
        self.assertIn(
            "observation_not_required_self_attestation_forbidden",
            produced.reason_codes,
        )

    def test_runtime_pending_observation_requires_trusted_resolver(self):
        proposal = _proposal(QUERY)
        proposal["observation_protocol_status"] = "runtime_pending"
        proposal["observation_protocol_id"] = "xrd-protocol-1"
        proposal["observation_resolver_paths"] = ["device.xrd.sample_mount"]
        untrusted = propose_stage_task_requirements(
            QUERY, "XRD", lambda _prompt: proposal,
        )
        self.assertEqual(untrusted.status, "incomplete")
        self.assertIn(
            "observation_runtime_resolver_self_attestation_forbidden",
            untrusted.reason_codes,
        )
        trusted = {"xrd-protocol-1": ["device.xrd.sample_mount"]}
        produced = propose_stage_task_requirements(
            QUERY, "XRD", lambda _prompt: proposal,
            trusted_runtime_resolvers=trusted,
        )
        self.assertEqual(produced.status, "ready", produced.reason_codes)
        requirements = produced.requirements
        assert requirements is not None
        untrusted_report = evaluate_stage_coverage(requirements, {})
        self.assertIn(
            "current_observation_protocol_unverified",
            untrusted_report.reason_codes,
        )
        trusted_report = evaluate_stage_coverage(
            requirements, {}, trusted_runtime_resolvers=trusted,
        )
        self.assertNotIn(
            "current_observation_protocol_unverified",
            trusted_report.reason_codes,
        )

    def test_one_selected_route_does_not_cover_all_stage_arms(self):
        agent, state, result, _, _ = _fixture()
        proposal = _proposal(QUERY)
        proposal["arms"][0]["route_goal"] = result.decision.goal.model_dump(mode="json")
        proposal["arms"][0]["design_rationale"] = "test route target is a synthetic fixture"
        requirements = StageTaskRequirementsV1.model_validate(proposal, strict=True)
        report = evaluate_stage_coverage(
            requirements, {"var-a": result.decision},
        )
        self.assertEqual(report.status, "incomplete")
        self.assertEqual(set(report.missing_arm_ids), {"var-b", "matrix", "mix"})
        self.assertIn("required_sample_arms_uncovered", report.reason_codes)
        self.assertIn("current_observation_protocol_unverified", report.reason_codes)
        self.assertEqual(report.arms[0].route_id, result.decision.selected_route_id)

    def test_workflow_opt_in_evaluates_each_arm_and_preserves_report(self):
        agent, state, result, _, _ = _fixture()
        agent._use_llm = True
        state.event.query = QUERY
        state.event.constraints["stage_task_coverage_v1"] = True
        proposal = _proposal(QUERY)
        proposal["arms"][0]["route_goal"] = result.decision.goal.model_dump(mode="json")
        proposal["arms"][0]["design_rationale"] = "test route target is a synthetic fixture"
        called_goals = []

        def evaluate(_state, goal, *, route_protocols=None):
            called_goals.append(goal["goal_id"])
            if goal["goal_id"] == result.decision.goal_id:
                return result
            raise RuntimeError("source review unavailable")

        state.current_stage_plan = "historical unsupported recipe must stay out"
        state.event.constraints["current_observation_point_v1"] = "XRD"
        with patch.object(agent, "invoke_text", return_value="{}") as invoke, patch.object(
            agent, "_parse_json_response", return_value=proposal,
        ), patch.object(agent, "_propose_attested_route_protocols", return_value=[]), patch.object(
            agent, "evaluate_route_decision_v1", side_effect=evaluate,
        ):
            consumed = agent._route_decision_gate_before_action(state, "B1")
        self.assertTrue(consumed)
        self.assertIn(QUERY, invoke.call_args.args[1])
        self.assertNotIn("historical unsupported recipe", invoke.call_args.args[1])
        self.assertEqual(len(called_goals), 4)
        self.assertEqual(state.failure_category, "stage_coverage_incomplete")
        self.assertEqual(state.stage_coverage_report_v1["status"], "incomplete")
        self.assertEqual(set(state.stage_route_decisions_v1), {
            "var-a", "var-b", "matrix", "mix",
        })
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.device_adaptation_handoff, {})
        reloaded = agent._state_from_dict(state.to_dict())
        self.assertEqual(
            reloaded.stage_coverage_report_v1,
            state.stage_coverage_report_v1,
        )

    def test_workflow_opt_in_without_decomposer_reports_engineering_gap(self):
        agent, state, _, _, _ = _fixture()
        state.event.query = QUERY
        state.event.constraints["stage_task_coverage_v1"] = True
        with patch.object(agent, "evaluate_route_decision_v1") as evaluate:
            consumed = agent._route_decision_gate_before_action(state, "B1")
        self.assertTrue(consumed)
        evaluate.assert_not_called()
        self.assertEqual(state.status, "manual_required")
        self.assertIn("task_decomposer_unavailable", state.manual_handoff)


if __name__ == "__main__":
    unittest.main()
