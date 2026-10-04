# Field Semantic Gate — Round-13 (G1) Checkpoint (2026-10-04)

Round 13 wires the already-accepted `state-proof-dag/v1` machinery into its two
formal consumption points — G1 消费多跳证明: the unsigned extraction pipeline
(`_prepare_unsigned_proposal`, parallel audit key) and the literal fact receipt
(`_state_derivation_proof`, fail-time rescue). No proof class was added, no
convention rule or `protocol-definition/v1` resource was extended, no capability
token is minted or carried, and the 3E operation-precondition diagnostic module
keeps zero proof dependency. The compiler, the science gate, source-label
resolution, and V2 publication remain unwired by design (see Boundaries).

Artifacts: runner `result/operation-structure-20260928/local-revision-r13.py`;
replay `local-revision-r13-replay.json` (sha256
`sha256_9084ca5497ccf0b46cdb0b10697953742bdb90814768d19b5ef0f1b30c7f2cb5`, two
consecutive runs byte-identical); audit `local-revision-r13-audit.json`. The
replay reuses the r10 proposal unchanged (sha256
`sha256_cb6b1ff7f6cffad29539a2ef560bc0f079cdad0d8953a1a09292d65a6b0dc2e7`,
injected via a fixed `invoke_json`) through the formal entry
`propose_pdf_group_unreviewed(..., check_required_graph_facts=True)` on the
signed NiFe-1 control group of `result/a01-v5-real-input-20260927/` (23 blocks,
digest `sha256_2e9435…12c8`). Nothing here is model-generated; no A01 pass, no
new model generation, and no V2 publication is claimed.

## Integration points

- `reaserch_agent/route_state_proof_dag.py` (new adapter):
  `build_verified_state_proof_dags(...)` builds one DAG per `.state` fact path
  from the live signed blocks and dual-verifies it (span-resolver mode and
  blocks-only mode; PASS requires both clean). Verdicts are attributed
  mechanically: `proven` on PASS; `evidence_gap` when BLOCKED with all direct
  upstream state dependencies proven, else `dependency_cascade`.
  `dag_entry_derivation(entry)` is the consumption guard: non-Mapping input,
  a foreign `schema_version` (the 3E diagnostic record is rejected here),
  a non-PASS verdict, a non-empty verify issue, or a root/field-path mismatch
  each yield no derivation.
- Extraction (`route_pdf_group_extraction.py`): `_prepare_unsigned_proposal`
  returns a parallel `convention_state_proof_dags` key (rows
  `status: "unreviewed_prerequisites_only"`, built from the current signed
  blocks) next to the untouched flat `convention_state_candidates` loop;
  `_invoke_bounded_proposals` carries the rows into
  `locator_production["convention_state_proof_dags"]` on the success/locator
  branch only — blocked batches (coverage/material-structure) carry no key and
  no partial batch advances.
- Receipt (`route_group_fact_receipt.py`): flat-first discipline. Output
  derivation and input inheritance run exactly as before; only when a `.state`
  fact is about to fail does `_dag_fallback` consult the per-group DAG map —
  which the receipt rebuilds and re-verifies from its own current signed
  blocks, never trusting a carried record. Accepted paths are classified into
  the new `PdfGroupLiteralStatusV1.dag_proven_state_field_paths` (default
  empty) with the derivation record marked `derivation="state_proof_dag_v1"`;
  input-path acceptance additionally requires the fact value to equal the DAG
  root claim's `target_state`.

## Nine-node table, reproduced through the formal entry

The r10/r11 round-3d table is reproduced byte-referenced
(`matches_r10_r11_table: true` on all nine rows):

| node | verdict | issue | attribution |
| --- | --- | --- | --- |
| graph[5].out | PASS | — | proven |
| graph[6].in | PASS | — | proven |
| graph[6].out | PASS | — | proven |
| graph[7].in (ms7a.in) | PASS | — | proven |
| ms7a.out (graph[7].out) | BLOCKED | retained_object_mention_precedes_operation | evidence_gap |
| ms7b.in (graph[8].in) | BLOCKED | retained_object_mention_precedes_operation | dependency_cascade |
| ms7b.out (graph[8].out) | BLOCKED | retained_object_mention_precedes_operation | dependency_cascade |
| graph[9].in | BLOCKED | retained_object_mention_precedes_operation | dependency_cascade |
| graph[9].out | BLOCKED | retained_object_mention_precedes_operation | dependency_cascade |

