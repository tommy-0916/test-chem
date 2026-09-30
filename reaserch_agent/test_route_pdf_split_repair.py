"""Regression: an arm-style structural revision must not break the verified
split representation.

Real regression (2026-09-29): a group-level structural revision fixed the
sample arms but collapsed the source split into ONE output port carrying
the part count as a material quantity (quantity = n parts).  The typed
structure constructor cannot expand that into n children, and the quantity
role changed.  These tests lock the semantic invariants: after the
deterministic repair, the source operation still represents the split, the
children cardinality equals the verified source count, the count never
lives in an output quantity, and every removal is audited.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_group_extraction import (
    construct_unreviewed_split_transfer_structure,
)
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfSourceBlockV1,
)
from reaserch_agent.route_pdf_split_repair import (
    repair_split_event_representation,
    split_count_from_operation,
)


def _group(phrase: str = "divided into 3 parts") -> PdfExperimentalGroupV1:
    quote = f"In this preparation, the suspension was {phrase} for washing."
    digest = "sha256_" + sha256(b"paper-split\0Group P\0" + quote.encode()).hexdigest()
    return PdfExperimentalGroupV1(
        source_scope=ExperimentalGroupScopeV1(
            paper_id="paper-split", experimental_group_id="Group P",
            section="Methods", locator="pdf:p2:b12-p2:b12",
            source_digest=digest,
        ),
        source_document="/attested/other-paper.pdf",
        blocks=(PdfSourceBlockV1("pdf:p2:b12-p2:b12", quote),),
    )


def _proposal_with_collapsed_split() -> dict:
    quote = ("In this preparation, the suspension was divided into 3 parts "
             "for washing.")
    digest = "sha256_" + sha256(
        b"paper-split\0Group P\0" + quote.encode()).hexdigest()
    return {
        "source_group_ref": {
            "paper_id": "paper-split", "experimental_group_id": "Group P",
            "source_digest": digest,
        },
        "role_hint": "synthesis",
        "material_graph": [
            {
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "divided into 3 parts", "sample_id": "arm-1",
                "provenance": {"kind": "paper", "reference": "fact:op"},
                "material_inputs": [{
                    "material_id": "mat_x", "material_instance_id": "inst_parent",
                    "name": "suspension", "state": "suspension",
                    "provenance": {"kind": "paper", "reference": "fact:pin"},
                }],
                "material_intermediates": [],
                "material_outputs": [{
                    "material_id": "mat_x", "material_instance_id": "inst_parts",
                    "name": "suspension", "state": "suspension",
                    "quantity": {"value": 3, "unit": "parts"},
                    "provenance": {"kind": "paper", "reference": "fact:outn"},
                }],
            },
            {
                "macro_step_id": "S2", "macro_action_id": "A2", "sequence": 2,
                "operation": "washed", "sample_id": "arm-1",
                "material_inputs": [{
                    "material_id": "mat_x", "material_instance_id": "inst_parts",
                    "name": "suspension", "state": "suspension",
                    "provenance": {"kind": "paper", "reference": "fact:washin"},
                }],
                "material_intermediates": [],
                "material_outputs": [{
                    "material_id": "mat_x", "material_instance_id": "inst_washed",
                    "name": "suspension", "state": "suspension",
                    "provenance": {"kind": "paper", "reference": "fact:washout"},
                }],
            },
        ],
        "route_facts": [
            {"fact_id": "f_op", "field_path": "material_graph[0].operation",
             "value": "divided into 3 parts", "unit": "",
             "excerpt": "the suspension was divided into 3 parts for washing",
             "required": True},
            {"fact_id": "f_pin", "field_path": "material_graph[0].material_inputs[0].name",
             "value": "suspension", "unit": "",
             "excerpt": "the suspension was divided into 3 parts for washing",
             "required": True},
            {"fact_id": "f_pstate",
             "field_path": "material_graph[0].material_inputs[0].state",
             "value": "suspension", "unit": "",
             "excerpt": "the suspension was divided into 3 parts for washing",
             "required": True},
            {"fact_id": "f_outn",
             "field_path": "material_graph[0].material_outputs[0].name",
             "value": "suspension", "unit": "",
             "excerpt": "the suspension was divided into 3 parts for washing",
             "required": True},
            {"fact_id": "f_qty",
             "field_path": "material_graph[0].material_outputs[0].quantity.value",
             "value": 3, "unit": "parts",
             "excerpt": "the suspension was divided into 3 parts for washing",
             "required": True},
        ],
    }


class SplitRepairTests(unittest.TestCase):
    def test_split_count_comes_from_source_operation(self):
        self.assertEqual(split_count_from_operation("divided into 8 parts"), 8)
        self.assertEqual(split_count_from_operation("was divided into 12 parts"), 12)
        self.assertIsNone(split_count_from_operation("aged for 20 h"))

    def test_collapsed_split_is_repaired_and_audited(self):
        proposal = _proposal_with_collapsed_split()
        repaired, audit, unresolved = repair_split_event_representation(proposal)
        revocations = [r for r in audit
                       if r["kind"] == "split_count_quantity_revoked"]
        self.assertEqual(len(revocations), 1)
        self.assertEqual(revocations[0]["part_count"], 3)
        self.assertEqual(revocations[0]["removed_port"]["quantity"],
                         {"value": 3, "unit": "parts"})
        self.assertTrue(any(r["kind"] == "split_count_fact_revoked"
                            and r["field_path"].endswith("quantity.value")
                            for r in audit))
        # The wrong port and its fact are gone from the live graph.
        step0 = repaired["material_graph"][0]
        self.assertEqual(step0["material_outputs"], [])
        fact_paths = {f["field_path"] for f in repaired["route_facts"]}
        self.assertNotIn("material_graph[0].material_outputs[0].quantity.value",
                         fact_paths)
        # The downstream reference to the revoked port is WITHDRAWN, not
        # re-pointed to the split parent: a whole-batch split leaves the
        # children as the only post-split material, and re-consuming the
        # parent would double-book it against them.
        step1_in = repaired["material_graph"][1]["material_inputs"][0]
        self.assertIsNone(step1_in["material_instance_id"])
        self.assertFalse(any(r["kind"] == "downstream_repointed_to_split_parent"
                             for r in audit))
        withdrawn = [r for r in audit
                     if r["kind"] == "downstream_reference_withdrawn"]
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(withdrawn[0]["from_instance"], "inst_parts")
        self.assertEqual(withdrawn[0]["split_parent_instance"], "inst_parent")
        self.assertEqual(len(withdrawn[0]["candidate_child_instances"]), 3)
        self.assertTrue(any("revoked split port instance" in r["reason"]
                            for r in unresolved))
        # The parent instance appears downstream of the split NOWHERE —
        # not in ports, not in relations.
        for later in repaired["material_graph"][1:]:
            for kind in ("material_inputs", "material_intermediates",
                         "material_outputs"):
                for port in later.get(kind, []) or []:
                    self.assertNotEqual(port.get("material_instance_id"),
                                        "inst_parent")
        # The original proposal is untouched.
        self.assertEqual(proposal["material_graph"][0]["material_outputs"][0]
                         ["quantity"], {"value": 3, "unit": "parts"})

    def test_withdrawn_reference_names_constructor_children(self):
        """The unresolved row's candidate set is the constructor's child set.

        Determinism matters: the review must be able to check the actual
        child instances the typed constructor generates, not a fresh guess.
        """
        proposal = _proposal_with_collapsed_split()
        repaired, audit, _u = repair_split_event_representation(proposal)
        structured, _rows, issues = construct_unreviewed_split_transfer_structure(
            repaired, _group())
        self.assertEqual(issues, [])
        built = structured["material_graph"][0]["material_outputs"]
        built_ids = sorted(p["material_instance_id"] for p in built)
        withdrawn = [r for r in audit
                     if r["kind"] == "downstream_reference_withdrawn"]
        self.assertEqual(sorted(withdrawn[0]["candidate_child_instances"]),
                         built_ids)
        # and none of the built children is the revoked local symbol
        self.assertNotIn("inst_parts", built_ids)

    def test_constructor_rebuilds_children_in_current_scope(self):
        proposal = _proposal_with_collapsed_split()
        repaired, _audit, _unresolved = repair_split_event_representation(proposal)
        structured, rows, issues = construct_unreviewed_split_transfer_structure(
            repaired, _group())
        self.assertEqual(issues, [])
        step0 = structured["material_graph"][0]
        children = step0["material_outputs"]
        self.assertEqual(len(children), 3)
        self.assertTrue(all(child["material_instance_id"] != "inst_parts"
                            for child in children))
        self.assertTrue(all("quantity" not in child for child in children))
        relations = step0["material_relations"]
        self.assertEqual(len(relations), 1)
        self.assertEqual(len(relations[0]["output_material_instance_ids"]), 3)
        self.assertEqual(
            relations[0]["input_material_instance_ids"], ["inst_parent"])

    def test_semantic_invariants_after_arm_style_revision(self):
        """The regression this slice fixes: fixing arms must keep the split."""
        proposal = _proposal_with_collapsed_split()
        repaired, audit, _u = repair_split_event_representation(proposal)
        structured, _rows, issues = construct_unreviewed_split_transfer_structure(
            repaired, _group())
        self.assertEqual(issues, [])
        # operation still represented
        self.assertEqual(structured["material_graph"][0]["operation"],
                         "divided into 3 parts")
        # children cardinality equals the verified source count
        self.assertEqual(len(structured["material_graph"][0]["material_outputs"]),
                         split_count_from_operation("divided into 3 parts"))
        # no count migration into an output quantity anywhere
        for step in structured["material_graph"]:
            for kind in ("material_inputs", "material_intermediates",
                         "material_outputs"):
                for port in step.get(kind, []) or []:
                    unit = (port.get("quantity") or {}).get("unit", "")
                    self.assertNotIn(str(unit).casefold(),
                                     {"part", "parts", "portion", "portions",
                                      "fraction", "fractions", "aliquot",
                                      "aliquots"})
        # every removal is auditable
        self.assertTrue(any(r["kind"] == "split_count_quantity_revoked"
                            for r in audit))


if __name__ == "__main__":
    unittest.main()
