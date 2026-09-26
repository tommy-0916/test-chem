"""Phase 5 strict-entry preflight tests.

Deterministic: read-only against the in-repo registries, no network/LLM.
"""

from __future__ import annotations

import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaserch_agent.strict_entry import (
    KNOWN_EXTERNAL_CONTRACT_MISSING,
    _LAB_DESIGN_ROOT,
    build_station_registry,
    build_strict_entry_preflight,
    resolve_operation,
    resolve_station,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "a01_frozen_package.json"


def synthetic_package(step_updates: dict) -> dict:
    step = {
        "macro_step_id": "MS_001",
        "sequence": 1,
        "operation": "超声清洗",
        "sample_id": "S1",
        "parameters": [],
        "logical_containers": [],
        "quantity_requirements": [],
        "provenance": {"kind": "paper", "reference": "r"},
    }
    step.update(step_updates)
    return {
        "schema_version": "2.0",
        "campaign_id": "c",
        "macro_steps": [step],
        "evidence_bundle": {"items": []},
    }


class StationResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_station_registry()

    def test_skill_hit_resolves_with_evidence(self):
        result = resolve_station("Ultrasonic_Disperser_V1", self.registry)
        self.assertEqual(result["status"], "resolved")
        self.assertIn("lab-design-all", result["via"])
        self.assertTrue(result["evidence"].endswith("SKILL.md"))

    def test_display_title_hit_resolves(self):
        result = resolve_station("批量加液工作站_V1", self.registry)
        # SKILL 存在但平台 wire 合同缺失 -> 显式 contract_missing,绝不
        # 借 SKILL 存在而放行平台层。
        self.assertEqual(result["status"], "external_contract_missing")

    def test_alias_map_hit(self):
        # Every 对照表 Chinese name that resolves is also a SKILL title in the
        # real registry (display match wins); the pure alias path is exercised
        # with a synthetic registry entry.
        direct = resolve_station("移液平台_1ml_V2", self.registry)
        self.assertEqual(direct["status"], "resolved")
        synthetic = {
            "stations": {
                "Synthetic_Station_V1": {
                    "code": "Synthetic_Station_V1",
                    "display_names": ["Synthetic_Station_V1"],
                    "skill_path": "synthetic/SKILL.md",
                    "operations": [],
                    "container_constraints": [],
                    "via": "lab-design-all/SKILL",
                }
            },
            "aliases": {"合成站_V1": "Synthetic_Station_V1"},
        }
        result = resolve_station("合成站_V1", synthetic)
        self.assertEqual(result["status"], "alias_resolved")
        self.assertIn("对照", result["evidence"])

    def test_unknown_station_unresolved(self):
        result = resolve_station("不存在的站_V9", self.registry)
        self.assertEqual(result["status"], "unresolved")

    def test_known_missing_set_matches_spec(self):
        self.assertEqual(
            KNOWN_EXTERNAL_CONTRACT_MISSING,
            {"批量加液工作站_V1", "离心样品转移工作站_V1"},
        )


class OperationResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_station_registry()

    def _entry(self, name):
        from reaserch_agent.strict_entry import _find_station_entry

        return _find_station_entry(name, self.registry)

    def test_canonical_alias_resolution(self):
        entry = self._entry("Drying_Oven_V1")
        result = resolve_operation(
            "静置烘干", entry, "Drying_Oven_V1", self.registry,
            {"Drying_Oven_V1": {"canonical": "烘干主流程", "aliases": ["静置烘干", "烘干"]}},
        )
        self.assertEqual(result["status"], "canonical_resolved")
        self.assertEqual(result["via"], "operation_aliases")

    def test_exact_skill_operation(self):
        entry = self._entry("Ultrasonic_Disperser_V1")
        result = resolve_operation(
            "超声清洗", entry, "Ultrasonic_Disperser_V1", self.registry, {}
        )
        self.assertEqual(result["status"], "resolved")

    def test_station_isolation(self):
        # "烘干" must not resolve at a station whose operations lack 烘干主流程.
        entry = self._entry("Centrifuge_V1")
        result = resolve_operation(
            "烘干", entry, "Centrifuge_V1", self.registry,
            {"Drying_Oven_V1": {"canonical": "烘干主流程", "aliases": ["烘干"]}},
        )
        self.assertEqual(result["status"], "unresolved")

    def test_unresolvable_operation(self):
        entry = self._entry("Drying_Oven_V1")
        result = resolve_operation(
            "不存在的操作", entry, "Drying_Oven_V1", self.registry, {}
        )
        self.assertEqual(result["status"], "unresolved")


class PreflightIntegrationTest(unittest.TestCase):
    def test_external_contract_missing_listed(self):
        package = synthetic_package(
            {
                "参数": "使用批量加液工作站_V1 进行批量加液",
            }
        )
        preflight = build_strict_entry_preflight(package, [])
        self.assertIn("批量加液工作站_V1", preflight["external_contract_missing"])
        statuses = {
            item["station"]: item["status"]
            for item in preflight["station_resolution"]
        }
        self.assertEqual(statuses["批量加液工作站_V1"], "external_contract_missing")
        self.assertFalse(preflight["fallback_used"])

    def test_fallback_substitution_detected(self):
        registry = build_station_registry()
        fake_entry = {
            "code": "Fake_Dispense_V1",
            "display_names": ["批量加液平台_V1"],
            "skill_path": "synthetic",
            "operations": [],
            "container_constraints": [],
            "via": "lab-design-all/SKILL",
        }
        patched_stations = dict(registry["stations"])
        patched_stations["Fake_Dispense_V1"] = fake_entry
        patched_registry = {"stations": patched_stations, "aliases": registry["aliases"]}
        package = synthetic_package(
            {"参数": "改用批量加液平台_V1 执行加液"}
        )
        with patch(
            "reaserch_agent.strict_entry.build_station_registry",
            return_value=patched_registry,
        ):
            preflight = build_strict_entry_preflight(package, [])
        self.assertTrue(preflight["fallback_used"])
        self.assertEqual(preflight["external_contract_missing"], [])

    def test_container_alignment_records_mismatch(self):
        package = synthetic_package(
            {
                "参数": "超声分散仪_V1 超声清洗",
                "logical_containers": [
                    {
                        "logical_container_id": "c1",
                        "container_type": "离心管",
                        "count": 1,
                    }
                ],
            }
        )
        preflight = build_strict_entry_preflight(package, [])
        alignment = preflight["container_lid_alignment"]
        self.assertGreaterEqual(alignment["checked"], 1)
        self.assertTrue(alignment["mismatches"])
        mismatch = alignment["mismatches"][0]
        self.assertEqual(mismatch["container_type"], "离心管")
        self.assertIn("进样瓶", " ".join(mismatch["skill_constraint"]))

    def test_registry_missing_fails_loud(self):
        with patch(
            "reaserch_agent.strict_entry._LAB_DESIGN_ROOT",
            Path(tempfile.gettempdir()) / "definitely-absent-registry-xyz",
        ):
            with self.assertRaises(RuntimeError):
                build_station_registry()

    def test_a01_fixture_preflight_shape(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        preflight = build_strict_entry_preflight(
            fixture["research_action_package_v2"], []
        )
        self.assertEqual(set(preflight), {
            "station_resolution",
            "operation_resolution",
            "container_lid_alignment",
            "external_contract_missing",
            "fallback_used",
        })
        self.assertFalse(preflight["fallback_used"])


class HandoffWiringTest(unittest.TestCase):
    def test_handoff_carries_preflight_block(self):
        from reaserch_agent.state import ResearchAgentState, ResearchEvent

        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        state = ResearchAgentState(
            event=ResearchEvent("bootstrap", "q", {"device_context": {}}),
            contract_version="v2",
        )
        state.research_action_package_v2 = fixture["research_action_package_v2"]
        state.macro_plan = []
        handoff = state.device_adaptation_external_handoff()
        block = handoff.get("strict_entry_preflight")
        self.assertIsInstance(block, dict)
        self.assertIn("external_contract_missing", block)
        self.assertFalse(block["fallback_used"])


if __name__ == "__main__":
    unittest.main()