ms7a.out stays BLOCKED on the retained-object evidence gap — the DAG wiring
does not loosen the retained-object convention.

## Receipt before/after (same signed group, same proposal)

- Baseline: status `blocked`; 7× `semantic_binding_pending` (fact[69],
  fact[73], fact[75], fact[78], fact[80], fact[83], fact[85]); 10 flat-derived
  state paths (graph[5].out, graph[6].in, and the eight graph[4] split
  outputs).
- Integrated: status `blocked` (unchanged — the ms7a chain is honestly
  unprovable); 5× `semantic_binding_pending`; the same 10 flat-derived paths;
  plus `dag_proven_state_field_paths` = {graph[6].out (fact[73→
  material_graph[6].material_outputs[0].state), ms7a.in
  (material_graph[7].material_inputs[0].state)}.

The delta is exactly the two DAG-exclusive paths. graph[5].out and graph[6].in
were already flat-derivable and stay classified as flat-derived under the
flat-first discipline — the DAG never relabels a fact the flat engine proves.
The three verified-literal state facts (graph[3].in[0..2]) are untouched.

## Extraction flat parity

With `build_verified_state_proof_dags` mocked out, the locator artifact is
identical to the integrated run except for the `convention_state_proof_dags`
key itself (`flat_candidates_identical: true`,
`only_changed_key: convention_state_proof_dags`). The pre-G1 artifact shape is
preserved key-for-key.

## Independent controls

- **Electrode Preparation group** (8-step graph, 70 facts; formal entry
  reaches `located_unreviewed`): extraction DAG key carries 24 rows with 2
  PASS — graph[1].in[1] and graph[4].in[1], both single-node `paper_literal`
  roots where the flat candidate set is empty. The receipt shows zero
  baseline-vs-integrated deltas: both paths were already `verified_literal`,
  so no DAG rescue fires. EP demonstrates DAG recognition on an independent
  group at the extraction point; the multi-hop DAG-exclusive receipt
  acceptance is demonstrated by the NiFe chain above.
- **ER group** (5-step graph, 33 facts, 11 state facts; proposal from
  `raw_llm_outputs.route_pdf_group_propose.proposals[3]` of the A01-v5
  iteration_00 research state): the formal entry stops at the
  operation-coverage audit (`source_operation_unrepresented`,
  `blocked_unreviewed`) before locator production, so no DAG key exists by
  design. The graph carries no material relations, so no state field is
  multi-hop-provable in any case. Recorded as a coverage gap; repairing it
  means authoring new graph steps, which is not a field-level fix and is out
  of scope this round.

## Negative battery (all reproduced in the replay)

- **Source mutation** (one block prefixed): every PASS DAG of the graph[5..7]
  chain flips to BLOCKED — `proof_dag_node_mismatch` (span-resolver mode) and
  `proof_dag_leaf_relocation_mismatch` (blocks-only mode); both receipt
  acceptances disappear.
- **Wrong scope**: a foreign `experimental_group_id` or `source_digest` yields
  `proof_dag_context_mismatch` on every entry.
- **Forged entries**: a tampered `root_id`, a cross-path substituted entry,
  and a BLOCKED entry with a forged PASS verdict are all rejected by the
  consumption guard.
- **Diagnostic-record injection**: the 3E diagnostic record (dict form and
  non-Mapping form) is rejected by the guard and never derives at the receipt.

## Fixed-constraints audit

- 3E diagnostics module (`route_operation_precondition_diagnostic.py`): zero
  proof dependency — source-audited, unchanged this round.
- The adapter mints no capability token and carries none; no new proof class;
  `protocol-definition/v1` untouched (rule resources byte-identical; both
  state-change nodes reference the pre-existing rule-pack digest
  `sha256_39fb6e77…8267`).
- ms7a.out stays BLOCKED with `retained_object_mention_precedes_operation`.
- Device not run; the eight-question campaign not rerun; contracts tree
  untouched (imported only).

## Boundaries not wired (explicit)

- The **group compiler** does not consume `state-proof-dag/v1`; its
  per-fact semantic gates are unchanged.
- The **science gate** and **source-label resolution** are untouched.
- **V2 publication** is untouched: DAG-proven facts are receipt-level
  classifications (`unreviewed_prerequisites_only` provenance at extraction),
  not V2 admissions; nothing here promotes a batch.

## Regression

- Full-slice `unittest discover`: `reaserch_agent` 1199 tests — failure set
  equals the recorded baseline except that the pre-existing flaky
  `test_b1_bootstrap_generates_initial_outputs` no longer fails (it passes
  standalone, repeatedly; the module is untouched by this round). Zero new
  failures. `chem_agent_contracts` 116 tests — failure set identical to
  baseline.
- New tests: `test_route_state_proof_dag.py` (15), plus one G1 regression test
  in each of `test_route_pdf_group_extraction.py` and
  `test_route_group_fact_receipt.py`.
- One pre-existing expectation updated:
  `test_route_retained_object_integration::test_receipt_derives_output_and_inheritance`
  asserted the pre-G1 outcome `fact[8]:semantic_binding_pending` for the
  redispersion output of its synthetic two-step chain. Under G1 that output is
  legitimately DAG-proven (REDISPERSION_V1 composed over the proven S1 chain,
  rebuilt from live blocks — the round-6 checkpoint already established that
  REDISPERSION_V1 fires once the parent output state is proven). The test now
  asserts `dag_proven_state_field_paths == (graph[1].out.state,)` and empty
  reason codes; its smuggled-record sibling is unchanged and still derives
  nothing, confirming stored/fabricated records have zero authority at the
  receipt's DAG layer as well.

## Design decisions (for the record)

1. **Fail-time rescue, not pre-consultation.** An earlier in-progress wiring
   consulted the DAG map before the literal gates; that relabeled
   literally-provable facts as DAG-proven (label drift). The merged design
   runs every pre-G1 check first and consults the DAG only at the five state
   gate return points (plus the string-value check) where the fact would
   otherwise be recorded as blocked.
2. **Receipt rebuilds, never trusts.** DAGs carried on the locator artifact
   are an audit trail; the receipt rebuilds and dual-verifies from its own
   current signed blocks, so a stale or smuggled record has no authority
   (negative battery, above).
3. **Dual verification is stricter than either mode alone** — PASS requires
   both the span-resolver and blocks-only modes clean. On the raw r10 proposal
   the blocks-only mode rejects graph[4].in[0]
   (`proof_dag_leaf_not_relocatable`); the formal entry's tightening resolves
   the quote and the entry PASSes. This asymmetry is intentional.

## r13b: acceptance-wording correction (recorded semantics unchanged)

The r13 runner's header described the Electrode Preparation control as having
"two DAG-proven input-state fields (flat could not derive them) … DAG-accepted
at the receipt". That sentence is inaccurate; the recorded replay already
contradicted it. The corrected wording, archived as
`result/operation-structure-20260928/local-revision-r13b.py`:

- **EP is an independent literal positive, not a DAG-acceptance positive.**
  Its two extraction DAG-PASS rows (graph[1].in[1], graph[4].in[1]) are
  single-node `paper_literal` roots; at the receipt both are
  `verified_literal` in the baseline and integrated runs alike, so the
  receipt's DAG acceptance for EP is **zero** by design (`ep_dag_proven: []`).
  EP proves DAG recognition on an independent group at the extraction point.
- The DAG-exclusive multi-hop receipt acceptance is demonstrated only on the
  NiFe Control chain (graph[6].out, ms7a.in). The ER control stays a coverage
  gap. An **independent multi-hop positive control** (a second signed group
  whose receipt DAG-accepts a multi-hop-proven field) is still pending
  acceptance and is not claimed.

r13b regenerates the replay/audit from the same fixed inputs and **pins both
outputs to the archived r13 digests** (replay LF `sha256_9084ca…2cb5`, audit
LF `sha256_9919a4…69ee`); a drift fails the runner. The pin is green, which
also evidences that the concurrent G1 follow-up fixes (receipt unit-check
rescue ordering, DAG-aware diagnostics producers, final-version DAG rows on
the locator artifact) are verdict-neutral for this acceptance.

Conventions, stated once explicitly:

- "Two runs byte-identical" compares the two in-memory UTF-8 serializations
  inside one process. `double_run_byte_identical: true` and `replay_sha256`
  appear in the **stdout summary only**; the persisted replay JSON does not
  contain them.
- Replay/audit files are written with `\n` newlines. On Windows with
  `core.autocrlf=true` the working tree shows CRLF while the git index stores
  LF; all pinned digests are computed over the LF bytes.
