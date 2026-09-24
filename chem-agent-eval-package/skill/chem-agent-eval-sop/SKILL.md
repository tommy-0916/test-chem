---
name: chem-agent-eval-sop
description: Run and evaluate Chem Agent as one black-box system on the fixed A01-D02 eight-case DOCX suite. Preserve every Query exactly, require real LLM responses and online scholarly retrieval, invoke only run_campaign.py, retain raw campaign artifacts, audit every Device workflow deterministically and with an independent LLM, assess paper quality and plan feasibility, verify workstation-ready parameters, compare a historical baseline, and refuse completion when evidence is missing. Use for Chem Agent evaluation, regression testing, benchmarking, acceptance testing, or workstation dispatch-readiness review.
---

# Chem Agent Black-Box Evaluation

Treat the complete Research/Device campaign as the system under test. Keep evaluator conclusions downstream from unmodified Chem Agent outputs.

## Resolve inputs

Require:

- Python 3.10 or newer, preferably the target repository's `.venv/bin/python`;
- a Chem Agent repository containing `run_campaign.py`, `reaserch_agent/`, `device_agent/`, and `chem_resources/`;
- a DOCX containing exactly `A01`, `A02`, `B01`, `B02`, `C01`, `C02`, `D01`, and `D02`;
- real provider configuration in the repository `.env` or process environment;
- the current workstation truth source under `chem_resources/lab-design-all/...` (`lab-design-main` is legacy fallback only).

Use the repository's `测试题目.docx` unless the user names another DOCX. Never rewrite, translate, normalize, or summarize a Query.

Read [references/rubric.md](references/rubric.md) completely before assigning verdicts. Read [references/report-format.md](references/report-format.md) completely before writing reports.

## Protect credentials and systems

- Read keys from environment variables only. Never add a key to a command argument, script, report, manifest, or log.
- Record provider/model/endpoint names, never secret values.
- Never use `--disable-llm`, fake models, mock responses, or evaluator-generated observations in a formal run.
- Never dispatch real laboratory instructions. Keep the public campaign's default non-real execution boundary.
- Stop at `AWAITING_OBSERVATION.md` when the suite provides no observation fixture.

## Preflight

Locate this Skill directory and run:

```bash
python3 <skill-dir>/scripts/preflight.py \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --probe-api
```

Do not start a formal run unless preflight returns `ready: true`. The API probe must receive non-empty text from the configured real provider. A credential, endpoint, model, protocol, DOCX, public-CLI, or workstation failure is blocking.

## Run the eight cases

Invoke only the bundled runner:

```bash
python3 <skill-dir>/scripts/run_suite.py \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --workers 4
```

Use `--campaign-model`, `--campaign-endpoint`, `--campaign-wire-api`, and `--campaign-reasoning-effort` only to pin documented public provider settings when environment defaults are insufficient. Use the corresponding `--schema-review-*` options for the independent reviewer.

The runner must invoke the public entrypoint once per case with only these semantic inputs:

```text
query=<exact DOCX Query>
online_literature=true
```

Treat campaign IDs, output roots, and provider pinning as harness configuration. Do not pass internal Research/Device policy flags, enable Web search/PDF download without explicit user direction, invent observations, or rerun an internal stage. Repeat a failed case only as a fresh whole black-box attempt in a new timestamped directory.

## Preserve evidence

Retain for every case:

- `input.json` and `query.sha256`;
- `chem_agent.log`, return code, elapsed time, and boundary status;
- the complete unmodified campaign directory;
- `case_summary.json` and every emitted `device_package.json`.

Retain `suite_manifest.json`, `raw_summary.json`, provider limitations, and all evaluation artifacts at run level. Do not repair or normalize raw Chem Agent output.

## Audit Device outputs

The runner automatically executes:

1. `scripts/audit_workflows.py` against every raw `workflow_json`;
2. `scripts/llm_review_workflows.py` against the same workflows using workstation Skills, audit rules, container/reagent plans, and formatting evidence;
3. an AND merge into `workstation_schema_verdict.json`.

Use the merged verdict without manual override:

```text
deterministic=yes AND LLM=yes -> dispatch_schema_match=yes
either=no -> dispatch_schema_match=no
no workflow -> not_evaluable
review missing or failed -> evaluation incomplete
```

Do not use the Device Agent's self-check, package status, formatter success, or dispatch validator as evaluator evidence.

## Produce formal reports

For every case, inspect all Research states, adopted papers, plans, workflows, deterministic findings, LLM findings, and workstation truth. Write:

- `evaluation/<case>/evaluation.md`;
- `evaluation/<case>/evaluation.json`;
- `evaluation/overall_report.md`;
- `evaluation/verdict_matrix.json`;
- `evaluation/workstation_direct_acceptance.md`.

Assign exactly four discrete case verdicts:

1. `process_completion`: `yes`, `partial`, or `no`;
2. `paper_quality_summary`: `high`, `mixed`, or `low`;
3. `plan_workstation_match`: `yes`, `no`, or `not_evaluable`;
4. `dispatch_schema_match`: copy the combined schema verdict exactly.

Verify every adopted paper's publication identity and relevance. Do not guess impact factors. Recheck every internal feasibility error against workstation truth; never equate an Agent rejection with physical impossibility.

Compare findings with `CHEMAGENT_TEST_SUITE_REPORT_20260715.md` when it exists and label regressions `fixed`, `still present`, or `new issue`.

## Validate completion

Run the strict validator after writing reports:

```bash
python3 <skill-dir>/scripts/validate_evaluation.py \
  --run "/path/to/chem-agent/result/chem-agent-eval-YYYYMMDD-HHMMSS" \
  --baseline "/path/to/chem-agent/CHEMAGENT_TEST_SUITE_REPORT_20260715.md"
```

Omit `--baseline` only when no baseline exists. Never use `--allow-recorded-llm-failures` for a formal evaluation.

Declare completion only when `evaluation/completion_check.json` contains `complete: true`. Any modified Query, absent online-literature request, missing case, recorded failed/empty/fallback LLM call, uncovered workflow, incomplete LLM review, missing report, mismatched dispatch verdict, absent workstation direct-acceptance report, or missing baseline comparison keeps the evaluation incomplete.

Report blockers with evidence. Do not convert incomplete evidence into a passing verdict.
