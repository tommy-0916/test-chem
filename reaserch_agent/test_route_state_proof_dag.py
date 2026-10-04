"""G1 adapter and consumption-point tests for state-proof-dag/v1.

The synthetic fixtures mirror ``test_route_proof_dag.py``: a composite
"centrifugation−redispersion protocol" step whose output state is proven
only via the retained-object convention, then a redispersion step whose
output the FLAT engine cannot prove (its parent premise accepts only
literal gates) — the DAG-exclusive multi-hop case.  Tests cover the shared
adapter (build + dual verify + JSON-serializable entries + mechanical
BLOCKED attribution), the consumption guard (forged entries and 3E
diagnostic records rejected), and both G1 consumption points (extraction's
parallel ``convention_state_proof_dags`` key with untouched flat
candidates; the receipt's DAG rescue after flat derivations AND literal
gates fail, classified as ``dag_proven_state_field_paths``).
"""

from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest import mock

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_group_fact_receipt import (
    _state_derivation_proof,
    produce_pdf_group_fact_receipt,
)
from reaserch_agent.route_pdf_group_extraction import (
    _prepare_unsigned_proposal,
)
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1,
    PdfSourceBlockV1,
)
from reaserch_agent.route_state_proof_dag import (
    STATE_PROOF_DAG_DERIVATION,
    build_verified_state_proof_dags,
    dag_entry_derivation,
)

OUT_STATE = "material_graph[0].material_outputs[0].state"
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
IN2_STATE_EXCERPT = (
    "The precipitates were labeled as LDH seeds, which were dispersed in 30"
)

BLOCK_1 = "pdf:p1:b1-p1:b1"
BLOCK_2 = "pdf:p1:b2-p1:b2"
BLOCK_3 = "pdf:p1:b3-p1:b3"
BLOCK_4 = "pdf:p1:b4-p1:b4"

DIGEST = "sha256_" + "a" * 64
SCOPE = {
    "paper_id": "paper-A",
    "experimental_group_id": "Group A",
    "source_digest": DIGEST,
}


def _blocks() -> list[tuple[str, str]]:
    return [
        (BLOCK_1, FILLER),
        (BLOCK_2, OP_SENTENCE),
        (BLOCK_3, NAMING_SENTENCE),
        (BLOCK_4, OP2_SENTENCE),
    ]


def _fact(fact_id: str, path: str, value: str, excerpt: str,
          locator: str) -> dict:
    return {
        "fact_id": fact_id, "field_path": path, "value": value,
        "unit": "", "required": True, "excerpt": excerpt,
        "source": {
            "paper_id": SCOPE["paper_id"],
            "experimental_group_id": SCOPE["experimental_group_id"],
            "section": "Methods",
            "locator": locator,
            "source_digest": SCOPE["source_digest"],
        },
    }


def _state_change_step(*, step_id: str, sequence: int, operation: str,
                       input_port: dict, output_port: dict,
                       relation_provenance_ref: str) -> dict:
    return {
        "macro_step_id": step_id, "macro_action_id": "A1",
        "sequence": sequence, "operation": operation, "sample_id": "sample-A",
        "provenance": {"kind": "paper", "reference": relation_provenance_ref},
        "material_inputs": [input_port],
        "material_outputs": [output_port],
        "operation_segments": [{
            "segment_id": f"{step_id}-seg1",
            "material_effect": "transform_material",
            "source_operation_ref": step_id,
            "provenance": {"kind": "paper",
                           "reference": relation_provenance_ref},
        }],
        "material_relations": [{
            "relation_id": f"{step_id}-rel1", "event_kind": "state_change",
            "input_material_instance_ids": [input_port["material_instance_id"]],
            "output_material_instance_ids": [
                output_port["material_instance_id"]],
            "quantity_basis": "whole_batch",
            "source_operation_ref": f"{step_id}-seg1",
            "provenance": {"kind": "paper",
                           "reference": relation_provenance_ref},
        }],
        "lineage_relation": {
            "relation_type": "state_change_of",
            "parent_material_instance_ids": [input_port["material_instance_id"]],
            "child_material_instance_ids": [output_port["material_instance_id"]],
        },
    }


