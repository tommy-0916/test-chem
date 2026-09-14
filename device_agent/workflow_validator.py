"""Deterministic schema validation of final dispatch parameters.

The success path of the device agent used to accept any ``workflow_json``
whose ``steps`` was a list — a fabricated parameter such as
``不存在的危险参数=999`` sailed through as ``success``.  This module builds a
parameter schema per (workstation, operation) from the on-disk truth sources
and validates every step's dispatch payload against it:

- unknown workstation                  → error ``unknown_workstation``
- unknown parameter field              → error ``unknown_parameter``
- missing contract-required parameter  → error ``missing_required_parameter``
- value outside every known range      → error ``value_out_of_range``
- unit suffix contradicting the schema → error ``wrong_unit``
- array/scalar type mismatch           → error ``type_mismatch``
- container type unsupported by station→ error ``unsupported_container``

Schema sources (union — a field is known if ANY source declares it):
1. legacy machine-readable JSON (``chem_resources/workstations/*.json``),
2. lab-design SKILL.md parameter tables,
3. the canonical format contract ``chem_resources/format_reference/reference.json``.

Strictness is asymmetric on purpose: *unknown names are rejected against the
union*, while *ranges pass if any source admits the value* — the validator
must never reject the canonical reference workflow itself.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.paths import format_reference_path, workstation_dir

UNIT_SUFFIX_RE = re.compile(r"[（(]([^（）()]*)[)）]\s*$")
RANGE_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*\]")
NUMBERED_SUFFIX_RE = re.compile(r"[一二三四五六七八九十0-9]+$")
CONTAINER_TOKEN_RE = re.compile(r"[一-鿿A-Za-z0-9]+")
# label/value option lists such as [{"label":"是","value":"1"},{"label":"否","value":"0"}]
LABEL_VALUE_RE = re.compile(r'"label"\s*:\s*"([^"]*)"\s*,\s*"value"\s*:\s*"?([^",}]*)"?')
# simple inline enum such as 示例值列 "加热/自然" or "CO2,N2"
SIMPLE_ENUM_RE = re.compile(r"^[\wA-Za-z0-9一-鿿]+(?:\s*[/，,、]\s*[\wA-Za-z0-9一-鿿]+)+$")

UNIT_SYNONYMS = {
    "分钟": "min", "min": "min", "minute": "min", "minutes": "min",
    "小时": "h", "h": "h", "hour": "h",
    "秒": "s", "s": "s", "sec": "s",
    "℃": "c", "c": "c", "°c": "c", "摄氏度": "c",
    "ml": "ml", "毫升": "ml", "ml/次": "ml",
    "ul": "ul", "微升": "ul", "μl": "ul",
    "rpm": "rpm", "r/min": "rpm", "转/分": "rpm",
    "w": "w", "瓦": "w",
    "mm": "mm", "cm": "cm", "g": "g", "mg": "mg",
}

KNOWN_CONTAINER_TYPES = (
    "进样瓶", "西林瓶", "50ml耐热瓶", "留样瓶", "耐压反应管",
    "96位塑料孔板", "96位石英孔板", "碳纸架", "测试架", "比色皿",
)

# Contract-required dispatch fields per operation, distilled from the
# authoritative format contract (format_reference/reference.json) — these are
# the fields every canonical example of the operation carries.
CORE_REQUIRED_BY_OPERATION: Dict[str, List[Tuple[str, ...]]] = {
    "物料拿取": [("容器类型",), ("容器编号",)],
    "物料放置": [("容器类型",), ("容器编号",)],
    "开盖": [("容器类型",), ("容器编号", "开盖编号")],
    "关盖": [("容器类型",), ("容器编号", "关盖编号")],
    "加液": [("容器编号", "进样瓶编号", "加样方案"), ("原液编号", "原液量", "手动设置加液量", "加样方案")],
    "磁力搅拌": [("容器编号", "进样瓶编号"), ("搅拌速度",), ("搅拌时间",)],
    "开始搅拌": [("容器编号", "进样瓶编号"), ("搅拌速度",), ("搅拌时间",)],
    "静置烘干": [("容器编号", "进样瓶编号"), ("恒温温度",), ("烘干时间",)],
    "烘干主流程": [("容器编号", "进样瓶编号"), ("恒温温度",), ("烘干时间",)],
    "离心": [("容器编号", "进样瓶编号"),],
    "标准离心": [("容器编号", "进样瓶编号"),],
    "超声清洗": [("容器编号", "进样瓶编号"),],
    "电化学检测": [("容器编号", "进样瓶编号"),],
    "固体加样": [("容器编号", "进样瓶编号"),],
}

META_STEP_KEYS = {
    "step_number", "workstation", "operation", "parameters",
    "id", "source_plan_step", "source_macro_step", "source_macro_steps",
    "macro_action_id", "observation_point_id", "notes",
}


def normalize_param_name(name: Any) -> str:
    """``搅拌时间（分钟）`` → ``搅拌时间``; trims hierarchy dashes and spaces."""
    text = str(name or "").strip().lstrip("-").strip()
    text = UNIT_SUFFIX_RE.sub("", text).strip()
    return text


def param_unit(name: Any) -> str:
    match = UNIT_SUFFIX_RE.search(str(name or "").strip())
    if not match:
        return ""
    return UNIT_SYNONYMS.get(match.group(1).strip().lower(), match.group(1).strip().lower())


def _normalize_station_form(name: str) -> str:
    return str(name or "").replace("_", "").replace(" ", "").lower()


def base_form(name: str) -> str:
    """``电化学任务一`` → ``电化学任务`` so numbered variants stay known."""
    stripped = NUMBERED_SUFFIX_RE.sub("", name)
    return stripped if len(stripped) >= 2 else name


TIME_UNIT_SECONDS = {"s": 1.0, "min": 60.0, "h": 3600.0}


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return float(text)
    return None


def _convert_for_range(number: float, value_unit: str, range_unit: str) -> Optional[float]:
    """Express ``number`` in the range's unit, or None when incomparable."""
    if not value_unit or not range_unit or value_unit == range_unit:
        return number
    if value_unit in TIME_UNIT_SECONDS and range_unit in TIME_UNIT_SECONDS:
        return number * TIME_UNIT_SECONDS[value_unit] / TIME_UNIT_SECONDS[range_unit]
    return None


