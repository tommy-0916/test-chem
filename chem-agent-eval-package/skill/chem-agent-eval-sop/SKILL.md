---
name: chem-agent-eval-sop
description: Run the fixed eight-case Chem Agent evaluation suite concurrently from 测试题目.docx with online scholarly retrieval enabled, preserve every Query exactly, save all artifacts under result/, and produce discrete process, publication-quality, workstation-capability, and workstation-input-schema verdicts without numeric scoring. Use when evaluating, regression-testing, benchmarking, comparing, or reporting on the chem-agent repository.
---

# Chem Agent Evaluation SOP

Evaluate a user-specified Chem Agent repository without dispatching real device instructions.

## Fixed inputs and configuration

- Read all eight cases from the user-specified `测试题目.docx` on every run.
- Preserve each Query byte-for-byte after DOCX extraction. Do not summarize, translate, clean, or rewrite it.
- Default to `https://anyrouter.top/v1`, model `gpt-5.6-sol`, wire API `codex_responses`, and reasoning effort `xhigh`. Allow the user to override all four settings.
- Always enable `--online-literature`. Keep `--web-search` and `--download-pdfs` off in the default stability run; enable both only with `--deep-literature` when specifically evaluating open-Web/full-text acquisition.
- Always enable Research device context and Device `--full-workstations`.
- Run cases concurrently with four workers by default. This only parallelizes Research/Device Agent LLM planning requests; it never dispatches work to physical workstations.
- Isolate each case's result directory, knowledge base, logs, ledger, and state files so concurrent cases cannot overwrite one another.
- Name every local Research campaign and Device experiment as `<case>-<run timestamp>`, for example `A01-20260718-013500` and `A02-20260718-013500`.
- Allow up to 7200 seconds for each long LLM call. Record Research and Device elapsed time separately.
- Never use a real execution adapter or call a laboratory dispatch API.
- Use full LLM mode; never add `--disable-llm` to a formal evaluation.
- Read API keys from environment variables or the repository `.env`; never place keys in commands, reports, logs, Skill files, or Git-tracked files.

## Run the suite

1. Inspect the repository README and both CLI `--help` outputs before running. Adapt only if the CLI contract changed.
2. Create a timestamped directory under `<repo>/result/`.
3. Run:

```bash
python3 "$HOME/.codex/skills/chem-agent-eval-sop/scripts/run_suite.py" \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --model "gpt-5.6-sol" \
  --endpoint "https://anyrouter.top/v1" \
  --wire-api "codex_responses" \
  --reasoning-effort "xhigh" \
  --api-key-env "CHEM_AGENT_EVAL_API_KEYS" \
  --workers 4
```

4. Store the key in an environment variable or the repository `.env`, then pass only its variable name through `--api-key-env`. The value may be one key, a JSON array, or a comma-separated list. The runner never places the key value in commands or logs.
5. When `--api-key-env` is omitted, fall back to `CHEM_AGENT_EVAL_API_KEYS`, `REFINER_LLM_POOL_<N>_API_KEY`, and `REFINER_LLM_API_KEY` in that order.
6. Treat a nonzero case exit, missing state, empty Macro Plan, `manual_required`, `feasibility_error`, malformed JSON, or timeout as an evaluation result. Do not repair or replace Agent output. Skip Device mapping when Research produces no Macro Plan.
7. If Device returns malformed JSON or exits abnormally, retry the identical Device mapping once. Preserve both attempts and label the retry; never hide the first failure.
8. Do not rerun only the failed cases and present them as the same run. A formal evaluation run always contains all eight cases. A retry must use a new timestamped result directory.

## Required artifacts

For every case retain:

- exact `input.json` and `query.sha256`;
- `research_state.json` and `research_cli.log`;
- isolated `knowledge_base/` and paper registry;
- `device_state.json`, `device_package.json`, and `device_cli.log` when a Macro Plan exists;
- `case_summary.json`.

At run level retain `suite_manifest.json`, `raw_summary.json`, and the evaluation reports described below. If Device is retried, retain `device_*_attempt_1.*` and `device_*_attempt_2.*` in addition to the canonical successful/terminal files.

Operational notes:

- `codex_responses` is non-streaming in this repository; long quiet periods are not by themselves failures.
- Record 429/provider fallback events instead of silently removing them.
- Local `exp_id` values are evaluation identifiers, not real 303 laboratory task IDs.
- Deep-literature mode can materially increase runtime; report this configuration when comparing runs.

## Evaluate the result

Read [references/rubric.md](references/rubric.md) completely before evaluating. Read [references/report-format.md](references/report-format.md) before writing reports. Never calculate a numeric score.

Evaluate each case using exactly four outputs:

1. `process_completion`: `yes`, `partial`, or `no`.
2. `paper_quality`: list the publication facts for every adopted paper, including published/preprint/unverified status, journal or venue, top-journal/top-subjournal/regular/unknown classification, publication year, DOI, and a verified impact factor when available. Do not convert this into a score.
3. `plan_workstation_match`: `yes`, `no`, or `not_evaluable`.
4. `dispatch_schema_match`: `yes`, `no`, or `not_evaluable`.

Keep outputs 3 and 4 separate:

- `plan_workstation_match` asks whether the experimental operations can be mapped to the available workstation capabilities and legal open-parameter ranges.
- `dispatch_schema_match` asks whether the generated JSON field names, required fields, nesting, types, enums, units, and ranges exactly match each workstation Skill input contract.

Important distinction:

```text
Parameter not exposed by Skill -> fixed/default; do not require it and do not reject the device.
Parameter exposed and required by Skill -> Device output must include it exactly and pass validation.
```

Do not equate `feasibility_error` with real physical impossibility. Classify it as one of:

- confirmed unsupported operation;
- exposed parameter violation;
- container or transfer discontinuity;
- false rejection of a fixed/default parameter;
- insufficient Skill description;
- model mapping error.

Also inspect the actual scholarly queries. Flag contamination by automation, workstation, device-dispatch, or `303 实验室` terms unless those terms are chemically necessary.

## Write reports

Create:

- `<run>/evaluation/<case>/evaluation.md` and `evaluation.json` for all eight cases;
- `<run>/evaluation/overall_report.md`;
- `<run>/evaluation/verdict_matrix.json`.

Include evidence paths for every important judgment. Clearly separate original Query, Chem Agent output, and evaluator conclusions. Never silently correct the Agent's result.

## Completion gate

Do not declare the evaluation complete unless:

- all eight exact Queries were attempted;
- online literature is confirmed in each Research log; Web/PDF flags are checked only when `--deep-literature` was requested;
- the model, endpoint, and wire API are recorded without exposing keys;
- all available Device plans were checked against the workstation Skills;
- all four discrete evaluation outputs are present for every case;
- limitations, timeouts, retrieval failures, and missing artifacts are reported.
- findings are compared with `CHEMAGENT_TEST_SUITE_REPORT_20260715.md` when that historical report exists, using `fixed`, `still present`, or `new issue` labels.