def _two_step_proposal() -> dict:
    """graph[0] composite retained_wet_solid; graph[1] redispersion."""
    step_1 = _state_change_step(
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
    step_2 = _state_change_step(
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
        _fact("op", "material_graph[0].operation", OP_VALUE, OP_EXCERPT,
              BLOCK_2),
        _fact("in_name", "material_graph[0].material_inputs[0].name",
              "suspension", FILLER, BLOCK_1),
        _fact("in_state", "material_graph[0].material_inputs[0].state",
              "suspension", FILLER, BLOCK_1),
        _fact("out_name", "material_graph[0].material_outputs[0].name",
              "LDH seeds", NAMING_SENTENCE, BLOCK_3),
        _fact("out_state", OUT_STATE, "retained_wet_solid", OP_EXCERPT,
              BLOCK_2),
        _fact("op2", "material_graph[1].operation", "dispersed", OP2_EXCERPT,
              BLOCK_4),
        _fact("in2_name", "material_graph[1].material_inputs[0].name",
              "LDH seeds", NAMING_SENTENCE, BLOCK_3),
        _fact("in2_state", STEP1_INPUT_STATE, "retained_wet_solid",
              IN2_STATE_EXCERPT, BLOCK_3),
        _fact("out2_name", "material_graph[1].material_outputs[0].name",
              "LDH seeds", NAMING_SENTENCE, BLOCK_3),
        _fact("out2_state", STEP1_OUT_STATE, "suspension", OP2_EXCERPT,
              BLOCK_4),
    ]
    return {
        "source_group_ref": dict(SCOPE),
        "material_graph": [step_1, step_2],
        "route_facts": facts,
    }


def _group() -> PdfExperimentalGroupV1:
    return PdfExperimentalGroupV1(
        source_scope=ExperimentalGroupScopeV1(
            paper_id=SCOPE["paper_id"],
            experimental_group_id=SCOPE["experimental_group_id"],
            section="Methods", locator="pdf:p1:b1-p1:b4",
            source_digest=SCOPE["source_digest"],
        ),
        source_document="/trusted/paper.pdf",
        blocks=tuple(
            PdfSourceBlockV1(locator, text) for locator, text in _blocks()),
    )


def _protocol() -> dict:
    proposal = _two_step_proposal()
    return {
        "paper_id": SCOPE["paper_id"],
        "experimental_group_id": SCOPE["experimental_group_id"],
        "group_role": "unclassified",
        "role_hint": "synthesis",
        "source": {
            "source_document": "/trusted/paper.pdf",
            "section": "Methods",
            "locator": "pdf:p1:b1-p1:b4",
            "source_digest": SCOPE["source_digest"],
        },
        "material_graph": proposal["material_graph"],
        "route_facts": proposal["route_facts"],
    }


class AdapterBuildVerifyTest(unittest.TestCase):
    """The shared adapter builds and dual-verifies every .state path."""

    def test_entries_cover_all_state_paths_and_dual_verify(self) -> None:
        proposal = _two_step_proposal()
        results = build_verified_state_proof_dags(
            proposal["material_graph"], proposal["route_facts"], **SCOPE,
            blocks=_blocks(),
        )
        self.assertEqual(set(results), {
            "material_graph[0].material_inputs[0].state",
            OUT_STATE, STEP1_INPUT_STATE, STEP1_OUT_STATE,
        })
        for path in (
            "material_graph[0].material_inputs[0].state",
            OUT_STATE, STEP1_INPUT_STATE, STEP1_OUT_STATE,
        ):
            entry = results[path]
            self.assertEqual(entry["verdict"], "PASS", path)
            self.assertEqual(entry["attribution"], "proven", path)
            self.assertEqual(entry["build_issue"], "", path)
            self.assertEqual(entry["verify_with_span_resolver"], "", path)
            self.assertEqual(entry["verify_blocks_only"], "", path)
            self.assertEqual(
                entry["dag"]["schema_version"], "state-proof-dag/v1", path)
            self.assertEqual(
                entry["derivation"]["derivation"], STATE_PROOF_DAG_DERIVATION,
                path)
            self.assertEqual(entry["derivation"]["field_path"], path, path)
        # The DAG-exclusive multi-hop proof: the redispersion output stands
        # on the input-inheritance node standing on the composite output.
        root = results[STEP1_OUT_STATE]["dag"][
            "nodes"][results[STEP1_OUT_STATE]["dag"]["root_id"]]
        self.assertEqual(root["node_type"], "state_change")
        self.assertEqual(
            results[STEP1_OUT_STATE]["derivation"]["target_state"],
            "suspension")
        # Entries are plain JSON-serializable dicts.
        json.dumps(results, ensure_ascii=False)

    def test_flat_engine_alone_cannot_prove_the_redispersion_output(self):
        # The value point: without the DAG the parent premise accepts only
        # literal gates, so graph[1].out stays unproven pre-G1.
        proposal = _two_step_proposal()
        from chem_agent_contracts.route_convention_basis import (
            derive_unreviewed_output_state,
        )
        from chem_agent_contracts.route_retained_object import (
            build_retained_object_resolver,
        )
        resolver = build_retained_object_resolver(
            proposal["material_graph"], proposal["route_facts"], _blocks())
        proof, issue = derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"],
            STEP1_OUT_STATE, **SCOPE, retained_object_resolver=resolver,
        )
        self.assertIsNone(proof)
        self.assertEqual(issue, "convention_parent_state_unverified")

    def test_invalid_inputs_return_empty(self) -> None:
        self.assertEqual(build_verified_state_proof_dags(
            None, [], **SCOPE, blocks=_blocks()), {})
        self.assertEqual(build_verified_state_proof_dags(
            [], [], **SCOPE, blocks=[]), {})

    def test_source_mutation_breaks_relocation(self) -> None:
        proposal = _two_step_proposal()
        results = build_verified_state_proof_dags(
            proposal["material_graph"], proposal["route_facts"], **SCOPE,
            blocks=_blocks(),
        )
        mutated = [
            (locator, "MUTATED " + text) if locator == BLOCK_2
            else (locator, text)
            for locator, text in _blocks()
        ]
        from chem_agent_contracts.route_proof_dag import StateProofDagVerifier
        issue = StateProofDagVerifier(
            proposal["material_graph"], proposal["route_facts"], **SCOPE,
            blocks=mutated,
        ).verify(results[OUT_STATE]["dag"])
        self.assertEqual(issue, "proof_dag_leaf_relocation_mismatch")

    def test_wrong_scope_is_context_mismatch(self) -> None:
        proposal = _two_step_proposal()
        results = build_verified_state_proof_dags(
            proposal["material_graph"], proposal["route_facts"], **SCOPE,
            blocks=_blocks(),
        )
        from chem_agent_contracts.route_proof_dag import StateProofDagVerifier
        issue = StateProofDagVerifier(
            proposal["material_graph"], proposal["route_facts"],
            paper_id=SCOPE["paper_id"], experimental_group_id="Group B",
            source_digest=SCOPE["source_digest"], blocks=_blocks(),
        ).verify(results[OUT_STATE]["dag"])
        self.assertEqual(issue, "proof_dag_context_mismatch")


