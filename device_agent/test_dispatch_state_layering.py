"""R3 tests: P5 planned/verified sample-state layering, P6 drying-operation
alias resource, P7 lid state-machine conclusions.

Deterministic: fixture workstation catalogs, no LLM, no network.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("CHEM_DEVICE_CONTRACT_AUDIT", "off")
os.environ.setdefault("CHEM_DEVICE_WORKFLOW_VERIFICATION", "deterministic")
os.environ.setdefault("CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS", "off")
os.environ.setdefault("CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS", "1")

from dispatch_checker import check_dispatch
from utils.workstation_loader import (
    WorkstationLoader,
    resolve_explicit_alias,
)

DEVICE_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEVICE_DIR.parent
FROZEN_B01 = REPO_ROOT / "campaigns" / "B01-v3-k3-engchain-20260926-012716" / "diagnostic_package.json"
WORKSTATION_ROOT = REPO_ROOT / "chem_resources" / "lab-design-all" / "skills" / "chemistry-experiment-workstation"

TABLE_HEADER = """| 参数名 | 备注 | 是否必填 | 单位 | 类型 | 示例值 | 默认值 |
|--------|------|----------|------|------|--------|--------|
"""
CONTAINER_ROWS = """| 容器类型 | 容器类型 | 是 | | string | 进样瓶 | |
| 容器数量 | 容器数量 | 是 | | int | 1 | |
| 容器编号 | 容器编号 | 是 | | array | [1] |
"""


def _write_station(root: Path, name: str, station_id: int, operation: str, io_text: str) -> None:
    directory = root / "references-Synthesis-Module" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\n工作站编码: {station_id}\nname: {name}\n---\n\n"
        f"# {name}\n\n{io_text}\n"
        f"## 参数设置\n\n- **{operation}**\n\n{TABLE_HEADER}{CONTAINER_ROWS}",
        encoding="utf-8",
    )


def _export_step(station: str, operation: str) -> dict[str, Any]:
    return {
        "workstation": station,
        "operation": operation,
        "parameters": [
            {"parameter_name": "容器类型", "type": "string"},
            {"parameter_name": "容器数量", "type": "int"},
            {"parameter_name": "容器编号", "type": "array"},
        ],
    }


def write_layering_catalog(root: Path, *, contract_dispense: bool) -> None:
    """Five fixture stations; the batch-dispense station is only listed in the
    platform export when contract_dispense is True."""
    _write_station(
        root,
        "Fixture_Acquire",
        201,
        "物料拿取",
        "## 输入输出约束\n\n- **输入约束**\n  - 样品状态：不限\n",
    )
    _write_station(
        root,
        "Fixture_Dose",
        202,
        "固体进样",
        "## 输入输出约束\n\n- **输入约束**\n  - 样品状态：不限\n"
        "- **输出约束**\n  - 样品状态：粉末\n",
    )
    _write_station(
        root,
        "Fixture_Dispense",
        203,
        "批量加液流程",
        "## 输入输出约束\n\n- **输入约束**\n  - 样品状态：不限\n"
        "- **输出约束**\n  - 样品状态：与输入保持一致\n  - 容器状态：有盖\n",
    )
    _write_station(
        root,
        "Fixture_Sonic",
        204,
        "超声清洗",
        "## 输入输出约束\n\n- **输入约束**\n  - 样品状态：悬浊液\n",
    )
    _write_station(
        root,
        "Fixture_Open",
        205,
        "开盖",
        "## 输入输出约束\n\n- **输入约束**\n  - 容器状态：有盖\n"
        "- **输出约束**\n  - 容器状态：无盖\n",
    )
    steps = [
        _export_step("Fixture_Acquire", "物料拿取"),
        _export_step("Fixture_Dose", "固体进样"),
        _export_step("Fixture_Sonic", "超声清洗"),
        _export_step("Fixture_Open", "开盖"),
    ]
    if contract_dispense:
        steps.append(_export_step("Fixture_Dispense", "批量加液流程"))
    (root / "0410数据转换.txt").write_text(
        json.dumps({"steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def layering_payload(*, state_change: bool, declared_after: bool) -> dict[str, Any]:
    def step(number: int, station: str, operation: str, plan_step: int) -> dict[str, Any]:
        return {
            "step_number": number,
            "workstation": station,
            "operation": operation,
            "id": 0,
            "source_plan_step": plan_step,
            "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]},
        }

    device_plan = [
        {"plan_step": 1, "operation_intent": "拿取", "material_event_kind": "none"},
        {"plan_step": 2, "operation_intent": "固体进样", "material_event_kind": "none"},
        {
            "plan_step": 3,
            "operation_intent": "批量加液",
            "material_event_kind": "state_change" if state_change else "none",
            "material_transition_ids": ["mt_1"] if declared_after else [],
        },
        {"plan_step": 4, "operation_intent": "超声清洗", "material_event_kind": "none"},
        {"plan_step": 5, "operation_intent": "开盖", "material_event_kind": "none"},
    ]
    payload = {
        "status": "success",
        "device_plan": device_plan,
        "workflow_json": {
            "steps": [
                step(1, "Fixture_Acquire", "物料拿取", 1),
                step(2, "Fixture_Dose", "固体进样", 2),
                step(3, "Fixture_Dispense", "批量加液流程", 3),
                step(4, "Fixture_Sonic", "超声清洗", 4),
                step(5, "Fixture_Open", "开盖", 5),
            ]
        },
    }
    if declared_after:
        payload["material_transitions"] = [
            {
                "transition_id": "mt_1",
                "transition_kind": "state_change",
                "after_material_states": ["悬浊液"],
            }
        ]
    return payload


class SampleStateLayeringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="r3-layering-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "catalog"
        write_layering_catalog(self.root, contract_dispense=False)

    def check(self, payload: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("workstation_root", self.root)
        return check_dispatch(payload, **kwargs)

    @staticmethod
    def codes(report: dict[str, Any]) -> set[str]:
        return {item["code"] for item in report["findings"]}

    def test_state_change_without_declared_result_is_unverified_not_error(self):
        report = self.check(layering_payload(state_change=True, declared_after=False))
        codes = self.codes(report)
        assert "sample_state_conflict" not in codes
        findings = [f for f in report["findings"] if f["code"] == "sample_state_unverified"]
        assert len(findings) == 1
        assert findings[0]["severity"] == "unverified"
        assert findings[0]["planned"] is None
        assert findings[0]["verified"] is None
        # downstream static analysis continues: step 5 is not blocked
        assert "blocked_by_previous_step" not in codes
        # the dispatch gate stays closed
        assert report["dispatchable"] is False
        assert report["status"] in {"not_verifiable", "failed"}

    def test_declared_after_state_feeds_planned_layer(self):
        report = self.check(layering_payload(state_change=True, declared_after=True))
        codes = self.codes(report)
        assert "sample_state_conflict" not in codes
        findings = [f for f in report["findings"] if f["code"] == "sample_state_unverified"]
        assert len(findings) == 1
        assert findings[0]["planned"] == ["suspension"]

    def test_planned_conflict_stays_error(self):
        # No state_change: the dispense step keeps the incoming planned state,
        # which genuinely conflicts with the downstream requirement.
        report = self.check(layering_payload(state_change=False, declared_after=False))
        assert "sample_state_conflict" in self.codes(report)
        assert report["dispatchable"] is False

    def test_verified_conflict_stays_error(self):
        payload = layering_payload(state_change=False, declared_after=False)
        payload["workflow_json"]["steps"] = payload["workflow_json"]["steps"][:1]
        payload["workflow_json"]["steps"][0].update(
            {"workstation": "Fixture_Sonic", "operation": "超声清洗"}
        )
        report = self.check(
            payload,
            initial_state={"containers": [{"container_type": "进样瓶", "container_id": 1, "sample_state": "粉末"}]},
        )
        assert "sample_state_conflict" in self.codes(report)

    def test_contracted_step_advances_verified_layer(self):
        write_layering_catalog(self.root, contract_dispense=True)
        payload = layering_payload(state_change=False, declared_after=False)
        # contracted dispense declares 与输入保持一致: verified powder persists,
        # and the sonic requirement (悬浊液) conflicts against the verified layer.
        report = self.check(payload)
        assert "sample_state_conflict" in self.codes(report)


class DryingAliasResourceTest(unittest.TestCase):
    def test_resource_merges_canonical_and_aliases(self):
        loader = WorkstationLoader(use_new_format=True)
        alias_map = loader.OPERATION_ALIAS_MAP
        assert alias_map["烘干主流程"][:1] == ["烘干主流程"]
        assert "静置烘干" in alias_map["烘干主流程"]
        assert "烘干" in alias_map["烘干主流程"]
        # builtin entries still available
        assert alias_map["开盖"] == ["开盖"]

    def test_station_scoped_isolation(self):
        loader = WorkstationLoader(use_new_format=True)
        alias_map = loader.OPERATION_ALIAS_MAP
        drying_ops = ["烘干主流程"]
        other_ops = ["搅拌", "开盖"]
        assert resolve_explicit_alias("烘干", drying_ops, alias_map) == "烘干主流程"
        assert resolve_explicit_alias("静置烘干", drying_ops, alias_map) == "烘干主流程"
        assert resolve_explicit_alias("烘干", other_ops, alias_map) is None

    def test_missing_resource_falls_back_to_builtin(self):
        loader = WorkstationLoader.__new__(WorkstationLoader)
        loader._new_workstations = {}
        loader._workstations = {}
        loader._station_alias_map = {}
        with tempfile.TemporaryDirectory() as empty:
            import utils.workstation_loader as loader_module

            original_root = loader_module.chem_resources_root
            loader_module.chem_resources_root = lambda: Path(empty)
            try:
                alias_map = loader._load_operation_alias_resource()
            finally:
                loader_module.chem_resources_root = original_root
        assert "烘干主流程" not in alias_map
        assert alias_map["静置烘干"] == ["静置烘干", "烘干"]

    def test_dryer_truth_sources_aligned(self):
        dryer = json.loads(
            (REPO_ROOT / "chem_resources" / "workstations" / "dryer.json").read_text(encoding="utf-8")
        )
        operation_names = [operation["name"] for operation in dryer["operations"]]
        assert "烘干主流程" in operation_names
        assert "静置烘干" not in operation_names
        usage = (
            REPO_ROOT / "chem_resources" / "workstations_new" / "dryer-workstation" / "USAGE.md"
        ).read_text(encoding="utf-8")
        assert "操作：烘干主流程" in usage
        resource = json.loads(
            (REPO_ROOT / "chem_resources" / "operation_aliases" / "operation_aliases.json").read_text(
                encoding="utf-8"
            )
        )
        drying = resource["station_aliases"]["Drying_Oven_V1"]
        assert drying["canonical"] == "烘干主流程"
        assert set(drying["aliases"]) == {"静置烘干", "烘干"}


@pytest.mark.skipif(not FROZEN_B01.exists(), reason="frozen B01 package not present")
class FrozenB01LidChainTest(unittest.TestCase):
    """P7: the current lid state machine neither fires on the real B01 chain
    nor has been relaxed (synthetic double-open without close still errors)."""

    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads(FROZEN_B01.read_text(encoding="utf-8"))
        cls.report = check_dispatch(
            payload,
            source_path=FROZEN_B01,
            artifact_root=REPO_ROOT,
        )

    def test_no_lid_state_conflict_on_b01_chain(self):
        findings = [f for f in self.report["findings"] if f["code"] == "lid_state_conflict"]
        assert findings == []
        # steps 12/72 evaluate clean because 批量加液 declares lid output 有盖
        # (Cleaning_and_Dispensing_Workstation_V1 SKILL), which the outdated
        # frozen validator segment never propagated.
        twelve = [
            f
            for f in self.report["findings"]
            if f.get("step_number") in (12, 72) and f["severity"] == "error"
        ]
        assert twelve == []

    def test_double_open_without_close_still_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "catalog"
            _write_station(
                root,
                "Fixture_Open",
                301,
                "开盖",
                "## 输入输出约束\n\n- **输入约束**\n  - 容器状态：有盖\n"
                "- **输出约束**\n  - 容器状态：无盖\n",
            )
            (root / "0410数据转换.txt").write_text(
                json.dumps({"steps": [_export_step("Fixture_Open", "开盖")]}, ensure_ascii=False),
                encoding="utf-8",
            )
            def open_step(number: int) -> dict[str, Any]:
                return {
                    "step_number": number,
                    "workstation": "Fixture_Open",
                    "operation": "开盖",
                    "id": 0,
                    "parameters": {"容器类型": "进样瓶", "容器数量": 1, "容器编号": [1]},
                }
            payload = {"status": "success", "workflow_json": {"steps": [open_step(1), open_step(2)]}}
            report = check_dispatch(
                payload,
                workstation_root=root,
                initial_state={
                    "containers": [
                        {"container_type": "进样瓶", "container_id": 1, "lid_state": "有盖"}
                    ]
                },
            )
            findings = [f for f in report["findings"] if f["code"] == "lid_state_conflict"]
            assert len(findings) == 1
            assert findings[0]["severity"] == "error"
            assert findings[0]["step_number"] == 2


if __name__ == "__main__":
    unittest.main()
