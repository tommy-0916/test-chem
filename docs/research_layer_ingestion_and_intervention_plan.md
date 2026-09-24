# Research Layer Ingestion 调研与 Intervention 完善计划

## 背景

本文记录 2026-07-07 对 `chemagent` research 层、`/Users/jyxc-dz-0100374/Desktop/auto_framework` 路径以及知识库 ingestion 能力的调研结论，并给出后续完善计划。目标是让 research agent 能稳定获取 paper、转换为可检索知识、结合自动化化学工作站能力生成更可靠的实验 macro plan。

## auto_framework 调研结论

最初检查 `/Users/jyxc-dz-0100374/Desktop/auto_framework` 时，该目录为空，没有可直接参考的多个 GitHub repo，也没有可读取的 ingestion/PDF/RAG 实现。

随后尝试从 GitHub 重新获取 `Echo-hyt/chem-agent`：

- `git fetch origin` 在当前 `/Users/jyxc-dz-0100374/Desktop/chemagent` 中成功，确认 `main` 与 `origin/main` 均为 `2fb9d83 Remove obsolete split workflow data`。
- 直接 SSH clone 到 `auto_framework/chem-agent` 时连接长时间无进度，最终出现 `early EOF`。
- HTTPS clone 因仓库认证失败，报 `could not read Username for 'https://github.com'`。
- 最终使用本地已 fetch 的最新仓库快照生成干净副本：`/Users/jyxc-dz-0100374/Desktop/auto_framework/chem-agent`。

因此，本次没有得到“多个外部 repo”的 ingestion 参考实现。后续若需要继续横向比较，需要先把目标 repo clone 到 `auto_framework`，或提供具体 GitHub URL。

## 当前 Research 层资料获取方式

当前 research 层不是在线 paper 搜索 agent，而是本地知识库驱动的 RAG/规划流程：

- 默认知识库路径：`reaserch_agent/chem_kb`
- 可通过 `--knowledge-base-dir` 指向其它目录，例如 `structured_outputs`
- `LocalExperimentCorpus` 扫描 `.json` 与 `.pdf`
- JSON 读取字段：`文献题目`、`1. 解决的问题`、`2. 具体的合成步骤`、`3. 性能`
- PDF 通过 PyMuPDF `fitz` 优先抽文本，失败后尝试 `pypdf`
- 抽取 `experimental section`、`materials and methods`、`synthesis`、`preparation` 等实验相关片段
- `KnowledgeQuery` 同时查询 literature memory 与本地 corpus
- `MemoryQuery` 查询 experiment memory，并回退到本地 corpus
- `load_device_context` 读取 `chem_resources` 下的工作站 `USAGE.md`、`AUDIT-RULES.md`、`SKILL.md`

也就是说，paper 需要先进入本地知识库，research agent 才能检索和使用。

## 已完成的 Intervention

已新增 ingestion 层，使 research agent 不再只能依赖手动整理好的 JSON/PDF：

- `reaserch_agent/tools/ingestion.py`
  - 新增 `KnowledgeIngestion`
  - 支持导入 `.pdf`、`.json`、`.txt`、`.md`
  - 输出当前 `LocalExperimentCorpus` 可直接读取的结构化 JSON
  - 可选写入 `LayeredChemMemory` literature 层
  - 支持 open PDF 下载到 `_pdf_sources/`，避免根目录重复检索

- `reaserch_agent/ingest_knowledge.py`
  - 新增 CLI
  - 支持本地文件/目录 ingestion
  - 支持 arXiv、Crossref、Semantic Scholar 外部元数据检索
  - Semantic Scholar 429 等外部错误会进入 `external_errors`，不再中断整个流程

- `reaserch_agent/test_ingestion.py`
  - 覆盖本地文本导入为可检索 JSON
  - 覆盖外部 paper metadata 转换为当前 schema

验证结果：

```bash
python3 -m unittest reaserch_agent.test_ingestion
python3 -m unittest discover -s reaserch_agent -t . -p "test*.py"
python3 reaserch_agent/ingest_knowledge.py --query "Prussian blue potassium ion battery" --sources arxiv --max-results 1 --dry-run --print-summary-json
python3 reaserch_agent/ingest_knowledge.py --query "Prussian blue potassium ion battery" --sources crossref --max-results 1 --dry-run --print-summary-json
```

## Smoke 实验发现

使用任务 query：

```text
我希望基于现有自动化化学工作站，设计一个 NiFe 普鲁士蓝类似物（NiFe-PBA）电化学活化性能优化实验。目标不是做完整电池，而是合成 NiFe-PBA 前驱体，并通过电化学工作站测试其活化后的电化学响应。
```

