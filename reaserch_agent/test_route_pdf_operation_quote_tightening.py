"""Operation quotations may be shortened only by source-bound, event-safe rules."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_material_structure import (
    construct_unreviewed_split_transfer_structure,
)
from reaserch_agent.route_pdf_operation_coverage import inventory_pdf_group_operations
from reaserch_agent.route_pdf_operation_quote_tightening import (
    tighten_unreviewed_operation_quotes,
)
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
from reaserch_agent import test_route_pdf_structure_production as structure_fixture


class PdfOperationQuoteTighteningTest(unittest.TestCase):
    EVENT = "The suspension was divided into 3 parts for washing."
    BLOCKS = (
        "The feed was prepared in a beaker.",
        "The temperature record was checked.",
        "The mixture was left to settle.",
        EVENT,
        "The separate portions were collected.",
    )

    @staticmethod
    def _group(
        blocks: tuple[str, ...], *, paper_id: str = "paper-long-layout",
        group_id: str = "Long layout group",
    ) -> PdfExperimentalGroupV1:
        digest = "sha256_" + sha256(
            (paper_id + "\0" + group_id + "\0" + " ".join(blocks))
            .encode("utf-8")
        ).hexdigest()
        return PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id=paper_id,
                experimental_group_id=group_id,
                section="Methods",
                locator=f"pdf:p2:b1-p2:b{len(blocks)}",
                source_digest=digest,
            ),
            source_document="/attested/long-layout.pdf",
            blocks=tuple(
                PdfSourceBlockV1(f"pdf:p2:b{index}-p2:b{index}", text)
                for index, text in enumerate(blocks, start=1)
            ),
        )

    @classmethod
    def _proposal(cls, group: PdfExperimentalGroupV1) -> dict:
        quote = " ".join(block.text for block in group.blocks)
        _unused_group, proposal = structure_fixture.PdfStructureProductionTest._fixture(
            sentence=quote,
            phrase="divided into 3 parts",
            count=3,
            paper_id=group.source_scope.paper_id,
            group_id=group.source_scope.experimental_group_id,
        )
        proposal["source_group_ref"]["source_digest"] = (
            group.source_scope.source_digest
        )
        # Minimal semantic input: the producer, rather than this fixture, must
        # instantiate child ports and their field facts after quote tightening.
        proposal["material_graph"][0]["material_outputs"] = []
        proposal["route_facts"] = [
            fact for fact in proposal["route_facts"]
            if "material_outputs" not in fact["field_path"]
        ]
        return proposal

    @staticmethod
    def _facts(proposal: dict) -> dict[str, dict]:
        return {fact["field_path"]: fact for fact in proposal["route_facts"]}

    @staticmethod
    def _candidate_rows(group: PdfExperimentalGroupV1) -> list[dict]:
        rows, _issues = inventory_pdf_group_operations([group])
        return [row for row in rows if row.get("kind") == "split"]

    def test_unique_short_same_event_quote_enables_strict_structure(self) -> None:
        group = self._group(self.BLOCKS)
        raw = self._proposal(group)
        original = deepcopy(raw)
        candidates = self._candidate_rows(group)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["excerpt"], self.EVENT)
        long_quote = self._facts(raw)["material_graph[0].operation"]["excerpt"]
        _binding, reason = bind_pdf_quote(
            [(block.locator, block.text) for block in group.blocks], long_quote,
        )
        self.assertEqual(reason, "fact_excerpt_span_too_long")

        prepared, audit, issues = tighten_unreviewed_operation_quotes(
            raw, group, candidates,
        )
        self.assertEqual(raw, original)
        self.assertEqual(issues, [])
        paths = {
            "material_graph[0].operation",
            "material_graph[0].material_inputs[0].name",
            "material_graph[0].material_inputs[0].state",
        }
        facts = self._facts(prepared)
        self.assertTrue(all(facts[path]["excerpt"] == self.EVENT for path in paths))
        self.assertTrue(all(
            facts[path]["verification_excerpt"] == long_quote for path in paths
        ))
        audit_by_path = {row["field_path"]: row for row in audit}
        self.assertTrue(paths <= audit_by_path.keys())
        for path in paths:
            self.assertEqual(audit_by_path[path]["original_excerpt"], long_quote)
            self.assertEqual(audit_by_path[path]["produced_excerpt"], self.EVENT)
            self.assertEqual(audit_by_path[path]["candidate_id"],
                             candidates[0]["candidate_id"])
            self.assertEqual(audit_by_path[path]["source_digest"],
                             group.source_scope.source_digest)

        constructed, structure_audit, structure_issues = (
            construct_unreviewed_split_transfer_structure(prepared, group)
        )
        self.assertEqual(structure_issues, [])
        self.assertTrue(structure_audit)
        children = constructed["material_graph"][0]["material_outputs"]
        self.assertEqual(len(children), 3)
        self.assertEqual(len({child["material_instance_id"] for child in children}), 3)

    def test_count_fact_crops_only_with_matching_unitless_value(self) -> None:
        group = self._group(self.BLOCKS)
        candidate = self._candidate_rows(group)
        raw = self._proposal(group)
        raw["material_graph"][0]["count"] = 3
        count_fact = {
            "fact_id": "split-count", "field_path": "material_graph[0].count",
            "value": 3, "unit": "", "excerpt": " ".join(self.BLOCKS),
            "required": True,
        }
        raw["route_facts"].append(count_fact)
        original = deepcopy(raw)
        prepared, audit, _issues = tighten_unreviewed_operation_quotes(
            raw, group, candidate,
        )
        self.assertEqual(raw, original)
        self.assertEqual(self._facts(prepared)["material_graph[0].count"]["excerpt"],
                         self.EVENT)
        self.assertTrue(any(row["field_path"] == "material_graph[0].count"
                            for row in audit))

        for value, unit in ((4, ""), (3, "parts")):
            with self.subTest(value=value, unit=unit):
                changed = deepcopy(raw)
                changed["material_graph"][0]["count"] = value
                self._facts(changed)["material_graph[0].count"]["value"] = value
                self._facts(changed)["material_graph[0].count"]["unit"] = unit
                prepared, audit, _issues = tighten_unreviewed_operation_quotes(
                    changed, group, candidate,
                )
                self.assertEqual(
                    self._facts(prepared)["material_graph[0].count"]["excerpt"],
                    count_fact["excerpt"],
                )
                self.assertFalse(any(row["field_path"] == "material_graph[0].count"
                                     for row in audit))

    def test_quantity_and_value_missing_from_short_quote_are_not_cropped(self) -> None:
        blocks = (
            "A 20 mL amount was measured for the preparation.",
            *self.BLOCKS[1:],
        )
        group = self._group(blocks)
        raw = self._proposal(group)
        parent = raw["material_graph"][0]["material_inputs"][0]
        parent["quantity"] = {"value": 20, "unit": "mL"}
        raw["route_facts"].append({
            "fact_id": "parent-amount",
            "field_path": "material_graph[0].material_inputs[0].quantity.value",
            "value": 20, "unit": "mL",
            "excerpt": " ".join(blocks), "required": True,
        })
        original = deepcopy(raw)
        prepared, audit, _issues = tighten_unreviewed_operation_quotes(
            raw, group, self._candidate_rows(group),
        )
        self.assertEqual(raw, original)
        path = "material_graph[0].material_inputs[0].quantity.value"
        self.assertEqual(self._facts(prepared)[path]["excerpt"],
                         self._facts(raw)[path]["excerpt"])
        self.assertFalse(any(row["field_path"] == path for row in audit))

        changed = deepcopy(raw)
        operation_path = "material_graph[0].operation"
        changed["material_graph"][0]["operation"] = "checked after preparation"
        self._facts(changed)[operation_path]["value"] = "checked after preparation"
        prepared, audit, _issues = tighten_unreviewed_operation_quotes(
            changed, group, self._candidate_rows(group),
        )
        self.assertEqual(self._facts(prepared)[operation_path]["excerpt"],
                         self._facts(changed)[operation_path]["excerpt"])
        self.assertFalse(any(row["field_path"] == operation_path for row in audit))

    def test_distinct_or_ambiguous_event_never_becomes_shorter_citation(self) -> None:
        second = "After storage, the suspension was divided into 3 parts."
        blocks = (
            self.BLOCKS[0], self.BLOCKS[1], self.BLOCKS[2], self.EVENT, second,
        )
        group = self._group(blocks, group_id="Two split events")
        raw = self._proposal(group)
        prepared, audit, _issues = tighten_unreviewed_operation_quotes(
            raw, group, self._candidate_rows(group),
        )
        operation_path = "material_graph[0].operation"
        self.assertEqual(self._facts(prepared)[operation_path]["excerpt"],
                         self._facts(raw)[operation_path]["excerpt"])
        self.assertFalse(any(row["field_path"] == operation_path for row in audit))

        repeated = self._group((
            self.BLOCKS[0], self.EVENT, self.BLOCKS[2], self.EVENT, self.BLOCKS[4],
        ), group_id="Repeated identical event")
        repeated_raw = self._proposal(repeated)
        forged_candidate = {
            "candidate_id": "ambiguous-candidate",
            "paper_id": repeated.source_scope.paper_id,
            "experimental_group_id": repeated.source_scope.experimental_group_id,
            "source_digest": repeated.source_scope.source_digest,
            "kind": "split", "count": 3,
            "excerpt": self.EVENT, "locator": "pdf:p2:b2-p2:b2",
        }
        prepared, audit, _issues = tighten_unreviewed_operation_quotes(
            repeated_raw, repeated, [forged_candidate],
        )
        self.assertEqual(self._facts(prepared)[operation_path]["excerpt"],
                         self._facts(repeated_raw)[operation_path]["excerpt"])
        self.assertFalse(any(row["field_path"] == operation_path for row in audit))

    def test_other_group_candidate_cannot_tighten_quote(self) -> None:
        group = self._group(self.BLOCKS)
        other = self._group(self.BLOCKS, group_id="Other sample arm")
        raw = self._proposal(group)
        original = deepcopy(raw)
        prepared, audit, _issues = tighten_unreviewed_operation_quotes(
            raw, group, self._candidate_rows(other),
        )
        self.assertEqual(raw, original)
        self.assertEqual(prepared, raw)
        self.assertEqual(audit, [])


if __name__ == "__main__":
    unittest.main()
