# TuringBrain / chemagent — 自主化学实验规划系统

> **Observation-Gated Macro-Action Planning and Workflow Evolution for Autonomous Chemical Experimentation**
>
> 快速上手（含流程图）请看 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 这是什么

一个面向自动化化学实验室的**闭环科研规划系统**。核心思想是在"全流程盲跑"和"逐步短视决策"之间建立一个有原则的中间规划粒度：

- **observation point（观测点）**：能更新科学判断的关键检测/表征节点（XRD、颜色变化、产率、电化学响应……）
- **stage**：从当前状态到下一个观测点之间的一整段科学计划——stage 的边界只能由观测点决定，不能按工艺步骤切分
- **macro action**：stage 内推进到下一次 observation 的整段实验，先规划目标、操作序列和完成条件
- **macro step**：在已确定的 macro action 下细化的实验步骤，包含试剂、条件、物料 I/O 和逻辑容器需求
- **campaign**：一次完整的科研任务（从目标输入到达成/终止），系统的文献、计划版本、实验记忆都以 campaign 为单位组织

每次拿到真实观测结果后，系统重新评估并**演化**后续计划——每个计划版本的产生、修改与放弃都带原因落盘，可审计、可追溯。

## 系统组成（三层 + 资源层）

```
用户目标 + 参考文献
      │
      ▼
┌─ Campaign 编排层 orchestrator/ + run_campaign.py ─────────────┐
│  自动循环驱动 · 执行边界适配器 · 终止判断 · 台账/报告落盘        │
│   ┌──────────────┐      ┌──────────────┐      ┌────────────┐ │
│   │  研究层       │ ───▶ │  设备适配层    │ ───▶ │  执行边界   │ │
│   │ reaserch_    │ ◀─── │ device_agent/ │ ◀─── │ (真实下发   │ │
│   │ agent/       │ 反馈  │              │ 结果  │  默认阻断)  │ │
│   └──────────────┘      └──────────────┘      └────────────┘ │
└───────────────────────────────────────────────────────────────┘
      ▲
      └── chem_resources/（工作站真源、格式契约、技能包）
```

### 1. 研究层 `reaserch_agent/` — 只回答"做什么实验"

手写分支状态机（非 LangGraph）：

- **B1 bootstrap（冷启动）**：统一联网 skill 获取文献 → 多轮调研 → protocol 抽取 → 调研报告 → 按观测点设计 stage 路线 → 独立 macro action 规划 → macro steps 细化（带质量门重试）
- **B2 post_observation（热循环）**：判断新观测是否落在当前 stage 上
  - **正常** → 推进 stage / 收束，出下一段 macro plan
  - **异常** → 增量调研 + **三层最小修复阶梯**（改 stage 内计划 → 换当前 stage → 重写整条路线 → 否则人工交接）
  - **设备可行性打回** → 保留科学目标，只重写路线级不可执行的化学表达；历史阻塞约束**累计传递**（`cumulative_device_constraints`），失败方案登记**签名**（`plan_v1/v2…`）防重复生成，两层共享 `device_snapshot_id`
- **观察点驱动的宏动作层级**：`观察点 → macro action → 设备步骤` 三级。每个 macro plan 是一个结构化 macro action（`MA_S<stage>_R<round>`,含 objective / completion_condition / expected_observation），一个 stage 可容纳多个相继的 macro action;B2 收到观测先按 macro action 记录 outcome（completed/needs_repair/device_rejected）再决策
- 输出严格保持**化学语义**：不选工作站、容器编号、机器动作（那是设备层的职责）;设备边界疑问只给对应步骤打 `needs_device_validation` 标记,**不清空整份计划**
- 失败显式四分类 `failure_category`（macro_generation / macro_quality / device_feasibility / network_or_retrieval）——空 macro plan 不再是黑盒
- 研究层规划步骤提供 `LLM 路径 + 确定性启发式路径`（`--disable-llm` 可让研究层全离线运行）;protocol 抽取失败**降级续跑**而非中止（计划各步诚实标注 `agent补全`）

