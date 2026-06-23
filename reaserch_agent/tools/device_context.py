"""Device context helpers for the research agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_USAGE_CHAR_LIMIT = 900
DEFAULT_AUDIT_CHAR_LIMIT = 1200
DEFAULT_TOTAL_CHAR_LIMIT = 9000


def default_workstations_dir() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "chem_resources"
        / "workstations_new"
    )


def load_device_context(
    workstations_dir: str | Path | None = None,
    *,
    max_total_chars: int = DEFAULT_TOTAL_CHAR_LIMIT,
) -> Dict[str, Any]:
    """Load a compact device capability context from workstation docs."""
    root = Path(workstations_dir).expanduser().resolve() if workstations_dir else default_workstations_dir()
    if not root.exists():
        raise FileNotFoundError(f"workstations directory not found: {root}")

    workstations: List[Dict[str, Any]] = []
    for station_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        usage = _read_text(station_dir / "USAGE.md")
        audit_rules = _read_text(station_dir / "AUDIT-RULES.md")
        skill = _read_text(station_dir / "SKILL.md")
        workstations.append(
            {
                "station_name": station_dir.name,
                "usage_summary": _trim(usage, DEFAULT_USAGE_CHAR_LIMIT),
                "audit_rules_summary": _trim(audit_rules, DEFAULT_AUDIT_CHAR_LIMIT),
                "skill_summary": _trim(skill, 500),
            }
        )

    context: Dict[str, Any] = {
        "source": str(root),
        "planning_policy": (
            "B1 research planning should prefer routes whose containers, operations, "
            "fixed-parameter steps, and observation handoffs are compatible with these "
            "device capabilities. Keep output at macro-action level; do not emit "
            "workstation pipeline/control JSON. Also avoid macro routes that require "
            "a sample to move across incompatible container classes when the loaded "
            "device capabilities do not expose a supported transfer or vessel-change "
            "operation; keep chemistry-level steps adaptable to one continuous "
            "container path through reaction, aging/resting, separation, washing, "
            "drying, and testing. Do not satisfy this by merely saying that the "
            "same compatible path is preserved; if resting/aging would require an "
            "incompatible storage vessel, express a chemistry-level alternative such "
            "as continued stirred maturation/reaction in the reaction system, or an "
            "explicit offline/manual handoff and reload. When the loaded device truth "
            "only supports liquid addition from loaded reagent bottles, limited-volume "
            "centrifugation, and no in-stirrer liquid dosing, prefer externally "
            "prepared/loaded precursor solutions, small-volume reaction systems, "
            "sequential or portionwise addition followed by magnetic stirring, and "
            "fixed-condition stirred aging instead of static aging or stirred dosing."
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
        {"station_name": station.get("station_name", "")}
        for station in context.get("workstations", [])
    ]
    trimmed["truncated"] = True
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
                "usage_summary": _trim(station.get("usage_summary", ""), usage_chars),
                "audit_rules_summary": _trim(station.get("audit_rules_summary", ""), audit_chars),
            }
        )
    trimmed["workstations"] = compact_workstations
    return trimmed
