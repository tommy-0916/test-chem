# Research Introduction

> 本文从 `chem-technical-report.md` 中抽取研究引言部分。
> 当前论文标题统一为：
> **TuringBrain: Observation-Gated Macro-Action Planning and Workflow Evolution for Autonomous Chemical Experimentation**

## 0. Research Idea：Observation-Gated Macro-Action Planning and Workflow Evolution for Autonomous Chemical Experimentation

> 本章是整个设计文档的 **idea 核心**。它独立于后续的工程实现细节，以 Motivation → 现有方法的不足 → Challenge → 核心 Idea → Novelty → Contribution 的逻辑组织。后续所有章节（术语、原则、架构、状态机、数据结构）都是为了实现本章提出的 idea。

### 0.1 Motivation：自主化学实验室的规划粒度困境

自主化学实验室正在从概念验证走向真实部署。Coscientist（Nature 2023, 794 citations）展示了 LLM 驱动的闭环反应优化；ChemCrow（Nature Machine Intelligence 2024, 809 citations）用 18 个专家工具增强了 LLM 的化学推理能力；A-Lab（Nature 2023）在 17 天内自主执行了 353 次固态合成实验。这些系统证明了一件事：**LLM-agent 驱动的自主化学实验是可行的。**

但它们同时暴露了一个共同的架构瓶颈：**规划粒度错了。**

A-Lab 在 17 天 353 次实验中，37% 的实验没有命中目标产物。这不是因为它的候选筛选算法差，它使用了 DFT + GNoME + active learning，而是因为 **一旦一次合成实验启动，系统就无法根据中间结果修正后续步骤**。每次合成是一个不可分割的整体：要么全部执行完再看结果，要么从头来过。中间步骤出了问题，温度偏移、中间产物异常、前驱体不纯，系统完全不知道，直到最终的 XRD 结果告诉它"这次失败了"。

这不是 A-Lab 独有的问题。Coscientist 的模式是"LLM 生成完整实验代码 -> 一次性执行 -> 看结果"；ChemCrow 虽然有 thought-action-observation 循环，但每个 action 只是一次工具调用，缺乏跨多步的时间连贯性；ChemAgents（JACS 2025）通过 protocol library 引入了可复用的实验模板，但 protocol 的边界是静态预定义的，不会根据运行时观测动态调整。

**核心矛盾可以概括为一句话：现有系统要么太粗（全流程盲跑），要么太细（逐步短视决策），没有一个系统在这两个极端之间建立了一个有原则的中间规划粒度。**

这个矛盾在简单的单步反应中不明显。但随着自主实验室从单步反应向多步合成战役（multi-stage synthesis campaigns）扩展，例如一个涉及 10-20 个工作站、跨越前驱体制备 -> 反应 -> 表征 -> 优化的完整材料发现流程，规划粒度的问题就变成了系统能否 scale 的核心瓶颈。

### 0.2 现有方法为什么不够

我们对 8 个代表性系统进行了系统调研（Coscientist, ChemCrow, A-Lab, ORGANA, ChemAgents, CLAIRify, BioMARS, ARChemist），发现它们的规划粒度可以分为三类，每一类都有根本性的不足：

**类型 A：全流程规划（Workflow-at-a-time）**

代表系统：Coscientist, A-Lab。系统先生成一个完整的实验方案，然后一次性交给执行端。执行完成后才分析结果。

根本缺陷：**不可中途修正**。如果第 3 步的中间产物已经偏离预期，系统仍然会执行第 4-10 步。A-Lab 37% 的失败率中，有相当比例可以归因于此。

更深层的问题：全流程规划暗含一个假设，"初始计划足够好，不需要中途调整"。这对简单反应或许成立，但对多步合成战役来说是不现实的。

**类型 B：逐动作规划（Action-by-action）**

代表系统：ChemCrow（tool-call 循环）。LLM 在每一步决策后执行一个工具调用，观察结果，再决定下一步。

