# 化学知识库设计方案 (简化版)

## 一、核心需求

### 知识库需要输出两种类型的知识

**类型1：散装知识（知识片段）**
- 随查询问题动态变化
- 来自论文中的段落、章节
- 用于回答理化性质、原理、机制等问题
- 示例查询："普鲁士蓝的晶体结构是什么？""姜泰勒效应的影响是什么？"

**类型2：完整实验流程**
- 结构化、完整的实验方案
- 细化到论文中提到的**所有步骤和参数**
- 用于直接指导实验执行
- 示例查询："共沉淀法制备高熵普鲁士蓝的流程"

**两种类型都需要支持 top-k 输出**

---

## 二、技术选型

| 组件 | 选择 | 说明 |
|------|------|------|
| PDF解析 | PyMuPDF | 快速准确 |
| 中文分词 | jieba | 成熟稳定 |
| 检索算法 | BM25 | 经典算法 |
| 数据存储 | JSON | 简单直接 |
| LLM使用 | 智能分析：段落分类、元数据提取、实验方案提取 | 提高质量和准确性，减少规则维护成本 |

---

## 三、知识库内部结构

### 3.1 目录结构

```
/workspace/chem_resources/knowledge_base/
├── raw_papers/              # 原始PDF论文存储
│   └── 普鲁士蓝/
│       └── *.pdf           # 46篇普鲁士蓝相关论文（只读，不修改）
│
├── parsed/                  # PDF解析结果存储
│   ├── papers/             # 单篇论文的解析结果
│   │   └── paper_*.json
│   └── index.json         # 论文索引：所有论文的ID、标题、文件名映射
│
├── fragments/               # 散装知识片段存储
│   └── fragment_*.json     # 单个知识片段：fragment_id, content, tokens, metadata
│
├── workflows/               # 实验流程存储
│   └── workflow_*.json    # 单个实验流程：元数据, 实验方案原文/中文, 分步骤描述
│
├── indexes/                 # 索引文件（加速检索）
│   ├── fragments_bm25_model.pkl
│   ├── fragments_corpus.pkl
│   ├── fragments_metadata.pkl
│   └── workflows_index.json
│
└── metadata/               # 元数据文件
    ├── statistics.json
    └── keywords_mapping.json
```

### 3.1.1 文件详细说明与字段解析

#### parsed/papers/paper_*.json

**作用**：单篇论文的解析结果，存储论文全文和段落结构，是后续内容分类和片段生成的基础数据。

**生成原子操作**：原子操作2（parse_single_pdf）

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| paper_id | str | 是 | 论文唯一标识，格式为"paper_001", "paper_002"等 | "paper_001" | **硬编码逻辑**：内部生成，格式为"paper_" + 三位序号 |
| filename | str | 是 | 原PDF文件名 | "01_Angew_高熵普鲁士蓝类似物用于锂硫电池.pdf" | **硬编码逻辑**：从PDF文件路径提取 |
| title | str | 是 | 论文标题 | "高熵普鲁士蓝类似物用于锂硫电池" | **硬编码逻辑**：从PDF首几行提取，失败则使用文件名 |
| full_text | str | 是 | 论文全文文本 | "Prussian blue analogues have attracted..." | **硬编码逻辑**：使用PyMuPDF逐页提取并拼接 |
| paragraphs | list | 是 | 段落列表，每项包含para_id和text | [{"para_id": "p001", "text": "第一段内容..."}] | **硬编码逻辑**：按句号（.?!。）分割全文，控制每段500-800字符，保持句子完整性 |

**paragraphs数组项详解**：

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| para_id | str | 是 | 段落唯一标识，格式为"p001", "p002"等 | "p001" | **硬编码逻辑**：内部生成，格式为"p" + 三位序号 |
| text | str | 是 | 段落文本内容 | "第一段内容..." | **硬编码逻辑**：从full_text按句号分割得到，每段500-800字符 |

#### parsed/index.json

**作用**：全局论文索引，提供所有论文的快速查找和映射。

**生成原子操作**：原子操作3（update_paper_index）

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| papers | list | 是 | 所有论文的列表 | [{"paper_id": "paper_001", "filename": "...", "title": "..."}] | **硬编码逻辑**：从parsed/papers/目录下所有paper_*.json读取并汇总 |
| total_count | int | 是 | 论文总数 | 46 | **硬编码逻辑**：len(papers) |

#### fragments/fragment_*.json

**作用**：单个散装知识片段，是混合检索（BM25+LLM）的基本单元。存储论文中的段落、章节或语义单元，用于回答知识查询。

**生成原子操作**：原子操作4（llm_intelligent_analysis）→ 散装知识处理部分

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| fragment_id | str | 是 | 片段唯一标识，格式为"frag_001", "frag_002"等 | "frag_001" | **硬编码逻辑**：内部生成，格式为"frag_" + 三位序号 |
| paper_id | str | 是 | 来源论文ID，关联到parsed/papers/ | "paper_001" | **硬编码逻辑**：从paper对象获取 |
| paper_title | str | 是 | 论文标题，用于结果展示 | "高熵普鲁士蓝类似物用于锂硫电池" | **硬编码逻辑**：从paper对象获取 |
| content | str | 是 | 片段文本内容，检索的核心内容 | "普鲁士蓝类似物具有面心立方结构..." | **硬编码逻辑**：从paper["paragraphs"]中根据para_id获取对应text |
| **summary** | **str** | **是** | **简报摘要（50-100字），用于BM25和LLM检索** | **"普鲁士蓝类似物具有面心立方结构(Fm-3m)，晶格参数约10.2Å，Fe-C-N-Fe线性配位，开放三维骨架有利于离子快速传输。"** | **LLM提取**：从分段内容中提取核心信息，控制在50-100字 |
| **summary_tokens** | **list** | **是** | **summary的分词结果，用于BM25计算** | **["普鲁士蓝", "类似物", "面心", "立方", "结构", "空间群", "Fm-3m", "晶格", "参数", ...]** | **硬编码逻辑**：使用jieba.cut对summary分词，去除停用词 |
| content_type | str | 是 | 内容类型，8个预定义类别之一 | "晶体结构" | **LLM提取**：从段落语义分析，选项：晶体结构、合成制备、电化学性能、反应机理、表征方法、稳定性研究、应用场景、其他 |
| entities | list | 否 | 提取的实体（化学物质、元素、表征方法等） | ["普鲁士蓝", "面心立方", "空间群Fm-3m"] | **LLM提取**：从段落文本中提取 |
| keywords | list | 否 | 关键词，用于辅助检索 | ["晶体结构", "电化学性能"] | **LLM提取**：从段落文本中提取5-10个关键词 |

#### workflows/workflow_*.json

**作用**：单个实验流程，存储从论文中提取的结构化实验方案。包含元数据用于过滤检索，中文描述供Pre-flow Agent转换为标准txt格式。

**生成原子操作**：原子操作4（llm_intelligent_analysis）→ 实验方案提取部分

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| workflow_id | str | 是 | 工作流唯一标识，格式为"workflow_001", "workflow_002"等 | "workflow_001" | **硬编码逻辑**：内部生成，格式为"workflow_" + 三位序号 |
| paper_id | str | 是 | 来源论文ID | "paper_001" | **硬编码逻辑**：从paper对象获取 |
| paper_title | str | 是 | 论文标题 | "高熵普鲁士蓝类似物用于锂硫电池" | **硬编码逻辑**：从paper对象获取 |
| **summary** | **str** | **是** | **简报摘要（80-150字），用于BM25和LLM检索** | **"共沉淀法制备高熵普鲁士蓝类似物(Fe/Mn/Co/Ni/Zn)：溶液A(K3[Fe(CN)6])和溶液B(金属氯化物)在60°C混合搅拌2h，陈化12h，离心洗涤后60°C真空干燥12h。"** | **LLM提取**：从实验方案中提取核心信息，包含方法、材料、关键步骤、条件，控制在80-150字 |
| **summary_tokens** | **list** | **是** | **summary的分词结果，用于BM25计算** | **["共沉淀法", "制备", "高熵", "普鲁士蓝", "类似物", "溶液", "搅拌", "陈化", "离心", "洗涤", "干燥", ...]** | **硬编码逻辑**：使用jieba.cut对summary分词，去除停用词 |

