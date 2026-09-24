"""Task prompts for the research agent workflows."""

from chem_agent_contracts.v2 import LOGICAL_LID_STATE_PROMPT

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
- query 应覆盖材料、方法、性能目标、关键变量中的至少两类信息
- query 只写化学研究内容（材料/合成方法/研究过程/表征测试/性能反应），
  不得包含自动化、化学工作站、工作站、设备、机器人、实验室编号、下发任务、任务 ID
  这类任务背景词；每条 query 必须至少含一个核心材料或反应实体
- 若附加约束中包含 device_context，query 应优先检索可与这些设备能力兼容或可改造成兼容路线的文献方法，
  但设备名称本身不得写进 query"""

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
- 若知识文本中含有 [p.N] 页码标记，请给对应 step 附加可选字段 "page"（整数，取该实验段所在页）；没有标记则省略该字段
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

### 设备边界上下文
{device_context_json}

## 强约束
1. 必须先在内部识别 `observation point`，再划分 `stage`
2. 不得按单个工艺步骤、操作动作或常规实验流程直接拆分 stage
3. 若 query 只有一个关键 observation point，则只允许输出一个 stage
4. 每个 stage 必须明确对应一个目标 observation point
5. 当前 stage 必须是最先应进入的 stage，而不是“最重要”的 stage
6. 若设备边界上下文非空，只用于避免规划明显需要当前平台不存在的大型设备、在线 observation 能力，
   或必须通过无支持转移/换瓶才能串联的容器路线；不要在 stage 设计中选择具体工作站、机器容器或容器编号，
   容器和设备动作由下游 device agent 决定。

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
- 不要输出 workstation pipeline 或机器控制语义；但可以在理由中说明某些 observation 需要离线 handoff

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

MACRO_ACTION_DESIGN_PROMPT = """## 任务名称
macro action design

先设计当前 stage 到下一个 observation point 的一整批 macro action，再由下一步细化 macro steps。
当前只使用 operation 层 skill：操作名称和科学语义；不得读取或输出工作站参数表、设备 I/O、容器或实例编号。
planned_operations 是有序的化学操作语义，不是机器命令，不得将一次洗涤等动作变为 stage。
结合 query、文献、当前 stage、真实 observation 与失败反馈，保留科学目标并避免已失败路线。
输出 JSON object：
{{
  "objective": "本批实验要解决的科学目标",
  "planned_operations": ["配制前驱体", "反应", "分离纯化", "目标表征"],
  "experiment_group": {{
    "group_id": "当前单组实验的稳定标识",
    "role": "experimental|control|repeat|calibration",
    "sample_id": "当前唯一样品标识",
    "hypothesis": "本组检验的假设",
    "comparison_to": [],
    "variables": {{"变量名": "本组取值"}}
  }},
  "expected_observation": "下一观察节点预期可获得的信息，不是编造的实验结果",
  "completion_condition": "什么真实 observation 可判定本批完成",
  "current_stage_plan": "本 stage 的推进逻辑、科学变量和完成条件"
}}

当前规划模式：{planning_mode}
当前 stage：{current_stage}
下一 observation point：{observation_point}

每次只规划一个实验组和一个样品执行单元。不得同时生成实验组、对照组或重复组的批量矩阵；
下一组必须等待本组 observation 及计划值、设备设定值、实际值和偏差返回后再规划。
"""

MACRO_STEP_CONTRACT_PROMPT = """
## 已先行确定的 macro action 与步骤接口
下面的 macro action 已先于步骤生成。必须将其 planned_operations 细化为当前待执行步骤，
保留 objective、expected_observation 和 completion_condition，不得改成不同科学路线或越过目标观察点。
{macro_action_json}

当前设备 skill 是 step 层，仅据其中声明的 I/O、容器兼容和返回信息细化：
{device_context_json}

每个 macro step 必须给出稳定字符串 macro_step_id，并保留步骤序号、操作、试剂/对象、参数、来源、
quantity_requirements。材料合同只能依据已授权的实验语义填写，不得按步骤编号、工作站类别或常见流程猜测：
- material_inputs / material_intermediates / material_outputs：数组；每项必须区分
  material_id（材料种类）和 material_instance_id（这一批/这一状态实例），并给出 name、state、
  logical_container_id、provenance。input 另给 material_origin=external_inventory|upstream_output；
  upstream_output 必须给 parent_output_refs=[{{"macro_step_id":"上游步骤ID", "material_instance_id":"上游输出实例ID"}}]。
  macro 内中间态使用 material_origin=same_step_relation，不得伪装成外部 input。
