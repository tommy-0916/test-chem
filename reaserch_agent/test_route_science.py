"""Offline, synthetic checks for the candidate science-gate adapter."""

from __future__ import annotations

import copy
import unittest

from chem_agent_contracts.route_candidate import RouteCandidateV1, RouteGoalV1
from chem_agent_contracts.route_decision import (
    DevicePreflightV1, RouteValidationReceiptV1, decide_routes,
)
from chem_agent_contracts.route_field_basis import state_source_locally_attributed
from chem_agent_contracts.route_source_labels import (
    build_source_label_context, state_attribution_outcome,
)
from chem_agent_contracts.v2 import ScientificCompletenessV2, canonical_digest
from reaserch_agent.route_science import _numeric_graph_fields, audit_route_candidate_science
from reaserch_agent.workflow import ResearchAgent


EXCERPT = "A synthetic protocol mixes feed with solvent at 20 C."
PAPER = {
    "kind": "paper", "reference": "E1", "evidence_class": "paper_explicit",
    "source_path": "evidence_bundle.items[0].excerpt", "excerpt": EXCERPT,
    "source_digest": canonical_digest(EXCERPT),
}


def _port(instance_id: str, *, origin: str | None = None, quantity: dict | None = None,
          provenance: dict | None = None) -> dict:
    port = {
        "material_id": instance_id,
        "material_instance_id": instance_id,
        "name": instance_id,
        "state": "solution",
        "provenance": provenance or PAPER,
    }
    if origin:
        port["material_origin"] = origin
    if quantity is not None:
        port["quantity"] = quantity
    return port


def _step(number: int, *, operation: str = "mix") -> dict:
    return {
        "macro_step_id": f"MS_{number:03d}",
        "macro_action_id": "MA_001",
        "sequence": number,
        "operation": operation,
        "sample_id": "sample_1",
        "provenance": PAPER,
        "material_inputs": [],
        "material_outputs": [],
    }


def _candidate(
    steps: list[dict], fields: list[dict] | None = None,
    *, bundle: list[dict] | None = None,
) -> RouteCandidateV1:
    return RouteCandidateV1.model_validate({
        "route_id": "route_1",
        "target": {"material": "product", "desired_state": "solution", "objective": "synthetic test"},
        "source_scope": {
            "paper_id": "paper_1", "experimental_group_id": "group_1",
            "source_digest": "sha256_" + "a" * 64,
        },
        "route_signature": {
            "route_family": "mixing",
            "target_transformation": "solution to solution",
            "operations": [step["operation"] for step in steps] or ["mix"],
            "endpoint_state": "solution",
        },
        "origin": "paper_experimental_group",
        "evidence_bundle": bundle if bundle is not None else [{
            "evidence_id": "E1", "verification_status": "verified_doi",
            "full_text_status": "parsed", "excerpt": EXCERPT,
        }],
        "material_graph": steps,
        "evidence_matrix": fields or [],
    })


class RouteScienceAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent = ResearchAgent(
            model=object(), use_llm=False, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )

    def audit(self, candidate: RouteCandidateV1) -> dict:
        return audit_route_candidate_science(candidate, agent=self.agent)

    def test_empty_graph_does_not_turn_zero_audit_counts_into_approval(self) -> None:
        result = self.audit(_candidate([]))
        self.assertIsNone(result["scientific_completeness"])
        self.assertEqual(result["scientific_gate_issues"], [])
        self.assertEqual(result["verified_graph_step_ids"], [])
        self.assertEqual(result["audited_field_paths"], [])

    def test_claimed_field_must_equal_a_real_graph_value_and_owner_provenance(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port(
            "feed", origin="external_inventory",
            quantity={"mode": "exact", "value": 5.0, "unit": "mL"},
        )]
        step["material_outputs"] = [_port("product")]
        path = "material_graph[0].material_inputs[0].quantity.value"
        field = {
            "field_path": path, "value": 6.0, "unit": "mL",
            "status": "supported", "provenance": PAPER,
        }
        result = self.audit(_candidate([step], [field]))
        self.assertNotIn(path, result["audited_field_paths"])
        self.assertIn(f"evidence_matrix_graph_mismatch:{path}", result["scientific_gate_issues"])

        field["value"] = 5.0
        result = self.audit(_candidate([step], [field]))
        self.assertIn(path, result["audited_field_paths"])
        # A matched graph field is only structural association; the missing
        # material contract still prevents a verified graph receipt.
        self.assertEqual(result["verified_graph_step_ids"], [])

    def test_port_name_anchors_distinct_quantity_paper_fact(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port(
            "feed", origin="external_inventory",
            quantity={"mode": "exact", "value": 5.0, "unit": "mL"},
        )]
        step["material_outputs"] = [_port("product")]
        prefix = "material_graph[0].material_inputs[0]"
        quantity_path = prefix + ".quantity.value"
        name_path = prefix + ".name"
        second_excerpt = "The feed amount was 5 mL."
        second_paper = {
            **PAPER, "reference": "E2",
            "source_path": "evidence_bundle.items[1].excerpt",
            "excerpt": second_excerpt,
            "source_digest": canonical_digest(second_excerpt),
        }
        scope = {
            "paper_id": "paper_1", "experimental_group_id": "group_1",
            "source_digest": "sha256_" + "a" * 64,
        }
        fields = [
            {"field_path": name_path, "value": "feed", "status": "supported",
             "provenance": PAPER, "evidence_id": "E1", "source_scope": scope},
            {"field_path": quantity_path, "value": 5.0, "unit": "mL",
             "status": "supported", "provenance": second_paper,
             "evidence_id": "E2", "source_scope": scope},
        ]
        bundle = [
            {"evidence_id": "E1", "verification_status": "verified_doi",
             "full_text_status": "parsed", "excerpt": EXCERPT},
            {"evidence_id": "E2", "verification_status": "verified_doi",
             "full_text_status": "parsed", "excerpt": second_excerpt},
        ]
        result = self.audit(_candidate([step], fields, bundle=bundle))
        self.assertIn(quantity_path, result["audited_field_paths"])
        self.assertNotIn(
            f"evidence_matrix_graph_mismatch:{quantity_path}",
            result["scientific_gate_issues"],
        )

        missing_anchor = self.audit(_candidate([step], fields[1:], bundle=bundle))
        self.assertIn(
            f"evidence_matrix_graph_mismatch:{quantity_path}",
            missing_anchor["scientific_gate_issues"],
        )
        wrong_source = copy.deepcopy(fields)
        wrong_source[1]["provenance"]["source_digest"] = canonical_digest("other")
        bad_binding = self.audit(_candidate([step], wrong_source, bundle=bundle))
        self.assertIn(
            f"evidence_matrix_graph_mismatch:{quantity_path}",
            bad_binding["scientific_gate_issues"],
        )

    def test_signature_fact_is_audited_only_when_equal_to_proposed_signature(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        path = "route_signature.operations[0]"
        field = {
            "field_path": path, "value": "mix", "status": "supported",
            "provenance": PAPER,
        }
        result = self.audit(_candidate([step], [field]))
        self.assertIn(path, result["audited_field_paths"])
        field["value"] = "heat"
        result = self.audit(_candidate([step], [field]))
        self.assertNotIn(path, result["audited_field_paths"])
        self.assertIn(
            f"evidence_matrix_signature_mismatch:{path}",
            result["scientific_gate_issues"],
        )

    def test_bare_agent_inferred_numeric_material_is_blocked(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port(
            "product", quantity={"mode": "exact", "value": 2.0, "unit": "g"},
            provenance={"kind": "agent_inferred", "rationale": "guess"},
        )]
        result = self.audit(_candidate([step]))
        self.assertTrue(any(
            issue.startswith("bare_agent_inferred:")
            for issue in result["scientific_gate_issues"]
        ))

    def test_missing_upstream_material_keeps_graph_unverified(self) -> None:
        step = _step(1)
        orphan = _port("feed", origin="upstream_output")
        orphan["parent_output_refs"] = [{
            "macro_step_id": "MS_000", "material_instance_id": "feed",
        }]
        step["material_inputs"] = [orphan]
        step["material_outputs"] = [_port("product")]
        result = self.audit(_candidate([step]))
        self.assertTrue(any(
            issue.startswith("material_graph:")
            for issue in result["scientific_gate_issues"]
        ))
        self.assertGreater(result["scientific_completeness"].broken_material_lineage, 0)
        self.assertEqual(result["verified_graph_step_ids"], [])

    def test_earlier_generic_measurement_does_not_verify_this_runtime_field(self) -> None:
        produce = _step(1, operation="weigh unrelated sample")
        produce["material_inputs"] = [_port("feed", origin="external_inventory")]
        produce["material_outputs"] = [_port(
            "wet_product",
            quantity={"mode": "runtime_measured", "unit": "mg",
                      "semantic": "runtime_measurement_required"},
        )]
        consume = _step(2, operation="consume product")
        downstream = _port("wet_product", origin="upstream_output")
        downstream["parent_output_refs"] = [{
            "macro_step_id": "MS_001", "material_instance_id": "wet_product",
        }]
        consume["material_inputs"] = [downstream]
        consume["material_outputs"] = [_port("product")]
        path = "material_graph[0].material_outputs[0].quantity.value"
        candidate = _candidate([produce, consume], [{
            "field_path": path, "status": "runtime_pending",
            "resolution_path": "weigh wet_product before consumption",
        }])
        result = self.audit(candidate)
        # An unrelated measurement phrase satisfies neither the material
        # return contract nor the evidence-matrix field's resolution proof.
        self.assertEqual(result["scientific_completeness"].counts["unresolved_runtime_dependency"], 1)
        self.assertEqual(result["verified_runtime_resolution_fields"], [])
        self.assertNotIn(path, result["audited_field_paths"])

    def test_pretended_convention_rule_is_not_verified_without_expansion(self) -> None:
        step = _step(1, operation="unrelated operation")
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        inferred = {"kind": "agent_inferred", "rationale": "claimed convention",
                    "evidence_class": "chemistry_convention", "inference_rule": "FAKE_RULE"}
        step["material_outputs"] = [_port("product", provenance=inferred)]
        path = "material_graph[0].material_outputs[0].state"
        result = self.audit(_candidate([step], [{
            "field_path": path, "value": "solution", "status": "supported",
            "provenance": inferred,
        }]))
        self.assertEqual(result["verified_convention_field_paths"], [])
        # The selector treats an unverified convention as unresolved.  Merely
        # failing to verify a claimed rule is not a hard science rejection.

    def test_paper_provenance_uses_excerpt_digest_not_document_digest(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        candidate = _candidate([step], [{
            "field_path": "material_graph[0].material_inputs[0].name",
            "value": "feed", "status": "supported", "provenance": PAPER,
        }])
        self.assertNotEqual(
            candidate.source_scope.source_digest, PAPER["source_digest"]
        )
        result = self.audit(candidate)
        self.assertIsInstance(result["scientific_completeness"], ScientificCompletenessV2)
        self.assertFalse(any(
            issue.startswith("material_provenance:")
            for issue in result["scientific_gate_issues"]
        ))

        wrong = copy.deepcopy(step)
        wrong["provenance"]["source_digest"] = candidate.source_scope.source_digest
        result = self.audit(_candidate([wrong], [{
            "field_path": "material_graph[0].material_inputs[0].name",
            "value": "feed", "status": "supported", "provenance": PAPER,
        }]))
        self.assertTrue(any(
            issue.startswith("material_provenance:")
            for issue in result["scientific_gate_issues"]
        ))
        self.assertEqual(result["verified_graph_step_ids"], [])

    def test_missing_current_bundle_is_pending_without_false_gate_failure(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        result = self.audit(_candidate([step], bundle=[]))
        self.assertIsNone(result["scientific_completeness"])
        self.assertEqual(result["verified_graph_step_ids"], [])
        self.assertFalse(any(
            issue.startswith("material_provenance:")
            for issue in result["scientific_gate_issues"]
        ))

    def test_parameter_paper_digest_is_checked_even_when_material_helper_skips_it(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        parameter_provenance = {**PAPER, "source_digest": "sha256_" + "b" * 64}
        step["parameters"] = [{
            "name": "temperature", "value": 20, "unit": "C",
            "provenance": parameter_provenance,
        }]
        result = self.audit(_candidate([step]))
        self.assertTrue(any(
            issue.startswith("material_provenance:paper_excerpt_digest_mismatch:")
            and "parameters[0]" in issue
            for issue in result["scientific_gate_issues"]
        ))

    def test_unverified_current_item_stays_pending(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        result = self.audit(_candidate([step], bundle=[{
            "evidence_id": "E1", "verification_status": "unknown",
            "full_text_status": "unknown", "excerpt": EXCERPT,
        }]))
        self.assertIsNone(result["scientific_completeness"])
        self.assertEqual(result["verified_graph_step_ids"], [])
        self.assertFalse(any(
            issue.startswith("material_provenance:")
            for issue in result["scientific_gate_issues"]
        ))

    def test_numeric_parameter_omitted_from_matrix_keeps_candidate_unresolved(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        step["parameters"] = [{
            "name": "temperature", "value": 20, "unit": "C", "provenance": PAPER,
        }]
        candidate = _candidate([step], [{
            "field_path": "material_graph[0].material_inputs[0].name",
            "value": "feed", "status": "supported", "provenance": PAPER,
        }])
        result = self.audit(candidate)
        self.assertIsNone(result["scientific_completeness"])
        self.assertEqual(result["verified_graph_step_ids"], [])
        self.assertNotIn(
            "material_graph[0].parameters[0].value", result["audited_field_paths"]
        )

    def test_numeric_parameter_value_mismatch_is_a_known_gate_failure(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        step["parameters"] = [{
            "name": "temperature", "value": 20, "unit": "C", "provenance": PAPER,
        }]
        path = "material_graph[0].parameters[0].value"
        result = self.audit(_candidate([step], [{
            "field_path": path, "value": 25, "unit": "C",
            "status": "supported", "provenance": PAPER,
        }]))
        self.assertIn(
            f"evidence_matrix_graph_mismatch:{path}", result["scientific_gate_issues"]
        )
        self.assertNotIn(path, result["audited_field_paths"])

    def test_numeric_enumerator_covers_nested_quantities_and_concentration(self) -> None:
        step = {
            "sequence": 1,
            "parameters": [{"value": 20, "unit": "C"}],
            "material_inputs": [{
                "quantity": {"value": 5, "unit": "mL"},
                "concentration_value": 0.2,
                "concentration_unit": "M",
            }],
            "quantity_requirements": [{"value": 3}],
            "material_relations": [{
                "input_allocations": [{"quantity": {"value": 2, "unit": "g"}}],
            }],
            "provenance": {"source_version": 3},
        }
        paths = {path for path, _value, _unit in _numeric_graph_fields([step])}
        self.assertEqual(paths, {
            "material_graph[0].parameters[0].value",
            "material_graph[0].material_inputs[0].quantity.value",
            "material_graph[0].material_inputs[0].concentration_value",
            "material_graph[0].quantity_requirements[0].value",
            "material_graph[0].material_relations[0].input_allocations[0].quantity.value",
        })

    def test_numeric_parameter_serialized_as_text_is_not_exempt(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        step["parameters"] = [{
            "name": "temperature", "value": "20", "unit": "C", "provenance": PAPER,
        }]
        result = self.audit(_candidate([step], [{
            "field_path": "material_graph[0].material_inputs[0].name",
            "value": "feed", "status": "supported", "provenance": PAPER,
        }]))
        self.assertIsNone(result["scientific_completeness"])
        self.assertEqual(result["verified_graph_step_ids"], [])

    def test_verified_route_signature_cannot_name_coprecipitation_for_reflux_graph(self) -> None:
        step = _step(1, operation="reflux")
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        candidate = _candidate([step])
        candidate.route_signature.operations = ["coprecipitation"]
        result = self.audit(candidate)
        self.assertIn(
            "route_signature_graph_operations_mismatch",
            result["scientific_gate_issues"],
        )
        self.assertEqual(result["verified_graph_step_ids"], [])

    def test_composite_segments_without_one_to_one_operation_mapping_are_pending(self) -> None:
        step = _step(1, operation="mix and heat")
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        step["operation_segments"] = [
            {"segment_id": "A", "material_effect": "none",
             "source_operation_ref": "mix", "provenance": PAPER},
            {"segment_id": "B", "material_effect": "none",
             "source_operation_ref": "heat", "provenance": PAPER},
        ]
        candidate = _candidate([step])
        candidate.route_signature.operations = ["mix", "heat"]
        result = self.audit(candidate)
        self.assertIsNone(result["scientific_completeness"])
        self.assertEqual(result["verified_graph_step_ids"], [])
        self.assertNotIn(
            "route_signature_graph_operations_mismatch",
            result["scientific_gate_issues"],
        )

    def test_agent_inferred_cannot_self_label_paper_explicit_in_graph_parameter(self) -> None:
        step = _step(1)
        step["material_inputs"] = [_port("feed", origin="external_inventory")]
        step["material_outputs"] = [_port("product")]
        step["parameters"] = [{
            "name": "temperature", "value": 20, "unit": "C",
            "provenance": {
                "kind": "agent_inferred", "rationale": "guessed",
                "evidence_class": "paper_explicit",
            },
        }]
        result = self.audit(_candidate([step]))
        self.assertTrue(any(
            issue.startswith("provenance_evidence_class_mismatch:")
            and "parameters[0]" in issue
            for issue in result["scientific_gate_issues"]
        ))


PH_SENTENCE = (
    "The pH of the solution was monitored using a pH meter (Mettler Toledo) "
    "and controlled to be 10 by dropwise adding solution B manually."
)
STATE_PATH = "material_graph[0].material_inputs[0].state"


def _source_state_candidate(
    *, names: tuple[str, ...] = ("solution B",), excerpt: str = PH_SENTENCE,
    name_excerpts: tuple[str, ...] | None = None,
) -> RouteCandidateV1:
    """One paper state claim with separately bound entity-name facts.

    This small fixture deliberately lacks an executable material contract;
    it exercises field association without implying whole-route approval.
    """
    scope = {
        "paper_id": "paper_1", "experimental_group_id": "group_1",
        "section": "Methods", "locator": "lines:1-3",
        "source_digest": "sha256_" + "a" * 64,
    }
    bundle: list[dict] = []
    provenances: dict[str, dict] = {}

    def paper(quote: str) -> dict:
        if quote not in provenances:
            index = len(bundle)
            evidence_id = f"E{index + 1}"
            bundle.append({
                "evidence_id": evidence_id, "verification_status": "verified_doi",
                "full_text_status": "parsed", "excerpt": quote,
            })
            provenances[quote] = {
                "kind": "paper", "reference": evidence_id,
                "evidence_class": "paper_explicit",
                "source_path": f"evidence_bundle.items[{index}].excerpt",
                "excerpt": quote, "source_digest": canonical_digest(quote),
            }
        return provenances[quote]

    state_paper = paper(excerpt)
    step = _step(1)
    step["provenance"] = state_paper
    fields = []
    for index, name in enumerate(names):
        name_paper = paper((name_excerpts or (excerpt,) * len(names))[index])
        port = _port(f"material_{index}", origin="external_inventory",
                     provenance=name_paper)
        port["name"] = name
        step["material_inputs"].append(port)
        fields.append({
            "field_path": f"material_graph[0].material_inputs[{index}].name",
            "value": name, "status": "supported", "provenance": name_paper,
            "evidence_id": name_paper["reference"], "source_scope": scope,
        })
    step["material_outputs"] = [_port("product", provenance=state_paper)]
    fields.append({
        "field_path": STATE_PATH, "value": "solution", "status": "supported",
        "provenance": state_paper, "evidence_id": state_paper["reference"],
        "source_scope": scope,
    })
    candidate = _candidate([step], fields, bundle=bundle)
    candidate.source_scope = candidate.evidence_matrix[0].source_scope.model_copy()
    candidate.required_capabilities = ["ambient-mixing"]
    return candidate


class SourceLabelScienceAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agent = ResearchAgent(
            model=object(), use_llm=False, enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )

    def audit(self, candidate: RouteCandidateV1) -> dict:
        return audit_route_candidate_science(candidate, agent=self.agent)

    def attribution(self, candidate: RouteCandidateV1):
        field = candidate.evidence_matrix[-1]
        return state_attribution_outcome(
            field.value, field.provenance.excerpt, field.field_path,
            candidate.material_graph, build_source_label_context(
                candidate.material_graph, candidate.evidence_matrix,
            ),
        )

    def test_original_ph_passage_audits_named_solution_amid_other_mentions(self):
        candidate = _source_state_candidate()
        self.assertFalse(state_source_locally_attributed(
            "solution", PH_SENTENCE, "solution B",
        ))
        outcome, binding = self.attribution(candidate)
        self.assertEqual(outcome, "binding")
        self.assertEqual(binding["entity_material_id"], "material_0")
        self.assertEqual(binding["mention_anchor"],
                         "material_graph[0].material_inputs[0].name")
        for annotate in (False, True):
            with self.subTest(stored_binding=annotate):
                payload = candidate.model_dump(mode="json")
                if annotate:
                    payload["evidence_matrix"][-1]["source_label_binding"] = binding
                result = self.audit(RouteCandidateV1.model_validate(payload))
                self.assertIn(STATE_PATH, result["audited_field_paths"])

    def test_shared_label_of_two_entities_never_audits_either_state(self):
        candidate = _source_state_candidate(names=("solution B", "solution B"))
        self.assertEqual(self.attribution(candidate), ("pending", None))
        self.assertNotIn(STATE_PATH, self.audit(candidate)["audited_field_paths"])

    def test_original_ph_passage_binds_bare_solution_to_its_same_passage_name(self):
        candidate = _source_state_candidate(names=("solution", "solution B"))
        outcome, binding = self.attribution(candidate)
        self.assertEqual(outcome, "binding")
        self.assertEqual(binding["source_surface"], "solution")
        self.assertEqual(binding["entity_material_id"], "material_0")
        self.assertIn(STATE_PATH, self.audit(candidate)["audited_field_paths"])

    def test_foreign_named_solution_does_not_prove_this_material_state(self):
        candidate = _source_state_candidate(
            names=("solution A", "solution B"),
            name_excerpts=("Solution A was prepared separately.", PH_SENTENCE),
        )
        self.assertEqual(self.attribution(candidate), ("pending", None))
        self.assertNotIn(STATE_PATH, self.audit(candidate)["audited_field_paths"])

    def test_engaged_failure_never_falls_back_to_legacy_matcher(self):
        excerpt = "The suspension in the solution was mixed gently."
        candidate = _source_state_candidate(
            names=("solution",), excerpt=excerpt,
            name_excerpts=("The solution was cooled overnight before use.",),
        )
        self.assertTrue(state_source_locally_attributed("solution", excerpt, "solution"))
        self.assertEqual(self.attribution(candidate), ("pending", None))
        self.assertNotIn(STATE_PATH, self.audit(candidate)["audited_field_paths"])

    def test_legacy_local_attribution_remains_available_without_label_structure(self):
        candidate = _source_state_candidate(
            names=("feed",), excerpt="The feed solution was mixed.",
        )
        self.assertEqual(self.attribution(candidate), ("legacy", None))
        self.assertIn(STATE_PATH, self.audit(candidate)["audited_field_paths"])

    def test_stored_binding_is_recomputed_including_entity_and_mention_anchor(self):
        candidate = _source_state_candidate()
        _outcome, binding = self.attribution(candidate)
        for key, value in (
            ("entity_material_id", "material_1"),
            ("mention_anchor", "material_graph[0].material_inputs[1].name"),
            ("label", "solution a"),
            ("source_surface", "solution A"),
            ("rule_id", "source-labels/v1:bare_state_mention"),
        ):
            with self.subTest(tampered=key):
                payload = candidate.model_dump(mode="json")
                payload["evidence_matrix"][-1]["source_label_binding"] = {
                    **binding, key: value,
                }
                result = self.audit(RouteCandidateV1.model_validate(payload))
                self.assertNotIn(STATE_PATH, result["audited_field_paths"])
                self.assertIn(
                    "evidence_matrix_source_label_binding_mismatch:" + STATE_PATH,
                    result["scientific_gate_issues"],
                )

    def test_forged_binding_cannot_authorize_legacy_claim(self):
        source = _source_state_candidate()
        _outcome, binding = self.attribution(source)
        legacy = _source_state_candidate(
            names=("feed",), excerpt="The feed solution was mixed.",
        ).model_dump(mode="json")
        legacy["evidence_matrix"][-1]["source_label_binding"] = binding
        result = self.audit(RouteCandidateV1.model_validate(legacy))
        self.assertNotIn(STATE_PATH, result["audited_field_paths"])
        self.assertIn(
            "evidence_matrix_source_label_binding_mismatch:" + STATE_PATH,
            result["scientific_gate_issues"],
        )

    def test_binding_never_upgrades_unverified_source_evidence(self):
        candidate = _source_state_candidate()
        candidate.evidence_bundle[0].verification_status = "unknown"
        result = self.audit(candidate)
        self.assertNotIn(STATE_PATH, result["audited_field_paths"])
        self.assertIsNone(result["scientific_completeness"])

    def test_decider_receives_actual_state_audit_and_retains_other_graph_blockers(self):
        candidate = _source_state_candidate()
        science = self.audit(candidate)
        receipt = RouteValidationReceiptV1(
            route_id=candidate.route_id, candidate_digest=canonical_digest(candidate),
            source_scope_verified=True, source_route_signature=candidate.route_signature,
            verified_evidence_ids=[item.evidence_id for item in candidate.evidence_bundle],
            verified_field_paths=[field.field_path for field in candidate.evidence_matrix],
            audited_field_paths=science["audited_field_paths"],
            verified_graph_step_ids=science["verified_graph_step_ids"],
            scientific_completeness=science["scientific_completeness"],
            scientific_gate_issues=science["scientific_gate_issues"],
            device_preflight=DevicePreflightV1(
                status="preflight_supported", snapshot_id="synthetic-device-snapshot",
                checked_capabilities=candidate.required_capabilities,
            ),
        )
        goal = RouteGoalV1(
            goal_id="state-audit-goal", target=candidate.target,
            constraint="open", required_fields=[STATE_PATH],
        )
        decision = decide_routes(goal, [candidate], lambda _candidate: receipt)
        missing_audit = "required_field_not_audited:" + STATE_PATH
        self.assertNotIn(missing_audit, decision.candidates[0].reasons)
        self.assertNotEqual(decision.status, "selected_for_planning")
        self.assertIn(
            "material_graph_step_unverified:MS_001", decision.candidates[0].reasons,
        )
        receipt.audited_field_paths.remove(STATE_PATH)
        control = decide_routes(goal, [candidate], lambda _candidate: receipt)
        self.assertIn(missing_audit, control.candidates[0].reasons)


if __name__ == "__main__":
    unittest.main()
