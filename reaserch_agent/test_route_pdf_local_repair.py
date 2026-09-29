"""Bounded, group-local repair of unreviewed PDF proposal mistakes."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_group_extraction import (
    PdfGroupExtractionBudgetV1,
    _build_prompt,
    propose_pdf_group_unreviewed,
)
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1


class PdfGroupLocalRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.digest = "sha256_" + sha256(b"fixed PDF for local repair").hexdigest()
        self.groups = [
            self._group("Group A", "pdf:p1:b1-p1:b1", "Group A: mix salt solution."),
            self._group("Group B", "pdf:p1:b2-p1:b2", "Group B: record XRD pattern."),
        ]

    def _group(self, name: str, locator: str, text: str) -> PdfExperimentalGroupV1:
        return PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper", experimental_group_id=name,
                section="Methods", locator=locator, source_digest=self.digest,
            ),
            source_document="/signed/fixed.pdf",
            blocks=(PdfSourceBlockV1(locator, text),),
        )

    def _reference(self, name: str) -> dict:
        return {
            "paper_id": "paper", "experimental_group_id": name,
            "source_digest": self.digest,
        }

    def _fact(self, fact_id: str, field_path: str, value, unit: str | None,
              excerpt: str) -> dict:
        return {
            "fact_id": fact_id, "field_path": field_path, "value": value,
            "unit": unit, "excerpt": excerpt, "required": True,
        }

    def _proposal_a(self, *, corrected: bool = False) -> dict:
        quote = "Group A: mix salt solution."
        material = "salt solution" if corrected else "product"
        return {
            "source_group_ref": self._reference("Group A"),
            "role_hint": "synthesis",
            "target": {"material": material},
            "route_facts": [
                self._fact("operation", "material_graph[0].operation",
                           "mix", "", quote),
                self._fact("material", "target.material", material, "", quote),
            ],
        }

    def _proposal_b(self) -> dict:
        return {
            "source_group_ref": self._reference("Group B"),
            "role_hint": "characterization",
            "route_facts": [],
        }

    def _run(self, repair: dict, *, initial_a: dict | None = None,
             groups: list[PdfExperimentalGroupV1] | None = None,
             **options):
        source_groups = groups if groups is not None else self.groups
        initial = {"proposals": [initial_a or self._proposal_a(), self._proposal_b()]}
        original = deepcopy(initial)
        prompts: list[str] = []

        def invoke(prompt: str) -> dict:
            prompts.append(prompt)
            if len(prompts) == 1:
                return initial
            if len(prompts) == 2:
                return {"proposals": [repair]}
            self.fail("a local repair must not trigger another model iteration")

        result = propose_pdf_group_unreviewed(source_groups, invoke, **options)
        self.assertEqual(initial, original, "the callback's raw output must be immutable")
        return result, prompts

    def test_repair_only_failing_group_and_preserve_unaffected_group(self) -> None:
        repaired = self._proposal_a(corrected=True)
        result, prompts = self._run(repaired)
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(prompts), 2)
        self.assertIn("Group A: mix salt solution.", prompts[1])
        self.assertNotIn("Group B: record XRD pattern.", prompts[1])
        self.assertEqual(
            [item["experimental_group_id"] for item in result.protocols],
            ["Group A", "Group B"],
        )
        self.assertEqual(result.protocols[0]["target"]["material"], "salt solution")
        self.assertEqual(result.protocols[0]["route_facts"][0]["value"], "mix")
        self.assertEqual(result.protocols[1]["role_hint"], "characterization")
        self.assertEqual(result.locator_production["located_proposals"][1],
                         self._proposal_b())

    def test_repair_rejects_wrong_group_reference_or_authority_field(self) -> None:
        variants = {}
        wrong_group = self._proposal_a(corrected=True)
        wrong_group["source_group_ref"] = self._reference("Group B")
        variants["wrong_group"] = wrong_group
        wrong_digest = self._proposal_a(corrected=True)
        wrong_digest["source_group_ref"]["source_digest"] = "sha256_" + "f" * 64
        variants["wrong_digest"] = wrong_digest
        authority_field = self._proposal_a(corrected=True)
        authority_field["group_role"] = "synthesis"
        variants["authority_field"] = authority_field
        expected = {
            "wrong_group": "local_revision_group_scope_changed",
            "wrong_digest": "local_revision_group_scope_changed",
            "authority_field": "local_revision_proposal_invalid",
        }
        for name, repaired in variants.items():
            with self.subTest(name=name):
                result, prompts = self._run(repaired)
                self.assertEqual(len(prompts), 2)
                self.assertEqual(result.protocols, [])
                self.assertTrue(result.diagnostics)
                self.assertEqual(
                    result.locator_production["local_revision"]["revisions"][0]
                    ["reason_code"], expected[name],
                )

    def test_repair_cannot_drop_or_demote_an_originally_required_fact(self) -> None:
        missing = self._proposal_a(corrected=True)
        missing["route_facts"] = missing["route_facts"][:1]
        demoted = self._proposal_a(corrected=True)
        demoted["route_facts"][1]["required"] = False
        passing_changed = self._proposal_a(corrected=True)
        passing_changed["route_facts"][0]["value"] = "stir"
        expected = {
            "missing": "local_revision_required_field_dropped",
            "demoted": "local_revision_required_field_dropped",
            "passing_changed": "local_revision_passing_fact_changed",
        }
        for name, repaired in (("missing", missing), ("demoted", demoted),
                               ("passing_changed", passing_changed)):
            with self.subTest(name=name):
                result, prompts = self._run(repaired)
                self.assertEqual(len(prompts), 2)
                self.assertEqual(result.protocols, [])
                self.assertTrue(result.diagnostics)
                self.assertEqual(
                    result.locator_production["local_revision"]["revisions"][0]
                    ["reason_code"], expected[name],
                )

    def test_repair_cannot_change_the_graph_claim_of_a_passing_fact(self) -> None:
        original = self._proposal_a()
        original["material_graph"] = [{"operation": "mix"}]
        revised = self._proposal_a(corrected=True)
        revised["material_graph"] = [{"operation": "stir"}]
        result, prompts = self._run(revised, initial_a=original)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.locator_production["local_revision"]["revisions"][0]
            ["reason_code"], "local_revision_unrelated_claim_changed",
        )

    def test_repair_cannot_shrink_graph_requirements_to_raise_pass_rate(self) -> None:
        original = self._proposal_a()
        original["material_graph"] = [{
            "operation": "mix", "material_inputs": [{
                "name": "salt solution", "state": "aqueous_solution",
            }],
        }]
        revised = self._proposal_a(corrected=True)
        revised["material_graph"] = [{"operation": "mix"}]
        result, _prompts = self._run(revised, initial_a=original)
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.locator_production["local_revision"]["revisions"][0]
            ["reason_code"], "local_revision_required_graph_path_dropped",
        )

    def test_unrelated_graph_step_cannot_be_appended(self) -> None:
        original = self._proposal_a()
        original["material_graph"] = [{
            "operation": "mix", "sample_id": "sample-a",
        }]
        revised = self._proposal_a(corrected=True)
        revised["material_graph"] = [
            {"operation": "mix", "sample_id": "sample-a"},
            {"operation": "heat", "sample_id": "sample-a"},
        ]
        revised["route_facts"].append(self._fact(
            "heat", "material_graph[1].operation", "heat", "",
            "Group A: mix salt solution.",
        ))
        result, _prompts = self._run(revised, initial_a=original)
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.locator_production["local_revision"]["revisions"][0]
            ["reason_code"], "local_revision_unrelated_claim_changed",
        )

    def test_failed_repair_is_not_retried_or_partially_returned(self) -> None:
        result, prompts = self._run(self._proposal_a())
        self.assertEqual(len(prompts), 2)
        self.assertEqual(result.protocols, [])
        self.assertTrue(result.diagnostics)
        self.assertTrue(result.locator_production["local_revision"]["final_issues"])

    def test_one_repair_call_per_failing_group_with_global_cap(self) -> None:
        group_b = self._group(
            "Group B", "pdf:p1:b2-p1:b2", "Group B: stir water.",
        )
        groups = [self.groups[0], group_b]
        initial_b = {
            "source_group_ref": self._reference("Group B"),
            "role_hint": "synthesis",
            "route_facts": [self._fact(
                "operation-b", "material_graph[0].operation", "mix", "",
                "Group B: stir water.",
            )],
        }
        repaired_b = deepcopy(initial_b)
        repaired_b["route_facts"][0]["value"] = "stir"

        def run_with_cap(cap: int):
            prompts: list[str] = []
            initial = {"proposals": [self._proposal_a(), initial_b]}

            def invoke(prompt: str) -> dict:
                prompts.append(prompt)
                if len(prompts) == 1:
                    return initial
                if "Group A: mix salt solution." in prompt:
                    return {"proposals": [self._proposal_a(corrected=True)]}
                if "Group B: stir water." in prompt:
                    return {"proposals": [repaired_b]}
                self.fail("repair prompt omitted its group's source text")

            return propose_pdf_group_unreviewed(
                groups, invoke, max_repair_groups=cap,
            ), prompts

        repaired, prompts = run_with_cap(2)
        self.assertEqual(repaired.diagnostics, [])
        self.assertEqual(len(prompts), 3)
        self.assertIn("Group A: mix salt solution.", prompts[1])
        self.assertNotIn("Group B: stir water.", prompts[1])
        self.assertIn("Group B: stir water.", prompts[2])
        self.assertNotIn("Group A: mix salt solution.", prompts[2])
        self.assertEqual(repaired.locator_production["local_revision"]
                         ["groups_merged"], 2)

        limited, prompts = run_with_cap(1)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(limited.protocols, [])
        self.assertEqual(limited.locator_production["local_revision"]
                         ["groups_budget_skipped"], 1)

    def test_repair_does_not_borrow_cross_group_or_repeated_quote(self) -> None:
        cross_group = self._proposal_a(corrected=True)
        cross_group["target"]["material"] = "XRD"
        cross_group["route_facts"][1].update({
            "value": "XRD", "excerpt": "Group B: record XRD pattern.",
        })
        result, prompts = self._run(cross_group)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(result.protocols, [])
        self.assertIn("fact_excerpt_not_in_block",
                      {item.reason_code for item in result.diagnostics})

        original = self.groups[0]
        repeated_a = PdfExperimentalGroupV1(
            source_scope=original.source_scope.model_copy(update={
                "locator": "pdf:p1:b1-p1:b3",
            }, deep=True),
            source_document=original.source_document,
            blocks=(
                original.blocks[0],
                PdfSourceBlockV1("pdf:p1:b3-p1:b3", "mix salt solution."),
            ),
        )
        repeated = self._proposal_a(corrected=True)
        repeated["route_facts"][1]["excerpt"] = "mix salt solution."
        result, prompts = self._run(repeated, groups=[repeated_a, self.groups[1]])
        self.assertEqual(len(prompts), 2)
        self.assertEqual(result.protocols, [])
        self.assertIn("fact_excerpt_ambiguous_in_group",
                      {item.reason_code for item in result.diagnostics})

    def test_literal_value_and_unit_failures_both_enter_local_repair(self) -> None:
        value_result, value_prompts = self._run(self._proposal_a(corrected=True))
        self.assertEqual(value_result.diagnostics, [])
        self.assertEqual(len(value_prompts), 2)
        self.assertIn("fact_value_not_in_excerpt", {
            item["reason_code"] for item in value_result.locator_production
            ["local_revision"]["initial_issues"]
        })

        quantity_group = self._group(
            "Group A", "pdf:p1:b1-p1:b1", "Group A: use 2 mmol salt solution.",
        )
        initial = {
            "source_group_ref": self._reference("Group A"),
            "role_hint": "synthesis",
            "route_facts": [self._fact(
                "amount", "target.quantity.value", 2, None,
                "Group A: use 2 mmol salt solution.",
            )],
        }
        repaired = deepcopy(initial)
        repaired["route_facts"][0]["unit"] = "mmol"
        unit_result, unit_prompts = self._run(
            repaired, initial_a=initial, groups=[quantity_group, self.groups[1]],
        )
        self.assertEqual(unit_result.diagnostics, [])
        self.assertEqual(len(unit_prompts), 2)
        self.assertEqual(unit_result.protocols[0]["route_facts"][0]["unit"], "mmol")
        self.assertIn("fact_unit_invalid", {
            item["reason_code"] for item in unit_result.locator_production
            ["local_revision"]["initial_issues"]
        })

    def test_initial_false_required_flag_is_repaired_or_blocked(self) -> None:
        initial = self._proposal_a(corrected=True)
        initial["route_facts"][1]["required"] = False
        corrected = self._proposal_a(corrected=True)

        repaired, prompts = self._run(corrected, initial_a=initial)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(repaired.diagnostics, [])
        self.assertTrue(repaired.protocols[0]["route_facts"][1]["required"])
        self.assertIn("fact_required_must_be_true", {
            item["reason_code"] for item in repaired.locator_production
            ["local_revision"]["initial_issues"]
        })

        unresolved, prompts = self._run(initial, initial_a=initial)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(unresolved.protocols, [])
        self.assertIn("fact_required_must_be_true",
                      {item.reason_code for item in unresolved.diagnostics})

    def test_repair_prompt_obeys_same_character_budget(self) -> None:
        # The initial request fits exactly; the revision also has to fit or
        # fail before the callback is invoked a second time.
        initial_prompt_size = len(_build_prompt(self.groups))
        budget = PdfGroupExtractionBudgetV1(max_prompt_chars=initial_prompt_size)
        result, prompts = self._run(
            self._proposal_a(corrected=True), budget=budget,
        )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.locator_production["local_revision"]["revisions"][0]
            ["reason_code"], "local_revision_prompt_char_budget_exceeded",
        )

    def test_strict_local_feedback_can_add_a_missing_graph_fact(self) -> None:
        quote = "Group A: mix salt solution."
        original = {
            "source_group_ref": self._reference("Group A"),
            "role_hint": "synthesis",
            "material_graph": [{
                "operation": "mix", "material_inputs": [{
                    "material_id": "salt", "name": "salt solution",
                    "state": "solution",
                }],
            }],
            "route_facts": [self._fact(
                "operation", "material_graph[0].operation", "mix", "", quote,
            )],
        }
        revised = deepcopy(original)
        revised["route_facts"].append(self._fact(
            "name", "material_graph[0].material_inputs[0].name",
            "salt solution", "", quote,
        ))
        # G1 requires a state fact for every port; the local feedback adds
        # the missing name claim AND the missing state claim.
        revised["route_facts"].append(self._fact(
            "state", "material_graph[0].material_inputs[0].state",
            "solution", "", quote,
        ))
        result, prompts = self._run(
            revised, initial_a=original, check_required_graph_facts=True,
        )
        self.assertEqual(len(prompts), 2)
        self.assertIn("required_graph_fact_missing", prompts[1])
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(result.protocols), 2)
        self.assertEqual(
            result.locator_production["local_revision"]["coverage_after"][0]
            ["missing_graph_required_paths"], [],
        )

    def test_strict_feedback_blocks_an_unmapped_target_fact_without_retry(self) -> None:
        original = self._proposal_a()
        result, prompts = self._run(
            self._proposal_a(corrected=True), initial_a=original,
            check_required_graph_facts=True,
        )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result.protocols, [])
        self.assertIn("fact_graph_path_missing", {
            item.reason_code for item in result.diagnostics
        })
        self.assertEqual(
            result.locator_production["local_revision"]["revisions"][0]
            ["reason_code"], "local_revision_original_graph_path_invalid",
        )


if __name__ == "__main__":
    unittest.main()
