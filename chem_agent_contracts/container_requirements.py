"""Shared, deterministic validation at Research generation and V2 publication.

Legacy missing/null/empty IDs and types retain their adapter defaults: generated
logical IDs and unknown types; count=None -> 1, capacity=None, omitted lid_state=unknown.
Explicit invalid values are never silently rewritten. Extra descriptive Research
metadata stays outside V2, as before; physical device bindings remain forbidden.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .identity import encode_json_scalar_identity
from .v2 import LogicalContainerV2, ValidationIssueV2


PHYSICAL_CONTAINER_FIELDS = (
    "station_id", "workstation_id", "slot_id", "bottle_id", "容器编号", "瓶号", "槽位",
)


def format_container_issue(issue: ValidationIssueV2) -> str:
    return (
        f"{issue.rule}; macro_step_id={issue.macro_step_id}; {issue.field_path}; "
        f"actual={json.dumps(issue.actual, ensure_ascii=False, default=str)}; "
        f"expected={json.dumps(issue.expected, ensure_ascii=False, default=str)}"
    )


class LogicalContainerContractError(ValueError):
    """Carries full paths even when a child Pydantic model rejects a value."""

    def __init__(self, issues: list[ValidationIssueV2]):
        self.issues = issues
        super().__init__(
            "logical container contract quality check failed: "
            + "; ".join(format_container_issue(issue) for issue in issues)
        )


def parse_logical_container_requirements(
    step: dict[str, Any], *, sequence: int, step_id: Any = None,
) -> list[LogicalContainerV2]:
    """Validate/convert without modifying the candidate, using canonical V2 types.

    ``sequence`` and JSON array indices, not model-generated step numbers, locate
    the candidate. Missing IDs can still be filled by the compatibility adapter.
    """
    resolved_step_id = step_id
    if resolved_step_id is None and step.get("macro_step_id") is not None:
        resolved_step_id = step["macro_step_id"]
    if resolved_step_id is None and step.get("logical_step_id") is not None:
        resolved_step_id = step["logical_step_id"]
    if resolved_step_id is None:
        resolved_step_id = f"MS_{sequence:03d}"
    step_id = encode_json_scalar_identity(
        resolved_step_id,
        f"macro_plan[{sequence - 1}].macro_step_id",
    )
    base_path = f"/macro_plan/{sequence - 1}/container_requirements"
    raw_containers = step.get("container_requirements", [])
    issues: list[ValidationIssueV2] = []
    parsed: list[LogicalContainerV2] = []

    def add_issue(path: str, actual: Any, expected: Any, rule: str) -> None:
        issues.append(ValidationIssueV2(
            issue_id=f"logical_container_{sequence}_{len(issues) + 1}",
            validator="research_logical_container_v2", macro_step_id=step_id,
            field_path=path, actual=actual, expected=expected, rule=rule,
            source_ref="chem_agent_contracts/v2.py:LogicalContainerV2",
            repair_scope="macro_step",
        ))

    if not isinstance(raw_containers, list):
        add_issue(base_path, raw_containers, "array", f"第 {sequence} 步逻辑容器需求必须为数组")
        raise LogicalContainerContractError(issues)

    schema = LogicalContainerV2.model_json_schema()["properties"] if raw_containers else {}
    for index, raw in enumerate(raw_containers):
        path = f"{base_path}/{index}"
        if not isinstance(raw, dict):
            add_issue(path, raw, "object", f"第 {sequence} 步第 {index + 1} 个容器必须为对象")
            continue
        logical_id = raw.get("logical_container_id")
        if logical_id is None or (isinstance(logical_id, str) and not logical_id):
            logical_id = f"LC_{step_id}_{index + 1}"
        container_type = raw.get("container_type")
        if container_type is None or (isinstance(container_type, str) and not container_type):
            container_type = "unknown"
        label = (
            f"第 {sequence} 步（{step.get('操作') or step.get('operation') or ''}）"
            f"第 {index + 1} 个逻辑容器 {logical_id}"
        )
        for key in PHYSICAL_CONTAINER_FIELDS:
            if key in raw:
                add_issue(f"{path}/{key}", raw[key], "logical requirement only",
                          f"{label} 不得在 Research 分配实体工作站/瓶号/槽位")
        payload = {
            "logical_container_id": logical_id,
            "container_type": container_type,
            "count": 1 if raw.get("count") is None else raw["count"],
            "capacity_ml": raw.get("capacity_ml"),
            "lid_state": raw.get("lid_state", "unknown"),
        }
        try:
            # Strict input types stop bool/float counts and numeric strings from
            # silently becoming valid requirements; the schema supplies bounds.
            container = LogicalContainerV2.model_validate(payload, strict=True)
            parsed.append(container)
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                field = str(error["loc"][0])
                pointer = "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                   for part in error["loc"])
                field_schema = schema.get(field, {})
                add_issue(f"{path}/{pointer}", raw.get(field),
                          field_schema.get("enum", field_schema),
                          f"{label} {field} 不符合 V2 契约：{error['msg']}")
    if issues:
        raise LogicalContainerContractError(issues)
    return parsed
