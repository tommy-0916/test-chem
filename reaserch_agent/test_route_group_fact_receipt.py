"""The automatic receipt is limited to source-scoped literal checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import unittest
from unittest import mock

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_group_fact_receipt import (
    PdfGroupFactReceiptBudgetV1, produce_pdf_group_fact_receipt,
)
from reaserch_agent.route_pdf_group_proposals import (
    PdfGroupProposalAssociationResultV1, PdfGroupProposalDiagnosticV1,
)
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfSourceBlockV1,
)


class PdfGroupFactReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.digest = "sha256_" + sha256(b"signed controlled PDF bytes").hexdigest()
        self.groups = [
            self._group("Arm A", "2 mmol Ni salt", "pdf:p1:b2-p1:b2"),
            self._group("Arm B", "3 mmol Fe salt", "pdf:p1:b4-p1:b4"),
        ]
        self.protocols = [
            self._protocol(self.groups[0], 2, "Ni salt"),
            self._protocol(self.groups[1], 3, "Fe salt"),
        ]

    def _group(self, name: str, text: str, block: str) -> PdfExperimentalGroupV1:
        return PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-1", experimental_group_id=name,
                section="Methods", locator=block,
                source_digest=self.digest,
            ),
            source_document="/controlled/paper.pdf",
            blocks=(PdfSourceBlockV1(block, text),),
        )

    def _protocol(
        self, group: PdfExperimentalGroupV1, amount: int, material: str,
    ) -> dict:
        scope = group.source_scope
        quote = group.blocks[0].text
        source = {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "section": scope.section,
            "locator": group.blocks[0].locator,
            "source_digest": scope.source_digest,
        }
        return {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "group_role": "unclassified",
            "role_hint": "synthesis",
            "source": {
                "source_document": group.source_document,
                "section": scope.section,
                "locator": scope.locator,
                "source_digest": scope.source_digest,
            },
            "route_facts": [
                {
                    "fact_id": "amount", "field_path": "material_graph[0].quantity.value",
                    "value": amount, "unit": "mmol", "excerpt": quote,
                    "source": source,
                },
                {
                    "fact_id": "material", "field_path": "material_graph[0].name",
                    "value": material, "unit": "", "excerpt": quote,
                    "source": source,
                },
            ],
        }

    def _receipt(self, protocols=None, groups=None, **kwargs):
        return produce_pdf_group_fact_receipt(
            self.groups if groups is None else groups,
            self.protocols if protocols is None else protocols,
            signed_inventory_verified=True,
            **kwargs,
        )

    def test_exact_scoped_literals_emit_unsigned_review_work_orders(self) -> None:
        result = self._receipt()
        self.assertEqual(result.status, "literal_facts_verified_pending_review")
        self.assertEqual(result.verification_mode, "automated_unsigned")
        self.assertEqual(len(result.group_results), 2)
        self.assertEqual(result.group_results[0].verified_field_paths, (
            "material_graph[0].quantity.value", "material_graph[0].name",
        ))
        self.assertEqual(result.review_work_orders[0]["group_locator"], "pdf:p1:b2-p1:b2")
        self.assertEqual(result.review_work_orders[0]["role_hint_untrusted"], "synthesis")
        self.assertIn("trusted_group_role_assignment",
                      result.review_work_orders[0]["pending_checks"])
        self.assertIn("trusted_capability_mapping_if_route_group",
                      result.review_work_orders[0]["pending_checks"])
        self.assertFalse(result.review_work_orders[0]["human_chemical_review_completed"])
        self.assertFalse(result.review_work_orders[0]["execution_authorized"])

    def test_g1_dag_classification_field_defaults_and_flat_parity(self) -> None:
        # G1 integration regression: PdfGroupLiteralStatusV1 carries the new
        # dag_proven_state_field_paths (empty here — no DAG-provable state
        # facts in the fixture) and disabling the DAG map changes nothing.
        result = self._receipt()
        for group_result in result.group_results:
            self.assertEqual(group_result.dag_proven_state_field_paths, ())
        self.assertIn("dag_proven_state_field_paths", asdict(result.group_results[0]))
        with mock.patch(
            "reaserch_agent.route_group_fact_receipt."
            "build_verified_state_proof_dags", lambda *args, **kwargs: {},
        ):
            baseline = self._receipt()
        self.assertEqual(asdict(baseline), asdict(result))

    def test_numeric_and_unit_must_be_literal_in_own_block(self) -> None:
        protocols = deepcopy(self.protocols)
        protocols[0]["route_facts"][0]["value"] = 3
        result = self._receipt(protocols)
        self.assertEqual(result.status, "blocked")
        self.assertIn("fact[0]:fact_quantity_not_in_excerpt",
                      result.group_results[0].reason_codes)
        self.assertEqual(result.group_results[1].status,
                         "literal_facts_verified_pending_review")

        protocols = deepcopy(self.protocols)
        protocols[0]["route_facts"][0]["unit"] = ""
        result = self._receipt(protocols)
        self.assertIn("fact[0]:fact_numeric_unit_missing",
                      result.group_results[0].reason_codes)

    def test_dimensionless_parameter_remains_pending_with_its_source_scope(self) -> None:
        group = self._group("Arm A", "Adjust to pH 10.", "pdf:p1:b2-p1:b2")
        protocol = self._protocol(group, 10, "pH")
        protocol["material_graph"] = [{
            "parameters": [{"name": "pH", "value": 10, "unit": ""}],
        }]
        protocol["route_facts"] = [protocol["route_facts"][0]]
        protocol["route_facts"][0].update(
            field_path="material_graph[0].parameters[0].value", unit="",
        )
        result = self._receipt([protocol, self.protocols[1]],
                               [group, self.groups[1]])
        self.assertIn("fact[0]:dimensionless_semantic_pending",
                      result.group_results[0].reason_codes)
        self.assertEqual(result.group_results[1].status,
                         "literal_facts_verified_pending_review")

        protocol["material_graph"][0]["parameters"][0]["name"] = "temperature"
        result = self._receipt([protocol, self.protocols[1]],
                               [group, self.groups[1]])
        self.assertIn("fact[0]:fact_numeric_unit_missing",
                      result.group_results[0].reason_codes)

    def test_quantity_cannot_be_borrowed_from_another_material_in_same_quote(self) -> None:
        group = self._group("Arm A", "A 1 mmol and B 2 mmol", "pdf:p1:b2-p1:b2")
        protocols = [self._protocol(group, 2, "A"), self.protocols[1]]
        result = self._receipt(protocols, [group, self.groups[1]])
        self.assertEqual(result.status, "blocked")
        self.assertIn("fact[0]:fact_quantity_attribution_unresolved",
                      result.group_results[0].reason_codes)
        self.assertNotIn("material_graph[0].quantity.value",
                         result.group_results[0].verified_field_paths)

        protocols[0]["route_facts"][0]["value"] = 1
        correct = self._receipt(protocols, [group, self.groups[1]])
        self.assertEqual(correct.group_results[0].status,
                         "literal_facts_verified_pending_review")

        protocols[0]["route_facts"][1]["value"] = "not in the quote"
        without_literal_identity = self._receipt(protocols, [group, self.groups[1]])
        self.assertIn("fact[0]:fact_quantity_attribution_unresolved",
                      without_literal_identity.group_results[0].reason_codes)

    def test_short_split_quote_requires_exact_rebound_range(self) -> None:
        group = self.groups[0]
        split_group = PdfExperimentalGroupV1(
            source_scope=group.source_scope.model_copy(update={
                "locator": "pdf:p1:b2-p1:b3",
            }),
            source_document=group.source_document,
            blocks=(
                PdfSourceBlockV1("pdf:p1:b2-p1:b2", "Mix 6.25 mmol"),
                PdfSourceBlockV1("pdf:p1:b3-p1:b3", "Na2CO3 in 50 mL water"),
            ),
        )
        groups = [split_group, self.groups[1]]
        protocols = deepcopy(self.protocols)
        protocols[0]["source"]["locator"] = "pdf:p1:b2-p1:b3"
        for fact in protocols[0]["route_facts"]:
            fact["excerpt"] = "Mix 6.25\nmmol Na2CO3 in\n50 mL water"
            fact["source"]["locator"] = "pdf:p1:b2-p1:b3"
        protocols[0]["route_facts"][0].update(value=6.25)
        protocols[0]["route_facts"][1].update(value="Na2CO3 in 50 mL water")
        result = self._receipt(protocols, groups)
        self.assertEqual(result.group_results[0].status,
                         "literal_facts_verified_pending_review")
        self.assertEqual(len(result.group_results[0].verified_field_paths), 2)

        protocols[0]["route_facts"][0]["source"]["locator"] = "pdf:p1:b2-p1:b2"
        wrong_range = self._receipt(protocols, groups)
        self.assertIn("fact[0]:fact_source_locator_mismatch",
                      wrong_range.group_results[0].reason_codes)
        protocols[0]["route_facts"][0]["source"]["locator"] = "pdf:p1:b2-p1:b3"
        protocols[0]["route_facts"][0]["excerpt"] = "Mix 6.25 mmol carbonate"
        paraphrase = self._receipt(protocols, groups)
        self.assertIn("fact[0]:fact_excerpt_not_in_block",
                      paraphrase.group_results[0].reason_codes)

    def test_cross_group_locator_and_source_digest_fail_closed(self) -> None:
        protocols = deepcopy(self.protocols)
        protocols[0]["route_facts"][0]["source"]["locator"] = self.groups[1].blocks[0].locator
        result = self._receipt(protocols)
        self.assertEqual(result.status, "blocked")
        self.assertIn("fact[0]:fact_block_outside_group",
                      result.group_results[0].reason_codes)

        protocols = deepcopy(self.protocols)
        protocols[0]["source"]["source_digest"] = "sha256_" + "0" * 64
        self.assertEqual(self._receipt(protocols).reason_codes,
                         ("group_proposal_scope_not_enumerated",))

        protocols = deepcopy(self.protocols)
        protocols[0]["route_facts"][0]["source"]["source_digest"] = "sha256_" + "0" * 64
        self.assertIn("fact[0]:fact_source_scope_mismatch",
                      self._receipt(protocols).group_results[0].reason_codes)

    def test_missing_duplicate_and_diagnostic_association_never_verify_batch(self) -> None:
        self.assertEqual(self._receipt(self.protocols[:1]).reason_codes,
                         ("group_proposal_coverage_incomplete",))
        self.assertEqual(self._receipt([self.protocols[0], self.protocols[0]]).reason_codes,
                         ("group_proposal_duplicate",))
        association = PdfGroupProposalAssociationResultV1(
            protocols=self.protocols,
            diagnostics=[PdfGroupProposalDiagnosticV1("proposal_group_not_enumerated")],
        )
        self.assertEqual(self._receipt(association).reason_codes,
                         ("proposal_association_has_diagnostics",))
        blocked = self._receipt(association)
        self.assertEqual(len(blocked.review_work_orders), 2)
        self.assertEqual(blocked.review_work_orders[0]["experimental_group_id"],
                         "Arm A")
        self.assertFalse(blocked.review_work_orders[0]["human_chemical_review_completed"])

    def test_duplicate_facts_and_authority_fields_are_rejected(self) -> None:
        protocols = deepcopy(self.protocols)
        protocols[0]["route_facts"].append(deepcopy(protocols[0]["route_facts"][0]))
        self.assertIn("fact[2]:fact_identity_duplicate",
                      self._receipt(protocols).group_results[0].reason_codes)

        protocols = deepcopy(self.protocols)
        protocols[0]["human_chemical_review_completed"] = True
        self.assertEqual(self._receipt(protocols).reason_codes,
                         ("group_proposal_authority_field_forbidden",))

        protocols = deepcopy(self.protocols)
        protocols[0]["route_facts"][0]["review_status"] = "approved"
        self.assertIn("fact[0]:fact_authority_field_forbidden",
                      self._receipt(protocols).group_results[0].reason_codes)

    def test_shared_fact_id_requires_identical_quote_and_source(self) -> None:
        protocols = deepcopy(self.protocols)
        # One source quotation can support both the reagent name and amount.
        protocols[0]["route_facts"][1]["fact_id"] = "amount"
        shared = self._receipt(protocols)
        self.assertEqual(shared.status, "literal_facts_verified_pending_review")
        self.assertEqual(shared.group_results[0].reason_codes, ())
        self.assertEqual(len(shared.group_results[0].verified_field_paths), 2)

        # Reusing the ID for a different literal quote is a provenance conflict
        # even when the shorter quote still occurs in the same PDF block.
        protocols[0]["route_facts"][1]["excerpt"] = "Ni salt"
        conflicting = self._receipt(protocols)
        self.assertEqual(conflicting.status, "blocked")
        self.assertIn("fact[1]:fact_id_conflict",
                      conflicting.group_results[0].reason_codes)

    def test_empty_context_section_has_no_literal_failure_or_route_authority(self) -> None:
        protocols = deepcopy(self.protocols)
        protocols[1]["role_hint"] = "characterization"
        protocols[1]["route_facts"] = []
        result = self._receipt(protocols)
        self.assertEqual(result.status, "literal_checks_completed_pending_review")
        self.assertEqual(result.group_results[1].status,
                         "no_facts_to_check_pending_role")
        self.assertEqual(result.group_results[1].reason_codes, ())
        self.assertFalse(result.review_work_orders[1]["execution_authorized"])
        self.assertIn("trusted_group_role_assignment",
                      result.review_work_orders[1]["pending_checks"])

    def test_unsigned_inventory_and_budgets_abstain(self) -> None:
        self.assertEqual(produce_pdf_group_fact_receipt(
            self.groups, self.protocols, signed_inventory_verified=False,
        ).reason_codes, ("signed_group_inventory_not_verified",))
        self.assertEqual(self._receipt(budget=PdfGroupFactReceiptBudgetV1(
            max_groups=1,
        )).reason_codes, ("group_budget_exceeded",))
        self.assertEqual(self._receipt(budget=PdfGroupFactReceiptBudgetV1(
            max_facts=3,
        )).reason_codes, ("fact_budget_exceeded",))
        self.assertEqual(self._receipt(budget=PdfGroupFactReceiptBudgetV1(
            max_proposal_chars=100,
        )).reason_codes, ("proposal_char_budget_exceeded",))
        with self.assertRaises(ValueError):
            PdfGroupFactReceiptBudgetV1(max_blocks=0)


if __name__ == "__main__":
    unittest.main()
