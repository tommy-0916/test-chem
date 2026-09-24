from __future__ import annotations

import unittest

from reaserch_agent.prompts.task_prompts import (
    MACRO_STEP_CONTRACT_PROMPT,
    V2_MACRO_PLAN_DESIGN_PROMPT,
)


class V2MacroPromptContractTest(unittest.TestCase):
    def test_macro_step_prompt_explains_allocation_and_flat_quantity_shape(self):
        prompt = MACRO_STEP_CONTRACT_PROMPT.format(
            macro_action_json="{}",
            device_context_json="{}",
        )

        self.assertIn("input_allocations 必须逐一覆盖所有输入端点", prompt)
        self.assertIn("output_allocations 必须", prompt)
        self.assertIn('"material_instance_id"', prompt)
        self.assertIn('"mode":"exact"', prompt)
        self.assertIn("不能把多输入或多输出关系写成 whole_batch", prompt)
        self.assertIn("value/unit 是每项的顶层字段", prompt)
        self.assertIn("不得把浓度×体积的计算结果标成文献原文直给数值", prompt)
        self.assertIn("洗涤水等新加入的外部物料同样属于主动投料", prompt)

    def test_v2_design_prompt_requires_complete_port_and_requirement_quantities(self):
        prompt = V2_MACRO_PLAN_DESIGN_PROMPT.format(
            query="example",
            survey_report_json="{}",
            extracted_protocols_json="{}",
            stage_route_json="[]",
            current_stage="example",
            stage_route_reason="example",
            current_stage_reason="example",
        )

        self.assertIn("顶层 kind、material_id、material、value、unit、source", prompt)
        self.assertIn("material port 的 quantity 必须显式给出", prompt)
        self.assertIn("不得写 null", prompt)
        self.assertIn("planned_target、planning_estimate", prompt)
        self.assertIn("绝不能填入 kind", prompt)
        self.assertIn("derivation 若需要，必须放在该需求对象顶层", prompt)


if __name__ == "__main__":
    unittest.main()
