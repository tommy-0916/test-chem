"""Offline, synthetic tests for route decision contracts and policy."""

from __future__ import annotations

import unittest

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
    RouteDecisionV1,
    RouteSearchBudgetV1,
    RouteSearchRoundV1,
    RouteValidationReceiptV1,
    decide_routes,
    targeted_search_status,
)
from chem_agent_contracts.v2 import (
    EvidenceItemV2,
    MacroStepV2,
    MaterialPortV2,
    ProvenanceV2,
    ScientificCompletenessV2,
    canonical_digest,
)


DOCUMENT_DIGEST = "sha256_" + "a" * 64
DEVICE_SNAPSHOT = "capability_snapshot_" + "b" * 64
FIELD_PATH = "precursor.amount"
EXCERPT = "The precursor solution contained 2 mmol metal salt."


def candidate(
    route_id: str = "R1",
    *,
    paper_id: str | None = None,
    group_id: str = "control",
    field_group_id: str | None = None,
    family: str = "precipitation",
    operations: list[str] | None = None,
    status: str = "supported",
    value: int = 2,
    provenance_kind: str = "paper",
    resolution_path: str = "",
    graph: bool = True,
    capabilities: list[str] | None = None,
) -> RouteCandidateV1:
    paper_id = paper_id or "paper-" + route_id
    scope = ExperimentalGroupScopeV1(
        paper_id=paper_id,
        experimental_group_id=group_id,
        section="Methods",
        locator="Methods / group control",
        source_digest=DOCUMENT_DIGEST,
    )
    field_scope = ExperimentalGroupScopeV1(
        paper_id=paper_id,
        experimental_group_id=field_group_id or group_id,
        section="Methods",
        locator="page 3, paragraph 2",
        source_digest=DOCUMENT_DIGEST,
    )
    evidence_id = "E-" + route_id
    paper_provenance = ProvenanceV2(
        kind="paper", reference=evidence_id, evidence_class="paper_explicit",
        source_path="evidence_bundle.items[0].excerpt", excerpt=EXCERPT,
        source_digest=canonical_digest(EXCERPT),
    )
    if provenance_kind == "agent_inferred":
        field_provenance = ProvenanceV2(
            kind="agent_inferred", reference=evidence_id,
            evidence_class="paper_explicit", rationale="old frozen value",
        )
    else:
        field_provenance = paper_provenance
    target = RouteTargetV1(
        material="target-material", desired_state="retained_wet_solid",
        objective="prepare target material",
    )
    material_graph = []
    if graph:
        material_graph = [MacroStepV2(
            macro_step_id="S-" + route_id,
            macro_action_id="A-" + route_id,
            sequence=1,
            operation="controlled precipitation",
            sample_id="sample-" + route_id,
            material_inputs=[MaterialPortV2(
                material_id="metal-salt", material_instance_id="in-" + route_id,
                name="metal salt", state="solution", provenance=paper_provenance,
            )],
            material_outputs=[MaterialPortV2(
                material_id="target-material", material_instance_id="out-" + route_id,
                name="target material", state="retained_wet_solid",
                provenance=paper_provenance,
            )],
            provenance=paper_provenance,
        )]
    return RouteCandidateV1(
        route_id=route_id,
        target=target,
        source_scope=scope,
        route_signature=RouteSignatureV1(
            route_family=family,
            target_transformation="precursor_to_precipitate",
            precursor_roles=["metal_salt"],
            reagent_roles=["base"],
            operations=operations if operations is not None else ["controlled_dosing", "precipitation"],
            control_modes=["pH_feedback"],
            endpoint_state="retained_wet_solid",
        ),
        evidence_bundle=[EvidenceItemV2(
            evidence_id=evidence_id, title="Synthetic primary procedure",
            excerpt=EXCERPT,
        )],
        evidence_matrix=[RouteFieldEvidenceV1(
            field_path=FIELD_PATH,
            value=value if status == "supported" else None,
            unit="mmol" if status == "supported" else "",
            required=True,
            status=status,
            provenance=field_provenance if status == "supported" else None,
            evidence_id=evidence_id if status == "supported" else "",
            source_scope=field_scope if status == "supported" else None,
            resolution_path=resolution_path,
        )],
        material_graph=material_graph,
        required_capabilities=capabilities if capabilities is not None else ["ph_control"],
        origin="paper_experimental_group",
    )


