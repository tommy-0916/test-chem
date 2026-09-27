"""Deterministic source-span checks for route candidates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1,
    RouteCandidateV1,
    RouteFieldEvidenceV1,
    RouteSignatureV1,
    RouteTargetV1,
)
from chem_agent_contracts.v2 import (
    EvidenceItemV2,
    MacroStepV2,
    MaterialPortV2,
    ProvenanceV2,
    canonical_digest,
)
from reaserch_agent.route_source import verify_route_source


class RouteSourceVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "primary_methods.md"
        self.signature = RouteSignatureV1(
            route_family="precipitation",
            target_transformation="precursor_to_wet_solid",
            precursor_roles=["metal_salt"],
            reagent_roles=["base"],
            operations=["dissolve", "precipitate"],
            control_modes=["pH_feedback"],
            endpoint_state="retained_wet_solid",
        )
        self.group_a_excerpt = "Mix 2 mmol metal salt with base to pH 10."
        self.group_b_excerpt = "Mix 9 mmol metal salt and heat at 100 C."
        self.lines = [
            "# Primary paper",
            "## Methods",
            "### control",
            self.group_a_excerpt,
            "```chem-agent-route-signature-v1",
            json.dumps(self.signature.model_dump(mode="json"), sort_keys=True),
            "```",
            "Wash the control precipitate with water.",
            "### treated",
            self.group_b_excerpt,
        ]
        self._write_source()

    def _write_source(self) -> None:
        self.source.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        self.document_digest = "sha256_" + hashlib.sha256(self.source.read_bytes()).hexdigest()

    def _candidate(self) -> RouteCandidateV1:
        group_scope = ExperimentalGroupScopeV1(
            paper_id="paper-1",
            experimental_group_id="control",
            section="Methods",
            locator="lines:3-8",
            source_digest=self.document_digest,
        )
        field_scope = ExperimentalGroupScopeV1(
            paper_id="paper-1",
            experimental_group_id="control",
            section="Methods",
            locator="lines:4-4",
            source_digest=self.document_digest,
        )
        return RouteCandidateV1(
            route_id="route-1",
            target=RouteTargetV1(
                material="target material",
                desired_state="retained_wet_solid",
                objective="synthesize target material",
            ),
            source_scope=group_scope,
            route_signature=self.signature,
            evidence_bundle=[EvidenceItemV2(
                evidence_id="E-1",
                excerpt=self.group_a_excerpt,
                # These metadata flags are not the verifier's authority.
                verification_status="unknown",
                full_text_status="unknown",
            )],
            evidence_matrix=[RouteFieldEvidenceV1(
                field_path="precursor.amount",
                value=2,
                unit="mmol",
                status="supported",
                source_scope=field_scope,
                evidence_id="E-1",
                provenance=ProvenanceV2(
                    kind="paper",
                    reference="E-1",
                    evidence_class="paper_explicit",
                    source_path="evidence_bundle.items[0].excerpt",
                    excerpt=self.group_a_excerpt,
                    source_digest=canonical_digest(self.group_a_excerpt),
                ),
            )],
            origin="paper_experimental_group",
        )

    def _verify(self, candidate: RouteCandidateV1):
        return verify_route_source(
            candidate,
            source_paths={"paper-1": self.source},
            source_root=self.root,
        )

    @staticmethod
    def _graph_provenance(candidate: RouteCandidateV1, excerpt: str) -> ProvenanceV2:
        return ProvenanceV2(
            kind="paper",
            reference="E-1",
            evidence_class="paper_explicit",
            source_path="evidence_bundle.items[0].excerpt",
            excerpt=excerpt,
            source_digest=canonical_digest(candidate.evidence_bundle[0].excerpt),
        )

    def _add_graph(self, candidate: RouteCandidateV1, port_excerpt: str) -> None:
        step_provenance = self._graph_provenance(candidate, self.group_a_excerpt)
        port_provenance = self._graph_provenance(candidate, port_excerpt)
        candidate.material_graph = [MacroStepV2(
            macro_step_id="step-1",
            macro_action_id="action-1",
            sequence=1,
            operation="precipitation",
            sample_id="sample-1",
            material_inputs=[MaterialPortV2(
                material_id="metal-salt",
                material_instance_id="input-1",
                name="metal salt",
                state="solution",
                provenance=port_provenance,
            )],
            material_outputs=[MaterialPortV2(
                material_id="target-material",
                material_instance_id="output-1",
                name="target material",
                state="retained_wet_solid",
                provenance=step_provenance,
            )],
            provenance=step_provenance,
        )]

    def test_exact_group_field_and_source_signature_verified(self) -> None:
        result = self._verify(self._candidate())
        self.assertTrue(result.source_scope_verified)
        self.assertEqual(result.verified_evidence_ids, ("E-1",))
        self.assertEqual(result.verified_field_paths, ("precursor.amount",))
        self.assertEqual(result.source_route_signature, self.signature)
        self.assertEqual(result.document_digest, self.document_digest)

    def test_changed_document_and_candidate_digest_cannot_reuse_verification(self) -> None:
        candidate = self._candidate()
        self.lines[3] = "Mix 3 mmol metal salt with base to pH 10."
        self._write_source()
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn("source_document_digest_mismatch", result.reasons)

    def test_other_experimental_group_cannot_supply_control_field(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].source_scope = ExperimentalGroupScopeV1(
            paper_id="paper-1", experimental_group_id="treated", section="Methods",
            locator="lines:10-10", source_digest=self.document_digest,
        )
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn(
            "field_source_scope_mismatch:precursor.amount", result.reasons
        )

    def test_same_group_label_from_other_document_cannot_supply_field(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].source_scope.source_digest = "sha256_" + "f" * 64
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertEqual(result.verified_field_paths, ())

    def test_excerpt_from_other_group_cannot_be_bound_to_control(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].provenance.excerpt = self.group_b_excerpt
        candidate.evidence_bundle[0].excerpt = self.group_b_excerpt
        candidate.evidence_matrix[0].provenance.source_digest = canonical_digest(
            self.group_b_excerpt
        )
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn(
            "field_excerpt_not_in_source_group:precursor.amount", result.reasons
        )

    def test_graph_claim_quote_outside_group_prevents_item_upgrade(self) -> None:
        candidate = self._candidate()
        invented_quote = "Dose 17 mmol unsupported salt."
        candidate.evidence_bundle[0].excerpt = (
            self.group_a_excerpt + "\n" + invented_quote
        )
        candidate.evidence_matrix[0].provenance.source_digest = canonical_digest(
            candidate.evidence_bundle[0].excerpt
        )
        self._add_graph(candidate, invented_quote)
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertEqual(result.verified_evidence_ids, ())
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn("evidence_item_excerpt_not_in_group:E-1", result.reasons)
        self.assertTrue(any(
            reason.startswith("graph_paper_excerpt_outside_group:material_graph[0].material_inputs[0]")
            for reason in result.reasons
        ))

    def test_nested_quantity_requirement_claim_is_checked(self) -> None:
        candidate = self._candidate()
        candidate.evidence_bundle[0].excerpt += "\nInvented 17 mmol detail."
        candidate.evidence_matrix[0].provenance.source_digest = canonical_digest(
            candidate.evidence_bundle[0].excerpt
        )
        self._add_graph(candidate, self.group_a_excerpt)
        candidate.material_graph[0].quantity_requirements = [{
            "kind": "target_dose",
            "material_id": "metal-salt",
            "value": 17,
            "unit": "mmol",
            "provenance": self._graph_provenance(candidate, "Invented 17 mmol detail.").model_dump(mode="python"),
        }]
        result = self._verify(candidate)
        self.assertEqual(result.verified_evidence_ids, ())
        self.assertTrue(any(
            reason.startswith("graph_paper_excerpt_outside_group:material_graph[0].quantity_requirements[0]")
            for reason in result.reasons
        ))

    def test_graph_claims_in_group_verify_but_unreferenced_item_does_not(self) -> None:
        candidate = self._candidate()
        candidate.evidence_bundle.append(EvidenceItemV2(
            evidence_id="E-unused", excerpt=self.group_a_excerpt
        ))
        self._add_graph(candidate, self.group_a_excerpt)
        result = self._verify(candidate)
        self.assertTrue(result.source_scope_verified)
        self.assertEqual(result.verified_evidence_ids, ("E-1",))
        self.assertEqual(result.verified_field_paths, ("precursor.amount",))

    def test_exact_quote_does_not_verify_a_different_quantity(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].value = 9
        result = self._verify(candidate)
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn(
            "field_quantity_not_in_excerpt:precursor.amount", result.reasons
        )

    def test_candidate_cannot_name_a_noncanonical_evidence_path(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].provenance.source_path = "paper.pdf"
        result = self._verify(candidate)
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn(
            "field_provenance_source_path_mismatch:precursor.amount", result.reasons
        )

    def test_provenance_digest_binds_the_full_bundle_excerpt(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].provenance.source_digest = self.document_digest
        result = self._verify(candidate)
        self.assertEqual(result.verified_field_paths, ())
        self.assertIn(
            "field_provenance_excerpt_digest_mismatch:precursor.amount",
            result.reasons,
        )

    def test_source_path_uses_the_evidence_ids_actual_bundle_index(self) -> None:
        candidate = self._candidate()
        candidate.evidence_bundle.insert(0, EvidenceItemV2(
            evidence_id="E-unrelated", excerpt="Unrelated source excerpt."
        ))
        candidate.evidence_matrix[0].provenance.source_path = (
            "evidence_bundle.items[1].excerpt"
        )
        verified = self._verify(candidate)
        self.assertEqual(verified.verified_field_paths, ("precursor.amount",))
        candidate.evidence_matrix[0].provenance.source_path = (
            "evidence_bundle.items[0].excerpt"
        )
        wrong_index = self._verify(candidate)
        self.assertEqual(wrong_index.verified_field_paths, ())

    def test_untrusted_path_and_unsupported_format_abstain(self) -> None:
        candidate = self._candidate()
        other = self.root.parent / (self.root.name + "_outside.md")
        other.write_bytes(self.source.read_bytes())
        self.addCleanup(other.unlink)
        outside = verify_route_source(
            candidate,
            source_paths={"paper-1": other},
            source_root=self.root,
        )
        self.assertFalse(outside.source_scope_verified)
        self.assertIn("source_outside_trusted_root", outside.reasons)
        pdf = self.root / "primary_methods.pdf"
        pdf.write_bytes(self.source.read_bytes())
        unsupported = verify_route_source(
            candidate,
            source_paths={"paper-1": pdf},
            source_root=self.root,
        )
        self.assertFalse(unsupported.source_scope_verified)
        self.assertIn("source_format_not_locatable", unsupported.reasons)

    def test_unannotated_methods_verify_fields_but_not_route_signature(self) -> None:
        self.lines = [
            "# Primary paper", "## Methods", "### control", self.group_a_excerpt,
            "### treated", self.group_b_excerpt,
        ]
        self._write_source()
        candidate = self._candidate()
        candidate.source_scope.locator = "lines:3-4"
        candidate.evidence_matrix[0].source_scope.source_digest = self.document_digest
        result = self._verify(candidate)
        self.assertTrue(result.source_scope_verified)
        self.assertEqual(result.verified_field_paths, ("precursor.amount",))
        self.assertIsNone(result.source_route_signature)

    def test_duplicate_group_heading_is_not_resolved_by_guessing(self) -> None:
        self.lines.extend(["### control", "Mix 7 mmol metal salt."])
        self._write_source()
        candidate = self._candidate()
        candidate.source_scope.source_digest = self.document_digest
        candidate.evidence_matrix[0].source_scope.source_digest = self.document_digest
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("experimental_group_not_unique_in_section", result.reasons)


if __name__ == "__main__":
    unittest.main()
