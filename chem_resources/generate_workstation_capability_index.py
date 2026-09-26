#!/usr/bin/env python3
"""Generate a compact Research-facing workstation capability index.

The authoritative source is the per-workstation ``SKILL.md`` collection.  The
index deliberately keeps operation-level input/output container constraints
separate from the usually broader ``容器类型`` parameter examples so Research
does not mistake a dispatch parameter enum for a valid material-flow edge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEVICE_AGENT_ROOT = REPO_ROOT / "device_agent"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(DEVICE_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEVICE_AGENT_ROOT))

from skill_contract_audit import (  # noqa: E402
    BOLD_BULLET_RE,
    OPERATION_HEADING_RE,
    STATION_ID_RE,
    STATION_NAME_RE,
    ParameterNode,
    parse_operation_schemas,
)
from agent_skills.capabilities import (  # noqa: E402
    PROJECTION_VERSION,
    REVIEWED_CAPABILITY_OPERATION_BINDINGS,
    REVIEWED_CAPABILITY_OPERATION_GAPS,
    SKILL_ROOT,
    TIER_SKILLS,
    extract_experiment_capabilities,
    project_device_context,
    semantic_mapping_digest,
    validated_capability_operation_bindings,
)


DEFAULT_SOURCE = (
    REPO_ROOT
    / "chem_resources"
    / "lab-design-all"
    / "skills"
    / "chemistry-experiment-workstation"
)
DEFAULT_OUTPUT = REPO_ROOT / "chem_resources" / "workstation_capability_index.json"
MODULES = (
    ("references-Synthesis-Module", "Synthesis Module"),
    ("references-Reaction-and-Testing-Module", "Reaction and Testing Module"),
    ("references-Characterization-Module", "Characterization Module"),
)
CONTAINER_NAMES = (
    "10ml耐压反应管",
    "50ml耐热瓶",
    "96位塑料孔板",
    "96位石英孔板",
    "液相54孔板",
    "气相54孔板",
    "色谱进样瓶",
    "XRD基底片",
    "进样瓶",
    "西林瓶",
    "留样瓶",
    "料斗",
    "碳纸架",
    "测试架",
)
CONSTRAINT_KEYS = ("容器类型", "容器状态", "样品状态", "模板名称")
LIMIT_MARKERS = ("[强约束]", "必须", "仅支持", "最大", "最高", "不得", "不超过")
QUANTITY_UNITS = {
    "g", "mg", "μg", "µg", "ug", "ml", "μl", "µl", "ul", "l",
}
RESOURCE_PARAMETER_MARKERS = (
    "容器类型", "容器数量", "容器编号", "样品载体类型", "样品载体数量", "样品载体编号",
)
DISPATCH_ONLY_PARAMETER_MARKERS = (
    "开门位置", "关门位置", "文件路径", "文件名称", "模板名称", "工作站编码",
)
MATERIAL_QUANTITY_ACTION_RE = re.compile(
    r"进样|加样|加液|移液|取液|倾倒|滴液|分装|投料|称量|转移|dispens|pipet|dose|transfer",
    re.IGNORECASE,
)
MEASUREMENT_SECTION_RE = re.compile(
    r"^#{2,4}\s*(?:输出数据|测量输出|数值反馈|返回结果|测量结果)\s*$"
)


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _normalize_container_text(text: str) -> str:
    return re.sub(r"50\s*m[lL]", "50ml", re.sub(r"10\s*m[lL]", "10ml", text or ""))


def _containers_from_text(text: str) -> list[str]:
    normalized = _normalize_container_text(text)
    # Longer names must win over substrings such as 色谱进样瓶 -> 进样瓶.
    ordered = sorted(CONTAINER_NAMES, key=len, reverse=True)
    matches: list[tuple[int, str]] = []
    for name in ordered:
        for match in re.finditer(re.escape(name), normalized, flags=re.IGNORECASE):
            start, end = match.span()
            if any(start >= old_start and end <= old_start + len(old_name) for old_start, old_name in matches):
                continue
            matches.append((start, name))
    return _unique(name for _, name in sorted(matches))


def _name_map(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    path = root / "工作站名称中英文对照.md"
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        code, display = (part.strip() for part in line.split("\t", 1))
        if code and display and code != "英文名" and set(code) != {"-"}:
            result[code] = display
    return result


def _description(content: str) -> str:
    match = re.search(r"^description:\s*(.+?)\s*$", content, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _audit_path(root: Path, station_code: str) -> Path | None:
    audit_dir = root / "references_audit"
    exact = audit_dir / f"{station_code}_audit.md"
    if exact.exists():
        return exact
    matches = sorted(audit_dir.glob(f"{station_code}_*.md")) if audit_dir.is_dir() else []
    return matches[0] if matches else None


def _new_io_side() -> dict[str, Any]:
    return {
        "container_type_raw": [],
        "containers": [],
        "container_states": [],
        "sample_states": [],
        "template_requirements": [],
        "same_as_input": False,
    }


def _operation_io_constraints(content: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    current_operation = ""
    current_section = ""
    current_side = ""

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        heading = re.match(r"^#{2,4}\s*(.+?)\s*$", stripped)
        if heading:
            current_section = heading.group(1).strip()
            if "输入输出约束" not in current_section:
                current_side = ""

        operation_match = OPERATION_HEADING_RE.match(stripped)
        if operation_match:
            current_operation = operation_match.group(1).strip()
            result.setdefault(
                current_operation,
                {"input": _new_io_side(), "output": _new_io_side()},
            )
            current_side = ""
        else:
            bullet_match = BOLD_BULLET_RE.match(stripped)
            bullet_name = bullet_match.group(1).strip() if bullet_match else ""
            if (
                bullet_name
                and current_section in {"操作", "输入输出约束"}
                and bullet_name not in {"输入约束", "输出约束"}
            ):
                current_operation = bullet_name
                result.setdefault(
                    current_operation,
                    {"input": _new_io_side(), "output": _new_io_side()},
                )
                current_side = ""

        if not current_operation:
            continue
        if "输入约束" in stripped:
            current_side = "input"
            continue
        if "输出约束" in stripped:
            current_side = "output"
            continue
        if not current_side or stripped.startswith("|"):
            continue

        clean = stripped.lstrip("- ").replace("**", "").strip()
        for key in CONSTRAINT_KEYS:
            match = re.match(rf"^{re.escape(key)}[：:]\s*(.+?)\s*$", clean)
            if not match:
                continue
            value = match.group(1).strip()
            side = result[current_operation][current_side]
            if key == "容器类型":
                side["container_type_raw"] = _unique(side["container_type_raw"] + [value])
                side["containers"] = _unique(
                    side["containers"] + _containers_from_text(value)
                )
                if "与输入保持一致" in value:
                    side["same_as_input"] = True
            elif key == "容器状态":
                side["container_states"] = _unique(side["container_states"] + [value])
            elif key == "样品状态":
                side["sample_states"] = _unique(side["sample_states"] + [value])
            else:
                side["template_requirements"] = _unique(
                    side["template_requirements"] + [value]
                )
            break

    return result


def _walk_parameter_nodes(nodes: Iterable[ParameterNode]) -> Iterable[ParameterNode]:
    for node in nodes:
        yield node
        yield from _walk_parameter_nodes(node.children)


def _parameter_container_options(operation: Any) -> list[str]:
    values: list[str] = []
    for node in _walk_parameter_nodes(operation.parameters.values()):
        if "容器类型" in node.name:
            values.extend(_containers_from_text(node.evidence_text))
    return _unique(values)


def _parameter_sample_carrier_options(operation: Any) -> list[str]:
    values: list[str] = []
    for node in _walk_parameter_nodes(operation.parameters.values()):
        if "载体类型" in node.name:
            values.extend(_containers_from_text(node.evidence_text))
    return _unique(values)


def _range_text(node: ParameterNode) -> str:
    # [1,2] in an array example is a list of selected vessels, not a numeric
    # range. Accept a scalar interval only when the example is exactly that
    # interval (or the remarks explicitly call it a range).
    if node.type_name.lower() in {"array", "object", "list", "file"} or "编号" in node.name:
        return ""
    interval = r"([\[(]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[\])])"
    match = re.fullmatch(interval, node.example.strip())
    if match is None and re.search(r"范围|区间|range", node.remark, re.IGNORECASE):
        match = re.search(interval, node.remark)
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def _parameter_role(node: ParameterNode, operation_name: str) -> str:
    if any(marker in node.name for marker in RESOURCE_PARAMETER_MARKERS):
        return "resource_requirement"
    if (
        node.type_name.lower() in {"file", "url"}
        or re.search(r"编号|瓶号|管号|孔位|孔板ID|位置|坐标|行号|文件(?:名|名称|路径)|照片名称|batchFile|模板|模版", node.name, re.IGNORECASE)
        or re.fullmatch(r"(?:N|\d+)号原液瓶|加样通道|是否开启|是否触发|第[一二三四\d]+排灯", node.name)
        or any(marker in node.name for marker in DISPATCH_ONLY_PARAMETER_MARKERS)
    ):
        return "dispatch_only"
    unit = node.unit.strip().lower()
    if unit in QUANTITY_UNITS or re.search(r"质量|重量|体积|用量|加料样", node.name):
        if MATERIAL_QUANTITY_ACTION_RE.search(f"{operation_name} {node.name} {node.remark}"):
            return "target_material_quantity"
        return "scientific_quantity_requirement"
    if node.type_name.lower() in {"object", "array", "list"}:
        return "parameter_group"
    return "process_control"


def _parameter_contracts(operation: Any) -> list[dict[str, Any]]:
    if operation is None:
        return []
    contracts: list[dict[str, Any]] = []
    for node in _walk_parameter_nodes(operation.parameters.values()):
        contracts.append(
            {
                "name": node.name,
                "required": bool(node.required),
                "unit": node.unit,
                "type": node.type_name,
                "range": _range_text(node),
                "role": _parameter_role(node, operation.name),
                "evidence": node.evidence_text,
                "source_line": node.line,
            }
        )
    return contracts


def _count_constraints(parameter_contracts: list[dict[str, Any]]) -> list[str]:
    constraints: list[str] = []
    for parameter in parameter_contracts:
        if parameter.get("name") not in {"容器数量", "样品载体数量"}:
            continue
        evidence = str(parameter.get("evidence") or "")
        for label, low, high in re.findall(
            r"([^;；,，\s]+?)最小\s*(\d+)\s*个?最大\s*(\d+)\s*个?",
            evidence,
        ):
            constraints.append(f"{label}:{low}..{high}")
        for parity in re.findall(r"(?:必须|须|要求)?\s*(偶数|奇数|基数)", evidence):
            constraints.append("数量=" + ("奇数" if parity == "基数" else parity))
        multiple = re.search(r"(?:必须|须|要求)?\s*(?:为|是)?\s*(\d+)\s*的倍数", evidence)
        if multiple:
            constraints.append(f"数量={multiple.group(1)}的倍数")
    return _unique(constraints)


def _reported_measurements(content: str) -> list[str]:
    """Extract only explicitly declared numeric feedback fields.

    Parameter tables are inputs/setpoints, never measurements.  A future Skill
    may add a dedicated output section; until then the safe value is an empty
    list, not an invented balance reading or yield.
    """
    fields: list[str] = []
    in_measurement_section = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            in_measurement_section = bool(MEASUREMENT_SECTION_RE.match(stripped))
            continue
        if not in_measurement_section:
            continue
        if stripped.startswith("|"):
            cells = [cell.strip().replace("**", "") for cell in stripped.split("|")[1:-1]]
            if cells and cells[0] and cells[0] not in {"字段", "字段名", "参数名"}:
                fields.append(cells[0])
        elif stripped.startswith("-"):
            value = stripped.lstrip("- ").strip()
            if value:
                fields.append(value.split("：", 1)[0].split(":", 1)[0])
    return _unique(fields)


def _operation_quantity_semantics(
    operation_name: str,
    output: dict[str, Any],
    parameter_contracts: list[dict[str, Any]],
    reported_measurements: list[str],
) -> dict[str, Any]:
    target_controls = [
        {"name": item["name"], "unit": item.get("unit", ""), "range": item.get("range", "")}
        for item in parameter_contracts
        if item.get("role") == "target_material_quantity"
    ]
    recipe_target_control = bool(
        re.search(r"固体进样|固体称量", operation_name)
        and any(
            item.get("required")
            and (
                str(item.get("type")) == "file"
                or "文件" in str(item.get("name"))
            )
            for item in parameter_contracts
        )
    )
    if recipe_target_control and not target_controls:
        target_controls.append(
            {
                "name": "上传文件",
                "unit": "",
                "range": "",
                "semantics": "recipe_defined_target_quantities",
            }
        )
    if target_controls:
        material_effect = "target_setpoint_applied_to_material_output"
    elif output.get("same_as_input"):
        material_effect = "whole_batch_processed_without_numeric_inventory_requirement"
    else:
        material_effect = "quantity_effect_not_declared"
    if target_controls:
        material_output_quantity_mode = "target_setpoint"
    elif output.get("same_as_input"):
        material_output_quantity_mode = "whole_batch"
    else:
        material_output_quantity_mode = "undeclared"
    return {
        "target_setpoints": target_controls,
        "material_output_effect": material_effect,
        "material_output_quantity_mode": material_output_quantity_mode,
        "reported_measurements": list(reported_measurements),
        "numeric_feedback_fields": list(reported_measurements),
        "actual_inventory_measurement_declared": bool(reported_measurements),
        "can_report_actual_inventory": bool(reported_measurements),
        "policy": (
            "A target setpoint is not an actual whole-batch inventory reading. "
            "An empty reported_measurements list means the Skill declares no numeric feedback."
        ),
    }


def _operation_container_contract(operation: dict[str, Any]) -> dict[str, Any]:
    """Return the compact, operation-level container truth Research needs.

    This is a projection of the immutable Skill contract, not a replacement
    parameter schema.  It keeps container type/state/count relations visible
    without copying dispatch-only fields into the Research prompt.
    """

    input_side = operation["input"]
    output_side = operation["output"]
    input_containers = input_side.get("containers") or operation.get(
        "container_parameter_options", []
    )
    output_containers = output_side.get("containers", [])
    if output_side.get("same_as_input"):
        output_containers = list(input_containers)
    return {
        "accepted_input_containers": list(input_containers),
        "input_container_states": list(input_side.get("container_states", [])),
        "input_sample_states": list(input_side.get("sample_states", [])),
        "input_count_constraints": list(
            operation.get("container_count_constraints", [])
        ),
        "output_containers": list(output_containers),
        "output_container_states": list(output_side.get("container_states", [])),
        "output_sample_states": list(output_side.get("sample_states", [])),
        "output_count_relation": (
            "same_as_input_count"
            if output_side.get("same_as_input")
            else "operation_declared"
        ),
        "sample_carriers": list(operation.get("sample_carrier_options", [])),
        "template_requirements": _unique(
            list(input_side.get("template_requirements", []))
            + list(output_side.get("template_requirements", []))
        ),
    }


def _compact_parameter(parameter: dict[str, Any]) -> str:
    return (
        f"{parameter['name']}{parameter.get('range', '')}{parameter.get('unit', '')}"
    )


def _operation_summary(operation: dict[str, Any]) -> str:
    input_side = operation["input"]
    output_side = operation["output"]
    input_containers = input_side.get("containers") or operation.get(
        "container_parameter_options", []
    )
    input_bits = ["/".join(input_containers) if input_containers else "未声明容器"]
    input_bits.extend(
        value for value in input_side.get("container_states", [])
        if value not in {"不限", "与输入保持一致"}
    )
    input_bits.extend(
        value for value in input_side.get("sample_states", [])
        if value not in {"不限", "与输入保持一致"}
    )
    counts = operation.get("container_count_constraints", [])
    if counts:
        input_bits.append("数量" + ",".join(counts))
    carriers = operation.get("sample_carrier_options", [])
    if carriers:
        input_bits.append("载体=" + "/".join(carriers))
    if output_side.get("same_as_input"):
        output_text = "同输入"
    else:
        output_text = "/".join(output_side.get("containers", [])) or "未声明"
    output_states = [
        value for value in output_side.get("sample_states", [])
        if value not in {"不限", "与输入保持一致"}
    ]
    output_states = [
        value for value in output_side.get("container_states", [])
        if value not in {"不限", "与输入保持一致"}
    ] + output_states
    if output_states:
        output_text += "/" + "/".join(output_states)
    controls = [
        item for item in operation.get("parameter_contracts", [])
        if item.get("required") and item.get("role") in {
            "target_material_quantity", "scientific_quantity_requirement", "process_control",
        }
    ]
    controls.sort(
        key=lambda item: (
            0 if item.get("role") == "target_material_quantity" else 1,
            str(item.get("name")),
        )
    )
    control_text = "/".join(_compact_parameter(item) for item in controls[:4]) or "无"
    quantity = operation["quantity_semantics"]
    if quantity["target_setpoints"]:
        quantity_output = (
            "按配方文件目标量输出"
            if any(
                item.get("semantics") == "recipe_defined_target_quantities"
                for item in quantity["target_setpoints"]
            )
            else "按目标设定量输出"
        )
    elif quantity["material_output_effect"].startswith("whole_batch"):
        quantity_output = "整批处理;不要求预知总量"
    else:
        quantity_output = "未声明"
    feedback = "/".join(quantity["reported_measurements"]) or "未声明"
    container_contract = operation["container_contract"]
    count_relation = container_contract["output_count_relation"]
    # Input count/parity is already present in I.  CReq only needs the output
    # cardinality relation, avoiding a near-duplicate of the full Skill table.
    container_requirement = (
        "I" if count_relation == "same_as_input_count" else "op"
    )
    return (
        f"{operation['name']}{{I={'/'.join(input_bits)};O={output_text};"
        f"CReq={container_requirement};Ctl={control_text};"
        f"Qout={quantity_output};Report={feedback}}}"
    )


def _critical_constraints(content: str, description: str) -> list[str]:
    constraints: list[str] = []
    prefix = content.split("## 操作", 1)[0]
    current_section = ""
    for raw_line in prefix.splitlines():
        stripped = raw_line.strip()
        heading = re.match(r"^#{2,4}\s*(.+?)\s*$", stripped)
        if heading:
            current_section = heading.group(1).strip()
            continue
        line = stripped.lstrip(">- ").strip()
        if not line or line == description or line.startswith("description:"):
            continue
        # Do not treat comparison prose about sibling stations as a constraint
        # of the current station.  Only explicit strong constraints or bullets
        # under a station-level constraint section are retained.
        in_constraint_section = any(
            marker in current_section for marker in ("整体流程约束", "重要说明", "限制", "约束")
        )
        if "[强约束]" in line or (
            in_constraint_section and any(marker in line for marker in LIMIT_MARKERS)
        ):
            constraints.append(re.sub(r"\s+", " ", line))
    return _unique(constraints)


def _evidence(path: Path, content: str, line: int, quote: str, section: str = "") -> dict[str, Any]:
    return {
        "path": _relative(path), "line": line, "section": section,
        "quote": quote, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _planning_constraints(content: str, path: Path, *, audit: bool = False, operation_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Keep complete planning sections, including conditional branch context.

    Audit parameter/id sections are machine-only. Other audit prose, explicit
    I/O and station-level process notes are never shortened for step planning.
    """
    result: list[dict[str, Any]] = []
    section = ""
    capture = False
    operation_names = operation_names or []
    current_operation = operation_names[0] if len(operation_names) == 1 else ""
    current_side = ""
    for line_number, raw in enumerate(content.splitlines(), 1):
        stripped = raw.strip()
        heading = re.match(r"^#{1,4}\s*(.+)$", stripped)
        if heading:
            section = heading.group(1)
            current_side = ""
            for operation in sorted(operation_names, key=len, reverse=True):
                if operation in section:
                    current_operation = operation
                    break
            capture = (
                audit and not re.search(r"工作站id|工作站名字|参数设置", section, re.IGNORECASE)
            ) or any(word in section for word in ("整体流程约束", "输入输出约束", "重要说明", "限制", "注意事项"))
            continue
        if not stripped or stripped.startswith("|") or stripped.startswith("```"):
            continue
        marker = stripped.lstrip("- ").replace("**", "")
        if marker in {"输入约束", "输出约束"}:
            current_side = "input" if marker == "输入约束" else "output"
            continue
        if capture and not re.fullmatch(r"\d+", stripped):
            evidence = _evidence(path, content, line_number, stripped, section)
            if current_operation:
                evidence["operation"] = current_operation
            if current_side:
                evidence["side"] = current_side
            result.append(evidence)
    return result


