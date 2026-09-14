"""Synthetic workflows checked against the shipped lab-design-all contracts.

These tests call only the local deterministic checker, never a model or device.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

try:
    from .dispatch_checker import check_dispatch, check_dispatch_file
except ImportError:
    from dispatch_checker import check_dispatch, check_dispatch_file


DEVICE_ROOT = Path(__file__).resolve().parent
CONTRACT_ROOT = DEVICE_ROOT.parent / "chem_resources/lab-design-all/skills/chemistry-experiment-workstation"
EXAMPLE_ROOT = DEVICE_ROOT / "examples/dispatch_check"
VOLUME_PATH = "/steps/2/parameters/加样方案/0/1号原液瓶/原液用量"


def workflow() -> dict:
    return json.loads((EXAMPLE_ROOT / "valid_workflow.json").read_text(encoding="utf-8"))


def source(step: dict) -> dict:
    return step["parameters"]["加样方案"][0]["1号原液瓶"]


class RealContractRegressionTests(unittest.TestCase):
    def check(self, value: dict) -> dict:
        return check_dispatch(value, workstation_root=CONTRACT_ROOT)

    def finding(self, result: dict, code: str) -> dict:
        matching = [item for item in result["findings"] if item["code"] == code]
        self.assertTrue(matching, f"Expected {code}: {result['findings']}")
        self.assertFalse(result["dispatchable"], result)
        return matching[0]

    def test_valid_three_step_real_contract_example_passes_unchanged(self):
        value = workflow()
        before = copy.deepcopy(value)
        result = self.check(value)
        self.assertEqual(result["status"], "passed", result["findings"])
        self.assertTrue(result["dispatchable"])
        self.assertEqual(result["summary"]["steps"], 3)
        self.assertEqual(value, before)
        self.assertTrue(value["example_metadata"]["synthetic"])

    def test_string_volume_example_points_to_exact_nested_parameter(self):
        result = check_dispatch_file(EXAMPLE_ROOT / "nested_parameter_error.json", workstation_root=CONTRACT_ROOT)
        item = self.finding(result, "type_mismatch")
        self.assertEqual(item["step_number"], 3)
        self.assertEqual(item["step_index"], 2)
        self.assertEqual(item["json_pointer"], VOLUME_PATH)
        self.assertEqual(item["actual"], "0.5")
        self.assertIn("Liquid_Handling_Station_1ml_V1", item["skill_path"])
        self.assertIsInstance(item["skill_line"], int)

    def test_string_target_does_not_skip_single_transfer_limit(self):
        value = workflow()
        source(value["steps"][2])["原液用量"] = 1.1
        item = self.finding(self.check(value), "single_transfer_volume")
        self.assertEqual(item["json_pointer"], VOLUME_PATH)
        self.assertEqual(item["expected"], 1.0)
        self.assertEqual(item["actual"], 1.1)

    def test_same_reagent_cumulative_volume_links_prior_steps(self):
        value = workflow()
        template = value["steps"][2]
        source(template)["原液用量"] = 1.0
        for number in (4, 5, 6):
            step = copy.deepcopy(template)
            step["step_number"] = number
            value["steps"].append(step)
        item = self.finding(self.check(value), "total_reagent_volume")
        self.assertEqual(item["step_number"], 6)
        self.assertEqual(item["json_pointer"], VOLUME_PATH.replace("/steps/2/", "/steps/5/"))
        self.assertEqual(item["expected"], 3.0)
        self.assertEqual(item["actual"], 4.0)
        self.assertIn(VOLUME_PATH, item["related_pointers"])

    def test_same_source_bottle_cannot_change_reagent_between_steps(self):
        value = workflow()
        step = copy.deepcopy(value["steps"][2])
        step["step_number"] = 4
        source(step)["配料名称"] = "乙醇"
        value["steps"].append(step)
        item = self.finding(self.check(value), "source_bottle_identity_conflict")
        self.assertEqual(item["step_number"], 4)
        self.assertEqual(item["json_pointer"], "/steps/3/parameters/加样方案/0/1号原液瓶")
        self.assertEqual(item["expected"], "水")
        self.assertEqual(item["actual"], "乙醇")
        self.assertIn(VOLUME_PATH, item["related_pointers"])

    def test_empty_opening_targets_do_not_open_the_declared_container(self):
        value = workflow()
        value["steps"][1]["parameters"]["开盖的瓶号"] = []
        item = self.finding(self.check(value), "container_target_mismatch")
        self.assertEqual(item["step_number"], 2)
        self.assertEqual(item["json_pointer"], "/steps/1/parameters/开盖的瓶号")
        self.assertEqual(item["expected"], [1])
        self.assertEqual(item["actual"], [])


if __name__ == "__main__":
    unittest.main()
