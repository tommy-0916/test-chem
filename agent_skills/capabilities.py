"""Source-grounded device capability projections, without device execution.

The experiment vocabulary below is deliberately conservative: an exact source
station AND matching description evidence are required. Station names alone do
not prove a scientific technique. Parameter truth remains in workstation Skills.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = REPO_ROOT / "chem_resources" / "workstation_capability_index.json"
SKILL_ROOT = REPO_ROOT / "chem_resources" / "agent-skills"
TIER_SKILLS = {
    "experiment": "experiment-capabilities",
    "operation": "operation-capabilities",
    "step": "macro-step-capabilities",
}
V2_TIER_ALIASES = {
    "stage": "experiment",
    "macro_action": "operation",
    "macro_step": "step",
}
V2_TIER_SKILLS = {
    "stage": "experiment-capabilities",
    "macro_action": "operation-capabilities",
    "macro_step": "macro-step-capabilities",
    "device": "device-capabilities",
}
PROJECTION_VERSION = "1.0"
UNAVAILABLE = {"offline", "unavailable", "down", "maintenance", "fault", "disabled", "停机", "维修", "故障", "不可用", "禁用"}

# (station codes, capability id, scientific label, literal description evidence)
# These reviewed aliases classify declarations; they do not introduce recipes.
EXPERIMENT_RULES = (
    (("XRD_V1",), "xrd", "乙醇分散固体样品滴加制样的 XRD 结构与物相表征", r"分散于乙醇中的固体样品进行滴加制样并开展XRD测试"),
    (("Infrared_Spectrometer_V1",), "infrared", "乙醇分散样品滴加制样的红外吸收光谱表征", r"分散于乙醇中的样品进行滴加制样并开展红外吸收光谱测试"),
    (("UV_Vis_Spectrometer_V1",), "uv-vis", "紫外可见吸收光谱表征", r"吸收光谱测试"),
    (("Fluorescence_Spectrometer_V1",), "fluorescence", "荧光发射表征", r"荧光发射特性"),
    (("Gas_Chromatograph_V1",), "gas-chromatography", "气相色谱分析", r"挥发性或半挥发性组分进行分离分析"),
    (("Liquid_Chromatograph_V1",), "liquid-chromatography", "液相色谱分析", r"分离、定性与定量分析"),
    (("Microplate_Reader_V1",), "microplate-assay", "高通量吸光度、荧光或化学发光检测", r"吸光度、荧光或化学发光等高通量检测"),
    (("Dual_Station_Electrochemical_Workstation_V2",), "electrochemistry", "电化学测试", r"循环伏安法、计时电流法、交流阻抗法"),
    (("High_Temperature_High_Pressure_Microreaction_Platform_V1",), "thermal-catalysis", "密闭高温高压气液固热催化", r"高温高压条件下热催化反应"),
    (("Photocatalysis_Workstation_V1", "Photocatalysis_Workstation_V2"), "photocatalysis", "光催化反应与材料筛选", r"光催化反应(?:的并行控温测试|测试)"),
    (("Photocatalysis_Workstation_V2",), "photodeposition", "光沉积负载助催化剂", r"光沉积法负载助催化剂"),
    (("LED_Illumination_and_Membrane_Clamping_Workstation_V1",), "photocatalytic-hydrogen", "光催化产氢性能评估", r"光催化材料的产氢性能评估"),
    (("Muffle_Furnace_V1",), "calcination", "程序高温煅烧", r"程序高温煅烧"),
    (("Darkbox_Imaging_Workstation_V1",), "darkbox-imaging", "暗箱彩色及荧光成像", r"彩色图像照片收集.*荧光发光图片采集"),
    (("Gas_Liquid_Mass_Transfer_High_Speed_Camera_V1",), "high-speed-imaging", "电化学工况气泡高速成像", r"气泡的成核、生长和脱离过程"),
    (("Interfacial_Wettability_and_Mass_Transfer_Characterization_Workstation",), "wettability", "接触角及气泡粘附力表征", r"液滴接触角、气泡粘附力测试"),
    (("Centrifuge_V1", "Purification_Workstation_V1"), "solid-liquid-separation", "离心固液分离", r"固液分离"),
    (("Purification_Workstation_V1",), "washing-purification", "沉淀清洗与纯化", r"沉淀清洗"),
    (("Post_Reaction_Processing_Platform_V1",), "reaction-workup", "反应后过滤、稀释及分析前处理", r"正压过滤.*稀释液混合"),
    (("Drying_Oven_V1",), "drying-aging", "加热干燥与老化", r"干燥处理及合成过程中的老化"),
    (("Heating_Magnetic_Stirring_Workstation_V1",), "heated-stirring", "加热搅拌反应与混合", r"搅拌与加热处理"),
    (("Room_Temperture_Magnetic_Stirrer_Workstation_V1",), "ambient-mixing", "常温混合与均一化", r"样品混合、溶液均一化"),
    (("Ultrasonic_Disperser_V1", "Ultrasonic_Disperser_V2", "Ultrasonic_Liquid_Handling_Workstation_V1"), "ultrasonic-treatment", "超声分散、混合与清洗", r"通过超声对样品进行[^。；]*?(?:清洗|混合)"),
    (("Cooling_Workstation_V1",), "cooling", "风扇辅助冷却", r"利用风扇加快容器降温"),
    (("Liquid_Pouring_Workstation_V1",), "supernatant-removal", "离心后去上清并保留沉淀", r"上清液倾倒处理"),
    (("Single_Channel_Solid_Weighing_Workstation_V1", "Multi_Channel_Solid_Weighing_Workstation_V1", "Multi_Channel_Solid_Weighing_Workstation_V2"), "solid-dosing", "定量固体称量与进样", r"定量称量与进样"),
    (("Solid_Sample_Transfer_Workstation_V1",), "solid-transfer", "固体样品转移", r"固体样品转移"),
    (("Liquid_Handling_Station_1ml_V1", "Liquid_Handling_Station_1ml_V2", "Liquid_Handling_Station_4Channel_V1", "Liquid_Handling_Station_5ml_V1", "Liquid_Handling_Station_5ml_V3", "Cleaning_and_Dispensing_Workstation_V1"), "liquid-dosing", "液体加样与配液", r"加液|液体加入|加入指定液体|加入指定液体|加入溶剂"),
    (("Liquid_Handling_Station_5ml_V2",), "liquid-transfer", "反应样品移液", r"移液:将溶液"),
    (("Spectroscopy_Magnetic_Stirrer_Workstation_V1",), "spectroscopy-preparation", "谱学测试前搅拌均一化", r"并行磁力搅拌"),
    (("General_Material_Station_V1", "Heat_Resistant_Material_Station"), "container-supply", "实验物料承载与周转", r"拿取|供取放"),
    (("Container_storaging_Station_V1", "Plate_storaging_Station_V1"), "sample-storage", "样品暂存与周转", r"样品暂存与周转"),
    (("Spectroscopy_Container_Transfer_Station_V1", "Intelligent_Photocatalysis_Container_Transfer_Station_V1"), "sample-routing", "测试前后样品中转", r"样品容器临时放置与中转"),
)


def semantic_mapping_digest() -> str:
    return hashlib.sha256(json.dumps(EXPERIMENT_RULES, ensure_ascii=False).encode()).hexdigest()


def extract_experiment_capabilities(station: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify only declarations supported by this exact station's text."""
    code = str(station.get("station_code", station.get("station_name", "")))
    description = str(station.get("description", ""))
    source = station.get("description_source", {})
    result = []
    for stations, identifier, label, pattern in EXPERIMENT_RULES:
        if code not in stations:
            continue
        match = re.search(pattern, description)
        if not match:
            continue
        prefix = re.split(r"[。；，,;]", description[:match.start()])[-1]
        denied = bool(re.search(r"不支持|不能|不可|禁止|不具备|不提供", prefix[-12:]))
        result.append({
            "id": identifier, "name": label, "support_status": "unsupported" if denied else "supported",
            "evidence": {**source, "quote": prefix + match.group(0) if denied else match.group(0)},
        })
    return result


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