class StationSchema:
    """Union parameter schema for one physical station."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.allowed_names: Set[str] = set()
        self.allowed_bases: Set[str] = set()
        # name -> list of (low, high, unit) triples; unit may be "".
        self.ranges: Dict[str, List[Tuple[float, float, str]]] = {}
        self.units: Dict[str, Set[str]] = {}
        self.array_params: Set[str] = set()
        self.numeric_params: Set[str] = set()
        self.enums: Dict[str, Set[str]] = {}
        self.operations: Set[str] = set()
        self.container_types: Set[str] = set()
        self.confident = False  # at least one parsed parameter table
        # operation -> top-level params the SKILL table marks 是否必填=是.
        # Applied only when the step names the station in SKILL form (code or
        # 对照表 display) — legacy-form payloads keep the reference contract.
        self.required_by_operation: Dict[str, Set[str]] = {}
        self.skill_form_names: Set[str] = set()
        # operation -> {param name: SKILL 默认值列 value}. Populated from the
        # 默认值 (last) column of each SKILL parameter table. Used only by the
        # deterministic completion pass to fill omitted required fields; never
        # changes validation behaviour.
        self.defaults_by_operation: Dict[str, Dict[str, Any]] = {}
        # operation -> required input lid state ("有盖"/"无盖") parsed from the
        # SKILL 输入约束 block's `容器状态：必须有盖/必须无盖` line. Drives the
        # lid-continuity check (issue #12): errors only on PROVEN conflicts.
        self.lid_requirement_by_operation: Dict[str, str] = {}

    def add_param(
        self,
        raw_name: str,
        *,
        unit: str = "",
        type_text: str = "",
        range_pair: Optional[Tuple[float, float]] = None,
        enum_values: Optional[List[str]] = None,
    ) -> None:
        name = normalize_param_name(raw_name)
        if not name:
            return
        self.allowed_names.add(name)
        self.allowed_bases.add(base_form(name))
        declared_unit = UNIT_SYNONYMS.get(unit.strip().lower(), unit.strip().lower()) if unit else param_unit(raw_name)
        if declared_unit:
            self.units.setdefault(name, set()).add(declared_unit)
        if range_pair:
            self.ranges.setdefault(name, []).append(
                (range_pair[0], range_pair[1], declared_unit)
            )
        if enum_values:
            self.enums.setdefault(name, set()).update(
                str(item).strip() for item in enum_values if str(item).strip()
            )
        type_lower = (type_text or "").strip().lower()
        if type_lower in {"array", "matrix"}:
            self.array_params.add(name)
        elif type_lower in {"int", "integer", "float", "number"} and not enum_values:
            # A label/value enum column is declared int in some SKILL tables
            # (保留瓶盖) yet dispatched as the label string "是"/"否"; do not
            # force numeric typing when an enum is present.
            self.numeric_params.add(name)

    def knows(self, name: str) -> bool:
        if name in self.allowed_names or base_form(name) in self.allowed_bases:
            return True
        # SKILL.md declares placeholder fields like `N号原液瓶`; real payloads
        # instantiate them as `3号原液瓶`.
        placeholder = re.sub(r"\d+", "N", name, count=1)
        return placeholder != name and placeholder in self.allowed_names


class WorkflowValidator:
    """Validate final workflow_json steps against the workstation truth source."""

    def __init__(
        self,
        workstation_loader: Any = None,
        *,
        old_workstation_dir: Optional[str] = None,
        reference_json_path: Optional[str] = None,
    ) -> None:
        self._loader = workstation_loader
        # Resolve once: refreshing a reused validator must not replace explicit
        # caller paths with defaults (or reinterpret a relative path in a new cwd).
        self._old_workstation_dir = str(Path(
            old_workstation_dir or workstation_dir(use_new_format=False)
        ).expanduser().resolve())
        self._reference_json_path = str(Path(
            reference_json_path or format_reference_path("json")
        ).expanduser().resolve())
        WorkflowValidator.refresh(self, workstation_loader)

    def refresh(self, workstation_loader: Any = None) -> None:
        """Rebuild facts from the same configured sources and a refreshed loader."""
        if workstation_loader is not None:
            self._loader = workstation_loader
        self._schemas: Dict[str, StationSchema] = {}
        self._alias_to_key: Dict[str, str] = {}
        try:
            self._build_from_old_json(self._old_workstation_dir)
        except Exception:
            pass
        try:
            self._build_from_loader()
        except Exception:
            pass
        try:
            self._build_from_reference(self._reference_json_path)
        except Exception:
            pass

    def source_paths(self) -> List[Path]:
        """Actual auxiliary sources for the Device run's drift guard."""
        root = Path(self._old_workstation_dir)
        paths = list(root.glob("*.json")) if root.is_dir() else []
        paths.append(Path(self._reference_json_path))
        return sorted(paths)

    # ------------------------------------------------------------------
    # schema construction
    # ------------------------------------------------------------------

    def _schema(self, key: str, label: str = "") -> StationSchema:
        if key not in self._schemas:
            self._schemas[key] = StationSchema(label or key)
        return self._schemas[key]

    def _register_alias(self, alias: str, key: str) -> None:
        alias = (alias or "").strip()
        if alias:
            self._alias_to_key.setdefault(alias, key)

    def _build_from_old_json(self, directory: str) -> None:
        if not directory or not os.path.isdir(directory):
            return
        for filename in os.listdir(directory):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, filename), "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            identity = data.get("station_identity") or {}
            name = str(identity.get("name", "")).strip()
            code = str(identity.get("code", filename[:-5])).strip()
            key = name or code
            schema = self._schema(key, label=name or code)
            self._register_alias(name, key)
            self._register_alias(code, key)
            for operation in data.get("operations", []) or []:
                op_name = str(operation.get("name", "")).strip()
                if op_name:
                    schema.operations.add(op_name)
                for param in operation.get("operational_parameters", []) or []:
                    constraints = param.get("constraints") or {}
                    range_pair = None
                    enum_values = None
                    if isinstance(constraints, dict):
                        low = constraints.get("min_value")
                        high = constraints.get("max_value")
                        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                            range_pair = (float(low), float(high))
                        raw_enum = constraints.get("enum_values")
                        if isinstance(raw_enum, list):
                            enum_values = [str(item) for item in raw_enum]
                    schema.add_param(
                        str(param.get("name", "")),
                        unit=str((constraints or {}).get("unit", "") or ""),
                        type_text=str(param.get("type", "") or ""),
                        range_pair=range_pair,
                        enum_values=enum_values,
                    )
                    schema.confident = True

    def _build_from_loader(self) -> None:
        if self._loader is None or not hasattr(self._loader, "get_all"):
            return
        for station in self._loader.get_all():
            if not isinstance(station, dict):
                continue
            code = str(station.get("station_name", "")).strip()
            display = str(station.get("display_name", "")).strip()
            if not code:
                continue
            key = code
            schema = self._schema(key, label=display or code)
            self._register_alias(code, key)
            self._register_alias(display, key)
            for form in (code, display):
                if form:
                    schema.skill_form_names.add(_normalize_station_form(form))
            content = str(station.get("usage_content", "") or station.get("skill_content", "") or "")
            self._parse_skill_markdown(schema, content)

    def _parse_skill_markdown(self, schema: StationSchema, content: str) -> None:
        in_table = False
        current_op = ""
        in_input_block = False
        # bold list items that are section labels, not operations
        non_operation_labels = {"输入约束", "输出约束", "参数", "示例", "注意", "备注", "约束"}
        for line in content.splitlines():
            stripped = line.strip()
            op_match = re.match(r"^(?:##\s*操作(?:\s*\d+\.)?|-)\s*\*\*(.+?)\*\*", stripped)
            if op_match:
                candidate_op = op_match.group(1).strip()
                if candidate_op not in non_operation_labels:
                    current_op = candidate_op
                    schema.operations.add(current_op)
                    in_input_block = False
                elif candidate_op == "输入约束":
                    in_input_block = True
                elif candidate_op == "输出约束":
                    in_input_block = False
            container_match = re.match(r"^-?\s*容器类型[:：]\s*(.+)$", stripped)
            if container_match:
                for token in CONTAINER_TOKEN_RE.findall(container_match.group(1)):
                    if token in KNOWN_CONTAINER_TYPES:
                        schema.container_types.add(token)
            # SKILL input-constraint lid state: `容器状态：必须有盖/必须无盖`
            lid_match = re.match(r"^-?\s*容器状态[:：]\s*必须(有盖|无盖)", stripped)
            if lid_match and current_op and in_input_block:
                schema.lid_requirement_by_operation.setdefault(
                    current_op, lid_match.group(1)
                )
            if stripped.startswith("|"):
                cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                if cells and cells[0] in {"参数名", "参数"}:
                    in_table = True
                    continue
                if in_table:
                    if set("".join(cells)) <= {"-", " ", ":"}:
                        continue
                    if len(cells) < 5:
                        in_table = False
                        continue
                    raw_name = cells[0]
                    if not raw_name or raw_name.startswith("**") and raw_name.endswith("**"):
                        raw_name = raw_name.strip("*")
                    unit = cells[3] if len(cells) > 3 else ""
                    type_text = cells[4] if len(cells) > 4 else ""
                    example = cells[5] if len(cells) > 5 else ""
                    default_cell = cells[6] if len(cells) > 6 else ""
                    blob = " ".join(cells[1:])
                    range_pair = None
                    range_match = RANGE_RE.search(blob)
                    if range_match and normalize_param_name(raw_name) not in {"容器编号", "开盖编号", "关盖编号"}:
                        range_pair = (float(range_match.group(1)), float(range_match.group(2)))
                    enum_values = self._parse_enum_from_example(example)
                    schema.add_param(
                        raw_name,
                        unit=unit,
                        type_text=type_text,
                        range_pair=range_pair,
                        enum_values=enum_values,
                    )
                    schema.confident = True
                    # 是否必填=是 on a TOP-LEVEL row (nested dash-notation rows
                    # like `-瓶号` are required within their parent, not at the
                    # step level) → step-level required for the current op.
                    required_cell = cells[2] if len(cells) > 2 else ""
                    is_nested = cells[0].lstrip("*").lstrip().startswith("-")
                    if current_op and required_cell == "是" and not is_nested:
                        name = normalize_param_name(raw_name)
                        if name:
                            schema.required_by_operation.setdefault(
                                current_op, set()
                            ).add(name)
                            # capture the SKILL 默认值 column for the completion
                            # pass (only a concrete, non-empty scalar default)
                            default_text = (default_cell or "").strip()
                            if default_text and current_op:
                                schema.defaults_by_operation.setdefault(
                                    current_op, {}
                                )[name] = default_text
            else:
                in_table = False

    def _parse_enum_from_example(self, example: str) -> Optional[List[str]]:
        """Extract enum members from a SKILL.md example-value cell.

        Handles label/value option lists (``[{"label":"是","value":"1"}]`` →
        both ``是`` and ``1`` accepted) and simple delimited enums
        (``加热/自然``, ``CO2,N2``). Ranges/placeholders return None.
        """
        text = (example or "").strip()
        if not text:
            return None
        pairs = LABEL_VALUE_RE.findall(text)
        if pairs:
            members: List[str] = []
            for label, value in pairs:
                if label.strip():
                    members.append(label.strip())
                if value.strip():
                    members.append(value.strip())
            return members or None
        # Do not treat range/interval or numeric-only hints as enums.
        if RANGE_RE.search(text) or "[" in text or "最小" in text or "最大" in text:
            return None
        if SIMPLE_ENUM_RE.match(text):
            return [part.strip() for part in re.split(r"[/，,、]", text) if part.strip()]
        return None

    def _build_from_reference(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return
        for step in (data.get("steps") or []):
            if not isinstance(step, dict):
                continue
            key = self._resolve_station(str(step.get("workstation", "")))
            if key is None:
                key = str(step.get("workstation", "")).strip()
                if not key:
                    continue
            schema = self._schema(key)
            self._register_alias(str(step.get("workstation", "")), key)
            operation = str(step.get("operation", "")).strip()
            if operation:
                schema.operations.add(operation)
            parameters = step.get("parameters")
            if isinstance(parameters, dict):
                for raw_name, value in parameters.items():
                    schema.add_param(
                        raw_name,
                        type_text="array" if isinstance(value, list) else "",
                    )

    # ------------------------------------------------------------------
    # resolution + validation
    # ------------------------------------------------------------------

    def _resolve_station(self, station_name: str) -> Optional[str]:
        name = (station_name or "").strip()
        if not name:
            return None
        if name in self._schemas:
            return name
        if name in self._alias_to_key:
            return self._alias_to_key[name]
        if self._loader is not None and hasattr(self._loader, "_map_station_name_to_code"):
            try:
                code = self._loader._map_station_name_to_code(name)
            except Exception:
                code = None
            if code:
                if code in self._schemas:
                    return code
                if code in self._alias_to_key:
                    return self._alias_to_key[code]
        for alias, key in self._alias_to_key.items():
            if alias and (alias in name or name in alias):
                return key
        return None

    def allowed_params_for(self, station_name: str) -> List[str]:
        """Dispatchable parameter names for one station (for repair prompts)."""
        key = self._resolve_station(station_name)
        if key is None:
            return []
        return sorted(self._schemas[key].allowed_names)

    def required_params_for(self, station_name: str, operation: str) -> Set[str]:
        """SKILL 是否必填=是 params for (station, operation). Empty when the
        station is not in SKILL form (legacy-form payloads keep the reference
        contract) or has no parsed required table. Read-only; for the
        completion pass to know which required fields may need filling."""
        key = self._resolve_station(station_name)
        if key is None:
            return set()
        schema = self._schemas[key]
        req = self._skill_required_for(schema, station_name, operation)
        return set(req or set())

    def defaults_for(self, station_name: str, operation: str) -> Dict[str, Any]:
        """SKILL 默认值 column values for (station, operation), keyed by
        normalized param name. Read-only; used by the completion pass to fill
        omitted required fields with their SKILL-declared default."""
        key = self._resolve_station(station_name)
        if key is None:
            return {}
        schema = self._schemas[key]
        table = getattr(schema, "defaults_by_operation", {})
        if operation in table:
            return dict(table[operation])
        normalized = _normalize_station_form(operation)
        for op, values in table.items():
            if _normalize_station_form(op) == normalized:
                return dict(values)
        return {}

    def validate(self, workflow_json: Any) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []
        steps = workflow_json.get("steps") if isinstance(workflow_json, dict) else None
        if not isinstance(steps, list) or not steps:
            return {
                "status": "failed",
                "errors": ["workflow_json.steps 缺失或为空，无法进行下发参数校验。"],
                "warnings": [],
                "checked_steps": 0,
            }
        if not self._schemas:
            return {
                "status": "passed",
                "errors": [],
                "warnings": ["工作站参数 schema 不可用，跳过严格下发校验。"],
                "checked_steps": 0,
            }

        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                errors.append(f"第 {index} 步不是 JSON object。")
                continue
            step_no = step.get("step_number", index)
            station_name = str(step.get("workstation", "")).strip()
            operation = str(step.get("operation", "")).strip()
            if not station_name:
                errors.append(f"第 {step_no} 步缺少 workstation 字段。")
                continue
            if not operation:
                errors.append(f"第 {step_no} 步（{station_name}）缺少 operation 字段。")
            for key in step.keys():
                if key not in META_STEP_KEYS:
                    warnings.append(f"第 {step_no} 步包含非契约字段 `{key}`。")

            station_key = self._resolve_station(station_name)
            if station_key is None:
                errors.append(
                    f"第 {step_no} 步的工作站 `{station_name}` 不在设备真源中（unknown_workstation）。"
                )
                continue
            schema = self._schemas[station_key]

            parameters = step.get("parameters")
            if parameters is None:
                warnings.append(f"第 {step_no} 步（{station_name}/{operation}）没有 parameters。")
                parameters = {}
            if not isinstance(parameters, dict):
                errors.append(f"第 {step_no} 步（{station_name}）parameters 必须是 JSON object。")
                continue
            if not schema.confident and not schema.allowed_names:
                warnings.append(
                    f"第 {step_no} 步（{station_name}）无可用参数 schema，仅做结构校验。"
                )
                continue

            self._validate_parameters(
                schema, station_name, operation, step_no, parameters, errors, warnings
            )
            self._validate_required(
                schema, station_name, operation, step_no, parameters, errors
            )
            self._validate_container_count(
                station_name, operation, step_no, parameters, errors
            )

        self._validate_lid_continuity(steps, errors, warnings)

        return {
            "status": "failed" if errors else "passed",
            "errors": errors,
            "warnings": warnings,
            "checked_steps": len(steps),
        }

    def _validate_lid_continuity(
        self,
        steps: List[Any],
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """Track per-(container type, id) lid state across steps and report
        PROVEN conflicts only (issue #12: container/sample-state continuity).

        开盖* sets 无盖, 关盖* sets 有盖; the initial state is unknown and an
        unknown state never errors — heuristic plans that omit lid steps stay
        valid. A double 开盖/关盖 on an already-known state is a warning; an
        operation whose SKILL 输入约束 demands 无盖 while the tracked state is
        有盖 (or vice versa) is an error.
        """
        lid_state: Dict[Tuple[str, Any], str] = {}
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            step_no = step.get("step_number", index)
            station_name = str(step.get("workstation", "")).strip()
            operation = str(step.get("operation", "")).strip()
            parameters = step.get("parameters")
            parameters = parameters if isinstance(parameters, dict) else {}
            container_type = str(parameters.get("容器类型", "")).strip()
            ids = parameters.get("容器编号")
            id_list = ids if isinstance(ids, list) else [None]
            keys = [(container_type, item) for item in id_list]

            requirement = ""
            station_key = self._resolve_station(station_name)
            if station_key is not None:
                schema = self._schemas[station_key]
                table = getattr(schema, "lid_requirement_by_operation", {})
                requirement = table.get(operation, "")
                if not requirement:
                    normalized_op = _normalize_station_form(operation)
                    for skill_op, state in table.items():
                        if _normalize_station_form(skill_op) == normalized_op:
                            requirement = state
                            break

            is_open = operation.startswith("开盖")
            is_close = operation.startswith("关盖")
            for key in keys:
                known = lid_state.get(key)
                if requirement and known and known != requirement:
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）要求容器必须"
                        f"{requirement}，但按前序开关盖步骤推断当前为{known}"
                        "（lid_state_conflict）。"
                    )
                if is_open:
                    if known == "无盖":
                        warnings.append(
                            f"第 {step_no} 步对已处于无盖状态的容器 {key[1]} 重复开盖。"
                        )
                    lid_state[key] = "无盖"
                elif is_close:
                    if known == "有盖":
                        warnings.append(
                            f"第 {step_no} 步对已处于有盖状态的容器 {key[1]} 重复关盖。"
                        )
                    lid_state[key] = "有盖"

    def validate_consistency(
        self,
        workflow_txt: str,
        workflow_json: Any,
    ) -> Dict[str, Any]:
        """Deterministic workflow_txt ↔ workflow_json agreement check
        (issue #12: previously asserted only inside the LLM self-check).

        Checks: the `第N步` block count matches len(steps), and each step's
        workstation (SKILL code, display name, or a resolvable alias) appears
        in its corresponding txt block. Anything unprovable is a warning, not
        an error — only demonstrable mismatches fail."""
        errors: List[str] = []
        warnings: List[str] = []
        text = str(workflow_txt or "")
        steps = workflow_json.get("steps") if isinstance(workflow_json, dict) else None
        steps = steps if isinstance(steps, list) else []

        blocks = re.split(r"(?=第\s*\d+\s*步)", text)
        blocks = [block for block in blocks if re.match(r"第\s*\d+\s*步", block.strip())]
        if not blocks:
            if steps:
                warnings.append("workflow_txt 中未发现『第N步』块，跳过逐步一致性比对。")
            return {"status": "passed" if not errors else "failed",
                    "errors": errors, "warnings": warnings}
        if len(blocks) != len(steps):
            errors.append(
                f"workflow_txt 有 {len(blocks)} 个『第N步』块，但 workflow_json 有 "
                f"{len(steps)} 个 steps（txt_json_mismatch）。"
            )
        for index, (block, step) in enumerate(zip(blocks, steps), start=1):
            if not isinstance(step, dict):
                continue
            station = str(step.get("workstation", "")).strip()
            if not station:
                continue
            candidates = {station}
            station_key = self._resolve_station(station)
            if station_key is not None:
                candidates.add(station_key)
                candidates.add(self._schemas[station_key].label)
                for alias, key in self._alias_to_key.items():
                    if key == station_key:
                        candidates.add(alias)
            if not any(candidate and candidate in block for candidate in candidates):
                errors.append(
                    f"workflow_txt 第 {index} 块未出现该步的工作站『{station}』"
                    "（txt_json_mismatch）。"
                )
        return {"status": "failed" if errors else "passed",
                "errors": errors, "warnings": warnings}

    def _validate_parameters(
        self,
        schema: StationSchema,
        station_name: str,
        operation: str,
        step_no: Any,
        parameters: Dict[str, Any],
        errors: List[str],
        warnings: List[str],
    ) -> None:
        for raw_name, value in parameters.items():
            name = normalize_param_name(raw_name)
            if not schema.knows(name):
                errors.append(
                    f"第 {step_no} 步（{station_name}/{operation}）参数 `{raw_name}` "
                    "不在该工作站可下发参数中（unknown_parameter）。"
                )
                continue

            unit = param_unit(raw_name)
            known_units = schema.units.get(name) or schema.units.get(base_form(name))
            if unit and known_units and unit not in known_units:
                convertible = any(
                    unit in TIME_UNIT_SECONDS and known in TIME_UNIT_SECONDS
                    for known in known_units
                )
                if not convertible:
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）参数 `{raw_name}` 单位 "
                        f"`{unit}` 与真源单位 {sorted(known_units)} 不一致（wrong_unit）。"
                    )

            if name in schema.array_params and not isinstance(value, list):
                errors.append(
                    f"第 {step_no} 步（{station_name}/{operation}）参数 `{raw_name}` "
                    "应为数组（type_mismatch）。"
                )
                continue
            if name in schema.numeric_params and not isinstance(value, list):
                if _coerce_number(value) is None:
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）参数 `{raw_name}` "
                        f"应为数值，实际为 `{value!r}`（type_mismatch）。"
                    )
                    continue

            ranges = schema.ranges.get(name) or schema.ranges.get(base_form(name))
            number = _coerce_number(value)
            if ranges and number is not None:
                in_any_range = False
                comparable = False
                for low, high, range_unit in ranges:
                    converted = _convert_for_range(number, unit, range_unit)
                    if converted is None:
                        continue
                    comparable = True
                    if low <= converted <= high:
                        in_any_range = True
                        break
                if comparable and not in_any_range:
                    bounds = " / ".join(
                        f"[{low:g},{high:g}]{(' ' + range_unit) if range_unit else ''}"
                        for low, high, range_unit in ranges
                    )
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）参数 `{raw_name}`="
                        f"{number:g} 超出真源允许范围 {bounds}（value_out_of_range）。"
                    )

            enum_values = schema.enums.get(name) or schema.enums.get(base_form(name))
            if enum_values and isinstance(value, str):
                text = value.strip()
                if text and text not in enum_values:
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）参数 `{raw_name}`="
                        f"`{text}` 不在真源枚举 {sorted(enum_values)} 中（invalid_enum_value）。"
                    )

            if name == "容器类型" and schema.container_types:
                text = str(value or "").strip()
                if text and text not in schema.container_types:
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）容器类型 `{text}` "
                        f"不在该工作站支持列表 {sorted(schema.container_types)} 中"
                        "（unsupported_container）。"
                    )

    def _validate_required(
        self,
        schema: StationSchema,
        station_name: str,
        operation: str,
        step_no: Any,
        parameters: Dict[str, Any],
        errors: List[str],
    ) -> None:
        provided = {normalize_param_name(key) for key in parameters.keys()}
        provided |= {base_form(name) for name in provided}

        # SKILL 是否必填=是 params, enforced only when the step names the
        # station in SKILL form (code/对照表 display). Legacy-form payloads
        # (物料站/液体进样站 — the reference.json contract) keep the core
        # fallback so canonical examples stay valid.
        skill_required = self._skill_required_for(schema, station_name, operation)
        if skill_required:
            for name in sorted(skill_required):
                if name not in provided and base_form(name) not in provided:
                    errors.append(
                        f"第 {step_no} 步（{station_name}/{operation}）缺少 SKILL 必填参数 "
                        f"`{name}`（missing_required_parameter）。"
                    )
            return

        required_groups = CORE_REQUIRED_BY_OPERATION.get(operation)
        if not required_groups:
            return
        for group in required_groups:
            if not any(normalize_param_name(candidate) in provided for candidate in group):
                label = "/".join(group)
                errors.append(
                    f"第 {step_no} 步（{station_name}/{operation}）缺少必填参数 "
                    f"`{label}`（missing_required_parameter）。"
                )

    def _skill_required_for(
        self, schema: StationSchema, station_name: str, operation: str
    ) -> Optional[Set[str]]:
        if not schema.required_by_operation:
            return None
        if _normalize_station_form(station_name) not in schema.skill_form_names:
            return None
        if operation in schema.required_by_operation:
            return schema.required_by_operation[operation]
        normalized_op = _normalize_station_form(operation)
        for skill_op, required in schema.required_by_operation.items():
            if _normalize_station_form(skill_op) == normalized_op:
                return required
        return None

    def _validate_container_count(
        self,
        station_name: str,
        operation: str,
        step_no: Any,
        parameters: Dict[str, Any],
        errors: List[str],
    ) -> None:
        """容器数量 must equal len(容器编号) when both are present."""
        count_value = None
        ids_value = None
        for raw_name, value in parameters.items():
            name = normalize_param_name(raw_name)
            if name == "容器数量":
                count_value = value
            elif name == "容器编号":
                ids_value = value
        if count_value is None or not isinstance(ids_value, list):
            return
        count = _coerce_number(count_value)
        if count is None or not float(count).is_integer():
            return
        if int(count) != len(ids_value):
            errors.append(
                f"第 {step_no} 步（{station_name}/{operation}）容器数量={int(count)} "
                f"与容器编号数量 {len(ids_value)} 不一致（container_count_mismatch）。"
            )


