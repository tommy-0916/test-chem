# Field Semantic Gate — Round-7 (Round 2) Checkpoint (2026-10-01)

Round 2 unblocks graph[5] (ms5) without loosening any round-6 hardening.
Round 6 left ms5 at `convention_rule_not_applicable_or_ambiguous`: the
paper's composite operation ("by a centrifugation−redispersion protocol
using deionized water three times", b70-b71) matches two convention rule
families, the proposal named no verbatim collect-precipitate intent, and
no generic composite rule was allowed. Round 2 separates the two concerns
the composite guard was conflating: **what the paper says was retained**
(a SOURCE relation, derived from the signed blocks) and **what canonical
state that implies** (still convention-derived, still fail-closed on
conflict).

Artifacts: `result/operation-structure-20260928/local-revision-r7-proposal.json`
(runner: `local-revision-r7.py`; replay: `local-revision-r7-replay.json`;
audit: `local-revision-r7-audit.json`, schema `bounded_local_revision/v7`).
The r7 proposal is a deep copy of the r6 proposal with ONLY graph[5]
changed (verified by diff): the operation value is now the verbatim
composite `centrifugation−redispersion protocol` (U+2212, exact substring
of the unchanged f_g5_op excerpt, never retyped with an ASCII hyphen),
repetitions="three" stays bound to the whole protocol by the text, and a
second segment `ms5-seg-redispersion` (`material_effect="unknown"`)
documents that the composite contains redispersion WITHOUT claiming a
micro-sequence endpoint; the state_change relation still references only
`ms5-seg-centrifugation`. All other steps, facts, outputs, relations, and
lineage are byte-identical to r6. The extra segment passed every
downstream gate, so the single-segment fallback was not needed.

## What Round 2 added

- **Source relation module** `chem_agent_contracts/route_retained_object.py`
  (`post-operation-retained-object/v1`, rule POST_OPERATION_RETAINED_OBJECT_V1
  v1.0.0). For a graph step it derives — from signed block spans only —
  whether a post-operation naming sentence binds a retained-object noun to
  exactly one of the step's output labels. Guards, in order: operation
  quote must bind (`retained_object_operation_span_missing`); exactly one
  output label bound (`retained_object_output_binding_unresolved`);
  captured object not predicated as discarded
  (`retained_object_discarded`); naming follows the operation
  (`retained_object_mention_precedes_operation`); no other modeled
  operation intervenes (`retained_object_intervening_operation`). The
  module performs no chemistry: it never maps the object to a state and
  never picks a convention rule. English-only patterns (Chinese C1 is out
  of Round 2).
- **Reviewed rule declaration** in
  `chem_resources/chemistry_conventions/conventions.json`:
  CENTRIFUGE_COLLECT_PRECIPITATE_V1 bumped to v1.1.0 with
  `accept_post_operation_retained_object: true` inside `preconditions`.
  This is the reviewed declaration that THIS rule may accept the source
  relation; no other rule was touched, and it is NOT a generic
  "centrifugation-redispersion ×3 → washed_wet_solid" rule.
- **Composite-guard arbitration** in
  `chem_agent_contracts/route_convention_basis.py`
  (`retained_object_resolver` kwarg, default None = byte-identical
  behavior). With a validated record, a composite operation proceeds to
  eligibility; the declaring rule skips the verbatim-intent affirmation
  (the source relation discharges it) and matches its
  `retained_object_patterns` against the record's normalized object, never
  the excerpt. After the loop, eligible rules are filtered to
  record-consistent ones only: a rule with object patterns must have
  matched the record object; a rule without them is consistent only when
  `normalize_material_state(record object)` equals its retained endpoint
  (fail-closed: unmapped objects normalize to "unknown" and match
  nothing). Any conflict between the record and a family endpoint stays
  `convention_rule_not_applicable_or_ambiguous` — nothing may stand on
  segment order or one family's premises alone. Winning proofs carry
  string fields `retained_object_rule_id`, `retained_object_rule_version`,
  `retained_object`, `retained_object_evidence_id`;
  `verify_bound_output_state` re-resolves and re-validates the record for
  such proofs (absent resolver → `convention_support_evidence_missing`;
  invalid record → `retained_object_output_binding_unresolved`), and
  `derive_unreviewed_input_state` threads the resolver so a proven
  composite parent unblocks inheritance. Without a record, composite
  operations stay ambiguous (the round-6 hardening tests pass unchanged).

### Ordering tie-break inside a shared physical block

The naming sentence "The precipitates were labeled as LDH seeds" begins
with "The" — the last word of physical block b71, the SAME block where the
operation sentence ends ("...protocol. The" / b72 "precipitates ..."). A
pure block-index guard (naming.first strictly greater than op.last) would
report the naming as not following the operation even though it textually
does: a PDF seam split the sentence boundary. When both excerpts touch the
same block, the module tie-breaks with exact character spans from the same
binding projection: the operation quote's last character must precede the
naming quote's first character in the shared block. A textually preceding
mention is never admitted; every specified negative test behaves
identically under both readings (none share a block). This is the one
deliberate refinement beyond the block-index-only rule, recorded here and
in the runner audit.

## Round-2 acceptance (verbatim from local-revision-r7-replay.json)

