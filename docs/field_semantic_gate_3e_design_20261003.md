# Round 3E feasibility-study design — operation-precondition inference (diagnostics only)

Status: chartered by owner decision 2026-10-03. First stage is limited to a feasibility
study and diagnostics. It is **not** approved to close `ms7a.out`. The prototype
implementation waits for the Round-3D sealing acceptance. This document fixes the paper
design; no code is written in this stage.

## Review bases (owner, 2026-10-03)

1. **SI = D stands.** Its precise meaning is "the three targeted evidence classes were not
   detected": the SI neither supplies the retained phase nor provides positive basis for
   reverse inference. (See `si-search/SI_SEARCH_MEMO.md` in the workspace.)
2. **Forward rules cannot be inverted.** REDISPERSION_V1 accepts two input states
   (`chem_resources/chemistry_conventions/conventions.json` L72, `allowed_input_states`).
   The whitelist defines rule applicability and may only be recorded as
   `rule_compatible_states`. It cannot prove that whatever the paper calls redispersion
   actually had an input in that set, still less that the input was uniquely
   `retained_wet_solid`.
3. **E10 enters the counter-example checks.** The SI shows no precipitate after NiFe E10's
   acid etching (S5, Figure S1 caption) while the Etching methods section still uniformly
   writes the follow-up washing protocol. This neither refutes the Control group's
   solid-phase assumption nor determines the post-wash state, but it requires any new
   inference to state experimental group, time point, and material continuity explicitly.

## The three propositions — each must be answered separately

| subject | the question it must answer alone |
|---|---|
| necessary input condition | What independent basis proves the condition is necessary? The forward whitelist may only be recorded as `rule_compatible_states`. |
| this material flow | Does the downstream input really come from THIS upstream output? |
| material instance binding | Does the evidence point to the instance under proof — not other materials, invocations, or branches? |

An operation name, or an edge the proposal drew itself, cannot answer any of the three.

## Standing boundaries

- **No circular evidence-taking.** `ms7b.in` already depends on `ms7a.out`; the downstream
  state may never be assumed in order to prove the upstream. Every extra assumption must
  be recorded explicitly.
- **First version delivers diagnostics only**: candidate states, unproven premises,
  alternative explanations, counter-example results. `diagnostics_only=true` and
  `feeds_verdict=false` are fixed; no consumable token is minted; `protocol-definition/v1`
  is not expanded; A01 is not presupposed to PASS; source identity and inference nature
  are recorded separately.

## Diagnostic record schema (paper v1)

1. source and figure-snapshot digest;
2. experimental group / stage / invocation;
3. per-proposition evidence and assumptions (three propositions, separately);
4. alternative explanations;
5. dependency relations;
6. open items;
7. source identity (`paper_explicit` / `supplement_explicit` / `external_primary`) —
   recorded separately from the inference nature.

## Minimal test set

real Control; whitelist inversion; two compatible states; instance/branch swap;
first/second invocation swap; cross-group/cross-stage E10; circular dependency;
source mutation.

## Calibration expectation

With the current materials, the Control group must be allowed to conclude
`insufficient`; a model standing on assumptions alone may output at most
`conditional_constraint`.

## Gate to a formal proof class

Necessary conditions carry independent basis; invocation and material binding hold;
alternative explanations are excluded; counter-example tests pass. Otherwise the blocker
stays BLOCKED — and the research still counts as complete.

## Completion standard for the feasibility stage

One A01 diagnosis + the necessity-rule basis + counter-example results. The conclusion
may remain BLOCKED. No implementation begins until the Round-3D sealing acceptance
(field-deletion attack, unverified minting, and diagnostic-artifact rejection) passes.