# ----------------------------------------------------------------------
# Structured error export (issue #4: per-error feedback fields)
# ----------------------------------------------------------------------

_ERROR_CODE_RE = re.compile(r"（([a-z_]+)）。?\s*$")
_STEP_PREFIX_RE = re.compile(r"^第\s*(\d+)\s*步(?:（([^／/）]+)(?:[／/]([^）]+))?）)?")
_PARAM_NAME_RE = re.compile(r"[`『「]([^`』」]+)[`』」]")
_ACTUAL_VALUE_RE = re.compile(r"[=＝]\s*([^\s，。（]+)")
_PLAN_INVARIANT_ERROR_PATTERNS = (
    (
        re.compile(r"device_plan\s+引用了\s*Research\s*不存在的\s+source_macro_step=", re.I),
        "invalid_source_macro_step",
    ),
    (
        re.compile(
            r"device_plan\s+缺少\s*Research\s+macro\s+step.+?覆盖|"
            r"删除了\s+macro\s+step\s+coverage",
            re.I,
        ),
        "macro_coverage_missing",
    ),
    (
        re.compile(
            r"source_macro_steps\s+未按\s*Research\s+macro\s+step\s+顺序排列|"
            r"改变了\s*Research\s+macro\s+step/reagent\s+执行顺序",
            re.I,
        ),
        "frozen_macro_order_drift",
    ),
    (
        re.compile(r"改变了既存\s+plan_step\s+的冻结\s+source_macro_step\s+绑定", re.I),
        "frozen_source_macro_binding_drift",
    ),
    (
        re.compile(r"operation\s+recomposition\s+改变了冻结\s+macro\s+source-set\s+union", re.I),
        "frozen_source_macro_union_drift",
    ),
    (
        re.compile(r"拆分/合并或新增了\s+plan\s+step.+?缺少逐组绑定", re.I),
        "missing_plan_recomposition_evidence",
    ),
    (
        re.compile(r"source_macro_step=.+?未逐字保留\s*Research\s*试剂/对象身份", re.I),
        "frozen_reagent_identity_missing",
    ),
    (
        re.compile(r"source_macro_step=.+?的显式试剂身份发生漂移", re.I),
        "frozen_reagent_identity_drift",
    ),
    (
        re.compile(r"source_macro_steps?=.+?引入了\s*Research\s*未授权的显式试剂身份", re.I),
        "frozen_reagent_identity_unauthorized",
    ),
)
_SOURCE_MACRO_ERROR_RE = re.compile(
    r"source_macro_step(?:s)?=(\[[^\]]+\]|[-+]?\d+)"
)


