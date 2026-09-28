"""An inherited state needs a quoted parent, exact edge, and current rule."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1, RouteCandidateV1, RouteGoalV1, RouteTargetV1,
)
from chem_agent_contracts.route_convention_basis import (
    derive_unreviewed_output_state, verify_bound_output_state,
)
from chem_agent_contracts.route_field_basis import (
    output_state_parent_role_issue, passive_material_operation_span,
    split_rule_pattern_matches, state_source_locally_attributed,
)
from chem_agent_contracts.test_route_research_package_draft import (
    _build as build_fixture_package, _reviewed_fixture,
)
from chem_agent_contracts.v2 import (
    EvidenceBundleV2, MacroStepV2, QuantityV2, ResearchActionPackageV2,
    canonical_digest, route_material_graph_digest_v1,
)
from reaserch_agent.route_discovery import discover_route_candidates
from reaserch_agent.route_group_compiler import (
    compile_experimental_group_protocols, output_quantity_role_issue,
)
from reaserch_agent.route_group_fact_receipt import produce_pdf_group_fact_receipt
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups
from reaserch_agent.route_pdf_source import verify_route_pdf_source
from reaserch_agent.route_pdf_proposal_quality import assess_unreviewed_proposal_literal_shape
from reaserch_agent.route_source import verify_route_source
from reaserch_agent.route_science import audit_route_candidate_science


STATE_1 = "material_graph[0].material_outputs[0].state"
STATE_2 = "material_graph[0].material_outputs[1].state"


class RouteConventionStateChainTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sentence = "In this split route, solution A was split into 2 parts for analysis."
        self.source_file = self.root / "paper.md"
        self.source_file.write_text(
            "# Study\n## Methods\n### Group A\n" + self.sentence + "\n",
            encoding="utf-8",
        )
        self.digest = "sha256_" + sha256(self.source_file.read_bytes()).hexdigest()
        self.source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:4-4",
            "source_digest": self.digest,
        }

    def _proposal(self) -> dict:
        signature = {
            "route_family": "split route", "target_transformation": "split",
            "precursor_roles": ["solution A"], "reagent_roles": [],
            "operations": ["split"], "control_modes": [],
            "phase_transitions": [], "endpoint_state": "solution",
        }
        facts = []
        for fact_id, path, value in (
            ("sig1", "route_signature.route_family", "split route"),
            ("sig2", "route_signature.target_transformation", "split"),
            ("sig3", "route_signature.precursor_roles[0]", "solution A"),
            ("sig4", "route_signature.operations[0]", "split"),
            ("sig5", "route_signature.endpoint_state", "solution"),
            ("op", "material_graph[0].operation", "split"),
            ("in_name", "material_graph[0].material_inputs[0].name", "solution A"),
            ("in_state", "material_graph[0].material_inputs[0].state", "solution"),
            ("out1_name", "material_graph[0].material_outputs[0].name", "solution A"),
            ("out2_name", "material_graph[0].material_outputs[1].name", "solution A"),
            ("out1_state", STATE_1, "solution"),
            ("out2_state", STATE_2, "solution"),
        ):
            excerpt = (
                "solution A was split into 2 parts"
                if fact_id in {"in_state", "op", "out1_state", "out2_state"}
                else self.sentence
            )
            facts.append({
                "fact_id": fact_id, "field_path": path, "value": value,
                "unit": "", "required": True, "excerpt": excerpt,
                "source": deepcopy(self.source),
            })
        step = {
            "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
            "operation": "split", "sample_id": "sample-A",
            "provenance": {"kind": "paper", "reference": "fact:op"},
            "material_inputs": [{
                "material_id": "solutionA", "material_instance_id": "parent",
                "name": "solution A", "state": "solution",
                "material_origin": "external_inventory",
                "provenance": {"kind": "paper", "reference": "fact:in_name"},
            }],
            "material_outputs": [
                {
                    "material_id": "solutionA", "material_instance_id": "child1",
                    "name": "solution A", "state": "solution",
                    "provenance": {"kind": "paper", "reference": "fact:out1_name"},
                },
                {
                    "material_id": "solutionA", "material_instance_id": "child2",
                    "name": "solution A", "state": "solution",
                    "provenance": {"kind": "paper", "reference": "fact:out2_name"},
                },
            ],
            "operation_segments": [{
                "segment_id": "seg1", "material_effect": "split_material",
                "source_operation_ref": "S1",
                "provenance": {"kind": "paper", "reference": "fact:op"},
            }],
            "material_relations": [{
                "relation_id": "relation1", "event_kind": "split_same_material",
                "input_material_instance_ids": ["parent"],
                "output_material_instance_ids": ["child1", "child2"],
                "quantity_basis": "runtime_measurement_required",
                "source_operation_ref": "seg1",
                "provenance": {"kind": "paper", "reference": "fact:op"},
            }],
            "lineage_relation": {
                "relation_type": "split_from_parent",
                "parent_material_instance_ids": ["parent"],
                "child_material_instance_ids": ["child1", "child2"],
            },
        }
        return {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "group_role": "synthesis",
            "source": {
                "source_document": str(self.source_file),
                "section": "Methods", "locator": "lines:3-4",
                "source_digest": self.digest,
            },
            "target": {
                "material": "solution A", "desired_state": "solution",
                "objective": "prepare two portions",
            },
            "route_signature": signature,
            "material_graph": [step],
            "required_capabilities": ["split"],
            "route_facts": facts,
        }

    def _proof(self, proposal: dict, path: str = STATE_1):
        return derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"], path,
            paper_id="paper-A", experimental_group_id="Group A",
            source_digest=self.digest,
        )

    def test_exact_split_proof_compiles_as_convention_not_paper(self) -> None:
        original = self._proposal()
        proof, issue = self._proof(original)
        self.assertEqual(issue, "")
        self.assertEqual(proof["rule_id"], "SPLIT_V1")
        compiled = compile_experimental_group_protocols([original])
        self.assertEqual(compiled.diagnostics, [])
        self.assertNotIn("evidence_matrix", original)
        group = compiled.protocols[0]
        fields = {item["field_path"]: item for item in group["evidence_matrix"]}
        child = fields[STATE_1]
        self.assertEqual(child["provenance"]["kind"], "agent_inferred")
        self.assertEqual(child["provenance"]["evidence_class"], "chemistry_convention")
        self.assertEqual(child["evidence_id"], "")
        self.assertEqual(json.loads(child["provenance"]["derivation"]), proof)
        self.assertEqual(
            group["material_graph"][0]["material_outputs"][0]["provenance"]["kind"],
            "paper",
        )
        self.assertEqual(
            verify_bound_output_state(
                proof, group["material_graph"],
                {item["evidence_id"]: item for item in group["evidence_bundle"]},
                paper_id="paper-A", experimental_group_id="Group A",
                source_digest=self.digest,
            ), "",
        )
        goal = RouteGoalV1(
            goal_id="goal",
            target=RouteTargetV1.model_validate(group["target"]),
            constraint="open", required_fields=[STATE_1],
        )
        discovery = discover_route_candidates(
            goal, compiled.protocols,
            trusted_source_paths={"paper-A": [self.source_file]},
        )
        self.assertEqual(discovery.diagnostics, [])
        self.assertEqual(len(discovery.candidates), 1)
        source = verify_route_source(
            discovery.candidates[0],
            source_paths={"paper-A": self.source_file}, source_root=self.root,
        )
        self.assertTrue(source.source_scope_verified, source.reasons)
        self.assertNotIn(STATE_1, source.verified_field_paths)
        self.assertIn(proof["parent_state_path"], source.verified_field_paths)
        self.assertIn(proof["operation_path"], source.verified_field_paths)
        science_data = discovery.candidates[0].model_dump(mode="json")
        for item in science_data["evidence_bundle"]:
            if item["evidence_id"] in source.verified_evidence_ids:
                item["verification_status"] = "local_file"
                item["full_text_status"] = "local_parsed"
        science = audit_route_candidate_science(
            RouteCandidateV1.model_validate(science_data)
        )
        self.assertIn(STATE_1, science["audited_field_paths"], science)
        self.assertIn(STATE_1, science["verified_convention_field_paths"], science)
        borrowed = discovery.candidates[0].model_copy(deep=True)
        borrowed_parent = next(field for field in borrowed.evidence_matrix
                               if field.field_path ==
                               "material_graph[0].material_inputs[0].state")
        borrowed_child = next(field for field in borrowed.evidence_matrix
                              if field.field_path == STATE_1)
        borrowed_child.provenance = borrowed_parent.provenance.model_copy(deep=True)
        borrowed_child.evidence_id = borrowed_parent.evidence_id
        source_rejection = verify_route_source(
            borrowed, source_paths={"paper-A": self.source_file},
            source_root=self.root,
        )
        self.assertIn(
            f"parent_state_not_child_evidence:{STATE_1}",
            source_rejection.reasons,
        )
        science_rejection = audit_route_candidate_science(borrowed)
        self.assertIn(
            f"evidence_matrix_parent_state_reused_for_output:{STATE_1}",
            science_rejection["scientific_gate_issues"],
        )
        wrong_amount = discovery.candidates[0].model_copy(deep=True)
        wrong_amount.material_graph[0].material_outputs[0].quantity = QuantityV2(
            mode="exact", value=2, unit="parts",
        )
        role_audit = audit_route_candidate_science(wrong_amount)
        self.assertIn(
            "numeric_graph_quantity_role_mismatch:"
            "material_graph[0].material_outputs[0].quantity.value",
            role_audit["scientific_gate_issues"],
        )

    def test_missing_or_wrong_premise_stays_pending(self) -> None:
        base = self._proposal()
        trials = []
        missing = deepcopy(base)
        missing["material_graph"][0]["material_relations"] = []
        trials.append(missing)
        unknown = deepcopy(base)
        unknown["material_graph"][0]["material_inputs"][0]["state"] = "unknown"
        trials.append(unknown)
        wrong_subject = deepcopy(base)
        wrong_subject["route_facts"] = [
            {**fact, "excerpt": "reactor was split into 2 parts"}
            if fact["fact_id"] in {"op", "out1_state", "out2_state"}
            else fact for fact in wrong_subject["route_facts"]
        ]
        trials.append(wrong_subject)
        single_child = deepcopy(base)
        single_child["material_graph"][0]["material_relations"][0][
            "output_material_instance_ids"
        ] = ["child1"]
        trials.append(single_child)
        mismatched = deepcopy(base)
        next(fact for fact in mismatched["route_facts"] if fact["field_path"] == STATE_1)[
            "value"
        ] = "powder"
        trials.append(mismatched)
        for proposal in trials:
            with self.subTest(trial=trials.index(proposal)):
                proof, issue = self._proof(proposal)
                self.assertIsNone(proof)
                self.assertTrue(issue)

    def test_parent_state_quote_cannot_be_paper_child_fact_without_relation(self) -> None:
        proposal = self._proposal()
        proposal["material_graph"][0]["material_relations"] = []
        shape = assess_unreviewed_proposal_literal_shape([proposal])
        self.assertTrue(any(
            item["field_path"] == STATE_1
            and item["reason_code"] == "parent_state_not_child_evidence"
            for item in shape
        ))
        compiled = compile_experimental_group_protocols([proposal])
        self.assertEqual(compiled.diagnostics[0].reason_code,
                         "parent_state_not_child_evidence")
        locator = "pdf:p1:b1-p1:b1"
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-A", experimental_group_id="Group A",
                section="Methods", locator=locator, source_digest=self.digest,
            ),
            source_document="/controlled/paper.pdf",
            blocks=(PdfSourceBlockV1(locator, self.sentence),),
        )
        proposal["source"]["source_document"] = group.source_document
        proposal["source"]["locator"] = locator
        for fact in proposal["route_facts"]:
            fact["source"]["locator"] = locator
        receipt = produce_pdf_group_fact_receipt(
            [group], [proposal], signed_inventory_verified=True,
        )
        self.assertTrue(any(
            "parent_state_not_child_evidence" in reason
            for result in receipt.group_results for reason in result.reason_codes
        ))

    def test_explicit_child_state_sentence_keeps_literal_path(self) -> None:
        graph = [{
            "operation": "split", "material_inputs": [{
                "name": "batch A", "state": "solution",
            }], "material_outputs": [{
                "name": "resulting portion", "state": "solution",
            }],
        }]
        path = "material_graph[0].material_outputs[0].state"
        excerpt = "Each resulting portion was a solution."
        self.assertFalse(output_state_parent_role_issue(
            path, graph, "solution", excerpt,
        ))
        self.assertTrue(state_source_locally_attributed(
            "solution", excerpt, "resulting portion",
        ))

    def test_explicit_child_state_survives_compiler_and_pdf_receipt(self) -> None:
        self.sentence += " Each resulting portion was a solution."
        self.source_file.write_text(
            "# Study\n## Methods\n### Group A\n" + self.sentence + "\n",
            encoding="utf-8",
        )
        self.digest = "sha256_" + sha256(self.source_file.read_bytes()).hexdigest()
        self.source["source_digest"] = self.digest
        proposal = self._proposal()
        step = proposal["material_graph"][0]
        step["material_relations"] = []
        step.pop("lineage_relation")
        for output in step["material_outputs"]:
            output["material_id"] = "resultingPortions"
            output["name"] = "resulting portion"
        for fact in proposal["route_facts"]:
            if fact["fact_id"] in {"out1_name", "out2_name"}:
                fact["value"] = "resulting portion"
            if fact["fact_id"] in {"out1_state", "out2_state"}:
                fact["excerpt"] = "Each resulting portion was a solution."
            fact["source"]["source_digest"] = self.digest
        compiled = compile_experimental_group_protocols([proposal])
        self.assertEqual(compiled.diagnostics, [])
        fields = {field["field_path"]: field for field in
                  compiled.protocols[0]["evidence_matrix"]}
        self.assertEqual(fields[STATE_1]["provenance"]["kind"], "paper")
        locator = "pdf:p1:b1-p1:b1"
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-A", experimental_group_id="Group A",
                section="Methods", locator=locator, source_digest=self.digest,
            ),
            source_document="/controlled/paper.pdf",
            blocks=(PdfSourceBlockV1(locator, self.sentence),),
        )
        proposal["source"]["source_document"] = group.source_document
        proposal["source"]["locator"] = locator
        for fact in proposal["route_facts"]:
            fact["source"]["locator"] = locator
        receipt = produce_pdf_group_fact_receipt(
            [group], [proposal], signed_inventory_verified=True,
        )
        self.assertEqual(receipt.status, "literal_facts_verified_pending_review")
        self.assertIn(STATE_1, receipt.group_results[0].verified_field_paths)

    def test_divided_into_requires_word_boundary_subject_and_child_count(self) -> None:
        self.assertTrue(split_rule_pattern_matches(
            "divided into", "divided into 8 parts",
        ))
        for text in (
            "undivided into 8 parts", "was not divided into 8 parts",
            "individual samples",
        ):
            self.assertFalse(split_rule_pattern_matches("divided into", text))
        source = "the suspension (∼500 mg samples) was divided into 8 parts"
        self.assertIsNotNone(passive_material_operation_span(
            source, "suspension", "split",
        ))
        self.assertIsNone(passive_material_operation_span(
            "the vessel (suspension) was divided into 8 parts",
            "suspension", "split",
        ))
        base = self._proposal()
        for fact in base["route_facts"]:
            if fact["fact_id"] in {"op", "out1_state", "out2_state", "in_state"}:
                fact["excerpt"] = (
                    "solution A (∼500 mg samples) was divided into 2 parts"
                )
            if fact["fact_id"] == "op":
                fact["value"] = "divided into 2 parts"
        base["material_graph"][0]["operation"] = "divided into 2 parts"
        proof, issue = self._proof(base)
        self.assertEqual(issue, "")
        self.assertEqual(proof["rule_id"], "SPLIT_V1")
        for invalid_quote in (
            "the vessel (solution A) was divided into 2 parts",
            "solution A was not divided into 2 parts",
            "solution A was undivided into 2 parts",
        ):
            trial = deepcopy(base)
            for fact in trial["route_facts"]:
                if fact["fact_id"] in {"op", "out1_state", "out2_state", "in_state"}:
                    fact["excerpt"] = invalid_quote
            denied, reason = self._proof(trial)
            self.assertIsNone(denied, invalid_quote)
            self.assertTrue(reason, invalid_quote)
        incomplete = deepcopy(base)
        for fact in incomplete["route_facts"]:
            if fact["fact_id"] in {"op", "out1_state", "out2_state", "in_state"}:
                fact["excerpt"] = "solution A was divided into 8 parts"
            if fact["fact_id"] == "op":
                fact["value"] = "divided into 8 parts"
        incomplete["material_graph"][0]["operation"] = "divided into 8 parts"
        denied, reason = self._proof(incomplete)
        self.assertIsNone(denied)
        self.assertEqual(reason, "convention_split_children_incomplete")

    def test_active_parent_object_is_not_child_literal_state(self) -> None:
        graph = [{
            "operation": "split", "material_inputs": [{
                "name": "suspension", "state": "suspension",
            }], "material_outputs": [{
                "name": "suspension", "state": "suspension",
            }],
        }]
        path = "material_graph[0].material_outputs[0].state"
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension", "We split the suspension into 8 parts.",
        ))
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension",
            "We split the NiFe suspension into 8 parts.",
        ))
        graph[0]["material_inputs"][0].update({
            "name": "reaction mixture", "state": "solution",
        })
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension",
            "the suspension (∼500 mg samples) was divided into 8 parts",
        ))
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension",
            "the suspension was not divided into 8 parts",
        ))
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension", "We transferred the suspension to a vial.",
        ))
        graph[0]["material_inputs"][0]["name"] = "NiFe mixture"
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension",
            "the suspension (∼500 mg samples) was divided into 8 parts",
        ))
        self.assertTrue(output_state_parent_role_issue(
            path, graph, "suspension", "We split the suspension into 8 parts.",
        ))

    def test_prior_reaction_output_is_not_treated_as_split_child(self) -> None:
        graph = [{
            "operation": "reaction", "material_inputs": [{
                "name": "reactants", "state": "solution",
            }], "material_outputs": [{
                "name": "suspension", "state": "suspension",
            }],
        }]
        quote = "the suspension (∼500 mg samples) was divided into 8 parts"
        self.assertFalse(output_state_parent_role_issue(
            STATE_1, graph, "suspension", quote,
        ))
        self.assertTrue(state_source_locally_attributed(
            "suspension", quote, "suspension",
        ))

    def test_convention_requires_affirmative_material_operation(self) -> None:
        base = self._proposal()
        for quote in (
            "We did not split solution A into 2 parts for analysis.",
            "We never split solution A into 2 parts for analysis.",
            "We split the vessel containing solution A into 2 parts.",
        ):
            trial = deepcopy(base)
            for fact in trial["route_facts"]:
                if fact["fact_id"] in {"op", "in_state", "out1_state", "out2_state"}:
                    fact["excerpt"] = quote
            proof, issue = self._proof(trial)
            self.assertIsNone(proof, quote)
            self.assertEqual(
                issue, "convention_operation_material_attribution_unresolved",
                quote,
            )
        trial = deepcopy(base)
        for fact in trial["route_facts"]:
            if fact["fact_id"] in {"op", "in_state", "out1_state", "out2_state"}:
                fact["excerpt"] = "We split solution A into 2 parts for analysis."
        proof, issue = self._proof(trial)
        self.assertEqual(issue, "")
        self.assertIsNotNone(proof)

        transfer = deepcopy(base)
        step = transfer["material_graph"][0]
        step["operation"] = "transfer"
        step["material_outputs"] = step["material_outputs"][:1]
        step["operation_segments"][0]["material_effect"] = "transfer_material"
        step["material_relations"][0]["event_kind"] = "process_same_material"
        step["material_relations"][0]["output_material_instance_ids"] = ["child1"]
        step["lineage_relation"]["relation_type"] = "transfer_of"
        step["lineage_relation"]["child_material_instance_ids"] = ["child1"]
        transfer["route_facts"] = [
            fact for fact in transfer["route_facts"] if fact["fact_id"] not in
            {"out2_name", "out2_state"}
        ]
        for quote in (
            "We did not transfer solution A to the vial.",
            "We never transferred solution A to the vial.",
            "We transferred the vessel containing solution A to the bench.",
        ):
            trial = deepcopy(transfer)
            operation_word = "transferred" if "transferred" in quote else "transfer"
            trial["material_graph"][0]["operation"] = operation_word
            for fact in trial["route_facts"]:
                if fact["fact_id"] in {"op", "in_state", "out1_state"}:
                    fact["excerpt"] = quote
                if fact["fact_id"] == "op":
                    fact["value"] = operation_word
            proof, issue = self._proof(trial)
            self.assertIsNone(proof, quote)
            self.assertEqual(
                issue, "convention_operation_material_attribution_unresolved",
                quote,
            )
        allowed = deepcopy(transfer)
        for fact in allowed["route_facts"]:
            if fact["fact_id"] in {"op", "in_state", "out1_state"}:
                fact["excerpt"] = "We transfer solution A to the vial."
            if fact["fact_id"] == "op":
                fact["value"] = "transfer"
        proof, issue = self._proof(allowed)
        self.assertEqual(issue, "")
        self.assertIsNotNone(proof)

    def test_strict_pdf_receipt_keeps_state_derived_not_literal(self) -> None:
        protocol = self._proposal()
        locator = "pdf:p1:b1-p1:b1"
        group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-A", experimental_group_id="Group A",
                section="Methods", locator=locator,
                source_digest=self.digest,
            ),
            source_document="/controlled/paper.pdf",
            blocks=(PdfSourceBlockV1(locator, self.sentence),),
        )
        protocol["source"]["source_document"] = group.source_document
        protocol["source"]["locator"] = locator
        for fact in protocol["route_facts"]:
            fact["source"]["locator"] = locator
        receipt = produce_pdf_group_fact_receipt(
            [group], [protocol], signed_inventory_verified=True,
        )
        self.assertEqual(receipt.status, "literal_facts_verified_pending_review")
        self.assertEqual(
            set(receipt.group_results[0].derived_state_field_paths),
            {STATE_1, STATE_2},
        )
        self.assertNotIn(STATE_1, receipt.group_results[0].verified_field_paths)
        self.assertIn(
            "material_graph[0].material_inputs[0].state",
            receipt.group_results[0].verified_field_paths,
        )

    def test_part_count_is_not_output_material_quantity(self) -> None:
        proposal = self._proposal()
        for output in proposal["material_graph"][0]["material_outputs"]:
            output["quantity"] = {"mode": "exact", "value": 2, "unit": "parts"}
        proposal["route_facts"].append({
            "fact_id": "wrong_child_amount",
            "field_path": "material_graph[0].material_outputs[0].quantity.value",
            "value": 2, "unit": "parts", "required": True,
            "excerpt": "solution A was split into 2 parts",
            "source": deepcopy(self.source),
        })
        proof, issue = self._proof(proposal)
        self.assertEqual(issue, "")
        self.assertIsNotNone(proof)
        compiled = compile_experimental_group_protocols([proposal])
        self.assertEqual(
            compiled.diagnostics[0].reason_code,
            "route_fact_quantity_role_mismatch",
        )

    def test_concentration_unit_does_not_confuse_metre_with_molar(self) -> None:
        path = "material_graph[0].material_outputs[0].quantity.value"
        graph = self._proposal()["material_graph"]
        graph[0]["material_relations"] = []
        self.assertTrue(output_quantity_role_issue(path, graph, "parts"))
        self.assertTrue(output_quantity_role_issue(path, graph, "portions"))
        self.assertTrue(output_quantity_role_issue(path, graph, "M"))
        self.assertTrue(output_quantity_role_issue(path, graph, "mM"))
        self.assertFalse(output_quantity_role_issue(path, graph, "m"))
        self.assertFalse(output_quantity_role_issue(path, graph, "mm"))

    @unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
    def test_pdf_source_verifies_exact_support_paths_and_rejects_tamper(self) -> None:
        import fitz

        pdf = self.root / "paper.pdf"
        document = fitz.open()
        page = document.new_page()
        for index, (body, size, font) in enumerate((
            ("Methods", 16, "hebo"),
            ("Group A", 14, "hebo"),
            (self.sentence, 10, "helv"),
        )):
            page.insert_text((72, 70 + index * 48), body,
                             fontsize=size, fontname=font)
        document.save(str(pdf))
        document.close()
        indexed = enumerate_pdf_experimental_groups(
            {"paper-A": pdf}, source_root=self.root,
        )
        self.assertEqual(indexed.diagnostics, [])
        self.assertEqual(len(indexed.groups), 1)
        group = indexed.groups[0]
        proposal = self._proposal()
        proposal["source"]["source_document"] = str(pdf)
        proposal["source"]["locator"] = group.source_scope.locator
        proposal["source"]["source_digest"] = group.source_scope.source_digest
        for fact in proposal["route_facts"]:
            fact["source"]["locator"] = group.blocks[1].locator
            fact["source"]["source_digest"] = group.source_scope.source_digest
        compiled = compile_experimental_group_protocols([proposal])
        self.assertEqual(compiled.diagnostics, [])
        goal = RouteGoalV1(
            goal_id="goal", target=RouteTargetV1.model_validate(proposal["target"]),
            constraint="open", required_fields=[STATE_1],
        )
        discovered = discover_route_candidates(
            goal, compiled.protocols, trusted_source_paths={"paper-A": [pdf]},
        )
        self.assertEqual(discovered.diagnostics, [])
        candidate = discovered.candidates[0]
        verified = verify_route_pdf_source(
            candidate, source_paths={"paper-A": pdf}, source_root=self.root,
        )
        self.assertTrue(verified.source_scope_verified, verified.reasons)
        self.assertIn("material_graph[0].material_inputs[0].state",
                      verified.verified_field_paths)
        self.assertNotIn(STATE_1, verified.verified_field_paths)
        tampered = candidate.model_copy(deep=True)
        child = next(field for field in tampered.evidence_matrix
                     if field.field_path == STATE_1)
        proof = json.loads(child.provenance.derivation)
        proof["parent_evidence_id"] = "not-the-parent-fact"
        child.provenance.derivation = json.dumps(proof)
        rejected = verify_route_pdf_source(
            tampered, source_paths={"paper-A": pdf}, source_root=self.root,
        )
        self.assertFalse(rejected.source_scope_verified)
        self.assertTrue(any(reason.startswith("convention_state_support_unverified:")
                            for reason in rejected.reasons))
        borrowed = candidate.model_copy(deep=True)
        borrowed_parent = next(field for field in borrowed.evidence_matrix
                               if field.field_path ==
                               "material_graph[0].material_inputs[0].state")
        borrowed_child = next(field for field in borrowed.evidence_matrix
                              if field.field_path == STATE_1)
        borrowed_child.provenance = borrowed_parent.provenance.model_copy(deep=True)
        borrowed_child.evidence_id = borrowed_parent.evidence_id
        source_rejection = verify_route_pdf_source(
            borrowed, source_paths={"paper-A": pdf}, source_root=self.root,
        )
        self.assertIn(
            f"parent_state_not_child_evidence:{STATE_1}",
            source_rejection.reasons,
        )

    def test_convention_proof_survives_v2_reconstruction_and_tamper_fails(self) -> None:
        compiled = compile_experimental_group_protocols([self._proposal()])
        self.assertEqual(compiled.diagnostics, [])
        group = compiled.protocols[0]
        child = next(item for item in group["evidence_matrix"]
                     if item["field_path"] == STATE_1)
        proof = json.loads(child["provenance"]["derivation"])
        draft, decision, bundle = _reviewed_fixture()
        baseline = build_fixture_package(draft, decision, bundle)
        payload = baseline.model_dump(mode="json", exclude_none=True)
        payload.pop("research_contract_hash")
        step = deepcopy(group["material_graph"][0])
        step["macro_action_id"] = baseline.macro_action.macro_action_id
        step["sample_id"] = baseline.macro_action.experiment_group.sample_id
        payload["macro_steps"] = [step]
        payload["evidence_bundle"]["items"] = [
            {
                **item, "verification_status": "verified_doi",
                "full_text_status": "parsed",
            }
            for item in group["evidence_bundle"]
        ]
        binding = payload["route_binding"]
        binding["source_paper_id"] = "paper-A"
        binding["experimental_group_id"] = "Group A"
        binding["source_digest"] = self.digest
        binding["convention_state_proofs"] = [proof]
        binding["material_graph_digest"] = route_material_graph_digest_v1([
            MacroStepV2.model_validate(step, strict=True),
        ])
        binding["evidence_bundle_digest"] = canonical_digest(
            EvidenceBundleV2.model_validate(payload["evidence_bundle"], strict=True)
        )
        binding["intent_digest"] = canonical_digest({
            "schema_version": "route-action-intent/v1",
            "route_id": binding["route_id"],
            "candidate_digest": binding["candidate_digest"],
            "decision_id": binding["decision_id"],
            "evidence_bundle_id": payload["evidence_bundle"]["bundle_id"],
            "evidence_bundle_digest": binding["evidence_bundle_digest"],
            "stage": payload["stage"], "macro_action": payload["macro_action"],
        })
        reconstructed = ResearchActionPackageV2.model_validate(payload, strict=True)
        wire = reconstructed.model_dump(mode="json", exclude_none=True)
        wire.pop("research_contract_hash")
        reconstructed = ResearchActionPackageV2.model_validate(wire, strict=True)
        wire = reconstructed.model_dump(mode="json", exclude_none=True)
        restored = ResearchActionPackageV2.model_validate(
            json.loads(json.dumps(wire)), strict=True,
        )
        self.assertEqual(restored.route_binding.convention_state_proofs[0]
                         .model_dump(mode="json"), proof)
        tampered = deepcopy(wire)
        tampered.pop("research_contract_hash")
        tampered["route_binding"]["convention_state_proofs"][0][
            "resource_digest"
        ] = "sha256_" + "0" * 64
        with self.assertRaisesRegex(ValueError, "convention state proof differs"):
            ResearchActionPackageV2.model_validate(tampered, strict=True)
        wrong_count = deepcopy(wire)
        wrong_count.pop("research_contract_hash")
        wrong_count["macro_steps"][0]["material_outputs"][0]["quantity"] = {
            "mode": "exact", "value": 2, "unit": "parts",
        }
        with self.assertRaisesRegex(ValueError, "output quantity role differs"):
            ResearchActionPackageV2.model_validate(wrong_count, strict=True)


if __name__ == "__main__":
    unittest.main()
