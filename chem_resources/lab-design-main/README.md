# Lab Design Skills (main)

本目录是 303 实验室规划与工作流转换资源的 `main` 快照，不是独立的端到端应用。当前只包含三个 Skill；不存在 `experiments-design/`、`run_design.py` 或 `workflow-generator-1.0.0/`。实验方案需要由上层 Agent 依据工作站规则生成，再显式传给转换脚本。

## 目录与职责

| 模块 | 作用 | 可执行入口 |
| --- | --- | --- |
| [`chemistry-experiment-workstation`](skills/chemistry-experiment-workstation/SKILL.md) | 工作站能力、操作参数、输入输出约束、跨站审计规则和附件模板 | 主要是规则库；不是实验设计 CLI |
| [`workflow-generator`](skills/workflow-generator/SKILL.md) | 将结构化实验步骤提交给工作流解析服务并返回模板信息 | `skills/workflow-generator/scripts/generate.py` |
| [`lab-operation`](skills/lab-operation/SKILL.md) | 查询实验室、模板、任务、工作站和任务结果；提供下发/启动入口 | `skills/lab-operation/scripts/*.py` |

工作站规则分别位于 `references-Synthesis-Module/`、`references-Reaction-and-Testing-Module/` 和 `references-Characterization-Module/`；跨站约束位于 `references_audit/`，文件型参数模板位于 `references_files/`。

## 环境准备

在本目录执行：

```bash
python3 -m pip install requests
```

`workflow-generator` 使用以下环境变量：

- `AICHEM_APP_TOKEN`：认证值。脚本只读取环境变量；输入 JSON 中即使包含 `token` 也会被忽略。请通过安全的密钥管理方式设置，不要写入仓库或命令历史。
- `WORKFLOW_SERVICE_URL`：仅填写 `host:port`，不要包含协议或路径。脚本固定请求 `http://<host:port>/parse_workstation`；未设置时会使用源码内的默认地址，部署时应显式覆盖。

## 生成工作流模板

```bash
python3 skills/workflow-generator/scripts/generate.py '<JSON>'
```

脚本只接受一个 JSON 字符串参数。其本地校验契约为：

- 顶层必须包含对象 `experiment_steps` 和字段 `plan_name`。
- `experiment_steps.steps` 必须是数组。
- 每个步骤必须包含正整数 `step_number`、`workstation` 和 `operation`。
- `id`、`parameters`、`unknown_steps` 等字段会随 `experiment_steps` 原样转发，但脚本不校验其业务正确性。

请求形状示例（工作站名称、ID、操作和参数必须以对应工作站 `SKILL.md` 为准）：

```json
{
  "experiment_steps": {
    "steps": [
      {
        "step_number": 1,
        "workstation": "常规物料站",
        "id": 1427568512205824,
        "operation": "物料拿取",
        "parameters": {
          "容器类型": "进样瓶",
          "容器数量": 1,
          "容器编号": [1]
        }
      }
    ],
    "unknown_steps": null
  },
  "plan_name": "example_plan"
}
```

成功时标准输出先包含解析提示，再打印服务端 JSON；若响应含 `data.template_id`，还会打印模板 ID 提示。因此 stdout 不是纯 JSON，也不会写本地结果文件。HTTP 错误、非 `200` 业务码或无效 JSON 会以退出码 `1` 结束。该脚本没有超时、重试或离线模式，并会真实访问模板解析服务；它不会自动读取工作站规则，也不会下发实验任务。

## 实验室操作与安全边界

查询类脚本会根据 `skills/lab-operation/scripts/config.py` 访问真实网关，运行前需安全配置 `AICHEM_CLOUD_GATEWAY` 和 `AICHEM_APP_TOKEN`。真实任务下发与启动则已在代码中阻断：

```bash
python3 skills/lab-operation/scripts/generate_task.py \
  --template-id <id> --template-source-label <lab> --app-label <lab>
python3 skills/lab-operation/scripts/start_task.py --task-id <id> --app-label <lab>
```

这两个命令只返回 `success: false`、`blocked: true` 和 `dispatch_blocked: true` 的结构化结果，不访问云网关，不创建或启动任务。不要将本目录描述为可直接执行真实实验。

## 版本差异与已知限制

| 快照 | 当前差异与限制 |
| --- | --- |
| `lab-design-main`（本目录） | 缺少 `skills/lab-operation/scripts/utils.py`；`select_task_list.py`、`select_template_list.py`、`select_workstation_list.py` 和 `select_workstation_type_list.py` 在导入阶段即失败。其他联网查询也依赖可用网关和有效授权，尚不能据此视为完整可用。 |
| `lab-design-all` | 包含 `utils.py` 和少量额外资源，但 `select_template_list.py` 以三个参数调用只接受两个参数的 `adapter_data`；接口成功返回并进入适配阶段后仍会失败。其工作站规则内容也与 `main` 有差异，不能假定为已验证的严格超集。 |
