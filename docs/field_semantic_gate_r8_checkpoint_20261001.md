# Field Semantic Gate — Round-8 (Round 3A) Checkpoint (2026-10-01)

Round 3A wires the round-7 source relation into the formal G1 path and
closes its trust boundary. Round 7 proved the
post-operation-retained-object record at the contract layer but left every
G1 stage running with the default `retained_object_resolver=None`
(fail-closed, byte-identical behavior). Round 3A rebuilds the resolver
LIVE from the signed group blocks at every wired stage, extends the record
with locators and character spans, and hardens the convention layer so a
stored or fabricated record has **zero authority** anywhere.

Artifacts: `result/operation-structure-20260928/local-revision-r8-proposal.json`
(runner: `local-revision-r8.py`; replay: `local-revision-r8-replay.json`;
audit: `local-revision-r8-audit.json`, schema `bounded_local_revision/v8`).
The r8 proposal is byte-identical to the r7 proposal (verified: it is a
plain copy); only the integration layer changed.

## Integration map

**Wired (resolver rebuilt LIVE per call from the signed group blocks):**

- G1 association `_prepare_unsigned_proposal`
  (`route_pdf_group_extraction.py`): one resolver per group; output-state
  derivation falls back to input-state derivation, both collected into
  `convention_state_candidates`. The bounded local repair callback now
  receives the group and re-prepares each revision with a live resolver
  (`route_pdf_local_repair.py`).
- G1 local diagnostics (`route_pdf_local_diagnostics.py`): a per-proposal
  `retained_object_resolver_for` helper feeds the convention derivation
  and the literal-fact reason; `assess_unreviewed_proposal_literal_shape`
  receives the groups so its derivations use live resolvers.
- G1 proposal quality (`route_pdf_proposal_quality.py`): resolvers built
  when groups are supplied; without groups the behavior is unchanged.
- G1 material structure (`route_pdf_material_structure.py`): resolver
  built before proof-issue collection.
- Literal receipt (`route_group_fact_receipt.py`):
  `_state_derivation_proof` and `_literal_fact_reason` take the resolver;
  `produce_pdf_group_fact_receipt` builds one live resolver per enumerated
  group and threads it into both call sites.
- Group compiler (`route_group_compiler.py`):
  `compile_experimental_group_protocols(..., retained_object_resolvers=)`
  keyed by `(paper_id, experimental_group_id, source_digest)` (paper_id
  falls back to the parent protocol), threaded through `_compile_group` →
  `_fact_issue` → `_state_derivation_proof`. Default None = byte-identical.

**Deliberately fail-closed (default None), with reasons:**

- `route_pipeline.py` compile enumeration: enumeration diagnostics append
  to `discovery`, which the compile consumes, so enumeration cannot be
  moved above the compile; the pipeline keeps the default.
- Science audit (`route_science.py`) and source verifier
  (`route_pdf_source.py`): both accept the optional parameter and thread
  it, but the pipeline call site stays None — a compiled candidate retains
  only hashed evidence ids, not raw fact ids, and proofs bind
  `_evidence_id(naming_fact_id)`, so a resolver rebuilt there could never
  reproduce identical proof fields.
- `route_decision.py` / `route_decision_v2.py` package re-verification: no
  blocks are available at that layer at all; publication-gate follow-up
  (Q1 gates publication first).

## Trust boundary

- **A record has zero authority.** Only a resolver rebuilt live from the
  signed group blocks is ever consulted. No stage reads stored records;
  `proposal['retained_object_records']` is never consulted, and with the
  required-facts gate enabled a proposal carrying such an authority field
  is refused wholesale (`proposal_source_or_status_field_forbidden`).
- **Two-layer validation.** Parse checks shape only (schema_version,
  rule_id, output.state_path present, retained_object non-empty). The new
  cross-check then re-verifies the record against the live graph and
  facts: state_path equals the requested field path; output port
  instance/material/label match the graph; output_index and
  step_index/macro_step_id match; the name fact's fact_id equals both
  `output_name_fact_id` and `naming_fact_id` and its excerpt equals
  `naming_excerpt`; the operation fact's fact_id equals
  `operation_fact_id`. Any parse or cross-check failure yields
  `retained_object_output_binding_unresolved`.
- **Records carry proof-grade coordinates**: `operation_locator`,
  `operation_char_span` ([start, end) over the projected normalized
  source), `naming_locator`, `naming_char_span`, and `output_name_fact_id`.
  Winning proofs echo them as string fields
  (`retained_object_output_name_fact_id`, `retained_object_operation_*`,
  `retained_object_naming_*`); `verify_bound_output_state` re-derives the
  record live and demands exact equality (absent resolver →
  `convention_support_evidence_missing`; invalid/mismatched record →
  `retained_object_output_binding_unresolved`; differing dict →
  `convention_proof_mismatch`).

