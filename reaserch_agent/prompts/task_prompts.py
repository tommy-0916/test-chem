"""Task prompts for the B1 bootstrap workflow."""

SURVEY_QUERY_GENERATE_PROMPT = """## 任务名称
survey query generate

## 任务目标
把人类 query 展开成第一轮知识调研 query。

## 输入
### 人类 query
{query}

### 附加约束
{constraints_json}

## 输出要求
只输出 JSON，对应格式如下：
{{
  "queries": ["调研关键词1", "调研关键词2", "调研关键词3"],
  "reason": "用一句话说明这些关键词覆盖了哪些知识空缺"
}}

要求：
- queries 至少 3 条，最多 6 条
- 每条 query 都应可直接用于本地知识库检索
- query 应覆盖材料、方法、性能目标、关键变量中的至少两类信息"""

SURVEY_EXPANSION_PROMPT = """## 任务名称
survey expansion

## 任务目标
判断当前知识是否足够支撑 stage 设计；若不足，则提出下一轮检索方向。

## 输入
### 人类 query
{query}

### 当前累计知识
{knowledge_context}

## 输出要求
只输出 JSON，对应格式如下：
{{
  "continue_research": false,
  "new_queries": [],
  "reason": "说明当前知识是否足够，以及还缺什么"
}}

要求：
- 当知识已经足够时，continue_research 必须为 false
- 当知识不足时，continue_research 为 true，new_queries 提供 1-4 条新增检索 query"""

SIMILAR_EXP_SEARCH_PROMPT = """## 任务名称
similar exp search

## 任务目标
生成 memory 检索 query，用于查找相似实验、相似材料或相似目标。

## 输入
### 人类 query
{query}

### 当前累计知识
{knowledge_context}

## 输出要求
只输出 JSON：
{{
  "queries": ["memory query 1", "memory query 2"],
  "reason": "说明为什么这些 query 有助于找相似实验"
}}

要求：
- query 最多 4 条
- 优先面向相似材料、相似工艺和相似目标"""

SURVEY_REPORT_GENERATE_PROMPT = """## 任务名称
survey report generate

## 任务目标
整理已有知识与历史案例，形成可直接支撑 stage 设计的结构化调研报告。

## 输入
### 人类 query
{query}

### 知识检索结果
{knowledge_context}

### 历史案例结果
{memory_context}

## 输出要求
只输出 JSON：
{{
  "summary": "150-300 字总结",
  "key_findings": ["发现1", "发现2"],
  "candidate_precedents": ["案例标题1", "案例标题2"],
  "route_implications": ["对 stage 设计的启发1", "启发2"],
  "open_questions": ["未解决问题1", "未解决问题2"]
}}"""

