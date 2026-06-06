### 开发目标
搭建主agent的第一条workflow也就是forward workflow：当前阶段的目标是串联好我已经搭建好的agent，实现人类研究员指定任务目标，agent根据自身所掌握的相关资料以及实验室可用资源和约束文档，自动设计符合底层驱动代码格式要求的实验方案。目前我们需要使用的agent有四个，分别是pre_flow_agent，work_flow_agent，verify_agent和translate_agent。他们的调用流程是pre_flow_agent --> work_flow_agent --> verify_agent --> translate_agent。这四个agent都已经搭建完毕，你需要确保主workflow以及state能够正确地与各个agent进行对接。另外agent的执行顺序也就是目前的workflow只考虑pre_flow_agent --> work_flow_agent --> verify_agent --> translate_agent的正向流程也就是在每个agent中定义的那个task。
### 前置要求
当前数据流所涉及的所有的agent也就是preflow、workflow generator、verify agent、format translate agent都开发完毕，这些代码都是你写的，你需要保证风格统一，可以查看对应代码与开发prompt例如preflow的代码和/workspace/chem_agent/pre_flow_agent/devlop_prompt.md的对应关系。
基础设施开发，现在已有的部分是在/workspace/chem_agent/utils。
详细浏览/workspace/chem_agent/develop_prompt.md和/workspace/chem_agent/DEVELOPMENT_PLAN.md，明确项目背景和我所规定的开发要求
在开发之前要先提交一版git，保证我们能够恢复到开发之前的状态。注意活动分支是master。git仓库在chem_agent主目录中。不准参考master之外的代码是指git仓库中有很多分支，你只能参考master内部的，但是在git仓库之外的/workspace/agentic_system_reference是可以参考的
开发时严禁随意修改/workspace/chem_agent/format_translate_agent，/workspace/chem_agent/pre_flow_agent，/workspace/chem_agent/verify_agent，/workspace/chem_agent/workflow_generator也就是四个子agent的任何代码逻辑，如确有必要，一定要征得我同意再去做。
开发时要在本文件同级目录建立进度文档，实时更新。
注意：不要参考主目录中那些test脚本，因为这些脚本只是单独测试某一个agent，输入输出都来自log通路而不是主agent的state传递，你现在要做的事情就是把agent之间的执行流用主agent的workflow和state串联。log通路你可以维持现状，让每个agent分别更新自己负责的部分。
architecture文档要在测试完成一切正常之后写。
库文件以及开发和评测都在chem agent下的虚拟环境中。
#### 如果遇到不确定的问题，一定要向我询问，绝对不允许乱改 ！！！！！！！！！！

### log要求
log文件的字段、命名和更新方式基本不变，只是有一点你需要注意，你需要在主agent中统一管理本次实验的log文件，每次实验开始时创建新的实验目录并做好管理，只需要向子agent传递目录即可。另外iteration也有主agent而不是preflow agent来控制，目前只有正向通路，所以只需要在数据流经preflow agent之前新建一个iteration即可。也就是说我们现在要实现统一管理的log通路。iteration是指从实验目标出发到输出一版可执行json方案为止的一次迭代，中间如果verify多次不通过，只是在iteration文件中新增多个workflow项，iteration的结束是json文件的输出。一次实验指的是从设定实验目标开始，到research agent判定完成实验目标为止的多个iteration也就是可能的多次可执行实验方案的生成。

### prompt 来源
system prompt按照设计文档中的内容自行编写即可

### State 定义（LangGraph 通信通道）
State 是 Agent 之间的传输，仅包含下一步执行所必须的字段。我们要开发 chem_agent 的主 Agent，它负责串联四个子 Agent：Pre-flow generate Agent, Workflow Generator, Verify Agent, Format translate Agent。数据流通是 pre_flow_agent --> work_flow_agent --> verify_agent --> translate_agent。 你需要基于我提供的子 Agent 状态定义，完善主 Agent 的 chem_agent/state.py，现在这个文件中应该已经有一些state的定义了，你需要根据子agent间需要传递的值来完善主agent的state.

