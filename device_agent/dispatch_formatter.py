"""Deterministic dispatch formatting: harness output → the device platform's
exact parameter form.

The LLM's ``workflow_json`` is semantically validated (WorkflowValidator) but
not guaranteed to use the platform's EXACT form. The platform truth source
(``0410数据转换.txt`` — the platform's own schema export) requires:

- platform station names   (``303物料站``, ``移液平台1ml_V2``, ``十通道磁力搅拌_V1``…)
- platform operation names (``物料拿取``, ``开盖-离心管``, ``烘干主流程``…)
- exact per-version parameter keys (``保留瓶盖`` on 1ml, ``是否保留瓶盖`` on 5ml_V2…)
- platform-declared types  (``恒温温度`` is a STRING, ``保留瓶盖`` is an INT…)
- the numeric station ``id`` (工作站编码 from each SKILL.md header)
- the ``{"experiment_steps": {"steps": […], "unknown_steps": null},
  "plan_name": …}`` envelope consumed by workflow-generator/generate.py

This module converts a validated workflow into that form deterministically.
The original ``workflow_json`` is never modified; anything that cannot be
mapped with confidence is kept as-is and reported in ``warnings`` — the
formatter must never fabricate a mapping.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from utils.paths import workstation_dir

CONVERSION_FILENAME = "0410数据转换.txt"

UNIT_SUFFIX_RE = re.compile(r"[（(]([^（）()]*)[)）]\s*$")
STATION_CODE_RE = re.compile(r"工作站编码[:：]\s*(\d+)")
N_BOTTLE_KEY_RE = re.compile(r"^N号(.+)$")

# Our identifiers (English codes / 对照表 display names / legacy aliases) →
# platform station names where normalization alone cannot bridge them.
CURATED_STATION_ALIASES: Dict[str, str] = {
    "物料站": "303物料站",
    "常规物料站": "303物料站",
    "General_Material_Station_V1": "303物料站",
    "耐热瓶物料站": "303物料-耐热",
    "Heat_Resistant_Material_Station": "303物料-耐热",
    "磁力搅拌工作站": "十通道磁力搅拌_V1",
    "常温磁力搅拌工作站_V1": "十通道磁力搅拌_V1",
    "常温磁力搅拌工作站": "十通道磁力搅拌_V1",
    "Room_Temperture_Magnetic_Stirrer_Workstation_V1": "十通道磁力搅拌_V1",
    "双工位电化学工作站_V2": "双工位电化学_V2",
    "双工位电化学工作站": "双工位电化学_V2",
    "Dual_Station_Electrochemical_Workstation_V2": "双工位电化学_V2",
    "谱学容器中转平台_V1": "谱学置物平台_V1",
    "Spectroscopy_Container_Transfer_Station_V1": "谱学置物平台_V1",
    "谱学磁力搅拌工作站_V1": "谱学磁力搅拌_V1",
    "Spectroscopy_Magnetic_Stirrer_Workstation_V1": "谱学磁力搅拌_V1",
    "容器置放平台_V1": "电催化置物平台_V1",
    "置物工作站": "电催化置物平台_V1",
    "电化学存储工作站": "电催化置物平台_V1",
    "Container_storaging_Station_V1": "电催化置物平台_V1",
    "移液平台四通道_V1": "四通道移液平台_V1",
    "Liquid_Handling_Station_4Channel_V1": "四通道移液平台_V1",
    "单通道固体称量工作站_V1": "单通道固体称量_v1",
    "固体进样工作站": "单通道固体称量_v1",
    "固体称量工作站": "单通道固体称量_v1",
    "Single_Channel_Solid_Weighing_Workstation_V1": "单通道固体称量_v1",
    "多通道固体称量工作站_V1": "多通道固体称量_V1",
    "Multi_Channel_Solid_Weighing_Workstation_V1": "多通道固体称量_V1",
    "液体进样站": "移液平台1ml_V2",
    "移液平台": "移液平台1ml_V2",
    "烘干机": "烘干机_V1",
    "离心机": "离心机_V1",
    "纯化工作站": "纯化工作站_V1",
    "超声清洗工作站": "超声分散仪_V2",
    "超声清洗": "超声分散仪_V2",
    "超声分散仪": "超声分散仪_V2",
    "XRD": "X射线衍射仪_V1",
    "X射线衍射仪": "X射线衍射仪_V1",
}

# Our operation names → platform operation names, applied per target station
# when the exact name is not in that station's platform operation list.
CURATED_OPERATION_ALIASES: Dict[str, List[str]] = {
    "静置烘干": ["烘干主流程"],
    "烘干": ["烘干主流程"],
    "磁力搅拌": ["开始搅拌", "加热磁力搅拌全流程"],
    "搅拌": ["开始搅拌"],
    "开盖": ["开盖-离心管", "开盖"],
    "关盖": ["关盖-离心管", "关盖"],
    "加液": ["加液_物料绑定"],
    "移液": ["纯移液"],
    "离心": ["离心-复位机制", "纯化离心"],
    "物料拿取": ["物料拿取", "容器拿取"],
    "获取容器": ["容器拿取", "物料拿取"],
    "物料放置": ["物料放置"],
    "静置": ["静置"],
    "XRD检测": ["XRD滴液检测全流程"],
    "XRD测试": ["XRD滴液检测全流程"],
}


def _normalize_name(name: str) -> str:
    return str(name or "").replace("_", "").replace(" ", "").lower()


def _normalize_param_name(name: str) -> str:
    text = str(name or "").strip()
    return UNIT_SUFFIX_RE.sub("", text).strip()


class DispatchCatalog:
    """Platform schema (station → operation → parameter specs) + station ids."""

    def __init__(self) -> None:
        self.stations: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
        self.station_ids: Dict[str, int] = {}
        self._normalized_station_index: Dict[str, str] = {}
        # English station code → 对照表 display name (loader-provided); lets
        # every English identifier bridge to the platform name generically.
        self._code_to_display: Dict[str, str] = {}

    @classmethod
    def load(cls, workstation_loader: Any = None) -> "DispatchCatalog":
        catalog = cls()
        try:
            catalog._load_conversion_spec()
        except Exception:
            pass
        try:
            catalog._load_from_loader(workstation_loader)
        except Exception:
            pass
        return catalog

    def _conversion_paths(self) -> List[str]:
        roots = []
        try:
            roots.append(str(workstation_dir(use_new_format=True)))
        except Exception:
            pass
        # the conversion spec currently ships only with lab-design-all
        for root in list(roots):
            sibling = root.replace("lab-design-main", "lab-design-all")
            if sibling not in roots:
                roots.append(sibling)
        return [os.path.join(root, CONVERSION_FILENAME) for root in roots]

    def _load_conversion_spec(self) -> None:
        for path in self._conversion_paths():
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for step in data.get("steps", []) or []:
                station = str(step.get("workstation", "")).strip()
                operation = str(step.get("operation", "")).strip()
                if not station or not operation:
                    continue
                specs: Dict[str, Dict[str, Any]] = {}
                for param in step.get("parameters", []) or []:
                    name = str(param.get("parameter_name", "")).strip()
                    if name:
                        specs[name] = {
                            "type": str(param.get("type", "") or ""),
                            "unit": param.get("unit"),
                            "options": param.get("options"),
                            "range": param.get("range"),
                        }
                self.stations.setdefault(station, {})[operation] = specs
                self._normalized_station_index.setdefault(
                    _normalize_name(station), station
                )
            break  # first existing spec wins

    def _load_from_loader(self, workstation_loader: Any) -> None:
        """工作站编码 + EN→display bridge from the loaded truth source."""
        if workstation_loader is None or not hasattr(workstation_loader, "get_all"):
            return
        for station in workstation_loader.get_all():
            if not isinstance(station, dict):
                continue
            code_name = str(station.get("station_name", ""))
            display = str(station.get("display_name", ""))
            if code_name and display and display != code_name:
                self._code_to_display[code_name] = display
            content = str(
                station.get("skill_content", "") or station.get("usage_content", "")
            )
            match = STATION_CODE_RE.search(content)
            if not match:
                continue
            code = int(match.group(1))
            for identifier in (code_name, display):
                if identifier:
                    self.station_ids[identifier] = code
        # once the bridge exists, platform names can inherit ids too
        for identifier, code in list(self.station_ids.items()):
            platform = self.resolve_station(identifier)
            if platform:
                self.station_ids.setdefault(platform, code)

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------

    def resolve_station(self, name: str) -> Optional[str]:
        text = str(name or "").strip()
        if not text or not self.stations:
            return None
        if text in self.stations:
            return text
        if text in CURATED_STATION_ALIASES:
            candidate = CURATED_STATION_ALIASES[text]
            if candidate in self.stations:
                return candidate
        # English station codes bridge through the 对照表 display name.
        display = self._code_to_display.get(text)
        if display:
            if display in CURATED_STATION_ALIASES:
                candidate = CURATED_STATION_ALIASES[display]
                if candidate in self.stations:
                    return candidate
            normalized_display = _normalize_name(display)
            if normalized_display in self._normalized_station_index:
                return self._normalized_station_index[normalized_display]
        normalized = _normalize_name(text)
        if normalized in self._normalized_station_index:
            return self._normalized_station_index[normalized]
        # containment fallback for suffix-only differences
        for norm, station in self._normalized_station_index.items():
            if norm and (norm in normalized or normalized in norm):
                return station
        return None

    def resolve_operation(self, platform_station: str, operation: str) -> Optional[str]:
        operations = self.stations.get(platform_station, {})
        text = str(operation or "").strip()
        if not text:
            return None
        if text in operations:
            return text
        for candidate in CURATED_OPERATION_ALIASES.get(text, []):
            if candidate in operations:
                return candidate
        normalized = _normalize_name(text)
        for candidate in operations:
            if _normalize_name(candidate) == normalized:
                return candidate
        for candidate in operations:
            norm_candidate = _normalize_name(candidate)
            if norm_candidate.startswith(normalized) or normalized.startswith(
                norm_candidate
            ):
                return candidate
        if len(operations) == 1:
            return next(iter(operations))
        return None

    def resolve_parameter(
        self,
        platform_station: str,
        platform_operation: str,
        name: str,
    ) -> Optional[str]:
        specs = self.stations.get(platform_station, {}).get(platform_operation, {})
        text = _normalize_param_name(name)
        if text in specs:
            return text
        # per-version wording variants: 保留瓶盖 ↔ 是否保留瓶盖, 开盖编号 ↔
        # 开盖瓶号 ↔ 开盖的瓶号, 关盖编号 ↔ 关盖瓶号 ↔ 关盖的瓶号 …
        normalized = _normalize_name(text)
        for candidate in specs:
            if _normalize_name(candidate) == normalized:
                return candidate
        for candidate in specs:
            base = _normalize_name(candidate).replace("是否", "").replace("的", "")
            mine = normalized.replace("是否", "").replace("的", "")
            if base == mine:
                return candidate
            if base.replace("编号", "瓶号") == mine.replace("编号", "瓶号"):
                return candidate
        return None

    def parameter_spec(
        self,
        platform_station: str,
        platform_operation: str,
        platform_param: str,
    ) -> Dict[str, Any]:
        return (
            self.stations.get(platform_station, {})
            .get(platform_operation, {})
            .get(platform_param, {})
        )


def _coerce_value(value: Any, spec: Dict[str, Any]) -> Tuple[Any, Optional[str]]:
    """Coerce a value to the platform-declared type; return (value, warning)."""
    declared = str(spec.get("type", "") or "").lower()
    if declared in {"", "array"} or isinstance(value, (list, dict)):
        return value, None
    if declared == "int":
        if isinstance(value, bool):
            return int(value), None
        if isinstance(value, int):
            return value, None
        if isinstance(value, float) and value.is_integer():
            return int(value), None
        text = str(value).strip()
        # SKILL label/value enums dispatch the value: 是→1, 否→0
        if text in {"是", "true", "True"}:
            return 1, None
        if text in {"否", "false", "False"}:
            return 0, None
        if re.fullmatch(r"-?\d+", text):
            return int(text), None
        return value, f"无法把 `{value!r}` 转为平台 int 类型"
    if declared == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), None
        text = str(value).strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return float(text), None
        return value, f"无法把 `{value!r}` 转为平台 float 类型"
    if declared == "string":
        if isinstance(value, str):
            return value, None
        if isinstance(value, bool):
            return ("1" if value else "0"), None
        if isinstance(value, (int, float)):
            return format(value, "g"), None
        return str(value), None
    return value, None


def _instantiate_n_bottle_keys(value: Any) -> Any:
    """``{"N号原液瓶": [{"瓶号": 3, …}]}`` → ``{"3号原液瓶": {…}}`` per the
    SKILL dash notation, where N is a placeholder instantiated by 瓶号."""
    if isinstance(value, list):
        return [_instantiate_n_bottle_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    rebuilt: Dict[str, Any] = {}
    for key, item in value.items():
        match = N_BOTTLE_KEY_RE.match(str(key))
        if match and isinstance(item, list):
            for entry in item:
                if isinstance(entry, dict) and entry.get("瓶号") is not None:
                    number = entry.get("瓶号")
                    body = {
                        k: _instantiate_n_bottle_keys(v)
                        for k, v in entry.items()
                        if k != "瓶号"
                    }
                    rebuilt[f"{number}号{match.group(1)}"] = body
                else:
                    rebuilt.setdefault(key, []).append(
                        _instantiate_n_bottle_keys(entry)
                    )
            continue
        if match and isinstance(item, dict) and item.get("瓶号") is not None:
            number = item.get("瓶号")
            body = {
                k: _instantiate_n_bottle_keys(v)
                for k, v in item.items()
                if k != "瓶号"
            }
            rebuilt[f"{number}号{match.group(1)}"] = body
            continue
        rebuilt[key] = _instantiate_n_bottle_keys(item)
    return rebuilt


def format_dispatch_payload(
    workflow_json: Dict[str, Any],
    catalog: DispatchCatalog,
    *,
    plan_name: str = "",
) -> Dict[str, Any]:
    """Convert a validated workflow into the platform's exact dispatch form.

    Returns ``{"payload": envelope, "warnings": [...], "mapped_steps": n,
    "unmapped_steps": n}``. Original workflow_json is left untouched; steps
    that cannot be mapped keep their original form and are flagged.
    """
    warnings: List[str] = []
    formatted_steps: List[Dict[str, Any]] = []
    mapped = 0
    unmapped = 0

    steps = workflow_json.get("steps") if isinstance(workflow_json, dict) else []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_no = step.get("step_number")
        station_in = str(step.get("workstation", "")).strip()
        operation_in = str(step.get("operation", "")).strip()
        parameters_in = step.get("parameters")
        parameters_in = parameters_in if isinstance(parameters_in, dict) else {}

        platform_station = catalog.resolve_station(station_in)
        if platform_station is None:
            warnings.append(
                f"第 {step_no} 步：工作站 `{station_in}` 未能映射到平台站名，保留原样"
            )
            formatted_steps.append(dict(step))
            unmapped += 1
            continue
        platform_operation = catalog.resolve_operation(platform_station, operation_in)
        if platform_operation is None:
            warnings.append(
                f"第 {step_no} 步：操作 `{operation_in}` 未能映射到 `{platform_station}` "
                "的平台操作，保留原样"
            )
            formatted_steps.append(dict(step))
            unmapped += 1
            continue

        out_params: Dict[str, Any] = {}
        for raw_name, raw_value in parameters_in.items():
            platform_param = catalog.resolve_parameter(
                platform_station, platform_operation, raw_name
            )
            value = _instantiate_n_bottle_keys(raw_value)
            if platform_param is None:
                # The dispatch payload must contain ONLY platform-known
                # fields; the original value stays auditable in workflow_json.
                warnings.append(
                    f"第 {step_no} 步：参数 `{raw_name}` 不在 `{platform_station}/"
                    f"{platform_operation}` 的平台参数中，已从下发 payload 省略"
                    "（原始 workflow_json 保留）"
                )
                continue
            spec = catalog.parameter_spec(
                platform_station, platform_operation, platform_param
            )
            coerced, warn = _coerce_value(value, spec)
            if warn:
                warnings.append(f"第 {step_no} 步 `{platform_param}`：{warn}")
            options = spec.get("options")
            if (
                isinstance(options, list)
                and options
                and isinstance(coerced, str)
                and coerced not in options
            ):
                warnings.append(
                    f"第 {step_no} 步 `{platform_param}`=`{coerced}` 不在平台枚举 "
                    f"{options} 中"
                )
            out_params[platform_param] = coerced

        formatted: Dict[str, Any] = {
            "step_number": step_no,
            "workstation": platform_station,
            "operation": platform_operation,
            "parameters": out_params,
        }
        station_id = catalog.station_ids.get(platform_station) or catalog.station_ids.get(
            station_in
        )
        if station_id:
            formatted["id"] = station_id
        # keep the observation-hierarchy trace (issue 6) on dispatch steps too
        for trace_key in ("source_macro_step", "macro_action_id", "observation_point_id"):
            if step.get(trace_key) is not None:
                formatted[trace_key] = step[trace_key]
        formatted_steps.append(formatted)
        mapped += 1

    envelope = {
        "experiment_steps": {"steps": formatted_steps, "unknown_steps": None},
        "plan_name": plan_name or "chemagent_workflow",
    }
    return {
        "payload": envelope,
        "warnings": warnings,
        "mapped_steps": mapped,
        "unmapped_steps": unmapped,
    }


# ----------------------------------------------------------------------
# Deterministic required-field completion (pre-validation)
# ----------------------------------------------------------------------

# Params whose value can be derived with ZERO ambiguity from a sibling field.
# 容器数量 == len(容器编号); 开盖/关盖编号 default to the containers being
# handled (容器编号) — the SKILL semantics of "开盖瓶号"/"关盖瓶号".
_COUNT_FROM_LIST = {"容器数量": "容器编号"}
_LIDNO_FROM_CONTAINER = {"开盖编号": "容器编号", "关盖编号": "容器编号"}


def complete_required_fields(
    workflow_json: Dict[str, Any],
    validator: Any,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fill mechanically-derivable required fields BEFORE strict validation.

    Conservative by construction — only touches fields it can determine with
    zero guessing, and never overwrites a value the LLM already wrote:

    - ``容器数量`` ← ``len(容器编号)`` when 容器编号 is a list and 容器数量 missing;
    - ``开盖编号`` / ``关盖编号`` ← ``容器编号`` when the op requires them and
      they are missing (SKILL 开盖瓶号/关盖瓶号 default to the handled vials);
    - any other SKILL 是否必填=是 field that has a concrete 默认值 column value
      (e.g. ``保留瓶盖`` = 1) — filled from ``validator.defaults_for``.

    ``加样方案`` and other structure-bearing fields are intentionally NOT
    synthesized (they need real semantics — left to the LLM self-repair round).
    Returns a DEEP COPY plus an audit log of exactly what was filled; the input
    object is never mutated. Requires the SKILL-form station name (the form the
    validator checks); legacy-form steps yield empty required sets and are
    left untouched.
    """
    import copy

    if not isinstance(workflow_json, dict):
        return workflow_json, []
    steps = workflow_json.get("steps")
    if not isinstance(steps, list) or not steps:
        return workflow_json, []

    result = copy.deepcopy(workflow_json)
    filled_log: List[Dict[str, Any]] = []

    for step in result.get("steps", []):
        if not isinstance(step, dict):
            continue
        station = str(step.get("workstation", "")).strip()
        operation = str(step.get("operation", "")).strip()
        params = step.get("parameters")
        if not station or not operation or not isinstance(params, dict):
            continue

        required = validator.required_params_for(station, operation)
        if not required:
            # not SKILL-form (legacy reference contract) or no required table
            continue
        provided = {_normalize_param_name(k) for k in params.keys()}
        step_no = step.get("step_number")

        for field in sorted(required):
            if field in provided:
                continue  # never overwrite what the LLM already wrote

            # 1) count derivable from a sibling list
            if field in _COUNT_FROM_LIST:
                src = params.get(_COUNT_FROM_LIST[field])
                if isinstance(src, list):
                    params[field] = len(src)
                    filled_log.append({
                        "step_number": step_no, "station": station,
                        "operation": operation, "param": field,
                        "value": len(src), "reason": f"len({_COUNT_FROM_LIST[field]})",
                    })
                    continue

            # 2) lid numbers default to the containers being handled
            if field in _LIDNO_FROM_CONTAINER:
                src = params.get(_LIDNO_FROM_CONTAINER[field])
                if isinstance(src, list) and src:
                    params[field] = list(src)
                    filled_log.append({
                        "step_number": step_no, "station": station,
                        "operation": operation, "param": field,
                        "value": list(src), "reason": f"defaults to {_LIDNO_FROM_CONTAINER[field]}",
                    })
                    continue

            # 3) SKILL 默认值 column (only a concrete declared default)
            defaults = validator.defaults_for(station, operation)
            if field in defaults:
                params[field] = defaults[field]
                filled_log.append({
                    "step_number": step_no, "station": station,
                    "operation": operation, "param": field,
                    "value": defaults[field], "reason": "SKILL 默认值",
                })
                continue
            # otherwise: cannot determine unambiguously → leave for LLM repair

    return result, filled_log
