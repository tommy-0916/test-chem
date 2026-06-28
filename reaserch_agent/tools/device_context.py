"""Device context helpers for the research agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_USAGE_CHAR_LIMIT = 900
DEFAULT_AUDIT_CHAR_LIMIT = 1200
DEFAULT_TOTAL_CHAR_LIMIT = 18000


def default_workstations_dir() -> Path:
    resource_root = Path(__file__).resolve().parents[2] / "chem_resources"
    for candidate in (
        resource_root
        / "lab-design-main"
        / "skills"
        / "chemistry-experiment-workstation",
        resource_root
        / "lab-design-all"
        / "skills"
        / "chemistry-experiment-workstation",
        resource_root / "workstations_new",
    ):
        if candidate.exists():
            return candidate
    return resource_root / "workstations_new"


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
        "planning_policy": (
            "B1 research planning should prefer routes whose containers, operations, "
            "fixed-parameter steps, reaction/testing modules, characterization "
            "handoffs, and observation points are compatible with the loaded device "
            "truth source. Keep output at macro-action level; do not emit workstation "
            "pipeline/control JSON. If the loaded source exposes characterization "
            "stations such as XRD/IR/UV-Vis/GC/LC, they may be used as device-supported "
            "observation points; otherwise keep them as offline handoffs. Avoid macro "
            "routes that require a sample to move across incompatible container classes "
            "when the loaded device capabilities do not expose a supported transfer or "
            "vessel-change operation."
        ),
        "workstations": workstations,
    }
    return _trim_context(context, max_total_chars)


def load_device_context_from_path(path_text: str | Path) -> Dict[str, Any]:
    path = Path(path_text).expanduser().resolve()
    if path.is_dir():
        return load_device_context(path)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"source": str(path), "workstations": parsed}
    raise ValueError("device context file must contain a JSON object or array")


def ensure_device_context(constraints: Dict[str, Any]) -> Dict[str, Any]:
    """Return constraints with device_context loaded when requested."""
    updated = dict(constraints or {})
    if updated.get("device_context"):
        return updated

    device_context_path = updated.get("device_context_path")
    if device_context_path:
        updated["device_context"] = load_device_context_from_path(str(device_context_path))
        return updated

    workstations_dir = updated.get("device_workstations_dir")
    if workstations_dir:
        updated["device_context"] = load_device_context(str(workstations_dir))
        return updated

    if updated.get("include_default_device_context"):
        updated["device_context"] = load_device_context()

    return updated


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


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
