# auto_framework AutoResearch 仓库调研：执行流与外部知识摄取

本文基于 `/Users/jyxc-dz-0100374/Desktop/auto_framework/` 下 16 个本地仓库阅读整理，重点关注两件事：第一，每个 autoresearch 系统的执行流；第二，它们如何获取、验证、转换和持久化 LLM 之外的知识，尤其是 paper、PDF、网页、本地文件、实验日志和评测轨迹。

## 总体结论

这些仓库可以分成四类：

1. **搜索/阅读工具型**：Alibaba DeepResearch、MiroThinker、STORM、deer-flow 主要把外部搜索、网页抓取、文件转换封装为 agent 工具，LLM 通过工具调用临时获得证据。
2. **本地 RAG/知识库型**：LearningCircuit local-deep-research 是最完整的本地 ingestion/RAG 系统，支持多源搜索、文件上传、PDF/Office/网页解析、分块、embedding、FAISS 检索。
3. **研究工作流/证据治理型**：ARIS、AutoResearchClaw 更强调 paper 元数据获取、去重、引用验证、research wiki、claim/evidence 追踪和多阶段研究流程。
4. **实验/benchmark 轨迹型**：karpathy_autoresearch、autoagent、evo、autocontext、codex-autoresearch、uditgoenka_autoresearch、CORAL 更偏自动迭代和评测优化，知识主要来自代码、日志、测试、评分、历史 lessons，而不是学术文献 ingestion。

对 chemagent 最有参考价值的是：**LearningCircuit 的文档/RAG ingestion、ARIS 的 paper 验证与 research-wiki、AutoResearchClaw 的多源文献检索与 citation verify、deer-flow 的上传文件转 Markdown、Alibaba/MiroThinker 的网页/PDF reader 工具化模式**。

## 横向对比

| 仓库 | 主要执行流 | 外部知识来源 | PDF/文档处理 | 持久化与验证 |
| --- | --- | --- | --- | --- |
| Alibaba-NLP_DeepResearch | ReAct 多轮工具调用 | Serper Search/Scholar、Jina Reader、文件解析 | pdfminer/pdfplumber、Office、表格、音视频、DocMind | 解析缓存；无完整 RAG |
| LearningCircuit_local-deep-research | 搜索策略 + 引擎注册 + 本地 library RAG | arXiv、S2、PubMed、OpenAlex、网页、上传文件、PubChem 等 | 多格式 loader、PDF 下载、分块、embedding | FAISS/DB、SSRF/egress 安全、下载队列 |
| stanford-oval_storm | persona 问答式知识收集 -> outline/article | You/Bing/Serper/Brave/Tavily/DDG/SearXNG、Qdrant CSV | 核心无通用 PDF parser | 检索结果表和引用；Qdrant 可选 |
| bytedance_deer-flow | LangGraph super-agent + skills + tools | Tavily、Jina、Crawl4AI、Firecrawl、SearXNG、arXiv 脚本、上传文件 | pymupdf4llm -> MarkItDown fallback，Office 转 MD | 文件预览注入 prompt；memory 非 RAG |
| aiming-lab_AutoResearchClaw | 23 阶段研究 pipeline | OpenAlex、S2、arXiv、Tavily/DDG、Google Scholar、Crawl4AI | PyMuPDF 本地/URL PDF 提取 | JSONL/BibTeX/KB/graph；citation verify |
| Human-Agent-Society_CORAL | 多 agent worktree + grader + notes | 共享 notes、attempts、skills、grader 反馈 | 未实现 paper/PDF ingestion | `.coral/public` Markdown 知识图 |
| MiroMindAI_MiroThinker | tool manager + MCP search/scrape + orchestrator | Serper、Google/Sogou、Jina、MarkItDown MCP、浏览器 | MarkItDown MCP；Jina 可读 URL/PDF 文本 | 主要是 trace/log；无本地 RAG |
| OpenNSWM-Lab_FAROS | blueprint/profile/provider runtime | Semantic Scholar、arXiv、local sample corpus | 未见完整 PDF ingestion | run artifact、ResearchMemory |
| Orchestra-Research_AI-Research-SKILLs | skill 文档库 | Exa、S2、arXiv、CrossRef、OpenAlex 等的流程建议 | 依赖宿主工具 | Markdown skill/ARA schema，强调证据忠实 |
| evo-hq_evo | benchmark tree search + gates | 代码、日志、benchmark、可选 WebSearch/arXiv/HF/GitHub | 未实现 PDF ingestion | `.evo` graph/outcome/trace |
| greyhaven-ai_autocontext | scenario 评测 -> lesson/playbook -> advisor | run trace、score、SQLite、playbook、可选 web policy | 未实现 PDF ingestion | per-scenario knowledge、TF-IDF 检索 |
| karpathy_autoresearch | 固定 ML harness 自动迭代 | HuggingFace dataset、训练日志、指标 | 无 paper/PDF | results.tsv、git commits |
| kevinrgu_autoagent | agent harness 自动改进 | Harbor 任务、容器文件、verifier、trajectory | 无 paper/PDF | ATIF trajectory、results.tsv |
| leo-lilinxiao_codex-autoresearch | Codex skill 自动优化循环 | 代码、测试、results、lessons、受限 web search | 无 paper/PDF | `autoresearch-results` TSV/JSON/MD |
| uditgoenka_autoresearch | 多平台 command/skill 自动迭代 | 代码、git、hooks、WebSearch 产品调研 | 无 paper/PDF | TSV/context/hooks/wiki |
| wanshuiyin_Auto-claude-code-research-in-sleep | ARIS research pipeline + 多模型审查 | Zotero、Obsidian、本地 PDF、arXiv、S2、OpenAlex、DeepXiv、Exa、Gemini | arXiv PDF 下载、pdftotext、paper style 提取；更偏 metadata/markdown/source | research-wiki、verify_papers、citation-audit |

## Research Harness 的信息收集机制

这些 harness 的共同点是：**LLM 不直接“知道”资料，而是负责提出检索意图、改写 query、筛选候选和总结证据；真正的信息收集由确定性工具、API、文件解析器、索引和日志系统完成**。差别主要在于外部知识进入系统后的生命周期有多长：有些只临时放进 prompt，有些会进入本地 library、research wiki 或实验轨迹库。

典型信息收集流水线如下：

1. **任务理解与 query 规划**：把用户目标拆成若干检索面，例如背景综述、关键方法、最新进展、失败案例、安全约束、评价指标。STORM 用 persona 提问扩展视角；AutoResearchClaw 有 search strategy 阶段；ARIS 的 research-lit 会先确定 source mix；LearningCircuit 由 search system/strategy 选择搜索引擎。
2. **候选发现**：调用 arXiv、Semantic Scholar、OpenAlex、PubMed、Google Scholar/Serper、Tavily、DuckDuckGo、SearXNG、Exa 等搜索源，先拿标题、摘要、URL、DOI、arXiv ID、引用量、年份、snippet。
3. **元数据补全**：对候选 paper 再查详情 API，例如 S2 paper details、OpenAlex Works、CrossRef DOI、arXiv metadata，把作者、venue、publication date、citation count、open access PDF、external IDs 补齐。
4. **正文获取**：能拿 openAccessPdf 或 arXiv PDF 时下载 PDF；网页类结果通过 Jina、Crawl4AI、trafilatura、readability、Playwright、Firecrawl 等转成正文或 Markdown；用户上传文件则由 loader/MarkItDown/PyMuPDF 等转换。
5. **去重与规范化**：按 DOI、arXiv ID、Semantic Scholar paperId、OpenAlex ID、规范化 title 去重。ARIS 和 AutoResearchClaw 都明确做这一步，避免同一篇 paper 以 preprint/published/web page 多种形式重复进入上下文。
6. **相关性筛选**：先用关键词、source score、年份/引用量粗排，再由 LLM 或规则判断是否与问题相关。deer-flow 的 literature review skill 会把 paper 分批交给 subagent 处理；LearningCircuit 有 preview/full-content 两阶段过滤。
7. **转换为可用证据**：PDF/HTML/Office 转 Markdown 或纯文本，按 section/page/chunk 切分，必要时抽取表格、摘要、methods、results。Alibaba/MiroThinker 更常用“抓取后让 LLM 抽取 query-relevant evidence”；LearningCircuit 更偏“转换、分块、embedding、索引”。
8. **持久化或临时注入**：临时工具型系统把结果作为 tool response 进入 prompt；RAG 型系统写入 FAISS/Qdrant/DB；research workflow 型系统写入 `research-wiki`、`candidates.jsonl`、BibTeX、claim/evidence 文件；benchmark 型系统写入 trace、score、lessons。
9. **验证与审计**：严谨系统不会把搜索结果直接当事实。ARIS 用 arXiv/CrossRef/Semantic Scholar 三层验证 paper；AutoResearchClaw 做 citation verify；Orchestra skills 要求 citation 不能 hallucinate；LearningCircuit 做 SSRF/egress/secret 安全检查。

