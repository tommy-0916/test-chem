"""
Format Translate Agent 状态定义
=================================
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict


class FormatTranslateAgentInputState(TypedDict):
    final_goal: str
    macro_plan: str
    observation_requirements: Dict[str, Any]
    goal_in_this_iteration: str
    workflow_txt: str
    workstation_descriptions: List[Dict[str, Any]]
    knowledge: str
    use_knowledge_agent: bool
    use_research_agent: bool
    iteration_id: int
    workflow_id: int
    exp_log_path: Optional[str]


class FormatTranslateAgentOutputState(TypedDict):
    workflow_json: Dict[str, Any]
    errors: List[str]
    status: str


class FormatTranslateAgentState(FormatTranslateAgentInputState, FormatTranslateAgentOutputState):
    pass


@dataclass
class FormatTranslateAgentTestState:
    final_goal: str
    macro_plan: str = ""
    observation_requirements: Dict[str, Any] = field(default_factory=dict)
    goal_in_this_iteration: str = ""
    workflow_txt: str = ""
    workstation_descriptions: List[Dict[str, Any]] = field(default_factory=list)
    knowledge: str = ""
    json_format_reference: str = ""
    workflow_json: Dict[str, Any] = field(default_factory=dict)
    use_knowledge_agent: bool = False
    use_research_agent: bool = False
    iteration_id: int = 0
    workflow_id: int = 0
    exp_log_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    status: str = "pending"
    forward_format_translate_prompt: str = ""
