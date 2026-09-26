# 科学语义修复实施方案（设计稿，待审核，未动工）

> 状态：**v2 已按审核意见修订,Phase 1–5 全部实施完成**(2026-09-26,未提交)。依据：科学语义修复交接说明（14 节）+ Research 层勘察报告 + 设备严格入口（lab-design-all.zip）核读 + 用户审核意见（4 决策 + 4 边界修正 A–D）。
> 关联：device 侧 R1–R3 已完成（quantity 语义 991→640→229，full-gated errors 10→2，commit b416b77 已推 test-chem）。

## 审核结论固化（v2 变更）

**四项决策**：① 保留 `ProvenanceV2.kind`，新增 `evidence_class`（kind=数据怎么来的；evidence_class=科学依据哪一类，两维正交）；② 状态词表放 `chem_resources/material_states/` 独立资源；③ 本轮不用 zip 覆盖仓库 `chem_resources/lab-design-all/`，只生成 diff 产物（见文末产物清单）；④ extraction 先 section 级，schema 预留 `locator`/`excerpt_hash` 细粒度接口（当前 null）。

**四条边界（写死，防隐式猜测回流）**：

- **A. kind 与 evidence_class 彻底分工**：`agent_inferred` 不删除——禁止的是 *unbounded* inference。允许：`agent_inferred + 有效 convention rule_id`、`agent_inferred + 形式化 derivation`（算术/守恒/谱系推导）；禁止：`agent_inferred` 无 rule_id/derivation 发布。"该事实是模型推导而非论文原文"的审计信息必须保留。
- **B. Phase 2 建双图+容器链，不是纯状态图**：**material lineage graph**（谁是谁的后代：split/merge/aliquot 只改 lineage 不改 state）与 **material state graph**（同物料状态变化：干燥/离心/洗涤改相/保留语义）分开；图检查字段：parent/child material、relation type、before/after state、retained/discarded、container before/after。aliquot/transfer/split/merge 与 drying/centrifugation/washing/filtration 走不同检查路径。
- **C. runtime_pending 的放行条件**：`runtime_pending` 本身不构成 scientific blocker，**仅当存在明确的 runtime measurement/resolution path（首个消费步之前）才允许进入 Device**。`runtime_pending + 下游数值依赖 + 无解析测量事件/能力 = BLOCK`；`runtime_pending + 消费前已排程测量 = PASS`。与 device 侧 quantity ledger R1–R3 语义直接衔接。
- **D. convention 规则必须带适用条件**：每条规则含 rule_id/version/preconditions（输入状态+文本意图条件）/allowed input states/output states/retained vs discard 语义/lineage effect/`numeric_generation_allowed: false`。规则库是 deterministic semantic expansion，不是新的 LLM 常识库（例：`collect precipitate` 与 `collect supernatant` 命中不同规则）。

## 0. 勘察关键结论（设计的现实基础）

1. **V2 合同地基好**：`ProvenanceV2`（六 kind：user/paper/agent_inferred/manual_revision/runtime/device_skill，paper 有 evidence_id/excerpt/digest 四级绑定）、`QuantityV2`（mode/semantic）、`MaterialRelationV2`（event_kind/quantity_basis）、`MacroStepV2`（material_inputs/outputs/parameters/logical_containers/provenance）已存在且发布门强制（`workflow.py:2443`）。**不是从零建，是补三类缺口**。
2. **三个真实缺口**：
   - 无 chemistry_convention 概念：惯例散在 prompt 散文和 V1 硬编码模板（`workflow.py:9205`，V2 路径禁用）；
   - 无显式 material-state graph（Research 层是"关系+段"，不是 node/edge 状态图；状态词表无受控枚举）;
   - 无 Scientific Completeness Audit（有发布门质量检查，但无 evidence_class 分类统计与 unsupported 阻断语义）。
