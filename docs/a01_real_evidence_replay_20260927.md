# A01 真实来源审计与离线路线 replay（2026-09-27）

> 后续纠偏：本文“v5 启动判据”记录的是当时尚未运行的判断，不再作为本地验证的启动门槛。本地 v5 可用真实任务和原文启动一次 `--forward-only` 流程，让生产器与现有门禁给出首个具体阻断；无需预先取得所有化学审阅 PASS 或真实 303 task id。真机执行仍单独授权。后续运行记录见 `a01_v5_local_run_20260927.md`。

## 范围与结论

本次仅核查 A01 历史配方的出处、一个可取得的原始论文实验组，以及现有路线 evaluator 对 A01 v4 保存协议的诊断。不运行 A01、Research 发布、Device preflight 或设备执行；不修改 `f37a431` 的路线绑定与发布门。

**当前结论：A01 旧冻结配方没有可继承的同路线原文证据，离线路线决策为 `unresolved`。** 不能把旧配方中的 `agent_inferred` 数值改写为 `paper_explicit`。Huang 2023 的 NiFe Control 是原文中的真实实验组，但它采用不同的比例、碳酸盐、pH 控制、加料与老化方式；若采用，属于显式改选实验路线。

## 历史出处

`reaserch_agent/fixtures/a01_frozen_package.json` 的 `stage.objective` 记录首轮没有知识库协议命中，固定参数由 agent 补全。包内结构化参数的 provenance 为 `agent_inferred`。该历史方案包括 Ni:Fe = 4:1、硝酸镍/硝酸铁与 NaOH、分批加碱、60 °C 搅拌熟化 360 min、8000 rpm 离心 10 min 和 60 °C 干燥 12 h。它是待验证的历史计划，不是已核实的论文实验组。

2026-09-26 的 `campaigns/A01-scisem4-20260926/iteration_00/research_state.json` 收到四个本地 reference 输入：一份 2018 NiFe-LDH 尿素/TEA 回流路线 JSON 和三份 PBA 路线 JSON。其三个 `extracted_protocols` 都是旧的论文级扁平结构，没有 `experimental_groups`、逐组 `route_facts` 或可供路线发现器核查的原始 PDF。四个 reference 的加入没有给历史 NaOH 路线提供逐组证据。

## Huang 2023 原文核查

