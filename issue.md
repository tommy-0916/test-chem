# Chem Agent 当前问题（简明版）

本文档记录三个相互独立的问题。最优先的是前两个：

| 优先级 | 问题 | 一句话说明 |
| --- | --- | --- |
| P0 | 联网论文检索关键词错误 | 检索论文时不应包含“自动化、设备、工作站、303 实验室”等任务背景词 |
| P0 | 输出对用户不可读 | 应把论文、Stage、Macro Plan、Device Step 和阻塞原因整理成一个清晰结果 |
| P1 | 联网结果重复性较差 | 相同 Query 多次运行时，论文和实验方案变化过大 |

---

# Issue 1：联网论文检索关键词不应包含设备和自动化关键词

## 问题是什么

用户的 Query 同时包含两类信息：

1. 真正需要检索论文的化学研究内容；
2. “基于现有自动化化学工作站”“下发到 303 实验室”等设备和任务背景。

当前 Chem Agent 曾经把完整 Query 直接用于联网论文检索，导致以下无关词进入学术搜索：

- 自动化；
- 自动化化学工作站；
- 设备；
- 工作站；
- 机器人；
- 303 实验室；
- 下发任务；
- 实验任务 ID；
- 设计、优化、系统等宽泛词。

这些词不是材料化学论文主题，会降低搜索准确性。例如，NiFe-PBA 任务曾错误命中“电气工程及其自动化系统”相关论文。

## 正确行为

联网搜索前，应先从原始 Query 中抽取“化学研究关键词”，再生成 2–4 条简短的学术检索式。

例如，NiFe-PBA 任务可以使用：

```text
NiFe Prussian blue analogue aqueous coprecipitation synthesis
NiFe PBA electrochemical activation cyclic voltammetry
nickel iron hexacyanoferrate surface reconstruction
NiFe Prussian blue analogue alkaline electrochemistry EIS
```

不应使用：

```text
automated chemistry workstation
303 laboratory
device planning
robotic experiment design
```

## 处理规则

联网论文检索关键词应保留：

- 目标材料：NiFe-PBA、Prussian blue analogue、hexacyanoferrate；
- 合成方法：coprecipitation、hydrothermal、electrodeposition 等；
- 研究过程：electrochemical activation、surface reconstruction；
- 表征与测试：XRD、XPS、CV、LSV、EIS；
- 性能目标：OER、HER、UOR、EOR 等具体反应。

联网论文检索关键词应删除：

- 自动化和机器人描述；
- 工作站名称和设备数量；
- 实验室编号；
- “请设计实验”“请下发任务”等用户指令；
- “返回任务 ID”等系统操作要求。

设备信息仍然可以提供给 Device Agent 做可执行性规划，但不能污染论文检索关键词。

## 验收标准

- 日志中可以看到实际发送给论文数据库的检索词；
- 检索词不包含自动化、设备、工作站、机器人、303 实验室等词；
- 每条检索词至少包含一个核心材料或反应实体；
- 只命中“自动化、设计、优化、系统”的论文不能进入知识库；
- 原始用户 Query 保持不变，关键词清理只用于论文搜索。

## 当前状态

状态：部分修复，仍需通过多组测试题统一验收。

---

# Issue 2：Chem Agent 输出对用户不可读

## 问题是什么

当前 Chem Agent 的结果分散在多个文件中，例如：

- `research_state.json`；
- `device_state.json`；
- `device_package.json`；
- `plan_versions.jsonl`；
- Research 和 Device CLI 日志；
- 论文知识库 registry。

普通用户很难判断：

- 原始 Query 是什么；
- 联网找到了哪些论文；
- 哪些论文真正被实验方案采用；
- 实验分成哪些 Stage；
- Macro Plan 是什么；
- 每个 Macro Step 被转换成哪些 Device Steps；
- 哪些步骤设备可以完成；
- 哪些步骤需要人工或离线处理；
- 为什么方案被判定为 `manual_required` 或 `feasibility_error`；
- 最终实验方案到底是什么。

## 正确行为

Chem Agent 应在保留所有原始文件的同时，额外生成一个统一的用户可读结果，例如：

```text
human_readable_result.json
```

