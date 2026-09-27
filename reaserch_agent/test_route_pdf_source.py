"""PDF page/block source checks; generated PDF bytes are the source of truth."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
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
from chem_agent_contracts.v2 import EvidenceItemV2, ProvenanceV2, canonical_digest
from reaserch_agent.route_pdf_source import verify_route_pdf_source


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class RoutePdfSourceVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "primary_methods.pdf"
        self.control_excerpt = "Mix 2 mmol metal salt with base to pH 10."
        self.treated_excerpt = "Mix 9 mmol metal salt and heat at 100 C."
        self.lines = [
            ("Methods", 16, "hebo"),
            ("control", 14, "hebo"),
            (self.control_excerpt, 10, "helv"),
            ("Wash the control precipitate with water.", 10, "helv"),
            ("treated", 14, "hebo"),
            (self.treated_excerpt, 10, "helv"),
        ]
        self._fitz = fitz
        self._write_pdf()

    def _write_pdf(self) -> None:
        doc = self._fitz.open()
        page = doc.new_page()
        line_index = 0
        for index, (body, size, font) in enumerate(self.lines):
            if index == getattr(self, "page_break_after", -1):
                page = doc.new_page()
                line_index = 0
            page.insert_text(
                self._fitz.Point(72, 70 + line_index * 48), body,
                fontsize=size, fontname=font,
            )
            line_index += 1
        doc.save(str(self.path))
        doc.close()
        self.digest = "sha256_" + sha256(self.path.read_bytes()).hexdigest()

    def _candidate(self) -> RouteCandidateV1:
        group_scope = ExperimentalGroupScopeV1(
            paper_id="paper-1",
            experimental_group_id="control",
            section="Methods",
            locator="pdf:p1:b2-p1:b4",
            source_digest=self.digest,
        )
        field_scope = group_scope.model_copy(update={"locator": "pdf:p1:b3-p1:b3"})
        return RouteCandidateV1(
            route_id="route-1",
            target=RouteTargetV1(
                material="target material",
                desired_state="retained_wet_solid",
                objective="synthesize target material",
            ),
            source_scope=group_scope,
            route_signature=RouteSignatureV1(
                route_family="precipitation",
                target_transformation="precursor_to_wet_solid",
                precursor_roles=["metal_salt"],
                reagent_roles=["base"],
                operations=["dissolve", "precipitate"],
                control_modes=["pH_feedback"],
                endpoint_state="retained_wet_solid",
            ),
            evidence_bundle=[EvidenceItemV2(
                evidence_id="E-1",
                excerpt=self.control_excerpt,
                verification_status="verified_doi",
                full_text_status="parsed",
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
                    excerpt=self.control_excerpt,
                    source_digest=canonical_digest(self.control_excerpt),
                ),
            )],
            origin="paper_experimental_group",
        )

    def _verify(self, candidate: RouteCandidateV1, path: Path | None = None):
        return verify_route_pdf_source(
            candidate,
            source_paths={"paper-1": path or self.path},
            source_root=self.root,
        )

    def test_exact_page_block_group_and_quantity_are_verified_without_signature(self) -> None:
        result = self._verify(self._candidate())
        self.assertTrue(result.source_scope_verified, result.reasons)
        self.assertEqual(result.verified_evidence_ids, ("E-1",))
        self.assertEqual(result.verified_field_paths, ("precursor.amount",))
        self.assertIsNone(result.source_route_signature)
        self.assertEqual(result.document_digest, self.digest)

    def test_neighboring_experimental_arm_cannot_supply_control_fact(self) -> None:
        candidate = self._candidate()
        candidate.evidence_bundle[0].excerpt = self.treated_excerpt
        candidate.evidence_matrix[0].provenance.excerpt = self.treated_excerpt
        candidate.evidence_matrix[0].provenance.source_digest = canonical_digest(self.treated_excerpt)
        candidate.evidence_matrix[0].value = 9
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertEqual(result.verified_evidence_ids, ())
        self.assertIn("field_excerpt_not_in_source_group:precursor.amount", result.reasons)

    def test_changed_pdf_bytes_do_not_reuse_a_prior_receipt(self) -> None:
        candidate = self._candidate()
        self.lines[2] = ("Mix 3 mmol metal salt with base to pH 10.", 10, "helv")
        self._write_pdf()
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("source_document_digest_mismatch", result.reasons)

    def test_group_can_span_two_pages_with_exact_page_block_locator(self) -> None:
        self.page_break_after = 3
        self._write_pdf()
        candidate = self._candidate()
        candidate.source_scope.locator = "pdf:p1:b2-p2:b1"
        result = self._verify(candidate)
        self.assertTrue(result.source_scope_verified, result.reasons)
        self.assertEqual(result.verified_field_paths, ("precursor.amount",))
        page_two_quote = "Wash the control precipitate with water."
        candidate.evidence_bundle[0].excerpt = page_two_quote
        field = candidate.evidence_matrix[0]
        field.source_scope.locator = "pdf:p2:b1-p2:b1"
        field.value = "water"
        field.unit = ""
        field.provenance.excerpt = page_two_quote
        field.provenance.source_digest = canonical_digest(page_two_quote)
        second_page = self._verify(candidate)
        self.assertTrue(second_page.source_scope_verified, second_page.reasons)
        self.assertEqual(second_page.verified_field_paths, ("precursor.amount",))

    def test_scope_must_name_the_entire_structural_group(self) -> None:
        candidate = self._candidate()
        candidate.source_scope.locator = "pdf:p1:b2-p1:b3"
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("experimental_group_locator_mismatch", result.reasons)

    def test_field_locator_cannot_reach_neighboring_arm(self) -> None:
        candidate = self._candidate()
        candidate.evidence_matrix[0].source_scope.locator = "pdf:p1:b6-p1:b6"
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("field_locator_outside_group:precursor.amount", result.reasons)

    def test_group_label_in_unstyled_body_prose_does_not_define_a_group(self) -> None:
        self.lines[1] = ("control", 10, "helv")
        self._write_pdf()
        candidate = self._candidate()
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("experimental_group_not_unique_in_section", result.reasons)

    def test_same_size_peer_heading_even_without_bold_closes_the_group(self) -> None:
        self.lines[4] = ("treated", 14, "helv")
        self._write_pdf()
        candidate = self._candidate()
        result = self._verify(candidate)
        self.assertTrue(result.source_scope_verified, result.reasons)
        candidate.evidence_bundle[0].excerpt = self.treated_excerpt
        candidate.evidence_matrix[0].provenance.excerpt = self.treated_excerpt
        candidate.evidence_matrix[0].provenance.source_digest = canonical_digest(self.treated_excerpt)
        candidate.evidence_matrix[0].value = 9
        contaminated = self._verify(candidate)
        self.assertFalse(contaminated.source_scope_verified)
        self.assertIn("field_excerpt_not_in_source_group:precursor.amount", contaminated.reasons)

    def test_duplicate_group_heading_is_ambiguous(self) -> None:
        self.lines.extend([
            ("control", 14, "hebo"),
            ("Mix 7 mmol metal salt.", 10, "helv"),
        ])
        self._write_pdf()
        candidate = self._candidate()
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("experimental_group_not_unique_in_section", result.reasons)

    def test_image_only_pdf_has_no_verifiable_text(self) -> None:
        doc = self._fitz.open()
        doc.new_page()
        doc.save(str(self.path), incremental=False)
        doc.close()
        candidate = self._candidate()
        candidate.source_scope.source_digest = "sha256_" + sha256(self.path.read_bytes()).hexdigest()
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("pdf_no_extractable_text", result.reasons)

    def test_untrusted_pdf_path_and_fake_content_are_rejected(self) -> None:
        candidate = self._candidate()
        external = self.root.parent / (self.root.name + "-outside.pdf")
        external.write_bytes(self.path.read_bytes())
        self.addCleanup(external.unlink)
        result = self._verify(candidate, external)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("source_outside_trusted_root", result.reasons)
        fake = self.root / "fake.pdf"
        fake.write_bytes(b"not PDF")
        candidate.source_scope.source_digest = "sha256_" + sha256(fake.read_bytes()).hexdigest()
        result = self._verify(candidate, fake)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("source_not_pdf", result.reasons)

    def test_provenance_status_and_claimed_signature_are_not_source_authority(self) -> None:
        candidate = self._candidate()
        candidate.route_signature.route_family = "hydrothermal"
        result = self._verify(candidate)
        self.assertTrue(result.source_scope_verified)
        self.assertIsNone(result.source_route_signature)
        candidate.evidence_matrix[0].value = 17
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("field_quantity_not_in_excerpt:precursor.amount", result.reasons)


if __name__ == "__main__":
    unittest.main()
