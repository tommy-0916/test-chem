"""Offline checks for experimental-group route discovery and abstention."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from chem_agent_contracts.route_candidate import RouteGoalV1, RouteTargetV1
from chem_agent_contracts.v2 import canonical_digest
from reaserch_agent.route_discovery import discover_route_candidates


def _digest(path: Path) -> str:
    return "sha256_" + hashlib.sha256(path.read_bytes()).hexdigest()


def _goal() -> RouteGoalV1:
    return RouteGoalV1(
        goal_id="synthetic-goal",
        target=RouteTargetV1(
            material="generic precipitate",
            desired_state="retained_wet_solid",
            objective="prepare a generic precipitate",
        ),
        constraint="open",
        required_fields=["precursor.amount"],
    )


def _group(
    path: Path,
    group_id: str,
    *,
    role: str = "synthesis",
    amount: int = 2,
) -> dict:
    digest = _digest(path)
    excerpt = f"Group {group_id}: use {amount} mmol generic metal salt to precipitate solid."
    evidence_id = f"evidence-{group_id}"
    field_scope = {
        "paper_id": "paper-1",
        "experimental_group_id": group_id,
        "section": "Methods",
        "locator": f"Methods > Group {group_id} > sentence 1",
        "source_digest": digest,
    }
    provenance = {
        "kind": "paper",
        "reference": evidence_id,
        "evidence_class": "paper_explicit",
        "source_path": "evidence_bundle.items[0].excerpt",
        "excerpt": excerpt,
        "source_digest": canonical_digest(excerpt),
    }
    return {
        "experimental_group_id": group_id,
        "group_role": role,
        "source": {
            "source_document": str(path),
            "section": "Methods",
            "locator": f"Methods > Group {group_id}",
            "source_digest": digest,
        },
        "steps": [{
            "step_role": role,
            "operation": "precipitate",
            "source": {
                "experimental_group_id": group_id,
                "locator": f"Methods > Group {group_id} > sentence 1",
            },
        }],
        "target": {
            "material": "generic precipitate",
            "desired_state": "retained_wet_solid",
            "objective": "prepare a generic precipitate",
        },
        "route_signature": {
            "route_family": "precipitation",
            "target_transformation": "solution_to_precipitate",
            "precursor_roles": ["metal_salt"],
            "reagent_roles": ["precipitant"],
            "operations": ["dissolve", "controlled_precipitation"],
            "control_modes": ["feedback_control"],
            "phase_transitions": [],
            "endpoint_state": "retained_wet_solid",
        },
        "evidence_bundle": [{
            "evidence_id": evidence_id,
            "title": "Synthetic Methods",
            "verification_status": "unassessed",
            "full_text_status": "parsed",
            "excerpt": excerpt,
        }],
        "evidence_matrix": [{
            "field_path": "precursor.amount",
            "value": amount,
            "unit": "mmol",
            "required": True,
            "status": "supported",
            "provenance": provenance,
            "evidence_id": evidence_id,
            "source_scope": field_scope,
        }],
        "material_graph": [{
            "macro_step_id": f"step-{group_id}",
            "macro_action_id": f"action-{group_id}",
            "sequence": 1,
            "operation": "controlled_precipitation",
            "sample_id": f"sample-{group_id}",
            "material_inputs": [{
                "material_id": "metal_salt",
                "material_instance_id": f"input-{group_id}",
                "name": "generic metal salt",
                "state": "solution",
                "provenance": provenance,
            }],
            "material_outputs": [{
                "material_id": "generic precipitate",
                "material_instance_id": f"output-{group_id}",
                "name": "generic precipitate",
                "state": "retained_wet_solid",
                "provenance": provenance,
            }],
            "provenance": provenance,
        }],
        "required_capabilities": ["controlled_dosing"],
    }


class RouteDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "primary_methods.txt"
        self.source.write_text(
            "Group A: use 2 mmol generic metal salt to precipitate solid.\n"
            "Group B: use 3 mmol generic metal salt to precipitate solid.\n"
            "Group C: test the product in electrolyte.\n",
            encoding="utf-8",
        )
        self.trusted = {"paper-1": [self.source]}

    def _discover(self, protocols: list[dict], *, trusted: bool = True):
        return discover_route_candidates(
            _goal(), protocols,
            trusted_source_paths=self.trusted if trusted else None,
        )

    def test_one_structured_group_is_unassessed_candidate_with_source_digest(self) -> None:
        group = _group(self.source, "A")
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.source_scope.experimental_group_id, "A")
        self.assertEqual(candidate.source_scope.source_digest, _digest(self.source))
        self.assertEqual(candidate.evidence_matrix[0].source_scope.locator,
                         "Methods > Group A > sentence 1")
        self.assertEqual(candidate.scientific_status, "unassessed")
        self.assertEqual(candidate.device_status, "unassessed")
        self.assertEqual(result.source_paths_by_digest[_digest(self.source)],
                         str(self.source.resolve()))

    def test_legacy_paper_level_protocol_without_group_fails_closed(self) -> None:
        legacy = {
            "source_title": "Legacy title",
            "source_file": str(self.source),
            "steps": [{"操作": "precipitate", "参数": "2 mmol"}],
        }
        result = self._discover([legacy])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "experimental_group_missing")

    def test_two_groups_are_separate_and_testing_group_is_excluded(self) -> None:
        a = _group(self.source, "A")
        b = _group(self.source, "B", amount=3)
        c = {"experimental_group_id": "C", "group_role": "performance_testing"}
        result = self._discover([{
            "paper_id": "paper-1", "experimental_groups": [a, b, c],
        }])
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(
            {candidate.source_scope.experimental_group_id for candidate in result.candidates},
            {"A", "B"},
        )
        self.assertNotEqual(result.candidates[0].route_id, result.candidates[1].route_id)
        for candidate in result.candidates:
            self.assertEqual(len(candidate.evidence_matrix), 1)
            self.assertEqual(
                candidate.evidence_matrix[0].source_scope.experimental_group_id,
                candidate.source_scope.experimental_group_id,
            )
        self.assertEqual(result.diagnostics[0].status, "excluded")
        self.assertEqual(result.diagnostics[0].reason_code,
                         "non_route_experimental_group")

    def test_independently_marked_context_section_is_excluded(self) -> None:
        result = self._discover([{
            "paper_id": "paper-1",
            "experimental_groups": [
                _group(self.source, "A"),
                {"experimental_group_id": "Materials", "group_role": "non_procedural"},
            ],
        }])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0].status, "excluded")
        self.assertEqual(result.diagnostics[0].reason_code,
                         "non_route_experimental_group")

    def test_field_from_other_experimental_group_is_not_imported(self) -> None:
        group = _group(self.source, "A")
        group["evidence_matrix"][0]["source_scope"]["experimental_group_id"] = "B"
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "experimental_group_mixing")

    def test_testing_step_inside_synthesis_group_is_rejected(self) -> None:
        group = _group(self.source, "A")
        group["steps"].append({
            "step_role": "performance_testing",
            "operation": "test product",
            "source": {"experimental_group_id": "A"},
        })
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "non_route_step_in_experimental_group")

    def test_source_digest_mismatch_blocks_group(self) -> None:
        group = _group(self.source, "A")
        group["source"]["source_digest"] = "sha256_" + "0" * 64
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code, "source_digest_mismatch")

    def test_field_provenance_must_name_the_same_trusted_document(self) -> None:
        group = _group(self.source, "A")
        group["evidence_matrix"][0]["provenance"]["source_path"] = "other.pdf"
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "field_provenance_path_mismatch")

    def test_reported_path_needs_independent_trusted_path(self) -> None:
        group = _group(self.source, "A")
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}],
                                trusted=False)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "trusted_source_document_missing")

    def test_missing_structured_fields_yields_diagnostic_instead_of_guess(self) -> None:
        group = _group(self.source, "A")
        group.pop("route_signature")
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "structured_route_field_missing:route_signature")

    def test_capability_string_is_not_split_into_character_ids(self) -> None:
        group = _group(self.source, "A")
        group["required_capabilities"] = "heating"
        result = self._discover([{"paper_id": "paper-1", "experimental_groups": [group]}])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "required_capabilities_invalid")

    def test_duplicate_group_records_are_not_merged(self) -> None:
        group = _group(self.source, "A")
        result = self._discover([{
            "paper_id": "paper-1",
            "experimental_groups": [group, deepcopy(group)],
        }])
        self.assertEqual(result.candidates, [])
        self.assertEqual(
            [item.reason_code for item in result.diagnostics],
            ["duplicate_experimental_group_scope"] * 2,
        )


if __name__ == "__main__":
    unittest.main()
