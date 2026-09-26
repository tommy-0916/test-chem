"""Strict-entry preflight for the Research -> Device handoff (Phase 5).

Read-only against the platform truth sources; never invents station or
operation contracts.  The output is a diagnostic layer persisted with the
handoff artifact -- the Device dispatch gate keeps owning hard blocking.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHEM_RESOURCES = _REPO_ROOT / "chem_resources"
_LAB_DESIGN_ROOT = (
    _CHEM_RESOURCES / "lab-design-all" / "skills" / "chemistry-experiment-workstation"
)
_STATION_NAME_MAP = _LAB_DESIGN_ROOT / "工作站名称中英文对照.md"
_OPERATION_ALIAS_RESOURCE = (
    _CHEM_RESOURCES / "operation_aliases" / "operation_aliases.json"
)
_OLD_WORKSTATIONS = _CHEM_RESOURCES / "workstations"
_NEW_WORKSTATIONS = _CHEM_RESOURCES / "workstations_new"

# Stations the platform export demonstrably lacks contracts for.  Their
# absence is a platform gap, never a license to fall back to a similar
# station.
KNOWN_EXTERNAL_CONTRACT_MISSING = frozenset(
    {"批量加液工作站_V1", "离心样品转移工作站_V1"}
)

_OPERATION_HEADING_RE = re.compile(r"^##+\s*操作[^\n*]*\*\*([^\*]+)\*\*", re.M)
_OPERATION_BULLET_RE = re.compile(r"^-\s*操作[::]\s*(\S+)", re.M)
_CONTAINER_CONSTRAINT_RE = re.compile(r"容器类型\s*[:：]\s*([^\n]+)")
_FRONT_MATTER_NAME_RE = re.compile(r"^name\s*:\s*(\S+)\s*$", re.M)
_TITLE_RE = re.compile(r"^#\s+([^\n#][^\n]*)", re.M)


_IO_HEADER_TOKENS = {"输入约束", "输出约束", "输入输出约束"}


def _parse_skill_operations(content: str) -> List[str]:
    """Operation names from a lab-design SKILL: ``## 操作 N. **X**``
    headings or ``- **X**`` bullets inside the ``## 操作`` section."""
    operations: List[str] = []
    in_operation_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            in_operation_section = bool(re.match(r"^##\s*操作", stripped))
            heading = _OPERATION_HEADING_RE.match(stripped)
            if heading:
                operations.append(heading.group(1).strip())
            continue
        if not in_operation_section:
            continue
        bullet = re.match(r"^-\s*\*\*([^\*]+)\*\*", stripped)
        if bullet:
            name = bullet.group(1).strip()
            if name and name not in _IO_HEADER_TOKENS:
                operations.append(name)
    return list(dict.fromkeys(operations))


def _normalize_station_name(name: Any) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[\s_]+", "", text)
    return re.sub(r"v\d+$", "", text)


def _split_container_tokens(line: str) -> List[str]:
    return [
        token.strip()
        for token in re.split(r"或|及|、|,|，", line)
        if token.strip() and token.strip() not in {"不限", "与输入保持一致"}
    ]


