"""Unit tests for the campaign orchestrator loop and execution adapters (P2)."""

from __future__ import annotations

import json
import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
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
    DEVICE_PLAN_OVERRIDE_TEMPLATE,
    DEVICE_REPAIR_MARKDOWN,
    DEVICE_REPAIR_REQUEST,
    STOP_DEVICE_ERROR,
    STOP_FEASIBILITY_DEADLOCK,
    STOP_GOAL_REACHED,
    STOP_MANUAL_REQUIRED,
    STOP_MAX_ITERATIONS,
    STOP_READY_FOR_DISPATCH,
    STOP_REVIEW_REQUIRED,
    CampaignConfig,
    CampaignRunner,
    DeviceRepairResumeError,
    package_requires_review,
)
from device_agent.feasibility_certificate import (
    FEASIBILITY_CERTIFICATE_VERSION,
    device_plan_contract_digest,
    device_plan_contract_view,
    feasibility_certificate_id,
    feasibility_certificate_protected_payload,
    stable_digest,
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
V2_PACKAGE_CONTRACT = {
    "contract_version": "v2",
    "contract_resolution": {
        "requested": "v2",
        "source_input": "v2",
        "effective": "v2",
        "requested_matches_effective": True,
        "source_matches_effective": True,
    },
}
SUCCESS_PACKAGE = {
    **V2_PACKAGE_CONTRACT,
    "status": "success",
    "workflow_json": {"steps": [{"step_number": 1}, {"step_number": 2}]},
    "workflow_txt": "1. 第1步 物料站：...",
}
REVIEW_FLAGGED_PACKAGE = {
    **V2_PACKAGE_CONTRACT,
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
    "feedback_type": "research_replan_required",
    "feedback_route": "research",
    "failure_scope": "route_feasibility",
    "error_package": {
        "type": "physical_infeasible",
        "blocking_constraints": ["缺少反应釜"],
    },
    "macro_plan": {"huge": "echoed research handoff"},
    "feasibility_assessment": {"huge": "raw llm output"},
}
UNVERIFIABLE_PACKAGE = {
    "status": "manual_required",
    "feedback_type": "human_review_required",
    "feedback_route": "human",
    "failure_scope": "device_quantity",
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


def _valid_v2_feasibility_certificate(
    package: Dict[str, Any],
) -> Dict[str, Any]:
    seed = package.get("feasibility_certificate")
    seed = seed if isinstance(seed, dict) else {}
    matrix = copy.deepcopy(
        package.get("sample_control_matrix")
        or seed.get("sample_control_matrix")
        or seed.get("sample_matrix")
        or []
    )
    accepted_plan = copy.deepcopy(package)
    accepted_plan["sample_control_matrix"] = matrix
    certificate: Dict[str, Any] = {
        "certificate_version": FEASIBILITY_CERTIFICATE_VERSION,
        "contract_version": "v2",
        "research_plan_signature": str(
            seed.get("research_plan_signature") or "route-frozen"
        ),
        "target_materials": [],
        "reaction_route": [],
        "reagent_identity_and_order": [],
        "observation_points": [],
        "sample_control_matrix": matrix,
        "accepted_device_sample_control_matrix": matrix,
        "device_sample_ids": [],
        "semantic_analysis": [],
        "accepted_device_plan_signature": stable_digest(
            accepted_plan.get("device_plan", []), prefix="device_plan"
        ),
        "accepted_device_plan_contract_sha256": device_plan_contract_digest(
            accepted_plan
        ),
        "acceptance_scope": "accepted_device_plan",
    }
    protected = feasibility_certificate_protected_payload(certificate)
    certificate.update(
        {
            "accepted": True,
            "device_snapshot_id": "snapshot-frozen",
            "device_truth_sha256": "snapshot-frozen",
            "protected_digest": stable_digest(protected),
            "route_signature": certificate["research_plan_signature"],
            "sample_matrix_signature": stable_digest(
                matrix, prefix="sample_matrix"
            ),
            "device_snapshot_signature": "snapshot-frozen",
            "device_snapshot": copy.deepcopy(
                seed.get("device_snapshot") or {"WS-01": "available"}
            ),
        }
    )
    certificate["certificate_id"] = feasibility_certificate_id(
        protected_digest=certificate["protected_digest"],
        device_snapshot_id=certificate["device_snapshot_id"],
        device_truth_sha256=certificate["device_truth_sha256"],
    )
    return certificate


# The shared success fixtures represent the default V2 runtime.  Keep their
# certificate bound to the exact contract fields above so orchestration tests
# exercise the same dispatch boundary as production instead of relying on a
# checker mock to bypass certificate validation.
for _success_fixture in (SUCCESS_PACKAGE, REVIEW_FLAGGED_PACKAGE):
    _success_fixture["feasibility_accepted"] = True
    _success_fixture["feasibility_certificate"] = (
        _valid_v2_feasibility_certificate(_success_fixture)
    )


def _valid_v2_successor_package(
    request: Dict[str, Any],
    override: Dict[str, Any],
    package: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build the success fixture the Device resume boundary now requires."""

    previous_plan = copy.deepcopy(request.get("last_device_plan") or {})
    revised_plan = copy.deepcopy(previous_plan)
    supplied_plan = override.get("device_plan")
    if isinstance(supplied_plan, dict):
        revised_plan.update(copy.deepcopy(supplied_plan))
    elif isinstance(supplied_plan, list):
        revised_plan["device_plan"] = copy.deepcopy(supplied_plan)
    for field_name in device_plan_contract_view({}):
        if field_name in override:
            revised_plan[field_name] = copy.deepcopy(override[field_name])

    result = copy.deepcopy(revised_plan)
    result.update(copy.deepcopy(package or SUCCESS_PACKAGE))
    predecessor = copy.deepcopy(request["feasibility_certificate"])
    before = device_plan_contract_view(previous_plan)
    after = device_plan_contract_view(result)
    predecessor.update(
        {
            "accepted": True,
            "certificate_version": FEASIBILITY_CERTIFICATE_VERSION,
            "contract_version": "v2",
            "accepted_device_plan_signature": stable_digest(
                result.get("device_plan", []), prefix="device_plan"
            ),
            "accepted_device_plan_contract_sha256": (
                device_plan_contract_digest(result)
            ),
            "acceptance_scope": "accepted_device_plan_revision",
            "supersedes_certificate_id": request["feasibility_certificate"][
                "certificate_id"
            ],
            "repair_request_id": request["request_id"],
            "repair_authority": "human_device_plan_override",
            "authorized_change_scope": {
                "changed_contract_fields": [
                    key for key in before if before[key] != after[key]
                ],
                "declared_changes": copy.deepcopy(override.get("changes") or []),
                "declarations": copy.deepcopy(override.get("declarations") or {}),
            },
        }
    )
    protected = feasibility_certificate_protected_payload(predecessor)
    predecessor["protected_digest"] = stable_digest(protected)
    predecessor["certificate_id"] = feasibility_certificate_id(
        protected_digest=predecessor["protected_digest"],
        device_snapshot_id=str(predecessor.get("device_snapshot_id") or ""),
        device_truth_sha256=str(predecessor.get("device_truth_sha256") or ""),
    )
    result["feasibility_accepted"] = True
    result["feasibility_certificate"] = predecessor
    return result


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
        self.device_calls: List[Dict[str, Any]] = []

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

    def device_step(
        self,
        state_path: Path,
        iteration_dir: Path,
        *,
        device_plan_override_path: Path | None = None,
        prior_repair_request_path: Path | None = None,
    ) -> Dict[str, Any]:
        self.device_calls.append(
            {
                "state_path": str(state_path),
                "override_path": str(device_plan_override_path or ""),
                "request_path": str(prior_repair_request_path or ""),
            }
        )
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
    transient_retry_limit: int = 0,
    requested_contract_version: str = "v2",
):
    config = CampaignConfig(
        query="测试 campaign query",
        campaign_id="cmp_test_runner",
        max_iterations=max_iterations,
        feasibility_deadlock_limit=deadlock_limit,
        transient_device_retry_limit=transient_retry_limit,
        requested_contract_version=requested_contract_version,
        campaigns_root=Path(tmp),
    )
    return CampaignRunner(
        config,
        adapter or MockExecutionAdapter(),
        research_step=steps.research_step,
        device_step=steps.device_step,
    )


class IsolatedCampaignTest(unittest.TestCase):
    """Loop/review fixtures omit device contracts; gate behavior has its own tests."""

    def setUp(self) -> None:
        checker = patch("orchestrator.runner.check_dispatch", return_value={
            "status": "passed", "dispatchable": True, "findings": [],
            "input_sha256": "orchestration-test-fixture",
        })
        reports = patch("orchestrator.runner.write_check_report", return_value={})
        checker.start()
        reports.start()
        self.addCleanup(checker.stop)
        self.addCleanup(reports.stop)


class CampaignRunnerTest(IsolatedCampaignTest):
    def test_device_args_cannot_override_selected_contract_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "device_args --contract-version"):
            CampaignConfig(
                query="contract boundary",
                requested_contract_version="v2",
                device_args=["--contract-version", "v1"],
            )

    def test_campaign_iteration_budget_is_capped_at_12(self) -> None:
        self.assertEqual(CampaignConfig(query="q").max_iterations, 12)
        self.assertEqual(CampaignConfig(query="q").transient_device_retry_limit, 0)
        for invalid in (0, 13):
            with self.assertRaises(ValueError):
                CampaignConfig(query="q", max_iterations=invalid)

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
            self.assertEqual(
                summary["contract_resolution"]["requested"], "v2"
            )
            self.assertTrue(
                (campaign_dir / "iteration_01" / "observation_in.json").exists()
            )

    def test_normal_v2_success_rejects_missing_or_tampered_certificate(self) -> None:
        cases = []
        missing = copy.deepcopy(SUCCESS_PACKAGE)
        missing.pop("feasibility_certificate")
        cases.append(("missing", missing))
        tampered = copy.deepcopy(SUCCESS_PACKAGE)
        tampered["feasibility_certificate"]["protected_digest"] = "sha256_tampered"
        cases.append(("tampered", tampered))
        contradictory = copy.deepcopy(SUCCESS_PACKAGE)
        contradictory["feasibility_accepted"] = False
        cases.append(("top_level_not_accepted", contradictory))
        revision = copy.deepcopy(SUCCESS_PACKAGE)
        revision_certificate = revision["feasibility_certificate"]
        revision_certificate["acceptance_scope"] = (
            "accepted_device_plan_revision"
        )
        revision_protected = feasibility_certificate_protected_payload(
            revision_certificate
        )
        revision_certificate["protected_digest"] = stable_digest(
            revision_protected
        )
        revision_certificate["certificate_id"] = feasibility_certificate_id(
            protected_digest=revision_certificate["protected_digest"],
            device_snapshot_id=str(
                revision_certificate.get("device_snapshot_id") or ""
            ),
            device_truth_sha256=str(
                revision_certificate.get("device_truth_sha256") or ""
            ),
        )
        cases.append(("revision_scope_without_lineage", revision))

        for label, package in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                steps = FakeSteps([PLAN_STATE, CLOSURE_STATE], [package])
                adapter = CountingMockAdapter()
                captured_reports = []
                with patch("orchestrator.runner.check_dispatch") as checker, patch(
                    "orchestrator.runner.write_check_report",
                    side_effect=lambda report, _directory: (
                        captured_reports.append(copy.deepcopy(report)) or {}
                    ),
                ):
                    result = make_runner(tmp, steps, adapter=adapter).run()

                self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
                self.assertEqual(adapter.calls, 0)
                self.assertEqual(
                    [call["event_type"] for call in steps.research_calls],
                    ["bootstrap"],
                )
                checker.assert_not_called()
                self.assertEqual(len(captured_reports), 1)
                report = captured_reports[0]
                self.assertEqual(report["status"], "failed")
                self.assertFalse(report["dispatchable"])
                self.assertEqual(
                    report["findings"][0]["code"],
                    "invalid_v2_feasibility_certificate",
                )

    def test_normal_v1_success_remains_certificate_compatible(self) -> None:
        package = copy.deepcopy(SUCCESS_PACKAGE)
        package["contract_version"] = "v1"
        package["contract_resolution"] = {
            "requested": "v1",
            "source_input": "v1",
            "effective": "v1",
            "requested_matches_effective": True,
            "source_matches_effective": True,
        }
        package.pop("feasibility_certificate", None)
        package.pop("feasibility_accepted", None)
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE, CLOSURE_STATE], [package])
            adapter = CountingMockAdapter()
            result = make_runner(
                tmp,
                steps,
                adapter=adapter,
                requested_contract_version="v1",
            ).run()

        self.assertEqual(result.stop_reason, STOP_GOAL_REACHED)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(len(steps.research_calls), 2)

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

    def test_effective_device_parameters_are_attached_to_observation(self) -> None:
        package = dict(SUCCESS_PACKAGE)
        package["feasibility_certificate"] = {
            "research_plan_signature": "research-plan-v1"
        }
        package["quantity_adjustments"] = [
            {
                "kind": "split_transfer",
                "before": {"value": 10, "unit": "mL"},
                "after": {"parts": [5, 5], "unit": "mL"},
                "requires_scientific_review": False,
            }
        ]
        package["batch_plan"] = [{"batch_id": "batch-1"}]
        package["material_ledger"] = {"checks": {"no_double_count": True}}
        package["feasibility_certificate"] = _valid_v2_feasibility_certificate(
            package
        )
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE, CLOSURE_STATE], [package])
            result = make_runner(tmp, steps).run()

            self.assertEqual(result.stop_reason, STOP_GOAL_REACHED)
            observation = steps.research_calls[1]["payload"]["observation"]
            actual = observation["actual_execution_parameters"]
            self.assertEqual(actual["research_plan_signature"], "research-plan-v1")
            self.assertEqual(actual["quantity_adjustments"][0]["kind"], "split_transfer")
            saved = json.loads(
                (
                    Path(result.campaign_dir)
                    / "iteration_01"
                    / "observation_in.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("actual_execution_parameters", saved)

    def test_transient_gateway_deadline_retries_same_research_state(self) -> None:
        transient = {
            "status": "failed",
            "feedback_type": "device_internal_error",
            "failure_stage": "device_internal_error",
            "error_package": {
                "type": "device_internal_error",
                "blocking_constraints": [
                    "stream disconnected before completion: gateway_deadline"
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps(
                [PLAN_STATE, CLOSURE_STATE],
                [transient, SUCCESS_PACKAGE],
            )
            runner = make_runner(
                tmp,
                steps,
                max_iterations=3,
                transient_retry_limit=1,
            )

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_GOAL_REACHED)
            self.assertEqual(result.iterations_run, 1)
            self.assertEqual(len(steps.device_calls), 2)
            # No Research B2 turn occurs for a transport failure; only the
            # bootstrap and the successful observation turn are present.
            self.assertEqual(len(steps.research_calls), 2)

    def test_transient_device_error_is_not_replayed_by_default(self) -> None:
        transient = {
            "status": "failed",
            "feedback_type": "device_internal_error",
            "failure_stage": "device_internal_error",
            "error_package": {
                "type": "device_internal_error",
                "blocking_constraints": ["gateway_deadline"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE], [transient, SUCCESS_PACKAGE])

            result = make_runner(tmp, steps).run()

            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(len(steps.device_calls), 1)
            self.assertEqual(len(steps.research_calls), 1)

    def test_nontransient_device_internal_error_remains_terminal(self) -> None:
        internal = {
            "status": "failed",
            "feedback_type": "device_internal_error",
            "error_package": {
                "type": "device_internal_error",
                "blocking_constraints": ["ValueError: malformed workflow JSON"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE], [internal])
            runner = make_runner(tmp, steps, max_iterations=3)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(result.iterations_run, 1)
            self.assertEqual(len(steps.device_calls), 1)

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
                feasibility_payload["feedback_type"], "research_replan_required"
            )
            self.assertNotIn("macro_plan", feasibility_payload)
            self.assertNotIn("feasibility_assessment", feasibility_payload)

    def test_translation_repair_exhaustion_stays_at_device_and_emits_handoff(self) -> None:
        """A post-feasibility workflow failure never enters Research B2."""
        translation_failed = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "failure_stage": "dispatch_validation",
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "plan_abc123",
                "sample_matrix": [],
                "device_snapshot": {"stations": ["WS-01"]},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
            "quantity_adjustments": [],
            "batch_plan": [
                {
                    "batch_id": "B-1",
                    "sample_id": "S-1",
                    "is_root_batch": True,
                }
            ],
            "material_ledger": {"entries": [{"entry_id": "M-1"}]},
            "workflow_json": {"steps": []},
            "repair_history": [{"cycle": 1}, {"cycle": 2}],
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

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(len(steps.research_calls), 1)
            iteration_dir = Path(result.campaign_dir) / "iteration_01"
            self.assertTrue((iteration_dir / DEVICE_REPAIR_MARKDOWN).exists())
            self.assertTrue((iteration_dir / DEVICE_REPAIR_REQUEST).exists())
            self.assertTrue((iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE).exists())
            request = json.loads(
                (iteration_dir / DEVICE_REPAIR_REQUEST).read_text(encoding="utf-8")
            )
            self.assertEqual(request["frozen_route_signature"], "plan_abc123")
            self.assertEqual(len(request["repair_history"]), 2)
            self.assertIsInstance(request["last_device_plan"], dict)
            self.assertEqual(
                request["last_device_plan"]["batch_plan"][0]["batch_id"],
                "B-1",
            )
            template = json.loads(
                (iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                template["device_plan"]["material_ledger"]["entries"][0][
                    "entry_id"
                ],
                "M-1",
            )

    def test_quantity_structured_errors_survive_in_device_repair_request(self) -> None:
        """Quantity codes/scopes remain machine-readable in the handoff."""
        structured_errors = [
            {
                "error_code": "aggregate_material_quantity_insufficient",
                "code": "aggregate_material_quantity_insufficient",
                "scope": "device_local_quantity",
                "message": "Ni(OH)2 总需求 57.8 mg，理论最多 27.8 mg。",
            },
            {
                "error_code": "unknown_yield",
                "code": "unknown_yield",
                "scope": "human_review_required",
                "message": "NiOxHy-FeEC 实际回收收率未知。",
            },
        ]
        quantity_failed = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_quantity",
            "feasibility_accepted": True,
            "failure_stage": "device_quantity_repair_exhausted",
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "plan_quantity_1",
                "sample_matrix": [],
                "device_snapshot": {"stations": ["WS-01"]},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
            "quantity_audit": {
                "status": "human_review_required",
                "issues": [dict(item) for item in structured_errors],
            },
            "error_package": {
                "type": "device_quantity_human_review_required",
                "blocking_constraints": [
                    item["message"] for item in structured_errors
                ],
                "structured_errors": [dict(item) for item in structured_errors],
                "failed_plan_signature": "plan_quantity_1",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps([PLAN_STATE], [quantity_failed])
            runner = make_runner(tmp, steps, adapter=adapter, deadlock_limit=2)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(len(steps.research_calls), 1)
            request = json.loads(
                (
                    Path(result.campaign_dir)
                    / "iteration_01"
                    / DEVICE_REPAIR_REQUEST
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(request["structured_errors"], structured_errors)
            self.assertEqual(
                [item["error_code"] for item in request["structured_errors"]],
                [
                    "aggregate_material_quantity_insufficient",
                    "unknown_yield",
                ],
            )
            self.assertEqual(
                [item["scope"] for item in request["structured_errors"]],
                ["device_local_quantity", "human_review_required"],
            )

    def test_legacy_workflow_feasibility_label_cannot_enter_research(self) -> None:
        mislabeled = {
            "status": "failed",
            "feedback_type": "device_feasibility_error",
            "failure_scope": "device_workflow",
            "feedback_route": "research",
            "feasibility_certificate": {"accepted": True},
            "error_package": {"type": "workflow_translation_failed"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE], [mislabeled])
            result = make_runner(tmp, steps).run()

            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(len(steps.research_calls), 1)

    def test_success_label_cannot_bypass_failed_device_gate(self) -> None:
        mislabeled = {
            **SUCCESS_PACKAGE,
            "dispatch_validation": {
                "status": "failed",
                "errors": ["formatter runtime failed"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CountingMockAdapter()
            steps = FakeSteps([PLAN_STATE], [mislabeled])
            result = make_runner(tmp, steps, adapter=adapter).run()

            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(len(steps.research_calls), 1)

    def test_nested_device_invariants_cannot_be_disguised_as_research_route(self) -> None:
        nested_denials = (
            {"details": [{"feasibility_accepted": True}]},
            {"details": [{"failure_scope": "device_workflow"}]},
            {"details": [{"feedback_route": "human"}]},
            {"details": [{"feedback_type": "device_workflow_error"}]},
            {"type": "workflow_translation_failed"},
            {
                "details": [
                    {"feasibility_certificate": {"accepted": True}}
                ]
            },
        )
        for index, nested in enumerate(nested_denials):
            disguised = {
                "status": "feasibility_error",
                "feedback_type": "research_replan_required",
                "feedback_route": "research",
                "failure_scope": "route_feasibility",
                "error_package": nested,
            }
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                steps = FakeSteps([PLAN_STATE], [disguised])
                result = make_runner(tmp, steps).run()

                self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
                self.assertEqual(len(steps.research_calls), 1)

    def test_distinct_feasibility_blockers_do_not_false_deadlock(self) -> None:
        """Different blockers mean the Research→Device loop made progress."""
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
            runner = make_runner(
                tmp, steps, deadlock_limit=2, max_iterations=2
            )

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MAX_ITERATIONS)
            summary = json.loads(
                (Path(result.campaign_dir) / "campaign_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            cumulative = summary["cumulative_device_constraints"]
            self.assertIn("容器不兼容：进样瓶无法进入马弗炉", cumulative)
            self.assertIn("单容器体积超过离心上限", cumulative)

    def test_workflow_skill_error_never_participates_in_feasibility_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow_error = {
                "status": "failed",
                "feedback_type": "device_workflow_error",
                "feedback_route": "device",
                "failure_scope": "device_workflow",
                "feasibility_accepted": True,
                "error_package": {
                "type": "workflow_skill_review_failed",
                "structured_errors": [{"error_code": "unparsed"}],
                "blocking_constraints": [
                    "步骤 [2]: 反应管不兼容（UNSUPPORTED_REACTION_TUBE）。"
                ],
                },
            }
            steps = FakeSteps([PLAN_STATE], [workflow_error])
            runner = make_runner(tmp, steps, deadlock_limit=2, max_iterations=2)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(result.iterations_run, 1)
            self.assertEqual(len(steps.research_calls), 1)

    def test_same_blocker_recurrence_triggers_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            error_a = dict(FEASIBILITY_PACKAGE)
            error_a["error_package"] = {
                "type": "physical_infeasible",
                "blocking_constraints": ["容器不兼容：进样瓶无法进入马弗炉"],
            }
            error_b = dict(FEASIBILITY_PACKAGE)
            error_b["error_package"] = {
                "type": "route_operation_missing",
                "blocking_constraints": [
                    "步骤 [4]: XRD 体积冲突（XRD_INPUT_VOLUME_CONFLICT）。"
                ],
            }
            error_b_again = dict(FEASIBILITY_PACKAGE)
            error_b_again["error_package"] = {
                "type": "route_operation_missing",
                "blocking_constraints": [
                    "步骤 [8]: 仍存在体积冲突（XRD_INPUT_VOLUME_CONFLICT）。"
                ],
            }
            steps = FakeSteps([PLAN_STATE], [error_a, error_b, error_b_again])
            runner = make_runner(tmp, steps, deadlock_limit=2)

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_FEASIBILITY_DEADLOCK)
            self.assertEqual(result.iterations_run, 3)

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

    def test_device_override_resume_skips_bootstrap_and_preserves_iteration(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
            "error_package": {"type": "device_workflow_repair_exhausted"},
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial_steps = FakeSteps([PLAN_STATE], [manual_package])
            initial = make_runner(tmp, initial_steps).run()
            request_path = (
                Path(initial.campaign_dir) / "iteration_01" / DEVICE_REPAIR_REQUEST
            )
            override_path = (
                Path(initial.campaign_dir)
                / "iteration_01"
                / DEVICE_PLAN_OVERRIDE_TEMPLATE
            )

            request = json.loads(request_path.read_text(encoding="utf-8"))
            override = json.loads(override_path.read_text(encoding="utf-8"))
            resumed_package = _valid_v2_successor_package(
                request,
                override,
                {
                    **SUCCESS_PACKAGE,
                    "status": "ready_for_dispatch",
                    "feedback_route": "success",
                },
            )
            resumed_steps = FakeSteps([CLOSURE_STATE], [resumed_package])
            config = CampaignConfig(
                query="测试 campaign query",
                campaign_id="cmp_test_runner",
                campaigns_root=Path(tmp),
                resume_device_repair=request_path,
                device_plan_override=override_path,
            )
            resumed = CampaignRunner(
                config,
                MockExecutionAdapter(),
                research_step=resumed_steps.research_step,
                device_step=resumed_steps.device_step,
            ).run()

            self.assertEqual(resumed.stop_reason, STOP_GOAL_REACHED)
            self.assertEqual(resumed.iterations_run, 1)
            self.assertEqual(len(resumed_steps.research_calls), 1)
            self.assertEqual(
                resumed_steps.research_calls[0]["event_type"], "new_observation"
            )
            self.assertTrue(resumed_steps.device_calls[0]["override_path"])
            self.assertTrue(resumed_steps.device_calls[0]["request_path"])

    def test_device_repair_resume_rejects_unbound_successor_certificates(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [
                {"source_macro_step": 1, "operation": "transfer"}
            ],
            "error_package": {"type": "device_workflow_repair_exhausted"},
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )

        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp, FakeSteps([PLAN_STATE], [manual_package])
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            override_path = iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE
            request = json.loads(request_path.read_text(encoding="utf-8"))
            override = json.loads(override_path.read_text(encoding="utf-8"))
            valid = _valid_v2_successor_package(request, override)

            def resign(candidate: Dict[str, Any]) -> None:
                certificate = candidate["feasibility_certificate"]
                protected = feasibility_certificate_protected_payload(
                    certificate
                )
                certificate["protected_digest"] = stable_digest(protected)
                certificate["certificate_id"] = feasibility_certificate_id(
                    protected_digest=certificate["protected_digest"],
                    device_snapshot_id=str(
                        certificate.get("device_snapshot_id") or ""
                    ),
                    device_truth_sha256=str(
                        certificate.get("device_truth_sha256") or ""
                    ),
                )

            missing = copy.deepcopy(valid)
            missing.pop("feasibility_certificate")
            bad_version = copy.deepcopy(valid)
            bad_version["feasibility_certificate"]["certificate_version"] = "2.3"
            bad_contract = copy.deepcopy(valid)
            bad_contract["feasibility_certificate"]["contract_version"] = "v1"
            bad_protected = copy.deepcopy(valid)
            bad_protected["feasibility_certificate"]["protected_digest"] = "bad"
            bad_id = copy.deepcopy(valid)
            bad_id["feasibility_certificate"]["certificate_id"] = "bad"
            bad_predecessor = copy.deepcopy(valid)
            bad_predecessor["feasibility_certificate"][
                "supersedes_certificate_id"
            ] = "wrong-predecessor"
            resign(bad_predecessor)
            bad_request = copy.deepcopy(valid)
            bad_request["feasibility_certificate"][
                "repair_request_id"
            ] = "wrong-request"
            resign(bad_request)
            bad_authority = copy.deepcopy(valid)
            bad_authority["feasibility_certificate"][
                "repair_authority"
            ] = "automatic_device_plan_rewrite"
            resign(bad_authority)
            bad_plan = copy.deepcopy(valid)
            bad_plan["container_plan"] = [{"container_id": "tampered"}]
            bad_scope = copy.deepcopy(valid)
            bad_scope["feasibility_certificate"]["authorized_change_scope"][
                "changed_contract_fields"
            ] = ["container_plan"]
            resign(bad_scope)

            cases = (
                ("missing", missing, "accepted feasibility_certificate"),
                ("version", bad_version, "version 2.4"),
                ("contract", bad_contract, "contract_version"),
                ("protected", bad_protected, "protected_digest"),
                ("certificate_id", bad_id, "certificate_id"),
                ("predecessor", bad_predecessor, "does not supersede"),
                ("request", bad_request, "repair_request_id"),
                ("authority", bad_authority, "repair_authority"),
                ("plan", bad_plan, "plan digest"),
                ("scope", bad_scope, "changed_contract_fields"),
            )
            for label, output, message in cases:
                with self.subTest(label=label):
                    resumed_steps = FakeSteps([CLOSURE_STATE], [output])
                    adapter = CountingMockAdapter()
                    runner = CampaignRunner(
                        CampaignConfig(
                            query="测试 campaign query",
                            campaign_id="cmp_test_runner",
                            campaigns_root=Path(tmp),
                            resume_device_repair=request_path,
                            device_plan_override=override_path,
                        ),
                        adapter,
                        research_step=resumed_steps.research_step,
                        device_step=resumed_steps.device_step,
                    )
                    with self.assertRaisesRegex(
                        DeviceRepairResumeError, message
                    ):
                        runner.run()
                    self.assertEqual(adapter.calls, 0)
                    self.assertEqual(resumed_steps.research_calls, [])

    def test_forward_only_resume_checks_without_execution_or_research(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [
                {"source_macro_step": 1, "operation": "transfer"}
            ],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )

        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp, FakeSteps([PLAN_STATE], [manual_package])
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            override_path = iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE
            request = json.loads(request_path.read_text(encoding="utf-8"))
            override = json.loads(override_path.read_text(encoding="utf-8"))
            resumed_package = _valid_v2_successor_package(request, override)
            resumed_steps = FakeSteps([CLOSURE_STATE], [resumed_package])
            adapter = CountingMockAdapter()

            result = CampaignRunner(
                CampaignConfig(
                    query="测试 campaign query",
                    campaign_id="cmp_test_runner",
                    campaigns_root=Path(tmp),
                    resume_device_repair=request_path,
                    device_plan_override=override_path,
                    forward_only=True,
                ),
                adapter,
                research_step=resumed_steps.research_step,
                device_step=resumed_steps.device_step,
            ).run()

            self.assertEqual(result.stop_reason, STOP_READY_FOR_DISPATCH)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(resumed_steps.research_calls, [])
            resume_dirs = sorted(
                Path(result.campaign_dir).glob(
                    "iteration_01_device_repair_resume_*"
                )
            )
            self.assertFalse((resume_dirs[-1] / "observation_in.json").exists())

    def test_conflicting_success_human_resume_skips_successor_and_dispatch(
        self,
    ) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [
                {"source_macro_step": 1, "operation": "transfer"}
            ],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )

        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp, FakeSteps([PLAN_STATE], [manual_package])
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            override_path = iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE
            resumed_steps = FakeSteps(
                [CLOSURE_STATE],
                [
                    {
                        **V2_PACKAGE_CONTRACT,
                        **UNVERIFIABLE_PACKAGE,
                        "status": "success",
                        "feedback_route": "human",
                    }
                ],
            )
            adapter = CountingMockAdapter()

            result = CampaignRunner(
                CampaignConfig(
                    query="测试 campaign query",
                    campaign_id="cmp_test_runner",
                    campaigns_root=Path(tmp),
                    resume_device_repair=request_path,
                    device_plan_override=override_path,
                ),
                adapter,
                research_step=resumed_steps.research_step,
                device_step=resumed_steps.device_step,
            ).run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            resume_dirs = sorted(
                Path(result.campaign_dir).glob(
                    "iteration_01_device_repair_resume_*"
                )
            )
            self.assertFalse(
                (resume_dirs[-1] / DEVICE_REPAIR_REQUEST).exists()
            )
            self.assertTrue(
                (resume_dirs[-1] / "AWAITING_CONDITION_REVIEW.md").exists()
            )
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(resumed_steps.research_calls, [])

    def test_forward_only_resume_continuation_checks_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "research_state.json"
            state_path.write_text(
                json.dumps(PLAN_STATE, ensure_ascii=False), encoding="utf-8"
            )
            steps = FakeSteps([CLOSURE_STATE], [SUCCESS_PACKAGE])
            adapter = CountingMockAdapter()
            runner = CampaignRunner(
                CampaignConfig(
                    query="测试 campaign query",
                    campaign_id="cmp_test_runner",
                    campaigns_root=Path(tmp),
                    forward_only=True,
                ),
                adapter,
                research_step=steps.research_step,
                device_step=steps.device_step,
            )

            stop_reason, _, _, _, _, _ = runner._continue_campaign_from_state(
                PLAN_STATE,
                state_path,
                start_iteration=2,
            )

            self.assertEqual(stop_reason, STOP_READY_FOR_DISPATCH)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(steps.research_calls, [])

    def test_device_repair_resume_rejects_contract_boundary_drift(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp, FakeSteps([PLAN_STATE], [manual_package])
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            template_path = iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE

            drifted_override = json.loads(
                template_path.read_text(encoding="utf-8")
            )
            drifted_override["contract_version"] = "v1"
            drifted_override_path = iteration_dir / "override-contract-v1.json"
            drifted_override_path.write_text(
                json.dumps(drifted_override, ensure_ascii=False),
                encoding="utf-8",
            )
            no_calls = FakeSteps([CLOSURE_STATE], [SUCCESS_PACKAGE])
            with self.assertRaisesRegex(
                DeviceRepairResumeError, "override contract_version"
            ):
                CampaignRunner(
                    CampaignConfig(
                        query="测试 campaign query",
                        campaign_id="cmp_test_runner",
                        campaigns_root=Path(tmp),
                        resume_device_repair=request_path,
                        device_plan_override=drifted_override_path,
                    ),
                    MockExecutionAdapter(),
                    research_step=no_calls.research_step,
                    device_step=no_calls.device_step,
                ).run()
            self.assertEqual(no_calls.device_calls, [])
            self.assertEqual(no_calls.research_calls, [])

            runtime_drift_calls = FakeSteps([CLOSURE_STATE], [SUCCESS_PACKAGE])
            with self.assertRaisesRegex(
                DeviceRepairResumeError, "selected runtime"
            ):
                CampaignRunner(
                    CampaignConfig(
                        query="测试 campaign query",
                        campaign_id="cmp_test_runner",
                        requested_contract_version="v1",
                        campaigns_root=Path(tmp),
                        resume_device_repair=request_path,
                        device_plan_override=template_path,
                    ),
                    MockExecutionAdapter(),
                    research_step=runtime_drift_calls.research_step,
                    device_step=runtime_drift_calls.device_step,
                ).run()
            self.assertEqual(runtime_drift_calls.device_calls, [])
            self.assertEqual(runtime_drift_calls.research_calls, [])

    def test_device_repair_resume_rejects_mismatched_output_contract(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp, FakeSteps([PLAN_STATE], [manual_package])
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            bad_output = {
                **SUCCESS_PACKAGE,
                "contract_version": "v1",
                "contract_resolution": {
                    "requested": "v1",
                    "source_input": "v2",
                    "effective": "v1",
                    "requested_matches_effective": True,
                    "source_matches_effective": False,
                },
            }
            resumed_steps = FakeSteps([CLOSURE_STATE], [bad_output])
            adapter = CountingMockAdapter()

            with self.assertRaisesRegex(
                DeviceRepairResumeError, "does not match requested"
            ):
                CampaignRunner(
                    CampaignConfig(
                        query="测试 campaign query",
                        campaign_id="cmp_test_runner",
                        campaigns_root=Path(tmp),
                        resume_device_repair=(
                            iteration_dir / DEVICE_REPAIR_REQUEST
                        ),
                        device_plan_override=(
                            iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE
                        ),
                    ),
                    adapter,
                    research_step=resumed_steps.research_step,
                    device_step=resumed_steps.device_step,
                ).run()
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(resumed_steps.research_calls, [])

    def test_human_quantity_approval_resume_is_validated_without_bootstrap(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_quantity",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "certificate_id": "certificate-quantity-001",
                "protected_digest": "certificate-digest-quantity-001",
                "research_plan_signature": "route-frozen",
                "sample_matrix_signature": "matrix-frozen",
                "device_snapshot_signature": "snapshot-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [
                {
                    "plan_step": 1,
                    "source_macro_step": 1,
                    "operation": "dry",
                }
            ],
            "batch_plan": [
                {
                    "batch_id": "wet-batch-001",
                    "sample_id": "sample-001",
                    "material_id": "wet-precursor-001",
                    "total_quantity": {"value": 0.18, "unit": "mmol"},
                },
                {
                    "batch_id": "dry-batch-001",
                    "sample_id": "sample-001",
                    "material_id": "dry-product-001",
                    "total_quantity": {"value": 0.09, "unit": "mmol"},
                },
            ],
            "material_transitions": [
                {
                    "transition_id": "transition-001",
                    "transition_kind": "state_change",
                    "quantity_basis": "measured_observation",
                    "parent_batch_ids": ["wet-batch-001"],
                    "child_batch_ids": ["dry-batch-001"],
                    "input_allocations": [
                        {
                            "batch_id": "wet-batch-001",
                            "quantity": {"value": 0.18, "unit": "mmol"},
                        }
                    ],
                    "output_allocations": [
                        {
                            "batch_id": "dry-batch-001",
                            "quantity": {"value": 0.09, "unit": "mmol"},
                        }
                    ],
                }
            ],
            "error_package": {
                "type": "unknown_yield",
                "structured_errors": [
                    {"code": "unknown_yield", "transition_id": "transition-001"}
                ],
            },
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp,
                FakeSteps([PLAN_STATE], [manual_package]),
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            override_path = iteration_dir / "device_plan_override.json"
            override = json.loads(
                (iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE).read_text(
                    encoding="utf-8"
                )
            )
            approval = dict(override["human_quantity_approval_template"])
            approval.update(
                {
                    "approval_id": "approval-quantity-001",
                    "transition_id": "transition-001",
                    "sample_id": "sample-001",
                    "material_id": "dry-product-001",
                    "batch_id": "dry-batch-001",
                    "basis": "observed_quantity",
                    "approved_quantity": {"value": 90, "unit": "umol"},
                    "approved_by": "reviewer@example.org",
                    "approved_at": "2026-08-27T06:30:00+08:00",
                    "acknowledges_scientific_review": True,
                }
            )
            approval.pop("yield_lower_bound", None)
            override["human_quantity_approvals"] = [approval]
            override_path.write_text(
                json.dumps(override, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            request = json.loads(request_path.read_text(encoding="utf-8"))
            resumed_package = _valid_v2_successor_package(request, override)
            resumed_steps = FakeSteps([CLOSURE_STATE], [resumed_package])
            config = CampaignConfig(
                query="测试 campaign query",
                campaign_id="cmp_test_runner",
                campaigns_root=Path(tmp),
                resume_device_repair=request_path,
                device_plan_override=override_path,
            )
            resumed = CampaignRunner(
                config,
                MockExecutionAdapter(),
                research_step=resumed_steps.research_step,
                device_step=resumed_steps.device_step,
            ).run()

            self.assertEqual(resumed.stop_reason, STOP_GOAL_REACHED)
            self.assertEqual(resumed.iterations_run, 1)
            self.assertEqual(len(resumed_steps.research_calls), 1)
            self.assertEqual(
                resumed_steps.research_calls[0]["event_type"], "new_observation"
            )

    def test_device_override_route_or_sample_matrix_drift_is_rejected(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [{"sample_id": "S-1", "group": "sample"}],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp,
                FakeSteps([PLAN_STATE], [manual_package]),
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            template = json.loads(
                (iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE).read_text(
                    encoding="utf-8"
                )
            )

            for mutation in ("route", "sample"):
                override = json.loads(json.dumps(template, ensure_ascii=False))
                if mutation == "route":
                    override["route_signature"] = "different-route"
                else:
                    # Dict form is also accepted for compatibility; embedding a
                    # changed matrix must still be rejected before Device runs.
                    override["device_plan"] = {
                        "steps": override["device_plan"],
                        "sample_matrix": [
                            {"sample_id": "S-2", "group": "sample"}
                        ],
                    }
                override_path = iteration_dir / f"override-{mutation}.json"
                override_path.write_text(
                    json.dumps(override, ensure_ascii=False),
                    encoding="utf-8",
                )
                config = CampaignConfig(
                    query="测试 campaign query",
                    campaign_id="cmp_test_runner",
                    campaigns_root=Path(tmp),
                    resume_device_repair=request_path,
                    device_plan_override=override_path,
                )
                with self.subTest(mutation=mutation), self.assertRaises(
                    DeviceRepairResumeError
                ):
                    CampaignRunner(
                        config,
                        MockExecutionAdapter(),
                        research_step=FakeSteps(
                            [CLOSURE_STATE], [SUCCESS_PACKAGE]
                        ).research_step,
                        device_step=FakeSteps(
                            [CLOSURE_STATE], [SUCCESS_PACKAGE]
                        ).device_step,
                    ).run()

    def test_device_override_cannot_hide_scientific_change_with_mechanical_kind(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp,
                FakeSteps([PLAN_STATE], [manual_package]),
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            override = json.loads(
                (iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE).read_text(
                    encoding="utf-8"
                )
            )
            override["changes"] = [
                {
                    "kind": "split_transfer",
                    "field": "单批总量",
                    "before": {"value": 0.18, "unit": "mmol"},
                    "after": {"value": 0.54, "unit": "mmol"},
                    "requires_scientific_review": False,
                }
            ]
            override["requires_scientific_review"] = False
            override_path = iteration_dir / "override-hidden-scientific-change.json"
            override_path.write_text(
                json.dumps(override, ensure_ascii=False),
                encoding="utf-8",
            )
            config = CampaignConfig(
                query="测试 campaign query",
                campaign_id="cmp_test_runner",
                campaigns_root=Path(tmp),
                resume_device_repair=request_path,
                device_plan_override=override_path,
            )

            with self.assertRaises(DeviceRepairResumeError):
                CampaignRunner(
                    config,
                    MockExecutionAdapter(),
                    research_step=FakeSteps(
                        [CLOSURE_STATE], [SUCCESS_PACKAGE]
                    ).research_step,
                    device_step=FakeSteps(
                        [CLOSURE_STATE], [SUCCESS_PACKAGE]
                    ).device_step,
                ).run()

    def test_failed_resumed_cycle_emits_next_versioned_repair_request(self) -> None:
        manual_package = {
            **V2_PACKAGE_CONTRACT,
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_workflow",
            "feasibility_accepted": True,
            "feasibility_certificate": {
                "accepted": True,
                "contract_version": "v2",
                "research_plan_signature": "route-frozen",
                "sample_matrix": [],
                "device_snapshot": {"WS-01": "available"},
            },
            "device_plan": [{"source_macro_step": 1, "operation": "transfer"}],
            "repair_history": [{"cycle": 1}, {"cycle": 2}],
        }
        manual_package["feasibility_certificate"] = (
            _valid_v2_feasibility_certificate(manual_package)
        )
        with tempfile.TemporaryDirectory() as tmp:
            initial = make_runner(
                tmp,
                FakeSteps([PLAN_STATE], [manual_package]),
            ).run()
            iteration_dir = Path(initial.campaign_dir) / "iteration_01"
            request_path = iteration_dir / DEVICE_REPAIR_REQUEST
            override_path = iteration_dir / DEVICE_PLAN_OVERRIDE_TEMPLATE
            first_request = json.loads(request_path.read_text(encoding="utf-8"))
            resumed_steps = FakeSteps([PLAN_STATE], [manual_package])
            config = CampaignConfig(
                query="测试 campaign query",
                campaign_id="cmp_test_runner",
                campaigns_root=Path(tmp),
                resume_device_repair=request_path,
                device_plan_override=override_path,
            )
            result = CampaignRunner(
                config,
                MockExecutionAdapter(),
                research_step=resumed_steps.research_step,
                device_step=resumed_steps.device_step,
            ).run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            self.assertEqual(len(resumed_steps.research_calls), 0)
            resume_dirs = sorted(
                Path(result.campaign_dir).glob("iteration_01_device_repair_resume_*")
            )
            next_request = json.loads(
                (resume_dirs[-1] / DEVICE_REPAIR_REQUEST).read_text(encoding="utf-8")
            )
            self.assertEqual(
                next_request["parent_request_id"], first_request["request_id"]
            )
            self.assertEqual(next_request["repair_generation"], 2)


class ReviewGateTest(IsolatedCampaignTest):
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
        quantity_change = {
            "status": "success",
            "quantity_adjustments": [
                {
                    "kind": "molar_ratio_change",
                    "requires_scientific_review": True,
                }
            ],
        }
        split_only = {
            "status": "success",
            "quantity_adjustments": [
                {
                    "kind": "split_transfer",
                    "requires_scientific_review": False,
                }
            ],
        }
        mislabeled_scientific_change = {
            "status": "success",
            "quantity_adjustments": [
                {
                    "kind": "split_transfer",
                    "field": "单批总量",
                    "before": {"value": 0.18, "unit": "mmol"},
                    "after": {"value": 0.54, "unit": "mmol"},
                    "requires_scientific_review": False,
                }
            ],
        }
        pure_capacity_split = {
            "status": "success",
            "quantity_adjustments": [
                {
                    "kind": "split_transfer",
                    "field": "transfer aliquots",
                    "before": {"total": 8, "unit": "mL"},
                    "after": {
                        "aliquots": [4, 4],
                        "total": 8,
                        "unit": "mL",
                    },
                    "requires_scientific_review": False,
                }
            ],
        }
        unlabelled_amount_change = {
            "status": "success",
            "quantity_adjustments": [
                {
                    "kind": "device_operational",
                    "before": {"value": 0.18, "unit": "mmol"},
                    "after": {"value": 0.54, "unit": "mmol"},
                    "requires_scientific_review": False,
                }
            ],
        }
        self.assertTrue(package_requires_review(quantity_change))
        self.assertFalse(package_requires_review(split_only))
        self.assertTrue(package_requires_review(mislabeled_scientific_change))
        self.assertFalse(package_requires_review(pure_capacity_split))
        self.assertTrue(package_requires_review(unlabelled_amount_change))

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
            self.assertFalse((iteration_dir / DEVICE_REPAIR_REQUEST).exists())

    def test_stage1_device_local_exhaustion_review_does_not_suggest_research(self) -> None:
        package = {
            "status": "manual_required",
            "feedback_type": "human_review_required",
            "feedback_route": "human",
            "failure_scope": "device_plan",
            "feasibility_accepted": False,
            "error_package": {
                "type": "stage1_plan_repair_exhausted",
                "blocking_constraints": ["逐瓶配方缺少可物化字段"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            steps = FakeSteps([PLAN_STATE], [package])
            runner = make_runner(tmp, steps, adapter=CountingMockAdapter())

            result = runner.run()

            self.assertEqual(result.stop_reason, STOP_MANUAL_REQUIRED)
            review = (
                Path(result.campaign_dir)
                / "iteration_01"
                / "AWAITING_CONDITION_REVIEW.md"
            ).read_text(encoding="utf-8")
            self.assertIn("不得把该错误返回 Research", review)
            self.assertNotIn("给 research layer 提出化学语义修改意见", review)


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
