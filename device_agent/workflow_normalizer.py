"""Deterministic workflow normalization + Skill-contract audit for the Device Agent.

Two responsibilities, both running BEFORE the repair loop sees the workflow:

1. ``normalize_workflow`` fixes what never needed an LLM: it injects each
   step's numeric workstation ``id`` straight from the Skill header, coerces
   scalar parameter types to the declared column type (int/float/string), maps
   enum labels to their declared ``value`` token, and projects slot display
   names only after each material identity is rooted in structured plan/ledger
   evidence. These edits are value-preserving — they never change chemistry,
   only representation.

2. ``contract_audit_errors`` runs the vendored evaluation-grade auditor on the
   raw workflow_json and renders its findings as repair-loop-compatible
   error strings ("第 N 步（工作站/操作）……（error_code）。"), so the chunked
   repair loop can anchor and fix what normalization cannot (invented
   operations/parameters, out-of-range values, volume-limit violations).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .contract_value_normalizer import normalize_contract_scalar
    from .reagent_slot_identity import (
        project_reagent_slot_identities,
        validate_reagent_slot_identity_roots,
    )
except ImportError:  # Direct script compatibility.
    from contract_value_normalizer import normalize_contract_scalar
    from reagent_slot_identity import (
        project_reagent_slot_identities,
        validate_reagent_slot_identity_roots,
    )

try:
    from .skill_contract_audit import (
        LABEL_VALUE_RE,
        OperationSchema,
        ParameterNode,
        StationSchema,
        CaseAuditor,
        _matches_parameter,
        _normalize_type,
        load_workstation_catalog,
    )
except ImportError:  # Direct script compatibility.
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
    def normalize_workflow(
        self,
        workflow_json: Any,
        *,
        reagent_slot_plan: Any = None,
        plan_steps: Any = None,
        batch_plan: Any = None,
        material_ledger: Any = None,
        material_identity_registry: Any = None,
        normalization_events: Optional[List[Dict[str, Any]]] = None,
        normalization_issues: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        """Normalize representation-only fields in place.

        ``normalization_events`` receives one structured, rule-versioned event
        per actual scalar change.  The historical string notes remain for
        backward compatibility with existing terminal packages.  Slot identity
        projection is skipped, with a structured issue, when the supplied IDs
        have no independent batch/registry/ledger root; station and scalar
        normalization still proceed independently.
        """
        notes: List[str] = []
        events = normalization_events if normalization_events is not None else []
        issues = normalization_issues if normalization_issues is not None else []
        if not isinstance(workflow_json, dict):
            return notes
        if reagent_slot_plan is not None:
            root_validation = validate_reagent_slot_identity_roots(
                reagent_slot_plan,
                batch_plan=batch_plan,
                material_ledger=material_ledger,
                material_identity_registry=material_identity_registry,
            )
            if (
                root_validation.claimed_identity_ids
                and not root_validation.anchored
            ):
                if not root_validation.trusted_identity_ids:
                    issues.append({
                        "rule_id": "reagent_slot_identity/v1",
                        "code": "reagent_slot_identity_unanchored",
                        "json_pointer": "/reagent_slot_plan",
                        "message": (
                            "Slot material identities have no independent root in "
                            "batch_plan, material_identity_registry, or material_ledger"
                        ),
                        "material_identity_ids": sorted(
                            root_validation.claimed_identity_ids
                        ),
                    })
                else:
                    issues.append({
                        "rule_id": "reagent_slot_identity/v1",
                        "code": "untrusted_reagent_slot_identity",
                        "json_pointer": "/reagent_slot_plan",
                        "message": (
                            "Slot material identities are absent from the trusted "
                            "material identity roots"
                        ),
                        "material_identity_ids": sorted(
                            root_validation.untrusted_identity_ids
                        ),
                        "trusted_identity_sources": list(
                            root_validation.evidence_sources
                        ),
                    })
            else:
                projection = project_reagent_slot_identities(
                    workflow_json,
                    reagent_slot_plan,
                    station_resolver=self.resolve_station,
                    plan_steps=plan_steps,
                )
                if projection.errors:
                    issues.extend(dict(item) for item in projection.errors)
                else:
                    if projection.events:
                        workflow_json.clear()
                        workflow_json.update(projection.workflow)
                        for item in projection.events:
                            event = dict(item)
                            if "old" in event:
                                event["original_value"] = event.pop("old")
                            if "new" in event:
                                event["new_value"] = event.pop("new")
                            events.append(event)
                        notes.append(
                            "canonicalized "
                            f"{len(projection.events)} reagent-slot display name(s)"
                        )
        steps = workflow_json.get("steps")
        if not isinstance(steps, list):
            return notes
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            station = self.resolve_station(step.get("workstation"))
            if station is None:
                continue
            step_no = step.get("step_number")
            if station.station_id is not None and step.get("id") != station.station_id:
                original_id = step.get("id")
                step["id"] = station.station_id
                notes.append(f"step {step_no}: injected station id {station.station_id}")
                events.append({
                    "rule_id": "skill_station_id/v1",
                    "json_pointer": f"/steps/{step_index}/id",
                    "step_number": step_no,
                    "workstation": step.get("workstation"),
                    "resolved_workstation": station.code,
                    "operation": step.get("operation"),
                    "original_value": original_id,
                    "new_value": station.station_id,
                    "skill_path": str(station.skill_path),
                })
            operation = station.operations.get(str(step.get("operation", "")).strip())
            parameters = step.get("parameters")
            if operation is None or not isinstance(parameters, dict):
                continue
            changed = self._normalize_params(
                parameters,
                operation.parameters,
                path=f"/steps/{step_index}/parameters",
                events=events,
                context={
                    "step_number": step_no,
                    "workstation": step.get("workstation"),
                    "resolved_workstation": station.code,
                    "operation": step.get("operation"),
                    "skill_path": str(station.skill_path),
                },
            )
            if changed:
                notes.append(f"step {step_no}: coerced {changed} parameter value(s)")
        return notes

    def _normalize_params(
        self,
        parameters: Dict[str, Any],
        schema: Dict[str, ParameterNode],
        *,
        path: str = "",
        events: Optional[List[Dict[str, Any]]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        changed = 0
        sink = events if events is not None else []
        event_context = context or {}
        for name in list(parameters.keys()):
            node = self._match_node(name, schema)
            if node is None:
                continue
            pointer = f"{path}/{_pointer_token(name)}" if path else f"/{_pointer_token(name)}"
            new_value, delta = self._normalize_value(
                parameters[name],
                node,
                path=pointer,
                events=sink,
                context=event_context,
            )
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

    def _normalize_value(
        self,
        value: Any,
        node: ParameterNode,
        *,
        path: str = "",
        events: Optional[List[Dict[str, Any]]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, int]:
        declared = _normalize_type(node.type_name)
        changed = 0
        sink = events if events is not None else []
        event_context = context or {}

        # enum: map declared label -> declared value token first
        pairs = LABEL_VALUE_RE.findall(node.evidence_text)
        if pairs and isinstance(value, str):
            label_to_value = {label.strip(): val.strip() for label, val in pairs if label.strip()}
            if value.strip() in label_to_value and value.strip() != label_to_value[value.strip()]:
                original = value
                value = label_to_value[value.strip()]
                changed += 1
                sink.append({
                    "rule_id": "skill_enum_label/v1",
                    "json_pointer": path,
                    "parameter": node.name,
                    "original_value": original,
                    "new_value": value,
                    "declared_type": node.type_name,
                    "declared_unit": node.unit,
                    "skill_line": node.line,
                    **event_context,
                })

        scalar = normalize_contract_scalar(value, node.type_name, node.unit)
        if scalar.accepted and scalar.changed:
            original = value
            value = scalar.new_value
            changed += 1
            sink.append({
                "rule_id": scalar.rule_id,
                "json_pointer": path,
                "parameter": node.name,
                "original_value": original,
                "new_value": value,
                "declared_type": scalar.declared_type,
                "declared_unit": scalar.declared_unit,
                "input_unit": scalar.input_unit,
                "canonical_unit": scalar.canonical_unit,
                "skill_line": node.line,
                **event_context,
            })
        elif declared in {"string", "str"}:
            if isinstance(value, bool):
                pass
            elif isinstance(value, (int, float)):
                original = value
                if isinstance(value, float) and value.is_integer():
                    value = str(int(value))
                else:
                    value = str(value)
                changed += 1
                sink.append({
                    "rule_id": "contract_string_scalar/v1",
                    "json_pointer": path,
                    "parameter": node.name,
                    "original_value": original,
                    "new_value": value,
                    "declared_type": node.type_name,
                    "declared_unit": node.unit,
                    "skill_line": node.line,
                    **event_context,
                })

        # recurse into declared children
        if node.children:
            if declared in {"array", "list", "matrix"} and isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        changed += self._normalize_params(
                            item,
                            {child.name: child for child in node.children},
                            path=f"{path}/{index}",
                            events=sink,
                            context=event_context,
                        )
            elif declared in {"object", "dict"} and isinstance(value, dict):
                changed += self._normalize_params(
                    value,
                    {child.name: child for child in node.children},
                    path=path,
                    events=sink,
                    context=event_context,
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


def _pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")
