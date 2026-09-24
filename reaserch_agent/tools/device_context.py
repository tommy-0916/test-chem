"""Device context helpers for the research agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_USAGE_CHAR_LIMIT = 900
DEFAULT_AUDIT_CHAR_LIMIT = 1200
DEFAULT_TOTAL_CHAR_LIMIT = 18000
RESOURCE_ROOT = Path(__file__).resolve().parents[2] / "chem_resources"
DEFAULT_CAPABILITY_INDEX_PATH = RESOURCE_ROOT / "workstation_capability_index.json"

DEVICE_PLANNING_POLICY = (
    "B1 research planning should prefer routes whose containers, operations, "
    "fixed-parameter steps, reaction/testing modules, characterization "
    "handoffs, and observation points are compatible with the loaded device "
    "truth source. Keep output at macro-action level; do not emit workstation "
    "pipeline/control JSON. If the loaded source exposes characterization "
    "stations such as XRD/IR/UV-Vis/GC/LC, they may be used as device-supported "
    "observation points; otherwise keep them as offline handoffs. All listed "
    "workstations are physically connected: an existing sample/container may "
    "move between stations without a human handoff. Material transfer from "
    "container A to container B is also connected through the platform transfer "
    "fabric, but the destination operation's container and sample-state constraints "
    "remain strict. Prefer one continuous sample/container lineage and introduce a "
    "new vessel or solvent-location change only when required by compatibility, "
    "capacity, split/merge, output format, or explicit scientific conditions."
)

UNAVAILABLE_STATUSES = {
    "offline",
    "unavailable",
    "down",
    "maintenance",
    "fault",
    "disabled",
    "停机",
    "维修",
    "故障",
    "不可用",
    "禁用",
}
BUSY_STATUSES = {"busy", "occupied", "占用", "忙碌"}


def load_device_status(path_text: str | Path) -> Dict[str, str]:
    """Read a station-availability map: flat {name: status} or {"stations": {...}}."""
    path = Path(path_text).expanduser().resolve()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict) and isinstance(parsed.get("stations"), dict):
        parsed = parsed["stations"]
    if not isinstance(parsed, dict):
        raise ValueError("device status file must be a JSON object")
    return {
        str(name).strip(): str(status).strip()
        for name, status in parsed.items()
        if str(name).strip()
    }


def apply_device_status(
    context: Dict[str, Any],
    status_map: Dict[str, str],
) -> Dict[str, Any]:
    """Overlay live station availability onto a loaded device context.

    Unavailable stations stay visible but are flagged ⛔ so the research
    layer avoids routes that need them (and the feasibility loop can name
    them explicitly) instead of mistaking them for missing capabilities.
    """
    if not status_map:
        return context
    normalized_status = {
        name.strip().lower(): status for name, status in status_map.items()
    }
    updated = dict(context)
    workstations: List[Dict[str, Any]] = []
    unavailable_names: List[str] = []
    for station in updated.get("workstations", []) or []:
        entry = dict(station)
        status = _lookup_station_status(entry, normalized_status)
        if status:
            entry["availability"] = status
            if status.lower() in UNAVAILABLE_STATUSES:
                entry["availability_note"] = (
                    f"⛔ 当前不可用（{status}）：规划路线必须避开该工作站能力。"
                )
                unavailable_names.append(
                    entry.get("display_name") or entry.get("station_name", "")
                )
            elif status.lower() in BUSY_STATUSES:
                entry["availability_note"] = (
                    f"⚠️ 当前占用（{status}）：可以使用但可能需要等待。"
                )
        workstations.append(entry)
    updated["workstations"] = workstations
    updated["station_status"] = dict(status_map)
    if unavailable_names:
        updated["planning_policy"] = (
            str(updated.get("planning_policy", ""))
            + " 当前设备可用性提示：以下工作站此刻不可用，规划的化学路线不得依赖它们——"
            + "、".join(name for name in unavailable_names if name)
            + "。忙碌(busy)设备可用但可能等待。"
        ).strip()
    return updated


def _lookup_station_status(
    station: Dict[str, Any],
    normalized_status: Dict[str, str],
) -> str:
    for key in ("station_name", "station_code", "station_id", "display_name", "code", "name"):
        value = str(station.get(key, "")).strip().lower()
        if value and value in normalized_status:
            return normalized_status[value]
    return ""


def default_workstations_dir() -> Path:
    for candidate in (
        RESOURCE_ROOT
        / "lab-design-all"
        / "skills"
        / "chemistry-experiment-workstation",
        RESOURCE_ROOT
        / "lab-design-main"
        / "skills"
        / "chemistry-experiment-workstation",
        RESOURCE_ROOT / "workstations_new",
    ):
        if candidate.exists():
            return candidate
    return RESOURCE_ROOT / "workstations_new"


def load_device_context(
    workstations_dir: str | Path | None = None,
    *,
    max_total_chars: int = DEFAULT_TOTAL_CHAR_LIMIT,
) -> Dict[str, Any]:
    """Load a compact device capability context from workstation docs."""
    root = Path(workstations_dir).expanduser().resolve() if workstations_dir else default_workstations_dir()
    root = _normalize_workstations_root(root)
    if not root.exists():
        raise FileNotFoundError(f"workstations directory not found: {root}")

    indexed_context = _load_indexed_device_context(root)
    if indexed_context is not None:
        return _trim_context(indexed_context, max_total_chars)
    if _is_lab_design_root(root):
        from agent_skills.capabilities import load_current_capability_index
        current = load_current_capability_index(source=root)
        context = _context_from_capability_index(current, DEFAULT_CAPABILITY_INDEX_PATH)
        if context is not None:
            return _trim_context(context, max_total_chars)

    workstations: List[Dict[str, Any]] = []
    for station_dir in _iter_station_dirs(root):
        audit_rules = _read_audit_rules(root, station_dir)
        usage = _read_usage_text(root, station_dir)
        skill = _read_text(station_dir / "SKILL.md")
        workstations.append(
            {
                "station_name": station_dir.name,
                "display_name": _station_display_name(root, station_dir.name),
                "module_name": _station_module_name(root, station_dir),
                "capability_summary": _extract_capability_summary(usage or skill),
                "input_output_summary": _extract_input_output_summary(usage or skill),
                "usage_summary": _trim(usage, DEFAULT_USAGE_CHAR_LIMIT),
                "audit_rules_summary": _trim(audit_rules, DEFAULT_AUDIT_CHAR_LIMIT),
                "skill_summary": _trim(skill, 500),
            }
        )

    context: Dict[str, Any] = {
        "source": str(root),
        "planning_policy": DEVICE_PLANNING_POLICY,
        "workstations": workstations,
    }
    return _trim_context(context, max_total_chars)


def load_device_context_from_path(path_text: str | Path) -> Dict[str, Any]:
    path = Path(path_text).expanduser().resolve()
    if path.is_dir():
        return load_device_context(path)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict):
        indexed_context = _context_from_capability_index(parsed, path)
        if indexed_context is not None:
            from agent_skills.capabilities import load_current_capability_index
            current = load_current_capability_index(path, source=indexed_context["source"])
            return _context_from_capability_index(current, path) or indexed_context
        return parsed
    if isinstance(parsed, list):
        return {"source": str(path), "workstations": parsed}
    raise ValueError("device context file must contain a JSON object or array")


def ensure_device_context(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """Return constraints with workstation truth loaded for Research planning.

    The compact ``lab-design-all`` index is a planning invariant, not an
    optional hint.  Callers may supply an explicit context/path/root (for
    example a live-status overlay), but an old
    ``include_default_device_context=false`` flag may no longer make the first
    Research plan blind to equipment capabilities.
    """
    updated = dict(constraints or {})
    if updated.get("device_context"):
        return _ensure_status_applied(updated)

    device_context_path = updated.get("device_context_path")
    if device_context_path:
        updated["device_context"] = load_device_context_from_path(str(device_context_path))
        return _ensure_status_applied(updated)

    workstations_dir = updated.get("device_workstations_dir")
    if workstations_dir:
        updated["device_context"] = load_device_context(str(workstations_dir))
        return _ensure_status_applied(updated)

    updated["device_context"] = load_device_context()

    return _ensure_status_applied(updated)


def _ensure_status_applied(constraints: Dict[str, Any]) -> Dict[str, Any]:
    context = constraints.get("device_context")
    if not isinstance(context, dict):
        return constraints
    if context.get("station_status"):
        return constraints

    status_map: Dict[str, str] = {}
    inline_status = constraints.get("device_status")
    if isinstance(inline_status, dict):
        status_map = {
            str(name): str(status) for name, status in inline_status.items()
        }
    else:
        status_path = constraints.get("device_status_path")
        if status_path:
            try:
                status_map = load_device_status(str(status_path))
            except (OSError, ValueError, json.JSONDecodeError):
                status_map = {}
    if status_map:
        constraints["device_context"] = apply_device_status(context, status_map)
    return constraints


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _load_indexed_device_context(root: Path) -> Optional[Dict[str, Any]]:
    if not DEFAULT_CAPABILITY_INDEX_PATH.exists():
        return None
    try:
        parsed = json.loads(DEFAULT_CAPABILITY_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    context = _context_from_capability_index(parsed, DEFAULT_CAPABILITY_INDEX_PATH)
    if context is None:
        return None
    indexed_source = Path(str(context["source"])).expanduser().resolve()
    if indexed_source != root.resolve():
        return None
    # A Skill/audit edit must be visible immediately, without requiring the
    # user to remember to regenerate a checked-in JSON artifact first.
    from agent_skills.capabilities import load_current_capability_index
    current = load_current_capability_index(DEFAULT_CAPABILITY_INDEX_PATH, source=root)
    return _context_from_capability_index(current, DEFAULT_CAPABILITY_INDEX_PATH)


def _context_from_capability_index(
    parsed: Dict[str, Any],
    index_path: Path,
) -> Optional[Dict[str, Any]]:
    research_context = parsed.get("research_context")
    workstations = parsed.get("workstations")
    source_text = str(parsed.get("source", "")).strip()
    if not (
        parsed.get("source_kind") == "lab-design-all"
        and isinstance(research_context, dict)
        and isinstance(research_context.get("text"), str)
        and isinstance(workstations, list)
        and source_text
    ):
        return None

    source_path = Path(source_text).expanduser()
    if not source_path.is_absolute():
        source_path = Path(__file__).resolve().parents[2] / source_path
    compact_stations = []
    for station in workstations:
        if not isinstance(station, dict):
            continue
        compact_stations.append(
            {
                "station_name": str(station.get("station_code", "")),
                "display_name": str(station.get("display_name", "")),
            }
        )
    return {
        "source": str(source_path.resolve()),
        "capability_index": str(index_path.resolve()),
        "source_kind": "lab-design-all capability index",
        "source_digest_sha256": str(parsed.get("source_digest_sha256", "")),
        "planning_policy": DEVICE_PLANNING_POLICY,
        "container_list": list(
            research_context.get("container_list", parsed.get("container_list", []))
        ),
        "sample_carrier_list": list(
            research_context.get(
                "sample_carrier_list", parsed.get("sample_carrier_list", [])
            )
        ),
        "compact_workstation_capabilities": research_context["text"],
        "compact_capability_semantics": dict(
            research_context.get("semantics", {})
        ),
        # Names are retained separately so live availability overlays can still
        # match either the canonical station code or the Chinese display name.
        "workstations": compact_stations,
    }


def _normalize_workstations_root(root: Path) -> Path:
    if _is_lab_design_root(root):
        return root
    nested = root / "skills" / "chemistry-experiment-workstation"
    if _is_lab_design_root(nested):
        return nested
    return root


def _is_lab_design_root(root: Path) -> bool:
    return root.is_dir() and any(
        (root / module_dir).is_dir()
        for module_dir in (
            "references-Synthesis-Module",
            "references-Reaction-and-Testing-Module",
            "references-Characterization-Module",
        )
    )


def _iter_station_dirs(root: Path) -> List[Path]:
    if _is_lab_design_root(root):
        station_dirs: List[Path] = []
        for module_dir in (
            "references-Synthesis-Module",
            "references-Reaction-and-Testing-Module",
            "references-Characterization-Module",
        ):
            module_path = root / module_dir
            if module_path.is_dir():
                station_dirs.extend(
                    sorted(path for path in module_path.iterdir() if path.is_dir())
                )
        return station_dirs
    return sorted(path for path in root.iterdir() if path.is_dir())


def _read_usage_text(root: Path, station_dir: Path) -> str:
    if _is_lab_design_root(root):
        return _read_text(station_dir / "SKILL.md")
    return _read_text(station_dir / "USAGE.md")


def _read_audit_rules(root: Path, station_dir: Path) -> str:
    if not _is_lab_design_root(root):
        return _read_text(station_dir / "AUDIT-RULES.md")

    exact_path = root / "references_audit" / f"{station_dir.name}_audit.md"
    if exact_path.exists():
        return _read_text(exact_path)
    audit_dir = root / "references_audit"
    if audit_dir.is_dir():
        for path in audit_dir.glob(f"{station_dir.name}_*.md"):
            return _read_text(path)
    return ""


def _station_display_name(root: Path, station_name: str) -> str:
    mapping_path = root / "工作站名称中英文对照.md"
    for line in _read_text(mapping_path).splitlines():
        if "\t" not in line:
            continue
        english_name, chinese_name = [part.strip() for part in line.split("\t", 1)]
        if english_name == station_name:
            return chinese_name
    return station_name


def _station_module_name(root: Path, station_dir: Path) -> str:
    if not _is_lab_design_root(root):
        return ""
    return {
        "references-Synthesis-Module": "Synthesis Module",
        "references-Reaction-and-Testing-Module": "Reaction and Testing Module",
        "references-Characterization-Module": "Characterization Module",
    }.get(station_dir.parent.name, station_dir.parent.name)


def _trim(text: str, max_chars: int) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "\n...[truncated]"


def _trim_context(context: Dict[str, Any], max_total_chars: int) -> Dict[str, Any]:
    text = json.dumps(context, ensure_ascii=False)
    if len(text) <= max_total_chars:
        return context

    trimmed = _compact_context(context, usage_chars=450, audit_chars=650)
    text = json.dumps(trimmed, ensure_ascii=False)
    if len(text) <= max_total_chars:
        return trimmed

    trimmed = _compact_context(context, usage_chars=220, audit_chars=260)
    text = json.dumps(trimmed, ensure_ascii=False)
    if len(text) <= max_total_chars:
        return trimmed

    trimmed = dict(context)
    trimmed["workstations"] = [
        _minimal_station_context(station)
        for station in context.get("workstations", [])
    ]
    trimmed["truncated"] = True
    text = json.dumps(trimmed, ensure_ascii=False)
    if len(text) <= max_total_chars:
        return trimmed

    trimmed["workstations"] = [
        {
            "station_name": station.get("station_name", ""),
            "display_name": station.get("display_name", ""),
            "module_name": station.get("module_name", ""),
            "has_input_output_constraints": bool(station.get("input_output_summary")),
        }
        for station in context.get("workstations", [])
    ]
    return trimmed


def _compact_context(
    context: Dict[str, Any],
    *,
    usage_chars: int,
    audit_chars: int,
) -> Dict[str, Any]:
    trimmed = dict(context)
    compact_workstations = []
    for station in context.get("workstations", []):
        compact_workstations.append(
            {
                "station_name": station.get("station_name", ""),
                "display_name": station.get("display_name", ""),
                "module_name": station.get("module_name", ""),
                "capability_summary": _trim(
                    station.get("capability_summary", ""),
                    min(usage_chars, 220),
                ),
                "input_output_summary": _trim(
                    station.get("input_output_summary", ""),
                    min(audit_chars, 260),
                ),
                "usage_summary": _trim(station.get("usage_summary", ""), usage_chars),
                "audit_rules_summary": _trim(station.get("audit_rules_summary", ""), audit_chars),
            }
        )
    trimmed["workstations"] = compact_workstations
    return trimmed


def _minimal_station_context(station: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "station_name": station.get("station_name", ""),
        "display_name": station.get("display_name", ""),
        "module_name": station.get("module_name", ""),
        "capability_summary": _trim(station.get("capability_summary", ""), 140),
        "input_output_summary": _trim(station.get("input_output_summary", ""), 140),
    }


def _extract_capability_summary(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    description = ""
    operations: List[str] = []
    for line in lines:
        if not description and "description:" in line:
            description = line.split("description:", 1)[1].strip()
            continue
        if line.startswith("- **") or line.startswith("1.**") or line.startswith("1. **"):
            operation = line.replace("1.**", "**", 1).replace("1. **", "**", 1)
            operations.append(operation.strip("- ").strip())
            if len(operations) >= 3:
                break
    parts = []
    if description:
        parts.append(description)
    if operations:
        parts.append("操作: " + "; ".join(operations))
    return _trim(" ".join(parts), 420)


def _extract_input_output_summary(text: str) -> str:
    input_lines = _extract_constraint_lines(text, "输入约束")
    output_lines = _extract_constraint_lines(text, "输出约束")
    parts = []
    if input_lines:
        parts.append("输入: " + "；".join(input_lines))
    if output_lines:
        parts.append("输出: " + "；".join(output_lines))
    return _trim(" | ".join(parts), 420)


def _extract_constraint_lines(text: str, heading: str) -> List[str]:
    lines = [line.strip(" -\t") for line in (text or "").splitlines()]
    capture = False
    captured: List[str] = []
    for line in lines:
        if not line:
            if capture and captured:
                break
            continue
        if heading in line:
            capture = True
            continue
        if capture and ("输入约束" in line or "输出约束" in line or line.startswith("##")):
            break
        if capture and any(key in line for key in ("容器类型", "容器状态", "样品状态", "模板名称")):
            captured.append(line)
            if len(captured) >= 4:
                break
    return captured
