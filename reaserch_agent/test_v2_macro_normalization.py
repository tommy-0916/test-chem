from __future__ import annotations

import unittest

from reaserch_agent.workflow import ResearchAgent


class V2MacroNormalizationTest(unittest.TestCase):
    def test_nested_calculation_record_moves_without_changing_raw_answer(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._contract_version = "v2"
        derivation = {"rule": "mM_times_mL_to_mmol_v1", "volume_value": 80}
        raw = [{
            "步骤序号": 1,
            "操作": "dissolve",
            "试剂/对象": "compound A",
            "参数": "80 mL",
            "quantity_requirements": [{
                "source": "literature_calculation",
                "provenance": {"kind": "paper", "derivation": derivation},
            }],
        }]

        normalized = agent._normalize_macro_plan(raw)

        claim = normalized[0]["quantity_requirements"][0]
        self.assertEqual(claim["derivation"], derivation)
        self.assertNotIn("derivation", claim["provenance"])
        self.assertIn("derivation", raw[0]["quantity_requirements"][0]["provenance"])

    def test_upstream_whole_batch_can_enter_runtime_measured_relation(self):
        step = {
            "material_inputs": [{
                "material_id": "batch",
                "material_instance_id": "batch-1",
                "name": "existing batch",
                "material_origin": "upstream_output",
                "parent_output_refs": [
                    {"macro_step_id": "prior", "material_instance_id": "batch-1"}
                ],
                "quantity": {
                    "mode": "all_available",
                    "semantic": "whole_batch_unspecified",
                },
            }],
            "material_intermediates": [],
            "material_outputs": [],
            "material_relations": [{
                "input_material_instance_ids": ["batch-1"],
                "quantity_basis": "runtime_measurement_required",
            }],
            "operation_segments": [],
            "material_contract_status": {"material_relations": "declared"},
            "provenance": {"kind": "paper", "reference": "source"},
        }
        issues = ResearchAgent._v2_macro_step_quality_issues(1, step)
        self.assertFalse(any("material_inputs[1] 数量" in issue for issue in issues), issues)

        step["material_inputs"][0]["material_origin"] = "external_inventory"
        step["material_inputs"][0]["parent_output_refs"] = []
        issues = ResearchAgent._v2_macro_step_quality_issues(1, step)
        self.assertTrue(any("material_inputs[1] 数量" in issue for issue in issues))

    def test_upstream_whole_batch_can_be_observed_without_consumption(self):
        step = {
            "material_inputs": [{
                "material_id": "batch",
                "material_instance_id": "batch-1",
                "name": "existing batch",
                "material_origin": "upstream_output",
                "parent_output_refs": [
                    {"macro_step_id": "prior", "material_instance_id": "batch-1"}
                ],
                "quantity": {
                    "mode": "all_available",
                    "semantic": "whole_batch_unspecified",
                },
            }],
            "material_intermediates": [],
            "material_outputs": [],
            "material_relations": [],
            "operation_segments": [{
                "segment_id": "observe-1",
                "material_effect": "observe_without_material_change",
                "source_operation_ref": "observation",
                "provenance": {"kind": "user", "reference": "query"},
            }],
            "material_contract_status": {"material_relations": "not_applicable"},
            "provenance": {"kind": "user", "reference": "query"},
        }
        issues = ResearchAgent._v2_macro_step_quality_issues(1, step)
        self.assertFalse(any("material_inputs[1] 数量" in issue for issue in issues), issues)

        step["operation_segments"][0]["material_effect"] = "unknown"
        issues = ResearchAgent._v2_macro_step_quality_issues(1, step)
        self.assertTrue(any("material_inputs[1] 数量" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
