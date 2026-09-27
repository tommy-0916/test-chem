# RouteDecision V1：实验组级化学路线决策层

状态：路线决策合同、受信 PDF 实验组枚举、逐组模型提案、签名来源验真和独立签名路线审阅回执均已接入保守决策门。原文核验支持可提取文本的 PDF 页/块定位，逐事实编译器可处理显式实验组事实。B1/B2 可显式启用决策门，但选中路线尚不能无损转为可发布的计划。有预算补证的状态机已实现，实际检索与抽取回路尚未接入。没有运行 A01，也没有改动现有 Phase 1–5 发布门。

## 本次交付状态

| 部分 | 当前状态 |
| --- | --- |
| 候选、目标、决策合同与固定排序 | 已实现；离线测试使用合成实验组 |
| 结构化 RouteSignature 比较 | 已实现精确结构比较；同义词必须先由可信归一化统一 |
| 候选级设备能力预检 | 已复用现有能力索引与投影；只核抽象 capability ID、映射和明确可用状态 |
| 实验组候选发现 | 已实现离线通用发现；旧论文级摘要或不完整组保持 unresolved |
| 原文逐字段 SourceVerifier | 已实现本地 UTF-8 `.md/.txt` 行号与可提取文本 `.pdf` 页/块的实验组、原文摘要和证据摘录核验；扫描 PDF、JSON 与无清晰组边界的文本弃权；PDF 不生成已验证路线签名 |
| 实验组逐事实编译 | 已实现 `route_facts` 到证据包/矩阵的保守编译；数值及关键路线、物料声明须各有同组原文摘录；不是独立文献语义认证 |
| 候选级 ScientificAudit 适配器 | 已复用现有 Phase 1–3、material provenance 与 quantity gate；图中每个数值须逐字段落入 evidence matrix |
| 可信适配器编排 | 已实现 `evaluate_route_decision_v1`；生产 `ResearchAgent` 只索引有外部受信事件、受控来源回执及 PaperRegistry 原始 PDF 关联的文件，再汇合独立回执调用确定性 selector |
| 原始来源身份 | 已实现 PDF 字节哈希、受控回执、外部受信事件和 registry 关联的交叉核验；尚无可信事件签发者，因此当前生产索引为空 |
| PDF 实验组枚举 | 已实现清晰标题下的逐组页/块范围和原字节摘要；模糊边界或重复组名弃权；尚未接入 live 化学事实抽取 |
| ResearchAgent 显式决策 API | `ResearchAgent.evaluate_route_decision_v1(state, goal)` 已可把完整决策和诊断持久化在 state；不会由此自动发布 macro plan |
| 定向补证 | 已实现逐字段预算回路，仅将来源核验且科学审计过的事实计为新增；实际检索与抽取回调尚未接入 B1/B2 |
| B1/B2 决策门 | `event.constraints.route_decision_goal_v1` 可显式启用；新 action 之前核验，未决或选中但尚未无损绑定计划时停止发布；旧流程没有自动启用 |
| Typed 路线动作绑定草稿 | `build_route_action_binding_draft_v1` 在既有 intent 预检后保存独立、重新校验的 Stage/Action、选中候选原样步骤图、当前证据包及决策/候选/设备快照身份；仅供审查，未接发布门 |
| 独立路线签名审阅 | 已接入经 Ed25519 签名的逐实验组审阅回执；与当前原 PDF、来源审计摘要、组定位、DOI、目标和候选路线结构逐项绑定。缺审阅或不匹配时保持 unresolved；尚无审阅签发服务或生产审阅件 |
| Typed Research 包草稿 | 已能把当次可信决策、当前证据、显式 Stage/Action 和选中路线图无损投影为可重新验证的 V2 包草稿；不含原始步骤/观测摘要，不能作为 Device handoff 发布 |
| A01 黑盒回归 | 未启动 |

