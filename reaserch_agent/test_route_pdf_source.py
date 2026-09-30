"""PDF page/block source checks; generated PDF bytes are the source of truth."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1,
    RouteCandidateV1,
    RouteFieldEvidenceV1,
    RouteSignatureV1,
    RouteTargetV1,
)
from chem_agent_contracts.v2 import (
    EvidenceItemV2, MacroStepV2, MaterialPortV2, ProvenanceV2,
    QuantityV2, canonical_digest,
)
from reaserch_agent.route_pdf_source import verify_route_pdf_source
from chem_agent_contracts.route_decision import (
    RouteValidationReceiptV1, _field_issue as decision_field_issue,
)


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

    def _verify(self, candidate: RouteCandidateV1, path: Path | None = None,
                *, inventory_register_paths=None):
        return verify_route_pdf_source(
            candidate,
            source_paths={"paper-1": path or self.path},
            source_root=self.root,
            inventory_register_paths=inventory_register_paths,
        )

    def _inventory_candidate(self, register="candidate_supply_spec/v1"):
        item = {
            "item_id": "supply-1", "material_name": "reagent Z",
            "supply_form": "solution", "record": "Referenced reagent Z solution form",
        }
        if register == "candidate_supply_spec/v1":
            item.update(candidate_supply_spec=True, stock_verified=False)
        self.spec_path = self.root / "supply-specs.json"
        self.spec_path.write_text(json.dumps({"schema": register, "items": [item]}), encoding="utf-8")
        digest = "sha256_" + sha256(self.spec_path.read_bytes()).hexdigest()
        route = self._candidate()
        proposed = ProvenanceV2(kind="agent_inferred", rationale="unreviewed structure")
        route.material_graph = [MacroStepV2(
            macro_step_id="S1", macro_action_id="A1", sequence=1,
            operation="mix", sample_id="sample-A", provenance=proposed,
            material_inputs=[MaterialPortV2(
                material_id="z", material_instance_id="z_batch", name="reagent Z",
                state="solution", material_origin="external_inventory", provenance=proposed,
            )],
        )]
        route.evidence_matrix.append(RouteFieldEvidenceV1(
            field_path="material_graph[0].material_inputs[0].state",
            value="solution", status="supported", resolution_path=register,
            provenance=ProvenanceV2(
                kind="inventory", reference=item["item_id"], excerpt=item["record"],
                evidence_class="inventory_record",
                source_digest=digest,
            ),
        ))
        return route, [item], digest

    def test_candidate_supply_spec_requires_registered_source_bytes(self):
        route, _, _ = self._inventory_candidate()
        field = route.evidence_matrix[-1]
        result = self._verify(route)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("inventory_resolution_unverified:" + field.field_path, result.reasons)
        self.assertEqual(decision_field_issue(field, route, None), "inventory_field_source_unverified")
        receipt = RouteValidationReceiptV1(
            route_id=route.route_id, candidate_digest=canonical_digest(route),
            source_scope_verified=True, verified_field_paths=["precursor.amount"],
        )
        self.assertEqual(decision_field_issue(field, route, receipt), "inventory_field_source_unverified")

    def test_candidate_supply_save_reload_reopens_and_verifies_registered_bytes(self):
        route, _, _ = self._inventory_candidate()
        reloaded = RouteCandidateV1.model_validate(json.loads(route.model_dump_json()))
        register = {"candidate_supply_spec/v1": self.spec_path}
        result = self._verify(reloaded, inventory_register_paths=register)
        self.assertTrue(result.source_scope_verified, result.reasons)
        field = reloaded.evidence_matrix[-1]
        self.assertIn(field.field_path, result.verified_field_paths)
        receipt = RouteValidationReceiptV1(
            route_id=reloaded.route_id, candidate_digest=canonical_digest(reloaded),
            source_scope_verified=result.source_scope_verified,
            verified_field_paths=list(result.verified_field_paths),
        )
        self.assertIsNone(decision_field_issue(field, reloaded, receipt))
        self.spec_path.write_text(self.spec_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        stale = self._verify(reloaded, inventory_register_paths=register)
        self.assertFalse(stale.source_scope_verified)
        self.assertNotIn(field.field_path, stale.verified_field_paths)

    def test_candidate_supply_cannot_forge_item_record_or_graph_binding(self):
        route, _, _ = self._inventory_candidate()
        mutations = {
            "unknown_item": lambda c: setattr(c.evidence_matrix[-1].provenance, "reference", "does-not-exist"),
            "fabricated_record": lambda c: setattr(c.evidence_matrix[-1].provenance, "excerpt", "fabricated reference"),
            "wrong_state": lambda c: setattr(c.evidence_matrix[-1], "value", "powder"),
            "wrong_material": lambda c: setattr(c.material_graph[0].material_inputs[0], "name", "reagent Y"),
            "graph_state_mismatch": lambda c: setattr(c.material_graph[0].material_inputs[0], "state", "powder"),
            "internal_material": lambda c: setattr(c.material_graph[0].material_inputs[0], "material_origin", "upstream_output"),
            "unknown_register": lambda c: setattr(c.evidence_matrix[-1], "resolution_path", "other/v1"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = route.model_copy(deep=True)
                mutate(candidate)
                result = self._verify(candidate, inventory_register_paths={"candidate_supply_spec/v1": self.spec_path})
                self.assertFalse(result.source_scope_verified, result)

    def test_candidate_supply_requires_candidate_schema_and_status(self):
        route, items, _ = self._inventory_candidate()
        for change in ({"schema": "material-inventory/v1"}, {"stock_verified": True}, {"candidate_supply_spec": False}):
            with self.subTest(change=change):
                payload = {"schema": "candidate_supply_spec/v1", "items": [items[0].copy()]}
                if "schema" in change:
                    payload.update(change)
                else:
                    payload["items"][0].update(change)
                self.spec_path.write_text(json.dumps(payload), encoding="utf-8")
                route.evidence_matrix[-1].provenance.source_digest = "sha256_" + sha256(self.spec_path.read_bytes()).hexdigest()
                result = self._verify(route, inventory_register_paths={"candidate_supply_spec/v1": self.spec_path})
                self.assertFalse(result.source_scope_verified)

    def test_candidate_supply_register_cannot_escape_trusted_root(self):
        route, _, _ = self._inventory_candidate()
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "specs.json"
            outside.write_bytes(self.spec_path.read_bytes())
            result = self._verify(route, inventory_register_paths={"candidate_supply_spec/v1": outside})
            self.assertFalse(result.source_scope_verified)

    def test_approved_inventory_is_reverified_and_cannot_cross_registers(self):
        route, items, digest = self._inventory_candidate("material-inventory/v1")
        with patch("reaserch_agent.route_pdf_source.load_inventory_resource", return_value=(items, digest)):
            result = self._verify(route)
            self.assertTrue(result.source_scope_verified, result.reasons)
            route.evidence_matrix[-1].resolution_path = "candidate_supply_spec/v1"
            result = self._verify(route)
            self.assertFalse(result.source_scope_verified)

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

    def test_source_quantity_must_belong_to_named_material(self) -> None:
        self.lines[2] = ("A 1 mmol and B 2 mmol.", 10, "helv")
        self._write_pdf()
        candidate = self._candidate()
        candidate.source_scope.source_digest = self.digest
        excerpt = self.lines[2][0]
        candidate.evidence_bundle[0].excerpt = excerpt
        field = candidate.evidence_matrix[0]
        field.field_path = "material_graph[0].material_inputs[0].quantity.value"
        field.source_scope.source_digest = self.digest
        field.provenance.excerpt = excerpt
        field.provenance.source_digest = canonical_digest(excerpt)
        inferred = ProvenanceV2(kind="agent_inferred", rationale="test candidate")
        candidate.material_graph = [MacroStepV2(
            macro_step_id="S1", macro_action_id="A1", sequence=1,
            operation="mix", sample_id="sample-A", provenance=inferred,
            material_inputs=[MaterialPortV2(
                material_id="A", material_instance_id="A1", name="A",
                state="solution", quantity=QuantityV2(value=2, unit="mmol"),
                provenance=inferred,
            )],
        )]
        wrong = self._verify(candidate)
        self.assertFalse(wrong.source_scope_verified)
        self.assertIn(
            "field_quantity_attribution_unresolved:material_graph[0].material_inputs[0].quantity.value",
            wrong.reasons,
        )

        field.value = 1
        candidate.material_graph[0].material_inputs[0].quantity.value = 1
        correct = self._verify(candidate)
        self.assertTrue(correct.source_scope_verified, correct.reasons)

    def test_definition_concentration_subject_is_rechecked_from_pdf_bytes(self) -> None:
        variants = (
            ("Solution A was washed, then Solution B was prepared by adding water (1 M).", "Solution B", False),
            ("Solution A was prepared by mixing 5 mL Solution B (1 M).", "Solution B", False),
            ("Solution A was prepared by dissolving 2 mmol salt in 2 mL Carrier Z (1 M).", "Carrier Z", False),
            ("Solution A was prepared by dissolving 2 mmol salt in 2 mL water (1 M).", None, True),
        )
        for excerpt, competing_name, expected in variants:
            with self.subTest(excerpt=excerpt):
                self.lines[2] = (excerpt, 8, "helv")
                self._write_pdf()
                candidate = self._candidate()
                candidate.evidence_bundle[0].excerpt = excerpt
                field = candidate.evidence_matrix[0]
                field.field_path = "material_graph[0].material_inputs[0].concentration_value"
                field.value, field.unit = 1, "M"
                field.provenance.excerpt = excerpt
                field.provenance.source_digest = canonical_digest(excerpt)
                inferred = ProvenanceV2(kind="agent_inferred", rationale="unreviewed candidate")
                ports = [MaterialPortV2(
                    material_id="solution_a", material_instance_id="batch_a", name="Solution A",
                    state="solution", concentration_value=1, concentration_unit="M", provenance=inferred,
                )]
                if competing_name:
                    ports.append(MaterialPortV2(
                        material_id="other_input", material_instance_id="other_batch",
                        name=competing_name, state="solution", provenance=inferred,
                    ))
                candidate.material_graph = [MacroStepV2(
                    macro_step_id="S1", macro_action_id="A1", sequence=1,
                    operation="prepare", sample_id="sample-A", provenance=inferred,
                    material_inputs=ports,
                )]
                for index, port in enumerate(ports):
                    candidate.evidence_matrix.append(field.model_copy(deep=True, update={
                        "field_path": f"material_graph[0].material_inputs[{index}].name",
                        "value": port.name, "unit": "",
                    }))
                result = self._verify(candidate)
                self.assertEqual(result.source_scope_verified, expected, result.reasons)
                if expected:
                    self.assertIn(field.field_path, result.verified_field_paths)
                else:
                    self.assertIn("field_quantity_attribution_unresolved:" + field.field_path, result.reasons)

    def test_cross_block_literal_quote_verifies_only_with_anchor_inside_span(self) -> None:
        self.lines.insert(4, ("The control yield was recorded.", 10, "helv"))
        self._write_pdf()
        candidate = self._candidate()
        candidate.source_scope.locator = "pdf:p1:b2-p1:b5"
        candidate.source_scope.source_digest = self.digest
        candidate.evidence_matrix[0].source_scope.source_digest = self.digest
        excerpt = self.control_excerpt + "\n" + self.lines[3][0]
        candidate.evidence_bundle[0].excerpt = excerpt
        field = candidate.evidence_matrix[0]
        field.source_scope.locator = "pdf:p1:b3-p1:b4"
        field.provenance.excerpt = excerpt
        field.provenance.source_digest = canonical_digest(excerpt)
        verified = self._verify(candidate)
        self.assertTrue(verified.source_scope_verified, verified.reasons)
        self.assertEqual(verified.verified_field_paths, ("precursor.amount",))

        # The stored field locator is the located anchor: it must lie inside
        # the verified evidence span. A trimmed fact anchors the short
        # excerpt inside the full context exactly this way.
        field.source_scope.locator = "pdf:p1:b3-p1:b3"
        anchored = self._verify(candidate)
        self.assertTrue(anchored.source_scope_verified, anchored.reasons)

        # An anchor on other group prose outside the evidence span cannot
        # claim the excerpt.
        field.source_scope.locator = "pdf:p1:b5-p1:b5"
        wrong_range = self._verify(candidate)
        self.assertIn("field_excerpt_not_in_source_group:precursor.amount",
                      wrong_range.reasons)

    def test_duplicate_quote_in_group_is_not_a_unique_source_binding(self) -> None:
        self.lines[3] = (self.control_excerpt, 10, "helv")
        self._write_pdf()
        candidate = self._candidate()
        candidate.source_scope.source_digest = self.digest
        candidate.evidence_matrix[0].source_scope.source_digest = self.digest
        result = self._verify(candidate)
        self.assertFalse(result.source_scope_verified)
        self.assertIn("field_excerpt_not_in_source_group:precursor.amount",
                      result.reasons)

    def test_string_value_may_cross_layout_whitespace_without_inference(self) -> None:
        self.lines[2] = ("Mix 2 mmol", 10, "helv")
        self.lines[3] = ("metal salt with base to pH 10.", 10, "helv")
        self._write_pdf()
        candidate = self._candidate()
        candidate.source_scope.source_digest = self.digest
        excerpt = "Mix 2 mmol\nmetal salt with base to pH 10."
        candidate.evidence_bundle[0].excerpt = excerpt
        field = candidate.evidence_matrix[0]
        field.source_scope.source_digest = self.digest
        field.source_scope.locator = "pdf:p1:b3-p1:b4"
        field.value = "mmol metal salt"
        field.unit = ""
        field.provenance.excerpt = excerpt
        field.provenance.source_digest = canonical_digest(excerpt)
        result = self._verify(candidate)
        self.assertTrue(result.source_scope_verified, result.reasons)

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
