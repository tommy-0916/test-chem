"""ProtocolReferenceProof: extraction, resolution, engine token, DAG nodes.

Round 3D phase 1.  The narrow proof class: a paper defines a named
protocol once ("a centrifugation−redispersion protocol using deionized
water three times, which was the first centrifugation−redispersion
protocol") and later references it by name and ordinal ("after a second
centrifugation−redispersion protocol one time").  Only the definition's
``operation_sequence`` and ``liquid_medium`` are inherited; execution
counts stay invocation-local annotations, and retained objects / output
states / material identities are never inherited (a protocol reference
must never reintroduce "first run produced X ⇒ second run produces X").

The DAG fixture carries a single-segment "redispersion protocol"
definition + reference so the reference step reaches the flat engine's
liquid gate (a compound reference operation would hit the
composite-operation guard first — that is the r10 chain's separate,
already-attributed blocker).  The module-level extraction/resolution
tests use the real NiFe compound phrases verbatim.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_convention_basis import (
    _VerifiedLiquidMedium,
    derive_unreviewed_output_state,
)
from chem_agent_contracts.route_proof_dag import (
    PROTOCOL_REFERENCE_PROOF_SCHEMA,
    build_state_proof_dag,
    node_id_for,
    verify_state_proof_dag,
)
from chem_agent_contracts.route_protocol_reference import (
    PROTOCOL_DEFINITION_SCHEMA,
    PROTOCOL_REFERENCE_RULE_ID,
    extract_protocol_definition,
    protocol_definition_digest,
    resolve_protocol_reference,
    validate_protocol_definition,
)
from chem_agent_contracts.route_retained_object import (
    build_excerpt_span_resolver,
)

SCOPE = {
    "paper_id": "paper-A", "experimental_group_id": "Group A",
    "source_digest": "sha256_source",
}

# The real NiFe Control group evidence (U+2212 minus), verbatim.
NIFE_DEF_EXCERPT = (
    "by a centrifugation−redispersion protocol using deionized water three "
    "times, which was the first centrifugation−redispersion protocol"
)
NIFE_REF_VALUE = "second centrifugation−redispersion protocol"
NIFE_REF_EXCERPT = (
    "Finally, after a second centrifugation−redispersion protocol one time"
)

FORBIDDEN_SLOTS = (
    "retained_object", "inter_segment_material_flow", "output_state",
    "material_instance_identity", "execution_count", "segment_input_material",
    "segment_output_material", "retained_phase", "inter_segment_flow",
    "derived_state_transition",
)


def _definition_record(**overrides):
    record, issue = extract_protocol_definition(
        NIFE_DEF_EXCERPT, evidence_id="ev_def", locator="pdf:p1:b2-b2",
        char_span=[10, 120], **SCOPE,
    )
    assert issue == "", issue
    record.update(overrides)
    return record


def _candidate(excerpt, evidence_id, position, *, scope=None,
               field_path="material_graph[5].operation"):
    return {
        "excerpt": excerpt, "evidence_id": evidence_id,
        "field_path": field_path, "locator": "pdf:p1:b1-b1",
        "char_span": [position, position + len(excerpt)],
        **(scope or SCOPE),
    }


def _resolve(reference_value=NIFE_REF_VALUE, reference_excerpt=NIFE_REF_EXCERPT,
             *, position=500, candidates=(), reference_evidence_id="ev_ref"):
    return resolve_protocol_reference(
        reference_value, reference_excerpt,
        reference_evidence_id=reference_evidence_id,
        reference_locator="pdf:p2:b74-b75", reference_position=position,
        candidates=candidates, **SCOPE,
    )


class DefinitionExtractionTest(unittest.TestCase):
    """The NiFe definition phrase extracts exactly the two ruled slots."""

    def test_nife_definition_extracts_exact_slots_and_annotations(self) -> None:
        record = _definition_record()
        self.assertEqual(record["schema_version"], PROTOCOL_DEFINITION_SCHEMA)
        # operation_sequence: segment_identity + segment_order, decomposed
        # from the compound name exactly like the graph[5] segments.
        self.assertEqual(record["operation_sequence"], [
            {"segment_identity": "centrifugation", "segment_order": 1},
            {"segment_identity": "redispersion", "segment_order": 2},
        ])
        self.assertEqual(record["liquid_medium"], "deionized water")
        self.assertEqual(record["protocol_name"],
                         "centrifugation-redispersion protocol")
        self.assertEqual(record["definition_ordinal_anchor"], "first")
        self.assertEqual(record["definition_evidence_id"], "ev_def")
        self.assertEqual(record["locator"], "pdf:p1:b2-b2")
        self.assertEqual(record["char_span"], [10, 120])
        self.assertEqual(validate_protocol_definition(record), "")

    def test_definition_record_has_no_execution_count_anywhere(self) -> None:
        record = _definition_record()
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("execution_count", blob)
        self.assertNotIn("three times", blob)
        self.assertEqual(
            set(record),
            {"schema_version", "protocol_name", "operation_sequence",
             "liquid_medium", "definition_evidence_id",
             "definition_ordinal_anchor", "paper_id",
             "experimental_group_id", "source_digest", "locator",
             "char_span"},
        )

    def test_medium_must_govern_the_protocol_mention(self) -> None:
        # A "using" clause that modifies another operation's phrase is not
        # the protocol definition's medium slot.
        record, issue = extract_protocol_definition(
            "the solid was washed using deionized water, followed by a "
            "centrifugation−redispersion protocol, which was the first "
            "centrifugation−redispersion protocol",
            evidence_id="ev_x", **SCOPE,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_definition_not_present")

    def test_definition_requires_the_ordinal_anchor(self) -> None:
        record, issue = extract_protocol_definition(
            "a centrifugation−redispersion protocol using deionized water "
            "three times.",
            evidence_id="ev_x", **SCOPE,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_definition_anchor_missing")

    def test_definition_medium_must_name_a_liquid(self) -> None:
        record, issue = extract_protocol_definition(
            "a centrifugation−redispersion protocol using a rotary "
            "evaporator, which was the first centrifugation−redispersion "
            "protocol",
            evidence_id="ev_x", **SCOPE,
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_definition_medium_missing")


class ClosedSchemaTest(unittest.TestCase):
    """The closed slot set rejects every forbidden result/object slot."""

    def test_valid_record_passes(self) -> None:
        self.assertEqual(validate_protocol_definition(_definition_record()), "")

    def test_each_forbidden_slot_key_is_rejected_individually(self) -> None:
        for key in FORBIDDEN_SLOTS:
            with self.subTest(key=key):
                record = _definition_record(**{key: "precipitate"})
                self.assertEqual(
                    validate_protocol_definition(record),
                    "protocol_definition_unknown_key",
                )

    def test_missing_required_key_and_bad_sequence_are_rejected(self) -> None:
        record = _definition_record()
        del record["liquid_medium"]
        self.assertEqual(
            validate_protocol_definition(record),
            "protocol_definition_missing_key",
        )
        record = _definition_record(
            operation_sequence=[
                {"segment_identity": "centrifugation", "segment_order": 2},
                {"segment_identity": "redispersion", "segment_order": 1},
            ],
        )
        self.assertEqual(
            validate_protocol_definition(record),
            "protocol_definition_invalid",
        )


class ResolutionTest(unittest.TestCase):
    """The v1 resolution rules: scope, uniqueness, precedence, ordinals."""

    def test_happy_path_first_definition_second_reference(self) -> None:
        record, issue = _resolve(
            candidates=[_candidate(NIFE_DEF_EXCERPT, "ev_def", 10)],
        )
        self.assertEqual(issue, "")
        self.assertEqual(record["schema_version"],
                         PROTOCOL_REFERENCE_PROOF_SCHEMA)
        self.assertEqual(record["rule_id"], PROTOCOL_REFERENCE_RULE_ID)
        self.assertEqual(record["definition_evidence_id"], "ev_def")
        self.assertEqual(record["reference_evidence_id"], "ev_ref")
        self.assertEqual(record["definition_ordinal_anchor"], "first")
        self.assertEqual(record["reference_ordinal"], "second")
        # Execution counts are invocation-local annotations, not slots.
        self.assertEqual(record["definition_execution_count"], "three times")
        self.assertEqual(record["reference_execution_count"], "one time")
        self.assertEqual(record["operation_sequence"], [
            {"segment_identity": "centrifugation", "segment_order": 1},
            {"segment_identity": "redispersion", "segment_order": 2},
        ])
        self.assertEqual(record["liquid_medium"], "deionized water")
        definition, _ = extract_protocol_definition(
            NIFE_DEF_EXCERPT, evidence_id="ev_def",
            locator="pdf:p1:b1-b1",
            char_span=[10, 10 + len(NIFE_DEF_EXCERPT)], **SCOPE,
        )
        self.assertEqual(record["definition_digest"],
                         protocol_definition_digest(definition))
        self.assertEqual(record["paper_id"], SCOPE["paper_id"])
        self.assertEqual(record["experimental_group_id"],
                         SCOPE["experimental_group_id"])
        self.assertEqual(record["source_digest"], SCOPE["source_digest"])

    def test_unresolved_without_any_definition(self) -> None:
        record, issue = _resolve(candidates=[])
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_reference_unresolved")

    def test_ambiguous_with_two_distinct_definitions(self) -> None:
        other = (
            "a centrifugation−redispersion protocol using water two times, "
            "which was the first centrifugation−redispersion protocol"
        )
        record, issue = _resolve(candidates=[
            _candidate(NIFE_DEF_EXCERPT, "ev_def1", 10),
            _candidate(other, "ev_def2", 200),
        ])
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_reference_ambiguous")

    def test_wrong_group_scope_fails_closed(self) -> None:
        other_scope = {
            "paper_id": "paper-A", "experimental_group_id": "Group B",
            "source_digest": "sha256_other",
        }
        record, issue = _resolve(candidates=[
            _candidate(NIFE_DEF_EXCERPT, "ev_def", 10, scope=other_scope),
        ])
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_reference_scope_mismatch")

    def test_definition_after_reference_fails_closed(self) -> None:
        record, issue = _resolve(
            position=50,
            candidates=[_candidate(NIFE_DEF_EXCERPT, "ev_def", 600)],
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_reference_definition_after_reference")

    def test_ordinal_inconsistency_fails_resolution(self) -> None:
        # A "first" reference cannot descend from the "first" definition.
        record, issue = _resolve(
            reference_value="first centrifugation−redispersion protocol",
            candidates=[_candidate(NIFE_DEF_EXCERPT, "ev_def", 10)],
        )
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_reference_ordinal_mismatch")

    def test_nearer_non_definition_medium_phrase_is_not_a_candidate(self) -> None:
        # "dispersed in 30 mL of water and aged" is a different operation's
        # medium phrase — nearer to the reference, never a definition.
        record, issue = _resolve(candidates=[
            _candidate("dispersed in 30 mL of water and aged for 20 h",
                       "ev_near", 400,
                       field_path="material_graph[6].operation"),
        ])
        self.assertIsNone(record)
        self.assertEqual(issue, "protocol_reference_unresolved")
        record, issue = _resolve(candidates=[
            _candidate("dispersed in 30 mL of water and aged for 20 h",
                       "ev_near", 400,
                       field_path="material_graph[6].operation"),
            _candidate(NIFE_DEF_EXCERPT, "ev_def", 10),
        ])
        self.assertEqual(issue, "")
        self.assertEqual(record["definition_evidence_id"], "ev_def")

    def test_facts_quoting_the_same_mention_dedupe_to_one(self) -> None:
        # Five facts quoting the SAME definition sentence are one mention;
        # the representative evidence binding prefers the operation fact.
        paths = [
            "route_signature.operations[5]",
            "material_graph[5].parameters[0].value",
            "material_graph[5].operation",
            "material_graph[5].material_inputs[0].name",
            "material_graph[5].material_outputs[0].state",
        ]
        record, issue = _resolve(candidates=[
            _candidate(NIFE_DEF_EXCERPT, f"ev_{index}", 10, field_path=path)
            for index, path in enumerate(paths)
        ])
        self.assertEqual(issue, "")
        self.assertEqual(record["definition_evidence_id"], "ev_2")

    def test_normalized_name_equality_dash_case_whitespace(self) -> None:
        # ASCII hyphen + uppercase + extra whitespace == U+2212 name.
        record, issue = _resolve(
            reference_value="SECOND  centrifugation-redispersion  protocol",
            candidates=[_candidate(NIFE_DEF_EXCERPT, "ev_def", 10)],
        )
        self.assertEqual(issue, "")
        self.assertEqual(record["protocol_name"],
                         "centrifugation-redispersion protocol")


# -- DAG + engine fixtures ----------------------------------------------------

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
DEF_VALUE = "redispersion protocol"
DEF_SENTENCE = (
    "The mixture was purified by a redispersion protocol using deionized "
    "water two times, which was the first redispersion protocol."
)
DEF_EXCERPT = (
    "by a redispersion protocol using deionized water two times, which was "
    "the first redispersion protocol"
)
REF_VALUE = "second redispersion protocol"
REF_SENTENCE = (
    "Finally, after a second redispersion protocol one time, the samples "
    "were collected for characterization."
)
REF_EXCERPT = (
    "after a second redispersion protocol one time, the samples were "
    "collected"
)
IN3_SENTENCE = "The washed_wet_solid was carried forward."

OUT_STATE = "material_graph[1].material_outputs[0].state"
REF_OPERATION = "material_graph[2].operation"
REF_INPUT_STATE = "material_graph[2].material_inputs[0].state"
REF_OUT_STATE = "material_graph[2].material_outputs[0].state"


def _blocks(*texts: str) -> list[tuple[str, str]]:
    return [
        (f"pdf:p1:b{index}-p1:b{index}", text)
        for index, text in enumerate(texts, start=1)
    ]


def _reseal(dag: dict, target_id: str, mutate) -> dict:
    """Mutate one node, then recompute every dependent content address."""
    from chem_agent_contracts.route_proof_dag import leaf_id_for

    dag = deepcopy(dag)
    mutate(dag["nodes"][target_id])
    while True:
        changed = False
        for node_id in list(dag["nodes"]):
            node = dag["nodes"][node_id]
            if isinstance(node.get("leaf"), dict):
                leaf_id = leaf_id_for(node["leaf"])
                if leaf_id != node["leaf"].get("leaf_id"):
                    node["leaf"]["leaf_id"] = leaf_id
            recomputed = node_id_for(node)
            if recomputed == node_id:
                continue
            del dag["nodes"][node_id]
            node["node_id"] = recomputed
            dag["nodes"][recomputed] = node
            for other in dag["nodes"].values():
                for premise in other["premises"]:
                    if premise["node_id"] == node_id:
                        premise["node_id"] = recomputed
            if dag["root_id"] == node_id:
                dag["root_id"] = recomputed
            changed = True
            break
        if not changed:
            return dag


class ProtocolReferenceFixtureMixin(unittest.TestCase):
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

    def _proposal(self) -> dict:
        """graph[0] definition carrier; graph[1] composite retained_wet_solid;
        graph[2] the protocol-reference redispersion step (liquid gate)."""
        step_0 = self._state_change_step(
            step_id="S0", sequence=0, operation=DEF_VALUE,
            input_port={
                "material_id": "product", "material_instance_id": "inst_s",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:def_op"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_t",
                "name": "LDH seeds", "state": "suspension",
                "provenance": {"kind": "paper", "reference": "fact:def_op"},
            },
            relation_provenance_ref="fact:def_op",
        )
        step_1 = self._state_change_step(
            step_id="S1", sequence=1, operation=OP_VALUE,
            input_port={
                "material_id": "product", "material_instance_id": "inst_a",
                "name": "suspension", "state": "suspension",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:in_name"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_b",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "provenance": {"kind": "paper", "reference": "fact:out_name"},
            },
            relation_provenance_ref="fact:op",
        )
        step_2 = self._state_change_step(
            step_id="S2", sequence=2, operation=REF_VALUE,
            input_port={
                "material_id": "product", "material_instance_id": "inst_w",
                "name": "washed_wet_solid", "state": "washed_wet_solid",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:ref_in_state"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_c",
                "name": "LDH seeds", "state": "suspension",
                "provenance": {"kind": "paper", "reference": "fact:ref_op"},
            },
            relation_provenance_ref="fact:ref_op",
        )
        facts = [
            self._fact("def_op", "material_graph[0].operation", DEF_VALUE,
                       DEF_EXCERPT),
            self._fact("op", "material_graph[1].operation", OP_VALUE, OP_EXCERPT),
            self._fact("in_name", "material_graph[1].material_inputs[0].name",
                       "suspension", FILLER),
            self._fact("in_state", "material_graph[1].material_inputs[0].state",
                       "suspension", FILLER),
            self._fact("out_name", "material_graph[1].material_outputs[0].name",
                       "LDH seeds", NAMING_SENTENCE),
            self._fact("out_state", OUT_STATE, "retained_wet_solid", OP_EXCERPT),
            self._fact("ref_op", REF_OPERATION, REF_VALUE, REF_EXCERPT),
            self._fact("ref_in_state", REF_INPUT_STATE, "washed_wet_solid",
                       IN3_SENTENCE),
            self._fact("ref_out_state", REF_OUT_STATE, "suspension", REF_EXCERPT),
        ]
        return {"material_graph": [step_0, step_1, step_2],
                "route_facts": facts}

    def _chain(self):
        proposal = self._proposal()
        blocks = _blocks(DEF_SENTENCE, FILLER, OP_SENTENCE, NAMING_SENTENCE,
                         IN3_SENTENCE, REF_SENTENCE)
        span_of = build_excerpt_span_resolver(blocks)
        return proposal, blocks, span_of

    def _kwargs(self):
        return {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "source_digest": self.digest,
        }

    def _build(self, proposal, path, span_of, expect_issue=""):
        dag, issue = build_state_proof_dag(
            proposal["material_graph"], proposal["route_facts"], path,
            span_of=span_of, **self._kwargs(),
        )
        self.assertEqual(issue, expect_issue)
        if not expect_issue:
            self.assertIsNotNone(dag)
        return dag

    def _verify(self, proposal, dag, span_of, blocks=None):
        return verify_state_proof_dag(
            dag, proposal["material_graph"], proposal["route_facts"],
            span_of=span_of, blocks=blocks, **self._kwargs(),
        )

    def _reference_dag(self):
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, REF_OUT_STATE, span_of)
        return proposal, blocks, span_of, dag

    def _protocol_reference_node_id(self, dag: dict) -> str:
        return next(
            node_id for node_id, node in dag["nodes"].items()
            if node["node_type"] == "protocol_reference"
        )


class EngineTokenTest(ProtocolReferenceFixtureMixin):
    """The _VerifiedLiquidMedium capability token at the liquid gate."""

    def _derive(self, proposal, token=None):
        return derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"],
            REF_OUT_STATE, verified_liquid_medium=token, **self._kwargs(),
        )

    def _token(self, operation_value=REF_VALUE, medium="deionized water",
               definition_digest="sha256_" + "a" * 64):
        return _VerifiedLiquidMedium(
            operation_value=operation_value, medium=medium,
            definition_digest=definition_digest,
        )

    def test_without_token_the_gate_fails_closed(self) -> None:
        proposal, _blocks_unused, _span = self._chain()
        proof, issue = self._derive(proposal)
        self.assertIsNone(proof)
        self.assertEqual(issue, "convention_liquid_participation_missing")

    def test_forged_token_with_wrong_operation_fails_closed(self) -> None:
        proposal, _blocks_unused, _span = self._chain()
        proof, issue = self._derive(
            proposal, token=self._token(operation_value="redispersion protocol"),
        )
        self.assertIsNone(proof)
        self.assertEqual(issue, "convention_liquid_participation_missing")

    def test_empty_medium_or_digest_fails_closed(self) -> None:
        proposal, _blocks_unused, _span = self._chain()
        for token in (
            self._token(medium=""),
            self._token(definition_digest=""),
        ):
            proof, issue = self._derive(proposal, token=token)
            self.assertIsNone(proof)
            self.assertEqual(issue, "convention_liquid_participation_missing")

    def test_correct_token_discharges_the_gate(self) -> None:
        proposal, _blocks_unused, _span = self._chain()
        proof, issue = self._derive(proposal, token=self._token())
        self.assertEqual(issue, "")
        self.assertEqual(proof["rule_id"], "REDISPERSION_V1")
        self.assertEqual(proof["target_state"], "suspension")
        # The proof dict carries no token trace: it is the same shape as a
        # literally-proven proof.
        self.assertFalse(any(key.startswith("liquid")
                             for key in proof))

    def test_token_never_discharges_retained_object_premises(self) -> None:
        # The composite operation still needs its own post-operation
        # retained-object source record; a liquid token bound to that very
        # operation changes nothing about the compound-operation guard.
        proposal, _blocks_unused, _span = self._chain()
        proof, issue = derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"], OUT_STATE,
            verified_liquid_medium=self._token(operation_value=OP_VALUE),
            **self._kwargs(),
        )
        # No span resolver here: no record exists, the guard fails exactly
        # as without any token.
        self.assertIsNone(proof)
        self.assertEqual(issue, "convention_rule_not_applicable_or_ambiguous")
        proof, issue = derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"], OUT_STATE,
            **self._kwargs(),
        )
        self.assertIsNone(proof)
        self.assertEqual(issue, "convention_rule_not_applicable_or_ambiguous")


class ProtocolReferenceDagTest(ProtocolReferenceFixtureMixin):
    """DAG build + verify of the liquid_medium premise and its node."""

    def test_reference_step_builds_and_verifies(self) -> None:
        proposal, blocks, span_of, dag = self._reference_dag()
        root = dag["nodes"][dag["root_id"]]
        self.assertEqual(root["node_type"], "state_change")
        self.assertEqual(root["rule_id"], "REDISPERSION_V1")
        roles = {premise["role"]: premise["node_id"]
                 for premise in root["premises"]}
        self.assertEqual(set(roles),
                         {"parent_state", "operation", "liquid_medium"})
        medium = dag["nodes"][roles["liquid_medium"]]
        self.assertEqual(medium["node_type"], "protocol_reference")
        self.assertEqual(medium["schema_version"],
                         PROTOCOL_REFERENCE_PROOF_SCHEMA)
        # The claim binds the operation the node certifies: the step's
        # operation path, no state, no material identity.
        self.assertEqual(medium["claim"], {
            "field_path": REF_OPERATION, "target_state": "",
            "material_id": "", "material_instance_id": "",
        })
        self.assertEqual(medium["operation_path"], REF_OPERATION)
        self.assertEqual(medium["operation_value"], REF_VALUE)
        self.assertEqual(medium["protocol_name"], "redispersion protocol")
        self.assertEqual(medium["operation_sequence"], [
            {"segment_identity": "redispersion", "segment_order": 1},
        ])
        self.assertEqual(medium["liquid_medium"], "deionized water")
        self.assertEqual(medium["definition_ordinal_anchor"], "first")
        self.assertEqual(medium["reference_ordinal"], "second")
        self.assertEqual(medium["definition_execution_count"], "two times")
        self.assertEqual(medium["reference_execution_count"], "one time")
        self.assertTrue(medium["definition_digest"])
        medium_roles = {premise["role"]: premise["node_id"]
                        for premise in medium["premises"]}
        self.assertEqual(set(medium_roles),
                         {"definition_evidence", "reference_evidence"})
        # The reference premise is the step's operation literal — the same
        # content-addressed node as the root's operation premise.
        self.assertEqual(medium_roles["reference_evidence"],
                         roles["operation"])
        definition = dag["nodes"][medium_roles["definition_evidence"]]
        self.assertEqual(definition["node_type"], "paper_literal")
        self.assertEqual(definition["leaf"]["field_path"],
                         "material_graph[0].operation")
        self.assertEqual(definition["leaf"]["claim_value"], DEF_VALUE)
        self.assertEqual(len(dag["nodes"]), 5)
        self.assertEqual(self._verify(proposal, dag, span_of), "")
        self.assertEqual(self._verify(proposal, dag, span_of, blocks=blocks), "")

    def test_tampered_definition_quote_invalidates(self) -> None:
        proposal, blocks, span_of, dag = self._reference_dag()
        old_root_id = dag["root_id"]
        modified = deepcopy(proposal)
        for fact in modified["route_facts"]:
            if fact["fact_id"] == "def_op":
                fact["excerpt"] = DEF_SENTENCE
        self.assertNotEqual(self._verify(modified, dag, span_of), "")
        rebuilt = self._build(modified, REF_OUT_STATE, span_of)
        self.assertNotEqual(rebuilt["root_id"], old_root_id)
        old_medium = self._protocol_reference_node_id(dag)
        new_medium = self._protocol_reference_node_id(rebuilt)
        self.assertNotEqual(old_medium, new_medium)
        self.assertNotEqual(
            dag["nodes"][old_medium]["definition_digest"],
            rebuilt["nodes"][new_medium]["definition_digest"],
        )
        self.assertEqual(self._verify(modified, rebuilt, span_of), "")
        # The old DAG still verifies against the unchanged facts.
        self.assertEqual(self._verify(proposal, dag, span_of), "")

    def test_tampered_reference_quote_invalidates(self) -> None:
        proposal, blocks, span_of, dag = self._reference_dag()
        modified = deepcopy(proposal)
        for fact in modified["route_facts"]:
            if fact["fact_id"] in {"ref_op", "ref_out_state"}:
                fact["excerpt"] = REF_SENTENCE
        self.assertNotEqual(self._verify(modified, dag, span_of), "")
        rebuilt = self._build(modified, REF_OUT_STATE, span_of)
        self.assertNotEqual(rebuilt["root_id"], dag["root_id"])
        self.assertEqual(self._verify(modified, rebuilt, span_of), "")
        self.assertEqual(self._verify(proposal, dag, span_of), "")

    def test_resealed_definition_role_substitution_fails(self) -> None:
        proposal, blocks, span_of, dag = self._reference_dag()
        medium_id = self._protocol_reference_node_id(dag)
        # The donor is a paper literal that verifies on its own inside this
        # DAG: only the role-fact binding can reject it.
        donor_id = next(
            premise["node_id"]
            for premise in dag["nodes"][dag["root_id"]]["premises"]
            if premise["role"] == "parent_state"
        )
        self.assertEqual(dag["nodes"][donor_id]["node_type"], "paper_literal")

        def substitute(node):
            for premise in node["premises"]:
                if premise["role"] == "definition_evidence":
                    premise["node_id"] = donor_id

        tampered = _reseal(dag, medium_id, substitute)
        # Every content address is honestly recomputed after the swap.
        resealed_id = next(
            node["node_id"] for node in tampered["nodes"].values()
            if node["node_type"] == "protocol_reference"
        )
        self.assertEqual(
            node_id_for(tampered["nodes"][resealed_id]), resealed_id,
        )
        self.assertEqual(
            self._verify(proposal, tampered, span_of),
            "proof_dag_protocol_reference_mismatch",
        )
        self.assertEqual(self._verify(proposal, dag, span_of), "")

    def test_protocol_reference_in_retained_object_role_is_rejected(self) -> None:
        proposal, blocks, span_of, dag_ref = self._reference_dag()
        # The composite output DAG carries a retained_object role bound to a
        # source_relation node; graft the verified protocol_reference node
        # into that role and reseal honestly.
        dag_out = self._build(proposal, OUT_STATE, span_of)
        root = dag_out["nodes"][dag_out["root_id"]]
        self.assertIn("retained_object",
                      {premise["role"] for premise in root["premises"]})
        merged = deepcopy(dag_out)
        for node_id, node in dag_ref["nodes"].items():
            merged["nodes"].setdefault(node_id, deepcopy(node))
        self.assertEqual(self._verify(proposal, merged, span_of), "")
        medium_id = self._protocol_reference_node_id(merged)

        def substitute(node):
            for premise in node["premises"]:
                if premise["role"] == "retained_object":
                    premise["node_id"] = medium_id

        tampered = _reseal(merged, merged["root_id"], substitute)
        self.assertEqual(
            self._verify(proposal, tampered, span_of),
            "proof_dag_retained_object_binding_mismatch",
        )
        self.assertEqual(self._verify(proposal, merged, span_of), "")


if __name__ == "__main__":
    unittest.main()