### 信息收集的四种模式

| 模式 | 代表仓库 | 如何收集信息 | 优点 | 短板 |
| --- | --- | --- | --- | --- |
| 临时工具调用 | Alibaba、MiroThinker、STORM、deer-flow | 搜索 -> 抓网页/PDF -> 摘要/片段进 prompt | 实现快，适合开放问答和深搜 | 缺少长期知识库，复用和审计弱 |
| 本地 RAG/library | LearningCircuit | 上传/下载文件 -> loader -> splitter -> embedding -> FAISS/DB | 可复用、可检索、适合私有资料 | 需要索引维护、chunk provenance 和安全边界 |
| Paper registry + research wiki | ARIS、AutoResearchClaw | paper API -> 去重验证 -> Markdown/JSONL/wiki/graph | 可审计，适合论文、claim、实验依据管理 | 比纯搜索慢，需要 schema 和流程约束 |
| 实验轨迹/lessons | evo、autocontext、karpathy、autoagent、CORAL、codex-autoresearch | 读取代码、测试、日志、score、attempt、notes | 适合自动迭代和经验沉淀 | 不解决学术论文全文 ingestion |

### 找论文的主要渠道

这些 repo 找论文不是只靠一个搜索框，而是把多个渠道组合起来。一般会用“结构化学术 API 做主干，网页/Scholar 做补漏，本地知识库做优先源，CrossRef/DOI 做验证”。

| 渠道 | 使用仓库 | 主要拿到什么 | 适合做什么 | 注意事项 |
| --- | --- | --- | --- | --- |
| arXiv Atom API | ARIS、AutoResearchClaw、deer-flow、FAROS、LearningCircuit | title、authors、abstract、published、categories、abs URL、PDF URL | 找预印本、下载开放 PDF、快速获取最新方向 | 不是正式发表记录；领域偏物理/CS/数学/部分材料 |
| Semantic Scholar Graph API | ARIS、AutoResearchClaw、FAROS、LearningCircuit | paperId、abstract、venue、year、citationCount、externalIds、openAccessPdf、references/citations、TLDR | 查论文详情、引用网络、开放 PDF、按相关性搜索 | 有速率限制；部分论文 abstract 或 PDF 缺失 |
| OpenAlex Works | ARIS、AutoResearchClaw、LearningCircuit | DOI、venue、authors、institution、concepts/topics、OA URL、citation count、abstract inverted index | 开放元数据、机构/主题分析、补全正式发表信息 | abstract 需要从 inverted index 还原；排序和领域覆盖需校验 |
| CrossRef/DOI | ARIS、AutoResearchClaw、Orchestra skills | DOI metadata、title、publisher、issued date、BibTeX | 验证引用真实性、生成可靠 BibTeX | 更适合验证，不适合主题发现；没有全文 |
| PubMed/PMC | LearningCircuit | biomedical paper metadata、abstract、PMC full text | 生物医学、药物、部分化学/材料生物交叉主题 | 化学材料主题覆盖有限；需要处理 NCBI 速率和格式 |
| Google Scholar/Serper Scholar/scholarly | Alibaba、AutoResearchClaw，MiroThinker 间接用 Serper 搜索 | 标题、snippet、引用线索、PDF URL、相关网页 | 高召回补漏，找不在单一 API 中的论文 | 非官方或第三方接口，结构化弱，稳定性和合规性要注意 |
| 通用 Web Search | Alibaba、MiroThinker、STORM、deer-flow、AutoResearchClaw、uditgoenka、evo | 网页、博客、项目页、PDF 链接、SOP、文档、新闻 | 找灰色资料、代码实现、实验 SOP、项目主页、补充证据 | search result 不是证据，必须 fetch 正文并验证 |
| Exa/DeepXiv/AlphaXiv/Gemini | ARIS | AI/语义搜索结果、paper 解读、相关工作线索 | 扩展探索空间，发现关键词不完全匹配的 paper | 需要回到 arXiv/S2/OpenAlex/CrossRef 验证 |
| Zotero/Obsidian/本地 PDF | ARIS、LearningCircuit、deer-flow 上传层 | 用户已有论文、笔记、标注、实验文档 | 高优先级私有知识；适合延续已有研究上下文 | 需要本地解析、去重、版本和 provenance 管理 |
| 向量库/本地语料 | STORM VectorRM、LearningCircuit library/collection | 已处理 chunk、title、url、description、metadata | 对私有 corpus 做语义检索 | 检索质量依赖 chunk、embedding 和 metadata |

### 它们实际怎么“找论文”

更细地看，找论文通常分成三条线并行：

1. **主题检索线**：从用户问题生成多组 query。比如 “NiFe Prussian blue analogue electrochemical activation”、“NiFe PBA oxygen evolution activation”、“Prussian blue analog electrochemical conditioning”、“nickel iron hexacyanoferrate electrochemistry”。这些 query 进入 arXiv/S2/OpenAlex/Web Search。
2. **实体扩展线**：从初始结果中抽取材料名、缩写、反应体系、表征方法和指标，再生成扩展 query。例如 NiFe-PBA 可能扩展到 nickel hexacyanoferrate、iron hexacyanoferrate、Prussian blue analogues、electrochemical activation、alkaline electrolyte、CV cycling、OER pre-catalyst。
3. **引用滚雪球线**：对高相关 paper 查询 references/citations，找 seminal paper、最新 follow-up 和实验方法来源。Semantic Scholar 和 OpenAlex 适合做这一步；Google Scholar/Serper 可补充引用线索。

高质量 harness 不会只做一次搜索。STORM 通过多 persona 反复提问；LearningCircuit 支持多引擎和 full-content fallback；ARIS 要求多源聚合且没有 source 贡献时显式失败；AutoResearchClaw 在 literature collection 后还有 screening 和 knowledge extraction 阶段。

### 从论文候选到可引用证据

论文搜索结果进入 agent 之前，严谨流程至少要过五道门：

