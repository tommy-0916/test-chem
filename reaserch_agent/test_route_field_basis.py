"""Field requirements distinguish quoted chemistry from generated identities."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import unittest

from pydantic import ValidationError

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1, RouteCandidateV1, RouteFieldEvidenceV1,
    RouteGoalV1, RouteSignatureV1, RouteTargetV1,
)
from chem_agent_contracts.route_decision import decide_routes
from chem_agent_contracts.v2 import EvidenceItemV2, MacroStepV2, ProvenanceV2
from reaserch_agent.route_group_compiler import (
    canonicalize_proposal_material_ids, classify_route_field_basis,
    compile_experimental_group_protocols,
    material_id_graph_issue,
)
from reaserch_agent.route_group_fact_receipt import produce_pdf_group_fact_receipt
from reaserch_agent.route_pdf_group_extraction import propose_pdf_group_unreviewed
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_local_diagnostics import assess_pdf_group_proposal_fields


class RouteFieldBasisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.quote = (
            "In precipitation, 2 mmol Ni salt solution was used to "
            "precipitate product as retained_wet_solid for "
            "solution_to_wet_solid."
        )
        self.digest = "sha256_" + sha256(b"fixed original PDF").hexdigest()
        self.locator = "pdf:p1:b1-p1:b1"
        scope = ExperimentalGroupScopeV1(
            paper_id="paper-1", experimental_group_id="Arm A",
            section="Methods", locator=self.locator, source_digest=self.digest,
        )
        self.group = PdfExperimentalGroupV1(
            source_scope=scope, source_document="/signed/paper.pdf",
            blocks=(PdfSourceBlockV1(self.locator, self.quote),),
        )

    def _fact(self, fact_id: str, path: str, value: str | int,
              unit: str = "", excerpt: str | None = None) -> dict:
        return {
            "fact_id": fact_id, "field_path": path, "value": value,
            "unit": unit, "excerpt": self.quote if excerpt is None else excerpt,
            "required": True,
            "source": {
                "paper_id": "paper-1", "experimental_group_id": "Arm A",
                "section": "Methods", "locator": self.locator,
                "source_digest": self.digest,
            },
        }

    def _proposal(self) -> dict:
        signature = {
            "route_family": "precipitation",
            "target_transformation": "solution_to_wet_solid",
            "precursor_roles": ["Ni salt"], "reagent_roles": [],
            "operations": ["precipitate"], "control_modes": [],
            "phase_transitions": [], "endpoint_state": "retained_wet_solid",
        }
        input_path = "material_graph[0].material_inputs[0]"
        output_path = "material_graph[0].material_outputs[0]"
        facts = [
            self._fact("signature", "route_signature.route_family", "precipitation"),
            self._fact("signature", "route_signature.target_transformation",
                       "solution_to_wet_solid"),
            self._fact("signature", "route_signature.precursor_roles[0]", "Ni salt"),
            self._fact("signature", "route_signature.operations[0]", "precipitate"),
            self._fact("signature", "route_signature.endpoint_state",
                       "retained_wet_solid"),
            self._fact("step", "material_graph[0].operation", "precipitate"),
            self._fact("input", input_path + ".name", "Ni salt"),
            self._fact("input", input_path + ".state", "solution"),
            self._fact("input", input_path + ".quantity.value", 2, "mmol"),
            self._fact("output", output_path + ".name", "product"),
            self._fact("output", output_path + ".state", "retained_wet_solid"),
        ]
        return {
            "paper_id": "paper-1", "experimental_group_id": "Arm A",
            "group_role": "synthesis", "role_hint": "synthesis",
            "source": {
                "source_document": "/signed/paper.pdf", "section": "Methods",
                "locator": self.locator, "source_digest": self.digest,
            },
            "target": {"material": "product", "desired_state": "retained_wet_solid",
                       "objective": "prepare product"},
            "route_signature": signature,
            "material_graph": [{
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "precipitate", "sample_id": "sample-A",
                "provenance": {"kind": "paper", "reference": "fact:step"},
                "material_inputs": [{
                    "material_id": "mat_precursor_01", "material_instance_id": "batch_01",
                    "name": "Ni salt", "state": "solution",
                    "material_origin": "external_inventory",
                    "quantity": {"mode": "exact", "value": 2, "unit": "mmol"},
                    "provenance": {"kind": "paper", "reference": "fact:input"},
                }],
                "material_intermediates": [],
                "material_outputs": [{
                    "material_id": "mat_product_01", "material_instance_id": "batch_02",
                    "name": "product", "state": "retained_wet_solid",
                    "provenance": {"kind": "paper", "reference": "fact:output"},
                }],
            }],
            "required_capabilities": ["precipitate"],
            "route_facts": facts,
        }

    def _compile(self, proposal: dict):
        return compile_experimental_group_protocols([{
            "paper_id": "paper-1", "source_title": "Original study",
            "experimental_groups": [proposal],
        }])

    def _unreviewed_proposal(self) -> dict:
        proposal = self._proposal()
        for key in ("paper_id", "experimental_group_id", "group_role", "source",
                    "required_capabilities"):
            proposal.pop(key)
        proposal["source_group_ref"] = {
            "paper_id": "paper-1", "experimental_group_id": "Arm A",
            "source_digest": self.digest,
        }
        for fact in proposal["route_facts"]:
            fact.pop("source")
        return proposal

    def test_generated_id_is_not_a_missing_paper_fact_in_strict_feedback(self) -> None:
        self.assertEqual(
            classify_route_field_basis("material_graph[0].material_inputs[0].material_id"),
            "generated_id",
        )
        self.assertEqual(
            classify_route_field_basis("material_graph[0].material_inputs[0].material_instance_id"),
            "generated_id",
        )
        self.assertEqual(
            classify_route_field_basis("material_graph[0].material_inputs[0].quantity.value"),
            "paper_literal",
        )
        proposal = self._proposal()
        local_proposal = self._unreviewed_proposal()
        assessment = assess_pdf_group_proposal_fields(
            [self.group], [local_proposal], check_required_graph_facts=True,
        )
        missing = {
            item["field_path"] for item in assessment.issues
            if item["reason_code"] == "required_graph_fact_missing"
        }
        self.assertNotIn("material_graph[0].material_inputs[0].material_id", missing)
        self.assertNotIn("material_graph[0].material_outputs[0].material_id", missing)
        self.assertEqual(self._compile(proposal).diagnostics, [])

    def test_unreviewed_production_assigns_internal_material_ids(self) -> None:
        model_proposal = self._unreviewed_proposal()
        model_input_id = model_proposal["material_graph"][0]["material_inputs"][0]["material_id"]
        result = propose_pdf_group_unreviewed(
            [self.group], lambda _prompt: {"proposals": [deepcopy(model_proposal)]},
            max_repair_groups=0, check_required_graph_facts=True,
        )
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(result.protocols), 1)
        output_id = result.protocols[0]["material_graph"][0]["material_inputs"][0]["material_id"]
        self.assertNotEqual(output_id, model_input_id)
        self.assertTrue(output_id.startswith("material_"))

    def test_route_decision_endpoint_uses_evidenced_name_with_generated_id(self) -> None:
        proposal = self._proposal()
        graph = proposal["material_graph"][0]
        graph["material_outputs"][0]["material_id"] = "material_" + "a" * 24
        compiled = self._compile(proposal)
        self.assertEqual(compiled.diagnostics, [])
        group = compiled.protocols[0]["experimental_groups"][0]
        target = RouteTargetV1.model_validate(group["target"])
        candidate = RouteCandidateV1(
            route_id="route-1", target=target,
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-1", experimental_group_id="Arm A",
                section="Methods", locator=self.locator, source_digest=self.digest,
            ),
            route_signature=RouteSignatureV1.model_validate(group["route_signature"]),
            evidence_bundle=[EvidenceItemV2.model_validate(item)
                             for item in group["evidence_bundle"]],
            evidence_matrix=[RouteFieldEvidenceV1.model_validate(item)
                             for item in group["evidence_matrix"]],
            material_graph=[MacroStepV2.model_validate(item)
                            for item in group["material_graph"]],
            required_capabilities=["precipitate"],
            origin="paper_experimental_group",
        )
        goal = RouteGoalV1(
            goal_id="goal-1", target=target, constraint="open",
            required_fields=["material_graph[0].material_inputs[0].quantity.value"],
        )

        def independent_review_pending(_candidate: RouteCandidateV1):
            raise RuntimeError("independent review pending")

        decision = decide_routes(goal, [candidate], independent_review_pending)
        self.assertNotIn(
            "material_graph_endpoint_mismatch", decision.candidates[0].reasons,
        )
        self.assertIn("validation_receipt_missing", decision.candidates[0].reasons)

        wrong = candidate.model_copy(deep=True)
        wrong.material_graph[-1].material_outputs[0].name = "different product"
        wrong_decision = decide_routes(goal, [wrong], independent_review_pending)
        self.assertIn(
            "material_graph_endpoint_mismatch", wrong_decision.candidates[0].reasons,
        )

    def test_generated_id_cannot_be_claimed_as_a_paper_literal(self) -> None:
        proposal = self._proposal()
        # Even an accidental string match in source prose cannot turn an
        # internal graph key into a paper fact.
        quote = self.quote + " The label mat_precursor_01 was printed."
        self.group = PdfExperimentalGroupV1(
            source_scope=self.group.source_scope,
            source_document=self.group.source_document,
            blocks=(PdfSourceBlockV1(self.locator, quote),),
        )
        for fact in proposal["route_facts"]:
            fact["excerpt"] = quote
        proposal["route_facts"].append(self._fact(
            "input", "material_graph[0].material_inputs[0].material_id",
            "mat_precursor_01", excerpt=quote,
        ))
        receipt = produce_pdf_group_fact_receipt(
            [self.group], [proposal], signed_inventory_verified=True,
        )
        self.assertIn(
            "fact[11]:fact_generated_id_paper_fact_forbidden",
            receipt.group_results[0].reason_codes,
        )
        compiled = self._compile(proposal)
        self.assertEqual(
            compiled.diagnostics[0].reason_code,
            "route_fact_generated_id_paper_fact_forbidden",
        )

    def test_material_id_collision_between_distinct_names_is_blocked(self) -> None:
        proposal = self._proposal()
        proposal["material_graph"][0]["material_outputs"][0]["material_id"] = "mat_precursor_01"
        self.assertNotEqual(self._compile(proposal).diagnostics, [])

    def test_same_instance_symbol_cannot_merge_two_sample_arms(self) -> None:
        source_ref = {
            "paper_id": "paper-1", "experimental_group_id": "Arm A",
            "source_digest": self.digest,
        }
        proposal = {
            "source_group_ref": source_ref,
            "material_graph": [
                {
                    "sample_id": sample,
                    "material_inputs": [{
                        "material_id": "solution_A",
                        "material_instance_id": "batch_1",
                        "name": "solution A",
                    }],
                }
                for sample in ("sample-1", "sample-2")
            ],
        }
        canonical, _audit = canonicalize_proposal_material_ids(proposal)
        self.assertEqual(
            canonical["material_graph"][0]["material_inputs"][0][
                "material_instance_id"
            ],
            canonical["material_graph"][1]["material_inputs"][0][
                "material_instance_id"
            ],
        )
        self.assertEqual(
            material_id_graph_issue(canonical["material_graph"]),
            "route_group_material_instance_scope_conflict",
        )
        other_group = deepcopy(proposal)
        other_group["source_group_ref"]["experimental_group_id"] = "Arm B"
        other_canonical, _ = canonicalize_proposal_material_ids(other_group)
        self.assertNotEqual(
            canonical["material_graph"][0]["material_inputs"][0][
                "material_instance_id"
            ],
            other_canonical["material_graph"][0]["material_inputs"][0][
                "material_instance_id"
            ],
        )

    def test_strict_production_blocks_dangling_relation_instance(self) -> None:
        proposal = self._unreviewed_proposal()
        proposal["material_graph"][0]["material_relations"] = [{
            "input_material_instance_ids": ["ghost_input"],
            "output_material_instance_ids": ["batch_02"],
        }]
        produced = propose_pdf_group_unreviewed(
            [self.group], lambda _prompt: {"proposals": [deepcopy(proposal)]},
            max_repair_groups=0, check_required_graph_facts=True,
        )
        self.assertEqual(produced.protocols, [])
        self.assertIn(
            "route_group_material_relation_reference_missing",
            [item.reason_code for item in produced.diagnostics],
        )

        compiled = self._proposal()
        compiled["material_graph"][0]["material_relations"] = deepcopy(
            proposal["material_graph"][0]["material_relations"]
        )
        self.assertEqual(
            self._compile(compiled).diagnostics[0].reason_code,
            "route_group_material_relation_reference_missing",
        )

    def test_strict_production_blocks_dangling_parent_output(self) -> None:
        proposal = self._unreviewed_proposal()
        precursor = proposal["material_graph"][0]["material_inputs"][0]
        precursor["material_origin"] = "upstream_output"
        precursor["parent_output_refs"] = [{
            "macro_step_id": "missing_step",
            "material_instance_id": "ghost_output",
        }]
        produced = propose_pdf_group_unreviewed(
            [self.group], lambda _prompt: {"proposals": [deepcopy(proposal)]},
            max_repair_groups=0, check_required_graph_facts=True,
        )
        self.assertEqual(produced.protocols, [])
        self.assertIn(
            "route_group_parent_output_reference_missing",
            [item.reason_code for item in produced.diagnostics],
        )

        valid = self._proposal()["material_graph"]
        valid.append({
            "macro_step_id": "S2", "sequence": 2, "sample_id": "sample-A",
            "material_inputs": [{
                "material_id": "mat_product_01",
                "material_instance_id": "batch_03", "name": "product",
                "material_origin": "upstream_output",
                "parent_output_refs": [{
                    "macro_step_id": "S1", "material_instance_id": "batch_02",
                }],
            }],
        })
        self.assertEqual(material_id_graph_issue(valid), "")
        valid[1]["material_inputs"][0]["material_id"] = "wrong_material"
        self.assertEqual(
            material_id_graph_issue(valid),
            "route_group_parent_output_material_id_mismatch",
        )

    def test_normalized_material_name_requires_mapping_not_fake_paper_literal(self) -> None:
        proposal = self._proposal()
        path = "material_graph[0].material_inputs[0].name"
        proposal["material_graph"][0]["material_inputs"][0]["name"] = (
            "nickel nitrate hexahydrate"
        )
        next(fact for fact in proposal["route_facts"]
             if fact["field_path"] == path)["value"] = (
                 "nickel nitrate hexahydrate"
             )
        self.assertEqual(classify_route_field_basis(path), "controlled_mapping")
        self.assertEqual(
            self._compile(proposal).diagnostics[0].reason_code,
            "semantic_binding_pending",
        )
        unreviewed = deepcopy(proposal)
        for key in ("paper_id", "experimental_group_id", "group_role", "source",
                    "required_capabilities"):
            unreviewed.pop(key)
        unreviewed["source_group_ref"] = {
            "paper_id": "paper-1", "experimental_group_id": "Arm A",
            "source_digest": self.digest,
        }
        for fact in unreviewed["route_facts"]:
            fact.pop("source")
        assessment = assess_pdf_group_proposal_fields(
            [self.group], [unreviewed], check_required_graph_facts=True,
        )
        self.assertIn(
            (path, "semantic_binding_pending"),
            {(issue["field_path"], issue["reason_code"])
             for issue in assessment.issues},
        )

    def test_quantity_requirement_must_reference_its_material_port(self) -> None:
        paper = {"kind": "paper", "reference": "E1", "evidence_class": "paper_explicit"}
        step = {
            "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
            "operation": "register", "sample_id": "sample-A",
            "provenance": paper,
            "material_inputs": [{
                "material_id": "mat_precursor_01", "material_instance_id": "batch_01",
                "name": "Ni salt", "state": "solution",
                "material_origin": "external_inventory",
                "quantity": {"mode": "all_available", "semantic": "whole_batch_unspecified"},
                "provenance": paper,
            }],
            "operation_segments": [{
                "segment_id": "segment_01", "material_effect": "register_existing_input",
                "source_operation_ref": "register", "provenance": paper,
            }],
            "material_contract_status": {
                "material_inputs": "declared", "material_intermediates": "unresolved",
                "material_outputs": "unresolved", "logical_containers": "unresolved",
                "material_relations": "unresolved",
            },
            "quantity_requirements": [{
                "kind": "whole_batch", "material_id": "mat_precursor_01",
                "material": "Ni salt", "source": "literature", "provenance": paper,
            }],
        }
        MacroStepV2.model_validate(step)
        step["quantity_requirements"][0]["material_id"] = "unknown_material"
        with self.assertRaisesRegex(ValidationError, "must bind one declared material_id"):
            MacroStepV2.model_validate(step)

    def test_malformed_quantity_requirements_returns_diagnostics(self) -> None:
        proposal = self._proposal()
        proposal["material_graph"][0]["quantity_requirements"] = 42
        compiled = self._compile(proposal)
        self.assertEqual(
            compiled.diagnostics[0].reason_code,
            "route_group_quantity_requirements_invalid",
        )
        self.assertFalse(
            "evidence_matrix" in compiled.protocols[0]["experimental_groups"][0]
        )

        local_proposal = self._unreviewed_proposal()
        local_proposal["material_graph"][0]["quantity_requirements"] = 42
        assessment = assess_pdf_group_proposal_fields(
            [self.group], [local_proposal], check_required_graph_facts=True,
        )
        self.assertIn(
            "route_group_quantity_requirements_invalid",
            {item["reason_code"] for item in assessment.issues},
        )
        self.assertEqual(assessment.group_issue_indexes, (0,))

    def test_dangling_material_relation_instance_fails_v2_contract(self) -> None:
        paper = {"kind": "paper", "reference": "E1", "evidence_class": "paper_explicit"}
        step = {
            "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
            "operation": "precipitate", "sample_id": "sample-A",
            "provenance": paper,
            "material_inputs": [{
                "material_id": "material_ni", "material_instance_id": "input_1",
                "name": "Ni salt", "state": "solution",
                "material_origin": "external_inventory",
                "quantity": {"mode": "all_available", "semantic": "whole_batch_unspecified"},
                "provenance": paper,
            }],
            "material_outputs": [{
                "material_id": "material_product", "material_instance_id": "output_1",
                "name": "product", "state": "retained_wet_solid",
                "quantity": {"mode": "all_available", "semantic": "whole_batch_unspecified"},
                "provenance": paper,
            }],
            "operation_segments": [{
                "segment_id": "segment_1", "material_effect": "transform_material",
                "source_operation_ref": "precipitate", "provenance": paper,
            }],
            "material_relations": [{
                "relation_id": "relation_1", "event_kind": "state_change",
                "input_material_instance_ids": ["input_1"],
                "output_material_instance_ids": ["output_1"],
                "quantity_basis": "whole_batch", "source_operation_ref": "segment_1",
                "provenance": paper,
            }],
            "material_contract_status": {
                "material_inputs": "declared", "material_intermediates": "unresolved",
                "material_outputs": "declared", "logical_containers": "unresolved",
                "material_relations": "declared",
            },
        }
        MacroStepV2.model_validate(step)
        step["material_relations"][0]["input_material_instance_ids"] = ["ghost_input"]
        with self.assertRaisesRegex(ValidationError, "unknown input instances"):
            MacroStepV2.model_validate(step)

    def test_missing_paper_name_or_quantity_remains_blocked(self) -> None:
        proposal = self._proposal()
        proposal["route_facts"] = [
            fact for fact in proposal["route_facts"]
            if fact["field_path"] != "material_graph[0].material_inputs[0].name"
        ]
        self.assertEqual(
            self._compile(proposal).diagnostics[0].reason_code,
            "route_fact_qualitative_graph_claim_missing",
        )
        proposal = self._proposal()
        proposal["route_facts"] = [
            fact for fact in proposal["route_facts"]
            if fact["field_path"] != "material_graph[0].material_inputs[0].quantity.value"
        ]
        self.assertEqual(
            self._compile(proposal).diagnostics[0].reason_code,
            "route_fact_numeric_graph_claim_missing",
        )

    def test_other_materials_number_cannot_prove_precursor_amount(self) -> None:
        proposal = self._proposal()
        quote = self.quote.replace(
            "2 mmol Ni salt solution",
            "1 mmol Ni salt solution and 2 mmol Co salt",
        )
        self.group = PdfExperimentalGroupV1(
            source_scope=self.group.source_scope,
            source_document=self.group.source_document,
            blocks=(PdfSourceBlockV1(self.locator, quote),),
        )
        for fact in proposal["route_facts"]:
            fact["excerpt"] = quote
        amount_index = next(
            index for index, fact in enumerate(proposal["route_facts"])
            if fact["field_path"] == "material_graph[0].material_inputs[0].quantity.value"
        )
        receipt = produce_pdf_group_fact_receipt(
            [self.group], [proposal], signed_inventory_verified=True,
        )
        self.assertIn(
            f"fact[{amount_index}]:fact_quantity_attribution_unresolved",
            receipt.group_results[0].reason_codes,
        )
        self.assertEqual(
            self._compile(proposal).diagnostics[0].reason_code,
            "route_fact_quantity_attribution_unresolved",
        )

    def test_normalized_state_without_mapping_basis_stays_pending(self) -> None:
        state_path = "material_graph[0].material_outputs[0].state"
        self.assertEqual(classify_route_field_basis(state_path), "controlled_mapping")
        proposal = self._proposal()
        # The source says a wet precipitate; the controlled state token is a
        # normalization claim and is absent from the quoted group text.
        quote = self.quote.replace("retained_wet_solid", "wet precipitate")
        self.group = PdfExperimentalGroupV1(
            source_scope=self.group.source_scope,
            source_document=self.group.source_document,
            blocks=(PdfSourceBlockV1(self.locator, quote),),
        )
        proposal["route_signature"]["endpoint_state"] = ""
        proposal["route_facts"] = [
            fact for fact in proposal["route_facts"]
            if fact["field_path"] != "route_signature.endpoint_state"
        ]
        for fact in proposal["route_facts"]:
            fact["excerpt"] = quote
        state_index = next(
            index for index, fact in enumerate(proposal["route_facts"])
            if fact["field_path"] == state_path
        )
        receipt = produce_pdf_group_fact_receipt(
            [self.group], [proposal], signed_inventory_verified=True,
        )
        self.assertIn(
            f"fact[{state_index}]:semantic_binding_pending",
            receipt.group_results[0].reason_codes,
        )
        self.assertEqual(
            self._compile(proposal).diagnostics[0].reason_code,
            "semantic_binding_pending",
        )
        local_proposal = deepcopy(proposal)
        for key in ("paper_id", "experimental_group_id", "group_role", "source",
                    "required_capabilities"):
            local_proposal.pop(key)
        local_proposal["source_group_ref"] = {
            "paper_id": "paper-1", "experimental_group_id": "Arm A",
            "source_digest": self.digest,
        }
        for fact in local_proposal["route_facts"]:
            fact.pop("source")
        assessment = assess_pdf_group_proposal_fields(
            [self.group], [local_proposal], check_required_graph_facts=True,
        )
        self.assertIn(
            (state_path, "semantic_binding_pending"),
            {(issue["field_path"], issue["reason_code"]) for issue in assessment.issues},
        )

    def test_device_sop_is_still_a_provenance_evidence_class(self) -> None:
        sop = ProvenanceV2(
            kind="device_skill", reference="SOP-1", evidence_class="device_sop",
        )
        self.assertEqual(sop.evidence_class, "device_sop")
        with self.assertRaises(ValidationError):
            ProvenanceV2(
                kind="paper", reference="SOP-1", evidence_class="device_sop",
            )


if __name__ == "__main__":
    unittest.main()
