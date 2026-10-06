# G3 正式模型生成记录 · 2026-10-06 · commit a4ddfa4 之后

入口：`propose_pdf_group_unreviewed`（真实 G1 入口，固定系统提示、有界预算、
有界修订机制原样保留）。模型：k3（LLMFactory，timeout=230 s，max_retries=1）。
来源：Du 2022 SI，经人工补件通道三段记录（received → identity_verified →
ingested）；无 du-2022 签名获取事件，提案仅为评审输入，未进入正式裁决路径。
解析器 route_pdf_groups/v4，枚举 12 组零诊断。

预算遵守：每组一次初始生成；binary 组 1 次调用，HE-PBA 与 PBA-S 组各 2 次
（第二次为入口自带的有界修订）。三组合并提示词超时（APITimeoutError），
逐组调用成功；无扩大预算行为。

## 逐组结果与逐字段缺口

### 1. Synthesis of binary PBAs（生成-synthesis-of-binary-pbas.json，1 次调用）

模型提案：6 操作（两次溶解、B→A 倾倒、搅拌 10 min、陈化 24 h、
离心收集+洗涤合并为一个操作）、material_graph 6 步、route_facts 50 条。
**0 个 unreviewed protocol 被接纳。**

| 缺口类别 | 字段 | 说明 |
| --- | --- | --- |
| semantic_binding_pending ×22 | route_signature.operations[0–5]、各步 operation、输入/输出 state | 状态与操作的语义绑定待证（含湿固体状态） |
| fact_graph_value_mismatch + fact_unit_non_numeric ×5 对 | graph[0–1].material_inputs[*].quantity.unit | 单位字段值与图不一致/非数值单位 |
| required_graph_fact_missing ×8 | graph[2–4] inputs/intermediates name+state | 中间步骤物料事实缺失 |
| route_group_material_instance_scope_conflict ×1 | — | 物料实例 scope 冲突 |

首个剩余 blocker：semantic_binding_pending（操作与状态绑定未闭合）。

### 2. Synthesis of high-entropy PBA（生成-synthesis-of-high-entropy-pba.json，2 次调用含一次有界修订）

模型提案：1 个笼统操作 "synthesis"，输入 Co/Ni/Cu/Mn/Zn（state 均 None），
输出 CoNiCuMnZnFe-PBA（state None）。模型未展开五盐模板细节——与
candidate-chain-v2 的 R1 关系一致：跨组引用不被当作操作展开。
**0 个 unreviewed protocol 被接纳。**

| 缺口类别 | 字段 | 说明 |
| --- | --- | --- |
| fact_quantity_attribution_unresolved ×5 | graph[0].inputs[*].quantity.value | 五盐 0.4 mmol 归属未解 |
| required_graph_fact_missing ×6 | graph[0] inputs[*].state + outputs[0].state | 输入/输出状态全缺 |

首个剩余 blocker：required_graph_fact_missing（状态字段无证据）。

### 3. Synthesis of NiFe-PBA-S, NiCuFe-PBA-S, CoNiCuFe-PBA-S, and HE-PBA-S
（生成-synthesis-of-nife-pba-s-….json，2 次调用含一次有界修订）

模型提案：2 操作（研磨 30 min；置入 50 mL 密封 Teflon autoclave 并
130 °C/12 h），物料链 NiFe-PBA powder + sublimed sulfur → fine mixture →
NiFe-PBA-S composites。与 candidate-chain-v2 的 B1–B3 一致（B2/B3 合并）。
**0 个 unreviewed protocol 被接纳。**

| 缺口类别 | 字段 | 说明 |
| --- | --- | --- |
| required_graph_fact_missing ×5 | graph[0].inputs[1].state、graph[0].outputs[0].state、graph[1].inputs[0].name/state、outputs[0].state | 硫输入态、混合物与复合物输出态缺证据 |

首个剩余 blocker：required_graph_fact_missing。

## 结论

- 摄入链路（16 MiB 预算 + v4 解析器 + 人工补件）已通：真实 SI → 12 组
  枚举 → 三组真实模型生成完成，原始响应与诊断均已存档。
- 候选链 v2（手工核验标签）与模型原始提案并列交付；二者在操作层一致，
  差异：模型合并了离心+洗涤、B2+B3，且对 HE-PBA 只给笼统操作。
- 所有状态字段仍按现有证明边界待证：无一字段进入正式裁决，零 token，
  未扩 v1；ms7a.out 级联不受影响（本轮未触碰 A01）。
- 字段证明成功 0 / 组级 receipt 未运行 / 协议准入 0 / 发布未运行——
  不混称成功。
