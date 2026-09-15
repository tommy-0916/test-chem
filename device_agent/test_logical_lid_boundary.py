"""Research lid vocabulary must not weaken physical dispatch prerequisites.

These isolated Skill/platform fixtures model capped and uncapped injection-vial
operations. They test the checker boundary, not a real workstation's full XRD
contract, and never invoke an LLM or dispatch an experiment.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.v2 import LogicalContainerV2
from device_agent import test_dispatch_checker as fixture
from device_agent.dispatch_checker import check_dispatch


class LogicalLidBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="logical-lid-boundary-")
        self.addCleanup(temporary.cleanup)
        self.catalog = Path(temporary.name)
        exports = []
        for cap, code, description in (("open", 201, "无盖"), ("closed", 202, "有盖")):
            station = f"Fixture_{cap}_measurement"
            directory = self.catalog / "references-Synthesis-Module" / station
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(
                f"---\n工作站编码: {code}\nname: {station}\n---\n\n"
                f"# {station}\n\n## 输入输出约束\n\n"
                f"- **输入约束**\n  - 容器状态：必须{description}\n\n"
                f"## 参数设置\n\n- **检测**\n\n"
                f"{fixture.TABLE_HEADER}{fixture.CONTAINER_ROWS}",
                encoding="utf-8",
            )
            exports.append({
                "workstation": station,
                "operation": "检测",
                "parameters": [
                    {"parameter_name": "容器类型", "type": "string"},
                    {"parameter_name": "容器数量", "type": "int"},
                    {"parameter_name": "容器编号", "type": "array"},
                ],
            })
        (self.catalog / "0410数据转换.txt").write_text(
            json.dumps({"steps": exports}, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def package(required_cap: str) -> dict:
        return {"status": "success", "workflow_json": {"steps": [{
            "step_number": 8,
            "source_macro_step": 8,
            "workstation": f"Fixture_{required_cap}_measurement",
            "operation": "检测",
            "id": 201 if required_cap == "open" else 202,
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]},
        }]}}

    @staticmethod
    def initial(cap: str) -> dict:
        return {"containers": [{
            "container_type": "进样瓶", "container_id": 1, "lid_state": cap,
        }]}

    def check(self, package: dict, initial_state: dict | None = None) -> dict:
        before_package = copy.deepcopy(package)
        before_initial = copy.deepcopy(initial_state)
        report = check_dispatch(
            package, workstation_root=self.catalog, initial_state=initial_state
        )
        self.assertEqual(package, before_package)
        self.assertEqual(initial_state, before_initial)
        self.assertNotIn("checker_internal_error", [item["code"] for item in report["findings"]])
        return report

    def finding(self, report: dict, code: str) -> dict:
        matches = [item for item in report["findings"] if item["code"] == code]
        self.assertTrue(matches, report)
        return matches[0]

    def assert_unresolved_cap_rejected(self, cap: str) -> None:
        # Both strings are valid logical Research values, but neither establishes
        # the actual cap state of an identified injection vial.
        LogicalContainerV2(logical_container_id="logical-vial", lid_state=cap)
        for required in ("open", "closed"):
            with self.subTest(required_cap=required):
                report = self.check(self.package(required), self.initial(cap))
                self.assertEqual(report["status"], "failed", report)
                self.assertFalse(report["dispatchable"], report)
                invalid = self.finding(report, "invalid_initial_state")
                self.assertEqual(invalid["json_pointer"], "/initial_state/containers/0/lid_state")
                self.assertEqual(invalid["actual"], cap)
                missing = self.finding(report, "unknown_initial_lid_state")
                self.assertEqual(missing["step_index"], 0)
                self.assertEqual(missing["step_number"], 8)
                self.assertEqual(missing["json_pointer"], "/workflow_json/steps/0")

    def test_none_does_not_satisfy_either_physical_cap_requirement(self) -> None:
        self.assert_unresolved_cap_rejected("none")

    def test_unknown_does_not_satisfy_either_physical_cap_requirement(self) -> None:
        self.assert_unresolved_cap_rejected("unknown")

    def test_known_open_vial_satisfies_open_requirement(self) -> None:
        report = self.check(self.package("open"), self.initial("open"))
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"], report)

    def test_known_closed_vial_satisfies_closed_requirement(self) -> None:
        report = self.check(self.package("closed"), self.initial("closed"))
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"], report)

    def test_open_vial_cannot_satisfy_closed_requirement(self) -> None:
        report = self.check(self.package("closed"), self.initial("open"))
        self.assertEqual(report["status"], "failed", report)
        self.assertFalse(report["dispatchable"], report)
        conflict = self.finding(report, "lid_state_conflict")
        self.assertEqual(conflict["expected"], "有盖")
        self.assertEqual(conflict["actual"], "无盖")
        self.assertEqual(conflict["step_number"], 8)

    def test_closed_vial_cannot_satisfy_open_requirement(self) -> None:
        report = self.check(self.package("open"), self.initial("closed"))
        self.assertEqual(report["status"], "failed", report)
        self.assertFalse(report["dispatchable"], report)
        conflict = self.finding(report, "lid_state_conflict")
        self.assertEqual(conflict["expected"], "无盖")
        self.assertEqual(conflict["actual"], "有盖")
        self.assertEqual(conflict["step_number"], 8)

    def test_logical_requirement_alone_is_not_physical_initial_state(self) -> None:
        for cap in ("open", "closed", "none", "unknown"):
            for required in ("open", "closed"):
                with self.subTest(logical_cap=cap, required_cap=required):
                    package = self.package(required)
                    logical = LogicalContainerV2(
                        logical_container_id="logical-vial", container_type="进样瓶", lid_state=cap
                    )
                    package["macro_plan"] = [{"container_requirements": [logical.model_dump()]}]
                    report = self.check(package)
                    self.assertEqual(report["status"], "not_verifiable", report)
                    self.assertFalse(report["dispatchable"], report)
                    self.finding(report, "unknown_initial_lid_state")

    def test_carrier_without_lid_does_not_replace_vial_cap_evidence(self) -> None:
        package = self.package("open")
        carrier = LogicalContainerV2(
            logical_container_id="A02-XRD-CARRIER", container_type="XRD基底片", lid_state="none"
        )
        package["macro_plan"] = [{"container_requirements": [carrier.model_dump()]}]
        unresolved = self.check(package)
        self.assertFalse(unresolved["dispatchable"], unresolved)
        self.finding(unresolved, "unknown_initial_lid_state")
        verified = self.check(package, self.initial("open"))
        self.assertEqual(verified["status"], "passed", verified)
        self.assertTrue(verified["dispatchable"], verified)


if __name__ == "__main__":
    unittest.main()
