"""Tests: harness output must convert to the device platform's exact form.

Ground truth = 0410数据转换.txt (platform schema export) + SKILL.md 工作站编码 +
the generate.py envelope. The formatter must produce platform station names,
platform operation names, exact per-version parameter keys, platform-declared
types, station ids, and the dispatch envelope — while never modifying the
semantic workflow_json.
"""

from __future__ import annotations

import copy
import json
import unittest

import sys

sys.path.insert(0, "device_agent")

from dispatch_formatter import (  # noqa: E402
    DispatchCatalog,
    format_dispatch_payload,
)
from utils.workstation_loader import WorkstationLoader  # noqa: E402


def _catalog() -> DispatchCatalog:
    return DispatchCatalog.load(WorkstationLoader(use_new_format=True))


class DispatchCatalogTest(unittest.TestCase):
    def test_platform_schema_loaded(self) -> None:
        catalog = _catalog()
        self.assertGreaterEqual(len(catalog.stations), 30)
        self.assertIn("303物料站", catalog.stations)
        self.assertIn("移液平台1ml_V2", catalog.stations)

    def test_station_bridging_from_all_identifier_kinds(self) -> None:
        catalog = _catalog()
        # English code → platform (via 对照表 display bridge)
        self.assertEqual(
            catalog.resolve_station("Liquid_Handling_Station_1ml_V2"), "移液平台1ml_V2"
        )
        self.assertEqual(
            catalog.resolve_station("General_Material_Station_V1"), "303物料站"
        )
        self.assertEqual(catalog.resolve_station("Drying_Oven_V1"), "烘干机_V1")
        # legacy Chinese aliases → platform
        self.assertEqual(catalog.resolve_station("物料站"), "303物料站")
        self.assertEqual(catalog.resolve_station("液体进样站"), "移液平台1ml_V2")
        self.assertEqual(catalog.resolve_station("磁力搅拌工作站"), "十通道磁力搅拌_V1")
        # 对照表 display names (underscore variants) → platform
        self.assertEqual(catalog.resolve_station("移液平台_1ml_V2"), "移液平台1ml_V2")
        self.assertEqual(
            catalog.resolve_station("双工位电化学工作站_V2"), "双工位电化学_V2"
        )

    def test_operation_bridging(self) -> None:
        catalog = _catalog()
        self.assertEqual(
            catalog.resolve_operation("移液平台1ml_V2", "开盖"), "开盖-离心管"
        )
        self.assertEqual(
            catalog.resolve_operation("十通道磁力搅拌_V1", "磁力搅拌"), "开始搅拌"
        )
        self.assertEqual(
            catalog.resolve_operation("烘干机_V1", "静置烘干"), "烘干主流程"
        )
        self.assertEqual(
            catalog.resolve_operation("移液平台1ml_V2", "加液"), "加液_物料绑定"
        )

    def test_station_ids_attached_from_skill_codes(self) -> None:
        catalog = _catalog()
        self.assertEqual(catalog.station_ids.get("303物料站"), 1427568512205824)
        self.assertEqual(catalog.station_ids.get("移液平台1ml_V2"), 2002186824385539)


