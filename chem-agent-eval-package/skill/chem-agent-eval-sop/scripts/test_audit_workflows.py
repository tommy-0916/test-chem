from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_workflows import audit_run


STATION_SKILL = """---
## 工作站编码
工作站编码: 123456
name: Test_Station_V1
---

## 操作 1. **开盖**
### 参数设置
| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|---|---|---|---|---|---|---|
| 容器类型 | | 是 | | string | 进样瓶 | |
| 容器数量 | | 是 | | int | 进样瓶最小1个最大10个 | |
| 容器编号 | | 是 | | array | | |
| 开盖编号 | | 是 | | array | | |
| -瓶号 | | 是 | | int | | |

## 操作 2. **烘干**
### 参数设置
| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|---|---|---|---|---|---|---|
| 容器类型 | | 是 | | string | 进样瓶 | |
| 容器数量 | | 是 | | int | 进样瓶最小1个最大10个 | |
| 容器编号 | | 是 | | array | | |
| 温度 | | 是 | ℃ | string | [20,200] | |

## 操作 3. **加液**
### 参数设置
| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|---|---|---|---|---|---|---|
| 容器类型 | | 是 | | string | 进样瓶 | |
| 容器数量 | | 是 | | int | 进样瓶最小1个最大10个 | |
| 容器编号 | | 是 | | array | | |
| 加样方案 | | 是 | | array | | |
| -加样瓶号 | | 是 | | int | | |
| -N号原液瓶 | N取值范围为1~6 | 是 | | object | | |
| --配料名称 | | 是 | | string | | |
| --原液用量 | | 是 | mL | float | (0,5] | |

## 操作 4. **文件任务**
### 参数设置
| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|---|---|---|---|---|---|---|
| 容器类型 | | 是 | | string | 进样瓶 | |
| 容器数量 | | 是 | | int | 进样瓶最小1个最大10个 | |
| 容器编号 | | 是 | | array | | |
| 上传文件 | | 是 | | file | | |
"""


class AuditWorkflowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.run = self.repo / "result" / "run"
        workstation_root = (
            self.repo
            / "chem_resources"
            / "lab-design-main"
            / "skills"
            / "chemistry-experiment-workstation"
        )
        station_dir = workstation_root / "references-Synthesis-Module" / "Test_Station_V1"
        station_dir.mkdir(parents=True)
        (station_dir / "SKILL.md").write_text(STATION_SKILL, encoding="utf-8")
        for module in (
            "references-Reaction-and-Testing-Module",
            "references-Characterization-Module",
        ):
            (workstation_root / module).mkdir(parents=True)
        (workstation_root / "工作站名称中英文对照.md").write_text(
            "英文名\t工作站名称\nTest_Station_V1\t测试站_V1\n", encoding="utf-8"
        )
        self.run.mkdir(parents=True)
        (self.run / "suite_manifest.json").write_text(
            json.dumps(
                {
                    "repo": str(self.repo),
                    "workstations_source": str(workstation_root),
                    "case_ids": ["A01"],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_package(self, package: dict) -> None:
        case_dir = self.run / "A01"
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "device_package.json").write_text(
            json.dumps(package, ensure_ascii=False), encoding="utf-8"
        )

    def valid_package(self) -> dict:
        input_file = self.run / "A01" / "input.xlsx"
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_bytes(b"test")
        steps = [
            {
                "step_number": 1,
                "id": 123456,
                "workstation": "测试站_V1",
                "operation": "开盖",
                "source_macro_step": 1,
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "开盖编号": [{"瓶号": 1}],
                },
            },
            {
                "step_number": 2,
                "id": 123456,
                "workstation": "Test_Station_V1",
                "operation": "烘干",
                "source_macro_step": 2,
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "温度": "80",
                },
            },
            {
                "step_number": 3,
                "id": 123456,
                "workstation": "Test_Station_V1",
                "operation": "加液",
                "source_macro_step": 3,
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "加样方案": [
                        {
                            "加样瓶号": 1,
                            "1号原液瓶": {"配料名称": "水", "原液用量": 1.0},
                        }
                    ],
                },
            },
            {
                "step_number": 4,
                "id": 123456,
                "workstation": "Test_Station_V1",
                "operation": "文件任务",
                "source_macro_step": 4,
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "上传文件": "input.xlsx",
                },
            },
        ]
        return {
            "status": "success",
            "workflow_json": {"steps": steps},
            "dispatch_formatting": {"mapped_steps": 4, "unmapped_steps": 0, "warnings": []},
        }

    def test_valid_workflow_passes(self) -> None:
        self.write_package(self.valid_package())
        result = audit_run(run_dir=self.run, expected_workstation_count=None)
        case = result["cases"][0]
        self.assertEqual(case["dispatch_schema_match"], "yes")
        self.assertEqual(case["errors"], [])

    def test_fine_grained_failures_are_detected(self) -> None:
        package = self.valid_package()
        steps = package["workflow_json"]["steps"]
        steps[0].pop("id")
        steps[0]["parameters"]["开盖编号"] = [1]
        steps[1]["parameters"]["温度"] = 80
        steps[2]["parameters"]["加样方案"][0] = {
            "加样瓶号": 1,
            "N号原液瓶": [{"瓶号": 1, "配料名称": "水", "原液用量": 1.0}],
        }
        steps[3]["parameters"]["上传文件"] = "missing.xlsx"
        steps.append(
            {
                "step_number": 5,
                "id": 123456,
                "workstation": "Test_Station_V1",
                "operation": "不存在的操作",
                "source_macro_step": 5,
                "parameters": {},
            }
        )
        package["dispatch_formatting"] = {
            "mapped_steps": 4,
            "unmapped_steps": 1,
            "warnings": ["第 2 步：参数 `温度` 已从下发 payload 省略"],
        }
        self.write_package(package)
        result = audit_run(run_dir=self.run, expected_workstation_count=None)
        case = result["cases"][0]
        codes = set(case["counts_by_code"])
        self.assertEqual(case["dispatch_schema_match"], "no")
        self.assertTrue(
            {
                "workstation_id_mismatch",
                "nested_type_mismatch",
                "type_mismatch",
                "missing_input_file",
                "unknown_operation",
                "dispatch_unmapped_steps",
                "dispatch_parameter_dropped",
            }.issubset(codes)
        )

    def test_missing_workflow_is_not_evaluable(self) -> None:
        self.write_package({"status": "feasibility_error"})
        result = audit_run(run_dir=self.run, expected_workstation_count=None)
        self.assertEqual(result["cases"][0]["dispatch_schema_match"], "not_evaluable")

    def test_all_blackbox_workflows_are_aggregated(self) -> None:
        case_dir = self.run / "A01"
        first = case_dir / "blackbox" / "campaign" / "iteration_01"
        second = case_dir / "blackbox" / "campaign" / "iteration_02"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / "device_package.json").write_text(
            json.dumps(self.valid_package(), ensure_ascii=False), encoding="utf-8"
        )
        invalid = self.valid_package()
        invalid["workflow_json"]["steps"][0].pop("id")
        (second / "device_package.json").write_text(
            json.dumps(invalid, ensure_ascii=False), encoding="utf-8"
        )
        result = audit_run(run_dir=self.run, expected_workstation_count=None)
        case = result["cases"][0]
        self.assertEqual(case["checked_workflows"], 2)
        self.assertEqual(case["dispatch_schema_match"], "no")
        self.assertIn("workstation_id_mismatch", case["counts_by_code"])

    def test_failed_intermediate_workflow_remains_in_final_verdict(self) -> None:
        case_dir = self.run / "A01"
        first = case_dir / "blackbox" / "campaign" / "iteration_01"
        second = case_dir / "blackbox" / "campaign" / "iteration_02"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        failed = self.valid_package()
        failed["status"] = "failed"
        failed.pop("dispatch_formatting")
        (first / "device_package.json").write_text(
            json.dumps(failed, ensure_ascii=False), encoding="utf-8"
        )
        (second / "device_package.json").write_text(
            json.dumps(self.valid_package(), ensure_ascii=False), encoding="utf-8"
        )

        result = audit_run(run_dir=self.run, expected_workstation_count=None)
        case = result["cases"][0]
        self.assertEqual(case["checked_workflows"], 2)
        self.assertEqual(case["dispatch_schema_match"], "no")
        self.assertIn("missing_dispatch_formatting", case["counts_by_code"])


if __name__ == "__main__":
    unittest.main()
