# Chem Agent 八题评估包

这个压缩包用于把 Chem Agent 的固定八题评估流程交给另一位使用者。对方已有基础 Chem Agent 源码和 45 个工作站 Skill，因此本包不重复携带工作站 Skill，也不会修改它们。

本包包含：

- `skill/chem-agent-eval-sop/`：Codex 评估 Skill；
- `test-data/测试题目.docx`：A01–D02 共 8 道固定测试题；
- `compat/device_agent/run_from_research_state.py`：支持并发实验 ID 的 Device CLI 兼容文件；
- `scripts/install.py`：安装 Skill、测试题和必要兼容文件；
- `scripts/preflight.py`：运行前检查；
- `.env.example`：不含真实密钥的配置模板。

## 1. 使用前提

对方需要准备：

1. 一份基础 Chem Agent 仓库；
2. Python 环境和项目依赖，推荐仓库内已有 `.venv`；
3. 45 个工作站 Skill，路径必须是：

   `chem_resources/lab-design-main/skills/chemistry-experiment-workstation`

4. 自己可用的模型、API endpoint、wire API 和 API key；
5. Codex，并允许从 `~/.codex/skills/` 加载个人 Skill。

本评估只根据工作站能力做规划和格式校验，不会向真实工作站下发任务。

## 2. 一键安装

解压后，在本包目录运行：

```bash
python3 scripts/install.py --repo "/你的路径/chem-agent"
```

安装脚本只执行以下操作：

- 安装或更新 `~/.codex/skills/chem-agent-eval-sop`；
- 把 `测试题目.docx` 放到 Chem Agent 仓库根目录；
- 如果 Device CLI 缺少 `--exp-id`，先备份原文件，再只替换 `device_agent/run_from_research_state.py`；
- 不修改其余源码，不修改或复制 45 个工作站 Skill，不创建 `.env`。

如果明确不希望安装兼容文件：

```bash
python3 scripts/install.py --repo "/你的路径/chem-agent" --skip-compat
```

如果 `CODEX_HOME` 不是默认位置：

```bash
python3 scripts/install.py \
  --repo "/你的路径/chem-agent" \
  --codex-home "/你的路径/.codex"
```

安装过程中如目标文件已存在且内容不同，脚本会先生成带时间戳的备份。

## 3. 配置自己的模型和 API key

不要把真实 API key 发给别人，也不要把 `.env` 提交到 Git。每位使用者应自行配置。

可复制模板：

```bash
cp .env.example "/你的路径/chem-agent/.env"
```

然后编辑 Chem Agent 仓库中的 `.env`。单 key 最小配置示例：

```env
REFINER_LLM_MODEL_NAME=你的模型名
REFINER_LLM_ENDPOINT_URL=https://你的服务地址/v1
REFINER_LLM_API_KEY=你的API_KEY
REFINER_LLM_WIRE_API=chat
REFINER_LLM_REASONING_EFFORT=medium
```

如果服务提供 OpenAI Responses/Codex Responses 协议，使用：

```env
REFINER_LLM_WIRE_API=codex_responses
```

如果服务只支持 Chat Completions，使用：

```env
REFINER_LLM_WIRE_API=chat
```

并发时可在 `.env` 中提供多个 key：

```env
CHEM_AGENT_EVAL_API_KEYS=["key1","key2","key3","key4"]
```

也可以使用任意自定义环境变量名，运行时通过 `--api-key-env` 指定。评估脚本只记录变量名和 key 数量，不记录 key 内容。

## 4. 运行前检查

在本包目录执行：

```bash
python3 scripts/preflight.py \
  --repo "/你的路径/chem-agent" \
  --api-key-env REFINER_LLM_API_KEY
```

预检会检查：

- 八题 DOCX 能否提取出 A01–D02，且正好是 8 个唯一 Query；
- 45 个工作站目录是否齐全；
- Research CLI 是否支持联网论文搜索、设备上下文和自定义模型参数；
- Device CLI 是否支持完整工作站描述与独立 `--exp-id`；
- API key 环境变量或仓库 `.env` 是否存在，但不会打印其值；
- 评估 runner 是否保持“只规划、不真实下发”。

所有项目显示 `PASS` 后再开始正式测试。

## 5. 推荐运行方式：让 Codex 使用 Skill

在 Codex 中打开 Chem Agent 仓库，然后提出：

```text
使用 $chem-agent-eval-sop 对当前 Chem Agent 运行完整八题评估。
测试题使用仓库根目录的 测试题目.docx，开启联网论文检索，保留原始 Query，
只做工作站能力规划和格式校验，不下发真实设备任务。
模型、endpoint、wire API、reasoning effort 和 API key 使用我的 .env 配置。
```

Skill 会运行 8 个案例，并基于原始结果完成逐题评估和总报告。默认并发数为 4；每题使用 `A01-时间`、`A02-时间` 等独立 ID，不会互相覆盖。

## 6. 直接运行八题 runner

