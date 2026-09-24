"""Offline dispatch checks preserve evidence and fail closed on missing facts.

The synthetic contracts deliberately do not depend on a developer's workstation
paths or the production catalog's station count. No LLM or network is needed.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dispatch_checker import check_dispatch, check_dispatch_file, write_check_report


TABLE_HEADER = """| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|--------|------|----------|------|------|--------|--------|
"""

BASE_ROWS = """| 温度 | 范围 [0,100] | 是 | ℃ | int | 20 | |
| 模式 | [{"label":"常规","value":1},{"label":"快速","value":2}] | 是 | | int | 1 | |
| 配方 | 嵌套结构 | 否 | | array | | |
| -a/b~c | 必填整数 | 是 | | int | 1 | |
"""

CONTAINER_ROWS = """| 容器类型 | 容器类型 | 是 | | string | 进样瓶 | |
| 容器数量 | 容器数量 | 是 | | int | 1 | |
| 容器编号 | 容器编号 | 是 | | array | [1] | |
"""


def write_fixture_catalog(root: Path) -> None:
    """Create exact Skill tables and a matching platform schema export."""
    contracts = [
        ("Fixture_Station", 101, "检测", BASE_ROWS, ""),
        (
            "Fixture_File",
            102,
            "加载文件",
            "| 输入文件 | 输入文件 | 是 | | file | input.csv | |\n",
            "",
        ),
        (
            "Fixture_Open",
            103,
            "开盖",
            CONTAINER_ROWS,
            "## 输入输出约束\n\n"
            "- **输出约束**\n  - 容器状态：必须无盖\n\n",
        ),
        (
            "Fixture_Closed",
            104,
            "封闭检测",
            CONTAINER_ROWS,
            "## 输入输出约束\n\n"
            "- **输入约束**\n  - 容器状态：必须有盖\n\n",
        ),
    ]
    for station, station_id, operation, rows, constraints in contracts:
        directory = root / "references-Synthesis-Module" / station
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\n工作站编码: {station_id}\nname: {station}\n---\n\n"
            f"# {station}\n\n{constraints}"
            f"## 参数设置\n\n- **{operation}**\n\n{TABLE_HEADER}{rows}",
            encoding="utf-8",
        )

    def parameter(name: str, kind: str, **extra: Any) -> dict[str, Any]:
        return {"parameter_name": name, "type": kind, **extra}

    container_parameters = [
        parameter("容器类型", "string"),
        parameter("容器数量", "int"),
        parameter("容器编号", "array"),
    ]
    export = {
        "steps": [
            {
                "workstation": "Fixture_Station",
                "operation": "检测",
                "parameters": [
                    parameter("温度", "int", range={"min": 0, "max": 100}),
                    parameter("模式", "int", options=[1, 2]),
                    parameter("配方", "array"),
                ],
            },
            {
                "workstation": "Fixture_File",
                "operation": "加载文件",
                "parameters": [parameter("输入文件", "file")],
            },
            {
                "workstation": "Fixture_Open",
                "operation": "开盖",
                "parameters": container_parameters,
            },
            {
                "workstation": "Fixture_Closed",
                "operation": "封闭检测",
                "parameters": container_parameters,
            },
        ]
    }
    (root / "0410数据转换.txt").write_text(
        json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class DispatchCheckerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dispatch-checker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "contracts"
        write_fixture_catalog(self.catalog)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()

    @staticmethod
    def step(number: Any = 1) -> dict[str, Any]:
        return {
            "step_number": number,
            "workstation": "Fixture_Station",
            "operation": "检测",
            "id": 101,
            "source_macro_step": 1,
            "parameters": {"温度": 20, "模式": 1},
        }

    def package(self, *steps: dict[str, Any]) -> dict[str, Any]:
        return {"status": "success", "workflow_json": {"steps": list(steps) or [self.step()]}}

    def check(self, payload: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("workstation_root", self.catalog)
        return check_dispatch(payload, **kwargs)

    def finding(self, report: dict[str, Any], code: str) -> dict[str, Any]:
        matches = [item for item in report["findings"] if item["code"] == code]
        self.assertTrue(matches, f"Missing {code}: {report}")
        return matches[0]

    def assert_failed(self, report: dict[str, Any]) -> None:
        self.assertEqual(report["status"], "failed", report)
        self.assertFalse(report["dispatchable"], report)

    def file_package(self, path: str) -> dict[str, Any]:
        step = self.step()
        step.update(workstation="Fixture_File", operation="加载文件", id=102)
        step["parameters"] = {"输入文件": path}
        return self.package(step)

    def container_step(self, number: int, *, opening: bool) -> dict[str, Any]:
        step = self.step(number)
        step.update(
            workstation="Fixture_Open" if opening else "Fixture_Closed",
            operation="开盖" if opening else "封闭检测",
            id=103 if opening else 104,
        )
        step["parameters"] = {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]}
        return step

    def state_contract(
        self, *, opening: bool = False, inputs: tuple[str, ...] = (), outputs: tuple[str, ...] = ()
    ) -> None:
        """Replace only a temporary fixture's I/O prose; keep its wire schema."""
        station = "Fixture_Open" if opening else "Fixture_Closed"
        station_id = 103 if opening else 104
        operation = "开盖" if opening else "封闭检测"
        io_text = "## 输入输出约束\n\n"
        if inputs:
            io_text += "- **输入约束**\n" + "".join(f"  - {line}\n" for line in inputs)
        if outputs:
            io_text += "- **输出约束**\n" + "".join(f"  - {line}\n" for line in outputs)
        skill = self.catalog / "references-Synthesis-Module" / station / "SKILL.md"
        skill.write_text(
            f"---\n工作站编码: {station_id}\nname: {station}\n---\n\n"
            f"# {station}\n\n{io_text}\n"
            f"## 参数设置\n\n- **{operation}**\n\n{TABLE_HEADER}{CONTAINER_ROWS}",
            encoding="utf-8",
        )

    @staticmethod
    def sample_initial(sample: str) -> dict[str, Any]:
        return {"containers": [{
            "container_type": "进样瓶", "container_id": 1, "sample_state": sample,
        }]}

    def test_valid_package_passes_without_mutating_input(self) -> None:
        package = self.package()
        before = copy.deepcopy(package)
        report = self.check(package)
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"], report)
        self.assertEqual(package, before)

    def test_bare_workflow_preserves_its_source_pointer(self) -> None:
        workflow = {"steps": [self.step()]}
        workflow["steps"][0]["id"] = -1
        report = self.check(workflow)
        self.assertEqual(
            self.finding(report, "workstation_id_mismatch")["json_pointer"], "/steps/0/id"
        )

    def test_valid_bare_wire_payload_is_accepted(self) -> None:
        payload = {"experiment_steps": {"steps": [self.step()], "unknown_steps": None}, "plan_name": "fixture"}
        report = self.check(payload)
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"], report)

    def test_wrong_station_id_is_reported_before_normalization(self) -> None:
        package = self.package()
        package["workflow_json"]["steps"][0]["id"] = 999
        before = copy.deepcopy(package)
        report = self.check(package)
        finding = self.finding(report, "workstation_id_mismatch")
        self.assert_failed(report)
        self.assertEqual(finding["expected"], 101)
        self.assertEqual(finding["actual"], 999)
        self.assertEqual(finding["json_pointer"], "/workflow_json/steps/0/id")
        self.assertEqual(package, before)

    def test_operation_must_belong_to_station(self) -> None:
        step = self.step()
        step["operation"] = "不存在的操作"
        report = self.check(self.package(step))
        self.assert_failed(report)
        self.assertEqual(self.finding(report, "unknown_operation")["step_index"], 0)

    def test_missing_required_parameter_has_contract_evidence(self) -> None:
        step = self.step()
        del step["parameters"]["温度"]
        report = self.check(self.package(step))
        finding = self.finding(report, "missing_required_parameter")
        self.assert_failed(report)
        self.assertEqual(finding["json_pointer"], "/workflow_json/steps/0/parameters/温度")
        self.assertTrue(finding["skill_path"].endswith("SKILL.md"))
        self.assertGreater(finding["skill_line"], 0)

    def test_integer_parameter_rejects_string_and_boolean(self) -> None:
        for value in ("20", True):
            with self.subTest(value=value):
                step = self.step()
                step["parameters"]["温度"] = value
                report = self.check(self.package(step))
                self.assert_failed(report)
                self.assertEqual(self.finding(report, "type_mismatch")["actual"], value)

    def test_enum_value_is_checked(self) -> None:
        step = self.step()
        step["parameters"]["模式"] = 3
        report = self.check(self.package(step))
        self.assert_failed(report)
        self.assertEqual(self.finding(report, "invalid_enum_value")["actual"], 3)

    def test_numeric_range_checks_boundaries(self) -> None:
        for value in (0, 100, -1, 101):
            with self.subTest(value=value):
                step = self.step()
                step["parameters"]["温度"] = value
                report = self.check(self.package(step))
                if value in (0, 100):
                    self.assertEqual(report["status"], "passed", report)
                else:
                    self.assert_failed(report)
                    self.finding(report, "value_out_of_range")

    def test_unknown_parameter_retains_empty_actual_values(self) -> None:
        for value in ([], {}):
            with self.subTest(value=value):
                step = self.step()
                step["parameters"]["未声明"] = value
                report = self.check(self.package(step))
                finding = self.finding(report, "unknown_parameter")
                self.assert_failed(report)
                self.assertIn("actual", finding)
                self.assertEqual(finding["actual"], value)

    def test_nested_array_pointer_escapes_slash_and_tilde(self) -> None:
        step = self.step()
        step["parameters"]["配方"] = [{"a/b~c": 1}, {"a/b~c": "bad"}]
        report = self.check(self.package(step))
        finding = self.finding(report, "type_mismatch")
        self.assert_failed(report)
        self.assertEqual(finding["json_pointer"], "/workflow_json/steps/0/parameters/配方/1/a~1b~0c")
        self.assertEqual(finding["actual"], "bad")

    def test_duplicate_numbers_locate_both_original_occurrences(self) -> None:
        report = self.check(self.package(self.step(7), self.step(7)))
        finding = self.finding(report, "duplicate_step_number")
        self.assert_failed(report)
        self.assertEqual(finding["step_index"], 1)
        self.assertEqual(finding["step_number"], 7)
        self.assertEqual(finding["json_pointer"], "/workflow_json/steps/1/step_number")
        self.assertIn("/workflow_json/steps/0/step_number", finding["related_pointers"])

    def test_unhashable_and_boolean_step_numbers_never_crash(self) -> None:
        for value in ([], {}, True):
            with self.subTest(value=value):
                report = self.check(self.package(self.step(value)))
                finding = self.finding(report, "invalid_step_number")
                self.assert_failed(report)
                self.assertEqual(finding["actual"], value)

    def test_empty_workflow_is_not_dispatchable(self) -> None:
        report = self.check({"workflow_json": {"steps": []}})
        self.assert_failed(report)
        self.finding(report, "empty_workflow")

    def test_malformed_json_returns_report_instead_of_exception(self) -> None:
        path = self.artifacts / "broken.json"
        path.write_text('{"workflow_json": {"steps": [}\n', encoding="utf-8")
        report = check_dispatch_file(path, workstation_root=self.catalog)
        self.assert_failed(report)
        self.finding(report, "invalid_json")

    def test_non_finite_numbers_never_pass_in_memory_or_json_file(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=str(value)):
                step = self.step()
                step["parameters"]["温度"] = value
                package = self.package(step)
                report = self.check(package)
                self.assert_failed(report)
                self.finding(report, "non_finite_number")
                path = self.artifacts / "non-finite.json"
                path.write_text(json.dumps(package), encoding="utf-8")
                report = check_dispatch_file(path, workstation_root=self.catalog)
                self.assert_failed(report)
                self.assertTrue(
                    {"non_finite_number", "invalid_json"}.intersection(
                        item["code"] for item in report["findings"]
                    ), report,
                )

    def test_missing_catalog_is_unverifiable_and_never_passes(self) -> None:
        report = check_dispatch(self.package(), workstation_root=self.root / "absent-catalog")
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        self.assertTrue(any(item["severity"] == "unverified" for item in report["findings"]))

    def test_station_without_parsed_operation_contract_cannot_pass(self) -> None:
        skill = self.catalog / "references-Synthesis-Module" / "Fixture_Station" / "SKILL.md"
        skill.write_text("工作站编码: 101\nname: Fixture_Station\n", encoding="utf-8")
        report = self.check(self.package())
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)

    def test_missing_required_file_is_a_located_error(self) -> None:
        report = self.check(self.file_package("absent.csv"), artifact_root=self.artifacts)
        finding = self.finding(report, "missing_input_file")
        self.assert_failed(report)
        self.assertEqual(finding["json_pointer"], "/workflow_json/steps/0/parameters/输入文件")

    def test_relative_file_uses_input_directory_or_explicit_root_only(self) -> None:
        name = "only-near-the-input.csv"
        (self.artifacts / name).write_text("x\n1\n", encoding="utf-8")
        package = self.file_package(name)
        source = self.artifacts / "device_package.json"
        source.write_text(json.dumps(package), encoding="utf-8")
        report = check_dispatch_file(source, workstation_root=self.catalog)
        self.assertFalse(any(item["code"] == "missing_input_file" for item in report["findings"]), report)

        other = self.root / "explicit-empty-root"
        other.mkdir()
        report = self.check(package, source_path=source, artifact_root=other)
        self.assert_failed(report)
        self.finding(report, "missing_input_file")

    def test_file_inside_skill_directory_does_not_satisfy_artifact_reference(self) -> None:
        name = "wrong-search-root.csv"
        station_dir = self.catalog / "references-Synthesis-Module" / "Fixture_File"
        (station_dir / name).write_text("x\n1\n", encoding="utf-8")
        report = self.check(self.file_package(name), artifact_root=self.artifacts)
        self.assert_failed(report)
        self.finding(report, "missing_input_file")

    def test_remote_file_is_unverifiable_without_network_side_effects(self) -> None:
        report = self.check(self.file_package("https://example.invalid/input.csv"))
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        self.assertTrue(any(item["severity"] == "unverified" for item in report["findings"]))

    def test_required_lid_state_without_initial_state_is_unverifiable(self) -> None:
        report = self.check(self.package(self.container_step(1, opening=False)))
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        self.assertTrue(any(item["severity"] == "unverified" for item in report["findings"]))

    def test_lid_conflict_points_back_to_prior_opening_step(self) -> None:
        report = self.check(self.package(
            self.container_step(11, opening=True), self.container_step(12, opening=False)
        ))
        finding = self.finding(report, "lid_state_conflict")
        self.assert_failed(report)
        self.assertEqual(finding["step_index"], 1)
        self.assertEqual(finding["step_number"], 12)
        self.assertTrue(finding["json_pointer"].startswith("/workflow_json/steps/1"))
        self.assertTrue(any(pointer.startswith("/workflow_json/steps/0") for pointer in finding["related_pointers"]))

    def test_explicit_initial_lid_state_satisfies_precondition(self) -> None:
        initial_state = {"containers": [{
            "container_type": "进样瓶", "container_id": 1, "lid_state": "有盖", "volume_ml": 0
        }]}
        report = self.check(
            self.package(self.container_step(1, opening=False)), initial_state=initial_state
        )
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"], report)

    def test_explicit_null_workflow_is_not_treated_as_a_wire_document(self) -> None:
        report = self.check({"status": "success", "workflow_json": None})
        self.assert_failed(report)
        self.assertTrue(any(
            item["stage"] == "input" and item["json_pointer"] == "/workflow_json"
            for item in report["findings"]
        ), report)

    def test_duplicate_json_fields_cannot_hide_the_first_value(self) -> None:
        path = self.artifacts / "duplicate.json"
        path.write_text('{"workflow_json":{"steps":[],"steps":[{}]}}', encoding="utf-8")
        report = check_dispatch_file(path, workstation_root=self.catalog)
        self.assert_failed(report)
        self.finding(report, "duplicate_json_key")

    def test_exponent_overflow_in_json_is_not_a_finite_number(self) -> None:
        package = self.package()
        document = json.dumps(package).replace('"\\u6e29\\u5ea6": 20', '"\\u6e29\\u5ea6": 1e999')
        self.assertIn("1e999", document)
        path = self.artifacts / "overflow.json"
        path.write_text(document, encoding="utf-8")
        report = check_dispatch_file(path, workstation_root=self.catalog)
        self.assert_failed(report)
        self.finding(report, "non_finite_number")

    def test_bare_wire_must_check_required_file_existence(self) -> None:
        step = self.file_package("missing-wire-input.csv")["workflow_json"]["steps"][0]
        payload = {"experiment_steps": {"steps": [step], "unknown_steps": None}, "plan_name": "fixture"}
        report = self.check(payload, artifact_root=self.artifacts)
        self.assert_failed(report)
        self.assertEqual(
            self.finding(report, "missing_input_file")["json_pointer"],
            "/experiment_steps/steps/0/parameters/输入文件",
        )

    def test_bare_wire_unknown_lid_precondition_cannot_pass(self) -> None:
        payload = {
            "experiment_steps": {"steps": [self.container_step(1, opening=False)], "unknown_steps": None},
            "plan_name": "fixture",
        }
        report = self.check(payload)
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        self.assertTrue(any(item["stage"] == "cross_step" for item in report["findings"]), report)

    def test_bare_wire_lid_conflict_retains_wire_source_pointers(self) -> None:
        payload = {
            "experiment_steps": {
                "steps": [self.container_step(1, opening=True), self.container_step(2, opening=False)],
                "unknown_steps": None,
            },
            "plan_name": "fixture",
        }
        report = self.check(payload)
        finding = self.finding(report, "lid_state_conflict")
        self.assert_failed(report)
        self.assertTrue(finding["json_pointer"].startswith("/experiment_steps/steps/1"))
        self.assertTrue(any(pointer.startswith("/experiment_steps/steps/0") for pointer in finding["related_pointers"]))

    def test_malformed_initial_state_is_a_located_input_error(self) -> None:
        states = [
            [],
            {"containers": {}},
            {"containers": [{"container_type": "进样瓶", "container_id": 1, "lid_state": {}}]},
            {"containers": [{"container_type": "进样瓶", "container_id": 1, "lid_state": []}]},
            {"containers": [{"container_type": "进样瓶", "container_id": 1, "volume_ml": float("nan")}]},
        ]
        for initial_state in states:
            with self.subTest(initial_state=initial_state):
                report = self.check(self.package(), initial_state=initial_state)
                self.assert_failed(report)
                finding = self.finding(report, "invalid_initial_state")
                self.assertTrue(finding["json_pointer"].startswith("/initial_state"))
                self.assertFalse(any(item["code"] == "checker_internal_error" for item in report["findings"]), report)

    def test_duplicate_station_code_cannot_silently_replace_a_contract(self) -> None:
        source = self.catalog / "references-Synthesis-Module" / "Fixture_Station" / "SKILL.md"
        duplicate = self.catalog / "references-Synthesis-Module" / "Fixture_Station_Copy"
        duplicate.mkdir()
        (duplicate / "SKILL.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        report = self.check(self.package())
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        self.assertTrue(any(item["stage"] == "contracts" for item in report["findings"]), report)

    def test_truncated_required_contract_row_cannot_be_silently_skipped(self) -> None:
        skill = self.catalog / "references-Synthesis-Module" / "Fixture_Station" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "| 必填开关 | desc | 是 |\n", encoding="utf-8"
        )
        report = self.check(self.package())
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        self.assertTrue(any(item["stage"] == "contracts" for item in report["findings"]), report)

    def test_required_sample_state_without_initial_sample_is_unverifiable(self) -> None:
        self.state_contract(inputs=("样品状态：必须为粉末状态",))
        report = self.check(self.package(self.container_step(1, opening=False)))
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        finding = self.finding(report, "unknown_initial_sample_state")
        self.assertEqual(finding["step_index"], 0)
        self.assertTrue(finding["json_pointer"].startswith("/workflow_json/steps/0"))

    def test_disjoint_sample_state_is_a_conflict(self) -> None:
        self.state_contract(inputs=("样品状态：纯液态",))
        report = self.check(
            self.package(self.container_step(1, opening=False)),
            initial_state=self.sample_initial("固体"),
        )
        self.assert_failed(report)
        finding = self.finding(report, "sample_state_conflict")
        self.assertEqual(finding["step_index"], 0)
        self.assertTrue(any(pointer.startswith("/initial_state/containers/0") for pointer in finding["related_pointers"]))

    def test_supported_sample_states_and_alternatives_satisfy_contract(self) -> None:
        cases = [
            ("必须无样品状态", "无样品"),
            ("粉末", "粉末"),
            ("固体", "固体"),
            ("固体", "粉末"),
            ("纯液态", "纯液态"),
            ("悬浊液", "悬浊液"),
            ("上层上清液和下层固体沉淀", "上层上清液和下层固体沉淀"),
            ("无溶液", "无样品"),
            ("无溶液", "粉末"),
            ("无溶液", "固体"),
            ("粉末或固体", "固体"),
        ]
        for required, actual in cases:
            with self.subTest(required=required, actual=actual):
                self.state_contract(inputs=(f"样品状态：{required}",))
                report = self.check(
                    self.package(self.container_step(1, opening=False)),
                    initial_state=self.sample_initial(actual),
                )
                self.assertEqual(report["status"], "passed", report)
                self.assertTrue(report["dispatchable"], report)

    def test_partly_compatible_output_sample_set_is_unverifiable(self) -> None:
        self.state_contract(opening=True, outputs=("样品状态：粉末或悬浊液",))
        self.state_contract(inputs=("样品状态：粉末",))
        report = self.check(self.package(
            self.container_step(1, opening=True), self.container_step(2, opening=False)
        ))
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        finding = self.finding(report, "ambiguous_sample_state")
        self.assertEqual(finding["step_index"], 1)
        self.assertTrue(any(pointer.startswith("/workflow_json/steps/0") for pointer in finding["related_pointers"]))
        # A generic solid may be powder, but that does not prove it is powder.
        report = self.check(
            self.package(self.container_step(1, opening=False)),
            initial_state=self.sample_initial("固体"),
        )
        self.assertEqual(report["status"], "not_verifiable", report)
        self.finding(report, "ambiguous_sample_state")

    def test_output_same_as_input_preserves_known_sample_state(self) -> None:
        self.state_contract(opening=True, outputs=("样品状态：与输入保持一致",))
        self.state_contract(inputs=("样品状态：粉末",))
        report = self.check(
            self.package(self.container_step(1, opening=True), self.container_step(2, opening=False)),
            initial_state=self.sample_initial("粉末"),
        )
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(report["dispatchable"], report)

    def test_unrestricted_output_does_not_reuse_old_sample_state(self) -> None:
        self.state_contract(opening=True, outputs=("样品状态：不限",))
        self.state_contract(inputs=("样品状态：粉末",))
        report = self.check(
            self.package(self.container_step(1, opening=True), self.container_step(2, opening=False)),
            initial_state=self.sample_initial("粉末"),
        )
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        finding = self.finding(report, "unknown_initial_sample_state")
        self.assertEqual(finding["step_index"], 1)

    def test_input_container_type_mismatch_is_located(self) -> None:
        self.state_contract(inputs=("容器类型：离心管",))
        report = self.check(self.package(self.container_step(1, opening=False)))
        self.assert_failed(report)
        finding = self.finding(report, "container_type_conflict")
        self.assertEqual(finding["step_index"], 0)
        self.assertTrue(finding["json_pointer"].startswith("/workflow_json/steps/0"))

    def test_changed_output_container_requires_an_explicit_mapping(self) -> None:
        self.state_contract(inputs=("容器类型：进样瓶",), outputs=("容器类型：离心管",))
        report = self.check(self.package(self.container_step(1, opening=False)))
        self.assertEqual(report["status"], "not_verifiable", report)
        self.assertFalse(report["dispatchable"], report)
        finding = self.finding(report, "container_transition_unverified")
        self.assertEqual(finding["step_index"], 0)

    def test_report_writer_cannot_overwrite_input_file(self) -> None:
        for source_name in ("workflow_check.json", "workflow_check.md"):
            with self.subTest(source_name=source_name):
                source = self.artifacts / source_name
                original = json.dumps(self.package(), ensure_ascii=False).encode("utf-8")
                source.write_bytes(original)
                report = check_dispatch_file(source, workstation_root=self.catalog)
                with self.assertRaises(ValueError):
                    write_check_report(report, self.artifacts)
                self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
