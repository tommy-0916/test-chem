from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_workflows import load_workstation_catalog
from scripts.llm_review_workflows import (
    _parse_json_text,
    combine_case_verdicts,
    review_case,
    review_case_workflows,
    validate_review,
)


class LLMReviewWorkflowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workstations = self.root / "workstations"
        station_dir = self.workstations / "references-Synthesis-Module" / "Test_Station_V1"
        station_dir.mkdir(parents=True)
        for module in (
            "references-Reaction-and-Testing-Module",
            "references-Characterization-Module",
        ):
            (self.workstations / module).mkdir(parents=True)
        (self.workstations / "SKILL.md").write_text(
            "# Root rule\nEvery step must contain the correct id.\n", encoding="utf-8"
        )
        (self.workstations / "工作站名称中英文对照.md").write_text(
            "英文名\t工作站名称\nTest_Station_V1\t测试站_V1\n", encoding="utf-8"
        )
        (station_dir / "SKILL.md").write_text(
            """---
## 工作站编码
工作站编码: 123
name: Test_Station_V1
---
## 操作 1. **处理**
### 参数设置
| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|---|---|---|---|---|---|---|
| 容器类型 | | 是 | | string | 进样瓶 | |
| 容器数量 | | 是 | | int | [1,10] | |
| 容器编号 | | 是 | | array | | |
""",
            encoding="utf-8",
        )
        audit_dir = self.workstations / "references_audit"
        audit_dir.mkdir()
        (audit_dir / "Test_Station_V1_audit.md").write_text(
            "# Audit\n处理前必须完成物料拿取。\n", encoding="utf-8"
        )
        self.stations, self.aliases = load_workstation_catalog(self.workstations)
        self.package = {
            "workflow_json": {
                "steps": [
                    {
                        "step_number": 1,
                        "id": 123,
                        "workstation": "测试站_V1",
                        "operation": "处理",
                        "source_macro_step": 1,
                        "parameters": {
                            "容器类型": "进样瓶",
                            "容器数量": 1,
                            "容器编号": [1],
                        },
                    }
                ]
            },
            "container_plan": [],
            "reagent_slot_plan": [],
            "dispatch_formatting": {
                "mapped_steps": 1,
                "unmapped_steps": 0,
                "warnings": [],
            },
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_json_fence_and_review_validation(self) -> None:
        parsed = _parse_json_text(
            "```json\n"
            + json.dumps(
                {
                    "case_id": "A01",
                    "review_status": "completed",
                    "verdict": "yes",
                    "checked_step_count": 1,
                    "findings": [],
                    "limitations": [],
                }
            )
            + "\n```"
        )
        validated = validate_review(parsed, case_id="A01", expected_steps=1)
        self.assertEqual(validated["verdict"], "yes")

    def test_independent_review_receives_skill_and_audit_rules(self) -> None:
        captured: dict[str, str] = {}

        def fake_call(**kwargs):
            captured["prompt"] = kwargs["prompt"]
            return json.dumps(
                {
                    "case_id": "A01",
                    "review_status": "completed",
                    "verdict": "no",
                    "checked_step_count": 1,
                    "findings": [
                        {
                            "severity": "error",
                            "code": "missing_predecessor",
                            "step_numbers": [1],
                            "workstation": "测试站_V1",
                            "operation": "处理",
                            "parameter_path": "",
                            "actual": "处理 is first",
                            "expected": "物料拿取 first",
                            "evidence_path": "Test_Station_V1_audit.md",
                            "evidence_quote": "处理前必须完成物料拿取。",
                            "explanation": "Required predecessor is absent.",
                        }
                    ],
                    "limitations": [],
                },
                ensure_ascii=False,
            )

        review = review_case(
            case_id="A01",
            package=self.package,
            deterministic_case={"case_id": "A01", "dispatch_schema_match": "yes"},
            workstations=self.workstations,
            stations=self.stations,
            aliases=self.aliases,
            keys=["fake"],
            key_offset=0,
            endpoint="https://example.invalid/v1",
            model="review-model",
            wire_api="codex_responses",
            reasoning_effort="xhigh",
            timeout=1,
            max_output_tokens=1000,
            attempts=1,
            call_fn=fake_call,
        )
        self.assertEqual(review["verdict"], "no")
        self.assertIn("Every step must contain the correct id", captured["prompt"])
        self.assertIn("处理前必须完成物料拿取", captured["prompt"])
        self.assertIn('"workflow_json"', captured["prompt"])

    def test_combined_verdict_is_an_and_gate(self) -> None:
        deterministic_no = {
            "case_id": "A01",
            "workflow_present": True,
            "dispatch_schema_match": "no",
            "error_count": 1,
        }
        llm_yes = {
            "case_id": "A01",
            "review_status": "completed",
            "verdict": "yes",
            "error_count": 0,
        }
        self.assertEqual(
            combine_case_verdicts(deterministic_no, llm_yes)["dispatch_schema_match"],
            "no",
        )
        deterministic_yes = dict(deterministic_no, dispatch_schema_match="yes", error_count=0)
        llm_no = dict(llm_yes, verdict="no", error_count=1)
        self.assertEqual(
            combine_case_verdicts(deterministic_yes, llm_no)["dispatch_schema_match"],
            "no",
        )
        self.assertEqual(
            combine_case_verdicts(deterministic_yes, llm_yes)["dispatch_schema_match"],
            "yes",
        )
        failed_llm = dict(llm_yes, review_status="failed", verdict="not_evaluable")
        failed = combine_case_verdicts(deterministic_yes, failed_llm)
        self.assertEqual(failed["dispatch_schema_match"], "not_evaluable")
        self.assertFalse(failed["evaluation_complete"])

    def test_blackbox_case_reviews_every_workflow(self) -> None:
        first = self.root / "iteration_01_device_package.json"
        second = self.root / "iteration_02_device_package.json"
        first.write_text(json.dumps(self.package, ensure_ascii=False), encoding="utf-8")
        second.write_text(json.dumps(self.package, ensure_ascii=False), encoding="utf-8")

        def fake_call(**kwargs):
            case_id = kwargs["prompt"].splitlines()[0].split(": ", 1)[1]
            return json.dumps(
                {
                    "case_id": case_id,
                    "review_status": "completed",
                    "verdict": "yes",
                    "checked_step_count": 1,
                    "findings": [],
                    "limitations": [],
                }
            )

        review = review_case_workflows(
            case_id="A01",
            deterministic_case={
                "case_id": "A01",
                "workflows": [
                    {"workflow_path": str(first)},
                    {"workflow_path": str(second)},
                ],
            },
            workstations=self.workstations,
            stations=self.stations,
            aliases=self.aliases,
            keys=["fake"],
            key_offset=0,
            endpoint="https://example.invalid/v1",
            model="review-model",
            wire_api="codex_responses",
            reasoning_effort="xhigh",
            timeout=1,
            max_output_tokens=1000,
            attempts=1,
            call_fn=fake_call,
        )
        self.assertEqual(review["verdict"], "yes")
        self.assertEqual(review["checked_workflow_count"], 2)
        self.assertEqual(review["checked_step_count"], 2)


if __name__ == "__main__":
    unittest.main()
