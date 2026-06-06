---
name: material-workstation
description:  The initial area designated for placing containers that do not require reactions exceeding 80°, from which the robot retrieves reaction containers to commence the experiment.This skill includes two sections, Function Description and Experimental Protocol Review.
---

# 物料站技能说明
本技能包含详细的执行逻辑和实验方案审核规则，分别存储在两个独立文件中。

## 1. 物料站使用说明文件
- 文件名：'USAGE.md'
- 用途：指导如何进行物料拿取操作，包含参数设置（容器类型、编号）。
- **适用任务**：实验执行、实验生成

## 2. 物料站实验方案审核规则文件
- 文件名：'AUDIT-RULES.md'
- 用途：包含实验方案审核的硬性约束（如进样瓶不可分批拿取、单一容器原则等）。
- **适用任务**：实验方案审核

## 当前任务判断
- 如果当前任务是**实验方案审核** → **必须读取 AUDIT-RULES.md**
- 如果当前任务是**实验方案生成** → 读取 USAGE.md

**请根据当前任务类型,读取对应的文件。**
