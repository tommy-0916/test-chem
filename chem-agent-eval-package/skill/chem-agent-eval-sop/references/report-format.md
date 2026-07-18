# Report format

## Per-case evaluation.md

Use this order:

1. Case ID, local run ID, exact original Query, and artifact inventory.
2. `process_completion: yes | partial | no`, with the stopping point and evidence.
3. Paper publication table and `paper_quality_summary: high | mixed | low`.
4. `plan_workstation_match: yes | no | not_evaluable`, with exact supporting or blocking operations.
5. `dispatch_schema_match: yes | no | not_evaluable`, with field-level Skill comparisons.
6. Hard facts and evidence paths.

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
- retrieval-query pollution;
- run limitations and provider failures;
- regression comparison with `CHEMAGENT_TEST_SUITE_REPORT_20260715.md` using `fixed`, `still present`, or `new issue` labels.

Write the machine-readable matrix to `evaluation/verdict_matrix.json`. Do not create a scorecard.

Keep original Chem Agent output and evaluator conclusions explicitly separated.