def _global_constraints(content: str, path: Path) -> list[dict[str, Any]]:
    # The source's narrative instruction blocks are not executable skill
    # instructions here. Preserve only the actual whole-experiment rules.
    start = content.find("   (1) **整体实验流程规则**")
    end = content.find("  (2) **打印要求**", start)
    if start < 0:
        return []
    prefix_lines = content[:start].count("\n")
    return [
        _evidence(path, content, prefix_lines + offset, line.strip(), "整体实验流程规则")
        for offset, line in enumerate(content[start:end if end >= 0 else None].splitlines(), 1)
        if line.strip() and "整体实验流程规则" not in line
    ]


def _feedback_contracts(content: str, path: Path, operation_names: list[str]) -> dict[str, dict[str, Any]]:
    """Recognize explicit feedback declarations, not control setpoints.

    A whole-station return section applies to an operation only when it is
    the sole operation. Otherwise it remains unknown rather than being copied
    onto every operation. Timing and numeric result fields are independent.
    """
    result = {
        name: {
            "returned_data": {"status": "unknown", "fields": [], "evidence": []},
            "intermediate_feedback": {"status": "unknown", "fields": [], "evidence": []},
        }
        for name in operation_names
    }
    current_operation = operation_names[0] if len(operation_names) == 1 else ""
    current_kind = ""
    for number, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        operation_match = OPERATION_HEADING_RE.match(stripped)
        bullet = BOLD_BULLET_RE.match(stripped)
        if operation_match or (bullet and bullet.group(1).strip() in result):
            current_operation = (operation_match or bullet).group(1).strip()
            current_kind = ""
            continue
        if stripped.startswith("#"):
            title = re.sub(r"^#+\s*", "", stripped)
            current_kind = (
                "intermediate_feedback" if re.search(r"中间(?:返回|反馈)|实时反馈|intermediate feedback", title, re.IGNORECASE)
                else "returned_data" if MEASUREMENT_SECTION_RE.match(stripped)
                else ""
            )
            continue
        if not current_kind or current_operation not in result or not stripped:
            continue
        target = result[current_operation][current_kind]
        if re.search(r"不支持|无中间|不返回|unsupported", stripped, re.IGNORECASE):
            target["status"] = "unsupported"
        elif re.search(r"未声明|未知|unknown", stripped, re.IGNORECASE):
            continue
        else:
            field = stripped.lstrip("- ").split("：", 1)[0].split(":", 1)[0]
            if stripped.startswith("|"):
                field = stripped.split("|")[1].strip()
                if not field or field in {"字段", "字段名", "参数名"} or re.fullmatch(r"[- :]+", field):
                    continue
            target["status"] = "supported"
            target["fields"] = _unique(target["fields"] + [field])
        target["evidence"].append(_evidence(path, content, number, stripped, current_kind))
    return result


