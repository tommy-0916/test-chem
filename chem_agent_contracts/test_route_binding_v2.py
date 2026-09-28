"""Focused, generic checks for the hash-bound typed route identity."""

from __future__ import annotations

import copy
import json
import unittest

from chem_agent_contracts.route_action_intent import (
    RouteActionIntentV1,
    build_route_action_binding_draft_v1,
)
from chem_agent_contracts.route_candidate import RouteFieldEvidenceV1
from chem_agent_contracts.route_decision import decide_routes
from chem_agent_contracts.route_field_basis import controlled_state_mapping
from chem_agent_contracts.route_saved_state import (
    build_selected_route_saved_state_v2,
    validate_selected_route_saved_state_v2,
)
from chem_agent_contracts.test_route_action_intent import DOI, _candidate
from chem_agent_contracts.test_route_research_package_draft import (
    _build,
    _reviewed_fixture,
)
from chem_agent_contracts.v2 import (
    RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
    RAW_STEP_DIGEST_SCOPE_V1,
    ResearchActionPackageV2,
    EvidenceItemV2,
    ProvenanceV2,
    canonical_digest,
    canonical_raw_observations_digest,
    canonical_raw_step_digest,
    canonicalize_v2_device_handoff,
)


def _mapped_fixture():
    draft, decision, bundle = _reviewed_fixture()
    candidate = decision.candidates[0].candidate.model_copy(deep=True)
    path = "material_graph[0].material_inputs[0].state"
    excerpt = "The nickel salt 溶液 was prepared."
    evidence_id = "E3"
    candidate.evidence_bundle.append(EvidenceItemV2(
        evidence_id=evidence_id, title="Primary group A", doi=DOI,
        excerpt=excerpt,
    ))
    current = bundle.model_copy(deep=True)
    current.items.append(EvidenceItemV2(
        evidence_id=evidence_id, title="Primary group A", doi=DOI,
        excerpt=excerpt, verification_status="verified_doi",
        full_text_status="parsed",
    ))
    mapping, issue = controlled_state_mapping(path, "溶液", "solution")
    assert not issue and mapping is not None
    candidate.evidence_matrix.append(RouteFieldEvidenceV1(
        field_path=path, value="溶液", status="supported",
        provenance=ProvenanceV2(
            kind="paper", reference=evidence_id,
            evidence_class="paper_explicit",
            source_path="evidence_bundle.items[2].excerpt",
            excerpt=excerpt, source_digest=canonical_digest(excerpt),
        ),
        evidence_id=evidence_id, source_scope=candidate.source_scope,
        controlled_mapping=mapping,
    ))
    receipt = decision.candidates[0].validation.model_copy(deep=True)
    receipt.candidate_digest = canonical_digest(candidate)
    receipt.verified_evidence_ids.append(evidence_id)
    receipt.verified_field_paths.append(path)
    receipt.audited_field_paths.append(path)
    selected = decide_routes(decision.goal, [candidate], lambda _candidate: receipt)
    assert selected.status == "selected_for_planning", selected.decision_reasons
    intent = RouteActionIntentV1(
        route_id=candidate.route_id,
        candidate_digest=canonical_digest(candidate),
        decision_id=selected.decision_id,
        evidence_bundle_id=current.bundle_id,
        evidence_bundle_digest=canonical_digest(current),
        stage=draft.stage, macro_action=draft.macro_action,
    )
    bound = build_route_action_binding_draft_v1(
        intent, decision=selected, current_evidence_bundle=current,
    )
    return bound, selected, current, path, mapping


