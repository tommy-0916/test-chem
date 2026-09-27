# RouteDecision V1：实验组级化学路线决策层

状态：阶段 2 离线适配器已实现。候选发现、原文核验、科学审计和设备预检已经组成可调用的确定性决策管线；B1/B2 工作流接入、可核验的 PDF/原始 SI 抽取、自动补证和 A01 黑盒回归仍未完成。没有运行 A01，也没有改动现有 Phase 1–5 发布门。

## 本次交付状态

| 部分 | 当前状态 |
| --- | --- |
| 候选、目标、决策合同与固定排序 | 已实现；离线测试使用合成实验组 |
| 结构化 RouteSignature 比较 | 已实现精确结构比较；同义词必须先由可信归一化统一 |
| 候选级设备能力预检 | 已复用现有能力索引与投影；只核抽象 capability ID、映射和明确可用状态 |
| 实验组候选发现 | 已实现离线通用发现；旧论文级摘要或不完整组保持 unresolved |
| 原文逐字段 SourceVerifier | 已实现本地 UTF-8 `.md/.txt` 实验组行号、原文摘要、证据摘录及所有图中论文引用的核验；PDF/JSON 和无可核验组位置的文本弃权 |
| 候选级 ScientificAudit 适配器 | 已复用现有 Phase 1–3、material provenance 与 quantity gate；图中每个数值须逐字段落入 evidence matrix |
| 可信适配器编排 | 已实现 `evaluate_route_decision_v1`；仅由 PaperRegistry 的已解析本地文本建立可信路径，独立回执汇合后调用确定性 selector |
| ResearchAgent 显式决策 API | `ResearchAgent.evaluate_route_decision_v1(state, goal)` 已可把完整决策和诊断持久化在 state；不会由此自动发布 macro plan |
| 定向补证 | 仅实现预算与无新增事实的停止判定；未接入检索或抽取 |
| B1/B2 自动决策接入与 A01 黑盒回归 | 未实现；未启动 A01 |

因此当前代码是**可审查、可独立调用的离线路线决策管线**，还不是生产流程中已自主选择化学路线的代理。合成夹具中可形成 `selected_for_planning`；实际知识库多为生成 JSON 摘要或 PDF，缺少该核验器要求的实验组行号、完整结构化候选和可独立核验的路线签名，不能据此宣称 A01 已具备选路条件。受控 KB 中出现一个 `.md/.txt` 文件及路线签名注释，只证明候选与该文件一致；当前管线尚未独立证明该文件是期刊发表的 primary paper/SI 或签名经过人工审定。`selected_for_planning` 也不会绕开原有 Research 发布门及 Device 硬门。

## 目标与边界

Chem-Agent 应从任务目标和可核验的论文实验组产生候选路线，依次审查证据、科学完整性和设备能力，形成可复查的 `RouteDecisionV1`，再让唯一选中的路线进入 macro action 和 macro plan。LLM 可以检索、抽取、提出假设和解释结果；最终准入与选择由确定性规则完成。

论文的 **experimental group** 是最小证据作用域。同一论文的不同合成组、处理组和性能测试组不得合并成一份配方。论文 ID 或相同目标产物都不足以授权参数转移。生产决策代码不得包含 A01、Huang、NiFe 等任务特判；这些名字仅可出现在测试夹具和回归测试中。

`stage_route` 表示研究阶段的顺序，并非化学合成路线。新的 `route_id`、`route_signature` 和 `selected_route_id` 必须使用独立字段。

## 现有流程与未来接入位置

当前 B1 在 `reaserch_agent/workflow.py` 中先做 survey、`_step_paper_protocol_extract`、`_attach_protocol_provenance`、survey report 和 stage design；随后 `_step_macro_plan_design` 调用 `_step_macro_action_design`，生成单条 action，再根据该 action 筛选证据，最后生成 macro plan。未来决策层应在 **protocol extraction 后、macro action 前** 消费实验组级候选，并把选中的路线传给 action 生成；不能继续让单次 action 生成先决定化学路线，再为它寻找支持。

B2 的 post-observation 规划也调用 `_step_macro_action_design`。未来接入须在它生成新 action 前重新检查真实 observation、失败约束和当前证据；若 B2 检索到了新文献，必须先对新材料做实验组级抽取和来源核验，不能直接复用旧候选。B2 的 `stage_route` 修复仍是阶段规划，与化学路线选择分开。当前 V2 device-adaptation 路线重写受到显式阻断，不得通过 RouteDecision 绕开。

预期数据流：