**元数据字段详解**（用于检索过滤）：

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| method_type | str | 是 | 合成方法类型 | "共沉淀法" | **LLM提取**：从实验方案文本识别，选项：共沉淀法、水热法、络合结晶法、sol-gel法、电沉积法等 |
| target_material | str | 是 | 目标材料名称 | "高熵普鲁士蓝类似物" | **LLM提取**：从实验方案文本提取 |
| elements | list | 是 | 包含的化学元素符号 | ["Fe", "Mn", "Co", "Ni", "Zn"] | **LLM提取**：从实验方案文本提取 |

**实验方案字段详解**：

| 字段名 | 类型 | 必填 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|------|
| 实验方案_中文 | str | 是 | 中文翻译（如果原文是英文）或原文 | "高熵普鲁士蓝类似物的合成：通常将1.658 g..." | **LLM提取**：如果原文是英文则翻译，中文论文直接复制 |
| 分步骤描述 | list | 是 | 用序号组织的步骤列表 | ["1. 配制溶液A：...", "2. 配制溶液B：...", ...] | **LLM提取**：必须包含所有步骤，每个步骤要有用量、参数、时间、温度等 |

#### indexes/fragments_summary_bm25_model.pkl

**作用**：BM25模型对象，由rank_bm25库生成，基于summary_tokens构建。用于两阶段检索的第一阶段（BM25粗筛）。

**生成原子操作**：原子操作5（build_summary_bm25_index）

**数据结构**：BM25Okapi对象的序列化结果，包含：
| 字段 | 说明 | 来源 |
|------|------|------|
| corpus | summary的分词语料库 | **硬编码逻辑**：从所有fragment的summary_tokens字段提取 |
| idf | 逆文档频率 | **硬编码逻辑**：BM25Okapi自动计算 |
| epsilon | IDF下界平滑参数 | **硬编码逻辑**：BM25Okapi默认值（0.25） |
| k1 | 词频饱和参数 | **硬编码逻辑**：配置文件设定（默认1.5） |
| b | 长度归一化参数 | **硬编码逻辑**：配置文件设定（默认0.75） |

#### indexes/fragments_summary_corpus.pkl

**作用**：所有片段summary的分词语料库，BM25模型的输入数据。

**生成原子操作**：原子操作5（build_summary_bm25_index）

**数据结构**：list of list，每个子列表是一个fragment的summary_tokens字段，例如：
```python
[
    ["普鲁士蓝", "类似物", "面心", "立方", "结构", ...],  # frag_001的summary_tokens
    ["PBA", "钠离子", "电池", "性能", ...],           # frag_002的summary_tokens
    ...
]
```

**来源**：**硬编码逻辑**：从fragments/目录下所有fragment_*.json的summary_tokens字段提取，按文件名排序

#### indexes/fragments_summary_metadata.pkl

**作用**：fragment_id到summary的映射字典，用于BM25检索后根据fragment_id获取完整信息。

**生成原子操作**：原子操作5（build_summary_bm25_index）

**数据结构**：
```python
{
    "frag_001": {"fragment_id": "frag_001", "summary": "普鲁士蓝类似物具有面心立方结构..."},
    "frag_002": {"fragment_id": "frag_002", "summary": "高熵普鲁士蓝类似物在钠离子电池中..."},
    ...
}
```

**来源**：**硬编码逻辑**：从fragments/目录下所有fragment_*.json读取，以fragment_id为键

#### indexes/workflows_summary_bm25_model.pkl

**作用**：BM25模型对象，由rank_bm25库生成，基于summary_tokens构建。用于两阶段检索的第一阶段（BM25粗筛）。

**生成原子操作**：原子操作5（build_summary_bm25_index）

**数据结构**：BM25Okapi对象的序列化结果，包含：
| 字段 | 说明 | 来源 |
|------|------|------|
| corpus | summary的分词语料库 | **硬编码逻辑**：从所有workflow的summary_tokens字段提取 |
| idf | 逆文档频率 | **硬编码逻辑**：BM25Okapi自动计算 |
| epsilon | IDF下界平滑参数 | **硬编码逻辑**：BM25Okapi默认值（0.25） |
| k1 | 词频饱和参数 | **硬编码逻辑**：配置文件设定（默认1.5） |
| b | 长度归一化参数 | **硬编码逻辑**：配置文件设定（默认0.75） |

#### indexes/workflows_summary_corpus.pkl

**作用**：所有工作流summary的分词语料库，BM25模型的输入数据。

**生成原子操作**：原子操作5（build_summary_bm25_index）

**数据结构**：list of list，每个子列表是一个workflow的summary_tokens字段

**来源**：**硬编码逻辑**：从workflows/目录下所有workflow_*.json的summary_tokens字段提取，按文件名排序

#### indexes/workflows_summary_metadata.pkl

**作用**：workflow_id到summary的映射字典，用于BM25检索后根据workflow_id获取完整信息。

**生成原子操作**：原子操作5（build_summary_bm25_index）

**数据结构**：
```python
{
    "workflow_001": {"workflow_id": "workflow_001", "summary": "共沉淀法制备高熵普鲁士蓝类似物..."},
    "workflow_002": {"workflow_id": "workflow_002", "summary": "水热法制备MnFe-PBA..."},
    ...
}
```

**来源**：**硬编码逻辑**：从workflows/目录下所有workflow_*.json读取，以workflow_id为键

#### metadata/statistics.json

**作用**：知识库统计信息，用于监控构建状态和了解库容量。

**生成原子操作**：原子操作6（generate_statistics）

| 字段名 | 类型 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|
| total_papers | int | 论文总数 | 46 | **硬编码逻辑**：从parsed/index.json的total_count读取 |
| total_fragments | int | 散装知识片段总数 | ~1500 | **硬编码逻辑**：统计fragments/目录下fragment_*.json文件数量 |
| total_workflows | int | 实验流程总数 | ~35 | **硬编码逻辑**：统计workflows/目录下workflow_*.json文件数量 |
| content_type_distribution | dict | 各内容类型的数量分布 | {"晶体结构": 245, "电化学性能": 312, ...} | **硬编码逻辑**：遍历所有fragment，统计"content_type"的分布 |
| last_updated | str | 最后更新时间 | "2026-02-02 12:00:00" | **硬编码逻辑**：当前系统时间 |
| build_status | str | 构建状态 | "completed" / "in_progress" / "failed" | **硬编码逻辑**：构建完成后设置为"completed"，失败时设置为"failed" |
| build_duration | str | 构建耗时（人类可读格式） | "1分45秒" | **硬编码逻辑**：构建结束时间 - 构建开始时间，转换为人类可读格式 |

#### metadata/keywords_mapping.json

**作用**：同义词和分类映射，用于查询预处理时进行同义词扩展，提高召回率。支持中英文术语。

**生成原子操作**：原子操作7（initialize_keywords_mapping）

| 字段名 | 类型 | 说明 | 示例 | 来源 |
|--------|------|------|------|------|
| synonyms | dict | 同义词映射，key为标准名称，value为别名列表 | {"普鲁士蓝": ["PB", "PBA", "Prussian Blue"], "共沉淀法": ["coprecipitation", "共沉淀"]} | **混合来源**：预定义基础同义词 + 从workflows提取的实际method_types和elements |
| categories | dict | 分类映射，key为类别名，value为该类别的所有选项 | {"合成方法": ["共沉淀法", "水热法", "络合结晶法"], "掺杂元素": ["Mn", "Fe", "Co", "Ni", "Cu"]} | **混合来源**：预定义基础分类 + 从workflows提取的实际数据更新 |

**synonyms字段详解**：

| 字段 | 来源 |
|------|------|
| 预定义同义词（如"普鲁士蓝"、"共沉淀法"、"比容量"、"循环寿命"） | **硬编码逻辑**：配置文件预定义 |
| method_types相关同义词 | **硬编码逻辑**：从workflows的"元数据.method_type"提取并去重 |
| elements相关同义词 | **硬编码逻辑**：从workflows的"元数据.elements"提取并去重 |

**categories字段详解**：

