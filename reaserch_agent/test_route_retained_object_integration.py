"""R4 integration: the formal G1 path trusts only LIVE retained-object records.

A fake signed ``PdfExperimentalGroupV1`` carries a composite operation
("centrifugation−redispersion protocol") and a following naming sentence
("The precipitates were labeled as LDH seeds ...").  Every wired stage —
the G1 association, the literal receipt, and the group compiler — must
rebuild its retained-object resolver LIVE from the signed group blocks
and derive graph[0].out (CENTRIFUGE_COLLECT_PRECIPITATE_V1) plus the
downstream input inheritance (PARENT_OUTPUT_STATE_INHERITANCE_V1).

A variant proposal whose facts no longer contain the naming sentence,
and which smuggles a perfectly shaped fabricated record in an injected
``retained_object_records`` field, must derive NOTHING: stored records
have zero authority at every layer.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from chem_agent_contracts.route_retained_object import (
    POST_OPERATION_RETAINED_OBJECT_RULE_ID,
    POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
    POST_OPERATION_RETAINED_OBJECT_SCHEMA,
    build_retained_object_resolver,
)
from reaserch_agent.route_group_compiler import compile_experimental_group_protocols
from reaserch_agent.route_group_fact_receipt import produce_pdf_group_fact_receipt
from reaserch_agent.route_pdf_group_extraction import propose_pdf_group_unreviewed
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfSourceBlockV1,
)
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote

DIGEST = "sha256_" + sha256(b"signed R4 integration PDF bytes").hexdigest()
SCOPE = {
    "paper_id": "paper-r4",
    "experimental_group_id": "Group R4",
    "source_digest": DIGEST,
}

B1 = ("A suspension of NiFe hydroxide was obtained by coprecipitation "
      "and aged for 12 h.")
B2 = ("The solid was isolated by a centrifugation−redispersion protocol "
      "using deionized water three times.")
B3 = "The precipitates were labeled as LDH seeds, which were kept wet."
B4 = "The LDH seeds were redispersed in water."
OP_VALUE = "centrifugation−redispersion protocol"
OP_EXCERPT = (
    "centrifugation−redispersion protocol using deionized water three times"
)
OUT_STATE = "material_graph[0].material_outputs[0].state"
IN_STATE = "material_graph[1].material_inputs[0].state"


def _group() -> PdfExperimentalGroupV1:
    return PdfExperimentalGroupV1(
        source_scope=ExperimentalGroupScopeV1(
            paper_id=SCOPE["paper_id"],
            experimental_group_id=SCOPE["experimental_group_id"],
            section="Methods", locator="pdf:p1:b1-p1:b4",
            source_digest=SCOPE["source_digest"],
        ),
        source_document="/controlled/paper-r4.pdf",
        blocks=tuple(
            PdfSourceBlockV1(f"pdf:p1:b{index}-p1:b{index}", text)
            for index, text in enumerate((B1, B2, B3, B4), start=1)
        ),
    )


def _state_step(step_id, sequence, operation, input_port, output_port, ref):
    return {
        "macro_step_id": step_id, "macro_action_id": "A1",
        "sequence": sequence, "operation": operation, "sample_id": "sample-R4",
        "provenance": {"kind": "paper", "reference": ref},
        "material_inputs": [input_port],
        "material_outputs": [output_port],
        "operation_segments": [{
            "segment_id": f"{step_id}-seg1",
            "material_effect": "transform_material",
            "source_operation_ref": step_id,
            "provenance": {"kind": "paper", "reference": ref},
        }],
        "material_relations": [{
            "relation_id": f"{step_id}-rel1", "event_kind": "state_change",
            "input_material_instance_ids": [input_port["material_instance_id"]],
            "output_material_instance_ids": [output_port["material_instance_id"]],
            "quantity_basis": "whole_batch",
            "source_operation_ref": f"{step_id}-seg1",
            "provenance": {"kind": "paper", "reference": ref},
        }],
        "lineage_relation": {
            "relation_type": "state_change_of",
            "parent_material_instance_ids": [input_port["material_instance_id"]],
            "child_material_instance_ids": [output_port["material_instance_id"]],
        },
    }


def _fact(fact_id, path, value, excerpt):
    return {
        "fact_id": fact_id, "field_path": path, "value": value,
        "unit": "", "required": True, "excerpt": excerpt,
    }


def _proposal(*, naming_excerpt: str = B3) -> dict:
    step0 = _state_step(
        "S1", 1, OP_VALUE,
        {"material_id": "product", "material_instance_id": "inst_a",
         "name": "suspension", "state": "suspension",
         "material_origin": "external_inventory",
         "provenance": {"kind": "paper", "reference": "fact:f_in_name"}},
        {"material_id": "product", "material_instance_id": "inst_b",
         "name": "LDH seeds", "state": "retained_wet_solid",
         "provenance": {"kind": "paper", "reference": "fact:f_out_name"}},
        "fact:f_op",
    )
    step1 = _state_step(
        "S2", 2, "redispersed",
        {"material_id": "product", "material_instance_id": "inst_b",
         "name": "LDH seeds", "state": "retained_wet_solid",
         "material_origin": "upstream_output",
         "parent_output_refs": [{"macro_step_id": "S1",
                                 "material_instance_id": "inst_b"}],
         "provenance": {"kind": "paper", "reference": "fact:f_in1_state"}},
        {"material_id": "product", "material_instance_id": "inst_c",
         "name": "LDH seeds", "state": "suspension",
         "provenance": {"kind": "paper", "reference": "fact:f_op2"}},
        "fact:f_op2",
    )
    return {
        "source_group_ref": dict(SCOPE),
        "role_hint": "synthesis",
        "material_graph": [step0, step1],
        "route_facts": [
            _fact("f_op", "material_graph[0].operation", OP_VALUE, OP_EXCERPT),
            _fact("f_in_name", "material_graph[0].material_inputs[0].name",
                  "suspension", B1),
            _fact("f_in_state", "material_graph[0].material_inputs[0].state",
                  "suspension", B1),
            _fact("f_out_name", "material_graph[0].material_outputs[0].name",
                  "LDH seeds", naming_excerpt),
            _fact("f_out_state", OUT_STATE, "retained_wet_solid", OP_EXCERPT),
            _fact("f_op2", "material_graph[1].operation", "redispersed", B4),
            _fact("f_in1_state", IN_STATE, "retained_wet_solid", B4),
            _fact("f_out1_name", "material_graph[1].material_outputs[0].name",
                  "LDH seeds", B4),
            _fact("f_out1_state", "material_graph[1].material_outputs[0].state",
                  "suspension", B4),
        ],
    }


def _fabricated_record() -> dict:
    """A perfectly shaped record for OUT_STATE, forged end to end."""
    return {
        "schema_version": POST_OPERATION_RETAINED_OBJECT_SCHEMA,
        "rule_id": POST_OPERATION_RETAINED_OBJECT_RULE_ID,
        "rule_version": POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
        "step_index": 0,
        "macro_step_id": "S1",
        "operation_fact_id": "f_op",
        "operation_locator": "pdf:p1:b2-p1:b2",
        "operation_char_span": [104, 174],
        "output": {
            "state_path": OUT_STATE,
            "name_path": "material_graph[0].material_outputs[0].name",
            "output_index": 0,
            "material_instance_id": "inst_b",
            "material_id": "product",
            "label": "LDH seeds",
        },
        "retained_object_surface": "precipitates",
        "retained_object": "precipitate",
        "object_modifiers": [],
        "naming_fact_id": "f_out_name",
        "output_name_fact_id": "f_out_name",
        "naming_excerpt": B3,
        "naming_locator": "pdf:p1:b3-p1:b3",
        "naming_char_span": [176, 240],
        "operation_span": [1, 1],
        "naming_span": [2, 2],
    }


def _protocol(group: PdfExperimentalGroupV1, proposal: dict) -> dict:
    """Receipt/compiler protocol shape: per-fact sources resolved to blocks."""
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    scope = group.source_scope
    facts = []
    for raw in proposal["route_facts"]:
        binding, issue = bind_pdf_quote(
            blocks, raw["excerpt"], caption_block_locators=captions,
        )
        assert binding is not None, (raw["fact_id"], issue)
        fact = dict(raw)
        fact["source"] = {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "section": scope.section,
            "locator": binding.locator,
            "source_digest": scope.source_digest,
        }
        facts.append(fact)
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
        "material_graph": deepcopy(proposal["material_graph"]),
        "route_facts": facts,
    }


class RetainedObjectG1IntegrationTest(unittest.TestCase):
    def _associate(self, proposal: dict):
        envelope = {"proposals": [proposal]}
        return propose_pdf_group_unreviewed(
            [self.group], lambda _prompt: envelope, max_repair_groups=0,
        )

    def setUp(self) -> None:
        self.group = _group()

    def test_g1_association_derives_output_and_inheritance(self) -> None:
        result = self._associate(_proposal())
        production = result.locator_production or {}
        candidates = {
            row["field_path"]: row["proof"]["rule_id"]
            for row in production.get("convention_state_candidates") or ()
        }
        self.assertEqual(
            candidates.get(OUT_STATE), "CENTRIFUGE_COLLECT_PRECIPITATE_V1",
        )
        self.assertEqual(
            candidates.get(IN_STATE), "PARENT_OUTPUT_STATE_INHERITANCE_V1",
        )
        final_issues = (production.get("local_revision") or {}).get(
            "final_issues") or []
        flagged = {
            (item["fact_id"], item["reason_code"]) for item in final_issues
        }
        # The derived facts are not pending, and the redispersion output the
        # flat engine could not prove is no longer flagged either: the local
        # diagnostics now consume the same live dual-verified DAG map as the
        # receipt (REDISPERSION_V1 over the proven S1 chain), matching the
        # receipt-side dag_proven classification of this same path.
        self.assertNotIn(("f_out_state", "semantic_binding_pending"), flagged)
        self.assertNotIn(("f_in1_state", "semantic_binding_pending"), flagged)
        self.assertNotIn(("f_out1_state", "semantic_binding_pending"), flagged)

    def test_g1_smuggled_fabricated_record_is_ignored(self) -> None:
        proposal = _proposal(naming_excerpt=B4)
        proposal["retained_object_records"] = [_fabricated_record()]
        result = self._associate(proposal)
        production = result.locator_production or {}
        candidates = {
            row["field_path"]: row["proof"]["rule_id"]
            for row in production.get("convention_state_candidates") or ()
        }
        # The naming sentence is gone from the facts; the injected record
        # must not make the output (or the downstream inheritance) derive.
        self.assertNotIn(OUT_STATE, candidates)
        self.assertNotIn(IN_STATE, candidates)
        final_issues = (production.get("local_revision") or {}).get(
            "final_issues") or []
        flagged = {
            (item["fact_id"], item["reason_code"]) for item in final_issues
        }
        self.assertIn(("f_out_state", "semantic_binding_pending"), flagged)
        self.assertIn(("f_in1_state", "semantic_binding_pending"), flagged)

    def test_receipt_derives_output_and_inheritance(self) -> None:
        proposal = _proposal()
        receipt = produce_pdf_group_fact_receipt(
            [self.group], [_protocol(self.group, proposal)],
            signed_inventory_verified=True,
        )
        result = receipt.group_results[0]
        self.assertEqual(result.derived_state_field_paths, (OUT_STATE, IN_STATE))
        self.assertIn(
            "material_graph[0].material_inputs[0].state",
            result.verified_field_paths,
        )
        # G1 (state-proof-dag/v1 at the receipt): the redispersion output
        # that the flat engine honestly could not prove is now DAG-proven —
        # REDISPERSION_V1 composed over the proven S1 chain, rebuilt and
        # dual-verified from the live signed blocks — so nothing stays
        # pending.  Pre-G1 this asserted fact[8]:semantic_binding_pending.
        self.assertEqual(
            result.dag_proven_state_field_paths,
            ("material_graph[1].material_outputs[0].state",),
        )
        self.assertEqual(result.reason_codes, ())

    def test_receipt_ignores_smuggled_fabricated_record(self) -> None:
        proposal = _proposal(naming_excerpt=B4)
        proposal["retained_object_records"] = [_fabricated_record()]
        receipt = produce_pdf_group_fact_receipt(
            [self.group], [_protocol(self.group, proposal)],
            signed_inventory_verified=True,
        )
        result = receipt.group_results[0]
        self.assertEqual(result.derived_state_field_paths, ())
        self.assertIn("fact[4]:semantic_binding_pending", result.reason_codes)


class RetainedObjectCompilerIntegrationTest(unittest.TestCase):
    """Compiler-level: live resolver derives; default stays fail-closed."""

    def setUp(self) -> None:
        self.group = _group()
        # The compiler's per-step material identity rule wants one
        # distinguishing port name per material id, so the single-step
        # compiler fixture names both ports "LDH seeds".
        step0 = _state_step(
            "S1", 1, OP_VALUE,
            {"material_id": "product", "material_instance_id": "inst_a",
             "name": "LDH seeds", "state": "suspension",
             "material_origin": "external_inventory",
             "provenance": {"kind": "paper", "reference": "fact:f_in_name"}},
            {"material_id": "product", "material_instance_id": "inst_b",
             "name": "LDH seeds", "state": "retained_wet_solid",
             "provenance": {"kind": "paper", "reference": "fact:f_out_name"}},
            "fact:f_op",
        )
        self.step = step0
        blocks = [(block.locator, block.text) for block in self.group.blocks]
        captions = [
            block.locator for block in self.group.blocks if block.caption
        ]
        scope = self.group.source_scope
        facts = [
            _fact("f_op", "material_graph[0].operation", OP_VALUE, OP_EXCERPT),
            _fact("f_in_name", "material_graph[0].material_inputs[0].name",
                  "LDH seeds", B1.replace("A suspension", "The LDH seeds suspension")),
            _fact("f_in_state", "material_graph[0].material_inputs[0].state",
                  "suspension", B1.replace("A suspension", "The LDH seeds suspension")),
            _fact("f_out_name", "material_graph[0].material_outputs[0].name",
                  "LDH seeds", B3),
            _fact("f_out_state", OUT_STATE, "retained_wet_solid", OP_EXCERPT),
        ]
        renamed_group = PdfExperimentalGroupV1(
            source_scope=self.group.source_scope,
            source_document=self.group.source_document,
            blocks=(
                PdfSourceBlockV1(
                    "pdf:p1:b1-p1:b1",
                    B1.replace("A suspension", "The LDH seeds suspension"),
                ),
                *self.group.blocks[1:],
            ),
        )
        self.group = renamed_group
        blocks = [(block.locator, block.text) for block in self.group.blocks]
        located_facts = []
        for raw in facts:
            binding, issue = bind_pdf_quote(
                blocks, raw["excerpt"], caption_block_locators=captions,
            )
            assert binding is not None, (raw["fact_id"], issue)
            fact = dict(raw)
            fact["source"] = {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "section": scope.section,
                "locator": binding.locator,
                "source_digest": scope.source_digest,
            }
            located_facts.append(fact)
        located_facts.append({
            "fact_id": "f_sig_op0", "field_path": "route_signature.operations[0]",
            "value": OP_VALUE, "unit": "", "required": True,
            "excerpt": OP_EXCERPT,
            "source": deepcopy(located_facts[0]["source"]),
        })
        self.protocol = {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "group_role": "unclassified",
            "role_hint": "synthesis",
            "target": {"product": "LDH seeds"},
            "required_capabilities": ["centrifugation"],
            "route_signature": {"operations": [OP_VALUE]},
            "source": {
                "source_document": self.group.source_document,
                "section": scope.section,
                "locator": scope.locator,
                "source_digest": scope.source_digest,
            },
            "material_graph": [self.step],
            "route_facts": located_facts,
        }
        self.resolver = build_retained_object_resolver(
            self.protocol["material_graph"], self.protocol["route_facts"],
            blocks, captions,
        )
        self.key = (
            scope.paper_id, scope.experimental_group_id, scope.source_digest,
        )

    def test_compiler_derives_with_live_resolver(self) -> None:
        compiled = compile_experimental_group_protocols(
            [self.protocol],
            retained_object_resolvers={self.key: self.resolver},
        )
        self.assertEqual(list(compiled.diagnostics), [])
        matrix = compiled.protocols[0]["evidence_matrix"]
        derived = {
            field["field_path"]: json.loads(field["provenance"]["derivation"])
            for field in matrix
            if (field.get("provenance") or {}).get("derivation")
        }
        proof = derived.get(OUT_STATE)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["rule_id"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1")
        self.assertEqual(
            proof["retained_object_rule_id"],
            POST_OPERATION_RETAINED_OBJECT_RULE_ID,
        )
        self.assertEqual(proof["retained_object"], "precipitate")
        self.assertEqual(proof["retained_object_output_name_fact_id"], "f_out_name")
        self.assertEqual(proof["retained_object_operation_locator"],
                         "pdf:p1:b2-p1:b2")
        self.assertEqual(proof["retained_object_naming_locator"],
                         "pdf:p1:b3-p1:b3")
        self.assertEqual(proof["retained_object_operation_span"], "[1,1]")
        self.assertEqual(proof["retained_object_naming_span"], "[2,2]")
        self.assertTrue(proof["retained_object_operation_char_span"])
        self.assertTrue(proof["retained_object_naming_char_span"])

    def test_compiler_without_resolver_stays_fail_closed(self) -> None:
        compiled = compile_experimental_group_protocols([self.protocol])
        self.assertEqual(
            [diagnostic.reason_code for diagnostic in compiled.diagnostics],
            ["semantic_binding_pending"],
        )
        self.assertNotIn("evidence_matrix", compiled.protocols[0])

    def test_compiler_fabricated_resolver_record_is_rejected(self) -> None:
        # A resolver that yields a fabricated record (a naming excerpt that
        # is not the fact's excerpt) is rejected at the contract cross-check;
        # the compiler surfaces the honest pending issue.
        fabricated = _fabricated_record()
        fabricated["naming_excerpt"] = (
            "The precipitates were labeled as LDH seeds."
        )

        def fabricated_resolver(_field_path: str):
            return fabricated, ""

        compiled = compile_experimental_group_protocols(
            [self.protocol],
            retained_object_resolvers={self.key: fabricated_resolver},
        )
        self.assertEqual(
            [diagnostic.reason_code for diagnostic in compiled.diagnostics],
            ["semantic_binding_pending"],
        )


if __name__ == "__main__":
    unittest.main()
