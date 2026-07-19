# Chem Agent 黑盒八题评估包

该包把完整 Chem Agent 当作黑盒评估。评估器只提供八个原始 Query 和强制联网论文检索参数，不直接调用或重新编排 Research Agent、Device Agent、设备可行性反馈、observation 或修复分支。

## 收件人快速开始

1. 解压本包并进入目录：

   ```bash
   unzip chem-agent-eval-blackbox-20260720.zip
   cd chem-agent-eval-package
   ```

2. 在待测 Chem Agent 项目自身的 `.env` 或 shell 环境中配置真实 API key。可参考本包的 `.env.example`，但不要把真实 key 写入或转发本包。

3. 安装 Skill 并复制八题测试文件：

   ```bash
   python3 scripts/install.py --repo "/path/to/chem-agent"
   ```

4. 执行预检：

   ```bash
   python3 scripts/preflight.py \
     --repo "/path/to/chem-agent" \
     --api-key-env REFINER_LLM_API_KEY
   ```

5. 预检通过后，可以在 Codex 中说“使用 `chem-agent-eval-sop` 评估这个 Chem Agent”，也可以直接执行下文的 runner 命令。

建议先在待测 Chem Agent 的副本或独立工作目录中运行。联网八题评估会真实调用模型和论文检索服务，并在待测项目的 `result/` 下写入结果。

## 安装

```bash
python3 scripts/install.py --repo "/path/to/chem-agent"
```

安装器只会：

- 安装 `chem-agent-eval-sop` 到 `$CODEX_HOME/skills`；
- 将测试 DOCX 放到 Chem Agent 根目录；
- 在覆盖不同版本时备份原文件。

安装器不会修改 Chem Agent 源码。

## 预检

```bash
python3 scripts/preflight.py \
  --repo "/path/to/chem-agent" \
  --api-key-env REFINER_LLM_API_KEY
```

预检确认：

- DOCX 中包含 A01–D02 八个唯一 Query；
- 45 个工作站 Skill 可用于输出审计；
- `run_campaign.py` 是可调用的公开入口并支持 `--query`、`--campaign-id`、`--campaigns-root` 和 `--online-literature`；
- evaluation runner 不直接调用 Research/Device 内部 CLI；
- 确定性审计器和独立 LLM reviewer 已安装；
- API key 存在但不会打印。

## 运行

```bash
python3 "$HOME/.codex/skills/chem-agent-eval-sop/scripts/run_suite.py" \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --workers 4
```

每题只通过公开入口调用：

```bash
python run_campaign.py \
  --query "<exact Query>" \
  --campaign-id "<case>-<timestamp>" \
  --campaigns-root "<isolated output root>" \
  --online-literature
```

`campaign-id` 和 `campaigns-root` 只用于隔离、定位输出。runner 不传入 `--include-device-context`、`--full-workstations`、execution adapter、mock observation 或其他内部策略参数。

如果 Chem Agent 输出 `AWAITING_OBSERVATION.md`，而八题没有提供 observation，runner 将其记录为 `awaiting_observation` 外部状态并停止该进程，不会生成假 observation。若要评估完整多轮闭环，测试输入必须另外提供 observation fixture。

## 结果

```text
<repo>/result/chem-agent-eval-YYYYMMDD-HHMMSS/
├── suite_manifest.json
├── raw_summary.json
├── A01/
│   ├── input.json
│   ├── query.sha256
│   ├── chem_agent.log
│   ├── case_summary.json
│   └── blackbox/<campaign-id>/...
└── evaluation/
    ├── workstation_schema_audit.json
    ├── workstation_schema_llm_review.json
    ├── workstation_schema_verdict.json
    ├── <case>/evaluation.md
    ├── <case>/evaluation.json
    ├── overall_report.md
    └── verdict_matrix.json
```

审计器会发现并检查黑盒 campaign 暴露出的所有 `device_package.json`，而不是假定每题只有一个固定 Device 输出。

每题最终给出四类离散结论，不使用数字评分：

1. `process_completion`；
2. `paper_quality_summary`；
3. `plan_workstation_match`；
4. `dispatch_schema_match`。

确定性 schema 审计与独立 LLM review 均通过，`dispatch_schema_match` 才能为 `yes`。