1. **存在性验证**：arXiv ID、DOI/CrossRef、S2 paperId、OpenAlex ID 至少命中一个可信源。
2. **身份合并**：同一工作可能同时有 arXiv preprint、journal article、publisher page、PDF mirror。应合并到一个 canonical paper，并保留多个 external IDs。
3. **全文可读性检查**：只有 metadata 不够支撑实验细节。若要引用合成路线、活化程序、电解液、扫描速率、循环数、表征条件，必须读取 abstract 之外的 full text、methods、supporting information 或可信网页。
4. **证据定位**：总结时记录 paper id、section、page/chunk、原始片段或 table id。否则后续无法判断某个实验条件到底来自哪里。
5. **claim 级验证**：paper 存在不等于支持当前 claim。ARIS 的 citation-audit 和 AutoResearchClaw 的 verify 思路都说明：每个结论要绑定具体证据，而不是只挂一个参考文献。

### Paper 获取、阅读与存储的工程做法

如果把这些 harness 的做法抽象成一个可落地实现，paper ingestion 不应该是“搜到 PDF 后直接总结”，而应该分成 **metadata registry、raw artifact、readable text、semantic chunks、structured facts、claim evidence** 六层。

#### 1. Paper 获取顺序

推荐的获取顺序是：

1. **本地已有资料优先**：先查 `chem_kb`、Zotero、Obsidian、用户上传 PDF、历史 literature 目录。ARIS 和 LearningCircuit 都把本地资料作为高优先级知识源，因为它最贴近当前课题和用户约束。
2. **结构化 API 搜索 metadata**：用 Semantic Scholar、OpenAlex、arXiv、PubMed 等搜索标题、摘要、DOI、arXiv ID、年份、venue、引用数、open access PDF URL。
3. **CrossRef/DOI 做身份验证**：对候选 paper 做 DOI、标题、作者、年份核验，避免 LLM 生成或搜索 snippet 引入假论文。
4. **全文 resolver 获取正文**：按 local PDF -> arXiv PDF -> S2 `openAccessPdf` -> OpenAlex OA URL -> PMC full text -> publisher OA HTML/PDF -> web reader 的顺序尝试。
5. **失败进入人工/待处理队列**：如果只有 metadata、没有全文，应保留 paper record，但标记 `full_text_status: missing`，不能用来支撑实验细节。

这个顺序对应 ARIS 的 Zotero/Obsidian/local-first 和 verify_papers，AutoResearchClaw 的 OpenAlex/S2/arXiv 多源检索，以及 LearningCircuit 的 content fetcher/download service。

#### 2. Paper 阅读方式

阅读 paper 时不要一次性把全文交给 LLM。更稳的方式是三层阅读：

1. **快速筛选阅读**：只读 title、abstract、keywords、venue、year、citation、TLDR，用于判断是否进入候选集。
2. **结构化章节阅读**：PDF/HTML 转 Markdown 后，按 section 识别 Abstract、Introduction、Methods/Experimental、Results、Discussion、Conclusion、Supporting Information、References。化学实验尤其要抓 Experimental/Synthesis/Electrochemical measurements。
3. **任务定向证据阅读**：围绕当前 query 抽取字段。例如 NiFe-PBA 电化学活化任务，应抽取 precursor composition、合成温度/时间、洗涤/干燥方式、电解液、工作电极制备、扫描窗口、扫描速率、循环数、活化判据、对照组、安全限制。

LLM 可以参与第三层抽取，但输入应是带 page/section/chunk 的文本片段，而不是整篇 PDF。Alibaba 和 MiroThinker 采用“网页/PDF 抓取后 query-focused extraction”；LearningCircuit 更偏先 chunk/index 再检索；ARIS/AutoResearchClaw 更强调抽取后必须保留证据来源。

#### 3. 知识内容如何存储

建议把每篇 paper 存成一个稳定目录，同时维护全局 registry 和索引：

```text
research_agent/chem_kb/
  registry/
    papers.jsonl
    sources.jsonl
    claims.jsonl
    evidence.jsonl
  papers/
    <paper_id>/
      metadata.json
      raw/
        paper.pdf
        source_semantic_scholar.json
        source_openalex.json
        source_crossref.json
      readable/
        paper.md
        sections.json
        tables/
          table_001.csv
          table_001.md
      chunks/
        chunks.jsonl
      extracted/
        summary.md
        experimental_conditions.json
        materials.json
        electrochemistry.json
        safety_notes.json
      provenance.json
  indexes/
    faiss/
    bm25/
  graph/
    edges.jsonl
```

各层职责不同：

- `metadata.json`：canonical paper record，保存 title、authors、year、venue、DOI、arXiv ID、S2 ID、OpenAlex ID、URL、PDF URL、verification status。
- `raw/`：保存原始 PDF 和各 API 返回 JSON。原始件不覆盖，只追加版本和 hash。
- `readable/paper.md`：PDF/HTML 转换后的可读正文，是后续 chunk 和 LLM 阅读的基础。
- `sections.json`：记录每个章节的标题、起止页、起止字符、section type。
- `tables/`：表格单独保存，避免 Markdown 转换丢失实验条件。
- `chunks.jsonl`：每个 chunk 带 `chunk_id`、section、page、token count、text、hash、embedding id。
- `extracted/*.json`：任务相关结构化知识，例如实验条件、电化学测试参数、安全信息。
- `claims.jsonl` 和 `evidence.jsonl`：存 claim 与证据的关系，而不是只存 summary。
- `graph/edges.jsonl`：存 paper-material-method-claim-experiment 的关系图。

#### 4. 最小 schema

Paper registry 可以用 JSONL 起步，不必一开始上复杂数据库：

```json
{
  "paper_id": "doi_10_0000_example",
  "title": "Example title",
  "authors": ["A. Author"],
  "year": 2024,
  "venue": "Journal Name",
  "doi": "10.0000/example",
  "arxiv_id": null,
  "semantic_scholar_id": "paperId",
  "openalex_id": "W...",
  "source_urls": ["https://..."],
  "pdf_path": "papers/doi_10_0000_example/raw/paper.pdf",
  "markdown_path": "papers/doi_10_0000_example/readable/paper.md",
  "verification_status": "verified_doi",
  "full_text_status": "parsed",
  "content_hash": "sha256...",
  "fetched_at": "2026-07-08T00:00:00Z"
}
```

Evidence record 应该比 paper record 更细：

```json
{
  "evidence_id": "ev_001",
  "paper_id": "doi_10_0000_example",
  "chunk_id": "chunk_034",
  "section": "Experimental",
  "page": 5,
  "claim_type": "electrochemical_protocol",
  "text": "Relevant extracted snippet or paraphrase source span.",
  "supports": "claim_012",
  "confidence": 0.82,
  "extraction_method": "llm_with_chunk",
  "verified": true
}
```

这样做的关键是：**paper 是文献实体，chunk 是可检索文本，evidence 是可引用依据，claim 是 agent 生成或抽取的结论**。四者不能混成一个 summary 文件。

#### 5. PDF 转可读文本的实现选择

这些仓库里出现的实现可以组合使用：

- 首选 `pymupdf4llm`：deer-flow 用它把 PDF 转 Markdown，适合作为第一选择。
- fallback `MarkItDown`：deer-flow 和 MiroThinker 都用它兜底 Office/PDF/网页文件。
- fallback `pdfplumber`/`pdfminer`：Alibaba 用于 PDF 文本和表格，对实验表格有价值。
- fallback `PyMuPDF`：AutoResearchClaw 用于 metadata、page text、section boundary。
- fallback `pdftotext`：ARIS 用于轻量 PDF 提取和 paper style 分析。

质量检查也很重要：如果每页字符过少、正文为空、表格丢失、参考文献占比异常，就应标记 `parse_status: low_quality`，并进入二次解析或人工队列。

#### 6. 进入检索系统

paper 被转换后，通常有两条检索路径：

1. **关键词/BM25 检索**：适合找精确实体，例如 “NiFe-PBA”、“K3Fe(CN)6”、“1 M KOH”、“CV activation”、“scan rate”。
2. **向量检索**：适合找语义相近的实验方法、机制解释或性能优化策略。

