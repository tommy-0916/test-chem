# Issue（草稿，未发表）：B01 离线工程联通收口——ledger builder 四语义 + 状态分层 + 两个 dispatch 小项

> 状态：草稿，仅供内部评审，尚未提交 GitHub。
> 关联战役包：`campaigns/B01-v3-k3-engchain-20260926-012716/`（最新一轮，含槽位修复报告）。
> 日期：2026-09-26（v2，按"离线工程联通"口径收缩范围）
>
> **R1 已实施**（同日主分支，未提交）：single_agent.py 增加 quantity_status 语义（known/unknown/pending_measurement）、source_kind 七枚举强制、占位符清洗、production_event_id 去重、audit 按"已知量违规/未知量未标注"分组；新增 test_ledger_quantity_status.py 15 测试全过，全量 884 passed/50 failed（零回归，失败均为 pre-existing）。B01 同输入重审 991→640；nonpositive/missing_source/missing_calculation/insufficient/duplicate_production 五类清零。剩余 top 为跨侧车一致性信号（batch_consumer_allocation_missing_from_ledger 74、transition_input/output_ledger_draw_* 108），根因是三 builder 各自建关系，归 R2 canonical 图；missing_batch_quantity_* 114 为 batch 层出处缺口（该层保持硬失败，R2 可考虑与 ledger 层统一）。campaigns/ 冻结产物未动。
>
> **R2 已实施**：material_relationship_compiler 输出 material_relation_graph（单源事实图），batch/ledger/transition 三视图由图派生；batch 层对齐 quantity_status（无出处即 unknown，planned 值保留 declared_*）；P4 守恒（Σout≤Σin，违例结构化真报，unknown 父量下游保持 unknown）；角色感知 processing_step_refs；图派生恢复被出处门控剥值的 draw/生产行（unknown_yield 不派生产量）。新增 test_material_relation_graph.py 12 测试全过，全量 896/50 零回归。B01 重审 640→229；allocation/draw/missing_batch_quantity/parent_lineage 类清零；守恒检查暴露真实违例 0（57 条数值边全满足）。
>
> **R3 已实施**：dispatch_checker 样品状态拆 planned/verified 双层（verified 仅经 wire 合同步推进；planned 满足仅 verified 缺失 → 新 unverified 信号 sample_state_unverified，不级联阻断，门不放行）；烘干命名三源收敛 canonical=烘干主流程，新增版本化资源 chem_resources/operation_aliases/operation_aliases.json，loader 改读资源（常量表回退）；lid_state_conflict ×12 排查结论=过期校验器假阳性（当前真源链 Cleaning_and_Dispensing 输出盖态传播完整），测试固化未放宽。新增 test_dispatch_state_layering.py 11 测试全过，全量 907/50 零回归。**B01 full-gated errors 10→2**（仅剩 2 个 upstream_gate_failed 冻结段），sample_state_conflict 8→0（转 8 条 honest unverified），blocked 级联 93→61（剩余 55 条链头为真实 ambiguous 多态，按红线不放宽），dispatchable 仍 false。
>
> **总体进度（B01 离线工程联通）**：quantity 991→640→229（剩余 top：unknown_yield 35 显式保留、yield_lower_bound 32 出范围、谱系非空冲突 ~50 待上游数据）；full-gated errors 10→2；两个缺失 wire 合同继续 external_contract_missing 显式阻断；R4 剩余候选：blocked 链 ambiguous 精化（待 measurement_artifact）、transition after_material_states 补声明、50 个 pre-existing 证书簇测试失败。

## 一句话结论

槽位身份层（reagent_slot_identity/v1）已闭环（full-gated errors 60→10）。本轮目标**不是**清零 1012 条 quantity issue、也不是拿到两个缺失 wire 合同，而是：**让内部 Plan → 物料状态 → ledger → workflow → payload 这条软件链在已知信息下自洽；未知事实保持 unknown；外部合同缺失明确隔离**。真正要修的是 ledger builder 的四个基础语义（unknown、来源、推导、谱系），外加状态分层和两个 dispatch 小项；缺 wire 合同与科学人审保持显式阻断，不伪造解决。

## 当前检查事实（修复后，slot-after-fullgate）

| 指标 | 值 |
| --- | --- |
| steps | 74 |
| errors | 10（8 sample_state_conflict + 2 upstream_gate_failed） |
| unverified | 121（93 blocked_by_previous_step + 14 ambiguous_sample_state + 14 dispatch station/wire unverified） |
| dispatchable | false |
| quantity_audit | failed，1012 issue |
| dispatch_validation（包内冻结段） | 1088 error，**已核实为旧版 validator 产物，属过期数据**（详见"问题 3"） |

---