STAGE_DESIGN_PROMPT = """## 任务名称
stage design

## 任务目标
基于人类 query 和调研报告，先在内部识别当前研究任务中的关键 `observation point`，
再将相邻 observation point 之间的科学计划划分为 `stage`，
并确定当前首先应进入的 `stage`。

## 重要定义
- `observation point`：
  对合成化合物的物理性质或理化性质进行关键观察、检测或表征的节点。
  只有这类节点才能作为 stage 的边界。
- `stage`：
  从当前已知状态出发，到下一个 observation point 之前的一整段科学计划。

## 输入
### 人类 query
{query}

### 调研报告
{survey_report_json}

## 强约束
1. 必须先在内部识别 `observation point`，再划分 `stage`
2. 不得按单个工艺步骤、操作动作或常规实验流程直接拆分 stage
3. 若 query 只有一个关键 observation point，则只允许输出一个 stage
4. 每个 stage 必须明确对应一个目标 observation point
5. 当前 stage 必须是最先应进入的 stage，而不是“最重要”的 stage

## 输出要求
只输出 JSON：
{{
  "stage_route": ["stage 1", "stage 2", "stage 3"],
  "current_stage": "stage 1",
  "stage_route_reason": "为什么这样拆 stage",
  "current_stage_reason": "为什么当前先做这个 stage"
}}

要求：
- 保持与既有系统兼容，不要新增 JSON 字段
- 不要输出单独的 `observation_points` 字段，而是把 observation point 的识别结果写进 `stage_route_reason`
- `stage_route` 中每个元素都应是 stage 名称，并且名称本身应尽量体现它对应的目标 observation point
- stage_route 至少 1 个 stage，最多 4 个
- current_stage 必须是 stage_route 中的一个
- 不要输出设备语义

特别注意：
“配液”“混合”“反应”“洗涤”“离心”“干燥”“取样”“送检”这类内容，
默认都应被视为到达某个 observation point 之前的执行步骤，
即 macro action 的候选内容，而不是 stage 的候选边界。
只有新的关键 observation point 才能形成新的 stage。

## 输出前自检
1. 是否先识别了 observation point，再生成 stage
2. 每个 stage 是否都对应一个 observation point
3. current_stage 是否来自 stage_route
4. 是否错误地把洗涤、干燥、搅拌、过滤这类工艺步骤单独拆成 stage
5. 若 query 只有一个关键 observation point，是否只输出了一个 stage

"""

MACRO_PLAN_DESIGN_PROMPT = """## 任务名称
macro plan design

## 任务目标
在当前 `stage` 下，设计一组推进到该 stage 目标 `observation point` 的 `待执行 macro plan`，
并同时生成该 stage 的完整化学语义实验计划。

## 重要定义
- `current_stage`：
  当前所处的 stage，其边界由 observation point 定义
- `待执行 macro plan`：
  当前 stage 内的具体执行步骤或步骤段，
  用于推进实验到达该 stage 的目标 observation point。
  它不构成新的 stage 边界。
- `current_stage_plan`：
  当前 stage 的完整科学语义计划，粒度高于待执行 macro plan，
  应概括该 stage 的目标、推进逻辑、关键变量、预期 observation 和完成条件。

## 输入
### 人类 query
{query}

### 调研报告
{survey_report_json}

### stage 路线
{stage_route_json}

### 当前 stage
{current_stage}

### stage 路线设计理由
{stage_route_reason}

### 当前 stage 设计理由
{current_stage_reason}

### 参考案例
{reference_context}

## 强约束
1. 待执行 macro plan 必须严格属于当前 stage
2. 待执行 macro plan 的目标必须是推进到当前 stage 的目标 observation point
3. 不得把未来 stage 的内容提前写入当前 macro plan
4. 不得把单个 macro step 写成设备/workstation 控制指令
5. 若当前 stage 只有一个 observation point，则所有 macro steps 都必须服务于该唯一 observation point

## 输出要求
只输出 JSON：
{{
  "current_stage_plan": "对当前 stage 的完整化学语义实验计划描述",
  "macro_plan": [
    {{
      "步骤序号": 1,
      "操作": "步骤名称",
      "试剂/对象": "对象",
      "参数": "自然语言参数"
    }}
  ],
  "macro_plan_summary": "一句话概括这段 macro plan 在做什么"
}}

要求：
- macro_plan 必须是一个步骤数组
- 每一步必须包含 步骤序号、操作、试剂/对象、参数
- 参数保持实验自然语言，不要翻译成 workstation 级动作
- `current_stage_plan` 必须写成一个高层化学语义计划字符串，但内容上要覆盖：
  当前 stage 名称、目标 observation point、stage goal、planning logic、key variables、expected observation、stage completion condition
- 如果当前 stage 的目标 observation point 是 XRD，则 macro_plan 应覆盖从样品制备到 XRD 观察点为止的完整流程，而不是只写合成前半段
- `macro_plan_summary` 应解释为什么这样组织当前 macro plan，以及它如何服务于当前 stage
- 生成的 macro_plan 必须严格属于当前 stage"""
