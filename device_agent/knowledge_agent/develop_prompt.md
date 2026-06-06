# 开发目标
论文库已到位，现在我们来开发knowledge agent。knowledge agent的功能是向上暴露接口，其它agent将该接口视为工具来调用，knowledge agent根据query来返回相应的结果。knowledge agent向下管理论文知识库，负责构建并维护论文知识库。因此我们要开发knowledge agent的三个task也就是三个子agent：knowledge base constructing，负责从pdf等格式的原始论文出发搭建可用的知识库；knowledge searching，负责返回零散的知识；workflow searching负责返回完整且详细的workflow
# 前置要求
preflow、workflow generator、verify agent开发完毕，这些代码都是你写的，你需要保证风格统一，可以查看对应代码与/workspace/chem_agent/pre_flow_agent/devlop_prompt.md的对应关系。
基础设施开发，现在已有的部分是在/workspace/chem_agent/utils。你对于LLM的调用也要使用同样的代码包装。
详细浏览/workspace/chem_agent/develop_prompt.md和/workspace/chem_agent/DEVELOPMENT_PLAN.md，明确项目背景、开发目标和开发要求，并严格遵守我设定的开发规则和约束，绝对不能违反。
你扮演的的git用户是nzy，在开发之前要先提交一版git，保证我们能够恢复到开发之前的状态。注意活动分支是master。git仓库在chem_agent主目录中。不准参考master之外的代码是指git仓库中有很多分支，你只能参考master内部的，但是在git仓库之外的/workspace/agentic_system_reference是可以参考的
开发时要在本文件同级目录建立进度文档，实时更新。
architecture文档要在测试完成一切正常之后写。
库文件以及开发和评测都在chem agent下的虚拟环境中，必须使用这个虚拟环境。
# log通路要求
实时更新，产生目标输出并解析后与更新state同步更新到log中。对于不同的task，我对log的要求也不同。
# prompt来源
system prompt按照/workspace/chem_agent/develop_prompt.md中来编写即可。由于之前的开发文档对于knowledge agent的描述不够详细，你可能需要自己根据项目背景和knowledge agent的功能要求扩写一些
# 执行流程
这里指的是knowledge agent本身而不是子agent的执行流。我们先暂且把这个workflow视为单纯的路由器逻辑，如果上层需要生成知识库则路由到knowledge base build agent ，另外的逻辑先不管，等我们构建好知识库之后再写。



# task : knowledge base build agent

这是knowledge agent的第一个功能，也就是构建一个以原始论文为来源的知识库。首先我们需要明确知识库的结构，然后我需要给你规定好知识库构建的流程。你需要严格遵守。你可以把完成这个任务的workflow视为一个子agent叫knowledge base build agent，这个agent的目录是/workspace/chem_agent/knowledge_agent/knowledge_base_build，workflow应该放在/workspace/chem_agent/knowledge_agent/knowledge_base_build/workflow.py。
## 设计说明
知识库字段设计、搭建流程的逻辑你需要参考/workspace/chem_agent/knowledge_agent/knowledge_base_build/knowledge_base_design.md，其中在需要调用LLM的步骤你可以参考其它agent的方法来构造请求。

## prompt模板
见设计文档。这是我们调用LLM进行论文处理时所需要使用的 task prompt模板

## 涉及的工具
这个任务似乎并没有用到别的工具，只需要在我们的虚拟环境中装好必要的库，并且复用已有的LLM调用代码与后端交互即可


## 测试要求
这个模块的测试应该也相对独立，输入是/workspace/chem_resources/knowledge_base/普鲁士蓝中的原始论文，你在开发之前应该重构一下knowledgebase这个目录以达成设计文档要求。另外，我要求你先测试一篇论文的整个构建流程，注意为了debug方便，你的测试脚本最好每个原子操作之后都检测一次看一下有没有达成预定效果，也就是分步来测试。测试脚本是/workspace/chem_agent/test_knowledge_agent_build.py，这个脚本不能直接call knowledge base build而是从knowledge agent来路由过去。也就是说实际上knowledge agent的主workflow在我们的测试中没有被调用，我们是直接测试的原子动作。注意，每个原子动作的输出你都需要详细观察是不是能和我们的设计要求的语义对应上，而不是仅仅看到有输出就通过了。

