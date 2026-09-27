# A01 v5：未审阅 PDF 提案的确定性定位（2026-09-27）

## 范围与实现

本轮只改变未审阅提案的引用坐标生产。模型提出字段、值、单位和原文摘录；`produce_pdf_proposal_locators` 在已验签 PDF 的**同一实验组**中调用现有 `bind_pdf_quote(..., asserted_block_locator=None)`。只有字面摘录唯一、符合既有跨块与图题规则时，才在提案深拷贝上写入真实起始 block locator。原始模型 JSON 留在 `raw_llm_outputs`；逐字段记录包含原坐标、计算坐标、完整跨度、PDF 摘要、实验组、布局清单摘要与解析/绑定规则版本。规则失败时保留已定位字段的诊断记录，但整批仍不进入下游。

新副本仍经原有严格关联器和逐字段字面核验。没有修改字段值、单位、试剂、实验组、路线、科学完整性门、发布门或独立审阅策略。定位记录是未审阅产物，不是 `paper_explicit` 证明或化学审阅回执。

## 无模型重放：保存的 A01 提案

输入为 `A01-v5-quote-context-20260927` 保存的原始模型提案及当前受签名约束的同一 Cambridge PDF。枚举得到 8 个 Methods 分节、0 个枚举诊断；8 份原始提案共有 207 个字段摘录。程序在各字段所属实验组中唯一定位 **207/207**，其中 180 个模型锚点已是起始块，27 个被归一化到真实起始块。27 个中有 22 个原本位于合法摘录跨度内但不是起始块，另 5 个确实落在跨度外。

原始提案经严格关联得到 0 份协议，并报告 NiFe Control 与 Electrode Preparation 的 `fact_block_locator_not_in_excerpt_span`。仅替换副本中的定位元数据后，严格关联得到 **8 份未审阅协议、0 个关联诊断**。这证明定位层进展，不代表 8 条合成路线成立。紧接着的逐字段字面核验仍为 `blocked/group_literal_check_failed`：原提案中大量 `unit` 为非字符串，另有部分数值未出现于摘录或归属不清。没有修改这些事实来追求通过。详细摘要在 `result/a01-v5-real-input-20260927/locator-replay-20260927.json`。

## 真实输入本地前向运行

Campaign：`A01-v5-source-locator-20260927`。使用原始开放任务、同一受信 PDF、XRD 当前观察点、`--forward-only --execution-adapter manual`；没有创建 303 task 或执行设备。完整状态与报告位于 `result/a01-v5-real-input-20260927/campaigns/A01-v5-source-locator-20260927/`。

这次模型生成了新的 8 份分节提案，不是上述保存提案的复用。其 85 个字段摘录中，77 个唯一定位、1 个在所属组找不到、7 个超过既有摘录跨度上限。逐字段 `field_records` 保留了 77 个成功定位及 8 个具体失败。整批原子返回仍为 0 份未审阅协议，字面回执因关联诊断而阻断；没有到达独立化学审阅。Stage 任务结构为 4 个样品臂、6 个比较关系，覆盖 **0/4**，最终 `manual_required / stage_coverage_incomplete / required_sample_arms_uncovered`。Research V2 包为空，Device 没有收到可执行 canonical package，真机下发保持关闭。

这次失败的首要直接原因是新摘录未通过现有字面定位规则；不能推断论文缺少路线，也不能推断设备能力不足。独立化学审阅、可信实验组角色和能力映射仍未提供，不会由自动定位冒充。

## 验证

定向测试 **62/62 通过**，覆盖错误模型锚点、缺锚点、同组重复、跨组引文、数值/单位不变、跨块摘录、布局与规则版本变化、原始输出保存、状态重建，以及程序坐标进入原有严格关联器。完整 Research 回归运行 706 项，8 fail + 21 error；与上一轮 694 项基线相比，失败的 29 个测试 ID、FAIL/ERROR 类别、异常类型和仓库栈来源函数均一致。部分异常文本受 Windows 控制台编码和临时路径影响，不能按原始字符串相等解释为完全相同。日志为 `result/a01-v5-real-input-20260927/research-full-unittest-locator.log`。
