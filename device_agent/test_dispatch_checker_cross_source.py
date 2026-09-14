"""Cross-source volume regressions using real local contracts, never devices."""

from __future__ import annotations

import copy
from fractions import Fraction
import unittest

from device_agent.dispatch_checker import check_dispatch, render_check_report
from device_agent.dispatch_wire_checker import check_wire_payload
from device_agent.test_dispatch_checker_real_contract import CONTRACT_ROOT, workflow


IDENTITY_CODE = "cross_source_reagent_identity_unverified"


def addition_workflow(entries, *, target=1, kind="进样瓶"):
    """entries are (source bottle number, declared label, volume in mL)."""
    value = workflow()
    template = copy.deepcopy(value["steps"][2])
    value["steps"] = value["steps"][:2]
    for step in value["steps"]:
        step["parameters"].update(容器类型=kind, 容器编号=[target])
    value["steps"][1]["parameters"]["开盖的瓶号"] = [{"瓶号": str(target)}]
    for number, (bottle, name, volume) in enumerate(entries, 3):
        step = copy.deepcopy(template)
        step["step_number"] = number
        step["parameters"].update(容器类型=kind, 容器编号=[target])
        step["parameters"]["加样方案"] = [{
            "加样瓶号": str(target),
            f"{bottle}号原液瓶": {"配料名称": name, "原液用量": volume},
        }]
        value["steps"].append(step)
    return value


def package(value):
    wire = check_wire_payload(value, None, workstation_root=CONTRACT_ROOT)
    return {"workflow_json": value, "dispatch_payload": wire["expected_payload"]}


