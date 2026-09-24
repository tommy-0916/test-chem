"""Issue 2 tests: unified user-readable extraction (human_readable_result.json).

The extractor must reorganize existing artifacts without mutating them,
preserve the original query verbatim, give every stage/macro step/device step
a stable id, keep device steps traceable to their macro step, surface Chinese
failure reasons, and explicitly mark agent-filled parameters.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.human_readable import (
    HUMAN_READABLE_FILENAME,
    build_human_readable_result,
    write_human_readable_result,
)

ORIGINAL_QUERY = "基于现有自动化化学工作站，合成 NiFe-PBA 并进行 XRD 表征，下发到303实验室"

RESEARCH_STATE = {
    "status": "completed",
    "event": {"query": ORIGINAL_QUERY},
    "current_stage": "合成 NiFe-PBA 并完成 XRD 观察",
    "stage_route": ["合成 NiFe-PBA 并完成 XRD 观察", "电化学活化"],
    "macro_action": {
        "macro_action_id": "MA_S01_R00",
        "observation_point": "XRD",
        "objective": "合成 NiFe-PBA 粉末",
        "completion_condition": "获得 XRD 有效结果",
    },
    "macro_plan": [
        {
            "步骤序号": 1,
            "操作": "配制前驱体",
            "试剂/对象": "Ni(NO3)2、K3Fe(CN)6",
            "参数": "各 2 mL",
            "来源": "10.1000/example (p.3)",
            "macro_action_id": "MA_S01_R00",
            "observation_point_id": "OP_XRD",
        },
        {
            "步骤序号": 2,
            "操作": "搅拌熟化",
            "试剂/对象": "混合液",
            "参数": "700 rpm 120 min",
            "来源": "agent补全(未直接引用文献)",
            "macro_action_id": "MA_S01_R00",
            "observation_point_id": "OP_XRD",
        },
    ],
    "knowledge_hits": [
        {"title": "NiFe PBA synthesis paper", "matched_terms": ["NiFe", "PBA"]},
    ],
    "seed_papers": [{"title": "Seed reference", "doi": "10.1000/seed"}],
}

SUCCESS_PACKAGE = {
    "status": "success",
    "requires_scientific_review": False,
    "workflow_txt": "1. 第1步 物料站：物料拿取",
    "workflow_json": {
        "steps": [
            {
                "step_number": 1,
                "workstation": "物料站",
                "operation": "物料拿取",
                "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
                "source_macro_step": 1,
                "macro_action_id": "MA_S01_R00",
                "observation_point_id": "OP_XRD",
            },
            {
                "step_number": 2,
                "workstation": "磁力搅拌工作站",
                "operation": "磁力搅拌",
                "parameters": {"搅拌速度": 700, "搅拌时间（分钟）": 120},
                "source_macro_step": 2,
            },
        ],
        "offline_handoffs": [{"name": "离线 XRD"}],
    },
    "dispatch_payload": {
        "experiment_steps": {
            "steps": [
                {
                    "step_number": 1,
                    "workstation": "303物料站",
                    "operation": "物料拿取",
                    "parameters": {"容器类型": "进样瓶", "容器编号": [1]},
                    "id": 1427568512205824,
                },
                {
                    "step_number": 2,
                    "workstation": "十通道磁力搅拌_V1",
                    "operation": "开始搅拌",
                    "parameters": {"搅拌速度": 700, "搅拌时间": 120},
                },
            ],
            "unknown_steps": None,
        },
        "plan_name": "test",
    },
    "dispatch_formatting": {"mapped_steps": 2, "unmapped_steps": 0, "warnings": []},
}

FEASIBILITY_PACKAGE = {
    "status": "feasibility_error",
    "error_package": {
        "type": "physical_infeasible",
        "blocking_constraints": ["设备真源没有球磨工作站"],
        "message": "需要 research 改写为常压路线",
    },
}


class HumanReadableExtractionTest(unittest.TestCase):
    def test_original_query_is_verbatim_and_state_untouched(self) -> None:
        state_copy = copy.deepcopy(RESEARCH_STATE)
        result = build_human_readable_result(state_copy, SUCCESS_PACKAGE)
        self.assertEqual(result["original_query"], ORIGINAL_QUERY)
        # extraction must not mutate the source state
        self.assertEqual(state_copy, RESEARCH_STATE)

    def test_stable_ids_and_macro_to_device_traceability(self) -> None:
        result = build_human_readable_result(RESEARCH_STATE, SUCCESS_PACKAGE)
        self.assertEqual([s["stage_id"] for s in result["stages"]], ["S01", "S02"])
        self.assertEqual([m["macro_step_id"] for m in result["macro_plan"]], ["M01", "M02"])
        device_ids = [d["device_step_id"] for d in result["device_plan"]]
        self.assertEqual(device_ids, ["D01", "D02"])
        # every device step traces back to its macro step
        self.assertEqual(result["device_plan"][0]["source_macro_step_id"], "M01")
        self.assertEqual(result["device_plan"][1]["source_macro_step_id"], "M02")

    def test_agent_fill_is_explicitly_marked(self) -> None:
        result = build_human_readable_result(RESEARCH_STATE, SUCCESS_PACKAGE)
        m1, m2 = result["macro_plan"]
        self.assertEqual(m1["evidence"]["source_type"], "paper_protocol")
        self.assertFalse(m1["evidence"]["requires_review"])
        self.assertEqual(m2["evidence"]["source_type"], "agent_generated")
        self.assertTrue(m2["evidence"]["requires_review"])

    def test_run_status_and_completed_plan(self) -> None:
        result = build_human_readable_result(RESEARCH_STATE, SUCCESS_PACKAGE)
        self.assertEqual(result["run_status"], "completed")
        self.assertIn("物料拿取", result["final_experiment_plan"])

    def test_dispatch_plan_is_distinct_platform_layer(self) -> None:
        """Issue 7: the strict platform-form dispatch layer is shown separately
        from the planning-format device_plan."""
        result = build_human_readable_result(RESEARCH_STATE, SUCCESS_PACKAGE)
        dispatch = result["dispatch_plan"]
        self.assertEqual(dispatch["mapped_steps"], 2)
        self.assertEqual(dispatch["unmapped_steps"], 0)
        first, second = dispatch["steps"]
        self.assertEqual(first["workstation"], "303物料站")
        self.assertEqual(first["workstation_id"], 1427568512205824)
        self.assertEqual(second["workstation"], "十通道磁力搅拌_V1")
        self.assertEqual(second["operation"], "开始搅拌")
        # planning layer keeps semantic names
        self.assertEqual(result["device_plan"][0]["workstation"], "物料站")

    def test_dispatch_plan_defaults_when_absent(self) -> None:
        result = build_human_readable_result(RESEARCH_STATE, FEASIBILITY_PACKAGE)
        self.assertEqual(result["dispatch_plan"]["steps"], [])
        self.assertEqual(result["dispatch_plan"]["mapped_steps"], 0)

    def test_feasibility_error_gives_chinese_reasons(self) -> None:
        result = build_human_readable_result(RESEARCH_STATE, FEASIBILITY_PACKAGE)
        self.assertEqual(result["run_status"], "feasibility_error")
        self.assertIn("设备真源没有球磨工作站", result["blocking_constraints"])

    def test_manual_required_without_device_package(self) -> None:
        state = copy.deepcopy(RESEARCH_STATE)
        state["status"] = "manual_required"
        state["manual_handoff"] = "静置老化无法适配，转人工"
        result = build_human_readable_result(state, None)
        self.assertEqual(result["run_status"], "manual_required")
        self.assertTrue(
            any("静置老化" in reason for reason in result["blocking_constraints"])
        )

    def test_papers_flag_used_in_plan(self) -> None:
        result = build_human_readable_result(RESEARCH_STATE, SUCCESS_PACKAGE)
        titles = {p["title"]: p for p in result["papers"]}
        self.assertIn("Seed reference", titles)
        self.assertIn("NiFe PBA synthesis paper", titles)

    def test_write_creates_named_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_human_readable_result(Path(tmp), RESEARCH_STATE, SUCCESS_PACKAGE)
            self.assertEqual(path.name, HUMAN_READABLE_FILENAME)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["original_query"], ORIGINAL_QUERY)


if __name__ == "__main__":
    unittest.main()
