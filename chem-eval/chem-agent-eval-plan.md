# How to Eval TuringBrain / Chem Agent

## 1. 评测目标

这套评测不是只回答“最后实验有没有成功”，而是要系统检验下面 5 个核心 claim：

1. `observation-gated macro-action planning` 比两端极端方案更优：
   - 优于 `workflow-at-a-time` 的一次性盲跑
   - 优于 `action-by-action` 的逐步短视重规划
2. 显式 `observation request` 能更好地平衡信息价值、观测成本和纠错时延。
3. `Research Layer` 与 `Device Adaptation Layer` 解耦，并加入 `FeasibilityFeedback`，能减少不可执行闭环和无效搜索。
4. `workflow evolution + patch strategy` 能在保留已验证前缀的同时，更快从异常或观测偏差中恢复。
5. 与 `macro-action` 对齐的层级记忆，能在长时、多轮、多分支 campaign 中提升表现并降低上下文成本。

因此，推荐把评测拆成 4 个层级，而不是只做单一 benchmark：

1. 组件级评测：研究层、设备适配层、安全层、记忆层分别评
2. 闭环仿真评测：在 replay/simulator 中评动态控制质量
3. 端到端 campaign 评测：评真实任务目标达成与 sample efficiency
4. 限量硬件在环评测：验证 simulator 中的收益能否迁移到真实设备

## 2. 核心研究问题

建议把实验部分明确写成下面 6 个 research questions：

### RQ1. Macro-action 粒度是否优于静态 workflow 和逐步重规划？

关注：

- 目标达成率
- 达标所需实验数
- 达标所需 wall-clock
- 安全事件数

### RQ2. Observation gating 是否真正学会了“该在哪里看一眼”？

关注：

- 观测次数
- 每次观测带来的收益
- 强制观测触发率
- 观测过密/过疏带来的性能变化

### RQ3. 研究层/设备适配层解耦是否提升了可执行性？

关注：

- `MacroActionPlan` 可执行率
- 设备不匹配导致的失败率
- `FeasibilityFeedback` 触发后的恢复成功率

### RQ4. Workflow evolution 是否能最小代价地修正未来而不破坏过去？

关注：

- prefix preservation
- patch minimality
- replan latency
- anomaly recovery success

### RQ5. 层级记忆是否在长时任务中带来稳定收益？

关注：

- 长时任务成功率
- 检索命中率
- token 消耗
- stale memory 污染率

### RQ6. 安全门是否能降低危险输出，同时不过度伤害效用？

关注：

- unsafe completion rate
- safety recall / false negative
- over-block rate
- utility drop

## 3. 总体评测框架

建议采用“三阶段主评测 + 一阶段真实验证”：

### Phase A. Offline / Component Evaluation

目标：低成本、高覆盖、快速迭代。

主要评：

- 化学推理与文献 grounding
- 协议/动作抽取与 workstation 映射
- 安全判断
- 记忆检索

### Phase B. Replay-Based Closed-Loop Evaluation

目标：在不接真实实验平台的情况下评动态闭环控制。

做法：

- 用历史 reaction/protocol 数据构造 replay 环境
- Agent 在预算内选择 `macro-action`
- 环境返回结构化 observation/anomaly/safety feedback
- 统计 campaign-level 指标

### Phase C. Simulator-Based Optimization Evaluation

目标：评 sample efficiency、regret、多目标优化能力。

做法：

- 使用 `Summit`、`Olympus` 等反应优化 benchmark
- 在标准实验预算下比较不同 agent/baseline

### Phase D. Hardware-in-the-Loop Validation

目标：验证 simulator 收益能否迁移到真实实验设置。

建议只选 3 类代表任务：

1. 简单样品配制/配液
2. 单步反应条件优化
3. 含分支决策的两阶段 workflow

## 4. Baselines

baseline 不能只比“别的模型”，必须比“别的架构”。建议至少包含下面 6 类。

### B1. Static Workflow Baseline

对应你文档中的 `workflow-at-a-time`。

实现方式：

- 初始时一次性生成完整 workflow
- 执行过程中不允许基于中间 observation 重写后续流程
- 只在终点或安全硬门触发时停止

它主要对应 `Coscientist` / `A-Lab` 风格的对照思路。