这个文件是对 Chem Agent 原始输出的抽取和整理，不得修改原始 Query，也不得人工改写 Agent 的实验方案。

## 建议结果结构

```json
{
  "original_query": "用户逐字符原始输入",
  "run_status": "completed | manual_required | feasibility_error",
  "papers": [
    {
      "title": "论文标题",
      "doi": "DOI",
      "source": "online | local_memory",
      "relevance": "为什么命中",
      "used_in_plan": true
    }
  ],
  "stages": [
    {
      "stage_id": "S01",
      "name": "合成并完成 XRD 观察",
      "goal": "该阶段要获得什么结果"
    }
  ],
  "macro_plan": [
    {
      "macro_step_id": "M01",
      "operation": "操作内容",
      "parameters": "参数",
      "evidence_refs": ["论文 DOI 或 agent_generated"]
    }
  ],
  "device_plan": [
    {
      "device_step_id": "D01",
      "source_macro_step_id": "M01",
      "workstation": "工作站名称",
      "operation": "设备操作",
      "parameters": {}
    }
  ],
  "offline_handoffs": [],
  "blocking_constraints": [],
  "final_experiment_plan": "面向用户的最终实验方案"
}
```

## 关于“agent 补全”

`agent 补全` 不表示人工修改了用户输入。

它表示：论文或知识库没有提供完整参数，Chem Agent 自己生成了缺失参数。

建议把含糊的 `agent 补全` 改成更明确的结构：

```json
{
  "source_type": "agent_generated",
  "reason": "检索到的论文没有给出完整沉淀参数",
  "requires_review": true
}
```

这样用户可以区分：

- 有论文直接支持的参数；
- 从论文推导的参数；
- Agent 自行补全、需要人工确认的参数。

## 前端最少需要展示的内容

可视化 Demo 至少需要以下页面：

1. 原始 Query；
2. 命中论文及其用途；
3. Stage 路线；
4. Macro Plan；
5. Device Steps；
6. Macro Step 到 Device Step 的对应关系；
7. 离线人工步骤；
8. 设备阻塞约束；
9. 最终人类可读实验方案；
10. 原始 JSON 和日志。

## 验收标准

- 用户不需要打开多个 JSON 文件就能看懂结果；
- 原始 Query 在展示和结果文件中逐字符保持不变；
- 论文、Stage、Macro Step 和 Device Step 都有稳定 ID；
- 每个 Device Step 能追溯到对应 Macro Step；
- `manual_required` 和 `feasibility_error` 有明确中文原因；
- Agent 自行补全的内容必须显式标记；
- 新的用户可读 JSON 只是抽取结果，不能覆盖原始 Agent 产物。

## 当前状态

状态：可视化 Demo 已能展示主要内容，但统一的 `human_readable_result.json` 仍需正式实现和稳定化。

---

# Issue 3：相同 Query 的联网结果重复性较差

## 问题是什么

同一个用户 Query 进行两次联网运行时：

- 高层研究目标大致一致；
- 命中的论文明显变化；
- Stage 数量和内容变化；
- 合成样品矩阵变化；
- 电化学活化条件变化；
- Device 可执行性结论也可能变化。

因此，当前 Chem Agent 的联网结果重复性较差：高层目标大致一致，但论文命中、Stage、合成矩阵和电化学方案均明显变化。

## 可能原因

- 论文数据库每次返回的候选不同；
- 网络限流导致可用来源变化；
- LLM 生成检索词和实验参数存在随机性；
- 候选论文排序和筛选规则不够稳定；
- 没有固定随机种子或确定性的参数选择规则；
- Agent 对证据不足的参数进行了不同补全。

## 建议改进

- 保存每次实际使用的论文检索词；
- 保存候选论文全集和筛选原因；
- 固定候选排序和去重规则；
- 对关键实验参数使用明确的证据优先级；
- 固定模型温度、随机种子和版本；
- 将“论文直接支持”和“Agent 补全”分开；
- 建立同一 Query 的重复运行对比测试。

## 验收标准

同一 Query、同一模型、同一知识库快照和同一配置重复运行时：

