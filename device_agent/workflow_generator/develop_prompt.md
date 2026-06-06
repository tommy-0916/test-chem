### 开发目标
现在我们来开发workflow generator的task 1，将其命名为forward task。
### 前置要求
preflow agent开发完毕并通过测试。这个代码也是你开发的，现在风格也需要保持一致。/workspace/chem_agent/pre_flow_agent你可以参考已有的代码实现和/workspace/chem_agent/pre_flow_agent/devlop_prompt.md的对应关系。
基础设施开发，现在已有的部分是在/workspace/chem_agent/utils。
详细浏览/workspace/chem_agent/develop_prompt.md和/workspace/chem_agent/DEVELOPMENT_PLAN.md，明确开发目标和要求
在开发之前要先提交一版git，保证我们能够恢复到开发之前的状态。注意活动分支是master。git仓库在chem_agent主目录中。不准参考master之外的代码是指git仓库中有很多分支，你只能参考master内部的，但是在git仓库之外的/workspace/agentic_system_reference是可以参考的
开发时要在本文件同级目录建立进度文档，实时更新。
### log通路要求
实时更新，产生目标输出并解析后与更新state同步更新到log中
### prompt来源
system prompt按照/workspace/chem_agent/develop_prompt.md中来编写即可。

### 执行流程
1. 调用LLM，本次调用的prompt命名为forward planning。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 本轮实验计划 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["goal_in_this_iteration"] |
| 相关知识 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["knowledge"] |
|结构化格式的论文中相关实验方案|Pre-flow Agent输出（通过State传递）|-|iterationN.json["related_workflows_txt"]|
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
| 以往相关实验方案 | Research Agent | 返回空 | - |
|txt格式参考|来自/workspace/chem_resources/format_reference/reference.txt，需要全部导入|-|-|

目的是：根据最终目标和当前实验计划，生成一版格式严格遵守txt参考、字段和参数严格参照每个用到的工作站的描述文件的结构化txt实验方案文本。这份实验方案可以从头生成，也可以参照已有的实验方案来修改，如果是修改已有方案的话，要注意格式、字段、参数的完整性，有不清楚的地方应该按照相关知识来。

输出是：结构化的txt文档，一定要保证格式正确：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 实验方案(TXT) | 规定格式txt，参考reference.txt，必须包含每个工作站的全部参数 | iterationN.json["workflows"][workflow_id]["workflow_txt"] |





### task prompt模板
#### forward planning
任务名称：当前任务的名称
任务目标：当前LLM的任务也就是生成符合本轮实验计划和总目标的结构化实验方案
任务输入：将所需的输入拼接
任务输出描述：规定好输出的内容和格式




### 涉及到的工具
当前并没有使用工具，所有逻辑都是硬编码在代码中


### 异常处理
LLM调用失败时重试机制继承BaseAgent，如果重试全都失败，直接报错并退出程序
输出解析失败也报错并退出程序



### 测试要求
按照设计文档中的要求，最大限度模拟真实数据流，保证输出格式和内容正确。另外你要打印测试中出现的所有prompt到类似于/workspace/chem_resources/exp_logs/exp_20260131_001/test_output.txt中。/workspace/chem_resources/exp_logs/exp_20260131_001这个是之前测试preflow生成的log，不如我们在测试之前复制一份，然后改成当前实验的目录，把这里面的信息作为输入，来测试，并且把输出也放到这里面。

### task 2补充要求
#### 执行流程
1. 调用LLM，本次调用的prompt命名为verify feedback regenerate planning。

输入是：
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 本轮实验计划 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["goal_in_this_iteration"] |
| 相关知识 | Pre-flow Agent输出（通过State传递） | - | iterationN.json["knowledge"] |
|结构化格式的论文中相关实验方案|Pre-flow Agent输出（通过State传递）|-|iterationN.json["related_workflows_txt"]|
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
| 以往相关实验方案 | Research Agent | 返回空 | - |
|txt格式参考|来自/workspace/chem_resources/format_reference/reference.txt，需要全部导入|-|-|
| Verify agent对于上一次生成的实验方案的修改意见 | Verify Agent输出（通过State传递） | - | iterationN.json["workflows"][source_workflow_id]["verification_suggestion"] |
| 上一次生成的实验方案 | Workflow Generator输出（通过State传递） | - | iterationN.json["workflows"][source_workflow_id]["workflow_txt"] |

目的是：根据最终目标和当前实验计划，生成一版格式严格遵守txt参考、字段和参数严格参照每个用到的工作站的描述文件的结构化txt实验方案文本。这一次不是从头生成，而是根据verify agent对于上一次实验方案的修改意见，重新生成一份完整的新实验方案。这份输出不能只是局部修改说明，而必须是完整的结构化txt实验方案全文；如果verify指出了字段缺失、参数超限、工作站约束违规、安全性问题或者流程不完整，那么新的方案里必须优先修复这些问题。

输出是：结构化的txt文档，一定要保证格式正确：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 实验方案(TXT) | 规定格式txt，参考reference.txt，必须包含每个工作站的全部参数，是根据verify意见重新生成后的完整新方案 | iterationN.json["workflows"][workflow_id]["workflow_txt"] |

注意这条数据通路中，原来被verify打回的workflow记录必须保留在原来的workflow_id位置，新的实验方案必须作为同一个iteration内的新workflow追加到workflows列表末尾，也就是说task 2输出对应的是新的workflow_id，而不是source_workflow_id，不能直接覆盖旧记录。

#### task prompt模板
##### verify feedback regenerate planning
任务名称：当前任务的名称
任务目标：当前LLM的任务也就是根据verify agent对上一次实验方案的修改意见重新生成本轮实验所需要的结构化实验方案
任务输入：将所需的输入拼接，尤其要明确包含上一次被打回的实验方案全文和verify agent的修改意见全文
任务输出描述：规定好输出的内容和格式，输出必须是完整实验方案，不能只输出修改点，不能只输出局部片段

#### 涉及到的工具
当前并没有使用工具，所有逻辑都是硬编码在代码中。是否进入task 2也不是由LLM自己决定，而是由主workflow根据verify agent的结果来硬编码控制。

#### 异常处理
LLM调用失败时重试机制继承BaseAgent，如果重试全都失败，直接报错并退出程序
输出解析失败也报错并退出程序
如果verify agent输出了refused但是verification_suggestion为空，也报错并退出程序
如果task 2错误地覆盖了旧workflow记录，而不是追加一个新的workflow，也报错并退出程序

#### 测试要求
task 2需要新增专门测试。你要在测试之前复制一份已经包含workflow generator输出并且被verify agent打回的log，把这里面的信息作为输入来测试，并且把输出也放到这个测试目录里面。如果当前没有现成的refused样例，你可以在复制后的测试目录中手动补一个明确的verification_result="refused"以及一段清晰的verification_suggestion，用它来模拟真实输入。task 2的测试除了检查workflow_txt非空和格式正确之外，还要额外检查原来的refused workflow仍然保留在原位置，没有被覆盖，新的workflow被追加到workflows列表末尾，新的workflow_id正确递增，并且新workflow记录中此时还没有新的verification_result和verification_suggestion，这些字段应该等下一轮verify agent来写。