LearningCircuit 的做法是 loader -> splitter -> embedding -> FAISS/DB。STORM 的 VectorRM 用 CSV + Qdrant。对 chemagent，建议两者结合：BM25 用于化学实体和参数，向量检索用于语义召回，最后用 reranker 或 LLM 对 top chunks 做任务相关性判断。

#### 7. 阅读结果如何进入 agent

agent 不应该直接读整个知识库，而应该拿到一个 evidence packet：

```text
Question: NiFe-PBA electrochemical activation optimization

Retrieved evidence:
1. paper_id=..., section=Experimental, page=4, verification=verified_doi
   finding: ...
   source_span: ...
2. paper_id=..., section=Electrochemical measurements, page=6, verification=verified_s2
   finding: ...
   source_span: ...

Known gaps:
- No full text for paper X.
- Supporting information missing for paper Y.
- Safety data for electrolyte/additive not found.
```

这样 LLM 的角色是“基于证据综合和规划”，而不是“凭记忆补实验细节”。这也是 ARIS citation-audit、AutoResearchClaw citation verify、Orchestra evidence fidelity 共同指向的工程原则。

#### 8. 对 chemagent 的最小实现建议

第一版可以按下面顺序做：

1. 写 `paper_search`：接 S2、OpenAlex、arXiv，返回候选 metadata。
2. 写 `paper_verify`：用 DOI/CrossRef、arXiv ID、S2/OpenAlex title match 标 verification status。
3. 写 `paper_fetch_fulltext`：优先 local/Zotero/chem_kb，再 openAccessPdf/arXiv/PMC/OA URL。
4. 写 `paper_parse`：PDF/HTML 转 Markdown，生成 sections、tables、chunks。
5. 写 `paper_index`：BM25 + FAISS/Qdrant，chunk 带 provenance。
6. 写 `paper_extract_for_task`：对 top chunks 抽取实验条件、材料、测试参数、安全信息。
7. 写 `evidence_logger`：任何实验建议都必须写 claim/evidence 记录。

这条链路跑通后，chemagent 才能回答“这个实验建议来自哪篇 paper、哪一节、哪个参数、可靠性如何”。

### 主要渠道的优先级建议

针对 chemagent 这类化学实验 agent，找论文和知识时建议按这个顺序：

1. **本地 KB 优先**：先扫 `research_agent/chem_kb`、Zotero/Obsidian、本地 PDF、已有 SOP 和实验记录。这里最可能包含用户真实约束和仪器条件。
2. **结构化学术 API 第二**：Semantic Scholar + OpenAlex + arXiv 组合使用。S2 用于相关性和 citation graph，OpenAlex 用于正式 metadata/OA，arXiv 用于预印本和 PDF。
3. **CrossRef/DOI 验证**：对进入报告或实验依据的 paper 做 DOI/题名/作者/年份核验。
4. **Web/Scholar 补漏**：当 API 召回不足、需要 publisher page、实验室主页、SOP、开源代码或 PDF mirror 时，再用 Serper/Scholar/Tavily/DuckDuckGo/SearXNG。
5. **全文和 supporting info**：涉及合成配方、电化学活化程序、测试窗口、浓度、pH、循环数、扫描速率时，不能只看摘要。需要 PDF/HTML/SI 转 Markdown，并在 evidence 中定位。
6. **安全资料单独检索**：化学实验还要查 SDS、废液处理、危险试剂、设备限制和电化学窗口。这类资料通常不在 paper API 中，需要本地 SOP、厂商手册或可信网页。

### 各仓库找论文能力分层

| 层级 | 仓库 | 说明 |
| --- | --- | --- |
| 完整 paper ingestion + 验证 | ARIS、AutoResearchClaw | 多 API 查 paper，去重，验证，写 wiki/JSONL/BibTeX，适合做研究证据治理 |
| 完整文档/RAG ingestion | LearningCircuit | paper API + full content + 上传文件 + FAISS，适合建设本地知识库 |
| 深搜工具链 | Alibaba、MiroThinker、deer-flow、STORM | 搜索和阅读能力强，但长期 paper registry/verification 较弱 |
| 方法论/skill 指令 | Orchestra skills | 规定应如何查证和引用，但依赖宿主 agent/tool 实现 |
| 实验优化/评测轨迹 | evo、autocontext、karpathy、autoagent、CORAL、codex-autoresearch、uditgoenka | 主要收集运行证据，不是文献检索系统 |

### 核心判断

这些 research harness 的信息收集本质上不是“让 LLM 上网搜一下”，而是一个分层系统：**query 规划 -> 多源候选发现 -> 元数据补全 -> 全文获取 -> 去重规范化 -> 证据抽取 -> 持久化索引 -> claim/citation 验证**。区别只在于各仓库实现了其中多少层。对于 chemagent，应至少实现本地 KB、S2/OpenAlex/arXiv、CrossRef 验证、PDF 转 Markdown、chunk retrieval 和 evidence logging；否则实验建议会很难追溯，也很难判断是否真的由文献支持。

## 仓库逐项分析

### 1. Alibaba-NLP_DeepResearch

主要文件：`inference/react_agent.py`、`tool_search.py`、`tool_scholar.py`、`tool_visit.py`、`tool_file.py`、`file_tools/file_parser.py`。

它是典型 ReAct agent。`MultiTurnReactAgent` 反复调用 LLM，解析 `<tool_call>` JSON，再把 `<tool_response>` 注入下一轮上下文。核心知识获取工具包括普通搜索、Google Scholar、网页访问和文件解析。

外部知识获取方式比较直接：

- `tool_search.py` 通过 Serper 的 Google Search API 获取网页结果。
- `tool_scholar.py` 通过 Serper Scholar 获取论文标题、年份、引用、snippet、`pdfUrl`。
- `tool_visit.py` 用 Jina Reader 读取网页正文，再用 OpenAI-compatible summarizer 按用户查询抽取证据。
- `tool_file.py` 允许 agent 解析本地文件或 URL 下载后的文件。

PDF 和文档处理很强。`file_parser.py` 支持 PDF、Word、PPT、Excel、CSV、HTML、ZIP、音视频等。PDF 本地 fallback 使用 pdfminer 和 pdfplumber，既提取文本，也尝试提取表格。Office 依赖 python-docx、python-pptx、pandas。大文件会截断或只返回 schema。它也支持 Alibaba DocMind IDP，将复杂文档转成 markdown layout。

它的问题是没有稳定的知识库层：搜索、网页、PDF 内容基本作为工具响应进入 prompt，最多做文件解析缓存，并没有统一的 paper registry、向量索引、claim/evidence graph 或 citation verification。

### 2. LearningCircuit_local-deep-research

主要文件：`src/local_deep_research/search_system.py`、`web_search_engines/search_engine_base.py`、`engine_registry.py`、`content_fetcher/fetcher.py`、`document_loaders/loader_registry.py`、`research_library/services/library_rag_service.py`、`local_embedding_manager.py`。

这是调研中 ingestion 层最完整的仓库。执行流由 `AdvancedSearchSystem` 协调：根据策略选择搜索引擎，得到 preview，再按需要抓取 full content，经过相关性过滤、citation 处理、findings 存储后生成答案或报告。

外部知识来源非常丰富。引擎注册表包含 arXiv、PubMed、Semantic Scholar、OpenAlex、NASA ADS、GitHub、Wikipedia、SearXNG、DuckDuckGo、Brave、Tavily、Serper、SerpApi、Paperless、Elasticsearch、PubChem、Zenodo、Gutenberg、OpenLibrary、StackExchange 等。这个设计非常适合 chemagent，因为化学任务往往同时需要论文、物性数据库、网页 SOP、本地实验记录和 PubChem 类结构化数据。