## R4 (fabricated-record rejection) design

- **Contract level**: the valid graph[5] record with a forged
  `naming_excerpt` (the real b67 block text) fed through a resolver is
  rejected with `retained_object_output_binding_unresolved`.
- **G1 excerpt swap**: the naming fact's excerpt is swapped to the real
  b67 block (no naming sentence). The proposal passes the proposal gate,
  the live derivation finds nothing, and graph[5].out / graph[6].in stay
  out of `convention_state_candidates` with `f_g5_out0_state` still
  flagged.
- **G1 smuggled record (gate)**: the untouched proposal plus the
  byte-perfect valid record as `proposal['retained_object_records']` is
  refused at the proposal gate
  (`proposal_source_or_status_field_forbidden`, 0 admitted protocols).
  Candidates may still list graph[5]/graph[6] — legitimately, via the live
  resolver on the untouched proposal before the gate runs — so admission,
  not derivation, is the pass condition.
- **G1 smuggled record (raw path)**: excerpt swap plus the smuggled
  byte-perfect record through the raw producer path (no required-facts
  gate): the proposal is processed, the stored record is never read, and
  nothing derives.

## Phase-1 closures re-verified

- R1–R3 (source-relation hardening, 6 probes): an affirmed unmodeled
  interval operation ("The solid was then dried at 60 C overnight.") and a
  conservative-fallback stem ("calcined") break continuity
  (`retained_object_intervening_operation`); a negated interval mention
  ("was not dried") keeps it; state-changing object modifiers ("The dried
  / calcined precipitates were labeled as …") reject
  (`retained_object_state_changing_modifier`); a benign modifier
  ("yellow") is recorded in `object_modifiers` and allowed.
- E1b–E5 (assertion polarity and medium binding, 8 probes): alternative
  scoping ("dried instead of / rather than being redispersed") and failed
  attempts ("attempted to redisperse … but failed") stay
  `convention_rule_not_applicable_or_ambiguous`; a hyphenated "water-free"
  medium and a medium named only in another clause stay
  `convention_liquid_participation_missing`; affirmed redispersion
  (participle and nominal) still proves REDISPERSION_V1.

## Round-3A acceptance (verbatim statuses from local-revision-r8-replay.json)

```json
{
  "r1_r3_probes": {"status": "PASS", "probes": "6/6 PASS (see replay)"},
  "e1b_e5_probes": {"status": "PASS", "probes": "8/8 PASS (see replay)"},
  "formal_g1": {
    "status": "PASS",
    "graph5_output_rule": "CENTRIFUGE_COLLECT_PRECIPITATE_V1",
    "graph6_input_rule": "PARENT_OUTPUT_STATE_INHERITANCE_V1",
    "g1_final_issue_reasons": {"semantic_binding_pending": 7},
    "g1_no_longer_flags": ["f_g5_out0_state", "f_g6_in0_state"],
    "admitted_protocols": 0
  },
  "r4_fabricated_record": {
    "status": "PASS",
    "contract_level": {"observed_issue": "retained_object_output_binding_unresolved", "status": "PASS"},
    "g1_excerpt_swap": {"graph5_output_in_candidates": false, "graph6_input_in_candidates": false, "f_g5_out0_state_flagged": true, "status": "PASS"},
    "g1_smuggled_record_gate": {"gate_rejection_present": true, "admitted_protocols": 0, "status": "PASS"},
    "g1_smuggled_record_raw": {"graph5_output_in_candidates": false, "graph6_input_in_candidates": false, "f_g5_out0_state_flagged": true, "status": "PASS"}
  },
  "live_resolver": {
    "status": "PASS",
    "stages": {
      "g1_association": {"source_digest": "sha256_2e9435bccb2e2e281…", "block_count": 23, "caption_count": 0},
      "receipt": {"source_digest": "sha256_2e9435bccb2e2e281…", "block_count": 23, "caption_count": 0},
      "compiler": {"paper_id": "paper-r8-compiler", "block_count": 4, "caption_count": 0}
    },
    "stored_records_read": "none"
  },
  "status": "PASS"
}
```

(Full probe rows, per-stage resolver descriptions, and the receipt and
compiler-demo detail are in the replay JSON; the digest above is truncated
here only for line width.)

## Replay result (local-revision-r8-replay.json)

- **Round-2 acceptance still PASS** on the unchanged proposal
  (post_operation_retained_object PASS; output_label_binding PASS;
  graph5_output_canonical_state "PASS: CENTRIFUGE_COLLECT_PRECIPITATE_V1
  1.1.0").
- **Formal G1**: `convention_state_candidates` now contains
  `material_graph[5].material_outputs[0].state` →
  CENTRIFUGE_COLLECT_PRECIPITATE_V1 and
  `material_graph[6].material_inputs[0].state` →
  PARENT_OUTPUT_STATE_INHERITANCE_V1 (alongside the eight graph[4] SPLIT_V1
  candidates, unchanged). G1 final issues drop from 9 to 7
  `semantic_binding_pending` — `f_g5_out0_state` and `f_g6_in0_state` are
  no longer flagged. For the avoidance of doubt: graph[5]'s suspension
  input passes the literal gates on its own (literal PASS), graph[5].out
  passes by derivation (CENTRIFUGE_COLLECT_PRECIPITATE_V1), and
  graph[6].in passes by inheritance
  (PARENT_OUTPUT_STATE_INHERITANCE_V1). The remaining 7 pending are
  exactly `f_g6_out0_state`, `f_g7a_in0_state`, `f_g7a_out0_state`,
  `f_g7b_in0_state`, `f_g7b_out0_state`, `f_g8_in0_state`, and
  `f_g8_out0_state` — i.e. graph[6].out onward: the graph[6] output and
  the honestly unresolved ms7a/ms7b/ms8 cascade. `admitted_protocols: 0`,
  reported as empty, never as a pass.
- **Receipt**: feeding the association's located proposal (facts re-bound
  to fresh `bind_pdf_quote` locators, as the receipt demands
  `source.locator == binding.locator`) into
  `produce_pdf_group_fact_receipt` derives
  `material_graph[5].material_outputs[0].state` and
  `material_graph[6].material_inputs[0].state` (plus the eight graph[4]
  SPLIT outputs). The group status stays `blocked` by design: the receipt
  queues every semantic trust decision (`semantic_binding_pending` for
  the honestly unresolved facts) and never substitutes derivation for
  review.
- **Compiler demo** (self-contained single-step fixture, both ports named
  "LDH seeds" to satisfy the per-step material identity rule): with the
  live resolver the compile reports no diagnostics and the evidence-matrix
  derivation for the output state carries
  CENTRIFUGE_COLLECT_PRECIPITATE_V1 plus all ten `retained_object_*`
  fields; without a resolver it stays `semantic_binding_pending`; a
  resolver returning a fabricated record (forged naming excerpt) is
  rejected at the contract cross-check and surfaces the same honest
  `semantic_binding_pending`.

## r7 artifacts refreshed

`local-revision-r7.py` was re-run: its replay/audit JSONs now carry the
extended record schema (`operation_locator`, `operation_char_span`,
`naming_locator`, `naming_char_span`, `output_name_fact_id`) and, because
the r7 runner's own association call now goes through the wired G1 path,
its `convention_state_candidates` also list graph[5].out / graph[6].in and
its G1 final issues drop to 7. No derivation, rule, or diagnostic other
than these wiring effects changed; the round-2 acceptance block is
unchanged and still PASS.

## r8 regression

- Full slice (`reaserch_agent/ chem_agent_contracts/`, minus
  llm_connectivity): **1175 passed, 30 failed** — every failure already on
  the known baseline lists (`../baseline-research-fails.log`,
  `../baseline-contracts-fails.log`, 31 entries); **zero new failures**.
  One baseline failure (`test_b1_bootstrap_generates_initial_outputs`) no
  longer fails — an improvement, not a regression.
- Targeted convention suites: `test_route_retained_object.py` (51 tests,
  including the new `RetainedObjectTrustBoundaryTest`: fabricated-record
  rejection, ten field mutations each →
  `retained_object_output_binding_unresolved`, inheritance over a record
  parent verifying only with a resolver, five forged proof fields →
  `convention_proof_mismatch`) and the new
  `test_route_retained_object_integration.py` (7 tests: G1 positive,
  G1 smuggled-record, receipt positive, receipt smuggled, compiler live /
  fail-closed / fabricated-resolver). 58 passed.

## Deliberately NOT done

- `route_pipeline.py` resolver threading (ordering dependency: enumeration
  diagnostics append to `discovery`, which the compile consumes).
- Science audit / source verifier / RouteDecision resolver threading (raw
  fact ids are unrecoverable from a compiled candidate — only hashed
  evidence ids survive; the decision layer has no blocks at all).
  Publication-gate follow-up: Q1 gates publication first.
- Chinese C1 naming/discard aliases (still English-only).
- No generic centrifugation-redispersion ×3 → washed_wet_solid rule and no
  composite-endpoint rule; "three times" stays a parameter.
- graph[6] output, the ms7a/ms7b/ms8 multi-hop cascade, and Q1 are not
  solved; the duplicate container ID question is deferred.
- Q1/productionization untouched.