class DispatchFormattingTest(unittest.TestCase):
    WORKFLOW = {
        "steps": [
            {
                "step_number": 1,
                "workstation": "物料站",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]},
                "source_macro_step": 1,
                "macro_action_id": "MA_S01_R00",
            },
            {
                "step_number": 2,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "开盖",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "开盖编号": [1],
                    "保留瓶盖": "是",
                },
            },
            {
                "step_number": 3,
                "workstation": "Liquid_Handling_Station_1ml_V2",
                "operation": "加液",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器数量": 1,
                    "容器编号": [1],
                    "加样方案": [
                        {
                            "加样瓶号": 1,
                            "N号原液瓶": [
                                {"瓶号": 3, "配料名称": "无水乙醇", "原液用量": 1.0}
                            ],
                        }
                    ],
                },
            },
            {
                "step_number": 4,
                "workstation": "磁力搅拌工作站",
                "operation": "磁力搅拌",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "搅拌速度": "700",
                    "搅拌时间（分钟）": 120,
                },
            },
            {
                "step_number": 5,
                "workstation": "烘干机",
                "operation": "静置烘干",
                "parameters": {
                    "容器类型": "进样瓶",
                    "容器编号": [1],
                    "恒温温度（℃）": 80,
                    "烘干时间（分钟）": 720,
                },
            },
        ]
    }

    def setUp(self) -> None:
        self.catalog = _catalog()

    def test_full_workflow_maps_to_platform_form(self) -> None:
        source = copy.deepcopy(self.WORKFLOW)
        result = format_dispatch_payload(source, self.catalog, plan_name="测试")
        self.assertEqual(result["unmapped_steps"], 0)
        self.assertEqual(result["mapped_steps"], 5)
        # the semantic workflow_json is untouched
        self.assertEqual(source, self.WORKFLOW)

        steps = result["payload"]["experiment_steps"]["steps"]
        by_no = {s["step_number"]: s for s in steps}

        # platform station names + ids
        self.assertEqual(by_no[1]["workstation"], "303物料站")
        self.assertEqual(by_no[1]["id"], 1427568512205824)
        self.assertEqual(by_no[2]["workstation"], "移液平台1ml_V2")
        self.assertEqual(by_no[4]["workstation"], "十通道磁力搅拌_V1")
        self.assertEqual(by_no[5]["workstation"], "烘干机_V1")

        # platform operation names
        self.assertEqual(by_no[2]["operation"], "开盖-离心管")
        self.assertEqual(by_no[3]["operation"], "加液_物料绑定")
        self.assertEqual(by_no[4]["operation"], "开始搅拌")
        self.assertEqual(by_no[5]["operation"], "烘干主流程")

        # observation-hierarchy trace preserved (issue 6)
        self.assertEqual(by_no[1]["macro_action_id"], "MA_S01_R00")

    def test_platform_declared_types_are_enforced(self) -> None:
        result = format_dispatch_payload(
            copy.deepcopy(self.WORKFLOW), self.catalog
        )
        by_no = {
            s["step_number"]: s
            for s in result["payload"]["experiment_steps"]["steps"]
        }
        # 保留瓶盖: platform int → "是" becomes 1
        self.assertEqual(by_no[2]["parameters"]["保留瓶盖"], 1)
        # 搅拌速度: platform int → "700" becomes 700
        self.assertEqual(by_no[4]["parameters"]["搅拌速度"], 700)
        # 恒温温度: platform STRING → 80 becomes "80"
        self.assertEqual(by_no[5]["parameters"]["恒温温度"], "80")
        # unit-suffixed keys are canonicalized
        self.assertIn("搅拌时间", by_no[4]["parameters"])
        self.assertNotIn("搅拌时间（分钟）", by_no[4]["parameters"])

    def test_n_bottle_placeholder_is_instantiated(self) -> None:
        result = format_dispatch_payload(
            copy.deepcopy(self.WORKFLOW), self.catalog
        )
        by_no = {
            s["step_number"]: s
            for s in result["payload"]["experiment_steps"]["steps"]
        }
        scheme = by_no[3]["parameters"]["加样方案"][0]
        self.assertIn("3号原液瓶", scheme)
        self.assertNotIn("N号原液瓶", scheme)
        self.assertEqual(scheme["3号原液瓶"]["配料名称"], "无水乙醇")
        self.assertNotIn("瓶号", scheme["3号原液瓶"])

    def test_unknown_station_kept_with_warning(self) -> None:
        workflow = {
            "steps": [
                {
                    "step_number": 1,
                    "workstation": "量子传送站",
                    "operation": "传送",
                    "parameters": {},
                }
            ]
        }
        result = format_dispatch_payload(workflow, self.catalog)
        self.assertEqual(result["unmapped_steps"], 1)
        self.assertTrue(any("量子传送站" in w for w in result["warnings"]))
        # step is preserved (auditable), not silently dropped
        self.assertEqual(
            result["payload"]["experiment_steps"]["steps"][0]["workstation"],
            "量子传送站",
        )

    def test_platform_unknown_parameter_is_omitted_from_payload(self) -> None:
        workflow = {
            "steps": [
                {
                    "step_number": 1,
                    "workstation": "液体进样站",
                    "operation": "关盖",
                    "parameters": {
                        "容器类型": "进样瓶",
                        "容器编号": [1],
                        "关盖编号": [1],
                        "保留瓶盖": "是",  # 关盖 has no such platform field
                    },
                }
            ]
        }
        result = format_dispatch_payload(workflow, self.catalog)
        step = result["payload"]["experiment_steps"]["steps"][0]
        self.assertNotIn("保留瓶盖", step["parameters"])
        self.assertTrue(any("保留瓶盖" in w and "省略" in w for w in result["warnings"]))

    def test_envelope_matches_generate_py_contract(self) -> None:
        result = format_dispatch_payload(
            copy.deepcopy(self.WORKFLOW), self.catalog, plan_name="NiFe-PBA"
        )
        payload = result["payload"]
        self.assertIn("experiment_steps", payload)
        self.assertIn("steps", payload["experiment_steps"])
        self.assertIsNone(payload["experiment_steps"]["unknown_steps"])
        self.assertEqual(payload["plan_name"], "NiFe-PBA")


class GeneratePyAcceptanceTest(unittest.TestCase):
    def test_formatted_payload_passes_generate_py_validation(self) -> None:
        """The formatted payload must be accepted by the dispatch script's own
        strict validation (same truth source, zero adaptation needed)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gen",
            "chem_resources/lab-design-all/skills/workflow-generator/scripts/generate.py",
        )
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)

        result = format_dispatch_payload(
            copy.deepcopy(DispatchFormattingTest.WORKFLOW), _catalog()
        )
        steps = result["payload"]["experiment_steps"]["steps"]
        self.assertTrue(gen.validate_experiment_steps(steps))


if __name__ == "__main__":
    unittest.main()
