"""
Format Translate Agent Prompts
==============================

导出的提示词:
- SYSTEM_PROMPT: 系统提示词（角色设定）
- FORWARD_FORMAT_TRANSLATE_PROMPT: 任务提示词（格式转译）
"""

from .system_prompt import SYSTEM_PROMPT
from .task_prompts import FORWARD_FORMAT_TRANSLATE_PROMPT

__all__ = [
    "SYSTEM_PROMPT",
    "FORWARD_FORMAT_TRANSLATE_PROMPT"
]