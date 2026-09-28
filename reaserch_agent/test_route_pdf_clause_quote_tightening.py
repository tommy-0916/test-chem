"""Unique-but-overlong quotes may tighten only to a value-bearing local clause."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_group_compiler import (
    material_identity_for_amount_path, quantity_has_local_attribution,
)
from reaserch_agent.route_pdf_clause_quote_tightening import (
    tighten_unreviewed_clause_quotes,
)
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_locator_production import produce_pdf_proposal_locators
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote


class PdfClauseQuoteTighteningTest(unittest.TestCase):
    SENTENCE = (
        "After a total reaction time of 1 h, around 200 mL of the suspension "
        "(~500 mg samples) was divided into 8 parts, followed by a washing "
        "protocol using deionized water three times before the portions were "
        "collected."
    )
    BLOCKS = (
        "The feed was prepared in a beaker.",
        "After a total reaction time of 1 h, around 200 mL of",
        "the suspension (~500 mg samples) was divided into 8 parts, followed",
        "by a washing protocol using deionized water three",
        "times before the portions were collected.",
    )
    TIME_CLAUSE = "After a total reaction time of 1 h"
    EVENT_CLAUSE = (
        "around 200 mL of the suspension (~500 mg samples) was divided "
        "into 8 parts"
    )

    @staticmethod
    def _group(
        blocks: tuple[str, ...], *, paper_id: str = "paper-compound-layout",
        group_id: str = "Compound layout group",
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
            source_document="/attested/compound-layout.pdf",
            blocks=tuple(
                PdfSourceBlockV1(f"pdf:p2:b{index}-p2:b{index}", text)
                for index, text in enumerate(blocks, start=1)
            ),
        )

    @classmethod
    def _proposal(cls, group: PdfExperimentalGroupV1) -> dict:
        scope = group.source_scope
        excerpt = " ".join(block.text for block in group.blocks)
        facts = [
            {"fact_id": "f-time",
             "field_path": "route_signature.operations[0]",
             "value": cls.TIME_CLAUSE, "unit": "", "excerpt": excerpt,
             "required": True},
            {"fact_id": "f-op",
             "field_path": "route_signature.operations[1]",
             "value": "divided into 8 parts", "unit": "", "excerpt": excerpt,
             "required": True},
            {"fact_id": "f-name",
             "field_path": "material_graph[0].material_outputs[0].name",
             "value": "suspension", "unit": "", "excerpt": excerpt,
             "required": True},
            {"fact_id": "f-state",
             "field_path": "material_graph[0].material_outputs[0].state",
             "value": "suspension", "unit": "", "excerpt": excerpt,
             "required": True},
            {"fact_id": "f-qty",
             "field_path": "material_graph[0].material_outputs[0].quantity.value",
             "value": 200, "unit": "mL", "excerpt": excerpt,
             "required": True},
        ]
        return {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [{
                "step_index": 0,
                "operation": "divided into 8 parts",
                "material_outputs": [{
                    "name": "suspension", "state": "suspension",
                    "quantity": {"value": 200, "unit": "mL"},
                }],
            }],
            "route_facts": facts,
        }

    @staticmethod
    def _facts(proposal: dict) -> dict[str, dict]:
        return {fact["fact_id"]: fact for fact in proposal["route_facts"]}

    def test_overlong_unique_quotes_tighten_to_value_bearing_clauses(self) -> None:
        group = self._group(self.BLOCKS)
        raw = self._proposal(group)
        original = deepcopy(raw)
        long_quote = raw["route_facts"][0]["excerpt"]
        _binding, reason = bind_pdf_quote(
            [(block.locator, block.text) for block in group.blocks], long_quote,
        )
        self.assertEqual(reason, "fact_excerpt_span_too_long")

        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(raw, original)
        self.assertEqual(issues, [])
        facts = self._facts(prepared)
        self.assertEqual(facts["f-time"]["excerpt"], self.TIME_CLAUSE)
        for fact_id in ("f-op", "f-name", "f-state", "f-qty"):
            self.assertEqual(facts[fact_id]["excerpt"], self.EVENT_CLAUSE)
        # The full original quotation stays on the fact for verification.
        for fact in prepared["route_facts"]:
            self.assertEqual(fact["verification_excerpt"], long_quote)
        # Approximation and attribution context survive inside the clause.
        self.assertIn("around 200 mL", facts["f-qty"]["excerpt"])
        self.assertIn("the suspension", facts["f-qty"]["excerpt"])
        # Values, units and the graph are never rewritten.
        for fact in prepared["route_facts"]:
            self.assertEqual(fact["value"], self._facts(raw)[fact["fact_id"]]["value"])
            self.assertEqual(fact["unit"], self._facts(raw)[fact["fact_id"]]["unit"])
        self.assertEqual(prepared["material_graph"], raw["material_graph"])
        self.assertEqual(len(audit), 5)
        for row in audit:
            self.assertEqual(row["version"], "route_pdf_clause_quote_tightening/v1")
            self.assertEqual(row["original_excerpt"], long_quote)
            self.assertEqual(row["reason_code"],
                             "unique_local_clause_source_subquote")
            self.assertTrue(row["produced_locator"])
        audit_by_fact = {row["fact_id"]: row for row in audit}
        self.assertEqual(set(audit_by_fact), {
            "f-time", "f-op", "f-name", "f-state", "f-qty",
        })

        # The tightened copy locates cleanly and keeps local attribution.
        located = produce_pdf_proposal_locators([group], [prepared])
        self.assertEqual(located.diagnostics, [])
        qty_excerpt = facts["f-qty"]["excerpt"]
        identity, identity_required = material_identity_for_amount_path(
            "material_graph[0].material_outputs[0].quantity.value",
            prepared["material_graph"], prepared["route_facts"],
        )
        self.assertEqual(identity, "suspension")
        self.assertTrue(identity_required)
        self.assertTrue(quantity_has_local_attribution(
            qty_excerpt, 200, "mL", identity=identity,
            identity_required=identity_required,
        ))

    def test_negation_over_value_blocks_clause_but_unnegated_fields_tighten(
        self,
    ) -> None:
        blocks = (
            "The feed was prepared in a beaker.",
            "After mixing, the suspension was not divided into 8",
            "parts, and the solid was filtered and",
            "washed again before the",
            "portions were collected.",
        )
        group = self._group(blocks)
        raw = self._proposal(group)
        original = deepcopy(raw)
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(raw, original)
        facts = self._facts(prepared)
        # The split claim sits under "not"; its clause is never used and the
        # fact stays pending with the untouched original quotation.
        self.assertEqual(facts["f-op"]["excerpt"],
                         original["route_facts"][1]["excerpt"])
        # The suspension itself is not negated, so name/state anchors may
        # still tighten to the full clause that carries the negation.
        negated_clause = "the suspension was not divided into 8 parts"
        self.assertEqual(facts["f-name"]["excerpt"], negated_clause)
        self.assertEqual(facts["f-state"]["excerpt"], negated_clause)
        self.assertEqual(
            [(issue["fact_id"], issue["reason_code"]) for issue in issues],
            [("f-op", "clause_subquote_drops_qualifying_context")],
        )
        self.assertEqual(
            {row["fact_id"] for row in audit}, {"f-name", "f-state"},
        )

    def test_negation_or_contrast_in_prior_clause_blocks_tightening(self) -> None:
        blocks = (
            "The feed was prepared in a beaker.",
            "Not the crude suspension, but the purified",
            "suspension was divided into 8 parts and",
            "the portions were",
            "collected separately.",
        )
        group = self._group(blocks)
        raw = self._proposal(group)
        original = deepcopy(raw)
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(raw, original)
        self.assertEqual(prepared["route_facts"], original["route_facts"])
        self.assertEqual(audit, [])
        self.assertEqual(
            {(issue["fact_id"], issue["reason_code"]) for issue in issues},
            {
                ("f-op", "clause_subquote_drops_qualifying_context"),
                ("f-name", "clause_subquote_drops_qualifying_context"),
                ("f-state", "clause_subquote_drops_qualifying_context"),
            },
        )

    def test_same_sentence_multiple_materials_keep_separate_clauses(self) -> None:
        blocks = (
            "The feed was prepared in a beaker.",
            "2 mL of solution A and",
            "5 mL of solution B were",
            "mixed in a flask and",
            "stirred briefly.",
        )
        group = self._group(blocks)
        scope = group.source_scope
        excerpt = " ".join(blocks)
        raw = {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [{
                "step_index": 0,
                "operation": "mixed",
                "material_inputs": [
                    {"name": "solution A", "quantity": {"value": 2, "unit": "mL"}},
                    {"name": "solution B", "quantity": {"value": 5, "unit": "mL"}},
                ],
            }],
            "route_facts": [
                {"fact_id": "qty-a",
                 "field_path": "material_graph[0].material_inputs[0].quantity.value",
                 "value": 2, "unit": "mL", "excerpt": excerpt,
                 "required": True},
                {"fact_id": "qty-b",
                 "field_path": "material_graph[0].material_inputs[1].quantity.value",
                 "value": 5, "unit": "mL", "excerpt": excerpt,
                 "required": True},
            ],
        }
        _binding, reason = bind_pdf_quote(
            [(block.locator, block.text) for block in group.blocks], excerpt,
        )
        self.assertEqual(reason, "fact_excerpt_span_too_long")
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(issues, [])
        facts = self._facts(prepared)
        self.assertEqual(facts["qty-a"]["excerpt"], "2 mL of solution A")
        self.assertEqual(facts["qty-b"]["excerpt"],
                         "5 mL of solution B were mixed in a flask")
        for fact_id, identity in (("qty-a", "solution A"), ("qty-b", "solution B")):
            self.assertTrue(quantity_has_local_attribution(
                facts[fact_id]["excerpt"], facts[fact_id]["value"],
                facts[fact_id]["unit"], identity=identity,
                identity_required=True,
            ))
        self.assertEqual(len(audit), 2)

    def test_duplicate_excerpts_tighten_independently(self) -> None:
        group = self._group(self.BLOCKS)
        raw = self._proposal(group)
        name_fact = self._facts(raw)["f-name"]
        state_fact = self._facts(raw)["f-state"]
        state_fact["excerpt"] = name_fact["excerpt"]
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(issues, [])
        facts = self._facts(prepared)
        self.assertEqual(facts["f-name"]["excerpt"], self.EVENT_CLAUSE)
        self.assertEqual(facts["f-state"]["excerpt"], self.EVENT_CLAUSE)
        rows = [row for row in audit if row["fact_id"] in {"f-name", "f-state"}]
        self.assertEqual(len(rows), 2)

    def test_two_uniquely_binding_clauses_stay_pending_as_ambiguous(self) -> None:
        blocks = (
            "The feed was prepared in a beaker.",
            "200 mL of water and",
            "200 mL of ethanol were",
            "combined in a flask and",
            "stirred briefly.",
        )
        group = self._group(blocks)
        scope = group.source_scope
        excerpt = " ".join(blocks)
        raw = {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [],
            "route_facts": [{
                "fact_id": "qty", "field_path": "material_graph[0]",
                "value": 200, "unit": "mL", "excerpt": excerpt,
                "required": True,
            }],
        }
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(prepared["route_facts"][0]["excerpt"], excerpt)
        self.assertEqual(audit, [])
        self.assertEqual(
            [issue["reason_code"] for issue in issues],
            ["clause_subquote_ambiguous_in_group"],
        )

    def test_later_clause_retraction_blocks_tightening(self) -> None:
        # Full meaning: the source affirms "dry solid" only to retract it in
        # the next clause, so no shorter quote may stand as its evidence.
        blocks = (
            "The feed was prepared in a beaker.",
            "The sample was a dry solid, an",
            "interpretation later ruled",
            "out by the control",
            "experiments.",
        )
        group = self._group(blocks)
        scope = group.source_scope
        excerpt = " ".join(blocks)
        raw = {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [],
            "route_facts": [{
                "fact_id": "state", "field_path": "material_graph[0]",
                "value": "dry solid", "unit": "", "excerpt": excerpt,
                "required": True,
            }],
        }
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        fact = prepared["route_facts"][0]
        self.assertEqual(fact["excerpt"], excerpt)
        self.assertNotIn("verification_excerpt", fact)
        self.assertEqual(audit, [])
        self.assertEqual(
            [issue["reason_code"] for issue in issues],
            ["clause_subquote_drops_qualifying_context"],
        )

    def test_estimate_rather_than_measurement_blocks_tightening(self) -> None:
        # Full meaning: 200 mL is an estimate, not a measurement; dropping
        # the qualifier would overstate the claim.
        blocks = (
            "The feed was prepared in a beaker.",
            "The suspension volume was 200 mL, as",
            "an estimate rather",
            "than a measurement",
            "by design.",
        )
        group = self._group(blocks)
        scope = group.source_scope
        excerpt = " ".join(blocks)
        raw = {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [],
            "route_facts": [{
                "fact_id": "qty", "field_path": "material_graph[0]",
                "value": 200, "unit": "mL", "excerpt": excerpt,
                "required": True,
            }],
        }
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        fact = prepared["route_facts"][0]
        self.assertEqual(fact["excerpt"], excerpt)
        self.assertNotIn("verification_excerpt", fact)
        self.assertEqual(audit, [])
        self.assertEqual(
            [issue["reason_code"] for issue in issues],
            ["clause_subquote_drops_qualifying_context"],
        )

    def test_qualifier_after_value_comma_blocks_tightening(self) -> None:
        # Full meaning: the qualifier sits after the value in the next
        # clause; the value-position scan alone would miss it.
        blocks = (
            "The feed was prepared in a beaker.",
            "The volume was 200 mL, although",
            "this was only a",
            "rough estimate",
            "at best.",
        )
        group = self._group(blocks)
        scope = group.source_scope
        excerpt = " ".join(blocks)
        raw = {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [],
            "route_facts": [{
                "fact_id": "qty", "field_path": "material_graph[0]",
                "value": 200, "unit": "mL", "excerpt": excerpt,
                "required": True,
            }],
        }
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        fact = prepared["route_facts"][0]
        self.assertEqual(fact["excerpt"], excerpt)
        self.assertNotIn("verification_excerpt", fact)
        self.assertEqual(audit, [])
        self.assertEqual(
            [issue["reason_code"] for issue in issues],
            ["clause_subquote_drops_qualifying_context"],
        )

    def test_sequencing_suffix_tightens_and_keeps_full_context(self) -> None:
        # Full meaning: the suffix only orders a later step; it does not
        # qualify the split claim, so trimming is safe and the full original
        # quotation must remain available for verification.
        blocks = (
            "The feed was prepared in a beaker.",
            "After 1 h the suspension was divided",
            "into 8 parts, followed",
            "by washing with",
            "water.",
        )
        group = self._group(blocks)
        raw = self._proposal(group)
        original = deepcopy(raw)
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(issues, [])
        facts = self._facts(prepared)
        self.assertEqual(
            facts["f-op"]["excerpt"],
            "After 1 h the suspension was divided into 8 parts",
        )
        for fact in prepared["route_facts"]:
            prior = self._facts(original)[fact["fact_id"]]
            self.assertEqual(
                fact.get("verification_excerpt", prior["excerpt"]),
                prior["excerpt"],
            )
        self.assertTrue(any(row["fact_id"] == "f-op" for row in audit))

    def test_value_absent_or_short_or_foreign_scope_quotes_are_untouched(self) -> None:
        group = self._group(self.BLOCKS)
        raw = self._proposal(group)
        facts = self._facts(raw)
        facts["f-time"]["value"] = "heated overnight"  # not in the excerpt
        facts["f-op"]["excerpt"] = self.EVENT_CLAUSE  # already short
        facts["f-name"]["value"] = "colloid"  # not in the excerpt
        facts["f-state"]["value"] = "gel"  # not in the excerpt
        facts["f-qty"]["value"] = 999  # not in the excerpt
        original = deepcopy(raw)
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(prepared, original)
        self.assertEqual((audit, issues), ([], []))
        foreign = self._proposal(self._group(self.BLOCKS, group_id="Other arm"))
        foreign_prepared, foreign_audit, foreign_issues = (
            tighten_unreviewed_clause_quotes(foreign, group)
        )
        self.assertEqual(foreign_prepared, foreign)
        self.assertEqual((foreign_audit, foreign_issues), ([], []))

    def test_clause_split_across_too_many_blocks_stays_pending(self) -> None:
        blocks = (
            "The feed was prepared in a beaker.",
            "around",
            "200",
            "mL of",
            "the suspension",
        )
        group = self._group(blocks)
        scope = group.source_scope
        excerpt = " ".join(blocks)
        raw = {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [],
            "route_facts": [{
                "fact_id": "qty", "field_path": "material_graph[0]",
                "value": 200, "unit": "mL", "excerpt": excerpt,
                "required": True,
            }],
        }
        prepared, audit, issues = tighten_unreviewed_clause_quotes(raw, group)
        self.assertEqual(prepared["route_facts"][0]["excerpt"], excerpt)
        self.assertEqual(audit, [])
        self.assertEqual(
            [issue["reason_code"] for issue in issues],
            ["clause_subquote_not_uniquely_bound"],
        )
        # The locator keeps reporting the budget problem honestly.
        located = produce_pdf_proposal_locators([group], [prepared])
        self.assertEqual(
            [d.reason_code for d in located.diagnostics],
            ["fact_excerpt_span_too_long"],
        )


if __name__ == "__main__":
    unittest.main()
