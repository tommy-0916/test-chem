"""The offline replay reports trust gaps without fabricating route evidence."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import tempfile
import unittest

from reaserch_agent.offline_route_replay import main, replay_route_evaluation


GOAL = {
    "goal_id": "generic-offline-goal",
    "target": {
        "material": "sample",
        "desired_state": "solution",
        "objective": "prepare sample",
    },
    "constraint": "open",
    "required_fields": ["material_graph[0].material_inputs[0].quantity.value"],
}


class OfflineRouteReplayTest(unittest.TestCase):
    def test_missing_protocols_and_signed_authority_stay_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = replay_route_evaluation(
                GOAL, [], knowledge_base_dir=directory,
            )
        self.assertEqual(report["decision_status"], "unresolved")
        self.assertIsNone(report["selected_route_id"])
        self.assertFalse(report["research_publication_executed"])
        self.assertFalse(report["device_hard_gate_executed"])
        self.assertIn("protocols_missing", report["reason_codes"]["input"])
        self.assertIn("signed_source_events_missing", report["reason_codes"]["input"])
        self.assertIn(
            "trusted_source_event_missing", report["reason_codes"]["discovery"]
        )

    def test_cli_writes_structured_unresolved_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            goal_path = root / "goal.json"
            protocols_path = root / "protocols.json"
            report_path = root / "report.json"
            goal_path.write_text(json.dumps(GOAL), encoding="utf-8")
            protocols_path.write_text("[]", encoding="utf-8")
            status = main([
                "--goal-json", str(goal_path),
                "--protocols-json", str(protocols_path),
                "--knowledge-base-dir", str(root),
                "--output-json", str(report_path),
            ])
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(report["schema_version"], "offline-route-replay/v1")
        self.assertEqual(report["decision_status"], "unresolved")
        self.assertIn(
            "candidate_discovery_incomplete", report["reason_codes"]["decision"]
        )

    def test_protocols_must_be_array_of_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "protocols must be a JSON array"):
                replay_route_evaluation(
                    GOAL, {"protocols": []}, knowledge_base_dir=directory,
                )

    @unittest.skipUnless(
        importlib.util.find_spec("fitz") and importlib.util.find_spec("cryptography"),
        "PyMuPDF and cryptography required",
    )
    def test_signed_pdf_without_independent_review_is_unresolved(self) -> None:
        from reaserch_agent.test_route_signature_pipeline import (
            SignedRouteSignaturePipelineTest,
        )

        fixture = SignedRouteSignaturePipelineTest(
            "test_valid_review_selects_route_and_binds_review_digest"
        )
        fixture.setUp()
        try:
            report = replay_route_evaluation(
                fixture.goal, fixture.protocols,
                knowledge_base_dir=fixture.root,
                route_trust={
                    "signed_route_source_events": [fixture.signed_event],
                    "trusted_route_public_keys": fixture.acquisition_keys,
                    "signed_route_signature_reviews": [],
                    "trusted_route_signature_public_keys": {},
                    "trusted_route_capabilities_by_group": fixture.capabilities,
                    "trusted_route_group_roles_by_group": fixture.roles,
                },
            )
            reviewed_report = replay_route_evaluation(
                fixture.goal, fixture.protocols,
                knowledge_base_dir=fixture.root,
                route_trust={
                    "signed_route_source_events": [fixture.signed_event],
                    "trusted_route_public_keys": fixture.acquisition_keys,
                    "signed_route_signature_reviews": [fixture.review],
                    "trusted_route_signature_public_keys": fixture.review_keys,
                    "trusted_route_capabilities_by_group": fixture.capabilities,
                    "trusted_route_group_roles_by_group": fixture.roles,
                },
            )
        finally:
            fixture.doCleanups()
        self.assertEqual(report["decision_status"], "unresolved")
        self.assertEqual(len(report["decision"]["candidates"]), 1)
        candidate = report["decision"]["candidates"][0]
        self.assertTrue(candidate["validation"]["source_scope_verified"])
        self.assertIsNone(candidate["validation"]["source_route_signature"])
        self.assertIn(
            "review:review_missing_or_unverified",
            report["reason_codes"]["validation"][candidate["route_id"]],
        )
        reviewed = reviewed_report["decision"]["candidates"][0]
        self.assertIsNotNone(reviewed["validation"]["source_route_signature"])
        self.assertTrue(any(
            reason.startswith("science:")
            for reason in reviewed_report["reason_codes"]["validation"][reviewed["route_id"]]
        ))


if __name__ == "__main__":
    unittest.main()