来源：[Huang 等，*ACS Applied Materials & Interfaces* 2023，DOI 10.1021/acsami.3c11651](https://doi.org/10.1021/acsami.3c11651)；实际读取的是 [Lawrence Berkeley National Laboratory / eScholarship 保存的论文 PDF](https://escholarship.org/content/qt64g5s3gx/qt64g5s3gx.pdf)。机构封面载有相同标题、期刊和 DOI；[PubMed Central 原文记录](https://pmc.ncbi.nlm.nih.gov/articles/PMC10685352/)提供交叉核对。下载的 PDF 为 7,448,586 字节，SHA-256 `b37decb52e102297fd6852422723aaf77e18fa033dbbec43b1892c443426ceb9`。以下为 PDF 第 3 页 `METHODS` 下 **“Synthesis of the Pristine Ni3Fe LDHs (NiFe Control)”** 这一组的审计摘录；同页酸蚀及酸蚀再结晶组不纳入。

| 字段 | NiFe Control 组原文支持的内容 | 与旧 A01 冻结方案的关系 |
| --- | --- | --- |
| 前驱体和比例 | Ni(NO₃)₂·6H₂O 37.5 mmol、Fe(NO₃)₃·9H₂O 12.5 mmol；Ni:Fe = 3:1 | 旧方案为 4:1 |
| 初始溶液 | 两种金属盐溶于 50 mL 水；另配 NaOH 100 mmol/100 mL 水及 Na₂CO₃ 6.25 mmol/50 mL 水 | 旧方案没有碳酸盐投料 |
| 加料与控制 | 金属盐液以 115 mL/h 注入碳酸钠液，800 rpm 搅拌；监测 pH 并手动滴加 NaOH 保持 pH 10 | 旧方案为分批加碱，未定义 pH 反馈 |
| 反应与后处理 | 反应 1 h；离心/水重分散三次，种子于室温、100 rpm 老化 20 h，最后再洗一次 | 旧方案为 60 °C 熟化 6 h，离心/洗涤与干燥参数不同 |

这些值是对 PDF 页面的人工核查记录，**尚未取得 Chem-Agent 的受信身份事件、逐组路线审阅签名或 `paper_explicit` 准入资格**。不得从该组借参数填补旧 A01 的 NaOH-only 方案。2018 [Scientific Reports 原文](https://doi.org/10.1038/s41598-018-22630-0)是尿素/TEA、100 °C 回流 48 h；[RSC 2015](https://doi.org/10.1039/C5RA05558J)采用胶体磨与独立陈化；[RSC 2019](https://doi.org/10.1039/C8SE00394G)涉及闪速纳米沉淀/钨酸根。它们也不能按“都是 NiFe-LDH”视为旧路线的数值来源。

NiFe Control 只覆盖一个合成实验组；A01 任务还要求不同 Fe 引入方式、Ni 基及物理混合对照和后续表征/电化学比较。该组不能单独授权整套 A01 样品矩阵或终点测试。

### PDF 机器定位障碍

对上述机构 PDF 调用当前 `enumerate_pdf_experimental_groups`，结果为 **0 组**，诊断 `experimental_section_missing`。视觉上 Methods 和组标题存在；PyMuPDF 按列聚合后，`METHODS` 混入第 3 页正文块，NiFe Control 的粗体标题与正文同在另一文本块。现有枚举器只接受独立的粗体标题块，因而保守弃权。这是当前 PDF 版面解析限制，不代表原文没有实验组；不能手造一个“已枚举”的 PDF locator 或伪造通过回执。出版方 SI 主要为图表，没有替代主文这一合成组的方法段落。

## A01 保存协议离线 replay

新增 `reaserch_agent/offline_route_replay.py` 只调用生产路线 evaluator、现有科学审计及候选级抽象能力预检，不调用 LLM、Research 发布或 Device。审计输入来自上面的 A01 保存 state：复制三个 `extracted_protocols`；`goal.json` 用原始 A01 查询作 objective，并把目标材料暂记为 `NiFe-based catalyst`、状态记为 `unknown`。这个目标投影只用于诊断，不是经用户确认的 RouteGoal。

```powershell
.venv\Scripts\python.exe -m reaserch_agent.offline_route_replay `
  --goal-json result/a01-evidence-audit-20260927/goal.json `
  --protocols-json result/a01-evidence-audit-20260927/protocols.json `
  --knowledge-base-dir reaserch_agent/chem_kb `
  --output-json result/a01-evidence-audit-20260927/replay_report.json
```

结果为 `decision_status=unresolved`、`selected_route_id=null`。此诊断目标的 `required_fields` 暂为空，因此即使后续补入候选，也不能把这份审计输入当作可发布路线任务；正式 replay 必须从完整任务要求建立必需字段。分层原因：

| 层 | reason code | 含义 |
| --- | --- | --- |
| 输入/来源 | `signed_source_events_missing`、`signed_route_signature_reviews_missing`、`trusted_source_event_missing` | 尚无独立签名的 PDF 身份及逐组路线审阅 |
| 候选发现 | `experimental_group_missing` | 三份保存协议没有实验组结构，不能编译为候选 |
| 决策 | `candidate_discovery_incomplete` | 候选全集不可证，不能选择路线 |

没有候选进入逐字段验证，因此本次没有产生 `evidence`、`material`、`runtime` 或 `fidelity` 的逐候选最终结论；不能把这些层记作 PASS。候选级能力检查也未发生。现有候选级 capability preflight 位于 RouteDecision 选择前；Research 发布后的 Device preflight 是另一道更具体的门。当前两者均没有足够输入进入。

## 继续条件

### 同路线原文的有界补查

在旧配方的硝酸镍/硝酸铁、Ni:Fe = 4:1、NaOH-only 分批加料、60 °C 熟化 6 h、8000 rpm/10 min 离心和 60 °C/12 h 干燥这一组联合条件下，本次有界查找未找到可以逐字段支撑它的 primary paper/SI。最接近的 [Zhao 等，*Materials* 2025 §2.2.1](https://doi.org/10.3390/ma18040911)虽有 Ni:Fe = 4:1 和 NaOH 滴加，却使用 NiCl₂/FeCl₃、pH 9–10、反应 30 min、陈化 24 h、60 °C 干燥 48 h，研究终点还是甲基橙去除；不能替旧 A01 的 OER 样品矩阵和不同参数背书。[Görlin 等，*Chemical Communications* 2019](https://doi.org/10.1039/C8CC06410E)研究 Ni/Fe 氧羟化物的物理混合 OER 对照，但其制备路线也不能补足旧方案的共沉淀数值。相似字段不得跨论文、跨实验组拼成一个 `paper_explicit` 配方。

这不是对所有文献的穷尽性否定；它说明**当前可审计来源集**没有旧配方所需的同路线证据。若后来找到原始论文/SI，应从原文字节、具体实验组和字段 locator 重新核验。旧冻结包的 `agent_inferred` 字段仍为 unsupported，不能自动升级。

### 开放任务与历史配方的边界

A01 原始 query 没有锁定 NaOH、Ni:Fe = 4:1 或 60 °C/6 h；旧配方是 agent 当时生成的一个方案。因此“复现历史冻结配方”和“满足原始开放任务”是两个不同的验证目标。对后者，路线决策层可以在具备合法来源与完整任务约束时自行选择一个合规实验组；若多条路线并列，仍要保留 `needs_route_choice`。但 A01 原始任务要求不同 Fe 引入/配位、Ni 基与物理混合样品及多项 OER/结构终点。当前 `RouteGoalV1`/`RouteDecisionV1` 选择一个候选，action binding 也限定单一 `sample_id`；尚没有对整套多臂样品矩阵的来源覆盖判定。不能把任何一个 NiFe Control 实验组的单 action publish 当成整个 A01 已就绪，也不能把新选路线回填到旧冻结配方后声称其被原文支持。

有界查找找到两套与**原始开放目标**更贴近的真实实验矩阵，均尚未进入受信 KB、独立审阅或 Research gate：

| Primary source | 已报告的实验组和可比内容 | 对 A01 的明确缺口 |
| --- | --- | --- |
| [Görlin 等，*Chemical Communications*，DOI 10.1039/C8CC06410E](https://doi.org/10.1039/C8CC06410E)，[机构主文及 SI](https://refubium.fu-berlin.de/handle/fub188/25756) | Ni(OH)₂、FeOOH、共合成 Ni–Fe、Ni+Fe 物理混合；SI p. S2 为合成/混合方法，主文比较 OER、Tafel、形貌和原位 XAS/UV-vis。 | 使用 190 °C 溶剂热等不同制备条件；未核实 XRD、XPS、ECSA、EIS、长期稳定性，303 能力未知。 |
| [Sondermann 等，*Molecules* 2025，DOI 10.3390/molecules30020208](https://doi.org/10.3390/molecules30020208)，[作者机构 PDF](https://www.ac1.hhu.de/fileadmin/redaktion/Fakultaeten/Mathematisch-Naturwissenschaftliche_Fakultaet/Chemie/AnorganischeChemie/AnorganischeChemie_I/Publikations-Dateien_ab_743/744.pdf) | Ni/Fe MOF 系列，另有 Ni(OH)₂+FeOOH 物理混合对照；原文分别报告 PXRD、SEM-EDX、OER、Tafel、EIS 和稳定性。 | MOF 与氢氧化物是不同组；不能把 MOF 组的 EIS/稳定性移给物理混合组。未见 XPS/ECSA，整体也不能直接覆盖全部 A01 终点。 |

这些文献提供**候选组**，不是已核验可发布的 A01 Evidence Bundle。任何跨论文实验组合都必须把各样品臂及各终点的来源分别审计；原文未报告的终点可设计为新测量，但不能伪称原文已有该结果。

### v5 启动判据

用户已授权在准备完成后自动运行 A01 v5。启动前必须逐项核实：受控 PDF 与独立签名来源事件、逐实验组签名审阅、覆盖原始多臂任务的 RouteGoal/Action intents、路线选择与逐字段 evidence、Research V2 保存重建和 publish gate、Device canonical V2 与能力预检均通过。若某项缺失，记录该项 reason code 并停在对应门；新选实验组不得静默改写旧冻结配方的来源。当前三份旧 `extracted_protocols` 没有 experimental-group 候选，尚无独立签名来源与审阅回执，也没有多臂覆盖证明，因此 v5 **未启动**。

当前 `run_campaign.py` 的 `--forward-only` 最多验证到 Device 规划/下发检查；现有 `real` execution adapter 不提供实际 303 任务创建。因此即使后续科学与设备门通过，也须把“本地 v5 通过”与“303 已接收并返回任务 id”分开记录。

1. 若保留旧 A01 路线，找到与其前驱体、比例、有序操作、加料控制模式和终点一致的 primary paper/SI 实验组；不能从近似路线拼参数。
2. 若为原始开放任务采用 Huang NiFe Control 作为其中一个样品臂，先对机构 PDF 的来源身份和实验组路线作独立审阅并签发机器可验回执；PDF 枚举器还需通用、保守地识别这种双栏内嵌标题格式。之后重新建立 RouteGoal、逐组 facts、物料图和 Stage/Action intent，补齐其他对照臂的证据及覆盖判定，并核实 pH 反馈等设备能力。
3. 只有路线 evaluator 选中、Research 完整性审计和发布门通过，才运行 Device hard preflight；两者都通过后才考虑 A01 v5。