每个子agent的state.py参考路径：
Pre-flow generate Agent: chem_agent/pre_flow_agent/state.py
Workflow Generator: chem_agent/workflow_generator/state.py
Verify Agent: chem_agent/verify_agent/state.py
Format translate Agent: chem_agent/format_translate_agent/state.py
### task：forward workflow
#### 定位
这条workflow是任务的起点，理论上只要是重新开始的任务，我们都要选择这个workflow，也就是说外界调用我们的agent时默认流向这个workflow。
#### 处理流程
1. 创建log并获取输入。
| 字段 | 实际运行时来源 | 临时数据通路 | 日志记录位置 |
|------|---------------|-------------|-------------|
| 实验目标 | 人类研究员输入（通过State传递） | - | exp_log.json["final_goal"] |
| 工作站描述与约束 | /workspace/chem_resources/workstations/*.json（需导入全部字段） | - | - |
|txt格式参考|来自/workspace/chem_resources/format_reference/reference.txt，需要全部导入|-|-|
|json格式参考|来自/workspace/chem_resources/format_reference/reference.json，需要全部导入|-|-|
注意：此处你需要注意的是，之前的agent测试时的实验目标都是来自log，工作站描述都是在各自测试时加载，preflow agent、workflow generator、verify agent的txt格式参考是在各自的workflow.py中加载，而translate agent的json参考是直接写在 task prompt里了，这里我们统一在主workflow中加载这些输入到state中，之后的数据全都改为从state中传递，所以你需要查看并修改对应agent的数据接口和prompt。这四个输入需要更新到Log中的只有实验目标，也在此处更新。log中的其它字段应该都是在相应agent输出该字段时顺便更新的，如果是这样的话我们就保持这些字段的更新方式不变


2. 调用preflow agent。preflow agent的输入现在是上一步获取的4个，剩下的信息的获取方式应该是包装在preflow agent内部的，不需要修改
preflow agent会在自己的内部来获取其它信息比如call knowledge agent等，然后执行自己定义好的workflow。
preflow agent会相应更新以下字段到log和state，但是其中"related_workflows_unformatted"貌似是不需要暴露给主agent的：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 相关知识 | 非结构化文本 | iterationN.json["knowledge"] |
| 参考实验方案 | 非结构化文本| iterationN.json["related_workflows_unformatted"]|
| 本轮实验计划 | 非结构化文本 | iterationN.json["goal_in_this_iteration"] |
| 参考实验方案 | 规定格式txt，参考reference.txt | iterationN.json["related_workflows_txt"] |

3. 调用workflow generator，输入都在主agent的state中。
workflow generator会更新以下字段到log和主state中：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 实验方案(TXT) | 规定格式txt，参考reference.txt，必须包含每个工作站的全部参数 | iterationN.json["workflows"][workflow_id]["workflow_txt"] |

4. 调用verify agent，输入都在主agent的state中。
输出同样会更新主agent的state和log：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 审核结果 | accepted/refused | iterationN.json["workflows"][workflow_id]["verification_result"] |
| 修改建议 | 非结构化文本（不通过时必须提供） | iterationN.json["workflows"][workflow_id]["verification_suggestion"] |

5. 条件判断：
这里原本的逻辑是如果verify 通过则进入下一步，如果不通过则返回workflow generator执行反馈路径上的任务并设置一定的反馈路径重试次数。但是由于我们的反馈路径还没有实现，所以这里你就先做个占位符来开发这个分支。
我们实际上使用的临时数据流是直接进入下一步，你可以通过可配置参数来决定我们使用原本的数据流还是临时数据流。但是注意两种数据流都要有代码实现。

6. 调用format translate agent，进行格式转换
输出同样会更新主agent的state和log：
| 字段 | 说明 | 日志记录位置 |
|------|------|-------------|
| 转译结果 | workflow_json | iterationN.json["workflows"][workflow_id]["workflow_json"] |

7. 输出state中的转移结果，这条workflow结束。



### 涉及到的工具
主workflow中并没有使用工具，所有逻辑都是硬编码在代码中


### 异常处理
子agent内部异常处理逻辑不变，当一个子agent最终返回失败之后，主workflow直接报错并退出即可。


### 测试要求
按照设计文档中的要求，最大限度模拟真实数据流，保证输出格式和内容正确。与子agent的测试流程相同，你也需要保存原始的prompt到一个txt文件中，这里原来都是在各个test脚本中实现的，我想你需要将这部分逻辑放到每个agent调用LLM时。/workspace/chem_agent/test_main_workflow.py这是最终测试脚本的地址。


### 总结
你要做的最主要工作就是实现主workflow 中的forward workflow来把我们整个前向流程跑通。为了实现这个目标，你除了新增代码之外，可能还需要修改一下原来子agent的部分子state更新逻辑、log保存逻辑和调用LLM时收集原始数据的逻辑等。但是注意，不要随意扩大修改范围，不确定的问题一定要交给我选择。另外，如果子agent的代码需要修改，不要忘记更新对应的注释，这可能需要你增大上下文查看的窗口