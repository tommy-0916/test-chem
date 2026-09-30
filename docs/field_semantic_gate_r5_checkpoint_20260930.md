# Field Semantic Gate — Round-5 Checkpoint (2026-09-30)

> **Addendum (same day): applicability gate made competing-independent.**
> The definition-site fallback now checks its applicability — single-reagent
> aqueous dissolution, judged from the parsed recipe itself — *before* the
> competing-entity loop, and returns unresolved outside that scope
> unconditionally. Previously the multi-solute / non-aqueous rejection only
> fired when a caller happened to list the competing entities; an omitted
> list could have changed the local result. The acceptance matrix is now:
> multi-solute and non-water-solvent passages reject with a full competing
> list **and** with an empty one; the single-solute aqueous positive binds
> with either. The Solution B 1 M binding, the register resolution (9/0) and
> the seven-fact triage below are unchanged under the stricter gate.

Round 5 corrects one round-4 error and otherwise carries the round-4
results forward. The round-3 proposal bytes are still not rewritten; arms,
split cardinality, quantities and the pH labeled quote remain untouched.

## Correction: the concentration release premise from ca18e31 is revoked

`ca18e31` added `_recipe_reproduces_concentration()`, which divided
amount-of-substance by the **water volume** and treated a matching quotient
as proof that the trailing parenthetical belongs to the prepared solution.
That premise is unsound and is removed:

- Concentration is defined against the **solution** volume
  (c = n_B / V_solution, IUPAC). Input amounts plus solvent volume never
  prove the final concentration; the aqueous case has no exception.
- Equal quotients across components do not prove which component a
  parenthetical describes; the multi-solute acceptance written into the
  round-4 tests is deleted with the branch.

What replaces it — source relation only. In a preparation definition
sentence, the trailing parenthetical describes the prepared solution (the
grammatical subject). A mention of the single quantified reagent or of the
water inside such a recipe is an **ingredient role**, not a competing
concentration bearer: the reagent's amount is already stated in its own
quantity, and pure water carries no solute concentration. The parser's
existing operand grammar (`_quantified_dissolution_operands`) supplies the
roles; no arithmetic participates in the release decision, and computed
concentrations would require a separately verified solution volume and a
derived-source label either way.

Blocking behavior, unchanged and still tested: any other co-occurring
source-bound entity (another solution, a non-water solvent/carrier/stock)
can itself bear the reported concentration and keeps the passage
unresolved; multi-solute recipes stay unresolved (equal quotients or not);
stock inputs, coordination, negation and multiple events stay unresolved.

For the paper sentence "Solution B is prepared by dissolving 100 mmol NaOH
in 100 mL of water (1 M)" the 1 M fact now binds with
`rule_id = source-labels/v1:definition_site_concentration`,
`source_surface = "Solution B"` — proven by definition-sentence subject and
ingredient roles, not by the numeric coincidence 100 mmol / 100 mL.

## Replay result (local-revision-r5-replay.json)

- `concentration_fact.binding` present (rule and surface above);
  `fact_quantity_attribution_unresolved` absent from diagnostics.
- `resolution_status`: 9 resolved, 0 source_missing (register naming binding
  for deionized water kept from round 4).
- diagnostics: `semantic_binding_pending: 7` only, triaged per fact exactly
  as in round 4 — 3 `review_pending_canonical_state` (precipitate:
  original-word localization source-supported at b67/b72, canonical mapping
  is Q4) and 4 `review_pending_source_relation` (suspension: no
  post-change source passage uses the word; b69 predates the state change;
  Q1/Q4).
- `admitted_protocols: 0`; G2 not entered; empty protocol list reported as
  empty, not as a pass. A01 remains open.

## Regression

Focused route suites (191 tests) green; full
`python -m unittest discover -s reaserch_agent -p "test*.py"` run after the
correction — failure/error IDs byte-identical to the baseline set (the same
29; zero new). The round-4 tests that asserted arithmetic-based release
were rewritten to assert the role-based rule, including the multi-solute
negative and the non-water-solvent negative; the pre-existing regression
tests that encode competing-entity blocking remain green unchanged.

## Still open

- Q1–Q4 with the independent chemistry reviewer. The review input is the
  round-4/round-5 triage: three precipitate facts need a canonical state
  under per-step conditions (phase, collection, retention) — no global
  alias; four suspension facts need post-change source support, not the
  pre-centrifugation b69 state. Split-downstream binding stays withdrawn:
  no parent re-consumption, no child selection, no default merge.
- 1 M is resolved by source relation; nothing else in this round claims
  computed concentrations.