- quantity 的 exact 仅表示计划目标或估计，必须增加 semantic=planned_target|planning_estimate；
  它不证明上游库存足够，也不表示设备已经称量、产出或消费了该数值；人工批准计划值也不能把它变成实测值。
  all_available 必须使用 semantic=whole_batch_unspecified 且不得带 value/unit；runtime_measured 必须使用
  semantic=runtime_measurement_required 且不得带 value/unit，它只表示未来运行时测量要求，不是实测证据。
  quantity_requirements 的 value/unit 是每项的顶层字段，不是嵌套的 quantity 对象；
  每个主动投料还须在 material_inputs[].quantity 中声明计划数量，两处的物料、数值和单位必须一致。
  论文只给浓度而未直接给剂量时，不得把浓度×体积的计算结果标成文献原文直给数值。
  若同一文献摘录以 respectively 明确给出有序试剂、有序 mM 浓度及 mL 体积，允许把计算出的
  mmol 剂量标为 source=literature_calculation，仍用绑定原文的 paper provenance，且必须在
  quantity_requirements 项的顶层、与 provenance 并列地附加
  derivation={{"rule":"mM_times_mL_to_mmol_v1","ordered_materials":[原文顺序的完整试剂名称],
  "material_evidence_name":"本项在原文中的完整名称","concentration_value":原文对应 mM 数值,
  "concentration_unit":"mM","volume_value":原文 mL 数值,"volume_unit":"mL"}}。
  结果必须等于浓度×体积/1000；不得把此计算结果写成 source=literature，也不得把任意
  列表中的其他试剂浓度错配给本试剂。摘录或顺序不完整时保持 unresolved。
- material_relations：显式关系数组。每项给 relation_id、event_kind
  (none|state_change|process_same_material|split_same_material|replicate_same_material)、
  input_material_instance_ids、output_material_instance_ids、logical_container_ids、quantity_basis、
  source_operation_ref（macro 内稳定操作段 ID）及 provenance。一个 macro 内有净化、洗涤、干燥等多段
  转换时，用 material_intermediates 和多条有序 relation 表达，不得压成一个转换。
  process_same_material、split_same_material、replicate_same_material 必须保持同一 material_id；每个 input
  实例只能被一条 relation 消费，每个 output 只能由一条 relation 产出。分支必须由一条显式 split relation
  产生不同 output 实例，不能让两条下游关系各自“使用全部”。process_same_material 不能用来绕过端点、
  event_kind 或数量账目检查；材料身份变化必须用有证据的 state_change。
  quantity_basis 只能为 whole_batch、conserved_inventory、runtime_measurement_required 或
  planning_yield_lower_bound；后者必须给 planning_quantity={{"mode":"exact","semantic":"planning_estimate",
  "value":数值,"unit":"单位"}} 及明确来源。split/replicate/多输入输出若不是 runtime_measurement_required，
  必须给覆盖全部实例的 input_allocations/output_allocations；计划估计不得冒充执行实测。
  每条 allocation 必须包含 material_instance_id，以及
  quantity={{"mode":"exact","semantic":"planned_target 或 planning_estimate","value":有证据的数值,
  "unit":"有证据的单位"}}。input_allocations 必须逐一覆盖所有输入端点，output_allocations 必须
  逐一覆盖所有输出端点；不能把多输入或多输出关系写成 whole_batch。若产物量在执行前未知，
  使用 runtime_measurement_required，不得为满足分配字段而编造产率或实际库存。
- operation_segments：非空数组，把当前 macro 中每个有授权依据的操作段写成
  {{segment_id, material_effect, source_operation_ref, provenance}}。material_effect 只能为
  none|register_existing_input|observe_without_material_change|consume_material|produce_material|
  transform_material|transfer_material|split_material|merge_material|unknown。不得由“步骤名称像登记/
  观察/干燥”来猜 material_effect；无法从当前证据确定时写 unknown，并使相关合同字段
  保持 unresolved。每条 material_relations[].source_operation_ref 必须精确匹配同 macro 内一个
  operation_segments[].segment_id；所有会消耗、产出、转换、转移、分样或合并物料的 segment
  必须由非 none relation 覆盖。
- material_contract_status：必须分别为 material_inputs、material_intermediates、material_outputs、
  logical_containers、material_relations 声明 declared|not_applicable|unresolved。declared 必须有完整非空记录；
  not_applicable 必须为空；unresolved 表示证据不足并会在进入 Device 前停止，不能用空数组冒充不适用。
  每个 not_applicable 必须在 material_applicability 中有且只有一条结构化声明：
  {{contract_field, assertion:"no_<contract_field>", operation_segment_ids:[...], provenance}}。这些 segment 和 provenance
  必须可回溯到当前用户输入或当前证据包；不得据操作名称、关键词或步骤类别判定 N/A。
  material_relations=not_applicable 所引 segment 只能是 none、register_existing_input 或
  observe_without_material_change；否则为冲突。证据不足则标为 unresolved 并停止。
  material_relations=not_applicable 只说明本步没有物料转换边，不能连带关闭其他物料字段：登记已存在、
  已装载的原液/样品时，仍须按原始证据声明 material_inputs、实例身份和逻辑容器；样品矩阵中的目标产物
  不能冒充已存在库存。若原始证据不能确定登记对象是否已经存在，则相应字段必须为 unresolved。
  对这种只登记、不消费的特定外部批次，若可用数量尚未测量，可在 input 使用
  runtime_measured/runtime_measurement_required；这只是后续需要数量时的待测要求，不是已经获得的库存读数。
  若 Research 的科学关系已经明确，即使 Device 当前编译器或工作站尚不支持，也必须保留 declared；
  下游软件/设备能力不足应由 Device 单独阻断，不能反写成 Research 证据不足。