class DagEntryDerivationGuardTest(unittest.TestCase):
    """The consumption guard accepts only honest dual-verified PASS entries."""

    def _pass_entry(self) -> dict:
        proposal = _two_step_proposal()
        return build_verified_state_proof_dags(
            proposal["material_graph"], proposal["route_facts"], **SCOPE,
            blocks=_blocks(),
        )[STEP1_OUT_STATE]

    def test_pass_entry_yields_marked_derivation(self) -> None:
        derivation = dag_entry_derivation(self._pass_entry())
        self.assertIsNotNone(derivation)
        self.assertEqual(derivation["derivation"], STATE_PROOF_DAG_DERIVATION)
        self.assertEqual(derivation["field_path"], STEP1_OUT_STATE)
        self.assertEqual(derivation["target_state"], "suspension")

    def test_blocked_entry_yields_nothing(self) -> None:
        proposal = _two_step_proposal()
        results = build_verified_state_proof_dags(
            proposal["material_graph"], proposal["route_facts"], **SCOPE,
            blocks=_blocks(),
        )
        entry = dict(results[STEP1_OUT_STATE])
        entry["verdict"] = "BLOCKED"
        self.assertIsNone(dag_entry_derivation(entry))

    def test_forged_verdict_and_cross_path_substitution_rejected(self) -> None:
        entry = self._pass_entry()
        forged = dict(entry)
        forged["derivation"] = dict(entry["derivation"])
        forged["derivation"]["proof_dag_root_id"] = "proof_node_" + "0" * 24
        self.assertIsNone(dag_entry_derivation(forged))
        cross_path = dict(entry)
        cross_path["field_path"] = OUT_STATE
        self.assertIsNone(dag_entry_derivation(cross_path))
        dirty_verify = dict(entry)
        dirty_verify["verify_blocks_only"] = "proof_dag_node_mismatch"
        self.assertIsNone(dag_entry_derivation(dirty_verify))

    def test_diagnostic_records_rejected(self) -> None:
        entry = self._pass_entry()
        # 3E diagnostic record in record_to_dict form: never a proof DAG.
        diagnostic = dict(entry)
        diagnostic["dag"] = {
            "schema_version": "operation-precondition-diagnostic/v1",
            "diagnostics_only": True,
            "feeds_verdict": False,
            "conclusion": "insufficient",
        }
        self.assertIsNone(dag_entry_derivation(diagnostic))
        # A non-Mapping artifact (e.g. a DiagnosticRecordV1 instance) too.
        not_mapping = dict(entry)
        not_mapping["dag"] = object()
        self.assertIsNone(dag_entry_derivation(not_mapping))
        self.assertIsNone(dag_entry_derivation(None))
        self.assertIsNone(dag_entry_derivation("state_proof_dag_v1"))