def _dependencies(constraints: list[dict[str, Any]], stations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aliases = {
        "谱学置物": "Spectroscopy_Container_Transfer_Station_V1",
        "谱学磁力搅拌": "Spectroscopy_Magnetic_Stirrer_Workstation_V1",
    }
    for station in stations:
        for alias in station["aliases"]:
            aliases[alias] = station["station_code"]
    result = []
    for evidence in constraints:
        text = evidence["quote"]
        if not re.search(r"前置工作站|后续必须|前一步|后一步|之前.*(?:平台|工作站)|之后.*(?:平台|工作站)", text):
            continue
        matched = _unique(code for alias, code in aliases.items() if alias in text)
        result.append({
            "relation": "predecessor" if re.search(r"前置|前一步|之前", text) else "successor",
            "station_codes": matched,
            "resolution": "referenced" if matched else "unresolved",
            "evidence": evidence,
        })
    return result


def _short(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def _research_summary(station: dict[str, Any]) -> str:
    operation_text = "/".join(
        _operation_summary(operation) for operation in station["operations"]
    ) or _short(station["description"], 80)
    parts = [f"{station['display_name']}[{station['station_code']}]", operation_text]
    if station["critical_constraints"]:
        parts.append(
            "Hard="
            + "|".join(_short(item, 60) for item in station["critical_constraints"][:2])
        )
    return "；".join(parts) + "。"


def _relative(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _source_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted({item.resolve() for item in paths}, key=lambda item: str(item)):
        digest.update(_relative(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def source_digest(source: Path = DEFAULT_SOURCE) -> str:
    """Content digest of the actual roster, root rules, names and used audits."""
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"workstation source not found: {source}")
    paths = [path for path in (source / "SKILL.md", source / "工作站名称中英文对照.md") if path.exists()]
    for module_dir, _ in MODULES:
        for skill in sorted((source / module_dir).glob("*/SKILL.md")):
            paths.append(skill)
            audit = _audit_path(source, skill.parent.name)
            if audit:
                paths.append(audit)
    return _source_digest(paths)


def build_index(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"workstation source not found: {source}")

    display_names = _name_map(source)
    workstations: list[dict[str, Any]] = []
    root_skill_path = source / "SKILL.md"
    root_skill = root_skill_path.read_text(encoding="utf-8") if root_skill_path.exists() else ""

    for module_dir, module_name in MODULES:
        module_path = source / module_dir
        if not module_path.is_dir():
            continue
        for station_dir in sorted(path for path in module_path.iterdir() if path.is_dir()):
            skill_path = station_dir / "SKILL.md"
            if not skill_path.exists():
                continue
            content = skill_path.read_text(encoding="utf-8")
            station_code = station_dir.name
            declared_match = STATION_NAME_RE.search(content)
            declared_name = declared_match.group(1).strip() if declared_match else station_code
            id_match = STATION_ID_RE.search(content)
            description = _description(content)
            schemas = parse_operation_schemas(content)
            io_by_operation = _operation_io_constraints(content)
            audit_path = _audit_path(source, station_code)
            audit_content = audit_path.read_text(encoding="utf-8") if audit_path else ""

            operations: list[dict[str, Any]] = []
            station_parameter_containers: list[str] = []
            station_sample_carriers: list[str] = []
            input_containers: list[str] = []
            output_containers: list[str] = []
            sample_states: list[str] = []
            output_same_as_input = False

            operation_names = _unique(list(schemas) + list(io_by_operation))
            reported_measurements = _reported_measurements(content)
            feedback_contracts = _feedback_contracts(content, skill_path, operation_names)
            for operation_name in operation_names:
                schema = schemas.get(operation_name)
                io = io_by_operation.get(
                    operation_name,
                    {"input": _new_io_side(), "output": _new_io_side()},
                )
                parameter_containers = (
                    _parameter_container_options(schema) if schema is not None else []
                )
                sample_carriers = (
                    _parameter_sample_carrier_options(schema)
                    if schema is not None
                    else []
                )
                station_parameter_containers = _unique(
                    station_parameter_containers + parameter_containers
                )
                station_sample_carriers = _unique(
                    station_sample_carriers + sample_carriers
                )
                input_containers = _unique(input_containers + io["input"]["containers"])
                explicit_outputs = io["output"]["containers"]
                output_containers = _unique(output_containers + explicit_outputs)
                sample_states = _unique(
                    sample_states
                    + io["input"]["sample_states"]
                    + io["output"]["sample_states"]
                )
                output_same_as_input = output_same_as_input or bool(
                    io["output"]["same_as_input"]
                )
                operation_contract: dict[str, Any] = {
                        "name": operation_name,
                        "input": io["input"],
                        "output": io["output"],
                        "container_parameter_options": parameter_containers,
                        "sample_carrier_options": sample_carriers,
                        "parameter_contracts": _parameter_contracts(schema),
                        "reported_measurements": reported_measurements,
                        "feedback_contract": feedback_contracts[operation_name],
                        "source": {"skill": _relative(skill_path), "audit_rules": _relative(audit_path)},
                    }
                for parameter in operation_contract["parameter_contracts"]:
                    parameter["source"] = _evidence(skill_path, content, parameter["source_line"], parameter["evidence"], operation_name)
                operation_contract["container_count_constraints"] = _count_constraints(
                    operation_contract["parameter_contracts"]
                )
                operation_contract["container_contract"] = (
                    _operation_container_contract(operation_contract)
                )
                operation_contract["quantity_semantics"] = _operation_quantity_semantics(
                    operation_name,
                    io["output"],
                    operation_contract["parameter_contracts"],
                    reported_measurements,
                )
                operations.append(operation_contract)

            if output_same_as_input:
                output_containers = _unique(output_containers + input_containers)
            if not input_containers:
                input_containers = list(station_parameter_containers)
            if not output_containers and output_same_as_input:
                output_containers = list(input_containers)

            aliases = _unique(
                [station_code, declared_name, display_names.get(station_code, station_code)]
            )
            station: dict[str, Any] = {
                "station_code": station_code,
                "declared_name": declared_name,
                "display_name": display_names.get(station_code, station_code),
                "aliases": aliases,
                "station_id": int(id_match.group(1)) if id_match else None,
                "module": module_name,
                "description": description,
                "description_source": _evidence(skill_path, content, next((number for number, line in enumerate(content.splitlines(), 1) if line.startswith("description:")), 1), description, "description"),
                "capabilities": operation_names,
                "input_containers": input_containers,
                "output_containers": output_containers,
                "output_same_as_input": output_same_as_input,
                "parameter_container_options": station_parameter_containers,
                "sample_carrier_options": station_sample_carriers,
                "sample_states": sample_states,
                "critical_constraints": _critical_constraints(content, description),
                "planning_constraints": _planning_constraints(content, skill_path, operation_names=operation_names) + (_planning_constraints(audit_content, audit_path, audit=True, operation_names=operation_names) if audit_path else []),
                "audit_content": audit_content,
                "operations": operations,
                "source": {
                    "skill": _relative(skill_path),
                    "audit_rules": _relative(audit_path),
                },
            }
            station["experiment_capabilities"] = extract_experiment_capabilities(station)
            station["capability_operation_bindings"] = (
                validated_capability_operation_bindings(station)
            )
            station["research_summary"] = _research_summary(station)
            workstations.append(station)

    generated_binding_keys = {
        (str(station.get("station_code") or "").strip(), capability_id)
        for station in workstations
        for capability_id in (
            station.get("capability_operation_bindings") or {}
        )
    }
    configured_binding_keys = set(REVIEWED_CAPABILITY_OPERATION_BINDINGS)
    if generated_binding_keys != configured_binding_keys:
        raise ValueError(
            "reviewed capability-operation catalog does not match generated "
            "workstation truth "
            f"(missing={sorted(configured_binding_keys - generated_binding_keys)}, "
            f"unexpected={sorted(generated_binding_keys - configured_binding_keys)})"
        )

    generated_gap_keys = {
        (str(station.get("station_code") or "").strip(), str(capability.get("id") or "").strip())
        for station in workstations
        for capability in (station.get("experiment_capabilities") or [])
        if isinstance(capability, dict)
        and capability.get("operation_mapping_status") == "truth_gap"
    }
    configured_gap_keys = set(REVIEWED_CAPABILITY_OPERATION_GAPS)
    if generated_gap_keys != configured_gap_keys:
        raise ValueError(
            "reviewed capability-operation truth-gap catalog does not match "
            "generated workstation truth "
            f"(missing={sorted(configured_gap_keys - generated_gap_keys)}, "
            f"unexpected={sorted(generated_gap_keys - configured_gap_keys)})"
        )

    for station in workstations:
        station["dependencies"] = _dependencies(station["planning_constraints"], workstations)

    compact_text = "\n".join(station["research_summary"] for station in workstations)
    all_containers = _unique(
        container
        for station in workstations
        for container in (
            station["input_containers"]
            + station["output_containers"]
            + station["parameter_container_options"]
        )
    )
    all_sample_carriers = _unique(
        carrier
        for station in workstations
        for carrier in station["sample_carrier_options"]
    )
    operation_count = sum(len(station["operations"]) for station in workstations)
    return {
        "schema_version": "2.0",
        "projection_version": PROJECTION_VERSION,
        "semantic_mapping_digest": semantic_mapping_digest(),
        "source_kind": "lab-design-all",
        "source": _relative(source),
        "source_digest_sha256": source_digest(source),
        "global_constraints": _global_constraints(root_skill, root_skill_path),
        "generation_policy": (
            "Generated from all per-workstation SKILL.md files. Explicit operation "
            "input/output constraints take precedence over broader container parameter examples. "
            "Skill parameter structures are immutable; this index is a read-only planning projection. "
            "Setpoints, material output effects, and reported numeric measurements are separate axes. "
            "Capability-to-operation authorization comes only from the reviewed exact station/capability map; "
            "missing or stale map entries stop generation. Reviewed description/operation truth gaps remain "
            "explicitly unsupported and never authorize Device operations."
        ),
        "workstation_count": len(workstations),
        "operation_count": operation_count,
        "container_count": len(all_containers),
        "container_list": all_containers,
        # Kept as a compatibility alias for existing readers.
        "containers": all_containers,
        "sample_carrier_list": all_sample_carriers,
        "research_context": {
            "format": (
                "one workstation per line; each operation contains compact I/O requirements, "
                "planning controls, quantitative material-output semantics, declared numeric feedback, "
                "and station hard constraints"
            ),
            "semantics": {
                "I": "physical input container/state/count requirements",
                "O": "physical material/container output",
                "CReq": "output container count relation: I means same count as input; op means operation-declared; input parity/multiple remains in I",
                "Ctl": "required scientific or quantitative setpoints; never a measured result",
                "Qout": "setpoint effect on material output; target dose differs from whole-batch inventory",
                "Report": "numeric feedback fields explicitly declared by Skill; 未声明 means do not invent",
                "Hard": "route/scale/science constraints whose omission could make a plan infeasible",
            },
            "char_count": len(compact_text),
            "container_list": all_containers,
            "sample_carrier_list": all_sample_carriers,
            "text": compact_text,
        },
        "workstations": workstations,
    }


def _render(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the existing output is missing or stale.",
    )
    parser.add_argument("--with-skill-references", action="store_true", help="Also generate/check the three progressively disclosed skill references.")
    parser.add_argument("--skill-root", type=Path, default=SKILL_ROOT)
    args = parser.parse_args()

    data = build_index(args.source)
    rendered = _render(data)
    output = args.output.expanduser().resolve()
    outputs = {output: rendered}
    if args.with_skill_references:
        for tier, name in TIER_SKILLS.items():
            outputs[args.skill_root.expanduser().resolve() / name / "references" / "capabilities.json"] = _render(project_device_context(data, tier))
    if args.check:
        stale = [path for path, content in outputs.items() if not path.exists() or path.read_text(encoding="utf-8") != content]
        if stale:
            print("stale workstation capability projections: " + ", ".join(str(path) for path in stale), file=sys.stderr)
            return 1
        print(f"workstation capability index is current: {output}")
        return 0

    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(
        f"wrote {output}: {data['workstation_count']} workstations, "
        f"{data['operation_count']} operations, "
        f"{data['research_context']['char_count']} compact-context characters"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