| 字段 | 来源 |
|------|------|
| 合成方法 | **硬编码逻辑**：从workflows的"元数据.method_type"提取并去重 |
| 掺杂元素 | **硬编码逻辑**：从workflows的"元数据.elements"提取并去重 |
| 电池类型 | **硬编码逻辑**：预定义（["钠离子电池", "钾离子电池", "锂离子电池", "锂硫电池"]） |
| 表征技术 | **硬编码逻辑**：预定义（["XRD", "SEM", "TEM", "XPS", "BET", "FTIR", "Raman"]） |

### 3.2 核心数据结构（简化版）

#### 3.2.1 解析论文

```json
{
    "paper_id": "paper_001",
    "filename": "01_Angew_高熵普鲁士蓝类似物用于锂硫电池.pdf",
    "title": "从论文中提取",
    "full_text": "论文全文文本...",
    "paragraphs": [
        {"para_id": "p001", "text": "第一段内容..."},
        {"para_id": "p002", "text": "第二段内容..."}
    ]
}
```

#### 3.2.2 散装知识片段

```json
{
    "fragment_id": "frag_001",
    "paper_id": "paper_001",
    "paper_title": "高熵普鲁士蓝类似物用于锂硫电池",
    "content": "普鲁士蓝类似物具有面心立方结构，空间群Fm-3m。晶格参数约为10.2Å，Fe-C-N-Fe呈现线性配位。这种开放的三维骨架结构有利于离子的快速传输，使得PBA材料在电化学储能领域具有广阔的应用前景。",
    "tokens": ["普鲁士蓝", "类似物", "具有", "面心", "立方", "结构", ...],
    "metadata": {
        "char_count": 126,
        "content_type": "晶体结构",
        "source_section": "introduction"
    },
    "entities": ["普鲁士蓝", "面心立方", "空间群Fm-3m", "Fe-C-N-Fe"],
    "keywords": ["晶体结构", "电化学性能", "离子传输"]
}
```

**字段说明**：

| 字段 | 必填 | 说明 | 来源 |
|------|------|------|------|
| fragment_id | 是 | 唯一标识 | **硬编码逻辑**：内部生成 |
| paper_id | 是 | 来源论文 | **硬编码逻辑**：从paper对象获取 |
| paper_title | 是 | 论文标题 | **硬编码逻辑**：从paper对象获取 |
| content | 是 | 片段内容 | **硬编码逻辑**：从paragraphs获取对应段落文本 |
| tokens | 是 | 分词结果，用于BM25 | **硬编码逻辑**：jieba分词 |
| metadata | 是 | 元数据 | - |
| metadata.char_count | 是 | 字符数 | **硬编码逻辑**：len(content) |
| metadata.content_type | 是 | 内容类型 | **LLM提取**：8个预定义类别之一 |
| metadata.source_section | 否 | 来源章节（如有） | **硬编码逻辑**：尝试提取章节信息 |
| entities | 否 | 提取的实体 | **LLM提取**：化学物质、元素、表征方法等 |
| keywords | 否 | 关键词 | **LLM提取**：5-10个关键词 |

#### 3.2.3 实验流程

```json
{
    "workflow_id": "workflow_001",
    "paper_id": "paper_001",
    "paper_title": "高熵普鲁士蓝类似物用于锂硫电池",

    "元数据": {
        "method_type": "共沉淀法",
        "target_material": "高熵普鲁士蓝类似物",
        "elements": ["Fe", "Mn", "Co", "Ni", "Zn"],
        "application": "钠离子电池正极材料",
        "temperature_range": [25, 60],
        "keywords": ["共沉淀", "高熵", "钠离子电池"]
    },

    "实验方案_原文": "Synthesis of HE-PBA: Typically, 1.658 g (5 mmol) of K3[Fe(CN)6]·3H2O was dissolved in 50 mL deionized water to form solution A. Separately, MnCl2·4H2O (1.979 g, 5 mmol), CoCl2·6H2O (2.377 g, 5 mmol), NiCl2·6H2O (2.376 g, 5 mmol), and ZnCl2 (1.363 g, 5 mmol) were dissolved in 250 mL deionized water to form solution B. Solution B was added dropwise into solution A at a rate of 2 mL/min under vigorous stirring at 60°C. After complete addition, the mixture was stirred for an additional 2 h. The resulting precipitate was aged for 12 h at room temperature, then collected by centrifugation at 8000 rpm for 5 min. The product was washed three times with deionized water and twice with ethanol, then dried under vacuum at 60°C for 12 h.",

    "实验方案_中文": "高熵普鲁士蓝类似物的合成：通常将1.658 g (5 mmol)的K3[Fe(CN)6]·3H2O溶于50 mL去离子水中形成溶液A。另外，将MnCl2·4H2O (1.979 g, 5 mmol)、CoCl2·6H2O (2.377 g, 5 mmol)、NiCl2·6H2O (2.376 g, 5 mmol)和ZnCl2 (1.363 g, 5 mmol)溶于250 mL去离子水中形成溶液B。在60°C剧烈搅拌下，以2 mL/min的速率将溶液B逐滴加入溶液A中。完全加入后，继续搅拌2 h。所得沉淀在室温下陈化12 h，然后以8000 rpm离心5 min收集。产物用去离子水洗涤3次，用乙醇洗涤2次，然后在60°C真空干燥12 h。",

    "分步骤描述": [
        "1. 配制溶液A：将1.658 g (5 mmol)的K3[Fe(CN)6]·3H2O溶于50 mL去离子水中",
        "2. 配制溶液B：将MnCl2·4H2O (1.979 g, 5 mmol)、CoCl2·6H2O (2.377 g, 5 mmol)、NiCl2·6H2O (2.376 g, 5 mmol)和ZnCl2 (1.363 g, 5 mmol)溶于250 mL去离子水中",
        "3. 共沉淀反应：在60°C剧烈搅拌下，以2 mL/min的速率将溶液B逐滴加入溶液A中",
        "4. 继续搅拌：完全加入后，继续搅拌2 h",
        "5. 陈化：所得沉淀在室温下陈化12 h",
        "6. 离心分离：以8000 rpm离心5 min收集沉淀",
        "7. 洗涤：用去离子水洗涤3次，用乙醇洗涤2次",
        "8. 干燥：在60°C真空干燥12 h"
    ],

    "性能数据": {
        "specific_capacity": "130 mAh/g",
        "cycle_life": "2000 cycles"
    },

    "表征方法": ["XRD", "SEM", "TEM", "XPS"],

    "source": {
        "section": "Experimental Section",
        "pages": [2, 4]
    }
}
```

**字段说明**：

| 字段 | 必填 | 说明 | 来源 |
|------|------|------|------|
| workflow_id | 是 | 唯一标识 | **硬编码逻辑**：内部生成 |
| paper_id | 是 | 来源论文 | **硬编码逻辑**：从paper对象获取 |
| paper_title | 是 | 论文标题 | **硬编码逻辑**：从paper对象获取 |
| 元数据 | 是 | 用于检索过滤的字段 | - |
| 元数据.method_type | 是 | 合成方法类型 | **LLM提取** |
| 元数据.target_material | 是 | 目标材料名称 | **LLM提取** |
| 元数据.elements | 是 | 包含的元素列表 | **LLM提取** |
| 元数据.application | 否 | 应用场景 | **LLM提取** |
| 元数据.temperature_range | 否 | 温度范围 | **LLM提取** |
| 元数据.keywords | 否 | 关键词 | **LLM提取** |
| 实验方案_原文 | 是 | 论文原文（保留） | **LLM提取** |
| 实验方案_中文 | 是 | 中文翻译或原文 | **LLM提取** |
| 分步骤描述 | 是 | 用序号组织的步骤列表 | **LLM提取** |
| 性能数据 | 否 | 如有则记录 | **LLM提取** |
| 表征方法 | 否 | 如有则记录 | **LLM提取** |
| source | 是 | 来源信息 | - |
| source.section | 否 | 章节名称 | **LLM提取** |
| source.pages | 否 | 页码范围 | **LLM提取** |

---

## 四、知识库构建流程（原子操作规范）

### 4.1 执行流程概览

