---
name: macro-step-capabilities
description: Supply operation-level material I/O, containers, sample states, dependencies, scientific controls and declared feedback for concrete research macro-step planning.
---

# 步骤能力：Macro Step

运行时通过 `agent_skills.capabilities.load_capability_skill(device_context, "step")`
注入本说明及能力事实；仅需事实时可用 `project_device_context`。
在选定 Macro Action 下编排具体实验步骤。明确物质、用量、容器、样品身份与操作间衔接，
但把最终工作站实例分配及机器参数翻译留给 Device。

- 以 operation 的 I/O 合同确定容器、盖状态、样品相态、数量和载体。参数示例不能扩大可接受输入范围。
- `input` / `output` 保存物理类型与状态；`container_contract` 只补充数量、载体关系及尚未表达的限制，省略与 I/O 完全相同的字段不表示该限制被取消。
- 使用完整 `planning_constraints` 和 `dependencies`，不要用旧的截断摘要替代强约束；前置/后置依赖存在未解析指代时，保留问题并在 Device 映射阶段解析，不自行略过。
- `scientific_controls` 是实验设定量；`output` 是物理输出；`feedback_contract` 才是数据返回声明。三者不能互换。
- `returned_data.status` 与 `intermediate_feedback.status` 分别表示已声明支持、明确不支持或 `unknown`。未知返回不能被用作必需的自动闭环判断，也不能被写成“设备绝不会返回”。
- 维护样品 lineage 与容器/盖状态。新容器、转移、分样和离线交接必须有实验或兼容性依据。
- 不根据目录猜测完整机器参数 schema；Device 选择工作站后加载该站完整 SKILL 与 audit，执行确定性合同校验。

默认真源的详细投影见 [自动生成的步骤能力目录](references/capabilities.json)，仅在此层查阅。
运行时优先使用投影接口以检查来源更新、保留用户自定义设备限制和实时可用性。
通过 `python chem_resources/generate_workstation_capability_index.py --with-skill-references`
更新生成目录，追加 `--check` 检查是否过期。
