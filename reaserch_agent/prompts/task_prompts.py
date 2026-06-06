"""Task prompts for the research agent workflows."""

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

PAPER_PROTOCOL_EXTRACT_PROMPT = """## 任务名称
paper protocol extract

## 任务目标
从知识库命中的论文内容中抽取论文实际报告的实验过程，并转成结构化 protocol。

## 输入
### 人类 query
{query}

### 知识库命中文本
{knowledge_context}

## 核心要求
1. 只抽取论文中已经出现或能由同一句/同一实验段直接确定的信息
2. 不要为了让流程完整而编造 mmol、mg、mL、温度、时间、转速、扫描条件等参数
3. 如果论文文本没有给出某个具体参数，写“文献未说明”，不要自行补全
4. 如果知识库中已有结构化参数列表，优先逐步复用该参数列表
5. 每个 step 必须是论文实验过程中的一个真实实验段，而不是研究建议
6. 输出应面向后续 macro plan 转换，因此保留试剂名称、用量、浓度、体积、温度、时间、后处理和表征条件

## 输出要求
只输出 JSON：
{{
  "protocols": [
    {{
      "source_title": "论文标题",
      "source_file": "来源文件路径或空字符串",
      "relevance": "为什么该 protocol 与 query 相关",
      "protocol_summary": "论文实验过程摘要",
      "steps": [
        {{
          "步骤序号": 1,
          "操作": "论文中的实验动作",
          "试剂/对象": "论文中的试剂、样品或对象",
          "参数": "论文给出的具体参数；若缺失写 文献未说明",
          "evidence": "支持该步骤的简短论文依据或上下文"
        }}
      ]
    }}
  ],
  "missing_parameters": ["论文未说明但后续执行可能需要的参数"]
}}

要求：
- protocols 最多输出 3 个，优先输出与 query 最相关的论文
- steps 中的“参数”不得使用“适量”“按需”“建议”等规划性表达，除非论文原文就是这样
- 不要输出设备/workstation 控制语义"""

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
  每个 macro action 必须同时包含具体实验操作、使用的试剂/样品/对象、以及关键实验参数。
  关键实验参数包括但不限于用量、浓度、体积、温度、时间、溶剂比例、电压、电流、电流密度、参比电极、终点现象等。
- `current_stage_plan`：
  当前 stage 的完整科学语义计划，粒度高于待执行 macro plan，
  应概括该 stage 的目标、推进逻辑、关键变量、预期 observation 和完成条件。

## 输入
### 人类 query
{query}

### 调研报告
{survey_report_json}

### 从知识库论文抽取的实验过程
{extracted_protocols_json}

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
6. macro step 的格式和粒度必须对齐“文献参数列表”风格，而不是泛化研究建议
7. 必须优先把“从知识库论文抽取的实验过程”转成 macro_plan
8. 不得随意改写论文抽取步骤中的试剂、用量、体积、温度、时间等具体参数
9. 若论文 protocol 足够完整，直接忠实转换；若论文没有读取到完整步骤或关键参数大量缺失，可以基于调研报告和化学常识补全一个可执行 macro plan，但必须在 current_stage_plan 中说明“部分参数为 agent 补全”