它的网页/文件抓取层也很系统。`ContentFetcher` 会先分类 URL：arXiv、PubMed、PMC、Semantic Scholar、bioRxiv、medRxiv、DOI、PDF、HTML 等，并拒绝 `javascript:`、`data:`、`file:` 等危险 scheme。下载器负责 SSRF/egress 检查、速率限制和 fallback。PDF bytes 会通过通用 downloader 提取文本，并做最大长度截断。

本地 RAG 是它的强项。用户上传文件后，loader registry 支持 PDF、TXT、MD、HTML、DOCX/DOC、ODT、PPT/PPTX、XLS/XLSX、RTF、EPUB、EML、CSV/TSV/JSON/YAML/XML/TOML、IPYNB、MHTML 等。`LibraryRAGService` 用 loader -> splitter -> embedding -> FAISS 的链路建立索引，默认 chunk size 1000、overlap 200。`LocalEmbeddingManager` 支持 sentence-transformers、Ollama、OpenAI 等 embedding provider，并把 chunk hash、metadata、source 一起入库。

它的安全设计也值得借鉴：egress policy、SSRF 检查、secret redaction、速率限制、下载隔离、索引完整性锁和 quarantine。对化学 agent 来说，这类边界比“能搜到更多资料”更重要。

### 3. stanford-oval_storm

主要文件：`knowledge_storm/rm.py`、`storm_wiki/modules/retriever.py`、`knowledge_curation.py`、`examples/storm_examples/run_storm_wiki_gpt_with_VectorRM.py`。

STORM 的执行流不是简单搜索总结，而是“先问问题，再写文章”。系统先生成多个 perspective/persona，每个 persona 与 topic expert 进行多轮问答。topic expert 把问题转换为查询，调用 retriever 找资料，再基于检索片段回答。所有对话 turn 进入 information table，之后生成 outline 和文章。

外部知识主要来自 retriever module：

- Web retriever：You.com、Bing、Serper、Brave、DuckDuckGo、Tavily、SearXNG。
- `WebPageHelper`：用 httpx 获取 HTML，用 trafilatura 抽取正文，再按约 1000 字符切片。
- `VectorRM`：读取 CSV 文档，字段包括 `content`、`title`、`url`、`description`，用 HuggingFaceEmbeddings 和 Qdrant 做向量检索。

它不强调 paper 下载或 PDF 解析。论文知识可以通过搜索结果或用户预先处理成 CSV/Qdrant 后接入。STORM 的价值在于 knowledge curation 形式：它用“多视角提问”避免一次搜索过窄，并把检索证据绑定到问答 turn。chemagent 可以借鉴这个流程做实验方案前的“多视角质询”：材料合成、电化学窗口、安全、表征、可复现实验条件分别问一轮。

### 4. bytedance_deer-flow

主要文件：`backend/README.md`、`skills/public/deep-research/SKILL.md`、`skills/public/systematic-literature-review/SKILL.md`、`scripts/arxiv_search.py`、`backend/src/tools/community/*`、`backend/src/middleware/uploads_middleware.py`、`backend/src/utils/file_conversion.py`。

deer-flow 是 LangGraph super-agent 架构。它通过 lead agent、subagents、skills、middleware、sandbox、tools、MCP 组合任务。deep-research skill 更像研究方法论，而不是固定 pipeline；systematic-literature-review skill 则有明确 arXiv 搜索脚本。

外部知识获取分为三层：

- Web 工具：Tavily、Jina、Crawl4AI、Firecrawl、fastCRW、DuckDuckGo、SearXNG、InfoQuest。
- 学术搜索：`arxiv_search.py` 调用 arXiv Atom API，返回 id、title、authors、abstract、published、categories、pdf_url、abs_url。
- 用户上传：middleware 会将上传文件转换后的 outline/preview 注入 prompt，agent 再按需 read/grep。

PDF 和文档转换值得直接复用。`file_conversion.py` 对 PDF 优先用 `pymupdf4llm.to_markdown`，如果提取结果太少则 fallback 到 MarkItDown。PPT、Excel、Word 也会转 Markdown。大文件会后台转换，并保存同目录 `.md`。`UploadsMiddleware` 会挑选最多 10 个与 query 相关或最近上传的文件，把文件名、路径、outline、preview 放进 `<uploaded_files>`。

它没有完整本地向量库。长期记忆是 memory middleware 从对话中抽取用户事实，注入后续 prompt，属于 personalization memory，不适合当文献库。但“文件转 Markdown + outline/preview + read_file/grep”的轻量 ingestion 对 chemagent 很实用。

### 5. aiming-lab_AutoResearchClaw

主要文件：`researchclaw/pipeline/stages.py`、`researchclaw/literature/search.py`、`arxiv_client.py`、`semantic_scholar.py`、`openalex_client.py`、`web/pdf_extractor.py`、`web/agent.py`、`citation/verify.py`、`knowledge/base.py`、`knowledge/graph/builder.py`。

AutoResearchClaw 是完整研究 pipeline，包含 23 个阶段：问题分解、搜索策略、文献收集、筛选、知识抽取、综合、假设生成、实验设计、代码生成、实验运行、结果分析、论文、peer review、质量门、知识归档和 citation verify。

论文获取链路很清晰：`search_papers` 先查 OpenAlex，再查 Semantic Scholar，再查 arXiv，最后按 DOI、arXiv ID、规范化标题去重，并按引用量和年份排序。OpenAlex 负责 publication metadata 和 inverted-index abstract 还原；Semantic Scholar 负责 paperId、abstract、venue、citationCount、externalIds；arXiv 负责预印本和 PDF URL。

Web 层也比较完整。`web/search.py` 以 Tavily 为主、DuckDuckGo HTML 为 fallback；`web/crawler.py` 用 Crawl4AI 转 markdown，失败后用 urllib + regex fallback；`web/pdf_extractor.py` 使用 PyMuPDF 解析本地或 URL PDF，提取文本、metadata、abstract 和 section boundary。

知识持久化包括：

- 文献候选 `candidates.jsonl`、BibTeX `references.bib`、网页上下文 `web_context.md`、检索元数据 `search_meta.json`。
- knowledge base categories：questions、literature、experiments、findings、decisions、reviews。
- knowledge graph builder：paper、method、dataset 等实体和关系。
- memory store：JSONL 记忆，带 embedding、confidence、timestamp。

它的 citation verification 是强项：先用 arXiv ID 验证，再用 DOI/CrossRef，再用 Semantic Scholar + arXiv 标题 fuzzy match，标记 verified、suspicious、hallucinated、skipped。chemagent 的 ingestion 层应该采用类似机制：资料可以先进入候选库，但未验证资料必须显式带 `[UNVERIFIED]`，不能被当成可靠依据。

### 6. Human-Agent-Society_CORAL

主要文件：`coral/hub/notes.py`、`CLAUDE.md`、README 中的 shared state 说明。

CORAL 是多 agent 协同研究/编码基础设施。每个 agent 在独立 git worktree 中工作，grader daemon 对 commit 打分，manager 通过 heartbeat 促使 agent 反思、合并或转向。它不是文献 ingestion 框架。

外部知识主要来自 agent 运行期间产生的公共状态：attempts、notes、skills、grader feedback、heartbeat、commit history。`notes.py` 支持带 YAML frontmatter 的 Markdown note，字段包括 type、claim、status、confidence、based_on、evidence、supersedes、refutes、tags、next 等。