```
初始化阶段
  ↓
原子操作1：扫描PDF文件 → 生成论文列表
  ↓
原子操作2：解析单篇PDF → 生成paper_*.json
  ↓
原子操作3：更新论文索引 → 更新parsed/index.json
  ↓
原子操作4：调用LLM进行智能分析 → 生成fragment_*.json 和 workflow_*.json
  ├→ 段落分类（散装知识 / 实验段落）
  ├→ 提取散装知识元数据（content_type、entities、keywords、summary）
  └→ 提取实验方案（method_type、summary、步骤等）
  ↓
原子操作5：构建BM25索引（基于summary） → 生成indexes/*_summary_bm25.pkl
  ↓
原子操作6：生成统计信息 → 更新metadata/statistics.json
  ↓
原子操作7：初始化同义词映射 → 生成metadata/keywords_mapping.json
```

### 4.2 流式日志规范

**日志格式**：纯文本流式输出，不使用JSON结构

**日志级别**：INFO, WARNING, ERROR

**日志输出位置**：标准输出（stdout）和日志文件

**日志格式规范**：
```
[时间戳] [级别] [操作名称] 消息内容
```

**示例**：
```
[2026-02-02 10:30:15] [INFO] [scan_pdfs] 开始扫描PDF文件...
[2026-02-02 10:30:15] [INFO] [scan_pdfs] 扫描目录：/workspace/chem_resources/knowledge_base/raw_papers/普鲁士蓝/
[2026-02-02 10:30:16] [INFO] [scan_pdfs] 发现46个PDF文件
[2026-02-02 10:30:17] [INFO] [parse_single_pdf] 开始解析：01_Angew_高熵普鲁士蓝类似物用于锂硫电池.pdf
[2026-02-02 10:30:18] [INFO] [parse_single_pdf] 提取到127个段落，保存到parsed/papers/paper_001.json
[2026-02-02 10:30:19] [WARNING] [parse_single_pdf] 文件损坏，跳过：05_corrupted.pdf
[2026-02-02 10:31:00] [INFO] [extract_workflow_with_llm] 调用LLM提取实验方案...
[2026-02-02 10:31:05] [INFO] [extract_workflow_with_llm] 成功提取1个workflow
[2026-02-02 10:32:00] [INFO] [build_complete] 知识库构建完成，耗时：1分45秒
```

**关键节点日志**：
- 操作开始：`[INFO] [操作名] 开始...`
- 操作成功：`[INFO] [操作名] 完成，结果：...`
- 操作失败：`[ERROR] [操作名] 失败，原因：...`
- 数据统计：`[INFO] [操作名] 统计：共N个...`

---

### 4.3 原子操作详细规范

#### 原子操作1：扫描PDF文件

**操作名称**：scan_pdfs

**目的**：扫描原始PDF论文目录，生成待处理的论文文件列表

**输入**：
| 字段 | 来源 |
|------|------|
| PDF目录路径 | /workspace/chem_resources/knowledge_base/raw_papers/普鲁士蓝/ |

**处理逻辑**：
1. 打开目录/workspace/chem_resources/knowledge_base/raw_papers/普鲁士蓝/
2. 遍历目录下所有.pdf后缀的文件
3. 按文件名字母顺序排序
4. 为每个文件分配序号（从001开始）
5. 输出日志：`[INFO] [scan_pdfs] 扫描到N个PDF文件`

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| pdf_file_list | PDF文件绝对路径列表 | 内存变量（传递给原子操作2） |
| paper_count | PDF文件总数 | 日志输出 |

**日志输出**：
```
[INFO] [scan_pdfs] 开始扫描PDF文件...
[INFO] [scan_pdfs] 扫描目录：/workspace/chem_resources/knowledge_base/raw_papers/普鲁士蓝/
[INFO] [scan_pdfs] 扫描到46个PDF文件
```

---

#### 原子操作2：解析单篇PDF

**操作名称**：parse_single_pdf

**目的**：使用PyMuPDF解析单个PDF文件，提取全文和段落结构

**输入**：
| 字段 | 来源 |
|------|------|
| pdf_file_path | 原子操作1输出的pdf_file_list中的当前项 |
| paper_id | 内部生成（格式：paper_001, paper_002...） |
| 文件序号 | 原子操作1分配的序号（001, 002...） |

**处理逻辑**：
1. 使用PyMuPDF打开PDF文件
2. 逐页提取文本，拼接成full_text
3. 按句号（.?!。）分割全文，控制每段500-800字符，保持句子完整性
4. 为每个段落生成para_id（格式：p001, p002, p003...）
5. 尝试从PDF首几行提取title（如果失败则用文件名）
6. 清洗title（去除多余空格、换行符）
7. 组装paper对象并保存

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| paper_id | 论文唯一标识 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的根字段 "paper_id" |
| filename | 原始PDF文件名 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的根字段 "filename" |
| title | 论文标题 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的根字段 "title" |
| full_text | 论文全文 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的根字段 "full_text" |
| paragraphs | 段落列表 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的根字段 "paragraphs" |
| paragraphs[i].para_id | 第i个段落的唯一标识 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的 "paragraphs[{i}]["para_id"]" |
| paragraphs[i].text | 第i个段落的文本内容 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json 中的 "paragraphs[{i}]["text"]" |

**异常处理**：
- PDF解析失败 → 输出错误日志，跳过该文件，继续处理下一个
- 文件无法打开 → 输出错误日志，跳过该文件

**日志输出**：
```
[INFO] [parse_single_pdf] 开始解析：01_Angew_高熵普鲁士蓝类似物用于锂硫电池.pdf
[INFO] [parse_single_pdf] 提取到127个段落
[INFO] [parse_single_pdf] 保存到：parsed/papers/paper_001.json
[ERROR] [parse_single_pdf] 解析失败：05_corrupted.pdf，文件已损坏
```

---

#### 原子操作3：更新论文索引

**操作名称**：update_paper_index

**目的**：维护全局论文索引文件，汇总所有已解析的论文信息

**输入**：
| 字段 | 来源 |
|------|------|
| 当前paper对象 | 原子操作2输出的parsed/papers/paper_{序号}.json |
| 已有索引文件 | /workspace/chem_resources/knowledge_base/parsed/index.json（如果存在） |

**处理逻辑**：
1. 读取现有的parsed/index.json（如果不存在则创建空结构：{"papers": [], "total_count": 0}）
2. 检查当前paper_id是否已在papers列表中
3. 如果不在，添加新条目：{"paper_id": "paper_001", "filename": "...", "title": "..."}
4. 更新total_count字段
5. 保存回parsed/index.json

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| papers | 所有论文的简要信息列表 | /workspace/chem_resources/knowledge_base/parsed/index.json 中的 "papers" 字段 |
| papers[i].paper_id | 第i个论文的唯一标识 | /workspace/chem_resources/knowledge_base/parsed/index.json 中的 "papers[{i}]["paper_id"]" |
| papers[i].filename | 第i个论文的文件名 | /workspace/chem_resources/knowledge_base/parsed/index.json 中的 "papers[{i}]["filename"]" |
| papers[i].title | 第i个论文的标题 | /workspace/chem_resources/knowledge_base/parsed/index.json 中的 "papers[{i}]["title"]" |
| total_count | 论文总数 | /workspace/chem_resources/knowledge_base/parsed/index.json 中的 "total_count" 字段 |

**异常处理**：
- 索引文件损坏 → 重新创建空索引
- 写入失败 → 输出错误日志并终止程序

**日志输出**：
```
[INFO] [update_paper_index] 更新索引：paper_001
[INFO] [update_paper_index] 当前论文总数：1
[INFO] [update_paper_index] 保存索引到：parsed/index.json
```

---

#### 原子操作4：调用LLM进行智能分析

**操作名称**：llm_intelligent_analysis

**目的**：让LLM对单篇论文进行智能分析，完成段落分类、散装知识元数据提取（含summary）、实验方案结构化提取（含summary）

**输入**：
| 字段 | 来源 |
|------|------|
| paper对象 | /workspace/chem_resources/knowledge_base/parsed/papers/paper_{序号}.json |
| full_text | paper["full_text"] |
| paragraphs | paper["paragraphs"] |
| paper_id | paper["paper_id"] |
| paper_title | paper["title"] |
| LLM API配置 | 配置文件（API地址、密钥、模型参数） |

**处理逻辑**：
1. 构造prompt（使用4.4节的prompt模板），传入论文全文和段落信息
2. 调用LLM API
3. 解析LLM返回的JSON，包含：
   - 段落分类结果：每个段落的分类（散装知识/实验）、content_type、entities、keywords、summary
   - 实验方案提取结果：method_type、target_material、elements、summary、实验方案中文、分步骤描述等