- 核心论文集合基本稳定；
- Stage 路线保持稳定；
- 核心对照组和实验变量保持稳定；
- 关键电化学测试条件保持稳定；
- 如果结果变化，输出中应说明变化来自论文、网络还是 Agent 补全。

---

# Issue 4：Device 反馈闭环存在，但多轮重规划仍可能不满足设备要求

## 问题是什么

当前 Chem Agent 已经具备以下闭环：

```text
Research 生成 Macro Plan
    ↓
Device 检查设备可执行性
    ↓
Device 返回 feasibility_error 和阻塞约束
    ↓
Research 根据反馈重新规划 Macro Plan
    ↓
再次交给 Device 检查
```

但是，这个闭环只能保证“继续尝试修复”，不能保证 Research 重规划后的新方案一定满足设备要求。

实际可能出现：

```text
第一轮：容器不兼容
    ↓
Research 修改路线
    ↓
第二轮：体积超过设备上限
    ↓
Research 再次修改
    ↓
第三轮：样品无法在两个工作站之间连续转移
    ↓
达到 deadlock 阈值，转人工处理
```

## 当前实现的主要限制

### 1. Research 收到的设备信息不够完整

Research 初始规划阶段通常接收压缩后的设备能力摘要。

Device 返回不可行结果后，Research B2 主要收到：

- 阻塞约束；
- 支持的容器列表；
- 支持的工作站列表；
- 不支持的操作或对象；
- Device 给出的修改建议。

它不一定收到全部 42 台设备的完整参数、容器状态转换规则、操作审计规则和设备间转移关系。

### 2. Device 每轮可能只暴露当前命中的问题

某个方案可能同时存在多个设备问题，但 Device 在当前路径上首先发现容器不兼容后就返回错误。

Research 修复容器问题后，下一轮才暴露体积、温度、配平或转移路径问题，造成逐层失败。

### 3. 多轮反馈没有形成完整的累计约束记忆

Research 重规划时应知道前面每一轮已经失败的原因。

当前实现可能只重点处理最新一轮反馈，没有把所有历史阻塞约束转换成不可违反的累计规则，因此可能再次生成相同或相近的不可执行路线。

### 4. Research 的质量检查不等于设备可执行性检查

Research 的 Macro Plan 质量门主要检查：

- 化学语义是否合理；
- 参数是否足够具体；
- 是否存在模糊表达；
- 是否保持目标材料、Stage 和观察点。

它不能完整验证：

- 容器类型是否连续；
- 开盖和关盖状态是否连续；
- 液体累计体积是否超限；
- 离心数量和配平是否成立；
- 固体是否能够合法转移；
- 前一个工作站的输出是否满足下一个工作站的输入；
- 所有设备参数是否处于真实支持范围。

这些问题通常只有进入 Device Agent 后才能发现。

### 5. 当前闭环有停止阈值，但没有收敛保证

达到 `feasibility_deadlock_limit` 后，系统会停止自动修复并转人工处理。

这个机制可以避免无限循环，但不能证明前几轮重规划会逐步接近设备可执行方案。

## 正确行为

Device 返回不可行结果后，Research 重规划应该同时接收：

1. 本轮所有已发现的阻塞约束；
2. 前几轮累计的阻塞约束；
3. 与 Device Agent 完全一致的设备能力快照；
4. 每个不可行 Macro Step 对应的具体设备原因；
5. Device 给出的可行替代路线或允许参数范围；
6. 已经尝试并失败的方案摘要，避免重复生成。

Research 生成新 Macro Plan 后，应先通过一次确定性的设备预检查，再进入下一轮正式 Device 映射。

## 建议反馈结构

```json
{
  "feedback_type": "device_feasibility_error",
  "device_snapshot_id": "workstation_snapshot_20260716",
  "failed_macro_steps": [
    {
      "macro_step_id": "M03",
      "requirement": "350 ℃空气煅烧",
      "blocking_constraint": "当前容器不能进入马弗炉",
      "unsupported_transition": "50ml耐热瓶 -> 96位石英孔板",
      "supported_alternatives": [
        "从实验开始使用96位石英孔板兼容路线",
        "改为设备支持的低温处理路线"
      ]
    }
  ],
  "cumulative_constraints": [
    "不得使用没有合法转移路径的50ml耐热瓶到96位石英孔板路线",
    "单种溶液加注量不得超过对应移液平台上限"
  ],
  "previous_failed_plan_ids": ["plan_v1", "plan_v2"],
  "request": "保留研究目标和观察点，重新生成满足累计设备约束的Macro Plan"
}
```