- container_requirements：数组；每项 logical_container_id、container_type、count、capacity_ml、lid_state，
  只填有依据的需求；未知数值用 null，不得虚构容器兼容性。logical_container_id 表示同一样品的逻辑容器，
  不是实体瓶号、机器槽位、原液瓶位或工作站编码。保留跨步骤物料与容器连续性。
  __LOGICAL_LID_STATE_PROMPT__
  count 必须为正整数或 null，capacity_ml 必须为有限正数或 null；禁止字符串数值和布尔值。
- intermediate_returns：数组；每项 name、availability（declared/undeclared）、required_for_next_step、source。
  declared 必须引用当前 step 投影的真实字段：name 与 fields 中字段完全一致，source 为包含
  station_code、operation 的对象，feedback_kind 为 returned_data（操作完成后返回）或
  intermediate_feedback（中间/实时反馈）；对应 feedback_contract 声明必须为 supported。
  完成后返回不能充当实时反馈；控制设定值和物料输出也不是测量返回。
  required_for_next_step 必须为布尔值。delivery_mode 为 automatic、observation 或 manual_handoff。
  未声明但科学步骤确实依赖的读数可保留为 undeclared，必须设置 delivery_mode 为 observation 或
  manual_handoff，并以非空 wait_for 说明等待何种真实读数/人工交接；收到前不得自动继续闭环。
  这些返回需求字段必须保留到 Device handoff，不能靠一个非空来源路径声称已声明支持。
实体工作站、实际瓶号/槽位、开关盖展开、机器参数及编译仍由 Device 决定。
本段关于逻辑容器需求的约定替代旧模板中笼统的“不选择容器”：允许逻辑要求，不允许实体分配。

V2 具体实验设计要求：
- 当前输出只允许服务于 macro action 中的一个 experiment_group/sample_id。
- 所有主动投加的材料必须在 material_inputs 与 quantity_requirements 中给出计划目标数值和单位；
  禁止“适量”“若干”“按需”等模糊投料。
  洗涤水等新加入的外部物料同样属于主动投料：文献只说“洗涤数次”而没有单次或总量时，
  不得用 runtime_measured 冒充已存在库存或计划剂量；对应输入数量保持 unresolved 并停止发布。
- 用户任务与当前证据都没有给出关键物料、数量、路线或终点时，不得用 agent_inferred 补成具体事实；
  对应 material contract 维度必须保持 unresolved，并在 Research -> Device 发布门停止。agent_inferred
  只可用于步骤总体说明或非权威推理记录，不能作为 material ports、relations、operation_segments 或
  material_applicability 的放行依据。
- 只有特定实例整批流转可使用 all_available；运行时待测可使用 runtime_measured。二者都不证明实际产量，
  也不得携带 value/unit。人工批准或自然语言说明不能变成 observed/actual 数量证据。
