---
name: operation-capabilities
description: Expose declared laboratory operations for macro-action planning while withholding operation inputs, outputs, container contracts and machine parameter schemas.
---

# 操作能力：Macro Action

运行时通过 `agent_skills.capabilities.load_capability_skill(device_context, "operation")`
注入本说明及当前允许的操作与支持工作站；仅需事实时可用 `project_device_context`。
组合这些操作形成宏观实验动作，保留原始操作名称及来源。

- 这一层只回答“可执行什么操作”，不分配试剂数量、具体容器或机器控制参数。
- 工作站能力存在与当前设备可用性是两个判断；不能依赖已标记离线的设备。
- 不通过复制底层摘要补充 I/O。动作确定后，在 macro-step-capabilities 层读取操作输入输出与样品衔接约束。

默认真源的操作目录见 [自动生成的操作目录](references/capabilities.json)。
生成与检查命令为 `python chem_resources/generate_workstation_capability_index.py --with-skill-references`
及相同命令追加 `--check`。目录是工作站真源的投影，不是另一份可手工维护的设备合同。
