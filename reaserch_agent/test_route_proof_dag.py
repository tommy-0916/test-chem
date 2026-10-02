"""Typed state-proof DAG: build, verify, invalidate, forge, cycle, leaves.

The two-step fixture mirrors the real r7 chain: a composite
"centrifugation−redispersion protocol" step whose output state
(retained_wet_solid) is proven only via CENTRIFUGE_COLLECT_PRECIPITATE_V1
standing on a post-operation retained-object record, then a redispersion
step whose input state is that same retained_wet_solid carried by
``material_origin="upstream_output"``.  The flat engine cannot prove the
second step's output because its parent premise accepts only literal gates;
the DAG composes the parent output's own proof node instead.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from chem_agent_contracts import route_convention_basis
from chem_agent_contracts.route_convention_basis import (
    _resolve_fact_provenance,
    convention_fact_evidence_by_id,
    derive_unreviewed_output_state,
    verify_bound_output_state,
)
from chem_agent_contracts.route_field_basis import state_source_locally_attributed
from chem_agent_contracts.route_proof_dag import (
    STATE_PROOF_DAG_SCHEMA,
    StateProofDagVerifier,
    build_state_proof_dag,
    leaf_id_for,
    node_id_for,
    verify_state_proof_dag,
)
from chem_agent_contracts.route_retained_object import (
    build_excerpt_span_resolver,
    build_retained_object_resolver,
)

OUT_STATE = "material_graph[0].material_outputs[0].state"
OUT_NAME = "material_graph[0].material_outputs[0].name"
STEP1_INPUT_STATE = "material_graph[1].material_inputs[0].state"
STEP1_OUT_STATE = "material_graph[1].material_outputs[0].state"

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
OP2_SENTENCE = (
    "The LDH seeds were dispersed in 30 mL of water and aged for 20 h at "
    "room temperature."
)
OP2_EXCERPT = "were dispersed in 30 mL of water and aged for 20 h"
# The second step's input state quote never names the state word: the flat
# literal gates cannot prove it, only inheritance through the DAG can.
IN2_STATE_EXCERPT = (
    "The precipitates were labeled as LDH seeds, which were dispersed in 30"
)


def _blocks(*texts: str) -> list[tuple[str, str]]:
    return [
        (f"pdf:p1:b{index}-p1:b{index}", text)
        for index, text in enumerate(texts, start=1)
    ]


def _reseal(dag: dict, target_id: str, mutate) -> dict:
    """Mutate one node, then recompute every dependent content address.

    A determined forger who can run this module's own hashing must reseal
    the whole premise chain up to the root after touching any node; this
    helper performs exactly that cascade.
    """
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


class ProofDagFixtureMixin(unittest.TestCase):
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

    def _two_step_proposal(self) -> dict:
        """graph[0] composite retained_wet_solid; graph[1] redispersion."""
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
            step_id="S2", sequence=2, operation="dispersed",
            input_port={
                "material_id": "product", "material_instance_id": "inst_b",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "upstream_output",
                "parent_output_refs": [{
                    "macro_step_id": "S1", "material_instance_id": "inst_b",
                }],
                "provenance": {"kind": "paper", "reference": "fact:in2_name"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_c",
                "name": "LDH seeds", "state": "suspension",
                "provenance": {"kind": "paper", "reference": "fact:out2_name"},
            },
            relation_provenance_ref="fact:op2",
        )
        facts = [
            self._fact("op", "material_graph[0].operation", OP_VALUE, OP_EXCERPT),
            self._fact("in_name", "material_graph[0].material_inputs[0].name",
                       "suspension", FILLER),
            self._fact("in_state", "material_graph[0].material_inputs[0].state",
                       "suspension", FILLER),
            self._fact("out_name", OUT_NAME, "LDH seeds", NAMING_SENTENCE),
            self._fact("out_state", OUT_STATE, "retained_wet_solid", OP_EXCERPT),
            self._fact("op2", "material_graph[1].operation",
                       "dispersed", OP2_EXCERPT),
            self._fact("in2_name", "material_graph[1].material_inputs[0].name",
                       "LDH seeds", NAMING_SENTENCE),
            self._fact("in2_state", STEP1_INPUT_STATE,
                       "retained_wet_solid", IN2_STATE_EXCERPT),
            self._fact("out2_name", "material_graph[1].material_outputs[0].name",
                       "LDH seeds", NAMING_SENTENCE),
            self._fact("out2_state", STEP1_OUT_STATE, "suspension", OP2_EXCERPT),
        ]
        return {"material_graph": [step_1, step_2], "route_facts": facts}

    def _chain(self):
        """The two-step proposal with its signed blocks and span resolver."""
        proposal = self._two_step_proposal()
        blocks = _blocks(FILLER, OP_SENTENCE, NAMING_SENTENCE, OP2_SENTENCE)
        span_of = build_excerpt_span_resolver(blocks)
        return proposal, blocks, span_of

    def _kwargs(self):
        return {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "source_digest": self.digest,
        }

    def _build(self, proposal, path, span_of):
        dag, issue = build_state_proof_dag(
            proposal["material_graph"], proposal["route_facts"], path,
            span_of=span_of, **self._kwargs(),
        )
        self.assertEqual(issue, "")
        self.assertIsNotNone(dag)
        return dag

    def _verify(self, proposal, dag, span_of, blocks=None):
        return verify_state_proof_dag(
            dag, proposal["material_graph"], proposal["route_facts"],
            span_of=span_of, blocks=blocks, **self._kwargs(),
        )


class CompositeOutputDagTest(ProofDagFixtureMixin):
    """Acceptance 1: the graph[5]-style composite output state DAG."""

    def test_composite_output_dag_builds_and_verifies(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, OUT_STATE, span_of)
        self.assertEqual(dag["schema_version"], STATE_PROOF_DAG_SCHEMA)
        root = dag["nodes"][dag["root_id"]]
        self.assertEqual(root["node_type"], "state_change")
        self.assertEqual(root["schema_version"],
                         "state-change-convention-proof/v1")
        self.assertEqual(root["rule_id"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1")
        self.assertEqual(root["rule_version"], "1.1.0")
        self.assertEqual(root["claim"]["target_state"], "retained_wet_solid")
        self.assertEqual(root["claim"]["material_instance_id"], "inst_b")
        roles = {premise["role"]: premise["node_id"]
                 for premise in root["premises"]}
        self.assertEqual(set(roles), {"parent_state", "operation",
                                      "retained_object"})
        parent = dag["nodes"][roles["parent_state"]]
        self.assertEqual(parent["node_type"], "paper_literal")
        self.assertEqual(parent["leaf"]["field_path"],
                         "material_graph[0].material_inputs[0].state")
        self.assertEqual(parent["leaf"]["claim_value"], "suspension")
        operation = dag["nodes"][roles["operation"]]
        self.assertEqual(operation["node_type"], "paper_literal")
        self.assertEqual(operation["leaf"]["field_path"],
                         "material_graph[0].operation")
        relation = dag["nodes"][roles["retained_object"]]
        self.assertEqual(relation["node_type"], "source_relation")
        self.assertEqual(relation["subtype"], "post_operation_retained_object")
        self.assertEqual(relation["record"]["retained_object"], "precipitate")
        relation_roles = {premise["role"] for premise in relation["premises"]}
        self.assertEqual(relation_roles, {"operation", "naming", "output_name"})
        # The naming and output-name roles dedupe onto one leaf node; the
        # operation role dedupe onto the root's own operation leaf.
        relation_premises = {premise["role"]: premise["node_id"]
                             for premise in relation["premises"]}
        self.assertEqual(relation_premises["naming"],
                         relation_premises["output_name"])
        self.assertEqual(relation_premises["operation"], roles["operation"])
        self.assertEqual(len(dag["nodes"]), 5)
        self.assertEqual(self._verify(proposal, dag, span_of), "")
        self.assertEqual(self._verify(proposal, dag, span_of, blocks=blocks), "")


class TwoStepChainDagTest(ProofDagFixtureMixin):
    """Acceptances 2-4: inheritance input node and redispersion output node."""

    def test_input_state_dag_is_inheritance_rooted(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_INPUT_STATE, span_of)
        root = dag["nodes"][dag["root_id"]]
        self.assertEqual(root["node_type"], "inheritance")
        self.assertEqual(root["schema_version"], "state-inheritance-proof/v1")
        self.assertEqual(root["rule_id"], "PARENT_OUTPUT_STATE_INHERITANCE_V1")
        self.assertEqual(root["claim"]["target_state"], "retained_wet_solid")
        self.assertEqual(root["parent_ref"], {
            "kind": "material_instance",
            "macro_step_id": "S1", "material_instance_id": "inst_b",
        })
        self.assertEqual(root["parent_state_path"], OUT_STATE)
        parent = dag["nodes"][root["premises"][0]["node_id"]]
        self.assertEqual(parent["node_type"], "state_change")
        self.assertEqual(parent["rule_id"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1")
        self.assertEqual(self._verify(proposal, dag, span_of), "")
        self.assertEqual(self._verify(proposal, dag, span_of, blocks=blocks), "")

    def test_output_state_dag_composes_parent_proof(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        root = dag["nodes"][dag["root_id"]]
        self.assertEqual(root["node_type"], "state_change")
        self.assertEqual(root["rule_id"], "REDISPERSION_V1")
        self.assertEqual(root["rule_version"], "1.1.0")
        self.assertEqual(root["claim"]["target_state"], "suspension")
        roles = {premise["role"]: premise["node_id"]
                 for premise in root["premises"]}
        self.assertEqual(set(roles), {"parent_state", "operation"})
        # The parent premise is the composite step's own proven output node.
        parent = dag["nodes"][roles["parent_state"]]
        self.assertEqual(parent["node_type"], "state_change")
        self.assertEqual(parent["rule_id"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1")
        self.assertEqual(parent["claim"]["field_path"], OUT_STATE)
        self.assertEqual(self._verify(proposal, dag, span_of), "")
        self.assertEqual(self._verify(proposal, dag, span_of, blocks=blocks), "")

    def test_no_literal_retained_wet_solid_fact_exists(self) -> None:
        proposal, _blocks_unused, span_of = self._chain()
        in2_fact = next(fact for fact in proposal["route_facts"]
                        if fact["fact_id"] == "in2_state")
        self.assertNotIn("retained_wet_solid", in2_fact["excerpt"])
        self.assertFalse(state_source_locally_attributed(
            "retained_wet_solid", in2_fact["excerpt"], "LDH seeds",
        ))
        self.assertTrue(all(
            "retained_wet_solid" not in fact["excerpt"]
            for fact in proposal["route_facts"]
        ))
        # The flat engine fails exactly here; the DAG succeeds.
        proof, issue = derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"],
            STEP1_OUT_STATE, **self._kwargs(),
        )
        self.assertIsNone(proof)
        self.assertEqual(issue, "convention_parent_state_unverified")
        for path in (STEP1_INPUT_STATE, STEP1_OUT_STATE):
            dag = self._build(proposal, path, span_of)
            self.assertFalse(any(
                node["node_type"] == "paper_literal"
                and node["leaf"]["field_path"] == STEP1_INPUT_STATE
                for node in dag["nodes"].values()
            ))


class SameStateNodeTest(ProofDagFixtureMixin):
    """A same-state (TRANSFER_V1) rule produces a same_state node."""

    def _transfer_proposal(self) -> dict:
        quote_1 = ("In this transfer route, we transfer feed A as a "
                   "solution to the vial.")
        quote_2 = "The vial content was mixed at room temperature."
        step_1 = {
            "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
            "operation": "transfer", "sample_id": "sample-A",
            "provenance": {"kind": "paper", "reference": "fact:op1"},
            "material_inputs": [{
                "material_id": "solutionA", "material_instance_id": "feed1",
                "name": "feed A", "state": "solution",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:in1_name"},
            }],
            "material_outputs": [{
                "material_id": "solutionA", "material_instance_id": "child1",
                "name": "feed A", "state": "solution",
                "provenance": {"kind": "paper", "reference": "fact:out1_name"},
            }],
            "operation_segments": [{
                "segment_id": "S1-seg1", "material_effect": "transfer_material",
                "source_operation_ref": "S1",
                "provenance": {"kind": "paper", "reference": "fact:op1"},
            }],
            "material_relations": [{
                "relation_id": "S1-rel1", "event_kind": "process_same_material",
                "input_material_instance_ids": ["feed1"],
                "output_material_instance_ids": ["child1"],
                "quantity_basis": "whole_batch",
                "source_operation_ref": "S1-seg1",
                "provenance": {"kind": "paper", "reference": "fact:op1"},
            }],
            "lineage_relation": {
                "relation_type": "transfer_of",
                "parent_material_instance_ids": ["feed1"],
                "child_material_instance_ids": ["child1"],
            },
        }
        facts = [
            self._fact("op1", "material_graph[0].operation", "transfer", quote_1),
            self._fact("in1_name", "material_graph[0].material_inputs[0].name",
                       "feed A", quote_1),
            self._fact("in1_state", "material_graph[0].material_inputs[0].state",
                       "solution", quote_1),
            self._fact("out1_name", "material_graph[0].material_outputs[0].name",
                       "feed A", quote_1),
            self._fact("out1_state", "material_graph[0].material_outputs[0].state",
                       "solution", quote_1),
            self._fact("op2", "material_graph[1].operation", "mixed", quote_2),
        ]
        return {"material_graph": [step_1], "route_facts": facts}

    def test_transfer_output_builds_same_state_node(self) -> None:
        proposal = self._transfer_proposal()
        quote_1 = proposal["route_facts"][0]["excerpt"]
        quote_2 = proposal["route_facts"][5]["excerpt"]
        blocks = _blocks(quote_1, quote_2)
        span_of = build_excerpt_span_resolver(blocks)
        dag = self._build(
            proposal, "material_graph[0].material_outputs[0].state", span_of,
        )
        root = dag["nodes"][dag["root_id"]]
        self.assertEqual(root["node_type"], "same_state")
        self.assertEqual(root["schema_version"],
                         "same-state-convention-proof/v1")
        self.assertEqual(root["rule_id"], "TRANSFER_V1")
        self.assertEqual({premise["role"] for premise in root["premises"]},
                         {"parent_state", "operation"})
        self.assertEqual(self._verify(proposal, dag, span_of), "")
        self.assertEqual(self._verify(proposal, dag, span_of, blocks=blocks), "")


class InvalidationTest(ProofDagFixtureMixin):
    """Acceptance 5: source text, naming text, or rule bytes invalidate all."""

    def test_operation_excerpt_change_invalidates_whole_chain(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        old_root_id = dag["root_id"]
        modified = deepcopy(proposal)
        for fact in modified["route_facts"]:
            if fact["fact_id"] in {"op", "out_state"}:
                fact["excerpt"] = OP_SENTENCE
        # The old DAG no longer verifies against the changed facts, and the
        # failure invalidates the whole chain (downstream nodes included).
        self.assertNotEqual(self._verify(modified, dag, span_of), "")
        rebuilt, issue = build_state_proof_dag(
            modified["material_graph"], modified["route_facts"],
            STEP1_OUT_STATE, span_of=span_of, **self._kwargs(),
        )
        self.assertEqual(issue, "")
        self.assertNotEqual(rebuilt["root_id"], old_root_id)
        old_op_leaf = next(
            node["leaf"]["leaf_id"] for node in dag["nodes"].values()
            if node["node_type"] == "paper_literal"
            and node["leaf"]["field_path"] == "material_graph[0].operation"
        )
        new_op_leaf = next(
            node["leaf"]["leaf_id"] for node in rebuilt["nodes"].values()
            if node["node_type"] == "paper_literal"
            and node["leaf"]["field_path"] == "material_graph[0].operation"
        )
        self.assertNotEqual(old_op_leaf, new_op_leaf)
        self.assertEqual(self._verify(modified, rebuilt, span_of), "")
        # The old DAG still verifies against the unchanged facts.
        self.assertEqual(self._verify(proposal, dag, span_of), "")

    def test_naming_excerpt_change_invalidates_whole_chain(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        modified = deepcopy(proposal)
        for fact in modified["route_facts"]:
            if fact["fact_id"] == "out_name":
                fact["excerpt"] = "The precipitates were labeled as LDH seeds"
        self.assertNotEqual(self._verify(modified, dag, span_of), "")
        rebuilt, issue = build_state_proof_dag(
            modified["material_graph"], modified["route_facts"],
            STEP1_OUT_STATE, span_of=span_of, **self._kwargs(),
        )
        self.assertEqual(issue, "")
        relation_ids = {
            node["node_id"] for node in dag["nodes"].values()
            if node["node_type"] == "source_relation"
        }
        self.assertTrue(all(
            node["node_id"] not in relation_ids
            for node in rebuilt["nodes"].values()
        ))
        self.assertNotEqual(rebuilt["root_id"], dag["root_id"])
        self.assertEqual(self._verify(modified, rebuilt, span_of), "")

    def test_rule_resource_change_invalidates_whole_chain(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        real = route_convention_basis._rule_resource
        rules, digest = real()
        forged = (rules, "sha256_" + "0" * 64)
        self.assertNotEqual(digest, forged[1])
        with mock.patch.object(
            route_convention_basis, "_rule_resource", return_value=forged,
        ):
            # The pinned context no longer matches: every layer, including
            # the downstream inheritance/state-change nodes, is invalid.
            self.assertEqual(
                self._verify(proposal, dag, span_of),
                "proof_dag_context_mismatch",
            )
        self.assertEqual(self._verify(proposal, dag, span_of), "")


class ForgeryTest(ProofDagFixtureMixin):
    """Acceptance 6: forged nodes and tampered premise ids fail closed."""

    def test_forged_upstream_node_fails_id_recompute(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        forged_dag = deepcopy(dag)
        donor = next(node for node in forged_dag["nodes"].values()
                     if node["node_type"] == "paper_literal")
        forged = deepcopy(donor)
        forged["claim"]["material_instance_id"] = "inst_zzz"
        forged["leaf"]["claim_value"] = "powder"
        # Valid shape, invented content, stale content address.
        forged_dag["nodes"][forged["node_id"]] = forged
        self.assertEqual(
            self._verify(proposal, forged_dag, span_of),
            "proof_dag_node_id_mismatch",
        )

    def test_dangling_premise_id_is_missing(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        tampered = deepcopy(dag)
        root = tampered["nodes"][tampered["root_id"]]
        root["premises"][0]["node_id"] = "proof_node_" + "f" * 24
        self.assertEqual(
            self._verify(proposal, tampered, span_of),
            "proof_dag_premise_missing",
        )


class CycleAndFutureTest(ProofDagFixtureMixin):
    """Acceptance 7: future references and premise cycles fail closed."""

    def test_inheritance_from_later_step_is_future_reference(self) -> None:
        proposal, blocks, span_of = self._chain()
        later = self._state_change_step(
            step_id="S3", sequence=3, operation="held",
            input_port={
                "material_id": "product", "material_instance_id": "inst_z",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:op3"},
            },
            output_port={
                "material_id": "product", "material_instance_id": "inst_late",
                "name": "LDH seeds", "state": "retained_wet_solid",
                "provenance": {"kind": "paper", "reference": "fact:op3"},
            },
            relation_provenance_ref="fact:op3",
        )
        proposal["material_graph"].append(later)
        proposal["material_graph"][1]["material_inputs"][0][
            "parent_output_refs"
        ] = [{"macro_step_id": "S3", "material_instance_id": "inst_late"}]
        dag, issue = build_state_proof_dag(
            proposal["material_graph"], proposal["route_facts"],
            STEP1_INPUT_STATE, span_of=span_of, **self._kwargs(),
        )
        self.assertIsNone(dag)
        self.assertEqual(issue, "proof_dag_future_reference")

    def test_mutual_premise_cycle_is_rejected(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, OUT_STATE, span_of)
        cycled = {
            "schema_version": dag["schema_version"],
            "context": dag["context"],
            "root_id": "proof_node_" + "a" * 24,
            "nodes": {
                "proof_node_" + "a" * 24: {
                    "schema_version": "paper-literal-proof/v1",
                    "node_type": "paper_literal",
                    "node_id": "proof_node_" + "a" * 24,
                    "claim": {"field_path": "", "target_state": "",
                              "material_id": "", "material_instance_id": ""},
                    "premises": [{"role": "parent_state",
                                  "node_id": "proof_node_" + "b" * 24}],
                    "rule_id": "", "rule_version": "",
                    "rule_resource_digest": "",
                },
                "proof_node_" + "b" * 24: {
                    "schema_version": "paper-literal-proof/v1",
                    "node_type": "paper_literal",
                    "node_id": "proof_node_" + "b" * 24,
                    "claim": {"field_path": "", "target_state": "",
                              "material_id": "", "material_instance_id": ""},
                    "premises": [{"role": "parent_state",
                                  "node_id": "proof_node_" + "a" * 24}],
                    "rule_id": "", "rule_version": "",
                    "rule_resource_digest": "",
                },
            },
        }
        self.assertEqual(
            self._verify(proposal, cycled, span_of),
            "proof_dag_cycle",
        )


class LeafRelocationTest(ProofDagFixtureMixin):
    """Acceptance 8: leaves relocate from the signed blocks alone."""

    def test_every_leaf_relocates_from_blocks(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        # The verifier is handed blocks only: it rebuilds its own span
        # resolver and re-extracts every leaf from the projection.
        verifier = StateProofDagVerifier(
            proposal["material_graph"], proposal["route_facts"],
            blocks=blocks, **self._kwargs(),
        )
        self.assertEqual(verifier.verify(dag), "")
        leaves = [
            node["leaf"] for node in dag["nodes"].values()
            if node["node_type"] == "paper_literal"
        ]
        self.assertTrue(leaves)
        for leaf in leaves:
            self.assertTrue(leaf["locator"])
            self.assertEqual(len(leaf["char_span"]), 2)

    def test_tampered_leaf_span_or_digest_fails(self) -> None:
        proposal, blocks, span_of = self._chain()
        dag = self._build(proposal, STEP1_OUT_STATE, span_of)
        target = next(
            node["node_id"] for node in dag["nodes"].values()
            if node["node_type"] == "paper_literal"
            and node["leaf"]["field_path"] == "material_graph[0].operation"
        )
        # Unresealed tampering breaks the content address itself.
        tampered = deepcopy(dag)
        tampered["nodes"][target]["leaf"]["char_span"] = [0, 5]
        self.assertEqual(
            self._verify(proposal, tampered, span_of, blocks=blocks),
            "proof_dag_node_id_mismatch",
        )
        # A resealed forgery still fails at source relocation.
        for mutate in (
            lambda node: node["leaf"].update(char_span=[0, 5]),
            lambda node: node["leaf"].update(
                excerpt_digest="sha256_" + "0" * 64),
            lambda node: node["leaf"].update(locator="pdf:p9:b9-p9:b9"),
        ):
            forged = _reseal(dag, target, mutate)
            self.assertEqual(
                self._verify(proposal, forged, span_of, blocks=blocks),
                "proof_dag_leaf_relocation_mismatch",
            )


class LegacyRegressionTest(ProofDagFixtureMixin):
    """Acceptance 9: flat legacy proofs are untouched by the DAG layer."""

    def test_flat_proof_still_derives_and_verifies(self) -> None:
        proposal, blocks, span_of = self._chain()
        resolver = build_retained_object_resolver(
            proposal["material_graph"], proposal["route_facts"], blocks,
        )
        proof, issue = derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"], OUT_STATE,
            retained_object_resolver=resolver, **self._kwargs(),
        )
        self.assertEqual(issue, "")
        self.assertEqual(proof["schema_version"], "route-convention-state/v1")
        self.assertEqual(proof["rule_id"], "CENTRIFUGE_COLLECT_PRECIPITATE_V1")
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

    def test_dag_nodes_never_use_legacy_proof_schema(self) -> None:
        proposal, blocks, span_of = self._chain()
        for path in (OUT_STATE, STEP1_INPUT_STATE, STEP1_OUT_STATE):
            dag = self._build(proposal, path, span_of)
            self.assertNotIn(
                "route-convention-state/v1",
                json.dumps(dag["nodes"], ensure_ascii=False),
            )


if __name__ == "__main__":
    unittest.main()
