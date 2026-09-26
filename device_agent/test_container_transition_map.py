"""container_transition_map: evidence-based internal container-transition checks.

The annotation pairs a step's own source container ids (parameters.容器编号)
with target container ids of the Skill-declared output kind. It is logical-layer
traceability only: it proves the internal mapping is complete, never the
platform wire mapping. These tests call only the local deterministic checker.
"""

from __future__ import annotations

import copy

from device_agent.dispatch_checker import check_dispatch
from device_agent.test_dispatch_checker_real_contract import CONTRACT_ROOT
from device_agent.test_dispatch_checker_cross_source import package

STATION = "固体样品转移工作站_V1"
OPERATION = "固体样品转移"


def transfer_workflow(ids=(1, 2), annotation=None):
    step = {
        "step_number": 1,
        "workstation": STATION,
        "operation": OPERATION,
        "parameters": {"容器类型": "进样瓶", "容器数量": len(ids), "容器编号": list(ids)},
    }
    if annotation is not None:
        step["container_transition_map"] = annotation
    return {"steps": [step]}


def valid_map(ids=(1, 2)):
    return {
        "source_kind": "进样瓶",
        "target_kind": "料斗",
        "pairs": [{"source_id": i, "target_id": i} for i in ids],
    }


def checked(value, **kwargs):
    original = copy.deepcopy(value)
    report = check_dispatch(value, workstation_root=CONTRACT_ROOT, **kwargs)
    assert value == original, "Checking must not mutate the input"
    assert not any(f["code"].endswith("internal_error") for f in report["findings"])
    return report


def codes(report):
    return [f["code"] for f in report["findings"]]


def test_missing_map_keeps_transition_unverified():
    report = checked(transfer_workflow(), require_payload=False)
    finding = next(f for f in report["findings"] if f["code"] == "container_transition_unverified")
    assert finding["severity"] == "unverified"
    assert finding["expected"] == "料斗"
    assert finding["actual"] == "进样瓶"


def test_valid_map_clears_transition_finding():
    report = checked(transfer_workflow(annotation=valid_map()), require_payload=False)
    assert "container_transition_unverified" not in codes(report)


def test_incomplete_map_is_error_and_keeps_unverified():
    bad = valid_map(ids=(1,))  # step handles [1, 2] but map covers only 1
    report = checked(transfer_workflow(annotation=bad), require_payload=False)
    assert "container_transition_map_incomplete" in codes(report)
    assert "container_transition_unverified" in codes(report)


def test_wrong_source_kind_is_rejected():
    bad = valid_map()
    bad["source_kind"] = "西林瓶"
    report = checked(transfer_workflow(annotation=bad), require_payload=False)
    assert "container_transition_map_invalid" in codes(report)
    assert "container_transition_unverified" in codes(report)


def test_wrong_target_kind_is_rejected():
    bad = valid_map()
    bad["target_kind"] = "试管"
    report = checked(transfer_workflow(annotation=bad), require_payload=False)
    assert "container_transition_map_invalid" in codes(report)
    assert "container_transition_unverified" in codes(report)


def test_merge_transition_allowed_with_evidence():
    # 多对一合批（同组成两瓶并入同一料斗）由计划证据声明时，映射如实承载，不得拒绝。
    bad = valid_map()
    bad["pairs"] = [{"source_id": 1, "target_id": 1}, {"source_id": 2, "target_id": 1}]
    bad["evidence"] = ["plan key_values: 保证两瓶同组成固体汇于同一料斗"]
    report = checked(transfer_workflow(annotation=bad), require_payload=False)
    assert "container_transition_map_invalid" not in codes(report)
    assert "container_transition_unverified" not in codes(report)


def test_non_positive_ids_are_rejected():
    bad = valid_map()
    bad["pairs"] = [{"source_id": 1, "target_id": 0}]
    report = checked(transfer_workflow(ids=(1,), annotation=bad), require_payload=False)
    assert "container_transition_map_invalid" in codes(report)


def test_non_object_map_is_rejected():
    report = checked(transfer_workflow(annotation="进样瓶->料斗"), require_payload=False)
    assert "container_transition_map_invalid" in codes(report)


def test_downstream_hopper_step_is_not_blocked_by_transfer():
    value = transfer_workflow(ids=(1,), annotation=valid_map(ids=(1,)))
    value["steps"].append({
        "step_number": 2,
        "workstation": "Multi_Channel_Solid_Weighing_Workstation_V2",
        "operation": "固体进样-文件传参-机器人",
        "parameters": {"容器类型": "料斗", "容器数量": 1, "容器编号": [1]},
    })
    report = checked(value, require_payload=False)
    assert "container_transition_unverified" not in codes(report)
    blocked = [f for f in report["findings"] if f["code"] == "blocked_by_previous_step"]
    assert [f["step_number"] for f in blocked] == [], report["findings"]


def test_wire_delta_groups_under_missing_contract_root_cause():
    value = package(transfer_workflow(ids=(1,), annotation=valid_map(ids=(1,))))
    # Simulate a stored payload produced by an older whitelist formatter: the
    # passthrough preview keeps fields the stored payload never carried.
    stored_step = value["dispatch_payload"]["experiment_steps"]["steps"][0]
    for internal in ("notes", "container_transition_map"):
        stored_step.pop(internal, None)
    report = checked(value, require_payload=True)
    root = next(f for f in report["findings"] if f["code"] == "dispatch_wire_station_unverified")
    delta = root.get("wire_field_delta") or {}
    assert "container_transition_map" in delta.get("omitted_from_payload", [])
    assert "dispatch_value_missing" not in codes(report)


def test_dosing_plan_multi_reagent_per_bottle_matches_by_set():
    # 每瓶多路物料（Ni+Mo 各一行）是合法形状；按目标瓶集合与容器编号集合一致性判定。
    value = transfer_workflow(ids=(1, 2), annotation=None)
    value["steps"][0]["workstation"] = "Liquid_Handling_Station_5ml_V1"
    value["steps"][0]["operation"] = "加液_物料绑定"
    value["steps"][0]["parameters"] = {
        "容器类型": "进样瓶", "容器数量": 2, "容器编号": [1, 2],
        "加样方案": [
            {"加样瓶号": "1", "1号原液瓶": {"配料名称": "Ni", "原液用量": 4.0}},
            {"加样瓶号": "1", "2号原液瓶": {"配料名称": "Mo", "原液用量": 4.0}},
            {"加样瓶号": "2", "1号原液瓶": {"配料名称": "Ni", "原液用量": 4.0}},
            {"加样瓶号": "2", "2号原液瓶": {"配料名称": "Mo", "原液用量": 4.0}},
        ],
    }
    report = checked(value, require_payload=False)
    assert "container_target_mismatch" not in codes(report)


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