## macro step 粒度标尺
每个 macro step 应该像结构化文献抽取中的 `参数列表`：
- 一个 step 是一个可独立交给下游 device adaptation layer 继续拆解的实验段
- `操作` 应是短语级实验动作，例如“配制 NiCo-PBA 前驱体 A 液”“共沉淀制备 NiCo-PBA”“制备电极浆料并涂覆”
- `试剂/对象` 应列出核心试剂、样品或被处理对象，例如“Ni(NO3)2 + sodium citrate”“K3Co(CN)6”“NiCo@A-NiCo-PBA/FTO”
- `参数` 应保留自然语言实验条件，优先包含 mmol、mg、mL、浓度、溶剂比例、时间、温度、电压、参比电极等关键量
- 不要输出“围绕 query 进行首轮探索性配方准备”“进一步优化条件”“结合文献细化”这类占位性步骤
- 不要把洗涤、干燥、离心、陈化简单删掉；如果它们在文献中和合成段绑定，可写进同一个 step 的 `参数`
- 通常输出 4-8 个 macro steps；若参考案例有可迁移的 `参数列表`，优先沿用其粒度并按当前 query 做必要改写
- 如果 extracted_protocols 提供了高质量 steps，则 macro_plan 应与其中最相关 protocol 的 steps 保持同源、同参数
- 如果 extracted_protocols 只有零散 PDF 句子、步骤缺少试剂/参数、或多数参数为“文献未说明”，允许生成 agent 补全 plan；但不要把补全内容伪装成论文原文

## 格式示例
下面是目标粒度示例，只用于学习格式和粒度，不表示当前任务必须使用这些试剂：
[
  {{
    "步骤序号": 1,
    "操作": "配制 NiCo-PBA 前驱体 A 液",
    "试剂/对象": "Ni(NO3)2 + sodium citrate",
    "参数": "0.6 mmol nickel nitrate + 0.9 mmol sodium citrate in 25 mL deionized water"
  }},
  {{
    "步骤序号": 2,
    "操作": "配制 NiCo-PBA 前驱体 B 液",
    "试剂/对象": "K3Co(CN)6",
    "参数": "0.4 mmol potassium hexacyanocobaltate(III) in 25 mL deionized water"
  }},
  {{
    "步骤序号": 3,
    "操作": "共沉淀制备 NiCo-PBA",
    "试剂/对象": "将 B 液滴加到 A 液",
    "参数": "magnetic stirring 10 min; age 24 h at room temperature; centrifuge, wash, vacuum dry at 60 C overnight"
  }}
]

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
- `步骤序号` 从 1 开始连续编号
- `current_stage_plan` 必须写成一个高层化学语义计划字符串，但内容上要覆盖：
  当前 stage 名称、目标 observation point、stage goal、planning logic、key variables、expected observation、stage completion condition
- 如果当前 stage 的目标 observation point 是 XRD，则 macro_plan 应覆盖从样品制备到 XRD 观察点为止的完整流程，而不是只写合成前半段
- `macro_plan_summary` 应解释为什么这样组织当前 macro plan，以及它如何服务于当前 stage
- 生成的 macro_plan 必须严格属于当前 stage

## 输出前自检
输出前必须检查：
1. 每一步是否都有明确的具体实验操作，而不是研究建议？
2. 每一步是否明确写出试剂、样品或处理对象？
3. 每一步参数是否包含具体实验条件，例如 mmol、mg、mL、M、h、min、C、V、mA g^-1、overnight、室温、滴加、洗涤至澄清等？
4. 是否出现了“围绕 query”“进一步优化”“结合文献细化”“当前缺少”“待补充”等占位表达？
5. 是否错误写成 workstation 级控制指令？
若任一检查不通过，必须重写 macro_plan 后再输出 JSON。"""


OBSERVATION_STAGE_FIT_JUDGE_PROMPT = """## 任务名称
observation stage fit judge

## 任务目标
判断最新 observation 是否仍然支持当前 stage，并给出结构化科学解释。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 当前 stage
{current_stage}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### 当前 stage 设计理由
{current_stage_reason}

## 输出要求
只输出 JSON：
{{
  "fits_current_stage": true,
  "status": "normal",
  "reason": "一句话解释 observation 与当前 stage 的关系",
  "observation_interpretation": {{
    "summary": "对 observation 的科学解释",
    "positive_signals": ["支持当前计划的信号"],
    "negative_signals": ["不支持当前计划的信号"],
    "uncertainties": ["仍需确认的信息"]
  }}
}}

