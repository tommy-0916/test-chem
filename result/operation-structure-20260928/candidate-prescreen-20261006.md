# 新候选实验组适格性预筛 — 2026-10-05（2026-10-06 按验收复核纠正）

执行范围：按验收方指令筛查 ≤3 个尚未参与验收的新实验组，优先其他论文、不同材料体系。W1/W2（Wu-2025 SI S14/S15）继续作为摄入回归样本，不重复计入。本轮不调用模型、不扩规则、不改写原文、不补造步骤。预筛结论不是正式 DAG 通过。

**2026-10-06 纠正说明**（对应 Codex 复验意见）：①G1 原记录"操作短语 ✓（'filtered' 含 'filtrat'）"为错误判断——'filtrat' 不是 'filtered' 的子串，操作词同样不命中；且主动句 "We filtered…" 同样不命中，不能归因于被动语态，本文撤回该归因及"真实论文高比例使用被动语态"的频率判断（当前样本不支持）。②G2 结论收窄为"未找到所需的分离湿固体链"，不涉及该负载材料能否处于湿态的物理判断。③G3 改标为"SI 未取得，证据不足待核验"，DOI 更正为 10.1002/anie.202209350。④总体结论由"三组均不适格"改为分项状态。

规则基线：`chem_resources/chemistry_conventions/conventions.json`，sha256 `39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b`，冻结 11 规则。命中语义（workflow.py L3708–3754 实读）：operation_blob = 操作+operation；intent_blob = 操作+试剂/对象+参数+operation+parameters；命中 = 操作短语（casefolded 子串）+ 意图短语 + 已证输入态三者联合；正式证明检查源摘录。

候选来源池（本地持有全文 PDF）：Zhang-2017 NatCommun（MoNi4 HER）、K-ion（Wiley 2016 钾电 PBA）、PBA-LiS（Angew 高熵 PBA 硫宿主）、Operando（JACS NiFe PBA 电催化）。Wu-2025 其余组经核对即 W1/W2 本身（SI S14/S15 两句），不产生新候选。

---

## G1 — Operando NiFe PBA / NFBPBA 组（其他论文、其他材料体系）

- 来源身份：main article → **paper_explicit**。JACS，DOI 10.1021/jacs.8b05294；PDF：`reaserch_agent/chem_kb/Operando Spectroscopic Identification of Active Sites in NiFe Prussian Blue Analogues as Electrocatalysts- Activation of Oxygen Atoms for Oxygen Evolution Reaction.pdf`。
- 位置：正文 PDF 第 6 页，Experimental Section / Synthesis of Samples。
- 原句（自 PDF 逐字提取）："Then, the solution A was poured into solution B, and the mixture was aged at room temperature for 48 h to get the precipitate. After that, the as-obtained NaxNiFe(CN)6 nanocubes were filtered, washed with deionized water and dried overnight in vacuum oven at 100 °C."
- 前一句："…5.882 g Na3C6H5O7 (Aladdin，analytical reagent) and 1 g NiCl2·6H2O (Aladdin， analytical reagent) were added in another 250 ml water to obtain solution B."
- 后一句："As the reference sample, the crystalline Ni(OH)2 was synthesized by an aqueous method as previous report47."
- 实验组归属：NFBPBA（NaxNiFe(CN)6 nanocubes），该论文主材料。
- 按实际 matcher 的分项核查（子串判断已用 Python 复核）：
  - **操作词**：✗。"filtered" 不含 "filtrat"（f-i-l-t-e-r-e-d vs f-i-l-t-r-a-t）；'dry' 也不含于 "dried"。分离/干燥操作词均不命中。
  - **意图短语**：✗。FILTRATION_COLLECT_RETAINED_V1 白名单 'collect the solid'/'collect retained' 等在原文及任何等效改写中均不出现。
  - **初始状态**："aged … to get the precipitate" 只是得到沉淀的陈述，不能直接当作已证明的 suspension 初始态证据。
  - **材料连续性**："as-obtained" 回指与下游墨水段消费干粉是化学上合理的指示，但不能直接当作已证明的连续多跳链。
- 状态：**不适格**——操作词与意图短语双双不命中冻结表；初始态与连续性亦未证。模型把操作改写为 "drying" 等形式不能补救，正式证明检查的是源摘录。

## G2 — Zhang-2017 MoNi4 组（其他论文、其他材料体系）

- 来源身份：main article → **paper_explicit**。NatCommun 2017，DOI 10.1038/ncomms15437；PDF：`backend/data/reference_sources/B01/B01-Zhang-2017-NatCommun-main.pdf`（+ 同目录 SI）。
- 位置：正文 PDF 第 7 页，Material synthesis 段。
- 原句（自 PDF 逐字提取）："First, the commercial nickel foam was successively washed with ethanol, a 1 M HCl aqueous solution and deionized water. … Third, the autoclave was heated at 150 °C for 6 h in a drying oven. After washing with deionized water, the NiMoO4 cuboids were achieved on the nickel foam."
- 实验组归属：MoNi4 电催化剂（泡沫镍原位生长 NiMoO4 → H2/Ar 还原）。
- 核查：全文（主文 8 页 + SI 36 页）无 collect/filter/centrifuge 类分离操作（扩展词项清点：centrifug 0、filtrat 0、collect 0、precipitate 0、SI 仅 1 个 "dried"）；WASHING_V1 的输入态要求 retained_wet_solid/washed_wet_solid，文本中不存在产生该状态的链头。
- 状态：**本次目标链可排除**——准确结论是"未找到所需的分离湿固体链"，不包含该负载材料能否处于湿态的物理判断。

## G3 — 高熵 PBA 硫宿主组（其他论文、其他材料体系）

- 来源身份：main article → **paper_explicit**。Angew. Chem. Int. Ed. 2022，DOI **10.1002/anie.202209350**；PDF：`reaserch_agent/chem_kb/High-Entropy Prussian Blue Analogues and Their Oxide Family as Sulfur Hosts for Lithium-Sulfur Batteries.pdf`（9 页通讯）。
- 实验组归属：NiFe-PBA-S / HE-PBA-S 等四个硫宿主组；制备对象为二元至高熵 PBA 库。
- 关键原文（第 3 页，L185–190 提取文本）："[Fe(CN)6] is fixed at 2 mmol, and the total moles of added other metal salts remain unchanged at 2 mmol. **The detailed steps of the synthetic process can be found in the Supporting Information.**"
- 核查：主文 PDF 全文词汇清点——centrifug 0、filter 0、washed 0、dried 0、decant 0、supernatant 0；collect 2 次均为光谱数据采集；"filtrat" 唯一命中是 "infiltrated"（硫熔渗，误报）。主文明确把详细合成步骤指向 SI。
- 状态：**SI 未取得，证据不足待核验**——尚不能判定规则兼容或不兼容。下一步定向取得该 SI 后再核对。

## 词汇筛查即淘汰（未占三组名额）

- K-ion（Wiley 2016 钾电 PBA，PDF 在 chem_kb）：全部分离词项零命中（centrifug/wash/filtrat/precipitate/dried/decant/supernatant 均 0；collect 2 次为数据采集）。无链可查。

## 分项状态汇总

| 组 | 状态 | 首个确切缺口 |
|---|---|---|
| G1 Operando NFBPBA | 不适格 | 操作词不命中（'filtrat' ⊄ 'filtered'），意图白名单亦不命中 |
| G2 Zhang MoNi4 | 本次目标链排除 | 未找到分离湿固体链（文本层面） |
| G3 高熵 PBA | 证据不足待核验 | SI 未取得 |

不扩规则、不改写原文、不补造步骤。真实模型生成继续推迟。