```text
任务目标及 route constraint
  → 检索原始论文 / SI → 按 experimental group 抽取
  → 来源核验 → RouteCandidateV1[]
  → 逐字段证据与科学规则审查
  → 有限额的缺口补证（必要时）
  → 设备能力预检
  → RouteDecisionV1
  → selected_for_planning 的唯一候选
  → macro action → macro plan
  → 现有 Phase 1–5 发布门 → Device hard gate
```

现有 `SearchHit` 和 `extracted_protocols` 仍以论文/步骤为主。抽取提示现在要求逐实验组拆分，归一化已保留实验组 ID、角色和步骤原文定位；但当前 live prompt 不产生完整 `RouteSignatureV1`、`EvidenceMatrix`、`MacroStepV2` 图和设备能力声明，因此 discovery 对普通 live output 会返回 `structured_route_field_missing`，不会凭摘要补齐。当前抽取最多覆盖有限的 top hits/protocols，不应将其视为完整候选全集。`MacroActionV2.experiment_group` 是计划执行的样品组，不能充当论文实验组 ID。

## 候选与决策合同

建议在 `chem_agent_contracts/` 增加 `route_candidate.py` 与 `route_decision.py`。合同负责类型、必需字段和一致性；来自 LLM 或外部数据的 `scientific_status`、`device_status` 仅是未核验声明，不能直接成为准入依据。

### `RouteCandidateV1`

每个候选至少保存：

| 部分 | 必需信息 |
| --- | --- |
| 身份 | `route_id`、目标材料/状态/任务、候选来源 |
| `source_scope` | `paper_id`、稳定的 `experimental_group_id`、论文或 SI 文件标识与哈希、章节/页码/段落 locator |
| `RouteSignatureV1` | 目标转化、前驱体角色、试剂角色、有序操作、控制模式、物相/状态转变、终点状态 |
| `EvidenceMatrix` | 每个必需字段的值、单位、用途、证据类别、原文摘录、来源实验组、核验状态和缺口原因 |
| 科学结构 | material state graph、lineage graph、需要的 chemistry convention rule ID |
| 设备需求 | 由操作和物料要求导出的能力、容器与控制/测量需求 |
| 审查结果 | 来源、科学、设备各自的校验回执及 reason code；未审查时为 `unknown` |

`RouteSignatureV1` 是结构化比较依据；`route_family` 仅是检索与粗筛标签。两个候选的目标产物或 family 相同，仍须逐项核对操作和控制方式。只有相同实验组范围内、与候选签名适用的字段才能绑定；跨组转用必须有单独的等价证据和明确的改编记录，否则拒绝。

`EvidenceMatrix` 使用 `required / non_required` 与 `supported / runtime_pending / unsupported / unknown`，不设可以掩盖必需缺口的总证据分数。`runtime_pending` 必须记录确定该值的测量路径，并由可信审计回执核对首次消费步骤；自由文本本身不能授权。`unsupported` 是审查结论，不是新的 provenance 或 `evidence_class` 值。历史 `agent_inferred`、fixture 和手工修订不能因后来发现数值相近的论文而自动升级为 `paper_explicit`。`required_fields` 也必须由可信任务/协议要求生成，不能直接相信 LLM 自报的短清单。

### `RouteDecisionV1`

决策记录保存 `decision_id`、完整目标和约束、候选快照与摘要、候选审查回执与摘要、各候选原因、`selected_route_id`（允许为空）、`selection_policy=route_decision_v1`、证据与设备预检快照摘要。选择状态区分 `selected_for_planning`、`unresolved`、`needs_route_choice`；`selected_for_planning` **不代表实验获准执行**。合同验证会检查决策内部一致性与摘要，但单纯持有一份 JSON 不能替代对当前原文和设备快照重新核验。证据或设备快照变化时旧决策失效。

## 可信适配器与现有规则的复用

1. **SourceVerifier**：核对受控本地文本的原始字节哈希、实验组边界、字段行号、证据包整段摘录和物料图所有论文引用。DOI、搜索摘要或论文级 `evidence_excerpt` 本身不足以证明一个数值属于某实验组。`ExperimentalGroupScope.source_digest` 是整篇源文件的 SHA256；`ProvenanceV2.source_digest` 继续按 V2 合同绑定 `EvidenceItemV2.excerpt` 的 canonical digest，两者不可混用。PDF/JSON 当前保持未核验。
2. **ScientificAudit**：把候选的步骤、物料和来源映射到既有 chemistry conventions、material state/lineage graph、provenance 校验及 Scientific Completeness Audit。convention 只可扩展其已授权的状态与谱系语义，不能产生新数值。图中数值如果没有同路径 evidence matrix 绑定，就保持 unresolved；明确错误的摘要、数值或 provenance 进入 gate issues。候选缺现有规则所需的上下文时返回未评估，不复制一套较宽松的平行科学规则。
3. **CapabilityPreflight**：当前候选级实现从现有 workstation 能力索引核验抽象 capability ID、经审核的操作映射与明确设备状态，并将限制一并纳入快照。容器、控制细节、规模及测量反馈仍需成型步骤和 Device 合同核验。现有 Research `strict_entry` 是基于 plan/package 的诊断层，不能单独证明候选路线完全可执行。没有权威能力回执时返回 `unknown`，缺少必需能力时返回 `blocked`；不能静默 fallback 或替换化学路线。

