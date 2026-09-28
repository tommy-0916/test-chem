"""Generic selected paper route through the existing Research/Device boundary.

This uses a synthetic experimental-group excerpt and a trusted typed evaluator
receipt. It tests data-flow gates offline; it does not claim independent review
of a real publication or authorize an experiment.
"""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1,
    RouteCandidateV1,
    RouteFieldEvidenceV1,
    RouteGoalV1,
    RouteSignatureV1,
    RouteTargetV1,
)
from chem_agent_contracts.route_decision import (
    DevicePreflightV1,
    RouteValidationReceiptV1,
    decide_routes,
)
from chem_agent_contracts.route_saved_state import validate_selected_route_saved_state_v2
from chem_agent_contracts.v2 import (
    EvidenceItemV2,
    ExperimentGroupV2,
    LogicalContainerV2,
    MacroActionV2,
    MacroStepV2,
    MaterialApplicabilityEvidenceV2,
    MaterialContractStatusV2,
    MaterialOperationImplementationV2,
    MaterialOperationSegmentV2,
    MaterialOperationStepRequirementV2,
    MaterialPortV2,
    MaterialRelationV2,
    ProvenanceV2,
    QuantityV2,
    ResearchActionPackageV2,
    ScientificCompletenessV2,
    ScientificParameterV2,
    StageV2,
    canonical_digest,
)
from device_agent.run_from_research_state import (
    build_device_agent_input_package,
    validate_v2_research_handoff_consistency,
)
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


QUERY = "Prepare a stirred nickel salt solution"
EXCERPT = (
    "Experimental group G7: Stir 2 mmol nickel salt solution in a vial "
    "at 25 °C for 10 min; retain the stirred nickel salt solution."
)
DOI = "10.0000/synthetic-generic-group"
FIELD_PATH = "material_graph[0].parameters[0].value"


