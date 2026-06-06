"""
Pre-Flow Agent task prompts.
"""

KNOWLEDGE_LOOP_PROMPT = """## 任务名称
Knowledge Sufficiency Decision（知识充分性判断与下一轮检索规划）

## 任务目标
你需要判断：基于当前已经拿到的上下文，下游 workflow 生成是否已经拥有“足够的相关知识”。

这里的“知识”只指：
1. 与该 macro_plan 直接相关的化学过程知识
2. 关键原料/中间体/产物的性质、注意事项和常见实验条件
3. 与该路线最接近的实验片段、操作经验和方法学提示

以下内容不属于本轮 knowledge 检索缺口：
- 工作站支持哪些参数、哪些操作
- 设备实例数、库存数量、独占关系、并行调度事实
- “当前实验室到底有几套设备”这类环境资源信息

这些内容应以下游收到的 `workstations_new` 描述文件和 macro_plan 显式要求为真源，由 downstream generator / verifier / feasibility 审核处理。

如果当前知识已经足够，请直接停止继续检索。
如果当前知识还不够，请给出一个更具体、下一轮可直接执行的 knowledge query。

## 当前轮次
- 当前是第 {round_index} / {max_rounds} 轮

## 输入
### macro_plan
{macro_plan}

### 已有知识片段
{existing_knowledge}

## 输出格式
请严格按以下标题输出，不要添加额外标题：

### 知识充分性判断
[用 2-4 句话说明当前知识是否已经足以支撑下游生成；如果不足，缺口主要是什么]

### 知识是否充分
[yes 或 no]

### 仍然缺失的知识
[若已充分写“无”；若不充分，写出还缺什么]

### 下一次知识检索query
[若已充分写“无”；若不充分，写一段更具体的 query]

## 注意事项
- 第 1 轮只能根据 macro_plan 本身判断，不要假装已经看过外部知识。
- 从第 2 轮开始，只能依据输入中给出的“已有知识片段”判断，不要编造新事实。
- 如果剩余问题只涉及设备数量、独占、同步、并行调度或环境库存，则应判定 chemistry/process knowledge 已经充分，不要继续查询 knowledge。
- query 要具体，优先包含：目标材料、关键反应/处理步骤、关键试剂、关键参数、关键注意事项。
- 不要生成 memory query。
- 不要生成关于设备库存、独占调度、资源冲突规则的 knowledge query。
"""


CONTEXT_SUMMARIZING_PROMPT = """## 任务名称
Context Summarizing（上下文汇总与下游输入整理）

## 任务目标
你已经完成了若干轮 knowledge 检索判断。现在请综合 macro_plan、已检索到的知识片段和相关工作站能力摘要，生成下游真正需要的结构化输入。

请完成以下工作：
1. 提炼一段简洁但完整的 macro_plan 摘要
2. 提取 observation 要求
3. 生成 memory 检索 query
4. 将最有用的 knowledge 信息总结成可供下游直接消费的要点

## 输入
### macro_plan
{macro_plan}

### 已检索到的 knowledge 片段
{knowledge_context}

### 相关工作站能力摘要
{workstation_descriptions}

## 输出格式
请严格按以下标题输出，不要添加额外标题：

### macro_plan摘要
[用 150-300 字概括方法、目标材料、关键步骤、关键参数和边界条件。若 macro_plan 明确写出设备数量、独立设备、同步开始/结束、独占或“若不满足则不可执行”的条件，必须保留这些条件。]

### 观察要求
- 是否需要观察：[是/否]
- 观察位置建议：[若需要，只能填写 observation-capable 工作站；当前冻结约束下优先填写 `dual-station-electrochemical-workstation`。若未来真源中加入 XRD/XDR 工作站，也只能在这些工作站中选择；若不需要写“无”]
- 观察内容：[例如颜色变化、沉淀形成、液位变化、澄清度等；若不需要写“无”]
- 观察目的：[说明这些观察为何对下游有帮助；若不需要写“无”]

### memory_query
[一段适合检索相似实验记录的查询文本]

### knowledge_takeaways
- [列出 3-6 条真正会影响下游 workflow 生成的知识要点；如果某个关键限制来自 macro_plan 的设备资源要求而不是知识库，请明确写成“环境可达性前提”，不要把它伪装成论文知识]

## 注意事项
- 输出要紧扣当前 macro_plan，不要泛泛而谈。
- knowledge_takeaways 应优先保留：关键化学约束、关键操作注意事项、关键参数范围、关键实验现象。
- 设备数量、独占、同步和“若不满足则不可执行”的条件若来自 macro_plan，应在摘要或 takeaways 中保留，但不要把它们解释成 temporary knowledge 检索所得事实。
- observation 位置建议只能落在 observation-capable 工作站上。当前真源目录中实际存在且可确认的 observation-capable 工作站是 `dual-station-electrochemical-workstation`；若未来真源中加入 XRD/XDR 工作站，也只能在这些工作站中选点。不要把磁搅、纯化、超声、烘干等工作站写成观察位置。
- 如果已检索知识有限，也要基于现有内容做出尽可能高质量的整理，不要拒答。
- memory_query 要服务于历史 workflow 检索，而不是复述全部知识片段。
"""


FORWARD_TRANSLATING_PROMPT = """## 任务名称
Forward Translating（检索结果装配记录）

## 任务目标
当前代码路径会把已经得到的：
1. macro_plan 摘要
2. observation 要求
3. memory 检索结果
4. knowledge 检索结果

整理成下游 WorkflowGenerator 直接消费的上下文。

这个 prompt 当前不触发新的 LLM 调用，只用于把“这一步应该如何理解和装配上下文”记录到实验日志中，方便人工审阅。

## 输入
### 1. macro_plan
{macro_plan}

### 2. macro_plan摘要
{macro_plan_summary}

### 3. 观察要求
{observation_requirements}

### 4. memory query
{memory_query}

### 5. knowledge queries
{knowledge_queries}

### 6. memory 检索结果
{memory_search_result}

### 7. knowledge 检索结果
{knowledge_search_result}

### 8. knowledge 要点
{knowledge_takeaways}

### 9. txt 格式参考
{reference_format}

## 装配原则
1. `/workspace/chem_resources/workstations_new` 是工作站能力与约束的唯一真源。
2. knowledge 应优先保留与当前 macro_plan 最贴近的化学要点、关键参数和注意事项。
3. 历史实验记录应优先保留与当前目标最接近、且最能帮助 workflow 生成的片段。
4. observation 要求必须显式保留，供下游决定是否在 workflow 中表达观察说明。
5. observation 位置只能落在 observation-capable 工作站上。当前真源目录中实际存在且可确认的 observation-capable 工作站是 `dual-station-electrochemical-workstation`；若未来真源中加入 XRD/XDR 工作站，也只能在这些工作站中选点。
6. macro_plan 中显式给出的设备数量、独立设备、同步开始/结束、独占或“若不满足则不可执行”的条件，必须被保留下游 feasibility 使用；不要在这里把它们弱化、删除或伪装成普通知识片段。
7. goal_in_this_iteration 应紧扣本轮 macro_plan，不扩展 research layer 的其他职责。
"""
