---
name: pure-workstation
description: The centrifugal purification workstation is mainly used for the centrifugation, washing, or purification of various mixtures.This skill includes two sections,Function Description and Experimental Protocol Review.
---

# 技能说明
本技能包含详细的执行逻辑和实验方案审核规则，分别存储在两个独立文件中。

## 1. 使用说明文件
- 文件名：'USAGE.md'
- 用途：指导如何使用纯化工作站
- **适用任务**：实验执行、实验生成

## 2. 实验方案审核规则文件
- 文件名：'AUDIT-RULES.md'
- 用途：包含实验方案审核的硬性约束。
- **适用任务**：实验方案审核

## 当前任务判断
- 如果当前任务是**实验方案审核** → **必须读取 AUDIT-RULES.md**
- 如果当前任务是**实验方案生成** → 读取 USAGE.md

**请根据当前任务类型,读取对应的文件。**
