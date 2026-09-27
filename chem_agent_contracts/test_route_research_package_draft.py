"""Focused checks for the nonpublishing typed route package draft."""

from __future__ import annotations

import copy
import unittest

from chem_agent_contracts.route_action_intent import build_route_action_binding_draft_v1
from chem_agent_contracts.route_decision import decide_routes
from chem_agent_contracts.route_research_package_draft import (
    build_route_research_package_draft_v2,
)
from chem_agent_contracts.test_route_action_intent import _candidate, _fixture
from chem_agent_contracts.v2 import (
    RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
    RAW_STEP_DIGEST_SCOPE_V1,
    ResearchActionPackageV2,
    canonical_digest,
    canonical_raw_observations_digest,
    canonical_raw_step_digest,
    canonicalize_v2_device_handoff,
)
from device_agent.run_from_research_state import (
    validate_v2_research_handoff_consistency,
)


def _reviewed_fixture(candidate=None):
    intent, earlier_decision, bundle = _fixture(candidate)
    selected_candidate = earlier_decision.candidates[0].candidate
    receipt = earlier_decision.candidates[0].validation.model_copy(deep=True)
    receipt.source_route_signature_review_digest = canonical_digest({
        "source_route_signature": receipt.source_route_signature,
        "review": "trusted test evaluator",
    })
    decision = decide_routes(
        earlier_decision.goal, [selected_candidate], lambda _candidate: receipt
    )
    intent.decision_id = decision.decision_id
    draft = build_route_action_binding_draft_v1(
        intent, decision=decision, current_evidence_bundle=bundle,
    )
    return draft, decision, bundle


def _build(draft, decision, bundle, **overrides):
    arguments = {
        "decision": decision,
        "current_evidence_bundle": bundle,
        "stage": draft.stage,
        "macro_action": draft.macro_action,
        "campaign_id": "campaign-A",
    }
    arguments.update(overrides)
    return build_route_research_package_draft_v2(draft, **arguments)


