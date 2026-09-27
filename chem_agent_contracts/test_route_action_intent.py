"""Offline checks for explicit selected-route action intent binding."""

from __future__ import annotations

import copy
import unittest

from chem_agent_contracts.route_action_intent import (
    RouteActionIntentV1,
    validate_route_action_intent_v1,
)
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
from chem_agent_contracts.v2 import (
    EvidenceBundleV2,
    EvidenceItemV2,
    ExperimentGroupV2,
    MacroActionV2,
    MacroStepV2,
    MaterialPortV2,
    ProvenanceV2,
    ScientificCompletenessV2,
    ScientificParameterV2,
    StageV2,
    canonical_digest,
)


DOI = "10.1234/route-group"
EXCERPTS = (
    "Mix 2 mmol nickel salt with base.",
    "Heat the mixture to 80 °C for 1 h.",
)
FIELD_PATH = "material_graph[0].parameters[0].value"


def _paper_provenance(index: int) -> ProvenanceV2:
    return ProvenanceV2(
        kind="paper",
        reference=f"E{index + 1}",
        evidence_class="paper_explicit",
        source_path=f"evidence_bundle.items[{index}].excerpt",
        excerpt=EXCERPTS[index],
        source_digest=canonical_digest(EXCERPTS[index]),
    )


def _candidate() -> RouteCandidateV1:
    first, second = _paper_provenance(0), _paper_provenance(1)
    target = RouteTargetV1(
        material="product", desired_state="retained_wet_solid",
        objective="prepare product",
    )
    scope = ExperimentalGroupScopeV1(
        paper_id="paper-A", experimental_group_id="group-A",
        locator="Methods, page 3", source_digest="sha256_" + "a" * 64,
    )
    return RouteCandidateV1(
        route_id="route-A",
        target=target,
        source_scope=scope,
        route_signature=RouteSignatureV1(
            route_family="precipitation",
            target_transformation="precursor_to_precipitate",
            operations=["mix", "isolate"],
            endpoint_state="retained_wet_solid",
        ),
        evidence_bundle=[
            EvidenceItemV2(
                evidence_id=f"E{index + 1}", title="Primary group A",
                doi=DOI, excerpt=excerpt,
            )
            for index, excerpt in enumerate(EXCERPTS)
        ],
        evidence_matrix=[RouteFieldEvidenceV1(
            field_path=FIELD_PATH, value=2, unit="mmol", required=True,
            status="supported", provenance=first, evidence_id="E1",
            source_scope=scope,
        )],
        material_graph=[
            MacroStepV2(
                macro_step_id="MS1", macro_action_id="MA1", sequence=1,
                operation="mix", reagent_or_object="nickel salt and base",
                sample_id="sample-A",
                parameters=[
                    ScientificParameterV2(
                        name="nickel_salt_amount", value=2, unit="mmol",
                        provenance=first,
                    ),
                    ScientificParameterV2(
                        name="temperature", value=80, unit="°C",
                        provenance=second,
                    ),
                ],
                material_inputs=[MaterialPortV2(
                    material_id="nickel_salt", material_instance_id="in-A",
                    name="nickel salt", state="solution", provenance=first,
                )],
                provenance=first,
            ),
            MacroStepV2(
                macro_step_id="MS2", macro_action_id="MA1", sequence=2,
                operation="isolate", reagent_or_object="product",
                sample_id="sample-A",
                material_outputs=[MaterialPortV2(
                    material_id="product", material_instance_id="out-A",
                    name="product", state="retained_wet_solid",
                    provenance=second,
                )],
                provenance=second,
            ),
        ],
        required_capabilities=["mixing"],
        origin="paper_experimental_group",
    )


def _selected(candidate: RouteCandidateV1):
    goal = RouteGoalV1(
        goal_id="goal-A", target=candidate.target, constraint="open",
        required_fields=[FIELD_PATH],
    )

    def receipt(route: RouteCandidateV1) -> RouteValidationReceiptV1:
        return RouteValidationReceiptV1(
            route_id=route.route_id, candidate_digest=canonical_digest(route),
            source_scope_verified=True,
            source_document_kind="primary_paper",
            source_identity_doi=DOI,
            source_attestation_digest="sha256_" + "b" * 64,
            source_route_signature=route.route_signature,
            verified_evidence_ids=["E1", "E2"],
            verified_field_paths=[FIELD_PATH],
            audited_field_paths=[FIELD_PATH],
            verified_graph_step_ids=["MS1", "MS2"],
            scientific_completeness=ScientificCompletenessV2(),
            device_preflight=DevicePreflightV1(
                status="preflight_supported", snapshot_id="device-snapshot-A",
                checked_capabilities=["mixing"],
            ),
        )

    decision = decide_routes(goal, [candidate], receipt)
    assert decision.status == "selected_for_planning", decision.decision_reasons
    return decision