真实 LLM 路径失败，原因是环境中未配置：

- `REFINER_LLM_MODEL_NAME`
- `REFINER_LLM_ENDPOINT_URL`
- `REFINER_LLM_API_KEY`
- `OPENAI_API_KEY`

使用 `--disable-llm` 的离线 smoke 可以跑通，但启发式 planner 偏向了高熵 PBA 硫负载路线，而不是 NiFe-PBA 电化学活化路线。说明该任务需要 LLM 或更强的领域 rerank/约束机制。

## 后续完善计划

### 1. 配置层去硬编码

将模型配置统一放入根目录 `.env`，代码只读环境变量，不硬编码 URL 或 key。

建议变量：

```bash
REFINER_LLM_MODEL_NAME=gpt-5.5
REFINER_LLM_ENDPOINT_URL=<openai-compatible-base-url>
REFINER_LLM_API_KEY=<your-api-key>
REFINER_LLM_WIRE_API=codex_responses
REFINER_LLM_REASONING_EFFORT=xhigh
```

同时补充 `.env.example`，只保留占位符，不提交真实 key。

### 2. Ingestion 质量增强

当前 ingestion 能把文件转为可检索 JSON，但 protocol 抽取仍偏启发式。下一步应加入可选 LLM extraction：

- 输入 PDF/text 的实验段落
- 输出稳定 schema：问题、合成步骤、性能、设备约束、关键 observation
- 保留 evidence sentence 与 source page
- 对 JSON schema 做本地校验
- 对重复 DOI/title 做去重

### 3. 外部 Knowledge 获取增强

建议把 arXiv/Crossref/Semantic Scholar 扩展为可缓存 source adapters：

- 每次查询写入 `ingestion_manifest.jsonl`
- 保存 source、query、DOI、URL、PDF URL、抓取时间、错误信息
- 支持失败重试和限流 backoff
- 对 Semantic Scholar 使用 `SEMANTIC_SCHOLAR_API_KEY` 降低 429 风险

### 4. NiFe-PBA 任务的检索与 Rerank

本次 smoke 暴露 top hit 偏移问题。建议增加 domain reranker：

- query 中出现 `NiFe`、`电化学活化`、`OER` 时，优先命中 NiFe-PBA/OER/activation 文献
- 降低与完整电池、硫负载、非目标体系相关文献的权重
- 在 B1 prompt 中显式约束“不是完整电池，不做硫负载，不做电池组装”

### 5. Research Plan 质量门

在 `_step_macro_plan_design` 后增加本地质量检查：

- 目标材料必须包含 NiFe-PBA 或 NiFe Prussian Blue analogue
- 当前 stage 必须包含合成前驱体和电化学活化/测试 observation
- 禁止无关路线：PBA/S、完整电池、电极全流程组装，除非 query 明确要求
- 若质量检查失败，LLM retry 时注入反馈

### 6. 设备上下文联动

Research 层应保持 macro-action 粒度，但需要更严格使用设备上下文：

- 如果电化学工作站可用，stage observation 可以是电化学响应
- 如果缺少某些表征设备，XRD/SEM 等保持 offline handoff
- B1 不生成具体工作站 JSON，仍交给 device agent 映射

### 7. 推荐运行路径

本地导入 paper：

```bash
python3 reaserch_agent/ingest_knowledge.py \
  --input /path/to/papers \
  --output-dir reaserch_agent/chem_kb
```

外部检索并写入知识库：

```bash
python3 reaserch_agent/ingest_knowledge.py \
  --query "NiFe Prussian blue analogue electrochemical activation OER" \
  --sources arxiv,crossref,semantic_scholar \
  --max-results 5 \
  --download-pdfs \
  --output-dir reaserch_agent/chem_kb
```

Research smoke：

```bash
python3 reaserch_agent/run_research_agent.py \
  --event-type bootstrap \
  --query "我希望基于现有自动化化学工作站，设计一个 NiFe 普鲁士蓝类似物（NiFe-PBA）电化学活化性能优化实验。目标不是做完整电池，而是合成 NiFe-PBA 前驱体，并通过电化学工作站测试其活化后的电化学响应。" \
  --knowledge-base-dir reaserch_agent/chem_kb \
  --include-device-context \
  --wire-api codex_responses \
  --reasoning-effort xhigh
```

## 结论

当前 agent 的主要短板不是 workflow 主链路，而是 knowledge ingestion、领域 rerank、模型配置和 macro plan 质量门。已补上基础 ingestion 能力，下一阶段应优先完成 `.env` 配置统一、NiFe-PBA 任务 rerank/质量检查，以及 LLM protocol extraction。
