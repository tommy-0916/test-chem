"""
工具调用框架
===========

功能说明:
提供Knowledge Agent和Research Agent的查询工具，支持双通路切换。

职责:
- 封装Knowledge Agent查询逻辑（双通路）
- 封装Research Agent查询逻辑（双通路）
- 提供工作站加载工具

数据通路说明:
- 临时通路：Knowledge Agent未开发时，直接读取文件；Research Agent未开发时，返回空
- 正式通路：调用实际的Agent（预留接口，当前未实现）

当前版本实现:
- [x] Knowledge Agent查询工具（临时通路）
- [x] Research Agent查询工具（临时通路）
- [x] 双通路切换逻辑
- [x] 工作站加载工具封装

未实现/待完善:
- [ ] Knowledge Agent正式通路（等待Knowledge Agent开发）
- [ ] Research Agent正式通路（等待Research Agent开发）

使用示例:
    from utils.tools import KnowledgeTool, ResearchTool, WorkstationTool

    # 使用临时通路
    knowledge_tool = KnowledgeTool(use_agent=False)
    result = knowledge_tool.query("普鲁士蓝")

    # 使用正式通路（当前会抛出NotImplementedError）
    knowledge_tool = KnowledgeTool(use_agent=True)
    result = knowledge_tool.query("普鲁士蓝")

版本: v0.1
创建日期: 2026-01-31
"""

import os
import logging
from typing import Dict, List, Optional, Any

from utils.paths import knowledge_agent_dir
from utils.workstation_loader import WorkstationLoader

logger = logging.getLogger(__name__)


# ==================== 知识库文件路径 ====================
KNOWLEDGE_BASE_PATH = str(knowledge_agent_dir())
KNOWLEDGE_FILE = os.path.join(KNOWLEDGE_BASE_PATH, "knowledge.txt")
SUMMARY_FILE = os.path.join(KNOWLEDGE_BASE_PATH, "summary.txt")
EXPERIMENT_WORKFLOW_FILE = os.path.join(KNOWLEDGE_BASE_PATH, "expriment_workflow_paper.txt")


