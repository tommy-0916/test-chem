"""PDF group proposals cannot choose their own source or borrow other arms."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chem_agent_contracts.route_candidate import RouteGoalV1, RouteTargetV1
from reaserch_agent.route_attestation import AttestedRouteSourceV1
from reaserch_agent.route_discovery import discover_route_candidates
from reaserch_agent.route_group_compiler import compile_experimental_group_protocols
from reaserch_agent.route_pdf_group_proposals import associate_pdf_group_proposals
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfGroupEnumerationResultV1, PdfSourceBlockV1,
    enumerate_pdf_experimental_groups,
)
from reaserch_agent.route_pdf_source import verify_route_pdf_source
from reaserch_agent.route_pipeline import evaluate_route_decision_v1


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class PdfGroupProposalAssociationTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "methods.pdf"
        self.quotes = {
            "Group A": (
                "Group A precipitation solution_to_wet_solid: mix 2 mmol "
                "salt solution into product retained_wet_solid."
            ),
            "Group B": (
                "Group B precipitation solution_to_wet_solid: mix 3 mmol "
                "salt solution into product retained_wet_solid."
            ),
        }
        document = fitz.open()
        page = document.new_page()
        for index, (text, size, font) in enumerate([
            ("Methods", 16, "hebo"),
            ("Group A", 14, "hebo"),
            (self.quotes["Group A"], 10, "helv"),
            ("Group B", 14, "hebo"),
            (self.quotes["Group B"], 10, "helv"),
        ]):
            page.insert_text(fitz.Point(72, 70 + index * 48), text,
                             fontsize=size, fontname=font)
        document.save(str(self.source))
        document.close()
        indexed = enumerate_pdf_experimental_groups(
            {"paper-1": self.source}, source_root=self.root,
        )
        self.assertEqual(indexed.diagnostics, [])
        self.groups = indexed.groups
        self.assertEqual(len(self.groups), 2)

    def _proposal(self, group_index: int) -> dict:
        group = self.groups[group_index]
        group_id = group.source_scope.experimental_group_id
        quote = self.quotes[group_id]
        amount = 2 if group_id == "Group A" else 3
        block_locator = group.blocks[1].locator
        claims = [
            ("signature", "route_signature.route_family", "precipitation", ""),
            ("signature", "route_signature.target_transformation", "solution_to_wet_solid", ""),
            ("signature", "route_signature.precursor_roles[0]", "salt", ""),
            ("signature", "route_signature.operations[0]", "mix", ""),
            ("signature", "route_signature.endpoint_state", "retained_wet_solid", ""),
            ("step", "material_graph[0].operation", "mix", ""),
            ("input", "material_graph[0].material_inputs[0].material_id", "salt", ""),
            ("input", "material_graph[0].material_inputs[0].name", "salt", ""),
            ("input", "material_graph[0].material_inputs[0].state", "solution", ""),
            ("input", "material_graph[0].material_inputs[0].quantity.value", amount, "mmol"),
            ("output", "material_graph[0].material_outputs[0].material_id", "product", ""),
            ("output", "material_graph[0].material_outputs[0].name", "product", ""),
            ("output", "material_graph[0].material_outputs[0].state", "retained_wet_solid", ""),
        ]
        return {
            "source_group_ref": {
                "paper_id": group.source_scope.paper_id,
                "experimental_group_id": group_id,
                "source_digest": group.source_scope.source_digest,
            },
            "role_hint": "synthesis",
            "target": {
                "material": "product", "desired_state": "retained_wet_solid",
                "objective": "prepare product",
            },
            "route_signature": {
                "route_family": "precipitation",
                "target_transformation": "solution_to_wet_solid",
                "precursor_roles": ["salt"],
                "operations": ["mix"],
                "endpoint_state": "retained_wet_solid",
            },
            "material_graph": [{
                "macro_step_id": f"step-{group_index}",
                "macro_action_id": f"action-{group_index}",
                "sequence": 1, "operation": "mix",
                "sample_id": f"sample-{group_index}",
                "provenance": {"kind": "paper", "reference": "fact:step"},
                "material_inputs": [{
                    "material_id": "salt", "material_instance_id": f"salt-{group_index}",
                    "name": "salt", "state": "solution",
                    "material_origin": "external_inventory",
                    "quantity": {"mode": "exact", "value": amount, "unit": "mmol"},
                    "provenance": {"kind": "paper", "reference": "fact:input"},
                }],
                "material_outputs": [{
                    "material_id": "product",
                    "material_instance_id": f"product-{group_index}",
                    "name": "product", "state": "retained_wet_solid",
                    "provenance": {"kind": "paper", "reference": "fact:output"},
                }],
            }],
            "route_facts": [{
                "fact_id": fact_id, "field_path": path, "value": value,
                "unit": unit, "excerpt": quote,
                "block_locator": block_locator,
            } for fact_id, path, value, unit in claims],
        }

    def _associate(self, proposals: list[dict]):
        capabilities = {
            (
                group.source_scope.paper_id,
                group.source_scope.experimental_group_id,
                group.source_scope.source_digest,
            ): ["mixing"]
            for group in self.groups
        }
        roles = {key: "synthesis" for key in capabilities}
        return associate_pdf_group_proposals(
            self.groups, proposals,
            required_capabilities_by_group=capabilities,
            group_roles_by_group=roles,
        )

    def test_two_groups_bind_to_own_scopes_then_existing_compiler(self) -> None:
        proposals = [self._proposal(1), self._proposal(0)]
        result = self._associate(proposals)
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(result.protocols), 2)
        self.assertEqual([item["experimental_group_id"] for item in result.protocols],
                         ["Group A", "Group B"])
        self.assertEqual(result.protocols[0]["source"]["source_document"],
                         str(self.source))
        self.assertEqual(result.protocols[0]["route_facts"][0]["source"]["locator"],
                         "pdf:p1:b3-p1:b3")
        self.assertNotIn("source", proposals[0]["route_facts"][0])
        compiled = compile_experimental_group_protocols(result.protocols)
        self.assertEqual(compiled.diagnostics, [])
        goal = RouteGoalV1(
            goal_id="goal", target=RouteTargetV1(
                material="product", desired_state="retained_wet_solid",
                objective="prepare product",
            ),
            constraint="open",
            required_fields=["material_graph[0].material_inputs[0].quantity.value"],
        )
        discovery = discover_route_candidates(
            goal, compiled.protocols,
            trusted_source_paths={"paper-1": [self.source]},
        )
        self.assertEqual(discovery.diagnostics, [])
        self.assertEqual(len(discovery.candidates), 2)
        for candidate in discovery.candidates:
            verified = verify_route_pdf_source(
                candidate, source_paths={"paper-1": self.source},
                source_root=self.root,
            )
            self.assertTrue(verified.source_scope_verified, verified.reasons)
            self.assertIsNone(verified.source_route_signature)

    def test_missing_group_abstains_without_partial_protocols(self) -> None:
        result = self._associate([self._proposal(0)])
        self.assertEqual(result.protocols, [])
        self.assertIn("enumerated_group_proposal_missing",
                      [item.reason_code for item in result.diagnostics])

    def test_neighboring_group_block_cannot_supply_a_fact(self) -> None:
        proposal_a = self._proposal(0)
        proposal_b = self._proposal(1)
        proposal_a["route_facts"][0]["block_locator"] = self.groups[1].blocks[1].locator
        result = self._associate([proposal_a, proposal_b])
        self.assertEqual(result.protocols, [])
        self.assertIn("fact_block_outside_group",
                      [item.reason_code for item in result.diagnostics])

    def test_duplicate_group_proposal_abstains(self) -> None:
        proposal_a = self._proposal(0)
        result = self._associate([proposal_a, deepcopy(proposal_a), self._proposal(1)])
        self.assertEqual(result.protocols, [])
        self.assertIn("duplicate_group_proposal",
                      [item.reason_code for item in result.diagnostics])

    def test_proposal_cannot_override_source_document_or_status(self) -> None:
        proposal_a = self._proposal(0)
        proposal_a["source"] = {
            "source_document": "elsewhere.pdf", "source_digest": "sha256_forged",
        }
        result = self._associate([proposal_a, self._proposal(1)])
        self.assertEqual(result.protocols, [])
        self.assertIn("proposal_source_or_status_field_forbidden",
                      [item.reason_code for item in result.diagnostics])

    def test_excerpt_must_be_in_the_named_block(self) -> None:
        proposal_a = self._proposal(0)
        proposal_a["route_facts"][0]["excerpt"] = "not present in original"
        result = self._associate([proposal_a, self._proposal(1)])
        self.assertEqual(result.protocols, [])
        self.assertIn("fact_excerpt_not_in_block",
                      [item.reason_code for item in result.diagnostics])

    def test_split_layout_quote_binds_exact_short_source_range(self) -> None:
        group = self.groups[0]
        split_group = PdfExperimentalGroupV1(
            source_scope=group.source_scope,
            source_document=group.source_document,
            blocks=(
                group.blocks[0],
                PdfSourceBlockV1("pdf:p1:b3-p1:b3", "mix 2 mmol"),
                PdfSourceBlockV1(
                    "pdf:p1:b4-p1:b4", "salt solution into product retained_wet_solid",
                ),
            ),
        )
        proposal_a = self._proposal(0)
        for fact in proposal_a["route_facts"]:
            fact["excerpt"] = "mix 2 mmol\n salt solution into product retained_wet_solid"
        keys = {
            (item.source_scope.paper_id, item.source_scope.experimental_group_id,
             item.source_scope.source_digest): "synthesis"
            for item in (split_group, self.groups[1])
        }
        result = associate_pdf_group_proposals(
            [split_group, self.groups[1]], [proposal_a, self._proposal(1)],
            group_roles_by_group=keys,
        )
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(
            result.protocols[0]["route_facts"][0]["source"]["locator"],
            "pdf:p1:b3-p1:b4",
        )
        proposal_a["route_facts"][0]["block_locator"] = group.blocks[0].locator
        wrong = associate_pdf_group_proposals(
            [split_group, self.groups[1]], [proposal_a, self._proposal(1)],
            group_roles_by_group=keys,
        )
        self.assertEqual(wrong.protocols, [])
        self.assertIn("fact_block_locator_not_in_excerpt_span",
                      [item.reason_code for item in wrong.diagnostics])

    def test_parser_marked_caption_is_skipped_only_between_prose_blocks(self) -> None:
        group = self.groups[0]
        split_group = PdfExperimentalGroupV1(
            source_scope=group.source_scope.model_copy(update={
                "locator": "pdf:p1:b2-p2:b2",
            }),
            source_document=group.source_document,
            blocks=(
                group.blocks[0],
                PdfSourceBlockV1("pdf:p1:b3-p1:b3", "mix 2 mmol"),
                PdfSourceBlockV1("pdf:p2:b1-p2:b1", "Scheme 1. Layout caption", True),
                PdfSourceBlockV1(
                    "pdf:p2:b2-p2:b2",
                    "salt solution into product retained_wet_solid.",
                ),
            ),
        )
        proposal_a = self._proposal(0)
        for fact in proposal_a["route_facts"]:
            fact["excerpt"] = "mix 2 mmol salt solution into product retained_wet_solid."
        result = associate_pdf_group_proposals(
            [split_group, self.groups[1]], [proposal_a, self._proposal(1)],
        )
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(
            result.protocols[0]["route_facts"][0]["source"]["locator"],
            "pdf:p1:b3-p2:b2",
        )
        unmarked_group = PdfExperimentalGroupV1(
            source_scope=split_group.source_scope,
            source_document=split_group.source_document,
            blocks=tuple(
                PdfSourceBlockV1(block.locator, block.text)
                for block in split_group.blocks
            ),
        )
        blocked = associate_pdf_group_proposals(
            [unmarked_group, self.groups[1]], [proposal_a, self._proposal(1)],
        )
        self.assertEqual(blocked.protocols, [])
        self.assertIn("fact_excerpt_not_in_block",
                      [item.reason_code for item in blocked.diagnostics])

    def test_stale_digest_group_reference_abstains(self) -> None:
        proposal_a = self._proposal(0)
        proposal_a["source_group_ref"]["source_digest"] = "sha256_" + "0" * 64
        result = self._associate([proposal_a, self._proposal(1)])
        self.assertEqual(result.protocols, [])
        self.assertIn("proposal_group_not_enumerated",
                      [item.reason_code for item in result.diagnostics])

    def test_model_cannot_supply_capability_ids(self) -> None:
        proposal_a = self._proposal(0)
        proposal_a["required_capabilities"] = ["invented_device"]
        result = self._associate([proposal_a, self._proposal(1)])
        self.assertEqual(result.protocols, [])
        self.assertIn("proposal_source_or_status_field_forbidden",
                      [item.reason_code for item in result.diagnostics])

    def test_model_non_route_role_hint_does_not_exclude_known_arm(self) -> None:
        proposal_b = self._proposal(1)
        proposal_b["role_hint"] = "characterization"
        result = associate_pdf_group_proposals(
            self.groups, [self._proposal(0), proposal_b],
        )
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(result.protocols[1]["group_role"], "unclassified")
        self.assertEqual(result.protocols[1]["role_hint"], "characterization")

    def test_independently_reviewed_non_route_role_can_be_preserved(self) -> None:
        proposal_b = self._proposal(1)
        proposal_b["role_hint"] = "synthesis"
        scope = self.groups[1].source_scope
        result = associate_pdf_group_proposals(
            self.groups, [self._proposal(0), proposal_b],
            group_roles_by_group={
                (scope.paper_id, scope.experimental_group_id, scope.source_digest):
                "characterization",
            },
        )
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(result.protocols[1]["group_role"], "characterization")
        self.assertEqual(result.protocols[0]["group_role"], "unclassified")

    def test_empty_reviewed_characterization_does_not_block_synthesis_group(self) -> None:
        synthesis = self.groups[0].source_scope
        characterization = self.groups[1].source_scope
        synthesis_key = (
            synthesis.paper_id, synthesis.experimental_group_id,
            synthesis.source_digest,
        )
        characterization_key = (
            characterization.paper_id, characterization.experimental_group_id,
            characterization.source_digest,
        )
        characterization_proposal = self._proposal(1)
        characterization_proposal["route_facts"] = []
        associated = associate_pdf_group_proposals(
            self.groups, [self._proposal(0), characterization_proposal],
            required_capabilities_by_group={synthesis_key: ["mixing"]},
            group_roles_by_group={
                synthesis_key: "synthesis",
                characterization_key: "characterization",
            },
        )
        self.assertEqual(associated.diagnostics, [])
        self.assertNotIn("route_facts", associated.protocols[1])
        compiled = compile_experimental_group_protocols(associated.protocols)
        self.assertEqual(compiled.diagnostics, [])
        goal = RouteGoalV1(
            goal_id="goal", target=RouteTargetV1(
                material="product", desired_state="retained_wet_solid",
                objective="prepare product",
            ),
            constraint="open",
            required_fields=["material_graph[0].material_inputs[0].quantity.value"],
        )
        discovery = discover_route_candidates(
            goal, compiled.protocols,
            trusted_source_paths={"paper-1": [self.source]},
        )
        self.assertEqual(len(discovery.candidates), 1)
        self.assertEqual(
            discovery.candidates[0].source_scope.experimental_group_id, "Group A"
        )
        self.assertTrue(any(
            item.reason_code == "non_route_experimental_group"
            and item.status == "excluded"
            and item.experimental_group_id == "Group B"
            for item in discovery.diagnostics
        ))
        receipt = AttestedRouteSourceV1(
            paper_id="paper-1", path=self.source,
            document_digest=synthesis.source_digest,
            document_kind="primary_paper", doi="10.1000/study",
            attestation_digest="sha256_" + "a" * 64,
        )
        with patch(
            "reaserch_agent.route_pipeline.attested_route_sources",
            return_value={"paper-1": [receipt]},
        ), patch(
            "reaserch_agent.route_pipeline.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=self.groups),
        ):
            evaluated = evaluate_route_decision_v1(
                goal, associated.protocols, source_root=self.root,
                verified_capabilities_by_group={synthesis_key: ["mixing"]},
                verified_group_roles_by_group={
                    synthesis_key: "synthesis",
                    characterization_key: "characterization",
                },
            )
        self.assertEqual(evaluated.compilation_diagnostics, [])
        self.assertNotIn(
            "candidate_discovery_incomplete", evaluated.decision.decision_reasons
        )

    def test_model_group_role_field_is_rejected(self) -> None:
        proposal_b = self._proposal(1)
        proposal_b["group_role"] = "characterization"
        result = self._associate([self._proposal(0), proposal_b])
        self.assertEqual(result.protocols, [])
        self.assertIn("proposal_source_or_status_field_forbidden",
                      [item.reason_code for item in result.diagnostics])


if __name__ == "__main__":
    unittest.main()
