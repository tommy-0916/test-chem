"""Offline contracts for tier isolation, source freshness and feedback truth."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_skills.capabilities import (
    DEFAULT_INDEX,
    SKILL_ROOT,
    TIER_SKILLS,
    extract_experiment_capabilities,
    load_capability_skill,
    load_current_capability_index,
    project_device_context,
)
from chem_resources.generate_workstation_capability_index import (
    _feedback_contracts,
    _range_text,
    build_index,
)
from device_agent.skill_contract_audit import ParameterNode
from reaserch_agent.tools.device_context import apply_device_status, load_device_context


def keys(value):
    result = set()
    if isinstance(value, dict):
        result.update(value)
        for nested in value.values():
            result.update(keys(nested))
    elif isinstance(value, list):
        for nested in value:
            result.update(keys(nested))
    return result


class CapabilityProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load_current_capability_index()

    def test_exact_source_roster_and_source_grounded_experiments(self):
        projected = project_device_context(self.index, "experiment")
        self.assertEqual(len(projected["workstations"]), 45)
        self.assertFalse([item for item in projected["capabilities"] if item["support_status"] == "unknown"])
        xrd = next(item for item in projected["capabilities"] if item.get("id") == "xrd")
        self.assertIn("XRD", xrd["name"])
        self.assertEqual(xrd["evidence"]["line"], 6)
        self.assertEqual(xrd["evidence"]["quote"], "分散于乙醇中的固体样品进行滴加制样并开展XRD测试")
        self.assertIn("乙醇分散", xrd["name"])
        self.assertEqual(extract_experiment_capabilities({"station_code": "XRD_V1", "description": "仅用于放置样品"}), [])
        self.assertEqual(extract_experiment_capabilities({"station_code": "Unknown_XRD_Station", "description": "开展XRD测试"}), [])
        negative = extract_experiment_capabilities({"station_code": "XRD_V1", "description": "不支持对分散于乙醇中的固体样品进行滴加制样并开展XRD测试"})
        self.assertEqual(negative[0]["support_status"], "unsupported")

    def test_tiers_do_not_leak_lower_level_contracts(self):
        forbidden = {"input", "output", "container_contract", "parameter_contracts", "scientific_controls", "feedback_contract", "planning_constraints", "audit_content", "compact_workstation_capabilities"}
        for tier in ("experiment", "operation"):
            projected = project_device_context(self.index, tier)
            self.assertFalse(keys(projected) & forbidden)
        operations = project_device_context(self.index, "operation")["operations"]
        self.assertEqual(len(operations), 61)
        self.assertIn("XRD滴液检测全流程", {item["name"] for item in operations})

    def test_step_preserves_full_constraints_dependencies_and_unknown_feedback(self):
        projected = project_device_context(self.index, "step")
        xrd = next(item for item in projected["operation_contracts"] if item["station_code"] == "XRD_V1")
        station = next(item for item in projected["workstations"] if item["station_code"] == "XRD_V1")
        self.assertEqual(xrd["input"]["containers"], ["进样瓶", "50ml耐热瓶"])
        self.assertIn("必须无盖", xrd["input"]["container_states"])
        constraints = json.dumps(station["planning_constraints"], ensure_ascii=False)
        self.assertIn("≥ 4.0ml", constraints)
        self.assertIn("4.0 mL 无水乙醇", constraints)
        self.assertIn("分支 A", constraints)
        self.assertTrue(any("Spectroscopy_Magnetic_Stirrer_Workstation_V1" in item["station_codes"] for item in station["dependencies"]))
        self.assertTrue(any("audit" in item["sources"] for item in station["planning_constraints"]))
        for operation in projected["operation_contracts"]:
            self.assertEqual(operation["feedback_contract"]["returned_data"]["status"], "unknown")
            self.assertEqual(operation["feedback_contract"]["intermediate_feedback"]["status"], "unknown")
            self.assertFalse(any(control.get("type") == "file" for control in operation["scientific_controls"]))
        self.assertTrue(projected["global_constraints"])

    def test_live_status_is_separate_from_declared_support_all_tiers(self):
        context = apply_device_status(load_device_context(), {"XRD_V1": "offline"})
        for tier, key in (("experiment", "capabilities"), ("operation", "operations"), ("step", "operation_contracts")):
            result = project_device_context(context, tier)
            entries = [item for item in result[key] if item["station_code"] == "XRD_V1"]
            self.assertTrue(entries)
            self.assertTrue(all(item["availability"] == "offline" and item["currently_usable"] is False for item in entries))
        self.assertEqual(project_device_context(context, "experiment")["capabilities"][-1]["support_status"], "supported")

    def test_projection_is_idempotent_and_cross_tier_roster_is_preserved(self):
        context = load_device_context()
        context["workstations"] = [{"station_name": "XRD_V1", "availability": "offline"}]
        experiment = project_device_context(context, "experiment")
        self.assertEqual(project_device_context(experiment, "experiment"), experiment)
        operation = project_device_context(experiment, "operation")
        self.assertEqual(len(operation["workstations"]), 1)
        self.assertEqual(operation["operations"][0]["name"], "XRD滴液检测全流程")
        self.assertIs(operation["operations"][0]["currently_usable"], False)

    def test_repeated_projection_rejects_extra_fields_at_every_nested_level(self):
        for tier, key in (("experiment", "capabilities"), ("operation", "operations")):
            clean = project_device_context(self.index, tier)
            dirty = copy.deepcopy(clean)
            dirty["skill_content"] = "machine secret"
            dirty["input"] = {"containers": ["unexpected"]}
            dirty["workstations"][0]["parameter_contracts"] = [{"name": "machine secret"}]
            dirty[key][0]["input"] = {"containers": ["unexpected"]}
            dirty[key][0].setdefault("evidence" if tier == "experiment" else "source", {})["skill_content"] = "machine secret"
            self.assertEqual(project_device_context(dirty, tier), clean)
            dirty[key][0]["name"] = {"input": "machine secret"}
            self.assertNotIn("input", keys(project_device_context(dirty, tier)))

    def test_repeated_step_projection_is_idempotent(self):
        clean = project_device_context(self.index, "step")
        self.assertEqual(project_device_context(clean, "step"), clean)

    def test_step_excludes_machine_only_identifiers_but_preserves_io_evidence(self):
        step = project_device_context(self.index, "step")
        for operation in step["operation_contracts"]:
            self.assertFalse(any(control["name"].endswith("编号") for control in operation["scientific_controls"]))
            self.assertFalse(any(control["name"] in {"瓶号", "位置", "孔板ID", "模板文件选择", "照片名称"} for control in operation["scientific_controls"]))
            self.assertTrue(operation.get("io_evidence"))
        self.assertNotIn('"value"', json.dumps(step["operation_contracts"], ensure_ascii=False))
        liquid = next(station for station in step["workstations"] if station["station_code"] == "Liquid_Handling_Station_1ml_V2")
        self.assertIn("3mL", liquid["capability_description"])

    def test_custom_context_never_substitutes_default_equipment(self):
        context = {
            "source": "custom lab", "planning_policy": "仅可使用声明操作，禁止 XRD",
            "excluded_capabilities": ["xrd"],
            "workstations": [{"station_name": "Custom", "capabilities": ["混合"], "availability": "busy"}],
        }
        original = copy.deepcopy(context)
        with patch("agent_skills.capabilities.load_current_capability_index", side_effect=AssertionError("must not load defaults")):
            for tier in TIER_SKILLS:
                result = project_device_context(context, tier)
                self.assertEqual(len(result["workstations"]), 1)
                self.assertEqual(result["workstations"][0]["station_code"], "Custom")
                self.assertEqual(result["excluded_capabilities"], ["xrd"])
                self.assertIn("禁止 XRD", result["additional_planning_policy"])
            self.assertEqual(project_device_context({"workstations": []}, "step")["workstations"], [])
        self.assertEqual(context, original)

    def test_explicit_operation_restrictions_override_indexed_defaults(self):
        context = load_device_context()
        context["workstations"] = [{"station_name": "Liquid_Handling_Station_1ml_V2", "capabilities": ["开盖"]}]
        operation = project_device_context(context, "operation")
        self.assertEqual([item["name"] for item in operation["operations"]], ["开盖"])
        experiment = project_device_context(context, "experiment")
        self.assertTrue(all(item["support_status"] == "unknown" for item in experiment["capabilities"]))

    def test_generated_references_match_runtime_projection(self):
        for tier, skill in TIER_SKILLS.items():
            path = SKILL_ROOT / skill / "references" / "capabilities.json"
            self.assertEqual(json.loads(path.read_text()), project_device_context(self.index, tier))

    def test_skill_instructions_are_loaded_fresh_without_changing_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / TIER_SKILLS["experiment"]
            folder.mkdir()
            skill = folder / "SKILL.md"
            skill.write_text("---\nname: experiment-capabilities\n---\n优先按实验模式检索。\n", encoding="utf-8")
            with patch("agent_skills.capabilities.SKILL_ROOT", root):
                first = load_capability_skill(self.index, "experiment")
                skill.write_text("---\nname: experiment-capabilities\n---\n同时保留方法学边界。\n", encoding="utf-8")
                second = load_capability_skill(first, "experiment")
            self.assertIn("同时保留方法学边界", second["instructions"])
            self.assertNotEqual(first["instructions_digest_sha256"], second["instructions_digest_sha256"])
            self.assertEqual(first["source_digest_sha256"], second["source_digest_sha256"])
            self.assertEqual(first["capabilities"], second["capabilities"])
            self.assertEqual(second["name"], "experiment-capabilities")
            self.assertEqual(second["skill_source_path"], str(skill.resolve()))
            self.assertEqual(project_device_context(second, "experiment"), project_device_context(self.index, "experiment"))

    def test_skill_loader_uses_only_fixed_tier_paths(self):
        context = project_device_context(self.index, "operation")
        context["skill_source_path"] = "/not/a/skill.md"
        loaded = load_capability_skill(context, "operation")
        self.assertEqual(loaded["skill_source_path"], str((SKILL_ROOT / "operation-capabilities" / "SKILL.md").resolve()))
        self.assertTrue(loaded["instructions"].startswith("---"))
        with self.assertRaises(ValueError):
            load_capability_skill(context, "../experiment")

    def test_example_array_is_not_scalar_range(self):
        def node(name, example, type_name="string"):
            return ParameterNode(name=name, required=True, type_name=type_name, unit="", remark="", example=example, default="", line=1)
        self.assertEqual(_range_text(node("容器编号", "[1,2]", "array")), "")
        self.assertEqual(_range_text(node("编号", "如[1,2]")), "")
        self.assertEqual(_range_text(node("扫描速度", "(0,20]")), "(0,20]")

    def test_explicit_feedback_support_does_not_imply_intermediate_support(self):
        text = "## 操作 1. **检测**\n### 参数设置\n| 温度 | 50 |\n### 输出数据\n- 光谱文件: 文件引用\n### 中间返回\n- 不支持中间反馈\n"
        result = _feedback_contracts(text, Path("fixture.md"), ["检测"])["检测"]
        self.assertEqual(result["returned_data"]["status"], "supported")
        self.assertEqual(result["returned_data"]["fields"], ["光谱文件"])
        self.assertEqual(result["intermediate_feedback"]["status"], "unsupported")
        self.assertNotIn("温度", json.dumps(result))
        global_section = "## 返回结果\n- 收率: number\n## 操作 1. **A**\n## 操作 2. **B**\n"
        multi = _feedback_contracts(global_section, Path("fixture.md"), ["A", "B"])
        self.assertTrue(all(item["returned_data"]["status"] == "unknown" for item in multi.values()))

    def test_stale_source_and_audit_rebuild_in_memory_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "lab"
            station = root / "references-Characterization-Module" / "XRD_V1"
            station.mkdir(parents=True)
            skill = station / "SKILL.md"
            skill.write_text("---\nname: XRD_V1\ndescription: 开展XRD测试\n---\n## 操作 1. **测试**\n### 输入输出约束\n- **输入约束**\n- 容器类型：进样瓶\n- **输出约束**\n- 容器类型：与输入保持一致\n", encoding="utf-8")
            audit_dir = root / "references_audit"
            audit_dir.mkdir()
            audit = audit_dir / "XRD_V1_audit.md"
            audit.write_text("## 整体流程约束\n- 必须有前置搅拌。\n", encoding="utf-8")
            path = Path(temporary) / "index.json"
            initial = build_index(root)
            path.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
            original = path.read_bytes()
            audit.write_text("## 整体流程约束\n- 必须有前置搅拌且温度不得超过50℃。\n", encoding="utf-8")
            current = load_current_capability_index(path)
            self.assertNotEqual(current["source_digest_sha256"], initial["source_digest_sha256"])
            self.assertIn("不得超过50℃", json.dumps(current["workstations"][0]["planning_constraints"], ensure_ascii=False))
            self.assertEqual(path.read_bytes(), original)

    def test_saved_generated_projection_refreshes_semantics_and_preserves_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "lab"
            station = root / "references-Characterization-Module" / "XRD_V1"
            station.mkdir(parents=True)
            skill = station / "SKILL.md"
            source = "---\nname: XRD_V1\ndescription: 可对分散于乙醇中的固体样品进行滴加制样并开展XRD测试。\n---\n## 操作 1. **测试**\n### 输入输出约束\n- **输入约束**\n- 容器类型：进样瓶\n- **输出约束**\n- 容器类型：与输入保持一致\n"
            skill.write_text(source, encoding="utf-8")
            other = root / "references-Characterization-Module" / "Other"
            other.mkdir()
            (other / "SKILL.md").write_text("---\nname: Other\ndescription: 样品暂存。\n---\n", encoding="utf-8")
            path = Path(temporary) / "index.json"
            path.write_text(json.dumps(build_index(root), ensure_ascii=False), encoding="utf-8")
            context = {"capability_index": str(path), "source": str(root), "workstations": [{"station_name": "XRD_V1", "availability": "offline"}], "excluded_capabilities": ["other-technique"]}
            first = project_device_context(context, "experiment")
            self.assertEqual(first["capabilities"][0]["support_status"], "supported")
            skill.write_text(source.replace("可对分散于乙醇中的固体样品进行滴加制样并开展XRD测试", "仅用于样品暂存，未声明任何测量实验"), encoding="utf-8")
            second = project_device_context(first, "experiment")
            self.assertNotEqual(first["source_digest_sha256"], second["source_digest_sha256"])
            self.assertEqual(second["capabilities"][0]["support_status"], "unknown")
            self.assertEqual([item["station_code"] for item in second["workstations"]], ["XRD_V1"])
            self.assertIs(second["capabilities"][0]["currently_usable"], False)
            self.assertEqual(second["excluded_capabilities"], ["other-technique"])
            self.assertEqual(project_device_context(second, "experiment"), second)

    def test_saved_custom_projection_without_index_never_loads_defaults(self):
        snapshot = project_device_context({"workstations": [{"station_name": "custom", "capabilities": ["混合"]}]}, "operation")
        snapshot["source_digest_sha256"] = "an-explicit-external-snapshot"
        self.assertFalse(snapshot["source_index"])
        with patch("agent_skills.capabilities.load_current_capability_index", side_effect=AssertionError("must not load default catalog")):
            self.assertEqual(project_device_context(snapshot, "operation"), snapshot)


if __name__ == "__main__":
    unittest.main()
