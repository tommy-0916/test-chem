"""Post-operation retained-object source relation and composite arbitration.

The composite operation "centrifugation−redispersion protocol" may not be
collapsed into one rule's endpoint (round-6 hardening).  A post-operation
naming sentence ("The precipitates were labeled as LDH seeds") can,
however, bind the retained object to the step's output label as a SOURCE
relation (post-operation-retained-object/v1); a rule that declares
``accept_post_operation_retained_object`` may then consume a validated
record, and the composite guard arbitrates to record-consistent rules
only.  Conflicts stay pending, never an override.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_convention_basis import (
    _evidence_id,
    _resolve_fact_provenance,
    convention_fact_evidence_by_id,
    derive_unreviewed_input_state,
    derive_unreviewed_output_state,
    verify_bound_output_state,
)
from chem_agent_contracts.route_retained_object import (
    POST_OPERATION_RETAINED_OBJECT_RULE_ID,
    POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
    POST_OPERATION_RETAINED_OBJECT_SCHEMA,
    build_excerpt_span_resolver,
    derive_post_operation_retained_object,
)

OUT_STATE = "material_graph[0].material_outputs[0].state"
OUT_NAME = "material_graph[0].material_outputs[0].name"
STEP1_INPUT_STATE = "material_graph[1].material_inputs[0].state"

OP_VALUE = "centrifugation−redispersion protocol"
OP_SENTENCE = (
    "followed by a centrifugation−redispersion protocol using deionized "
    "water three times, which was the first centrifugation−redispersion "
    "protocol."
)
OP_EXCERPT = (
    "by a centrifugation−redispersion protocol using deionized water three "
    "times, which was the first centrifugation−redispersion protocol"
)
NAMING_SENTENCE = (
    "The precipitates were labeled as LDH seeds, which were dispersed in "
    "30 mL of water."
)
FILLER = "The suspension was divided into 8 parts."


def _blocks(*texts: str) -> list[tuple[str, str]]:
    return [
        (f"pdf:p1:b{index}-p1:b{index}", text)
        for index, text in enumerate(texts, start=1)
    ]


def _resolver(mapping: dict):
    """field_path -> (record|None, issue) from a {path: (record, issue)} map."""

    def resolve(field_path: str):
        record, issue = mapping.get(field_path, (None, ""))
        return record, issue

    return resolve


class PostOperationRetainedObjectTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_file = self.root / "paper.md"
        self.source_file.write_text("# Study\n## Methods\n### Group A\nplaceholder\n",
                                    encoding="utf-8")
        self.digest = "sha256_" + sha256(self.source_file.read_bytes()).hexdigest()
        self.source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:4-4",
            "source_digest": self.digest,
        }

    def _fact(self, fact_id: str, path: str, value: str, excerpt: str) -> dict:
        return {
            "fact_id": fact_id, "field_path": path, "value": value,
            "unit": "", "required": True, "excerpt": excerpt,
            "source": deepcopy(self.source),
        }

    @staticmethod
    def _state_change_step(
        *, step_id: str, sequence: int, operation: str,
        input_port: dict, output_port: dict, relation_provenance_ref: str,
    ) -> dict:
        return {
            "macro_step_id": step_id, "macro_action_id": "A1",
            "sequence": sequence, "operation": operation, "sample_id": "sample-A",
            "provenance": {"kind": "paper", "reference": relation_provenance_ref},
            "material_inputs": [input_port],
            "material_outputs": [output_port],
            "operation_segments": [{
                "segment_id": f"{step_id}-seg1", "material_effect": "transform_material",
                "source_operation_ref": step_id,
                "provenance": {"kind": "paper", "reference": relation_provenance_ref},
            }],
            "material_relations": [{
                "relation_id": f"{step_id}-rel1", "event_kind": "state_change",
                "input_material_instance_ids": [input_port["material_instance_id"]],
                "output_material_instance_ids": [output_port["material_instance_id"]],
                "quantity_basis": "whole_batch",
                "source_operation_ref": f"{step_id}-seg1",
                "provenance": {"kind": "paper", "reference": relation_provenance_ref},
            }],
            "lineage_relation": {
                "relation_type": "state_change_of",
                "parent_material_instance_ids": [input_port["material_instance_id"]],
                "child_material_instance_ids": [output_port["material_instance_id"]],
            },
        }

    def _composite_proposal(
        self, *, operation: str = OP_VALUE, op_excerpt: str = OP_EXCERPT,
        name_excerpt: str = NAMING_SENTENCE, out_name: str = "LDH seeds",
        out_state: str = "retained_wet_solid", extra_steps: tuple = (),
        extra_facts: tuple = (),
    ) -> dict:
        input_port = {
            "material_id": "product", "material_instance_id": "inst_a",
            "name": "suspension", "state": "suspension",
            "material_origin": "external_inventory",
            "provenance": {"kind": "paper", "reference": "fact:in_name"},
        }
        output_port = {
            "material_id": "product", "material_instance_id": "inst_b",
            "name": out_name, "state": out_state,
            "provenance": {"kind": "paper", "reference": "fact:out_name"},
        }
        step = self._state_change_step(
            step_id="S1", sequence=1, operation=operation,
            input_port=input_port, output_port=output_port,
            relation_provenance_ref="fact:op",
        )
        facts = [
            self._fact("op", "material_graph[0].operation", operation, op_excerpt),
            self._fact("in_name", "material_graph[0].material_inputs[0].name",
                       "suspension", FILLER),
            self._fact("in_state", "material_graph[0].material_inputs[0].state",
                       "suspension", FILLER),
            self._fact("out_name", OUT_NAME, out_name, name_excerpt),
            self._fact("out_state", OUT_STATE, out_state, op_excerpt),
            *extra_facts,
        ]
        return {"material_graph": [step, *extra_steps], "route_facts": facts}

    def _derive(self, proposal: dict, path: str = OUT_STATE, resolver=None):
        return derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"], path,
            paper_id="paper-A", experimental_group_id="Group A",
            source_digest=self.digest,
            retained_object_resolver=resolver,
        )

    def _positive(self):
        blocks = _blocks(FILLER, OP_SENTENCE, NAMING_SENTENCE)
        proposal = self._composite_proposal()
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        return proposal, record, issue

    def test_span_resolver_binds_block_indexes(self) -> None:
        span_of = build_excerpt_span_resolver(
            _blocks(FILLER, OP_SENTENCE, NAMING_SENTENCE))
        self.assertEqual(span_of(OP_EXCERPT), (1, 1))
        self.assertEqual(span_of(NAMING_SENTENCE), (2, 2))
        self.assertIsNone(span_of("no such sentence anywhere"))
        self.assertIsNone(span_of(""))

    def test_positive_record_binds_precipitates_to_ldh_seeds(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        self.assertEqual(record["schema_version"],
                         POST_OPERATION_RETAINED_OBJECT_SCHEMA)
        self.assertEqual(record["rule_id"], POST_OPERATION_RETAINED_OBJECT_RULE_ID)
        self.assertEqual(record["rule_version"],
                         POST_OPERATION_RETAINED_OBJECT_RULE_VERSION)
        self.assertEqual(record["step_index"], 0)
        self.assertEqual(record["macro_step_id"], "S1")
        self.assertEqual(record["operation_fact_id"], "op")
        self.assertEqual(record["output"], {
            "state_path": OUT_STATE,
            "name_path": OUT_NAME,
            "output_index": 0,
            "material_instance_id": "inst_b",
            "material_id": "product",
            "label": "LDH seeds",
        })
        self.assertEqual(record["retained_object_surface"], "precipitates")
        self.assertEqual(record["retained_object"], "precipitate")
        self.assertEqual(record["naming_fact_id"], "out_name")
        self.assertEqual(record["output_name_fact_id"], "out_name")
        self.assertEqual(record["naming_excerpt"], NAMING_SENTENCE)
        self.assertEqual(record["operation_span"], [1, 1])
        self.assertEqual(record["naming_span"], [2, 2])
        # The source-binding proof basis: binding locators and projected
        # character spans for both excerpts.
        self.assertEqual(record["operation_locator"], "pdf:p1:b2-p1:b2")
        self.assertEqual(record["naming_locator"], "pdf:p1:b3-p1:b3")
        blocks = _blocks(FILLER, OP_SENTENCE, NAMING_SENTENCE)
        projected = " ".join(
            " ".join(text.split()) for _locator, text in blocks
        )
        for excerpt, key in (
            (OP_EXCERPT, "operation_char_span"),
            (NAMING_SENTENCE, "naming_char_span"),
        ):
            start, end = record[key]
            self.assertIsInstance(start, int)
            self.assertIsInstance(end, int)
            self.assertLess(start, end)
            self.assertEqual(projected[start:end], " ".join(excerpt.split()))
        json.dumps(record)  # the record must stay JSON-serializable

    def test_positive_derivation_proves_canonical_state(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        resolver = _resolver({OUT_STATE: (record, "")})
        proof, derive_issue = self._derive(proposal, resolver=resolver)
        self.assertEqual(derive_issue, "")
        self.assertEqual(proof["rule_id"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1")
        self.assertEqual(proof["rule_version"], "1.1.0")
        self.assertEqual(proof["target_state"], "retained_wet_solid")
        self.assertEqual(proof["retained_object_rule_id"],
                         POST_OPERATION_RETAINED_OBJECT_RULE_ID)
        self.assertEqual(proof["retained_object_rule_version"],
                         POST_OPERATION_RETAINED_OBJECT_RULE_VERSION)
        self.assertEqual(proof["retained_object"], "precipitate")
        self.assertEqual(
            proof["retained_object_evidence_id"],
            _evidence_id("paper-A", "Group A", "out_name"),
        )
        self.assertTrue(all(isinstance(value, str) for value in proof.values()))
        evidence = convention_fact_evidence_by_id(
            proposal["route_facts"], paper_id="paper-A",
            experimental_group_id="Group A",
        )
        # Verification runs against the compiled graph, where the unsigned
        # "fact:" placeholders have been rewritten to bound evidence ids.
        prepared = _resolve_fact_provenance(
            proposal["material_graph"], paper_id="paper-A",
            experimental_group_id="Group A",
            operation_evidence_id=proof["operation_evidence_id"],
        )
        self.assertEqual(
            verify_bound_output_state(
                proof, prepared, evidence,
                paper_id="paper-A", experimental_group_id="Group A",
                source_digest=self.digest,
                retained_object_resolver=resolver,
            ), "",
        )
        self.assertEqual(
            verify_bound_output_state(
                proof, prepared, evidence,
                paper_id="paper-A", experimental_group_id="Group A",
                source_digest=self.digest,
            ), "convention_support_evidence_missing",
        )

    def test_composite_without_record_stays_ambiguous(self) -> None:
        proposal, _record, issue = self._positive()
        self.assertEqual(issue, "")
        proof, derive_issue = self._derive(proposal)
        self.assertIsNone(proof)
        self.assertEqual(derive_issue, "convention_rule_not_applicable_or_ambiguous")

    def test_mention_preceding_operation_is_rejected(self) -> None:
        blocks = _blocks(NAMING_SENTENCE, "The mixture was stirred.", OP_SENTENCE)
        proposal = self._composite_proposal()
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_mention_precedes_operation")
        resolver = _resolver({OUT_STATE: (None, issue)})
        proof, derive_issue = self._derive(proposal, resolver=resolver)
        self.assertIsNone(proof)
        self.assertEqual(derive_issue, "retained_object_mention_precedes_operation")

    def test_label_owned_by_another_material_is_unresolved(self) -> None:
        # The naming sentence names THIS group's "LDH seeds", but it is the
        # other step's output that carries it; this step's own output label
        # is never bound by a naming sentence.
        blocks = _blocks(
            "The precipitate fraction was set aside.",
            OP_SENTENCE,
            NAMING_SENTENCE,
            "The solid was held.",
        )
        other = self._state_change_step(
            step_id="S2", sequence=2, operation="held",
            input_port={
                "material_id": "product", "material_instance_id": "inst_c",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:op2"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_d",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "provenance": {"kind": "paper", "reference": "fact:out2_name"},
            },
            relation_provenance_ref="fact:op2",
        )
        proposal = self._composite_proposal(
            out_name="precipitate fraction",
            name_excerpt="The precipitate fraction was set aside.",
            extra_steps=(other,),
            extra_facts=(
                self._fact("op2", "material_graph[1].operation", "held",
                           "The solid was held."),
                self._fact("out2_name",
                           "material_graph[1].material_outputs[0].name",
                           "LDH seeds", NAMING_SENTENCE),
            ),
        )
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_output_binding_unresolved")

    def test_discarded_object_is_rejected(self) -> None:
        discarded = (
            "The precipitates were labeled as LDH seeds, and the "
            "precipitates were discarded."
        )
        blocks = _blocks(FILLER, OP_SENTENCE, discarded)
        proposal = self._composite_proposal(name_excerpt=discarded)
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_discarded")
        # Without the comma the naming pattern's label runs to end-of-sentence
        # ("LDH seeds and the precipitates were discarded"), so the sentence
        # never binds the label; the outcome is unresolved, never a record.
        runaway = (
            "The precipitates were labeled as LDH seeds and the "
            "precipitates were discarded."
        )
        blocks = _blocks(FILLER, OP_SENTENCE, runaway)
        proposal = self._composite_proposal(name_excerpt=runaway)
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_output_binding_unresolved")

    def test_general_discussion_is_unresolved(self) -> None:
        discussion = "Precipitate formation is common in LDH syntheses."
        blocks = _blocks(FILLER, OP_SENTENCE, discussion)
        proposal = self._composite_proposal(name_excerpt=discussion)
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_output_binding_unresolved")

    def test_different_label_is_unresolved(self) -> None:
        naming = "The precipitates were labeled as NiFe seeds."
        blocks = _blocks(FILLER, OP_SENTENCE, naming)
        proposal = self._composite_proposal(name_excerpt=naming)
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_output_binding_unresolved")

    def test_intervening_operation_is_rejected(self) -> None:
        blocks = _blocks(FILLER, OP_SENTENCE, "The mixture was heated.",
                         NAMING_SENTENCE)
        other = self._state_change_step(
            step_id="S2", sequence=2, operation="heated",
            input_port={
                "material_id": "product", "material_instance_id": "inst_c",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:op2"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_d",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "provenance": {"kind": "paper", "reference": "fact:op2"},
            },
            relation_provenance_ref="fact:op2",
        )
        proposal = self._composite_proposal(
            extra_steps=(other,),
            extra_facts=(
                self._fact("op2", "material_graph[1].operation", "heated",
                           "The mixture was heated."),
            ),
        )
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_intervening_operation")

    def test_conflicting_family_endpoint_stays_pending(self) -> None:
        # The record binds "precipitate" to the output label, but the
        # proposal claims "suspension": CENTRIFUGE_COLLECT_PRECIPITATE_V1's
        # endpoint mismatches and REDISPERSION_V1 is object-inconsistent
        # (and its input-state premise fails); nothing may override.
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        proposal["material_graph"][0]["material_outputs"][0]["state"] = "suspension"
        for fact in proposal["route_facts"]:
            if fact["fact_id"] == "out_state":
                fact["value"] = "suspension"
        resolver = _resolver({OUT_STATE: (record, "")})
        proof, derive_issue = self._derive(proposal, resolver=resolver)
        self.assertIsNone(proof)
        self.assertEqual(derive_issue, "convention_rule_not_applicable_or_ambiguous")

    def test_inheritance_through_proven_composite_parent(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        downstream = {
            "macro_step_id": "S2", "macro_action_id": "A1", "sequence": 2,
            "operation": "redispersed", "sample_id": "sample-A",
            "provenance": {"kind": "paper", "reference": "fact:op2"},
            "material_inputs": [{
                "material_id": "product", "material_instance_id": "inst_b",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "upstream_output",
                "parent_output_refs": [{
                    "macro_step_id": "S1", "material_instance_id": "inst_b",
                }],
                "provenance": {"kind": "paper", "reference": "fact:in1_state"},
            }],
            "material_outputs": [{
                "material_id": "product", "material_instance_id": "inst_c",
                "name": "LDH seeds", "state": "suspension",
                "provenance": {"kind": "paper", "reference": "fact:op2"},
            }],
        }
        proposal["material_graph"].append(downstream)
        proposal["route_facts"].extend([
            self._fact("op2", "material_graph[1].operation", "redispersed",
                       "The LDH seeds were redispersed in water."),
            self._fact("in1_state", STEP1_INPUT_STATE, "retained_wet_solid",
                       "The LDH seeds were redispersed in water."),
        ])
        resolver = _resolver({OUT_STATE: (record, "")})
        proof, input_issue = derive_unreviewed_input_state(
            proposal["material_graph"], proposal["route_facts"],
            STEP1_INPUT_STATE, deepcopy(self.source),
            retained_object_resolver=resolver,
        )
        self.assertEqual(input_issue, "")
        self.assertEqual(proof["rule_id"], "PARENT_OUTPUT_STATE_INHERITANCE_V1")
        self.assertEqual(proof["parent_instance_id"], "inst_b")
        self.assertEqual(proof["child_instance_id"], "inst_b")
        self.assertEqual(proof["target_state"], "retained_wet_solid")
        proof, input_issue = derive_unreviewed_input_state(
            proposal["material_graph"], proposal["route_facts"],
            STEP1_INPUT_STATE, deepcopy(self.source),
        )
        self.assertIsNone(proof)
        self.assertEqual(input_issue, "convention_parent_state_unverified")

    def test_invalid_record_is_rejected(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        for mutate in (
            lambda item: item["output"].update(
                state_path="material_graph[0].material_outputs[1].state"),
            lambda item: item.update(retained_object=""),
            lambda item: item.update(rule_id="SOME_OTHER_RULE"),
            lambda item: item.update(schema_version="other-schema/v9"),
        ):
            tampered = deepcopy(record)
            mutate(tampered)
            resolver = _resolver({OUT_STATE: (tampered, "")})
            proof, derive_issue = self._derive(proposal, resolver=resolver)
            self.assertIsNone(proof)
            self.assertEqual(derive_issue,
                             "retained_object_output_binding_unresolved")

    def test_operation_span_missing(self) -> None:
        proposal = self._composite_proposal(op_excerpt="no such text anywhere")
        span_of = build_excerpt_span_resolver(
            _blocks(FILLER, OP_SENTENCE, NAMING_SENTENCE))
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_operation_span_missing")


class Round1SourceRelationHardeningTest(PostOperationRetainedObjectTest):
    """Round-1 review gaps R1–R3 in the retained-object source relation.

    R1: an operation the graph never modeled still breaks continuity when
    the raw source interval between the operation quote and the naming
    quote affirms it ("The solid was then dried at 60 C overnight.");
    a negated interval mention ("was not dried") does not.  R2/R3: a
    state-changing modifier on the named object ("The dried precipitates
    were labeled as X") rejects conservatively, while a benign modifier
    ("yellow") is recorded and allowed.
    """

    def test_unmodeled_interval_operation_breaks_continuity(self) -> None:
        blocks = _blocks(FILLER, OP_SENTENCE,
                         "The solid was then dried at 60 C overnight.",
                         NAMING_SENTENCE)
        proposal = self._composite_proposal()
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_intervening_operation")

    def test_negated_interval_operation_keeps_continuity(self) -> None:
        blocks = _blocks(FILLER, OP_SENTENCE, "The solid was not dried.",
                         NAMING_SENTENCE)
        proposal = self._composite_proposal()
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertEqual(issue, "")
        self.assertEqual(record["retained_object"], "precipitate")
        self.assertEqual(record["object_modifiers"], [])

    def test_fallback_only_stem_breaks_continuity(self) -> None:
        # "calcin" is in the conservative fallback set; no conventions
        # resource stem matches "calcined".
        blocks = _blocks(FILLER, OP_SENTENCE, "The solid was calcined at 500 C.",
                         NAMING_SENTENCE)
        proposal = self._composite_proposal()
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "retained_object_intervening_operation")

    def test_state_changing_object_modifier_is_rejected(self) -> None:
        for naming in (
            "The dried precipitates were labeled as LDH seeds.",
            "The calcined precipitates were labeled as LDH seeds.",
        ):
            with self.subTest(naming=naming):
                blocks = _blocks(FILLER, OP_SENTENCE, naming)
                proposal = self._composite_proposal(name_excerpt=naming)
                span_of = build_excerpt_span_resolver(blocks)
                record, issue = derive_post_operation_retained_object(
                    proposal["material_graph"], proposal["route_facts"], 0,
                    span_of=span_of,
                )
                self.assertIsNone(record)
                self.assertEqual(
                    issue, "retained_object_state_changing_modifier",
                )

    def test_benign_object_modifier_is_recorded(self) -> None:
        naming = "The yellow precipitates were labeled as LDH seeds."
        blocks = _blocks(FILLER, OP_SENTENCE, naming)
        proposal = self._composite_proposal(name_excerpt=naming)
        span_of = build_excerpt_span_resolver(blocks)
        record, issue = derive_post_operation_retained_object(
            proposal["material_graph"], proposal["route_facts"], 0,
            span_of=span_of,
        )
        self.assertEqual(issue, "")
        self.assertEqual(record["retained_object"], "precipitate")
        self.assertEqual(record["object_modifiers"], ["yellow"])
        json.dumps(record)


class RetainedObjectTrustBoundaryTest(PostOperationRetainedObjectTest):
    """R4: a retained-object record has ZERO authority.

    A perfectly shaped but fabricated record — one whose content does not
    match the graph and facts at the consuming layer — is rejected by the
    contract cross-check, no matter which single field is forged.  An
    inheritance proof whose parent output stands on the record verifies
    only when the live resolver is forwarded into the inheritance branch.
    """

    def _tampered_resolver(self, record: dict, mutate) -> object:
        tampered = deepcopy(record)
        mutate(tampered)
        return _resolver({OUT_STATE: (tampered, "")})

    def test_fabricated_record_is_rejected(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        # Perfectly shaped, fully forged: the naming sentence below is not
        # the excerpt of any fact and matches nothing in the graph.
        fabricated = deepcopy(record)
        fabricated["naming_excerpt"] = (
            "The precipitates were labeled as LDH seeds."
        )
        resolver = _resolver({OUT_STATE: (fabricated, "")})
        proof, derive_issue = self._derive(proposal, resolver=resolver)
        self.assertIsNone(proof)
        self.assertEqual(derive_issue,
                         "retained_object_output_binding_unresolved")

    def test_each_cross_checked_field_mutation_is_rejected(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        mutations = {
            "material_instance_id": lambda item: item["output"].update(
                material_instance_id="inst_zzz"),
            "material_id": lambda item: item["output"].update(
                material_id="other_material"),
            "label": lambda item: item["output"].update(label="NiFe seeds"),
            "output_index": lambda item: item["output"].update(output_index=1),
            "naming_excerpt": lambda item: item.update(
                naming_excerpt="The precipitates were labeled as LDH seeds."),
            "naming_fact_id": lambda item: item.update(
                naming_fact_id="other_fact"),
            "output_name_fact_id": lambda item: item.update(
                output_name_fact_id="other_fact"),
            "operation_fact_id": lambda item: item.update(
                operation_fact_id="op_fabricated"),
            "step_index": lambda item: item.update(step_index=1),
            "macro_step_id": lambda item: item.update(macro_step_id="S9"),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                resolver = self._tampered_resolver(record, mutate)
                proof, derive_issue = self._derive(proposal, resolver=resolver)
                self.assertIsNone(proof)
                self.assertEqual(derive_issue,
                                 "retained_object_output_binding_unresolved")

    def _downstream_proposal(self):
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        downstream = {
            "macro_step_id": "S2", "macro_action_id": "A1", "sequence": 2,
            "operation": "redispersed", "sample_id": "sample-A",
            "provenance": {"kind": "paper", "reference": "fact:op2"},
            "material_inputs": [{
                "material_id": "product", "material_instance_id": "inst_b",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "upstream_output",
                "parent_output_refs": [{
                    "macro_step_id": "S1", "material_instance_id": "inst_b",
                }],
                "provenance": {"kind": "paper", "reference": "fact:in1_state"},
            }],
            "material_outputs": [{
                "material_id": "product", "material_instance_id": "inst_c",
                "name": "LDH seeds", "state": "suspension",
                "provenance": {"kind": "paper", "reference": "fact:op2"},
            }],
        }
        proposal["material_graph"].append(downstream)
        proposal["route_facts"].extend([
            self._fact("op2", "material_graph[1].operation", "redispersed",
                       "The LDH seeds were redispersed in water."),
            self._fact("in1_state", STEP1_INPUT_STATE, "retained_wet_solid",
                       "The LDH seeds were redispersed in water."),
        ])
        return proposal, record

    def test_inheritance_over_record_parent_verifies_with_resolver(self) -> None:
        proposal, record = self._downstream_proposal()
        resolver = _resolver({OUT_STATE: (record, "")})
        proof, input_issue = derive_unreviewed_input_state(
            proposal["material_graph"], proposal["route_facts"],
            STEP1_INPUT_STATE, deepcopy(self.source),
            retained_object_resolver=resolver,
        )
        self.assertEqual(input_issue, "")
        self.assertEqual(proof["rule_id"], "PARENT_OUTPUT_STATE_INHERITANCE_V1")
        evidence = convention_fact_evidence_by_id(
            proposal["route_facts"], paper_id="paper-A",
            experimental_group_id="Group A",
        )
        prepared = _resolve_fact_provenance(
            proposal["material_graph"], paper_id="paper-A",
            experimental_group_id="Group A",
            operation_evidence_id=proof["operation_evidence_id"],
        )
        # The inheritance branch of verify must forward the resolver: the
        # parent output stands on the retained-object record, so the proof
        # verifies only with the live record and fails closed without it.
        self.assertEqual(
            verify_bound_output_state(
                proof, prepared, evidence,
                paper_id="paper-A", experimental_group_id="Group A",
                source_digest=self.digest,
                retained_object_resolver=resolver,
            ), "",
        )
        self.assertEqual(
            verify_bound_output_state(
                proof, prepared, evidence,
                paper_id="paper-A", experimental_group_id="Group A",
                source_digest=self.digest,
            ), "convention_parent_state_unverified",
        )

    def test_fabricated_proof_fields_fail_verify(self) -> None:
        proposal, record, issue = self._positive()
        self.assertEqual(issue, "")
        resolver = _resolver({OUT_STATE: (record, "")})
        proof, derive_issue = self._derive(proposal, resolver=resolver)
        self.assertEqual(derive_issue, "")
        evidence = convention_fact_evidence_by_id(
            proposal["route_facts"], paper_id="paper-A",
            experimental_group_id="Group A",
        )
        prepared = _resolve_fact_provenance(
            proposal["material_graph"], paper_id="paper-A",
            experimental_group_id="Group A",
            operation_evidence_id=proof["operation_evidence_id"],
        )
        self.assertEqual(
            verify_bound_output_state(
                proof, prepared, evidence,
                paper_id="paper-A", experimental_group_id="Group A",
                source_digest=self.digest,
                retained_object_resolver=resolver,
            ), "",
        )
        for field, forged in (
            ("retained_object_naming_locator", "pdf:p9:b9-p9:b9"),
            ("retained_object_operation_locator", "pdf:p9:b9-p9:b9"),
            ("retained_object_naming_span", "[0,0]"),
            ("retained_object_operation_char_span", "0:1"),
            ("retained_object_output_name_fact_id", "forged_fact"),
        ):
            with self.subTest(forged_field=field):
                tampered_proof = dict(proof)
                tampered_proof[field] = forged
                self.assertEqual(
                    verify_bound_output_state(
                        tampered_proof, prepared, evidence,
                        paper_id="paper-A", experimental_group_id="Group A",
                        source_digest=self.digest,
                        retained_object_resolver=resolver,
                    ), "convention_proof_mismatch",
                )


if __name__ == "__main__":
    unittest.main()
