"""Source operation coverage detects omitted, affirmative material events."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_operation_coverage import (
    audit_unreviewed_operation_coverage,
    inventory_pdf_group_operations,
)
from reaserch_agent import test_route_pdf_structure_production as structure_fixture


class PdfOperationCoverageTest(unittest.TestCase):
    @staticmethod
    def _group(
        blocks: list[str], *, group_id: str = "Other paper Group A",
        paper_id: str = "paper-independent",
    ) -> PdfExperimentalGroupV1:
        digest = "sha256_" + sha256(
            (paper_id + "\0" + group_id + "\0" + "\0".join(blocks)).encode("utf-8")
        ).hexdigest()
        last = len(blocks)
        return PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id=paper_id, experimental_group_id=group_id,
                section="Methods", locator=f"pdf:p2:b1-p2:b{last}",
                source_digest=digest,
            ),
            source_document="/attested/independent-paper.pdf",
            blocks=tuple(PdfSourceBlockV1(
                f"pdf:p2:b{index}-p2:b{index}", text,
            ) for index, text in enumerate(blocks, start=1)),
        )

    @staticmethod
    def _proposal(group: PdfExperimentalGroupV1, quote: str,
                  operation: str, *, output_count: int = 1) -> dict:
        _fixture_group, proposal = structure_fixture.PdfStructureProductionTest._fixture(
            phrase="divided into 3 parts", count=3,
            group_id=group.source_scope.experimental_group_id,
            paper_id=group.source_scope.paper_id,
            sentence=quote,
        )
        proposal["source_group_ref"] = {
            "paper_id": group.source_scope.paper_id,
            "experimental_group_id": group.source_scope.experimental_group_id,
            "source_digest": group.source_scope.source_digest,
        }
        step = proposal["material_graph"][0]
        step["operation"] = operation
        step["material_outputs"] = step["material_outputs"][:output_count]
        for fact in proposal["route_facts"]:
            if fact["field_path"] in {
                "material_graph[0].operation",
                "route_signature.target_transformation",
                "route_signature.operations[0]",
            }:
                fact["value"] = operation
            fact["excerpt"] = quote
        proposal["route_facts"] = [
            fact for fact in proposal["route_facts"]
            if not fact["field_path"].startswith(
                "material_graph[0].material_outputs[1]"
            ) and not fact["field_path"].startswith(
                "material_graph[0].material_outputs[2]"
            )
        ] if output_count == 1 else proposal["route_facts"]
        proposal["route_signature"]["target_transformation"] = operation
        proposal["route_signature"]["operations"] = [operation]
        return proposal

    @staticmethod
    def _reason_codes(rows: list[dict]) -> set[str]:
        return {row["reason_code"] for row in rows}

    def test_split_in_time_step_excerpt_is_unrepresented(self) -> None:
        quote = "After 1 h, the suspension was divided into 3 parts for washing."
        group = self._group([quote])
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertEqual(inventory_issues, [])
        self.assertTrue(any(
            row.get("kind") == "split" and row.get("count") == 3
            and row.get("experimental_group_id") == group.source_scope.experimental_group_id
            for row in candidates
        ))
        proposal = self._proposal(group, quote, "After 1 h")
        original = deepcopy(proposal)
        audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertEqual(proposal, original)
        self.assertIn("source_operation_unrepresented",
                      self._reason_codes(diagnostics))
        self.assertTrue(audit)

    def test_split_subject_may_be_prior_step_output_not_current_input(self) -> None:
        precursor = "A dark precipitate appeared during the reaction."
        split = "After 1 h, the suspension was divided into 3 parts for washing."
        group = self._group([precursor, split], group_id="Reaction then split")
        proposal = self._proposal(group, split, "After 1 h")
        parent = proposal["material_graph"][0]["material_inputs"][0]
        parent["name"] = "precipitate"
        parent["state"] = "precipitate"
        for fact in proposal["route_facts"]:
            if fact["field_path"] == "material_graph[0].material_inputs[0].name":
                fact["value"] = "precipitate"
                fact["excerpt"] = precursor
            elif fact["field_path"] == "material_graph[0].material_inputs[0].state":
                fact["value"] = "precipitate"
                fact["excerpt"] = precursor
        candidates, _inventory_issues = inventory_pdf_group_operations([group])
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertIn("source_operation_unrepresented",
                      self._reason_codes(diagnostics))

    def test_modeled_split_operation_is_covered_without_paper_specific_ids(self) -> None:
        quote = "The suspension was divided into 3 parts for parallel washing."
        group = self._group([quote], group_id="Different mixture")
        candidates, _ = inventory_pdf_group_operations([group])
        proposal = self._proposal(group, quote, "divided into 3 parts",
                                  output_count=3)
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertNotIn("source_operation_unrepresented",
                         self._reason_codes(diagnostics))

    def test_negated_or_wrong_subject_does_not_become_actionable_omission(self) -> None:
        for quote in (
            "After 1 h, the suspension was not divided into 3 parts.",
            "After 1 h, the vessel containing suspension was divided into 3 parts.",
        ):
            group = self._group([quote])
            candidates, _inventory_issues = inventory_pdf_group_operations([group])
            proposal = self._proposal(group, quote, "After 1 h")
            _audit, diagnostics = audit_unreviewed_operation_coverage(
                proposal, group, candidates,
            )
            with self.subTest(quote=quote):
                self.assertNotIn("source_operation_unrepresented",
                                 self._reason_codes(diagnostics))

    def test_repeated_ambiguous_and_other_group_mentions_do_not_transfer(self) -> None:
        quote = "The suspension was divided into 3 parts for washing."
        group = self._group([quote, quote])
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertFalse(any(
            row.get("kind") == "split" and row.get("locator")
            for row in candidates
        ))
        self.assertIn("fact_excerpt_ambiguous_in_group",
                      self._reason_codes(inventory_issues))
        proposal = self._proposal(group, quote, "After 1 h")
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertNotIn("source_operation_unrepresented",
                         self._reason_codes(diagnostics))

        other = self._group(["The suspension was held for 1 h."],
                            group_id="Other group")
        other_proposal = self._proposal(
            other, other.blocks[0].text, "held for 1 h"
        )
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            other_proposal, other, candidates,
        )
        self.assertNotIn("source_operation_unrepresented",
                         self._reason_codes(diagnostics))

    def test_two_distinct_same_group_split_events_need_separate_representation(self) -> None:
        first = "After washing, the suspension was divided into 3 parts."
        second = "After storage, the suspension was divided into 3 parts."
        group = self._group([first, second], group_id="Two distinct splits")
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertEqual(inventory_issues, [])
        split_rows = [row for row in candidates if row.get("kind") == "split"]
        self.assertEqual(len(split_rows), 2)
        self.assertEqual({row["locator"] for row in split_rows}, {
            "pdf:p2:b1-p2:b1", "pdf:p2:b2-p2:b2",
        })
        proposal = self._proposal(
            group, first, "divided into 3 parts", output_count=3,
        )
        audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertEqual(
            {row["locator"]: row["status"] for row in audit},
            {"pdf:p2:b1-p2:b1": "represented", "pdf:p2:b2-p2:b2": "unrepresented"},
        )
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["reason_code"],
                         "source_operation_unrepresented")
        self.assertEqual(diagnostics[0]["locator"], "pdf:p2:b2-p2:b2")

    def test_alternate_count_spanning_two_layout_blocks_is_found(self) -> None:
        blocks = [
            "The suspension was divided",
            "into 4 parts for separate measurements.",
        ]
        group = self._group(blocks, group_id="Four measured portions")
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertEqual(inventory_issues, [])
        self.assertTrue(any(
            row.get("kind") == "split" and row.get("count") == 4
            and row.get("experimental_group_id") == group.source_scope.experimental_group_id
            for row in candidates
        ))

    def test_material_subject_and_split_across_blocks_remain_actionable(self) -> None:
        blocks = ["After 1 h, the suspension was", "divided into 3 parts."]
        group = self._group(blocks, group_id="Cross-block subject")
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertEqual(inventory_issues, [])
        splits = [row for row in candidates if row.get("kind") == "split"]
        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0]["count"], 3)
        self.assertEqual(splits[0]["locator"], "pdf:p2:b1-p2:b2")
        proposal = self._proposal(group, " ".join(blocks), "After 1 h")
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertIn("source_operation_unrepresented",
                      self._reason_codes(diagnostics))

    def test_multiple_events_and_unknown_count_are_scoped_unresolved(self) -> None:
        cases = (
            (
                "The suspension was transferred to a vial and then divided into 3 parts.",
                "source_operation_multiple_events_unresolved",
                "multiple",
            ),
            (
                "The suspension was divided into portions for washing.",
                "source_operation_count_unresolved",
                "split",
            ),
            (
                "The suspension was divided into portions and washed with "
                "3 parts water.",
                "source_operation_count_unresolved",
                "split",
            ),
        )
        for quote, code, kind in cases:
            with self.subTest(quote=quote):
                group = self._group([quote], group_id=f"Unresolved {kind}")
                candidates, inventory_issues = inventory_pdf_group_operations([group])
                self.assertIn(code, self._reason_codes(inventory_issues))
                unresolved = [row for row in candidates if row.get("kind") == kind]
                self.assertEqual(len(unresolved), 1)
                self.assertEqual(unresolved[0]["paper_id"], group.source_scope.paper_id)
                self.assertEqual(
                    unresolved[0]["experimental_group_id"],
                    group.source_scope.experimental_group_id,
                )
                self.assertEqual(unresolved[0]["source_digest"],
                                 group.source_scope.source_digest)
                proposal = self._proposal(group, quote, "After 1 h")
                _audit, diagnostics = audit_unreviewed_operation_coverage(
                    proposal, group, candidates,
                )
                self.assertIn(code, self._reason_codes(diagnostics))

                unrelated = self._group(
                    ["The solution was kept under observation."],
                    group_id="Unrelated material group",
                )
                other_proposal = self._proposal(
                    unrelated, unrelated.blocks[0].text, "kept under observation",
                )
                _audit, other_diagnostics = audit_unreviewed_operation_coverage(
                    other_proposal, unrelated, candidates,
                )
                self.assertNotIn(code, self._reason_codes(other_diagnostics))

    def test_operation_fact_with_unique_preceding_context_still_represents_event(self) -> None:
        context = "The mixture was held at room temperature."
        event = "The suspension was divided into 3 parts."
        group = self._group([context, event], group_id="Context before split")
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertEqual(inventory_issues, [])
        split_rows = [row for row in candidates if row.get("kind") == "split"]
        self.assertEqual(len(split_rows), 1)
        self.assertEqual(split_rows[0]["locator"], "pdf:p2:b2-p2:b2")
        broad_quote = f"{context} {event}"
        proposal = self._proposal(
            group, broad_quote, "divided into 3 parts", output_count=3,
        )
        audit, diagnostics = audit_unreviewed_operation_coverage(
            proposal, group, candidates,
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["status"], "represented")

    def test_transfer_omission_and_exact_representation(self) -> None:
        quote = "After washing, the suspension was transferred to a clean vial."
        group = self._group([quote], paper_id="paper-transfer",
                            group_id="Transfer experiment")
        candidates, inventory_issues = inventory_pdf_group_operations([group])
        self.assertEqual(inventory_issues, [])
        self.assertTrue(any(row.get("kind") == "transfer" for row in candidates))
        omitted = self._proposal(group, quote, "After washing")
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            omitted, group, candidates,
        )
        self.assertIn("source_operation_unrepresented",
                      self._reason_codes(diagnostics))
        represented = self._proposal(group, quote, "transferred")
        _audit, diagnostics = audit_unreviewed_operation_coverage(
            represented, group, candidates,
        )
        self.assertNotIn("source_operation_unrepresented",
                         self._reason_codes(diagnostics))


if __name__ == "__main__":
    unittest.main()
