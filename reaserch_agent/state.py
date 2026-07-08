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
    experiment_details: str = ""
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
    extracted_protocols: List[Dict[str, Any]] = field(default_factory=list)
    survey_report: Dict[str, Any] = field(default_factory=dict)

    stage_route: List[str] = field(default_factory=list)
    current_stage: str = ""
    current_stage_plan: str = ""
    macro_plan: List[Dict[str, Any]] = field(default_factory=list)
    stage_route_reason: str = ""
    current_stage_reason: str = ""

    latest_observation: Dict[str, Any] = field(default_factory=dict)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    previous_macro_plan: List[Dict[str, Any]] = field(default_factory=list)
    observation_stage_fit: Dict[str, Any] = field(default_factory=dict)
    observation_interpretation: Dict[str, Any] = field(default_factory=dict)
    stage_progress: Dict[str, Any] = field(default_factory=dict)
    stage_progress_status: str = ""
    post_observation_repair_path: str = ""
    manual_handoff: str = ""

    persistent_outputs: Dict[str, Any] = field(default_factory=dict)
    device_adaptation_handoff: Dict[str, Any] = field(default_factory=dict)
    raw_llm_outputs: Dict[str, Any] = field(default_factory=dict)

    campaign_id: str = ""
    reference_inputs: List[Dict[str, Any]] = field(default_factory=list)
    seed_papers: List[Dict[str, Any]] = field(default_factory=list)
    plan_revisions: List[Dict[str, Any]] = field(default_factory=list)
    tool_invocations: List[Dict[str, Any]] = field(default_factory=list)

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
            "从知识库论文抽取的实验过程": self.extracted_protocols,
            "最新 observation": self.latest_observation,
            "observation 与当前 stage 的一致性判断": self.observation_stage_fit,
            "当前 observation 的结构化科学解释": self.observation_interpretation,
            "当前阶段推进状态": self.stage_progress,
            "post observation 修复路径": self.post_observation_repair_path,
            "人工交接摘要": self.manual_handoff,
        }

    def device_adaptation_external_handoff(self) -> Dict[str, Any]:
        return {
            "handoff_type": "research_to_device_adaptation",
            "query": self.event.query,
            "stage 路线": self.stage_route,
            "当前 stage": self.current_stage,
            "当前 stage 的完整化学语义实验计划": self.current_stage_plan,
            "待执行 macro plan": self.macro_plan,
            "stage路线设计理由": self.stage_route_reason,
            "当前stage设计理由": self.current_stage_reason,
            "调研报告": self.survey_report,
            "从知识库论文抽取的实验过程": self.extracted_protocols,
            "知识库命中摘要": [
                {
                    "title": hit.title,
                    "file_path": hit.file_path,
                    "score": hit.score,
                    "problem": hit.problem,
                    "synthesis_summary": hit.synthesis_summary,
                    "experiment_details": hit.experiment_details,
                    "steps": hit.steps[:8],
                    "performance": hit.performance[:5],
                    "matched_terms": hit.matched_terms[:10],
                }
                for hit in self.knowledge_hits[:5]
            ],
            "device_agent_contract": {
                "research_output_level": (
                    "research agent 输出化学语义 macro action：应做什么实验、关键试剂/"
                    "样品对象和关键参数；不负责选择具体机器容器、工作站、容器编号、"
                    "原液瓶位、开盖/关盖、分瓶/配平或机器人动作。"
                ),
                "device_agent_input_should_add": (
                    "当前化学工作站描述、每个设备的 usage/audit_rules、可使用的实验器材/"
                    "容器信息。"
                ),
                "device_agent_responsibility": (
                    "先评估当前 macro action 能否由设备和器材映射执行；若可以，选择"
                    "具体容器/工作站并生成机器 workflow；若不可以，返回 device_feasibility_error "
                    "及无法映射的硬约束。"
                ),
            },
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
