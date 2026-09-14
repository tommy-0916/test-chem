---
name: chemistry-experiment-workstation
description: 如果涉及到化学实验工作站的相关操作，请先阅读此规则集。
---


# chemistry-experiment-workstation Skills

- 本技能为 303 实验室自动化工作站操作规范与约束规则库。每个最小层级目录对应独立工作站，承载设备元数据及标准操作要求。

## 一、触发关键字

站，工作站，平台，仪，机，炉，AGV，摄像

## 二、前置条件

- Python 3.8+
- 已安装依赖：`pip install -r scripts/requirements.txt`


## 三、说明
- **可复用宏实验**：`references-Macro-operation/[macro-name]/SKILL.md`是预编排的多工作站组合流程，实验规划时优先检索，若匹配当前实验需求则直接使用。
- **实际可用工作站**：`references-Synthesis-Module/[workstation-name]、references-Reaction-and-Testing-Module/[workstation-name]、references-Characterization-Module/[workstation-name]`是以工作站名字命名的目录,`[workstation-name]/SKILL.md`文件是303实验室实际可用的工作站的使用指导。

## 四、workflow步骤

1. **工作站选择阶段**：根据任务需求，模型判断预计需要的工作站，读取多工作站组合的宏实验流程和所需的对应工作站的信息。
2. **实验方案生成阶段**：依据实验需求、工作站中定义的操作参数，生成实验方案。
3. **方案复核阶段**：对相关工作站内容重新检测一次，即复核一次，若复核出问题，必须进行更改。
4. **方案逻辑审核**：各工作站间输入与输出的逻辑一致性校验，参考 references_audit 目录下对应`workstation-name.md文件`的规则，深度校验各工作站间输入与输出的逻辑一致性。若校验不通过，必须溯源修改方案并重新审计，直至逻辑完全闭合。
5. **输出json格式方案**：将实验生成阶段的方案转换为json格式。
6. **格式转换**：使用新技能`workflow-generator`。
7. **下发实验**：使用新技能`lab-operation`。


## 五、workflow详细步骤

1. **工作站选择阶段**：
   (1) 工作站预选择：根据任务需要判断**预计需要的工作站**。
   (2) 读取宏实验流程：优先读取`references-Macro-operation`目录下的宏实验SKILL，若当前实验需求可被宏流程覆盖，则优先使用宏实验流程，避免重复编排单工作站操作。
   (3) 读取对应工作站：根据预计需要的工作站，读取所涉及的`[workstation-name]/SKILL.md`文件。

   **工作站选择路由决策树：**
```
用户使用工作站
  │
  ├─ SKILL.md
  ├─ references-Macro-Operation/[macro-name]/SKILL.md 【第一优先级】预编排的多工作站组合流程，实验规划时优先检索，若匹配当前实验需求则直接使用
  ├─ references-Synthesis-Module/[workstation-name]/SKILL.md 合成模块工作站的集合
  ├─ references-Reaction-and-Testing-Module/[workstation-name]/SKILL.md 反应与测试模块工作站的集合
  ├─ references-Characterization-Module/[workstation-name]/SKILL.md 表征模块工作站的集合
  ├─ references_files 已预置工作站的配置文件，供以文件的形式传给工作站
  ```


2. **实验方案生成阶段**：

依据 `[workstation-name]/SKILL.md`中定义的操作参数，根据实验需求选择合适的工作站和操作顺序，生成实验方案。

   (1) **整体实验流程规则**
- **工作站约束**：模型开展实验设计，必须严格限定使用指定工作站资源。工作站仅可从`[workstation-name]/SKILL.md`路径内选取。若目标工作站不存在，立即终止后续全部流程，同时明确打印输出缺失的工作站名称。
- **工作站操作名称约束**：所有操作名称须严格规范沿用，禁止自行修改，示例：超声清洗、物料放置。
- **实验开始**：每个实验必须以物料配置开始。测试场景除外。
- **实验结束**：每个实验必须以"容器置放平台_V1-物料放置"结束。测试场景除外。若当前
  macro action 的终点是表征测试、离线 observation 或数据回传接口，则以该测试工作站的
  合法前置中转/进样步骤或明确的 offline_handoff 结束，也属于“测试场景除外”；不得为了
  形式上补置放步骤而把样品从测试链路倒回或增加无意义容器移动。
- **原液**：所有原液均已提前配制完成，无需进行前驱体配制操作，统一通过移液平台工作站完成取用。
- **容器类型**：默认沿用同一容器类型和同一样品身份。若后续 operation 的输入容器、容量、
  相态或实验目的明确不兼容，允许通过平台默认联通的容器/物料/样品处理链进行一次最短必要
  的 A→B 转移或换瓶；也允许 offline_handoff 明确返回已装入 B 容器的样品。此时必须在
  workflow/container_plan/offline_handoff 中记录 A、B 的容器类型与编号、转移后的样品状态和
  sample lineage。不同容器类型各自独立编号，不能因编号相同就把 A、B 当成同一容器。
  禁止无约束地改变容器类型、跳过样品状态建立、无意义溶剂位置转换或 A→B→A 往返。