候选级审查只是选出可继续规划的路线。后续 macro plan 仍须经过现有 Phase 1–5 的完整发布门，Device 仍保留最终 hard gate。任何候选审查回执不得替代这些最终校验。

## 确定性准入与选择策略

先施加硬约束：目标不匹配、实验组混参、必需证据 unsupported、无解析路径的 runtime 数值、物料谱系断裂、必需设备能力缺失、缺少等价证据的科学保真风险，均不得进入排序。未核验或设备能力未知时保持 `unresolved`，不得当作通过。

多个可准入候选使用固定的字典序策略，依次比较：原始实验直接证据、必需字段完整性、是否无需路线改编、设备是否原生支持、保真风险数量、派生/缩放参数数量。不使用 LLM 自由打分，也不把软优势抵消硬缺口。仍无法区分且涉及不同化学路线时，返回 `needs_route_choice` 并保存并列原因。

支持三种约束：

| `route_constraint` | 行为 |
| --- | --- |
| `open` | 在全部合规候选中按固定策略自主选择 |
| `locked_family` | 仅在用户指定路线家族内选择；其他路线只能作为备选建议 |
| `locked_experimental_group` | 只核验用户指定的具体论文实验组及可执行性，不自动切换 |

锁定路线无法通过时返回 `unresolved` 和具体 blocker，不以另一路线静默替代。任何缩放、试剂替换或操作改编都需要独立的适用性与等价证据。

## 有限额补证状态机

后续在线接入采用 `discover → extract_groups → verify_sources → build_candidates → scientific_audit → targeted_search（如有可补缺口）→ capability_preflight → decide`。补证查询只能针对具体候选、具体必需字段和具体缺口，并记录查询、命中文献、实验组与新增已核验事实。

配置硬上限 `max_search_rounds`、`max_candidate_papers`、`max_queries_per_missing_field`。一轮没有新增 **verified fact** 即停止；相同 blocker signature 反复出现、原文/SI 不可得或检索服务故障时也停止并分别报告原因。不能靠无限重跑完整 A01 或扩大无关文献集使 unresolved 变成通过。服务失败与科学证据缺失分开记录。

## 分阶段交付与回归

**阶段 1：合同与离线确定性策略。** 新增通用候选/决策模型、规范化哈希、硬拒绝与字典序策略的纯函数，以及使用合成实验组的离线测试。测试覆盖跨组字段拒绝、缺失必需字段、已核验的 runtime 解析路径、无权威设备回执时弃权、设备能力缺失、三种路线锁定、排序与并列、快照变化后失效，以及旧 `agent_inferred` 不升级。测试中的核验回执为合成 fixture；它们验证策略如何消费回执，尚未验证原文和完整科学门的真实接线。阶段 1 不修改生产工作流，不宣称系统已经自主选路，也不运行 A01。

**阶段 2：离线可信适配器。** 已实现实验组发现、受控本地文本核验、复用现有科学 gate 的候选审计、设备能力预检和 receipt 编排；归一化已保留组别和定位。离线测试包含跨组、篡改原文、摘录伪造、图中漏列数值与缺失来源。生产 B1/B2 仍未调用此管线。

**后续接入：** 先给原始论文/SI 建立可独立核验的页/段定位和实验组级结构化抽取，再让 B1/B2 在 macro action 前调用决策；把唯一选中 candidate 的原文、证据快照与 material graph 绑定到后续计划，最终仍过现有 Research V2 发布门和 Device hard gate。自动补证限额接入后再做 A01 黑盒端到端验收。

A01/Huang 仅作黑盒回归：尿素及 PBA 路线不得向共沉淀路线输参数；同论文 control、后处理/蚀刻及 OER 测试实验组不得混参；旧冻结值不能追认论文出处；同路线 primary-paper 实验组可独立成候选；缺 pH-feedback 等必需能力时由 capability layer 阻断；没有合法路线时返回 `unresolved`，不猜配方。当前仓库有 frozen A01 包与合成测试记录，但尚无可供直接核验的 Huang 原文实验组 fixture；补齐原始来源与定位后才能将它用于实证回归。