def _fixture(
    *, unresolved_materials: bool = False, proposal_evidence_unknown: bool = False,
):
    provenance = ProvenanceV2(
        kind="paper", reference="E-G7", evidence_class="paper_explicit",
        source_path="evidence_bundle.items[0].excerpt", excerpt=EXCERPT,
        source_digest=canonical_digest(EXCERPT),
    )
    evidence = EvidenceItemV2(
        evidence_id="E-G7", title="Synthetic generic experimental group",
        doi=DOI,
        verification_status=(
            "unknown" if proposal_evidence_unknown else "verified_doi"
        ),
        full_text_status="unknown" if proposal_evidence_unknown else "parsed",
        excerpt=EXCERPT,
    )
    scope = ExperimentalGroupScopeV1(
        paper_id="paper-G7", experimental_group_id="group-G7",
        section="Methods", locator="Methods page 3, group G7",
        source_digest="sha256_" + "a" * 64,
    )
    quantity = QuantityV2(
        mode="exact", semantic="planned_target", value=2, unit="mmol"
    )
    status = MaterialContractStatusV2(
        material_inputs="unresolved" if unresolved_materials else "declared",
        material_intermediates="not_applicable",
        material_outputs="declared",
        logical_containers="declared",
        material_relations="declared",
    )
    segment = MaterialOperationSegmentV2(
        segment_id="SEG-G7", material_effect="transform_material",
        source_operation_ref="stir", provenance=provenance,
        device_implementation=MaterialOperationImplementationV2(
            ordered_steps=[MaterialOperationStepRequirementV2(
                role_id="stir-role", capability_id="ambient-mixing"
            )],
            commit_role_id="stir-role",
        ),
    )
    step = MacroStepV2(
        macro_step_id="MS-G7", macro_action_id="MA-G7", sequence=1,
        operation="stir", reagent_or_object="nickel salt solution",
        sample_id="sample-G7",
        parameters=[
            ScientificParameterV2(
                name="nickel_salt_amount", value=2, unit="mmol",
                provenance=provenance,
            ),
            ScientificParameterV2(
                name="temperature", value=25, unit="°C",
                provenance=provenance,
            ),
            ScientificParameterV2(
                name="stir_time", value=10, unit="min",
                provenance=provenance,
            ),
        ],
        quantity_requirements=[{
            "kind": "scientific_input_setpoint",
            "material_id": "nickel salt solution",
            "material": "nickel salt solution",
            "source": "literature", "value": 2, "unit": "mmol",
            "provenance": provenance.model_dump(mode="json"),
        }],
        material_inputs=[MaterialPortV2(
            material_id="nickel salt solution", material_instance_id="in-G7",
            name="nickel salt solution", state="solution", quantity=quantity,
            material_origin="external_inventory", logical_container_id="LC-G7",
            provenance=provenance,
        )],
        material_outputs=[MaterialPortV2(
            material_id="nickel salt solution", material_instance_id="out-G7",
            name="nickel salt solution", state="solution", quantity=quantity,
            material_origin="same_step_relation", logical_container_id="LC-G7",
            provenance=provenance,
        )],
        logical_containers=[LogicalContainerV2(
            logical_container_id="LC-G7", container_type="vial", count=1,
            capacity_ml=20, lid_state="open",
        )],
        material_relations=[MaterialRelationV2(
            relation_id="REL-G7", event_kind="process_same_material",
            input_material_instance_ids=["in-G7"],
            output_material_instance_ids=["out-G7"],
            logical_container_ids=["LC-G7"], quantity_basis="whole_batch",
            source_operation_ref="SEG-G7", provenance=provenance,
        )],
        operation_segments=[segment],
        material_applicability=[MaterialApplicabilityEvidenceV2(
            contract_field="material_intermediates",
            assertion="no_material_intermediates",
            operation_segment_ids=["SEG-G7"], provenance=provenance,
        )],
        material_contract_status=status,
        provenance=provenance,
    )
    target = RouteTargetV1(
        material="nickel salt solution", desired_state="solution",
        objective="prepare stirred nickel salt solution",
    )
    candidate = RouteCandidateV1(
        route_id="route-G7", target=target, source_scope=scope,
        route_signature=RouteSignatureV1(
            route_family="solution_processing",
            target_transformation="stir_solution",
            operations=["stir"], endpoint_state="solution",
        ),
        evidence_bundle=[evidence],
        evidence_matrix=[
            RouteFieldEvidenceV1(
                field_path=FIELD_PATH, value=2, unit="mmol", required=True,
                status="supported", provenance=provenance, evidence_id="E-G7",
                source_scope=scope,
            ),
            RouteFieldEvidenceV1(
                field_path="material_graph[0].material_outputs[0].name",
                value="nickel salt solution", required=True,
                status="supported", provenance=provenance, evidence_id="E-G7",
                source_scope=scope,
            ),
        ],
        material_graph=[step], required_capabilities=["ambient-mixing"],
        origin="paper_experimental_group",
    )
    goal = RouteGoalV1(
        goal_id="goal-G7", target=target, constraint="open",
        required_fields=[FIELD_PATH],
    )
    receipt = RouteValidationReceiptV1(
        route_id=candidate.route_id, candidate_digest=canonical_digest(candidate),
        source_scope_verified=True, source_document_kind="primary_paper",
        source_identity_doi=DOI,
        source_attestation_digest="sha256_" + "b" * 64,
        source_route_signature=candidate.route_signature,
        source_route_signature_review_digest="sha256_" + "c" * 64,
        verified_evidence_ids=["E-G7"], verified_field_paths=[
            FIELD_PATH, "material_graph[0].material_outputs[0].name",
        ],
        audited_field_paths=[
            FIELD_PATH, "material_graph[0].material_outputs[0].name",
        ], verified_graph_step_ids=["MS-G7"],
        scientific_completeness=ScientificCompletenessV2(),
        device_preflight=DevicePreflightV1(
            status="preflight_supported", snapshot_id="device-snapshot-G7",
            checked_capabilities=["ambient-mixing"],
        ),
    )
    decision = decide_routes(goal, [candidate], lambda _candidate: receipt)
    assert decision.status == "selected_for_planning", (
        decision.decision_reasons, [item.reasons for item in decision.candidates]
    )
    stage = StageV2(
        stage_id="ST-G7", name="solution processing",
        objective=target.objective, observation_point="uniform solution",
        completion_condition="stirred solution available",
        capability_requirements=["ambient-mixing"],
    )
    action = MacroActionV2(
        macro_action_id="MA-G7", stage_id=stage.stage_id,
        observation_point_id="OP-G7",
        experiment_group=ExperimentGroupV2(
            group_id="group-G7", role="experimental", sample_id="sample-G7",
            hypothesis="stirring keeps the solution uniform",
        ),
        objective="stir nickel salt solution",
        planned_operations=["stir"],
        expected_observation="uniform solution",
        completion_condition="stirred solution available",
    )
    state = ResearchAgentState(
        event=ResearchEvent("bootstrap", QUERY, {"device_context": {}}),
        contract_version="v2", campaign_id="campaign-G7",
        route_decision_enabled_v1=True,
        route_decision_v1=decision.model_dump(mode="json"),
        branch_history=["B1"],
    )
    agent = ResearchAgent(
        model=object(), use_llm=False, enable_memory=False,
        enable_online_literature=False, enable_web_search=False,
        contract_version="v2",
    )
    result = SimpleNamespace(decision=decision)
    bundle = agent._current_route_evidence_bundle_v2(state, result, action)
    intent = {
        "route_id": candidate.route_id,
        "candidate_digest": canonical_digest(candidate),
        "decision_id": decision.decision_id,
        "evidence_bundle_id": bundle.bundle_id,
        "evidence_bundle_digest": canonical_digest(bundle),
        "stage": stage.model_dump(mode="json"),
        "macro_action": action.model_dump(mode="json"),
    }
    return agent, state, result, intent, bundle.model_dump(mode="json")


