# Discrete evaluation rules

Do not assign numeric scores, percentages, rankings, or weighted totals. Produce the four required outputs below for every case.

## 1. Process completion

Output exactly one value:

- `yes`: Research produced a valid state and non-empty Macro Plan, and Device produced a valid terminal package with `success` or `feasibility_error`.
- `partial`: Research produced usable artifacts, but the chain stopped before a valid Device terminal package because of `manual_required`, empty Macro Plan, timeout, malformed JSON, or Device process failure.
- `no`: Research did not produce a readable state, the Query was modified, online literature was not attempted, or the run failed before producing a usable Research result.

Report the Research status, Device status, attempts, timeouts, elapsed time, and missing artifacts as supporting facts.

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

- `yes`: every required experimental operation has a corresponding workstation capability; all Agent-changeable parameter values are within the Skill's legal ranges; container/sample-state transitions have a supported route.
- `no`: at least one necessary operation has no workstation, an open parameter is outside the allowed range, or a required container/sample transfer is unsupported.
- `not_evaluable`: Research produced no Macro Plan or there is insufficient Device/Skill evidence to perform the check.

Important:

- A parameter not exposed by the Skill is fixed/default and is not a reason for `no`.
- A `feasibility_error` is not automatically `no`. Recheck the claimed blocker against all 45 workstation Skills.
- State the exact Macro Action, workstation Skill, operation, and constraint behind every `no`.

## 4. Generated format and parameters to workstation input match

Output exactly one value:

- `yes`: every generated Device Step exactly follows the selected workstation operation's Skill input contract.
- `no`: any generated step has a nonexistent workstation/operation, misses a Skill-open required field, invents an unsupported field, uses incorrect nesting/type/enum/unit/range, or violates a cross-field constraint.
- `not_evaluable`: no Device workflow was generated.

Check mechanically for every step:

- workstation name exists;
- operation belongs to that workstation;
- all open required parameters exist;
- parameter names and nesting match exactly;
- types, enums, units, and ranges are valid;
- container counts match container-number arrays;
- reagent/container plans agree with the workflow steps;
- source Macro Action/step is traceable.

Example:

```text
Error: 物料拿取 only contains 容器类型 and 容器编号.
Skill input: 容器类型, 容器数量, and 容器编号 are required.
Verdict: dispatch_schema_match = no.
```

## Hard facts

Report these independently of the four values: Query modification, no online retrieval attempt, fabricated paper identity, unsafe chemistry, real dispatch attempt, nonexistent workstation/operation, or missing Skill-open required input.