## 建议改进

### P0：累计保存所有 Device 阻塞约束

每次 Device 返回 `feasibility_error` 后，将阻塞原因写入 Campaign 级约束集合。

新一轮 Research 规划必须显式检查并满足全部历史约束，不能只处理最新错误。

### P0：Research 和 Device 使用相同设备快照

两层应使用同一个 `device_snapshot_id` 和同一份设备真源，避免 Research 根据摘要规划、Device 根据完整规则拒绝。

### P0：反馈必须关联到具体 Macro Step

Device 应明确指出：

- 哪个 Macro Step 不可行；
- 违反了哪条设备规则；
- 对应工作站、容器和参数是什么；
- 有哪些设备支持的替代方案。

### P1：增加确定性的 Device 预检查

Research 生成新 Macro Plan 后，先检查：

- 容器连续性；
- 工作站输入输出兼容性；
- 体积和温度范围；
- 离心配平；
- 样品状态转换；
- 固体和液体转移路径；
- 设备当前可用状态。

预检查失败时，直接在 Research 内部继续修复，不消耗一次完整 Device 迭代。

### P1：禁止重复生成已失败路线

对每个失败方案生成稳定的 `plan_signature`。

如果新方案与历史失败方案的关键容器路径、设备操作和参数基本相同，应在进入 Device 前拒绝。

### P1：输出多轮修复轨迹

用户应能看到：

```text
Plan v1：为什么不可行
Plan v2：修改了什么，仍然为什么不可行
Plan v3：修改了什么，是否通过 Device
```

## 验收标准

- Device 的每条阻塞约束都能关联到具体 Macro Step；
- Research 每次重规划都能读取全部历史阻塞约束；
- Research 和 Device 使用同一个设备能力快照；
- 新方案不得重复已经失败的关键路线；
- 每轮输出清楚记录“失败原因—修改内容—新检查结果”；
- 只有 Device 返回 `success` 才能标记方案为设备可执行；
- 达到 deadlock 时，应输出无法收敛的累计原因，而不是只显示最后一次错误；
- 在固定测试集上，多轮修复后的 Device 成功率应明显高于当前版本。

## 当前状态

状态：Open。

现有代码已经实现 Device→Research→Device 的反馈框架，但缺少完整设备约束传递、历史约束累计、重复方案检测和确定性收敛检查。

---

# Issue 5：Macro Plan 的失败位置不一致，Research 与 Device 的可行性判定存在断层

## 问题现象

当前测试中出现了两种不同的失败形式：

1. **能够生成 Macro Plan，但 Device 不支持。**
   Research 认为方案在化学语义和初步设备边界上成立，因此输出 Macro Plan；进入 Device 后，才发现具体设备操作、容器路径或可下发参数不足，最终返回 `feasibility_error`，没有 Device Steps。

2. **Research 直接不能生成 Macro Plan。**
   Macro Plan 候选已经生成或正在生成，但被 Research 内部的质量门禁拒绝，最终状态为 `manual_required`，对用户表现为 Macro Plan 为空，Device 也没有机会进行正式判断。

## 两个复现实例

### A02：有 Macro Plan，但 Device 不支持

A02 重跑时，Research 成功生成了 6 个 Macro Actions，包括前驱体制备、碱化、搅拌熟化、离心洗涤、干燥和 XRD 表征。

进入 Device 后返回 `feasibility_error`。阻塞点是 XRD 步骤要求 Cu Kα 辐射源和 `2θ = 5–80°` 扫描范围，但 `XRD_V1` 的可下发参数 schema 只包含步长、扫描速度、滴液体积和干燥时间，无法保证辐射源及扫描起止角参数。

这说明 Research 使用的是较粗粒度的设备能力描述，而 Device 使用更严格的可下发参数 schema；两层对“设备支持”的定义不一致。

### C02：Research 直接没有 Macro Plan

C02 重跑时成功命中论文并形成 Stage，但 Macro Plan 最终为空，状态为 `manual_required`。