## 一、工程侧必须修的 4 件事（按轮次）

### 第一轮：ledger 基础语义（预计消掉 1012 条中的最大部分）

**P1. unknown ≠ 0（最高优先级）**

现状：ledger entry 模式里根本没有数量状态字段（实测 entry 仅含 produced/consumed/reserved/balance/calculation/source_* 等 15 个键，**无 quantity_status**），上游已有的 `blocked_pending_measurement` 语义在物化成数值台账时丢失，未知量被默认填 0。典型链路：不知道数量 → 填 0 → produced=0 → 后面 consumed=6 → 账不平/非正流量/审计爆炸（139 条 nonpositive_ledger_flow_quantity 即由此而来）。

要求：

- 已知 0 → quantity=0，但无流量事件就不写 consumed/produced 字段；
- 未知 → quantity_status=unknown；
- 未来测量 → quantity_status=pending_measurement；
- 确定值 → quantity=x+unit。
- 禁止 `value = raw or 0`，禁止 `max(value, eps)` —— 这是伪造物料事实。

**P2. 每笔正式数量强制 source + derivation**

现状：64 条 missing_ledger_quantity_source、114 条 missing_ledger_quantity_calculation，batch 层另有 46+46 条同款。数字存在但"为什么是这个数"缺失。

要求 builder 建正式 entry 时强制 source_kind 枚举之一：`research_explicit / device_measurement / derived_from_parent / split_from_parent / merge_from_children / runtime_pending / unknown`。确定值必须同时有 source_refs + calculation；生成不出出处与推导时，**宁可标 unknown，也不建一条看起来完整的假账**（produced=8.0 + source=None + calculation=None 是违规形态）。

**P3. 修 lineage builder，不逐条修约 200 个谱系错误**

现状：consumer 数组混进 `none`（61 条 batch_consumer_lineage_mismatch）；transition parents 与 batch 声明不一致（26 条，extra 两种原液 stock）；processing_step_refs 对不上（63 条）；缺 sample_id/parent_batch；同一生产来源重复入账（76 条）。根因是同一事实由 batch_plan、ledger、transition 三个 builder 各自"猜一遍"，必然漂移。

要求：

- 建一个 canonical material relation/event 图，batch_plan、material_ledger、material_transition 全部由它派生，而非各自独立建关系；
- None/空串/占位符永远不得进入正式 ID 数组；
- parent/child/consumer/processing refs 使用同一套 canonical ID；
- 同一 production event 有稳定 event_id，天然去重。

**P4. split / merge / aliquot 守恒**

B01 是"母批 → 多子批 → 干样 → 悬浊样"长链（母批分裂、多 aliquot Pooling、残余流转），builder 的直线流程假设在此集中爆发。最低要求：

- split：Σ child_quantity ≤ parent_available；
- merge：output ≤ Σ valid_inputs；
- parent_available=unknown 时**不得算出假的确定 output**，下游保持 quantity_status=unknown 或要求 measurement。

### 第二轮：lineage 统一

即 P3 的 canonical 图落地：batch/ledger/transition 三视图同源派生，配合 P4 守恒检查。

### 第三轮：workflow 状态与 dispatch 小项

**P5. 状态分层：planned_state ≠ verified_runtime_state**

步 70 冲突（要求液态/悬浊液、追踪仍是 powder）根因是步 69 批量加液无 wire 合同、迁移未确认。**禁止在步 70 把 sample_state 硬改成 suspension**（把上游未验证事实伪造成已成立）。改为双字段：

- planned_state = suspension（工程静态分析可继续）；
- verified_runtime_state = unknown（正式 dispatch/feasibility 仍不放行）。

分层只用于让离线分析不被单点卡死，**不得成为门禁放行理由**。

**P6. 烘干操作三命名收敛 + alias 资源化**

已核实的现状：workflow 用 `烘干主流程`（当前 validator 已通过，无 unknown_operation）；但三个真源三套名——SKILL.md=`烘干主流程`、dryer.json=`静置烘干`、`OPERATION_ALIAS_MAP`（device_agent/utils/workstation_loader.py:173 的 Python 常量）=`静置烘干/烘干`，且不认 `烘干主流程`。工作项：

- 收敛为版本化资源（如 `chem_resources/.../operation_aliases.json`）中的 canonical 条目 + 站点级 alias；
- 禁止继续向 Python 常量表堆条目；禁止 `if case == "B01"` 特判；禁止默认所有设备上"烘干"等价（station-scoped）。

**P7. lid_state_conflict 排查（修状态机，不放宽规则）**