## 测试后需要修改/workspace/chem_agent/knowledge_agent/knowledge_base_build/knowledge_base_design.md中与实际实现不符的字段，逐字修改

# 开发任务先到这里结束


<!-- # task： knowledge search agent
这是knowledge agent的第二个子agent，任务是根据查询query来查询知识库中的散装知识并返回。knowledge base的设计方案需要去查看/workspace/chem_agent/knowledge_agent/knowledge_base_build/knowledge_base_design.md以及对应的knowledge base build agent，参考知识库的结构来开发我们的检索逻辑。


## 执行流程

#### 步骤1：查询预处理

调用查询预处理模块，对原始查询进行分词、去停用词、同义词扩展处理。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |

|------|---------------|-------------|-------------|
| 原始查询query | 人类研究员输入或上层agent调用（通过State传递） | 通过测试脚本赋值 | |
| 同义词映射表 | /workspace/chem_resources/knowledge_base/metadata/keywords_mapping.json | 读取文件 | - |

目的是：对查询进行标准化处理，提高检索召回率。

处理逻辑：
1. 使用jieba.cut对query进行分词
2. 去除停用词（预定义的停用词列表）
3. 根据同义词映射表进行同义词扩展
4. 返回处理后的查询词列表

输出如下，需要同时更新state和log：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| processed_query_tokens | 处理后的查询词列表 | iterationN.json["knowledge_search_processed_tokens"] |
| expanded_query | 扩展后的查询字符串（包含同义词） | iterationN.json["knowledge_search_expanded_query"] |


#### 步骤2：BM25粗筛检索

调用BM25检索模块，基于summary进行粗筛，返回top-N候选片段。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| processed_query_tokens | 步骤1输出（通过State传递） | - | iterationN.json["knowledge_search_processed_tokens"] |
| BM25模型 | /workspace/chem_resources/knowledge_base/indexes/fragments_summary_bm25_model.pkl | 读取文件 | - |
| fragment元数据映射 | /workspace/chem_resources/knowledge_base/indexes/fragments_summary_metadata.pkl | 读取文件 | - |
| candidate_n | 配置参数（默认50） | 硬编码常量 | - |

目的是：使用BM25算法快速筛选出与查询最相关的前N个候选片段，减少后续LLM精排的计算量。

处理逻辑：
1. 加载BM25模型对象（BM25Okapi，内部已包含corpus）
2. 加载fragment_id到summary的映射字典
3. 使用processed_query_tokens作为查询，调用bm25.get_scores(query_tokens)
4. BM25返回分数数组（每个分数对应corpus中的一个fragment，索引从0开始）
5. 按分数降序排序，取前N个（candidate_n=50）的索引
6. 根据索引计算fragment_id（索引+1，如索引0对应frag_001）
7. 根据索引从元数据映射获取对应的summary

输出如下，需要同时更新state和log：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| candidate_indices | 候选片段在corpus中的索引列表（按BM25分数降序） | iterationN.json["knowledge_search_candidate_indices"] |
| candidate_fragment_ids | 候选片段ID列表（根据indices计算：索引+1） | iterationN.json["knowledge_search_candidate_ids"] |
| candidate_summaries | 候选摘要列表（根据indices从metadata获取） | iterationN.json["knowledge_search_candidate_summaries"] |
| bm25_scores | 候选片段的BM25分数列表 | iterationN.json["knowledge_search_bm25_scores"] |

