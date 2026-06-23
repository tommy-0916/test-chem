"""Reverse workstation workflow JSON into readable experiment steps.

This module handles both converted workflow files with nodeMap/path/dataMap and
the database-style ``data`` column payloads that contain a ``dataList``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import unquote


CONTROL_WORKSTATION_TYPES = {"start", "end", "lock", "unlock"}
CONTROL_WORKSTATION_NAMES = {"开始", "结束", "加锁", "解锁"}

ACTION_NAME_ALIASES = {
    "add_liquid": "加液",
    "add_liquid_for_bos": "加液",
    "calc_loop_count_for_bos": "计算循环次数",
    "common": "子流程",
    "contactAngleRiseAndFallSecondHalf": "接触角升降测试后半段",
    "create_batch": "创建批次",
    "create_method": "创建方法",
    "default": "默认流程",
    "enter_sample_info": "录入样品信息",
    "EntrySampleList": "录入样品列表",
    "extraction": "配方提取",
    "llm_optimize": "LLM优化",
    "measuringAdhesion2": "粘附力测量",
    "measuringFirstHalf": "测量前半段",
    "result_for_bos": "输出结果",
    "rotate": "旋转",
    "run": "运行",
    "selection": "配方选择",
    "send_sample_info": "发送样品信息",
    "sleep": "等待",
    "solid_feeding_scheme": "固体加样方案",
    "start": "启动",
    "start_check": "开始检测",
    "start_heating_and_stirring": "加热搅拌",
    "start_llm_task": "启动LLM任务",
    "takerPaperPosition": "取纸位置",
    "test_all": "完整测试",
    "test_liquid_for_bos": "液体测试",
    "to_LC": "送入液相色谱",
}

PIPELINE_NAME_ALIASES = {
    "common_take_flow": "物料拿取",
    "multi_add_flow": "加液",
    "solid_add_flow": "固体加样",
    "new_pure_flow": "离心",
    "put_flow": "物料放置",
}

DEFAULT_OPERATION_BY_WORKSTATION = {
    "物料站": "物料拿取",
    "液体进样站": "加液",
    "移液工作站": "移液",
    "移液系统工作站": "移液",
    "固体进样工作站": "固体加样",
    "固体进样器": "固体加样",
    "磁力搅拌工作站": "磁力搅拌",
    "磁力搅拌": "磁力搅拌",
    "烘干机": "静置烘干",
    "纯化工作站": "离心",
    "超声清洗": "超声清洗",
    "置物工作站": "物料放置",
}


def parse_jsonish(value: Any) -> Any:
    """Parse JSON-like parameter values without turning ordinary strings into numbers."""

    if isinstance(value, (dict, list, tuple)):
        return _decode_strings(value)
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text == "":
        return ""

    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None

    if text[0] in "[{\"":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return unquote(text)
        return _decode_strings(parsed)

    return unquote(text)


def reverse_workflow_data(payload: Any) -> Dict[str, Any]:
    """Return structured steps and formatted text from a converted workflow payload."""

    workflow = _coerce_payload(payload)
    nodes = list(_iter_workstation_nodes(workflow))
    steps = [reverse_step(node, index + 1) for index, node in enumerate(nodes)]
    return {
        "steps": steps,
        "workflow_text": format_workflow_text(steps),
        "unknown_steps": workflow.get("unknown_steps"),
    }


def reverse_to_workflow_json(payload: Any, default_step_id: Optional[Any] = None) -> Dict[str, Any]:
    """Return the canonical workflow_json shape used by the forward converter."""

    workflow = _coerce_payload(payload)
    steps: List[Dict[str, Any]] = []
    for node in _iter_workstation_nodes(workflow):
        steps.extend(_canonical_steps_from_node(node, default_step_id))

    for index, step in enumerate(steps, start=1):
        step["step_number"] = index

    return {
        "steps": steps,
        "unknown_steps": workflow.get("unknown_steps"),
    }


def reverse_step(node: Mapping[str, Any], step_number: int) -> Dict[str, Any]:
    """Reverse one workstation node into one readable step."""

    workstation = _workstation_name(node)
    actions = _action_details(node)
    container_fields = _container_fields(node)

    if actions:
        operation = "、".join(_unique(action["operation"] for action in actions))
    else:
        operation = _operation_from_pipeline_or_workstation(
            node.get("pipeline"), workstation
        )

    if len(actions) == 1:
        action_parameters = actions[0]["parameters"]
        parameters: "OrderedDict[str, Any]" = OrderedDict()
        _merge_container_fields(parameters, container_fields, lookahead=action_parameters)
        for key, value in action_parameters.items():
            parameters[key] = value
    elif actions:
        parameters = OrderedDict()
        _merge_container_fields(parameters, container_fields)
        parameters["动作列表"] = [
            {
                "操作": action["operation"],
                "actionCode": action["actionCode"],
                "key": action["key"],
                "parameters": action["parameters"],
            }
            for action in actions
        ]
    else:
        parameters = OrderedDict()
        _merge_container_fields(parameters, container_fields)

    output: Dict[str, Any] = {
        "step_number": step_number,
        "workstation": workstation,
        "operation": operation,
        "parameters": parameters,
    }

    for key in ("dataId", "workstationType", "pipeline", "definitionVersion"):
        if node.get(key) not in (None, ""):
            output[key] = node.get(key)

    return output


def format_workflow_text(steps: Sequence[Mapping[str, Any]]) -> str:
    """Format reversed steps as the text style used by the prompt examples."""

    blocks: List[str] = []
    for step in steps:
        step_number = step.get("step_number")
        workstation = step.get("workstation", "未知工作站")
        operation = step.get("operation", "未知操作")
        lines = [f"{step_number}. 第{step_number}步 {workstation}："]
        lines.append(f"   - 操作：{operation}")
        lines.extend(_format_parameter_lines(step.get("parameters", {}), "   "))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def reverse_json_file(input_path: Path) -> Dict[str, Any]:
    with input_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return reverse_workflow_data(payload)


def reverse_excel_workbook(
    input_path: Path,
    sheets: Optional[Sequence[str]] = None,
    data_column: str = "data",
    shape: str = "readable",
) -> Dict[str, Any]:
    """Reverse the data column in workbook experiment sheets."""

    import pandas as pd

    excel = pd.ExcelFile(input_path)
    selected_sheets = list(sheets) if sheets else [
        sheet for sheet in excel.sheet_names if sheet.endswith("实验数据")
    ]

    output: Dict[str, Any] = {}
    for sheet in selected_sheets:
        df = pd.read_excel(
            input_path,
            sheet_name=sheet,
            dtype=object,
            keep_default_na=False,
        )
        if data_column not in df.columns:
            raise KeyError(f"Sheet {sheet!r} does not contain column {data_column!r}")

        records = []
        for _, row in df.iterrows():
            record = _row_metadata(row)
            if shape == "workflow_json":
                record["workflow_json"] = reverse_to_workflow_json(
                    row[data_column],
                    default_step_id=record.get("id"),
                )
                record["workflow_text"] = format_workflow_text(
                    record["workflow_json"]["steps"]
                )
            else:
                record["natural_workflow"] = reverse_workflow_data(row[data_column])
            records.append(record)
        output[sheet] = records
    return output


def write_json(output: Any, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def _coerce_payload(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return {}
        return json.loads(text)
    raise TypeError(f"Unsupported workflow payload type: {type(payload).__name__}")


def _iter_workstation_nodes(workflow: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    if isinstance(workflow.get("dataList"), list):
        for item in workflow["dataList"]:
            if _is_workstation_node(item):
                yield item
        return

    if isinstance(workflow.get("dataMap"), Mapping):
        yielded_data_ids: Set[str] = set()
        for node in _iter_nodes_by_path(workflow):
            data_id = node.get("dataId")
            data = workflow["dataMap"].get(data_id)
            if _is_workstation_node(data):
                yielded_data_ids.add(data_id)
                yield data

        for data_id, data in workflow["dataMap"].items():
            if data_id not in yielded_data_ids and _is_workstation_node(data):
                yield data
        return

    if isinstance(workflow, list):
        for item in workflow:
            if _is_workstation_node(item):
                yield item


def _iter_nodes_by_path(workflow: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    node_map = workflow.get("nodeMap") or {}
    path = workflow.get("path") or {}
    current = workflow.get("startNodeId") or "开始节点id"
    end_node = workflow.get("endNodeId") or "结束节点id"
    seen: Set[str] = set()

    while current in path and current not in seen:
        seen.add(current)
        current = path[current]
        if current == end_node:
            break
        node = node_map.get(current)
        if isinstance(node, Mapping):
            yield node


def _is_workstation_node(node: Any) -> bool:
    if not isinstance(node, Mapping):
        return False
    if node.get("stepType") != "workstation":
        return False
    workstation_type = str(node.get("workstationType") or "")
    workstation_name = str(node.get("workstationTypeName") or "")
    if workstation_type in CONTROL_WORKSTATION_TYPES:
        return False
    if workstation_name in CONTROL_WORKSTATION_NAMES:
        return False
    return True


def _workstation_name(node: Mapping[str, Any]) -> str:
    return str(
        node.get("workstationTypeName")
        or node.get("workstationName")
        or node.get("workstationType")
        or "未知工作站"
    )


def _action_details(node: Mapping[str, Any]) -> List[Dict[str, Any]]:
    details = []
    for action in node.get("actionParams") or []:
        if not isinstance(action, Mapping):
            continue
        action_code = str(action.get("actionCode") or action.get("key") or "")
        key = str(action.get("key") or action_code)
        operation = _friendly_action_name(action_code, key)
        details.append(
            {
                "actionCode": action_code,
                "key": key,
                "operation": operation,
                "parameters": _params_to_ordered_dict(action.get("params") or []),
            }
        )
    return details


def _canonical_steps_from_node(
    node: Mapping[str, Any],
    default_step_id: Optional[Any],
) -> List[Dict[str, Any]]:
    workstation = _workstation_name(node)
    actions = _action_details(node)
    container_fields = _container_fields(node)

    if not actions:
        parameters: "OrderedDict[str, Any]" = OrderedDict()
        _merge_platform_field(parameters, node)
        _merge_container_fields(parameters, container_fields)
        return [
            _canonical_step(
                step_number=0,
                workstation=workstation,
                operation=_operation_from_pipeline_or_workstation(
                    node.get("pipeline"), workstation
                ),
                default_step_id=default_step_id,
                parameters=parameters,
            )
        ]

    steps = []
    for action in actions:
        parameters = OrderedDict()
        _merge_platform_field(parameters, node)
        _merge_container_fields(
            parameters,
            container_fields,
            lookahead=action["parameters"],
        )
        for key, value in action["parameters"].items():
            parameters[key] = value
        steps.append(
            _canonical_step(
                step_number=0,
                workstation=workstation,
                operation=action["operation"],
                default_step_id=default_step_id,
                parameters=parameters,
            )
        )
    return steps


def _canonical_step(
    step_number: int,
    workstation: str,
    operation: str,
    default_step_id: Optional[Any],
    parameters: Mapping[str, Any],
) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "step_number": step_number,
        "workstation": workstation,
        "operation": operation,
    }
    if default_step_id not in (None, ""):
        output["id"] = _stringify_excel_value(default_step_id)
    output["parameters"] = dict(parameters)
    return output


def _params_to_ordered_dict(params: Sequence[Any]) -> "OrderedDict[str, Any]":
    output: "OrderedDict[str, Any]" = OrderedDict()
    for index, param in enumerate(params, start=1):
        if not isinstance(param, Mapping):
            continue
        key = str(param.get("paramCode") or param.get("paramName") or f"param_{index}")
        value = parse_jsonish(param.get("value"))
        _set_or_append(output, key, value)
    return output


def _container_fields(node: Mapping[str, Any]) -> "OrderedDict[str, Any]":
    container = node.get("container") or {}
    allocations = container.get("allocation") if isinstance(container, Mapping) else []
    allocation_rows = []

    for allocation in allocations or []:
        if not isinstance(allocation, Mapping):
            continue
        selected_ids = _selected_container_ids(allocation)
        count = allocation.get("containerCount")
        type_name = allocation.get("containerTypeName") or allocation.get("containerTypeCode")
        if not selected_ids and _is_zeroish(count):
            continue
        allocation_rows.append(
            {
                "容器类型": type_name,
                "容器编号": selected_ids,
                "数量": count,
                "分配方式": allocation.get("allocateMode"),
            }
        )

    fields: "OrderedDict[str, Any]" = OrderedDict()
    primary = next(
        (item for item in allocation_rows if item.get("容器编号")),
        allocation_rows[0] if allocation_rows else None,
    )
    if primary:
        fields["容器类型"] = primary.get("容器类型")
        if primary.get("容器编号"):
            fields["容器编号"] = primary.get("容器编号")
    if len(allocation_rows) > 1:
        fields["容器分配"] = allocation_rows
    return fields


def _selected_container_ids(allocation: Mapping[str, Any]) -> List[Any]:
    selected = allocation.get("selectedContainers") or []
    ids = []
    for item in selected:
        if not isinstance(item, Mapping):
            continue
        if item.get("selected", True):
            ids.append(item.get("logicNo"))
    return ids


def _merge_container_fields(
    parameters: "OrderedDict[str, Any]",
    container_fields: Mapping[str, Any],
    lookahead: Optional[Mapping[str, Any]] = None,
) -> None:
    if not container_fields:
        return
    existing_keys = set(parameters)
    if lookahead:
        existing_keys.update(lookahead)

    if "容器类型" in container_fields and "容器类型" not in existing_keys:
        parameters["容器类型"] = container_fields["容器类型"]
    has_container_number = any(
        key in existing_keys for key in ("容器编号", "进样瓶编号", "瓶号")
    )
    if not has_container_number and "容器编号" in container_fields:
        parameters["容器编号"] = container_fields["容器编号"]
    if "容器分配" in container_fields and "容器分配" not in parameters:
        parameters["容器分配"] = container_fields["容器分配"]


def _merge_platform_field(
    parameters: "OrderedDict[str, Any]",
    node: Mapping[str, Any],
) -> None:
    workstation = _workstation_name(node)
    platform = node.get("definitionVersion") or node.get("workstationModelName")
    if workstation in {"液体进样站", "移液工作站", "移液系统工作站"} and platform:
        parameters["平台"] = platform


def _friendly_action_name(action_code: str, key: str) -> str:
    candidate = action_code or key
    if not candidate:
        return "默认流程"
    if _contains_cjk(candidate):
        return candidate
    return ACTION_NAME_ALIASES.get(candidate, candidate)


def _operation_from_pipeline_or_workstation(pipeline: Any, workstation: str) -> str:
    pipeline_text = str(pipeline or "")
    if pipeline_text in PIPELINE_NAME_ALIASES:
        return PIPELINE_NAME_ALIASES[pipeline_text]
    if workstation in DEFAULT_OPERATION_BY_WORKSTATION:
        return DEFAULT_OPERATION_BY_WORKSTATION[workstation]
    return pipeline_text or "默认流程"


def _is_zeroish(value: Any) -> bool:
    return value in (None, "", 0, "0", 0.0, "0.0")


def _format_parameter_lines(params: Any, indent: str) -> List[str]:
    lines: List[str] = []
    if not isinstance(params, Mapping):
        return lines

    for key, value in params.items():
        if key == "动作列表" and isinstance(value, list):
            lines.append(f"{indent}- 动作列表：")
            for action in value:
                operation = action.get("操作", "未知动作")
                lines.append(f"{indent}  - {operation}：")
                action_params = action.get("parameters") or {}
                for sub_key, sub_value in action_params.items():
                    lines.append(
                        f"{indent}      - {sub_key}：{_format_value(sub_value)}"
                    )
            continue
        lines.append(f"{indent}- {key}：{_format_value(value)}")
    return lines


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "无"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _row_metadata(row: Any) -> Dict[str, Any]:
    metadata = OrderedDict()
    for key in ("id", "task_name", "create_user_name"):
        if key in row:
            metadata[key] = _stringify_excel_value(row[key])
    return metadata


def _stringify_excel_value(value: Any) -> Any:
    if value is None:
        return ""
    text = str(value)
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def _decode_strings(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _decode_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_strings(item) for item in value]
    if isinstance(value, tuple):
        return [_decode_strings(item) for item in value]
    if isinstance(value, str):
        return unquote(value)
    return value


def _set_or_append(output: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    if key not in output:
        output[key] = value
        return
    if output[key] == value:
        return
    if not isinstance(output[key], list):
        output[key] = [output[key]]
    output[key].append(value)


def _unique(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    output = []
    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)
    return output


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reverse converted workstation workflow JSON/data columns into readable steps."
    )
    parser.add_argument("input", type=Path, help="Input .json or .xlsx file")
    parser.add_argument("-o", "--output", type=Path, help="Output JSON path")
    parser.add_argument(
        "--sheets",
        nargs="+",
        help="Workbook sheets to process. Defaults to sheets ending with 实验数据.",
    )
    parser.add_argument(
        "--data-column",
        default="data",
        help="Workbook column that stores converted workstation JSON.",
    )
    parser.add_argument(
        "--shape",
        choices=("readable", "workflow_json"),
        default="workflow_json",
        help="Output shape for workbooks. workflow_json matches the forward-converter schema.",
    )
    args = parser.parse_args()

    input_path = args.input
    if input_path.suffix.lower() in {".xlsx", ".xls"}:
        result = reverse_excel_workbook(
            input_path,
            sheets=args.sheets,
            data_column=args.data_column,
            shape=args.shape,
        )
        suffix = "workflow_json反转换" if args.shape == "workflow_json" else "自然语言反转换"
        default_output = input_path.with_name(f"{input_path.stem}_{suffix}.json")
    else:
        if args.shape == "workflow_json":
            with input_path.open("r", encoding="utf-8") as f:
                result = {"workflow_json": reverse_to_workflow_json(json.load(f))}
        else:
            result = reverse_json_file(input_path)
        suffix = "workflow_json_reversed" if args.shape == "workflow_json" else "reversed"
        default_output = input_path.with_name(f"{input_path.stem}_{suffix}.json")

    output_path = args.output or default_output
    write_json(result, output_path)
    print(str(output_path))


if __name__ == "__main__":
    main()