4. 验证输出格式和必需字段
5. 处理散装知识段落：
   - 获取分段原文（从paragraphs列表，已按句号分段）
   - 代码计算：summary_tokens（jieba分词）
   - 组装fragment对象（包含LLM提供的content_type、entities、keywords、summary）
   - 保存到fragments/fragment_*.json
6. 处理实验方案：
   - 代码计算：summary_tokens（jieba分词）
   - 组装workflow对象（包含LLM提供的summary、method_type等）
   - 保存到workflows/workflow_*.json

**输出**：
| 字段 | 说明 | 写入位置 | 决定方 |
|------|------|---------|--------|
| fragment_id | 片段唯一标识 | fragments/fragment_*.json | 代码 |
| content | 片段文本内容 | fragments/fragment_*.json | 代码（从paragraphs获取，已按句号分段） |
| summary | 简报摘要（50-100字） | fragments/fragment_*.json | **LLM** |
| summary_tokens | summary分词结果 | fragments/fragment_*.json | 代码（jieba） |
| content_type | 内容类型 | fragments/fragment_*.json | **LLM** |
| entities | 实体列表 | fragments/fragment_*.json | **LLM** |
| keywords | 关键词列表 | fragments/fragment_*.json | **LLM** |
| workflow_id | 工作流唯一标识 | workflows/workflow_*.json | 代码 |
| summary | 简报摘要（80-150字） | workflows/workflow_*.json | **LLM** |
| summary_tokens | summary分词结果 | workflows/workflow_*.json | 代码（jieba） |
| 元数据 | 实验方案元数据 | workflows/workflow_*.json | **LLM** |
| 实验方案_中文 | 中文翻译 | workflows/workflow_*.json | **LLM** |
| 分步骤描述 | 步骤列表 | workflows/workflow_*.json | **LLM** |

**异常处理**：
- LLM调用失败 → 重试3次，仍失败则输出错误日志，跳过该论文
- JSON解析失败 → 重试1次，仍失败则输出错误日志，跳过该论文
- 输出格式验证失败 → 输出错误日志，跳过该论文
- 分类结果为空 → 输出警告日志，继续处理

**日志输出**：
```
[INFO] [llm_intelligent_analysis] 处理paper_001
[INFO] [llm_intelligent_analysis] 调用LLM API...
[INFO] [llm_intelligent_analysis] LLM返回：127个段落，35个实验段落，92个散装知识段落
[INFO] [llm_intelligent_analysis] 生成92个fragment
[INFO] [llm_intelligent_analysis] 提取1个workflow
[INFO] [llm_intelligent_analysis] 保存fragment到：fragments/fragment_001.json ~ fragment_092.json
[INFO] [llm_intelligent_analysis] 保存workflow到：workflows/workflow_001.json
[ERROR] [llm_intelligent_analysis] paper_005的LLM调用失败，重试中... (1/3)
[ERROR] [llm_intelligent_analysis] paper_005处理失败，跳过
```

**LLM Prompt模板**（见4.4节）

---

### 版本2：批次处理方案（需要实现）

**问题背景**：
- 原版设计采用一次性将整个paper的所有段落发送给LLM
- 对于大型论文（如50000+字符），会导致消息长度超出LLM API限制
- 实际测试中，即使设置了`max_paragraphs=10`和`max_chars_per_paragraph=500`，JSON序列化后的消息仍达到79740字符
- LLM API返回错误：`"Received response with null value for choices"`
- 需要采用批次处理策略解决此问题，同时保持字段来源的一致性

**批次处理策略**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| batch_size | 3 | 每批处理的段落数量（新段落更长，500-800字符） |
| overlap | 1 | 批次间重叠的段落数量，避免边界信息丢失 |

**处理逻辑**：进行这些操作直到所有文字都被处理完毕之后才能进入下一个操作
1. **段落分批**：将paragraphs列表按批次分割，每批3个段落（已按句号分段，500-800字符），批次间重叠1个段落
2. **批次处理**：对每个批次独立调用LLM API，使用相同的system prompt和task prompt
3. **结果合并**：合并所有批次的返回结果，基于para_id去重（保留更详细的版本）
4. **ID生成**：在批次处理前初始化fragment_id和workflow_id计数器，确保ID连续且唯一
5. **分词处理**：批次合并后，统一使用jieba对summary进行分词生成summary_tokens
6. **文件保存**：保存fragment到fragments/fragment_*.json，保存workflow到workflows/workflow_*.json

**新增/修改的方法**：

| 方法名 | 功能 | 说明 |
|--------|------|------|
| `_split_paragraphs_into_batches_with_overlap` | 分段（带重叠） | 将paragraphs分成多个批次，批次间有重叠段落 |
| `_process_paragraph_batch` | 处理单个批次 | 对单个批次调用LLM，解析返回结果 |
| `_merge_batch_results_with_dedup` | 合并并去重 | 合并所有批次结果，基于para_id去重 |

**ID维护策略**：
- 在调用`_split_paragraphs_into_batches_with_overlap`前，初始化`fragment_counter = 1`和`workflow_counter = 1`
- 处理每个批次时，递增计数器：`fragment_counter += 1`，`workflow_counter += 1`
- 确保fragment_id和workflow_id在整个处理过程中连续且唯一

**日志输出（批次处理版）**：
```
[INFO] [llm_intelligent_analysis] 处理paper_001
[INFO] [llm_intelligent_analysis] 共127个段落，分为26个批次（每批5段，重叠1段）
[INFO] [llm_intelligent_analysis] 处理批次1/26（段落p001-p005）
[INFO] [llm_intelligent_analysis] 调用LLM API...
[INFO] [llm_intelligent_analysis] 批次1 LLM返回长度: 4523
[INFO] [llm_intelligent_analysis] 处理批次2/26（段落p005-p009）
[INFO] [llm_intelligent_analysis] 调用LLM API...
[INFO] [llm_intelligent_analysis] 批次2 LLM返回长度: 3891
...
[INFO] [llm_intelligent_analysis] 所有批次处理完成，合并结果
[INFO] [llm_intelligent_analysis] 合并结果：92个段落分类，1个实验方案
[INFO] [llm_intelligent_analysis] 生成92个fragment
[INFO] [llm_intelligent_analysis] 提取1个workflow
[INFO] [llm_intelligent_analysis] 保存fragment到：fragments/fragment_001.json ~ fragment_092.json
[INFO] [llm_intelligent_analysis] 保存workflow到：workflows/workflow_001.json
[ERROR] [llm_intelligent_analysis] 批次15处理失败: JSON解析错误，跳过
[ERROR] [llm_intelligent_analysis] paper_005的批次3/26处理失败，重试中... (1/3)
[ERROR] [llm_intelligent_analysis] paper_005处理失败，跳过
```

**异常处理（批次处理版）**：
- **单个批次失败** → 记录错误日志，继续处理下一批次（不中断整个流程）
- **所有批次失败** → 输出错误日志，跳过该论文
- **合并失败** → 输出错误日志，终止程序
- **JSON解析失败（单个批次）** → 重试1次，仍失败则跳过该批次
- **LLM调用失败（单个批次）** → 重试3次，仍失败则跳过该批次

**输出字段（批次处理版）**：

| 字段 | 说明 | 写入位置 | 决定方 |
|------|------|---------|--------|
| fragment_id | 片段唯一标识 | fragments/fragment_*.json | **硬编码逻辑**（批次前初始化计数器） |
| content | 片段文本内容 | fragments/fragment_*.json | **硬编码逻辑**（从paragraphs获取，已按句号分段，批次间正确关联） |
| summary | 简报摘要（50-100字） | fragments/fragment_*.json | **LLM提取**（每个批次独立提取） |
| summary_tokens | summary分词结果 | fragments/fragment_*.json | **硬编码逻辑**（批次合并后统一用jieba分词） |
| content_type | 内容类型 | fragments/fragment_*.json | **LLM提取**（8个预定义类别之一） |
| entities | 实体列表 | fragments/fragment_*.json | **LLM提取**（化学物质、元素、表征方法等） |
| keywords | 关键词列表 | fragments/fragment_*.json | **LLM提取**（5-10个关键词） |
| workflow_id | 工作流唯一标识 | workflows/workflow_*.json | **硬编码逻辑**（批次前初始化计数器） |
| summary | 简报摘要（80-150字） | workflows/workflow_*.json | **LLM提取**（每个批次独立提取） |
| summary_tokens | summary分词结果 | workflows/workflow_*.json | **硬编码逻辑**（批次合并后统一用jieba分词） |
| 元数据 | 实验方案元数据 | workflows/workflow_*.json | **LLM提取**（method_type, target_material, elements等） |
| 实验方案_中文 | 中文翻译 | workflows/workflow_*.json | **LLM提取**（如果原文是英文则翻译） |
| 分步骤描述 | 步骤列表 | workflows/workflow_*.json | **LLM提取**（序号组织的步骤列表） |

