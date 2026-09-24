# 实验室操作脚本（lab-design-all）

本目录提供实验室鉴权、任务与模板查询、工作站查询，以及任务下发/启动入口。脚本以 JSON 输出结果，并将运行日志写入 `/tmp/logs/skill-lab-operation/`。

## 环境与配置

需要 Python 3 和 `requests`：

```bash
python3 -m pip install requests
cd chem_resources/lab-design-all/skills/lab-operation
export AICHEM_CLOUD_GATEWAY="<gateway-url>"
export AICHEM_APP_TOKEN="<app-token>"
```

`scripts/config.py` 通过 `AICHEM_CLOUD_GATEWAY` 和 `AICHEM_APP_TOKEN` 读取网关地址及鉴权令牌。请在运行环境中注入真实值，不要把令牌写入 README、日志、源码或提交记录。

当前 `lab-design-all` 副本的 `config.py` 仍带有一个历史非空 token fallback。不要依赖该默认值；应将其视为已泄露并在部署侧轮换，同时始终显式设置 `AICHEM_APP_TOKEN`。这属于当前实现的已知安全遗留，不代表推荐配置。

除下发/启动入口外，鉴权、任务结果和查询脚本都会向配置的网关发起 GET 请求；它们不是离线命令，需要可达网络和有效凭据。

## 安全边界

`generate_task.py` 和 `start_task.py` 的真实实验操作已由 `dispatch_guard.py` 阻断。它们不会访问网关，不会创建或启动任务，只返回 `success: false`、`blocked: true`、`dispatch_blocked: true` 等状态；返回值不包含 `task_id`。

```bash
python3 scripts/generate_task.py \
  --template-id dry-run-template \
  --template-source-label test-lab \
  --app-label test-lab

python3 scripts/start_task.py --task-id dry-run-task --app-label test-lab
```

以上命令仅用于验证安全阻断，不代表任务已下发或启动。

## CLI 索引

| 命令 | 必填参数 | 主要可选参数 |
| --- | --- | --- |
| `get_current_lab_label.py` | 无 | 无 |
| `check_lab_consistency.py` | `--app-label` | 无 |
| `generate_task.py` | `--template-id`、`--template-source-label`、`--app-label` | 无；始终阻断 |
| `start_task.py` | `--task-id`、`--app-label` | 无；始终阻断 |
| `select_task_list.py` | `--app-label` | `--start-date`、`--end-date`、`--create-user-name`、`--status`、`--task-name`、`--template-name`、`--task-id`、`--size` |
| `get_task_result.py` | `--task-id`、`--app-label` | 无 |
| `select_template_list.py` | `--app-label` | `--start-date`、`--end-date`、`--create-user-name`、`--enable`、`--source-type`、`--name`、`--template-id`、`--size` |
| `select_workstation_list.py` | `--app-label` | `--workstation-name`、`--workstation-type-name`、`--model-name`、`--status`、`--size` |
| `select_workstation_type_list.py` | `--app-label` | `--name`、`--definer`、`--update-time-start`、`--update-time-end`、`--size` |

时间参数格式为 `yyyy-MM-dd HH:mm:ss`，列表命令的 `--size` 默认值为 `10`。除 `get_current_lab_label.py` 外，使用 `python3 scripts/<name>.py --help` 可查看参数说明；该无参数脚本不会解析 `--help`，直接运行会访问网关。

## 当前限制

`scripts/utils.py` 中的 `adapter_data` 只接受两个参数，但 `select_template_list.py` 在网关返回成功数据后以三个参数调用它。因此模板查询的成功数据处理路径当前会返回适配失败信息，不能视为可用结果。

其余查询及鉴权脚本可运行，但实际成功仍取决于依赖已安装、网关可达、令牌有效以及服务端响应符合预期。下发和启动入口始终按上述安全边界阻断。
