"""Local PDF feedback retains passing facts without admitting partial routes."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_group_proposals import associate_pdf_group_proposals
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_local_diagnostics import (
    assess_pdf_group_proposal_fields,
)


class PdfProposalLocalDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.digest = "sha256_" + sha256(b"fixed PDF").hexdigest()
        scope = ExperimentalGroupScopeV1(
            paper_id="paper", experimental_group_id="group-a",
            section="Methods", locator="pdf:p1:b1-p1:b1",
            source_digest=self.digest,
        )
        self.group = PdfExperimentalGroupV1(
            source_scope=scope, source_document="/signed/fixed.pdf",
            blocks=(PdfSourceBlockV1(
                "pdf:p1:b1-p1:b1", "A 1 mmol and B 2 mmol were mixed."
            ),),
        )

    def _proposal(self, facts: list[dict], graph: list | None = None) -> dict:
        return {
            "source_group_ref": {
                "paper_id": "paper", "experimental_group_id": "group-a",
                "source_digest": self.digest,
            },
            "role_hint": "synthesis",
            "route_facts": facts,
            "material_graph": graph if graph is not None else [],
        }

    def _fact(self, fact_id: str, field_path: str, value,
              unit, excerpt: str) -> dict:
        return {
            "fact_id": fact_id, "field_path": field_path,
            "value": value, "unit": unit, "excerpt": excerpt,
            "required": True,
        }

    def test_local_quantity_attribution_uses_all_sibling_facts(self) -> None:
        quote = "A 1 mmol and B 2 mmol were mixed."
        facts = [
            self._fact("amount", "material_graph[0].material_inputs[0].quantity.value",
                       2, "mmol", quote),
            self._fact("name", "material_graph[0].material_inputs[0].name",
                       "A", "", quote),
        ]
        graph = [{"material_inputs": [{"name": "A", "quantity": {
            "value": 2, "unit": "mmol",
        }}]}]
        proposals = [self._proposal(facts, graph)]
        original = deepcopy(proposals)
        assessed = assess_pdf_group_proposal_fields([self.group], proposals)
        self.assertEqual(proposals, original)
        self.assertEqual(assessed.located.diagnostics, [])
        self.assertIn(
            (0, 0, "fact_quantity_attribution_unresolved"),
            {(issue["proposal_index"], issue["fact_index"], issue["reason_code"])
             for issue in assessed.issues},
        )
        self.assertIn((0, 1), assessed.passing_fact_slots)
        self.assertNotIn((0, 0), assessed.passing_fact_slots)
        self.assertEqual(assessed.group_issue_indexes, (0,))

    def test_locator_failure_and_shape_failure_are_both_visible(self) -> None:
        facts = [
            self._fact("missing", "material_graph[0].operation",
                       "centrifuge", "", "No such excerpt"),
            self._fact("bad-unit", "material_graph[0].material_inputs[0].name",
                       "A", None, "A 1 mmol and B 2 mmol were mixed."),
            self._fact("passing", "material_graph[0].material_inputs[1].name",
                       "B", "", "A 1 mmol and B 2 mmol were mixed."),
        ]
        proposals = [self._proposal(facts)]
        assessed = assess_pdf_group_proposal_fields([self.group], proposals)
        issues = {
            (issue["fact_id"], issue["reason_code"])
            for issue in assessed.issues
        }
        self.assertIn(("missing", "fact_excerpt_not_in_block"), issues)
        self.assertIn(("bad-unit", "fact_unit_invalid"), issues)
        self.assertEqual(assessed.passing_fact_slots, ((0, 2),))
        self.assertEqual(assessed.group_issue_indexes, (0,))
        # The strict association still admits no partial protocol batch.
        strict = associate_pdf_group_proposals(
            [self.group], assessed.located.proposals,
        )
        self.assertEqual(strict.protocols, [])

    def test_full_literal_pass_has_no_issues(self) -> None:
        quote = "A 1 mmol and B 2 mmol were mixed."
        proposals = [self._proposal([
            self._fact("amount", "material_graph[0].material_inputs[0].quantity.value",
                       1, "mmol", quote),
            self._fact("name", "material_graph[0].material_inputs[0].name",
                       "A", "", quote),
        ], [{"material_inputs": [{"name": "A", "quantity": {
            "value": 1, "unit": "mmol",
        }}]}])]
        assessed = assess_pdf_group_proposal_fields([self.group], proposals)
        self.assertEqual(assessed.issues, [])
        self.assertEqual(assessed.passing_fact_slots, ((0, 0), (0, 1)))
        self.assertEqual(assessed.group_issue_indexes, ())

    def test_strict_feedback_names_missing_graph_fact_and_invalid_path(self) -> None:
        quote = "A 1 mmol and B 2 mmol were mixed."
        proposal = self._proposal([
            self._fact("op", "material_graph[0].operation", "mixed", "", quote),
            self._fact("bad", "target.material", "A", "", quote),
        ], [{"operation": "mixed", "material_inputs": [{"name": "A"}]}])
        assessed = assess_pdf_group_proposal_fields(
            [self.group], [proposal], check_required_graph_facts=True,
        )
        self.assertIn(
            ("required_graph_fact_missing", "material_graph[0].material_inputs[0].name"),
            {(item["reason_code"], item["field_path"]) for item in assessed.issues},
        )
        self.assertIn(
            ("fact_graph_path_missing", "target.material"),
            {(item["reason_code"], item["field_path"]) for item in assessed.issues},
        )

    def test_strict_feedback_checks_fact_value_against_graph_leaf(self) -> None:
        quote = "A 1 mmol and B 2 mmol were mixed."
        proposal = self._proposal([
            self._fact("op", "material_graph[0].operation", "mixed", "", quote),
        ], [{"operation": "stirred"}])
        assessed = assess_pdf_group_proposal_fields(
            [self.group], [proposal], check_required_graph_facts=True,
        )
        self.assertIn("fact_graph_value_mismatch", {
            item["reason_code"] for item in assessed.issues
        })


if __name__ == "__main__":
    unittest.main()