### 2. 设备适配层 `device_agent/` — 只回答"怎么在本实验室做"

两段设备规划与翻译，加上确定性防线，把 macro plan 映射为机器工作流：

- 产出 `workflow_txt`（仿 `chem_resources/format_reference/reference.txt`）与 `workflow_json` 两个工作流视图
- 自动补全容器选择、瓶位、开关盖、离心配平、洗涤子步骤等映射细节
- **可行性三桶分类**（`feasibility_rules.py`）：每条 `device_feasibility_error` 约束按文本证据分入 **hard**（缺站/无转移路径/容器不兼容/体积超限/离线——才允许 `physical_infeasible`）/ **adaptable**（"边滴入边搅拌"等时序语义 → 一次适配重试实现为交错批次，`requires_scientific_review`）/ **unverifiable**（默认；无同名下发字段、无法证明等价 → `needs_human_review`）。设备未开放的固定参数（Cu Kα、扫描范围等）**绝不构成不可行理由**
- **严格下发校验**（`workflow_validator.py`）：45 个工作站 SKILL 参数表解析为机器 schema（必填/类型/枚举/范围/单位感知），成功输出先经**确定性必填补全**（`dispatch_formatter.complete_required_fields`：容器数量=len(容器编号)、开关盖编号、SKILL 默认值如保留瓶盖=1——只补缺失、绝不覆盖模型已写值、不合成结构字段）再校验；失败给一轮 LLM 自修复（补全同样作用于修复轮输出），仍失败降级 `status=failed / failure_stage=dispatch_validation`——**不合规绝不以 success 返回**
- **平台形式下发层**（`dispatch_formatter.py`）：success 包附加 `dispatch_payload`，以平台自身 schema 导出（`0410数据转换.txt`）为真源确定性转换为平台准确形式（平台站名/操作名/按版本参数名/类型强制/`N号原液瓶` 实例化/工作站数字 id/generate.py 信封），与语义层 `workflow_json` 显式分离；转换产物必须通过 generate.py 自身校验（测试法则）
- 每个设备步骤携带 `source_macro_step` / `macro_action_id` / `observation_point_id`，可溯源到它服务的观察点；success/error 包均带 `device_snapshot_id`（真源快照哈希，与研究层共享）
- 设备层没有 heuristic 模式，即使研究层使用 `--disable-llm`，设备映射仍需要可用 LLM

### 3. Campaign 编排层 `orchestrator/` + `run_campaign.py` — 自动闭环

- 以子进程驱动两层 CLI，产物按轮落盘 `campaigns/<campaign_id>/iteration_XX/`
- **执行边界适配器**（真实下发链路保持安全阻断）：
  | 适配器 | 行为 |
  |---|---|
  | `mock` | 仿真结果（测试/演示） |
  | `manual` | 等待人工把结果写入 `observation_in.json` |
  | `listen` | 起 HTTP 端口，`POST /observation` 接收机器回传结果 |
  | `real` | **有意阻断**（与 `dispatch_guard` 一致），接通需人工授权 |
- **终止条件**：目标达成（stage 路线收束、macro plan 清空）/ 人工接管 / 可行性死锁（连续 N 次设备错误，默认 3）/ 最大轮数
- 结束产出 `final_report.md`（停止原因 + 计划版本演化表 + 迭代轨迹）与 `campaign_summary.json`

## 关键机制

### 分层 Skill 与原生工具接口

45 个工作站 SKILL 保持独立、作为设备事实真源；`chem_resources/agent-skills/` 中新增四个应用 skill：

| Skill | 加载时机 | 设备信息粒度 |
| --- | --- | --- |
| `online-research` | 首次调研、异常补检、模型临时补查 | 一个任务式入口，内部统一网页/论文搜索、阅读、按需 OA 下载 |
| `experiment-capabilities` | 检索和 stage 设计 | 实验类型及适用边界，不含操作 I/O |
| `operation-capabilities` | macro action 规划 | 操作及用途，不含设备 I/O |
| `macro-step-capabilities` | macro step 细化 | 物料 I/O、容器、科学条件、前置约束及返回信息 |