要求：
- status 只能是 normal、abnormal、inconclusive 之一
- 如果 observation 明显说明失败、杂相、无沉淀、无有效信号、偏离目标，则 fits_current_stage 为 false
- 如果 observation.feedback_type 是 device_feasibility_error，说明实验尚未执行，而是设备适应层拒绝执行；
  此时必须把 status 设为 abnormal，fits_current_stage 设为 false，并在 negative_signals 中列出设备不支持原因
- 如果信息不足但没有明确失败，status 用 inconclusive"""


STAGE_PROGRESS_UPDATE_PROMPT = """## 任务名称
stage progress update

## 任务目标
在不重写 stage 路线的前提下，根据正常 observation 更新当前 stage 的推进位置。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 当前 stage
{current_stage}

### stage 路线
{stage_route_json}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### stage 路线设计理由
{stage_route_reason}

### 当前 stage 设计理由
{current_stage_reason}

### observation stage fit judge 结果
{fit_judge_json}

## 输出要求
只输出 JSON：
{{
  "stage_progress_status": "continue_current_stage",
  "current_stage": "更新后的当前 stage",
  "current_stage_reason": "为什么当前应处于这个 stage",
  "stage_route": ["stage 1", "stage 2"],
  "stage_route_reason": "保持或更新 stage 路线的理由",
  "progress_summary": "当前 observation 后的阶段推进摘要"
}}

要求：
- stage_progress_status 只能是 continue_current_stage、advance_to_next_stage、closure_ready 之一
- 除非 observation 已经完成当前 stage 的目标 observation point，否则不要进入下一个 stage
- 不要新增设备语义字段"""


POST_OBSERVATION_MACRO_PLAN_DESIGN_PROMPT = """## 任务名称
post-observation macro plan design

## 任务目标
基于最新 observation、上一段 macro plan 和当前 stage，生成下一段待执行 macro plan。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 调研报告
{survey_report_json}

### stage 路线
{stage_route_json}

### 当前 stage
{current_stage}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### stage 路线设计理由
{stage_route_reason}

### 当前 stage 设计理由
{current_stage_reason}

### 参考案例
{reference_context}

## device_feasibility_error 特别规则
如果最新 observation 的 feedback_type 是 device_feasibility_error：
- 这不是实验结果异常，而是设备适应层判断上一段 macro plan 不能落地
- 必须读取 observation 中的 unsupported_reasons、blocking_constraints、unsupported_requested_items
- 必须读取 observation 中的 supported_device_capabilities，尤其是 supported_containers 和 supported_workstations
- 必须读取 observation 中的 previous_stage_context、previous_macro_action、prior_paper_hits
- 新 macro_plan 必须保留上一次规划的科学目标和论文依据，但要避开设备不支持的容器、工作站和动作
- 不要再次输出被拒绝的反应釜、高压釜、聚四氟乙烯内衬反应釜、不可用 XRD 工作站或不可用 pH 闭环动作
- 如果平台只支持进样瓶、西林瓶、50ml耐热瓶、留样瓶，应优先把路线改写为瓶内配液、混合、搅拌、老化、离心洗涤、干燥或离线 observation
- 若某个目标 observation 只能离线完成，应在 macro_plan 中明确写成“离线 observation”，不要伪造设备层不存在的工作站

## 输出要求
只输出 JSON：
{{
  "current_stage_plan": "更新后的当前 stage 完整化学语义实验计划",
  "macro_plan": [
    {{
      "步骤序号": 1,
      "操作": "步骤名称",
      "试剂/对象": "对象",
      "参数": "自然语言参数"
    }}
  ],
  "macro_plan_summary": "一句话说明这段 plan 如何响应最新 observation"
}}

要求：
- macro_plan 必须严格属于当前 stage
- 每一步必须包含具体实验操作、试剂/对象、参数
- 参数应包含关键实验条件，不要输出占位性研究建议
- 不要重复上一段已经完成且 observation 已确认成功的 macro steps，除非需要复现实验或修复异常"""


DEVICE_ADAPTATION_MACRO_PLAN_DESIGN_PROMPT = """## 任务名称
device-adaptation macro plan design