注意：×12（步 12 四条 + 步 72 八条）只存在于包内过期校验段，当前 validator 并不触发。仍需按以下项排查确认：container_id → 前序开关盖 → 期望/实际盖态 → 本步操作，重点查重复插入开盖、上一步盖态未传播、容器 identity 漂移、translator 重复生成辅助操作。**禁止改成"开盖时当前状态任意"**。

---

## 二、本轮明确不修（保持显式阻断）

1. **两个缺失 wire 合同**（批量加液工作站_V1 步 10/11/69；离心样品转移工作站_V1 步 31/32/33/58）：整个仓库无任何合同来源，属平台资料缺口。标记 external_contract_missing；可向平台方提问"批量加液是否即 Liquid_Pouring_Workstation_V1 别名"，但不得自行映射、不得发明协议。不阻碍验证内部参数正确生成并传到 wire adapter 前一层；阻碍宣称真实平台接口打通。
2. **unknown_yield（35 条）**：无匹配 measurement_artifact，禁止从自由文本猜产率。保持 yield=unknown + requires_measurement=true，只需保证不被写成 0、不生成假的确定库存。
3. **科学人审项**：洗瓶/洗内容物、派生族命名含义、工艺可替代性、真实产率、方案合理性——继续 pending，工程系统只保证"不知道不破坏数据结构"。

---

## 三、工程联通完成标准（离线口径）

不要求 quantity_audit=0 findings，要求：

- **已知量**：来源明确 + calculation 明确 + unit 正确 + lineage 一致 + 不重复记账；
- **未知量**：显式 unknown/pending_measurement + 不默认成 0 + 不进入伪造 balance + 下游真正需要确定量时正确阻断；
- **split/merge**：数量已知 → 守恒检查通过；未知 → 保持 unknown 不伪造；
- **状态**：支持 planned_state 与 verified_runtime_state 分离；
- **平台**：有合同站离线 formatter+checker 通过；无合同站标记 external_contract_missing 而不制造 payload。

## 四、执行顺序

| 轮次 | 内容 | 预计效果 |
| --- | --- | --- |
| R1 | P1 unknown≠0 + P2 source/calculation 强制 + 占位符禁入 + production 去重 | 消掉 1012 条中最大部分（非正流量、账不平、缺出处/推导、重复记账、部分谱系） |
| R2 | P3 canonical 关系图 + P4 split/merge 守恒 | 谱系类错误闭环 |
| R3 | P5 状态分层 + P6 烘干命名/alias 资源化 + P7 lid 状态机排查 | dispatch 噪声清零 |
| 并行 | 向平台方要两个 wire 合同 | 外部依赖，不阻塞 R1–R3 |

## 五、不变的红线

- 禁止 `max(value, eps)` / `raw or 0` 式硬修；
- 禁止在 step 70 之类下游点硬改状态冒充上游事实；
- 禁止占位符（none/空串）进正式 ID 字段；
- 禁止为通过检查而写 case 特判或全局宽松 alias；
- 缺合同节点不得进入正式 dispatch。

---

## 六、背景：槽位身份层（已完成，留档）

v3-k3-engchain 战役已完成 reagent_slot_identity/v1 修复：3 个槽位重编号（移液平台_5ml_V1 3–6 号，锚定 device_plan step 31/39 与 workflow step 41 证据）、14 个 material_identity_id 补登（全部锚定 batch_plan 根批次并经消费步授权）、normalize 投影与 dispatch 镜像同步、权威校验 anchored（untrusted=0）。修复前后：missing_material_identity_id 14→0、missing_slot_binding 36→0、full-gated errors 60→10，connectivity 8/121 无回归。剩余 10 error 均非槽位层（8 状态冲突 + 2 上游门），即本 issue 的内容。报告：`campaigns/B01-v3-k3-engchain-20260926-012716/SLOT_REPAIR_REPORT.md`。

## 七、证据索引

- 修复后检查：`campaigns/B01-v3-k3-engchain-20260926-012716/slot-after-fullgate/workflow_check.json`（10 errors + 121 unverified）
- 台账错误样例：`.../diagnostic_package.json` → quantity_audit.issues（1012 条，六大类分布见上文）
- ledger entry 无状态字段：`.../diagnostic_package.json` → material_ledger.entries 键集
- 过期校验段：`.../diagnostic_package.json` → dispatch_validation（报错文案在当前源码已无对应）
- 当前 full-gated 输出：`b01e_fg.out.json`（findings 无 unknown_operation / lid_state_conflict）
- alias 常量：`device_agent/utils/workstation_loader.py:173`（OPERATION_ALIAS_MAP）
- 上游 pending 语义：`device_agent/material_relationship_compiler.py`（blocked_pending_measurement）、`device_agent/single_agent.py:5762` 起（quantity_status 门禁）
- 工作站注册表：`chem_resources/workstations*/`（9 站，无批量加液/离心样品转移）