设备投影由 `chem_resources/generate_workstation_capability_index.py` 同源生成，并绑定来源摘要。未声明的测量返回/中间反馈保持 `unknown`，不推断为不存在，也不将设定量当作测量结果。检查生成物：

```bash
python3 -B chem_resources/generate_workstation_capability_index.py --check --with-skill-references
```

步骤的 `intermediate_returns` 若标记 `declared`，必须以 `source.station_code`、`source.operation` 和 `feedback_kind` 引用真源中已声明的返回字段与时点。未声明但实验必需的读数必须指定 `delivery_mode=observation/manual_handoff` 和 `wait_for`，不能伪装成自动回传。

若等待点后仍有机器执行步骤，Device 会阻断跨边界的签证和下发，并保留原计划、返回需求及人工交接说明；不能用 Device override 代替真实 observation。先按观测边界拆分、取得真实返回后，再由 Research 生成下一段宏动作。

运行接口使用 LangChain `StructuredTool` + Pydantic schema：

```python
# service 由 workflow 创建，设备能力、campaign、provider 配置由应用注入。
tool = service.as_tool()  # name == "online_research"
result = tool.invoke({
    "query": "目标材料的合成与 XRD 表征",
    "objective": "查找可复现的实验步骤",
    "references": [],
    "evidence_depth": "full_text",  # 仍受应用的下载开关约束
})
print(tool.args_schema.model_json_schema())
```

真实模型调用由 `agent_skills/native_tools.py` 使用 `bind_tools`、`AIMessage.tool_calls` 和关联 call ID 的 `ToolMessage` 执行有界循环；不再执行文本中的 `tool_request`。普通无工具模型调用保留原接口，需要工具的后端必须支持原生调用，不能静默回退 CLI。

Device 先读取 45 站能力目录，再用 `load_workstation_skill(station_code)` 读取选中站完整 SKILL、audit 和机器合同；第一段分配工作站/容器/槽位，第二段翻译机器参数。模型按需加载不影响确定性审计的全目录覆盖。能力标签仅用于文献筛选，不能替代 Device 的执行安全门。

生成后的 workflow 与实际下发 payload 使用
[workflow-checker Skill](chem_resources/lab-design-all/skills/workflow-checker/SKILL.md)
进行离线检查，入口为 `chem_resources/lab-design-all/skills/workflow-checker/scripts/check.py`。
它与 Campaign 执行前检查共享确定性引擎，按步骤/嵌套参数输出 JSON 和 Markdown 报告；
不调用模型或设备，检查失败或无法验证时不能放行。它属于 Device 下发边界，不是新增的
Research 能力投影层。

### 计划版本台账（每次修改/放弃都落盘 + 原因）

`reaserch_agent/plan_ledger.py` → `campaigns/<id>/plan_versions.jsonl`，每轮一条：

```json
{"plan_version": 2, "event": "revised", "scope": "macro_plan",
 "trigger": "device_feasibility_error", "branch_path": "B2/device_adaptation",
 "reason": "observation stage fit: abnormal | 设备阻塞约束: 缺少反应釜…",
 "previous_plan": {...}, "new_plan": {...}, "evidence_refs": [...]}
```

事件类型：`initial / advanced / revised / closure / abandoned`；原因来自 fit judge、三层修复评估、设备阻塞约束——不新造、只归档。

### 文献自获取(种子 → 滚雪球 → 验证 → campaign 归档)

`tools/literature_acquisition.py` + `tools/paper_registry.py` + `tools/web_search.py` + `tools/paper_download.py`：

文献获取在存在待解析的 `--reference`、显式使用 `--online-literature`，或开启 `--web-search` 时运行；无 reference 且未开启这些选项时，B1 直接使用本地知识库。

