# 输入与报告约定

## 输入形态与来源

接受完整 package 的 `workflow_json.steps` 与 `dispatch_payload`，也接受裸
`{steps}`、裸 `{experiment_steps, plan_name}` 或包装在 `terminal_package` 中的对象。
输入中的自报 `status=success` 不能作为校验依据。JSON 重复字段或语法错误不会被
静默接受；语法错误保留行、列。

只有 workflow 而没有 payload 时，默认使用确定性下发预览，报告的
`payload_source=generated_preview`。加 `--require-payload` 后，缺少真实 payload
不能放行。裸下发对象可以检查设备合同，但没有源 workflow 时
`source_correspondence=not_available`，不能声称完成源到下发对象的一致性对照。

`--workstations-dir` 指向包含各模块工作站目录和 `0410数据转换.txt` 的合同根目录，
不是指向某一个工作站目录。规则缺失或相互冲突时保留待核实项，不自动合并宽松规则。

## 初始容器状态

仅在有真实已知初始条件时传入 `--initial-state /path/to/initial_state.json`：

```json
{
  "containers": [
    {
      "container_type": "进样瓶",
      "container_id": 1,
      "lid_state": "有盖",
      "volume_ml": 1.0,
      "sample_state": "纯液态"
    }
  ]
}
```

容器按类型和正整数编号联合标识；不同类型的 1 号瓶不是同一容器。
`lid_state` 支持 `有盖`、`无盖`、`closed`、`open`；体积须为有限非负数。
样品状态词表包括无样品、粉末、固体、纯液态、悬浊液、上层上清液和下层固体沉淀及
用“或”连接的可能状态。不把多种可能状态中的有利一种视为确定事实。
省略状态字段意味着未知，所需条件无法确认时报告 `not_verifiable`。

相对附件路径只按 `--artifact-root` 或输入文件目录解析；不会去 Skill 模板目录
寻找同名文件作为替代。外部 URL、另一操作系统的路径或设备端可达性不能凭本地存在
来确认。输入和依赖文件不能与报告目标文件重合。

## 输出和退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | `status=passed` 且 `dispatchable=true` |
| `1` | 检查失败或无法验证，包括输入不可读、非法 JSON、参数错误、缺少必要事实 |
| `2` | CLI 无法运行或保存报告，包括参数错误、初始状态文件读取错误、报告覆盖输入、缺少仓库引擎 |

`workflow_check.json` 是机器结果，`workflow_check.md` 是同一结果的可读视图。
报告至少说明判定、检查范围、汇总和 `findings`。正常完成的报告还记录：

- `input_sha256`、`input_file_sha256`（文件入口）：原始输入的审计摘要。
- `source_manifest`：实际选中的规则目录、来源文件及 SHA-256。
- `check_context`：附件根目录及显式初始状态摘要。
- `payload_source`、`source_correspondence`：真实 payload/预览，以及是否有源 workflow 对照。
- `checks`、`checked_steps`、`limitations`：已执行项目、逐步状态和适用限制。

错误字段包括 `code`、`severity`、`stage`、`message`、`json_pointer`；步骤错误
还包含原始 `step_number`、零起始 `step_index`，可定位的规则带 `skill_path` 和
`skill_line`。`expected`、`actual`、`related_pointers` 仅在适用时出现；缺字段与
显式 `null` 不混为一谈。指针遵守 JSON Pointer 转义：`~` 写作 `~0`，`/` 写作 `~1`。

例如加液量误写成字符串时，应报告“第 3 步的原液用量应为数字，实际为字符串”，
并保留 `/steps/2/parameters/加样方案/0/1号原液瓶/原液用量` 与对应 Skill 行号。
不要把同一错误在 workflow 和 payload 两阶段的两条证据描述成两次独立设备故障。

## 离线自检

仓库的 `device_agent/examples/dispatch_check/` 包含明确标注为合成测试的合法 workflow
与嵌套参数反例，不是历史实验结果。在仓库根目录运行：

```bash
python chem_resources/lab-design-all/skills/workflow-checker/scripts/check.py --input device_agent/examples/dispatch_check/valid_workflow.json --report-dir checker_reports/skill-valid
python chem_resources/lab-design-all/skills/workflow-checker/scripts/check.py --input device_agent/examples/dispatch_check/nested_parameter_error.json --report-dir checker_reports/skill-invalid
```

分别预期退出 `0` 和 `1`。这两个输入没有实际 payload，演示不能加
`--require-payload` 后仍预期通过。修复后必须重新检查实际待用输入，不能复用示例报告。
