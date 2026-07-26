#!/usr/bin/env python3
# Vendored from the chem-agent-eval-sop deterministic auditor so the Device
# Agent validates raw workflow_json against the exact same Skill contract the
# external evaluation enforces (id, types, enums, ranges, nesting, volumes).
"""Deterministically audit Device workflow JSON against workstation Skills.

This auditor is deliberately independent of the repository's Device validator.
It parses the authoritative per-workstation ``SKILL.md`` files into an
operation-specific, hierarchical parameter schema and checks both the raw
``workflow_json`` and the recorded dispatch-formatting result.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


MODULE_DIRS = (
    "references-Synthesis-Module",
    "references-Reaction-and-Testing-Module",
    "references-Characterization-Module",
)
META_STEP_KEYS = {
    "step_number",
    "workstation",
    "operation",
    "parameters",
    "id",
    "source_macro_step",
    "macro_action_id",
    "observation_point_id",
    "notes",
}
OPERATION_HEADING_RE = re.compile(
    r"^#{2,4}\s*操作(?:\s*\d+\.)?\s*\*\*(.+?)\*\*"
)
BOLD_BULLET_RE = re.compile(r"^-\s*\*\*(.+?)\*\*")
STATION_ID_RE = re.compile(r"工作站编码\s*:\s*(\d+)")
STATION_NAME_RE = re.compile(r"^name\s*:\s*(\S+)\s*$", re.MULTILINE)
RANGE_RE = re.compile(
    r"([\[(])\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*"
    r"(-?\d+(?:\.\d+)?)\s*([\])])"
)
LABEL_VALUE_RE = re.compile(
    r'"label"\s*:\s*"([^"]*)"\s*,\s*"value"\s*:\s*"?([^",}]*)"?'
)
DYNAMIC_NUMBER_RE = re.compile(r"(\d+)")
REMOTE_FILE_RE = re.compile(r"^(?:https?|obs|s3)://", re.IGNORECASE)


@dataclass
class ParameterNode:
    name: str
    required: bool
    type_name: str
    unit: str
    remark: str
    example: str
    default: str
    line: int
    children: list["ParameterNode"] = field(default_factory=list)

    @property
    def evidence_text(self) -> str:
        return " ".join(
            part for part in (self.remark, self.example, self.default) if part
        )


@dataclass
class OperationSchema:
    name: str
    parameters: dict[str, ParameterNode] = field(default_factory=dict)


@dataclass
class StationSchema:
    code: str
    display_name: str
    station_id: int | None
    skill_path: Path
    operations: dict[str, OperationSchema]
    aliases: set[str] = field(default_factory=set)


@dataclass
class Finding:
    code: str
    message: str
    severity: str = "error"
    case_id: str = ""
    step_number: Any = None
    workstation: str = ""
    resolved_workstation: str = ""
    operation: str = ""
    parameter_path: str = ""
    expected: Any = None
    actual: Any = None
    skill_path: str = ""
    skill_line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if value not in (None, "", [], {})
        }


def _clean_cell(value: str) -> str:
    return value.replace("**", "").strip()


def _normalize_type(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return type(value).__name__


def _type_matches(value: Any, declared: str) -> bool:
    declared = _normalize_type(declared)
    if not declared:
        return True
    if declared in {"file", "url"}:
        return isinstance(value, str)
    if declared in {"int", "integer"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if declared in {"float", "number", "double"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared in {"string", "str"}:
        return isinstance(value, str)
    if declared in {"array", "list", "matrix"}:
        return isinstance(value, list)
    if declared in {"object", "dict"}:
        return isinstance(value, dict)
    if declared in {"bool", "boolean"}:
        return isinstance(value, bool)
    return True


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
        return float(value)
    return None


def _range_from_node(node: ParameterNode) -> tuple[float, float, bool, bool] | None:
    match = RANGE_RE.search(node.evidence_text)
    if not match:
        return None
    return (
        float(match.group(2)),
        float(match.group(3)),
        match.group(1) == "[",
        match.group(4) == "]",
    )


def _value_in_range(value: Any, bounds: tuple[float, float, bool, bool]) -> bool:
    number = _numeric(value)
    if number is None:
        return True
    low, high, include_low, include_high = bounds
    low_ok = number >= low if include_low else number > low
    high_ok = number <= high if include_high else number < high
    return low_ok and high_ok


def _enum_values(node: ParameterNode) -> set[str]:
    result: set[str] = set()
    for label, value in LABEL_VALUE_RE.findall(node.evidence_text):
        if label.strip():
            result.add(label.strip())
        if value.strip():
            result.add(value.strip())
    return result


def _enum_token(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _dynamic_pattern(name: str) -> re.Pattern[str] | None:
    if "N" not in name:
        return None
    return re.compile("^" + re.escape(name).replace("N", r"(\d+)") + "$")


def _matches_parameter(actual_name: str, schema_name: str) -> bool:
    if actual_name == schema_name:
        return True
    pattern = _dynamic_pattern(schema_name)
    return bool(pattern and pattern.fullmatch(actual_name))


def parse_operation_schemas(content: str) -> dict[str, OperationSchema]:
    operations: dict[str, OperationSchema] = {}
    current_operation = ""
    current_section = ""
    in_table = False
    stack: dict[int, ParameterNode] = {}
    non_operations = {"输入约束", "输出约束", "参数", "示例", "注意", "备注", "约束"}

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.strip()
        heading = re.match(r"^#{2,4}\s*(.+?)\s*$", stripped)
        if heading:
            current_section = heading.group(1).strip()
        operation_match = OPERATION_HEADING_RE.match(stripped)
        if operation_match:
            candidate = operation_match.group(1).strip()
            current_operation = candidate
            operations.setdefault(candidate, OperationSchema(name=candidate))
            stack = {}
        else:
            bullet_match = BOLD_BULLET_RE.match(stripped)
            if bullet_match and (
                current_section.startswith("操作") or current_section.startswith("参数设置")
            ):
                candidate = bullet_match.group(1).strip()
                if candidate not in non_operations:
                    current_operation = candidate
                    operations.setdefault(candidate, OperationSchema(name=candidate))
                    stack = {}

        if not stripped.startswith("|"):
            in_table = False
            continue

        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and _clean_cell(cells[0]) in {"参数名", "参数"}:
            in_table = True
            stack = {}
            continue
        if not in_table or not current_operation or len(cells) < 5:
            continue
        if set("".join(cells)) <= {"-", " ", ":", "\t"}:
            continue

        cells += [""] * (7 - len(cells))
        raw_name = _clean_cell(cells[0])
        depth = len(raw_name) - len(raw_name.lstrip("-"))
        name = raw_name.lstrip("-").strip()
        if not name:
            continue
        node = ParameterNode(
            name=name,
            required=_clean_cell(cells[2]) == "是",
            unit=_clean_cell(cells[3]),
            type_name=_normalize_type(cells[4]),
            example=_clean_cell(cells[5]),
            default=_clean_cell(cells[6]),
            remark=_clean_cell(cells[1]),
            line=line_number,
        )
        operation = operations[current_operation]
        if depth == 0:
            operation.parameters[name] = node
        else:
            parent = stack.get(depth - 1)
            if parent is not None:
                parent.children.append(node)
        stack[depth] = node
        for old_depth in list(stack):
            if old_depth > depth:
                del stack[old_depth]

    return operations


def _read_name_map(root: Path) -> dict[str, str]:
    path = root / "工作站名称中英文对照.md"
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in raw_line:
            continue
        code, display = (part.strip() for part in raw_line.split("\t", 1))
        if not code or not display or code in {"英文名", "合成模块Synthesis Module"}:
            continue
        if set(code) == {"-"}:
            continue
        mapping[code] = display
    return mapping


def load_workstation_catalog(root: Path) -> tuple[dict[str, StationSchema], dict[str, str]]:
    name_map = _read_name_map(root)
    stations: dict[str, StationSchema] = {}
    aliases: dict[str, str] = {}
    for module_dir in MODULE_DIRS:
        module = root / module_dir
        if not module.is_dir():
            continue
        for station_dir in sorted(path for path in module.iterdir() if path.is_dir()):
            skill_path = station_dir / "SKILL.md"
            if not skill_path.exists():
                continue
            content = skill_path.read_text(encoding="utf-8")
            name_match = STATION_NAME_RE.search(content)
            code = name_match.group(1).strip() if name_match else station_dir.name
            id_match = STATION_ID_RE.search(content)
            display = name_map.get(code, station_dir.name)
            station = StationSchema(
                code=code,
                display_name=display,
                station_id=int(id_match.group(1)) if id_match else None,
                skill_path=skill_path.resolve(),
                operations=parse_operation_schemas(content),
            )
            station.aliases.update({code, station_dir.name, display})
            stations[code] = station
            for alias in station.aliases:
                if alias:
                    aliases.setdefault(alias, code)
                    aliases.setdefault(alias.replace(" ", ""), code)
    return stations, aliases


class CaseAuditor:
    def __init__(
        self,
        *,
        case_id: str,
        case_dir: Path,
        repo: Path,
        run_dir: Path,
        stations: dict[str, StationSchema],
        aliases: dict[str, str],
    ) -> None:
        self.case_id = case_id
        self.case_dir = case_dir
        self.repo = repo
        self.run_dir = run_dir
        self.stations = stations
        self.aliases = aliases
        self.errors: list[Finding] = []
        self.warnings: list[Finding] = []
        self.current_step: dict[str, Any] = {}
        self.current_station: StationSchema | None = None
        self.liquid_totals: dict[tuple[str, int, int], float] = {}

    def add(
        self,
        code: str,
        message: str,
        *,
        severity: str = "error",
        parameter_path: str = "",
        expected: Any = None,
        actual: Any = None,
        node: ParameterNode | None = None,
    ) -> None:
        step = self.current_step
        station = self.current_station
        finding = Finding(
            code=code,
            message=message,
            severity=severity,
            case_id=self.case_id,
            step_number=step.get("step_number"),
            workstation=str(step.get("workstation", "")),
            resolved_workstation=station.code if station else "",
            operation=str(step.get("operation", "")),
            parameter_path=parameter_path,
            expected=expected,
            actual=actual,
            skill_path=str(station.skill_path) if station else "",
            skill_line=node.line if node else None,
        )
        (self.errors if severity == "error" else self.warnings).append(finding)

    def resolve_station(self, name: Any) -> StationSchema | None:
        text = str(name or "").strip()
        code = self.aliases.get(text) or self.aliases.get(text.replace(" ", ""))
        return self.stations.get(code) if code else None

    def audit(self, package: dict[str, Any]) -> dict[str, Any]:
        workflow = package.get("workflow_json")
        if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
            return {
                "case_id": self.case_id,
                "workflow_present": False,
                "dispatch_schema_match": "not_evaluable",
                "checked_steps": 0,
                "errors": [],
                "warnings": [],
                "counts_by_code": {},
            }

        steps = workflow["steps"]
        seen_numbers: set[Any] = set()
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                self.current_step = {"step_number": index}
                self.current_station = None
                self.add("step_not_object", "Device step must be a JSON object", actual=step)
                continue
            self.current_step = step
            self.current_station = self.resolve_station(step.get("workstation"))
            step_number = step.get("step_number")
            if not isinstance(step_number, int) or isinstance(step_number, bool):
                self.add(
                    "invalid_step_number",
                    "step_number must be an integer",
                    parameter_path="step_number",
                    expected="int",
                    actual=step_number,
                )
            elif step_number in seen_numbers:
                self.add(
                    "duplicate_step_number",
                    "step_number must be unique",
                    parameter_path="step_number",
                    actual=step_number,
                )
            seen_numbers.add(step_number)

            for key in step:
                if key not in META_STEP_KEYS:
                    self.add(
                        "unknown_step_field",
                        "Step contains a field outside the workflow contract",
                        severity="warning",
                        parameter_path=key,
                        actual=step[key],
                    )

            if self.current_station is None:
                self.add(
                    "unknown_workstation",
                    "Workstation is not one of the loaded workstation Skills",
                    parameter_path="workstation",
                    actual=step.get("workstation"),
                )
                continue
            station = self.current_station
            if step.get("id") != station.station_id:
                self.add(
                    "workstation_id_mismatch",
                    "Step id must exactly match the workstation Skill id",
                    parameter_path="id",
                    expected=station.station_id,
                    actual=step.get("id"),
                )
            operation_name = str(step.get("operation", "")).strip()
            operation = station.operations.get(operation_name)
            if operation is None:
                self.add(
                    "unknown_operation",
                    "Operation does not belong to the selected workstation",
                    parameter_path="operation",
                    expected=sorted(station.operations),
                    actual=operation_name,
                )
                continue
            if step.get("source_macro_step") is None:
                self.add(
                    "missing_source_macro_step",
                    "Device step is not traceable to a source Macro Plan step",
                    parameter_path="source_macro_step",
                )
            parameters = step.get("parameters")
            if not isinstance(parameters, dict):
                self.add(
                    "parameters_not_object",
                    "parameters must be a JSON object",
                    parameter_path="parameters",
                    expected="object",
                    actual=_json_type(parameters),
                )
                continue
            self._audit_parameters(operation, parameters)
            self._audit_cross_fields(operation, parameters)

        self._audit_dispatch_formatting(package, len(steps))
        error_dicts = [finding.as_dict() for finding in self.errors]
        warning_dicts = [finding.as_dict() for finding in self.warnings]
        return {
            "case_id": self.case_id,
            "workflow_present": True,
            "dispatch_schema_match": "no" if self.errors else "yes",
            "checked_steps": len(steps),
            "error_count": len(error_dicts),
            "warning_count": len(warning_dicts),
            "counts_by_code": dict(sorted(Counter(item["code"] for item in error_dicts).items())),
            "errors": error_dicts,
            "warnings": warning_dicts,
        }

    def _audit_parameters(
        self, operation: OperationSchema, parameters: dict[str, Any]
    ) -> None:
        schema_names = operation.parameters
        for name, value in parameters.items():
            if name not in schema_names:
                self.add(
                    "unknown_parameter",
                    "Parameter is not declared for this workstation operation",
                    parameter_path=name,
                    expected=sorted(schema_names),
                    actual=value,
                )
        for name, node in schema_names.items():
            if node.required and name not in parameters:
                self.add(
                    "missing_required_parameter",
                    "Required operation parameter is missing",
                    parameter_path=name,
                    expected=node.type_name,
                    node=node,
                )
                continue
            if name in parameters:
                self._audit_value(parameters[name], node, name)

    def _audit_value(self, value: Any, node: ParameterNode, path: str) -> None:
        if not _type_matches(value, node.type_name):
            self.add(
                "type_mismatch",
                "Parameter JSON type does not match the Skill contract",
                parameter_path=path,
                expected=node.type_name,
                actual=_json_type(value),
                node=node,
            )
            return

        bounds = _range_from_node(node)
        if bounds and not _value_in_range(value, bounds):
            self.add(
                "range_violation",
                "Parameter value is outside the Skill range",
                parameter_path=path,
                expected={
                    "low": bounds[0],
                    "high": bounds[1],
                    "include_low": bounds[2],
                    "include_high": bounds[3],
                    "unit": node.unit,
                },
                actual=value,
                node=node,
            )
        enum_values = _enum_values(node)
        if enum_values and _enum_token(value) not in enum_values:
            self.add(
                "enum_violation",
                "Parameter value is not a declared Skill option",
                parameter_path=path,
                expected=sorted(enum_values),
                actual=value,
                node=node,
            )
        if node.type_name == "file" and isinstance(value, str):
            self._audit_file(value, node, path)

        if not node.children:
            return
        if node.type_name in {"array", "list", "matrix"}:
            for index, item in enumerate(value):
                self._audit_nested_object(item, node.children, f"{path}[{index}]")
        elif node.type_name in {"object", "dict"}:
            self._audit_nested_object(value, node.children, path)

    def _audit_nested_object(
        self, value: Any, nodes: list[ParameterNode], path: str
    ) -> None:
        if not isinstance(value, dict):
            self.add(
                "nested_type_mismatch",
                "Nested Skill rows require an object",
                parameter_path=path,
                expected="object",
                actual=_json_type(value),
                node=nodes[0] if nodes else None,
            )
            return
        keys = list(value)
        for node in nodes:
            matches = [key for key in keys if _matches_parameter(key, node.name)]
            if node.required and not matches:
                self.add(
                    "missing_nested_parameter",
                    "Required nested parameter is missing",
                    parameter_path=f"{path}.{node.name}",
                    expected=node.type_name,
                    node=node,
                )
            for key in matches:
                self._audit_dynamic_index(key, node, f"{path}.{key}")
                self._audit_value(value[key], node, f"{path}.{key}")
        for key in keys:
            if not any(_matches_parameter(key, node.name) for node in nodes):
                self.add(
                    "unknown_nested_parameter",
                    "Nested parameter is not declared by the Skill hierarchy",
                    parameter_path=f"{path}.{key}",
                    actual=value[key],
                )

    def _audit_dynamic_index(
        self, actual_name: str, node: ParameterNode, path: str
    ) -> None:
        pattern = _dynamic_pattern(node.name)
        if not pattern:
            return
        match = pattern.fullmatch(actual_name)
        if not match:
            return
        number = int(match.group(1))
        text = node.evidence_text
        range_match = re.search(
            r"(?:范围|取值范围(?:为)?)\s*(\d+)\s*号?\s*[~～至-]\s*(\d+)\s*号?",
            text,
        )
        if range_match:
            low, high = int(range_match.group(1)), int(range_match.group(2))
            if not low <= number <= high:
                self.add(
                    "dynamic_parameter_range",
                    "Instantiated N parameter is outside its Skill range",
                    parameter_path=path,
                    expected=[low, high],
                    actual=number,
                    node=node,
                )

    def _audit_file(self, value: str, node: ParameterNode, path: str) -> None:
        if REMOTE_FILE_RE.match(value.strip()):
            self.add(
                "remote_file_not_verified",
                "Remote file reference cannot be verified locally",
                severity="warning",
                parameter_path=path,
                actual=value,
                node=node,
            )
            return
        candidate = Path(value).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [
            self.case_dir / candidate,
            self.repo / candidate,
            self.run_dir / candidate,
            self.current_station.skill_path.parent / candidate if self.current_station else candidate,
        ]
        if not any(path.exists() and path.is_file() for path in candidates):
            self.add(
                "missing_input_file",
                "Required workstation file input does not exist",
                parameter_path=path,
                expected="existing local file or verifiable remote URI",
                actual=value,
                node=node,
            )

    def _audit_cross_fields(
        self, operation: OperationSchema, parameters: dict[str, Any]
    ) -> None:
        count = parameters.get("容器数量")
        identifiers = parameters.get("容器编号")
        if isinstance(count, int) and not isinstance(count, bool) and isinstance(identifiers, list):
            if count != len(identifiers):
                self.add(
                    "container_count_mismatch",
                    "容器数量 must equal the length of 容器编号",
                    parameter_path="容器数量/容器编号",
                    expected=count,
                    actual=len(identifiers),
                )
        carrier_count = parameters.get("样品载体数量")
        carrier_ids = parameters.get("样品载体编号")
        if isinstance(carrier_count, int) and isinstance(carrier_ids, list):
            if carrier_count != len(carrier_ids):
                self.add(
                    "carrier_count_mismatch",
                    "样品载体数量 must equal the length of 样品载体编号",
                    parameter_path="样品载体数量/样品载体编号",
                    expected=carrier_count,
                    actual=len(carrier_ids),
                )
        self._audit_container_specific_count(operation, parameters)
        self._audit_plan_targets(parameters)
        self._audit_liquid_limits(parameters)

    def _audit_container_specific_count(
        self, operation: OperationSchema, parameters: dict[str, Any]
    ) -> None:
        container_type = parameters.get("容器类型")
        count = parameters.get("容器数量")
        node = operation.parameters.get("容器数量")
        if not isinstance(container_type, str) or not isinstance(count, int) or not node:
            return
        pattern = re.compile(
            r"([^;；]+?)最小\s*(-?\d+(?:\.\d+)?)\s*个?最大\s*"
            r"(-?\d+(?:\.\d+)?)"
        )
        for raw_type, low, high in pattern.findall(node.evidence_text):
            normalized_type = raw_type.strip().split()[-1]
            if normalized_type != container_type:
                continue
            low_value, high_value = float(low), float(high)
            if not low_value <= count <= high_value:
                self.add(
                    "container_count_range",
                    "Container count is outside the type-specific Skill range",
                    parameter_path="容器数量",
                    expected=[low_value, high_value],
                    actual=count,
                    node=node,
                )

    def _audit_plan_targets(self, parameters: dict[str, Any]) -> None:
        container_ids = parameters.get("容器编号")
        if not isinstance(container_ids, list):
            return
        for plan_name, target_keys in (
            ("加样方案", ("加样瓶号",)),
            ("加液参数", ("加样瓶号", "加样通道")),
        ):
            plan = parameters.get(plan_name)
            if not isinstance(plan, list):
                continue
            targets: list[int] = []
            for item in plan:
                if not isinstance(item, dict):
                    continue
                raw_target = next((item.get(key) for key in target_keys if key in item), None)
                try:
                    targets.append(int(raw_target))
                except (TypeError, ValueError):
                    continue
            normalized_ids: list[int] = []
            for item in container_ids:
                try:
                    normalized_ids.append(int(item))
                except (TypeError, ValueError):
                    pass
            if targets and sorted(targets) != sorted(normalized_ids):
                self.add(
                    "reagent_plan_target_mismatch",
                    f"{plan_name} targets must match 容器编号",
                    parameter_path=plan_name,
                    expected=sorted(normalized_ids),
                    actual=sorted(targets),
                )

    def _audit_liquid_limits(self, parameters: dict[str, Any]) -> None:
        plan = parameters.get("加样方案")
        station = self.current_station
        if not isinstance(plan, list) or station is None:
            return
        if station.code == "Liquid_Handling_Station_1ml_V2":
            max_single, max_total, max_bottle = 1.0, 3.0, 16
        elif station.code == "Liquid_Handling_Station_5ml_V1":
            max_single, max_total, max_bottle = 5.0, 30.0, 6
        else:
            return
        for item_index, item in enumerate(plan):
            if not isinstance(item, dict):
                continue
            try:
                target = int(item.get("加样瓶号"))
            except (TypeError, ValueError):
                target = -1
            sources: list[tuple[Any, Any, str]] = []
            placeholder = item.get("N号原液瓶")
            if isinstance(placeholder, list):
                for entry_index, entry in enumerate(placeholder):
                    if isinstance(entry, dict):
                        sources.append(
                            (
                                entry.get("瓶号"),
                                entry.get("原液用量"),
                                f"加样方案[{item_index}].N号原液瓶[{entry_index}]",
                            )
                        )
            for key, value in item.items():
                match = re.fullmatch(r"(\d+)号原液瓶", key)
                if match and isinstance(value, dict):
                    sources.append(
                        (
                            int(match.group(1)),
                            value.get("原液用量"),
                            f"加样方案[{item_index}].{key}",
                        )
                    )
            for bottle, volume, path in sources:
                if not isinstance(bottle, int) or isinstance(bottle, bool) or not 1 <= bottle <= max_bottle:
                    self.add(
                        "source_bottle_range",
                        "Source bottle number is outside the workstation range",
                        parameter_path=f"{path}.瓶号",
                        expected=[1, max_bottle],
                        actual=bottle,
                    )
                if isinstance(volume, (int, float)) and not isinstance(volume, bool):
                    if float(volume) > max_single:
                        self.add(
                            "single_transfer_volume",
                            "Single liquid transfer exceeds the workstation limit",
                            parameter_path=f"{path}.原液用量",
                            expected=f"<= {max_single} mL",
                            actual=volume,
                        )
                    if isinstance(bottle, int) and target >= 0:
                        key = (station.code, target, bottle)
                        cumulative = self.liquid_totals.get(key, 0.0) + float(volume)
                        self.liquid_totals[key] = cumulative
                        if cumulative > max_total:
                            self.add(
                                "total_reagent_volume",
                                "Cumulative volume of one source liquid exceeds the workstation limit",
                                parameter_path=f"{path}.原液用量",
                                expected=f"cumulative <= {max_total} mL",
                                actual={
                                    "target_container": target,
                                    "source_bottle": bottle,
                                    "cumulative_mL": cumulative,
                                },
                            )

    def _audit_dispatch_formatting(self, package: dict[str, Any], workflow_steps: int) -> None:
        formatting = package.get("dispatch_formatting")
        if not isinstance(formatting, dict):
            self.current_step = {}
            self.current_station = None
            self.add(
                "missing_dispatch_formatting",
                "Successful Device package lacks dispatch formatting evidence",
            )
            return
        self.current_step = {}
        self.current_station = None
        mapped = formatting.get("mapped_steps")
        unmapped = formatting.get("unmapped_steps")
        if isinstance(unmapped, int) and unmapped > 0:
            self.add(
                "dispatch_unmapped_steps",
                "Final dispatch formatter left workstation steps unmapped",
                expected=0,
                actual=unmapped,
            )
        if isinstance(mapped, int) and isinstance(unmapped, int) and mapped + unmapped != workflow_steps:
            self.add(
                "dispatch_step_count_mismatch",
                "Formatter mapped/unmapped totals do not cover workflow steps",
                expected=workflow_steps,
                actual={"mapped": mapped, "unmapped": unmapped},
            )
        for warning in formatting.get("warnings", []) or []:
            text = str(warning)
            if "已从下发 payload 省略" in text:
                self.add(
                    "dispatch_parameter_dropped",
                    "Formatter dropped a workflow parameter from the final payload",
                    actual=text,
                )
            elif "未能映射" in text or "formatting failed" in text:
                self.add(
                    "dispatch_mapping_warning",
                    "Formatter reported an unresolved dispatch mapping",
                    actual=text,
                )
            else:
                self.add(
                    "dispatch_formatting_warning",
                    "Formatter emitted a non-blocking warning",
                    severity="warning",
                    actual=text,
                )


def _load_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return parsed


def _infer_repo(run_dir: Path, explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    manifest_path = run_dir / "suite_manifest.json"
    if manifest_path.exists():
        repo = _load_json(manifest_path).get("repo")
        if repo:
            return Path(str(repo)).expanduser().resolve()
    raise ValueError("Cannot infer repository; pass --repo or provide suite_manifest.json")


def _infer_workstations(
    run_dir: Path, repo: Path, explicit: Path | None
) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    manifest_path = run_dir / "suite_manifest.json"
    if manifest_path.exists():
        source = _load_json(manifest_path).get("workstations_source")
        if source and Path(str(source)).exists():
            return Path(str(source)).expanduser().resolve()
    return (
        repo
        / "chem_resources"
        / "lab-design-main"
        / "skills"
        / "chemistry-experiment-workstation"
    ).resolve()


def _case_package_paths(case_dir: Path) -> list[Path]:
    """Locate every Device package exposed by one black-box campaign."""
    paths: list[Path] = []
    summary_path = case_dir / "case_summary.json"
    if summary_path.exists():
        try:
            summary = _load_json(summary_path)
        except (OSError, json.JSONDecodeError, ValueError):
            summary = {}
        for raw_path in summary.get("workflow_packages", []) or []:
            candidate = Path(str(raw_path)).expanduser()
            if not candidate.is_absolute():
                candidate = case_dir / candidate
            if candidate.is_file():
                paths.append(candidate.resolve())

    if not paths:
        paths.extend(
            path.resolve()
            for path in sorted(case_dir.rglob("device_package.json"))
            if "evaluation" not in path.parts
        )
    canonical = case_dir / "device_package.json"
    if canonical.is_file():
        paths.append(canonical.resolve())
    return list(dict.fromkeys(paths))


def audit_run(
    *,
    run_dir: Path,
    repo: Path | None = None,
    workstations: Path | None = None,
    case_ids: Iterable[str] | None = None,
    expected_workstation_count: int | None = 45,
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    repo = _infer_repo(run_dir, repo)
    workstations = _infer_workstations(run_dir, repo, workstations)
    stations, aliases = load_workstation_catalog(workstations)
    if expected_workstation_count is not None and len(stations) != expected_workstation_count:
        raise ValueError(
            f"Expected {expected_workstation_count} workstation Skills, loaded {len(stations)} "
            f"from {workstations}"
        )
    empty_schemas = [
        f"{station.code}/{operation.name}"
        for station in stations.values()
        for operation in station.operations.values()
        if not operation.parameters
    ]
    missing_operations = [station.code for station in stations.values() if not station.operations]
    if missing_operations or empty_schemas:
        raise ValueError(
            "Workstation Skill parsing incomplete: "
            f"stations_without_operations={missing_operations}, "
            f"operations_without_parameters={empty_schemas}"
        )
    selected = list(case_ids or [])
    if not selected:
        manifest_path = run_dir / "suite_manifest.json"
        if manifest_path.exists():
            selected = [str(item) for item in _load_json(manifest_path).get("case_ids", [])]
        if not selected:
            selected = sorted(path.name for path in run_dir.iterdir() if path.is_dir())

    cases: list[dict[str, Any]] = []
    for case_id in selected:
        case_dir = run_dir / case_id
        package_paths = _case_package_paths(case_dir)
        if not package_paths:
            cases.append(
                {
                    "case_id": case_id,
                    "workflow_present": False,
                    "dispatch_schema_match": "not_evaluable",
                    "checked_steps": 0,
                    "error_count": 0,
                    "warning_count": 0,
                    "errors": [],
                    "warnings": [],
                    "counts_by_code": {},
                    "workflows": [],
                    "evidence_paths": [],
                }
            )
            continue

        workflow_results: list[dict[str, Any]] = []
        for package_path in package_paths:
            try:
                package = _load_json(package_path)
                result = CaseAuditor(
                    case_id=case_id,
                    case_dir=case_dir,
                    repo=repo,
                    run_dir=run_dir,
                    stations=stations,
                    aliases=aliases,
                ).audit(package)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                result = {
                    "case_id": case_id,
                    "workflow_present": False,
                    "dispatch_schema_match": "no",
                    "checked_steps": 0,
                    "error_count": 1,
                    "warning_count": 0,
                    "counts_by_code": {"malformed_device_package": 1},
                    "errors": [
                        {
                            "code": "malformed_device_package",
                            "message": (
                                "Could not read Device package: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            "severity": "error",
                            "case_id": case_id,
                        }
                    ],
                    "warnings": [],
                }
            result["workflow_path"] = str(package_path)
            for finding in result.get("errors", []) + result.get("warnings", []):
                if isinstance(finding, dict):
                    finding.setdefault("workflow_path", str(package_path))
            workflow_results.append(result)

        workflow_present = any(item.get("workflow_present") for item in workflow_results)
        if any(item.get("dispatch_schema_match") == "no" for item in workflow_results):
            verdict = "no"
        elif workflow_present:
            verdict = "yes"
        else:
            verdict = "not_evaluable"
        errors = [
            finding
            for item in workflow_results
            for finding in item.get("errors", [])
        ]
        warnings = [
            finding
            for item in workflow_results
            for finding in item.get("warnings", [])
        ]
        cases.append(
            {
                "case_id": case_id,
                "workflow_present": workflow_present,
                "dispatch_schema_match": verdict,
                "checked_workflows": len(workflow_results),
                "checked_steps": sum(
                    int(item.get("checked_steps", 0)) for item in workflow_results
                ),
                "error_count": len(errors),
                "warning_count": len(warnings),
                "counts_by_code": dict(
                    sorted(Counter(item.get("code", "unknown") for item in errors).items())
                ),
                "errors": errors,
                "warnings": warnings,
                "workflows": workflow_results,
                "evidence_paths": [str(path) for path in package_paths],
            }
        )

    verdict_counts = Counter(case["dispatch_schema_match"] for case in cases)
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "run_dir": str(run_dir),
        "repository": str(repo),
        "workstations_source": str(workstations),
        "workstation_count": len(stations),
        "cases": cases,
        "verdict_counts": dict(sorted(verdict_counts.items())),
    }


def write_audit(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_dir = Path(result["run_dir"])
    for case in result["cases"]:
        per_case = run_dir / "evaluation" / case["case_id"] / "workstation_schema_audit.json"
        per_case.parent.mkdir(parents=True, exist_ok=True)
        per_case.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit Device workflow_json against all workstation Skill contracts."
    )
    parser.add_argument("--run", type=Path, required=True, help="Evaluation run directory")
    parser.add_argument("--repo", type=Path, help="Repository under test")
    parser.add_argument("--workstations", type=Path, help="45-workstation Skill root")
    parser.add_argument("--case", action="append", dest="cases", help="Case id; repeat as needed")
    parser.add_argument("--output", type=Path, help="Run-level audit JSON output")
    parser.add_argument("--expected-workstations", type=int, default=45)
    parser.add_argument("--fail-on-mismatch", action="store_true")
    args = parser.parse_args()

    result = audit_run(
        run_dir=args.run,
        repo=args.repo,
        workstations=args.workstations,
        case_ids=args.cases,
        expected_workstation_count=args.expected_workstations,
    )
    output = args.output or args.run / "evaluation" / "workstation_schema_audit.json"
    write_audit(result, output)
    summary = {
        "output": str(output.resolve()),
        "workstation_count": result["workstation_count"],
        "cases": [
            {
                "case_id": case["case_id"],
                "dispatch_schema_match": case["dispatch_schema_match"],
                "checked_steps": case.get("checked_steps", 0),
                "error_count": case.get("error_count", 0),
            }
            for case in result["cases"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.fail_on_mismatch and any(
        case["dispatch_schema_match"] == "no" for case in result["cases"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
