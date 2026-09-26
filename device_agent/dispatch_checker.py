"""Offline, non-mutating dispatch checks against an explicit lab-design-all snapshot.

This module has no model, network, dotenv or execution dependencies. A passing
report concerns static contracts, not live instrument availability or chemistry.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

try:
    from .dispatch_formatter import (
        CONVERSION_FILENAME,
        CURATED_OPERATION_ALIASES,
        CURATED_STATION_ALIASES,
        _normalize_name,
        audit_material_execution_dispatch_guards,
    )
except ImportError:  # Direct CLI compatibility.
    from dispatch_formatter import (
        CONVERSION_FILENAME,
        CURATED_OPERATION_ALIASES,
        CURATED_STATION_ALIASES,
        _normalize_name,
        audit_material_execution_dispatch_guards,
    )

try:
    from .skill_contract_audit import (
        LABEL_VALUE_RE, MODULE_DIRS, OPERATION_HEADING_RE, ParameterNode,
        _dynamic_pattern, _json_type, _normalize_type, _range_from_node,
        _type_matches, _value_in_range, load_workstation_catalog,
    )
except ImportError:  # Direct CLI compatibility.
    from skill_contract_audit import (
        LABEL_VALUE_RE, MODULE_DIRS, OPERATION_HEADING_RE, ParameterNode,
        _dynamic_pattern, _json_type, _normalize_type, _range_from_node,
        _type_matches, _value_in_range, load_workstation_catalog,
    )
try:
    from .reagent_slot_identity import (
        build_reagent_slot_registry,
        collect_material_identity_registry_evidence,
        validate_reagent_slot_identity_roots,
        validate_reagent_slot_identities,
    )
except ImportError:  # Direct CLI compatibility.
    from reagent_slot_identity import (
        build_reagent_slot_registry,
        collect_material_identity_registry_evidence,
        validate_reagent_slot_identity_roots,
        validate_reagent_slot_identities,
    )

DEFAULT_WORKSTATION_ROOT = (
    Path(__file__).resolve().parents[1] / "chem_resources" / "lab-design-all"
    / "skills" / "chemistry-experiment-workstation"
)
REPORT_VERSION = "1.0"
_MISSING = object()
_TYPES = {"int", "integer", "float", "number", "double", "string", "str",
          "array", "list", "matrix", "object", "dict", "bool", "boolean", "file", "url"}


def _ptr(parent: str, key: Any) -> str:
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_number": str(value)}
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (RecursionError, TypeError, ValueError):
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_manifest(root: Path) -> dict[str, Any]:
    paths = [root / "SKILL.md", root / "0410数据转换.txt", root / "工作站名称中英文对照.md"]
    for directory in MODULE_DIRS:
        paths.extend(sorted((root / directory).glob("*/SKILL.md")))
    paths.extend(sorted((root / "references_audit").glob("*.md")))
    files = []
    for path in paths:
        if path.is_file():
            files.append({"path": path.relative_to(root).as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"root": str(root), "files": files, "sha256": _digest(files)}


def _all_nodes(nodes: dict[str, ParameterNode]):
    for node in nodes.values():
        yield node
        yield from _all_nodes({child.name: child for child in node.children})


def _io_rules(content: str) -> dict[str, dict[str, list[tuple[int, str]]]]:
    """Keep exact source lines; operation-local sections override common I/O."""
    result: dict[str, dict[str, list[tuple[int, str]]]] = {"": {"input": [], "output": []}}
    operation, direction, section = "", "", ""
    for number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        match = OPERATION_HEADING_RE.match(line)
        if match:
            operation, direction = match.group(1), ""
            result.setdefault(operation, {"input": [], "output": []})
        heading = re.match(r"^#{1,4}\s*(.+)", line)
        if heading:
            section = heading.group(1)
            direction = "input" if section == "输入约束" else "output" if section == "输出约束" else ""
        plain = line.replace("**", "")
        if re.match(r"^-?\s*输入约束\s*[:：]?$", plain):
            direction = "input"
            continue
        if re.match(r"^-?\s*输出约束\s*[:：]?$", plain):
            direction = "output"
            continue
        if direction and plain and not plain.startswith("|"):
            result.setdefault(operation, {"input": [], "output": []})[direction].append((number, plain))
    return result


def _io_value(lines: list[tuple[int, str]], name: str) -> tuple[str, int | None]:
    for number, line in lines:
        match = re.match(r"^-?\s*" + re.escape(name) + r"\s*[:：]\s*(.+)", line)
        if match:
            return match[1].strip().rstrip("。"), number
    return "", None


def _sample_states(text: str) -> set[str] | None:
    """Recognize a bounded vocabulary; None is unknown, never an inferred state."""
    text = re.sub(r"^必须(?:为)?", "", text).replace("分散均匀的", "").strip().rstrip("。")
    if text.endswith("状态"):
        text = text[:-2]
    tokens = {
        "无样品": {"empty"}, "无样品状态": {"empty"}, "空": {"empty"},
        "无溶液": {"empty", "solid", "powder"}, "固体": {"solid", "powder"},
        "粉末": {"powder"}, "纯液态": {"liquid"}, "悬浊液": {"suspension"},
        "上层上清液和下层固体沉淀": {"separated"},
    }
    result: set[str] = set()
    for part in text.split("或"):
        if part.strip() not in tokens:
            return None
        result.update(tokens[part.strip()])
    return result or None


@dataclass
class _LiquidTotal:
    # Use the decimal spelling of JSON numbers, not their binary float value.
    # Fractions avoid both repeated-addition drift and rounding away small excesses.
    amount: Fraction = Fraction(0)
    origins: list[str] = field(default_factory=list)
    bottles: set[str] = field(default_factory=set)

    def record(self, volume: Fraction, bottle: str, pointer: str) -> None:
        self.amount += volume
        self.origins.append(pointer)
        self.bottles.add(bottle)


class _Checker:
    def __init__(self, payload: Any, root: Path, artifact_root: Path | None,
                 initial_state: Any, require_payload: bool):
        self.original = payload
        self.input_sha256 = _digest(payload)
        self.root, self.artifact_root = root, artifact_root
        self.initial_state, self.require_payload = initial_state, require_payload
        self.findings: list[dict[str, Any]] = []
        self.stations, self.aliases = {}, {}
        self.contents: dict[str, str] = {}
        self.rules: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {}
        self.index: int | None = None
        self.step: dict[str, Any] = {}
        self.station = None
        self.workflow_pointer = "/workflow_json"
        self.steps: list[Any] = []
        self.states: dict[tuple[str, int], dict[str, Any]] = {}
        self.source_bottles: dict[tuple[str, str], tuple[str, str]] = {}
        self.liquid_totals: dict[tuple[str, str, int, str], _LiquidTotal] = {}
        self.cross_source_totals: dict[tuple[str, str, int, str], _LiquidTotal] = {}
        self.reagent_slot_bindings: dict[tuple[str, int], Any] = {}
        self.authoritative_slot_registry = False
        self.executed: set[str] = {"input"}
        self.assumptions: list[str] = []
        self.payload_source = "none"
        self.source_correspondence = "unverified"
        self.file_dependencies: set[str] = set()
        # P5: planned vs verified runtime sample-state layering.
        self._wire_operations_cache: set[tuple[str, str]] | None = None
        self._material_context_cache: Any = None

    def add(self, code: str, message: str, pointer: str, *, stage: str = "workflow_contract",
            severity: str = "error", expected: Any = _MISSING, actual: Any = _MISSING,
            node: ParameterNode | None = None, related: list[str] | None = None,
            suggestion: str = "", **extra: Any) -> None:
        item: dict[str, Any] = {"code": code, "severity": severity, "stage": stage,
                                "message": message, "json_pointer": pointer}
        if self.index is not None:
            item.update(step_index=self.index, step_number=_safe(self.step.get("step_number")),
                        workstation=self.step.get("workstation", ""), operation=self.step.get("operation", ""))
            for key in (
                "source_plan_step",
                "source_macro_step_id",
                "source_macro_step",
                "macro_action_id",
                "observation_point_id",
            ):
                if key in self.step:
                    item[key] = _safe(self.step[key])
        if self.station is not None:
            item.update(resolved_workstation=self.station.code, skill_path=str(self.station.skill_path))
        if node:
            item["skill_line"] = node.line
        if expected is not _MISSING:
            item["expected"] = _safe(expected)
        if actual is not _MISSING:
            item["actual"] = _safe(actual)
        if related:
            item["related_pointers"] = list(dict.fromkeys(related))
        if suggestion:
            item["suggestion"] = suggestion
        item.update(extra)
        self.findings.append(item)

    def check_json_values(self, value: Any, pointer: str = "") -> None:
        if isinstance(value, float) and not math.isfinite(value):
            self.add("non_finite_number", "JSON 数值必须有限，不能是 NaN 或 Infinity。", pointer,
                     stage="input", actual=value)
        elif isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    self.add("invalid_json_key", "JSON 对象键必须为字符串。", pointer, stage="input", actual=key)
                self.check_json_values(child, _ptr(pointer, key))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                self.check_json_values(child, _ptr(pointer, i))
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            self.add("invalid_json_value", "输入包含非 JSON 值。", pointer, stage="input", actual=value)

    def load_contracts(self) -> bool:
        self.executed.add("contracts")
        try:
            self.manifest = _source_manifest(self.root)
            self.stations, self.aliases = load_workstation_catalog(self.root)
        except (OSError, ValueError) as exc:
            self.add("contract_load_error", f"设备合同读取失败：{exc}", "", stage="contracts", severity="unverified")
            return False
        if not self.stations:
            self.add("missing_workstation_contracts", "指定目录未找到工作站 Skill，不能跳过检查。", "",
                     stage="contracts", severity="unverified", actual=str(self.root))
            return False
        declarations: dict[str, str] = {}
        for directory in MODULE_DIRS:
            for path in sorted((self.root / directory).glob("*/SKILL.md")):
                declaration = re.search(r"^name\s*:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), re.M)
                code = declaration[1] if declaration else path.parent.name
                if code in declarations:
                    self.add("contract_name_conflict", "多个 Skill 目录声明了相同工作站名称。", "", stage="contracts",
                             severity="unverified", actual=code, source_paths=[declarations[code], str(path)])
                declarations[code] = str(path)
        seen_ids: dict[int, str] = {}
        for code, station in self.stations.items():
            self.contents[code] = station.skill_path.read_text(encoding="utf-8")
            self.rules[code] = _io_rules(self.contents[code])
            if station.station_id is not None:
                if station.station_id in seen_ids:
                    self.add("contract_id_conflict", "不同工作站声明了相同 ID。", "", stage="contracts",
                             severity="unverified", actual=[seen_ids[station.station_id], code])
                seen_ids[station.station_id] = code
        # Include Chinese headings as explicit source aliases; no fuzzy cross-version guesses.
        for code, content in self.contents.items():
            title = re.search(r"^#\s+([^\n]+)", content, re.M)
            if title:
                alias = title.group(1).strip()
                previous = self.aliases.get(alias)
                if previous and previous != code:
                    self.add("contract_alias_conflict", "设备名称对应多个工作站。", "", stage="contracts",
                             severity="unverified", actual=alias)
                else:
                    self.aliases[alias] = code
        return True

    def resolve(self, name: Any):
        if not isinstance(name, str):
            return None
        code = self.aliases.get(name.strip()) or self.aliases.get(name.strip().replace(" ", ""))
        return self.stations.get(code)

    def check_reagent_slot_identities(
        self,
        package: dict[str, Any],
        prefix: str,
        workflow: dict[str, Any],
    ) -> None:
        """Run the shared identity rule in read-only checker mode.

        Projection and legacy migration belong upstream.  This method only
        validates the package registry and the workflow references, then maps
        their structured pointers into the checker envelope.
        """
        if "reagent_slot_plan" not in package:
            return
        self.executed.add("reagent_slot_identity")
        plan = package.get("reagent_slot_plan")
        plan_steps = package.get("device_plan")
        before = _digest((workflow, plan, plan_steps))
        try:
            registry = build_reagent_slot_registry(
                plan, station_resolver=self.resolve
            )
            validation = validate_reagent_slot_identities(
                workflow,
                plan,
                station_resolver=self.resolve,
                plan_steps=plan_steps,
            )
        except Exception as exc:  # Defensive isolation for a strict checker.
            self.add(
                "reagent_slot_identity_check_internal_error",
                f"原液槽位身份检查未完成：{type(exc).__name__}: {exc}",
                prefix + "/reagent_slot_plan",
                stage="cross_step",
                severity="unverified",
            )
            return
        after = _digest((workflow, plan, plan_steps))
        if after != before:
            self.add(
                "reagent_slot_identity_check_mutated_input",
                "原液槽位身份检查器修改了输入；结果不能作为只读审计依据。",
                prefix + "/reagent_slot_plan",
                stage="checker",
            )
            return
        if registry.ok and registry.bindings:
            registry_evidence = collect_material_identity_registry_evidence(
                package
            )
            root_validation = validate_reagent_slot_identity_roots(
                plan,
                batch_plan=package.get("batch_plan"),
                material_ledger=package.get("material_ledger"),
                material_identity_registry=registry_evidence,
            )
            slot_ids = root_validation.claimed_identity_ids
            untrusted_ids = root_validation.untrusted_identity_ids
            if not root_validation.trusted_identity_ids:
                self.add(
                    "reagent_slot_identity_unanchored",
                    "原液槽位声明了 material_identity_id，但 package 未提供可独立核验的"
                    "根批次、物料身份注册表或含身份 ID 的物料台账；这些自报 ID 不能作为"
                    "跨槽累计限额的权威身份依据。",
                    prefix + "/reagent_slot_plan",
                    stage="cross_step",
                    severity="unverified",
                    material_identity_ids=sorted(slot_ids),
                    rule_id="reagent_slot_identity/v1",
                )
            elif untrusted_ids:
                self.add(
                    "untrusted_reagent_slot_identity",
                    "原液槽位中的 material_identity_id 不属于 package 的可信物料身份根；"
                    "不能用未锚定 ID 拆分同一试剂的累计量。",
                    prefix + "/reagent_slot_plan",
                    stage="cross_step",
                    expected="every slot identity is a member of a trusted material registry",
                    actual=sorted(untrusted_ids),
                    trusted_identity_sources=list(
                        root_validation.evidence_sources
                    ),
                    rule_id="reagent_slot_identity/v1",
                )
            else:
                self.reagent_slot_bindings = dict(registry.bindings)
                self.authoritative_slot_registry = True

        for issue in validation.errors:
            item = dict(issue)
            code = str(item.pop("code", "reagent_slot_identity_error"))
            message = str(item.pop("message", code))
            raw_pointer = str(item.pop("json_pointer", "/reagent_slot_plan"))
            if raw_pointer == "/steps" or raw_pointer.startswith("/steps/"):
                pointer = self.workflow_pointer + raw_pointer
            elif raw_pointer.startswith("/"):
                pointer = prefix + raw_pointer
            else:
                pointer = raw_pointer

            step_match = re.match(r"^/steps/(\d+)(?:/|$)", raw_pointer)
            previous_context = (self.index, self.step, self.station)
            if step_match:
                step_index = int(step_match.group(1))
                if step_index < len(self.steps):
                    step = self.steps[step_index]
                    self.index = step_index
                    self.step = step if isinstance(step, dict) else {}
                    self.station = self.resolve(self.step.get("workstation"))
            item.pop("rule_id", None)
            expected = item.pop("expected", _MISSING)
            actual = item.pop("actual", _MISSING)
            related = item.pop("related_pointers", None)
            self.add(
                code,
                message,
                pointer,
                stage="cross_step",
                expected=expected,
                actual=actual,
                related=related if isinstance(related, list) else None,
                rule_id="reagent_slot_identity/v1",
                **{key: _safe(value) for key, value in item.items()},
            )
            self.index, self.step, self.station = previous_context

    def check_material_execution_guards(
        self,
        package: dict[str, Any],
        prefix: str,
    ) -> None:
        """Fail closed when planned material sidecars lack an executable gate."""

        findings = audit_material_execution_dispatch_guards(package)
        if not findings:
            return
        self.executed.add("material_execution_guards")
        for raw in findings:
            item = dict(raw)
            code = str(item.pop("code", "material_execution_guard_invalid"))
            message = str(item.pop("message", code))
            pointer = str(item.pop("json_pointer", ""))
            if pointer.startswith("/"):
                pointer = prefix + pointer
            stage = str(item.pop("stage", "dispatch_payload"))
            severity = str(item.pop("severity", "error"))
            self.add(
                code,
                message,
                pointer,
                stage=stage,
                severity=severity,
                **{key: _safe(value) for key, value in item.items()},
            )

    def validate_object(self, parameters: dict[str, Any], nodes: dict[str, ParameterNode], pointer: str) -> None:
        matches: dict[str, list[str]] = {name: [] for name in nodes}
        for key, value in parameters.items():
            matching = [name for name in nodes if key == name or
                        (_dynamic_pattern(name) and _dynamic_pattern(name).fullmatch(key))]
            if not matching:
                self.add("unknown_parameter", "参数未在当前工作站操作中声明。", _ptr(pointer, key),
                         expected=list(nodes), actual=value,
                         suggestion="按当前操作的参数表修改字段，不能引用其他版本的字段名。")
                continue
            if len(matching) != 1:
                self.add("ambiguous_parameter_contract", "参数同时匹配多个设备定义。", _ptr(pointer, key),
                         severity="unverified", expected=matching, actual=value)
                continue
            name = matching[0]
            matches[name].append(key)
            node = nodes[name]
            dynamic = _dynamic_pattern(name)
            if dynamic:
                captured = dynamic.fullmatch(key)
                if not captured:
                    self.add("uninstantiated_dynamic_parameter", "N 占位符必须替换为实际瓶号。", _ptr(pointer, key), node=node)
                else:
                    bound = re.search(r"(?:范围|取值范围(?:为)?)\s*(\d+)号?\s*[~～至-]\s*(\d+)号?", node.evidence_text)
                    number = int(captured.group(1))
                    if bound and not int(bound[1]) <= number <= int(bound[2]):
                        self.add("dynamic_parameter_range", "动态瓶号超出 Skill 范围。", _ptr(pointer, key), node=node,
                                 expected=[int(bound[1]), int(bound[2])], actual=number)
            self.validate_value(value, node, _ptr(pointer, key))
        for name, node in nodes.items():
            if node.required and not matches[name]:
                self.add("missing_required_parameter", "缺少设备必填参数。", _ptr(pointer, name), node=node,
                         expected={"type": node.type_name, "unit": node.unit},
                         suggestion="补齐该参数；检查器不会自动补值或覆盖原始输出。", missing=True,
                         parent_pointer=pointer)

    def validate_value(self, value: Any, node: ParameterNode, pointer: str) -> None:
        declared = _normalize_type(node.type_name)
        if declared not in _TYPES:
            self.add("unsupported_contract_type", "设备参数类型未声明或检查器尚不支持。", pointer,
                     severity="unverified", expected=node.type_name, actual=value, node=node)
            return
        if not _type_matches(value, declared):
            self.add("type_mismatch", "参数的 JSON 类型与设备合同不一致。", pointer, node=node,
                     expected={"type": declared, "unit": node.unit}, actual=value, actual_type=_json_type(value))
            return
        if isinstance(value, float) and not math.isfinite(value):
            return  # Already reported by strict JSON traversal.
        if declared in {"file", "url"}:
            self.check_file(value, pointer, node)
        pairs = LABEL_VALUE_RE.findall(node.evidence_text)
        if pairs:
            allowed: list[Any] = []
            for _label, token in pairs:
                token = token.strip()
                if declared in {"int", "integer"} and re.fullmatch(r"-?\d+", token):
                    allowed.append(int(token))
                elif declared in {"float", "number", "double"}:
                    try:
                        allowed.append(float(token))
                    except ValueError:
                        pass
                else:
                    allowed.append(token)
            if value not in allowed:
                self.add("invalid_enum_value", "参数不是设备允许的枚举编码。", pointer, node=node,
                         expected=allowed, actual=value, suggestion="按 Skill 中 value 列填写，label 仅用于显示。")
        elif isinstance(value, str) and node.example and re.fullmatch(r"[\w. +℃-]+(?:或|/|、)[\w. +℃/、或-]+", node.example):
            allowed = re.split(r"或|/|、", node.example)
            if value not in allowed:
                self.add("invalid_enum_value", "参数不在设备列出的选项中。", pointer, node=node,
                         expected=allowed, actual=value)
        bounds = _range_from_node(node)
        if bounds and declared not in {"array", "list", "matrix", "object", "dict"}:
            if not _value_in_range(value, bounds):
                self.add("value_out_of_range", "参数超出设备允许范围。", pointer, node=node,
                         expected={"min": bounds[0], "max": bounds[1], "include_min": bounds[2],
                                   "include_max": bounds[3], "unit": node.unit}, actual=value)
        if node.children:
            children = {child.name: child for child in node.children}
            if isinstance(value, list):
                for i, child in enumerate(value):
                    if not isinstance(child, dict):
                        self.add("type_mismatch", "该数组元素必须是包含子参数的对象。", _ptr(pointer, i),
                                 node=node, expected="object", actual=child)
                    else:
                        self.validate_object(child, children, _ptr(pointer, i))
            elif isinstance(value, dict):
                self.validate_object(value, children, pointer)
        if node.name in {"容器编号", "样品载体编号"} and isinstance(value, list):
            seen: dict[int, int] = {}
            for i, item in enumerate(value):
                if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                    self.add("invalid_container_id", "容器编号必须是正整数。", _ptr(pointer, i), node=node, actual=item)
                elif item in seen:
                    self.add("duplicate_container_id", "同一步骤重复指定同一容器。", _ptr(pointer, i), node=node,
                             actual=item, related=[_ptr(pointer, seen[item])])
                else:
                    seen[item] = i

    def check_file(self, value: str, pointer: str, node: ParameterNode) -> None:
        self.executed.add("files")
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://|^\\\\", value):
            self.add("remote_file_not_verified", "必需文件位于外部地址，本地检查无法核实。", pointer,
                     stage="files", severity="unverified", actual=value, node=node)
            return
        if (os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", value)) or (os.name == "nt" and value.startswith("/")):
            self.add("foreign_file_path", "文件路径属于另一运行环境，需要明确的路径映射。", pointer,
                     stage="files", severity="unverified", actual=value, node=node)
            return
        candidate = Path(value)
        if not candidate.is_absolute():
            if self.artifact_root is None:
                self.add("missing_artifact_root", "相对文件路径需要输入文件位置或 --artifact-root。", pointer,
                         stage="files", severity="unverified", actual=value, node=node)
                return
            candidate = self.artifact_root / candidate
        candidate = candidate.resolve()
        self.file_dependencies.add(str(candidate))
        try:
            if not value.strip() or not candidate.is_file():
                self.add("missing_input_file", "设备所需的本地文件不存在。", pointer, stage="files", node=node,
                         expected="existing readable file", actual=value, resolved_path=str(candidate))
                return
            with candidate.open("rb") as stream:
                prefix = stream.read(8)
            if not prefix:
                self.add("empty_input_file", "设备所需的文件为空。", pointer, stage="files", node=node, actual=value)
            elif candidate.suffix.lower() == ".xlsx" and not prefix.startswith(b"PK"):
                self.add("invalid_input_file_type", "文件名为 XLSX，但内容不符合其容器格式。", pointer,
                         stage="files", node=node, actual=value)
        except OSError as exc:
            self.add("unreadable_input_file", f"无法读取所需文件：{exc}", pointer, stage="files", node=node, actual=value)

    def _wire_contract_operations(self) -> set[tuple[str, str]]:
        """(platform station, operation) pairs with a wire contract in the
        selected export.  An absent/unreadable export means nothing is
        runtime-verified (fail closed)."""
        if self._wire_operations_cache is not None:
            return self._wire_operations_cache
        operations: set[tuple[str, str]] = set()
        try:
            raw = (self.root / CONVERSION_FILENAME).read_text(encoding="utf-8-sig")
            payload = json.loads(raw)
        except (OSError, ValueError):
            payload = None
        steps = payload.get("steps") if isinstance(payload, dict) else None
        if isinstance(steps, list):
            for item in steps:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("workstation"), str)
                    and isinstance(item.get("operation"), str)
                ):
                    operations.add((item["workstation"], item["operation"]))
        self._wire_operations_cache = operations
        return operations

    def _step_runtime_verified(self, operation: str) -> bool:
        """True only when the selected export carries a wire contract for
        this station + operation -- the platform evidence that the declared
        output state was actually produced."""
        station = self.station
        if station is None:
            return False
        contracts = self._wire_contract_operations()
        if not contracts:
            return False
        station_names = {station.code, station.display_name}
        for name in list(station_names):
            alias_target = CURATED_STATION_ALIASES.get(name)
            if alias_target:
                station_names.add(alias_target)
        matched_stations = {
            contracted
            for contracted, _ in contracts
            if contracted in station_names
            or any(
                _normalize_name(name) == _normalize_name(contracted)
                for name in station_names
            )
        }
        if not matched_stations:
            return False
        operation_names = {operation}
        operation_names.update(CURATED_OPERATION_ALIASES.get(operation, []))
        operation_names.update(
            alias
            for alias, targets in CURATED_OPERATION_ALIASES.items()
            if operation in targets
        )
        for contracted_station in matched_stations:
            for contracted, contracted_op in contracts:
                if contracted != contracted_station:
                    continue
                if contracted_op in operation_names:
                    return True
                if any(
                    _normalize_name(name) == _normalize_name(contracted_op)
                    for name in operation_names
                ):
                    return True
        return False

    def _material_context(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """device_plan steps and material transitions from the package
        payload, used as internal material-semantic evidence for the
        planned sample-state layer."""
        if self._material_context_cache is not None:
            return self._material_context_cache
        plan_steps: dict[str, Any] = {}
        transitions: dict[str, Any] = {}
        payload = self.original
        if isinstance(payload, dict):
            device_plan = payload.get("device_plan")
            if isinstance(device_plan, list):
                for item in device_plan:
                    if isinstance(item, dict) and item.get("plan_step") is not None:
                        plan_steps[str(item.get("plan_step"))] = item
            material_transitions = payload.get("material_transitions")
            if isinstance(material_transitions, list):
                for item in material_transitions:
                    if isinstance(item, dict):
                        transition_id = str(item.get("transition_id") or "").strip()
                        if transition_id:
                            transitions[transition_id] = item
        self._material_context_cache = (plan_steps, transitions)
        return self._material_context_cache

    def _step_material_state_change(self, step: dict[str, Any] | None) -> bool:
        """Whether the plan declares a material state change at this step
        (workflow material_event_kind or the bound device_plan step)."""
        if not isinstance(step, dict):
            return False
        if str(step.get("material_event_kind") or "").strip() == "state_change":
            return True
        plan_steps, _ = self._material_context()
        plan_step = plan_steps.get(str(step.get("source_plan_step") or "").strip())
        if isinstance(plan_step, dict):
            return (
                str(plan_step.get("material_event_kind") or "").strip() == "state_change"
            )
        return False

    def _planned_after_sample_states(self, step: dict[str, Any] | None) -> set[str] | None:
        """Sample states declared by the bound material transitions'
        after-states, when the sidecar carries structured evidence."""
        if not isinstance(step, dict):
            return None
        plan_steps, transitions = self._material_context()
        plan_step = plan_steps.get(str(step.get("source_plan_step") or "").strip())
        transition_ids: list[str] = []
        if isinstance(step.get("material_transition_ids"), list):
            transition_ids.extend(
                str(value).strip()
                for value in step.get("material_transition_ids") or []
                if str(value).strip()
            )
        if isinstance(plan_step, dict) and isinstance(
            plan_step.get("material_transition_ids"), list
        ):
            transition_ids.extend(
                str(value).strip()
                for value in plan_step.get("material_transition_ids") or []
                if str(value).strip()
            )
        states: set[str] = set()
        for transition_id in dict.fromkeys(transition_ids):
            transition = transitions.get(transition_id)
            if not isinstance(transition, dict):
                continue
            after_states = transition.get("after_material_states")
            if after_states is None:
                after_states = transition.get("after_material_state")
            if not isinstance(after_states, list):
                after_states = [after_states]
            for value in after_states:
                parsed = _sample_states(str(value or ""))
                if parsed:
                    states |= parsed
        return states or None

    def initialize_state(self) -> None:
        if self.initial_state is None:
            return
        containers = self.initial_state.get("containers") if isinstance(self.initial_state, dict) else None
        if not isinstance(containers, list):
            self.add("invalid_initial_state", "initial_state.containers 必须为数组。", "/initial_state",
                     stage="cross_step", actual=self.initial_state)
            return
        for index, item in enumerate(containers):
            pointer = f"/initial_state/containers/{index}"
            if not isinstance(item, dict):
                self.add("invalid_initial_state", "初始容器状态必须为对象。", pointer, stage="cross_step", actual=item)
                continue
            kind = item.get("container_type", item.get("容器类型"))
            number = item.get("container_id", item.get("容器编号"))
            if not isinstance(kind, str) or not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                self.add("invalid_initial_state", "初始状态缺少容器类型或正整数编号。", pointer, stage="cross_step", actual=item)
                continue
            key = (kind, number)
            if key in self.states:
                self.add("duplicate_initial_container", "初始容器状态重复。", pointer, stage="cross_step", actual=item)
                continue
            state = {"origin": pointer}
            lid = item.get("lid_state")
            if isinstance(lid, str) and lid in {"有盖", "closed", "无盖", "open"}:
                state["lid"] = "有盖" if lid in {"有盖", "closed"} else "无盖"
            elif lid is not None:
                self.add("invalid_initial_state", "lid_state 必须为 有盖/无盖 或 closed/open。", _ptr(pointer, "lid_state"),
                         stage="cross_step", actual=lid)
            volume = item.get("volume_ml")
            if volume is not None:
                if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not math.isfinite(volume) or volume < 0:
                    self.add("invalid_initial_state", "volume_ml 必须是有限非负数。", _ptr(pointer, "volume_ml"), stage="cross_step", actual=volume)
                else:
                    state["volume"] = float(volume)
            if isinstance(item.get("sample_state"), str):
                initial_samples = _sample_states(item["sample_state"])
                state["sample"] = initial_samples
                if initial_samples:
                    state["verified_sample"] = set(initial_samples)
            elif "sample_state" in item:
                self.add("invalid_initial_state", "sample_state 必须是状态字符串。", _ptr(pointer, "sample_state"),
                         stage="input", actual=item["sample_state"])
            self.states[key] = state

    def check_step(self, step: dict[str, Any], pointer: str) -> None:
        self.station = self.resolve(step.get("workstation"))
        if self.station is None:
            self.add("unknown_workstation", "工作站不在指定设备合同目录中。", _ptr(pointer, "workstation"), actual=step.get("workstation"))
            return
        station = self.station
        if station.station_id is None:
            self.add("missing_station_id_contract", "Skill 未提供工作站 ID，无法判定。", _ptr(pointer, "id"),
                     severity="unverified", stage="contracts")
        elif type(step.get("id")) is not int or step.get("id") != station.station_id:
            self.add("workstation_id_mismatch", "工作站 ID 与 Skill 声明不一致。", _ptr(pointer, "id"),
                     expected=station.station_id, actual=step.get("id"), suggestion="使用该工作站 Skill 声明的 ID。")
        operation_name = step.get("operation")
        if not station.operations:
            self.add("missing_operation_contract", "Skill 中没有可解析的操作合同，不能认定操作受支持或不受支持。",
                     _ptr(pointer, "operation"), severity="unverified", stage="contracts", actual=operation_name)
            return
        operation = station.operations.get(operation_name) if isinstance(operation_name, str) else None
        if operation is None:
            self.add("unknown_operation", "操作不属于当前工作站。", _ptr(pointer, "operation"),
                     expected=sorted(station.operations), actual=operation_name)
            return
        if not operation.parameters:
            self.add("missing_operation_contract", "操作没有可解析的参数合同，不能直接判为通过。", _ptr(pointer, "operation"),
                     severity="unverified", stage="contracts")
        parsed_lines = {node.line for op in station.operations.values() for node in _all_nodes(op.parameters)}
        raw_parameter_lines = set()
        for n, raw in enumerate(self.contents[station.code].splitlines(), 1):
            cells = raw.strip().strip("|").split("|")
            if raw.lstrip().startswith("|") and len(cells) >= 3 and cells[2].strip().replace("**", "") in {"是", "否"}:
                raw_parameter_lines.add(n)
        if raw_parameter_lines - parsed_lines:
            self.add("unparsed_contract_rows", "该 Skill 含有未成功解析的参数行。", pointer, stage="contracts",
                     severity="unverified", actual=sorted(raw_parameter_lines - parsed_lines))
        parameters = step.get("parameters")
        if not isinstance(parameters, dict):
            self.add("parameters_not_object", "parameters 必须为对象。", _ptr(pointer, "parameters"), expected="object", actual=parameters)
            return
        before = len(self.findings)
        self.validate_object(parameters, operation.parameters, _ptr(pointer, "parameters"))
        self.check_counts(parameters, operation.parameters, _ptr(pointer, "parameters"))
        schema_failed = any(f["severity"] == "error" for f in self.findings[before:])
        self.check_flow(parameters, operation_name, pointer, schema_failed, step)

    def check_counts(self, params: dict[str, Any], nodes: dict[str, ParameterNode], pointer: str) -> None:
        for count_name, ids_name in (("容器数量", "容器编号"), ("样品载体数量", "样品载体编号")):
            count, identifiers = params.get(count_name), params.get(ids_name)
            if type(count) is int and isinstance(identifiers, list) and count != len(identifiers):
                self.add("container_count_mismatch", "声明数量与编号数组长度不一致。", _ptr(pointer, count_name),
                         expected=len(identifiers), actual=count, related=[_ptr(pointer, ids_name)], node=nodes.get(count_name))
        count_node = nodes.get("容器数量")
        if count_node and type(params.get("容器数量")) is int:
            for kind, low, high in re.findall(r"([^;；]+?)最小\s*(\d+)个?最大\s*(\d+)", count_node.evidence_text):
                if kind.strip() == params.get("容器类型") and not int(low) <= params["容器数量"] <= int(high):
                    self.add("value_out_of_range", "数量超出该容器类型的设备范围。", _ptr(pointer, "容器数量"),
                             expected=[int(low), int(high)], actual=params["容器数量"], node=count_node)
        ids = params.get("容器编号")
        for name, target in (("加样方案", "加样瓶号"), ("开盖编号", "瓶号"), ("关盖编号", "瓶号"),
                             ("开盖的瓶号", "瓶号"), ("关盖的瓶号", "瓶号"),
                             ("开盖瓶号", "瓶号"), ("关盖瓶号", "瓶号")):
            plan = params.get(name)
            if isinstance(ids, list) and isinstance(plan, list):
                targets = [item.get(target) for item in plan if isinstance(item, dict)]
                targets = [int(x) if isinstance(x, str) and re.fullmatch(r"[1-9]\d*", x) else x for x in targets]
                for i, value in enumerate(targets):
                    if type(value) is not int or value <= 0:
                        self.add("invalid_target_container", "目标瓶号必须表示正整数容器编号。",
                                 _ptr(_ptr(_ptr(pointer, name), i), target), actual=value)
                # 每瓶多路物料（如 Ni+Mo 各一行）是合法形状（计划 key_values 声明每瓶各物料用量），
                # 按目标瓶集合与声明容器集合一致性判定，不按行数 multiplicity 判定。
                if all(type(x) is int for x in targets + ids) and set(targets) != set(ids):
                    self.add("container_target_mismatch", "操作的目标瓶号与容器编号不一致。", _ptr(pointer, name),
                             expected=ids, actual=sorted(set(targets)), related=[_ptr(pointer, "容器编号")])

    def check_flow(self, params: dict[str, Any], operation: str, pointer: str, schema_failed: bool, step: dict[str, Any] | None = None) -> None:
        self.executed.add("cross_step")
        kind, ids = params.get("容器类型"), params.get("容器编号")
        if not isinstance(kind, str) or not isinstance(ids, list) or not all(type(x) is int and x > 0 for x in ids):
            return
        rules = self.rules[self.station.code]
        op_rules = rules.get(operation, {})
        inputs = op_rules.get("input") or rules.get("", {}).get("input", [])
        outputs = op_rules.get("output") or rules.get("", {}).get("output", [])
        input_text = "\n".join(line for _, line in inputs)
        output_text = "\n".join(line for _, line in outputs)
        input_lid, lid_line = _io_value(inputs, "容器状态")
        out_lid, _ = _io_value(outputs, "容器状态")
        input_lids = {s for s in ("无盖", "有盖") if s in input_lid}
        output_lids = {s for s in ("无盖", "有盖") if s in out_lid}
        required_lid = next(iter(input_lids)) if len(input_lids) == 1 else ""
        output_lid = next(iter(output_lids)) if len(output_lids) == 1 else ""
        input_sample, sample_line = _io_value(inputs, "样品状态")
        out_sample, output_sample_line = _io_value(outputs, "样品状态")
        allowed_samples = _sample_states(input_sample)
        output_samples = _sample_states(out_sample)
        input_kind, kind_line = _io_value(inputs, "容器类型")
        out_kind, output_kind_line = _io_value(outputs, "容器类型")
        io_failed = False
        # 容器类型列表可用"或"或"及"连接（如纯移液："进样瓶及50ml耐热瓶"）。
        # 只影响对 Skill 原文的读取，不扩大任何操作声明的受理范围。
        if input_kind and input_kind not in {"不限", "与输入保持一致"} and kind not in re.split(r"或|及", input_kind):
            self.add("container_type_conflict", "容器类型不满足该操作的输入约束。", _ptr(_ptr(pointer, "parameters"), "容器类型"),
                     stage="cross_step", expected=input_kind, actual=kind, skill_line=kind_line)
            io_failed = True
        if out_kind and out_kind not in {"与输入保持一致", kind}:
            if not self._verified_container_transition(step, params, kind, ids, out_kind, pointer, output_kind_line):
                self.add("container_transition_unverified", "输出容器发生变化，但缺少可核验的源到目标容器编号映射。", pointer,
                         stage="cross_step", severity="unverified", expected=out_kind, actual=kind, skill_line=output_kind_line,
                         suggestion="提供 container_transition_map：逐对列出源/目标逻辑容器编号。仅证明内部映射完整，不代表平台 wire 映射通过。")
                io_failed = True
        if input_sample and input_sample != "不限" and allowed_samples is None:
            self.add("sample_contract_unverified", "该样品输入条件超出当前规则词表，不能自动放行。", pointer,
                     stage="contracts", severity="unverified", actual=input_sample, skill_line=sample_line)
            io_failed = True
        if out_sample and out_sample not in {"不限", "与输入保持一致"} and output_samples is None:
            self.add("sample_contract_unverified", "该样品输出状态不能确定，不能据此推断后续步骤可执行。", pointer,
                     stage="contracts", severity="unverified", actual=out_sample, skill_line=output_sample_line)
            io_failed = True
        acquisition = operation in {"物料拿取", "容器拿取", "获取容器"}
        if "偶数" in input_text and len(ids) % 2:
            line = next((n for n, text in inputs if "偶数" in text), None)
            self.add("container_balance_error", "设备输入要求偶数个容器。", _ptr(_ptr(pointer, "parameters"), "容器数量"),
                     stage="cross_step", expected="even", actual=len(ids), skill_line=line)
        additions = self.check_additions(params, pointer)
        for number in ids:
            key = (kind, number)
            state = self.states.setdefault(key, {})
            if acquisition and not state:
                # This is an explicit static precondition of the acquisition operation.
                state.update(lid=required_lid or output_lid, volume=0.0, origin=pointer,
                             sample={"empty"}, verified_sample={"empty"})
                assumption = "物料获取步骤的初始容器按其 Skill 输入条件建模；未核实实时库存。"
                if assumption not in self.assumptions:
                    self.assumptions.append(assumption)
            conflict = io_failed
            related = [state["origin"]] if state.get("origin") else []
            if state.get("invalid"):
                self.add("blocked_by_previous_step", f"容器 {kind}/{number} 的前序步骤未通过，后续状态不能确认。", pointer,
                         stage="cross_step", severity="unverified", related=related)
                conflict = True
            elif required_lid:
                if not state.get("lid"):
                    self.add("unknown_initial_lid_state", f"无法确认容器 {kind}/{number} 是否满足盖状态前置条件。", pointer,
                             stage="cross_step", severity="unverified", expected=required_lid, actual="unknown", related=related,
                             suggestion="提供 initial_state，或补齐由设备合同支持的前序步骤。")
                    conflict = True
                elif state["lid"] != required_lid:
                    self.add("lid_state_conflict", f"容器 {kind}/{number} 的盖状态与该操作输入要求冲突。", pointer,
                             stage="cross_step", expected=required_lid, actual=state["lid"], related=related,
                             skill_line=next((n for n, line in inputs if required_lid in line), None))
                    conflict = True
            if allowed_samples is not None and not state.get("invalid"):
                # P5: planned_state != verified_runtime_state.  The verified
                # layer only advances through wire-contract-backed steps (or
                # caller-provided initial_state); the planned layer follows
                # the declared plan.  A conflict against either layer stays
                # an error; a requirement satisfied only by the plan is an
                # unverified signal and must not block downstream analysis.
                current_samples = state.get("sample")
                verified_samples = state.get("verified_sample")
                reference_samples = (
                    verified_samples if verified_samples else current_samples
                )
                if not reference_samples:
                    if state.get("sample_pending_confirmation"):
                        # Upstream evidence existed but an unconfirmed material
                        # state change cleared both layers: keep static analysis
                        # moving without fabricating a state, and without
                        # blocking downstream steps.
                        self.add("sample_state_unverified",
                                 f"容器 {kind}/{number} 的前序物料状态迁移缺少运行时确认，真实样品状态未知。", pointer,
                                 stage="cross_step", severity="unverified", expected=input_sample,
                                 actual="planned=unknown;verified=unknown",
                                 planned=None, verified=None,
                                 related=related, skill_line=sample_line,
                                 suggestion="该平台步骤缺少 wire 合同证据；正式 dispatch 仍需运行时状态确认。")
                    else:
                        self.add("unknown_initial_sample_state", f"无法确认容器 {kind}/{number} 的样品状态。", pointer,
                                 stage="cross_step", severity="unverified", expected=input_sample, actual="unknown",
                                 related=related, skill_line=sample_line,
                                 planned=sorted(current_samples) if current_samples else None,
                                 verified=None,
                                 suggestion="提供 initial_state.sample_state 或补齐有明确输出状态的前序步骤。")
                        conflict = True
                elif reference_samples.isdisjoint(allowed_samples):
                    self.add("sample_state_conflict", f"容器 {kind}/{number} 的样品状态不满足该操作。", pointer,
                             stage="cross_step", expected=input_sample, actual=sorted(reference_samples),
                             related=related, skill_line=sample_line,
                             planned=sorted(current_samples) if current_samples else None,
                             verified=sorted(verified_samples) if verified_samples else None)
                    conflict = True
                elif not reference_samples <= allowed_samples:
                    self.add("ambiguous_sample_state", f"容器 {kind}/{number} 只有部分可能状态满足该操作，需核实真实状态。", pointer,
                             stage="cross_step", severity="unverified", expected=input_sample, actual=sorted(reference_samples),
                             related=related, skill_line=sample_line,
                             planned=sorted(current_samples) if current_samples else None,
                             verified=sorted(verified_samples) if verified_samples else None)
                    conflict = True
                elif not verified_samples:
                    self.add("sample_state_unverified",
                             f"容器 {kind}/{number} 的计划样品状态满足该操作，但缺少运行时确认。", pointer,
                             stage="cross_step", severity="unverified", expected=input_sample,
                             actual="planned=" + ",".join(sorted(reference_samples)) + ";verified=unknown",
                             planned=sorted(reference_samples), verified=None,
                             related=related, skill_line=sample_line,
                             suggestion="该平台步骤缺少 wire 合同证据；正式 dispatch 仍需运行时状态确认。")
            cap = re.search(r"(?:体积|液量)[^\n。；]{0,18}?(?:不超过|不能超过|不得超过|最大(?:为)?|≤)\s*(\d+(?:\.\d+)?)\s*m[lL]", input_text)
            if cap:
                if "volume" not in state:
                    self.add("unknown_container_volume", f"缺少容器 {kind}/{number} 的累计体积，不能确认容量约束。", pointer,
                             stage="cross_step", severity="unverified", expected=f"<= {cap[1]} mL", related=related)
                    conflict = True
                elif state["volume"] > float(cap[1]):
                    self.add("container_volume_exceeded", f"容器 {kind}/{number} 累计体积超出该操作输入范围。", pointer,
                             stage="cross_step", expected=f"<= {cap[1]} mL", actual=state["volume"], related=related)
                    conflict = True
            if schema_failed or conflict:
                state.update(invalid=True, origin=state.get("origin", pointer) if conflict else pointer)
                continue
            if output_lid:
                state["lid"] = output_lid
            elif out_lid == "不限" or len(output_lids) > 1:
                state.pop("lid", None)
            if out_sample and out_sample != "与输入保持一致":
                state["sample"] = output_samples
                state.pop("sample_pending_confirmation", None)
                if output_samples and self._step_runtime_verified(operation):
                    state["verified_sample"] = set(output_samples)
                else:
                    state.pop("verified_sample", None)
            elif out_sample == "不限":
                state["sample"] = output_samples
                state.pop("verified_sample", None)
                state.pop("sample_pending_confirmation", None)
            elif self._step_material_state_change(step):
                # The plan declares a material state change but neither the
                # skill nor the material sidecar names the resulting state:
                # keep both layers unknown rather than faking the old state.
                planned_after = self._planned_after_sample_states(step)
                if planned_after:
                    state["sample"] = set(planned_after)
                    state.pop("sample_pending_confirmation", None)
                else:
                    state.pop("sample", None)
                    state["sample_pending_confirmation"] = True
                state.pop("verified_sample", None)
            if number in additions and "volume" in state:
                state["volume"] += additions[number]
            # Removal/transfer volumes cannot be inferred from arbitrary prose.
            if re.search(r"离心|倾倒|纯移液|清洗", operation):
                state.pop("volume", None)
            state["origin"] = pointer

    def _verified_container_transition(self, step: dict[str, Any] | None, params: dict[str, Any], kind: str,
                                       ids: list[int], out_kind: str, pointer: str, skill_line: int | None) -> bool:
        """Verify an explicit container_transition_map for operations whose declared
        output container kind differs from the input kind (e.g. 进样瓶 -> 料斗).

        The annotation is logical-layer traceability evidence: it pairs the step's
        own source container ids (parameters.容器编号) with target container ids of
        the Skill-declared output kind. It stays in workflow_json; the dispatch
        formatter never copies it into the wire payload, so a verified map proves
        the internal mapping is complete — it does not prove the platform wire
        mapping. A missing map keeps the previous unverified finding; an invalid
        map is an error and also keeps it.
        """
        annotation = step.get("container_transition_map") if isinstance(step, dict) else None
        if annotation is None:
            return False
        p = _ptr(pointer, "container_transition_map")

        def invalid(code: str, message: str, ptr: str | None = None, **kwargs: Any) -> bool:
            self.add(code, message, ptr or p, stage="cross_step", skill_line=skill_line, **kwargs)
            return False

        if not isinstance(annotation, dict):
            return invalid("container_transition_map_invalid", "容器转移映射必须是对象。", actual=annotation)
        source_kind = annotation.get("source_kind")
        target_kind = annotation.get("target_kind")
        pairs = annotation.get("pairs")
        if source_kind != kind:
            return invalid("container_transition_map_invalid", "映射的 source_kind 必须等于本步骤的容器类型。",
                           expected=kind, actual=source_kind)
        if target_kind != out_kind:
            return invalid("container_transition_map_invalid", "映射的 target_kind 必须等于设备合同声明的输出容器类型。",
                           expected=out_kind, actual=target_kind)
        if not isinstance(pairs, list) or not pairs:
            return invalid("container_transition_map_invalid", "映射的 pairs 必须是非空数组。", actual=pairs)
        source_ids: list[int] = []
        target_ids: list[int] = []
        for i, pair in enumerate(pairs):
            pp = _ptr(_ptr(p, "pairs"), i)
            if not isinstance(pair, dict):
                return invalid("container_transition_map_invalid", "映射对必须是对象。", ptr=pp, actual=pair)
            source_id, target_id = pair.get("source_id"), pair.get("target_id")
            if type(source_id) is not int or source_id <= 0 or type(target_id) is not int or target_id <= 0:
                return invalid("container_transition_map_invalid", "映射对的 source_id/target_id 必须是正整数。",
                               ptr=pp, actual=pair)
            source_ids.append(source_id)
            target_ids.append(target_id)
        if sorted(source_ids) != sorted(ids):
            return invalid("container_transition_map_incomplete",
                           "映射必须恰好覆盖本步骤参数中的全部源容器编号。",
                           expected=sorted(ids), actual=sorted(source_ids))
        # 多对一合批（同组成整批并入同一料斗）由计划证据声明，映射层如实承载；
        # 不禁止目标重复，合并授权必须出现在 evidence 中。
        for pair in pairs:
            state = self.states.setdefault((target_kind, pair["target_id"]), {})
            state.update(origin=p)
        return True

    def check_additions(self, params: dict[str, Any], pointer: str) -> dict[int, float]:
        additions: dict[int, float] = {}
        plan = params.get("加样方案")
        if not isinstance(plan, list):
            return additions
        own_description = self.contents[self.station.code].split("## 相似工作站", 1)[0].replace("**", "")
        single = re.search(r"单次(?:最大)?(?:移液量|加液量)[^\d\n]{0,5}(\d+(?:\.\d+)?)\s*m[lL]", own_description)
        total_pattern = r"单种溶液总加注量不超过\s*(\d+(?:\.\d+)?)\s*m[lL]"
        total = re.search(total_pattern, own_description)
        total_limit = Fraction(total[1]) if total else None
        total_line = next((n for n, line in enumerate(own_description.splitlines(), 1)
                           if re.search(total_pattern, line)), None)
        kind = params["容器类型"]  # check_flow has already checked this container key.
        for i, item in enumerate(plan):
            if not isinstance(item, dict):
                continue
            target = item.get("加样瓶号")
            if isinstance(target, str) and re.fullmatch(r"[1-9]\d*", target):
                target = int(target)
            if type(target) is not int or target <= 0:
                continue
            for bottle, source in item.items():
                bottle_match = re.fullmatch(r"(\d+)号原液瓶", bottle)
                if not bottle_match or not isinstance(source, dict):
                    continue
                field = _ptr(_ptr(_ptr(_ptr(_ptr(pointer, "parameters"), "加样方案"), i), bottle), "原液用量")
                volume, name = source.get("原液用量"), source.get("配料名称")
                if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not math.isfinite(volume):
                    continue
                if volume < 0:
                    self.add("negative_liquid_volume", "加液量不能为负数。", field, stage="cross_step", actual=volume)
                    continue  # An invalid removal must never offset later additions.
                if single and volume > float(single[1]):
                    self.add("single_transfer_volume", "单次加液量超过工作站限制。", field, stage="cross_step", expected=float(single[1]), actual=volume)
                # Normalize only ledger keys; retain the original fields and pointers.
                bottle_key = f"{int(bottle_match[1])}号原液瓶"
                label = name.strip() if isinstance(name, str) else None
                identity = (self.station.code, bottle_key)
                binding = self.reagent_slot_bindings.get(
                    (self.station.code, int(bottle_match[1]))
                )
                if not self.authoritative_slot_registry and isinstance(name, str):
                    previous = self.source_bottles.get(identity)
                    if previous and previous[0] != label:
                        self.add("source_bottle_identity_conflict", "同一原液瓶在不同步骤被指派了不同试剂。", field.rsplit("/", 1)[0],
                                 stage="cross_step", expected=previous[0], actual=name, related=[previous[1]])
                    else:
                        self.source_bottles[identity] = (label, field)
                scope = (self.station.code, kind, target)
                source_total = self.liquid_totals.setdefault((*scope, bottle_key), _LiquidTotal())
                source_origins = list(source_total.origins)
                exact_volume = Fraction(str(volume))
                source_total.record(exact_volume, bottle_key, field)
                if total_limit is not None and source_total.amount > total_limit:
                    self.add("total_reagent_volume", "该目标容器的同种原液累计加注量超限。", field,
                             stage="cross_step", expected=float(total_limit), actual=float(source_total.amount),
                             related=source_origins, skill_line=total_line,
                             actual_ml_fraction=str(source_total.amount))
                if (
                    self.authoritative_slot_registry
                    and binding is not None
                    and total_limit is not None
                ):
                    candidate = self.cross_source_totals.setdefault(
                        (*scope, binding.material_identity_id), _LiquidTotal()
                    )
                    candidate_origins = list(candidate.origins)
                    candidate.record(exact_volume, bottle_key, field)
                    known_overflow = any(
                        self.liquid_totals[(*scope, key)].amount > total_limit
                        for key in candidate.bottles
                    )
                    if (
                        len(candidate.bottles) > 1
                        and candidate.amount > total_limit
                        and not known_overflow
                    ):
                        self.add(
                            "total_reagent_volume",
                            "同一物料身份从多个原液槽位向同一目标容器的累计加注量超过单种溶液限值。",
                            field,
                            stage="cross_step",
                            expected=float(total_limit),
                            actual=float(candidate.amount),
                            related=candidate_origins,
                            skill_line=total_line,
                            identity_basis="material_identity_id",
                            material_identity_id=binding.material_identity_id,
                            source_bottles=sorted(
                                candidate.bottles,
                                key=lambda key: int(key.split("号", 1)[0]),
                            ),
                            actual_ml_fraction=str(candidate.amount),
                        )
                elif label is not None and total_limit is not None:
                    # Equal labels are possible identity, not proof of equal composition,
                    # concentration or batch. Block only when that uncertainty affects
                    # the bound; do not invent new machine parameters or merge aliases.
                    candidate = self.cross_source_totals.setdefault((*scope, label), _LiquidTotal())
                    candidate_origins = list(candidate.origins)
                    candidate.record(exact_volume, bottle_key, field)
                    known_overflow = any(
                        self.liquid_totals[(*scope, key)].amount > total_limit for key in candidate.bottles
                    )
                    if len(candidate.bottles) > 1 and candidate.amount > total_limit and not known_overflow:
                        self.add(
                            "cross_source_reagent_identity_unverified",
                            "多个同名原液瓶向同一目标容器的合计加注量超过单种溶液限值；"
                            "若为同一溶液则超限，但现有字段不能确认其成分、浓度或物料身份，不能放行。",
                            field, stage="cross_step", severity="unverified",
                            expected=float(total_limit), actual=float(candidate.amount),
                            related=candidate_origins, skill_line=total_line,
                            identity_basis="matching_labels_only", reagent_label=label,
                            source_bottles=sorted(candidate.bottles, key=lambda key: int(key.split("号", 1)[0])),
                            actual_ml_fraction=str(candidate.amount),
                            suggestion="核实溶液身份与限值统计范围，依据确认的设备合同重新规划并复检；"
                                       "不要通过改名、删步骤、擅自减量或手写通过标记规避检查。",
                        )
                additions[target] = additions.get(target, 0.0) + volume
        return additions

    def run(self) -> dict[str, Any]:
        self.check_json_values(self.original)
        if not isinstance(self.original, dict):
            self.add("invalid_input_structure", "输入必须是 package、workflow 或下发对象。", "", stage="input", actual=self.original)
            return self.finish()
        package, prefix = self.original, ""
        if isinstance(package.get("terminal_package"), dict):
            package, prefix = package["terminal_package"], "/terminal_package"
        self.check_material_execution_guards(package, prefix)
        bare_wire = "workflow_json" not in package and "steps" not in package and "experiment_steps" in package
        if "workflow_json" in package:
            workflow, self.workflow_pointer = package["workflow_json"], prefix + "/workflow_json"
        elif "steps" in package:
            workflow, self.workflow_pointer = package, prefix
        elif "experiment_steps" in package:
            workflow, self.workflow_pointer = None, ""
        else:
            self.add("missing_workflow", "输入中没有 workflow_json.steps 或 experiment_steps.steps。", prefix, stage="input")
            return self.finish()
        if not self.load_contracts():
            return self.finish()
        if not bare_wire:
            if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
                self.add("invalid_workflow_structure", "workflow.steps 必须为数组。", self.workflow_pointer, stage="input", actual=workflow)
                return self.finish()
            self.steps = workflow["steps"]
            self.check_reagent_slot_identities(package, prefix, workflow)
            if not self.steps:
                self.add("empty_workflow", "空工作流不能下发。", self.workflow_pointer + "/steps", stage="input", actual=[])
            self.executed.add("workflow_contract")
            self.initialize_state()
            seen: dict[int, str] = {}
            for i, step in enumerate(self.steps):
                self.index, self.step, self.station = i, step if isinstance(step, dict) else {}, None
                pointer = self.workflow_pointer + f"/steps/{i}"
                if not isinstance(step, dict):
                    self.add("step_not_object", "工作流步骤必须为对象。", pointer, actual=step)
                    continue
                number = step.get("step_number")
                if type(number) is not int or number <= 0:
                    self.add("invalid_step_number", "步骤号必须是正整数。", _ptr(pointer, "step_number"), actual=number)
                elif number in seen:
                    self.add("duplicate_step_number", "步骤号重复；使用数组位置定位各个错误。", _ptr(pointer, "step_number"),
                             actual=number, related=[seen[number]])
                else:
                    seen[number] = _ptr(pointer, "step_number")
                self.check_step(step, pointer)
                returns = step.get("intermediate_returns", [])
                if i < len(self.steps) - 1 and isinstance(returns, list):
                    for j, item in enumerate(returns):
                        if isinstance(item, dict) and item.get("required_for_next_step") and item.get("delivery_mode") in {"observation", "manual_handoff"}:
                            self.add("external_return_wait_required", "后续步骤依赖外部真实返回，必须在该处划分执行边界。",
                                     f"{pointer}/intermediate_returns/{j}", stage="cross_step", actual=item)
        self.index, self.step, self.station = None, {}, None
        for gate in ("quantity_audit", "recipe_materialization", "dispatch_validation"):
            value = package.get(gate)
            if isinstance(value, dict) and value.get("status") in {"failed", "human_review_required"}:
                self.add("upstream_gate_failed", f"Device 的 {gate} 尚未通过。", _ptr(prefix, gate),
                         stage="package", actual=value.get("status"))
        supplied_payload = package if bare_wire else package.get("dispatch_payload")
        payload_pointer = prefix if bare_wire else prefix + "/dispatch_payload"
        if supplied_payload is None and self.require_payload:
            self.add("missing_dispatch_payload", "缺少最终下发 payload，不能放行执行。", payload_pointer,
                     stage="dispatch_payload", severity="unverified")
        self.payload_source = "provided" if supplied_payload is not None else "generated_preview"
        # Do not let a formatter crash on structurally invalid input and hide the primary finding.
        structure_invalid = any(f["code"] in {"step_not_object", "invalid_json_key", "invalid_json_value", "non_finite_number"} for f in self.findings)
        if not structure_invalid:
            try:
                try:
                    from .dispatch_wire_checker import check_wire_payload
                except ImportError:
                    from dispatch_wire_checker import check_wire_payload
                wire = check_wire_payload(workflow, supplied_payload, workstation_root=self.root,
                                          workflow_pointer=self.workflow_pointer, payload_pointer=payload_pointer)
                self.findings.extend(wire.get("findings", []))
                self.executed.add("dispatch_payload")
                self.source_correspondence = wire.get("source_correspondence", "unverified")
                if bare_wire:
                    experiment = package.get("experiment_steps")
                    raw_steps = experiment.get("steps") if isinstance(experiment, dict) else None
                    self.steps = raw_steps if isinstance(raw_steps, list) else []
                    projection = wire.get("semantic_workflow")
                    if wire.get("semantic_projection_complete") is True and isinstance(projection, dict):
                        self.workflow_pointer = payload_pointer + "/experiment_steps"
                        self.initialize_state()
                        self.executed.add("workflow_contract")
                        start = len(self.findings)
                        for i, step in enumerate(projection["steps"]):
                            self.index, self.step, self.station = i, step, None
                            self.check_step(step, self.workflow_pointer + f"/steps/{i}")
                        # Semantic inspection uses documented names only in a derived view.
                        # All findings still refer to fields of the untouched input payload.
                        aliases = []
                        for i, names in enumerate(wire.get("semantic_parameter_names", [])):
                            base = self.workflow_pointer + f"/steps/{i}/parameters"
                            aliases.extend((_ptr(base, canonical), _ptr(base, original))
                                           for canonical, original in names.items() if canonical != original)
                        def original_pointer(value: str) -> str:
                            for canonical, original in aliases:
                                if value == canonical or value.startswith(canonical + "/"):
                                    return original + value[len(canonical):]
                            return value
                        for finding in self.findings[start:]:
                            finding["json_pointer"] = original_pointer(finding["json_pointer"])
                            if "related_pointers" in finding:
                                finding["related_pointers"] = [original_pointer(p) for p in finding["related_pointers"]]
                        self.index, self.step, self.station = None, {}, None
                    else:
                        self.add("wire_semantic_projection_incomplete", "下发字段无法完整对应到 Skill，文件与跨步条件不能全部核验。",
                                 payload_pointer, stage="contracts", severity="unverified")
            except Exception as exc:
                self.add("wire_checker_internal_error", f"下发参数检查未完成：{type(exc).__name__}: {exc}", payload_pointer,
                         stage="dispatch_payload", severity="unverified")
        if _source_manifest(self.root).get("sha256") != self.manifest.get("sha256"):
            self.add("contract_changed_during_check", "检查期间设备合同发生变化，请重新检查。", "", stage="contracts", severity="unverified")
        return self.finish()

    def finish(self) -> dict[str, Any]:
        if (
            _digest(self.original) != self.input_sha256
            and not any(
                item.get("code") == "checker_mutated_input"
                for item in self.findings
            )
        ):
            self.add(
                "checker_mutated_input",
                "检查期间输入对象被修改；本报告不能作为只读检查依据。",
                "",
                stage="checker",
            )
        unique, seen = [], set()
        for finding in self.findings:
            key = _digest(finding)
            if key not in seen:
                unique.append(finding)
                seen.add(key)
        counts = Counter(f.get("severity", "error") for f in unique)
        status = "failed" if counts["error"] else "not_verifiable" if counts["unverified"] else "passed"
        steps = []
        for i, step in enumerate(self.steps):
            related = [f for f in unique if f.get("step_index") == i]
            steps.append({"step_index": i, "step_number": step.get("step_number") if isinstance(step, dict) else None,
                          "status": "failed" if any(f["severity"] == "error" for f in related) else
                          "not_verifiable" if any(f["severity"] == "unverified" for f in related) else "passed",
                          "finding_count": len(related)})
        return {"report_version": REPORT_VERSION, "status": status, "dispatchable": status == "passed",
                "scope": "static_dispatch_contract", "input_sha256": self.input_sha256,
                "source_manifest": self.manifest, "payload_source": self.payload_source,
                "source_correspondence": self.source_correspondence,
                "check_context": {"artifact_root": str(self.artifact_root) if self.artifact_root else None,
                                  "initial_state_sha256": _digest(self.initial_state) if self.initial_state is not None else None},
                "file_dependencies": sorted(self.file_dependencies),
                "summary": {"steps": len(self.steps), "errors": counts["error"], "warnings": counts["warning"],
                            "unverified": counts["unverified"]}, "checked_steps": _safe(steps),
                "checks": {name: "executed" for name in sorted(self.executed)}, "findings": unique,
                "assumptions": self.assumptions,
                "limitations": ["仅核验本地设备合同；未连接设备，未验证实时库存、校准、文件在设备端的可达性或实验效果。",
                                "自然语言规则只覆盖已实现的容器、盖状态、样品状态、加液和等待边界；不等同完整化学语义审核。",
                                (
                                    "原液身份按 package 中工作站局部槽位、material_identity_id 与 canonical_name 的结构化绑定核验；"
                                    "未独立验证浓度、批次内容真实性或跨工作站的累计统计范围。"
                                    if self.authoritative_slot_registry
                                    else
                                    "加液累计按工作站和目标容器类型/编号划分；同名跨源瓶仅作潜在同种溶液的保守上界检查，"
                                    "未核验浓度、批次、不同名称的别名关系或跨工作站的溶液身份及统计范围。"
                                )]}


def check_dispatch(payload: Any, *, source_path: str | Path | None = None,
                   artifact_root: str | Path | None = None, workstation_root: str | Path | None = None,
                   initial_state: Any = None, require_payload: bool = False) -> dict[str, Any]:
    """Check an original package/workflow/wire object; never normalize or mutate it."""
    root = Path(workstation_root).resolve() if workstation_root is not None else DEFAULT_WORKSTATION_ROOT
    artifacts = Path(artifact_root).resolve() if artifact_root is not None else Path(source_path).resolve().parent if source_path else None
    checker = _Checker(payload, root, artifacts, initial_state, require_payload)
    try:
        report = checker.run()
    except Exception as exc:
        checker.add("checker_internal_error", f"检查未完成：{type(exc).__name__}: {exc}", "", stage="checker", severity="unverified")
        report = checker.finish()
    if source_path is not None:
        report["input_path"] = str(Path(source_path).resolve())
    return report


class _DuplicateKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(f"JSON 对象包含重复字段：{key}")
        obj[key] = value
    return obj


def check_dispatch_file(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    """Return a report even for malformed JSON; syntax errors keep line/column."""
    source = Path(path).resolve()
    data = b""
    try:
        data = source.read_bytes()
        payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, ValueError) as exc:
        code = "duplicate_json_key" if isinstance(exc, _DuplicateKey) else "input_read_error" if isinstance(exc, OSError) else "invalid_json"
        finding = {"code": code, "severity": "error", "stage": "input", "message": str(exc), "json_pointer": ""}
        if isinstance(exc, json.JSONDecodeError):
            finding.update(line=exc.lineno, column=exc.colno, offset=exc.pos)
        return {"report_version": REPORT_VERSION, "status": "failed", "dispatchable": False,
                "scope": "static_dispatch_contract", "input_path": str(source),
                "input_sha256": hashlib.sha256(data).hexdigest(), "source_manifest": {},
                "summary": {"steps": 0, "errors": 1, "warnings": 0, "unverified": 0},
                "checked_steps": [], "checks": {"input": "failed"}, "findings": [finding]}
    kwargs.setdefault("source_path", source)
    report = check_dispatch(payload, **kwargs)
    report["input_path"] = str(source)
    report["input_file_sha256"] = hashlib.sha256(data).hexdigest()
    return report


def render_check_report(report: dict[str, Any]) -> str:
    """Readable view of the same structured findings, not a second verdict."""
    labels = {"passed": "PASS", "failed": "FAIL", "not_verifiable": "INCOMPLETE"}
    summary = report.get("summary") or {}
    lines = ["# Workflow 下发检查", "", f"结果：**{labels.get(report.get('status'), report.get('status', 'INCOMPLETE'))}**",
             "", "检查范围：本地静态下发合同。此报告不表示已经执行真实实验。", "",
             f"步骤：{summary.get('steps', 0)}；错误：{summary.get('errors', 0)}；"
             f"待核实：{summary.get('unverified', 0)}；警告：{summary.get('warnings', 0)}。", "",
             f"输入 SHA-256：`{report.get('input_sha256', '')}`", "",
             f"设备合同：`{(report.get('source_manifest') or {}).get('root', '')}`", ""]
    if report.get("input_path"):
        lines += [f"输入文件：`{report['input_path']}`", ""]
    if report.get("payload_source") == "generated_preview":
        lines += ["下发载荷：仅检查确定性生成的预览，输入未提供真实 payload；下发路径指向预览对象。", ""]
    elif report.get("payload_source") == "provided":
        lines += ["下发载荷：检查输入提供的真实 payload。", ""]
    for i, finding in enumerate(report.get("findings", []), 1):
        index = finding.get("step_index")
        position = f"数组第 {index + 1} 项 / 声明步骤 {finding.get('step_number')}" if isinstance(index, int) else "全局"
        lines += [f"## {i}. {position} · {finding.get('code', 'unknown')}", "",
                  f"级别：{finding.get('severity', 'error')}；阶段：{finding.get('stage', '')}", "",
                  str(finding.get("message", "")), "", f"定位：`{finding.get('json_pointer', '') or '/'}`", ""]
        for key, label in (("workstation", "工作站"), ("operation", "操作"), ("expected", "要求"), ("actual", "实际"),
                           ("actual_ml_fraction", "精确累计量（mL，分数表示）"),
                           ("source_bottles", "涉及原液瓶"), ("reagent_label", "同名标签（非身份确认）")):
            if key in finding:
                value = json.dumps(_safe(finding[key]), ensure_ascii=False)
                lines += [f"{label}：`{value}`", ""]
        if finding.get("line"):
            lines += [f"JSON 位置：第 {finding['line']} 行，第 {finding.get('column')} 列。", ""]
        if finding.get("skill_path"):
            path = str(finding["skill_path"]).replace("\\", "/")
            suffix = f":{finding['skill_line']}" if finding.get("skill_line") else ""
            lines += [f"规则依据：[Skill{suffix}](<{path}{suffix}>)", ""]
        if finding.get("related_pointers"):
            lines += ["关联位置：" + "、".join(f"`{p}`" for p in finding["related_pointers"]), ""]
        if finding.get("suggestion"):
            lines += ["建议：" + finding["suggestion"], ""]
    if not report.get("findings"):
        lines += ["已执行的检查未发现违规。", ""]
    for text in report.get("assumptions", []) + report.get("limitations", []):
        lines += [str(text), ""]
    return "\n".join(lines)


def write_check_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    target = Path(output_dir)
    json_path, md_path = target / "workflow_check.json", target / "workflow_check.md"
    protected = list(report.get("file_dependencies", []))
    if report.get("input_path"):
        protected.append(report["input_path"])
    for original in protected:
        source = Path(original).resolve()
        for path in (json_path, md_path):
            if path.resolve() == source or (path.exists() and source.exists() and path.samefile(source)):
                raise ValueError("Report output would overwrite the original workflow input or a referenced file")
    target.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(_safe(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_check_report(report), encoding="utf-8")
    return {"json": str(json_path.resolve()), "markdown": str(md_path.resolve())}