## 任务目标
根据设备适应层返回的 device_feasibility_error，只做设备适配层面的 macro_plan 改写。
这一步不是重新定义研究目标，也不是重写 stage route。

## 输入
### 人类 query
{query}

### 设备适应层 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 调研报告
{survey_report_json}

### stage 路线
{stage_route_json}

### 当前 stage
{current_stage}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### stage 路线设计理由
{stage_route_reason}

### 当前 stage 设计理由
{current_stage_reason}

### 参考案例/知识库命中
{reference_context}

## 硬性边界
- 必须保留输入中的 current_stage 原意、stage_route、原始 query、目标材料/目标相和目标 observation point。
- 不允许把目标从 K2Fe[Fe(CN)6]·2H2O / 亚铁氰化铁改成其他材料或只做颜色/产量观察。
- 不允许把 XRD completion condition 改成颜色、质量、浑浊度或其他过程 observation。
- 如果设备层没有 XRD 工作站，XRD 必须写成“离线 XRD observation / 送样 / 数据回传”，不能伪造设备内 XRD 工作站。
- 只允许修改设备无法执行的容器、工作站、转移方式、体积尺度、分批/分瓶方式、加热/老化方式和洗涤干燥落地方式。
- 优先保留上一段 macro plan 中的核心试剂、化学计量关系和论文依据；若需要缩放，应说明按比例缩放而不是更换合成目标。
- 必须读取 unsupported_reasons、blocking_constraints、unsupported_requested_items，避免再次输出这些不支持项。
- 必须读取 supported_device_capabilities，尤其 supported_containers 和 supported_workstations，并把 macro_plan 写成这些设备可执行的动作。
- macro_plan 的每一步应是正向可执行命令，不要把“不使用某设备”写成设备动作。
- 不要直接照抄 observation 中被设备层判定不支持的动作、容器、工作站、传感器、闭环判断或在线表征能力。
- 如果最终 observation point 需要设备外表征，必须明确区分“设备内可执行步骤”和“离线 handoff/数据回传”，但不要伪造设备描述中没有的表征工作站。

## 输出要求
只输出 JSON：
{{
  "current_stage_plan": "设备适配后的当前 stage 计划说明；必须保留原 stage 目标和 observation point",
  "macro_plan": [
    {{
      "步骤序号": 1,
      "操作": "步骤名称",
      "试剂/对象": "设备支持容器/工作站中的对象",
      "参数": "具体可执行参数"
    }}
  ],
  "macro_plan_summary": "一句话说明只做了哪些设备适配，不改变合成目标"
}}

要求：
- macro_plan 必须严格属于当前 stage。
- 参数应包含关键实验条件、体积/摩尔量/温度/时间/转速等，不要输出占位性研究建议。
- 最后一类目标 observation 若设备不可执行，应作为离线 observation/handoff 继续保留。"""


ABNORMAL_OBSERVATION_SURVEY_QUERY_GENERATE_PROMPT = """## 任务名称
abnormal observation survey query generate

## 任务目标
围绕异常 observation 生成增量知识库检索 query。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 当前 stage
{current_stage}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### 已有调研报告
{survey_report_json}

### observation stage fit judge 结果
{fit_judge_json}

## 输出要求
只输出 JSON：
{{
  "queries": ["增量调研关键词1", "增量调研关键词2"],
  "reason": "说明这些 query 需要补充哪些异常修复知识"
}}

要求：
- queries 至少 2 条，最多 5 条
- query 应面向异常原因、替代路线、修复策略或相似失败案例
- 如果 observation.feedback_type 是 device_feasibility_error，queries 应重点检索设备可执行替代路线、
  室温/瓶内/无反应釜合成、设备约束下的实验改写，而不是实验结果失败原因"""


ABNORMAL_OBSERVATION_SURVEY_EXPANSION_PROMPT = """## 任务名称
abnormal observation survey expansion