"""

MACRO_STEP_CONTRACT_PROMPT = MACRO_STEP_CONTRACT_PROMPT.replace(
    "__LOGICAL_LID_STATE_PROMPT__", LOGICAL_LID_STATE_PROMPT
)

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
  每个 macro step 必须同时包含具体实验操作、使用的试剂/样品/对象、以及关键实验参数。
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

### 设备边界上下文
{device_context_json}

## 强约束
1. 待执行 macro plan 必须严格属于当前 stage
2. 待执行 macro plan 的目标必须是推进到当前 stage 的目标 observation point
3. 不得把未来 stage 的内容提前写入当前 macro plan
4. 若当前 stage 只有一个 observation point，则所有 macro steps 都必须服务于该唯一 observation point
5. macro step 的格式和粒度不能是泛化研究建议，每个 macro step 必须同时包含具体实验操作、使用的试剂/样品/对象、以及关键实验参数。
6. 在设备能力满足的情况下，优先把“从知识库论文抽取的实验过程”转成 macro_plan
7. 不得随意改写论文抽取步骤中的试剂、用量、体积、温度、时间等具体参数
8. 若论文 protocol 足够完整，直接忠实转换；若关键物料、数量、路线或终点缺失，不得用化学常识补成
   可执行事实。V2 必须把相应 material contract 维度保留为 unresolved 并停止发布；总体说明可记录
   agent_inferred 的判断，但不能把它当作物料证据。
9. 若设备边界上下文非空，macro_plan 仍应保持化学实验语义，不要选择具体机器容器、工作站、
    容器编号或设备动作；只需避免明确要求当前平台不存在的大型设备（如反应釜/高压釜）或在线表征能力。
10. 如果论文路线含有当前平台不支持的设备或能力，保持原有科学路线并明确报告 capability blocker；
    不得擅自改为常压、低温、外部预配、外部表征或其他不同物料边界。具体物理容器和机器动作仍由
    device agent 在不改变科学语义的范围内决定。
11. 若设备边界上下文非空，macro_plan 还必须避免“容器连续性硬冲突”：
    工作站、默认容器运输、物料转移和样品处理链视为联通，不要求相邻 Skill 重复声明运输边；
    不要把缺少显式运输文字当作硬冲突。只在后续 operation 明确不接受当前容器类型、相态或
    刚性载体时改写路线，并优先保持同一样品、最少换瓶和最少溶剂位置变化。
    research layer 不需要指定具体容器，但要把实验条件写成下游可在同一兼容容器路径内实现的化学语义；
    若文献路线含有会造成设备容器断链的环节，应优先选择化学上等价、设备可适配的宏观表达，或在
    current_stage_plan 中说明该环节需由 device layer 判定可行性，不要把不可转移的容器切换写成必需步骤。
    特别注意：不能仅写“保持同一兼容反应容器路径”“后续直接进入分离”等口头声明来绕过容器断链。
    若静置/老化会触发不可连通的暂存容器，保持已授权的静置/老化语义并报告 capability blocker；
    只有原始任务或当前证据明确授权时，才可写外部/人工 handoff 或改成搅拌熟化。
12. 若设备边界上下文显示固体称量或大体积离心存在限制，必须保留原任务的物料形态、库存边界和尺度：
    - 不得把固体改写成“外部预配并已装载的原液/前驱体溶液”；
    - 不得为了满足设备上限擅自缩小反应体积或改变配比；超出能力时明确阻断；
    - “持续磁力搅拌条件下加入”“边搅拌边滴加”“同步搅拌加液”或缓慢/控速加入是可保留的化学时间语义，
      但必须允许下游 device agent 将其改写成固定体积分批加液、批次间固定转速搅拌的可审计节拍；
      不要把缺少单站原子化并行能力直接当成 feasibility_error。
    - 结晶/老化方式、温度、转速和时长只能来自用户任务或当前证据；不得用体系类别套入固定模板。
13. 不得仅因参考论文或设备便利而引入、移除或替换特定载体/基底；证据中的刚性载体不能自动改成
    粉末或悬浊液，反之亦然。物料形态没有明确依据时保持 unresolved，而不是选择“化学等价”路线。

## macro step 粒度标尺
每个 macro step 应该像结构化文献抽取中的 `参数列表`：
- 一个 step 是一个可独立交给下游 device adaptation layer 继续拆解的实验段
- `操作` 应是短语级实验动作，例如“配制 NiCo-PBA 前驱体 A 液”“共沉淀制备 NiCo-PBA”“制备电极浆料并涂覆”
- `试剂/对象` 应列出核心试剂、样品或被处理对象，例如“Ni(NO3)2 + sodium citrate”“K3Co(CN)6”“NiCo@A-NiCo-PBA/FTO”
- `参数` 应保留自然语言实验条件，优先包含 mmol、mg、mL、浓度、溶剂比例、时间、温度、电压、参比电极等关键量
- `quantity_requirements` 逐项说明数量语义、来源、可调整性和权限边界；每项必须用
  `material_id` 精确绑定本步已声明的 material port，并带完整结构化 `provenance`。数量语义必须由你
  根据当前用户任务或当前 evidence item 的明确片段判断，不能只根据“取样/称量/干燥”等
  单个关键词分类，也不能从 `参数` 文本正则回填。允许的 `kind` 为
  `scientific_input_setpoint`、`target_dose`、`whole_batch`、`runtime_measured_inventory`；
  `whole_batch` 不填写虚构 value/unit，表示整批直接进入下一操作，只有后续 Skill/科学约束明确要求定量
  取样时才另外增加 `target_dose`。`target_dose` 是目标取用量，不等于整批实际库存读数。
- `source=user_query` 时 provenance.kind 必须为 user，并精确绑定
  `evidence_bundle.query` 的 source_path/excerpt/source_digest；`source=literature` 时 provenance.kind
  必须为 paper，并精确绑定当前 evidence_id 及该 item 的 excerpt 路径/摘录/digest。
  `source=process_semantics` 只可复用上述 user/paper 证据说明明确的整批或待测语义。
  不允许 `agent_proposed`、`workstation_requirement`、裸 `evidence` 字符串或无证据来源升级为
  Research 科学数量；缺少精确绑定时保持相关合同维度 unresolved 并阻断发布。正常生成不得使用
  `manual_revision`；它只允许由修订 CLI 注入。
- 每项还必须给 `owner=research_scientific`、`required_by=research_plan`、
  `device_policy=scientific_review_before_change` 和与来源一致的 `scientifically_fixed`。计划目标、预计值、
  运行时待测值和实际测量值不得互相替代。
- 设备摘要的 `I/O/CReq` 给出容器输入、输出和数量/状态约束；`Ctl` 只是控制设定，`Qout` 是物料
  输出效果，`Report` 才是设备返回的数值。
  `Report=未声明` 时不得把烘干、称量、转移或表征工作站写成会返回实际整批质量/收率。
- 不要输出“围绕 query 进行首轮探索性配方准备”“进一步优化条件”“结合文献细化”这类占位性步骤
- 不要把洗涤、干燥、离心、陈化简单删掉；如果它们在文献中和合成段绑定，可写进同一个 step 的 `参数`
- 通常输出 4-8 个 macro steps；若参考案例有可迁移的 `参数列表`，优先沿用其粒度并按当前 query 做必要改写
- 如果 extracted_protocols 提供了高质量 steps，则 macro_plan 应与其中最相关 protocol 的 steps 保持同源、同参数
- 如果 extracted_protocols 只有零散 PDF 句子、步骤缺少试剂/参数、或多数参数为“文献未说明”，
  可以生成非权威的总体说明，但不得补齐可执行物料事实；V2 相应维度保持 unresolved 并停止发布。

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
      "macro_step_id": "MS_001",
      "步骤序号": 1,
      "操作": "步骤名称",
      "试剂/对象": "对象",
      "参数": "自然语言参数",
      "provenance": {{
        "kind": "paper | user | agent_inferred",
        "reference": "paper 时精确等于当前 evidence item 的 evidence_id；user 时标识当前用户任务",
        "rationale": "agent_inferred 时必填：数值推导理由",
        "source_path": "user 时必须为 evidence_bundle.query；paper 时必须为 evidence_bundle.items[i].excerpt",
        "excerpt": "对应 source_path 字段中的精确片段"
      }},
      "material_inputs": [
        {{
          "material_id": "water",
          "material_instance_id": "water_loaded_batch_01",
          "name": "主动加入的具体材料",
          "state": "solid | liquid | solution | suspension",
          "material_origin": "external_inventory",
          "parent_output_refs": [],
          "logical_container_id": "reaction_container_01",
          "quantity": {{"mode": "exact", "semantic": "planned_target", "value": 5, "unit": "mg"}},
          "provenance": {{"kind": "paper | user", "reference": "paper 时为当前 evidence_id；user 时标识当前任务", "source_path": "paper/user 必填的规范路径", "excerpt": "该路径字段的精确片段"}}
        }}
      ],
      "material_intermediates": [],
      "material_outputs": [
        {{
          "material_id": "product",
          "material_instance_id": "product_batch_01",
          "name": "本步输出物",
          "state": "solution | suspension | solid | other",
          "logical_container_id": "reaction_container_01",
          "quantity": {{"mode": "runtime_measured", "semantic": "runtime_measurement_required"}},
          "provenance": {{"kind": "paper | user", "reference": "授权该输出身份/状态的当前来源", "source_path": "可核验的当前 package 路径", "excerpt": "精确来源片段"}}
        }}
      ],
      "material_relations": [
        {{
          "relation_id": "MR_001",
          "event_kind": "state_change",
          "input_material_instance_ids": ["water_loaded_batch_01"],
          "output_material_instance_ids": ["product_batch_01"],
          "logical_container_ids": ["reaction_container_01"],
          "quantity_basis": "runtime_measurement_required",
          "source_operation_ref": "MS_001/OP_01",
          "provenance": {{"kind":"paper | user","reference":"授权该关系的当前来源","source_path":"可核验的当前 package 路径","excerpt":"精确来源片段"}}
        }}
      ],
      "operation_segments": [
        {{
          "segment_id": "MS_001/OP_01",
          "material_effect": "transform_material",
          "source_operation_ref": "当前证据中授权这段操作的稳定引用",
          "provenance": {{"kind":"paper","reference":"当前 evidence bundle 中的精确 evidence_id","source_path":"evidence_bundle.items[0].excerpt","excerpt":"该 evidence item excerpt 中的精确片段"}}
        }}
      ],
      "material_applicability": [
        {{
          "contract_field": "material_intermediates",
          "assertion": "no_material_intermediates",
          "operation_segment_ids": ["MS_001/OP_01"],
          "provenance": {{"kind":"paper","reference":"当前 evidence bundle 中明确支持该断言的 evidence_id","source_path":"evidence_bundle.items[0].excerpt","excerpt":"明确支持不适用断言的精确片段"}}
        }}
      ],
      "material_contract_status": {{
        "material_inputs": "declared",
        "material_intermediates": "not_applicable",
        "material_outputs": "declared",
        "logical_containers": "declared",
        "material_relations": "declared"
      }},
      "container_requirements": [
        {{"logical_container_id":"reaction_container_01","container_type":"reaction vessel","count":1,"capacity_ml":10,"lid_state":"unknown"}}
      ],
      "quantity_requirements": [
        {{
          "kind": "scientific_input_setpoint | target_dose | whole_batch | runtime_measured_inventory",
          "material_id": "与本步 material port 完全相同的 material_id",
          "material": "该数量对应的物料",
          "value": 5,
          "unit": "mg",
          "source": "user_query | literature | process_semantics",
          "provenance": {{"kind":"user | paper","reference":"当前任务或精确 evidence_id","source_path":"evidence_bundle.query 或 evidence_bundle.items[i].excerpt","excerpt":"对应字段的精确片段"}},
          "adjustability": "fixed | scalable_with_scientific_review | runtime",
          "owner": "research_scientific",
          "required_by": "research_plan",
          "device_policy": "scientific_review_before_change",
          "scientifically_fixed": true
        }}
      ]
    }}
  ],
  "macro_plan_summary": "一句话概括这段 macro plan 在做什么"
}}

上面的 relation 仅展示字段形状，不是通用 state_change 模板；实际 event_kind、实例和关系必须逐步依据获准语义填写。

要求：
- macro_plan 必须是一个步骤数组
- 每一步必须包含 步骤序号、操作、试剂/对象、参数
- 参数保持实验自然语言，不要翻译成 workstation 级动作
- 每一步必须给出 `provenance`。文献或用户直接给定值写 paper/user；自行补全写
  `agent_inferred` 并给出非空 rationale。user 必须写
  `source_path="evidence_bundle.query"`与用户原文的精确 `excerpt`；系统在发布前确认摘录后确定性写入
  该 query 字段的 `source_digest`。仅填“user request”或非空 reference 不算绑定。paper 的
  `reference` 必须精确等于当前 evidence item 的 `evidence_id`，并写出
  `source_path="evidence_bundle.items[i].excerpt"` 及该 excerpt 字段中的精确片段；系统仅在这些字段
  可核验时写入该完整 excerpt 字段的 `source_digest`，不得用题目/DOI 子串模糊匹配。正常生成绝对不得输出 `manual_revision`；该 kind 只能
  由离线修订 CLI 校验 manifest 后注入。可同时保留旧字段 `来源` 供 V1 视图使用。
- `quantity_requirements` 是必需数组。没有数值数量需求但要把产物整批继续处理时，至少输出一条
  `kind=whole_batch`；若一步确实没有物料数量语义，可输出空数组。不得把通用设备摘要中的可配置范围
  端点或示例值抄成当前任务的固定数量。每项必须包含与本步 material port 精确一致的 `material_id`
  以及按上述规则绑定当前 query/evidence item 的完整 provenance；缺失时不得从参数文本回填或升级来源。
- 如果输入包含设备边界上下文，参数应尽量写成下游可判断的固定化学条件，例如固定体积、固定时间、
  固定洗涤次数、固定温度、离线 observation/handoff；不要选择具体机器容器、工作站、容器编号或机器动作。
- 设备边界上下文不能授权改变物料形态、库存边界、反应尺度、路线或终点；当前平台缺少能力时保留
  原始要求并明确 capability blocker。只有当前用户任务或证据已授权时，才可声明外部预配、分批节拍、
  老化方式或具体条件。
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
3. 每一步参数是否包含具体实验条件，例如 mmol、mg、mL、M、h、min、C、V、mA g^-1、overnight、室温、固定加入时间、固定洗涤次数和固定干燥时间等？
4. 是否出现了“围绕 query”“进一步优化”“结合文献细化”“当前缺少”“待补充”等占位表达？
5. 是否错误写成 workstation 级控制指令？
6. 若输入含设备边界上下文，macro_plan 是否避免了必须依赖无支持转移/换瓶的容器连续性硬冲突？
7. 是否只是用“同一兼容路径”这类口头声明掩盖了静置/老化与后续分离/测试之间的容器断链？
8. 是否根据当前 skill 的明确限制判断称量、容器容量、条件式洗涤/干燥与独立静置老化，
   而非一概禁止设备内固体称量、20 mL 以上体系或在线 XRD？并确认任何“边加液边搅拌”语义都能交给
   device agent 转换为分批/间隔节拍，而不是被误判为硬阻塞？
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


V2_MACRO_PLAN_DESIGN_PROMPT = """## 任务名称
V2 macro step design