**批次处理流程图**：
```
paragraphs列表
    ↓
_split_paragraphs_into_batches_with_overlap(batch_size=3, overlap=1)
    ↓
生成批次列表：[批次1, 批次2, ..., 批次N]
    ↓
遍历每个批次：
    ├→ _process_paragraph_batch(批次i)
    │   ├→ 构建批次数据（paper_id, title, paragraphs, batch_id）
    │   ├→ 构建messages（system + user prompt）
    │   ├→ 调用LLM API
    │   ├→ 解析JSON返回
    │   └→ 返回批次结果
    └→ 收集批次结果
    ↓
_merge_batch_results_with_dedup(batch_results)
    ├→ 合并段落分类结果（基于para_id去重）
    └→ 合并实验方案提取结果
    ↓
生成fragment和workflow对象
    ├→ 计算summary_tokens（jieba分词）
    └→ 保存到文件
```

**关键注意事项**：
1. **字段来源一致性**：批次处理方案中所有字段的来源标记与原版保持完全一致（除段落分割方式外：从"按双换行符分割自然段"改为"按句号分段（500-800字符）"）
2. **ID连续性**：必须在批次处理前初始化计数器，确保ID连续
3. **去重策略**：基于para_id去重时，如果同一para_id出现在多个批次中，保留summary更长的版本
4. **重叠区域处理**：重叠段落的分类结果可能会出现差异，去重时选择更详细的版本
5. **跨段落实验方案**：完整的实验方案可能跨越多个批次，合并后需要检查是否完整
6. **错误隔离**：单个批次失败不应影响其他批次的处理
7. **段落分割方式**：采用按句号（.?!。）分段的方式，控制每段500-800字符，保持句子完整性，避免PDF格式导致的自然段分割不准确问题

---

#### 原子操作5：构建BM25索引（基于summary）

**操作名称**：build_summary_bm25_index

**目的**：加载所有fragment和workflow的summary，构建BM25检索模型和索引文件

**输入**：
| 字段 | 来源 |
|------|------|
| 所有fragment文件 | /workspace/chem_resources/knowledge_base/fragments/ 目录下所有 fragment_*.json |
| 所有workflow文件 | /workspace/chem_resources/knowledge_base/workflows/ 目录下所有 workflow_*.json |
| BM25参数k1 | 配置文件（默认1.5） |
| BM25参数b | 配置文件（默认0.75） |

**处理逻辑**：
1. 遍历/workspace/chem_resources/knowledge_base/fragments/目录，按文件名排序加载所有fragment_*.json
2. 提取所有fragment的summary_tokens字段，按frag_001, frag_002...顺序组成fragments_corpus列表
3. 使用BM25Okapi(fragments_corpus, k1=k1, b=b)构建fragments模型
4. 创建fragment_id到summary的映射字典
5. 保存三个fragments索引文件到/workspace/chem_resources/knowledge_base/indexes/
6. 遍历/workspace/chem_resources/knowledge_base/workflows/目录，按文件名排序加载所有workflow_*.json
7. 提取所有workflow的summary_tokens字段，组成workflows_corpus列表
8. 使用BM25Okapi(workflows_corpus, k1=k1, b=b)构建workflows模型
9. 创建workflow_id到summary的映射字典
10. 保存三个workflows索引文件到/workspace/chem_resources/knowledge_base/indexes/

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| fragments_bm25_model | fragments的BM25模型对象 | /workspace/chem_resources/knowledge_base/indexes/fragments_summary_bm25_model.pkl |
| fragments_corpus | fragments的summary_tokens列表 | /workspace/chem_resources/knowledge_base/indexes/fragments_summary_corpus.pkl |
| fragments_metadata | fragment_id到summary的映射 | /workspace/chem_resources/knowledge_base/indexes/fragments_summary_metadata.pkl |
| workflows_bm25_model | workflows的BM25模型对象 | /workspace/chem_resources/knowledge_base/indexes/workflows_summary_bm25_model.pkl |
| workflows_corpus | workflows的summary_tokens列表 | /workspace/chem_resources/knowledge_base/indexes/workflows_summary_corpus.pkl |
| workflows_metadata | workflow_id到summary的映射 | /workspace/chem_resources/knowledge_base/indexes/workflows_summary_metadata.pkl |
| metadata映射 | fragment_id到fragment完整数据的字典 | /workspace/chem_resources/knowledge_base/indexes/fragments_metadata.pkl |

**异常处理**：
- 没有fragment文件 → 输出错误日志并终止程序
- 序列化失败 → 输出错误日志并终止程序

**日志输出**：
```
[INFO] [build_bm25_index] 加载1523个fragment
[INFO] [build_bm25_index] 构建BM25模型（k1=1.5, b=0.75）
[INFO] [build_bm25_index] 保存索引到：indexes/fragments_bm25_model.pkl
[INFO] [build_bm25_index] 保存语料库到：indexes/fragments_corpus.pkl
[INFO] [build_bm25_index] 保存元数据映射到：indexes/fragments_metadata.pkl
```

---

#### 原子操作6：生成统计信息

**操作名称**：generate_statistics

**目的**：统计知识库的各项指标，生成统计文件

**输入**：
| 字段 | 来源 |
|------|------|
| total_papers | /workspace/chem_resources/knowledge_base/parsed/index.json 中的 "total_count" 字段 |
| fragment文件 | /workspace/chem_resources/knowledge_base/fragments/ 目录下所有 fragment_*.json |
| workflow文件 | /workspace/chem_resources/knowledge_base/workflows/ 目录下所有 workflow_*.json |
| build_start_time | 内部变量（构建开始时间戳） |
| build_end_time | 内部变量（构建结束时间戳） |

**处理逻辑**：
1. 统计fragment文件数量：遍历/workspace/chem_resources/knowledge_base/fragments/目录
2. 统计workflow文件数量：遍历/workspace/chem_resources/knowledge_base/workflows/目录
3. 统计content_type分布：
   - 遍历所有fragment_*.json
   - 读取"content_type"字段
   - 统计每个类型的数量
4. 计算构建耗时：build_end_time - build_start_time
5. 设置build_status为"completed"
6. 保存到/workspace/chem_resources/knowledge_base/metadata/statistics.json

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| total_papers | 论文总数 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "total_papers" 字段 |
| total_fragments | fragment总数 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "total_fragments" 字段 |
| total_workflows | workflow总数 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "total_workflows" 字段 |
| content_type_distribution | 各内容类型的数量分布 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "content_type_distribution" 字段 |
| content_type_distribution.晶体结构 | "晶体结构"类型的fragment数量 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "content_type_distribution.晶体结构" |
| content_type_distribution.合成制备 | "合成制备"类型的fragment数量 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "content_type_distribution.合成制备" |
| content_type_distribution.电化学性能 | "电化学性能"类型的fragment数量 | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "content_type_distribution.电化学性能" |
| ...（其他content_type） | ... | ... |
| last_updated | 最后更新时间（格式：YYYY-MM-DD HH:MM:SS） | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "last_updated" 字段 |
| build_status | 构建状态（completed/failed） | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "build_status" 字段 |
| build_duration | 构建耗时（人类可读格式，如：1分45秒） | /workspace/chem_resources/knowledge_base/metadata/statistics.json 中的 "build_duration" 字段 |

**异常处理**：
- 统计失败 → 输出错误日志，设置build_status为"failed"

