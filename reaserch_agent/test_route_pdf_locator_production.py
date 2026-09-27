"""Unreviewed PDF quote coordinates are produced from the source layout."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_group_fact_receipt import produce_pdf_group_fact_receipt
from reaserch_agent.route_pdf_group_proposals import associate_pdf_group_proposals
from reaserch_agent.route_pdf_groups import (
    PDF_GROUP_PARSER_VERSION, PdfExperimentalGroupV1, PdfSourceBlockV1,
)
from reaserch_agent.route_pdf_locator_production import produce_pdf_proposal_locators
from reaserch_agent.route_pdf_quote_binding import PDF_QUOTE_BINDING_VERSION


class PdfProposalLocatorProductionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.digest = "sha256_" + sha256(b"controlled original PDF").hexdigest()
        self.groups = [
            self._group(
                "Arm A", self.digest,
                [("pdf:p1:b1-p1:b1", "Arm A"),
                 ("pdf:p1:b2-p1:b2", "Mix 2 mmol Ni salt.")],
            ),
            self._group(
                "Arm B", self.digest,
                [("pdf:p1:b3-p1:b3", "Arm B"),
                 ("pdf:p1:b4-p1:b4", "Mix 3 mmol Fe salt.")],
            ),
        ]

    @staticmethod
    def _group(
        name: str, digest: str, blocks: list[tuple[str, str]],
    ) -> PdfExperimentalGroupV1:
        first = blocks[0][0].split("-")[0]
        last = blocks[-1][0].split("-")[-1]
        return PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-1", experimental_group_id=name,
                section="Methods", locator=f"{first}-{last}",
                source_digest=digest,
            ),
            source_document="/controlled/paper.pdf",
            blocks=tuple(PdfSourceBlockV1(locator, text)
                         for locator, text in blocks),
        )

    @staticmethod
    def _proposal(
        group: PdfExperimentalGroupV1, *, excerpt: str,
        raw_locator: str | None, value: int = 2, unit: str = "mmol",
    ) -> dict:
        scope = group.source_scope
        fact = {
            "fact_id": "amount", "field_path": "material_graph[0].quantity.value",
            "value": value, "unit": unit, "excerpt": excerpt, "required": True,
        }
        if raw_locator is not None:
            fact["block_locator"] = raw_locator
        return {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "role_hint": "synthesis",
            "route_facts": [fact],
        }

    def _produce(
        self, groups: list[PdfExperimentalGroupV1], proposals: list[dict],
        *, parser_version: str = "parser.v1", binding_version: str = "binding.v1",
    ):
        return produce_pdf_proposal_locators(
            groups, proposals, parser_version=parser_version,
            binding_version=binding_version,
        )

    def test_wrong_model_anchor_is_recomputed_without_mutating_raw(self) -> None:
        raw = [self._proposal(
            self.groups[0], excerpt="Mix 2 mmol Ni salt.",
            raw_locator="pdf:p1:b1-p1:b1",
        )]
        before = deepcopy(raw)
        produced = self._produce(self.groups, raw)
        self.assertEqual(produced.diagnostics, [])
        self.assertEqual(raw, before)
        self.assertIsNot(produced.proposals[0], raw[0])
        self.assertEqual(
            produced.proposals[0]["route_facts"][0]["block_locator"],
            "pdf:p1:b2-p1:b2",
        )
        record = produced.resolutions[0]
        self.assertEqual(record["status"], "located_unreviewed")
        self.assertEqual(record["raw_block_locator"], "pdf:p1:b1-p1:b1")
        self.assertEqual(record["resolved_block_locator"], "pdf:p1:b2-p1:b2")
        self.assertEqual(record["resolved_span"], "pdf:p1:b2-p1:b2")
        self.assertEqual(record["source_digest"], self.digest)
        self.assertEqual(record["experimental_group_id"], "Arm A")
        self.assertEqual(record["fact_id"], "amount")
        self.assertEqual(record["field_path"], "material_graph[0].quantity.value")
        self.assertTrue(record["group_layout_digest"])
        self.assertEqual(record["parser_version"], "parser.v1")
        self.assertEqual(record["binding_version"], "binding.v1")
        # The original association still checks the new claimed block and
        # supplies the full source locator; its fail-closed behavior is intact.
        associated = associate_pdf_group_proposals(
            self.groups, [produced.proposals[0], {
                "source_group_ref": {
                    "paper_id": "paper-1", "experimental_group_id": "Arm B",
                    "source_digest": self.digest,
                },
                "role_hint": "non-route", "route_facts": [],
            }],
        )
        self.assertEqual(associated.diagnostics, [])
        self.assertEqual(
            associated.protocols[0]["route_facts"][0]["source"]["locator"],
            "pdf:p1:b2-p1:b2",
        )

    def test_missing_model_anchor_is_also_computed(self) -> None:
        raw = [self._proposal(
            self.groups[0], excerpt="Mix 2 mmol Ni salt.", raw_locator=None,
        )]
        produced = self._produce(self.groups, raw)
        self.assertEqual(produced.diagnostics, [])
        self.assertNotIn("block_locator", raw[0]["route_facts"][0])
        self.assertEqual(
            produced.proposals[0]["route_facts"][0]["block_locator"],
            "pdf:p1:b2-p1:b2",
        )
        production_defaults = produce_pdf_proposal_locators(self.groups, raw)
        self.assertEqual(production_defaults.parser_version, PDF_GROUP_PARSER_VERSION)
        self.assertEqual(production_defaults.binding_version, PDF_QUOTE_BINDING_VERSION)

    def test_repeated_quote_within_same_group_never_chooses_first(self) -> None:
        group = self._group(
            "Arm A", self.digest,
            [("pdf:p1:b1-p1:b1", "Arm A"),
             ("pdf:p1:b2-p1:b2", "Mix 2 mmol Ni salt."),
             ("pdf:p1:b3-p1:b3", "Mix 2 mmol Ni salt.")],
        )
        raw = [self._proposal(
            group, excerpt="Mix 2 mmol Ni salt.",
            raw_locator="pdf:p1:b2-p1:b2",
        )]
        produced = self._produce([group], raw)
        self.assertTrue(produced.diagnostics)
        self.assertIn(
            "fact_excerpt_ambiguous_in_group",
            [item.reason_code for item in produced.diagnostics],
        )
        self.assertEqual(produced.resolutions[0]["status"], "blocked")
        self.assertEqual(
            produced.resolutions[0]["reason_code"],
            "fact_excerpt_ambiguous_in_group",
        )
        # A model-supplied anchor does not break the tie.
        self.assertEqual(raw[0]["route_facts"][0]["block_locator"],
                         "pdf:p1:b2-p1:b2")

    def test_quote_only_in_another_group_is_not_borrowed(self) -> None:
        raw = [self._proposal(
            self.groups[0], excerpt="Mix 3 mmol Fe salt.",
            raw_locator="pdf:p1:b4-p1:b4",
        )]
        produced = self._produce(self.groups, raw)
        self.assertTrue(produced.diagnostics)
        self.assertIn(
            "fact_excerpt_not_in_block",
            [item.reason_code for item in produced.diagnostics],
        )
        self.assertEqual(produced.resolutions[0]["status"], "blocked")
        self.assertEqual(produced.resolutions[0]["experimental_group_id"], "Arm A")

    def test_per_field_progress_survives_an_atomic_batch_failure(self) -> None:
        raw = [self._proposal(
            self.groups[0], excerpt="Mix 2 mmol Ni salt.",
            raw_locator="pdf:p1:b1-p1:b1",
        )]
        raw[0]["route_facts"].append({
            "fact_id": "absent", "field_path": "material_graph[0].operation",
            "value": "stir", "unit": "", "excerpt": "stir overnight",
            "block_locator": "pdf:p1:b2-p1:b2", "required": True,
        })
        produced = self._produce(self.groups, raw)
        self.assertTrue(produced.diagnostics)
        by_id = {item["fact_id"]: item for item in produced.resolutions}
        self.assertEqual(by_id["amount"]["status"], "located_unreviewed")
        self.assertEqual(by_id["amount"]["resolved_block_locator"],
                         "pdf:p1:b2-p1:b2")
        self.assertEqual(by_id["absent"]["status"], "blocked")
        self.assertEqual(by_id["absent"]["reason_code"],
                         "fact_excerpt_not_in_block")

    def test_locator_repair_does_not_change_value_or_unit(self) -> None:
        for value, unit, expected_reason in (
            (3, "mmol", "fact_quantity_not_in_excerpt"),
            (2, "mL", "fact_quantity_not_in_excerpt"),
        ):
            with self.subTest(value=value, unit=unit):
                raw = [self._proposal(
                    self.groups[0], excerpt="Mix 2 mmol Ni salt.",
                    raw_locator="pdf:p1:b1-p1:b1", value=value, unit=unit,
                )]
                produced = self._produce(self.groups, raw)
                self.assertEqual(produced.diagnostics, [])
                fact = produced.proposals[0]["route_facts"][0]
                self.assertEqual((fact["value"], fact["unit"]), (value, unit))
                associated = associate_pdf_group_proposals(
                    self.groups, [produced.proposals[0], {
                        "source_group_ref": {
                            "paper_id": "paper-1",
                            "experimental_group_id": "Arm B",
                            "source_digest": self.digest,
                        },
                        "route_facts": [],
                    }],
                )
                self.assertEqual(associated.diagnostics, [])
                receipt = produce_pdf_group_fact_receipt(
                    self.groups, associated, signed_inventory_verified=True,
                )
                self.assertEqual(receipt.status, "blocked")
                self.assertIn(
                    f"fact[0]:{expected_reason}",
                    receipt.group_results[0].reason_codes,
                )

    def test_exact_cross_block_quote_returns_canonical_start_and_span(self) -> None:
        group = self._group(
            "Arm A", self.digest,
            [("pdf:p1:b1-p1:b1", "Arm A"),
             ("pdf:p1:b2-p1:b2", "Mix 6.25 mmol"),
             ("pdf:p1:b3-p1:b3", "Ni salt in water.")],
        )
        raw = [self._proposal(
            group, excerpt="Mix 6.25 mmol Ni salt", raw_locator="pdf:p1:b1-p1:b1",
            value=6.25,
        )]
        produced = self._produce([group], raw)
        self.assertEqual(produced.diagnostics, [])
        self.assertEqual(
            produced.proposals[0]["route_facts"][0]["block_locator"],
            "pdf:p1:b2-p1:b2",
        )
        self.assertEqual(
            produced.resolutions[0]["resolved_span"],
            "pdf:p1:b2-p1:b3",
        )
        associated = associate_pdf_group_proposals([group], produced.proposals)
        self.assertEqual(associated.diagnostics, [])
        self.assertEqual(
            associated.protocols[0]["route_facts"][0]["source"]["locator"],
            "pdf:p1:b2-p1:b3",
        )

    def test_document_layout_and_rule_versions_are_recomputed(self) -> None:
        original = self.groups[0]
        raw = [self._proposal(
            original, excerpt="Mix 2 mmol Ni salt.",
            raw_locator="pdf:p1:b1-p1:b1",
        )]
        first = self._produce([original], raw)
        self.assertEqual(first.diagnostics, [])

        # A rule-version change is visible even when the PDF parser happens
        # to return the same layout. No old record is silently reused.
        new_rules = self._produce(
            [original], raw, parser_version="parser.v2",
            binding_version="binding.v2",
        )
        self.assertEqual(new_rules.diagnostics, [])
        # The inventory fingerprint includes the parser/binder versions, so
        # rule changes invalidate a coordinate even if blocks are identical.
        self.assertNotEqual(first.resolutions[0]["group_layout_digest"],
                            new_rules.resolutions[0]["group_layout_digest"])
        self.assertEqual(new_rules.resolutions[0]["parser_version"], "parser.v2")
        self.assertEqual(new_rules.resolutions[0]["binding_version"], "binding.v2")

        # Same bytes under a changed parser layout: the source hash is the
        # same, but coordinates and layout digest must change.
        changed_layout = self._group(
            "Arm A", self.digest,
            [("pdf:p1:b8-p1:b8", "Arm A"),
             ("pdf:p1:b9-p1:b9", "Mix 2 mmol Ni salt.")],
        )
        second = self._produce(
            [changed_layout], raw, parser_version="parser.v2",
            binding_version="binding.v2",
        )
        self.assertEqual(second.diagnostics, [])
        self.assertEqual(
            second.proposals[0]["route_facts"][0]["block_locator"],
            "pdf:p1:b9-p1:b9",
        )
        self.assertNotEqual(
            first.resolutions[0]["group_layout_digest"],
            second.resolutions[0]["group_layout_digest"],
        )
        self.assertEqual(second.resolutions[0]["parser_version"], "parser.v2")
        self.assertEqual(second.resolutions[0]["binding_version"], "binding.v2")
        self.assertEqual(raw[0]["route_facts"][0]["block_locator"],
                         "pdf:p1:b1-p1:b1")

        changed_digest = "sha256_" + sha256(b"different PDF").hexdigest()
        new_document = self._group(
            "Arm A", changed_digest,
            [("pdf:p1:b8-p1:b8", "Arm A"),
             ("pdf:p1:b9-p1:b9", "Mix 2 mmol Ni salt.")],
        )
        stale = self._produce([new_document], raw)
        self.assertTrue(stale.diagnostics)
        self.assertIn("proposal_group_not_enumerated",
                      [item.reason_code for item in stale.diagnostics])

        new_raw = [self._proposal(
            new_document, excerpt="Mix 2 mmol Ni salt.",
            raw_locator="pdf:p1:b1-p1:b1",
        )]
        fresh = self._produce([new_document], new_raw)
        self.assertEqual(fresh.diagnostics, [])
        self.assertEqual(fresh.resolutions[0]["source_digest"], changed_digest)


if __name__ == "__main__":
    unittest.main()
