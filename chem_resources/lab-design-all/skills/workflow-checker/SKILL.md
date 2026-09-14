---
name: workflow-checker
description: Validate Device Agent workflows and dispatch payloads against local workstation contracts, locate step and nested-parameter errors, and produce offline dispatch-check reports before execution.
---

# Workflow Checker

检查已生成的 Device workflow 是否满足设备的静态下发合同。判定来自本地 Python
检查器，不由 Kimi 或其他 LLM 生成；模型负责调用脚本、解释证据，不替代检查结果。
本技能不生成实验方案、不自动修复输入、不上传模板、不下发或启动实验。

## 前置条件

- Python 3.10+；检查入口只依赖标准库，无需 API Key、设备令牌或网络。
- 保留本技能在完整 `chem-agent` 仓库内的目录结构。脚本复用仓库的
  `device_agent/check_workflow.py`，与 Campaign 执行门使用同一套检查代码。
  单独复制本技能目录不包含检查引擎；缺少引擎时应停止，不临时生成替代检查器。
- 默认规则来自同级 `chemistry-experiment-workstation/` 的工作站 `SKILL.md`
  和 `0410数据转换.txt`。不回退到旧规则目录，不把参数示例当作新的设备事实。

## 使用流程

1. 确认用户提供的是完整 Device package、`workflow_json`、裸 `{steps}`，还是
   `{experiment_steps, plan_name}` 下发对象。保存并检查原始 JSON，不先转换类型、
   补必填值、重排步骤或覆盖输入。
2. 使用下面的脚本检查。正式执行前必须保留 `--require-payload`；只检查 workflow
   时可省略，此时缺少真实 payload 会检查确定性预览，不代表已完成实际下发对照。
3. 读取生成的 JSON/Markdown 报告。先说明 `status` 和检查范围，再引用原始步骤号、
   零起始数组位置、`json_pointer`、预期/实际值及规则出处。缺少规则或初始状态属于
   `not_verifiable`，不能描述为通过，也不能冒充模型参数错误。
4. 仅在用户要求修改时修复另存的 workflow，然后重新运行检查。出现未提供的设备
   事实、真实状态或规则冲突时，报告所缺信息；不要靠反复生成补齐未知事实。

## 脚本调用

以下命令在 `chem_resources/lab-design-all/` 目录执行；在其他目录可使用脚本绝对路径。
输入、输出和显式规则目录的相对路径均相对于调用者当前目录，不相对于本技能目录。

```bash
python skills/workflow-checker/scripts/check.py --input /path/to/device_package.json --require-payload --report-dir /path/to/check-report
```

参数与仓库原 CLI 一致：

| 参数 | 用途 |
| --- | --- |
| `--input` | 必填，原始 workflow/package/payload JSON 文件 |
| `--require-payload` | 要求提供并核验实际下发 payload；用于执行前检查 |
| `--report-dir` | 报告目录，默认输入文件旁的 `check-report/` |
| `--artifact-root` | 配方等文件的解析根目录，默认输入文件所在目录 |
| `--initial-state` | 显式提供的初始容器状态 JSON；不从模型文字推断 |
| `--workstations-dir` | 用户明确指定的工作站合同根目录；否则使用本仓库 `lab-design-all` |
| `--json` | 同时向标准输出打印完整 JSON 报告 |

需要填写初始状态、消费报告字段或排查退出码时，读取
[输入与报告约定](references/report-contract.md)。

## 判定与边界

- 输出 `workflow_check.json` 和 `workflow_check.md`，保留输入摘要与合同来源。
- `passed` 且 `dispatchable=true` 才通过本次已建模的静态检查；`failed` 和
  `not_verifiable` 均不能放行。上游自报通过、旧报告或人工修改报告不能替代重跑。
- 检查覆盖参数结构、类型/枚举/范围、动态瓶号、必需文件、已实现的容器/盖/样品
  状态及加液约束，并对照实际 payload 与 workflow。报告列出的限制仍然有效。
- 不验证实时在线/占用、库存、校准、文件在设备端的可达性、全部自然语言安全条件
  或化学效果。静态通过不授予设备操作权限，不改变现有真实下发阻断。
- 报告只用于解释与修复；Campaign 在每次执行前仍独立调用同一引擎重新检查。