如果只希望先生成 Chem Agent 原始输出，可直接运行：

```bash
python3 "$HOME/.codex/skills/chem-agent-eval-sop/scripts/run_suite.py" \
  --repo "/你的路径/chem-agent" \
  --docx "/你的路径/chem-agent/测试题目.docx" \
  --model "你的模型名" \
  --endpoint "https://你的服务地址/v1" \
  --wire-api "chat" \
  --reasoning-effort "medium" \
  --api-key-env "REFINER_LLM_API_KEY" \
  --workers 4
```

AnyRouter + Responses 协议示例：

```bash
python3 "$HOME/.codex/skills/chem-agent-eval-sop/scripts/run_suite.py" \
  --repo "/你的路径/chem-agent" \
  --docx "/你的路径/chem-agent/测试题目.docx" \
  --model "gpt-5.6-sol" \
  --endpoint "https://anyrouter.top/v1" \
  --wire-api "codex_responses" \
  --reasoning-effort "xhigh" \
  --api-key-env "CHEM_AGENT_EVAL_API_KEYS" \
  --workers 4
```

`--online-literature` 在正式评估中始终开启。若还要测试开放网页检索和开放获取 PDF 下载，额外添加：

```bash
--deep-literature
```

直接运行 runner 只生成原始运行产物；完整的论文质量、研究方案和工作站匹配评估应由 Codex 按 Skill 继续完成。

## 7. 结果在哪里

每次正式运行会新建：

```text
<chem-agent>/result/chem-agent-eval-YYYYMMDD-HHMMSS/
```

主要文件：

- `suite_manifest.json`：模型、endpoint、协议、并发数和 8 个本地运行 ID；
- `raw_summary.json`：8 题流程状态汇总；
- `<case>/input.json`：从 DOCX 提取的原始 Query；
- `<case>/query.sha256`：原始 Query 完整性校验；
- `<case>/research_state.json`：Research Agent 原始状态；
- `<case>/research_cli.log`：Research 运行日志；
- `<case>/knowledge_base/`：该题独立的联网论文资料；
- `<case>/device_state.json`、`device_package.json`：Device 规划结果；
- `<case>/case_summary.json`：该题流程摘要；
- `evaluation/<case>/evaluation.md` 和 `evaluation.json`：逐题评估；
- `evaluation/overall_report.md`：八题总报告；
- `evaluation/verdict_matrix.json`：机器可读的结论矩阵。

Device 首次返回异常 JSON 时，runner 最多按完全相同输入重试一次，并同时保留 attempt 1 和 attempt 2，绝不隐藏首次失败。

## 8. 评估输出

每题只输出离散结论，不打分：

1. `process_completion`: `yes | partial | no`；
2. `paper_quality_summary`: `high | mixed | low`，同时列出论文是否发表、期刊/会议、年份、DOI、期刊层级、可验证影响因子和方法支持情况；
3. `plan_workstation_match`: `yes | no | not_evaluable`；
4. `dispatch_schema_match`: `yes | no | not_evaluable`。

第 3 项判断实验操作是否能由 45 个工作站完成；第 4 项单独判断 Device JSON 的字段、类型、枚举、单位、范围和嵌套是否严格符合工作站 Skill 输入合同。

## 9. 常见问题

### 预检提示 Device 缺少 `--exp-id`

重新运行安装脚本且不要使用 `--skip-compat`。安装器会备份并只替换 `device_agent/run_from_research_state.py`。

### 工作站数量不是 45

确认目录结构和模块名未改变，并确认 45 个工作站 Skill 位于指定路径。本包不包含这些 Skill。

### Research 没有生成 Macro Plan

这是有效测试结果，不要人工补写。保留 Research 状态和日志，Device 映射会被跳过，评估通常标为 `partial` 或 `no`。

### Device 返回 `feasibility_error`

不能直接等同于真实设备不可行。评估器会重新对照 45 个 Skill，区分真实能力缺失、开放参数越界、容器衔接问题、固定参数被误判、Skill 描述不足和模型映射错误。

### Device JSON 格式异常

runner 会按相同输入自动重试一次并保留两次结果。最终报告必须注明首次异常。

### LLM 长时间没有输出或超时

`codex_responses` 在当前 Chem Agent 中是非流式请求，长时间安静不一定代表卡死。默认单次 LLM 超时 7200 秒；并发数可用 `--workers` 调低。

### 联网论文搜索与开放网页搜索有什么区别

正式评估始终开启学术检索 `--online-literature`。`--deep-literature` 才会额外开启开放网页和 PDF 下载，因此对比不同运行时必须记录是否启用了该参数。

## 10. 安全说明

- 本包不包含真实 `.env`、API key、历史日志、运行结果、Git 元数据、缓存或 45 个工作站 Skill；
- 不要把真实 key 写进命令行、README、报告或 Git；
- 评估 ID 是本地规划 ID，不是实验室真实任务 ID；
- runner 不调用真实执行适配器或工作站下发 API。
