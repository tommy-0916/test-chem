"""
Verify Agent 状态定义
======================
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict


class VerifyAgentInputState(TypedDict):
    final_goal: str
    macro_plan: str
    macro_plan_summary: str
    observation_requirements: Dict[str, Any]
    goal_in_this_iteration: str
    knowledge: str
    related_workflows_txt: str
    workstation_descriptions: List[Dict[str, Any]]
    workflow_txt: str
    use_knowledge_agent: bool
    use_research_agent: bool
    iteration_id: int
    workflow_id: int
    exp_log_path: Optional[str]


class VerifyAgentOutputState(TypedDict):
    verification_result: str
    verification_category: str
    blocking_constraints: List[str]
    verification_suggestion: str
    errors: List[str]
    status: str


class VerifyAgentState(VerifyAgentInputState, VerifyAgentOutputState):
    pass


@dataclass
class VerifyAgentTestState:
    final_goal: str
    macro_plan: str = ""
    macro_plan_summary: str = ""
    observation_requirements: Dict[str, Any] = field(default_factory=dict)
    goal_in_this_iteration: str = ""
    knowledge: str = ""
    related_workflows_txt: str = ""
    workflow_txt: str = ""
    verification_result: str = ""
    verification_category: str = ""
    blocking_constraints: List[str] = field(default_factory=list)
    verification_suggestion: str = ""
    workstation_descriptions: List[Dict[str, Any]] = field(default_factory=list)
    use_knowledge_agent: bool = False
    use_research_agent: bool = False
    iteration_id: int = 0
    workflow_id: int = 0
    exp_log_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    status: str = "pending"
    forward_constraint_verifying_prompt: str = ""
    forward_feasibility_verifying_prompt: str = ""