1. `--reference` 支持本地 PDF/TXT/MD/JSON、DOI、arXiv id、论文标题（可重复）
2. 种子解析自动降级：DOI 走 Crossref → Semantic Scholar → OpenAlex；arXiv 走 arXiv → Semantic Scholar → OpenAlex；标题跨 S2/OpenAlex/Crossref/arXiv 匹配
3. **引文滚雪球**（深度 1、有界）+ 关键词检索线，确定性相关性过滤
4. **检索词纪律（issue #1/#8）**：出站查询先经 `query_sanitizer` 双层清洗——整句删除任务下发从句（"请将实验下发至…并返回任务 id"）+ 词级删除自动化/工作站/实验室编号语义,化学实体按构造保留,原始用户 Query 永不修改;关键词线**逐条消费** Research 生成的短 survey queries（≤4 条独立检索,长 Query 仅作回退）,实发查询记录于 `actual_scholarly_queries` 供审计
5. **材料+反应联合硬门槛**（`chemistry_gate`）：候选论文须同时命中锚文本的材料体系（元素符号防误配正则 + 中英文名 + PBA/LDH/MOF 类词）与目标反应/观测（16 组跨语言同义词,OER↔析氧）,仅命中"碱性/乙醇/氧化"等弱词的论文被拒并记录理由;锚缺词时门槛自动失效
6. **零命中有界修复**：首轮 0 保留时最多 2 轮确定性重试（去表征词 → 拉丁核心词）,逐轮记录;检索结果三态归因 `retrieval_status ∈ success / no_relevant_papers / provider_failure`（429/403/超时 = provider_failure,与"关键词不相关"分开）,另有 `protocol_status` / `planning_status`
7. 候选经 DOI/arXiv/S2 **验证打标**后进 `chem_kb/registry/papers.jsonl`；学术、Web、本地文件按身份命名空间隔离，同标题仅在年份/首作者兼容且强标识不冲突时合并
8. `--download-pdfs` 走 arXiv → Semantic Scholar OA → Unpaywall → CORE → 元数据 URL → Web PDF 线；校验 Content-Type、大小、`%PDF-` 文件头和可提取正文，损坏/扫描件会继续切换下一来源
9. 网络失败一律降级为日志告警，保留逐供应商 attempt trail；registry 使用文件锁和原子替换，支持并发 campaign，**绝不中断规划分支**

**数据来源全景**（全部 stdlib urllib、密钥走环境变量；学术检索多源聚合，解析/Web/PDF 按序降级）：

| 类别 | 来源 | 凭据 | 默认 |
|---|---|---|---|
| 学术 API | Semantic Scholar / Crossref / arXiv / **OpenAlex** | 免钥（S2 可加 `SEMANTIC_SCHOLAR_API_KEY`；mailto 礼貌参数） | ✅ 关键词线默认 |
| 学术 API | Google Scholar（经 Serper `/scholar`） | `SERPER_API_KEY` | 有钥自动加入 |
| 学术 API | PubMed（NCBI eutils） | 免钥（可加 `NCBI_API_KEY`） | 按需（`--sources pubmed`） |
| 引文图谱 | S2 references/citations 滚雪球 | 同 S2 | ✅ 有种子即用 |
| **开放 PDF** | arXiv / S2 OA / Unpaywall / CORE / 元数据 URL / Web PDF | `UNPAYWALL_EMAIL` / `CORE_API_KEY` 可选 | `--download-pdfs` 开启 |
| **通用 Web** | Tavily → Serper(Google) → Brave → SearXNG → DuckDuckGo（按已配置凭据自动降级链） | `TAVILY_API_KEY` / `SERPER_API_KEY` / `BRAVE_API_KEY` / `SEARXNG_BASE_URL`（DDG 免钥兜底） | `--web-search` 显式开启 |
| 网页阅读 | Jina Reader（`r.jina.ai`）→ 直连抓取+去标签 | 可选 `JINA_API_KEY` | 随 web 线 |

