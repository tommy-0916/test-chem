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

- **B1 bootstrap（冷启动）**：文献获取 → 多轮调研 → protocol 抽取 → 调研报告 → 按观测点设计 stage 路线 → 生成首个 macro plan（带质量门重试）
- **B2 post_observation（热循环）**：判断新观测是否落在当前 stage 上
  - **正常** → 推进 stage / 收束，出下一段 macro plan
  - **异常** → 增量调研 + **三层最小修复阶梯**（改 stage 内计划 → 换当前 stage → 重写整条路线 → 否则人工交接）
  - **设备可行性打回** → 保留科学目标，只重写路线级不可执行的化学表达
- 输出严格保持**化学语义**：不选工作站、容器编号、机器动作（那是设备层的职责）
- 每个规划步骤都有 `LLM 路径 + 确定性启发式路径`（`--disable-llm` 全离线可跑）

### 2. 设备适配层 `device_agent/` — 只回答"怎么在本实验室做"

单次 LLM 调用，把 macro plan 映射为机器可执行工作流：

- 产出 `workflow_txt`（仿 `chem_resources/format_reference/reference.txt`）与 `workflow_json` 两个一致视图
- 自动补全容器选择、瓶位、开关盖、离心配平、洗涤子步骤等映射细节
- **只有**当设备真源无法实现某个必要化学动作时才返回 `device_feasibility_error`（附阻塞约束与修订建议），回流研究层 B2
- 成功输出前强制 `device_self_check`，自检不过不得返回 success

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

`tools/literature_acquisition.py` + `tools/paper_registry.py` + `tools/web_search.py`：

1. `--reference` 支持本地 PDF/TXT/MD/JSON、DOI、arXiv id、论文标题（可重复）
2. 种子解析：Crossref(DOI) / arXiv / Semantic Scholar+Crossref(标题匹配)
3. **引文滚雪球**（深度 1、有界）+ 关键词检索线，确定性相关性过滤
4. 全部候选经 DOI/arXiv **验证打标**后进 `chem_kb/registry/papers.jsonl`（DOI→arXiv→标题归一去重；同一论文多 campaign 复用只追加标签）
5. 网络失败一律降级为日志告警，**绝不中断规划分支**

**数据来源全景**（全部 stdlib urllib、密钥走环境变量、逐源降级）：

| 类别 | 来源 | 凭据 | 默认 |
|---|---|---|---|
| 学术 API | Semantic Scholar / Crossref / arXiv / **OpenAlex** | 免钥（S2 可加 `SEMANTIC_SCHOLAR_API_KEY`；mailto 礼貌参数） | ✅ 关键词线默认 |
| 学术 API | Google Scholar（经 Serper `/scholar`） | `SERPER_API_KEY` | 有钥自动加入 |
| 学术 API | PubMed（NCBI eutils） | 免钥（可加 `NCBI_API_KEY`） | 按需（`--sources pubmed`） |
| 引文图谱 | S2 references/citations 滚雪球 | 同 S2 | ✅ 有种子即用 |
| **通用 Web** | Tavily → Serper(Google) → Brave → SearXNG → DuckDuckGo（按已配置凭据自动降级链） | `TAVILY_API_KEY` / `SERPER_API_KEY` / `BRAVE_API_KEY` / `SEARXNG_BASE_URL`（DDG 免钥兜底） | `--web-search` 显式开启 |
| 网页阅读 | Jina Reader（`r.jina.ai`）→ 直连抓取+去标签 | 可选 `JINA_API_KEY` | 随 web 线 |

**Web 结果 ≠ 证据**：网页内容入库一律标 `web_unverified` / `source: web:<engine>`，只作线索；实验参数级引用仍须落在 DOI/arXiv 验证过的文献上（evidence packet 会把这类缺口列进 known_gaps）。

**模型可主动调用的 web 工具**（`tools/web_tool.py`，`--web-search` 开启且 LLM 模式时生效）：本仓库 LLM 严格 text-in/JSON-out、不用原生 function-calling（codex_responses 后端不支持），因此采用 **prompt 级工具协议**——任何规划步骤中模型可输出 `{"tool_request": {"tool": "web_search", "query": "..."}}` 或 `{"tool": "web_read", "url": "..."}`，workflow 执行后把结果追加回 prompt 并重新调用该步骤（每任务上限 `RESEARCH_WEB_TOOL_MAX_ROUNDS`，默认 2 轮）。`web_read` 读到的页面自动按 `web_unverified` 归档进 campaign 知识库；每次工具调用记入 `state.tool_invocations` 供审计。