class ExtractionConsumptionPointTest(unittest.TestCase):
    """Extraction: parallel DAG key, flat candidates byte-unchanged."""

    def test_prepare_unsigned_proposal_adds_dag_key_and_keeps_flat(self) -> None:
        proposal = _two_step_proposal()
        normalizations = _prepare_unsigned_proposal(proposal, _group())
        self.assertIn("convention_state_proof_dags", normalizations)
        rows = {
            row["field_path"]: row
            for row in normalizations["convention_state_proof_dags"]
        }
        self.assertEqual(rows[STEP1_OUT_STATE]["verdict"], "PASS")
        self.assertEqual(
            rows[STEP1_OUT_STATE]["status"], "unreviewed_prerequisites_only")
        # The flat loop is untouched: it cannot derive the DAG-exclusive
        # redispersion output, but proves the composite output.
        flat_paths = {
            row["field_path"]
            for row in normalizations["convention_state_candidates"]
        }
        self.assertIn(OUT_STATE, flat_paths)
        self.assertNotIn(STEP1_OUT_STATE, flat_paths)
        json.dumps(normalizations, ensure_ascii=False)

    def test_flat_candidates_identical_with_dag_disabled(self) -> None:
        enabled = _prepare_unsigned_proposal(_two_step_proposal(), _group())
        with mock.patch(
            "reaserch_agent.route_pdf_group_extraction."
            "build_verified_state_proof_dags", lambda *args, **kwargs: {},
        ):
            disabled = _prepare_unsigned_proposal(
                _two_step_proposal(), _group())
        self.assertEqual(
            enabled["convention_state_candidates"],
            disabled["convention_state_candidates"])
        self.assertEqual(disabled["convention_state_proof_dags"], [])
        for key in ("required_fact_normalizations",
                    "qualitative_unit_normalizations",
                    "controlled_state_normalizations"):
            self.assertEqual(enabled[key], disabled[key])