**Web 结果 ≠ 证据**：网页内容入库一律标 `web_unverified` / `source: web:<engine>`，只作线索；实验参数级引用只接受已成功解析全文的 DOI、arXiv、Semantic Scholar 验证记录或本地文件（evidence packet 会把其他来源列进 known_gaps）。

**模型可主动调用的外部工具**（`tools/web_tool.py`，LLM 模式）：`--web-search` 开放 `web_search` / `web_read`，`--online-literature` 开放 `paper_search`，同时加 `--download-pdfs` 才开放 `paper_download`。工具结果会回喂同一步骤；论文按强标识优先去重，PDF 只尝试开放访问来源，已解析文件默认命中缓存。每任务上限由 `RESEARCH_EXTERNAL_TOOL_MAX_ROUNDS` 控制（默认 2，兼容旧变量）；所有外部正文均按不可信数据处理，每次调用及供应商 attempts 写入 state/registry 审计。

### 记忆与检索（自研、确定性、无向量依赖）

- 两层 SQLite 记忆（LITERATURE / EXPERIMENT），campaign/stage 元数据即层级树
- 使用 `--enable-memory` 时，每轮计划事件写**轨迹节点**（观测、决策理由、修复路径、evidence_refs）；stage 切换/收束写确定性 rollup 摘要
- **三层召回**注入主要研究规划任务的 LLM 上下文（字符硬上限 → 上下文有界）：
  1. 路径摘要（总读，≤2000 字）
  2. 当前 stage 最近 3 个节点全文（≤3000 字）
  3. 跨 campaign 相似案例（仅异常/设备错误触发，带时效标签，≤1500 字）
- 检索打分：化学式保全分词（`K3Fe(CN)6` 不被拆碎）+ 可选 jieba/BM25 增强（未安装自动回退）

### 证据级溯源（反幻觉）

- PDF 抽取带 `[p.N]` 页码标记，protocol 步骤带 `page` 与 `evidence` 原句
- 抽取的 protocol 自动挂载注册表身份（`paper_id` / `verification_status` / `full_text_status`）
- 每个 macro step 附加**加法式**字段 `来源`：LLM 引用 → 保守匹配（≥0.5 重叠才归因，带页码）→ 否则诚实标注 `agent补全(未直接引用文献)`——**绝不虚构引用**
- `evidence_packet`（来源清单 + 已知缺口 + 引用规则）注入主要研究规划任务；`evidence_refs` 进台账与启用后的记忆节点

### 设备可用性动态接入

`--device-status-json` 支持 `{"站名": "offline|busy|available"}` 或 `{"stations": {...}}`。设备层会直接应用状态；研究层还需同时提供 `--include-device-context`、`--device-workstations-dir` 或 `--device-context-json`，才能把状态叠加到设备能力上下文并避开不可用设备。

### 用户可读统一结果（human_readable_result.json）

`orchestrator/human_readable.py` 只读抽取器把散落的原始产物整理为单文件结果：逐字符原始 Query、`run_status` + `failure_category`、论文清单（含 `used_in_plan`）、S01/M01/D01 稳定 ID 的 Stage/Macro/Device 三层、每个设备步骤到宏步骤的溯源、结构化证据标记（`source_type: paper_protocol | agent_generated` + `requires_review`）、平台形式 `dispatch_plan`（与规划格式分开展示）、中文阻塞原因与最终实验方案。campaign 每轮 iteration 与终态各写一份;单独跑设备层用 `--human-readable-output`。**只读抽取,绝不覆盖原始产物**。

### 科学审查门（requires_scientific_review）

设备层近似执行（如时序语义改为交错批次）的 success 包携带 `requires_scientific_review`;编排器在**跨真实边界的适配器**（manual/listen/real）执行前阻断,写 `AWAITING_SCIENTIFIC_REVIEW.md` 并以 `scientific_review_required`（退出码 7）停止;人工在迭代目录写 `review_approval.json`（`{"approved": true, "approver": ...}`）放行。mock 适配器直接执行但审查记录入 trace。设备 `needs_human_review` 错误停为 `manual_required`（`AWAITING_CONDITION_REVIEW.md`）,不消耗可行性死锁计数。