## 目标
把已确定的单个 macro action 细化为 4-8 个可交给 Device Agent 的化学实验步骤，
从当前起点一直推进到本 stage 的 observation point。

## 当前科学上下文
- 用户 query：{query}
- 调研报告：{survey_report_json}
- 当前证据包的实验信息：{extracted_protocols_json}
- stage 路线：{stage_route_json}
- 当前 stage：{current_stage}
- 路线理由：{stage_route_reason}
- 当前 stage 理由：{current_stage_reason}

## 规则
1. 只设计附加的 macro action 中的一个 experiment_group/sample_id；不并行新增对照组、重复组或未来 stage。
2. 步骤必须覆盖完整样品 lineage，并以可产生真实 observation 的步骤结束。
3. 只有本调用给出的当前证据包可作为 paper 来源。用户任务和当前证据中都找不到的物料、数值、
   路线或终点不得补成具体事实；对应 material contract 维度保持 `unresolved` 并停止发布。
   `agent_inferred` 只可描述步骤总体推理，不能证明物料端口、关系、操作段或不适用断言。
4. 每一项主动投料都必须有数值和单位。禁止“适量”“若干”“按需”。未知产率的整批中间物用
   `all_available`/`whole_batch`，不得虚构库存质量。
5. 保持化学实验语义；不选具体工作站、版本、机器参数、实体容器号或槽位。
   允许声明逻辑容器类型、数量、容量和盖状态要求。