class ReceiptConsumptionPointTest(unittest.TestCase):
    """Receipt: DAG rescue only after flat derivations AND gates fail."""

    def test_dag_exclusive_state_is_dag_proven(self) -> None:
        receipt = produce_pdf_group_fact_receipt(
            [_group()], [_protocol()], signed_inventory_verified=True)
        result = receipt.group_results[0]
        self.assertEqual(result.status, "literal_facts_verified_pending_review")
        self.assertIn(STEP1_OUT_STATE, result.dag_proven_state_field_paths)
        self.assertNotIn(STEP1_OUT_STATE, result.verified_field_paths)
        self.assertNotIn(STEP1_OUT_STATE, result.derived_state_field_paths)
        self.assertEqual(result.reason_codes, ())
        # Flat-derived and literal-verified classifications are unmoved.
        self.assertIn(OUT_STATE, result.derived_state_field_paths)
        self.assertIn(
            "material_graph[0].material_inputs[0].state",
            result.verified_field_paths)
        self.assertIn(STEP1_INPUT_STATE, result.derived_state_field_paths)

    def test_pre_g1_baseline_blocks_the_dag_exclusive_state(self) -> None:
        with mock.patch(
            "reaserch_agent.route_group_fact_receipt."
            "build_verified_state_proof_dags", lambda *args, **kwargs: {},
        ):
            receipt = produce_pdf_group_fact_receipt(
                [_group()], [_protocol()], signed_inventory_verified=True)
        result = receipt.group_results[0]
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.dag_proven_state_field_paths, ())
        self.assertTrue(any(
            reason.endswith("semantic_binding_pending")
            or reason.endswith("fact_value_not_in_excerpt")
            for reason in result.reason_codes))
        self.assertNotIn(STEP1_OUT_STATE, result.verified_field_paths)

    def test_diagnostic_record_in_dag_map_never_derives(self) -> None:
        protocol = _protocol()
        fact = next(
            item for item in protocol["route_facts"]
            if item["field_path"] == STEP1_OUT_STATE)
        scope = _group().source_scope
        diagnostic_entry = {
            "field_path": STEP1_OUT_STATE,
            "verdict": "PASS",
            "verify_with_span_resolver": "",
            "verify_blocks_only": "",
            "dag": {"schema_version": "operation-precondition-diagnostic/v1"},
            "derivation": {
                "derivation": STATE_PROOF_DAG_DERIVATION,
                "field_path": STEP1_OUT_STATE,
                "target_state": "suspension",
                "proof_dag_root_id": "proof_node_" + "0" * 24,
            },
        }
        derived = _state_derivation_proof(
            fact, protocol["route_facts"], protocol["material_graph"], scope,
            dag_proofs={STEP1_OUT_STATE: diagnostic_entry},
        )
        self.assertIsNone(derived)

    def test_input_value_must_match_dag_claim(self) -> None:
        # A tampered fact value on an input path must not ride the DAG:
        # the builder's own child-value check fails the build, and the
        # receipt-side mirror check would refuse a mismatched derivation.
        protocol = _protocol()
        proposal = _two_step_proposal()
        tampered = deepcopy(proposal)
        for item in tampered["route_facts"]:
            if item["field_path"] == STEP1_INPUT_STATE:
                item["value"] = "dry_powder"
        results = build_verified_state_proof_dags(
            tampered["material_graph"], tampered["route_facts"], **SCOPE,
            blocks=_blocks(),
        )
        self.assertEqual(results[STEP1_INPUT_STATE]["verdict"], "BLOCKED")
        self.assertEqual(
            results[STEP1_INPUT_STATE]["build_issue"],
            "convention_child_state_value_mismatch")

    def test_dag_rescue_still_runs_independent_unit_check(self) -> None:
        # Hole fix (unit check bypass): a DAG rescue lifts ONLY the
        # state-literal-evidence gates; the independent unit check still
        # applies to the rescued fact.  Three parallel receipts:
        #   HOLE — unit "kg" on the DAG-exclusive path blocks with
        #          fact_unit_non_numeric and never enters dag_proven;
        #   POS  — unit "" (fixture default) keeps the rescue;
        #   CTRL — the same unit violation on the flat-provable OUT_STATE
        #          blocks identically and was never DAG-eligible.
        def _receipt_with_unit(path: str, unit: str):
            protocol = _protocol()
            index = next(
                i for i, item in enumerate(protocol["route_facts"])
                if item["field_path"] == path)
            protocol["route_facts"][index]["unit"] = unit
            receipt = produce_pdf_group_fact_receipt(
                [_group()], [protocol], signed_inventory_verified=True)
            return index, receipt.group_results[0]

        index, hole = _receipt_with_unit(STEP1_OUT_STATE, "kg")
        self.assertEqual(hole.status, "blocked")
        self.assertIn(f"fact[{index}]:fact_unit_non_numeric", hole.reason_codes)
        self.assertEqual(hole.dag_proven_state_field_paths, ())

        _index, pos = _receipt_with_unit(STEP1_OUT_STATE, "")
        self.assertEqual(pos.status, "literal_facts_verified_pending_review")
        self.assertIn(STEP1_OUT_STATE, pos.dag_proven_state_field_paths)

        ctrl_index, ctrl = _receipt_with_unit(OUT_STATE, "kg")
        self.assertEqual(ctrl.status, "blocked")
        self.assertIn(
            f"fact[{ctrl_index}]:fact_unit_non_numeric", ctrl.reason_codes)
        # The DAG-exclusive path still rescues cleanly in the same receipt.
        self.assertIn(STEP1_OUT_STATE, ctrl.dag_proven_state_field_paths)


if __name__ == "__main__":
    unittest.main()