它对 chemagent 的价值不是获取 paper，而是“团队知识治理”：实验或分析 agent 可以把结论、失败尝试、证据路径和反驳关系写成结构化 note，后续 agent 检索和复用。它缺少 PDF、网页和数据库 connector。

### 7. MiroMindAI_MiroThinker

主要文件：`apps/miroflow-agent/main.py`、`src/core/pipeline.py`、`core/orchestrator.py`、`core/tool_executor.py`、`libs/miroflow-tools/manager.py`、MCP tools 下的 `serper_mcp_server.py`、`search_and_scrape_webpage.py`、`jina_scrape_llm_summary.py`、`reading_mcp_server.py`。

MiroThinker 是深度搜索和 benchmark trace 框架。Hydra config 创建 LLM client 和 ToolManager，ToolManager 连接 MCP server，Orchestrator 负责主 agent、subagent、工具定义缓存、重复查询 rollback、上下文压缩和错误恢复。

外部知识获取由 MCP tools 提供：

- Serper/Google Search：使用 `SERPER_API_KEY` 和 `SERPER_BASE_URL`。
- Sogou/Tencent Cloud SearchPro：偏中文网页搜索。
- Jina scrape：`https://r.jina.ai` 把 URL 转成可读文本。
- `jina_scrape_llm_summary.py`：先 Jina 或 direct httpx 抓取，再用 summary LLM 按查询抽取信息。
- `reading_mcp_server.convert_to_markdown`：通过 MarkItDown MCP 把 URL、本地文件、Office、PDF、Excel、ZIP 等转 Markdown。
- Browser session：Playwright 页面导航和 snapshot。

它有明确的防泄漏策略，例如阻断 HuggingFace datasets/spaces 的 benchmark 数据访问。它不提供本地向量索引或 paper registry。适合借鉴的点是：把搜索、抓取、Markdown 转换都变成 MCP 工具，并在 executor 层做空结果、重复查询和工具误用的自动修正。

### 8. OpenNSWM-Lab_FAROS

主要文件：`backend/app/faros/runtime/orchestrator.py`、`memory/research_memory.py`、`backend/app/services/search_service.py`、`backend/app/faros/providers/*`、`skills/literature-grounding/skill.json`。

FAROS 是面向 research lifecycle 的 blueprint/profile/provider runtime。Orchestrator 读取 blueprint，创建 run，初始化 ResearchMemory，执行 ready nodes，解析 agent/skill/provider，保存 artifacts、events、checkpoints。

它的外部知识 ingestion 还比较早期。`search_service.py` 实现了 Semantic Scholar graph API、arXiv API 和 local sample corpus 检索，返回 SearchResult：title、authors、abstract、year、venue、url、doi、arxiv_id、citation_count、source、relevance_score，并按标题简单去重。未看到 PDF 下载、全文解析、向量库或 citation audit。

`ResearchMemory` 是运行态 memory envelope，支持 data、summary、scopes、history、archives、policy、compact、query、recall。它存的是阶段输出和 run artifacts，不是文献级 RAG。

它值得借鉴的是 provider/external backend 模式：file、workspace_file、queue_file、approval_file、approval_queue、command。chemagent 可以用类似方式把“资料 ingestion 请求”“人工确认请求”“实验下发请求”隔离成不同队列，并把实验下发链路默认关闭。

### 9. Orchestra-Research_AI-Research-SKILLs

主要文件：`0-autoresearch-skill/SKILL.md`、`20-ml-paper-writing/ml-paper-writing/SKILL.md`、`22-agent-native-research-artifact/compiler/SKILL.md`、`references/ara-schema.md`。

这是 skill/知识模板库，不是运行时系统。它把研究过程、论文写作、citation、RAG、artifact schema 等整理为可供 agent 加载的 Markdown 指令。

`0-autoresearch-skill` 定义 workspace：`research-state.yaml`、`research-log.md`、`findings.md`、`literature/`、`experiments/`、`data/`、`paper/`。文献 bootstrap 推荐 Exa MCP、Semantic Scholar、arXiv、CrossRef，并要求每篇 paper 保存摘要和 survey。

`ml-paper-writing` 强调不要 hallucinate citation。建议用 Semantic Scholar、arXiv、CrossRef、DOI BibTeX content negotiation，引用必须经两个来源交叉验证。`ARA Compiler` 要求把 PDF、arXiv link、repo、logs、notes、code、实验记录整理成结构化 research artifact，且明确区分 raw evidence、derived subset、inferred claim。

它没有实际 downloader 或 parser，但对 chemagent 很有价值：可以直接借鉴“证据忠实”规范，即任何实验建议都必须标注来源、证据类型、是否推断、是否验证。

### 10. evo-hq_evo

主要文件：`plugins/evo/src/evo/core.py`、`plugins/evo/agents/ideator.md`、`benchmark-reviewer.md`、`verifier.md`。

evo 是 benchmark-driven autoresearch。系统维护 `.evo/graph.json`、config、annotations、infra log、run/experiment/attempt artifacts，在多个 worktree/sandbox 中并行尝试改动，用 gates 和 benchmark score 决定是否保留。

它的外部知识不是 paper ingestion，而是实验轨迹 ingestion：日志、trace、outcome、gate、失败类别、review annotation。`ideator.md` 中的 literature brief 允许 agent 用 WebSearch/WebFetch 扫 arXiv、HuggingFace Papers、HF Hub、GitHub、issues、blogs，形成 proposal。但 repo 没有实现自动 PDF 下载、论文解析或本地索引。

对 chemagent 的启发是：实验优化应该把每次实验 proposal、执行条件、观测结果、失败原因、保留/丢弃理由写入结构化 graph，而不是只保留最终答案。

### 11. greyhaven-ai_autocontext

主要文件：`autocontext/src/autocontext/knowledge/search.py`、`export.py`、`trajectory.py`、`CLAUDE.md`。

autocontext 是 recursive agent improvement harness。它对一个 scenario 反复运行 agent、评估、分析、更新 playbook，最终把有效经验导出为 skill package 或训练数据。

知识来源包括 run trace、generation strategy、analysis、score、SQLite 中的历史结果、per-scenario playbook、hints、snapshots。`search.py` 实现的是 TF-IDF 风格的 solved-scenario 知识搜索：匹配 scenario 名称、描述、策略接口、eval criteria、lessons、playbook excerpt、hints、task prompt、judge rubric 等。

它不做学术 paper/PDF ingestion。它的价值是“经验压缩和跨运行迁移”：chemagent 可以把多次文献检索、方案筛选和 smoke test 结论沉淀成可检索 playbook，而不仅是 chat history。

### 12. karpathy_autoresearch

主要文件：`program.md`、`prepare.py`、`train.py`。

这是最小化的自动实验优化 harness。`prepare.py` 从 HuggingFace dataset `karpathy/climbmix-400b-shuffle` 下载训练 shard，训练 tokenizer 并缓存到 `~/.cache/autoresearch`。`program.md` 指导 agent：先跑 baseline，再循环修改 `train.py`、训练、记录 `val_bpb` 和资源指标、保留提升、丢弃退化。

它没有 paper、PDF、网页或 RAG。外部知识就是固定数据集、训练日志、指标和 git commit。对 chemagent 的参考点是“可重复 smoke loop”：每次只改一个因素，有 baseline，有 TSV，有保留/回滚规则。

### 13. kevinrgu_autoagent

主要文件：`agent.py`、`agent-claude.py`、`program.md`。

这个仓库把 autoresearch 思路用于 agent harness 改进。`agent.py` 使用 OpenAI Agents SDK，提供 `run_shell` 工具在 Harbor 环境里执行命令，并输出 ATIF trajectory。`agent-claude.py` 是 Claude SDK 版本，支持 preset tools、MCP、hooks、subagents。

