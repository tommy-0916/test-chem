"""
Format Translate Agent 模块
=============================

功能说明:
将经过 verify agent 审核通过的 txt 格式的实验方案转译为指定格式规范的 json 格式，
确保字段对应正确且完整。

导出的类:
- FormatTranslateAgent: 格式转译 Agent
- FormatTranslateAgentTestState: 测试状态类
"""

from .workflow import FormatTranslateAgent
from .state import FormatTranslateAgentTestState

__all__ = [
    "FormatTranslateAgent",
    "FormatTranslateAgentTestState"
]