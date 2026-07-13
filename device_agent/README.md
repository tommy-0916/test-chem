# Device Agent 使用指南

`device_agent` 接收 research agent 保存的 state JSON，将其中的化学语义 `macro_plan` 映射为工作站级 `workflow_txt` 与 `workflow_json`。当前实现是单 Agent：一次映射流程只调用一次模型的 `.invoke()`，不再包含旧版 `pre_flow_agent -> workflow_generator -> verify_agent -> format_translate_agent` 多 Agent 链路。模型池配置可能在一次 `.invoke()` 内按后端重试。

设备层必须使用 LLM，没有 heuristic 模式，也不支持 `--disable-llm`。

## 安装与前提

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r device_agent/requirements.txt
export REFINER_LLM_API_KEY="<your-api-key>"
```

输入必须是 JSON object，并在以下任一位置包含非空 `macro_plan`：`device_adaptation_handoff`、外部交接输出、顶层或 `persistent_outputs`。默认 `chat` 模式调用 OpenAI-compatible Chat Completions；`codex_responses` 模式还要求本机存在 `codex` CLI（可用 `REFINER_CODEX_CLI_PATH` 指定）。

## 运行

```bash
python3 device_agent/run_from_research_state.py \
  --research-state path/to/research_state.json \
  --output device_agent/output/device_state.json \
  --package-output device_agent/output/device_package.json \
  --print-package-json
```

该示例使用默认设备真源。常用参数如下：

| 参数 | 作用 |
| --- | --- |
| `--research-state` | 必填，research state JSON 路径 |
| `--output` / `--package-output` | 分别保存完整运行 state / 终态 package |
| `--model-name` / `--api-key` / `--base-url` | 覆盖模型、密钥和 OpenAI-compatible endpoint |
| `--wire-api` | `chat`（默认）或 `codex_responses` |
| `--reasoning-effort` | `codex_responses` 的推理强度，默认 `xhigh` |
| `--timeout-seconds` / `--max-tokens` | 默认 `360` 秒 / `4096` tokens |
| `--workstations-dir` | 覆盖工作站真源目录 |
| `--full-workstations` | 将全部已加载工作站摘要放入 prompt；默认只选择与 query、stage 和 macro steps 相关的工作站，无法命中时回退到全部 |
| `--device-status-json` | 可选实时状态 JSON；支持扁平 map 或 `{"stations": {...}}` |
| `--print-macro-plan` / `--print-package-json` | 打印组装后的交接输入 / 终态 package |

## 真源与配置优先级

工作站真源优先使用 `--workstations-dir`（等价于 `CHEM_WORKSTATIONS_NEW_DIR`），否则依次查找：

1. `chem_resources/lab-design-main/skills/chemistry-experiment-workstation`
2. `chem_resources/lab-design-all/skills/chemistry-experiment-workstation`
3. `chem_resources/workstations_new`

`lab-design-*` 布局读取各工作站 `SKILL.md` 及 `references_audit/*_audit.md`；旧目录布局读取 `USAGE.md`、`AUDIT-RULES.md` 和可选 `SKILL.md`。TXT/JSON 格式参考默认为 `chem_resources/format_reference/reference.txt` 与 `reference.json`。可通过 `CHEM_RESOURCES_DIR`、`CHEM_TXT_FORMAT_REFERENCE`、`CHEM_JSON_FORMAT_REFERENCE` 覆盖。

模型配置可放在 `device_agent/.env` 或仓库根 `.env`。单后端主要变量为 `REFINER_LLM_MODEL_NAME`、`REFINER_LLM_API_KEY`、`REFINER_LLM_ENDPOINT_URL`；模型池使用连续编号的 `REFINER_LLM_POOL_<N>_{NAME,PROVIDER,MODEL_NAME,API_KEY,ENDPOINT_URL}`。设备状态也可通过 `CHEM_DEVICE_STATUS_JSON` 指定；离线工作站会作为限制写入 prompt，占用工作站会提示等待。

## 输出与校验边界

可映射时，终态 package 的 `status` 为 `success`，包含试剂槽位、容器计划、`workflow_txt`、`workflow_json` 和模型生成的 `device_self_check`；完整 state 的状态为 `completed`。无法映射时，package 返回 `status: feasibility_error`、`feedback_type: device_feasibility_error` 和 `error_package`，完整 state 的状态为 `feasibility_error`。可行性错误仍属于正常终态，CLI 返回码为 `0`；模型调用或输出结构异常会直接失败并返回非零。

`device_self_check` 不是独立的确定性验证器，而是同一次模型输出的一部分。当前代码只在字段文本显式包含 `fail`、`失败`、`不通过` 或 `未通过` 时拒绝成功结果；缺失自检、隐含矛盾以及 TXT/JSON 一致性不会被程序完整验证。执行真实设备前仍需外部审计与人工复核。
