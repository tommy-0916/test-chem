"""HTTP request and response models for the ChemAgent backend."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JobMode = Literal["research_preview", "full_campaign"]
ExecutionAdapterName = Literal["mock", "manual"]
WireAPIName = Literal["chat", "codex_responses"]


class CampaignCreate(BaseModel):
    """Flat campaign creation contract shared with the frontend."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=20_000)
    mode: JobMode = "research_preview"
    references: List[str] = Field(default_factory=list, max_length=50)
    execution_adapter: ExecutionAdapterName = "mock"
    max_iterations: int = Field(default=3, ge=1, le=50)
    feasibility_deadlock_limit: int = Field(default=3, ge=1, le=10)
    enable_memory: bool = False
    include_device_context: bool = True
    online_literature: bool = False
    web_search: bool = False
    download_pdfs: bool = False
    full_workstations: bool = False
    wire_api: WireAPIName = "chat"
    reasoning_effort: str = Field(default="xhigh", min_length=1, max_length=32)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized

    @field_validator("references")
    @classmethod
    def normalize_references(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            item = str(value).strip()
            if not item:
                continue
            if len(item) > 4_096:
                raise ValueError("each reference must be at most 4096 characters")
            normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def validate_external_source_options(self) -> "CampaignCreate":
        if self.download_pdfs and not self.online_literature:
            raise ValueError("download_pdfs requires online_literature")
        return self


class Observation(BaseModel):
    """One experiment observation submitted to a waiting manual campaign."""

    model_config = ConfigDict(extra="allow")

    summary: str = Field(min_length=1, max_length=20_000)
    observation_type: str = Field(default="experiment_result", max_length=128)
    status: str = Field(default="success", max_length=128)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=20_000)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("observation summary must not be blank")
        return normalized


class ObservationSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: Observation


class JobView(BaseModel):
    schema_version: str = "1.0"
    id: str
    mode: JobMode
    status: str
    phase: str
    query: str
    execution_adapter: ExecutionAdapterName = "mock"
    current_iteration: int = 0
    max_iterations: int = 1
    stop_reason: Optional[str] = None
    goal_reached: Optional[bool] = None
    outcome_severity: str = "neutral"
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    return_code: Optional[int] = None
    error: Optional[str] = None
    can_cancel: bool = False
    awaiting_observation: bool = False


class ResearchView(BaseModel):
    schema_version: str = "1.0"
    iteration: Optional[int] = None
    query: str = ""
    campaign_id: str = ""
    status: str = ""
    current_branch: str = ""
    next_branch: Optional[str] = None
    last_completed_branch: Optional[str] = None
    route_message: str = ""
    branch_history: List[Any] = Field(default_factory=list)
    current_stage: str = ""
    stage_route: List[Any] = Field(default_factory=list)
    current_stage_plan: str = ""
    macro_plan: List[Any] = Field(default_factory=list)
    stage_route_reason: str = ""
    current_stage_reason: str = ""
    survey_report: Dict[str, Any] = Field(default_factory=dict)
    knowledge_hits: List[Any] = Field(default_factory=list)
    extracted_protocols: List[Any] = Field(default_factory=list)
    latest_observation: Dict[str, Any] = Field(default_factory=dict)
    observations: List[Any] = Field(default_factory=list)
    observation_stage_fit: Dict[str, Any] = Field(default_factory=dict)
    observation_interpretation: Dict[str, Any] = Field(default_factory=dict)
    stage_progress: Dict[str, Any] = Field(default_factory=dict)
    stage_progress_status: str = ""
    post_observation_repair_path: str = ""
    manual_handoff: str = ""
    plan_revisions: List[Any] = Field(default_factory=list)
    created_at: str = ""
    errors: List[Any] = Field(default_factory=list)
    logs: List[Any] = Field(default_factory=list)
    extensions: Dict[str, Any] = Field(default_factory=dict)


class DeviceView(BaseModel):
    schema_version: str = "1.0"
    iteration: Optional[int] = None
    status: str = ""
    exp_id: str = ""
    workflow_txt: str = ""
    workflow_json: Dict[str, Any] = Field(default_factory=dict)
    terminal_package: Dict[str, Any] = Field(default_factory=dict)
    feasibility: Dict[str, Any] = Field(default_factory=dict)
    device_self_check: Dict[str, Any] = Field(default_factory=dict)
    reagent_slot_plan: List[Any] = Field(default_factory=list)
    container_plan: List[Any] = Field(default_factory=list)
    errors: List[Any] = Field(default_factory=list)
    logs: List[Any] = Field(default_factory=list)
    extensions: Dict[str, Any] = Field(default_factory=dict)


class CampaignView(BaseModel):
    schema_version: str = "1.0"
    summary: Dict[str, Any] = Field(default_factory=dict)
    plan_revisions: List[Any] = Field(default_factory=list)
    iterations: List[Dict[str, Any]] = Field(default_factory=list)
    final_report: str = ""


class LogsView(BaseModel):
    lines: List[str] = Field(default_factory=list)
    cursor: int = 0


class CampaignDetail(BaseModel):
    """Stable five-block detail response consumed by the frontend."""

    job: JobView
    research: ResearchView
    device: DeviceView
    campaign: CampaignView
    logs: LogsView


class CampaignList(BaseModel):
    items: List[JobView]
    total: int


class UploadResult(BaseModel):
    id: str
    name: str
    size: int
    content_type: str
    reference: str


class UploadResponse(BaseModel):
    upload: UploadResult
