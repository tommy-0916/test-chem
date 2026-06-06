"""
Workflow Generator
==================

功能说明:
根据实验目标和本轮实验计划，生成符合规范的结构化txt实验方案。

职责:
- 根据Pre-flow Agent输出的实验计划，生成可执行的实验方案
- 确保实验方案符合实验室的工作站规范
- 确保实验方案包含每个工作站的全部参数

当前版本实现:
- [x] Forward Task: 从零开始生成实验方案

未实现/待完善:
- [ ] Task2: 根据verify agent意见修改方案
- [ ] Task3: 根据上轮实验结果生成新方案

数据通路:
1. 主数据通路: 通过State在Agent间传递
2. 临时数据通路: 直接读取文件或返回空
3. 日志通路: 通过LogManager实时更新到JSON文件

版本: v0.1
创建日期: 2026-02-01
"""

from .workflow import WorkflowGenerator

__all__ = [
    "WorkflowGenerator",
]