def structure_validation_errors(
    errors: Any,
    workflow_json: Any,
) -> List[Dict[str, Any]]:
    """Parse our own validator error strings into structured records
    (issue #4's required feedback fields). The message wording is generated
    by this module, so the patterns are stable; anything unmatched degrades
    to {"error_code": "unparsed", "message": ...} — information is never lost.

    Each record carries: error_code, message, step_number, workstation,
    operation, parameter_path, actual, plus source_macro_step /
    macro_action_id / observation_point_id back-filled from the step."""
    steps_by_number: Dict[Any, Dict[str, Any]] = {}
    if isinstance(workflow_json, dict):
        for step in workflow_json.get("steps") or []:
            if isinstance(step, dict):
                steps_by_number[step.get("step_number")] = step
                steps_by_number[str(step.get("step_number"))] = step

    structured: List[Dict[str, Any]] = []
    for raw in errors or []:
        text = str(raw)
        record: Dict[str, Any] = {"message": text, "error_code": "unparsed"}
        code_match = _ERROR_CODE_RE.search(text)
        if code_match:
            record["error_code"] = code_match.group(1)
        else:
            for pattern, error_code in _PLAN_INVARIANT_ERROR_PATTERNS:
                if pattern.search(text):
                    record["error_code"] = error_code
                    break
        source_macro_match = _SOURCE_MACRO_ERROR_RE.search(text)
        if source_macro_match:
            source_value = source_macro_match.group(1)
            if re.fullmatch(r"[-+]?\d+", source_value):
                record["source_macro_step"] = int(source_value)
            else:
                try:
                    parsed_sources = json.loads(source_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    try:
                        parsed_sources = ast.literal_eval(source_value)
                    except (SyntaxError, ValueError):
                        parsed_sources = None
                if isinstance(parsed_sources, list):
                    record["source_macro_steps"] = parsed_sources
        step_match = _STEP_PREFIX_RE.match(text)
        if step_match:
            number_text = step_match.group(1)
            record["step_number"] = int(number_text)
            if step_match.group(2):
                record["workstation"] = step_match.group(2).strip()
            if step_match.group(3):
                record["operation"] = step_match.group(3).strip()
            step = steps_by_number.get(int(number_text)) or steps_by_number.get(number_text)
            if isinstance(step, dict):
                record.setdefault("workstation", str(step.get("workstation", "")))
                record.setdefault("operation", str(step.get("operation", "")))
                for key in (
                    "source_macro_step",
                    "source_macro_step_id",
                    "device_step_id",
                    "macro_action_id",
                    "observation_point_id",
                ):
                    if step.get(key) is not None:
                        record[key] = step[key]
        param_match = _PARAM_NAME_RE.search(text)
        if param_match:
            record["parameter_path"] = param_match.group(1)
        actual_match = _ACTUAL_VALUE_RE.search(text)
        if actual_match and record["error_code"] in {
            "value_out_of_range", "container_count_mismatch", "invalid_enum_value",
            "type_mismatch",
        }:
            record["actual"] = actual_match.group(1).strip("`")
        structured.append(record)
    return structured