6. 使用上下文中选中 operation contract 的 I/O、容器、科学控制范围和返回字段作为边界。
   工作站没有声明的返回值不得当作自动闭环测量。
7. 设备能力限制不授权 Research 改变物料形态、库存边界、配方体积或加料时序；不得把固体输入改成
   “外部预配且已装载的原液”，也不得擅自缩量或改成分批加液。无法按原证据表达时保留阻断，交给
   明确授权的 Research 修订或人工审查。
8. 只保留用户任务或当前证据明确授权的反应、熟化、分离、洗涤、干燥、表征及终点；不得为了补齐
   一条看似完整的工艺链而新增操作。缺少操作或终点依据时相应维度保持 unresolved。
9. 用户 query 才能作为 kind=user 的来源。上游生成的 macro action、调研报告和 device_context
   只能作为规划上下文或能力边界，不能冒充用户原文。不得把纯空容器拿取、摆放或设备转运单独写成
   Research 化学 macro step；在相关化学步骤的 container_requirements 中声明逻辑容器需求。

## 输出
只输出 JSON object：
{{
  "current_stage_plan": "覆盖 stage 目标、逻辑、变量、预期 observation 和完成条件",
  "macro_plan": [
    {{
      "macro_step_id": "当前 stage 内稳定步骤 ID",
      "步骤序号": 1,
      "操作": "短语级化学操作",
      "试剂/对象": "具体材料或样品",
      "参数": "带数值和单位的实验条件",
      "来源": "精确 evidence_id 或 agent补全",
      "provenance": {{"kind":"paper|user|agent_inferred","reference":"paper 时精确 evidence_id","source_path":"paper 时为 evidence_bundle.items[i].excerpt；user 时为 evidence_bundle.query","excerpt":"对应字段中的精确片段","rationale":"agent_inferred 的推导理由"}},
      "material_inputs": [],
      "material_intermediates": [],
      "material_outputs": [],
      "material_relations": [],
      "operation_segments": [],
      "material_applicability": [],
      "material_contract_status": {{
        "material_inputs":"unresolved",
        "material_intermediates":"unresolved",
        "material_outputs":"unresolved",
        "logical_containers":"unresolved",
        "material_relations":"unresolved"
      }},
      "quantity_requirements": [],
      "container_requirements": [],
      "intermediate_returns": []
    }}
  ],
  "macro_plan_summary": "该步骤组如何推进到 observation point"
}}