class KnowledgeTool:
    """
    Knowledge Agent查询工具

    提供向Knowledge Agent查询知识的功能，支持双通路切换。

    数据通路:
        - 临时通路（use_agent=False）：直接读取文件
        - 正式通路（use_agent=True）：调用Knowledge Agent（预留接口）

    当前版本实现:
        - [x] 临时通路
        - [ ] 正式通路（预留接口）
    """

    def __init__(self, use_agent: bool = False):
        """
        初始化Knowledge查询工具

        Args:
            use_agent: 是否使用Knowledge Agent
                - False: 使用临时通路，直接读取文件
                - True: 使用正式通路，调用Knowledge Agent（当前未实现）

        示例:
            >>> tool = KnowledgeTool(use_agent=False)
        """
        self.use_agent = use_agent
        self._knowledge_agent = None  # 预留：Knowledge Agent实例

    def _read_file(self, file_path: str) -> str:
        """
        读取文件内容

        Args:
            file_path: 文件路径

        Returns:
            文件内容，如果文件不存在返回空字符串
        """
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}")
            return ""

        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def get_knowledge(self) -> str:
        """
        获取背景知识

        临时通路：读取knowledge.txt
        正式通路：调用Knowledge Agent（预留）

        Returns:
            背景知识文本

        示例:
            >>> tool = KnowledgeTool(use_agent=False)
            >>> knowledge = tool.get_knowledge()
        """
        if self.use_agent:
            # 正式通路：调用Knowledge Agent
            # TODO: 等待Knowledge Agent开发后实现
            raise NotImplementedError(
                "Knowledge Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：直接读取文件
            return self._read_file(KNOWLEDGE_FILE)

    def get_summary(self) -> str:
        """
        获取论文知识摘要

        临时通路：读取summary.txt
        正式通路：调用Knowledge Agent（预留）

        Returns:
            论文知识摘要文本

        示例:
            >>> tool = KnowledgeTool(use_agent=False)
            >>> summary = tool.get_summary()
        """
        if self.use_agent:
            # 正式通路：调用Knowledge Agent
            raise NotImplementedError(
                "Knowledge Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：直接读取文件
            return self._read_file(SUMMARY_FILE)

    def get_experiment_workflows(self) -> str:
        """
        获取论文中的实验方案

        临时通路：读取expriment_workflow_paper.txt
        正式通路：调用Knowledge Agent（预留）

        Returns:
            实验方案文本

        示例:
            >>> tool = KnowledgeTool(use_agent=False)
            >>> workflows = tool.get_experiment_workflows()
        """
        if self.use_agent:
            # 正式通路：调用Knowledge Agent
            raise NotImplementedError(
                "Knowledge Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：直接读取文件
            return self._read_file(EXPERIMENT_WORKFLOW_FILE)

    def query(self, query: str) -> Dict[str, str]:
        """
        查询知识库（综合查询）

        返回所有相关知识，包括背景知识、论文摘要、实验方案。

        Args:
            query: 查询内容（当前临时通路忽略此参数，返回全部内容）

        Returns:
            包含knowledge、summary、experiment_workflows的字典

        示例:
            >>> tool = KnowledgeTool(use_agent=False)
            >>> result = tool.query("普鲁士蓝")
            >>> print(result["knowledge"])
        """
        if self.use_agent:
            # 正式通路：调用Knowledge Agent进行智能查询
            raise NotImplementedError(
                "Knowledge Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：返回所有文件内容
            return {
                "knowledge": self.get_knowledge(),
                "summary": self.get_summary(),
                "experiment_workflows": self.get_experiment_workflows()
            }


class ResearchTool:
    """
    Research Agent查询工具

    提供向Research Agent查询实验记录的功能，支持双通路切换。

    数据通路:
        - 临时通路（use_agent=False）：返回空信息
        - 正式通路（use_agent=True）：调用Research Agent（预留接口）

    当前版本实现:
        - [x] 临时通路（返回空）
        - [ ] 正式通路（预留接口）
    """

    def __init__(self, use_agent: bool = False):
        """
        初始化Research查询工具

        Args:
            use_agent: 是否使用Research Agent
                - False: 使用临时通路，返回空信息
                - True: 使用正式通路，调用Research Agent（当前未实现）

        示例:
            >>> tool = ResearchTool(use_agent=False)
        """
        self.use_agent = use_agent
        self._research_agent = None  # 预留：Research Agent实例

    def query_related_experiments(self, goal: str) -> List[str]:
        """
        查询与目标相关的实验记录

        临时通路：返回空列表
        正式通路：调用Research Agent（预留）

        Args:
            goal: 实验目标

        Returns:
            相关实验方案列表（临时通路返回空列表）

        示例:
            >>> tool = ResearchTool(use_agent=False)
            >>> experiments = tool.query_related_experiments("合成普鲁士蓝")
            >>> print(experiments)  # []
        """
        if self.use_agent:
            # 正式通路：调用Research Agent
            raise NotImplementedError(
                "Research Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：返回空列表
            logger.info("ResearchTool: 使用临时通路，返回空列表")
            return []

    def query_previous_workflow(self, exp_id: str, iteration_id: int) -> Optional[Dict]:
        """
        查询上一轮实验的方案

        临时通路：返回None
        正式通路：调用Research Agent（预留）

        Args:
            exp_id: 实验ID
            iteration_id: 迭代ID

        Returns:
            上一轮实验方案（临时通路返回None）

        示例:
            >>> tool = ResearchTool(use_agent=False)
            >>> workflow = tool.query_previous_workflow("exp_20260128_000", 0)
            >>> print(workflow)  # None
        """
        if self.use_agent:
            # 正式通路：调用Research Agent
            raise NotImplementedError(
                "Research Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：返回None
            logger.info("ResearchTool: 使用临时通路，返回None")
            return None

    def query_analysis_and_suggestion(self, exp_id: str, iteration_id: int) -> Dict[str, str]:
        """
        查询上一轮实验的分析和建议

        临时通路：返回空字典
        正式通路：调用Research Agent（预留）

        Args:
            exp_id: 实验ID
            iteration_id: 迭代ID

        Returns:
            包含analysis和suggestion的字典（临时通路返回空字符串）

        示例:
            >>> tool = ResearchTool(use_agent=False)
            >>> result = tool.query_analysis_and_suggestion("exp_20260128_000", 0)
            >>> print(result)  # {"analysis": "", "suggestion": ""}
        """
        if self.use_agent:
            # 正式通路：调用Research Agent
            raise NotImplementedError(
                "Research Agent正式通路尚未实现。"
                "请设置use_agent=False使用临时通路。"
            )
        else:
            # 临时通路：返回空信息
            logger.info("ResearchTool: 使用临时通路，返回空信息")
            return {
                "analysis": "",
                "suggestion": ""
            }


class WorkstationTool:
    """
    工作站加载工具

    封装WorkstationLoader，提供统一的工作站查询接口。

    当前版本实现:
        - [x] 加载所有工作站
        - [x] 格式化为prompt文本
        - [x] 按名称/代码查询
    """

    def __init__(self):
        """
        初始化工作站工具

        示例:
            >>> tool = WorkstationTool()
        """
        self._loader = WorkstationLoader()

    def get_all_workstations(self) -> List[Dict]:
        """
        获取所有工作站配置

        Returns:
            工作站配置列表

        示例:
            >>> tool = WorkstationTool()
            >>> workstations = tool.get_all_workstations()
        """
        return self._loader.get_all()

    def get_workstation_by_code(self, code: str) -> Optional[Dict]:
        """
        根据代码获取工作站

        Args:
            code: 工作站代码

        Returns:
            工作站配置字典，如果不存在返回None

        示例:
            >>> tool = WorkstationTool()
            >>> ws = tool.get_workstation_by_code("liquid_dispensing")
        """
        return self._loader.get_by_code(code)

    def get_workstation_by_name(self, name: str) -> Optional[Dict]:
        """
        根据名称获取工作站

        Args:
            name: 工作站名称

        Returns:
            工作站配置字典，如果不存在返回None

        示例:
            >>> tool = WorkstationTool()
            >>> ws = tool.get_workstation_by_name("液体进样站")
        """
        return self._loader.get_by_name(name)

    def format_for_prompt(self) -> str:
        """
        将所有工作站格式化为prompt可用的文本

        Returns:
            格式化后的文本

        示例:
            >>> tool = WorkstationTool()
            >>> prompt_text = tool.format_for_prompt()
        """
        return self._loader.format_for_prompt()

    def get_operations_summary(self) -> str:
        """
        获取工作站操作摘要

        Returns:
            操作摘要文本

        示例:
            >>> tool = WorkstationTool()
            >>> summary = tool.get_operations_summary()
        """
        return self._loader.format_operations_summary()
