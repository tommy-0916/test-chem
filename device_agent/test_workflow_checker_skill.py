"""Black-box compatibility tests for the unified offline workflow-checker Skill."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from device_agent.test_dispatch_checker import write_fixture_catalog

SKILL_SCRIPT = (
    REPO_ROOT / "chem_resources" / "lab-design-all" / "skills"
    / "workflow-checker" / "scripts" / "check.py"
)
ORIGINAL_CLI = REPO_ROOT / "device_agent" / "check_workflow.py"


class WorkflowCheckerSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="workflow-checker-skill-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.outside_cwd = self.root / "outside-repository"
        self.outside_cwd.mkdir()
        self.catalog = self.root / "synthetic-contracts"
        write_fixture_catalog(self.catalog)
        self.source = self.root / "workflow.json"
        self.report_dir = self.root / "skill-report"
        self.environment = dict(os.environ)
        self.environment.update(PYTHONPATH="", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    @staticmethod
    def package(*, bad_nested: bool = False) -> dict[str, Any]:
        parameters: dict[str, Any] = {"温度": 20, "模式": 1}
        if bad_nested:
            parameters["配方"] = [{"a/b~c": "not-an-integer"}]
        return {
            "status": "success",
            "workflow_json": {"steps": [{
                "step_number": 1,
                "workstation": "Fixture_Station",
                "operation": "检测",
                "id": 101,
                "source_macro_step": 1,
                "parameters": parameters,
            }]},
        }

    def write_input(self, package: dict[str, Any], *, path: Path | None = None) -> Path:
        destination = path or self.source
        destination.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination

    def run_script(self, *arguments: str, script: Path = SKILL_SCRIPT) -> subprocess.CompletedProcess[str]:
        self.assertTrue(script.is_file(), f"Missing checker entry point: {script}")
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=self.outside_cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def run_check(
        self, *extra: str, source: Path | None = None,
        catalog: Path | None = None, report_dir: Path | None = None,
        script: Path = SKILL_SCRIPT,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "--input", str(source or self.source),
            "--workstations-dir", str(catalog or self.catalog),
            "--report-dir", str(report_dir or self.report_dir),
            "--json", *extra, script=script,
        )

    def report_from(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"Checker did not emit a JSON report: {exc}; stderr={result.stderr}; stdout={result.stdout}")
        self.assertIsInstance(report, dict)
        return report

    def test_help_works_from_outside_repository(self) -> None:
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--input", "--require-payload", "--workstations-dir", "--initial-state", "--json"):
            self.assertIn(flag, result.stdout)
        self.assertFalse(self.report_dir.exists())

    def test_valid_synthetic_workflow_passes_and_preserves_input(self) -> None:
        self.write_input(self.package())
        original = self.source.read_bytes()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = self.report_from(result)
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue((self.report_dir / "workflow_check.json").is_file())
        self.assertTrue((self.report_dir / "workflow_check.md").is_file())

    def test_nested_error_retains_exact_parameter_pointer(self) -> None:
        self.write_input(self.package(bad_nested=True))
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        report = self.report_from(result)
        self.assertEqual(report["status"], "failed", report)
        expected = "/workflow_json/steps/0/parameters/配方/0/a~1b~0c"
        matches = [finding for finding in report["findings"] if finding["json_pointer"] == expected]
        self.assertTrue(matches, report)
        self.assertTrue(any(finding["code"] == "type_mismatch" for finding in matches))
        saved = json.loads((self.report_dir / "workflow_check.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["findings"], report["findings"])
        self.assertIn(expected, (self.report_dir / "workflow_check.md").read_text(encoding="utf-8"))

    def test_required_missing_dispatch_payload_blocks(self) -> None:
        self.write_input(self.package())
        result = self.run_check("--require-payload")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = self.report_from(result)
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertIn("missing_dispatch_payload", [item["code"] for item in report["findings"]])

    def test_explicit_empty_contract_root_does_not_fall_back(self) -> None:
        self.write_input(self.package())
        empty = self.root / "empty-contracts"
        empty.mkdir()
        result = self.run_check(catalog=empty)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = self.report_from(result)
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"])

    def test_report_conflict_cannot_overwrite_original_input(self) -> None:
        source = self.write_input(self.package(), path=self.root / "workflow_check.json")
        original = source.read_bytes()
        result = self.run_check(source=source, report_dir=self.root)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("overwrite an input file", result.stderr)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.root / "workflow_check.md").exists())

    def test_skill_and_original_cli_reports_have_identical_findings(self) -> None:
        self.write_input(self.package(bad_nested=True))
        skill_result = self.run_check()
        original_result = self.run_check(script=ORIGINAL_CLI, report_dir=self.root / "original-report")
        self.assertEqual(skill_result.returncode, original_result.returncode)
        skill_report, original_report = self.report_from(skill_result), self.report_from(original_result)
        for field in ("status", "dispatchable", "findings", "input_sha256", "summary"):
            self.assertEqual(skill_report[field], original_report[field], field)


if __name__ == "__main__":
    unittest.main()
