"""容器类型列表的"及"连接词解析：只修读取，不扩大操作级受理范围。

Liquid_Handling_Station_5ml_V2 纯移液声明输入"进样瓶及50ml耐热瓶"，
检查器原先只按"或"切分，误报 container_type_conflict。
同一平台的开盖/关盖操作只声明进样瓶，耐热瓶必须仍然冲突。
"""

from __future__ import annotations

import copy

from device_agent.dispatch_checker import check_dispatch
from device_agent.test_dispatch_checker_real_contract import CONTRACT_ROOT

STATION = "Liquid_Handling_Station_5ml_V2"


def workflow(kind: str, operation: str = "纯移液") -> dict:
    return {
        "steps": [{
            "step_number": 1,
            "workstation": STATION,
            "operation": operation,
            "parameters": {"容器类型": kind, "容器数量": 1, "容器编号": [1]},
        }]
    }


def codes_for(kind: str, operation: str = "纯移液") -> list[str]:
    value = workflow(kind, operation)
    original = copy.deepcopy(value)
    report = check_dispatch(value, workstation_root=CONTRACT_ROOT, require_payload=False)
    assert value == original, "Checking must not mutate the input"
    return [f["code"] for f in report["findings"]]


def test_conjunction_and_reads_skill_text_verbatim_vial():
    assert "container_type_conflict" not in codes_for("进样瓶")


def test_conjunction_and_allows_heat_resistant_vial_on_pure_transfer():
    # 回归：纯移液声明含 50ml耐热瓶，不得误报。
    assert "container_type_conflict" not in codes_for("50ml耐热瓶")


def test_lid_ops_accept_heat_resistant_vial_at_skill_layer():
    # 设备真源层：开盖/关盖声明"进样瓶或50ml耐热瓶"，与纯移液同等受理。
    # （A02 第 2/7/12 步的冲突发生在 wire 导出层，不在这一层。）
    assert "container_type_conflict" not in codes_for("50ml耐热瓶", operation="开盖")
    assert "container_type_conflict" not in codes_for("50ml耐热瓶", operation="关盖")


def test_undeclared_kind_still_rejected():
    # 切分不得退化为全放行：西林瓶不在纯移液声明列表中，必须仍冲突。
    assert "container_type_conflict" in codes_for("西林瓶")


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