def load_current_capability_index(
    index_path: str | Path = DEFAULT_INDEX, *, source: str | Path | None = None,
) -> dict[str, Any]:
    """Read a fresh catalog, rebuilding stale projections in memory only.

    A changed Skill or audit must never silently use yesterday's constraints.
    This function does not rewrite either user documents or generated files.
    """
    from chem_resources.generate_workstation_capability_index import (
        DEFAULT_SOURCE, build_index, source_digest,
    )

    path = _resolve_path(index_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    source_path = _resolve_path(source or data.get("source") or DEFAULT_SOURCE)
    digest = source_digest(source_path)
    if (
        data.get("source_digest_sha256") != digest
        or data.get("semantic_mapping_digest") != semantic_mapping_digest()
        or data.get("projection_version") != PROJECTION_VERSION
        or _resolve_path(data.get("source") or source_path) != source_path
    ):
        data = build_index(source_path)
    return data


def _identity(station: dict[str, Any]) -> dict[str, Any]:
    result = {
        "station_code": str(station.get("station_code") or station.get("station_name") or station.get("code") or station.get("name") or ""),
        "display_name": str(station.get("display_name") or station.get("station_name") or station.get("station_code") or ""),
    }
    for key in ("availability", "availability_note"):
        if key in station:
            result[key] = copy.deepcopy(station[key])
    return result


def _status_for(station: dict[str, Any], status: dict[str, Any]) -> str:
    normalized = {str(key).lower(): str(value) for key, value in status.items()}
    for name in (station.get("station_code"), station.get("station_name"), station.get("display_name"), station.get("station_id")):
        if str(name).lower() in normalized:
            return normalized[str(name).lower()]
    return str(station.get("availability", ""))


def _resolve_context(context: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Hydrate only explicit catalog references, never arbitrary custom context."""
    context = copy.deepcopy(context)
    if not context:
        data = load_current_capability_index()
        return data, data["workstations"]
    catalog_reference = context.get("capability_index") or (context.get("source_index") if str(context.get("source_kind", "")).startswith("lab-design-all") else None)
    if catalog_reference:
        data = load_current_capability_index(catalog_reference, source=context.get("source"))
        stations = data["workstations"]
        # A caller may narrow the generated roster. Never reintroduce omitted stations.
        if "workstations" in context:
            overrides = {
                _identity(item)["station_code"]: item
                for item in context["workstations"] if isinstance(item, dict)
            }
            by_alias = {}
            for key, item in overrides.items():
                by_alias[key] = item
                by_alias[str(item.get("display_name", ""))] = item
            selected = []
            for station in stations:
                override = by_alias.get(station["station_code"]) or by_alias.get(station["display_name"])
                if override is None:
                    continue
                # Inline structured contracts are explicit restrictions, not merely status.
                merged = {**station, **override}
                if "capabilities" in override and "operations" not in override:
                    names = {str(item.get("name", "")) if isinstance(item, dict) else str(item) for item in override["capabilities"]}
                    merged["operations"] = [item for item in station.get("operations", []) if item.get("name") in names]
                if ("operations" in override or "capabilities" in override) and "experiment_capabilities" not in override:
                    # An operation restriction can invalidate a composite
                    # experiment mode. Do not retain the unrestricted label.
                    merged["experiment_capabilities"] = []
                selected.append(merged)
            stations = selected
        metadata = {**data, **{key: value for key, value in context.items() if key not in {"source_digest_sha256", "workstations"}}}
        return metadata, stations
    # A raw generated catalog is already self-contained (also used by generation).
    # Custom contexts with only textual summaries stay custom and visibly unknown.
    return context, [item for item in context.get("workstations", []) if isinstance(item, dict)]


def _source_only(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: copy.deepcopy(source[key]) for key in ("skill", "audit_rules", "path", "line", "section", "sha256") if key in source}


def _operations(station: dict[str, Any]) -> list[dict[str, Any]]:
    raw = station.get("operations", station.get("capabilities", []))
    return [dict(item) if isinstance(item, dict) else {"name": str(item)} for item in raw]


def _scientific_controls(operation: dict[str, Any]) -> list[dict[str, Any]]:
    controls = []
    for parameter in operation.get("parameter_contracts", []):
        if parameter.get("role") not in {"target_material_quantity", "scientific_quantity_requirement", "process_control"}:
            continue
        if str(parameter.get("name", "")).endswith("编号"):
            continue
        control = {key: copy.deepcopy(parameter[key]) for key in ("name", "required", "unit", "range", "role", "source_line") if key in parameter and parameter[key] not in ("", None)}
        if control.get("role") == "process_control":
            del control["role"]  # Default for scientific_controls; dose roles remain explicit.
        evidence = str(parameter.get("evidence", ""))
        # Enum labels carry scientific choices; opaque machine values do not.
        # Preserve the full original table evidence in the shared source index.
        labels = re.findall(r'"label"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', evidence)
        if labels:
            control["options"] = list(dict.fromkeys(labels))
            evidence = re.sub(r"`?\[\s*\{.*?\}\s*\]`?", "", evidence)
        # Range, name and source line already have dedicated fields. Keep
        # actual remarks, but avoid repeating a bare range/default as evidence.
        evidence = evidence.strip()
        if evidence and evidence not in {str(parameter.get("name", "")), str(parameter.get("range", ""))}:
            control["note"] = evidence
        controls.append(control)
    return controls


def _compact_evidence(evidence: Any) -> dict[str, Any]:
    """Full paths and content hashes stay in the shared source index."""
    if not isinstance(evidence, dict):
        return {}
    result = {key: copy.deepcopy(evidence[key]) for key in ("line", "section", "quote") if key in evidence}
    if evidence.get("path"):
        result["document"] = "audit" if "audit" in str(evidence["path"]) else "skill"
    return result


def _compact_constraints(items: list[Any]) -> list[dict[str, Any]]:
    by_quote: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        item = item if isinstance(item, dict) else {"quote": str(item)}
        quote = str(item.get("quote", ""))
        if not quote:
            continue
        # Exact duplicates can be shared, but conditional branch text and
        # numeric bounds are never clipped or replaced by a lossy summary.
        operation, side = str(item.get("operation", "")), str(item.get("side", ""))
        entry = by_quote.setdefault((quote, operation if side else "", side), {"text": quote, "sources": {}})
        if side:
            entry.update({"operation": operation, "side": side})
        source = _compact_evidence(item)
        if source.get("line"):
            lines = entry["sources"].setdefault(source.get("document", "skill"), [])
            if source["line"] not in lines:
                lines.append(source["line"])
    return list(by_quote.values())


def _duplicate_io_constraint(item: dict[str, Any], station: dict[str, Any]) -> bool:
    """Remove only exact restatements of an already emitted operation I/O fact."""
    side_name = item.get("side")
    if side_name not in {"input", "output"}:
        return False
    operation = next((operation for operation in _operations(station) if operation.get("name") == item.get("operation")), None)
    if operation is None:
        return False
    clean = re.sub(r"\s+|\*\*", "", str(item.get("quote", "")).lstrip("- "))
    match = re.match(r"^(容器类型|容器状态|样品状态|模板名称)[：:](.*)$", clean)
    if not match:
        return False
    field = {"容器类型": "container_type_raw", "容器状态": "container_states", "样品状态": "sample_states", "模板名称": "template_requirements"}[match.group(1)]
    values = operation.get(side_name, {}).get(field, [])
    return match.group(2) in {re.sub(r"\s+|\*\*", "", str(value)) for value in values}


def _pick(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: copy.deepcopy(value[key]) for key in fields if key in value} if isinstance(value, dict) else {}


def _pick_scalars(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: item for key, item in _pick(value, fields).items() if item is None or isinstance(item, (str, int, float, bool))}


def _sanitize_projection(context: dict[str, Any], tier: str) -> dict[str, Any]:
    """A tier label is not authority to smuggle lower-tier fields into a prompt."""
    common_fields = ("tier", "skill", "projection_version", "source", "source_kind", "source_digest_sha256", "semantic_mapping_digest", "source_index", "planning_policy", "additional_planning_policy", "restrictions", "excluded_capabilities", "allowed_capabilities", "allowed_operations", "declaration_status", "note")
    result = _pick_scalars(context, common_fields)
    result.update(_pick(context, ("restrictions", "excluded_capabilities", "allowed_capabilities", "allowed_operations")))
    station_fields = ("station_code", "display_name", "availability", "availability_note")
    result["workstations"] = []
    for station in context.get("workstations", []):
        if not isinstance(station, dict):
            continue
        entry = _pick_scalars(station, station_fields)
        if tier == "step":
            if "capability_description" in station:
                entry["capability_description"] = station["capability_description"]
            entry["planning_constraints"] = [_pick(item, ("text", "sources", "operation", "side")) for item in station.get("planning_constraints", [])]
            entry["dependencies"] = [
                {**_pick(item, ("relation", "station_codes", "resolution")), "evidence": _pick(item.get("evidence"), ("line", "section", "quote", "document"))}
                for item in station.get("dependencies", []) if isinstance(item, dict)
            ]
        result["workstations"].append(entry)
    common = ("station_code", "availability", "currently_usable", "name")
    key = {"experiment": "capabilities", "operation": "operations", "step": "operation_contracts"}[tier]
    result[key] = []
    for original in context.get(key, []):
        if not isinstance(original, dict):
            continue
        entry = _pick_scalars(original, common)
        if tier == "experiment":
            entry.update(_pick_scalars(original, ("id", "support_status")))
            if "evidence" in original:
                entry["evidence"] = _pick_scalars(original["evidence"], ("line", "section", "quote", "document"))
        else:
            entry["source"] = _pick_scalars(original.get("source"), ("document", "section"))
            if tier == "operation":
                entry.update(_pick_scalars(original, ("support_status",)))
            else:
                for side in ("input", "output"):
                    entry[side] = _pick(original.get(side), ("container_type_raw", "containers", "container_states", "sample_states", "template_requirements", "same_as_input", "declaration_status"))
                entry["container_contract"] = _pick(original.get("container_contract"), ("accepted_input_containers", "input_container_states", "input_sample_states", "input_count_constraints", "output_containers", "output_container_states", "output_sample_states", "output_count_relation", "sample_carriers", "template_requirements", "declaration_status"))
                entry["scientific_controls"] = [_pick(control, ("name", "required", "unit", "range", "role", "source_line", "options", "note")) for control in original.get("scientific_controls", [])]
                entry["quantity_semantics"] = _pick(original.get("quantity_semantics"), ("target_setpoints", "material_output_effect", "material_output_quantity_mode", "reported_measurements"))
                entry["feedback_contract"] = _compact_feedback(original.get("feedback_contract"))
                if "io_evidence" in original:
                    entry["io_evidence"] = _pick(original["io_evidence"], ("skill", "audit"))
        result[key].append(entry)
    if tier == "step":
        result.update(_pick(context, ("global_constraints", "container_list", "sample_carrier_list")))
    return result


def _compact_feedback(feedback: Any) -> dict[str, Any]:
    result = {}
    for kind in ("returned_data", "intermediate_feedback"):
        declaration = feedback.get(kind, {}) if isinstance(feedback, dict) else {}
        result[kind] = {
            "status": declaration.get("status", "unknown"),
            "fields": copy.deepcopy(declaration.get("fields", [])),
        }
        if declaration.get("evidence"):
            result[kind]["evidence"] = [_compact_evidence(item) for item in declaration["evidence"]]
    return result


def _residual_container_contract(operation: dict[str, Any]) -> dict[str, Any]:
    """Keep count/carrier relations and only facts not already in physical I/O.

    This is structural deduplication, not truncation: callers read input/output
    for type/state, and this contract for additional restrictions and relations.
    """
    contract = copy.deepcopy(operation.get("container_contract", {"declaration_status": "unknown"}))
    input_side, output_side = operation.get("input", {}), operation.get("output", {})
    duplicate_fields = {
        "accepted_input_containers": input_side.get("containers"),
        "input_container_states": input_side.get("container_states"),
        "input_sample_states": input_side.get("sample_states"),
        "output_containers": input_side.get("containers") if output_side.get("same_as_input") else output_side.get("containers"),
        "output_container_states": output_side.get("container_states"),
        "output_sample_states": output_side.get("sample_states"),
    }
    for key, already_declared in duplicate_fields.items():
        if key in contract and contract[key] == already_declared:
            del contract[key]
    for key in ("sample_carriers", "template_requirements"):
        if contract.get(key) == []:
            del contract[key]
    return contract


def _compact_io(side: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(side)
    if result.get("template_requirements") == []:
        del result["template_requirements"]
    raw = result.get("container_type_raw", [])
    if raw == ["与输入保持一致"] and result.get("same_as_input"):
        del result["container_type_raw"]
    elif len(raw) == 1:
        names = re.split(r"[或、/，,\s]+", raw[0])
        if names == result.get("containers"):
            # E.g. '进样瓶或50ml耐热瓶' is already the exact containers list.
            del result["container_type_raw"]
    return result


def project_device_context(context: dict[str, Any], tier: str) -> dict[str, Any]:
    """Return only the information appropriate for a Research planning tier.

    ``{}`` selects the default truth source. Nonempty custom contexts are never
    replaced by that default. A generated context may reference its index and
    restrict its roster or override individual station contracts and status.
    """
    if tier not in TIER_SKILLS:
        raise ValueError(f"unknown device capability tier: {tier!r}")
    if context.get("tier") == tier and context.get("skill") == TIER_SKILLS[tier] and context.get("projection_version") == PROJECTION_VERSION:
        # Generated projections may be saved in state across source edits.
        # Check the content AND vocabulary digest before reusing a snapshot;
        # explicit custom snapshots without an index are never substituted.
        index_path = context.get("source_index")
        if index_path and str(context.get("source_kind", "")).startswith("lab-design-all"):
            current = load_current_capability_index(index_path, source=context.get("source"))
            fresh = (
                context.get("source_digest_sha256") == current.get("source_digest_sha256")
                and context.get("semantic_mapping_digest") == current.get("semantic_mapping_digest")
            )
            if fresh:
                return _sanitize_projection(context, tier)
            # Only identities/status are input restrictions. Do not merge old
            # projected descriptions, compact constraints or dependency evidence
            # over the rebuilt authoritative station contracts.
            context = {
                "capability_index": str(index_path),
                "source": str(current["source"]),
                "workstations": [_pick_scalars(station, ("station_code", "display_name", "availability", "availability_note")) for station in context.get("workstations", []) if isinstance(station, dict)],
                **_pick(context, ("restrictions", "excluded_capabilities", "allowed_capabilities", "allowed_operations", "station_status")),
                **({"planning_policy": context["additional_planning_policy"]} if context.get("additional_planning_policy") else {}),
            }
        else:
            return _sanitize_projection(context, tier)
    metadata, stations = _resolve_context(context or {})
    result: dict[str, Any] = {
        "tier": tier, "skill": TIER_SKILLS[tier], "projection_version": PROJECTION_VERSION,
        "source": str(metadata.get("source", "explicit device context")),
        "source_kind": str(metadata.get("source_kind", "explicit device context")),
        "source_digest_sha256": str(metadata.get("source_digest_sha256", "")),
        "semantic_mapping_digest": str(metadata.get("semantic_mapping_digest", "")),
        "source_index": str(metadata.get("capability_index") or DEFAULT_INDEX) if str(metadata.get("source_kind", "")).startswith("lab-design-all") else "",
        "planning_policy": {
            "experiment": "Use declared experiment types to search and design stages. A capability is not proof that every route or parameterization is executable. Unlisted techniques are unknown, not invented support. Respect unavailable stations.",
            "operation": "Compose macro actions from the listed operations. This layer deliberately does not expose operation inputs, outputs or machine parameters. Respect unavailable stations.",
            "step": "Specify substances, quantities, containers and sample lineage using operation contracts. input/output declare physical I/O; container_contract adds count/carrier relations and nonduplicate restrictions. Scientific control role defaults to process_control. Setpoints are not measured results. Unknown feedback cannot support a required automatic decision loop. Machine fields are loaded by Device only after workstation selection.",
        }[tier],
        "workstations": [],
    }
    status_map = metadata.get("station_status", metadata.get("device_status", {}))
    status_map = status_map if isinstance(status_map, dict) else {}
    item_key = {"experiment": "capabilities", "operation": "operations", "step": "operation_contracts"}[tier]
    result[item_key] = []
    for original in stations:
        station = copy.deepcopy(original)
        availability = _status_for(station, status_map)
        if availability:
            station["availability"] = availability
        identity = _identity(station)
        if tier == "step":
            identity["capability_description"] = str(station.get("description", ""))
            all_constraints = station.get("planning_constraints", station.get("critical_constraints", []))
            identity["planning_constraints"] = _compact_constraints([item for item in all_constraints if not isinstance(item, dict) or not _duplicate_io_constraint(item, station)])
            identity["dependencies"] = [
                {**{key: copy.deepcopy(dependency[key]) for key in ("relation", "station_codes", "resolution") if key in dependency}, "evidence": _compact_evidence(dependency.get("evidence"))}
                for dependency in station.get("dependencies", [])
            ]
        result["workstations"].append(identity)
        common = {
            "station_code": identity["station_code"],
            "availability": availability or "unknown",
            "currently_usable": availability.lower() not in UNAVAILABLE if availability else "unknown",
        }
        if tier == "experiment":
            capabilities = station.get("experiment_capabilities")
            if capabilities is None:
                capabilities = extract_experiment_capabilities(station)
            for capability in capabilities:
                if not isinstance(capability, dict):
                    continue
                entry = {key: copy.deepcopy(capability[key]) for key in ("id", "name", "support_status", "evidence") if key in capability}
                entry["evidence"] = _compact_evidence(entry.get("evidence"))
                result[item_key].append({**common, **entry})
            if not capabilities:
                result[item_key].append({**common, "name": "实验类型未声明", "support_status": "unknown"})
        elif tier == "operation":
            for operation in _operations(station):
                result[item_key].append({**common, "name": operation.get("name", ""), "support_status": "declared", "source": {"document": "skill", "section": operation.get("name", "")}})
        else:
            for operation in _operations(station):
                entry = {
                    **common, "name": operation.get("name", ""),
                    "source": {"document": "skill", "section": operation.get("name", "")},
                    "input": _compact_io(operation.get("input", {"declaration_status": "unknown"})),
                    "output": _compact_io(operation.get("output", {"declaration_status": "unknown"})),
                    "container_contract": _residual_container_contract(operation),
                    "scientific_controls": _scientific_controls(operation),
                    "quantity_semantics": {key: copy.deepcopy(value) for key, value in operation.get("quantity_semantics", {}).items() if key in {"target_setpoints", "material_output_effect", "material_output_quantity_mode", "reported_measurements"}},
                    "feedback_contract": _compact_feedback(operation.get("feedback_contract")),
                }
                io_evidence: dict[str, list[int]] = {}
                for evidence in station.get("planning_constraints", []):
                    if evidence.get("operation") == operation.get("name") and _duplicate_io_constraint(evidence, station):
                        document = "audit" if "audit" in evidence.get("path", "") else "skill"
                        io_evidence.setdefault(document, []).append(evidence["line"])
                if io_evidence:
                    entry["io_evidence"] = io_evidence
                result[item_key].append(entry)
    # Do not pass opaque old planning_policy/summary prose to upper tiers: it
    # commonly includes all lower-tier I/O. Explicit restrictions get a separate
    # field at the detailed tier; structured capability/operation limits above win.
    if tier == "step":
        for key in ("global_constraints", "container_list", "sample_carrier_list"):
            if key in metadata:
                result[key] = _compact_constraints(metadata[key]) if key == "global_constraints" else copy.deepcopy(metadata[key])
        if context.get("planning_policy"):
            result["additional_planning_policy"] = context["planning_policy"]
    elif context.get("planning_policy") and not context.get("source_kind"):
        # Explicit user restrictions are not generated device facts. Preserve
        # them even at upper tiers rather than silently relaxing a custom lab.
        result["additional_planning_policy"] = context["planning_policy"]
    for key in ("restrictions", "excluded_capabilities", "allowed_capabilities", "allowed_operations"):
        if key in context:
            result[key] = copy.deepcopy(context[key])
    if not stations:
        result["declaration_status"] = "unknown"
        result["note"] = "Explicit context declares no structured workstation capabilities; no default capabilities were substituted."
    return result


def load_capability_skill(context: dict[str, Any], tier: str) -> dict[str, Any]:
    """Load the selected tier's current SOP alongside its isolated facts.

    The path comes only from the fixed tier registry, never from model or
    context input. Instructions are deliberately read on every invocation so
    an edited repository-local SKILL takes effect without restarting the agent.
    Their digest is separate from the immutable workstation-fact digest.
    """
    projection = project_device_context(context, tier)
    name = TIER_SKILLS[tier]
    source = SKILL_ROOT / name / "SKILL.md"
    instructions = source.read_text(encoding="utf-8")
    return {
        **projection,
        "name": name,
        "instructions": instructions,
        "skill_source_path": str(source.resolve()),
        "instructions_digest_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    }


def capability_snapshot_id(context: dict[str, Any]) -> str:
    """Return one snapshot identity shared by every V2 projection tier."""

    metadata, stations = _resolve_context(context or {})
    raw_status = metadata.get("station_status", metadata.get("device_status", {}))
    status_map = raw_status if isinstance(raw_status, dict) else {}
    payload = {
        "source": metadata.get("source", "explicit device context"),
        "source_digest_sha256": metadata.get("source_digest_sha256", ""),
        "semantic_mapping_digest": metadata.get("semantic_mapping_digest", ""),
        "station_status": status_map,
        "roster": [
            {
                "station_code": _identity(station)["station_code"],
                "availability": _status_for(station, status_map),
            }
            for station in stations
        ],
    }
    return "capability_snapshot_" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _normalized_operation_name(value: Any) -> str:
    return re.sub(r"[\s_\-—:：]+", "", str(value or "")).casefold()


def _filter_macro_step_projection(
    projection: dict[str, Any], selected_operations: list[str]
) -> dict[str, Any]:
    """Keep selected operation contracts and their declared dependencies."""

    requested = {
        _normalized_operation_name(item)
        for item in selected_operations
        if _normalized_operation_name(item)
    }
    if not requested:
        return projection
    contracts = []
    station_codes: set[str] = set()
    for item in projection.get("operation_contracts", []):
        if not isinstance(item, dict):
            continue
        name = _normalized_operation_name(item.get("name"))
        if not any(token in name or name in token for token in requested if name):
            continue
        contracts.append(copy.deepcopy(item))
        station_codes.add(str(item.get("station_code", "")))
    if not contracts:
        fallback = copy.deepcopy(projection)
        fallback["selected_operations"] = list(selected_operations)
        fallback["selection_miss"] = True
        fallback["selection_policy"] = (
            "no safe operation-name match; returned the complete macro-step projection "
            "instead of hiding a potentially valid workstation"
        )
        return fallback
    by_code = {
        str(item.get("station_code", "")): item
        for item in projection.get("workstations", [])
        if isinstance(item, dict)
    }
    dependency_codes: set[str] = set()
    for code in list(station_codes):
        for dependency in by_code.get(code, {}).get("dependencies", []) or []:
            if isinstance(dependency, dict):
                dependency_codes.update(
                    str(item)
                    for item in dependency.get("station_codes", []) or []
                    if str(item)
                )
    kept_codes = station_codes | dependency_codes
    filtered = copy.deepcopy(projection)
    filtered["operation_contracts"] = contracts
    filtered["workstations"] = [
        copy.deepcopy(item)
        for item in projection.get("workstations", [])
        if isinstance(item, dict) and str(item.get("station_code", "")) in kept_codes
    ]
    filtered["selected_operations"] = list(selected_operations)
    filtered["dependency_station_codes"] = sorted(dependency_codes)
    filtered["selection_policy"] = (
        "all operation-name matches plus declared dependency closure; no Top-K"
    )
    return filtered


def _device_projection(context: dict[str, Any]) -> dict[str, Any]:
    """Expose all station choices while keeping full source files tool-loaded."""

    metadata, stations = _resolve_context(context or {})
    raw_status = metadata.get("station_status", metadata.get("device_status", {}))
    status_map = raw_status if isinstance(raw_status, dict) else {}
    catalog: list[dict[str, Any]] = []
    for raw in stations:
        station = copy.deepcopy(raw)
        identity = _identity(station)
        availability = _status_for(station, status_map) or "unknown"
        operations = []
        for operation in _operations(station):
            operations.append(
                {
                    "name": str(operation.get("name", "")),
                    "input": _compact_io(
                        operation.get("input", {"declaration_status": "unknown"})
                    ),
                    "output": _compact_io(
                        operation.get("output", {"declaration_status": "unknown"})
                    ),
                    "container_contract": _residual_container_contract(operation),
                    "scientific_controls": _scientific_controls(operation),
                    "quantity_semantics": _pick(
                        operation.get("quantity_semantics"),
                        (
                            "target_setpoints",
                            "material_output_effect",
                            "material_output_quantity_mode",
                            "reported_measurements",
                        ),
                    ),
                    "feedback_contract": _compact_feedback(
                        operation.get("feedback_contract")
                    ),
                }
            )
        declared_experiments = station.get("experiment_capabilities")
        catalog.append(
            {
                **identity,
                "station_id": station.get("station_id"),
                "availability": availability,
                "currently_usable": availability.lower() not in UNAVAILABLE,
                "capability_description": str(station.get("description", "")),
                "experiment_capabilities": copy.deepcopy(
                    declared_experiments
                    if declared_experiments is not None
                    else extract_experiment_capabilities(station)
                ),
                "operations": operations,
                "planning_constraints": _compact_constraints(
                    station.get(
                        "planning_constraints", station.get("critical_constraints", [])
                    )
                ),
                "dependencies": copy.deepcopy(station.get("dependencies", [])),
            }
        )
    return {
        "tier": "device",
        "skill": V2_TIER_SKILLS["device"],
        "projection_version": "2.0",
        "source": str(metadata.get("source", "explicit device context")),
        "source_kind": str(metadata.get("source_kind", "explicit device context")),
        "source_digest_sha256": str(metadata.get("source_digest_sha256", "")),
        "semantic_mapping_digest": str(metadata.get("semantic_mapping_digest", "")),
        "capability_snapshot_id": capability_snapshot_id(context),
        "workstation_count": len(catalog),
        "workstations": catalog,
        "loading_policy": (
            "The complete catalog is visible. Load full selected SKILL, audit and "
            "wire contracts with load_workstation_skill(station_code)."
        ),
    }


def project_capability_tier(
    context: dict[str, Any],
    tier: str,
    *,
    selected_operations: list[str] | None = None,
) -> dict[str, Any]:
    """Return one of the four V2 capability abstraction levels."""

    if tier == "device":
        return _device_projection(context)
    legacy_tier = V2_TIER_ALIASES.get(tier)
    if legacy_tier is None:
        raise ValueError(f"unknown V2 capability tier: {tier!r}")
    projection = project_device_context(context, legacy_tier)
    if tier == "macro_step" and selected_operations:
        projection = _filter_macro_step_projection(projection, selected_operations)
    projection = copy.deepcopy(projection)
    projection["legacy_tier"] = legacy_tier
    projection["tier"] = tier
    projection["skill"] = V2_TIER_SKILLS[tier]
    projection["capability_snapshot_id"] = capability_snapshot_id(context)
    return projection


def load_capability_tier_skill(
    context: dict[str, Any],
    tier: str,
    *,
    selected_operations: list[str] | None = None,
) -> dict[str, Any]:
    """Load a V2 projection together with its repository-local SOP."""

    projection = project_capability_tier(
        context,
        tier,
        selected_operations=selected_operations,
    )
    name = V2_TIER_SKILLS[tier]
    source = SKILL_ROOT / name / "SKILL.md"
    instructions = source.read_text(encoding="utf-8")
    return {
        **projection,
        "name": name,
        "instructions": instructions,
        "skill_source_path": str(source.resolve()),
        "instructions_digest_sha256": hashlib.sha256(
            instructions.encode("utf-8")
        ).hexdigest(),
    }