因此当前代码已有**可审查的路线决策门**，还不是能够自动完成目标到计划的生产代理。合成夹具中可形成 `selected_for_planning`；实际知识库多为生成 JSON 摘要，目前没有已登记、具独立身份回执、可供这条管线直接核验的 PDF 原文实验组和完整结构化候选，不能据此宣称 A01 已具备选路条件。低层离线测试使用 `.md/.txt` 文件及路线签名注释，只证明候选与测试文件一致；生产入口不将它们当作 primary paper/SI。`selected_for_planning` 也不会绕开原有 Research 发布门及 Device 硬门。

## 目标与边界

Chem-Agent 应从任务目标和可核验的论文实验组产生候选路线，依次审查证据、科学完整性和设备能力，形成可复查的 `RouteDecisionV1`，再让唯一选中的路线进入 macro action 和 macro plan。LLM 可以检索、抽取、提出假设和解释结果；最终准入与选择由确定性规则完成。

论文的 **experimental group** 是最小证据作用域。同一论文的不同合成组、处理组和性能测试组不得合并成一份配方。论文 ID 或相同目标产物都不足以授权参数转移。生产决策代码不得包含 A01、Huang、NiFe 等任务特判；这些名字仅可出现在测试夹具和回归测试中。

`stage_route` 表示研究阶段的顺序，并非化学合成路线。新的 `route_id`、`route_signature` 和 `selected_route_id` 必须使用独立字段。

## 现有流程与未来接入位置

当前 B1 在 `reaserch_agent/workflow.py` 中先做 survey、`_step_paper_protocol_extract`、`_attach_protocol_provenance`。显式启用 `route_decision_goal_v1` 时，它在 **protocol extraction 后、macro action 前** 调用路线决策；未选中时停止。即使已经选中，当前也停在 `selected_unbound`，因为旧 V2 raw-plan adapter 会重新解析参数并丢失候选图的逐字段 provenance。未经无损绑定不能进入 macro plan 发布。未启用时原有 action→证据→plan 路径仍运行。

B2 的 post-observation 规划也调用 `_step_macro_action_design`。显式启用的路线目标会跨 B2 保留，并在任何新 action 前重新核验；未能重新核验或未能无损绑定时清空本轮待发布包并停止。若 B2 检索到了新文献，仍需先做实验组级抽取和来源核验。B2 的 `stage_route` 修复仍是阶段规划，与化学路线选择分开。当前 V2 device-adaptation 路线重写受到显式阻断，不得通过 RouteDecision 绕开。

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

现有 `SearchHit` 和 `extracted_protocols` 仍以论文/步骤为主。抽取提示现在要求逐实验组拆分，归一化已保留实验组 ID、角色和步骤原文定位；但当前 live prompt 不产生完整 `RouteSignatureV1`、`EvidenceMatrix`、`MacroStepV2` 图和设备能力声明，因此 discovery 对普通 live output 会返回 `structured_route_field_missing`，不会凭摘要补齐。可提取文本的受信 PDF 现在可枚举明确的实验组；进入决策管线的每个已知组都须在抽取结果中以相同论文 ID、组 ID 和文件摘要出现，否则决策返回 `attested_group_not_extracted`，组边界模糊也会阻断。实验组的合成/表征/测试角色还须与独立可信映射一致；模型把合成组自报为 characterization 不能让该组消失。当前抽取最多覆盖有限的 top hits/protocols，不应将其视为完整候选全集。`MacroActionV2.experiment_group` 是计划执行的样品组，不能充当论文实验组 ID。

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

