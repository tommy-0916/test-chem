"""Tests for the deterministic capability audit (issues #9 and #11).

Fixtures are VERBATIM offline_handoff fragments from the archived eval runs
(result/chem-agent-eval-20260718-*), so the audit is proven against the real
failure distribution, not synthetic examples.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "device_agent")

from capability_audit import (  # noqa: E402
    audit_offline_handoffs,
    scan_manual_material_operations,
    weighing_false_hard_guard,
)

# --- verbatim archived handoffs that MUST be flagged (issue #9) -------------

HANDOFF_C02_A1 = {
    "name": "干燥固体10.0 mg分样与余样密封",
    "instructions": [
        "保持原瓶1-18与XRD瓶101-118一一对应",
        "每份称取10.0 mg至对应XRD进样瓶",
        "XRD瓶保持无盖并返回自动化线",
    ],
}
HANDOFF_C02_A2 = {
    "name": "干燥固体定量分样与剩余样品密封保存",
    "procedure": (
        "每份人工称取10.0 mg至对应有盖进样瓶101-118；"
        "其余固体分别转入并密封于进样瓶201-218。"
    ),
    "requires_review": True,
}
HANDOFF_A01 = {
    "name": "XRD样品离线定量装载",
    "required_action": (
        "从瓶1-10对应干燥样品各称取5.0 mg，依次装入有盖进样瓶21-30，并保持样品映射关系。"
    ),
}
HANDOFF_D02_MIX = {
    "name": "同批次单金属物理混合称量",
    "required_actions": [
        "制备Ni75+Co25：15.0 mg Ni100 + 5.0 mg Co100",
        "分别装入21-29号进样瓶并保持重复号对应",
    ],
}

# --- handoffs that must NOT be flagged --------------------------------------

HANDOFF_XRD_RETURN = {
    "name": "离线 XRD observation",
    "sample": "样品",
    "required_return_data": ["XRD 图谱", "判读结果"],
}
HANDOFF_QUARTZ_TRUE_GAP = {
    "name": "石英孔板装粉",
    "required_action": "向无盖96位石英孔板定量装粉 10.0 mg 后送马弗炉煅烧",
}
HANDOFF_REVIEW_ONLY = {
    "name": "安全审核",
    "required_action": "人工审核高温步骤的风险并确认",
}
HANDOFF_NO_MASS = {
    "name": "定性分样说明",
    "required_action": "将粉末分样并记录外观颜色",  # weighing verb but no mass
}

# --- C01's real hard constraint (must stay hard) ----------------------------

C01_QUARTZ_CONSTRAINT = (
    "NiO 前驱体在进样瓶内完成离心、洗涤和 60 ℃干燥后，必须转入 96 位石英孔板才能进入马弗炉，"
    "但设备真源没有“进样瓶固体直接转移至 96 位石英孔板”的操作。"
)


class AuditOfflineHandoffsTest(unittest.TestCase):
    def test_archived_weighing_handoffs_are_flagged(self) -> None:
        wf = {"offline_handoffs": [HANDOFF_C02_A1, HANDOFF_C02_A2, HANDOFF_A01, HANDOFF_D02_MIX]}
        findings = audit_offline_handoffs(wf)
        self.assertEqual(len(findings), 4)
        for finding in findings:
            self.assertEqual(finding["type"], "unnecessary_offline_handoff")
            self.assertIn("route", finding)
            self.assertIn("固体样品转移", finding["route"])

    def test_legitimate_handoffs_are_not_flagged(self) -> None:
        wf = {"offline_handoffs": [
            HANDOFF_XRD_RETURN, HANDOFF_QUARTZ_TRUE_GAP,
            HANDOFF_REVIEW_ONLY, HANDOFF_NO_MASS,
        ]}
        self.assertEqual(audit_offline_handoffs(wf), [])

    def test_mixed_list_flags_only_the_weighing_ones(self) -> None:
        wf = {"offline_handoffs": [HANDOFF_XRD_RETURN, HANDOFF_A01, HANDOFF_QUARTZ_TRUE_GAP]}
        findings = audit_offline_handoffs(wf)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["handoff_index"], 2)

    def test_mass_out_of_all_station_ranges_is_not_flagged(self) -> None:
        wf = {"offline_handoffs": [{
            "name": "超量称样",
            "required_action": "称取 500 g 粉末装入进样瓶",  # beyond every station
        }]}
        self.assertEqual(audit_offline_handoffs(wf), [])

    def test_issue9_p1_consistency_cases(self) -> None:
        """#9 P1: 5 mg and 10 mg into vials → always flag; quartz plate →
        never flag; repeated runs → byte-identical verdicts (determinism)."""
        five = {"offline_handoffs": [{"name": "a", "required_action": "各称取5.0 mg装入进样瓶"}]}
        ten = {"offline_handoffs": [{"name": "b", "required_action": "每份称取10.0 mg至对应进样瓶"}]}
        quartz = {"offline_handoffs": [HANDOFF_QUARTZ_TRUE_GAP]}
        self.assertEqual(len(audit_offline_handoffs(five)), 1)
        self.assertEqual(len(audit_offline_handoffs(ten)), 1)
        self.assertEqual(audit_offline_handoffs(quartz), [])
        self.assertEqual(audit_offline_handoffs(five), audit_offline_handoffs(five))


class ManualMaterialScanTest(unittest.TestCase):
    def test_manual_weighing_in_handoff_is_flagged(self) -> None:
        wf = {"offline_handoffs": [HANDOFF_C02_A2]}
        findings = scan_manual_material_operations(wf)
        self.assertTrue(findings)
        self.assertEqual(findings[0]["type"], "forbidden_manual_material_operation")

    def test_manual_verbs_in_txt_and_steps_are_flagged(self) -> None:
        wf = {"steps": [{
            "step_number": 3, "workstation": "x", "operation": "转移",
            "notes": "由操作员人工搬运至下一站",
        }]}
        findings = scan_manual_material_operations(wf, "第5步 人工装载样品")
        wheres = {finding["where"] for finding in findings}
        self.assertIn("第 3 步", wheres)
        self.assertIn("workflow_txt", wheres)

    def test_review_wording_is_whitelisted(self) -> None:
        wf = {"offline_handoffs": [HANDOFF_REVIEW_ONLY]}
        self.assertEqual(scan_manual_material_operations(wf), [])
        self.assertEqual(
            scan_manual_material_operations({}, "该步骤需人工复核后继续"), []
        )


class WeighingFalseHardGuardTest(unittest.TestCase):
    def test_weighing_gap_claim_is_downgraded(self) -> None:
        self.assertTrue(
            weighing_false_hard_guard("真源中缺少固体称量工作站，无法完成 10 mg 定量")
        )
        self.assertTrue(weighing_false_hard_guard("设备无法称取 5 mg 粉末"))

    def test_true_quartz_gap_stays_hard(self) -> None:
        self.assertFalse(weighing_false_hard_guard(C01_QUARTZ_CONSTRAINT))
        self.assertFalse(
            weighing_false_hard_guard("马弗炉仅接受96位石英孔板，无法称量装载")
        )

    def test_non_weighing_hard_constraints_untouched(self) -> None:
        self.assertFalse(weighing_false_hard_guard("缺少高压反应釜，无法进行180℃溶剂热反应"))
        self.assertFalse(weighing_false_hard_guard(""))


if __name__ == "__main__":
    unittest.main()