3. **跨层数量词汇已有一条桥**：`material_relationship_compiler.py:4901` 把 `runtime_measurement_required → pending_measurement`。Research 侧补 status 词汇后与此对齐即可，device 侧不动。
4. **设备严格入口（zip）核实**：工作站 SKILL 合同库 + `0410数据转换.txt` wire 导出（2609 行，与仓库副本同_station 集合_）+ 流程"按工作站规则生成方案 → 复核 → 逻辑审计闭合 → JSON → workflow-generator → lab-operation"。**新版仍无批量加液/离心样品转移两站合同**——外部依赖结论不变，方案中保持 external_contract_missing。
5. **zip 与仓库副本差异**：audit 文件批量再生成、README 差异（仓库版多 workflow-checker 技能入口）、`0410数据转换.txt` 有差异待核。**待用户决策是否同步仓库副本**（见 §8 问题 3）。

## 1. 总体设计：三层分离 + 五分类 provenance

```
Literature procedure(含 tacit knowledge)
        ↓  retrieval + extraction(带出处)
Research scientific representation(V2 合同 + material-state graph)
        ↓  Scientific Completeness Audit(发布门前)
Device execution workflow(严格入口:工作站合同 + wire 导出)
```

**provenance 五分类落地方式（推荐）**：不改 `ProvenanceV2.kind` 枚举（兼容已有合同与测试），新增规范化字段 `evidence_class ∈ {paper_explicit, chemistry_convention, device_sop, runtime_measurement, unsupported}`，与 kind 显式映射：

| 现有 kind | evidence_class |
|---|---|
| paper（四级绑定通过） | paper_explicit |
| device_skill | device_sop |
| runtime | runtime_measurement |
| agent_inferred | **禁止直通**：必须改写为 convention（带 rule_id）或降级 unsupported，发布门拦截裸 agent_inferred 参数 |
| manual_revision | 修订机制标签，与 evidence_class 正交（修订必须保留原 evidence_class） |

`unsupported` 不是 provenance 值，而是 completeness audit 对"必需但无证据"事实的判定结果，落在 `material_contract_status=unresolved` + 结构化 gap 记录（见 §4）。

## 2. 分阶段实施计划

### Phase 1 — 受控 chemistry convention 规则库（机器可读，非 LLM 自由发挥）

- 新增版本化资源 `chem_resources/chemistry_conventions/conventions.json`（`chemistry-conventions/v1`），规则结构按边界 D 写死：`rule_id / version / operation / preconditions(输入状态+文本意图) / allowed_input_states / output_states / retained_output / discard_outputs / lineage_effect / numeric_generation_allowed: false`。首批 8 个操作：**centrifugation、washing、drying、filtration、aliquot、transfer、split、merge**（后三者与 device ledger 的 split_from_parent/merge_from_children 直接对齐）：
  - `centrifugation`：`collect precipitate` 与 `collect supernatant` 是两条不同规则（retained 相不同）；输出 retained_wet_solid + supernatant；
  - `washing`：wet_solid + wash_solvent → washed_wet_solid + wash_supernatant；多次洗涤保持同一 material lineage；
  - `drying`：wet_solid → dry_solid；**质量恒为 runtime_pending，不得生成确定值**；
  - `filtration`：suspension → retained_solid + filtrate，目标相由原文命中规则决定；
  - `aliquot/sampling`：parent → sampled_fraction + remaining_parent，必须 split_from_parent 谱系。
- 每条规则：rule_id、输入/输出状态（受控状态词表）、lineage 关系类型、**显式非目标**（"只补状态关系，不创造数字"）。
- 应用通道：`workflow.py` 发布门后新增 convention 展开 pass——只展开状态/相/谱系，展开处写 provenance{evidence_class: chemistry_convention, inference_rule: rule_id}；**凡涉及数值一律不展开**。
- V1 硬编码 PBA 模板（`workflow.py:9205`）标记 deprecated 并加警告日志，V2 路径保持禁用。

### Phase 2 — material lineage + state 双图（边界 B）