1. **SourceVerifier**：核对受控本地文本的原始字节哈希、实验组边界、字段行号、证据包整段摘录和物料图所有论文引用；PDF 采用原始文件哈希与 `pdf:p1:b2-p2:b4` 页/文本块定位。文本中即使已有机器可读路线签名，签名每个关键字段仍须有同组原文逐项引文才能成为已验证签名。DOI、搜索摘要或论文级 `evidence_excerpt` 本身不足以证明一个数值属于某实验组。`ExperimentalGroupScope.source_digest` 是整篇源文件的 SHA256；`ProvenanceV2.source_digest` 继续按 V2 合同绑定 `EvidenceItemV2.excerpt` 的 canonical digest，两者不可混用。PDF 核验只证明本地 PDF 中的组和摘录，未认证出版者身份或路线签名；扫描 PDF/JSON 仍保持未核验。
2. **ScientificAudit**：把候选的步骤、物料和来源映射到既有 chemistry conventions、material state/lineage graph、provenance 校验及 Scientific Completeness Audit。convention 只可扩展其已授权的状态与谱系语义，不能产生新数值。图中数值如果没有同路径 evidence matrix 绑定，就保持 unresolved；明确错误的摘要、数值或 provenance 进入 gate issues。候选缺现有规则所需的上下文时返回未评估，不复制一套较宽松的平行科学规则。
3. **CapabilityPreflight**：当前候选级实现从现有 workstation 能力索引核验抽象 capability ID、经审核的操作映射与明确设备状态，并将限制一并纳入快照。生产还要求另一个可信编译结果逐实验组确认**完整**的 required capability 列表；模型自报的短列表即使全部可用也只能得到 `unknown`。容器、控制细节、规模及测量反馈仍需成型步骤和 Device 合同核验。现有 Research `strict_entry` 是基于 plan/package 的诊断层，不能单独证明候选路线完全可执行。没有权威能力回执时返回 `unknown`，缺少必需能力时返回 `blocked`；不能静默 fallback 或替换化学路线。

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

**阶段 3 当前进度：** PDF 页/块核验、实验组逐事实编译、有预算的补证回路和 B1/B2 显式阻断门已实现。该门保证路线未决时不会继续生成新 action；选中仍停在 `selected_unbound`，避免旧 raw-plan adapter 丢逐字段 provenance。完整 RouteSignature 仍需独立核验，普通 live 提取还不能输出完整结构化候选。

**阶段 4 当前进度：** 来源身份回执模型、原字节/DOI 交叉核验、PDF 实验组枚举、漏抽组/角色核验和候选能力完整性核验已实现。逐组提案关联器可把模型的逐块摘录与受信枚举组对齐；可信表征/测试组保持在清单中，但不会因没有合成路线事实而误阻断其余合成组。模型无法写入来源身份、组角色或设备能力。离线 Ed25519 验签器已实现，部署公钥必须同时绑定允许的签发者；公开决策、来源索引和 PDF 组枚举入口均要求已签名事件，旧的未签名事件对象会被拒绝，缺事件时报告 `trusted_source_event_missing`。独立签发服务、部署公钥配置、完整实验组事实抽取、独立组角色与能力编译、路线签名核验以及无损 V2 发布仍未实现。受信事件集合须由当前目标的有限额检索确定；把整个知识库都送入决策会令无关实验组阻断选路。下载器已保留请求、跳转、终跳、内容类型和原字节哈希，但仍没有出版者身份依据，不能由 `verified_doi` 自动签发受信事件。

**阶段 5 当前进度：** `RouteActionIntentV1` 对显式提供的阶段、动作、当前证据包和已选路线做离线绑定预检，逐项核对决策及候选摘要、设备快照、样品与步骤 ID、操作顺序、论文来源和逐字段 provenance，包括每个论文数值参数与其摘录的对应关系；数字字符串不能绕过数值核验。`build_route_action_binding_draft_v1` 在相同预检后生成 `RouteActionBindingDraftV1`：深拷贝并重新校验显式 Stage/Action、选中候选的原样 `MacroStepV2` 图、当前 `EvidenceBundleV2`，以及 decision/candidate/evidence/device 快照身份。读取保存草稿时须调用 `validate_route_action_binding_draft_v1`，用当次可信决策和证据重新绑定并比对整份草稿；单独解析草稿或检查其自带摘要不能作为授权。它只是一份 typed 绑定草稿；不构造 `ResearchActionPackageV2`，未接入 Research 发布门或 Device 重建门，不能因草稿成立就运行设备。