每步的 V2 字段精确合同见下方“已先行确定的 macro action 与步骤接口”。
上面的 unresolved 是防止复制示例后误放行的占位状态；实际输出必须逐字段依据当前证据改成
declared 或确实不适用的 not_applicable。任何 unresolved 都会在 Research -> Device 发布前停止。
 paper provenance 必须精确绑定当前 evidence item：reference 等于完整 evidence_id，source_path 使用
 该 item 的 evidence_source_path，excerpt 为其 evidence_excerpt 中的精确片段；没有可核验摘录的
 本地命中不能标成 paper。不得用论文题目、DOI 或短字符串做模糊子串匹配。
 user provenance 必须绑定 evidence_bundle.query 中的用户原文。source_digest 仅由确定性质量检查和
 发布门在上述绑定核验后写入；模型不要生成该字段。
quantity_requirements 每项必须用 material_id 绑定本步 material port，并带同样可核验的 user/paper
provenance；不得从参数中的裸数值、名称子串、设备范围或模型常识回填或升级来源。缺失时保持
相关合同维度 unresolved 并阻断发布；正常生成不得输出 manual_revision。
每项 quantity_requirements 必须包含顶层 kind、material_id、material、value、unit、source、
provenance、owner、required_by、device_policy、scientifically_fixed；不要写成仅有
material_id/port/quantity 的对象。所有 material port 的 quantity 必须显式给出合法的
计划目标、计划估计、整批非数值或运行时待测语义；不得写 null。多输入关系若不能给出有证据的
逐端分配，应保留待测或 unresolved，不得用 whole_batch 掩盖。
quantity_requirements.kind 只能是 scientific_input_setpoint、target_dose、whole_batch、
runtime_measured_inventory 或 semantic_classification_required；planned_target、planning_estimate、
whole_batch_unspecified 和 runtime_measurement_required 只能作为 material port 的 quantity.semantic，
绝不能填入 kind。主动外部投料的计划剂量通常为 scientific_input_setpoint；整批上游输出继续处理
才用 whole_batch。source 只能是 user_query、literature、literature_calculation 或 process_semantics；
不能写 user。owner 必须是 research_scientific，required_by 必须是 research_plan，
device_policy 必须是 scientific_review_before_change。derivation 若需要，必须放在该需求对象顶层，
不得放入 provenance 内；provenance 只负责绑定原始来源，不承载推算字段。
输出前检查：序号连续；每步操作/对象/参数完整；主动投料全部定量；lineage 连续；最后一步到达 observation point。
"""


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
- 必须读取 observation 中的 previous_stage_context、previous_macro_action、prior_paper_hits
- 新 macro_plan 必须保留上一次规划的科学目标、物料形态、库存边界、操作顺序和论文依据；
  device_feasibility_error 本身不授权修改化学路线
- 不要把具体机器容器、工作站、容器编号、原液瓶位、开盖/关盖、分瓶/配平、单步洗涤展开等设备层映射细节写入 macro_plan
- 如果被拒绝项是路线级设备或能力要求，应保留原要求并明确阻断；只有当前用户任务或当前证据包
  已经明确授权替代路线时，才可形成可追溯的 Research 修订。不得自动改成常压、低温、室温老化或外部预配
- 如果被拒绝项只是缺少具体容器、工作站或设备动作表达，应在 macro_plan_summary 中说明“交由 device agent 选择/映射”，不要把它改写成具体设备 workflow
- 若某个目标 observation 无法由当前设备完成，只有原任务/证据已授权离线 handoff 时才保留该边界；
  否则明确阻断，不得自动把在线要求改成离线 observation

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
- 不要重复上一段已经完成且 observation 已确认成功的 macro steps，除非需要复现实验或修复异常
- V2 物料与路线修订必须使用当前用户任务或当前 evidence item 的精确来源绑定；缺少依据的维度保持
  unresolved，不能用“agent补全”放行。"""