外部知识来自 Harbor 任务文件、容器文件系统、verifier 输出和 trajectory。`program.md` 要求 meta-agent 根据失败任务的轨迹和 verifier logs 分组诊断，修改 agent harness，再 rerun suite。

它没有学术 ingestion，但 trajectory JSON 是一种重要的“非 LLM 知识”：真实执行过程、工具调用、错误和 verifier feedback。chemagent 也应把每次检索、解析、筛选、生成方案的 trace 落盘，方便后续审计。

### 14. leo-lilinxiao_codex-autoresearch

主要文件：`SKILL.md`、`references/web-search-protocol.md`、`references/lessons-protocol.md`、`scripts/autoresearch_init_run.py`、`scripts/autoresearch_record_iteration.py`、`scripts/autoresearch_lessons.py`。

这是 Codex skill 形式的自动优化流程。执行流是：读取上下文、定义 metric、跑 baseline、做小改动、验证、保留或丢弃、记录结果。状态写入 `autoresearch-results/results.tsv`、`state.json`、`context.json`、`lessons.md`。

外部知识策略很克制。`web-search-protocol.md` 要求前 3 次迭代不搜索；只有连续卡住、未知错误或外部库不确定时才搜索；每 10 次迭代最多 3 次搜索。搜索结果只是 hypothesis，必须经过本地测试验证。

它没有 paper/PDF/RAG，但 lessons 协议值得借鉴：把每次尝试的 strategy、outcome、insight、context、iteration、timestamp 结构化保存，避免重复失败。

### 15. uditgoenka_autoresearch

主要文件：`guide/autoresearch.md`、`docs/system-architecture.md`、`.claude/commands/autoresearch/improve.md`、`learn.md`、`scripts/orchestrate.sh`。

这是多平台 command/skill 自动迭代系统。核心 loop 仍然是 setup、baseline、focused change、verify、guard、keep/revert、TSV。它的 hook 系统会注入上下文、阻断危险命令、做隐私扫描、提醒 dev rules。

它的 `/autoresearch:learn` 可以从代码生成 wiki，属于代码库知识 ingestion。`/autoresearch:improve` 偏产品研究，会用 WebSearch 收集 ICP challenges、competitor gaps、market trends、UX、growth/revenue insight，并要求去重、source confidence、与代码能力交叉检查。

没有学术 paper API 或 PDF ingestion。对 chemagent 的价值是安全策略：自动化系统需要 command screen、隐私/secret block、scope guard。化学系统还应额外加入“实验下发阻断”和“危险方案人工确认”。

### 16. wanshuiyin_Auto-claude-code-research-in-sleep

主要文件：`skills/research-pipeline/SKILL.md`、`skills/research-lit/SKILL.md`、`skills/arxiv/SKILL.md`、`skills/semantic-scholar/SKILL.md`、`skills/openalex/SKILL.md`、`skills/research-wiki/SKILL.md`、`skills/citation-audit/SKILL.md`、`tools/arxiv_fetch.py`、`tools/semantic_scholar_fetch.py`、`tools/openalex_fetch.py`、`tools/verify_papers.py`、`tools/research_wiki.py`、`tools/extract_paper_style.py`。

ARIS 是最完整的“研究工作流 skill 系统”。执行流通常是 `research-pipeline`：idea-discovery -> experiment-bridge -> auto-review-loop -> paper-writing。状态保存在 `.aris/runs/<run_id>.json`，阶段只有通过跨模型审查或确定性 gate 后才算 accepted。

`research-lit` 的知识获取层非常清楚：

- 本地优先：Zotero MCP、Obsidian MCP、本地 `papers/` 或 `literature/` 下 PDF。
- 外部 paper：arXiv、Semantic Scholar、OpenAlex、DeepXiv、Exa、Gemini。
- Web：WebSearch 和网页读取。
- 聚合规则：只有实际请求并调用过的 source 才能贡献结果；如果没有任何 source 贡献，必须显式失败。
- 去重规则：arXiv ID、DOI、normalized title；如果 Semantic Scholar 有正式 venue/DOI，优先 published metadata，而不是 arXiv preprint。

paper 工具实现扎实：

- `arxiv_fetch.py` 调 arXiv Atom API，解析 title、authors、abstract、published、updated、categories、pdf_url、abs_url，并能下载 PDF。
- `semantic_scholar_fetch.py` 调 S2 Graph API，支持 search、bulk search、paper details、openAccessPdf、externalIds、citations、tldr 等。
- `openalex_fetch.py` 调 OpenAlex Works，支持 abstract inverted index 还原、authors、venue、OA URL、topics、keywords、citation counts。
- `verify_papers.py` 做 anti-hallucination：arXiv batch、CrossRef DOI、Semantic Scholar fuzzy title 三层验证，输出 PASS/WARN/BLOCKED/ERROR 和 hallucination_rate。
- `research_wiki.py` 建立 `research-wiki/{papers,ideas,experiments,claims,graph}`，每篇 paper、claim、idea、experiment 都是可追溯 Markdown/JSONL。

PDF 处理上，ARIS 更偏“结构化 metadata + markdown/source”，而不是全文向量 RAG。arXiv PDF 可下载；local PDF 在 research-lit 中只读前几页；`extract_paper_style.py` 用 `pdftotext` 从 PDF 提取结构风格；论文编译和 slides polish 也使用 `pdfinfo`、`pdftotext`、`pdffonts`、`pdftoppm`。它没有像 LearningCircuit 那样完整的本地 FAISS 文档库。

ARIS 对 chemagent 很关键的一点是 citation/claim audit。它把“文献是否存在”“引用是否支持当前句子”“claim 是否有 evidence”作为独立流程，而不是相信 LLM 生成的参考文献。

## Paper 获取与处理模式总结

### arXiv

ARIS、AutoResearchClaw、deer-flow、FAROS、LearningCircuit 都使用 arXiv。常见实现是调用 `export.arxiv.org/api/query` 的 Atom API，解析 title、authors、summary、published、categories、abs URL 和 PDF URL。ARIS 和 AutoResearchClaw 还支持 PDF 下载。

适合 chemagent 的做法：把 arXiv 当作预印本源，不把它等同于 peer-reviewed 事实；如果同一论文在 Semantic Scholar/OpenAlex 有正式 venue/DOI，应合并 metadata，并标注 published source。

### Semantic Scholar

AutoResearchClaw、ARIS、FAROS、LearningCircuit 都接入 S2 Graph API。它适合拿 paperId、abstract、citationCount、externalIds、openAccessPdf、tldr、references/citations。S2 对去重和 citation graph 很有价值，但也需要 API key、速率限制和 429 backoff。

### OpenAlex

AutoResearchClaw、ARIS、LearningCircuit 使用 OpenAlex。OpenAlex 的优势是开放、metadata 丰富、机构/概念/venue/OA 信息较好；缺点是 abstract 通常是 inverted index，需要还原。

### CrossRef/DOI

AutoResearchClaw、ARIS、Orchestra skills 都把 CrossRef/DOI 作为 citation verify 的关键层。它不适合全文阅读，但适合验证 DOI 是否真实、题名是否匹配、BibTeX 是否可靠。

### Google Scholar/Serper/Scholarly

Alibaba 用 Serper Scholar；AutoResearchClaw 支持 scholarly；MiroThinker 用 Serper 搜索。Scholar 类工具召回好，但稳定性、合规和结构化程度不如 S2/OpenAlex/arXiv。建议作为补充，不作为唯一事实源。

### Web Reader