**阶段 6 当前进度：** opt-in 路线决策入口现在从已签名 PDF 清单重新枚举实验组，按固定的组数、文本块和输入/输出长度预算向模型请求逐组提案，再由已有的原文块关联器核对完整覆盖、组 ID、文件摘要和逐条摘录。模型调用只接收受控 PDF 组清单和固定系统指令，不注入旧 workflow 摘要、`extracted_protocols`、memory 或工具上下文；预算覆盖实际发送的两段提示。缺独立审阅的组角色或完整设备能力映射时，模型不会被调用；异常、超预算或任何组提案错误均不返回部分协议。路线专用协议提案与旧 `extracted_protocols` 分离，B1/B2 的路线门不再把知识库摘要提取结果当作 PDF 实验组事实。诊断保存到 `route_group_proposal_diagnostics_v1`，新一轮失败会清除上一轮模型提案；协议提案仍须经过 compiler、原文核验、科学和设备审计。当前 PDF 核验器不会从模型提案或普通散文中追认完整 RouteSignature；`source_route_signature=None` 仍会阻断路线选择。

**阶段 7 当前进度：** `reviewed_route_signature_manifest_v1` 定义了外部审阅者对完整 RouteSignature 的逐组声明，`signed_reviewed_route_signature_manifest_v1` 将其与签发者和独立部署公钥做 Ed25519 验签。签名载荷明确列出操作顺序、角色、控制模式、状态转变和全部目标字段；缺字段、别名归一化、改动签名字段、错误密钥或签发者均拒绝。生产管线从当次原 PDF 枚举结果、已验签来源审计和任务目标构造期望作用域，不从模型候选或审阅件复制期望值。每份已验签的来源事件必须进入受信索引，每份受信来源必须成功枚举；被静默丢弃的 PDF、审计文件或 registry 关联会阻断整个候选集。只有原文逐字段核验通过、所有已知组的覆盖与角色审计完整、候选与目标一致、审阅作用域和当前 PDF 字节/实验组定位完全一致且只有一份有效审阅时，审阅路线签名和审阅摘要才进入 `RouteValidationReceiptV1`。相同审阅字节重复输入会去重，不同有效审阅或混入无效审阅会弃权。候选的路线家族与审阅家族也须一致。审阅者对化学语义的判断仍需独立完成；验签只能证明声明来自被配置的审阅者，不能证明声明本身化学正确。当前没有签发服务、生产审阅件或审阅公钥配置，默认仍保持 unresolved。`decision_id` 和 `evidence_snapshot_hash` 覆盖决策记录内的审阅摘要；单独保存的 validation diagnostics 不包含在这两个哈希中。

**阶段 8 当前进度：** `build_route_research_package_draft_v2` 要求显式 campaign ID，并用本次可信决策和当前证据重新校验绑定草稿；它保留 Stage/Action、参数、物料图和逐字段 provenance，验证 JSON 往返后的 Research V2 包哈希。它不制造原始步骤或观测摘要，因此结果仍是不可发布草稿。原生 V2 adapter 已能直接保留显式结构化参数，不再从展示文本重解析这些值。Device CLI 仍会从保存的 raw plan 重建 Research 包；当前 adapter 对 material contract migration 等字段的投影与 typed 草稿不一致，Research 的旧发布门也要求 raw 步骤字段。必须让两条入口按同一 typed 图逐字段重建，并通过原有 Research 科学发布门和 Device raw/canonical 一致性门，才能将 `selected_unbound` 改为可发布。

**后续接入：** 建立独立审阅的 RouteSignature 签发流程与普通论文术语的受控语义映射，不靠提案字段和原文中偶然出现的词判定操作顺序或控制模式；把唯一选中 candidate 的原文、证据快照与 material graph **无损**绑定到 macro action/plan，经过现有 Research V2 发布门和 Device hard gate。将预算回路接上真实检索与抽取后再做 A01 黑盒端到端验收。