def build_station_registry() -> Dict[str, Any]:
    """Read-only station registry from the platform truth sources.

    Fail loud: the preflight must never silently degrade to "everything
    unresolved" because a registry directory went missing.
    """
    if not _LAB_DESIGN_ROOT.is_dir():
        raise RuntimeError(
            "strict entry preflight: lab-design-all workstation registry "
            f"missing at {_LAB_DESIGN_ROOT}"
        )
    stations: Dict[str, Dict[str, Any]] = {}
    module_dirs = sorted(
        path
        for path in _LAB_DESIGN_ROOT.iterdir()
        if path.is_dir() and path.name.startswith("references-")
    )
    if not module_dirs:
        raise RuntimeError(
            "strict entry preflight: no references-* module directories under "
            f"{_LAB_DESIGN_ROOT}"
        )
    for module_dir in module_dirs:
        for station_dir in sorted(module_dir.iterdir()):
            skill_path = station_dir / "SKILL.md"
            if not skill_path.is_file():
                continue
            content = skill_path.read_text(encoding="utf-8", errors="replace")
            code_match = _FRONT_MATTER_NAME_RE.search(content)
            code = code_match.group(1).strip() if code_match else station_dir.name
            title_match = _TITLE_RE.search(content)
            displays = {code, station_dir.name}
            if title_match:
                displays.add(title_match.group(1).strip())
            operations = _parse_skill_operations(content)
            container_constraints = [
                token
                for line in _CONTAINER_CONSTRAINT_RE.findall(content)
                for token in _split_container_tokens(line)
            ]
            stations[code] = {
                "code": code,
                "display_names": sorted(displays),
                "skill_path": str(skill_path),
                "operations": operations,
                "container_constraints": sorted(set(container_constraints)),
                "via": "lab-design-all/SKILL",
            }
    if _OLD_WORKSTATIONS.is_dir():
        for json_file in sorted(_OLD_WORKSTATIONS.glob("*.json")):
            try:
                payload = json.loads(
                    json_file.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError):
                continue
            identity = payload.get("station_identity") or {}
            display = str(identity.get("name") or json_file.stem)
            operations = [
                str(operation.get("name"))
                for operation in (payload.get("operations") or [])
                if isinstance(operation, dict) and operation.get("name")
            ]
            key = f"legacy:{json_file.stem}"
            stations.setdefault(
                key,
                {
                    "code": key,
                    "display_names": [display, json_file.stem],
                    "skill_path": str(json_file),
                    "operations": operations,
                    "container_constraints": [],
                    "via": "workstations_json",
                },
            )
    if _NEW_WORKSTATIONS.is_dir():
        for station_dir in sorted(_NEW_WORKSTATIONS.iterdir()):
            if not station_dir.is_dir():
                continue
            usage = station_dir / "USAGE.md"
            displays = {station_dir.name}
            operations: List[str] = []
            if usage.is_file():
                content = usage.read_text(encoding="utf-8", errors="replace")
                operations = [
                    match.strip()
                    for match in _OPERATION_BULLET_RE.findall(content)
                ]
            key = f"new:{station_dir.name}"
            stations.setdefault(
                key,
                {
                    "code": key,
                    "display_names": sorted(displays),
                    "skill_path": str(station_dir),
                    "operations": operations,
                    "container_constraints": [],
                    "via": "workstations_new/USAGE",
                },
            )
    aliases: Dict[str, str] = {}
    if _STATION_NAME_MAP.is_file():
        for raw_line in _STATION_NAME_MAP.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            parts = [part.strip() for part in raw_line.split("\t")]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                continue
            english, chinese = parts
            if english == "英文名":
                continue
            aliases.setdefault(chinese, english)
            aliases.setdefault(english, english)
    return {"stations": stations, "aliases": aliases}


def _load_operation_aliases() -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(
            _OPERATION_ALIAS_RESOURCE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(payload, dict)
        or str(payload.get("schema") or "").strip() != "operation-aliases/v1"
    ):
        return {}
    station_aliases = payload.get("station_aliases")
    return station_aliases if isinstance(station_aliases, dict) else {}


