"""Concentration attribution through proposal association and both fact gates."""

from __future__ import annotations

from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from chem_agent_contracts.route_source_labels import RULE_DEFINITION_SITE_CONCENTRATION
from reaserch_agent.route_group_compiler import _fact_issue
from reaserch_agent.route_group_fact_receipt import (
    _literal_fact_reason, produce_pdf_group_fact_receipt,
)
from reaserch_agent.route_pdf_group_proposals import associate_pdf_group_proposals
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1


class ConcentrationConsumerRegressionTests(unittest.TestCase):
    PATH = "material_graph[0].material_inputs[0].concentration_value"
    LOCATOR = "pdf:p1:b2-p1:b2"

    def _associated_case(
        self, excerpt: str, *, subject: str = "Solution A", competitor: str | None = None,
    ) -> tuple[PdfExperimentalGroupV1, dict, dict]:
        """Let the existing association own the source fields of each fact."""
        digest = "sha256_" + sha256(b"controlled synthetic PDF source").hexdigest()
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="synthetic-paper", experimental_group_id="Preparation",
                section="Methods", locator=self.LOCATOR, source_digest=digest,
            ),
            source_document="/controlled/synthetic.pdf",
            blocks=(PdfSourceBlockV1(self.LOCATOR, excerpt),),
        )
        ports = [{
            "material_id": "product-entity", "name": subject, "state": "solution",
            "concentration_value": 1, "concentration_unit": "M",
        }]
        facts = [{
            "fact_id": "product-name",
            "field_path": "material_graph[0].material_inputs[0].name",
            "value": subject, "unit": "", "excerpt": excerpt, "required": True,
        }]
        if competitor is not None:
            ports.append({
                "material_id": "other-entity", "name": competitor, "state": "solution",
            })
            facts.append({
                "fact_id": "other-name",
                "field_path": "material_graph[0].material_inputs[1].name",
                "value": competitor, "unit": "", "excerpt": excerpt, "required": True,
            })
        facts.append({
            "fact_id": "claimed-concentration", "field_path": self.PATH,
            "value": 1, "unit": "M", "excerpt": excerpt, "required": True,
        })
        for fact in facts:
            fact["block_locator"] = self.LOCATOR
        proposed = {
            "source_group_ref": {
                "paper_id": group.source_scope.paper_id,
                "experimental_group_id": group.source_scope.experimental_group_id,
                "source_digest": digest,
            },
            "role_hint": "synthesis",
            "material_graph": [{
                "macro_step_id": "S1", "sequence": 1, "sample_id": "sample-1",
                "material_inputs": ports, "material_outputs": [],
            }],
            "route_facts": facts,
        }
        associated = associate_pdf_group_proposals([group], [proposed])
        self.assertEqual(associated.diagnostics, [])
        self.assertEqual(len(associated.protocols), 1)
        protocol = associated.protocols[0]
        concentration = next(
            fact for fact in protocol["route_facts"] if fact["field_path"] == self.PATH
        )
        self.assertEqual(concentration["source"]["locator"], self.LOCATOR)
        return group, protocol, concentration

    def _consumer_results(self, group, protocol, fact):
        compiler_binding: dict = {}
        receipt_binding: dict = {}
        scope = group.source_scope
        compiler_reason = _fact_issue(
            fact, paper_id=scope.paper_id, group_id=scope.experimental_group_id,
            section=scope.section, document_digest=scope.source_digest,
            graph=protocol["material_graph"], signature={},
            facts=protocol["route_facts"], binding_sink=compiler_binding,
        )
        receipt_reason = _literal_fact_reason(
            fact, group, tuple((block.locator, block.text) for block in group.blocks),
            graph=protocol["material_graph"], facts=protocol["route_facts"],
            binding_sink=receipt_binding,
        )
        receipt = produce_pdf_group_fact_receipt(
            [group], [protocol], signed_inventory_verified=True,
        )
        return compiler_reason, receipt_reason, compiler_binding, receipt_binding, receipt

    def _assert_blocked(self, excerpt, **kwargs):
        group, protocol, fact = self._associated_case(excerpt, **kwargs)
        compiler, literal, compiler_binding, literal_binding, receipt = (
            self._consumer_results(group, protocol, fact)
        )
        self.assertEqual(compiler, "route_fact_quantity_attribution_unresolved")
        self.assertEqual(literal, "fact_quantity_attribution_unresolved")
        self.assertNotIn("binding", compiler_binding)
        self.assertNotIn("binding", literal_binding)
        self.assertEqual(receipt.status, "blocked")
        self.assertNotIn(self.PATH, receipt.group_results[0].verified_field_paths)

    def test_concentration_cannot_cross_preparation_subjects_or_sentences(self):
        for subject, other in (
            ("Solution A", "Solution B"), ("Batch Violet", "Batch Teal"),
        ):
            for separator in (", then ", ". ", "; "):
                quote = (
                    f"{subject} was washed{separator}{other} was prepared by "
                    "dissolving 100 mmol reagent in 100 mL solvent (1 M)."
                )
                with self.subTest(quote=quote):
                    self._assert_blocked(quote, subject=subject, competitor=other)

    def test_single_subject_source_definition_passes_both_consumers(self):
        for subject in ("Solution B", "Batch Violet"):
            quote = (
                f"{subject} was prepared by dissolving 100 mmol NaOH "
                "in 100 mL water (1 M)."
            )
            group, protocol, fact = self._associated_case(quote, subject=subject)
            compiler, literal, compiler_binding, literal_binding, receipt = (
                self._consumer_results(group, protocol, fact)
            )
            with self.subTest(subject=subject):
                self.assertEqual(compiler, "")
                self.assertEqual(literal, "")
                self.assertEqual(compiler_binding, literal_binding)
                binding = compiler_binding["binding"]
                self.assertEqual(binding["rule_id"], RULE_DEFINITION_SITE_CONCENTRATION)
                self.assertEqual(binding["entity_material_id"], "product-entity")
                self.assertEqual(
                    binding["mention_anchor"],
                    "material_graph[0].material_inputs[0].name",
                )
                self.assertEqual(receipt.status, "literal_facts_verified_pending_review")
                self.assertIn(self.PATH, receipt.group_results[0].verified_field_paths)

    def test_mixing_another_source_stock_cannot_certify_product_concentration(self):
        self._assert_blocked(
            "Solution A was prepared by mixing 5 mL Solution B "
            "with 20 mL water (1 M).",
            competitor="Solution B",
        )

    def test_quantified_competing_entity_still_leaves_concentration_ambiguous(self):
        # The text has the same single dissolution grammar as the positive
        # case.  The competing, source-bound entity makes the difference;
        # forgetting to pass that context to either consumer breaks this test.
        self._assert_blocked(
            "Solution A was prepared by dissolving 100 mmol reagent "
            "in 100 mL carrier (1 M).",
            competitor="carrier",
        )


if __name__ == "__main__":
    unittest.main()
