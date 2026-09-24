"""Read-only validation of the actual platform wire payload.

The selected workstation directory is the only source of device contracts.
Unlike the legacy formatter, this checker neither guesses a workstation by
substring nor widens old platform enums.  A missing or conflicting contract
is reported as unverified.  No network calls or input normalization occur.
"""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

try:
    from .contract_value_normalizer import normalize_contract_scalar
    from .dispatch_formatter import (
        CONVERSION_FILENAME, CURATED_OPERATION_ALIASES, CURATED_STATION_ALIASES,
        DispatchCatalog, WORKFLOW_ONLY_PLATFORM_METADATA, _normalize_name,
        _normalize_param_name, format_dispatch_payload,
    )
    from .skill_contract_audit import (
        LABEL_VALUE_RE, ParameterNode, StationSchema, _dynamic_pattern,
        _range_from_node, _type_matches, _value_in_range,
        load_workstation_catalog,
    )
except ImportError:  # Direct script execution.
    from contract_value_normalizer import normalize_contract_scalar
    from dispatch_formatter import (
        CONVERSION_FILENAME, CURATED_OPERATION_ALIASES, CURATED_STATION_ALIASES,
        DispatchCatalog, WORKFLOW_ONLY_PLATFORM_METADATA, _normalize_name,
        _normalize_param_name, format_dispatch_payload,
    )
    from skill_contract_audit import (
        LABEL_VALUE_RE, ParameterNode, StationSchema, _dynamic_pattern,
        _range_from_node, _type_matches, _value_in_range,
        load_workstation_catalog,
    )


