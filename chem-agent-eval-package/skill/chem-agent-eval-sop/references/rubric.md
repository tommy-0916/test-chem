# Discrete evaluation rules

Do not assign numeric scores, percentages, rankings, or weighted totals. Produce the four required outputs below for every case.

## 1. Process completion

Output exactly one value:

- `yes`: the documented Chem Agent public entrypoint accepted the exact Query, online literature was requested, and the black box produced a readable external boundary state such as `goal_reached`, `awaiting_observation`, `manual_required`, `feasibility_deadlock`, or another documented terminal campaign status with preserved artifacts.
- `partial`: the black-box process crashed or timed out after producing some usable campaign artifacts, but no documented external boundary state could be established.
- `no`: the public entrypoint could not be invoked, no readable black-box output was produced, the Query was modified, or online literature was not requested.

Report the public entrypoint, return code, boundary status, stop reason, timeout, elapsed time, artifact inventory, and missing outputs as supporting facts. Internal Research/Device statuses may be reported as evidence but do not define process completion by themselves.

## 2. Paper quality

Do not score papers. For every paper actually adopted by Research, report:

```text
title
relevance: relevant | partially_relevant | irrelevant
publication_status: published | preprint | unpublished | unverified
journal_or_venue
venue_level: top_journal | top_subjournal | regular | unknown
publication_year
doi_or_identifier
impact_factor: verified numeric value with year/source | not_verified
full_text_status
protocol_support: yes | partial | no
```

Rules:

- Determine relevance from the exact material, reaction, intervention variable, required observations, and controls—not generic keyword overlap.
- Mark `published` only when a formal journal/conference identity is verified.
- Mark arXiv-only or equivalent manuscripts as `preprint`.
- Use `unverified` when publication identity cannot be confirmed.
- Do not guess an impact factor. Include the metric year and source when verified; otherwise write `not_verified`.
- Classify a venue as `top_journal` or `top_subjournal` only with explicit venue evidence. Otherwise use `regular` or `unknown`.
- Report metadata-only and abstract-only evidence honestly.
- List actual scholarly queries and flag pollution from automation, workstation, device dispatch, or `303 实验室` terms.

At case level, summarize paper quality as `high`, `mixed`, or `low`:

- `high`: adopted evidence is directly relevant and predominantly verified formal publications with usable methods.
- `mixed`: relevant evidence exists but includes metadata-only, indirect, preprint, weak-venue, or unverified items.
- `low`: no adopted papers, predominantly irrelevant papers, or publication identities cannot be verified.

## 3. Experimental plan to workstation capability match

Output exactly one value:

- `yes`: every required experimental operation exposed anywhere in the black-box campaign has a corresponding workstation capability; all Agent-changeable parameter values are within the Skill's legal ranges; container/sample-state transitions have a supported declared route.
- `no`: at least one necessary operation has no workstation, an open parameter is outside the allowed range, or a required container/sample transfer is unsupported.
- `not_evaluable`: the black box exposed no plan/workflow or there is insufficient output/Skill evidence to perform the check.

Important:

- A parameter not exposed by the Skill is fixed/default and is not a reason for `no`.
- An internal `feasibility_error` is not automatically `no`. Recheck the claimed blocker against the recorded workstation truth source.
- State the exact Macro Action, workstation Skill, operation, and constraint behind every `no`.

## 4. Generated format and parameters to workstation input match

Use both `scripts/audit_workflows.py` and `scripts/llm_review_workflows.py` on every workflow emitted by the black-box campaign. The tested repository's own `dispatch_validation`, self-check, formatter success, or package `status` cannot establish this verdict.

Output exactly one value:

- `yes`: every generated Device Step in every exposed workflow exactly follows the selected workstation operation's Skill input contract.
- `no`: any generated step has a nonexistent workstation/operation, misses a Skill-open required field, invents an unsupported field, uses incorrect nesting/type/enum/unit/range, or violates a cross-field constraint.
- `not_evaluable`: no Device workflow was generated.

Copy the final case-level verdict from `<run>/evaluation/<case>/workstation_schema_verdict.json` without manual override. A case passes only when every exposed workflow passes. A later formatter repair does not convert a nonconforming raw `workflow_json` into `yes`.

Combine the reviews using an AND gate:

- deterministic `yes` + LLM `yes` -> `yes`;
- either review `no` -> `no`;
- no workflow -> `not_evaluable`;
- generated workflow with failed, malformed, or incomplete LLM review -> evaluation incomplete.

The independent LLM must review the raw workflow against the root workstation rules, the full Skills for the used workstations, their available audit rules, container/reagent plans, and dispatch-formatting evidence. It must cover every step and return structured findings with evidence quotes. It may add errors but may never remove deterministic errors.

Check mechanically for every step:

- workstation name exists;
- operation belongs to that workstation;
- workstation `id` exactly matches the selected Skill;
- all open required parameters exist;
- parameter names and nesting match exactly;
- types, enums, units, and ranges are valid;
- container counts match container-number arrays;
- carrier counts match carrier-number arrays;
- Skill-declared `file` inputs exist locally or are recorded as unverified remote references;
- reagent/container plans agree with the workflow steps;
- source Macro Action/step is traceable.
- final dispatch formatting has zero unmapped steps, zero dropped parameters, and no formatting failure.

Do not validate names against a station-wide parameter union. A parameter is legal only when it appears under the selected operation at the correct hierarchy. Rows prefixed by `-` and `--` in a workstation Skill define nested JSON structure. Dynamic fields such as `N号原液瓶` must be instantiated at the declared level and must retain the declared type.

Treat each of the following as `no`:

- missing or wrong workstation id;
- primitive lid-number arrays when the Skill requires object arrays;
- numeric JSON values where the Skill declares `string`;
- a placeholder array where the Skill declares a dynamic object;
- absent required upload files;
- an operation known elsewhere but not on the selected workstation;
- formatter warnings that say a step was not mapped or a parameter was omitted.

Example:

```text
Error: 物料拿取 only contains 容器类型 and 容器编号.
Skill input: 容器类型, 容器数量, and 容器编号 are required.
Verdict: dispatch_schema_match = no.
```

## Hard facts

Report these independently of the four values: Query modification, no online retrieval attempt, fabricated paper identity, unsafe chemistry, real dispatch attempt, nonexistent workstation/operation, or missing Skill-open required input.
