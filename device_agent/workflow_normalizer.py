"""Deterministic workflow normalization + Skill-contract audit for the Device Agent.

Two responsibilities, both running BEFORE the repair loop sees the workflow:

1. ``normalize_workflow`` fixes what never needed an LLM: it injects each
   step's numeric workstation ``id`` straight from the Skill header, coerces
   scalar parameter types to the declared column type (int/float/string),
   and maps enum labels to their declared ``value`` token. These edits are
   value-preserving — they never change chemistry, only representation.

2. ``contract_audit_errors`` runs the vendored evaluation-grade auditor on the
   raw workflow_json and renders its findings as repair-loop-compatible
   error strings ("第 N 步（工作站/操作）……（error_code）。"), so the chunked
   repair loop can anchor and fix what normalization cannot (invented
   operations/parameters, out-of-range values, volume-limit violations).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skill_contract_audit import (
    LABEL_VALUE_RE,
    OperationSchema,
    ParameterNode,
    StationSchema,
    CaseAuditor,
    _matches_parameter,
    _normalize_type,
    load_workstation_catalog,
)

_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# Findings the in-agent gate must NOT block on: dispatch formatting is audited
# separately by _run_full_checks' formatter preview, and missing recipe files
# are produced at packaging time (the repair LLM cannot create files).
_NON_BLOCKING_CODES = {
    "missing_dispatch_formatting",
    "dispatch_unmapped_steps",
    "dispatch_step_count_mismatch",
    "dispatch_parameter_dropped",
    "dispatch_mapping_warning",
    "dispatch_formatting_warning",
    "missing_input_file",
}


class SkillContractEngine:
    """Loads the workstation Skill catalog once and serves both passes."""

    def __init__(self, workstations_root: Path) -> None:
        self.root = Path(workstations_root)
        self.stations, self.aliases = load_workstation_catalog(self.root)

    # ------------------------------------------------------------ resolve --
    def resolve_station(self, name: Any) -> Optional[StationSchema]:
        text = str(name or "").strip()
        code = self.aliases.get(text) or self.aliases.get(text.replace(" ", ""))
        return self.stations.get(code) if code else None

    # ---------------------------------------------------------- normalize --
    def normalize_workflow(self, workflow_json: Any) -> List[str]:
        """In-place normalization. Returns human-readable change notes."""
        notes: List[str] = []
        if not isinstance(workflow_json, dict):
            return notes
        steps = workflow_json.get("steps")
        if not isinstance(steps, list):
            return notes
        for step in steps:
            if not isinstance(step, dict):
                continue
            station = self.resolve_station(step.get("workstation"))
            if station is None:
                continue
            step_no = step.get("step_number")
            if station.station_id is not None and step.get("id") != station.station_id:
                step["id"] = station.station_id
                notes.append(f"step {step_no}: injected station id {station.station_id}")
            operation = station.operations.get(str(step.get("operation", "")).strip())
            parameters = step.get("parameters")
            if operation is None or not isinstance(parameters, dict):
                continue
            changed = self._normalize_params(parameters, operation.parameters)
            if changed:
                notes.append(f"step {step_no}: coerced {changed} parameter value(s)")
        return notes

    def _normalize_params(
        self, parameters: Dict[str, Any], schema: Dict[str, ParameterNode]
    ) -> int:
        changed = 0
        for name in list(parameters.keys()):
            node = self._match_node(name, schema)
            if node is None:
                continue
            new_value, delta = self._normalize_value(parameters[name], node)
            parameters[name] = new_value
            changed += delta
        return changed

    @staticmethod
    def _match_node(
        actual_name: str, schema: Dict[str, ParameterNode]
    ) -> Optional[ParameterNode]:
        node = schema.get(actual_name)
        if node is not None:
            return node
        for schema_name, candidate in schema.items():
            if _matches_parameter(actual_name, schema_name):
                return candidate
        return None

    def _normalize_value(self, value: Any, node: ParameterNode) -> Tuple[Any, int]:
        declared = _normalize_type(node.type_name)
        changed = 0

        # enum: map declared label -> declared value token first
        pairs = LABEL_VALUE_RE.findall(node.evidence_text)
        if pairs and isinstance(value, str):
            label_to_value = {label.strip(): val.strip() for label, val in pairs if label.strip()}
            if value.strip() in label_to_value and value.strip() != label_to_value[value.strip()]:
                value = label_to_value[value.strip()]
                changed += 1

        if declared in {"int", "integer"}:
            if isinstance(value, bool):
                pass
            elif isinstance(value, float) and value.is_integer():
                value, changed = int(value), changed + 1
            elif isinstance(value, str) and _NUMERIC_RE.fullmatch(value.strip()):
                number = float(value.strip())
                if number.is_integer():
                    value, changed = int(number), changed + 1
        elif declared in {"float", "number", "double"}:
            if isinstance(value, str) and _NUMERIC_RE.fullmatch(value.strip()):
                value, changed = float(value.strip()), changed + 1
        elif declared in {"string", "str"}:
            if isinstance(value, bool):
                pass
            elif isinstance(value, (int, float)):
                if isinstance(value, float) and value.is_integer():
                    value = str(int(value))
                else:
                    value = str(value)
                changed += 1

        # recurse into declared children
        if node.children:
            if declared in {"array", "list", "matrix"} and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        changed += self._normalize_params(
                            item, {child.name: child for child in node.children}
                        )
            elif declared in {"object", "dict"} and isinstance(value, dict):
                changed += self._normalize_params(
                    value, {child.name: child for child in node.children}
                )
        return value, changed

    # -------------------------------------------------------------- audit --
    def contract_audit_errors(self, workflow_json: Any) -> List[str]:
        """Run the evaluation-grade auditor; render blocking findings as
        repair-loop-parsable strings."""
        auditor = CaseAuditor(
            case_id="in-agent",
            case_dir=self.root,
            repo=self.root,
            run_dir=self.root,
            stations=self.stations,
            aliases=self.aliases,
        )
        report = auditor.audit({"workflow_json": workflow_json})
        rendered: List[str] = []
        for finding in report.get("errors", []):
            code = str(finding.get("code", ""))
            if code in _NON_BLOCKING_CODES:
                continue
            rendered.append(_render_finding(finding))
        return rendered


def _render_finding(finding: Dict[str, Any]) -> str:
    step = finding.get("step_number")
    station = finding.get("workstation") or "?"
    operation = finding.get("operation") or "?"
    path = finding.get("parameter_path") or ""
    expected = finding.get("expected")
    actual = finding.get("actual")
    detail = finding.get("message", "")
    parts = []
    if isinstance(step, int):
        parts.append(f"第 {step} 步（{station}／{operation}）")
    else:
        parts.append(f"全局（{station}／{operation}）")
    if path:
        parts.append(f"参数 `{path}`")
    parts.append(str(detail))
    if expected is not None:
        parts.append(f"期望: {_short(expected)}")
    if actual is not None:
        parts.append(f"实际: {_short(actual)}")
    code = str(finding.get("code", "contract_violation"))
    return "，".join(parts) + f"（{code}）。"


def _short(value: Any, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