## 任务目标
判断异常 observation 场景下的增量调研是否足够支撑修复。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### observation stage fit judge 结果
{fit_judge_json}

### 已有调研报告
{survey_report_json}

### 当前累计知识
{knowledge_context}

## 输出要求
只输出 JSON：
{{
  "continue_research": false,
  "new_queries": [],
  "reason": "说明当前知识是否足够修复"
}}

要求：
- 如果知识足以判断修复层级，continue_research 为 false
- 如果仍缺关键异常原因或替代路线，continue_research 为 true，并给出 1-4 条 new_queries"""


SIMILAR_ABNORMAL_CASE_SEARCH_PROMPT = """## 任务名称
similar abnormal case search

## 任务目标
生成 memory 检索 query，用于查找相似异常 observation 或修复案例。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### observation stage fit judge 结果
{fit_judge_json}

### 当前累计知识
{knowledge_context}

### 当前 stage
{current_stage}

### stage 路线
{stage_route_json}

## 输出要求
只输出 JSON：
{{
  "queries": ["memory query 1", "memory query 2"],
  "reason": "说明这些 query 如何帮助检索相似异常案例"
}}

要求：
- queries 最多 4 条
- 优先包含材料名、异常现象、目标 observation point、修复方向"""


POST_OBSERVATION_REPORT_UPDATE_PROMPT = """## 任务名称
post-observation report update

## 任务目标
根据异常 observation、增量知识和历史案例更新调研报告。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### observation stage fit judge 结果
{fit_judge_json}

### 增量知识
{knowledge_context}

### memory 结果
{memory_context}

### 已有调研报告
{survey_report_json}

## 输出要求
只输出 JSON：
{{
  "summary": "更新后的调研总结",
  "key_findings": ["发现1", "发现2"],
  "candidate_precedents": ["案例标题1", "案例标题2"],
  "route_implications": ["对修复或后续 stage 的启发"],
  "open_questions": ["仍未解决的问题"]
}}

要求：
- 保留已有调研报告中仍然有效的信息
- 明确加入当前异常 observation 对后续计划的影响
- 如果 observation.feedback_type 是 device_feasibility_error，应明确写入：
  当前设备层不支持、具体不支持原因、上一段 stage/macro_action 中哪些部分需要删除或替换、
  以及后续路线必须满足的设备能力边界"""


STAGE_INTERNAL_REPAIR_ASSESS_PROMPT = """## 任务名称
stage internal repair assess

## 任务目标
判断是否可以不改变当前 stage，只重写当前 stage 内部实验计划来修复异常。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 更新后的调研报告
{survey_report_json}

### 当前 stage
{current_stage}

### 当前 stage 设计理由
{current_stage_reason}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### observation stage fit judge 结果
{fit_judge_json}

## 输出要求
只输出 JSON：
{{
  "repairable": true,
  "updated_current_stage_plan": "如果可修复，给出更新后的当前 stage 完整计划",
  "repair_reason": "为什么可以或不可以在 stage 内部修复"
}}

要求：
- 只有当目标 observation point 和 stage 边界仍然合理时，repairable 才能为 true
- 如果需要改变 observation point 或 stage 边界，repairable 必须为 false
- 如果 observation.feedback_type 是 device_feasibility_error，优先判断是否能在不改变当前 observation point 的前提下，
  仅把当前 stage 内部 macro_plan 改写成设备可执行路线；若可以，repairable 应为 true。
  updated_current_stage_plan 必须保留原 current_stage、目标材料/目标相和 XRD/目标 observation point。"""


CURRENT_STAGE_REPAIR_ASSESS_PROMPT = """## 任务名称
current stage repair assess

## 任务目标
判断是否需要并可以修改当前 stage 来修复异常。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 更新后的调研报告
{survey_report_json}