- 受控**状态词表**（`chem_resources/material_states/v1`，独立资源；与 device 侧样品状态集兼容）：powder / suspension / solution / retained_wet_solid / washed_wet_solid / dry_solid / supernatant / filtrate / gas …（首版对齐 B01/A01 实证状态；未识别值进 unknown，不报错）。
- `MacroStepV2` 增量扩展：`state_transition: {before_state, after_state, confidence: explicit|convention|unknown}` + `lineage_relation: {relation_type: split_from_parent|merge_from_children|aliquot_of|transfer_of|state_change_of, parent_material_instance_ids, child_material_instance_ids}` + 容器链（container before/after）；MaterialPortV2.state 收敛到词表。
- 发布门图检查（边界 B 清单）：final sample 可追溯到 root materials；无 orphan output；非初始 material 必须有 parent/source；split 后 parent-child 明确；merge 输出追到全部 children；discard phase 明确；operation 输入必须来自先前存在的 material；state transition 符合受控词表；**lineage 与 state 分检**（transfer/split/merge 允许 state 不变、lineage 变）。

### Phase 3 — Scientific Completeness Audit（发布门前，`workflow.py` 发布门内）

对每个 material/process relation 检查交接说明 §12 的 10 项，输出：

```json
"scientific_completeness": {
  "paper_explicit": 31, "chemistry_convention": 17, "device_sop": 8,
  "runtime_measurement": 6, "unsupported": 2,
  "unsupported_details": [{fact, why_required, ladder_exhausted: [...]}],
  "requires_scientific_review": [{risk, step, reason}]
}
```

- **阻断条件（按审核修正）**：`required_unsupported > 0` **或** `unresolved_runtime_dependency > 0`（边界 C：runtime_pending + 下游数值依赖 + 消费前无解析测量事件/能力）**或** `scientific_fidelity_risk_requiring_review > 0`（含加料-搅拌交替等保真风险，无等价性证据即保持 true）**或** `broken_material_lineage > 0`。**unsupported 区分 required 与 non-required**（如论文没写溶液颜色且 workflow 不依赖 → 不阻断）；`runtime_pending` 有 resolution path 时不阻断。
- 执行保真风险标记：加料-搅拌交替 vs 连续搅拌等改变局部浓度/动力学的适配 → `requires_scientific_review=true`，附等价性证据字段（无证据则保持 true，Device 侧 R3 已有的 unverified 语义承接）。

### Phase 4 — retrieval 证据阶梯 + extraction 出处 + A01 防回归

- **空字段 ≠ literature insufficient**：`known_gaps` 结构化，记录逐级排查位置：`main_text → supporting_information → cited_method → extraction_completeness → convention_expansion → device_sop → runtime_pending → unsupported`（`workflow.py:2173 _evidence_packet` 扩展）。
- **extraction 出处**：structured_outputs 参数抽取增加 source 定位，schema 预留细粒度接口（当前 `locator=null`）：`{source_document, section, locator: null, excerpt_hash: null}`——未来升 paragraph/sentence 级只补值不改合同。
- **A01 防回归测试（按审核加严）**：冻结 A01 已知参数清单（试剂用量、NaOH 分批、600 rpm、60 °C、360 min、8000 rpm/10 min、水洗、醇洗、60 °C/12 h 干燥），逐参数断言六元组：**value + unit + evidence_class + source reference + material association + macro-step association**——防止"数值还在但绑错 material/step"的假通过；变 unknown 即 pipeline regression，测试直接红。

### Phase 5 — handoff 对齐设备严格入口（固定流水线顺序）

```
scientific completeness(audit 通过)
→ operation canonicalization(operation_aliases/v1)
→ station/operation contract resolution(严格入口合同库)
→ handoff
```

