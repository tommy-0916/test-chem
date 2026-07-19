"""Unit tests for the campaign orchestrator loop and execution adapters (P2)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from orchestrator.execution_adapters import (
    ExecutionBlockedError,
    ListenExecutionAdapter,
    MockExecutionAdapter,
    RealExecutionAdapter,
)
from orchestrator.runner import (
    APPROVAL_FILENAME,
    STOP_FEASIBILITY_DEADLOCK,
    STOP_GOAL_REACHED,
    STOP_MANUAL_REQUIRED,
    STOP_MAX_ITERATIONS,
    STOP_REVIEW_REQUIRED,
    CampaignConfig,
    CampaignRunner,
    package_requires_review,
)

MACRO_STEP = {
    "步骤序号": 1,
    "操作": "配制前驱体",
    "试剂/对象": "K4Fe(CN)6",
    "参数": "0.5 mmol in 10 mL",
}
PLAN_STATE = {"status": "completed", "macro_plan": [MACRO_STEP]}
CLOSURE_STATE = {"status": "completed", "macro_plan": []}
MANUAL_STATE = {"status": "manual_required", "macro_plan": [MACRO_STEP]}
SUCCESS_PACKAGE = {
    "status": "success",
    "workflow_json": {"steps": [{"step_number": 1}, {"step_number": 2}]},
    "workflow_txt": "1. 第1步 物料站：...",
}
REVIEW_FLAGGED_PACKAGE = {
    "status": "success",
    "requires_scientific_review": True,
    "workflow_json": {
        "steps": [{"step_number": 1}, {"step_number": 2}],
        "temporal_adaptations": [
            {
                "original_requirement": "边滴入边搅拌",
                "execution_fidelity": "approximated",
                "adaptation_schedule": "加液 -> 搅拌 -> 加液",
                "requires_scientific_review": True,
            }
        ],
    },
    "workflow_txt": "1. 第1步 液体进样站：...",
}
FEASIBILITY_PACKAGE = {
    "status": "feasibility_error",
    "feedback_type": "device_feasibility_error",
    "error_package": {
        "type": "physical_infeasible",
        "blocking_constraints": ["缺少反应釜"],
    },
    "macro_plan": {"huge": "echoed research handoff"},
    "feasibility_assessment": {"huge": "raw llm output"},
}
UNVERIFIABLE_PACKAGE = {
    "status": "feasibility_error",
    "feedback_type": "device_feasibility_error",
    "error_package": {
        "type": "needs_human_review",
        "blocking_constraints": ["XRD 辐射源无法从真源证明满足"],
        "constraint_classification": {
            "hard": [],
            "adaptable": [],
            "unverifiable": ["XRD 辐射源无法从真源证明满足"],
        },
    },
}


class BoundaryCrossingAdapter(MockExecutionAdapter):
    """Simulates a manual/listen/real adapter that reaches the lab boundary."""

    name = "boundary"
    real_lab_boundary = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def execute(self, package: Dict[str, Any], iteration_dir: Path) -> Dict[str, Any]:
        self.calls += 1
        return super().execute(package, iteration_dir)


class CountingMockAdapter(MockExecutionAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def execute(self, package: Dict[str, Any], iteration_dir: Path) -> Dict[str, Any]:
        self.calls += 1
        return super().execute(package, iteration_dir)


class FakeSteps:
    """Scripted research/device step functions (last item repeats)."""

    def __init__(
        self,
        research_results: List[Dict[str, Any]],
        device_results: List[Dict[str, Any]],
    ) -> None:
        self.research_results = [dict(item) for item in research_results]
        self.device_results = [dict(item) for item in device_results]
        self.research_calls: List[Dict[str, Any]] = []
        self.device_calls: List[str] = []

    def research_step(
        self,
        event_type: str,
        *,
        query: str,
        previous_state_path,
        payload,
        iteration_dir: Path,
        references,
    ) -> Dict[str, Any]:
        self.research_calls.append(
            {
                "event_type": event_type,
                "query": query,
                "payload": payload,
                "references": list(references),
            }
        )
        index = min(len(self.research_calls) - 1, len(self.research_results) - 1)
        result = dict(self.research_results[index])
        (iteration_dir / "research_state.json").write_text(
            json.dumps(result, ensure_ascii=False),
            encoding="utf-8",
        )
        return result

    def device_step(self, state_path: Path, iteration_dir: Path) -> Dict[str, Any]:
        self.device_calls.append(str(state_path))
        index = min(len(self.device_calls) - 1, len(self.device_results) - 1)
        result = dict(self.device_results[index])
        (iteration_dir / "device_package.json").write_text(
            json.dumps(result, ensure_ascii=False),
            encoding="utf-8",
        )
        return result


def make_runner(
    tmp: str,
    steps: FakeSteps,
    adapter=None,
    *,
    max_iterations: int = 5,
    deadlock_limit: int = 3,
):
    config = CampaignConfig(
        query="测试 campaign query",
        campaign_id="cmp_test_runner",
        max_iterations=max_iterations,
        feasibility_deadlock_limit=deadlock_limit,
        campaigns_root=Path(tmp),
    )
    return CampaignRunner(
        config,
        adapter or MockExecutionAdapter(),
        research_step=steps.research_step,
        device_step=steps.device_step,
    )


class CampaignRunnerTest(unittest.TestCase):
    def test_goal_reached_after_one_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE, CLOSURE_STATE], [SUCCESS_PACKAGE])
            runner = make_runner(tmp, steps)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_GOAL_REACHED)
            self.assertTrue(result.goal_reached)
            self.assertEqual(result.iterations_run, 1)
            self.assertEqual(len(steps.research_calls), 2)
            self.assertEqual(steps.research_calls[0]["event_type"], "bootstrap")
            self.assertEqual(steps.research_calls[1]["event_type"], "new_observation")
            observation = steps.research_calls[1]["payload"]["observation"]
            self.assertIn("[mock 执行]", observation["summary"])

            campaign_dir = Path(result.campaign_dir)
            self.assertTrue((campaign_dir / "final_report.md").exists())
            summary = json.loads(
                (campaign_dir / "campaign_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["stop_reason"], STOP_GOAL_REACHED)
            self.assertTrue(
                (campaign_dir / "iteration_01" / "observation_in.json").exists()
            )

    def test_max_iterations_stops_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE], [SUCCESS_PACKAGE])
            runner = make_runner(tmp, steps, max_iterations=2)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MAX_ITERATIONS)
            self.assertFalse(result.goal_reached)
            self.assertEqual(result.iterations_run, 2)
            self.assertEqual(len(steps.research_calls), 3)  # bootstrap + 2 turns
            self.assertEqual(len(steps.device_calls), 2)

    def test_feasibility_deadlock_stops_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps([PLAN_STATE], [FEASIBILITY_PACKAGE])
            runner = make_runner(tmp, steps, adapter=adapter, deadlock_limit=2)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_FEASIBILITY_DEADLOCK)
            self.assertEqual(result.iterations_run, 2)
            self.assertEqual(adapter.calls, 0)
            # first feasibility error fed back to research, second hit the limit
            self.assertEqual(len(steps.research_calls), 2)
            feasibility_payload = steps.research_calls[1]["payload"]
            self.assertEqual(
                feasibility_payload["feedback_type"], "device_feasibility_error"
            )
            self.assertNotIn("macro_plan", feasibility_payload)
            self.assertNotIn("feasibility_assessment", feasibility_payload)

    def test_translation_failed_package_flows_back_to_research(self) -> None:
        """Issue #4: an exhausted-repair failed package (feedback_type
        device_feasibility_error + error_package.type
        workflow_translation_failed) must ride the SAME channel as
        feasibility errors — research re-planning is invoked with the
        structured errors, and the deadlock counter applies."""
        translation_failed = {
            "status": "failed",
            "feedback_type": "device_feasibility_error",
            "failure_stage": "dispatch_validation",
            "error_package": {
                "type": "workflow_translation_failed",
                "blocking_constraints": ["第 1 步：参数 `X` 不在可下发参数中"],
                "structured_errors": [
                    {"error_code": "unknown_parameter", "step_number": 1,
                     "parameter_path": "X"}
                ],
                "failed_plan_signature": "plan_abc123",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps([PLAN_STATE], [translation_failed])
            runner = make_runner(tmp, steps, adapter=adapter, deadlock_limit=2)

            result = runner.run()

            # rode the feasibility channel: no execution, deadlock at limit
            self.assertEqual(result.stop_reason, STOP_FEASIBILITY_DEADLOCK)
            self.assertEqual(adapter.calls, 0)
            # research received the structured feedback for re-planning
            self.assertEqual(len(steps.research_calls), 2)
            payload = steps.research_calls[1]["payload"]
            self.assertEqual(payload["feedback_type"], "device_feasibility_error")
            self.assertEqual(
                payload["error_package"]["type"], "workflow_translation_failed"
            )
            self.assertTrue(payload["error_package"]["structured_errors"])
            # cumulative constraints captured the translation blockers too
            summary = json.loads(
                (Path(result.campaign_dir) / "campaign_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(
                any("参数" in item for item in summary.get(
                    "cumulative_device_constraints", []))
            )

    def test_deadlock_reports_cumulative_constraints(self) -> None:
        """Issue 4: on deadlock the campaign summary lists every device
        constraint seen, not just the last error."""
        with tempfile.TemporaryDirectory() as tmp:
            error_a = dict(FEASIBILITY_PACKAGE)
            error_a["error_package"] = {
                "type": "physical_infeasible",
                "blocking_constraints": ["容器不兼容：进样瓶无法进入马弗炉"],
            }
            error_b = dict(FEASIBILITY_PACKAGE)
            error_b["error_package"] = {
                "type": "physical_infeasible",
                "blocking_constraints": ["单容器体积超过离心上限"],
            }
            steps = FakeSteps([PLAN_STATE], [error_a, error_b])
            runner = make_runner(tmp, steps, deadlock_limit=2)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_FEASIBILITY_DEADLOCK)
            summary = json.loads(
                (Path(result.campaign_dir) / "campaign_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            cumulative = summary["cumulative_device_constraints"]
            self.assertIn("容器不兼容：进样瓶无法进入马弗炉", cumulative)
            self.assertIn("单容器体积超过离心上限", cumulative)

    def test_manual_required_stops_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE, MANUAL_STATE], [SUCCESS_PACKAGE])
            runner = make_runner(tmp, steps)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(result.iterations_run, 1)

    def test_bootstrap_manual_stops_before_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([MANUAL_STATE], [SUCCESS_PACKAGE])
            runner = make_runner(tmp, steps)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(result.iterations_run, 0)
            self.assertEqual(len(steps.device_calls), 0)


class ReviewGateTest(unittest.TestCase):
    """Review finding 3: review-flagged success must not auto-dispatch."""

    def test_package_requires_review_detection(self) -> None:
        self.assertFalse(package_requires_review(SUCCESS_PACKAGE))
        self.assertTrue(package_requires_review(REVIEW_FLAGGED_PACKAGE))
        nested_only = {
            "status": "success",
            "workflow_json": {
                "steps": [],
                "temporal_adaptations": [{"requires_scientific_review": True}],
            },
        }
        self.assertTrue(package_requires_review(nested_only))

    def test_review_flagged_package_blocks_boundary_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BoundaryCrossingAdapter()
            steps = FakeSteps([PLAN_STATE], [REVIEW_FLAGGED_PACKAGE])
            runner = make_runner(tmp, steps, adapter=adapter)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_REVIEW_REQUIRED)
            self.assertEqual(adapter.calls, 0)  # never reached the adapter
            iteration_dir = Path(result.campaign_dir) / "iteration_01"
            self.assertTrue(
                (iteration_dir / "AWAITING_SCIENTIFIC_REVIEW.md").exists()
            )

    def test_approval_file_releases_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BoundaryCrossingAdapter()
            steps = FakeSteps(
                [PLAN_STATE, CLOSURE_STATE], [REVIEW_FLAGGED_PACKAGE]
            )
            runner = make_runner(tmp, steps, adapter=adapter)
            approval_dir = runner.campaign_dir / "iteration_01"
            approval_dir.mkdir(parents=True, exist_ok=True)
            (approval_dir / APPROVAL_FILENAME).write_text(
                json.dumps({"approved": True, "approver": "郭老师"}),
                encoding="utf-8",
            )

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_GOAL_REACHED)
            self.assertEqual(adapter.calls, 1)

    def test_unapproved_approval_file_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BoundaryCrossingAdapter()
            steps = FakeSteps([PLAN_STATE], [REVIEW_FLAGGED_PACKAGE])
            runner = make_runner(tmp, steps, adapter=adapter)
            approval_dir = runner.campaign_dir / "iteration_01"
            approval_dir.mkdir(parents=True, exist_ok=True)
            (approval_dir / APPROVAL_FILENAME).write_text(
                json.dumps({"approved": False, "approver": "郭老师"}),
                encoding="utf-8",
            )

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_REVIEW_REQUIRED)
            self.assertEqual(adapter.calls, 0)

    def test_mock_adapter_proceeds_but_records_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps(
                [PLAN_STATE, CLOSURE_STATE], [REVIEW_FLAGGED_PACKAGE]
            )
            runner = make_runner(tmp, steps, adapter=adapter)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_GOAL_REACHED)
            self.assertEqual(adapter.calls, 1)
            summary = json.loads(
                (Path(result.campaign_dir) / "campaign_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            review_entries = [
                entry
                for entry in summary["trace"]
                if entry.get("phase") == "review_gate"
            ]
            self.assertEqual(len(review_entries), 1)
            self.assertEqual(
                review_entries[0]["status"], "pending_review_simulated_execution"
            )

    def test_unverifiable_error_goes_to_manual_not_replanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps([PLAN_STATE], [UNVERIFIABLE_PACKAGE])
            runner = make_runner(tmp, steps, adapter=adapter)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(adapter.calls, 0)
            # never fed back into research as a feasibility replan
            self.assertEqual(len(steps.research_calls), 1)
            iteration_dir = Path(result.campaign_dir) / "iteration_01"
            self.assertTrue(
                (iteration_dir / "AWAITING_CONDITION_REVIEW.md").exists()
            )


class ExecutionAdapterTest(unittest.TestCase):
    def test_real_adapter_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = RealExecutionAdapter()
            with self.assertRaises(ExecutionBlockedError):
                adapter.execute({}, Path(tmp))

    def test_mock_adapter_default_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockExecutionAdapter()
            observation = adapter.execute(SUCCESS_PACKAGE, Path(tmp))
            self.assertIn("workflow 共 2 步", observation["summary"])
            self.assertEqual(observation["status"], "success")

    def test_mock_adapter_observation_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sequence_path = Path(tmp) / "observations.json"
            sequence_path.write_text(
                json.dumps(
                    [
                        {"summary": "第一轮结果"},
                        {"summary": "第二轮结果"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            adapter = MockExecutionAdapter(observation_file=sequence_path)
            first = adapter.execute({}, Path(tmp))
            second = adapter.execute({}, Path(tmp))
            third = adapter.execute({}, Path(tmp))
            self.assertEqual(first["summary"], "第一轮结果")
            self.assertEqual(second["summary"], "第二轮结果")
            self.assertEqual(third["summary"], "第二轮结果")

    def test_listen_adapter_receives_posted_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ListenExecutionAdapter(
                host="127.0.0.1",
                port=0,
                timeout_seconds=10.0,
            )
            holder: Dict[str, Any] = {}

            def run_adapter() -> None:
                holder["observation"] = adapter.execute({}, Path(tmp))

            thread = threading.Thread(target=run_adapter)
            thread.start()
            for _ in range(200):
                if adapter.bound_port:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(adapter.bound_port)

            request = urllib.request.Request(
                f"http://127.0.0.1:{adapter.bound_port}/observation",
                data=json.dumps({"summary": "端口回传结果"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)

            thread.join(timeout=10)
            self.assertEqual(holder["observation"]["summary"], "端口回传结果")
            self.assertTrue((Path(tmp) / "observation_in.json").exists())


if __name__ == "__main__":
    unittest.main()