**日志输出**：
```
[INFO] [generate_statistics] 统计知识库信息...
[INFO] [generate_statistics] 论文总数：46
[INFO] [generate_statistics] fragment总数：1523
[INFO] [generate_statistics] workflow总数：35
[INFO] [generate_statistics] content_type分布：晶体结构245, 电化学性能312, 反应机理198, 表征方法156, 合成制备289, 稳定性研究145, 应用场景178
[INFO] [generate_statistics] 构建耗时：1分45秒
[INFO] [generate_statistics] 保存统计到：metadata/statistics.json
```

---

#### 原子操作7：初始化同义词映射

**操作名称**：initialize_keywords_mapping

**目的**：创建或更新同义词映射文件，用于查询扩展

**输入**：
| 字段 | 来源 |
|------|------|
| existing_mapping | 已有的metadata/keywords_mapping.json（如存在），从文件读取 |
| 所有workflow文件 | /workspace/chem_resources/knowledge_base/workflows/ 目录下所有 workflow_*.json |

**处理逻辑**：
1. 如果keywords_mapping.json已存在，读取并保留自定义添加的条目
2. 如果不存在，使用预定义的基础映射
3. 从所有workflow文件中提取实际的method_types和elements，更新categories
4. 合并预定义同义词和用户自定义同义词
5. 保存为metadata/keywords_mapping.json

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| synonyms | 同义词映射字典 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "synonyms" 字段 |
| categories | 分类枚举字典 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "categories" 字段 |

**预定义同义词**（基础版本）：
```json
{
    "普鲁士蓝": ["PB", "PBA", "Prussian Blue", "Prussian blue analogues", "普鲁士蓝类似物"],
    "共沉淀法": ["coprecipitation", "共沉淀", "沉淀法"],
    "比容量": ["specific capacity", "容量", "capacity", "放电比容量"],
    "循环寿命": ["cycle life", "循环性能", "cycling stability", "循环稳定性"]
}
```

**预定义分类**（基础版本，会被实际数据覆盖）：
```json
{
    "合成方法": [],
    "掺杂元素": [],
    "电池类型": ["钠离子电池", "钾离子电池", "锂离子电池", "锂硫电池"],
    "表征技术": ["XRD", "SEM", "TEM", "XPS", "BET", "FTIR", "Raman"]
}
```

**异常处理**：
- 文件损坏 → 使用基础映射重新生成
- 写入失败 → 输出错误日志并终止程序

**日志输出**：
```
[INFO] [initialize_keywords_mapping] 初始化同义词映射...
[INFO] [initialize_keywords_mapping] 加载预定义同义词：4组
[INFO] [initialize_keywords_mapping] 从workflows_index提取：4种方法，8种元素
[INFO] [initialize_keywords_mapping] 保存到：metadata/keywords_mapping.json
```

---

**操作名称**：initialize_keywords_mapping

**目的**：创建或更新同义词映射文件，用于查询扩展

**输入**：
| 字段 | 来源 |
|------|------|
| 已有映射文件 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json（如果存在） |
| workflows_index | /workspace/chem_resources/knowledge_base/indexes/workflows_index.json |
| 预定义同义词 | 配置文件 |

**处理逻辑**：
1. 如果/workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json已存在：
   - 读取并保留用户自定义添加的条目
2. 如果不存在：
   - 使用预定义的基础映射创建
3. 从workflows_index中提取：
   - method_types列表 → 更新"categories.合成方法"
   - elements列表 → 更新"categories.掺杂元素"
4. 合并预定义同义词和用户自定义同义词
5. 保存到/workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json

**输出**：
| 字段 | 说明 | 写入位置 |
|------|------|---------|
| synonyms | 同义词映射字典 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "synonyms" 字段 |
| synonyms.普鲁士蓝 | 标准名称到别名的映射 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "synonyms.普鲁士蓝" |
| synonyms.共沉淀法 | 标准名称到别名的映射 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "synonyms.共沉淀法" |
| ...（其他同义词） | ... | ... |
| categories | 分类枚举字典 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "categories" 字段 |
| categories.合成方法 | 合成方法列表 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "categories.合成方法" |
| categories.掺杂元素 | 元素列表 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "categories.掺杂元素" |
| categories.电池类型 | 电池类型列表 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "categories.电池类型" |
| categories.表征技术 | 表征技术列表 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json 中的 "categories.表征技术" |

**预定义同义词**（基础版本）：
```json
{
    "普鲁士蓝": ["PB", "PBA", "Prussian Blue", "Prussian blue analogues", "普鲁士蓝类似物"],
    "共沉淀法": ["coprecipitation", "共沉淀", "沉淀法"],
    "比容量": ["specific capacity", "容量", "capacity", "放电比容量"],
    "循环寿命": ["cycle life", "循环性能", "cycling stability", "循环稳定性"]
}
```

**预定义分类**（基础版本，会被实际数据覆盖）：
```json
{
    "合成方法": [],
    "掺杂元素": [],
    "电池类型": ["钠离子电池", "钾离子电池", "锂离子电池", "锂硫电池"],
    "表征技术": ["XRD", "SEM", "TEM", "XPS", "BET", "FTIR", "Raman"]
}
```

**异常处理**：
- 文件损坏 → 使用基础映射重新生成
- 写入失败 → 输出错误日志并终止程序

**日志输出**：
```
[INFO] [initialize_keywords_mapping] 初始化同义词映射...
[INFO] [initialize_keywords_mapping] 加载预定义同义词：4组
[INFO] [initialize_keywords_mapping] 从workflows_index提取：4种方法，8种元素
[INFO] [initialize_keywords_mapping] 保存到：metadata/keywords_mapping.json
```

---

### 4.4 LLM Prompt模板，需要扩写。

#### task prompt

**任务名称**：llm_intelligent_analysis

**任务目标**：对单篇化学论文进行智能分析，完成段落分类、散装知识元数据提取、实验方案结构化提取

**任务输入**：
- paper_id：论文唯一标识
- paper_title：论文标题
- full_text：论文全文
- paragraphs：论文段落列表（包含para_id和text）

**任务输出描述**：
按以下JSON格式输出（只输出JSON，不要其他内容）：

```json
{
    "段落分类结果": [
        {
            "para_id": "p001",
            "classification": "散装知识",
            "summary": "普鲁士蓝类似物具有面心立方结构(Fm-3m)，晶格参数约10.2Å，Fe-C-N-Fe线性配位，开放三维骨架有利于离子快速传输。",
            "content_type": "晶体结构",
            "entities": ["普鲁士蓝", "面心立方", "空间群Fm-3m", "Fe-C-N-Fe"],
            "keywords": ["晶体结构", "电化学性能", "离子传输", "面心立方"]
        },
        {
            "para_id": "p002",
            "classification": "实验",
            "summary": null,
            "content_type": null,
            "entities": null,
            "keywords": null
        }
    ],
    "实验方案提取结果": [
        {
            "method_type": "共沉淀法",
            "target_material": "高熵普鲁士蓝类似物",
            "elements": ["Fe", "Mn", "Co", "Ni", "Zn"],
            "summary": "共沉淀法制备高熵普鲁士蓝类似物(Fe/Mn/Co/Ni/Zn)：溶液A(K3[Fe(CN)6])和溶液B(金属氯化物)在60°C混合搅拌2h，陈化12h，离心洗涤后60°C真空干燥12h。",
            "实验方案_中文": "高熵普鲁士蓝类似物的合成：通常将1.658 g (5 mmol)的K3[Fe(CN)6]·3H2O溶于50 mL去离子水中",
            "分步骤描述": [
                "1. 配制溶液A：将1.658 g (5 mmol)的K3[Fe(CN)6]·3H2O溶于50 mL去离子水中",
                "2. 配制溶液B：将MnCl2·4H2O (1.979 g, 5 mmol)、CoCl2·6H2O (2.377 g, 5 mmol)、NiCl2·6H2O (2.376 g, 5 mmol)和ZnCl2 (1.363 g, 5 mmol)溶于250 mL去离子水中",
                "3. 共沉淀反应：在60°C剧烈搅拌下，以2 mL/min的速率将溶液B逐滴加入溶液A中",
                "4. 继续搅拌：完全加入后，继续搅拌2 h",
                "5. 陈化：所得沉淀在室温下陈化12 h",
                "6. 离心分离：以8000 rpm离心5 min收集沉淀",
                "7. 洗涤：用去离子水洗涤3次，用乙醇洗涤2次",
                "8. 干燥：在60°C真空干燥12 h"
            ]
        }
    ]
}
```