def _bundle(candidate: RouteCandidateV1) -> EvidenceBundleV2:
    items = [
        item.model_copy(update={
            "verification_status": "verified_doi", "full_text_status": "parsed",
        })
        for item in candidate.evidence_bundle
    ]
    return EvidenceBundleV2(
        bundle_id="current-bundle-A", scope="macro_action",
        query="Prepare product from nickel salt", objective="prepare product",
        retrieval_status="success", current_invocation_only=True,
        items=items,
    )


def _fixture(candidate: RouteCandidateV1 | None = None):
    candidate = candidate or _candidate()
    decision = _selected(candidate)
    bundle = _bundle(candidate)
    intent = RouteActionIntentV1(
        route_id=candidate.route_id,
        candidate_digest=canonical_digest(candidate),
        decision_id=decision.decision_id,
        evidence_bundle_id=bundle.bundle_id,
        evidence_bundle_digest=canonical_digest(bundle),
        stage=StageV2(
            stage_id="ST1", name="Precipitation", objective="prepare product",
            observation_point="wet precipitate", completion_condition="solid isolated",
            capability_requirements=["mixing"],
        ),
        macro_action=MacroActionV2(
            macro_action_id="MA1", stage_id="ST1",
            observation_point_id="OP1",
            experiment_group=ExperimentGroupV2(
                group_id="group-A", role="experimental", sample_id="sample-A",
            ),
            objective="mix and isolate product",
            planned_operations=["mix", "isolate"],
            expected_observation="wet precipitate",
            completion_condition="solid isolated",
        ),
    )
    return intent, decision, bundle


