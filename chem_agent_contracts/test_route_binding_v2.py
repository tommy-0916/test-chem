"""Focused, generic checks for the hash-bound typed route identity."""

from __future__ import annotations

import copy
import unittest

from chem_agent_contracts.test_route_action_intent import _candidate
from chem_agent_contracts.test_route_research_package_draft import (
    _build,
    _reviewed_fixture,
)
from chem_agent_contracts.v2 import (
    RAW_OBSERVATIONS_DIGEST_SCOPE_V1,
    RAW_STEP_DIGEST_SCOPE_V1,
    ResearchActionPackageV2,
    canonical_raw_observations_digest,
    canonical_raw_step_digest,
    canonicalize_v2_device_handoff,
)


class RouteBindingV2Test(unittest.TestCase):
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