**详细任务说明**：

##### 任务1：段落分类和元数据提取
对论文中的每个段落进行分析，判断它是"散装知识段落"还是"实验段落"，并提取元数据。

**判断标准**：
- **散装知识段落**：包含理论知识、原理、机理、性能数据、表征结果、背景介绍等
- **实验段落**：包含具体的实验操作、合成步骤、材料配制、反应条件等

**分类规则**：
- 如果段落描述的是"做了什么实验、怎么做实验"，则标记为实验段落
- 如果段落描述的是"实验结果、理论原理、性能数据"，则标记为散装知识段落
- 注意："Results and Discussion"章节中的段落通常是散装知识（包含实验结果，不是实验步骤）

**元数据提取（仅对散装知识段落）**：
- content_type：内容类型，可选值：
  - "晶体结构"：涉及晶格参数、空间群、结构描述
  - "合成制备"：合成方法介绍（但不包含具体步骤）
  - "电化学性能"：比容量、循环寿命、倍率性能等
  - "反应机理"：反应路径、电子传输、离子扩散机理
  - "表征方法"：XRD、SEM、TEM、XPS等表征技术
  - "稳定性研究"：循环稳定性、结构稳定性、化学稳定性
  - "应用场景"：电池类型、催化应用、传感器等
  - "其他"：无法归类的其他内容
- entities：提取的实体，包括：
  - 化学物质：普鲁士蓝、高熵普鲁士蓝类似物、MnFe-PBA等
  - 元素符号：Fe、Mn、Co、Ni、Zn、Cu、K、Na等
  - 表征方法：XRD、SEM、TEM、XPS、BET、FTIR、Raman等
  - 空间群：Fm-3m、Pm-3m等
  - 性能指标：比容量、循环寿命、倍率性能等
- keywords：5-10个关键词，用于后续检索

##### 任务2：实验方案提取
识别论文中的实验方案段落，提取结构化的实验方案信息。

**提取要求**：
- 提取论文中所有完整的实验方案（通常在"Experimental"或"Synthesis"章节）
- 每个实验方案包含：
  - 合成方法类型：共沉淀法、水热法、络合结晶法、sol-gel法等
  - 目标材料名称
  - 包含的化学元素
  - 详细的分步骤描述（每个步骤必须包含用量、参数、时间、温度等）
  - 性能数据（如有）
  - 表征方法（如有）

**重要注意事项**：
1. 对于实验段落，summary、content_type、entities、keywords字段设为null
2. 对于散装知识段落，content_type必须从给定的8个类别中选择，不能创建新类别
3. summary必须简洁明了，fragment的summary控制在50-100字，workflow的summary控制在80-150字
4. 分步骤描述必须包含所有步骤，不能遗漏
5. 如果实验方案信息不完整，仍然提取，但在summary中标注"信息不完整"
6. entities和keywords的提取要全面，不要遗漏重要信息
7. 确保JSON格式正确，可以被Python解析

**示例**：

```json
{
    "段落分类结果": [
        {
            "para_id": "p001",
            "classification": "散装知识",
            "summary": "普鲁士蓝类似物具有面心立方结构(Fm-3m)，晶格参数约10.2Å，Fe-C-N-Fe线性配位，开放三维骨架有利于离子快速传输。",
            "content_type": "晶体结构",
            "entities": ["普鲁士蓝", "面心立方", "空间群Fm-3m", "Fe-C-N-Fe"],
            "keywords": ["晶体结构", "电化学性能", "离子传输", "面心立方"]
        }
    ],
    "实验方案提取结果": [
        {
            "method_type": "共沉淀法",
            "target_material": "高熵普鲁士蓝类似物",
            "elements": ["Fe", "Mn", "Co", "Ni", "Zn"],
            "summary": "共沉淀法制备高熵普鲁士蓝类似物(Fe/Mn/Co/Ni/Zn)：溶液A(K3[Fe(CN)6])和溶液B(金属氯化物)在60°C混合搅拌2h，陈化12h，离心洗涤后60°C真空干燥12h。",
            "实验方案_中文": "高熵普鲁士蓝类似物的合成：通常将1.658 g (5 mmol)的K3[Fe(CN)6]·3H2O溶于50 mL去离子水中",
            "分步骤描述": [
                "1. 配制溶液A：将1.658 g (5 mmol)的K3[Fe(CN)6]·3H2O溶于50 mL去离子水中",
                "2. 配制溶液B：将MnCl2·4H2O (1.979 g, 5 mmol)、CoCl2·6H2O (2.377 g, 5 mmol)、NiCl2·6H2O (2.376 g, 5 mmol)和ZnCl2 (1.363 g, 5 mmol)溶于250 mL去离子水中",
                "3. 共沉淀反应：在60°C剧烈搅拌下，以2 mL/min的速率将溶液B逐滴加入溶液A中",
                "4. 继续搅拌：完全加入后，继续搅拌2 h",
                "5. 陈化：所得沉淀在室温下陈化12 h",
                "6. 离心分离：以8000 rpm离心5 min收集沉淀",
                "7. 洗涤：用去离子水洗涤3次，用乙醇洗涤2次",
                "8. 干燥：在60°C真空干燥12 h"
            ]
        }
    ]
}
```


---



## 五、检索流程（混合检索：BM25 + LLM）

### 5.1 散装知识检索

**输入**：
- query (str)：用户查询问题
- top_k (int)：最终返回结果数量（如5）
- candidate_n (int)：BM25候选集大小（如50，默认=10*top_k）

**处理逻辑**：
1. **阶段1：BM25粗筛**
   - 查询预处理：分词 + 去停用词 + 同义词扩展
   - BM25评分：基于summary计算每个片段的相关性分数
   - 返回top-N候选（N=candidate_n）
2. **阶段2：LLM精排**
   - 将top-N候选的summary发送给LLM
   - LLM判断每个候选与查询的相关性
   - 返回重新排序的top-k结果
3. **阶段3：结果组装**
   - 根据fragment_id读取完整的fragment信息
   - 返回top-k片段

**输出**：片段列表（包含相关性分数和排序）

**性能**：
- 阶段1（BM25）：<50ms
- 阶段2（LLM）：150-200ms（N=50）
- 阶段3（读取）：<50ms
- **总计**：250-300ms

### 5.2 实验流程检索

**输入**：
- query (str)：用户查询问题
- top_k (int)：最终返回结果数量（如3）
- candidate_n (int)：BM25候选集大小（如30，默认=10*top_k）

**处理逻辑**：
1. **阶段1：BM25粗筛**
   - 查询预处理：分词 + 去停用词 + 同义词扩展
   - BM25评分：基于summary计算每个工作流的相关性分数
   - 返回top-N候选（N=candidate_n）
2. **阶段2：LLM精排**
   - 将top-N候选的summary发送给LLM
   - LLM判断每个候选与查询的相关性（考虑方法匹配、材料匹配、完整性等）
   - 返回重新排序的top-k结果
3. **阶段3：结果组装**
   - 根据workflow_id读取完整的workflow信息
   - 返回top-k工作流

**输出**：workflow列表（包含相关性分数和排序）

**性能**：
- 阶段1（BM25）：<30ms
- 阶段2（LLM）：100-150ms（N=30）
- 阶段3（读取）：<30ms
- **总计**：200-250ms

---

## 六、参数配置

```python
# BM25参数（基于summary）
BM25_CONFIG = {"k1": 1.5, "b": 0.75}

# 混合检索参数
HYBRID_SEARCH_CONFIG = {
    "fragments": {
        "top_k": 5,          # 最终返回数量
        "candidate_n": 50,   # BM25候选集大小
    },
    "workflows": {
        "top_k": 3,          # 最终返回数量
        "candidate_n": 30,   # BM25候选集大小
    }
}

# 停用词
STOPWORDS = {"的", "了", "在", "是", ...}

# LLM配置
LLM_CONFIG = {
    "max_retries": 3,
    "timeout": 30,
}
```

---

## 七、依赖项

```
pymupdf>=1.23.0
jieba>=0.42.1
rank-bm25>=0.2.2
numpy>=1.24.0
openai>=1.0.0  # 用于LLM调用
```