日志输出：
```
[INFO] [bm25_retrieval] 开始BM25粗筛检索
[INFO] [bm25_retrieval] 加载BM25模型: indexes/fragments_summary_bm25_model.pkl
[INFO] [bm25_retrieval] 查询词: ["普鲁士蓝", "晶体", "结构", "PBA", "Prussian Blue"]
[INFO] [bm25_retrieval] BM25计算完成，分数数组长度: 1523
[INFO] [bm25_retrieval] 按分数排序，取前50个
[INFO] [bm25_retrieval] 候选索引: [0, 45, 123, ...]
[INFO] [bm25_retrieval] 对应fragment_id: [frag_001, frag_046, frag_124, ...]
[INFO] [bm25_retrieval] 最高分: 12.45, 最低分: 3.21
```

#### 步骤3：LLM精排

调用LLM对BM25筛选出的候选片段进行语义相关性判断和重新排序。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 原始查询query | 人类研究员输入或上层agent调用（通过State传递） | - | iterationN.json["knowledge_search_query"] |
| candidate_summaries | 步骤2输出（通过State传递） | - | iterationN.json["knowledge_search_candidate_summaries"] |
| top_k | 配置参数（默认5） | 硬编码常量 | - |

目的是：利用LLM的语义理解能力，对候选片段进行深度相关性判断，返回与查询语义最相关的top-k结果。

处理逻辑：
1. 构建LLM prompt（包含原始查询和候选摘要列表）
2. 调用LLM API
3. 解析LLM返回的JSON，包含：
   - ranking_results：排序结果列表，每项包含index（候选索引）、relevance_score（相关性分数0-10）、reason（排序理由）
4. 根据ranking_results中的index和relevance_score，对candidate_fragment_ids进行重排
5. 取前top_k个结果

输出如下，需要同时更新state和log：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| ranked_fragment_ids | 排序后的片段ID列表（top-k） | iterationN.json["knowledge_search_ranked_ids"] |
| relevance_scores | 相关性分数列表（与fragment_id对应） | iterationN.json["knowledge_search_relevance_scores"] |
| ranking_reasons | 排序理由列表 | iterationN.json["knowledge_search_ranking_reasons"] |

日志输出：
```
[INFO] [llm_reranking] 开始LLM精排
[INFO] [llm_reranking] 候选数量: 50, 返回数量: 5
[INFO] [llm_reranking] 调用LLM API...
[INFO] [llm_reranking] LLM返回解析成功
[INFO] [llm_reranking] 精排完成
```

#### 步骤4：加载完整片段信息

根据ranked_fragment_ids，从fragments目录加载完整的片段信息。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| ranked_fragment_ids | 步骤3输出（通过State传递） | - | iterationN.json["knowledge_search_ranked_ids"] |
| fragment文件目录 | /workspace/chem_resources/knowledge_base/fragments/ | 读取目录 | - |

目的是：组装最终的检索结果，包含片段的完整信息。

处理逻辑：
1. 遍历ranked_fragment_ids列表
2. 对每个fragment_id，构建文件路径：fragments/fragment_{序号}.json
3. 读取JSON文件，获取完整信息
4. 组装最终结果对象

输出如下，需要同时更新state和log：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| search_results | 检索结果列表（top-k） | iterationN.json["knowledge_search_results"] |
| search_results[i].fragment_id | 片段唯一标识 | - |
| search_results[i].paper_id | 来源论文ID | - |
| search_results[i].paper_title | 来源论文标题 | - |
| search_results[i].content | 片段原文内容 | - |
| search_results[i].summary | 片段摘要 | - |
| search_results[i].content_type | 内容类型 | - |
| search_results[i].entities | 实体列表 | - |
| search_results[i].keywords | 关键词列表 | - |
| search_results[i].relevance_score | 相关性分数（来自步骤3） | - |
| search_results[i].ranking_reason | 排序理由（来自步骤3） | - |

日志输出：
```
[INFO] [load_fragments] 开始加载完整片段信息
[INFO] [load_fragments] 加载5个片段
[INFO] [load_fragments] 片段加载完成
[INFO] [knowledge_search] 检索完成，返回5个结果
``` -->