# G3 候选制备链 v2（更正版）· 2026-10-06 · 基线见报告 commit

更正声明：v1（g3-candidate-chain-round-20261006.md §3）的六行是**摘要标签**，
不是完整操作序列。v2 按 SI 原文展开为逐操作链；跨组引用**单列为关系**，
不再伪装成操作。本表仍是手工核验标签，不冒充模型生成或正式入口输出。
来源：Du 2022 SI（sha256 9f05be9f…，39 页），解析器 route_pdf_groups/v4，
12 组零诊断；下列 locator 均为 v4 枚举实测范围。

## A. HE-PBA 制备链（目标材料 CoNiCuMnZnFe-PBA）

源 protocol 组：`Synthesis of binary PBAs`（pdf:p3:b22-p3:b29）。
引用组：`Synthesis of high-entropy PBA`（pdf:p3:b40-p3:b42）。

| 步 | 操作 | 原句 locator | 参数（原文） | 输出 / 保留对象 | 物料连接 |
| --- | --- | --- | --- | --- | --- |
| A1 | dissolution | p3:b23-24 | 2 mmol Co(NO3)2·6H2O + 2.25 mmol Na3C6H5O7·2H2O，50 mL 去离子水 | clear solution A | 盐 → 溶液 A |
| A2 | dissolution | p3:b24 | 2 mmol K3[Fe(CN)6]，50 mL 去离子水 | clear solution B | 盐 → 溶液 B |
| A3 | pouring（B→A） | p3:b24-25 | 室温、搅拌中直接倾倒 | A+B 混合液 | 溶液 A+B → 混合液 |
| A4 | stirring | p3:b25 | 持续搅拌 10 min | 混合液 | 同上 |
| A5 | aging | p3:b25-26 | 室温陈化 24 h | 陈化后混合物 | 同上 |
| A6 | centrifugation-collect | p3:b26 | — | "the product"（湿固体，**状态待证明**） | 混合物 → 湿固体 |
| A7 | washing | p3:b26-27 | 去离子水、乙醇，分别反复洗涤 | 洗后产物（wet/dry 未交代，**待证明**） | 湿固体 → 洗后产物 |

**关系 R1（跨组方法引用，不是操作）**：`Synthesis of high-entropy PBA`
组声明 "The synthesis steps of high-entropy PBA are similar with that of
binary PBAs, except that five metal salts replace one metal salt."（p3:b41），
并给出五盐等比 0.4 mmol:0.4:0.4:0.4:0.4（Co/Ni/Cu/Mn/Zn，p3:b41-42）。
即 HE-PBA 链 = A1–A7 模板 + 盐组成替换。引用继承未被
protocol-reference/v1 支持（模块契约：单组 scope），R1 仅作关系记录。

## B. 硫复合物分支（目标材料 HE-PBA-S）

模板组：`Synthesis of NiFe-PBA-S, NiCuFe-PBA-S, CoNiCuFe-PBA-S, and
HE-PBA-S`（pdf:p4:b2-p4:b6）。

| 步 | 操作 | 原句 locator | 参数（原文） | 输出 | 物料连接 |
| --- | --- | --- | --- | --- | --- |
| B1 | grinding | p4:b4 | 43 mg NiFe-PBA 粉 + 100 mg 升华硫，研磨 30 min | 细混合物 | HE-PBA* + S → 混合物 |
| B2 | sealing/placement | p4:b5 | 置入 50 mL 密封 Teflon 容器（autoclave） | 密封混合物 | 同上 |
| B3 | melt-diffusion heating | p4:b3-5 | 升温至 130 °C，保温 12 h | PBA-S 复合物 | 混合物 → M-PBA-S |

\* B1–B3 模板主语是 NiFe-PBA；对 HE-PBA-S 的物料来源是 R2。

**关系 R2（同组内跨材料同法引用，不是操作）**："Moreover, NiCuFe-PBA-S,
CoNiCuFe-PBA-S, and HE-PBA-S were also prepared via the same synthetic
procedure."（p4:b6）。HE-PBA-S = B1–B3 模板施于 HE-PBA。

**模板参数继承一致性（本轮更正点）**：43 mg/100 mg（B1）与 130 °C/12 h
（B3）同出 NiFe-PBA-S 模板句组（p4:b3-b5）。二者对 HE-PBA-S 的继承地位
必须一致：都只经 R2 引用继承，且该继承**未被证明**；不允许无依据地
禁止前者却直接继承后者。v2 中两者同标「模板局部参数，继承待证」。

## C. 逐组引用验收（按实际实验组 scope，v4 枚举实测）

| 引用 | 内容 | 验收 scope | 结果 |
| --- | --- | --- | --- |
| S1 | "The product was collected by centrifugation and washed repeatedly with deionized water and ethanol, respectively." | binary PBAs 组 | ✓ pdf:p3:b26-p3:b27 |
| S1 | 同上 | HE-PBA 组 | ✗ `fact_excerpt_not_in_block`（跨组引用问题，正文块匹配成功**不能**替代） |
| S3（补全） | "…similar with that of binary PBAs, **except that five metal salts replace one metal salt**." | HE-PBA 组 | ✓ pdf:p3:b41-p3:b41 |
| S3b | 五盐 0.4 mmol 等比句 | HE-PBA 组 | ✓ pdf:p3:b41-p3:b42 |
| S4（补全） | "…were annealed at **500 °C (2 °C min-1) in air for 2 h**, for obtaining…" | cubic metal oxides 组 | ✓ pdf:p3:b44-p3:b45 |
| S5（补全） | 研磨 30 min + 50 mL 密封 Teflon + 130 °C/12 h 全句组 | PBA-S 组 | ✓ pdf:p4:b4-p4:b5 |
| S5 | 同上 | binary PBAs 组 | ✗ `fact_excerpt_not_in_block` |
| S6 | "…prepared via the same synthetic procedure." | PBA-S 组 | ✓ pdf:p4:b6-p4:b6 |

## D. 规则/证明边界状态（与 v1 一致，未松动）

- A6 保留对象识别：命名动词 "designated" 不在 `_NAMING_PATTERN`；
  "product" ≠ precipitate/pellet，CENTRIFUGE_COLLECT_PRECIPITATE_V1
  intent 两路径均不通过 → 湿固体状态**待证明**。
- A7 WASHING_V1 操作/意图命中，但输入态依赖 A6 输出（待证明）。
- R1/R2 跨组引用：protocol-reference/v1 单组 scope，不跨组；不扩 v1。
- B 分支研磨/熔融扩散、退火（A 分支氧化物方向）无冻结状态推导规则
  → 骨架可提取，状态不推导（≠ 不能映射）。
- 固定约束：3D 双通过；3E 仅诊断、feeds_verdict=false、零 token；
  A01 ms7a.out 及级联保持 BLOCKED。
