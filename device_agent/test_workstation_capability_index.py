"""Regression checks for the generated Research-facing workstation index."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reaserch_agent.tools.device_context import (  # noqa: E402
    load_device_context,
    load_device_context_from_path,
)


INDEX_PATH = REPO_ROOT / "chem_resources" / "workstation_capability_index.json"


def _load() -> dict:
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def test_index_uses_lab_design_all_and_covers_all_stations() -> None:
    data = _load()
    assert data["source_kind"] == "lab-design-all"
    assert "/lab-design-all/" in f"/{data['source']}/"
    assert data["workstation_count"] == 45
    assert len(data["workstations"]) == 45
    assert len({item["station_code"] for item in data["workstations"]}) == 45


def test_index_has_operations_and_container_evidence() -> None:
    data = _load()
    assert data["operation_count"] >= 45
    assert data["container_count"] == len(data["container_list"])
    assert data["container_list"] == data["containers"]
    assert all(item["capabilities"] for item in data["workstations"])
    assert all(
        item["input_containers"] or item["parameter_container_options"]
        for item in data["workstations"]
    )
    assert all(item["output_containers"] for item in data["workstations"])
    assert "西林瓶" in data["containers"]
    assert "50ml耐热瓶" in data["containers"]
    assert "10ml耐压反应管" in data["containers"]
    assert "XRD基底片" in data["sample_carrier_list"]
    assert all(
        side["containers"] or all("与输入保持一致" in raw for raw in side["container_type_raw"])
        for station in data["workstations"]
        for operation in station["operations"]
        for side in (operation["input"], operation["output"])
        if side["container_type_raw"]
    )


def test_compact_research_context_is_complete_and_bounded() -> None:
    data = _load()
    text = data["research_context"]["text"]
    assert data["research_context"]["char_count"] == len(text)
    assert len(text.splitlines()) == 45
    assert len(text) <= 10000
    assert data["schema_version"] == "2.0"
    assert "Ctl" in data["research_context"]["semantics"]
    assert "Qout" in data["research_context"]["semantics"]
    assert "Report" in data["research_context"]["semantics"]
    assert "CReq" in data["research_context"]["semantics"]


def test_compact_index_separates_setpoints_material_output_and_feedback() -> None:
    data = _load()
    by_code = {item["station_code"]: item for item in data["workstations"]}

    drying = by_code["Drying_Oven_V1"]
    drying_operation = drying["operations"][0]
    assert drying_operation["quantity_semantics"]["target_setpoints"] == []
    assert drying_operation["quantity_semantics"]["reported_measurements"] == []
    assert drying_operation["quantity_semantics"]["material_output_quantity_mode"] == "whole_batch"
    assert drying_operation["quantity_semantics"]["can_report_actual_inventory"] is False
    assert drying_operation["container_contract"]["output_count_relation"] == "same_as_input_count"
    assert drying_operation["container_contract"]["accepted_input_containers"] == [
        "进样瓶",
        "50ml耐热瓶",
    ]
    assert "整批处理;不要求预知总量" in drying["research_summary"]
    assert "CReq=" in drying["research_summary"]

    weighing = by_code["Single_Channel_Solid_Weighing_Workstation_V1"]
    weighing_operation = weighing["operations"][0]
    assert weighing_operation["quantity_semantics"]["target_setpoints"] == [
        {"name": "进样质量", "unit": "g", "range": "(0,200)"}
    ]
    assert weighing_operation["quantity_semantics"]["reported_measurements"] == []
    assert weighing_operation["quantity_semantics"]["material_output_quantity_mode"] == "target_setpoint"
    assert "进样质量(0,200)g" in weighing["research_summary"]
    assert "20 mg" not in weighing["research_summary"]

    xrd = by_code["XRD_V1"]
    assert "总液量" in " ".join(xrd["critical_constraints"])
    assert "4.0 mL 无水乙醇" in " ".join(xrd["critical_constraints"])
    assert not any(
        parameter["name"] in {"粉末质量", "样品质量"}
        for parameter in xrd["operations"][0]["parameter_contracts"]
    )
    assert all(
        "parameter_contracts" in operation
        and "container_count_constraints" in operation
        and "quantity_semantics" in operation
        for station in data["workstations"]
        for operation in station["operations"]
    )


def test_cleaning_station_operation_matches_its_parameter_contract() -> None:
    data = _load()
    station = next(
        item
        for item in data["workstations"]
        if item["station_code"] == "Cleaning_and_Dispensing_Workstation_V1"
    )
    assert station["capabilities"] == ["批量加液流程"]
    operation = station["operations"][0]
    assert operation["name"] == "批量加液流程"
    skill_text = (REPO_ROOT / station["source"]["skill"]).read_text(encoding="utf-8")
    assert skill_text.count("**批量加液流程**") >= 2
    for parameter in ("容器类型", "容器数量", "容器编号", "加液参数"):
        assert f"| {parameter}" in skill_text


def test_research_loader_uses_the_compact_index() -> None:
    context = load_device_context()
    direct_context = load_device_context_from_path(INDEX_PATH)
    assert context == direct_context
    assert context["source_kind"] == "lab-design-all capability index"
    assert len(context["workstations"]) == 45
    assert context["container_list"] == _load()["container_list"]
    assert context["sample_carrier_list"] == ["XRD基底片"]
    assert len(context["compact_workstation_capabilities"].splitlines()) == 45
    assert len(json.dumps(context, ensure_ascii=False)) <= 18000


if __name__ == "__main__":
    test_index_uses_lab_design_all_and_covers_all_stations()
    test_index_has_operations_and_container_evidence()
    test_compact_research_context_is_complete_and_bounded()
    test_compact_index_separates_setpoints_material_output_and_feedback()
    test_cleaning_station_operation_matches_its_parameter_contract()
    test_research_loader_uses_the_compact_index()
    print("workstation capability index tests passed")
