# Field Semantic Gate — Round-4 Checkpoint (2026-09-30)

> **Corrected in round 5** (`docs/field_semantic_gate_r5_checkpoint_20260930.md`):
> the arithmetic release premise introduced here — solvent volume standing
> in for solution volume, and equal n/V quotients dissolving component
> ambiguity — is revoked. The 1 M binding is re-established on source
> relation only. Keep this document for the register and triage records,
> which round 5 carries over unchanged.

Round 4 replays the unchanged round-3 proposal (`local-revision-r3-proposal.json`)
through the same strict gates after two bounded changes and one triage
deliverable. No structural rewrite; arms, split cardinality, quantities and
the pH labeled quote are untouched.

## What changed

1. **deionized water — register naming binding completed.**
   `candidate-supply-specs.json` gains `spec_water_milliq_deionized`
   (`material_name: "deionized water"`, same CAS 7732-18-5, same Milli-Q
   paper identity at b51-b52). The quality evidence was already sufficient —
   the gap was purely the register name key, so per the review directive this
   was completed through the existing registration flow, not routed to
   chemistry review. The resolver re-verifies the register digest on every
   replay. Q5 in `split-downstream-review-questions.json` is marked
   `resolved_by_register_amendment`.

2. **1 M concentration — attribution capability completed.**
   `definition_site_concentration_binding`
   (`chem_agent_contracts/route_source_labels.py`) gains one bounded branch:
   when a competing source-bound entity appears in the definition sentence,
   the binding still fails — unless the recipe is an **aqueous** dissolution
   whose own quantified operands reproduce the trailing parenthetical. Then
   "the product is 1 M" and "the solute in this product is 1 M" are the same
   claim and the ambiguity collapses. The aqueous guard matters: for a
   non-water solvent/carrier (e.g. "2 mmol salt in 2 mL Carrier Z (1 M)")
   the parenthetical can describe the carrier itself, so those stay
   unresolved. No case-matching rules, no sentence context removed, no NLP
   resolver. For the paper sentence "Solution B is prepared by dissolving
   100 mmol NaOH in 100 mL of water (1 M)" the branch binds; the
   `fact_quantity_attribution_unresolved` diagnostic is gone.

3. **Seven pending state facts — per-fact triage, machine-readable.**
   `local-revision-r4.py` writes `pending_item_triage` into the replay JSON:
   each of the 7 `semantic_binding_pending` items carries its live gate
   outcomes (`controlled_state_mapping`, `state_attribution_outcome`,
   literal presence), a category, the missing premise, and the review
   question it blocks on. The three reviewer-mandated outcomes are now
   separated instead of one bucket:
   - `review_pending_canonical_state` (3 facts: graph[5].out, graph[6].in,
     graph[8].out — value `precipitate`): original-word/object localization
     is source-supported (b67 singular, b72 plural), but no reviewed
     controlled-state mapping for "precipitate" exists in
     material-states/v1. Blocked on Q4.
   - `review_pending_source_relation` (4 facts: graph[6].out, graph[7].in,
     graph[7].out, graph[8].in — value `suspension`): the quoted passages
     never use the state word; the only source occurrence (b69) predates the
     centrifugation state change. Blocked on Q1/Q4.
   - No item landed in "pass after binding" or "minimal mechanism fix" this
     round; the concentration item was the mechanism-gap case and is fixed
     under (2).

## Replay result (local-revision-r4-replay.json)

- `resolution_status`: 9 resolved, 0 source_missing (was 8/1).
- diagnostics: `semantic_binding_pending: 7` only — `required_graph_fact_missing`
  and `fact_quantity_attribution_unresolved` are cleared; the pH
  dimensionless diagnostic stays absent (regression guard holds).
- `admitted_protocols: 0`; G2 not entered. Empty protocol list is reported
  as empty, not as a pass. A01 remains open, gated on Q1–Q4 review answers
  and the seven triaged state facts.

## Regression

Full `python -m unittest discover -s reaserch_agent -p "test*.py"` run
after the change; failure/error IDs match the pre-change baseline (the same
29). Two regression tests that encoded the old always-unresolved behavior
for quantified competing entities were updated with the aqueous-reproduction
rule, and new tests pin both the positive (Solution B) and negative
(Carrier Z, distinct quotients) branches.

## Still open

- Q1–Q4 with the independent chemistry reviewer (split downstream binding,
  per-part vs total 30 mL, final merge, canonical states for
  precipitate/suspension). The executable binding from split children to
  step 5 remains withdrawn: no parent re-consumption, no child selection,
  no merge.
- The 7 state facts, tracked per-fact in `pending_item_triage` with their
  missing premises.
