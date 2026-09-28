# Clause quote tightening checkpoint (2026-09-28, WIP)

Baseline: `e1962a3`. This slice converges generic source-evidence span handling for unsigned PDF proposals. It adds no route, material-graph, or split-relation features, does not loosen the binder budget, changes no fact value, and approves no chemistry.

**Status of this checkpoint (WIP, not a fully verified stable baseline):**

| Item | Status |
| --- | --- |
| Saved-input span repair (the six `fact_excerpt_span_too_long` facts) | Verified on saved replays |
| Trim qualifier fidelity (trim must not change what the source says) | Bounded guard implemented with counterexamples; it is a lexical guard, not a semantic proof — independent review still adjudicates meaning |
| Verification-context provenance and cross-layer carry-through | Closed: the full context must bind to the consuming group (its own explicit budget, never the display budget) and contain the located excerpt before any semantic check uses it; formal evidence (bundle/provenance/matrix) carries the same full context, and rebuild verification re-binds it and anchors the located locator inside its span |
| Research cause-level failure fingerprint vs baseline | Proven by same-environment e1962a3-vs-patch comparison (0 new / 0 removed / 0 changed after tempfile normalization; one run-varying timing threshold recorded separately). The legacy historical-baseline comparison remains encoding-impaired and is kept only as supporting evidence |
| Fresh live generation / complete local A01 | Not completed this round: live model credential rejected (HTTP 401 before any proposal), and the semantic gate still blocks (11 items, triaged below) |

## Why the six span failures were one problem

The six `fact_excerpt_span_too_long` failures in the second real NiFe Control replay all quoted the same one-sentence excerpt, which the registered PDF splits across `pdf:p2:b68-p2:b71`. The binder returns this reason only after the quote was found unique, so these were verbose quotations of one source event — not missing or ambiguous evidence. They merge by source span, event, and field role into one general case.

## Mechanism: location/display trim, full context for verification

`route_pdf_clause_quote_tightening.tighten_unreviewed_clause_quotes` handles any fact, on any path, in any group:

- Only an excerpt that binds uniquely in the proposal's own group and fails with exactly `fact_excerpt_span_too_long` is a candidate. Missing or ambiguous originals keep their existing diagnosis; short quotes are untouched.
- The located excerpt becomes the **local clause** containing the fact's literal value, using the same `_LOCAL_CLAUSE` unit the attribution checks admit. Qualifiers inside the kept clause (approximation, ordering) are never cut.
- The trim is for **location and display only**. The fact keeps the full original quotation as `verification_excerpt`, and the semantic gates (`route_group_compiler._fact_issue`, `route_group_fact_receipt._literal_fact_reason`, and the local-diagnostics/repair path built on it) read the full context for value presence, quantity attribution, state attribution, and role checks. Location-side consumers (locator, strict association, receipt locator match, PDF source verification) continue to bind the short located excerpt.
- **Refusal guards** (the fact stays pending with its original quotation, and an issue is recorded):
  - a negation or contrast marker between the sentence start and the value (`clause_subquote_drops_qualifying_context`);
  - the deleted context (anything outside the kept clause) contains a negation, contrast, retraction, or limitation marker — e.g. `ruled out`, `rather than`, `estimate`, `although` (`clause_subquote_drops_qualifying_context`). This covers later-clause retractions and after-the-value qualifiers that a value-position scan alone misses;
  - more than one value-bearing clause binds uniquely (`clause_subquote_ambiguous_in_group`);
  - no value-bearing clause fits the budget (`clause_subquote_not_uniquely_bound`) — a necessary clause split across too many blocks stays pending instead of losing context.
- These guards are a bounded lexical check. They can refuse safe trims; they cannot certify semantics. The expected outcomes of the counterexample tests are fixed by full-sentence meaning, not computed by the same regular expressions the mechanism uses.
- Values, units, material identity, graph structure, and the raw model response are never modified. Every rewrite records `original_excerpt`, `produced_excerpt`, `produced_locator`, and scope under `clause_quote_tightening` with version `route_pdf_clause_quote_tightening/v1`.

The same treatment applies to the pre-existing event-anchored operation tightening (`route_pdf_operation_quote_tightening`): it also preserves `verification_excerpt` and refuses to trim when the deleted context carries a qualifying marker (`operation_subquote_drops_limiting_context`).

## Verification-context provenance and cross-layer carry-through

Filling `verification_excerpt` grants no verification authority. `route_pdf_verification_context.verification_context_reason` re-establishes the source binding at every consumption boundary, against the consuming group's own blocks:

1. the full context must occur uniquely in that exact PDF group, under its own explicit resource budget (`MAX_VERIFICATION_CONTEXT_BLOCKS`, separate from the three-block display budget, so a legitimate long context is not re-rejected; over-budget contexts are refused explicitly);
2. the located excerpt must be literally contained in the full context.

