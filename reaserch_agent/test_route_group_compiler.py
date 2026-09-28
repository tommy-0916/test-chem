"""The route fact compiler must preserve experimental-group boundaries."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_candidate import (
    RouteCandidateV1, RouteGoalV1, RouteTargetV1,
)
from reaserch_agent.route_discovery import discover_route_candidates
from reaserch_agent.route_group_compiler import (
    _fact_issue, compile_experimental_group_protocols,
    dimensionless_numeric_field_pending, material_identity_for_amount_path,
    quantity_has_local_attribution,
)
from reaserch_agent.route_science import audit_route_candidate_science
from reaserch_agent.route_source import verify_route_source


class RouteGroupCompilerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "methods.md"
        self.excerpt = (
            "In this precipitation, metal_salt is used for the "
            "solution_to_wet_solid transformation: precipitate product as "
            "retained_wet_solid from 2 mmol metal salt solution (salt)."
        )
        signature = {
            "route_family": "precipitation",
            "target_transformation": "solution_to_wet_solid",
            "precursor_roles": ["metal_salt"],
            "reagent_roles": [],
            "operations": ["precipitate"],
            "control_modes": [],
            "phase_transitions": [],
            "endpoint_state": "retained_wet_solid",
        }
        self.source.write_text("\n".join([
            "# Primary study", "## Methods", "### Group A", self.excerpt,
            "```chem-agent-route-signature-v1", json.dumps(signature), "```",
            "### Group B", "Add 3 mmol metal salt to make product.",
        ]) + "\n", encoding="utf-8")
        self.digest = "sha256_" + sha256(self.source.read_bytes()).hexdigest()
        self.target = {
            "material": "product", "desired_state": "retained_wet_solid",
            "objective": "prepare product",
        }
        self.signature = signature

    def _group(self) -> dict:
        source = {
            "paper_id": "paper-A", "experimental_group_id": "Group A",
            "section": "Methods", "locator": "lines:4-4",
            "source_digest": self.digest,
        }
        raw_claims = [
            ("signature", "route_signature.route_family", "precipitation", ""),
            ("signature", "route_signature.target_transformation", "solution_to_wet_solid", ""),
            ("signature", "route_signature.precursor_roles[0]", "metal_salt", ""),
            ("signature", "route_signature.operations[0]", "precipitate", ""),
            ("signature", "route_signature.endpoint_state", "retained_wet_solid", ""),
            ("step_op", "material_graph[0].operation", "precipitate", ""),
            ("salt_amount", "material_graph[0].material_inputs[0].name", "metal salt", ""),
            ("salt_amount", "material_graph[0].material_inputs[0].state", "solution", ""),
            ("salt_amount", "material_graph[0].material_inputs[0].quantity.value", 2, "mmol"),
            ("out", "material_graph[0].material_outputs[0].name", "product", ""),
            ("out", "material_graph[0].material_outputs[0].state", "retained_wet_solid", ""),
        ]
        facts = [
            {
                "fact_id": fact_id, "field_path": path, "value": value,
                "unit": unit, "excerpt": self.excerpt, "source": deepcopy(source),
            }
            for fact_id, path, value, unit in raw_claims
        ]
        salt_provenance = {"kind": "paper", "reference": "fact:salt_amount"}
        return {
            "experimental_group_id": "Group A", "group_role": "synthesis",
            "source": {
                "source_document": str(self.source), "section": "Methods",
                "locator": "lines:3-7", "source_digest": self.digest,
            },
            "target": self.target,
            "route_signature": self.signature,
            "material_graph": [{
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "precipitate", "sample_id": "sample-A",
                "provenance": {"kind": "paper", "reference": "fact:step_op"},
                "material_inputs": [{
                    "material_id": "salt", "material_instance_id": "salt-A",
                    "name": "metal salt", "state": "solution",
                    "material_origin": "external_inventory",
                    "quantity": {"mode": "exact", "value": 2, "unit": "mmol"},
                    "provenance": deepcopy(salt_provenance),
                }],
                "material_outputs": [{
                    "material_id": "product", "material_instance_id": "product-A",
                    "name": "product", "state": "retained_wet_solid",
                    "provenance": {"kind": "paper", "reference": "fact:out"},
                }],
            }],
            "required_capabilities": ["precipitate"],
            "route_facts": facts,
        }

    def _protocol(self) -> dict:
        return {"paper_id": "paper-A", "source_title": "Primary study",
                "experimental_groups": [self._group()]}

    def _compile(self, protocol: dict):
        return compile_experimental_group_protocols([protocol])

    def test_full_fact_binding_produces_discoverable_source_verified_candidate(self) -> None:
        original = self._protocol()
        result = self._compile(original)
        self.assertEqual(result.diagnostics, [])
        self.assertNotIn("evidence_bundle", original["experimental_groups"][0])
        group = result.protocols[0]["experimental_groups"][0]
        self.assertEqual(group["evidence_bundle"][0]["verification_status"], "unassessed")
        self.assertEqual(
            next(item["value"] for item in group["evidence_matrix"]
                 if item["field_path"].endswith("quantity.value")), 2,
        )
        self.assertEqual(
            group["material_graph"][0]["material_inputs"][0]["provenance"],
            next(item["provenance"] for item in group["evidence_matrix"]
                 if item["field_path"].endswith("quantity.value")),
        )
        goal = RouteGoalV1(
            goal_id="goal", target=RouteTargetV1.model_validate(self.target),
            constraint="open", required_fields=[
                "material_graph[0].material_inputs[0].quantity.value",
            ],
        )
        discovery = discover_route_candidates(
            goal, result.protocols,
            trusted_source_paths={"paper-A": [self.source]},
        )
        self.assertEqual(discovery.diagnostics, [])
        self.assertEqual(len(discovery.candidates), 1)
        verification = verify_route_source(
            discovery.candidates[0], source_paths={"paper-A": self.source},
            source_root=self.root,
        )
        self.assertTrue(verification.source_scope_verified, verification.reasons)
        self.assertTrue(set(goal.required_fields).issubset(verification.verified_field_paths))

    def test_one_port_can_bind_distinct_name_state_and_quantity_facts(self) -> None:
        protocol = self._protocol()
        group = protocol["experimental_groups"][0]
        port = group["material_graph"][0]["material_inputs"][0]
        fact_ids = {
            "name": "salt_name", "state": "salt_state",
            "quantity.value": "salt_quantity",
        }
        for fact in group["route_facts"]:
            for suffix, fact_id in fact_ids.items():
                if fact["field_path"] == (
                    "material_graph[0].material_inputs[0]." + suffix
                ):
                    fact["fact_id"] = fact_id
        port["provenance"] = {"kind": "paper", "reference": "fact:salt_name"}
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics, [])
        compiled = result.protocols[0]["experimental_groups"][0]
        fields = {item["field_path"]: item for item in compiled["evidence_matrix"]}
        quantity = fields["material_graph[0].material_inputs[0].quantity.value"]
        self.assertEqual(quantity["value"], 2)
        self.assertEqual(quantity["unit"], "mmol")
        self.assertNotEqual(
            quantity["provenance"],
            compiled["material_graph"][0]["material_inputs"][0]["provenance"],
        )
        goal = RouteGoalV1(
            goal_id="goal", target=RouteTargetV1.model_validate(self.target),
            constraint="open", required_fields=[
                "material_graph[0].material_inputs[0].quantity.value",
            ],
        )
        discovered = discover_route_candidates(
            goal, result.protocols,
            trusted_source_paths={"paper-A": [self.source]},
        )
        self.assertEqual(discovered.diagnostics, [])
        candidate_data = discovered.candidates[0].model_dump(mode="json")
        for item in candidate_data["evidence_bundle"]:
            item["verification_status"] = "verified_doi"
            item["full_text_status"] = "parsed"
        # Synthetic source identity is controlled by this fixture. This test
        # checks the compiler/science association, not source authenticity.
        audited = audit_route_candidate_science(
            RouteCandidateV1.model_validate(candidate_data)
        )
        self.assertIn(
            "material_graph[0].material_inputs[0].name",
            audited["audited_field_paths"],
        )
        self.assertIn(
            "material_graph[0].material_inputs[0].quantity.value",
            audited["audited_field_paths"],
        )

    def test_nonstring_unit_cannot_be_erased_into_discoverable_evidence(self) -> None:
        protocol = self._protocol()
        # A qualitative claim normally has an empty unit. A malformed unit
        # must not be coerced to "" and published as a paper-explicit fact.
        protocol["experimental_groups"][0]["route_facts"][0]["unit"] = 123
        result = self._compile(protocol)
        self.assertEqual(
            result.diagnostics[0].reason_code, "route_fact_unit_invalid",
        )
        group = result.protocols[0]["experimental_groups"][0]
        self.assertNotIn("evidence_matrix", group)
        goal = RouteGoalV1(
            goal_id="goal", target=RouteTargetV1.model_validate(self.target),
            constraint="open", required_fields=[
                "material_graph[0].material_inputs[0].quantity.value",
            ],
        )
        discovery = discover_route_candidates(
            goal, result.protocols,
            trusted_source_paths={"paper-A": [self.source]},
        )
        self.assertEqual(discovery.candidates, [])
        # There is no candidate to present to SourceVerifier as verified.

    def test_cross_group_fact_does_not_compile(self) -> None:
        protocol = self._protocol()
        protocol["experimental_groups"][0]["route_facts"][0]["source"][
            "experimental_group_id"
        ] = "Group B"
        result = self._compile(protocol)
        self.assertEqual(
            result.diagnostics[0].reason_code,
            "route_fact_experimental_group_mismatch",
        )
        self.assertNotIn("evidence_matrix", result.protocols[0]["experimental_groups"][0])

    def test_same_quote_other_material_quantity_cannot_compile(self) -> None:
        protocol = self._protocol()
        group = protocol["experimental_groups"][0]
        wrong_quote = self.excerpt.replace(
            "2 mmol metal salt solution (salt)",
            "1 mmol metal salt solution (salt) and 2 mmol other salt",
        )
        for fact in group["route_facts"]:
            fact["excerpt"] = wrong_quote
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code,
                         "route_fact_quantity_attribution_unresolved")
        self.assertNotIn("evidence_matrix", result.protocols[0]["experimental_groups"][0])

        group["material_graph"][0]["material_inputs"][0]["quantity"]["value"] = 1
        for fact in group["route_facts"]:
            if fact["field_path"].endswith("quantity.value"):
                fact["value"] = 1
        correct = self._compile(protocol)
        self.assertEqual(correct.diagnostics, [])

    def test_step_concentration_without_material_port_uses_unique_literal(self) -> None:
        identity, required = material_identity_for_amount_path(
            "material_graph[0].concentration_value", [{}],
        )
        self.assertEqual((identity, required), ("", False))
        self.assertTrue(quantity_has_local_attribution(
            "The solution was 1 M.", 1, "M", identity=identity,
            identity_required=required,
        ))
        self.assertFalse(quantity_has_local_attribution(
            "A 1 M and B 2 M.", 2, "M", identity=identity,
            identity_required=required,
        ))

    def test_dimensionless_numeric_path_is_pending_not_unit_exempt(self) -> None:
        graph = [{
            "parameters": [
                {"name": "pH", "value": 10, "unit": ""},
                {"name": "molar_ratio", "value": 2, "unit": ""},
                {"name": "temperature", "value": 10, "unit": ""},
            ],
            "logical_containers": [{"count": 2}],
            "material_inputs": [{"quantity": {"value": 2, "unit": ""}}],
        }]
        def reason(path: str, value: int) -> str:
            fact = {
                "fact_id": "numeric", "field_path": path,
                "value": value, "unit": "", "excerpt": "pH 10 and 2 mmol salt",
                "source": {
                    "paper_id": "paper-A", "experimental_group_id": "Group A",
                    "section": "Methods", "locator": "lines:4-4",
                    "source_digest": self.digest,
                },
            }
            return _fact_issue(
                fact, paper_id="paper-A", group_id="Group A",
                section="Methods", document_digest=self.digest,
                graph=graph, signature={},
            )

        self.assertTrue(dimensionless_numeric_field_pending(
            "material_graph[0].parameters[0].value", graph,
        ))
        self.assertEqual(reason("material_graph[0].parameters[0].value", 10),
                         "dimensionless_semantic_pending")
        self.assertEqual(reason("material_graph[0].parameters[1].value", 2),
                         "dimensionless_semantic_pending")
        self.assertEqual(reason("material_graph[0].logical_containers[0].count", 2),
                         "dimensionless_semantic_pending")
        self.assertEqual(reason("material_graph[0].parameters[2].value", 10),
                         "route_fact_numeric_unit_missing")
        self.assertEqual(
            reason("material_graph[0].material_inputs[0].quantity.value", 2),
            "route_fact_numeric_unit_missing",
        )

    def test_intervening_entity_is_not_an_attribution_link(self) -> None:
        self.assertFalse(quantity_has_local_attribution(
            "A b 2 mmol", 2, "mmol", identity="A", identity_required=True,
        ))
        self.assertTrue(quantity_has_local_attribution(
            "2 mmol of A", 2, "mmol", identity="A", identity_required=True,
        ))
        self.assertFalse(quantity_has_local_attribution(
            "A 2 mmol", 10 ** 5000, "mmol", identity="A",
            identity_required=True,
        ))

    def test_missing_excerpt_does_not_compile(self) -> None:
        protocol = self._protocol()
        protocol["experimental_groups"][0]["route_facts"][0].pop("excerpt")
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code, "route_fact_excerpt_missing")

    def test_unreferenced_numeric_graph_leaf_does_not_compile(self) -> None:
        protocol = self._protocol()
        protocol["experimental_groups"][0]["material_graph"][0][
            "logical_containers"
        ] = [{"logical_container_id": "C1", "count": 2}]
        result = self._compile(protocol)
        self.assertEqual(
            result.diagnostics[0].reason_code,
            "route_fact_numeric_graph_claim_missing",
        )

    def test_missing_graph_is_not_invented(self) -> None:
        protocol = self._protocol()
        protocol["experimental_groups"][0].pop("material_graph")
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code, "route_group_material_graph_missing")

    def test_unbound_graph_paper_provenance_does_not_compile(self) -> None:
        protocol = self._protocol()
        protocol["experimental_groups"][0]["material_graph"][0][
            "material_outputs"
        ][0]["provenance"] = {"kind": "paper", "reference": "fact:other"}
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code,
                         "route_fact_graph_provenance_mismatch")

    def test_signature_operation_needs_its_own_literal_fact(self) -> None:
        protocol = self._protocol()
        facts = protocol["experimental_groups"][0]["route_facts"]
        facts[:] = [fact for fact in facts
                    if fact["field_path"] != "route_signature.operations[0]"]
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code, "route_fact_signature_claim_missing")

    def test_material_state_without_any_fact_stays_blocked(self) -> None:
        protocol = self._protocol()
        facts = protocol["experimental_groups"][0]["route_facts"]
        facts[:] = [fact for fact in facts
                    if fact["field_path"] != "material_graph[0].material_outputs[0].state"]
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code,
                         "route_fact_qualitative_graph_claim_missing")

    def test_material_state_cannot_come_from_model_default(self) -> None:
        protocol = self._protocol()
        port = protocol["experimental_groups"][0]["material_graph"][0][
            "material_inputs"
        ][0]
        port.pop("state")
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code,
                         "route_group_material_state_missing")

    def test_legacy_protocol_is_passed_through_without_new_claims(self) -> None:
        protocol = {"source_title": "old", "steps": [{"操作": "mix", "参数": "2 mmol"}]}
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(result.protocols, [protocol])

    def test_fact_cannot_mark_itself_optional(self) -> None:
        protocol = self._protocol()
        protocol["experimental_groups"][0]["route_facts"][0]["required"] = False
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics[0].reason_code, "route_fact_required_must_be_true")

    def test_pdf_fact_locator_is_accepted_as_a_proposal(self) -> None:
        protocol = self._protocol()
        for fact in protocol["experimental_groups"][0]["route_facts"]:
            fact["source"]["locator"] = "pdf:p1:b3-p1:b3"
        result = self._compile(protocol)
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(
            result.protocols[0]["experimental_groups"][0]["evidence_matrix"][0]
            ["source_scope"]["locator"],
            "pdf:p1:b3-p1:b3",
        )


if __name__ == "__main__":
    unittest.main()
