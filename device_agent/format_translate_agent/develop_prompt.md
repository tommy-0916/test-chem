### 开发目标
现在我们来开发format_translate_agent agent的task 1，将其命名为forward task。
### 前置要求
preflow，workflow generator和verify agent开发完毕，这些代码都是你写的，你需要保证风格统一，可以查看对应代码与/workspace/chem_agent/verify_agent/devlop_prompt.md的对应关系。
基础设施开发，现在已有的部分是在/workspace/chem_agent/utils。
详细浏览/workspace/chem_agent/develop_prompt.md和/workspace/chem_agent/DEVELOPMENT_PLAN.md，明确开发目标和要求
在开发之前要先提交一版git，保证我们能够恢复到开发之前的状态。注意活动分支是master。git仓库在chem_agent主目录中。不准参考master之外的代码是指git仓库中有很多分支，你只能参考master内部的，但是在git仓库之外的/workspace/agentic_system_reference是可以参考的
开发时要在本文件同级目录建立进度文档，实时更新。
architecture文档要在测试完成一切正常之后写。
### log通路要求
实时更新，产生目标输出并解析后与更新state同步更新到log中
### prompt来源
system prompt按照/workspace/chem_agent/develop_prompt.md中来编写即可。

### 执行流程
1. 调用LLM，本次调用的prompt命名为forward format translate。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 本轮实验计划 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["goal_in_this_iteration"] |
| 相关知识 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["knowledge"] |
|结构化格式的论文中相关实验方案|Pre-flow Agent输出（通过State传递）|-|iterationN.json["related_workflows_txt"]|
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
| 以往相关实验方案 | Research Agent | 返回空 | - |
| 待转译的实验方案 | Verify Agent 输出（通过State传递） | - | iterationN.json["workflows"][workflow_id]["workflow_txt"] |

目的是：根据工作站描述文档，对当前的workflow_txt文档进行转译，将workflow_txt文档转译成工作站能够接受的json文件。注意，你转译出来的json字段必须要和/workspace/chem_resources/workstations/*.json的字段对应，不能自己取名。

输出参照/workspace/chem_resources/workstations/*.json里的结构。
输出如下，需要同时更新state和log。
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 转译结果 | workflow_json | iterationN.json["workflows"][workflow_id]["workflow_json"] |




<!-- 2. 注意现在根据上一次LLM调用的结果产生分支：如果LLM审核不通过，则直接终止本次verify工作。
如果审核通过则进行下一次LLM调用。 -->



<!-- 2. 调用LLM，本次调用的prompt命名为forward format translate。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 本轮实验计划 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["goal_in_this_iteration"] |
| 相关知识 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["knowledge"] |
|结构化格式的论文中相关实验方案|Pre-flow Agent输出（通过State传递）|-|iterationN.json["related_workflows_txt"]|
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
| 以往相关实验方案 | Research Agent | 返回空 | - |
| 待转译的实验方案 | Verify Agent 输出（通过State传递） | - | iterationN.json["workflows"][workflow_id]["workflow_txt"] |

目的是：根据以往实验记录，当前这个方案是不是陷入了死循环？有没有执行必要？如果有的话结合相关化学知识和实验理论还能有什么效率、成功率上的优化空间？

输出如下，需要同时更新state和log。注意：在log和state中，这一步更新的字段和上一次LLM调用相同，对于审核结果，你要做的就是覆盖上一次调用所更新的字段。对于建议，你要做的不是覆盖字段，而是在其后新增内容，保留上一次的结果。
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 审核结果 | accepted/refused | iterationN.json["workflows"][workflow_id]["verification_result"] |
| 修改建议 | 非结构化文本（不通过时必须提供） | iterationN.json["workflows"][workflow_id]["verification_suggestion"] | -->


### task prompt模板
#### forward format translate
任务名称：当前任务的名称
任务目标：当前LLM的任务
任务输入：将所需的输入拼接
任务输出描述：规定好输出的内容和格式


### 涉及到的工具
当前并没有使用工具，所有逻辑都是硬编码在代码中


### 异常处理
LLM调用失败时重试机制继承BaseAgent，如果重试全都失败，直接报错并退出程序
输出解析失败也报错并退出程序



### 测试要求
按照设计文档中的要求，最大限度模拟真实数据流，保证输出格式和内容正确。另外你要打印测试中出现的所有prompt到类似于/workspace/chem_resources/exp_logs/exp_20260131_001/test_output.txt中。/workspace/chem_resources/exp_logs/exp_20260201_test这个是之前测试workflow generator生成的log，我们在测试之前复制一份，然后改成当前实验的目录，把这里面的信息作为输入，来测试，并且把输出log也放到这里面。