class CrossSourceVolumeTests(unittest.TestCase):
    def check(self, value, *, require_payload=True, **kwargs):
        original = copy.deepcopy(value)
        report = check_dispatch(value, workstation_root=CONTRACT_ROOT, require_payload=require_payload, **kwargs)
        self.assertEqual(value, original, "Checking must not normalize the original data")
        self.assertFalse(any(f["code"].endswith("internal_error") for f in report["findings"]), report)
        return report

    @staticmethod
    def cross_source_entries():
        return [(1, "水", 1.0), (1, "水", 1.0), (2, "水", 1.0), (2, "水", 1.0)]

    def assert_identity_block(self, report, prefix="/workflow_json"):
        self.assertEqual(report["status"], "not_verifiable", report["findings"])
        self.assertFalse(report["dispatchable"])
        finding, = [f for f in report["findings"] if f["code"] == IDENTITY_CODE]
        self.assertEqual(finding["severity"], "unverified")
        self.assertEqual((finding["step_number"], finding["step_index"]), (6, 5))
        tail = "/parameters/加样方案/0/"
        self.assertEqual(finding["json_pointer"], prefix + "/steps/5" + tail + "2号原液瓶/原液用量")
        self.assertEqual(finding["related_pointers"], [
            prefix + f"/steps/{index}" + tail + f"{bottle}号原液瓶/原液用量"
            for index, bottle in ((2, 1), (3, 1), (4, 2))
        ])
        self.assertEqual(finding["expected"], 3.0)
        self.assertEqual(finding["actual"], 4.0)
        self.assertEqual(finding["source_bottles"], ["1号原液瓶", "2号原液瓶"])
        self.assertEqual(finding["identity_basis"], "matching_labels_only")
        self.assertGreater(finding["skill_line"], 0)

    def test_full_package_cross_source_overflow_is_not_silently_passed(self):
        self.assert_identity_block(self.check(package(addition_workflow(self.cross_source_entries()))))

    def test_bare_workflow_preview_also_blocks(self):
        report = self.check(addition_workflow(self.cross_source_entries()), require_payload=False)
        self.assert_identity_block(report, prefix="")
        self.assertEqual(report["payload_source"], "generated_preview")

    def test_bare_wire_reports_original_wire_pointers(self):
        report = self.check(package(addition_workflow(self.cross_source_entries()))["dispatch_payload"])
        self.assert_identity_block(report, prefix="/experiment_steps")

    def test_terminal_package_preserves_envelope_pointer(self):
        report = self.check({"terminal_package": package(addition_workflow(self.cross_source_entries()))})
        self.assert_identity_block(report, prefix="/terminal_package/workflow_json")

    def test_three_ml_upper_bound_across_sources_passes(self):
        report = self.check(package(addition_workflow(self.cross_source_entries()[:3])))
        self.assertEqual(report["status"], "passed", report["findings"])

    def test_decimal_boundary_does_not_accumulate_float_drift(self):
        for bottles in ([1] * 30, [1] * 15 + [2] * 15):
            with self.subTest(bottles=bottles):
                report = self.check(package(addition_workflow([(b, "水", 0.1) for b in bottles])))
                self.assertEqual(report["status"], "passed", report["findings"])

    def test_even_tiny_positive_excess_is_not_rounded_away(self):
        entries = self.cross_source_entries()[:3] + [(2, "水", 1e-100)]
        report = self.check(package(addition_workflow(entries)))
        self.assertEqual(report["status"], "not_verifiable", report["findings"])
        finding, = [f for f in report["findings"] if f["code"] == IDENTITY_CODE]
        self.assertEqual(Fraction(finding["actual_ml_fraction"]), Fraction(3) + Fraction("1e-100"))
        markdown = render_check_report(report)
        self.assertIn("精确累计量（mL，分数表示）", markdown)
        self.assertIn(finding["actual_ml_fraction"], markdown)
        self.assertIn("涉及原液瓶", markdown)
        self.assertIn("同名标签（非身份确认）", markdown)

    def test_decimal_volume_above_boundary_is_still_blocked(self):
        for bottles, status, code in (
            ([1] * 31, "failed", "total_reagent_volume"),
            ([1] * 15 + [2] * 16, "not_verifiable", IDENTITY_CODE),
        ):
            with self.subTest(status=status):
                report = self.check(package(addition_workflow([(b, "水", 0.1) for b in bottles])))
                self.assertEqual(report["status"], status, report["findings"])
                finding, = [f for f in report["findings"] if f["code"] == code]
                self.assertEqual(finding["actual"], 3.1)

    def test_same_source_confirmed_overflow_remains_an_error(self):
        report = self.check(package(addition_workflow([(1, "水", 1.0)] * 4)))
        self.assertEqual(report["status"], "failed", report["findings"])
        self.assertIn("total_reagent_volume", [f["code"] for f in report["findings"]])
        self.assertNotIn(IDENTITY_CODE, [f["code"] for f in report["findings"]])

    def test_confirmed_overflow_is_not_replaced_by_identity_uncertainty(self):
        entries = [(1, "水", 1.0)] * 4 + [(2, "水", 1.0)]
        report = self.check(package(addition_workflow(entries)))
        self.assertEqual(report["status"], "failed", report["findings"])
        self.assertIn("total_reagent_volume", [f["code"] for f in report["findings"]])
        self.assertNotIn(IDENTITY_CODE, [f["code"] for f in report["findings"]])

    def test_negative_volume_cannot_reduce_the_positive_total(self):
        entries = [(1, "水", -1.0)] + self.cross_source_entries()
        report = self.check(package(addition_workflow(entries)))
        codes = [f["code"] for f in report["findings"]]
        self.assertEqual(report["status"], "failed", report["findings"])
        self.assertIn("negative_liquid_volume", codes)
        self.assertIn(IDENTITY_CODE, codes)

    def test_whitespace_cannot_split_the_candidate_name_group(self):
        entries = [(b, name if b == 1 else " 水 ", v) for b, name, v in self.cross_source_entries()]
        self.assert_identity_block(self.check(package(addition_workflow(entries))))

    def test_same_source_name_whitespace_does_not_invent_an_identity_change(self):
        value = package(addition_workflow([(1, "水", 1.0), (1, " 水 ", 1.0)]))
        report = self.check(value)
        self.assertEqual(report["status"], "passed", report["findings"])

    def test_leading_zero_source_number_does_not_reset_same_source_total(self):
        entries = [(1, "水", 1.0)] * 2 + [("01", "水", 1.0)] * 2
        report = self.check(package(addition_workflow(entries)))
        self.assertEqual(report["status"], "failed", report["findings"])
        finding, = [f for f in report["findings"] if f["code"] == "total_reagent_volume"]
        self.assertTrue(finding["json_pointer"].endswith("/01号原液瓶/原液用量"))
        self.assertEqual(finding["actual"], 4.0)
        self.assertNotIn(IDENTITY_CODE, [f["code"] for f in report["findings"]])

    def test_multiple_sources_in_one_addition_plan_are_all_counted(self):
        value = addition_workflow([(1, "水", 1.0)] * 2)
        for step in value["steps"][2:]:
            step["parameters"]["加样方案"][0]["2号原液瓶"] = {"配料名称": "水", "原液用量": 1.0}
        report = self.check(package(value))
        self.assertEqual(report["status"], "not_verifiable", report["findings"])
        finding, = [f for f in report["findings"] if f["code"] == IDENTITY_CODE]
        self.assertEqual(finding["step_number"], 4)
        self.assertEqual(finding["actual"], 4.0)
        self.assertEqual(len(finding["related_pointers"]), 3)

    def test_workstation_alias_does_not_reset_candidate_total(self):
        value = addition_workflow(self.cross_source_entries())
        for step in value["steps"][4:]:
            step["workstation"] = "移液平台_1ml_V1"
        self.assert_identity_block(self.check(package(value)))

    def test_distinct_declared_names_are_not_claimed_to_be_one_solution(self):
        entries = [(b, name if b == 1 else "乙醇", v) for b, name, v in self.cross_source_entries()]
        report = self.check(package(addition_workflow(entries)))
        self.assertEqual(report["status"], "passed", report["findings"])
        self.assertTrue(any("别名" in item for item in report["limitations"]))

    def test_different_target_numbers_have_separate_totals(self):
        entries = [(1, "水", 1.0), (2, "水", 1.0)]
        value = addition_workflow(entries)
        value["steps"].extend(addition_workflow(entries, target=2)["steps"])
        for number, step in enumerate(value["steps"], 1):
            step["step_number"] = number
        report = self.check(package(value))
        self.assertEqual(report["status"], "passed", report["findings"])

    def test_different_container_types_have_separate_totals(self):
        for entries in ([(1, "水", 1.0)] * 3, self.cross_source_entries()[:3]):
            with self.subTest(entries=entries):
                value = addition_workflow(entries)
                # The material station cannot acquire a 50 mL bottle. Supply its
                # known initial state instead of inventing an acquisition capability.
                value["steps"].extend(addition_workflow(entries, kind="50ml耐热瓶")["steps"][1:])
                for number, step in enumerate(value["steps"], 1):
                    step["step_number"] = number
                report = self.check(package(value), initial_state={"containers": [{
                    "container_type": "50ml耐热瓶", "container_id": 1,
                    "lid_state": "有盖", "volume_ml": 0, "sample_state": "无样品",
                }]})
                self.assertEqual(report["status"], "passed", report["findings"])


if __name__ == "__main__":
    unittest.main()
