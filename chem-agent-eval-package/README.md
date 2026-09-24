# Chem Agent 八例黑箱评估 Skill 包

本包安装一个自包含的 `chem-agent-eval-sop` Skill。接收者只需准备 Chem Agent 仓库、固定八题 DOCX 和真实 LLM 环境变量，即可执行预检、八例黑箱运行、双层 workstation 审计、正式报告和完成性验收。

要求 Python 3.10 或更高版本；优先使用待测仓库的 `.venv/bin/python`。

## 安装

```bash
python3 scripts/install.py --repo "/path/to/chem-agent"
```

安装器把 Skill 复制到 `${CODEX_HOME:-$HOME/.codex}/skills/chem-agent-eval-sop`。如果目标 Skill 已存在，会先创建时间戳备份。只有仓库缺少 `测试题目.docx` 时才自动复制本包测试文件；使用 `--replace-docx` 才会备份并替换已有文件。

安装器不会修改 Chem Agent 源码，也不会保存 API key。

## 配置

在 Chem Agent 自己的 `.env` 或 shell 环境中设置：

```text
REFINER_LLM_MODEL_NAME
REFINER_LLM_ENDPOINT_URL
REFINER_LLM_API_KEY
REFINER_LLM_WIRE_API
REFINER_LLM_REASONING_EFFORT
```

参考 `.env.example`。不要把真实密钥写入或转发本包。

## 真实预检

```bash
python3 scripts/preflight.py \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --probe-api
```

预检必须返回 `ready: true`，并确认 provider 返回了非空真实 LLM 文本。

## 使用 Skill

在 Codex 中说：

```text
使用 $chem-agent-eval-sop 对这个 Chem Agent 完成固定八例黑箱评估，生成正式报告并严格验证完成性。
```

Skill 会使用公开 `run_campaign.py`，保留原始输出，对每个 Device workflow 同时执行确定性和独立 LLM 审查，并生成：

```text
<repo>/result/chem-agent-eval-YYYYMMDD-HHMMSS/
├── suite_manifest.json
├── raw_summary.json
├── A01/ ... D02/
└── evaluation/
    ├── workstation_schema_audit.json
    ├── workstation_schema_llm_review.json
    ├── workstation_schema_verdict.json
    ├── A01/evaluation.md ... D02/evaluation.json
    ├── verdict_matrix.json
    ├── overall_report.md
    ├── workstation_direct_acceptance.md
    └── completion_check.json
```

只有 `completion_check.json` 的 `complete` 为 `true` 才算完成。任何 Query 变化、缺失案例、未覆盖 workflow、失败或 fallback LLM 调用、缺失 workstation 直接接收结论都会使验收失败。

## 直接命令

不通过 Codex 对话时，可直接运行黑箱和 schema 审计：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/chem-agent-eval-sop/scripts/run_suite.py" \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --workers 4
```

语义性论文质量、plan-to-workstation 和回归结论仍须按 Skill rubric 写入正式报告，再运行 `validate_evaluation.py`。脚本不会伪造这些判断。