根本缺陷：**缺乏时间连贯性**。每一步都是独立的 tool-call 决策，不存在"接下来连续执行 3 个步骤、在第 3 步后做一次全面评估"的概念。这导致决策碎片化和观测过载。如果每一步都触发完整的分析-规划循环，系统的总开销在 10+ 步流程中变得不可接受。

**类型 C：静态 Protocol Library**

代表系统：ChemAgents（JACS 2025）, CLAIRify, ARChemist。系统维护一个预定义的 protocol/模板库，在执行时从库中选择并实例化。

根本缺陷：**Protocol 边界是设计时确定的，不随运行时状态变化**。系统不能根据运行时观测动态决定"当前 protocol 应该提前终止"或"在 protocol 内部的某个点增加一次观测"。

与 macro-action 的关键区别：Protocol library 是 **静态模板选择**，macro-action 是 **动态在线规划**。Protocol 的边界由开发者预设，macro-action 的边界由 Agent Team 在每个 decision point 根据观测结果实时决定。

### 0.3 Technical Challenges

在全流程盲跑和逐步短视之间建立一个有原则的中间规划粒度，面临三个具体的技术挑战。每一个都对应一个设计决策。

**Challenge 1：何时停下来看一眼？——Observation-as-Decision 问题**

在多步化学实验中，并非所有工作站都能返回可分析的结果。哪些工作站属于 `result-capable workstation`，以及它们能返回哪些结果类型，应由对应的 `workstation` 描述与能力约束定义。其他工作站则可能只负责执行操作而不回传结构化结果。

这意味着"何时观测"不是一个自由选择，它受限于设备拓扑。但在受限范围内，**选择在哪个 result-capable workstation 发起观测，是一个非平凡的规划决策**：观测太频繁退化为逐步决策、观测太稀疏则问题累积到不可逆、观测位置不当则在信息价值低的位置浪费分析资源而在关键节点错过修正窗口。

**没有任何现有系统将 "是否在此处观测" 形式化为一个显式的规划决策。** Coscientist 和 A-Lab 在流程结束时默认观测；ChemCrow 在每个 tool call 后都获取输出；ChemAgents 的 protocol 中观测位置是预设的。

**Challenge 2：科学推理 vs 设备适配——职责混淆问题**

一个化学家在决定下一步时，思考的是"我应该验证哪个假设"。一个实验室技术员在执行时，思考的是"这个工作站能不能做这件事、参数范围是多少"。

**这两种推理在现有系统中被混在一起。** Coscientist 的 LLM 同时生成科学计划和 Opentrons 代码；ChemCrow 的 LLM 在同一个循环中既做化学推理又选择工具。这导致科学推理被设备约束隐式污染，且设备适配缺乏专注的搜索空间。

ChemAgents 部分缓解了这个问题（角色分离），但它的 Experiment Designer 仍然同时承担"科学方向选择"和"实验方案设计"两个职责。

更关键的是，ChemCrow 的研究发现 GPT-4 **无法区分自身的正确输出和错误输出**。这意味着研究层（科学推理的核心）如果没有外部验证机制（grounding），一个被幻觉的假设就会沿着 设备适应 -> 执行 -> 记忆 的管线传播，并被记忆层永久记录为项目知识，形成 **错误放大闭环**。

**Challenge 3：长期实验知识的组织与召回——Context Flooding 问题**

内容多+信息稀疏

一个多步合成战役可能跨越 20+ 个 macro-action、持续数天、产生大量中间结果和路线变更。研究层在每个 decision point 都需要理解"我们走过了什么路、尝试过什么方向、哪些假设被推翻了"。


**如果把所有历史信息平铺塞进 LLM 上下文，会导致 context flooding**：token 数量线性增长很快超出窗口；大量无关信息稀释 LLM 对当前决策真正相关信息的注意力；时间维度与因果维度在平铺日志中无法被有效区分。

G-Memory（NeurIPS 2025）用三层图解决了类似问题（+20.89% action success），Mem-T 用层级检索获得了 +14.92% F1 和 ~24.45% 的 inference token 减少。**但这些方案都没有针对化学实验的特殊结构（project -> query -> stage -> macro-action 的天然层级）做定制化设计。**