def _find_station_entry(name: str, registry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for entry in registry["stations"].values():
        if name in entry["display_names"] or name == entry["code"]:
            return entry
    return None


def resolve_station(
    name: Any, registry: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    text = str(name or "").strip()
    if not text:
        return None
    if text in KNOWN_EXTERNAL_CONTRACT_MISSING:
        return {
            "station": text,
            "status": "external_contract_missing",
            "via": "platform_export_absence",
            "evidence": "",
        }
    entry = _find_station_entry(text, registry)
    if entry is not None:
        return {
            "station": text,
            "status": "resolved",
            "via": entry["via"],
            "evidence": entry["skill_path"],
        }
    alias_target = registry["aliases"].get(text)
    if alias_target:
        entry = _find_station_entry(alias_target, registry)
        if entry is not None:
            return {
                "station": text,
                "status": "alias_resolved",
                "via": "station_name_map",
                "evidence": str(_STATION_NAME_MAP),
            }
    return {"station": text, "status": "unresolved", "via": "", "evidence": ""}


def resolve_operation(
    operation: Any,
    station_entry: Optional[Dict[str, Any]],
    station_name: str,
    registry: Dict[str, Any],
    operation_aliases: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    text = str(operation or "").strip()
    result = {"operation": text, "station": station_name, "status": "unresolved", "via": ""}
    if not text:
        return result
    station_operations: List[str] = []
    if station_entry is not None:
        station_operations = list(station_entry.get("operations") or [])
        if text in station_operations:
            result.update(status="resolved", via=station_entry["via"])
            return result
    # Station-scoped canonical resolution through the versioned resource.
    # An alias can never make an operation acceptable at an unrelated
    # station: the canonical target must exist in that station's own list.
    candidates: List[Tuple[str, Dict[str, Any]]] = []
    for station_ref, record in operation_aliases.items():
        if not isinstance(record, dict):
            continue
        entry = _find_station_entry(str(station_ref), registry)
        entry_names = set()
        if entry is not None:
            entry_names = set(entry.get("display_names") or []) | {entry["code"]}
        if station_name in entry_names or station_ref == station_name:
            candidates.append((str(station_ref), record))
    for _, record in candidates:
        canonical = str(record.get("canonical") or "").strip()
        aliases = [str(value).strip() for value in (record.get("aliases") or [])]
        if text == canonical and (not station_operations or canonical in station_operations):
            result.update(status="canonical_resolved", via="operation_aliases")
            return result
        if text in aliases and canonical and canonical in station_operations:
            result.update(status="canonical_resolved", via="operation_aliases")
            return result
    return result


def _collect_references(
    package: Optional[Dict[str, Any]],
    macro_plan: Optional[List[Dict[str, Any]]],
    registry: Dict[str, Any],
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Collect (station names) and (station, operation) pairs referenced by
    the V2 package and the raw macro plan.  Text mentions use the registry
    display names as a controlled lexicon (location only, never inference)."""
    stations: List[str] = []
    operations: List[Tuple[str, str]] = []
    lexicon = sorted(
        {
            name
            for entry in registry["stations"].values()
            for name in entry["display_names"]
        }
        | set(registry["aliases"]),
        key=len,
        reverse=True,
    )

    def note_station(name: str) -> None:
        if name and name not in stations:
            stations.append(name)

    def note_operation(station: str, operation: str) -> None:
        pair = (station, operation)
        if operation and pair not in operations:
            operations.append(pair)

    steps: List[Dict[str, Any]] = []
    if isinstance(package, dict):
        steps.extend(
            step for step in (package.get("macro_steps") or []) if isinstance(step, dict)
        )
    if macro_plan:
        steps.extend(step for step in macro_plan if isinstance(step, dict))
    for step in steps:
        for requirement in step.get("quantity_requirements") or []:
            if not isinstance(requirement, dict):
                continue
            workstation = str(requirement.get("workstation") or "").strip()
            operation = str(requirement.get("operation") or "").strip()
            if workstation:
                note_station(workstation)
                if operation:
                    note_operation(workstation, operation)
        text = " ".join(
            str(step.get(key) or "")
            for key in ("操作", "试剂/对象", "参数", "operation", "notes")
        )
        for token in lexicon:
            if token and token in text:
                note_station(token)
    return stations, operations


def _container_lid_alignment(
    package: Optional[Dict[str, Any]],
    registry: Dict[str, Any],
    station_resolution: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Coarse container-type alignment against SKILL I/O constraints.

    Vocabulary-level check only (never the full device-engine validation);
    mismatches are recorded, never blocking.
    """
    checked = 0
    mismatches: List[Dict[str, Any]] = []
    resolved_by_name = {
        item["station"]: item for item in station_resolution if item["status"] in {"resolved", "alias_resolved"}
    }
    steps = (
        [
            step
            for step in (package.get("macro_steps") or [])
            if isinstance(step, dict)
        ]
        if isinstance(package, dict)
        else []
    )
    for step in steps:
        containers = step.get("logical_containers") or []
        if not containers:
            continue
        step_text = " ".join(
            str(step.get(key) or "")
            for key in ("操作", "试剂/对象", "参数", "operation", "notes")
        )
        stations_for_step = [
            name for name in resolved_by_name if name and name in step_text
        ]
        for container in containers:
            if not isinstance(container, dict):
                continue
            container_type = str(container.get("container_type") or "").strip()
            if not container_type or container_type == "unknown":
                continue
            checked += 1
            for station_name in stations_for_step or list(resolved_by_name):
                entry = _find_station_entry(
                    resolved_by_name[station_name]["station"], registry
                ) or _find_station_entry(station_name, registry)
                if entry is None:
                    continue
                constraints = entry.get("container_constraints") or []
                if not constraints:
                    continue
                if container_type not in constraints:
                    mismatches.append(
                        {
                            "step": str(step.get("macro_step_id") or ""),
                            "station": station_name,
                            "container_type": container_type,
                            "skill_constraint": constraints,
                            "skill_path": entry["skill_path"],
                        }
                    )
    return {"checked": checked, "mismatches": mismatches}


def _fallback_detected(
    referenced_stations: List[str], station_resolution: List[Dict[str, Any]]
) -> bool:
    """A resolved station whose normalized name is similar to a known
    missing-contract station is treated as a substitution fallback."""
    status_by_name = {item["station"]: item["status"] for item in station_resolution}
    for name in referenced_stations:
        if name in KNOWN_EXTERNAL_CONTRACT_MISSING:
            continue
        normalized = _normalize_station_name(name)
        if not normalized:
            continue
        for missing in KNOWN_EXTERNAL_CONTRACT_MISSING:
            missing_norm = _normalize_station_name(missing)
            if name == missing or not missing_norm:
                continue
            if status_by_name.get(name) not in {"resolved", "alias_resolved"}:
                continue
            common_prefix = 0
            for left, right in zip(normalized, missing_norm):
                if left != right:
                    break
                common_prefix += 1
            if (
                normalized in missing_norm
                or missing_norm in normalized
                or common_prefix >= 3
            ):
                return True
    return False


def build_strict_entry_preflight(
    research_action_package_v2: Optional[Dict[str, Any]],
    macro_plan: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Phase 5 entry preflight; persisted as a handoff diagnostic block."""
    registry = build_station_registry()
    operation_aliases = _load_operation_aliases()
    referenced_stations, referenced_operations = _collect_references(
        research_action_package_v2, macro_plan, registry
    )
    station_resolution: List[Dict[str, Any]] = []
    entry_by_name: Dict[str, Dict[str, Any]] = {}
    for name in referenced_stations:
        resolution = resolve_station(name, registry)
        if resolution is None:
            continue
        station_resolution.append(resolution)
        entry_by_name[name] = resolution
    operation_resolution: List[Dict[str, Any]] = []
    for station_name, operation in referenced_operations:
        station_entry = None
        resolution = entry_by_name.get(station_name)
        if resolution and resolution["status"] in {"resolved", "alias_resolved"}:
            station_entry = _find_station_entry(
                resolution["station"], registry
            ) or _find_station_entry(station_name, registry)
        operation_resolution.append(
            resolve_operation(
                operation, station_entry, station_name, registry, operation_aliases
            )
        )
    external_contract_missing = sorted(
        {
            item["station"]
            for item in station_resolution
            if item["status"] == "external_contract_missing"
        }
    )
    alignment = _container_lid_alignment(
        research_action_package_v2, registry, station_resolution
    )
    fallback_used = _fallback_detected(referenced_stations, station_resolution)
    return {
        "station_resolution": station_resolution,
        "operation_resolution": operation_resolution,
        "container_lid_alignment": alignment,
        "external_contract_missing": external_contract_missing,
        "fallback_used": fallback_used,
    }
