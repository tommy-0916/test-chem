"""
主工作流状态定义
===============

定义主工作流中 agent 之间传递的状态对象。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkflowState(BaseModel):
    """主工作流状态。"""

    # 输入字段
    final_goal: str = Field(default="", description="兼容字段，语义上等同于macro_plan")
    macro_plan: str = Field(default="", description="research layer 提供的局部实验计划")
    exp_id: Optional[str] = Field(default=None, description="实验ID")
    exp_log_path: Optional[str] = Field(default=None, description="实验日志路径")
    iteration_id: int = Field(default=0, description="当前迭代ID")
    workflow_id: int = Field(default=0, description="当前workflow ID")

    # 配置与参考
    workstation_descriptions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="工作站描述与约束"
    )
    txt_format_reference: str = Field(default="", description="TXT格式参考")
    json_format_reference: str = Field(default="", description="JSON格式参考")
    use_temp_data_flow: bool = Field(default=True, description="是否使用临时数据通路")

    # PreFlow 输出
    macro_plan_summary: str = Field(default="", description="macro_plan摘要")
    observation_requirements: Dict[str, Any] = Field(
        default_factory=dict,
        description="观察要求"
    )
    knowledge: str = Field(default="", description="相关知识汇总")
    related_workflows_unformatted: str = Field(default="", description="相关方案原始文本")
    related_workflows_txt: str = Field(default="", description="相关方案txt")
    goal_in_this_iteration: str = Field(default="", description="兼容字段，本轮翻译目标摘要")
    knowledge_raw_inputs: Dict[str, Any] = Field(default_factory=dict, description="知识原始输入")
    memory_raw_inputs: Dict[str, Any] = Field(default_factory=dict, description="memory原始输入")

    # 设备适应层预可行性判断
    pre_feasibility_prompt: str = Field(default="", description="设备预可行性LLM判断prompt")
    pre_feasibility_raw_response: str = Field(default="", description="设备预可行性LLM原始输出")
    pre_feasibility_report: Dict[str, Any] = Field(default_factory=dict, description="设备预可行性结构化判断")
    pre_feasibility_source: str = Field(default="", description="设备预可行性判断来源")

    # Workflow Generator 输出
    workflow_skeleton_txt: str = Field(default="", description="工作流骨架")
    workflow_txt: str = Field(default="", description="TXT格式实验方案")

    # Verify 输出
    verification_result: str = Field(default="", description="审核结果")
    verification_category: str = Field(default="", description="审核失败类型")
    blocking_constraints: List[str] = Field(default_factory=list, description="阻塞约束")
    verification_suggestion: str = Field(default="", description="修改建议")
    retry_count: int = Field(default=0, description="Task2重写次数")

    # Format Translate 输出
    workflow_json: Optional[Dict[str, Any]] = Field(default=None, description="JSON格式实验方案")
    terminal_package: Optional[Dict[str, Any]] = Field(default=None, description="对外业务包")

    # 跟踪字段
    current_stage: str = Field(default="init", description="当前阶段")
    status: str = Field(default="running", description="运行状态")
    errors: List[str] = Field(default_factory=list, description="错误信息")
    logs: List[str] = Field(default_factory=list, description="日志信息")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="创建时间")

    def add_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")

    def add_error(self, error: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.errors.append(f"[{timestamp}] {error}")
        self.status = "failed"

    def update_stage(self, stage: str) -> None:
        self.current_stage = stage
        self.add_log(f"Stage updated to: {stage}")

    def is_verification_passed(self) -> bool:
        return self.verification_result.lower() == "accepted"

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    class Config:
        arbitrary_types_allowed = True
