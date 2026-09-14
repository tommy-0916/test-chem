---
name: experiment-capabilities
description: Provide source-grounded experiment types and technical-route capabilities for literature search and stage planning, without operation I/O or machine parameters.
---

# 实验能力：检索与 Stage

运行时通过 `agent_skills.capabilities.load_capability_skill(device_context, "experiment")`
注入本说明及当前设备上下文的高层能力；仅需事实时可用 `project_device_context`。
联网检索和 Stage 设计必须使用同一上下文及可用性快照。

- 用实验类型与科学目的形成检索词，不把内部工作站编码拼进查询。
- `support_status=supported` 仅表示真源明确声明该实验类型，不保证任意配方、实验条件或整条技术路线都能执行。
- `support_status=unknown` 是真源未声明，不是已证实不支持。`availability` 与能力声明分开：停机设备不可用于当前自动方案，状态未知不得冒充实时在线。
- 该层不读取操作 I/O、容器编号或机器参数；操作选择进入 operation-capabilities，实验步骤进入 macro-step-capabilities。

离线查阅默认实验室能力时读取 [自动生成的能力目录](references/capabilities.json)。
运行时接口会校验来源内容摘要；显式自定义设备上下文不得被默认 45 站覆盖。
引用目录由 `python chem_resources/generate_workstation_capability_index.py --with-skill-references`
从原始工作站 SKILL 与审计约束生成，不直接修改生成文件。