class RouteBindingV2Test(unittest.TestCase):
    def test_mapping_snapshot_survives_v2_state_and_device_handoff(self) -> None:
        draft, decision, bundle, path, mapping = _mapped_fixture()
        package = _build(draft, decision, bundle)
        record = package.route_binding.controlled_state_mappings[0]
        self.assertEqual(record.field_path, path)
        self.assertEqual(record.evidence_id, "E3")
        self.assertEqual(record.source_value, "溶液")
        self.assertEqual(record.target_value, "solution")
        self.assertEqual(record.rule_id, mapping["rule_id"])
        self.assertEqual(record.resource_digest, mapping["resource_digest"])

        saved = build_selected_route_saved_state_v2(
            draft, decision=decision, current_evidence_bundle=bundle,
            campaign_id="campaign-mapped", observations=[],
        )
        self.assertEqual(
            saved["route_binding"],
            saved["research_action_package_v2"]["route_binding"],
        )
        restored = validate_selected_route_saved_state_v2(
            json.loads(json.dumps(saved, ensure_ascii=False))
        )
        self.assertEqual(restored.route_binding.controlled_state_mappings, [record])
        self.assertEqual(
            saved["device_adaptation_handoff"]["research_action_package_v2"]
            ["route_binding"]["controlled_state_mappings"][0],
            record.model_dump(mode="json"),
        )
        canonical_handoff = canonicalize_v2_device_handoff({
            "contract_version": "v2",
            "campaign_id": saved["campaign_id"],
            "macro_action_steps": saved["macro_plan"],
            "observations": saved["observations"],
            "research_action_package_v2": saved["research_action_package_v2"],
        })
        self.assertEqual(
            canonical_handoff["research_action_package_v2"]
            ["route_binding"]["controlled_state_mappings"][0],
            record.model_dump(mode="json"),
        )

        for field, replacement in (
            ("source_value", "invented state"),
            ("target_value", "powder"),
            ("evidence_id", "missing-evidence"),
            ("resource_digest", "sha256_" + "0" * 64),
        ):
            with self.subTest(field=field):
                changed = package.model_dump(mode="json", exclude_none=True)
                changed["route_binding"]["controlled_state_mappings"][0][field] = replacement
                with self.assertRaises(ValueError):
                    ResearchActionPackageV2.model_validate(changed, strict=True)

        changed = package.model_dump(mode="json", exclude_none=True)
        changed.pop("research_contract_hash")
        changed["route_binding"]["controlled_state_mappings"][0]["target_value"] = "powder"
        with self.assertRaisesRegex(ValueError, "controlled mapping graph state differs"):
            ResearchActionPackageV2.model_validate(changed, strict=True)

    def test_legacy_bound_package_hash_is_unchanged(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        package = _build(draft, decision, bundle)
        self.assertIsNone(package.route_binding.controlled_state_mappings)
        self.assertEqual(
            package.research_contract_hash,
            "research_v2_3ca79eeec28c8316ec3f9c7e41fd6d0c32a0a845eeb28113933e9fd856feff6e",
        )
        restored = ResearchActionPackageV2.model_validate(
            package.model_dump(mode="json", exclude_none=True), strict=True,
        )
        self.assertEqual(restored.research_contract_hash, package.research_contract_hash)

    def test_mapping_rule_and_resource_are_rechecked_when_v2_hash_is_rebuilt(self) -> None:
        draft, decision, bundle, _path, _mapping = _mapped_fixture()
        package = _build(draft, decision, bundle)
        for field, replacement in (
            ("rule_id", "material-states/v1:alias:powder"),
            ("resource_digest", "sha256_" + "0" * 64),
        ):
            with self.subTest(field=field):
                changed = package.model_dump(mode="json", exclude_none=True)
                changed.pop("research_contract_hash")
                changed["route_binding"]["controlled_state_mappings"][0][
                    field
                ] = replacement
                with self.assertRaisesRegex(
                    ValueError, "controlled mapping rule differs",
                ):
                    ResearchActionPackageV2.model_validate(changed, strict=True)

    def test_route_and_paper_group_are_distinct_hash_bound_identities(self) -> None:
        candidate = _candidate()
        scope = candidate.source_scope.model_copy(deep=True)
        scope.experimental_group_id = "paper-treatment-group-7"
        scope.section = "Methods"
        candidate.source_scope = scope
        candidate.evidence_matrix[0].source_scope = scope.model_copy(deep=True)
        candidate.evidence_matrix[1].source_scope = scope.model_copy(deep=True)
        draft, decision, bundle = _reviewed_fixture(candidate)
        package = _build(draft, decision, bundle)

        binding = package.route_binding
        self.assertIsNotNone(binding)
        self.assertEqual(binding.route_id, candidate.route_id)
        self.assertEqual(binding.experimental_group_id, scope.experimental_group_id)
        self.assertEqual(binding.source_paper_id, scope.paper_id)
        self.assertEqual(binding.source_section, scope.section)
        self.assertEqual(binding.source_locator, scope.locator)
        self.assertEqual(binding.source_digest, scope.source_digest)
        self.assertEqual(binding.required_capabilities, candidate.required_capabilities)
        self.assertNotEqual(
            binding.experimental_group_id,
            package.macro_action.experiment_group.group_id,
        )
        restored = ResearchActionPackageV2.model_validate(
            package.model_dump(mode="json", exclude_none=True), strict=True
        )
        self.assertEqual(restored.route_binding, binding)
        self.assertEqual(restored.research_contract_hash, package.research_contract_hash)

        for field, replacement in (
            ("route_id", "another-route"),
            ("experimental_group_id", "another-paper-group"),
            ("source_locator", "Methods, page 99"),
            ("required_capabilities", ["other-capability"]),
        ):
            with self.subTest(field=field):
                payload = package.model_dump(mode="json", exclude_none=True)
                payload["route_binding"][field] = replacement
                with self.assertRaises(ValueError):
                    ResearchActionPackageV2.model_validate(payload, strict=True)

    def test_package_rejects_intent_graph_evidence_and_snapshot_divergence(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        package = _build(draft, decision, bundle)
        mutations = (
            (("stage", "objective"), "different objective", "intent digest"),
            (("macro_steps", 0, "reagent_or_object"), "different reagent", "material graph digest"),
            (("evidence_bundle", "objective"), "different evidence scope", "evidence bundle digest"),
            (("capability_snapshot_id",), "different snapshot", "device snapshot"),
        )
        for path, replacement, error in mutations:
            with self.subTest(path=path):
                payload = package.model_dump(mode="json", exclude_none=True)
                payload.pop("research_contract_hash")
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                with self.assertRaisesRegex(ValueError, error):
                    ResearchActionPackageV2.model_validate(payload, strict=True)

    def test_device_canonical_projection_keeps_and_validates_binding(self) -> None:
        draft, decision, bundle = _reviewed_fixture()
        source = _build(draft, decision, bundle)
        raw_steps = []
        annotated_steps = []
        for step in source.macro_steps:
            raw = step.model_dump(mode="json", exclude_none=True)
            raw["observation_point_id"] = source.macro_action.observation_point_id
            raw_steps.append(raw)
            annotated = step.model_copy(deep=True)
            annotated.raw_step_digest_scope = RAW_STEP_DIGEST_SCOPE_V1
            annotated.raw_step_sha256 = canonical_raw_step_digest(raw)
            annotated_steps.append(annotated)
        published = ResearchActionPackageV2(
            campaign_id=source.campaign_id,
            capability_snapshot_id=source.capability_snapshot_id,
            stage=source.stage,
            macro_action=source.macro_action,
            macro_steps=annotated_steps,
            evidence_bundle=source.evidence_bundle,
            scientific_completeness=source.scientific_completeness,
            route_binding=source.route_binding,
            raw_observations_digest_scope=RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
            raw_observations_sha256=canonical_raw_observations_digest([]),
        )
        handoff = {
            "contract_version": "v2",
            "campaign_id": source.campaign_id,
            "macro_action_steps": raw_steps,
            "observations": [],
            "research_action_package_v2": published.model_dump(
                mode="json", exclude_none=True
            ),
        }
        canonical = canonicalize_v2_device_handoff(handoff)
        self.assertEqual(
            canonical["research_action_package_v2"]["route_binding"],
            published.route_binding.model_dump(mode="json", exclude_none=True),
        )

        tampered = copy.deepcopy(handoff)
        tampered["research_action_package_v2"]["route_binding"][
            "experimental_group_id"
        ] = "wrong-paper-group"
        with self.assertRaises(ValueError):
            canonicalize_v2_device_handoff(tampered)


if __name__ == "__main__":
    unittest.main()
