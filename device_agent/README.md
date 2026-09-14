# Device Agent 使用指南

`device_agent` 接收 research agent 保存的 state JSON，将其中的化学语义 `macro_plan` 映射为工作站级 `workflow_txt` 与 `workflow_json`。生产路径首先让一个独立 Device LLM 读取完整 Research handoff、前后 macro steps 和完整工作站摘要，生成冻结的结构化语义合同（能力类别、数量含义、核心化学/观察边界、物料 identity IDs）；计划 LLM 随后只能引用该合同。确定性代码只核对合同完整性、工作站真源、数值、谱系和证书，不再用自然语言关键词代替这些化学语义判断。之后系统通过路线可行性门并签发 `feasibility_certificate`，再在 Device 层生成和审核 workflow；路线一旦接受，后续数量、翻译、参数或 Skill 错误都不会返回 Research。

设备层必须使用 LLM，没有 heuristic 模式，也不支持 `--disable-llm`。

旧关键词兼容路径仅供历史单元测试：必须同时设置 `CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS=off` 和 `CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS=1` 才会启用。正式 campaign 不设置第二个测试专用开关，因此始终执行 LLM 全上下文语义审查。

## 安装与前提

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r device_agent/requirements.txt
export REFINER_LLM_API_KEY="<your-api-key>"
```

输入必须是 JSON object，并在以下任一位置包含非空 `macro_plan`：`device_adaptation_handoff`、外部交接输出、顶层或 `persistent_outputs`。默认 `chat` 模式调用 OpenAI-compatible Chat Completions；`codex_responses` 的原生工具通道使用直接 Responses API。模型必须支持 `bind_tools` 对应的原生工具协议；工具阶段不支持 `REFINER_RESPONSES_TRANSPORT=cli/codex_cli`，也不会静默退回文本协议。独立无工具 JSON 调用保留既有传输实现；其 CLI 路径仍需要本机 `codex`（可用 `REFINER_CODEX_CLI_PATH` 指定）。

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
| `--timeout-seconds` / `--max-tokens` | 默认 `600` 秒 / `32768` tokens |
| `--workstations-dir` | 覆盖工作站真源目录 |
| `--full-workstations` | 保留旧 CLI 参数兼容性；正常流程统一使用全站目录和按需完整合同加载，不再用关键词筛选替代能力目录 |
| `--device-status-json` | 可选实时状态 JSON；支持扁平 map 或 `{"stations": {...}}` |
| `--print-macro-plan` / `--print-package-json` | 打印组装后的交接输入 / 终态 package |

## 真源与配置优先级

工作站真源优先使用 `--workstations-dir`（等价于 `CHEM_WORKSTATIONS_NEW_DIR`），否则依次查找：

1. `chem_resources/lab-design-all/skills/chemistry-experiment-workstation`
2. `chem_resources/lab-design-main/skills/chemistry-experiment-workstation`（兼容旧快照）
3. `chem_resources/workstations_new`

`lab-design-*` 布局读取各工作站 `SKILL.md` 及 `references_audit/*_audit.md`；旧目录布局读取 `USAGE.md`、`AUDIT-RULES.md` 和可选 `SKILL.md`。TXT/JSON 格式参考默认为 `chem_resources/format_reference/reference.txt` 与 `reference.json`。可通过 `CHEM_RESOURCES_DIR`、`CHEM_TXT_FORMAT_REFERENCE`、`CHEM_JSON_FORMAT_REFERENCE` 覆盖。

模型配置可放在 `device_agent/.env` 或仓库根 `.env`。单后端主要变量为 `REFINER_LLM_MODEL_NAME`、`REFINER_LLM_API_KEY`、`REFINER_LLM_ENDPOINT_URL`；模型池使用连续编号的 `REFINER_LLM_POOL_<N>_{NAME,PROVIDER,MODEL_NAME,API_KEY,ENDPOINT_URL}`。设备状态也可通过 `CHEM_DEVICE_STATUS_JSON` 指定；离线工作站会作为限制写入 prompt，占用工作站会提示等待。

## 分层修复与输出边界

设备流程保留两段：先生成 `device_plan`、实体容器/槽位和批次分配，再生成 `workflow_json` 并确定性转换为 `dispatch_payload`。Research 的逻辑容器需求不是已经分配的机器瓶号。

模型最初只看到全局跨站规则和完整 45 站能力目录，通过 LangChain 原生 `load_workstation_skill(station_code)` 加载选中站的完整 SKILL、audit、操作参数树和 wire 契约。引用未加载站的候选会先补齐合同并重审，负面能力结论必须核验完整候选目录；不截断详细参数。加载记录在 `loaded_workstation_skills` 和 `skill_load_events` 中可审计。

翻译默认每块最多 6 个设备计划步骤；若供应商明确报告上下文超限，当前块按顺序二分，保留完整合同、冻结计划、源步骤 ID 和前序容器状态。单步仍超限则明确失败，不截断参数，不将普通 API 或格式错误当作超限。

确定性检查始终保留全设备目录。版本 2.2 的可行性证书绑定独立的完整真源、机器契约、设备状态及必需外部返回合同，而不是提示词全文；未选中工作站的事实变化同样会使旧证书失效。

冻结 Research 输入若要求外部 observation/manual_handoff 返回，且该等待点后仍有机器步骤，Device 返回 `manual_required` / `device_external_return_wait_required`，保留 `pending_returns` 和原候选用于审查，但不签证、不暴露可下发 workflow。末尾观测交给现有 observation 边界；Device override 不能代替真实返回。

每个已接受的 Device plan 最多经历两个 workflow 周期。每个周期包含一个初始候选和最多 8 个新的 LLM 修改候选；每个候选都重新经过确定性参数校验、配方物化、dispatch formatter 和完整 Workstation Skill 审核。第一周期耗尽后仅允许一次 Device plan 最小改写，第二周期仍失败则返回 `status: manual_required`，不会进入 Research B2。

终态错误按责任层分类：

- `research_replan_required`：只用于签证前已证明的路线硬缺口，`feedback_route=research`、`failure_scope=route_feasibility`。
- `device_local_quantity_error`：剂量、分批、容量、料位和物料台账错误。
- `device_workflow_error`：schema、工作站、operation、容器、瓶盖、translation 或 Skill 错误。
- `device_internal_error`：API、解析、formatter、配方物化或运行时错误。
- `human_review_required`：两周期耗尽、未知收率或无法确认合批权限。

成功 package 除 `workflow_txt`、`workflow_json` 和平台 dispatch payload 外，还携带 `quantity_adjustments`、`batch_plan`、`material_ledger`、`feasibility_certificate` 与修复历史。任何 dispatch/quantity/recipe gate 失败都会阻止 `status: success`。

人工修订由 campaign 入口生成 `AWAITING_DEVICE_REPAIR.md`、`device_repair_request.json` 和 `device_plan_override.template.json`。编辑模板后可从冻结的 Device 层续跑：

```bash
python3 run_campaign.py \
  --resume-device-repair path/to/device_repair_request.json \
  --device-plan-override path/to/device_plan_override.json \
  --execution-adapter mock
```

续跑会校验 Research state、路线、试剂顺序、样品矩阵和完整设备真源快照，不重新 bootstrap Research，也不消耗新的 Research↔Device iteration。真实设备执行仍由外部执行边界控制；本命令不会绕过该边界。

对于 `unknown_yield`，可把模板中的 `human_quantity_approval_template` 复制到
`human_quantity_approvals[]`。每项批准必须绑定原始 repair request 文件 SHA-256、
feasibility certificate、路线/样品矩阵/设备快照签名，以及具体的
`transition_id/sample_id/material_id/batch_id` 和数量。`basis=observed_quantity`
表示具名人工对测量值作出证明（不会伪装成普通 observation）；
`basis=planning_yield_lower_bound` 表示具名人工批准规划收率下界。两者都必须明确
确认 scientific review。入口校验成功后才生成 Device quantity gate 可消费的
进程内 typed approval bundle；该 bundle 不写入 override、handoff、日志或 JSON
产物，并由一次性 capability 绑定。bundle 同时签入 effective
`device_plan/quantity_adjustments/batch_plan/material_transitions/material_ledger`
的 canonical digest，
因此批准后再修改 transition、quantity basis、批次或 ledger 会被拒绝。模型或计划
自报任何 validated/trusted approval 字段也会被拒绝。