### 0.4 核心 Idea：Observation-Gated Macro-Action Planning and Workflow Evolution

我们提出 **observation-gated macro-action planning**：以 **macro-action**，即两个相邻 observation-gated decision point 之间的一段连续执行，作为自主化学实验室的核心在线规划单位。

一句话定义：macro-action 是一段由多个 workstation 组成的连续执行序列，它在一个显式的 observation request 触发的 decision point 处终止，系统在此处分析实验结果、更新假设、规划下一段执行。

在系统层面，这意味着 `workflow` 不再是一次性静态生成的脚本，而是围绕已执行前缀、在每个 `decision point` 后持续演化的未来执行骨架。换句话说，`macro-action planning` 决定局部怎么走，`workflow evolution` 决定整条未来路径如何随观测结果被重写。

这个概念受到强化学习中 temporal abstraction / options framework（Sutton et al., 1999; Bacon et al., 2017）的启发，但做了关键的领域适配：

| 维度 | RL Options | TuringBrain Macro-Action |
|------|-----------|--------------------------|
| 终止条件 | 学习到的终止函数 β(s) | 显式规划的 observation request + 安全网 |
| 内部策略 | 可微分的 intra-option policy | 设备适应层确定性生成的 workstation 序列 |
| 反馈频率 | 每步都有 reward | 仅在 decision point 获得 observation |
| episode 数 | 大量 episode | 极少次实验，不可逆 |
| 粒度来源 | 端到端学习 | 规划时根据不确定性动态确定 |

**这不是简单地 "把 workflow 切成几段"。** 核心创新在于三个相互耦合的机制：

**机制 1：Observation Request 作为一等规划原语**

我们将 "是否在此处观测" 提升为与 "执行哪个 workstation" 同等重要的规划决策。每个 `macro_action_plan` 必须显式回答三个问题：(1) 接下来连续执行哪些 workstation？(2) 在哪个 result-capable workstation 发起 observation request？(3) 在什么条件下提前终止（安全网触发）？

这直接回应 Challenge 1。为防止过于激进的"不观测"决策，系统设有两层安全网：最大盲执行长度（连续 N 个 workstation 无观测时强制观测）和异常触发强制观测（任何 workstation 报告执行异常时强制观测）。

**机制 2：研究层与设备适应层的显式解耦 + 快速回退 + 科学推理 Grounding**

我们将 "为什么做"（科学推理）和 "怎么做"（设备适配）分为两个独立层：研究层只输出 `ScientificDirective`（科学目标、假设、成功判据），设备适应层只输出 `MacroActionPlan`（可执行的 workstation 序列）。

这直接回应 Challenge 2。与 ChemAgents 等系统的简单角色分离不同，我们增加了三个关键机制：

- **快速回退通道**：当设备适应层连续 N 次找不到可行方案时，通过 `FeasibilityFeedback` 直接告诉研究层 "这个方向在当前设备上做不到"。研究层维护一个 `feasibility_prior`（基于历史成功率），避免反复生成不可行方向。
- **evidence_source 标注**：研究层的每个科学判断标注证据来源（experiment_result / tool_calculation / literature / llm_inference），使下游层可以区分有证据的判断和纯 LLM 推理。
- **confidence_level**：研究层对每条指令输出置信度（high / medium / low）。设备适应层对 low 置信度指令发出 `ClarificationRequest` 要求补充证据，而非直接翻译执行。

**机制 3：与规划粒度对齐的层级记忆 + 分层召回**

我们将实验知识组织为一棵与规划粒度天然对齐的树：`project -> query -> stage -> macro-action`。上层节点保存摘要和 meta_info，下层节点保存具体执行细节。**规划粒度与记忆粒度的一致性是有意为之的**：研究层在每个 decision point 需要的上下文，正好对应记忆树中当前 stage 路径下最近若干 macro-action 的信息。

