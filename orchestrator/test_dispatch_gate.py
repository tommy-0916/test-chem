"""Fresh contract checks guard normal, resumed and continued execution paths."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from device_agent.dispatch_checker import check_dispatch
from device_agent.dispatch_wire_checker import check_wire_payload
from device_agent.check_workflow import main as check_workflow_main
from orchestrator.runner import (
    REPO_ROOT,
    STOP_DEVICE_ERROR,
    DispatchCheckBlockedError,
)
from orchestrator.test_runner import (
    CLOSURE_STATE,
    PLAN_STATE,
    SUCCESS_PACKAGE,
    CountingMockAdapter,
    FakeSteps,
    make_runner,
)


class DispatchGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Use the real checker to obtain a complete report shape. These legacy
        # success fixtures have no workstation contract and cannot dispatch.
        cls.rejected = check_dispatch(copy.deepcopy(SUCCESS_PACKAGE), require_payload=True)
        assert cls.rejected["dispatchable"] is False

    def make_context(self, tmp: str):
        steps = FakeSteps([PLAN_STATE, CLOSURE_STATE], [copy.deepcopy(SUCCESS_PACKAGE)])
        adapter = CountingMockAdapter()
        runner = make_runner(tmp, steps, adapter=adapter)
        return runner, steps, adapter

    def assert_reports(self, directory: Path) -> None:
        self.assertTrue((directory / "workflow_check.json").is_file())
        self.assertTrue((directory / "workflow_check.md").is_file())
        report = json.loads((directory / "workflow_check.json").read_text(encoding="utf-8"))
        self.assertFalse(report["dispatchable"])

    def real_cross_source_package(self, addition_count: int) -> dict:
        """Same named liquid across source bottles; identity details are absent."""
        source = REPO_ROOT / "device_agent/examples/dispatch_check/valid_workflow.json"
        workflow = json.loads(source.read_text(encoding="utf-8"))
        addition_template = copy.deepcopy(workflow["steps"][2])
        workflow["steps"] = workflow["steps"][:2]
        for index in range(addition_count):
            step = copy.deepcopy(addition_template)
            step["step_number"] = index + 3
            row = step["parameters"]["加样方案"][0]
            liquid = row.pop("1号原液瓶")
            liquid["原液用量"] = 1.0
            row["1号原液瓶" if index < 2 else "2号原液瓶"] = liquid
            workflow["steps"].append(step)
        contract_root = (
            REPO_ROOT / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation"
        )
        wire = check_wire_payload(workflow, None, workstation_root=contract_root)
        self.assertEqual(wire["findings"], [], wire["findings"])
        return {
            "status": "success",
            "workflow_json": workflow,
            "dispatch_payload": wire["expected_payload"],
        }

    def test_real_cross_source_same_name_over_limit_blocks_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, steps, adapter = self.make_context(tmp)
            runner.config.device_args = [
                "--workstations-dir",
                str(REPO_ROOT / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation"),
            ]
            package = self.real_cross_source_package(4)
            original = copy.deepcopy(package)
            blocked = False
            try:
                runner._execute_checked_package(package, Path(tmp))
            except DispatchCheckBlockedError:
                blocked = True
            report = json.loads((Path(tmp) / "workflow_check.json").read_text(encoding="utf-8"))
            self.assertTrue(
                blocked,
                f"Gate did not block: status={report['status']}, adapter.calls={adapter.calls}",
            )
            self.assertEqual(report["status"], "not_verifiable", report)
            self.assertFalse(report["dispatchable"])
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(steps.research_calls, [])
            self.assertEqual(package, original)
            finding = next(
                item for item in report["findings"]
                if item["code"] == "cross_source_reagent_identity_unverified"
            )
            self.assertEqual(finding["severity"], "unverified")
            self.assertEqual(finding["step_number"], 6)
            self.assertEqual(finding["step_index"], 5)
            self.assertEqual(
                finding["json_pointer"],
                "/workflow_json/steps/5/parameters/加样方案/0/2号原液瓶/原液用量",
            )
            self.assertEqual(finding["related_pointers"], [
                "/workflow_json/steps/2/parameters/加样方案/0/1号原液瓶/原液用量",
                "/workflow_json/steps/3/parameters/加样方案/0/1号原液瓶/原液用量",
                "/workflow_json/steps/4/parameters/加样方案/0/2号原液瓶/原液用量",
            ])
            self.assert_reports(Path(tmp))

    def test_real_cross_source_at_three_ml_boundary_passes_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, steps, adapter = self.make_context(tmp)
            runner.config.device_args = [
                "--workstations-dir",
                str(REPO_ROOT / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation"),
            ]
            package = self.real_cross_source_package(3)
            original = copy.deepcopy(package)
            observation = runner._execute_checked_package(package, Path(tmp))
            report = json.loads((Path(tmp) / "workflow_check.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed", report)
            self.assertTrue(report["dispatchable"])
            self.assertEqual(report["findings"], [])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(steps.research_calls, [])
            self.assertEqual(observation["source"], "mock_execution_adapter")
            self.assertEqual(package, original)

    def test_real_check_blocks_legacy_success_before_adapter_or_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, steps, adapter = self.make_context(tmp)
            result = runner.run()
            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual([c["event_type"] for c in steps.research_calls], ["bootstrap"])
            directory = Path(result.campaign_dir) / "iteration_01"
            self.assert_reports(directory)
            self.assertFalse((directory / "observation_in.json").exists())

    def test_resume_rejection_preserves_frozen_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, steps, adapter = self.make_context(tmp)
            directory = Path(tmp) / "resume"
            directory.mkdir()
            state = copy.deepcopy(PLAN_STATE)
            state_path = Path(tmp) / "frozen.json"
            with patch("orchestrator.runner.check_dispatch", return_value=self.rejected) as check:
                result = runner._execute_resumed_package(
                    copy.deepcopy(SUCCESS_PACKAGE), state, state_path, directory, 1
                )
            self.assertEqual(result, (STOP_DEVICE_ERROR, False, state, state_path))
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(steps.research_calls, [])
            self.assertEqual(state, PLAN_STATE)
            self.assertTrue(check.call_args.kwargs["require_payload"])
            self.assert_reports(directory)

    def test_continuation_rejection_never_replans_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, steps, adapter = self.make_context(tmp)
            state_path = Path(tmp) / "research_state.json"
            with patch("orchestrator.runner.check_dispatch", return_value=self.rejected):
                result = runner._continue_campaign_from_state(
                    copy.deepcopy(PLAN_STATE), state_path, start_iteration=2
                )
            self.assertEqual(result[0], STOP_DEVICE_ERROR)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(steps.research_calls, [])
            self.assertEqual(result[4], state_path)
            self.assert_reports(runner.campaign_dir / "iteration_02")

    def test_not_verifiable_also_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, adapter = self.make_context(tmp)
            report = dict(self.rejected, status="not_verifiable", dispatchable=False)
            with patch("orchestrator.runner.check_dispatch", return_value=report):
                result = runner.run()
            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(adapter.calls, 0)

    def test_supplied_pass_attestation_is_never_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, adapter = self.make_context(tmp)
            package = copy.deepcopy(SUCCESS_PACKAGE)
            package["workflow_check"] = {"status": "passed", "dispatchable": True}
            package["dispatch_validation"] = {"status": "passed"}
            original = copy.deepcopy(package)
            with patch("orchestrator.runner.check_dispatch", return_value=self.rejected) as check:
                with self.assertRaises(DispatchCheckBlockedError):
                    runner._execute_checked_package(package, Path(tmp))
            check.assert_called_once()
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(package, original)

    def test_pass_checks_each_adapter_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, adapter = self.make_context(tmp)
            report = dict(self.rejected, status="passed", dispatchable=True, findings=[])
            package = copy.deepcopy(SUCCESS_PACKAGE)
            package["quantity_adjustments"] = [{"amount": 1}]
            original = copy.deepcopy(package)
            with patch("orchestrator.runner.check_dispatch", return_value=report) as check:
                observation = runner._execute_checked_package(package, Path(tmp))
                runner._execute_checked_package(package, Path(tmp))
            self.assertEqual(check.call_count, 2)
            self.assertEqual(adapter.calls, 2)
            self.assertIn("actual_execution_parameters", observation)
            self.assertEqual(package, original)

    def test_checker_fault_is_local_error_with_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, steps, adapter = self.make_context(tmp)
            with patch("orchestrator.runner.check_dispatch", side_effect=RuntimeError("catalog failed")):
                result = runner.run()
            self.assertEqual(result.stop_reason, STOP_DEVICE_ERROR)
            self.assertEqual(adapter.calls, 0)
            self.assertEqual(len(steps.research_calls), 1)
            self.assert_reports(runner.campaign_dir / "iteration_01")

    def test_unsaved_report_blocks_even_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, adapter = self.make_context(tmp)
            report = dict(self.rejected, status="passed", dispatchable=True)
            with patch("orchestrator.runner.check_dispatch", return_value=report), patch(
                "orchestrator.runner.write_check_report", side_effect=OSError("read-only")
            ):
                with self.assertRaises(DispatchCheckBlockedError):
                    runner._execute_checked_package(copy.deepcopy(SUCCESS_PACKAGE), Path(tmp))
            self.assertEqual(adapter.calls, 0)

    def test_selected_source_is_explicit_and_has_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _ = self.make_context(tmp)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    runner._dispatch_workstation_root(),
                    REPO_ROOT / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation",
                )
                runner.config.device_args = ["--workstations-dir=missing-custom-contracts"]
                self.assertEqual(runner._dispatch_workstation_root(), REPO_ROOT / "missing-custom-contracts")
            with patch.dict(os.environ, {"CHEM_WORKSTATIONS_NEW_DIR": tmp}):
                runner.config.device_args = ["--workstations-dir", "explicit-contracts"]
                self.assertEqual(runner._dispatch_workstation_root(), REPO_ROOT / "explicit-contracts")
                runner.config.device_args = []
                self.assertEqual(runner._dispatch_workstation_root(), Path(tmp).resolve())

    def test_cli_real_invalid_workflow_emits_reports_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "device_package.json"
            input_path.write_text(json.dumps(SUCCESS_PACKAGE), encoding="utf-8")
            original = input_path.read_bytes()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = check_workflow_main(["--input", str(input_path), "--require-payload", "--json"])
            self.assertEqual(exit_code, 1)
            self.assertFalse(json.loads(stdout.getvalue())["dispatchable"])
            self.assertEqual(input_path.read_bytes(), original)
            self.assert_reports(Path(tmp) / "check-report")

    def test_cli_initial_state_must_be_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initial = Path(tmp) / "initial.json"
            initial.write_text("[]", encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                check_workflow_main(["--input", str(Path(tmp) / "workflow.json"), "--initial-state", str(initial)])
            self.assertEqual(error.exception.code, 2)

    def test_cli_pass_and_option_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initial = Path(tmp) / "initial.json"
            initial.write_text('{"containers": {}}', encoding="utf-8")
            report = dict(self.rejected, status="passed", dispatchable=True, findings=[])
            args = [
                "--input", str(Path(tmp) / "workflow.json"),
                "--report-dir", str(Path(tmp) / "reports"),
                "--artifact-root", tmp,
                "--workstations-dir", str(Path(tmp) / "contracts"),
                "--initial-state", str(initial), "--require-payload",
            ]
            with patch("device_agent.check_workflow.check_dispatch_file", return_value=report) as check, redirect_stdout(io.StringIO()):
                self.assertEqual(check_workflow_main(args), 0)
            self.assertTrue(check.call_args.kwargs["require_payload"])
            self.assertEqual(check.call_args.kwargs["initial_state"], {"containers": {}})
            self.assertEqual(check.call_args.kwargs["workstation_root"], Path(tmp) / "contracts")
            self.assertEqual(check.call_args.kwargs["artifact_root"], Path(tmp))

    def test_cli_report_write_error_has_exit_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("device_agent.check_workflow.check_dispatch_file", return_value=self.rejected), patch(
                "device_agent.check_workflow.write_check_report", side_effect=OSError("report unavailable")
            ), redirect_stderr(io.StringIO()) as stderr:
                result = check_workflow_main(["--input", str(Path(tmp) / "input.json")])
            self.assertEqual(result, 2)
            self.assertIn("report unavailable", stderr.getvalue())

    def test_cli_report_names_cannot_overwrite_input(self) -> None:
        for filename in ("workflow_check.json", "workflow_check.md"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / filename
                source.write_text(json.dumps(SUCCESS_PACKAGE), encoding="utf-8")
                original = source.read_bytes()
                with patch("device_agent.check_workflow.write_check_report") as writer, redirect_stderr(io.StringIO()):
                    result = check_workflow_main(["--input", str(source), "--report-dir", tmp])
                self.assertEqual(result, 2)
                writer.assert_not_called()
                self.assertEqual(source.read_bytes(), original)

    def test_cli_report_cannot_overwrite_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "workflow.json"
            source.write_text(json.dumps(SUCCESS_PACKAGE), encoding="utf-8")
            initial = Path(tmp) / "workflow_check.json"
            initial.write_text('{"containers": {}}', encoding="utf-8")
            original = initial.read_bytes()
            with patch("device_agent.check_workflow.write_check_report") as writer, redirect_stderr(io.StringIO()):
                result = check_workflow_main([
                    "--input", str(source), "--initial-state", str(initial), "--report-dir", tmp,
                ])
            self.assertEqual(result, 2)
            writer.assert_not_called()
            self.assertEqual(initial.read_bytes(), original)

    def test_cli_hardlinked_report_cannot_overwrite_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "workflow.json"
            source.write_text(json.dumps(SUCCESS_PACKAGE), encoding="utf-8")
            os.link(source, Path(tmp) / "workflow_check.json")
            original = source.read_bytes()
            with patch("device_agent.check_workflow.write_check_report") as writer, redirect_stderr(io.StringIO()):
                result = check_workflow_main(["--input", str(source), "--report-dir", tmp])
            self.assertEqual(result, 2)
            writer.assert_not_called()
            self.assertEqual(source.read_bytes(), original)

    def test_report_write_failure_blocks_all_three_execution_paths(self) -> None:
        for execution_path in ("normal", "resume", "continue"):
            with self.subTest(execution_path=execution_path), tempfile.TemporaryDirectory() as tmp:
                runner, steps, adapter = self.make_context(tmp)
                report = dict(self.rejected, status="passed", dispatchable=True, findings=[])
                with patch("orchestrator.runner.check_dispatch", return_value=report), patch(
                    "orchestrator.runner.write_check_report", side_effect=OSError("unwritable")
                ):
                    if execution_path == "normal":
                        stop_reason = runner.run().stop_reason
                    elif execution_path == "resume":
                        stop_reason = runner._execute_resumed_package(
                            copy.deepcopy(SUCCESS_PACKAGE), copy.deepcopy(PLAN_STATE),
                            Path(tmp) / "state.json", Path(tmp), 1,
                        )[0]
                    else:
                        stop_reason = runner._continue_campaign_from_state(
                            copy.deepcopy(PLAN_STATE), Path(tmp) / "state.json", start_iteration=2,
                        )[0]
                self.assertEqual(stop_reason, STOP_DEVICE_ERROR)
                self.assertEqual(adapter.calls, 0)
                self.assertNotIn("new_observation", [call["event_type"] for call in steps.research_calls])


if __name__ == "__main__":
    unittest.main()
