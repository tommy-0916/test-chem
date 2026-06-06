"""
Workflow Generator 状态定义
============================
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict


class WorkflowGeneratorInputState(TypedDict):
    final_goal: str
    macro_plan: str
    macro_plan_summary: str
    observation_requirements: Dict[str, Any]
    goal_in_this_iteration: str
    knowledge: str
    related_workflows_txt: str
    workstation_descriptions: List[Dict[str, Any]]
    use_knowledge_agent: bool
    use_research_agent: bool
    iteration_id: int
    workflow_id: int
    exp_log_path: Optional[str]


class WorkflowGeneratorOutputState(TypedDict):
    workflow_skeleton_txt: str
    workflow_txt: str
    errors: List[str]
    status: str


class WorkflowGeneratorState(WorkflowGeneratorInputState, WorkflowGeneratorOutputState):
    pass


@dataclass
class WorkflowGeneratorTestState:
    final_goal: str
    macro_plan: str = ""
    macro_plan_summary: str = ""
    observation_requirements: Dict[str, Any] = field(default_factory=dict)
    goal_in_this_iteration: str = ""
    knowledge: str = ""
    related_workflows_txt: str = ""
    txt_format_reference: str = ""
    workflow_skeleton_txt: str = ""
    workflow_txt: str = ""
    source_workflow_txt: str = ""
    workstation_descriptions: List[Dict[str, Any]] = field(default_factory=list)
    use_knowledge_agent: bool = False
    use_research_agent: bool = False
    iteration_id: int = 0
    workflow_id: int = 0
    exp_log_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    status: str = "pending"
    forward_skeleton_prompt: str = ""
    forward_parameter_prompt: str = ""
    backward_skeleton_prompt: str = ""
    backward_parameter_prompt: str = ""
