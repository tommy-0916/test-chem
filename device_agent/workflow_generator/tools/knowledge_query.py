"""
Knowledge Query 工具
====================

功能说明:
查询知识库中的相关信息，支持双通路设计。

数据通路:
1. 临时数据通路: 直接读取 /workspace/chem_resources/knowledge_agent/ 下的文件
2. 正式数据通路: 调用 Knowledge Agent（预留接口）

注意: Workflow Generator 的 Forward Task 不直接调用此工具，
     因为相关知识已由 Pre-flow Agent 提供。

使用方式:
    from tools.knowledge_query import KnowledgeQuery

    # 使用临时数据通路
    query = KnowledgeQuery(use_knowledge_agent=False)
    knowledge = query.get_knowledge()

版本: v0.1
创建日期: 2026-02-01
"""

import os
from typing import Dict, Any


class KnowledgeQuery:
    """
    知识查询工具

    支持双通路设计，用于查询知识库中的相关信息。

    数据通路:
        - 临时通路: 直接读取文件
        - 正式通路: 调用 Knowledge Agent（预留）
    """

    KNOWLEDGE_DIR = "/workspace/chem_resources/knowledge_agent"

    def __init__(self, use_knowledge_agent: bool = False):
        """
        初始化知识查询工具

        Args:
            use_knowledge_agent: 是否使用 Knowledge Agent
                                 True=调用正式通路，False=使用临时通路

        示例:
            >>> # 使用临时通路
            >>> query = KnowledgeQuery(use_knowledge_agent=False)
            >>> knowledge = query.get_knowledge()

            >>> # 使用正式通路（预留）
            >>> query = KnowledgeQuery(use_knowledge_agent=True)
        """
        self._use_knowledge_agent = use_knowledge_agent

    def get_knowledge(self) -> str:
        """
        获取基础知识

        Returns:
            基础知识文本（来自 knowledge.txt）

        示例:
            >>> query = KnowledgeQuery()
            >>> knowledge = query.get_knowledge()
        """
        if self._use_knowledge_agent:
            # 正式通路：调用 Knowledge Agent（预留）
            raise NotImplementedError("Knowledge Agent not implemented yet")

        # 临时通路：直接读取文件
        filepath = os.path.join(self.KNOWLEDGE_DIR, "knowledge.txt")
        if not os.path.exists(filepath):
            return ""

        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()

    def get_summary(self) -> str:
        """
        获取论文知识摘要

        Returns:
            论文知识摘要（来自 summary.txt）

        示例:
            >>> query = KnowledgeQuery()
            >>> summary = query.get_summary()
        """
        if self._use_knowledge_agent:
            raise NotImplementedError("Knowledge Agent not implemented yet")

        filepath = os.path.join(self.KNOWLEDGE_DIR, "summary.txt")
        if not os.path.exists(filepath):
            return ""

        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()

    def get_paper_workflows(self) -> str:
        """
        获取论文中的实验方案

        Returns:
            论文中的实验方案（来自 expriment_workflow_paper.txt）

        示例:
            >>> query = KnowledgeQuery()
            >>> workflows = query.get_paper_workflows()
        """
        if self._use_knowledge_agent:
            raise NotImplementedError("Knowledge Agent not implemented yet")

        filepath = os.path.join(self.KNOWLEDGE_DIR, "expriment_workflow_paper.txt")
        if not os.path.exists(filepath):
            return ""

        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()

    def get_all(self) -> Dict[str, str]:
        """
        获取所有知识内容

        Returns:
            包含所有知识内容的字典

        示例:
            >>> query = KnowledgeQuery()
            >>> all_knowledge = query.get_all()
        """
        return {
            "knowledge": self.get_knowledge(),
            "summary": self.get_summary(),
            "paper_workflows": self.get_paper_workflows()
        }