class RouteResearchPackageDraftTest(unittest.TestCase):
    def test_preserves_typed_graph_each_provenance_and_wire_hash(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        graph_before = copy.deepcopy(draft.macro_steps)
        package = _build(draft, decision, bundle)

        self.assertIsInstance(package, ResearchActionPackageV2)
        self.assertEqual(package.stage, draft.stage)
        self.assertEqual(package.macro_action, draft.macro_action)
        self.assertEqual(package.macro_steps, graph_before)
        self.assertEqual(package.evidence_bundle, bundle)
        self.assertEqual(
            package.scientific_completeness,
            decision.candidates[0].validation.scientific_completeness,
        )
        self.assertEqual(package.capability_snapshot_id,
                         decision.device_contract_snapshot_hash)
        self.assertEqual(
            [item.provenance.reference for item in package.macro_steps[0].parameters],
            ["E1", "E2"],
        )
        self.assertTrue(package.research_contract_hash.startswith("research_v2_"))
        restored = ResearchActionPackageV2.model_validate(
            package.model_dump(mode="json", exclude_none=True)
        )
        self.assertEqual(restored.research_contract_hash,
                         package.research_contract_hash)
        self.assertEqual(restored.macro_steps, graph_before)

        decision.candidates[0].candidate.material_graph[0].parameters[0].value = 999
        bundle.items[0].excerpt = "changed"
        draft.macro_steps[0].parameters[1].provenance.reference = "changed"
        self.assertEqual(package.macro_steps, graph_before)

    def test_rejects_stale_draft_and_mismatched_explicit_intent(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        forged = draft.model_copy(deep=True)
        forged.macro_steps[0].parameters[0].value = 999
        with self.assertRaisesRegex(ValueError, "differs from current selected route"):
            _build(forged, decision, bundle)

        with self.assertRaisesRegex(TypeError, "current typed RouteDecisionV1"):
            build_route_research_package_draft_v2(
                draft,
                decision=decision.model_dump(mode="json"),
                current_evidence_bundle=bundle,
                stage=draft.stage,
                macro_action=draft.macro_action,
                campaign_id="campaign-A",
            )

        wrong_stage = draft.stage.model_copy(deep=True)
        wrong_stage.observation_point = "different endpoint"
        with self.assertRaisesRegex(ValueError, "explicit stage differs"):
            _build(draft, decision, bundle, stage=wrong_stage)

        wrong_action = draft.macro_action.model_copy(deep=True)
        wrong_action.expected_observation = "different observation"
        with self.assertRaisesRegex(ValueError, "explicit macro_action differs"):
            _build(draft, decision, bundle, macro_action=wrong_action)

    def test_requires_campaign_and_trusted_paper_review_digest(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        with self.assertRaisesRegex(ValueError, "campaign_id"):
            _build(draft, decision, bundle, campaign_id="")

        receipt = decision.candidates[0].validation.model_copy(deep=True)
        receipt.source_route_signature_review_digest = ""
        unreviewed = decide_routes(
            decision.goal, [decision.candidates[0].candidate],
            lambda _candidate: receipt,
        )
        unreviewed_draft = build_route_action_binding_draft_v1(
            {
                "route_id": draft.route_id,
                "candidate_digest": draft.candidate_digest,
                "decision_id": unreviewed.decision_id,
                "evidence_bundle_id": draft.evidence_bundle_id,
                "evidence_bundle_digest": draft.evidence_bundle_digest,
                "stage": draft.stage.model_dump(mode="json"),
                "macro_action": draft.macro_action.model_dump(mode="json"),
            },
            decision=unreviewed, current_evidence_bundle=bundle,
        )
        with self.assertRaisesRegex(ValueError, "source signature review digest"):
            _build(unreviewed_draft, unreviewed, bundle)

    def test_v2_package_validation_fails_closed_on_unsupported_requirement(self) -> None:
        candidate = _candidate()
        candidate.material_graph[0].quantity_requirements.append({
            "material": "unmentioned material", "value": 2, "unit": "mmol",
            "provenance": candidate.material_graph[0].provenance.model_dump(mode="json"),
        })
        draft, decision, bundle = _reviewed_fixture(candidate)
        with self.assertRaisesRegex(ValueError, "does not identify its bound material"):
            _build(draft, decision, bundle)

    def test_rejects_unattested_raw_transport_digest(self) -> None:
        candidate = _candidate()
        step = candidate.material_graph[0]
        step.raw_step_digest_scope = RAW_STEP_DIGEST_SCOPE_V1
        step.raw_step_sha256 = "sha256_" + "a" * 64
        draft, decision, bundle = _reviewed_fixture(candidate)
        with self.assertRaisesRegex(ValueError, "unattested raw step transport digests"):
            _build(draft, decision, bundle)

    def test_device_cli_cannot_rebuild_even_an_annotated_typed_package(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        package = _build(draft, decision, bundle)
        raw_steps = []
        annotated_steps = []
        for step in package.macro_steps:
            raw = step.model_dump(mode="json", exclude_none=True)
            raw["observation_point_id"] = package.macro_action.observation_point_id
            raw_steps.append(raw)
            annotated = step.model_copy(deep=True)
            annotated.raw_step_digest_scope = RAW_STEP_DIGEST_SCOPE_V1
            annotated.raw_step_sha256 = canonical_raw_step_digest(raw)
            annotated_steps.append(annotated)
        annotated_package = ResearchActionPackageV2(
            campaign_id=package.campaign_id,
            capability_snapshot_id=package.capability_snapshot_id,
            stage=package.stage,
            macro_action=package.macro_action,
            macro_steps=annotated_steps,
            evidence_bundle=package.evidence_bundle,
            scientific_completeness=package.scientific_completeness,
            raw_observations_digest_scope=RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
            raw_observations_sha256=canonical_raw_observations_digest([]),
        )
        wire = annotated_package.model_dump(mode="json", exclude_none=True)
        wire.pop("research_contract_hash")
        annotated_package = ResearchActionPackageV2.model_validate(wire)
        package_wire = annotated_package.model_dump(mode="json", exclude_none=True)
        handoff = canonicalize_v2_device_handoff({
            "contract_version": "v2",
            "campaign_id": package.campaign_id,
            "macro_action_steps": raw_steps,
            "observations": [],
            "research_action_package_v2": package_wire,
        })
        self.assertEqual(handoff["research_action_package_v2"]["research_contract_hash"],
                         annotated_package.research_contract_hash)

        persisted_state = {
            "contract_version": "v2",
            "campaign_id": package.campaign_id,
            "current_stage": package.stage.name,
            "macro_action": package.macro_action.model_dump(mode="json"),
            "macro_plan": raw_steps,
            "observations": [],
            "current_evidence_bundle": {
                "bundle_id": bundle.bundle_id,
                "scope": bundle.scope,
                "query": bundle.query,
                "objective": bundle.objective,
                "retrieval_status": bundle.retrieval_status,
                "current_invocation_only": True,
                "results": [
                    dict(item.model_dump(mode="json"), evidence_excerpt=item.excerpt)
                    for item in bundle.items
                ],
                "errors": bundle.errors,
            },
            "research_action_package_v2": package_wire,
        }
        with self.assertRaisesRegex(
            SystemExit, "does not match the persisted raw macro plan/action/evidence state"
        ):
            validate_v2_research_handoff_consistency(persisted_state, raw_steps)


if __name__ == "__main__":
    unittest.main()
