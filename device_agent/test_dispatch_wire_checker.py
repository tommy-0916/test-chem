"""Offline regressions for wire contract checks and source/payload fidelity."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

try:
    from .dispatch_wire_checker import check_wire_payload
except ImportError:
    from dispatch_wire_checker import check_wire_payload


REAL_ROOT = Path(__file__).resolve().parents[1] / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation"


def material(step: int = 1) -> dict:
    return {"step_number": step, "workstation": "General_Material_Station_V1", "operation": "物料拿取",
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]}}


def addition() -> dict:
    return {"step_number": 2, "workstation": "Liquid_Handling_Station_1ml_V2", "operation": "加液_物料绑定",
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1],
                           "加样方案": [{"加样瓶号": 1, "N号原液瓶": [{"瓶号": 1, "配料名称": "水", "原液用量": 0.5}]}]}}


class WireCheckerTests(unittest.TestCase):
    def check(self, workflow: dict, payload=None, root: Path = REAL_ROOT) -> dict:
        return check_wire_payload(workflow, payload, workstation_root=root)

    def codes(self, result: dict) -> set:
        return {item["code"] for item in result["findings"]}

    def test_real_material_preview_and_payload_pass_without_mutation(self):
        workflow = {"steps": [material()]}
        frozen = copy.deepcopy(workflow)
        preview = self.check(workflow)
        self.assertEqual(preview["findings"], [])
        self.assertEqual(preview["source"], "preview")
        payload = preview["expected_payload"]
        original_payload = copy.deepcopy(payload)
        actual = self.check(workflow, payload)
        self.assertEqual(actual["findings"], [])
        self.assertEqual(actual["source"], "supplied")
        self.assertEqual(workflow, frozen)
        self.assertEqual(payload, original_payload)

    def test_actual_id_is_checked_and_located(self):
        workflow = {"steps": [material()]}
        payload = self.check(workflow)["expected_payload"]
        payload["experiment_steps"]["steps"][0]["id"] = 101
        result = self.check(workflow, payload)
        item = next(item for item in result["findings"] if item["code"] == "dispatch_station_id_mismatch")
        self.assertEqual(item["step_index"], 0)
        self.assertEqual(item["step_number"], 1)
        self.assertEqual(item["json_pointer"], "/dispatch_payload/experiment_steps/steps/0/id")
        self.assertIn("SKILL.md", item["skill_path"])

    def test_workflow_valid_but_payload_tampered(self):
        workflow = {"steps": [material()]}
        payload = self.check(workflow)["expected_payload"]
        payload["experiment_steps"]["steps"][0]["parameters"]["容器编号"] = [2]
        result = self.check(workflow, payload)
        item = next(item for item in result["findings"] if item["code"] == "dispatch_value_mismatch")
        self.assertEqual(item["json_pointer"], "/dispatch_payload/experiment_steps/steps/0/parameters/容器编号/0")
        self.assertEqual(item["expected"], 1)
        self.assertEqual(item["actual"], 2)

    def test_exact_platform_operation_not_formatter_guess(self):
        workflow = {"steps": [material()]}
        workflow["steps"][0]["operation"] = "totally unknown"
        result = self.check(workflow)
        self.assertIn("dispatch_operation_unverified", self.codes(result))

    def test_no_truth_source_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.check({"steps": [material()]}, root=Path(directory))
        self.assertEqual(self.codes(result), {"dispatch_contract_unavailable"})
        self.assertEqual(result["findings"][0]["severity"], "unverified")

    def test_missing_wire_step_is_not_accepted(self):
        workflow = {"steps": [material(), material(2)]}
        payload = self.check(workflow)["expected_payload"]
        payload["experiment_steps"]["steps"].pop()
        self.assertIn("dispatch_step_count_mismatch", self.codes(self.check(workflow, payload)))

    def test_swapped_steps_are_detected(self):
        workflow = {"steps": [material(), material(2)]}
        payload = self.check(workflow)["expected_payload"]
        payload["experiment_steps"]["steps"].reverse()
        self.assertIn("dispatch_value_mismatch", self.codes(self.check(workflow, payload)))

    def test_dropped_parameter_reported_before_formatting(self):
        workflow = {"steps": [material()]}
        workflow["steps"][0]["parameters"]["invented"] = 5
        result = self.check(workflow)
        self.assertIn("dispatch_parameter_dropped", self.codes(result))
        finding = next(item for item in result["findings"] if item["code"] == "dispatch_parameter_dropped")
        self.assertEqual(finding["json_pointer"], "/workflow_json/steps/0/parameters/invented")

    def test_parameter_alias_collision_detected(self):
        step = material()
        step["parameters"]["容器数量（个）"] = 1
        self.assertIn("dispatch_parameter_collision", self.codes(self.check({"steps": [step]})))

    def test_dynamic_bottle_conversion_valid(self):
        result = self.check({"steps": [addition()]})
        self.assertEqual(result["findings"], [])
        plan = result["expected_payload"]["experiment_steps"]["steps"][0]["parameters"]["加样方案"][0]
        self.assertEqual(plan["1号原液瓶"]["原液用量"], 0.5)
        self.assertNotIn("N号原液瓶", plan)

    def test_duplicate_bottle_conversion_must_not_overwrite_silently(self):
        step = addition()
        sources = step["parameters"]["加样方案"][0]["N号原液瓶"]
        sources.append({"瓶号": 1, "配料名称": "乙醇", "原液用量": 0.3})
        self.assertIn("dispatch_parameter_collision", self.codes(self.check({"steps": [step]})))

    def test_placeholder_and_explicit_key_collision(self):
        step = addition()
        step["parameters"]["加样方案"][0]["1号原液瓶"] = {"配料名称": "乙醇", "原液用量": 0.3}
        self.assertIn("dispatch_parameter_collision", self.codes(self.check({"steps": [step]})))

    def test_nested_type_and_required_fields(self):
        workflow = {"steps": [addition()]}
        payload = self.check(workflow)["expected_payload"]
        nested = payload["experiment_steps"]["steps"][0]["parameters"]["加样方案"][0]["1号原液瓶"]
        nested["原液用量"] = "0.5"
        del nested["配料名称"]
        result = self.check(workflow, payload)
        self.assertIn("dispatch_type_mismatch", self.codes(result))
        self.assertIn("dispatch_required_parameter_missing", self.codes(result))

    def test_wire_enum_and_range_are_checked(self):
        workflow = {"steps": [material()]}
        payload = self.check(workflow)["expected_payload"]
        params = payload["experiment_steps"]["steps"][0]["parameters"]
        params["容器数量"] = 999
        self.assertIn("dispatch_platform_range_mismatch", self.codes(self.check(workflow, payload)))
        params["容器类型"] = "unknown"
        self.assertIn("dispatch_platform_enum_mismatch", self.codes(self.check(workflow, payload)))

    def test_self_reported_pass_cannot_override_invalid_payload(self):
        workflow = {"steps": [material()]}
        payload = self.check(workflow)["expected_payload"]
        payload["validation"] = {"status": "passed"}
        payload["experiment_steps"]["steps"][0]["parameters"]["容器数量"] = True
        result = self.check(workflow, payload)
        self.assertIn("dispatch_type_mismatch", self.codes(result))
        self.assertIn("dispatch_unknown_envelope_field", self.codes(result))

    def test_duplicate_step_number_and_custom_pointer(self):
        workflow = {"steps": [material(), material()]}
        result = check_wire_payload(workflow, None, workstation_root=REAL_ROOT,
                                    workflow_pointer="", payload_pointer="/preview")
        finding = next(item for item in result["findings"] if item["code"] == "dispatch_duplicate_step_number")
        self.assertEqual(finding["step_index"], 1)
        self.assertEqual(finding["json_pointer"], "/preview/experiment_steps/steps/1/step_number")

    def test_bad_envelope_does_not_crash(self):
        for payload in ([], 1, {"plan_name": 8}, {"experiment_steps": {"steps": {}}}):
            result = self.check({"steps": [material()]}, payload)
            self.assertIn("dispatch_envelope_invalid", self.codes(result))

    def test_new_skill_and_export_enum_conflict_is_unverified(self):
        step = addition()
        step["parameters"]["容器类型"] = "50ml耐热瓶"
        result = self.check({"steps": [step]})
        conflict = next(item for item in result["findings"] if item["code"] == "dispatch_contract_conflict")
        self.assertEqual(conflict["severity"], "unverified")

    def test_non_scalar_container_type_does_not_crash(self):
        step = material()
        step["parameters"]["容器类型"] = ["进样瓶"]
        result = self.check({"steps": [step]})
        self.assertIn("dispatch_type_mismatch", self.codes(result))

    def test_standalone_wire_checks_contract_without_source(self):
        payload = self.check({"steps": [material()]})["expected_payload"]
        result = self.check(None, payload)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["source_correspondence"], "not_available")
        self.assertTrue(result["semantic_projection_complete"])
        self.assertEqual(result["semantic_workflow"]["steps"][0]["workstation"], "General_Material_Station_V1")
        payload["experiment_steps"]["steps"][0]["id"] = 1
        self.assertIn("dispatch_station_id_mismatch", self.codes(self.check(None, payload)))


if __name__ == "__main__":
    unittest.main()