DEVICE_ADAPTATION_MACRO_PLAN_DESIGN_PROMPT = """## 任务名称
device-adaptation macro plan design

## 任务目标
根据设备适应层返回的 device_feasibility_error，只在 research layer 的化学语义层面修正 macro_plan。
这一步不是重新定义研究目标，也不是重写 stage route；也不是把实验翻译成机器 workflow。

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
- 不允许改变输入中任何目标材料、材料身份/相态、库存边界或 completion condition，也不得用更容易的
  过程 observation 替代目标 observation。
- 缺少目标 observation 能力时明确阻断；只有原任务或当前证据已授权离线 handoff 时才能保留该边界，
  且不能伪造设备内工作站。
- research layer 的输出是化学语义 macro action：说明应做什么实验、用什么试剂/样品、关键摩尔量/浓度/体积/温度/时间/洗涤次数/目标 observation。
- 不要选择具体机器容器、工作站、容器编号、原液瓶位、开盖/关盖、分瓶/配平、单步纯化动作、机器人转移路径或设备字段；这些属于下游 device agent 的职责。
- 设备反馈只能指出能力缺口，不能授权 Research 修改化学路线；路线级不可执行点必须阻断并等待用户、
  当前证据或显式 Research 修订授权。容器/机器动作缺失仍交由 Device 映射。
- 必须逐字保留上一段 macro plan 中有来源的试剂、物料形态、库存边界、化学计量、尺度、顺序和论文依据；
  不得因设备上限自动缩放或换成外部预配。
- 必须读取 unsupported_reasons、blocking_constraints、unsupported_requested_items，避免再次输出这些不支持项。
- macro_plan 的每一步应是正向可执行命令，不要把“不使用某设备”写成设备动作。
- 不要直接照抄 observation 中被设备层判定不支持的动作、容器、工作站、传感器、闭环判断或在线表征能力。
- 如果最终 observation point 需要当前设备外的能力，只有既有 Research 合同已授权离线 handoff 时才可
  区分设备内步骤与离线边界；否则保持 capability blocker。
- 不得把整个 current stage 都改成 offline_handoff 来规避一个局部错误；只把真源没有任何兼容
  operation 的最小连续化学处理段设为离线边界，其前后仍可执行的合成、加液、反应、分散或表征
  必须保留为正向 macro action，供 Device 产生非空 workflow。
- 设备反馈指出载体或物料状态不兼容时，保持证据中的真实身份并阻断；不得根据 query 是否提及某载体，
  自动把刚性载体改成粉末/悬浊液或反向引入载体。

## 输出要求
只输出 JSON：
{{
  "current_stage_plan": "设备适配后的当前 stage 计划说明；必须保留原 stage 目标和 observation point",
  "macro_plan": [
    {{
      "步骤序号": 1,
      "操作": "步骤名称",
      "试剂/对象": "化学试剂/样品/反应体系对象，不写机器容器或工作站",
      "参数": "化学实验自然语言参数，不写容器编号、工作站字段或机器人动作"
    }}
  ],
  "macro_plan_summary": "一句话说明哪些化学路线级约束被修正；若仅需设备映射，应说明交由 device agent 选择容器/工作站"
}}

要求：
- macro_plan 必须严格属于当前 stage。
- 参数应包含关键实验条件、体积/摩尔量/温度/时间/转速等，不要输出占位性研究建议。
- 参数只能保留原任务或当前证据给出的固定条件；若终点或时间关系需要设备未声明的闭环感知，明确阻断，
  不得自行把连续/同步语义改成分批、间隔或固定转速。
- 参数不要写“进样瓶编号”“原液编号”“工作站”“液体进样站”“纯化工作站”“烘干机”等机器执行字段，除非这些词来自原始化学对象且确实是研究目标的一部分。
- 最终 observation 若设备不可执行且原合同未授权离线 handoff，应保持阻断。
- 所有物料、路线和操作修改必须绑定当前用户任务或当前 evidence item 的精确来源；缺少依据时保持
  unresolved，不得标注“agent补全(设备适配改写)”后放行。"""


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
  以及哪些内容应交由下游 device agent 做容器/工作站映射"""


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
  仅把当前 stage 内部 macro_plan 中化学路线级不可执行部分改写为可由设备层进一步适配的化学语义路线；
  若可以，repairable 应为 true。
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
