"""Unreviewed proposal feedback mirrors the receipt's basic literal shape."""

from __future__ import annotations

from copy import deepcopy
import unittest

from reaserch_agent.route_pdf_proposal_quality import (
    assess_unreviewed_proposal_literal_shape,
)


class UnreviewedProposalLiteralShapeTest(unittest.TestCase):
    def _fact(self, value, unit, excerpt: str) -> dict:
        return {
            "fact_id": "f1", "field_path": "material_graph[0].operation",
            "value": value, "unit": unit, "excerpt": excerpt,
            "required": True,
        }

    def _audit(self, fact: dict) -> list[dict]:
        proposals = [{"route_facts": [fact]}]
        original = deepcopy(proposals)
        result = assess_unreviewed_proposal_literal_shape(proposals)
        self.assertEqual(proposals, original)
        return result

    def test_null_unit_is_reported_without_converting_it(self) -> None:
        result = self._audit(self._fact("mixed", None, "The solution was mixed."))
        self.assertEqual(result, [{
            "proposal_index": 0, "fact_index": 0,
            "fact_id": "f1", "field_path": "material_graph[0].operation",
            "reason_code": "fact_unit_invalid",
        }])

    def test_false_required_flag_is_reported(self) -> None:
        fact = self._fact("mixed", "", "The solution was mixed.")
        fact["required"] = False
        self.assertEqual(
            [item["reason_code"] for item in self._audit(fact)],
            ["fact_required_must_be_true"],
        )

    def test_paraphrased_string_value_is_not_literal(self) -> None:
        result = self._audit(self._fact(
            "prepare nickel precursor solution", "",
            "Ni(NO3)2 was dissolved in water.",
        ))
        self.assertEqual(result[0]["reason_code"], "fact_value_not_in_excerpt")

    def test_null_unit_and_paraphrase_are_reported_together(self) -> None:
        result = self._audit(self._fact(
            "prepare precursor solution", None, "The salts were dissolved in water.",
        ))
        self.assertEqual(
            [item["reason_code"] for item in result],
            ["fact_unit_invalid", "fact_value_not_in_excerpt"],
        )

    def test_exact_string_uses_receipt_whitespace_and_word_boundaries(self) -> None:
        self.assertEqual(self._audit(self._fact(
            "mixed with water", "", "The salts were mixed\nwith water.",
        )), [])
        result = self._audit(self._fact("mix", "", "The mixture was stirred."))
        self.assertEqual(result[0]["reason_code"], "fact_value_not_in_excerpt")

    def test_numeric_unit_and_literal_quantity_are_both_required(self) -> None:
        self.assertEqual(self._audit(self._fact(2, "mmol", "2 mmol Ni salt")), [])
        missing = self._audit(self._fact(2, "", "2 mmol Ni salt"))
        self.assertEqual(missing[0]["reason_code"], "fact_numeric_unit_missing")
        false_quote = self._audit(self._fact(2, "mmol", "3 mmol Ni salt"))
        self.assertEqual(false_quote[0]["reason_code"],
                         "fact_quantity_not_in_excerpt")

    def test_non_numeric_value_and_string_with_unit_are_reported(self) -> None:
        self.assertEqual(
            self._audit(self._fact(True, "", "true"))[0]["reason_code"],
            "fact_value_type_unverifiable",
        )
        self.assertEqual(
            self._audit(self._fact("stir", "rpm", "stir at 800 rpm"))[0]
            ["reason_code"], "fact_unit_non_numeric",
        )

    def test_records_indices_for_multiple_proposals_without_partial_admission(self) -> None:
        proposals = [
            {"route_facts": [self._fact("mix", "", "mix"),
                             self._fact(2, "mmol", "3 mmol")]},
            {"route_facts": [self._fact("stir", None, "stir")]},
        ]
        result = assess_unreviewed_proposal_literal_shape(proposals)
        self.assertEqual(
            [(item["proposal_index"], item["fact_index"], item["reason_code"])
             for item in result],
            [(0, 1, "fact_quantity_not_in_excerpt"),
             (1, 0, "fact_unit_invalid")],
        )


if __name__ == "__main__":
    unittest.main()
