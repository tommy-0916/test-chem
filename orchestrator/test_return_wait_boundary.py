"""External returns must stop a campaign, not become an approval bypass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.runner import DEVICE_REPAIR_REQUEST, STOP_MANUAL_REQUIRED, feedback_route, package_feasibility_accepted
from orchestrator.test_runner import CountingMockAdapter, FakeSteps, PLAN_STATE, make_runner


class ExternalReturnBoundaryTest(unittest.TestCase):
    def test_required_return_stops_execution_and_preserves_wait_instruction(self):
        package = {
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "feasibility_accepted": False,
            "manual_repair_context": {"feasibility_certificate": {"accepted": True, "version": "historical"}},
            "workflow_json": {},
            "dispatch_payload": {},
            "error_package": {
                "type": "device_external_return_wait_required",
                "pending_returns": [{
                    "source_macro_step": 2,
                    "name": "晶相分析",
                    "wait_for": "等待人工读取 XRD 图谱并确定晶相",
                }],
            },
        }
        self.assertEqual(feedback_route(package), "human")
        self.assertFalse(package_feasibility_accepted(package))
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps([PLAN_STATE], [package])
            result = make_runner(tmp, steps, adapter=adapter).run()
            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(len(steps.research_calls), 1)
            iteration = Path(result.campaign_dir) / "iteration_01"
            instructions = (iteration / "AWAITING_CONDITION_REVIEW.md").read_text(encoding="utf-8")
            self.assertIn("等待人工读取 XRD 图谱并确定晶相", instructions)
            self.assertIn("不能用审核同意", instructions)
            self.assertIn("post_observation", instructions)
            self.assertFalse((iteration / DEVICE_REPAIR_REQUEST).exists())


if __name__ == "__main__":
    unittest.main()
