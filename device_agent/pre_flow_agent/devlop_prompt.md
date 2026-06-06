### 开发目标
现在我们来开发preflow agent的task 1，将其命名为forward task。
### 前置要求
代表knowledge agent的前置知识已经填充完毕。
基础设施开发，现在已有的部分是在/workspace/chem_agent/utils。
详细浏览/workspace/chem_agent/develop_prompt.md和/workspace/chem_agent/DEVELOPMENT_PLAN.md，明确开发目标和要求
在开发之前要先提交一版git，保证我们能够恢复到开发之前的状态。注意活动分支是master。
开发时要在本文件同级目录建立进度文档，实时更新。
### log通路要求
实时更新，产生目标输出并解析后与更新state同步更新到log中
### prompt来源
system prompt按照/workspace/chem_agent/develop_prompt.md中来编写即可。

### 执行流程
1. 获取输入：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
| 背景知识 | Knowledge Agent | knowledge.txt | - |
| 论文知识 | Knowledge Agent | summary.txt | - |
| 论文中的实验方案 | Knowledge Agent | expriment_workflow_paper.txt | - |
| 以往相关实验方案 | Research Agent | 返回空 | - |
其中实验目标是来自主workflow中的state，目前应该已经设定好了，直接导入prompt即可；工作站描述和约束需要导入所有字段到prompt中；背景知识、论文知识、论文方案理论上来自knowledge agent，虽然现在还没实现，但是这个查询操作在我们这里应该被抽象成了一个工具调用，工具函数内部应该已经做好了临时数据流，所以这个查询操作的工具调用直接硬编码在代码中即可，只需要最后测试的时候配置好参数来使用临时数据流；实验记录的查询直接返回空。



2. 调用LLM，本次调用的prompt命名为forward planning，本次的输入是上表所示全部输入。

目的是：根据最终实验目标以及手头的信息，选择一个最合适的本轮实验计划，如目标为“优化普鲁士蓝的某项理化性质”，当前的计划就应该是“由于目前是从头开始的状态，所以现在的计划是从0开始设计一份可执行的普鲁士蓝合成实验方案”。设定好计划之后需要对各种相关知识进行浓缩提取。最后还要对相关实验方案进行挑选，根据当前实验室所能提供的工作站的描述和约束，选择一些在当前环境下可行性较高而且参数全面、流程清晰的相关方案。

LLM输出是以下表中三个字段为标识的三段非结构化文本：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 相关知识 | 非结构化文本 | iterationN.json["knowledge"] |
| 参考实验方案 | 非结构化文本| iterationN.json["related_workflows_unformatted"]|
| 本轮实验计划 | 非结构化文本 | iterationN.json["goal_in_this_iteration"] |




3. 调用LLM，本次调用的prompt命名为forward translating，输入为：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 本轮实验计划 | 上一轮调用所更新的state |-| iterationN.json["goal_in_this_iteration"] |
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
| 相关知识 | 上一轮调用所更新的state |-|iterationN.json["knowledge"]|
|txt格式参考|来自/workspace/chem_resources/format_reference/reference.txt，需要全部导入|-|-|
| 论文中的实验方案 | 上一轮更新在state中的非结构化文本 |-|iterationN.json["related_workflows_unformatted"]|注：这个字段是新增的，在之前的文档中没有提到。
| 以往相关实验方案 | Research Agent | 返回空 | - |

调用目的是：根据实验目标和本轮实验计划，借助相关知识以及工作站参数文档和模板，参考我们提供的txt格式，将上一轮调用所选中的相关实验方案转译为以我们所能接受的工作站为单位的带有完整参数的txt格式实验方案

LLM输出是以下表字段为标识的结构化txt格式文本：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 参考实验方案 | 规定格式txt，参考reference.txt | iterationN.json["related_workflows_txt"] |


### task prompt模板
#### forward planning
任务名称：当前任务的名称
任务目标：当前LLM的任务也就是流程中所说的规划并提取知识
任务输入：将所需的输入拼接
任务输出描述：规定好输出的内容和格式
#### forward translating
任务名称：当前任务的名称
任务目标：当前LLM的任务也就是流程中所说的转译txt格式
任务输入：将所需的输入拼接
任务输出描述：规定好输出的内容和格式



### 涉及到的工具
当前并没有赋予LLM自行决定工具调用的能力，整个流程中也只使用了访问knowledge agent和research agent两个工具。


### 异常处理
LLM调用失败时重试机制继承BaseAgent，如果重试全都失败，直接报错并退出程序
输出解析失败也报错并退出程序



### 测试要求
按照设计文档中的要求，最大限度模拟真实数据流，保证输出格式和内容正确。另外你要打印测试中出现的所有prompt到类似于/workspace/chem_resources/exp_logs/exp_20260131_001/test_output.txt中。