### B2. Stepwise ReAct Baseline

对应 `action-by-action`。

实现方式：

- 每个 workstation 后都执行 `observe -> analyze -> plan`
- 不允许形成多步连续 `macro-action`

它主要对应 `ChemCrow` 风格的逐工具调用循环。

### B3. Static Protocol Library Baseline

对应“静态 protocol library / template retrieval”。

实现方式：

- 从 protocol 库中检索最相似模板
- 填参数后直接执行
- protocol 边界固定，不随运行时 observation 动态变化

这个 baseline 很重要，因为它最接近“工程上看起来也很强”的替代方案。

### B4. No-Workflow-Evolution Baseline

保留 `macro-action`，但禁用 `workflow versioning` 和 `patch strategy`。

实现方式：

- 每轮只重生“下一段”
- 不维护 lineage
- 不支持 `suffix_replan` / `full_regenerate`

这个 baseline 用来单独验证 `workflow evolution` 的贡献。

### B5. No-Hierarchical-Memory / Flat-Memory Baselines

至少做两个：

- `No-Memory`：除当前 observation 外不读取历史
- `Flat-Memory`：把最近 N 轮日志平铺塞入上下文，不做层级摘要和触发式召回

它们用来验证你“规划粒度与记忆粒度对齐”的 claim。

### B6. Classical Optimization Baselines

在反应优化 benchmark 上，必须加入经典 experiment planning baseline，而不只比 LLM agent：

- Random
- Latin Hypercube Sampling
- Full Factorial
- Nelder-Mead
- SNOBFIT
- SOBO
- TSEMO
- MTBO

这些策略在 `Summit` 中已有实现。

### 可选上界

- `Human Expert`
- `Oracle Observation Policy`
- `Oracle Device Adapter`

如果人力允许，建议在小规模子集上加入人工化学家对照。

## 5. Benchmarks 与 Datasets

这里要区分：

- `benchmark`：评测任务/套件
- `dataset`：benchmark 背后的数据源

由于现有文献已经指出，计算化学/化学工作流 agent 目前还没有统一的 agentic benchmark，建议采用“公共 benchmark + 自建 replay benchmark”的混合方案。

## 5.1 Track A: 化学推理与知识 grounding

### Benchmark A1. ChemBench (Nature Chemistry 2025)

用途：

- 评研究层的化学知识、推理、计算、直觉
- 适合评 `ScientificDirective` 的上游质量

推荐原因：

- 有大规模题库
- 有人类专家对照
- 有 mini 子集，方便日常回归

建议评测项：

- overall accuracy
- topic-wise accuracy
- difficulty-wise accuracy
- calibration / overconfidence

### Benchmark A2. ChemLLMBench

用途：

- 评结构化化学任务，而非纯问答
- 可以覆盖反应预测、反应条件、性质预测、分子命名等

推荐原因：

- 官方仓库已公开多个经典数据集入口
- 更适合测“化学任务能力”，不是泛化常识

建议优先使用其中与 agent 最相关的子任务：

- reaction prediction
- reaction condition / yield related tasks
- property prediction

## 5.2 Track B: Protocol / Action / Workstation 映射

这个 track 专门评 `ScientificDirective -> MacroActionPlan` 的中间链路。

### Benchmark B1. ULSA

用途：

- 评从合成段落到结构化 action ontology 的映射能力
- 适合作为 `workstation_sequence` 生成前的中间 benchmark

数据特点：

- 3040 条由领域专家标注的 ceramics synthesis procedures

### Benchmark B2. Solution-Based Inorganic Synthesis Dataset

用途：

- 评多步无机/材料合成中的操作、条件、顺序恢复
- 适合构造多 workstation 的 replay episode

数据特点：

- 20,037 条 hydrothermal 反应
- 15,638 条 precipitation 反应
- 每条记录包含 precursor、target、operations、conditions

### Benchmark B3. Procedure-to-Action Extraction

用途：

- 评有机实验 procedure 到 action sequence 的转译能力

推荐做法：

- 如果你有可用授权，可加入基于 Pistachio 的 industrial track
- 如果没有授权，就把它作为可选补充，不作为主 benchmark

## 5.3 Track C: 闭环反应优化与 sample efficiency