### 当前 stage
{current_stage}

### 当前 stage 设计理由
{current_stage_reason}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### observation stage fit judge 结果
{fit_judge_json}

### stage 路线
{stage_route_json}

### stage 路线设计理由
{stage_route_reason}

## 输出要求
只输出 JSON：
{{
  "repairable": true,
  "current_stage": "新的当前 stage",
  "current_stage_reason": "新的当前 stage 理由",
  "current_stage_plan": "新的当前 stage 完整计划",
  "repair_reason": "为什么可以或不可以通过修改当前 stage 修复"
}}

要求：
- 新 current_stage 必须仍服务于原始 query
- 如果必须重写整个 stage 路线，repairable 必须为 false
- 如果 observation.feedback_type 是 device_feasibility_error，repairable 必须为 false；
  设备不可执行只能触发设备适配 macro_plan，不能修改 current_stage 或目标 observation point"""


STAGE_ROUTE_REPAIR_ASSESS_PROMPT = """## 任务名称
stage route repair assess

## 任务目标
判断是否需要修改整个 stage 路线来修复异常。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 上一段 macro plan
{previous_macro_plan_json}

### 更新后的调研报告
{survey_report_json}

### 当前 stage
{current_stage}

### 当前 stage 设计理由
{current_stage_reason}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### observation stage fit judge 结果
{fit_judge_json}

### stage 路线
{stage_route_json}

### stage 路线设计理由
{stage_route_reason}

## 输出要求
只输出 JSON：
{{
  "repairable": true,
  "stage_route": ["更新后的 stage 1", "更新后的 stage 2"],
  "stage_route_reason": "为什么需要这样更新 stage 路线",
  "repair_reason": "为什么可以或不可以通过修改 stage 路线修复"
}}

要求：
- stage_route 必须以 observation point 为边界
- 如果没有足够信息重写路线，repairable 必须为 false
- 如果 observation.feedback_type 是 device_feasibility_error，repairable 必须为 false；
  设备适配不允许重写 stage_route 或合成目标"""


NEW_ROUTE_STAGE_DESIGN_PROMPT = """## 任务名称
new route stage design

## 任务目标
在 stage route 已更新后，确定新的当前 stage、设计理由和完整 stage 计划。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 更新后的调研报告
{survey_report_json}

### stage 路线
{stage_route_json}

### stage 路线设计理由
{stage_route_reason}

## 输出要求
只输出 JSON：
{{
  "current_stage": "新的当前 stage",
  "current_stage_reason": "为什么当前进入这个 stage",
  "current_stage_plan": "新的当前 stage 完整化学语义实验计划"
}}

要求：
- current_stage 必须是 stage_route 中的一个
- current_stage_plan 必须服务于当前 observation 后的下一步合法决策点"""


MANUAL_HANDOFF_COMPOSE_PROMPT = """## 任务名称
manual handoff compose

## 任务目标
当 B2 无法自动修复时，生成最小必要人工交接摘要。

## 输入
### 人类 query
{query}

### 最新 observation
{observation_json}

### 当前 stage
{current_stage}

### 当前 stage 设计理由
{current_stage_reason}

### stage 路线
{stage_route_json}

### stage 路线设计理由
{stage_route_reason}

### 当前 stage 的完整化学语义实验计划
{current_stage_plan}

### 上一段 macro plan
{previous_macro_plan_json}

### 更新后的调研报告
{survey_report_json}

### observation stage fit judge 结果
{fit_judge_json}

### 各层修复失败原因
{repair_failures_json}

## 输出要求
只输出 JSON：
{{
  "manual_handoff": "面向人工接管的简明摘要",
  "blocking_questions": ["需要人工判断的问题1", "问题2"]
}}

要求：
- 摘要应包括异常事实、已尝试修复层级、为什么自动链路不可靠、建议人工优先检查什么"""
