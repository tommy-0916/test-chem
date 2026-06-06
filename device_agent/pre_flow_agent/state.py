"""
Pre-Flow Agent state definitions.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict


class PreFlowAgentInputState(TypedDict):
    final_goal: str
    macro_plan: str
    workstation_descriptions: List[Dict[str, Any]]
    use_knowledge_agent: bool
    use_research_agent: bool
    iteration_id: int
    exp_log_path: Optional[str]


class PreFlowAgentOutputState(TypedDict):
    macro_plan_summary: str
    observation_requirements: Dict[str, Any]
    knowledge: str
    related_workflows_unformatted: str
    goal_in_this_iteration: str
    related_workflows_txt: str
    errors: List[str]
    status: str


class PreFlowAgentState(PreFlowAgentInputState, PreFlowAgentOutputState):
    pass


@dataclass
class PreFlowAgentTestState:
    final_goal: str
    macro_plan: str = ""
    workstation_descriptions: List[Dict[str, Any]] = field(default_factory=list)
    txt_format_reference: str = ""
    macro_plan_summary: str = ""
    observation_requirements: Dict[str, Any] = field(default_factory=dict)
    knowledge: str = ""
    related_workflows_unformatted: str = ""
    goal_in_this_iteration: str = ""
    related_workflows_txt: str = ""
    knowledge_raw_inputs: Dict[str, Any] = field(default_factory=dict)
    memory_raw_inputs: Dict[str, Any] = field(default_factory=dict)
    use_knowledge_agent: bool = False
    use_research_agent: bool = False
    iteration_id: int = 0
    exp_log_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    status: str = "pending"
    knowledge_decision_prompts: List[str] = field(default_factory=list)
    context_summary_prompt: str = ""
    forward_translating_prompt: str = ""