def _ptr(base: str, key: Any) -> str:
    return base.rstrip("/") + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _same(left: Any, right: Any) -> bool:
    """JSON equality must not confuse bool and int, or int and wire string."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


class _ExplicitCatalog(DispatchCatalog):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.export_path = root / CONVERSION_FILENAME
        self.skills, self.skill_aliases = load_workstation_catalog(root)
        self.export_issues: list[str] = []
        self.ambiguous_operations: set[tuple[str, str]] = set()
        self.skill_by_platform: dict[str, list[StationSchema]] = {}
        data = json.loads(self.export_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            raise ValueError("Platform export must contain a steps array")
        for step in data["steps"]:
            if not isinstance(step, dict):
                self.export_issues.append("Platform export contains a non-object step")
                continue
            station, operation = step.get("workstation"), step.get("operation")
            params = step.get("parameters")
            if not isinstance(station, str) or not isinstance(operation, str) or not isinstance(params, list):
                self.export_issues.append("Platform export contains an incomplete operation")
                continue
            specs: dict[str, dict[str, Any]] = {}
            for param in params:
                if not isinstance(param, dict) or not isinstance(param.get("parameter_name"), str):
                    self.ambiguous_operations.add((station, operation))
                    continue
                name = param["parameter_name"]
                if name in specs:
                    self.ambiguous_operations.add((station, operation))
                specs[name] = copy.deepcopy(param)
            if operation in self.stations.get(station, {}):
                self.ambiguous_operations.add((station, operation))
            self.stations.setdefault(station, {})[operation] = specs
            self._normalized_station_index[_normalize_name(station)] = station
        for skill in self.skills.values():
            self._code_to_display[skill.code] = skill.display_name
        for skill in self.skills.values():
            platform = self.resolve_station(skill.code) or self.resolve_station(skill.display_name)
            if platform:
                self.skill_by_platform.setdefault(platform, []).append(skill)
        for platform, skills in self.skill_by_platform.items():
            ids = {item.station_id for item in skills if item.station_id is not None}
            if len(ids) == 1:
                self.station_ids[platform] = next(iter(ids))

    def resolve_station(self, name: str) -> str | None:
        text = str(name or "").strip()
        if text in self.stations:
            return text
        candidates: set[str] = set()
        display = self._code_to_display.get(text, "")
        for token in (text, display):
            if not token:
                continue
            alias = CURATED_STATION_ALIASES.get(token)
            if alias in self.stations:
                candidates.add(alias)
            candidates.update(item for item in self.stations if _normalize_name(item) == _normalize_name(token))
        return next(iter(candidates)) if len(candidates) == 1 else None

    def resolve_operation(self, platform_station: str, operation: str) -> str | None:
        operations = self.stations.get(platform_station, {})
        text = str(operation or "").strip()
        if text in operations:
            return text
        candidates = {item for item in CURATED_OPERATION_ALIASES.get(text, []) if item in operations}
        candidates.update(item for item in operations if _normalize_name(item) == _normalize_name(text))
        return next(iter(candidates)) if len(candidates) == 1 else None

    def skill_for(self, platform: str) -> StationSchema | None:
        found = self.skill_by_platform.get(platform, [])
        return found[0] if len(found) == 1 else None

    def nodes_for(self, platform: str, operation: str) -> dict[str, ParameterNode] | None:
        skill = self.skill_for(platform)
        if skill is None:
            return None
        operations = [item for name, item in skill.operations.items() if self.resolve_operation(platform, name) == operation]
        if len(operations) != 1:
            return None
        nodes: dict[str, ParameterNode] = {}
        for name, node in operations[0].parameters.items():
            wire = self.resolve_parameter(platform, operation, name)
            if wire is not None:
                if wire in nodes:
                    return None
                nodes[wire] = node
        return nodes


class _WireCheck:
    def __init__(self, catalog: _ExplicitCatalog, workflow_pointer: str, payload_pointer: str) -> None:
        self.catalog = catalog
        self.wp = workflow_pointer.rstrip("/")
        self.pp = payload_pointer.rstrip("/")
        self.findings: list[dict[str, Any]] = []
        self.index: int | None = None
        self.step: dict[str, Any] = {}
        self.skill: StationSchema | None = None

    def add(self, code: str, message: str, pointer: str, *, severity: str = "error",
            expected: Any = None, actual: Any = None, related: list[str] | None = None,
            node: ParameterNode | None = None, source: str | None = None) -> None:
        finding = {
            "code": code, "severity": severity, "stage": "dispatch_payload",
            "message": message, "step_index": self.index,
            "step_number": self.step.get("step_number"), "json_pointer": pointer,
            "related_pointers": related or [], "workstation": self.step.get("workstation", ""),
            "operation": self.step.get("operation", ""), "expected": expected, "actual": actual,
        }
        if source or self.skill:
            finding["skill_path"] = source or str(self.skill.skill_path)
        if node:
            finding["skill_line"] = node.line
        self.findings.append(finding)

    def collision_check(self, value: Any, pointer: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                self.collision_check(item, _ptr(pointer, index))
        elif isinstance(value, dict):
            output_keys: dict[str, str] = {}
            for key, item in value.items():
                match = re.fullmatch(r"N号(.+)", str(key))
                entries = item if isinstance(item, list) else [item]
                destinations: list[tuple[str, str]] = []
                if match and isinstance(item, (list, dict)):
                    for index, entry in enumerate(entries):
                        path = _ptr(_ptr(pointer, key), index) if isinstance(item, list) else _ptr(pointer, key)
                        if isinstance(entry, dict) and entry.get("瓶号") is not None:
                            number = entry["瓶号"]
                            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                                self.add("dispatch_dynamic_index_invalid", "Dynamic bottle number must be a positive integer", _ptr(path, "瓶号"), actual=number)
                            destinations.append((f"{number}号{match.group(1)}", path))
                        else:
                            self.add("dispatch_dynamic_index_missing", "Dynamic bottle entry lacks 瓶号; no exact wire key can be generated", path)
                else:
                    destinations.append((key, _ptr(pointer, key)))
                for destination, path in destinations:
                    if destination in output_keys:
                        self.add("dispatch_parameter_collision", "Multiple source fields overwrite the same wire field", path,
                                 actual=destination, related=[output_keys[destination]])
                    output_keys[destination] = path
                self.collision_check(item, _ptr(pointer, key))

    def source_mapping(self, workflow_steps: list[Any]) -> None:
        for index, step in enumerate(workflow_steps):
            self.index, self.step, self.skill = index, step if isinstance(step, dict) else {}, None
            pointer = _ptr(_ptr(self.wp, "steps"), index)
            if not isinstance(step, dict):
                self.add("dispatch_source_step_invalid", "Source step must be an object", pointer, actual=step)
                continue
            station = self.catalog.resolve_station(step.get("workstation"))
            if station is None:
                self.add("dispatch_station_unverified", "No unique platform station exists in the selected export", _ptr(pointer, "workstation"),
                         severity="unverified", actual=step.get("workstation"), source=str(self.catalog.export_path))
                continue
            self.skill = self.catalog.skill_for(station)
            operation = self.catalog.resolve_operation(station, step.get("operation"))
            if operation is None:
                self.add("dispatch_operation_unverified", "No unique platform operation exists in the selected export", _ptr(pointer, "operation"),
                         severity="unverified", actual=step.get("operation"), source=str(self.catalog.export_path))
                continue
            params = step.get("parameters")
            if not isinstance(params, dict):
                self.add("dispatch_source_parameters_invalid", "Source parameters must be an object", _ptr(pointer, "parameters"), actual=params)
                continue
            mapped: dict[str, str] = {}
            for key, value in params.items():
                path = _ptr(_ptr(pointer, "parameters"), key)
                target = self.catalog.resolve_parameter(station, operation, key)
                if target is None:
                    metadata = WORKFLOW_ONLY_PLATFORM_METADATA.get((station, operation), set())
                    if _normalize_param_name(key) not in metadata:
                        self.add("dispatch_parameter_dropped", "Source parameter has no approved wire mapping", path, actual=key,
                                 source=str(self.catalog.export_path))
                elif target in mapped:
                    self.add("dispatch_parameter_collision", "Multiple source parameters map to one wire parameter", path,
                             actual=target, related=[mapped[target]])
                else:
                    mapped[target] = path
                self.collision_check(value, path)

    def compare(self, expected: Any, actual: Any, pointer: str, source_pointer: str) -> None:
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(expected.keys() - actual.keys()):
                self.add("dispatch_value_missing", "Wire payload omits a source-mapped field", _ptr(pointer, key), expected=expected[key],
                         related=[_ptr(source_pointer, key)])
            for key in sorted(actual.keys() - expected.keys()):
                self.add("dispatch_value_added", "Wire payload adds a field absent from the deterministic source mapping", _ptr(pointer, key), actual=actual[key],
                         related=[source_pointer])
            for key in sorted(expected.keys() & actual.keys()):
                self.compare(expected[key], actual[key], _ptr(pointer, key), _ptr(source_pointer, key))
        elif isinstance(expected, list) and isinstance(actual, list):
            if len(expected) != len(actual):
                self.add("dispatch_array_length_mismatch", "Wire array length differs from source", pointer,
                         expected=len(expected), actual=len(actual), related=[source_pointer])
            for index, (left, right) in enumerate(zip(expected, actual)):
                self.compare(left, right, _ptr(pointer, index), _ptr(source_pointer, index))
        elif not _same(expected, actual):
            self.add("dispatch_value_mismatch", "Wire value differs from the approved deterministic conversion", pointer,
                     expected=expected, actual=actual, related=[source_pointer])

    def node_check(self, value: Any, node: ParameterNode, pointer: str, *, top_spec: dict[str, Any] | None = None) -> None:
        declared = str((top_spec or {}).get("type") or node.type_name).lower()
        if not _type_matches(value, declared):
            self.add("dispatch_type_mismatch", "Wire parameter has the wrong type", pointer, expected=declared, actual=value, node=node)
            return
        if isinstance(value, float) and not math.isfinite(value):
            self.add("dispatch_nonfinite_number", "Wire numeric values must be finite", pointer, actual=repr(value), node=node)
            return
        enums = LABEL_VALUE_RE.findall(node.evidence_text)
        if enums and not isinstance(value, (list, dict)):
            tokens: list[Any] = []
            for _label, token in enums:
                token = token.strip()
                if declared in {"int", "integer"} and re.fullmatch(r"-?\d+", token):
                    tokens.append(int(token))
                elif declared in {"float", "number", "double"} and re.fullmatch(r"-?\d+(?:\.\d+)?", token):
                    tokens.append(float(token))
                else:
                    tokens.append(token)
            if not any(_same(value, token) for token in tokens):
                self.add("dispatch_enum_mismatch", "Wire enum must use the declared encoded value", pointer, expected=tokens, actual=value, node=node)
        bounds = _range_from_node(node)
        if bounds and not isinstance(value, (list, dict)) and not _value_in_range(value, bounds):
            self.add("dispatch_range_mismatch", "Wire parameter is outside the Skill range", pointer,
                     expected=list(bounds), actual=value, node=node)
        if node.children:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    path = _ptr(pointer, index)
                    if isinstance(item, dict):
                        self.object_check(item, node.children, path)
                    elif len(node.children) == 1 and not node.children[0].children and not _dynamic_pattern(node.children[0].name):
                        self.node_check(item, node.children[0], path)
                    else:
                        self.add("dispatch_nested_object_required", "Nested parameter entry must be an object", path, actual=item, node=node)
            elif isinstance(value, dict):
                self.object_check(value, node.children, pointer)
        elif isinstance(value, (list, dict)) and node.name not in {"容器编号"}:
            self.add("dispatch_nested_schema_unverified", "Nested wire values have no child schema in the selected Skill", pointer,
                     severity="unverified", node=node)
        if node.name == "容器编号" and isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                    self.add("dispatch_container_id_invalid", "Container identifiers must be positive integers", _ptr(pointer, index), actual=item, node=node)

    def object_check(self, value: dict[str, Any], nodes: list[ParameterNode], pointer: str) -> None:
        for key, item in value.items():
            candidates = [node for node in nodes if key == node.name or (_dynamic_pattern(node.name) and _dynamic_pattern(node.name).fullmatch(str(key)))]
            if len(candidates) != 1:
                self.add("dispatch_unknown_nested_parameter", "Nested field is absent or ambiguous in the Skill schema", _ptr(pointer, key), actual=item)
                continue
            node = candidates[0]
            if _dynamic_pattern(node.name):
                if key == node.name:
                    self.add("dispatch_dynamic_key_uninstantiated", "Wire payload still contains an N bottle placeholder", _ptr(pointer, key), actual=key, node=node)
                else:
                    match = re.search(r"(\d+)", str(key))
                    declared = re.search(r"(?:范围\s*)?(\d+)号\s*[~～至到-]\s*(\d+)号", node.evidence_text)
                    if match and declared and not int(declared.group(1)) <= int(match.group(1)) <= int(declared.group(2)):
                        self.add("dispatch_dynamic_index_range", "Dynamic bottle number is outside the Skill range", _ptr(pointer, key),
                                 expected=[int(declared.group(1)), int(declared.group(2))], actual=int(match.group(1)), node=node)
            self.node_check(item, node, _ptr(pointer, key))
        for node in nodes:
            pattern = _dynamic_pattern(node.name)
            present = any(key == node.name or (pattern and pattern.fullmatch(str(key))) for key in value)
            if node.required and not present:
                self.add("dispatch_required_parameter_missing", "Required nested wire parameter is missing", _ptr(pointer, node.name), node=node)

    def export_value_check(self, value: Any, spec: dict[str, Any], pointer: str, params: dict[str, Any],
                           node: ParameterNode | None = None) -> None:
        declared = str(spec.get("type", "")).lower()
        known_types = {"int", "integer", "float", "number", "double", "string", "str", "array", "list", "matrix", "object", "dict", "bool", "boolean", "file", "url"}
        if declared not in known_types:
            self.add("dispatch_type_unverified", "Export parameter type is missing or unsupported", pointer, severity="unverified", actual=declared,
                     source=str(self.catalog.export_path))
        elif not _type_matches(value, declared):
            self.add("dispatch_type_mismatch", "Parameter does not have the platform-declared wire type", pointer,
                     expected=declared, actual=value, source=str(self.catalog.export_path))
            return
        options = spec.get("options")
        if isinstance(options, list) and options and not any(_same(value, item) for item in options):
            # The shipped export and newer Skill can disagree (for example,
            # 50 mL bottles on the 1 mL V2 station). Do not silently widen the
            # export, and do not label an explicit contract conflict a model bug.
            conflict = isinstance(value, str) and node is not None and bool(value) and value in node.evidence_text
            self.add("dispatch_contract_conflict" if conflict else "dispatch_platform_enum_mismatch",
                     "Skill permits this value but the selected platform export excludes it" if conflict else "Parameter is not in the platform export's allowed values", pointer,
                     severity="unverified" if conflict else "error", expected=options, actual=value,
                     source=str(self.catalog.export_path))
        ranges = spec.get("range")
        if isinstance(ranges, dict) and not ({"min", "max"} & ranges.keys()):
            container = params.get("容器类型")
            ranges = ranges.get(container) if isinstance(container, str) else None
            if ranges is None:
                self.add("dispatch_range_unverified", "No range branch matches the selected container type", pointer,
                         severity="unverified", actual=params.get("容器类型"), source=str(self.catalog.export_path))
        number = value
        if isinstance(ranges, dict) and isinstance(value, str):
            if re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
                number = float(value)
            elif {"min", "max"} & ranges.keys():
                self.add("dispatch_numeric_range_invalid", "Platform range requires a numeric representation", pointer,
                         expected=ranges, actual=value, source=str(self.catalog.export_path))
        if isinstance(ranges, dict) and isinstance(number, (int, float)) and not isinstance(number, bool):
            low, high = ranges.get("min"), ranges.get("max")
            if (isinstance(low, (int, float)) and number < low) or (isinstance(high, (int, float)) and number > high):
                self.add("dispatch_platform_range_mismatch", "Parameter exceeds the platform export range", pointer,
                         expected=ranges, actual=value, source=str(self.catalog.export_path))

    def payload_step(self, step: Any, index: int) -> None:
        self.index, self.step, self.skill = index, step if isinstance(step, dict) else {}, None
        pointer = _ptr(_ptr(_ptr(self.pp, "experiment_steps"), "steps"), index)
        if not isinstance(step, dict):
            self.add("dispatch_step_invalid", "Wire step must be an object", pointer, actual=step)
            return
        allowed = {"step_number", "workstation", "operation", "parameters", "id", "source_macro_step_id", "source_macro_step", "macro_action_id", "observation_point_id", "notes"}
        for key in sorted(step.keys() - allowed):
            self.add("dispatch_unknown_step_field", "Wire step field is not in the generator contract", _ptr(pointer, key), actual=step[key])
        number = step.get("step_number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            self.add("dispatch_step_number_invalid", "Wire step_number must be a positive integer", _ptr(pointer, "step_number"), actual=number)
        station, operation = step.get("workstation"), step.get("operation")
        if not isinstance(station, str) or station not in self.catalog.stations:
            known_skill = isinstance(station, str) and any(
                _normalize_name(station) == _normalize_name(alias) for alias in self.catalog.skill_aliases
            )
            absent_export = known_skill and self.catalog.resolve_station(station) is None
            self.add("dispatch_wire_station_unverified" if absent_export else "dispatch_wire_station_invalid",
                     "Station exists in Skills but has no platform wire contract in the selected export" if absent_export else "Wire payload requires an exact platform station name",
                     _ptr(pointer, "workstation"), actual=station, severity="unverified" if absent_export else "error",
                     source=str(self.catalog.export_path))
            return
        self.skill = self.catalog.skill_for(station)
        if not isinstance(operation, str) or operation not in self.catalog.stations[station]:
            known_skill_op = self.skill is not None and isinstance(operation, str) and operation in self.skill.operations
            absent_export = known_skill_op and self.catalog.resolve_operation(station, operation) is None
            self.add("dispatch_wire_operation_unverified" if absent_export else "dispatch_wire_operation_invalid",
                     "Skill operation has no wire contract in the selected export" if absent_export else "Wire payload requires an exact platform operation name",
                     _ptr(pointer, "operation"), actual=operation, severity="unverified" if absent_export else "error",
                     source=str(self.catalog.export_path))
            return
        if (station, operation) in self.catalog.ambiguous_operations:
            self.add("dispatch_contract_ambiguous", "Selected export repeats this operation or its parameter names", pointer,
                     severity="unverified", source=str(self.catalog.export_path))
        expected_id = self.catalog.station_ids.get(station)
        if expected_id is None:
            self.add("dispatch_station_id_unverified", "Selected Skills do not establish one station ID", _ptr(pointer, "id"), severity="unverified")
        elif not isinstance(step.get("id"), int) or isinstance(step.get("id"), bool) or step["id"] != expected_id:
            self.add("dispatch_station_id_mismatch", "Wire station ID does not match the selected Skill", _ptr(pointer, "id"), expected=expected_id, actual=step.get("id"))
        params = step.get("parameters")
        if not isinstance(params, dict):
            self.add("dispatch_parameters_invalid", "Wire parameters must be an object", _ptr(pointer, "parameters"), actual=params)
            return
        specs = self.catalog.stations[station][operation]
        nodes = self.catalog.nodes_for(station, operation)
        if nodes is None:
            self.add("dispatch_skill_contract_unverified", "Cannot uniquely link wire operation to a full Skill contract", pointer, severity="unverified")
            nodes = {}
        for name in sorted(specs.keys() - nodes.keys() - params.keys()):
            self.add("dispatch_requiredness_unverified", "Export field has no matching Skill definition; omission cannot be certified", _ptr(_ptr(pointer, "parameters"), name),
                     severity="unverified", actual=name, source=str(self.catalog.export_path))
        for name, node in nodes.items():
            if node.required and name not in params:
                self.add("dispatch_required_parameter_missing", "Required wire parameter is missing", _ptr(_ptr(pointer, "parameters"), name), node=node)
        for name, value in params.items():
            path = _ptr(_ptr(pointer, "parameters"), name)
            if name not in specs:
                self.add("dispatch_unknown_parameter", "Parameter is not an exact platform field", path, actual=value, source=str(self.catalog.export_path))
                continue
            self.export_value_check(value, specs[name], path, params, nodes.get(name))
            if name in nodes:
                self.node_check(value, nodes[name], path, top_spec=specs[name])
            else:
                self.add("dispatch_parameter_contract_unverified", "Wire parameter lacks the Skill contract needed to verify required/nested constraints", path,
                         severity="unverified", actual=name)
        count, ids = params.get("容器数量"), params.get("容器编号")
        if isinstance(ids, list) and isinstance(count, int) and not isinstance(count, bool) and count != len(ids):
            self.add("dispatch_container_count_mismatch", "Container count does not match the identifier array", _ptr(_ptr(pointer, "parameters"), "容器数量"),
                     expected=len(ids), actual=count, related=[_ptr(_ptr(pointer, "parameters"), "容器编号")])

    def semantic_projection(self, steps: list[Any]) -> tuple[dict[str, Any], bool, list[dict[str, str]]]:
        """Reverse documented field aliases for other deterministic checkers.

        This is a separate derived view, never an input rewrite or a repair.
        Required semantic-only fields are not guessed. Callers must honor the
        completeness flag before claiming full semantic/state coverage.
        """
        result: list[dict[str, Any]] = []
        field_maps: list[dict[str, str]] = []
        complete = True
        for raw in steps:
            if not isinstance(raw, dict) or not isinstance(raw.get("parameters"), dict):
                complete = False
                continue
            station = raw.get("workstation")
            operation = raw.get("operation")
            if not isinstance(station, str) or not isinstance(operation, str):
                complete = False
                continue
            skill = self.catalog.skill_for(station)
            nodes = self.catalog.nodes_for(station, operation)
            if skill is None or nodes is None:
                complete = False
                continue
            choices = [name for name in skill.operations if self.catalog.resolve_operation(station, name) == operation]
            if len(choices) != 1:
                complete = False
                continue
            params: dict[str, Any] = {}
            field_map: dict[str, str] = {}
            for wire_name, raw_value in raw["parameters"].items():
                node = nodes.get(wire_name)
                if node is None:
                    complete = False
                    continue
                value = copy.deepcopy(raw_value)
                declared = node.type_name.lower()
                normalized = normalize_contract_scalar(
                    value, node.type_name, node.unit
                )
                if normalized.accepted:
                    value = normalized.new_value
                elif isinstance(value, (int, float)) and not isinstance(value, bool) and declared in {"string", "str"}:
                    value = format(value, "g")
                params[node.name] = value
                field_map[node.name] = wire_name
            if any(node.required and name not in params for name, node in skill.operations[choices[0]].parameters.items()):
                complete = False
            step = copy.deepcopy(raw)
            step.update(workstation=skill.code, operation=choices[0], parameters=params)
            result.append(step)
            field_maps.append(field_map)
        return {"steps": result}, complete and len(result) == len(steps), field_maps


def check_wire_payload(workflow: Any, payload: Any, *, workstation_root: Path,
                       workflow_pointer: str = "/workflow_json", payload_pointer: str = "/dispatch_payload") -> dict[str, Any]:
    """Check a supplied wire envelope or a deterministic preview (payload=None).

    Findings point to zero-based array positions, retain the original step
    number, and include the selected contract's source.  Inputs are never
    changed.  No self-reported validation/formatting summaries are consumed.
    """
    root = Path(workstation_root).expanduser().resolve()
    source = "preview" if payload is None else "supplied"
    try:
        catalog = _ExplicitCatalog(root)
    except (OSError, ValueError, TypeError) as exc:
        return {"findings": [{
            "code": "dispatch_contract_unavailable", "severity": "unverified", "stage": "dispatch_payload",
            "message": f"Cannot load selected platform contract: {exc}", "step_index": None, "step_number": None,
            "json_pointer": payload_pointer, "related_pointers": [], "workstation": "", "operation": "",
            "expected": str(root / CONVERSION_FILENAME), "actual": None,
            "skill_path": str(root / CONVERSION_FILENAME),
        }], "checked_steps": 0, "source": source}
    checker = _WireCheck(catalog, workflow_pointer, payload_pointer)
    for issue in catalog.export_issues:
        checker.add("dispatch_export_invalid", issue, payload_pointer, severity="unverified", source=str(catalog.export_path))
    standalone = workflow is None and payload is not None
    workflow_steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if not standalone and not isinstance(workflow_steps, list):
        checker.add("dispatch_source_workflow_invalid", "Source workflow must contain a steps array", workflow_pointer)
        return {"findings": checker.findings, "checked_steps": 0, "source": source}
    if not standalone:
        checker.source_mapping(workflow_steps)
    # Safe, bounded formatter reuse: catalog loading and resolution are explicit
    # and strict; omissions/collisions are independently identified above.
    plan_name = payload.get("plan_name", "") if isinstance(payload, dict) else ""
    try:
        formatted = format_dispatch_payload(copy.deepcopy(workflow), catalog, plan_name=plan_name) if not standalone else None
    except (ValueError, TypeError, OverflowError) as exc:
        checker.index, checker.step, checker.skill = None, {}, None
        checker.add("dispatch_conversion_invalid", f"Deterministic wire conversion could not represent the source: {exc}", workflow_pointer)
        formatted = None
    expected = formatted["payload"] if formatted is not None else None
    actual = expected if payload is None else payload
    checker.index, checker.step, checker.skill = None, {}, None
    if not isinstance(actual, dict):
        checker.add("dispatch_envelope_invalid", "Wire payload must be an object", payload_pointer, actual=actual)
        return {"findings": checker.findings, "checked_steps": 0, "source": source, "expected_payload": expected}
    for name in sorted(actual.keys() - {"experiment_steps", "plan_name"}):
        checker.add("dispatch_unknown_envelope_field", "Unexpected wire envelope field", _ptr(payload_pointer, name), actual=actual[name])
    if not isinstance(actual.get("plan_name"), str) or not actual["plan_name"].strip():
        checker.add("dispatch_plan_name_invalid", "plan_name must be a nonempty string", _ptr(payload_pointer, "plan_name"), actual=actual.get("plan_name"))
    experiment = actual.get("experiment_steps")
    if not isinstance(experiment, dict) or not isinstance(experiment.get("steps"), list):
        checker.add("dispatch_envelope_invalid", "experiment_steps.steps must be an array", _ptr(payload_pointer, "experiment_steps"), actual=experiment)
        return {"findings": checker.findings, "checked_steps": 0, "source": source, "expected_payload": expected}
    for key in sorted(experiment.keys() - {"steps", "unknown_steps"}):
        checker.add("dispatch_unknown_envelope_field", "Unexpected experiment_steps field", _ptr(_ptr(payload_pointer, "experiment_steps"), key), actual=experiment[key])
    if experiment.get("unknown_steps") not in (None, []):
        checker.add("dispatch_unknown_steps", "Unresolved workflow steps cannot be dispatched", _ptr(_ptr(payload_pointer, "experiment_steps"), "unknown_steps"), actual=experiment["unknown_steps"])
    steps = experiment["steps"]
    if not steps:
        checker.add("dispatch_empty_steps", "Wire step list must not be empty", _ptr(_ptr(payload_pointer, "experiment_steps"), "steps"))
    if not standalone and len(steps) != len(workflow_steps):
        checker.add("dispatch_step_count_mismatch", "Wire step count does not match source workflow", _ptr(_ptr(payload_pointer, "experiment_steps"), "steps"),
                    expected=len(workflow_steps), actual=len(steps), related=[_ptr(workflow_pointer, "steps")])
    seen: dict[int, str] = {}
    for index, step in enumerate(steps):
        checker.payload_step(step, index)
        pointer = _ptr(_ptr(_ptr(payload_pointer, "experiment_steps"), "steps"), index)
        if isinstance(step, dict) and isinstance(step.get("step_number"), int) and not isinstance(step["step_number"], bool):
            number = step["step_number"]
            if number in seen:
                checker.add("dispatch_duplicate_step_number", "Wire step_number repeats an earlier step", _ptr(pointer, "step_number"), actual=number, related=[seen[number]])
            seen[number] = _ptr(pointer, "step_number")
        if expected is not None and index < len(expected["experiment_steps"]["steps"]):
            checker.compare(expected["experiment_steps"]["steps"][index], step, pointer, _ptr(_ptr(workflow_pointer, "steps"), index))
    semantic_workflow, projection_complete, field_maps = checker.semantic_projection(steps)
    result = {"findings": checker.findings, "checked_steps": len(steps), "source": source,
              "source_correspondence": "not_available" if standalone else ("checked" if expected is not None else "unverified"),
              "semantic_workflow": semantic_workflow, "semantic_projection_complete": projection_complete,
              "semantic_parameter_names": field_maps}
    if expected is not None:
        result["expected_payload"] = expected
    return result
