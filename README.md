# TuringBrain / chemagent — 自主化学实验规划系统

> **Observation-Gated Macro-Action Planning and Workflow Evolution for Autonomous Chemical Experimentation**
>
> 快速上手（含流程图）请看 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 这是什么

一个面向自动化化学实验室的**闭环科研规划系统**。核心思想是在"全流程盲跑"和"逐步短视决策"之间建立一个有原则的中间规划粒度：

- **observation point（观测点）**：能更新科学判断的关键检测/表征节点（XRD、颜色变化、产率、电化学响应……）
- **stage**：从当前状态到下一个观测点之间的一整段科学计划——stage 的边界只能由观测点决定，不能按工艺步骤切分
- **macro action**：stage 内推进到目标观测点的具体执行步骤段（含操作、试剂/对象、关键参数）
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

- **B1 bootstrap（冷启动）**：按需获取文献 → 多轮调研 → protocol 抽取 → 调研报告 → 按观测点设计 stage 路线 → 生成首个 macro plan（带质量门重试）
- **B2 post_observation（热循环）**：判断新观测是否落在当前 stage 上
  - **正常** → 推进 stage / 收束，出下一段 macro plan
  - **异常** → 增量调研 + **三层最小修复阶梯**（改 stage 内计划 → 换当前 stage → 重写整条路线 → 否则人工交接）
  - **设备可行性打回** → 保留科学目标，只重写路线级不可执行的化学表达
- 输出严格保持**化学语义**：不选工作站、容器编号、机器动作（那是设备层的职责）
- 研究层规划步骤提供 `LLM 路径 + 确定性启发式路径`（`--disable-llm` 可让研究层全离线运行）

### 2. 设备适配层 `device_agent/` — 只回答"怎么在本实验室做"

单次 LLM 调用，把 macro plan 映射为机器可执行工作流：

- 产出 `workflow_txt`（仿 `chem_resources/format_reference/reference.txt`）与 `workflow_json` 两个工作流视图
- 自动补全容器选择、瓶位、开关盖、离心配平、洗涤子步骤等映射细节
- 模型判定必要化学动作无法由设备真源实现时，返回 `device_feasibility_error`（附阻塞约束与修订建议），回流研究层 B2
- Prompt 要求模型返回 `device_self_check`；当前实现会拒绝显式包含 `fail/失败/不通过` 的自检，但只做基础结构检查，尚未确定性验证工作站参数或 TXT/JSON 一致性
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
4. 候选经 DOI/arXiv/S2 **验证打标**后进 `chem_kb/registry/papers.jsonl`；学术、Web、本地文件按身份命名空间隔离，同标题仅在年份/首作者兼容且强标识不冲突时合并
5. `--download-pdfs` 走 arXiv → Semantic Scholar OA → Unpaywall → CORE → 元数据 URL → Web PDF 线；校验 Content-Type、大小、`%PDF-` 文件头和可提取正文，损坏/扫描件会继续切换下一来源
6. 网络失败一律降级为日志告警，保留逐供应商 attempt trail；registry 使用文件锁和原子替换，支持并发 campaign，**绝不中断规划分支**

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
python device_agent/test_single_agent.py                          # 设备层（需已装依赖）
./.venv/bin/python -m pytest backend/test_api.py                   # 后端
cd frontend && npm test && npm run lint && npm run build          # 前端
# 可选浏览器回归：PLAYWRIGHT_CHANNEL=chrome npm run test:e2e
```

约定：测试用暴露 `.invoke()` 的假模型 mock LLM，网络客户端注入 fake，不触真实端点；研究层的 `--disable-llm` 路径无凭据可跑。

## 目录速览

```
run_campaign.py            # 闭环入口
orchestrator/              # 编排器 + 执行边界适配器
backend/                   # FastAPI、任务进程、SSE 与持久化适配层
frontend/                  # React + TypeScript 测试控制台
reaserch_agent/            # 研究层（目录名拼写是有意保留的）
  workflow.py              #   B0/B1/B2 状态机
  plan_ledger.py           #   计划版本台账
  tools/                   #   检索/摄取/文献获取/注册表/设备上下文
  memory/                  #   两层记忆 + 三层召回 + 打分
  chem_kb/                 #   本地知识库（语料 + registry）
device_agent/              # 设备适配层（单 agent 映射）
chem_resources/            # 工作站真源、格式契约、lab-operation 技能（含 dispatch_guard）
campaigns/                 # 运行产物（gitignored）
chem-eval/  docs/          # 设计/评测文档、调研与快速上手
```

## 重要约定

- **交接载荷中的中文键是数据契约**（`待执行 macro plan`、`当前 stage`、`原液量`…），不得改名/翻译
- `reaserch_agent` 目录名拼写**有意保留**，除非同步迁移全部 import
- 宏动作步骤位于 `state.macro_plan`，每步 `{步骤序号, 操作, 试剂/对象, 参数[, 来源]}`（`来源` 为加法式可选键）
- 完整设计文档：`chem-eval/chem-technical-report.md`；研究层状态机全谱：`reaserch_agent/research_layer.md`（B3–B7 分支已设计、暂未实现）
