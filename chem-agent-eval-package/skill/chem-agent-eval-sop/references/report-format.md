# Report format

## Per-case evaluation.md

Use this order:

1. Case ID, local run ID, exact original Query, black-box public entrypoint, and artifact inventory.
2. `process_completion: yes | partial | no`, with the external boundary state, stop reason, return code, elapsed time, and evidence.
3. Paper publication table and `paper_quality_summary: high | mixed | low`.
4. `plan_workstation_match: yes | no | not_evaluable`, with exact supporting or blocking operations.
5. `dispatch_schema_match: yes | no | not_evaluable`, with field-level Skill comparisons.
6. Hard facts and evidence paths.

For section 5, read the deterministic audit, independent LLM review, and combined verdict for every workflow emitted by the black-box campaign. Reproduce `<run>/evaluation/<case>/workstation_schema_verdict.json` exactly. Summarize deterministic and LLM-only errors separately by workflow path, workstation, operation, parameter path, and affected steps. Do not replace either independent review with the Chem Agent's own validation result.

Do not include numeric scores, percentages, total scores, or weighted rankings.

For every mismatch use:

```text
Error: <actual Chem Agent output>
Workstation Skill/ground truth: <required capability or input contract>
Correct: <expected operation, format, or parameter structure>
Impact: <planning capability or Device JSON impact>
```

## evaluation.json

```json
{
  "case_id": "A01",
  "local_run_id": "A01-20260718-013500",
  "query_sha256": "...",
  "blackbox_entrypoint": ".../run_campaign.py",
  "boundary_status": "goal_reached|awaiting_observation|manual_required|...",
  "stop_reason": "...",
  "process_completion": "yes|partial|no",
  "paper_quality_summary": "high|mixed|low",
  "papers": [
    {
      "title": "...",
      "relevance": "relevant|partially_relevant|irrelevant",
      "publication_status": "published|preprint|unpublished|unverified",
      "journal_or_venue": "...",
      "venue_level": "top_journal|top_subjournal|regular|unknown",
      "publication_year": "...",
      "doi_or_identifier": "...",
      "impact_factor": "<value + year + source>|not_verified",
      "full_text_status": "...",
      "protocol_support": "yes|partial|no"
    }
  ],
  "plan_workstation_match": "yes|no|not_evaluable",
  "dispatch_schema_match": "yes|no|not_evaluable",
  "workstation_schema_audit": {
    "verdict": "yes|no|not_evaluable",
    "checked_workflows": 0,
    "checked_steps": 0,
    "error_count": 0,
    "counts_by_code": {},
    "evidence_path": "evaluation/<case>/workstation_schema_audit.json"
  },
  "workstation_schema_llm_review": {
    "review_status": "completed|failed",
    "verdict": "yes|no|not_evaluable",
    "checked_workflows": 0,
    "checked_steps": 0,
    "error_count": 0,
    "model": "...",
    "evidence_path": "evaluation/<case>/workstation_schema_llm_review.json"
  },
  "workstation_schema_combined": {
    "deterministic_verdict": "yes|no|not_evaluable",
    "llm_verdict": "yes|no|not_evaluable",
    "verdict": "yes|no|not_evaluable",
    "evaluation_complete": true,
    "evidence_path": "evaluation/<case>/workstation_schema_verdict.json"
  },
  "findings": [],
  "hard_facts": [],
  "evidence_paths": []
}
```

## Overall report

Include one eight-case verdict table:

| Case | Process | Paper quality | Plan↔workstation | Format↔Skill input |
|---|---|---|---|---|
| A01 | yes/partial/no | high/mixed/low | yes/no/N/A | yes/no/N/A |

Then report:

- papers grouped by published/preprint/unverified and venue level;
- recurring workstation capability mismatches;
- recurring Device input-schema mismatches;
- formatter-unmapped steps, dropped parameters, and missing workstation input files;
- retrieval-query pollution;
- run limitations and provider failures;
- regression comparison with `CHEMAGENT_TEST_SUITE_REPORT_20260715.md` using `fixed`, `still present`, or `new issue` labels.

Write the machine-readable matrix to `evaluation/verdict_matrix.json`. Do not create a scorecard.

The overall report must link `evaluation/workstation_schema_audit.json`, `evaluation/workstation_schema_llm_review.json`, and `evaluation/workstation_schema_verdict.json`. State the workstation count, truth-source path, reviewer model/endpoint, review coverage, and any failed attempts. If a report verdict differs from the combined verdict, or any generated workflow lacks a completed LLM review, the evaluation is incomplete.

Keep original Chem Agent output and evaluator conclusions explicitly separated.