class RoutePublishHandoffTest(unittest.TestCase):
    def test_trusted_receipt_promotes_unknown_proposal_evidence_for_current_bundle(self):
        agent, state, result, intent, bundle = _fixture(
            proposal_evidence_unknown=True
        )
        proposal_item = result.decision.candidates[0].candidate.evidence_bundle[0]
        self.assertEqual(proposal_item.verification_status, "unknown")
        self.assertEqual(proposal_item.full_text_status, "unknown")
        self.assertEqual(bundle["items"][0]["verification_status"], "local_file")
        self.assertEqual(bundle["items"][0]["full_text_status"], "local_parsed")
        agent._bind_selected_route(
            state, result=result, intent_raw=intent, branch="B1"
        )
        self.assertEqual(state.route_binding_status_v1, "publishable")
        self.assertEqual(
            state.research_action_package_v2["evidence_bundle"]["items"][0][
                "verification_status"
            ],
            "local_file",
        )

    def test_b1_route_decision_gate_publishes_only_with_explicit_binding_inputs(self):
        agent, state, result, intent, bundle = _fixture()
        state.event.constraints.update({
            "route_decision_goal_v1": result.decision.goal.model_dump(mode="json"),
            "route_action_intent_v1": intent,
        })

        def evaluate(current_state, _goal):
            current_state.route_decision_v1 = result.decision.model_dump(mode="json")
            return result

        with patch.object(agent, "evaluate_route_decision_v1", side_effect=evaluate):
            consumed = agent._route_decision_gate_before_action(state, "B1")
        self.assertTrue(consumed)
        self.assertEqual(state.route_binding_status_v1, "publishable")
        saved = json.loads(json.dumps(state.to_dict(), ensure_ascii=False))
        canonical = validate_v2_research_handoff_consistency(
            saved, saved["macro_plan"]
        )
        self.assertEqual(canonical["route_binding"]["route_id"], "route-G7")

        agent, unbound, result, _, _ = _fixture()
        unbound.event.constraints["route_decision_goal_v1"] = (
            result.decision.goal.model_dump(mode="json")
        )

        def evaluate_unbound(current_state, _goal):
            current_state.route_decision_v1 = result.decision.model_dump(mode="json")
            return result

        with patch.object(
            agent, "evaluate_route_decision_v1", side_effect=evaluate_unbound
        ):
            consumed = agent._route_decision_gate_before_action(unbound, "B1")
        self.assertTrue(consumed)
        self.assertEqual(unbound.route_binding_status_v1, "selected_unbound")
        self.assertEqual(unbound.status, "manual_required")
        self.assertEqual(unbound.research_action_package_v2, {})
        self.assertEqual(unbound.device_adaptation_handoff, {})
        self.assertIn("route_plan_binding_unavailable", unbound.manual_handoff)

    def test_event_supplied_evidence_bundle_is_rejected(self):
        agent, state, result, intent, bundle = _fixture()
        state.event.constraints.update({
            "route_decision_goal_v1": result.decision.goal.model_dump(mode="json"),
            "route_action_intent_v1": intent,
            "route_current_evidence_bundle_v2": bundle,
        })

        def evaluate(current_state, _goal):
            current_state.route_decision_v1 = result.decision.model_dump(mode="json")
            return result

        with patch.object(
            agent, "evaluate_route_decision_v1", side_effect=evaluate
        ), patch.object(
            agent, "_publish_v2_contract", wraps=agent._publish_v2_contract
        ) as publish:
            consumed = agent._route_decision_gate_before_action(state, "B1")
        self.assertTrue(consumed)
        publish.assert_not_called()
        self.assertEqual(state.route_binding_status_v1, "selected_unbound")
        self.assertEqual(state.failure_category, "route_binding_contract_failed")
        self.assertIn("cannot be supplied through event constraints", state.manual_handoff)
        self.assertEqual(state.research_action_package_v2, {})

    def test_b2_cannot_publish_before_previous_observation_closure(self):
        agent, state, result, intent, _ = _fixture()
        state.event.event_type = "new_observation"
        state.event.payload = {"observation": {"summary": "previous result pending"}}
        state.event.constraints.update({
            "route_decision_goal_v1": result.decision.goal.model_dump(mode="json"),
            "route_action_intent_v1": intent,
        })

        def evaluate(current_state, _goal):
            current_state.route_decision_v1 = result.decision.model_dump(mode="json")
            return result

        with patch.object(
            agent, "evaluate_route_decision_v1", side_effect=evaluate
        ), patch.object(
            agent, "_publish_v2_contract", wraps=agent._publish_v2_contract
        ) as publish:
            consumed = agent._route_decision_gate_before_action(state, "B2")
        self.assertTrue(consumed)
        publish.assert_not_called()
        self.assertEqual(state.route_binding_status_v1, "selected_unbound")
        self.assertIn("awaits the previous action", state.manual_handoff)
        self.assertEqual(state.research_action_package_v2, {})

    def test_selected_generic_route_passes_real_publish_and_device_canonical_handoff(self):
        agent, state, result, intent, bundle = _fixture()
        with patch.object(agent, "_publish_v2_contract", wraps=agent._publish_v2_contract) as publish:
            agent._bind_selected_route(
                state, result=result, intent_raw=intent,
                branch="B1",
            )
        publish.assert_called_once()
        self.assertEqual(state.route_binding_status_v1, "publishable")
        self.assertEqual(state.status, "completed")

        saved = json.loads(json.dumps(state.to_dict(), ensure_ascii=False))
        research = validate_selected_route_saved_state_v2(saved)
        received = ResearchActionPackageV2.model_validate(
            validate_v2_research_handoff_consistency(saved, saved["macro_plan"]),
            strict=True,
        )
        self.assertEqual(received.research_contract_hash, research.research_contract_hash)
        self.assertEqual(received.macro_steps[0].material_inputs,
                         research.macro_steps[0].material_inputs)
        self.assertEqual(received.macro_steps[0].material_outputs,
                         research.macro_steps[0].material_outputs)
        self.assertEqual(received.macro_steps[0].material_relations,
                         research.macro_steps[0].material_relations)
        self.assertEqual(received.macro_steps[0].material_inputs[0].quantity.value, 2)
        self.assertEqual(received.macro_steps[0].material_inputs[0].quantity.unit, "mmol")
        self.assertEqual(received.macro_steps[0].material_inputs[0].provenance,
                         research.macro_steps[0].material_inputs[0].provenance)
        self.assertEqual(received.route_binding.route_id, "route-G7")
        self.assertEqual(received.route_binding.experimental_group_id, "group-G7")
        self.assertEqual(received.route_binding.source_locator,
                         "Methods page 3, group G7")
        device_input = build_device_agent_input_package(
            saved, saved["macro_plan"],
            canonical_v2_package=received.model_dump(mode="json", exclude_none=True),
        )
        self.assertEqual(
            device_input["route_published_research_state_v2"], saved,
        )
        self.assertEqual(
            device_input["research_action_package_v2"]["research_contract_hash"],
            received.research_contract_hash,
        )
        self.assertEqual(
            device_input["research_action_package_v2"]["macro_steps"][0]["material_inputs"],
            research.model_dump(mode="json", exclude_none=True)["macro_steps"][0]["material_inputs"],
        )

    def test_stale_current_evidence_binding_blocks_before_publication(self):
        agent, state, result, intent, bundle = _fixture()
        stale = copy.deepcopy(intent)
        stale["evidence_bundle_id"] = "missing-current-bundle"
        stale["evidence_bundle_digest"] = canonical_digest({"missing": True})
        with patch.object(agent, "_publish_v2_contract", wraps=agent._publish_v2_contract) as publish:
            with self.assertRaises(ValueError) as failure:
                agent._bind_selected_route(
                    state, result=result, intent_raw=stale,
                    branch="B1",
                )
        publish.assert_not_called()
        self.assertIn("current macro-action evidence bundle identity mismatch",
                      str(failure.exception))
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.macro_plan, [])

    def test_unresolved_material_contract_still_blocks_existing_publish_gate(self):
        agent, state, result, intent, bundle = _fixture(unresolved_materials=True)
        with patch.object(agent, "_publish_v2_contract", wraps=agent._publish_v2_contract) as publish:
            with self.assertRaises(ValueError) as failure:
                agent._bind_selected_route(
                    state, result=result, intent_raw=intent,
                    branch="B1",
                )
        publish.assert_called_once()
        self.assertIn("material_inputs", str(failure.exception))
        self.assertIn("unresolved", str(failure.exception))
        self.assertEqual(state.research_action_package_v2, {})
        self.assertEqual(state.macro_plan, [])

        agent, state, result, intent, _ = _fixture(unresolved_materials=True)
        state.event.constraints.update({
            "route_decision_goal_v1": result.decision.goal.model_dump(mode="json"),
            "route_action_intent_v1": intent,
        })

        def evaluate(current_state, _goal):
            current_state.route_decision_v1 = result.decision.model_dump(mode="json")
            return result

        with patch.object(agent, "evaluate_route_decision_v1", side_effect=evaluate):
            agent._route_decision_gate_before_action(state, "B1")
        self.assertEqual(state.failure_category, "route_research_publish_gate_failed")
        self.assertIn("material_inputs", state.manual_handoff)
        self.assertEqual(state.research_action_package_v2, {})


if __name__ == "__main__":
    unittest.main()
