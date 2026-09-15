"""Strongly typed Chem Agent V2 interchange contracts.

The models deliberately describe scientific intent separately from machine
binding.  Research owns the former.  Device may expand it into more steps, but
the immutable Research contract digest makes silent scientific edits visible.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION_V2 = "2.0"


def canonical_digest(value: Any, *, prefix: str = "sha256") -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProvenanceV2(StrictModel):
    kind: Literal["user", "paper", "agent_inferred", "runtime", "device_skill"]
    reference: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def require_inference_rationale(self) -> "ProvenanceV2":
        if self.kind == "agent_inferred" and not self.rationale.strip():
            raise ValueError("agent_inferred provenance requires a rationale")
        return self


class QuantityV2(StrictModel):
    mode: Literal["exact", "all_available", "runtime_measured"] = "exact"
    value: Optional[float] = None
    unit: str = ""

    @model_validator(mode="after")
    def validate_quantity(self) -> "QuantityV2":
        if self.mode == "exact":
            if self.value is None or not self.unit.strip():
                raise ValueError("exact quantity requires value and unit")
            if self.value < 0:
                raise ValueError("quantity value cannot be negative")
        return self


class MaterialPortV2(StrictModel):
    material_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    state: str = "unknown"
    quantity: Optional[QuantityV2] = None
    concentration_value: Optional[float] = None
    concentration_unit: str = ""
    provenance: ProvenanceV2

    @model_validator(mode="after")
    def concentration_has_unit(self) -> "MaterialPortV2":
        if self.concentration_value is not None and not self.concentration_unit.strip():
            raise ValueError("concentration value requires concentration_unit")
        return self


class ScientificParameterV2(StrictModel):
    name: str = Field(min_length=1)
    value: Any
    unit: str = ""
    provenance: ProvenanceV2


LogicalLidStateV2 = Literal["open", "closed", "none", "unknown"]
LOGICAL_LID_STATE_DESCRIPTIONS = {
    "open": "有盖结构的容器当前要求开盖/无盖",
    "closed": "有盖结构的容器当前要求关盖/有盖",
    "none": "容器结构不适用盖子概念（例如无盖结构的 XRD 基底片），不是未知盖状态",
    "unknown": "尚未确定盖状态，不表示已开盖",
}
LOGICAL_LID_STATE_PROMPT = (
    "lid_state 只能为 " + ", ".join(get_args(LogicalLidStateV2)) + "；"
    + "；".join(f"{value}：{LOGICAL_LID_STATE_DESCRIPTIONS[value]}"
               for value in get_args(LogicalLidStateV2))
    + "。不得输出 not_applicable、null 或其他别名。none/unknown 都不能替代真实进样瓶等的开盖要求。"
)


class LogicalContainerV2(StrictModel):
    logical_container_id: str = Field(min_length=1)
    container_type: str = "unknown"
    count: int = Field(default=1, ge=1)
    capacity_ml: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    lid_state: LogicalLidStateV2 = Field(
        default="unknown", description=LOGICAL_LID_STATE_PROMPT
    )


class EvidenceItemV2(StrictModel):
    evidence_id: str = Field(min_length=1)
    title: str = ""
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    verification_status: str = "unknown"
    full_text_status: str = "unknown"
    excerpt: str = ""


class EvidenceBundleV2(StrictModel):
    bundle_id: str = Field(min_length=1)
    scope: Literal["stage", "macro_action"]
    query: str
    objective: str = ""
    retrieval_status: str = "unknown"
    current_invocation_only: bool = True
    items: List[EvidenceItemV2] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class StageV2(StrictModel):
    stage_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    observation_point: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)
    capability_requirements: List[str] = Field(default_factory=list)


class ExperimentGroupV2(StrictModel):
    group_id: str = Field(min_length=1)
    role: Literal["experimental", "control", "repeat", "calibration"] = "experimental"
    sample_id: str = Field(min_length=1)
    hypothesis: str = ""
    comparison_to: List[str] = Field(default_factory=list)
    variables: Dict[str, Any] = Field(default_factory=dict)


class MacroActionV2(StrictModel):
    macro_action_id: str = Field(min_length=1)
    stage_id: str = Field(min_length=1)
    experiment_group: ExperimentGroupV2
    objective: str = Field(min_length=1)
    planned_operations: List[str] = Field(min_length=1)
    expected_observation: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)


class MacroStepV2(StrictModel):
    macro_step_id: str = Field(min_length=1)
    macro_action_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    operation: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    material_inputs: List[MaterialPortV2] = Field(default_factory=list)
    material_outputs: List[MaterialPortV2] = Field(default_factory=list)
    parameters: List[ScientificParameterV2] = Field(default_factory=list)
    logical_containers: List[LogicalContainerV2] = Field(default_factory=list)
    expected_return: List[Dict[str, Any]] = Field(default_factory=list)
    provenance: ProvenanceV2


class ResearchActionPackageV2(StrictModel):
    schema_version: Literal["2.0"] = CONTRACT_VERSION_V2
    campaign_id: str
    capability_snapshot_id: str = ""
    stage: StageV2
    macro_action: MacroActionV2
    macro_steps: List[MacroStepV2] = Field(min_length=1)
    evidence_bundle: EvidenceBundleV2
    research_contract_hash: str = ""

    @model_validator(mode="after")
    def validate_links_and_hash(self) -> "ResearchActionPackageV2":
        action_id = self.macro_action.macro_action_id
        if self.macro_action.stage_id != self.stage.stage_id:
            raise ValueError("macro_action.stage_id must match stage.stage_id")
        if any(step.macro_action_id != action_id for step in self.macro_steps):
            raise ValueError("all macro_steps must reference macro_action_id")
        expected = list(range(1, len(self.macro_steps) + 1))
        actual = [step.sequence for step in self.macro_steps]
        if actual != expected:
            raise ValueError("macro_steps must have contiguous sequence numbers")
        if any(
            step.sample_id != self.macro_action.experiment_group.sample_id
            for step in self.macro_steps
        ):
            raise ValueError("one Research action package may contain only one sample group")
        payload = self.model_dump(mode="json", exclude={"research_contract_hash"})
        digest = canonical_digest(payload, prefix="research_v2")
        if self.research_contract_hash and self.research_contract_hash != digest:
            raise ValueError("research_contract_hash does not match package content")
        self.research_contract_hash = digest
        return self


class WorkstationRequirementV2(StrictModel):
    workstation_task_id: str = Field(min_length=1)
    macro_step_id: str = Field(min_length=1)
    functional_role: str = Field(min_length=1)
    required_capabilities: List[str] = Field(default_factory=list)
    input_state: str = "unknown"
    output_state: str = "unknown"
    candidate_station_codes: List[str] = Field(default_factory=list)


class DeviceStepV2(StrictModel):
    device_step_id: str = Field(min_length=1)
    source_macro_step_id: str = Field(min_length=1)
    workstation_task_id: str = ""
    station_code: str = Field(min_length=1)
    station_version: str = Field(min_length=1)
    platform_name: str = Field(min_length=1)
    station_id: Optional[int] = None
    operation: str = Field(min_length=1)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    container_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    material_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    skill_sha256: str = ""


class WorkstationMappingV2(StrictModel):
    requirements: List[WorkstationRequirementV2] = Field(default_factory=list)
    device_steps: List[DeviceStepV2] = Field(default_factory=list)


class ValidationIssueV2(StrictModel):
    issue_id: str = Field(min_length=1)
    validator: str = Field(min_length=1)
    severity: Literal["error", "warning"] = "error"
    macro_step_id: str = ""
    device_step_id: str = ""
    field_path: str = ""
    actual: Any = None
    expected: Any = None
    rule: str = ""
    source_ref: str = ""
    repair_scope: Literal["device_step", "macro_step", "terminal"] = "device_step"


class ValidationReportV2(StrictModel):
    status: Literal["passed", "failed"]
    rounds_used: int = Field(default=0, ge=0, le=10)
    max_rounds: Literal[10] = 10
    issues: List[ValidationIssueV2] = Field(default_factory=list)
    locked_step_hashes: Dict[str, str] = Field(default_factory=dict)


class DeviceWorkflowPackageV2(StrictModel):
    schema_version: Literal["2.0"] = CONTRACT_VERSION_V2
    status: Literal[
        "ready_for_dispatch",
        "terminal_unmappable",
        "human_review_required",
        "device_internal_error",
    ]
    campaign_id: str = ""
    device_package_id: str
    capability_snapshot_id: str = ""
    research_contract_hash: str
    workstation_mapping: WorkstationMappingV2
    workflow: Dict[str, Any] = Field(default_factory=dict)
    dispatch_payload: Dict[str, Any] = Field(default_factory=dict)
    loaded_workstation_skills: List[Dict[str, Any]] = Field(default_factory=list)
    validation: ValidationReportV2
    terminal_macro_step_ids: List[str] = Field(default_factory=list)


class ParameterTraceV2(StrictModel):
    macro_step_id: str = Field(min_length=1)
    device_step_id: str = ""
    name: str = Field(min_length=1)
    planned_value: Any = None
    device_setpoint: Any = None
    actual_value: Any = None
    unit: str = ""
    deviation: Any = None
    actual_status: Literal["reported", "unavailable", "not_applicable"] = "unavailable"


class ObservationEventV2(StrictModel):
    schema_version: Literal["2.0"] = CONTRACT_VERSION_V2
    campaign_id: str = ""
    macro_action_id: str = Field(min_length=1)
    device_package_id: str = Field(min_length=1)
    execution_status: str
    summary: str = ""
    macro_parameter_summary: List[Dict[str, Any]] = Field(default_factory=list)
    device_parameter_trace: List[ParameterTraceV2] = Field(default_factory=list)
    measurements: Dict[str, Any] = Field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    material_consumption: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