其中一次 Macro LLM 调用超时后完成内部重试；最终失败原因不是联网失败，而是 Research 质量门禁将“静置老化、独立静置老化”判定为当前设备边界下不可直接适配的表达，并清空了整份 Macro Plan。

这说明 Research 的前置门禁可能过严：单个步骤存在设备适配疑问时，没有保留其余有效步骤，也没有把候选计划交给 Device 做权威可行性判断或触发稳定的改写降级路径。

## 根本原因

- Research 和 Device 没有共享同一套确定性可行性校验器；
- Research 主要检查自然语言表达和压缩后的设备能力，Device 检查完整操作与参数约束；
- Research 门禁既可能漏过设备不支持的细节，也可能过早拒绝可改写的操作；
- Macro Plan 的质量问题采用“整份清空”，缺少逐步标记、局部修复和可解释输出；
- 用户无法从空 Macro Plan 区分 LLM 生成失败、Research 门禁拒绝、网络错误和设备不可行。

## 建议改进

### P0：明确两层职责

- Research 负责生成完整的化学语义 Macro Plan；
- Research 发现疑似设备问题时，应给步骤添加 `needs_device_validation` 或 `adaptation_required` 标记，而不是直接清空整个计划；
- Device 作为设备可执行性的权威判定层，只有 Device 返回 `success` 才能标记方案可执行。

### P0：建立共享的确定性预校验器

Research 和 Device 应共同调用同一套设备能力检查，至少覆盖：

- 操作是否存在；
- 参数字段是否可下发；
- 参数范围是否满足；
- 容器和样品状态是否连续；
- 工作站之间是否存在合法转移路径。

### P1：质量门禁改为逐步报告和局部修复

当“静置老化”等表达不满足要求时，应保留候选 Macro Plan，指出具体问题步骤，并尝试改写为设备支持的等价操作。只有无法修复且会破坏实验目标时，才进入 `manual_required`。

### P1：统一失败状态和用户可读原因

建议显式区分：

- `macro_generation_error`：模型没有生成合法计划；
- `macro_quality_error`：Research 质量门禁未通过；
- `device_feasibility_error`：Device 判定不可执行；
- `network_or_retrieval_error`：联网或论文检索失败。

即使 Macro Plan 最终未通过，也应向用户展示候选步骤、被拒绝的具体步骤、规则和修复建议。

## 验收标准

- A02 类案例在 Research 阶段即可发现 XRD 缺少可下发参数，或明确标记等待 Device 验证；
- C02 类案例不会因为一个“静置老化”表达而无解释地清空整个 Macro Plan；
- Research 与 Device 对相同设备约束给出一致结果；
- 所有失败均能明确显示发生层级、具体步骤、触发规则和建议替代方案；
- UI 不再仅以“Macro Plan 为空”表示多种完全不同的失败原因。

## 当前状态

状态：Open。

当前系统同时存在 Research 前置门禁过严和设备细节校验不足的问题，因此会出现“直接没有 Macro Plan”与“有 Macro Plan 但 Device 不支持”两种不一致表现。

---

# Issue 6：Macro Action 与 Stage 的层级语义发生偏移

## 原始设计

原始设计中，实验方案应以观察结果为边界组织 Macro Action：每个 Macro Action 都应服务于一个明确的 observation point，并在完成后产出该观察结果。

目标层级应为：

```text
Observation Point（观察结果）
        ↓
Macro Action（获得该结果所需的完整化学动作）
        ↓
Device Steps（设备执行步骤）
```

例如 NiFe-LDH 活化实验可以组织为：

- Macro Action 1：制备未活化 NiFe-LDH，并获得 XRD 基线结果；
- Macro Action 2：活化 10 min，并获得电化学响应；
- Macro Action 3：活化 30 min，并获得电化学响应；
- Macro Action 4：活化 60 min，并获得电化学响应。

每个 Macro Action 的结束条件都是一个可识别的 observation point，而不是单纯完成若干设备操作。

## 当前实现

当前 Research Agent 实际采用了另一种层级：

```text
Stage 1：未反应样品阶段
  └── Macro Plan：制备、洗涤、干燥、XRD

Stage 2：活化阶段
  └── Macro Plan：活化和测试
```