- handoff 前新增"入口适配预检"：V2 包引用的工作站/操作名对照严格入口合同库（经 `operation_aliases/v1` 资源）；两缺失站保持 `external_contract_missing`，**禁止 fallback 到相似工作站**——scientific semantics missing ≠ platform contract missing，本轮彻底分开；容器/盖态语义与 SKILL 输入输出约束粗对齐。
- 与仓库内 workflow-checker 技能的关系：workflow-checker 复用 device_agent 检查引擎，本 Phase 只保证 handoff 产物的**命名与结构**对得上，不重写设备检查逻辑。

## 3.5 严格入口 zip diff 产物（本轮交付，不刷仓库）

生成 `strict_entry_zip_diff_20260926.json/.md`（zip vs 仓库 `chem_resources/lab-design-all/`)：分类 `added / removed / modified / semantically_equivalent / contract_relevant / contract_irrelevant`。**本轮不覆盖仓库副本**；是否 `chore: sync strict-entry lab resources` 待五个 Phase 完成后单独决定、单独提交。

## 3. 与 device 侧（R1–R3）的兼容承诺

- quantity 词汇：Research 侧新增 status 一律映射到 device 已有 `{known, unknown, pending_measurement}`，桥接点单点改动；
- `unknown → 0` 在 Research 侧同样禁止（与 device 红线一致）；
- device_agent 代码本轮**零改动**（除非桥接映射表需要补条目）。

## 4. 验收标准（对齐交接说明 §13，本轮可达部分）

- A01 重跑（LLM 可用时；不可用则启发式+冻结输入）：material lineage 可从 final sample 回溯到根批次；每个科学数字有 evidence_class；tacit 步骤展开后 identity/state/container/lineage 完整；runtime_pending 不填 0；unsupported 只报真缺口并附阶梯记录。
- 单测：unittest 全绿 + `chem_agent_contracts/test_v2.py` + device 套件零回归（基线 907 passed / 50 pre-existing failed）。
- 完整 A01→A02→B01/B02 重跑属"先完成这一层后"的下一阶段，本轮不承诺。

## 5. 明确不做（本轮）

- 不重建检索 ranking、不接新数据源；
- 不改 device_agent 检查引擎与 wire 逻辑；
- 不为缺失的两个工作站合同造任何协议内容；
- 不让 LLM 在无证据时生成参数（红线不变）。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| ProvenanceV2 加字段破坏严格校验/存量合同 | 字段可选+默认推导；存量 kind→evidence_class 迁移规则单测钉住 |
| 状态词表过严导致合法步骤被标 unknown | 词表首版从 A01/B01 实证状态归纳；未识别只标 unknown 不报错 |
| convention 规则误展开（把论文没写的相当成事实） | 规则只产状态/相/谱系且必须可溯源到 rule_id；展开结果过发布门 |
| audit 阻断误伤（runtime_pending 被当问题） | 阻断条件只有 unsupported+fidelity；测试覆盖"pending 不阻断" |

## 7. 工作量粗估与顺序

Phase 1（规则库+展开通道）→ Phase 2（状态图）→ Phase 3（completeness audit）→ Phase 4（证据阶梯+A01 测试）→ Phase 5（入口预检）。每个 Phase 独立提交、独立可验；Phase 1–3 是主链，4–5 可并行。

## 8. 需要用户审核决策的问题

1. **ProvenanceV2 扩展方式**：推荐"新增 evidence_class 字段、保留 kind"（兼容存量）；备选"直接改 kind 枚举"（干净但要迁移存量合同与测试）。——默认按推荐做。
2. **状态词表维护位置**：推荐 `chem_resources/material_states/` 独立版本化资源；备选内嵌 chem_agent_contracts。——默认按推荐做。
3. **是否用 zip 刷新仓库 `chem_resources/lab-design-all/`**：zip 与副本存在 audit/README/0410 差异（station 集合相同）。刷新属于"同步平台真源"，建议单独提交；**默认本轮不刷**，只把差异清单列出来给用户定。
4. **Phase 4 extraction 出处粒度**：做到"段落级"需要动 structured_outputs 抽取 prompt 与 schema（改动面大）；做到"论文+section 级"改动小。——建议先 section 级。