Alibaba、deer-flow、MiroThinker、STORM、AutoResearchClaw 都采用网页 reader：Jina、trafilatura、Crawl4AI、Firecrawl、readability、Playwright。共同模式是：搜索只返回候选；真正进入上下文前必须 fetch 正文、转 markdown/text、截断、再抽取 query-relevant evidence。

### PDF/文件转换

主要技术路线：

- `pymupdf4llm`：deer-flow 优先使用，适合转 Markdown。
- MarkItDown：deer-flow fallback，MiroThinker 通过 MCP 使用。
- pdfminer/pdfplumber：Alibaba 用于文本和表格提取。
- PyMuPDF/fitz：AutoResearchClaw 用于 PDF 文本、metadata、section boundary。
- pdftotext/poppler：ARIS 用于 paper style、PDF 编译检查和轻量提取。
- LangChain loaders：LearningCircuit 支持最多文档格式，并接入 splitter/embedding/RAG。

最成熟的组合是：**原始文件落盘 + hash/cache + PDF 转 Markdown + fallback parser + section/table extraction + chunking + metadata/provenance + vector index**。

## 对 chemagent ingestion 层的建议

chemagent 的目标不是做通用聊天检索，而是为化学实验设计提供可追溯、可审计、可阻断执行的知识层。建议采用“结构化 KB + 文档 RAG + 验证队列”的混合架构。

### 1. Source Connector 层

建议把资料来源统一成 connector：

- 本地知识库：`research_agent/chem_kb`、本地 PDF、Markdown、SOP、历史实验记录、仪器说明书。
- 学术源：arXiv、Semantic Scholar、OpenAlex、CrossRef/DOI。
- 化学专用源：PubChem、材料数据库、专利/标准/SDS 数据源。如果短期没有专用 API，也要预留 connector interface。
- Web reader：Jina/Crawl4AI/Playwright/Tavily/SearXNG 任选可用实现。
- 用户上传：PDF、DOCX、PPTX、XLSX、CSV、图片说明等。

connector 只负责“发现和抓取”，不直接把内容塞给 LLM。

### 2. Canonical KnowledgeItem

所有资料统一落成结构化对象：

```yaml
id: sha256/source-native-id
source: arxiv | semantic_scholar | openalex | local_pdf | web | pubchem
title: ...
authors: [...]
year: ...
doi: ...
arxiv_id: ...
url: ...
pdf_path: ...
markdown_path: ...
raw_path: ...
content_hash: ...
verification_status: verified | unverified | suspicious | failed
license: ...
provenance:
  fetched_at: ...
  connector: ...
  query: ...
  tool_version: ...
chem_tags:
  materials: [...]
  methods: [...]
  hazards: [...]
  measurements: [...]
```

这样后续检索、引用、去重、审计都基于同一套 metadata，而不是散落在 prompt 文本里。

### 3. PDF 转可读文件流程

建议采用多级 fallback：

1. 保存原始 PDF，并计算 hash，避免重复下载。
2. 首选 `pymupdf4llm` 转 Markdown。
3. 如果页均字符过少或 Markdown 质量差，fallback 到 PyMuPDF/pdfplumber/pdfminer。
4. 表格单独抽取为 Markdown table 或 CSV。
5. 识别 section heading、abstract、methods、results、electrochemical tests、synthesis、safety notes。
6. 输出 `paper.md`、`metadata.json`、`tables/`、`figures_manifest.json`。
7. 对 Markdown 分块，写入 vector index，同时保留 source location。

这个设计综合了 LearningCircuit 的 RAG、deer-flow 的 Markdown 转换、Alibaba 的表格解析和 AutoResearchClaw 的 section extraction。

### 4. Verification 队列

建议把 ARIS/AutoResearchClaw 的验证流程前置：

- arXiv ID 能验证则标 verified_preprint。
- DOI/CrossRef 能验证则标 verified_doi。
- Semantic Scholar/OpenAlex 标题、作者、年份相符则提升置信度。
- 只在网页或 LLM 输出中出现、无法被数据库确认的 paper 标 `[UNVERIFIED]`。
- citation、claim、实验条件必须保留 evidence pointer，例如 `source_id + section + chunk_id + quote/span`。

LLM 生成的参考文献不允许直接进入 verified library，只能进入 candidate queue。

### 5. 双层存储：Research Wiki + Vector Index

建议同时使用两种存储：

- **Research Wiki**：Markdown/JSONL，适合人读和审计。目录可包括 `papers/`、`protocols/`、`claims/`、`materials/`、`experiments/`、`safety/`、`graph/edges.jsonl`。
- **Vector Index**：FAISS/Qdrant/Chroma，适合相似检索。只索引已经转换成 canonical Markdown 的内容；chunk 必须带 `source_id`、section、page、hash。

Research Wiki 借鉴 ARIS；Vector Index 借鉴 LearningCircuit。不要只做向量库，否则很难审计实验建议的来源。

### 6. 化学实验安全和下发阻断

由于 chemagent 涉及实验设计，ingestion 和 planning 必须与“实验下发”隔离：

- ingestion 层只读外部资料和本地 KB，不触发仪器、机器人或工作站。
- 实验 proposal 只能写入 review queue，例如 `runs/<id>/proposed_experiment.md`。
- 任何包含真实执行、仪器控制、试剂配比下发、工作站脚本运行的动作，必须经过独立 approval gate。
- 安全资料不足时，输出缺口，而不是补全细节。
- 对危险试剂、气体、高温高压、电化学窗口、废液处置、SDS 缺失项单独打 tag。

### 7. 推荐 MVP

第一版 ingestion 层可以按这个顺序实现：

1. 扫描 `research_agent/chem_kb`，识别 PDF/MD/DOCX/XLSX/CSV。
2. PDF 转 Markdown，保存 raw/hash/metadata。
3. 建立 `KnowledgeItem` registry 和去重逻辑。
4. 对 Markdown chunk embedding，建立 FAISS 本地索引。
5. 接入 arXiv/Semantic Scholar/OpenAlex/CrossRef，只做 metadata + abstract + PDF URL。
6. 实现 verify queue，所有候选 paper 先验证再入库。
7. 实现 retrieval API：输入 query，返回 top chunks + source metadata + verification status。
8. 实现 claim/evidence logger：任何实验建议必须记录引用到的 chunk。

这样可以先把“资料可读、可检索、可追溯”跑通，再扩展 PubChem、专利、SDS、仪器手册和网页 reader。

## 最重要的工程取舍

1. **不要让 LLM 直接管理知识库**。LLM 可以提出搜索词、抽取字段、总结证据，但资料下载、hash、去重、验证、索引必须由确定性代码完成。
2. **不要把 PDF 原文一次性塞进 prompt**。先转 Markdown，分 section/chunk，再按 query 检索。
3. **不要把搜索结果当证据**。搜索结果只是 candidate，必须 fetch 正文或验证 metadata。
4. **不要把 paper metadata 和 claim evidence 混在一起**。paper 存在不代表它支持某个实验建议；claim 需要单独 evidence pointer。
5. **不要把实验设计和实验下发放在同一条链路**。对于化学 agent，proposal、review、dispatch 必须是三个不同边界。

## 结论

如果只选择一个仓库作为 ingestion 层蓝本，LearningCircuit local-deep-research 最完整；如果只选择一个仓库作为 research governance 蓝本，ARIS 最完整；如果要做化学实验 agent，最佳路线是把两者合并：用 LearningCircuit 的文件/RAG pipeline 处理 `chem_kb`，用 ARIS/AutoResearchClaw 的验证和 research-wiki 管住 paper、claim 和实验依据，再用 deer-flow/Alibaba/MiroThinker 的 reader 工具补足网页与复杂文件转换。
