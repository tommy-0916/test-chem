"""
Agent基类
=========

功能说明:
定义所有Agent的基类，提供统一的LLM调用接口和错误处理机制。

职责:
- 封装LLM调用逻辑
- 提供retry机制
- 提供统一的run接口

当前版本实现:
- [x] BaseAgent基类
- [x] LLM调用封装
- [x] retry机制
- [x] 错误处理

未实现/待完善:
- [ ] 工具调用支持（等待具体Agent开发时实现）
- [ ] 流式输出支持

使用示例:
    from core import BaseAgent
    from utils.llm_factory import LLMFactory

    class MyAgent(BaseAgent):
        def run(self, state):
            # 实现具体逻辑
            result = self.invoke({"input": state.input})
            state.output = result
            return state

版本: v0.1
创建日期: 2026-01-31
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

logger = logging.getLogger(__name__)


class LLMResponseError(Exception):
    """LLM响应错误异常"""
    pass


class BaseAgent:
    """
    Agent基类

    所有Agent的基类，提供统一的LLM调用接口。

    当前版本实现:
        - [x] LLM调用封装
        - [x] retry机制
        - [x] prompt构建

    未实现:
        - [ ] 工具调用（等待具体处理逻辑指定）
    """

    def __init__(
        self,
        model: BaseChatModel,
        system_prompt: Optional[str] = None,
        task_prompt: Optional[str] = None,
        **kwargs
    ) -> None:
        """
        初始化Agent

        Args:
            model: LLM实例（来自LLMFactory.create()）
            system_prompt: 系统提示词（角色设定）
            task_prompt: 任务提示词模板（包含{变量}占位符）
            **kwargs: 其他配置参数
                - max_retries: 最大重试次数，默认5
                - parse_json: 是否解析JSON输出，默认True

        示例:
            >>> model = LLMFactory.create()
            >>> agent = BaseAgent(
            ...     model=model,
            ...     system_prompt="你是一名化学专家",
            ...     task_prompt="请分析以下实验目标：{goal}"
            ... )
        """
        self._model = model
        self._system_prompt = system_prompt
        self._task_prompt = task_prompt
        self._max_retries = kwargs.get("max_retries", 5)
        self._parse_json = kwargs.get("parse_json", True)

    def _coerce_text_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        text = text.strip()
                        if text:
                            parts.append(text)
                        continue
                    if item.get("type") == "text" and item.get("content"):
                        text = str(item["content"]).strip()
                        if text:
                            parts.append(text)
                        continue
                if item is not None:
                    text = str(item).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        if content is None:
            return ""
        return str(content).strip()

    def _build_messages(self, variables: Dict[str, Any]) -> List:
        """
        构建LLM调用的消息列表

        Args:
            variables: 用于填充task_prompt的变量字典

        Returns:
            消息列表，包含SystemMessage和HumanMessage

        示例:
            >>> messages = agent._build_messages({"goal": "合成普鲁士蓝"})
        """
        messages = []

        # 添加系统提示词
        if self._system_prompt:
            messages.append(SystemMessage(content=self._system_prompt))

        # 添加任务提示词（填充变量）
        if self._task_prompt:
            task_content = self._task_prompt.format(**variables)
            messages.append(HumanMessage(content=task_content))

        return messages

    def _parse_response(self, response_content: str) -> Any:
        """
        解析LLM响应内容

        如果parse_json=True，尝试从响应中提取JSON。
        支持从markdown代码块中提取JSON。

        Args:
            response_content: LLM响应的原始文本

        Returns:
            解析后的内容（JSON对象或原始文本）

        示例:
            >>> result = agent._parse_response('```json\\n{"key": "value"}\\n```')
            >>> print(result)
            {'key': 'value'}
        """
        if not self._parse_json:
            return response_content

        # 尝试从markdown代码块中提取JSON
        json_pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
        matches = re.findall(json_pattern, response_content)

        if matches:
            # 使用最后一个匹配的代码块
            json_str = matches[-1].strip()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

        # 尝试直接解析整个响应
        try:
            return json.loads(response_content)
        except json.JSONDecodeError:
            # 如果无法解析JSON，返回原始文本
            return response_content

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((Exception, LLMResponseError)),
        reraise=True
    )
    def _invoke_with_retry(self, messages: List) -> str:
        """
        带重试机制的LLM调用

        Args:
            messages: 消息列表

        Returns:
            LLM响应的文本内容

        Raises:
            LLMResponseError: LLM返回空响应或错误
        """
        try:
            response = self._model.invoke(messages)

            if response is None:
                raise LLMResponseError("LLM returned None response")

            content = self._coerce_text_content(getattr(response, "content", None))
            if not content:
                raise LLMResponseError("LLM returned empty content")

            return content

        except TypeError as e:
            error_msg = str(e)
            if "NoneType" in error_msg or "null value" in error_msg:
                logger.warning(f"API returned malformed response, retrying... Error: {e}")
                raise LLMResponseError(f"API returned malformed response: {e}") from e
            raise

        except Exception as e:
            logger.warning(f"LLM invocation failed: {type(e).__name__}: {e}")
            raise

    def invoke(self, input_dict: Dict[str, Any]) -> Any:
        """
        调用LLM并返回解析后的结果

        Args:
            input_dict: 输入变量字典，用于填充task_prompt

        Returns:
            LLM响应（如果parse_json=True则返回解析后的JSON，否则返回原始文本）

        示例:
            >>> result = agent.invoke({"goal": "合成普鲁士蓝"})
            >>> print(result)
        """
        messages = self._build_messages(input_dict)
        raw_response = self._invoke_with_retry(messages)
        return self._parse_response(raw_response)

    def run(self, state: Any) -> Any:
        """
        运行Agent的主方法（子类必须实现）

        Args:
            state: 工作流状态对象

        Returns:
            更新后的状态对象

        Raises:
            NotImplementedError: 子类必须实现此方法
        """
        raise NotImplementedError("Subclasses must implement run() method")

    def run_safe(self, state: Any) -> Any:
        """
        安全运行Agent，捕获异常并更新状态

        Args:
            state: 工作流状态对象

        Returns:
            更新后的状态对象（如果出错，状态中会记录错误信息）

        示例:
            >>> state = agent.run_safe(state)
            >>> if state.status == "failed":
            ...     print(state.errors)
        """
        try:
            return self.run(state)
        except Exception as e:
            error_msg = f"{self.__class__.__name__} failed: {str(e)}"
            logger.error(error_msg)

            # 更新状态中的错误信息
            if hasattr(state, "errors") and isinstance(state.errors, list):
                state.errors.append(error_msg)
            if hasattr(state, "status"):
                state.status = "failed"

            return state
