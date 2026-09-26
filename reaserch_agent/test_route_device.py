"""Offline candidate-level checks against the existing capability projection."""

from __future__ import annotations

import unittest

from reaserch_agent.route_device import preflight_route_capabilities


def context(
    *, status: str | None = "available", support: str = "supported",
    mapping: str = "mapped",
) -> dict:
    station = {
        "station_code": "S1",
        "experiment_capabilities": [{
            "id": "feedback_control",
            "support_status": support,
            "operation_mapping_status": mapping,
        }],
    }
    if status is not None:
        station["availability"] = status
    return {"source": "synthetic contract", "workstations": [station]}


class RouteDevicePreflightTest(unittest.TestCase):
    def test_exact_reviewed_capability_with_live_status_is_preflight_supported(self) -> None:
        result = preflight_route_capabilities(["feedback_control"], context())
        self.assertEqual(result["status"], "preflight_supported")
        self.assertTrue(result["snapshot_id"].startswith("route_device_snapshot_"))

    def test_unlisted_capability_is_unknown(self) -> None:
        result = preflight_route_capabilities(["lookalike_control"], context())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["unresolved_capabilities"], ["lookalike_control"])

    def test_truth_gap_and_explicit_unsupported_are_blocked(self) -> None:
        truth_gap = preflight_route_capabilities(
            ["feedback_control"], context(support="unsupported", mapping="truth_gap"),
        )
        unsupported = preflight_route_capabilities(
            ["feedback_control"], context(support="unsupported", mapping="unknown"),
        )
        self.assertEqual(truth_gap["status"], "blocked")
        self.assertEqual(unsupported["status"], "blocked")

    def test_unknown_live_status_is_not_executable_proof(self) -> None:
        result = preflight_route_capabilities(
            ["feedback_control"], context(status=None),
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            preflight_route_capabilities(
                ["feedback_control"], context(status="unknown"),
            )["status"],
            "unknown",
        )

    def test_offline_station_is_blocked(self) -> None:
        result = preflight_route_capabilities(
            ["feedback_control"], context(status="offline"),
        )
        self.assertEqual(result["status"], "blocked")

    def test_restrictions_and_empty_requirements_fail_closed(self) -> None:
        restricted = {**context(), "excluded_capabilities": ["feedback_control"]}
        self.assertEqual(
            preflight_route_capabilities(["feedback_control"], restricted)["status"],
            "blocked",
        )
        self.assertEqual(preflight_route_capabilities([], context())["status"], "unknown")
        malformed = {**context(), "excluded_capabilities": "feedback_control"}
        self.assertEqual(
            preflight_route_capabilities(["feedback_control"], malformed)["status"],
            "unknown",
        )
        self.assertNotEqual(
            preflight_route_capabilities(["feedback_control"], context())["snapshot_id"],
            preflight_route_capabilities(["feedback_control"], restricted)["snapshot_id"],
        )


if __name__ == "__main__":
    unittest.main()