## 安全边界

- **真实实验下发链路被硬阻断**：`chem_resources/lab-design-*/skills/lab-operation/scripts/dispatch_guard.py` 使 `generate_task.py`/`start_task.py` 只返回 blocked，不接触云网关
- orchestrator 的 `real` 适配器同样拒绝执行；接通真实执行需要人工显式授权与实现
- 闭环可用 `mock`/`manual`/`listen` 完整运行，真实结果通过网络端口回传

## 安装与配置

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r device_agent/requirements.txt
cp .env.example .env
```

LLM 配置读根目录 `.env`。研究层按 `REFINER_LLM_*` → `GEMINI_*` → `OPENAI_*` 回退；设备层只读取 `REFINER_LLM_*`。OpenAI 兼容后端由已锁定依赖支持；Gemini 仅研究层可用，需额外安装 `langchain-google-genai`；`codex_responses` 还要求本机存在 `codex` CLI 和 endpoint URL。

其他常用环境变量：`SEMANTIC_SCHOLAR_API_KEY`（兼容 `S2_API_KEY`）、`CROSSREF_MAILTO`、`OPENALEX_MAILTO`、`NCBI_API_KEY`（学术 API）；`UNPAYWALL_EMAIL`、`CORE_API_KEY`（PDF）；`TAVILY_API_KEY`（可用逗号配置多个 Key 并轮换）、`SERPER_API_KEY`、`BRAVE_API_KEY`、`SEARXNG_BASE_URL`、`JINA_API_KEY`（Web 搜索/阅读）。完整占位配置见 `.env.example`；运行开关还有 `RESEARCH_ONLINE_LITERATURE`、`RESEARCH_WEB_SEARCH` 和 `RESEARCH_EXTERNAL_TOOL_MAX_ROUNDS`。

`.env` 只用于本机运行并已被 Git 忽略；仓库只提交无真实值的 `.env.example`。在线模式会把检索词、DOI/arXiv 标识和目标 URL 发送给所选第三方服务，启用 Jina Reader 时目标 URL 也会发送给 Jina。

模型发起的网页/PDF URL 会在请求和每次重定向前校验 DNS，拒绝 localhost、私网及链路本地地址；跨域重定向会移除认证头，响应体和 PDF 大小都有硬上限。

## Web 测试控制台（前后端分离）

`backend/` 是现有 CLI 之上的 FastAPI 适配层，负责异步子进程、SQLite 任务状态、SSE、取消、参考资料上传和 manual observation 回传；`frontend/` 是 React + TypeScript 操作台。Agent 代码仍通过 CLI 隔离运行，后续更新 Agent 时无需把运行时导入 HTTP worker。

```bash
# 首次安装
./.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend && npm install && cd ..

# 终端 1：后端（保持单 worker）
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8000

# 终端 2：前端
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173
```

浏览器打开 `http://127.0.0.1:5173`。`Research Preview` 不需要 LLM Key，且默认关闭外部检索；开启 Online literature、Web Search 或 Download PDFs 后会访问外部服务，免钥源仍可工作，配置对应 Key 可提高稳定性。`Full Campaign` 会运行设备映射与闭环编排，需要配置设备层 LLM。Web API 没有内置鉴权，只开放 `mock` / `manual`，应保持绑定 `127.0.0.1`；真实实验下发继续阻断。任务知识库与运行数据保存在已忽略的 `backend/data/`。

## 测试

```bash
python -m unittest discover -s reaserch_agent -t . -p "test*.py"   # 研究层
python -m unittest discover -s orchestrator  -t . -p "test*.py"   # 编排层
python -m unittest discover -s agent_skills  -t . -p "test*.py"   # 同源能力投影与分层
python -m pytest device_agent -q                                # 含原生工具、渐进加载和安全门
python device_agent/test_single_agent.py                          # 设备层（需已装依赖）
./.venv/bin/python -m pytest backend/test_api.py                   # 后端
cd frontend && npm test && npm run lint && npm run build          # 前端
# 可选浏览器回归：PLAYWRIGHT_CHANNEL=chrome npm run test:e2e
```