这个 track 是你论文主实验最重要的一组，因为它最能验证 `macro-action planning` 的价值。

### Benchmark C1. Summit

建议至少使用：

- `SnAr Benchmark`
- `Reizman Suzuki Emulator`
- `Cross-Coupling Emulator Benchmarks`

为什么重要：

- 是标准 closed-loop reaction optimization benchmark
- 支持 mechanistic benchmark 与 data-driven emulator
- 可以直接测样本效率和多目标优化

### Benchmark C2. Olympus

用途：

- 补充不同噪声水平、不同 planner、不同实验数据驱动 emulator
- 检查 agent 在 noisy optimization 下的鲁棒性

推荐使用方式：

- 作为 Track C 的补充 benchmark
- 重点测噪声、预算变化、planner robustness

### Benchmark C3. ORD Replay Benchmark

这是你最该自建的一项 benchmark。

做法：

1. 以 [Open Reaction Database](https://pubs.acs.org/doi/10.1021/jacs.1c09820) 为主数据源
2. 把反应记录转成 replayable episode
3. episode 中只暴露当前允许观测到的信息
4. agent 选择下一段 `macro-action`
5. 环境返回 outcome / observation / anomaly / safety signal

它的价值在于：

- 比纯 QA 更接近真实闭环
- 比真实机器人更便宜和可复现
- 可以直接测试 observation gating、replanning、memory

## 5.4 Track D: 长时记忆与多轮 campaign

现有公开 benchmark 里，这部分最缺。

建议自建一个 `ChemAgent-LongHorizonBench`：

### 数据来源

- ORD episode
- ULSA / solution-based synthesis 数据
- 你自己的 simulator 日志
- 历史内部实验日志

### 构造原则

每个 episode 至少包含：

- 8-20 个 `macro-action`
- 至少 2 次路线切换机会
- 至少 1 次 anomaly 或 observation surprise
- 至少 1 个需要跨 stage 检索历史经验的点

### 任务类型

- `anomaly recovery`
- `similar-case recall`
- `route switch after surprise`
- `stale memory filtering`
- `cross-stage planning`

## 5.5 Track E: 安全

### Benchmark E1. ChemSafetyBench

用途：

- 化学属性查询
- 化学用途合法性
- 合成方法描述安全性

非常适合测：

- research layer 输出是否会生成危险建议
- safety gate 是否能阻断危险路线

### Benchmark E2. LabSafety Bench

用途：

- 实验室场景安全
- 危害识别
- 后果预测
- PPE / waste / equipment / emergency response

非常适合测：

- 执行层前的 safety review
- 场景化安全判断，而不仅是文字拒答

## 5.6 Track F: 文献驱动科学判断

如果你想把“论文 API + 文献 grounding”作为亮点单独打出来，建议加一个补充 track。

### 可选 Benchmark F1. ChemPaperBench

用途：

- 文献 grounding
- 多跳化学推理
- 多模态论文上下文理解

建议定位：

- 作为 supplementary benchmark
- 不作为主结果表唯一证据

原因：

- 这个方向和你的 research layer 非常契合
- 但它更偏 literature-grounded QA，不足以替代闭环实验 benchmark

## 6. Metrics

指标必须分层报告，不能只报一个最终 success rate。

## 6.1 端到端结果指标

### M1. Goal Achievement Rate

定义：

- 达到用户目标指标的 episode 占比

适用：

- 所有端到端 benchmark

### M2. Time-to-Target

定义：

- 达到目标所需的 wall-clock / macro-action 数 / 实验数

建议至少报 3 个版本：

- wall-clock
- experiment count
- macro-action count

### M3. Best Objective Value

定义：

- 在预算内达到的最好目标值

示例：

- yield
- purity
- selectivity
- STY
- E-factor

### M4. Simple Regret / Cumulative Regret

适用：

- reaction optimization benchmark

说明：

- `simple regret`：与最优值的最终差距
- `cumulative regret`：整个搜索过程中的累计损失

### M5. Hypervolume

适用：

- 多目标优化任务

尤其适合：

- Summit 中同时优化 STY 和 E-factor 的场景

## 6.2 规划与 observation 指标

### M6. Observation Efficiency

定义：

- 每次 observation 带来的性能提升 / 信息增益

可实现为：

- `delta(best_objective) / #observations`
- 或带成本权重版本

### M7. Blind Execution Length

定义：

- 相邻 decision point 之间执行的 workstation 数

要报：

- mean
- median
- p90
- max

这个指标直接反映是否真的实现了 `macro-action` 粒度。

### M8. Forced Observation Trigger Rate

定义：

- 因 `max_blind_execution_length` 或 anomaly 被系统强制插入 observation 的比例

解读：

- 太高：说明 agent 边界规划太激进
- 太低但效果差：说明 observation 可能过疏

### M9. Observation Placement Quality

推荐定义：

- 选择的 observation 点是否接近高信息价值节点

实现方式：

- 在 replay/simulator 中，用 hindsight 估计每个候选观测点的 value of information
- 比较 agent 选择与 oracle 选择的差距

## 6.3 设备适配与执行指标

### M10. Plan Executability Rate

定义：

- 提议的 `MacroActionPlan` 中，可被成功 dispatch 的比例

### M11. First-Pass Adaptation Success

定义：

- 不需要重试或回退就完成设备适配的比例

### M12. Adaptation Failure Breakdown

按原因拆分：

- `device_unavailable`
- `param_out_of_range`
- `dependency_unmet`
- `no_capability_match`

### M13. Feasibility Recovery Rate

定义：

- 收到 `FeasibilityFeedback` 后，在 K 轮内恢复出可执行方向的比例

## 6.4 Replanning / Workflow evolution 指标

### M14. Prefix Preservation Rate

定义：

- 新版本 workflow 中，被正确保留的已验证前缀占比

### M15. Patch Minimality

定义：

- 在满足修复目标前提下，对未来 workflow 的改动有多小

实现方式：

- edit distance
- changed macro-action count
- changed workstation count

### M16. Replan Latency

定义：

- 从异常/新 observation 到生成可执行新计划的时间

### M17. Recovery Success After Anomaly

定义：

- 出现执行异常后，系统仍能继续完成 query/stage 目标的比例

## 6.5 记忆指标

### M18. Retrieval Hit Rate

定义：

- 被召回的记忆片段中，真正被最终决策引用且有帮助的比例

### M19. Memory Gain

定义：

- 与 `No-Memory` 或 `Flat-Memory` 相比的性能提升

建议至少报：

- success gain
- regret reduction
- token reduction

### M20. Freshness Error Rate

定义：

- 过时记忆误导当前决策的比例

### M21. Context Compression Ratio

定义：

- 使用层级记忆后，实际注入 token / 平铺历史 token

## 6.6 安全指标

### M22. Unsafe Completion Rate

定义：

- 面对危险请求时仍输出可执行危险方案的比例

### M23. Safety Recall / False Negative Rate

定义：

- 真正危险 case 被拦住的比例 / 未拦住的比例

### M24. Over-Blocking Rate

定义：

- 本来安全可执行的 case 被错误阻断的比例

### M25. Hazard Identification Score

适用：

- `LabSafety Bench`

## 6.7 成本与系统效率指标

### M26. Token Cost per Successful Campaign

### M27. Material Cost / Waste per Successful Campaign

### M28. Throughput

建议参考 SDL 文献的标准报告方式，同时报：

- theoretical throughput
- demonstrated throughput

## 7. 如何具体评：推荐实验方案

## 7.1 公平对照原则

所有 architecture baseline 必须满足：

1. 使用同一个 backbone model
2. 使用同一套外部工具
3. 使用同一个 workstation registry
4. 使用同一个 safety rule set
5. 使用同样的时间/轮数/实验预算
6. 使用同样的随机种子集合

否则结论会混杂“模型差异”和“架构差异”。

另外建议把模型实验拆成两层：

1. 主实验：固定同一个 backbone，只比较架构
2. 次实验：在 1 个 frontier general model 和 1 个 chemistry-oriented model 上复现实验，检查结论是否稳健

## 7.2 数据划分原则

### 对 reaction / protocol 数据

- 按 reaction family 划分 train/dev/test
- 尽量避免同类模板泄漏
- 优先做 temporal split 或 publication split

### 对 workstation / adaptation 数据

- 保留一部分 `held-out workstation combinations`
- 检查 OOD 设备组合泛化

### 对安全 benchmark

- 正常样例与 jailbreak 样例分开报

## 7.3 Phase A: 组件级评测

### A. 研究层

用：

- `ChemBench`
- `ChemLLMBench`
- 可选 `ChemPaperBench`

评：

- 准确率
- grounded claim ratio
- evidence coverage
- calibration

### B. 设备适配层

用：

- `ULSA`
- solution-based inorganic synthesis dataset
- 自建 workstation graph benchmark

评：

- action sequence match
- parameter validity
- executable plan rate
- constraint violation rate

### C. 安全层

用：

- `ChemSafetyBench`
- `LabSafety Bench`

评：

- 安全准确率
- 拒绝危险请求能力
- 场景危害识别能力

### D. 记忆层

用：

- 自建 long-horizon benchmark

评：

- retrieval precision / recall
- token reduction
- stale-memory robustness

## 7.4 Phase B: Replay 闭环评测

这是主实验之一。

### 环境构建

从 `ORD + ULSA + solution-based synthesis dataset + internal simulator logs` 构建 replay episodes。

每个 episode 包含：

- 初始 query
- 可用 workstation registry
- result-capable workstation 标记
- 可选 observation 点
- 真值 outcome / observation / anomaly / safety signal

### Episode 运行规则

每轮：

1. agent 读取当前 `DecisionContext + MemoryContext`
2. 输出 `ScientificDirective`
3. 设备适应层输出 `MacroActionPlan`
4. 环境检查可执行性与安全性
5. 返回 `ObservationEvent` / `AnomalyEvent` / `SafetyBlockedEvent`
6. 更新 memory 和 runtime state

### 报告指标

主指标：

- Goal Achievement Rate
- experiments-to-target
- macro-actions-to-target
- plan executability
- anomaly recovery rate

辅助指标：

- blind execution length
- forced observation rate
- token cost
- replan latency

## 7.5 Phase C: Simulator 优化评测

### 任务设置

在 `Summit` / `Olympus` 上统一实验预算，例如：

- low-budget: 10 experiments
- medium-budget: 20 experiments
- high-budget: 40 experiments

### 对比对象

- TuringBrain
- Static Workflow
- Stepwise ReAct
- Protocol Library
- Classical optimizers

### 主要报告

- best objective vs experiments
- simple regret curve
- cumulative regret
- hypervolume
- success under budget

### 关键分析

这里最重要的是证明：

- 同样预算下，TuringBrain 更快接近最优
- 同样目标下，TuringBrain 用更少 observation / experiments
- 收益不是来自“多试几次”，而是来自更好的中间决策粒度

## 7.6 Phase D: 硬件在环评测

建议只做小规模、高价值验证。

### 任务 1：简单配液/样品制备

目的：

- 验证 protocol translation
- 验证安全门不过度阻塞

### 任务 2：单步反应优化

目的：

- 验证 observation-gated optimization 是否优于 static workflow

### 任务 3：两阶段 workflow

例如：

- precursor preparation -> reaction -> characterization

目的：

- 验证 workflow evolution
- 验证 anomaly recovery

### 真实实验报告建议

至少报：

- 成功率
- 平均实验次数
- 平均用时
- 安全阻断次数
- 人工干预次数

## 8. 消融实验

消融是你论文最关键的部分之一。建议至少做下面 7 个。

### Ablation 1. `-macro-action`

退化为 stepwise planning。

目的：

- 证明中间规划粒度有效

### Ablation 2. `-observation-gating`

改成固定 observation schedule。

目的：

- 证明“在哪里看一眼”本身是有价值的规划决策

### Ablation 3. `-workflow-evolution`

禁用 suffix replan / full regenerate。

目的：

- 证明动态 future rewrite 的必要性

### Ablation 4. `-feasibility-feedback`

去掉设备适配层到研究层的快速回退通道。

目的：

- 证明解耦不是形式上的，而是真的减少无效闭环

### Ablation 5. `-hierarchical-memory`

用 flat memory 替代。

目的：

- 证明“结构对齐”优于“平铺日志”

### Ablation 6. `-evidence/confidence`

去掉 `evidence_source` 与 `confidence_level`。

目的：

- 证明 grounding 与 uncertainty 标注能减少幻觉传播

### Ablation 7. `-safety-gate`

只在 post-hoc 记录风险，不做 execution hard gate。

目的：

- 证明安全层不是 cosmetic module

## 9. 统计检验与报告规范

### 重复次数

对 replay/simulator benchmark：

- 每个任务至少 5-10 个随机种子

对真实实验：

- 若成本允许，每个设置至少 3 次

### 统计方法

推荐：

- paired bootstrap CI
- Wilcoxon signed-rank test
- effect size

### 报告形式

主表建议至少 3 张：

1. 端到端主结果表
2. 安全与可执行性表
3. 记忆与成本表

主图建议至少 4 张：

1. best objective vs experiments
2. success vs budget
3. unsafe rate vs utility
4. token cost vs performance

## 10. 最终推荐的主实验组合

如果你想先做一版“够 strong、又能落地”的实验，我建议优先做下面这个组合。

### Main Table 1: Closed-loop campaign

benchmark：

- `Summit` SnAr
- `Summit` Reizman Suzuki
- `ORD Replay Benchmark`

baseline：

- Static Workflow
- Stepwise ReAct
- Protocol Library
- TuringBrain

metrics：

- Goal Achievement Rate
- experiments-to-target
- simple regret
- hypervolume
- unsafe completion rate

### Main Table 2: Executability + Replanning

benchmark：

- ULSA
- solution-based synthesis dataset
- replay anomaly suite

metrics：

- plan executability
- first-pass adaptation success
- feasibility recovery rate
- prefix preservation
- patch minimality

### Main Table 3: Memory

benchmark：

- self-built `ChemAgent-LongHorizonBench`

baselines：

- No-Memory
- Flat-Memory
- Hierarchical-Memory

metrics：

- success rate
- retrieval hit rate
- context compression ratio
- token cost

### Main Table 4: Safety

benchmark：

- ChemSafetyBench
- LabSafety Bench

metrics：

- unsafe completion rate
- safety recall
- over-block rate
- hazard identification accuracy

## 11. 一句话总结

对你的 chem agent，最合理的评测不是“找一个单独 benchmark 跑分”，而是：

- 用 `ChemBench / ChemLLMBench` 评 research layer
- 用 `ULSA / solution-based synthesis dataset` 评 device adaptation
- 用 `Summit / Olympus / ORD replay` 评 observation-gated macro-action 的闭环收益
- 用自建 `LongHorizonBench` 评层级记忆
- 用 `ChemSafetyBench / LabSafety Bench` 评安全
- 再用少量 hardware-in-the-loop 验证 simulator 结论

如果论文只保留一条主线，那么主 claim 应该落在：

`TuringBrain 在相同预算和相同 backbone 下，相比 static workflow、stepwise planning 和 protocol-library baselines，能够以更少实验、更低风险和更低上下文成本完成闭环化学实验任务。`

## 12. 参考资源

- [Coscientist: Autonomous chemical research with large language models](https://www.nature.com/articles/s41586-023-06792-0)
- [ChemCrow: Augmenting large language models with chemistry tools](https://www.nature.com/articles/s42256-024-00832-8)
- [A-Lab: An autonomous laboratory for the accelerated synthesis of inorganic materials](https://www.nature.com/articles/s41586-023-06734-w)
- [ChemGraph: agentic framework for computational chemistry workflows](https://www.nature.com/articles/s42004-025-01776-9)
- [ChemBench (Nature Chemistry 2025)](https://www.nature.com/articles/s41557-025-01815-x)
- [ChemLLMBench official repo](https://github.com/ChemFoundationModels/ChemLLMBench)
- [Open Reaction Database](https://pubs.acs.org/doi/10.1021/jacs.1c09820)
- [Summit benchmark](https://gosummit.readthedocs.io/en/latest/)
- [Olympus benchmark](https://aspuru-guzik-group.github.io/olympus/index.html)
- [ULSA](https://arxiv.org/abs/2201.09329)
- [Solution-based inorganic synthesis dataset](https://www.nature.com/articles/s41597-022-01317-2)
- [ChemSafetyBench](https://arxiv.org/abs/2411.16736)
- [LabSafety Bench](https://yujunzhou.github.io/LabSafetyBench.github.io/)
- [Performance metrics for self-driving labs](https://www.nature.com/articles/s41467-024-45569-5)