class RouteActionIntentTest(unittest.TestCase):
    def test_selected_graph_retains_distinct_parameter_provenances(self) -> None:
        intent, decision, bundle = _fixture()
        before = copy.deepcopy(decision.candidates[0].candidate.material_graph)
        bound = validate_route_action_intent_v1(
            intent, decision=decision, current_evidence_bundle=bundle,
        )
        self.assertEqual(bound.material_graph, before)
        self.assertIsNot(bound.material_graph[0], decision.candidates[0].candidate.material_graph[0])
        first, second = bound.material_graph[0].parameters
        self.assertEqual((first.value, first.unit, first.provenance.reference), (2, "mmol", "E1"))
        self.assertEqual((second.value, second.unit, second.provenance.reference), (80, "°C", "E2"))

    def test_missing_scientific_or_group_intent_is_rejected(self) -> None:
        intent, decision, bundle = _fixture()
        for path in (
            ("stage", "observation_point"),
            ("stage", "completion_condition"),
            ("macro_action", "observation_point_id"),
            ("macro_action", "expected_observation"),
            ("macro_action", "completion_condition"),
            ("macro_action", "experiment_group", "sample_id"),
            ("macro_action", "experiment_group", "group_id"),
        ):
            with self.subTest(path=path):
                payload = intent.model_dump(mode="json")
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = " "
                with self.assertRaises(ValueError):
                    validate_route_action_intent_v1(
                        payload, decision=decision, current_evidence_bundle=bundle,
                    )
        payload = intent.model_dump(mode="json")
        del payload["macro_action"]["experiment_group"]
        with self.assertRaises(ValueError):
            validate_route_action_intent_v1(
                payload, decision=decision, current_evidence_bundle=bundle,
            )

    def test_action_identity_and_operation_order_must_match_graph(self) -> None:
        intent, decision, bundle = _fixture()
        changes = (
            ("macro_action_id", "other-action"),
            ("stage_id", "other-stage"),
            ("planned_operations", ["isolate", "mix"]),
        )
        for key, value in changes:
            with self.subTest(key=key):
                payload = intent.model_dump(mode="json")
                payload["macro_action"][key] = value
                with self.assertRaises(ValueError):
                    validate_route_action_intent_v1(
                        payload, decision=decision, current_evidence_bundle=bundle,
                    )
        payload = intent.model_dump(mode="json")
        payload["macro_action"]["experiment_group"]["sample_id"] = "other-sample"
        with self.assertRaisesRegex(ValueError, "execution sample ID mismatch"):
            validate_route_action_intent_v1(
                payload, decision=decision, current_evidence_bundle=bundle,
            )

    def test_decision_candidate_and_current_bundle_identity_are_bound(self) -> None:
        intent, decision, bundle = _fixture()
        for key, value in (
            ("route_id", "other-route"),
            ("candidate_digest", "sha256_" + "0" * 64),
            ("decision_id", "sha256_" + "0" * 64),
            ("evidence_bundle_id", "other-bundle"),
            ("evidence_bundle_digest", "sha256_" + "0" * 64),
        ):
            with self.subTest(key=key):
                payload = intent.model_dump(mode="json")
                payload[key] = value
                with self.assertRaises(ValueError):
                    validate_route_action_intent_v1(
                        payload, decision=decision, current_evidence_bundle=bundle,
                    )
        changed = bundle.model_copy(deep=True)
        changed.items[1].doi = "10.1234/other-paper"
        payload = intent.model_dump(mode="json")
        payload["evidence_bundle_digest"] = canonical_digest(changed)
        with self.assertRaisesRegex(ValueError, "candidate evidence identity mismatch"):
            validate_route_action_intent_v1(
                payload, decision=decision, current_evidence_bundle=changed,
            )

    def test_graph_paper_reference_must_match_exact_evidence_item(self) -> None:
        candidate = _candidate()
        candidate.material_graph[0].parameters[1].provenance.source_path = (
            "evidence_bundle.items[0].excerpt"
        )
        intent, decision, bundle = _fixture(candidate)
        with self.assertRaisesRegex(ValueError, "source_path does not match evidence_id"):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=bundle,
            )

    def test_graph_parameter_provenance_digest_must_match_excerpt(self) -> None:
        candidate = _candidate()
        candidate.material_graph[0].parameters[1].provenance.source_digest = (
            "sha256_" + "0" * 64
        )
        intent, decision, bundle = _fixture(candidate)
        with self.assertRaisesRegex(ValueError, "source digest does not match"):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=bundle,
            )

    def test_graph_cannot_borrow_a_second_papers_parameter(self) -> None:
        candidate = _candidate()
        candidate.evidence_bundle[1].doi = "10.1234/different-paper"
        intent, decision, bundle = _fixture(candidate)
        with self.assertRaisesRegex(ValueError, "DOI differs from attested route source"):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=bundle,
            )

    def test_attested_source_identity_allows_unasserted_evidence_doi(self) -> None:
        candidate = _candidate()
        for item in candidate.evidence_bundle:
            item.doi = ""
        intent, decision, bundle = _fixture(candidate)
        bound = validate_route_action_intent_v1(
            intent, decision=decision, current_evidence_bundle=bundle,
        )
        self.assertEqual([item.doi for item in bound.evidence_bundle], ["", ""])

    def test_attested_doi_comparison_is_case_insensitive(self) -> None:
        candidate = _candidate()
        for item in candidate.evidence_bundle:
            item.doi = DOI.upper()
        intent, decision, bundle = _fixture(candidate)
        validate_route_action_intent_v1(
            intent, decision=decision, current_evidence_bundle=bundle,
        )

    def test_receipt_must_verify_the_exact_graph_step_ids(self) -> None:
        intent, decision, bundle = _fixture()
        record = decision.candidates[0]
        record.validation.verified_graph_step_ids = ["MS1"]
        record.validation_receipt_digest = canonical_digest(record.validation)
        decision.decision_id = canonical_digest(
            decision.model_dump(mode="json", exclude={"decision_id"})
        )
        intent.decision_id = decision.decision_id
        with self.assertRaisesRegex(ValueError, "step IDs do not match verified receipt"):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=bundle,
            )

    def test_paper_reference_requires_current_verified_parsed_record(self) -> None:
        intent, decision, bundle = _fixture()
        changed = bundle.model_copy(deep=True)
        changed.items[1].full_text_status = "unknown"
        payload = intent.model_dump(mode="json")
        payload["evidence_bundle_digest"] = canonical_digest(changed)
        with self.assertRaisesRegex(ValueError, "not verified and parsed"):
            validate_route_action_intent_v1(
                payload, decision=decision, current_evidence_bundle=changed,
            )

    def test_paper_route_needs_attested_identity_before_binding(self) -> None:
        intent, decision, bundle = _fixture()
        record = decision.candidates[0]
        record.validation.source_attestation_digest = ""
        record.validation_receipt_digest = canonical_digest(record.validation)
        decision.decision_id = canonical_digest(
            decision.model_dump(mode="json", exclude={"decision_id"})
        )
        intent.decision_id = decision.decision_id
        with self.assertRaisesRegex(ValueError, "attested source identity"):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=bundle,
            )

    def test_extra_current_paper_cannot_enter_selected_action_bundle(self) -> None:
        intent, decision, bundle = _fixture()
        extra = bundle.model_copy(deep=True)
        extra.items.append(EvidenceItemV2(
            evidence_id="other-paper", doi="10.1234/other",
            excerpt="Other route uses a different reagent.",
            verification_status="verified_doi", full_text_status="parsed",
        ))
        intent.evidence_bundle_digest = canonical_digest(extra)
        with self.assertRaisesRegex(ValueError, "differs from selected route evidence"):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=extra,
            )

    def test_mutating_previously_validated_decision_is_detected(self) -> None:
        intent, decision, bundle = _fixture()
        decision.candidates[0].candidate.material_graph[0].parameters[0].value = 999
        with self.assertRaises(ValueError):
            validate_route_action_intent_v1(
                intent, decision=decision, current_evidence_bundle=bundle,
            )


if __name__ == "__main__":
    unittest.main()
