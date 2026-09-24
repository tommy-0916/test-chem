---
name: device-capabilities
description: Expose the complete compact 45-workstation catalog to Device and load selected source contracts on demand.
---

# 设备能力：Device 映射

运行时通过 `agent_skills.capabilities.load_capability_tier_skill(device_context, "device")`
读取全部工作站的紧凑目录。目录用于发现候选，不能替代工作站原始合同。

- Device 必须先看到完整工作站目录，不使用随机抽样或固定 Top-K 排除候选。
- LLM 负责提出功能工作站映射和具体版本工作站选择。
- 对每个候选或最终选中的 `station_code`，调用只读工具
  `load_workstation_skill(station_code)`，读取完整 SKILL、audit 与下发合同。
- 确定性校验器负责检查工作站 ID、operation、参数、容器、I/O、物料守恒和平台格式；
  校验器不得静默替 LLM 选择另一台工作站或补造缺失的科学参数。
- 所有 Device step 必须保留 `source_macro_step_id`，使错误可定位并定点修复。
