"""State models for the research agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ResearchEvent:
    """External event received by the research runtime."""

    event_type: str
    query: str = ""
    constraints: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    """Local corpus hit returned by the knowledge or memory tools."""

    title: str
    file_path: str
    score: float
    problem: str
    synthesis_summary: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    performance: List[Dict[str, Any]] = field(default_factory=list)
    matched_terms: List[str] = field(default_factory=list)


@dataclass
class ResearchAgentState:
    """Runtime state for the partially implemented research agent."""

    event: ResearchEvent
    status: str = "pending"
    current_branch: str = "B0"
    next_branch: Optional[str] = None
    last_completed_branch: Optional[str] = None
    route_message: str = ""
    branch_history: List[str] = field(default_factory=lambda: ["B0"])

    survey_queries: List[str] = field(default_factory=list)
    survey_rounds: List[Dict[str, Any]] = field(default_factory=list)
    memory_queries: List[str] = field(default_factory=list)

    knowledge_hits: List[SearchHit] = field(default_factory=list)
    memory_hits: List[SearchHit] = field(default_factory=list)
    survey_report: Dict[str, Any] = field(default_factory=dict)

    stage_route: List[str] = field(default_factory=list)
    current_stage: str = ""
    current_stage_plan: str = ""
    macro_plan: List[Dict[str, Any]] = field(default_factory=list)
    stage_route_reason: str = ""
    current_stage_reason: str = ""

    persistent_outputs: Dict[str, Any] = field(default_factory=dict)
    device_adaptation_handoff: Dict[str, Any] = field(default_factory=dict)
    raw_llm_outputs: Dict[str, Any] = field(default_factory=dict)

    errors: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def add_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")

    def add_error(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.errors.append(f"[{timestamp}] {message}")

    def research_layer_internal_outputs(self) -> Dict[str, Any]:
        return {
            "stage 路线": self.stage_route,
            "当前 stage": self.current_stage,
            "当前 stage 的完整化学语义实验计划": self.current_stage_plan,
            "待执行 macro plan": self.macro_plan,
            "stage路线设计理由": self.stage_route_reason,
            "当前stage设计理由": self.current_stage_reason,
            "调研报告": self.survey_report,
        }

    def device_adaptation_external_handoff(self) -> Dict[str, Any]:
        return {
            "query": self.event.query,
            "stage 路线": self.stage_route,
            "当前 stage": self.current_stage,
            "当前 stage 的完整化学语义实验计划": self.current_stage_plan,
            "待执行 macro plan": self.macro_plan,
            "stage路线设计理由": self.stage_route_reason,
            "当前stage设计理由": self.current_stage_reason,
        }

    def debug_snapshot(self) -> Dict[str, Any]:
        raw_state = asdict(self)
        raw_state["persistent_outputs"] = self.research_layer_internal_outputs()
        raw_state["device_adaptation_handoff"] = self.device_adaptation_external_handoff()
        raw_state["A. research layer 内部持久化输出"] = self.research_layer_internal_outputs()
        raw_state["B. 发给下游 device adaptation layer agent 的外部交接输出"] = (
            self.device_adaptation_external_handoff()
        )
        return raw_state

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
