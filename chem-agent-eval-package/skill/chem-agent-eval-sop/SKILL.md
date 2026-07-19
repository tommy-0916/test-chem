---
name: chem-agent-eval-sop
description: Evaluate Chem Agent as one black-box system on the fixed eight-case suite. Preserve each Query exactly, force online scholarly retrieval, invoke only the repository's public campaign entrypoint, collect its unmodified campaign outputs, and then assess process completion, paper quality, plan-to-workstation feasibility, and workflow-to-Skill schema compliance with deterministic and independent LLM audits. Use for Chem Agent evaluation, regression testing, benchmarking, comparison, and reporting.
---

# Chem Agent Evaluation SOP

Treat the complete Chem Agent, including its Research/Device routing, feasibility feedback, observation handling, repair loops, and termination logic, as the system under test.

## Black-box boundary

Supply only these semantic inputs for each case:

```json
{
  "query": "the exact Query extracted from 测试题目.docx",
  "online_literature": true
}
```

Campaign IDs and output roots are harness instrumentation for isolation, not planning inputs.

Never:

- invoke `reaserch_agent/run_research_agent.py` directly;
- invoke `device_agent/run_from_research_state.py` directly;
- reproduce or replace Chem Agent's internal orchestration;
- force internal retrieval, workstation-selection, memory, retry, repair, or execution-adapter policies except `online_literature=true`;
- create mock observations or feed Device errors back into Research;
- repair, normalize, or replace Chem Agent output;
- dispatch real laboratory instructions.

Use the repository's documented top-level public entrypoint. For this repository, use `run_campaign.py`. Inspect its `--help` before a formal run and adapt only if the public contract changed.

When the black box exposes `AWAITING_OBSERVATION.md` and the test case provides no observation, record `awaiting_observation` as the external boundary state and stop the harness process. Do not invent an observation. Full multi-turn closed-loop evaluation requires an explicit observation fixture supplied as an additional external test input.

## Fixed suite

- Extract exactly `A01`, `A02`, `B01`, `B02`, `C01`, `C02`, `D01`, and `D02` from the user-specified `测试题目.docx` on every run.
- Preserve each Query exactly after DOCX extraction. Do not summarize, translate, clean, or rewrite it.
- Save `input.json` and `query.sha256` for every case.
- Always pass `--online-literature` through the public black-box entrypoint.
- Do not implicitly enable Web search or PDF download. Those are separate external test conditions and require explicit user input.
- Read model/provider credentials from the repository environment. Record configuration names and endpoints without recording secret values.
- Run up to four black-box cases concurrently by default. Keep each case's harness output and Chem Agent campaign directory separate.
- Never rerun only failed internal stages. A repeated case is a new black-box attempt in a new timestamped run directory.

## Run

```bash
python3 "$HOME/.codex/skills/chem-agent-eval-sop/scripts/run_suite.py" \
  --repo "/path/to/chem-agent" \
  --docx "/path/to/chem-agent/测试题目.docx" \
  --workers 4
```

The runner invokes only:

```bash
python run_campaign.py \
  --query "<exact Query>" \
  --campaign-id "<case>-<timestamp>" \
  --campaigns-root "<isolated harness directory>" \
  --online-literature
```

The final two arguments that identify the campaign and output root are instrumentation. Do not add internal Research/Device flags.

## Preserve black-box outputs

For each case retain:

- exact `input.json` and `query.sha256`;
- `chem_agent.log`, exit status, elapsed time, and boundary status;
- the complete unmodified Chem Agent campaign directory;
- `case_summary.json` containing an artifact inventory and paths to every emitted workflow package.

At run level retain `suite_manifest.json`, `raw_summary.json`, and all evaluation artifacts. Do not require a fixed internal file layout beyond the black box's exported campaign directory. Internal states may be read as evidence but must never be used to drive the run.

## Audit exposed outputs

Read [references/rubric.md](references/rubric.md) completely before evaluating. Read [references/report-format.md](references/report-format.md) before writing reports.

After all black-box cases reach an external boundary:

1. Discover every emitted `device_package.json` from each campaign output.
2. Run `scripts/audit_workflows.py` against every raw `workflow_json`.
3. Run `scripts/llm_review_workflows.py` against every generated workflow, using only the raw workflow, the recorded workstation truth source, used workstation Skills/audit rules, container/reagent plans, and formatting evidence.
4. Combine deterministic and LLM verdicts with an AND gate:

```text
all deterministic reviews=yes AND all LLM reviews=yes -> dispatch_schema_match=yes
any deterministic or LLM review=no -> dispatch_schema_match=no
no workflow -> not_evaluable
review failure for an emitted workflow -> evaluation incomplete
```

The audits are downstream consumers. They must not influence or repair the black-box run.

## Required case verdicts

Produce no numeric score. For every case output exactly:

1. `process_completion`: `yes`, `partial`, or `no` based on the public black-box invocation and external boundary state.
2. `paper_quality_summary`: `high`, `mixed`, or `low`, with publication facts for every adopted paper.
3. `plan_workstation_match`: `yes`, `no`, or `not_evaluable` for every plan/workflow exposed by the black box.
4. `dispatch_schema_match`: copy the combined verdict from `workstation_schema_verdict.json` without override.

Do not equate an internal `feasibility_error` with physical impossibility. Recheck the exposed result against the workstation truth source and classify it as a confirmed unsupported operation, exposed parameter violation, container/transfer discontinuity, false rejection of a fixed/default parameter, insufficient Skill description, or model mapping error.

## Reports and completion

Create:

- `<run>/evaluation/<case>/evaluation.md` and `evaluation.json`;
- `<run>/evaluation/overall_report.md`;
- `<run>/evaluation/verdict_matrix.json`;
- run-level and per-case deterministic audits, LLM reviews, and combined verdicts.

Do not declare the evaluation complete unless all eight exact Queries were attempted through the public black-box entrypoint, online literature was requested for every case, all black-box outputs and limitations were retained, every emitted workflow was covered by both schema reviewers, all four case verdicts are present, and historical findings are compared with `CHEMAGENT_TEST_SUITE_REPORT_20260715.md` when available.