这直接回应 Challenge 3。具体的召回策略是三层分层召回：(1) **路径摘要层**（总是读取）：当前路径上所有祖先节点的 meta_info；(2) **局部前序层**（按规则触发）：当前 stage 下最近 K=3 个已完成的 macro-action 细节；(3) **跨路径检索层**（按信号触发）：当出现异常、安全风险或观测偏差时，跨路径检索相似案例。每次召回附带 freshness 标签，过时记忆降权。

### 0.5 Novelty：与现有系统的精确差异

**Novelty 1：Observation-gated decision point 作为形式化规划原语。**

调研覆盖 8+ 系统，无一采用此设计。这不是 "把 workflow 切成段" 的工程优化，它引入了一个新的抽象层：observation request 是一个显式的、有成本的规划决策，decision point 是系统重新获得合法判断权的唯一入口。最接近的竞品 ChemAgents 的 protocol library 功能上类似 macro-action，但它的 protocol 边界是设计时预设的，不支持运行时动态调整，也没有 "是否在此处观测" 的显式决策语义。

**Novelty 2：研究/设备适应解耦 + 快速回退通道 + 置信度标注。**

单纯的角色分离不新（ChemAgents 已有）。创新在于：(1) 快速回退通道使设备适应层的设备约束能 bypass 完整闭环直接影响研究层；(2) evidence_source 标注使下游层可以区分有证据的判断和纯 LLM 推理，直接回应 ChemCrow 发现的 LLM 自验证失败问题；(3) feasibility_prior 避免研究层反复探索死胡同。

**Novelty 3：规划粒度与记忆粒度的结构对齐。**

现有层级记忆系统（G-Memory, TiMem, MemTree）针对通用 QA 或具身任务设计。我们首次将层级记忆的树结构与实验规划的天然粒度（macro-action）对齐：记忆写入、摘要、召回的粒度都以 macro-action 为基本单位。

### 0.6 Contributions

1. **我们形式化了 observation-gated macro-action 作为自主实验室的在线规划原语。** 我们引入了 observation request（显式观测决策）和 decision point（观测后合法判断边界）作为规划粒度的基本构件。这提供了一个介于全流程盲跑和逐步决策之间的有原则的中间方案。调研覆盖 8+ 现有系统，无一提供此抽象。

2. **我们设计了一个五层架构，通过类型化的层间契约实现关注点分离，并支持可追溯的 workflow evolution。** Runtime / 研究层 / 设备适应层 / 执行层 / 记忆层通过 `ScientificDirective`、`MacroActionPlan`、`ObservationEvent`、`MemoryContext` 等类型化对象通信。研究层与设备适应层之间增设快速回退通道（`FeasibilityFeedback`），研究层对每个科学判断标注证据来源与置信度，并通过 `ReplanTicket`、`workflow_version` 与 patch strategy 在保留已验证前缀的前提下持续演化未来 workflow，避免 LLM 幻觉在闭环中被永久记忆。

3. **我们提出了与规划粒度结构对齐的层级记忆系统。** project -> query -> stage -> macro-action 的树结构使记忆的写入/摘要/召回粒度与规划粒度一致。三层分层召回策略替代全量扫描，受 G-Memory（NeurIPS 2025, +20.89% action success）和 TiMem 的 recall planner 启发。

4. **[待验证] 我们通过多步合成基准实验表明，macro-action 级规划在实验效率上优于全流程规划和逐步规划。**

### 0.7 本章与后续章节的关系

本章定义了 **idea 的核心逻辑**。后续章节是实现这个 idea 的工程文档：

- **第 1 章**（术语定义）：将本章概念形式化为精确的术语体系
- **第 2 章**（任务目标）：将本章的四层分工展开为各层的详细输入输出规约
- **第 3 章**（设计原则）：将本章隐含的设计约束提炼为不可违背的系统边界
- **第 4-8 章**（实现设计）：状态机、上下文装配、数据结构、版本管理、Patch 策略

阅读建议：如果只想理解核心创新，读本章。如果要评估设计是否 solid，读本章 + 第 3 章。如果要实现，读全文。