约定：普通调用用 `.invoke()` 假模型；工具测试使用 `.bind_tools()`、原生 `AIMessage.tool_calls` 和 `ToolMessage`，网络客户端注入 fake，不触真实端点；研究层的 `--disable-llm` 路径无凭据可跑。

## 八题评估包（chem-agent-eval-package/）

可独立分发的固定八题（A01–D02,`测试题目.docx`）评估工具:`skill/chem-agent-eval-sop/` 为 Codex 评估 Skill（SOP + 离散判定 rubric:process_completion / paper_quality / plan_workstation_match / dispatch_schema_match,不打分）;`scripts/run_suite.py` 并发跑八题（每题独立 ID 与知识库,Query sha256 校验,Device 异常自动重试一次且两次产物都保留）;`scripts/preflight.py` 运行前自检;`scripts/install.py` 安装到 `~/.codex/skills/`。只做规划与格式校验,**不真实下发**。

`result/` 内已入库四次完整评测运行供复核:`chem-agent-eval-20260718-161647`（修复前基线,8/8 manual_required）与 `164330/181702/193746`（修复后三次,8/8 Research completed、6–8 步 macro plan;Device 层瓶颈证据与校验错误语料）。

## 目录速览

```
run_campaign.py            # 闭环入口
orchestrator/              # 编排器 + 执行边界适配器 + human_readable 抽取器
backend/                   # FastAPI、任务进程、SSE 与持久化适配层
frontend/                  # React + TypeScript 测试控制台
reaserch_agent/            # 研究层（目录名拼写是有意保留的）
  workflow.py              #   B0/B1/B2 状态机
  plan_ledger.py           #   计划版本台账
  tools/                   #   检索/摄取/文献获取（含 chemistry_gate）/query_sanitizer/注册表/设备上下文
  memory/                  #   两层记忆 + 三层召回 + 打分
  chem_kb/                 #   本地知识库（语料 + registry）
agent_skills/              # 同源能力投影与有界原生工具调用（无设备执行）
device_agent/              # 设备适配层（单 agent 映射 + 三层确定性防线）
  feasibility_rules.py     #   可行性三桶分类（hard/adaptable/unverifiable）
  workflow_validator.py    #   SKILL schema 严格校验（必填/类型/枚举/范围）
  dispatch_formatter.py    #   平台形式转换 + 确定性必填补全
chem_resources/            # 工作站真源、格式契约、lab-operation 技能（含 dispatch_guard）
chem-agent-eval-package/   # 可分发八题评估包（SOP skill + rubric + runner）
result/                    # 已入库的评测运行产物（1 基线 + 3 修复后）
campaigns/                 # 运行产物（gitignored）
chem-eval/  docs/          # 设计/评测文档、调研与快速上手（docs/代码审查.md 为整改日志）
```

## 重要约定

- **交接载荷中的中文键是数据契约**（`待执行 macro plan`、`当前 stage`、`原液量`…），不得改名/翻译
- `reaserch_agent` 目录名拼写**有意保留**，除非同步迁移全部 import
- `state.macro_action` 保存先行规划的 `planned_operations`、目标和完成条件；`pending_macro_action` 仅用于发布前暂存，避免覆盖上一批次的 observation outcome
- 宏步骤仍位于 `state.macro_plan`，保留 `{步骤序号, 操作, 试剂/对象, 参数[, 来源]}`，并支持 `material_inputs`、`material_outputs`、`container_requirements`、`intermediate_returns`；实际工作站、瓶号与槽位由 Device 分配
- 完整设计文档：`chem-eval/chem-technical-report.md`；研究层状态机全谱：`reaserch_agent/research_layer.md`（B3–B7 分支已设计、暂未实现）