### 记忆与检索（自研、确定性、无向量依赖）

- 两层 SQLite 记忆（LITERATURE / EXPERIMENT），campaign/stage 元数据即层级树
- 每轮计划事件写**轨迹节点**（观测、决策理由、修复路径、evidence_refs）；stage 切换/收束写确定性 rollup 摘要
- **三层召回**注入每次 LLM 上下文（字符硬上限 → 上下文有界）：
  1. 路径摘要（总读，≤2000 字）
  2. 当前 stage 最近 3 个节点全文（≤3000 字）
  3. 跨 campaign 相似案例（仅异常/设备错误触发，带时效标签，≤1500 字）
- 检索打分：化学式保全分词（`K3Fe(CN)6` 不被拆碎）+ 可选 jieba/BM25 增强（未安装自动回退）

### 证据级溯源（反幻觉）

- PDF 抽取带 `[p.N]` 页码标记，protocol 步骤带 `page` 与 `evidence` 原句
- 抽取的 protocol 自动挂载注册表身份（`paper_id` / `verification_status` / `full_text_status`）
- 每个 macro step 附加**加法式**字段 `来源`：LLM 引用 → 保守匹配（≥0.5 重叠才归因，带页码）→ 否则诚实标注 `agent补全(未直接引用文献)`——**绝不虚构引用**
- `evidence_packet`（来源清单 + 已知缺口 + 引用规则）随每次 LLM 调用注入；`evidence_refs` 进台账与记忆节点

### 设备可用性动态接入

`--device-status-json`（`{"站名": "offline|busy|available"}` 或 `{"stations": {...}}`）同时叠加两层：研究层规划避开 ⛔ 不可用设备；设备层提示中禁选（不可用 ≠ 能力不存在，绕不开则正常走 feasibility 回流）。

## 安全边界

- **真实实验下发链路被硬阻断**：`chem_resources/*/lab-operation/scripts/dispatch_guard.py` 使 `generate_task.py`/`start_task.py` 只返回 blocked，不接触云网关
- orchestrator 的 `real` 适配器同样拒绝执行；接通真实执行需要人工显式授权与实现
- 闭环可用 `mock`/`manual`/`listen` 完整运行，真实结果通过网络端口回传

## 安装与配置

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r device_agent/requirements.txt
```

LLM 配置读根目录 `.env`（不硬编码 URL/Key），凭据优先级：`REFINER_LLM_*` → `GEMINI_*`（仅研究层）→ `OPENAI_*`。三种后端：OpenAI 兼容（默认）/ Gemini / `codex_responses`（经 `codex` CLI 的 gpt-5.5 路径）。

其他常用环境变量：`SEMANTIC_SCHOLAR_API_KEY`、`CROSSREF_MAILTO`、`OPENALEX_MAILTO`、`NCBI_API_KEY`（学术 API）；`TAVILY_API_KEY`、`SERPER_API_KEY`、`BRAVE_API_KEY`、`SEARXNG_BASE_URL`、`JINA_API_KEY`（web 搜索/阅读）；`RESEARCH_ENABLE_MEMORY`、`RESEARCH_ONLINE_LITERATURE`、`RESEARCH_WEB_SEARCH`、`RESEARCH_MEMORY_STORE_DIR`、`CHEM_DEVICE_STATUS_JSON`、`CHEM_WORKSTATIONS_NEW_DIR`。

## 测试

```bash
python -m unittest discover -s reaserch_agent -t . -p "test*.py"   # 研究层（58）
python -m unittest discover -s orchestrator  -t . -p "test*.py"   # 编排层（9）
python device_agent/test_single_agent.py                          # 设备层（需已装依赖）
```

约定：测试用暴露 `.invoke()` 的假模型 mock LLM，网络客户端注入 fake，不触真实端点；离线（`--disable-llm`）路径无凭据可跑。

## 目录速览

```
run_campaign.py            # 闭环入口
orchestrator/              # 编排器 + 执行边界适配器
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