### 真实闭环尚需的两个合同

**原始来源身份。** PaperRegistry 的 `verified_doi` 目前只表示检索记录带 DOI，`local_file` 也不能单独证明文件是相应论文/SI。下载后的 PDF 存在 registry 的 `pdf_files`，JSON 摘要存在 `corpus_files`。内部离线测试实现仍可读取本地 MD/TXT/PDF fixture；公开路线决策入口没有关闭来源验签的参数。生产 `ResearchAgent.evaluate_route_decision_v1` 只接受原 PDF，并同时要求：由部署配置提供与签发者绑定的公钥并通过 Ed25519 验签的事件、受控 KB 中的 `SourceDocumentAttestationV1`、PDF 原字节 SHA256、PaperRegistry 的 PDF 关联及 DOI/父 DOI 全部匹配。受信文件哈希与候选哈希持续比对，证据条目的非空 DOI 须与受信 DOI 一致；来源身份及回执摘要进入决策记录。证据条目的标题和 URL 仍是提案文本，不视作已核验身份。回执中的 URL 与身份依据内容须由独立签发方核验；本模块不联网复核，也没有自行生成可信事件的服务。当前 CLI 尚未配置公钥或事件，且运行环境缺 `cryptography` 时验签明确拒绝；无有效事件时生产来源索引为空。当前知识库登记记录没有可直接用于路线核验的原始实验组，不能仅凭现有 JSON 摘要自动产生 `paper_explicit`。

部署签名验证时安装 `reaserch_agent/requirements-route-source.txt`。每个公钥配置项需含原始 Ed25519 公钥字节和唯一允许的 `issuer`；裸公钥字节会被拒绝。公钥映射与签名事件只能由部署配置注入 `ResearchAgent` 构造器；不能放入任务约束、模型输出或可写知识库。缺依赖、缺公钥、签发者不符或验签失败均保留未决状态。

后续可信签发者应保存 DOI 登记记录和出版者页面的原始响应摘要，从该页面明确指向的正文或 SI 链接获取 PDF，并保留请求、逐跳重定向、终跳、内容类型和原字节哈希。正文需核对 DOI 与标题；SI 需核对其链接来自对应正文的出版者页面。签发者在独立环境中审核这些事实并签名，ResearchAgent 使用部署公钥验签。签名只认证事件由持有私钥的签发者发出，不替代签发者的来源身份审核。当前纯 `TrustedAcquisitionEventV1` 对象会被生产管线拒绝，不能从用户输入、模型输出或 KB 文件反序列化后直接当作受信事件。

**选中路线到 V2 计划。** `RouteCandidateV1` 没有 `StageV2` 和 `MacroActionV2` 必需的观测点、完成判据、执行样品组等字段；从目标材料推这些字段会造出实验意图。现有 raw-plan adapter 会重新解析参数文本并丢逐字段 provenance，Device 入口还会从 raw plan 重建包并比较哈希。当前 typed 草稿只接受外部显式提供且通过预检的阶段/动作，不重解析 raw 参数或补造缺失字段。`selected_for_planning` 在生产工作流中仍保持 `selected_unbound`，直到共用 Research 发布门的无损 typed adapter 与 Device 重建核对完成。

A01/Huang 仅作黑盒回归：尿素及 PBA 路线不得向共沉淀路线输参数；同论文 control、后处理/蚀刻及 OER 测试实验组不得混参；旧冻结值不能追认论文出处；同路线 primary-paper 实验组可独立成候选；缺 pH-feedback 等必需能力时由 capability layer 阻断；没有合法路线时返回 `unresolved`，不猜配方。当前仓库有 frozen A01 包与合成测试记录，但尚无可供直接核验的 Huang 原文实验组 fixture；补齐原始来源与定位后才能将它用于实证回归。