- **容器数量**：整个实验流程中，随着实验步骤的增加，“容器数量”不能出现个数变多的情况，个数可以保持一致或者减少。
- **容器开盖与关盖规则**: 
- 仅移液平台具备容器开盖、关盖功能，其余所有工作站无开关盖操作功能。
- 96位塑料孔板或96位石英孔板默认都不带盖子。
- 模型需实时追踪并维护每个容器的 lid_status（open/closed）。任何进入非移液平台的操作前，必须检索该工作站对盖子状态的要求：若上一工作站输出的容器盖状态，与下一工作站要求的输入盖状态不一致，必须在两个工作站之间插入移液平台，完成对应的开盖或关盖过渡操作。

  (2) **打印要求**：
- 实验生成阶段查看了哪些工作站的skill必须打印出来。
- 实验生成阶段实际使用了哪些工作站的skill必须打印出来。


3. **方案复核阶段**：

再次依据`[workstation-name]/SKILL.md`中定义的操作参数，对实验方案进行严格的校验，若复核出问题，必须进行更改。

   (1)**复核要求**：
必须显式展示以下思维链（CoT）过程，严禁直接跳过：
CHECKPOINT 1 - 约束校验表：列出每个步骤的参数是否落在 SKILL.md 定义的 range 或 options 内。
CHECKPOINT 2 - 约束校验表：对问题进行更改。
只有在上述校验全部输出“PASS”后，方可进入下一步。

   (2)**打印要求**：在方案逻辑审核阶段，按时间顺序完整依次输出方案过程中产生的全部修改内容，逐条清晰展示所有调整与变更细节。禁止静默修改。


4. **方案逻辑审核**：

对方案使用到的各工作站间输入与输出的逻辑一致性校验，参考`references_audit`目录下对应工作站的 `markdown文件` 。

   (1)**逻辑审核要求**：
必须显式展示以下思维链（CoT）过程，严禁直接跳过：
CHECKPOINT 1 - 约束校验表：引用`references_audit`判定步骤顺序是否合法。
CHECKPOINT 2 - 约束校验表：对问题进行更改。
只有在上述校验全部输出“PASS”后，方可进入下一步。

   (2)**打印要求**：在方案逻辑审核阶段，按时间顺序完整依次输出方案过程中产生的全部修改内容，逐条清晰展示所有调整与变更细节。禁止静默修改。


5. **输出json格式方案**：
将实验生成阶段的方案转换为json格式。
   (1)**输出格式要求**：
   - 调用skills返回的实验可执行方案需满足json格式，包含步骤序列号，每步骤中的工作站中文名称、id、对应的操作名称、以及可接受的参数。
   - 第6步的校验只校验工作站的id，id必须写对。
   - 操作名称必须写对。
   - 每个工作站的必填项必须填写。
   - 开盖的瓶号必须使用对象数组格式，正确格式示例："开盖的瓶号": [{"瓶号": "1"}, {"瓶号": "2"}]，严禁输出纯数字数组格式："开盖瓶号": [1, 2]。

**示例参考如下，仅做参考，以实际的实验步骤输出**：


```
{
  "实验名称": "...",
  "steps": [
    {
      "step_number": 1,
      "id": 1427568512205824
      "workstation": "常规物料站",
	  "id":
      "operation": "物料拿取",
      "parameters": {
        "容器类型": "进样瓶",
        "容器数量": 2,
        "容器编号": [1, 2]
      }
    },
    {
      "step_number": 2,
      "workstation": "多通道固体称量_V1",
	  "id":1834679312417792
      "operation": "固体称量",
      "parameters": {
        "容器类型": "进样瓶",
        "容器数量": 2,
        "容器编号": [1, 2],
        "药品名称": [ "NiSO4·6H2O", "CoSO4·7H2O", "MnSO4·H2O"],
        "目标质量": [0.556, 0.0695, 0.0675],
        "称量精度": 0.0001
      }
    },
    "step_number": 3,
      "id": 1710453843264512,
      "workstation": "气相色谱仪_V1",
      "operation": "气相分析",
      "parameters": {
        "容器类型": "气相54孔板",
        "容器数量": 1,
        "容器编号": [
          1
        ],
        "新建方法文件":  "https://aichem-cloud-service.obs.cn-east-3.myhuaweicloud.com/validationnull/dbc1c537-07e2-4383-b279-568ffd91fb74.json",
        "开始分析行号": 1,
        "BatchFile":  "https://aichem-cloud-service.obs.cn-east-3.myhuaweicloud.com/validationnull/aaa1c537-07e2-4383-b279-568ffd91fb74.json",
        "batchFile使用次数": 1
      }
    }
  ]
    ],
    
   "unknown_steps": null
  }
```


6. **格式转换**：输出json格式后使用新技能`workflow-generator`。

7. **下发实验**：使用新技能`lab-operation`。