def goal(
    *, constraint: str = "open", locked_family: str = "",
    locked_scope: ExperimentalGroupScopeV1 | None = None,
    required_fields: list[str] | None = None,
) -> RouteGoalV1:
    return RouteGoalV1(
        goal_id="goal-1",
        target=RouteTargetV1(
            material="target-material", desired_state="retained_wet_solid",
            objective="prepare target material",
        ),
        constraint=constraint,
        locked_family=locked_family,
        locked_scope=locked_scope,
        required_fields=required_fields if required_fields is not None else [FIELD_PATH],
    )


def receipt(
    route: RouteCandidateV1,
    *,
    device_status: str = "preflight_supported",
    snapshot: str = DEVICE_SNAPSHOT,
    source_verified: bool = True,
    source_route_signature: RouteSignatureV1 | None = None,
    audit: ScientificCompletenessV2 | None = None,
    adaptations: int = 0,
    native: bool = True,
    runtime_verified: bool = False,
) -> RouteValidationReceiptV1:
    return RouteValidationReceiptV1(
        route_id=route.route_id,
        candidate_digest=canonical_digest(route),
        source_scope_verified=source_verified,
        source_route_signature=(
            source_route_signature if source_route_signature is not None
            else route.route_signature
        ),
        verified_evidence_ids=[item.evidence_id for item in route.evidence_bundle],
        verified_field_paths=[field.field_path for field in route.evidence_matrix if field.status == "supported"],
        audited_field_paths=[field.field_path for field in route.evidence_matrix],
        verified_runtime_resolution_fields=(
            [field.field_path for field in route.evidence_matrix if field.status == "runtime_pending"]
            if runtime_verified else []
        ),
        verified_graph_step_ids=[step.macro_step_id for step in route.material_graph],
        scientific_completeness=audit if audit is not None else ScientificCompletenessV2(),
        device_preflight=DevicePreflightV1(
            status=device_status, snapshot_id=snapshot,
            checked_capabilities=route.required_capabilities,
        ),
        route_adaptation_count=adaptations,
        native_device_support=native,
    )