A model proposal, a saved replay, or any other entry point carrying a forged, foreign, or non-containing context is blocked: strict association rejects the proposal (`proposal_verification_context_*`), the receipt predicate returns `verification_context_*` before any value, quantity, or state check, and the compiler re-checks containment without blocks. Save and rebuild re-run the binding: the PDF source verifier re-binds the carried evidence text with the context budget and requires the stored field locator — the located anchor — to lie inside the verified context span, so post-compile science audit, independent review, and V2 reconstruction all see the same text the semantic gates used, and a modified or downgraded context is rejected by digest and re-binding rather than silently accepted as the short excerpt.

Formal evidence carries that same context: the compiler's evidence bundle, field provenance, and evidence matrix project the full verification text (with its digest) whenever a bounded trim shortened the located excerpt; untrimmed facts are unchanged. `verification_excerpt` is an allowed fact key in strict association, the fact receipt, and local repair. The binder budget is unchanged; a quote within budget binds identically under any chunking of the same text (tested), and over-budget remains a distinct, honestly reported reason.

## Saved replay outcome

Offline replay of the saved second real proposal: four operation tightenings plus six clause tightenings, zero blocked field records, zero locator diagnostics. Field accounting: 55 original facts — 54 located, and the split-count fact `f055` consumed by design (it agreed with the source operation and the unitless count fact, so child cardinality carries the count); 16 constructed facts added (eight child names, eight child states). No original field was dropped. The `pdf:p2:b69` eight-part split remains `represented`; the first real proposal still reports `source_operation_unrepresented` unchanged. Verification gates reading full context did not change any outcome for these facts.

The replay then stops at the field-semantic gate: nine `semantic_binding_pending`, one `route_group_material_id_identity_conflict`, one `fact_quantity_attribution_unresolved`. Triage (from the saved input, no new model call):

- The nine pending states all propose `solution`/`suspension` for ports whose excerpts quote the pH-adjustment or injection sentences — the state word describes another object/stage, not the fact's own port. Category: state evidence misattributed; needs source-grounded state facts bound to the own material, not quote edits.
- `mat_reaction_mixture` carries three stage names (`reaction`, `solution`, `suspension`). Category: material-ID name drift; the identity contract question is whether stage labels may share one ID — not to be cleared by minting new IDs.
- The 1 M concentration provably belongs to solution B at preparation ("Solution B is prepared by dissolving 100 mmol NaOH in 100 mL of water (1 M)"); the unresolved status comes from case-sensitive identity matching (`solution B` vs source-initial `Solution B`). Category: identity case mismatch; policy decision for the next slice.

Full triage: ignored `result/operation-structure-20260928/semantic-gate-triage.json`.

## Verification at this checkpoint

| Check | Recorded result | Limit |
| --- | --- | --- |
| Route-PDF suites (`test_route_pdf_*`) | 163/163 pass | Synthetic and negative cases; counterexamples fix expected outcomes by full-sentence meaning; four acceptance tests cover forged/foreign context, non-containing context, legitimate >3-block context, and compile/save/rebuild tamper rejection |
| Contracts `unittest discover` | 116/116 pass | Contract suites only |
| Research same-env comparison (e1962a3 worktree vs patched, identical env, UTF-8) | 817 vs 834 run (17 new focused tests, all pass); 8 fail + 21 error on both sides; 0 new / 0 removed failure IDs; 0 changed signatures after tempfile-path normalization; one budget assertion fails in both with a run-varying threshold (recorded, test is outside touched files). See `same-env-failure-comparison.json` | This is the cause-level proof; the legacy historical-baseline comparison remains encoding-impaired |
| Device `pytest device_agent -q --tb=short` | 985 run; 935 pass, 50 fail; all 50 fingerprints match the checked-in baseline strictly (0 new / 0 removed / 0 changed) | No new Device handoff or true dispatch claim; `device-final-wip.xml` retained under ignored `result/operation-structure-20260928/` |
| Fresh live generation | Not executed: the configured model credential is rejected with HTTP 401 before any proposal is produced | External credential condition, not a model-generation or span-repair failure |

Local artifacts under ignored `result/operation-structure-20260928/`: saved-proposal replays, `same-env-failure-comparison.json`, `semantic-gate-triage.json`, Device/research logs and fingerprint comparisons.

## Next boundary

The evidence-span path is closed generically, with qualifier-fidelity guards and full-context verification. Do not claim autonomous completion of the A01 section or chemistry approval from the successful split structure. The next slice owns the field-semantic gate on its own terms (state evidence roles, material-ID identity policy, quantity/concentration identity matching). The complete local A01 forward run remains gated on a valid independent-review input; independent chemistry review, Research publication, Device preflight, and 303 dispatch remain unstarted, and no review approval is signed automatically. After the model credential is restored, one fresh generation must pass the same field, attribution, and coverage requirements — no reduced required fields, no switched standards.