代码和输出字段以 `stage_route`、`current_stage`、`current_stage_plan` 为主要规划单位，Macro Plan 被定义为“属于当前 Stage 的一组动作”。因此，观察点被用于划分 Stage，而不是直接定义 Macro Action 的结果边界。

## 问题表现

- UI 主要展示 Stage，用户看不到每个 Macro Action 对应的目标观察结果；
- 一个 Stage 可能包含多个实验目标或多个观察结果；
- Macro Action 变成了 Stage 内部的操作清单，而不是可独立验收的实验动作单元；
- Device Agent 接收到的是 Stage 内的动作集合，难以明确每个设备步骤服务于哪个观察结果；
- 观察结果返回后，系统只能判断当前 Stage 是否完成，不能稳定判断哪个 Macro Action 已完成、哪个 Macro Action 需要重规划。

## 影响

这会造成实验规划、设备映射和观测反馈之间的边界不清：

1. Research 层无法以 Macro Action 为单位维护实验进度；
2. Device 层无法以 observation point 为单位验证设备路线是否达成目标；
3. 多轮 observation 反馈时，系统可能更新整个 Stage，而不是只更新对应的 Macro Action；
4. 前端展示与用户原本理解的“观察点—宏观动作—设备步骤”关系不一致。

## 建议的数据结构

建议保留 Stage 作为可选的流程上下文，但将 Macro Action 恢复为观察点驱动的一级规划对象：

```json
{
  "stage": "电化学活化与性能比较",
  "observation_points": [
    {
      "id": "activation_10min_response",
      "goal": "获得 10 min 活化后的电化学响应",
      "macro_action": {
        "objective": "在统一电解液和载量下活化 10 min 并完成测试",
        "completion_condition": "得到有效电化学响应数据",
        "steps": []
      },
      "device_steps": []
    }
  ]
}
```

其中：

- `stage`：表示当前实验所处的上下文，可用于组织多个相关观察点；
- `observation_point`：表示需要获得的实验结果；
- `macro_action`：表示为获得该结果需要执行的完整化学动作；
- `device_steps`：表示 Macro Action 到工作站操作的映射；
- `completion_condition`：用于判断该 Macro Action 是否真正完成。

## 建议改进

### P0：恢复 Macro Action 的观察点结果语义

Macro Action 必须包含：

- 对应的 observation point；
- 实验目标；
- 输入样品或前置条件；
- 化学动作序列；
- 预期观察结果；
- 完成条件。

### P0：不要让 Stage 替代 Macro Action

Stage 可以继续用于路线管理和上下文记录，但不能作为 Macro Action 的结果边界。一个 Stage 可以包含多个 Macro Action，但每个 Macro Action 必须对应一个明确的 observation point。

### P1：统一 Research、Device 和 UI 的标识

每个 Device Step 都应带有：

```text
observation_point_id
macro_action_id
source_macro_step
```

这样可以追踪设备步骤最终服务于哪个实验观察结果。

### P1：按 Macro Action 处理 observation 反馈

当新的 observation 返回时，系统应先判断对应的 Macro Action 是否完成，再决定：

- 生成同一 Stage 下的下一个 Macro Action；
- 修复当前 Macro Action；
- 切换到下一个 Stage；
- 重新规划整个实验路线。

## 验收标准

- 每个 Macro Action 都有唯一的 observation point 和 completion condition；
- 一个 Stage 可以包含多个 Macro Action，但 Macro Action 不再只是 Stage 的无结果步骤列表；
- UI 能直接显示“观察结果—Macro Action—Device Steps”的对应关系；
- Device feasibility error 能关联到具体的 observation point 和 Macro Action；
- 新 observation 能准确更新对应 Macro Action 的状态，而不是默认更新整个 Stage；
- 对 NiFe-LDH 活化测试，10、30、60 min 等结果应分别表示为独立 Macro Action。

## 当前状态

状态：Open。

当前系统已经识别 observation point，但在输出层将其用于划分 Stage，并把 Macro Plan 从属于 Stage，尚未实现原始设计中的“以 observation point 定义 Macro Action 结果边界”。