class RouteDecisionOfflineTest(unittest.TestCase):
    def test_primary_group_route_selected_for_planning(self) -> None:
        route = candidate()
        result = decide_routes(goal(), [route], receipt)
        self.assertEqual(result.status, "selected_for_planning")
        self.assertEqual(result.selected_route_id, route.route_id)
        self.assertEqual(result.candidates[0].candidate.source_scope.experimental_group_id, "control")
        self.assertEqual(result.candidates[0].validation.candidate_digest, canonical_digest(route))

    def test_document_digest_cannot_replace_v2_excerpt_digest(self) -> None:
        route = candidate()
        route.evidence_matrix[0].provenance.source_digest = DOCUMENT_DIGEST
        result = decide_routes(goal(), [route], receipt)
        self.assertIn(
            "paper_excerpt_digest_mismatch:precursor.amount",
            result.candidates[0].reasons,
        )

    def test_paper_field_must_reference_exact_bundle_excerpt_path(self) -> None:
        route = candidate()
        route.evidence_matrix[0].provenance.source_path = "original-paper.md"
        result = decide_routes(goal(), [route], receipt)
        self.assertIn(
            "paper_source_path_mismatch:precursor.amount",
            result.candidates[0].reasons,
        )

    def test_locked_group_also_locks_declared_source_version(self) -> None:
        route = candidate()
        locked = route.source_scope.model_copy(deep=True)
        locked.source_digest = "sha256_" + "9" * 64
        result = decide_routes(
            goal(constraint="locked_experimental_group", locked_scope=locked),
            [route], receipt,
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIn(
            "locked_experimental_group_mismatch", result.candidates[0].reasons,
        )

    def test_locked_family_requires_verified_source_family(self) -> None:
        route = candidate(family="precipitation")
        verified = route.route_signature.model_copy(deep=True)
        verified.route_family = "reflux"
        result = decide_routes(
            goal(constraint="locked_family", locked_family="precipitation"),
            [route], lambda item: receipt(item, source_route_signature=verified),
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIn(
            "locked_source_route_family_mismatch", result.candidates[0].reasons,
        )

    def test_open_goal_does_not_accept_proposed_family_conflicting_with_review(self) -> None:
        route = candidate(family="precipitation")
        reviewed = route.route_signature.model_copy(deep=True)
        reviewed.route_family = "reflux"
        result = decide_routes(
            goal(), [route],
            lambda item: receipt(item, source_route_signature=reviewed),
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIn(
            "route_signature_mismatch:route_family", result.candidates[0].reasons,
        )

    def test_same_paper_other_group_cannot_supply_field(self) -> None:
        route = candidate(field_group_id="treated")
        result = decide_routes(goal(), [route], receipt)
        self.assertEqual(result.status, "unresolved")
        self.assertIn("experimental_group_mixing:precursor.amount", result.candidates[0].reasons)
        self.assertEqual(result.candidates[0].status, "rejected")

    def test_required_unsupported_rejected(self) -> None:
        route = candidate(status="unsupported")
        result = decide_routes(goal(), [route], receipt)
        self.assertIn("required_unsupported:precursor.amount", result.candidates[0].reasons)

    def test_old_agent_inferred_cannot_upgrade_to_paper_explicit(self) -> None:
        route = candidate(provenance_kind="agent_inferred")
        result = decide_routes(goal(), [route], receipt)
        self.assertIn("agent_inferred_not_convention_backed:precursor.amount", result.candidates[0].reasons)

    def test_numeric_claim_must_appear_with_unit_in_excerpt(self) -> None:
        route = candidate(value=9)
        result = decide_routes(goal(), [route], receipt)
        self.assertIn("paper_quantity_excerpt_mismatch:precursor.amount", result.candidates[0].reasons)
        self.assertIsNone(result.selected_route_id)

    def test_numeric_string_cannot_evade_paper_quantity_check(self) -> None:
        route = candidate(value="999")  # type: ignore[arg-type]
        result = decide_routes(goal(), [route], receipt)
        self.assertIn("quantity_value_not_numeric:precursor.amount", result.candidates[0].reasons)
        self.assertIsNone(result.selected_route_id)

    def test_device_blocked_or_unknown_never_selects(self) -> None:
        route = candidate()
        blocked = decide_routes(goal(), [route], lambda value: receipt(value, device_status="blocked"))
        unknown = decide_routes(goal(), [route], lambda value: receipt(value, device_status="unknown"))
        self.assertIn("required_device_capability_missing", blocked.candidates[0].reasons)
        self.assertEqual(blocked.candidates[0].scientific_status, "admissible")
        self.assertEqual(blocked.candidates[0].device_status, "blocked")
        self.assertIn("device_preflight_unknown", unknown.candidates[0].reasons)
        self.assertIsNone(blocked.selected_route_id)
        self.assertIsNone(unknown.selected_route_id)

    def test_missing_scientific_graph_or_required_field_abstains(self) -> None:
        no_graph = candidate(graph=False)
        missing_field = candidate()
        result_a = decide_routes(goal(), [no_graph], receipt)
        result_b = decide_routes(goal(required_fields=["temperature"]), [missing_field], receipt)
        self.assertIn("material_graph_not_evaluated", result_a.candidates[0].reasons)
        self.assertIn("required_field_not_covered:temperature", result_b.candidates[0].reasons)

    def test_runtime_pending_needs_resolution_path(self) -> None:
        missing = candidate(status="runtime_pending")
        scheduled = candidate(status="runtime_pending", resolution_path="measure before first consumer")
        self.assertIn(
            "runtime_resolution_path_missing:precursor.amount",
            decide_routes(goal(), [missing], receipt).candidates[0].reasons,
        )
        self.assertIn(
            "runtime_resolution_unverified:precursor.amount",
            decide_routes(goal(), [scheduled], receipt).candidates[0].reasons,
        )
        self.assertEqual(
            decide_routes(
                goal(), [scheduled],
                lambda value: receipt(value, runtime_verified=True),
            ).status,
            "selected_for_planning",
        )

    def test_preflight_cannot_claim_support_with_missing_capability(self) -> None:
        route = candidate()
        result = decide_routes(
            goal(), [route],
            lambda value: receipt(value).model_copy(update={
                "device_preflight": DevicePreflightV1(
                    status="preflight_supported",
                    snapshot_id=DEVICE_SNAPSHOT,
                    checked_capabilities=value.required_capabilities,
                    missing_capabilities=["ph_control"],
                ),
            }),
        )
        self.assertIsNone(result.selected_route_id)
        self.assertEqual(result.candidates[0].device_status, "blocked")

    def test_string_quantity_cannot_bypass_excerpt_check(self) -> None:
        route = candidate()
        payload = route.model_dump(mode="json")
        payload["evidence_matrix"][0]["value"] = "9"
        changed = RouteCandidateV1.model_validate(payload)
        result = decide_routes(goal(), [changed], receipt)
        self.assertIn("quantity_value_not_numeric:precursor.amount", result.candidates[0].reasons)

    def test_unverified_graph_step_and_field_cannot_select(self) -> None:
        route = candidate()
        validation = receipt(route).model_copy(update={
            "verified_field_paths": [],
            "verified_graph_step_ids": [],
        })
        result = decide_routes(goal(), [route], lambda _: validation)
        self.assertIn("paper_field_source_unverified:precursor.amount", result.candidates[0].reasons)
        self.assertIn("material_graph_step_unverified:S-R1", result.candidates[0].reasons)

    def test_existing_scientific_audit_blockers_are_hard_rejects(self) -> None:
        route = candidate()
        result = decide_routes(
            goal(), [route],
            lambda value: receipt(
                value, audit=ScientificCompletenessV2(broken_material_lineage=1),
            ),
        )
        self.assertIn("broken_material_lineage", result.candidates[0].reasons)
        self.assertEqual(result.candidates[0].scientific_status, "rejected")
        self.assertEqual(result.candidates[0].device_status, "preflight_supported")

    def test_inconsistent_saved_decision_rejected_on_readback(self) -> None:
        result = decide_routes(goal(), [candidate()], receipt)
        payload = result.model_dump(mode="json")
        self.assertEqual(RouteDecisionV1.model_validate(payload).decision_id, result.decision_id)
        payload["selected_route_id"] = "NONEXISTENT"
        with self.assertRaises(ValueError):
            RouteDecisionV1.model_validate(payload)
        payload = result.model_dump(mode="json")
        payload["goal"]["constraint"] = "locked_family"
        payload["goal"]["locked_family"] = "unrelated"
        with self.assertRaises(ValueError):
            RouteDecisionV1.model_validate(payload)

    def test_source_attestation_change_invalidates_decision_snapshot(self) -> None:
        route = candidate()
        first = decide_routes(
            goal(), [route],
            lambda value: receipt(value).model_copy(update={
                "source_attestation_digest": "sha256_" + "a" * 64,
                "source_identity_doi": "10.1000/source",
                "source_document_kind": "primary_paper",
            }),
        )
        changed = decide_routes(
            goal(), [route],
            lambda value: receipt(value).model_copy(update={
                "source_attestation_digest": "sha256_" + "b" * 64,
                "source_identity_doi": "10.1000/source",
                "source_document_kind": "primary_paper",
            }),
        )
        self.assertNotEqual(first.decision_id, changed.decision_id)
        self.assertNotEqual(first.evidence_snapshot_hash, changed.evidence_snapshot_hash)

    def test_unknown_field_status_cannot_select(self) -> None:
        route = candidate()
        payload = route.model_dump(mode="json")
        payload["evidence_matrix"][0]["status"] = "unknown"
        changed = RouteCandidateV1.model_validate(payload)
        result = decide_routes(goal(), [changed], receipt)
        self.assertIn("field_evidence_unknown:precursor.amount", result.candidates[0].reasons)

    def test_candidate_cannot_self_authorize_science_or_device(self) -> None:
        payload = candidate().model_dump(mode="json")
        payload["scientific_status"] = "admissible"
        with self.assertRaises(ValueError):
            RouteCandidateV1.model_validate(payload)
        payload["scientific_status"] = "unassessed"
        payload["device_status"] = "preflight_supported"
        with self.assertRaises(ValueError):
            RouteCandidateV1.model_validate(payload)

    def test_route_family_and_experimental_group_locks(self) -> None:
        route = candidate()
        family_result = decide_routes(
            goal(constraint="locked_family", locked_family="reflux"), [route], receipt,
        )
        group_result = decide_routes(
            goal(
                constraint="locked_experimental_group",
                locked_scope=ExperimentalGroupScopeV1(
                    paper_id="paper-R1", experimental_group_id="other",
                    locator="Methods", source_digest=DOCUMENT_DIGEST,
                ),
            ), [route], receipt,
        )
        self.assertIn("locked_route_family_mismatch", family_result.candidates[0].reasons)
        self.assertIn("locked_experimental_group_mismatch", group_result.candidates[0].reasons)

    def test_fixed_priority_chooses_unadapted_route(self) -> None:
        routes = [candidate("R1"), candidate("R2")]
        result = decide_routes(
            goal(), routes,
            lambda value: receipt(value, adaptations=1 if value.route_id == "R1" else 0),
        )
        self.assertEqual(result.selected_route_id, "R2")

    def test_unverified_llm_hypothesis_cannot_veto_verified_primary_route(self) -> None:
        primary = candidate("R1")
        proposal_payload = candidate("R2").model_dump(mode="json")
        proposal_payload["origin"] = "hypothesis"
        proposal_payload["source_scope"] = None
        hypothesis = RouteCandidateV1.model_validate(proposal_payload)
        result = decide_routes(goal(), [primary, hypothesis], receipt)
        self.assertEqual(result.selected_route_id, "R1")
        self.assertEqual(result.candidates[1].status, "rejected")

    def test_structural_signature_mismatch_rejects_parameter_transfer(self) -> None:
        route = candidate()
        source = route.route_signature.model_copy(update={
            "operations": ["urea_decomposition", "reflux"],
        })
        result = decide_routes(
            goal(), [route],
            lambda value: receipt(value, source_route_signature=source),
        )
        self.assertIn("route_signature_mismatch:operation_sequence", result.candidates[0].reasons)
        self.assertEqual(result.candidates[0].scientific_status, "rejected")

    def test_tie_abstains_and_order_does_not_change_decision_hash(self) -> None:
        routes = [candidate("R1"), candidate("R2")]
        first = decide_routes(goal(), routes, receipt)
        reversed_ = decide_routes(goal(), list(reversed(routes)), receipt)
        self.assertEqual(first.status, "needs_route_choice")
        self.assertIsNone(first.selected_route_id)
        self.assertEqual(first.decision_id, reversed_.decision_id)

    def test_incomplete_competing_candidate_prevents_selection(self) -> None:
        routes = [candidate("R1"), candidate("R2")]

        def validate(value: RouteCandidateV1) -> RouteValidationReceiptV1:
            if value.route_id == "R2":
                raise RuntimeError("source unavailable")
            return receipt(value)

        result = decide_routes(goal(), routes, validate)
        self.assertEqual(result.status, "unresolved")
        self.assertIn("candidate_validation_incomplete", result.decision_reasons)

    def test_device_snapshot_mismatch_invalidates_selection(self) -> None:
        routes = [candidate("R1"), candidate("R2")]
        result = decide_routes(
            goal(), routes,
            lambda value: receipt(
                value,
                snapshot=DEVICE_SNAPSHOT if value.route_id == "R1" else "capability_snapshot_" + "c" * 64,
                adaptations=0 if value.route_id == "R1" else 1,
            ),
        )
        self.assertEqual(result.status, "unresolved")
        self.assertIn("device_contract_snapshot_inconsistent", result.decision_reasons)

    def test_bounded_search_stops_after_no_new_verified_fact(self) -> None:
        budget = RouteSearchBudgetV1(
            max_search_rounds=3, max_candidate_papers=10,
            max_queries_per_missing_field=2,
        )
        self.assertEqual(targeted_search_status(budget, [], [FIELD_PATH]), "continue")
        self.assertEqual(
            targeted_search_status(
                budget,
                [RouteSearchRoundV1(
                    queries_by_field={FIELD_PATH: 1},
                    candidate_papers_seen=2,
                    new_verified_fact_ids=[],
                )],
                [FIELD_PATH],
            ),
            "no_new_verified_facts",
        )
        self.assertEqual(
            targeted_search_status(
                budget,
                [
                    RouteSearchRoundV1(new_verified_fact_ids=["F1"]),
                    RouteSearchRoundV1(new_verified_fact_ids=["F1"]),
                ],
                [FIELD_PATH],
            ),
            "no_new_verified_facts",
        )


if __name__ == "__main__":
    unittest.main()