```json
{
  "graph5_macro_operation": "centrifugation−redispersion protocol",
  "post_operation_retained_object": {
    "status": "PASS",
    "record": {
      "schema_version": "post-operation-retained-object/v1",
      "rule_id": "POST_OPERATION_RETAINED_OBJECT_V1",
      "rule_version": "1.0.0",
      "step_index": 5,
      "macro_step_id": "ms5",
      "operation_fact_id": "f_g5_op",
      "output": {
        "state_path": "material_graph[5].material_outputs[0].state",
        "name_path": "material_graph[5].material_outputs[0].name",
        "output_index": 0,
        "material_instance_id": "inst_ldh_seeds",
        "material_id": "mat_ni3fe_ldh",
        "label": "LDH seeds"
      },
      "retained_object_surface": "precipitates",
      "retained_object": "precipitate",
      "naming_fact_id": "f_g5_out0_name",
      "naming_excerpt": "The precipitates were labeled as LDH seeds, which were dispersed in 30",
      "operation_span": [16, 17],
      "naming_span": [17, 18]
    }
  },
  "output_label_binding": {
    "status": "PASS",
    "retained_object": "precipitate",
    "label": "LDH seeds",
    "output_name": "LDH seeds"
  },
  "graph5_output_canonical_state": {
    "status": "PASS: CENTRIFUGE_COLLECT_PRECIPITATE_V1 1.1.0"
  }
}
```

(The full proof dict is in the replay JSON; it carries the
`retained_object_*` evidence fields and all-string values.)

## Replay result (local-revision-r7-replay.json)

- `admitted_protocols: 0`; diagnostics `semantic_binding_pending: 9` —
  G1's receipt path runs WITHOUT the resolver (default None), so the
  unsigned association outcome is unchanged from r6; G2 not entered; empty
  list reported as empty, never as a pass.
- Per-graph derivation outcomes (exact codes from the replay JSON):
  - graph[5] input suspension: `semantic_binding_pending` (input path,
    unchanged).
  - graph[5] output: **output_state_proof CENTRIFUGE_COLLECT_PRECIPITATE_V1
    (rule_version 1.1.0)** — the composite operation, arbitrated by the
    validated precipitates ↔ "LDH seeds" record; REDISPERSION_V1's
    endpoint conflicts and is filtered out.
  - graph[6] input (inheritance): **input_state_inheritance
    PARENT_OUTPUT_STATE_INHERITANCE_V1** — unblocked by the proven ms5
    parent output.
  - graph[6] output: `convention_parent_state_unverified` (unchanged; the
    parent input state word is not literal in its excerpt).
  - graph[7a] input: `semantic_binding_pending` (input-path code;
    inheritance issue `convention_parent_state_unverified`); graph[7a]
    output: `convention_parent_state_unverified` (unchanged).
  - graph[7b] input: `semantic_binding_pending` (inheritance issue
    `convention_parent_state_unverified`); graph[7b] output:
    `convention_parent_state_unverified` (unchanged).
  - graph[8] input: `semantic_binding_pending` (inheritance issue
    `convention_parent_state_unverified`); graph[8] collected output:
    `convention_parent_state_unverified` (unchanged).
- Per-step source-relation outcomes (replay
  `post_operation_retained_object_per_step`): ms5 record (precipitate ↔
  LDH seeds, operation span [16,17], naming span [17,18]); ms6/ms7a/ms7b
  honestly report `retained_object_mention_precedes_operation` (the b71-72
  naming sentence precedes their own later operations); ms0-ms3 and ms8
  `retained_object_output_binding_unresolved`; ms4
  `retained_object_operation_span_missing`.

## r6 regression

- Pre-edit rerun of `local-revision-r6.py` reproduced the committed r6
  replay byte-identically.
- Post-edit rerun: the ONLY differences are the 8 `resource_digest`
  strings inside graph[4]'s SPLIT_V1 candidate proofs — the sha256 of
  `conventions.json` changed with the mandated v1.1.0 bump, and the digest
  is embedded in every convention proof. No derivation, issue, diagnostic,
  count, or structure differs; the resolver defaults to None everywhere in
  r6's path. The committed r6 replay file was restored afterwards, so the
  r6 artifacts in git remain untouched.
- Tests: `test_route_retained_object.py` (14 tests: positive record +
  derivation + verify, precedes/other-label/discarded/discussion/
  wrong-label/intervening negatives, composite-without-record hardening,
  conflict-stays-pending, inheritance threading, record validation, span
  resolver), plus one new assertion in
  `test_chemistry_conventions.py` (declaration present, scoped to the one
  rule). Targeted suites: 67 passed. Broader slice
  (`reaserch_agent/ chem_agent_contracts/`, minus llm_connectivity):
  1110 passed, 30 failed — every failure already on the known baseline
  lists (`../baseline-research-fails.log`,
  `../baseline-contracts-fails.log`); zero new failures.

## Deliberately NOT done

- G1 `_prepare_unsigned_proposal` / compiler / receipt plumbing of the
  resolver: they run with the default (None) and are byte-identical in
  behavior; wiring the source relation into the receipt is the next
  integration step.
- Chinese C1 naming/discard aliases (Round 2 is English-only).
- No generic composite rule and no "×3 → washed_wet_solid" rule; the
  "three times" repetition stays a parameter, not an operation sequence.
- graph[6] output, multi-hop chains (ms7a/ms7b/ms8 cascade), and Q1 are
  not solved; the duplicate container ID question is deferred.
- The discard-check unit test uses the comma variant ("...labeled as LDH
  seeds, and the precipitates were discarded."): without the comma the
  naming pattern's label run to end-of-sentence never binds the label, so
  that exact sentence yields `retained_object_output_binding_unresolved`
  rather than `retained_object_discarded` (both asserted in the test).
