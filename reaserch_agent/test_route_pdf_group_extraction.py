"""The live PDF adapter only makes bounded, source-scoped proposals."""

from __future__ import annotations

from copy import deepcopy
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_pdf_group_extraction import (
    PdfGroupExtractionBudgetV1,
    propose_pdf_group_protocols,
)
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1,
    PdfSourceBlockV1,
)


class PdfGroupExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        digest = "sha256_" + "a" * 64
        self.groups = [
            PdfExperimentalGroupV1(
                source_scope=ExperimentalGroupScopeV1(
                    paper_id="paper-1", experimental_group_id="Group A",
                    section="Methods", locator="pdf:p1:b2-p1:b3",
                    source_digest=digest,
                ),
                source_document="/trusted/paper.pdf",
                blocks=(
                    PdfSourceBlockV1("pdf:p1:b2-p1:b2", "Group A"),
                    PdfSourceBlockV1(
                        "pdf:p1:b3-p1:b3", "Group A: mix 2 mmol salt solution."
                    ),
                ),
            ),
            PdfExperimentalGroupV1(
                source_scope=ExperimentalGroupScopeV1(
                    paper_id="paper-1", experimental_group_id="Group B",
                    section="Methods", locator="pdf:p1:b4-p1:b5",
                    source_digest=digest,
                ),
                source_document="/trusted/paper.pdf",
                blocks=(
                    PdfSourceBlockV1("pdf:p1:b4-p1:b4", "Group B"),
                    PdfSourceBlockV1(
                        "pdf:p1:b5-p1:b5", "Group B: XRD characterization."
                    ),
                ),
            ),
        ]
        self.keys = [
            (
                group.source_scope.paper_id,
                group.source_scope.experimental_group_id,
                group.source_scope.source_digest,
            )
            for group in self.groups
        ]
        self.roles = {
            self.keys[0]: "synthesis", self.keys[1]: "characterization",
        }
        self.capabilities = {self.keys[0]: ["mixing"]}

    def _proposal(self, index: int) -> dict:
        paper_id, group_id, digest = self.keys[index]
        proposal = {
            "source_group_ref": {
                "paper_id": paper_id,
                "experimental_group_id": group_id,
                "source_digest": digest,
            },
            "role_hint": "synthesis" if index == 0 else "characterization",
        }
        if index == 0:
            proposal.update({
                "target": {
                    "material": "product", "desired_state": "retained_wet_solid",
                    "objective": "prepare product",
                },
                "route_signature": {
                    "route_family": "mixing",
                    "target_transformation": "solution_to_wet_solid",
                    "operations": ["mix"],
                    "endpoint_state": "retained_wet_solid",
                },
                "material_graph": [{
                    "macro_step_id": "step-1", "macro_action_id": "action-1",
                    "sequence": 1, "operation": "mix", "sample_id": "sample-1",
                    "material_inputs": [], "material_outputs": [],
                    "provenance": {"kind": "paper", "reference": "fact:mix"},
                }],
                "route_facts": [{
                    "fact_id": "mix", "field_path": "material_graph[0].operation",
                    "value": "mix", "unit": "",
                    "excerpt": "mix 2 mmol salt solution",
                    "block_locator": "pdf:p1:b3-p1:b3", "required": True,
                }],
            })
        else:
            proposal["route_facts"] = []
        return proposal

    def _extract(self, callback, **overrides):
        options = {
            "group_roles_by_group": self.roles,
            "required_capabilities_by_group": self.capabilities,
        }
        options.update(overrides)
        return propose_pdf_group_protocols(self.groups, callback, **options)

    def test_all_groups_use_exact_scopes_and_reviewed_maps(self) -> None:
        prompts: list[str] = []

        def invoke(prompt: str) -> dict:
            prompts.append(prompt)
            return {"proposals": [self._proposal(1), self._proposal(0)]}

        result = self._extract(invoke)
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(prompts), 1)
        self.assertIn("Group A: mix 2 mmol salt solution.", prompts[0])
        self.assertIn("Group B: XRD characterization.", prompts[0])
        self.assertIn(self.keys[0][2], prompts[0])
        self.assertEqual(
            [protocol["experimental_group_id"] for protocol in result.protocols],
            ["Group A", "Group B"],
        )
        self.assertEqual(result.protocols[0]["group_role"], "synthesis")
        self.assertEqual(result.protocols[0]["required_capabilities"], ["mixing"])
        self.assertEqual(
            result.protocols[0]["route_facts"][0]["source"]["locator"],
            "pdf:p1:b3-p1:b3",
        )
        self.assertEqual(
            result.protocols[0]["source"]["source_document"],
            "/trusted/paper.pdf",
        )
        self.assertEqual(result.protocols[1]["group_role"], "characterization")
        self.assertNotIn("route_facts", result.protocols[1])
        self.assertNotIn("required_capabilities", result.protocols[1])

    def test_empty_inventory_and_input_budgets_never_call_model(self) -> None:
        def forbidden(_prompt: str) -> dict:
            self.fail("model must not be called")

        cases = [
            ([], PdfGroupExtractionBudgetV1(), "enumerated_group_inventory_empty"),
            (self.groups, PdfGroupExtractionBudgetV1(max_groups=1),
             "group_budget_exceeded"),
            (self.groups, PdfGroupExtractionBudgetV1(max_blocks=3),
             "block_budget_exceeded"),
            (self.groups, PdfGroupExtractionBudgetV1(max_prompt_chars=100),
             "prompt_char_budget_exceeded"),
        ]
        for groups, budget, reason in cases:
            with self.subTest(reason=reason):
                result = propose_pdf_group_protocols(
                    groups, forbidden,
                    group_roles_by_group=self.roles,
                    required_capabilities_by_group=self.capabilities,
                    budget=budget,
                )
                self.assertEqual(result.protocols, [])
                self.assertEqual(result.diagnostics[0].reason_code, reason)

    def test_reviewed_maps_are_required_before_invocation(self) -> None:
        def forbidden(_prompt: str) -> dict:
            self.fail("model must not be called")

        cases = [
            ({self.keys[1]: "characterization"}, self.capabilities,
             "trusted_group_role_missing"),
            ({self.keys[0]: "unknown", self.keys[1]: "characterization"},
             self.capabilities, "trusted_group_role_invalid"),
            (self.roles, {}, "trusted_capability_mapping_missing"),
            (self.roles, {self.keys[0]: ["mixing", "mixing"]},
             "trusted_capability_mapping_invalid"),
        ]
        for roles, capabilities, reason in cases:
            with self.subTest(reason=reason):
                result = self._extract(
                    forbidden, group_roles_by_group=roles,
                    required_capabilities_by_group=capabilities,
                )
                self.assertEqual(result.protocols, [])
                self.assertEqual(result.diagnostics[0].reason_code, reason)

    def test_duplicate_source_group_identity_never_calls_model(self) -> None:
        duplicate = deepcopy(self.groups[0])
        duplicate.source_scope.source_digest = "sha256_" + "b" * 64

        def forbidden(_prompt: str) -> dict:
            self.fail("model must not be called")

        result = propose_pdf_group_protocols(
            [self.groups[0], duplicate], forbidden,
            group_roles_by_group=self.roles,
            required_capabilities_by_group=self.capabilities,
        )
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.diagnostics[0].reason_code, "enumerated_group_duplicate"
        )

    def test_invalid_model_envelopes_and_callback_failure_abstain(self) -> None:
        for envelope in (
            [], {}, {"proposals": {}},
            {"proposals": [], "verified": True},
        ):
            with self.subTest(envelope=envelope):
                result = self._extract(lambda _prompt: envelope)
                self.assertEqual(result.protocols, [])
                self.assertEqual(
                    result.diagnostics[0].reason_code,
                    "proposal_model_envelope_invalid",
                )

        def failing(_prompt: str) -> dict:
            raise RuntimeError("model unavailable")

        result = self._extract(failing)
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.diagnostics[0].reason_code, "proposal_model_invocation_failed"
        )

    def test_reviewed_inputs_are_snapshotted_before_model_invocation(self) -> None:
        roles = dict(self.roles)
        capabilities = {key: list(value) for key, value in self.capabilities.items()}

        def invoke(_prompt: str) -> dict:
            roles[self.keys[0]] = "characterization"
            capabilities[self.keys[0]][0] = "invented"
            self.groups[0].source_scope.experimental_group_id = "Changed"
            return {"proposals": [self._proposal(0), self._proposal(1)]}

        result = self._extract(
            invoke, group_roles_by_group=roles,
            required_capabilities_by_group=capabilities,
        )
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(result.protocols[0]["group_role"], "synthesis")
        self.assertEqual(result.protocols[0]["required_capabilities"], ["mixing"])
        self.assertEqual(result.protocols[0]["experimental_group_id"], "Group A")

    def test_missing_duplicate_foreign_and_forged_proposals_abstain(self) -> None:
        good = [self._proposal(0), self._proposal(1)]
        wrong_block = deepcopy(good)
        wrong_block[0]["route_facts"][0]["block_locator"] = (
            self.groups[1].blocks[1].locator
        )
        forged_role = deepcopy(good)
        forged_role[0]["group_role"] = "characterization"
        forged_capability = deepcopy(good)
        forged_capability[0]["required_capabilities"] = ["invented"]
        forged_source = deepcopy(good)
        forged_source[0]["source"] = {"source_document": "elsewhere.pdf"}
        cases = [
            (good[:1], "enumerated_group_proposal_missing"),
            ([good[0], deepcopy(good[0]), good[1]], "duplicate_group_proposal"),
            (wrong_block, "fact_block_outside_group"),
            (forged_role, "proposal_source_or_status_field_forbidden"),
            (forged_capability, "proposal_source_or_status_field_forbidden"),
            (forged_source, "proposal_source_or_status_field_forbidden"),
        ]
        for proposals, reason in cases:
            with self.subTest(reason=reason):
                result = self._extract(lambda _prompt: {"proposals": proposals})
                self.assertEqual(result.protocols, [])
                self.assertIn(
                    reason, [item.reason_code for item in result.diagnostics]
                )

    def test_response_budget_and_invalid_response_values_abstain(self) -> None:
        result = self._extract(
            lambda _prompt: {"proposals": [self._proposal(0), self._proposal(1)]},
            budget=PdfGroupExtractionBudgetV1(max_response_chars=100),
        )
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.diagnostics[0].reason_code, "response_char_budget_exceeded"
        )
        result = self._extract(lambda _prompt: {"proposals": [float("nan")]})
        self.assertEqual(result.protocols, [])
        self.assertEqual(
            result.diagnostics[0].reason_code, "proposal_model_envelope_invalid"
        )

    def test_budget_configuration_requires_positive_integers(self) -> None:
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PdfGroupExtractionBudgetV1(max_groups=value)


if __name__ == "__main__":
    unittest.main()
