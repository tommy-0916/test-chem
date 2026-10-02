# Field Semantic Gate — Round-10 (Round 3C) Checkpoint (2026-10-02)

Round 3C seals the proof-DAG premise-substitution soundness hole (3C-0),
fixes the graph[7]/graph[8] evidence representation (3C-1), and runs the
sealed DAG over the full real chain tail of the signed NiFe Control
group: multi-hop inheritance now proves graph[7].in, while the second
centrifugation−redispersion protocol's outputs stay honestly BLOCKED with
clean attribution — one genuine retained-object evidence gap, one
independent liquid-participation diagnostic, and three dependency
cascades.

Artifacts: `result/operation-structure-20260928/local-revision-r10.py`
(runner), `local-revision-r10-proposal.json` (3C-1 proposal),
`local-revision-r10-replay.json` (replay), `local-revision-r10-audit.json`
(audit, schema `bounded_local_revision/v10`). The r7/r8 runners re-run
**byte-identical**; the r9 runner re-runs with all nine round-3B
acceptance items PASS and byte-identical JSONs.

## 3C-0: premise-substitution soundness fix

Round 3B verified "a valid node hangs on role `parent_state`" without
checking "that node's claim is exactly the parent-state proposition the
dependent node's proof references". Three coordinated changes close it
(`chem_agent_contracts/route_proof_dag.py`,
`chem_agent_contracts/route_convention_basis.py`):

- **Binding invariant** `proof_dag_parent_state_binding_mismatch`:
  `_parent_state_binding_issue` requires the parent_state premise node's
  claim to equal the dependent node's own computed parent binding
  (field_path + material_instance_id + state value) exactly, in the
  builder and in `StateProofDagVerifier._verify_convention` /
  `_verify_inheritance`. For inheritance nodes the premise claim must bind
  the resolved upstream output port (path, instance, port state). An
  otherwise-valid but unrelated node — or the correct ancestor at the
  wrong chain level — now fails even when every content address is
  honestly resealed with the module's own hashing.
- **Canonical chain**: `_DagBuilder._build_output_state` no longer
  attaches the upstream step's OUTPUT node directly as the parent_state
  premise; it attaches `build_node(parent_state_path)` — THIS step's own
  input state path — which routes through `_build_input_state` to an
  inheritance node (or a paper literal when the input state is
  literal-provable). Chain: graph[5].out → graph[6].in → graph[6].out.
- **Capability token replaces the public bool**: the public
  `parent_state_proven: bool` engine hook is GONE (the r9 doc's "The
  `parent_state_proven` engine hook" description is **superseded**). The
  module-private `_VerifiedParentStateEvidence`
  (`__slots__ = ("field_path", "state_value", "material_instance_id")`,
  absent from `__all__`) is minted only after the premise node is
  built/verified and the binding check passed; the flat engine re-checks
  the token's triple against its own computed parent binding and fails
  closed with `convention_parent_state_unverified` on any mismatch. The
  token forwarded through `_inheritance_proof_for_evidence` certifies the
  GRANDPARENT binding inside inheritance re-derivation, so chains of more
  than two consecutive non-literal hops compose — the 3B-1 follow-up
  noted in the r9 doc is resolved.

### Probe + acceptance tests

The out-of-repo probe (`Claude outputs/q4_round3b_probe.py`, never
committed) builds the real chain fixture, substitutes the valid but
unrelated `source_relation` node into the graph[6].out root's
`parent_state` role, honestly reseals every dependent content address,
and prints `PROBE: PASS` — the tampered DAG is rejected with exactly
`proof_dag_parent_state_binding_mismatch` while the untouched DAG still
verifies. Before 3C-0 the same probe printed FAIL (the substitution
verified). Five `PremiseBindingTest` acceptance tests in
`reaserch_agent/test_route_proof_dag.py` pin the invariant: unrelated
valid node substitution, wrong instance, wrong state, correct ancestor at
the wrong level, and a three-hop contamination fixture (graph[2].in →
graph[1].out → graph[1].in → graph[0].out).

## 3C-1: graph[7]/graph[8] representation fix (r10 proposal)

In the r8 proposal ms7a/ms7b each carried a BARE WORD (`"centrifugation"`,
`"redispersion"`) as the operation value standing alone on the compound
quote — bypassing the compound-operation guard and misattributing
blockers. The r10 proposal changes ONLY graph[7]/graph[8] so their source
operation is the compound `"second centrifugation−redispersion protocol"`
(U+2212), mirroring graph[5]'s composite-operation representation exactly:

- `operation` = the compound string; `operation_segments` = a
  centrifugation segment and a redispersion segment, each with provenance
  kind=paper → the step's operation evidence id; the segment the step's
  relation claims keeps `material_effect: transform_material`, the
  unclaimed segment is `unknown` (graph[5]'s convention);
- `material_relations[*].source_operation_ref` = the claimed segment id
  (ms7a → `ms7a-seg-centrifugation`, ms7b → `ms7b-seg-redispersion`);
- facts `f_g7a_op` / `f_g7b_op` carry the compound value on the unchanged
  excerpt `Finally, after a second centrifugation−redispersion protocol
  one time` — located in the signed blocks at `pdf:p2:b74-p2:b75`,
  binding issue `""` (the sentence continues into `pdf:p2:b76`).

The runner diffs the r10 envelope against r8: exactly six changed paths
(the two operations, the two segment lists, the two fact values);
graph[5], graph[6], graph[9] and every other fact are byte-identical to
r8. This is an evidence-semantics precondition, not cosmetic.

## 3C-2: multi-hop inheritance on the real chain

`material_graph[7].material_inputs[0].state` (ms7a.in, upstream_output
from graph[6].out) now builds and VERIFIES as an inheritance node — two
consecutive non-literal inheritance hops composing through the forwarded
capability token:

```
inheritance   graph[7].in = suspension            (root, 9-node DAG)
 └─ parent_state  state_change REDISPERSION_V1    graph[6].out = suspension
     ├─ operation   paper_literal                 material_graph[6].operation
     └─ parent_state  inheritance                 graph[6].in = retained_wet_solid
         └─ parent_state  state_change CENTRIFUGE_COLLECT_PRECIPITATE_V1
             graph[5].out = retained_wet_solid → operation/parent literals
             + source_relation POST_OPERATION_RETAINED_OBJECT_V1
```

graph[7].in needs no graph[5] paper facts directly: its own typed fields
reference only graph[6] evidence (`parent_evidence_id` =
`route_fact_61e3a6a8…` = f_g6_in0_state, `operation_evidence_id` =
`route_fact_9b523fc8…` = f_g6_op); graph[5] facts enter only as leaves of
the nested, independently verified premise subgraph — the verifier
recurses. Verified both with the span resolver and from the signed blocks
alone (`verify_with_span_resolver: ""`, `verify_blocks_only: ""`).

## 3C-3 / 3C-4: honest blockers, attributed

**ms7a.out — BLOCKED, evidence gap.** The paper affirms the second
centrifugation−redispersion protocol happened but never says which phase
the second centrifugation retained. With the genuine graph[7].in proof
discharging the parent gate, the build fails at the compound-operation
guard, which surfaces the resolver's recorded blocker
`retained_object_mention_precedes_operation` (it would fall back to
`convention_rule_not_applicable_or_ambiguous` only if the resolver had
recorded nothing — the retained-object evidence gap is the primary
attribution, not "rule not applicable"). The audit shows the only
"LDH seeds" naming in the signed blocks IS the first protocol's
retained-object naming (candidate span [17, 18] equals graph[5]'s naming
span [17, 18]; the second protocol's operation mention spans [20, 21],
later) — there is no retained-object mention FOR the second
centrifugation at all. It does NOT pass via protocol similarity
("which was the first …" + "a second … protocol" → copy retained
semantics = protocol-reference inheritance, a proof class that does not
exist) and NOT via downstream constraint ("redispersion follows, so the
solid must have been kept" = operation-precondition inference, also not a
published proof type). The task-sanctioned error-code refinement
(`retained_object_for_operation_unverified`) was NOT shipped: the
per-step resolver issues are recorded verbatim in the r7/r8 replay/audit
JSONs and those runners must re-run byte-identical (hard constraint), so
the resolver code stays untouched and the honest issue is reported with
this reasoning.

**ms7b.in — BLOCKED**, cascade from ms7a.out (build issue
`retained_object_mention_precedes_operation` propagated through the
inheritance edge).

**ms7b.out — BLOCKED**, reported as a dual result (verbatim from the
audit):

```
ms7b.out
proof status: BLOCKED
primary blocker: parent_state_unverified  caused_by: ms7a.out
    (build issue retained_object_mention_precedes_operation, propagated
     ms7b.out parent_state -> ms7b.in -> ms7a.out retained-object gap)
independent convention diagnostics:
    liquid_participation: FAIL  convention_liquid_participation_missing
```

The diagnostics are computed under a what-if harness — a local
`_VerifiedParentStateEvidence` minted in the RUNNER discharges the parent
gate as if ms7b.in were satisfied — are labelled
`"diagnostics_only": true, "feeds_verdict": false`, and never feed any
proof or pass verdict. Under that assumption the engine is STILL
independently blocked by the compound-operation guard
(`retained_object_mention_precedes_operation` — a second independent
gap), and the rule-local probe scoped to the graph's declared redispersion
segment shows every REDISPERSION_V1 premise passing except liquid
participation (`operation_affirmed: true`, `intent_affirmed: true`,
`input_state_allowed: true`, `liquid_participation: false`): the second
protocol's own evidence names no liquid medium and ms7b has no
liquid-state input port — the first protocol's water ("using deionized
water three times") must NOT propagate to the "second … protocol"
without a new rule.

**graph[9].in / graph[9].out — BLOCKED**, pure dependency cascades.

### Final 9-node state table (from local-revision-r10-replay.json)

| node | verdict | issue | attribution |
|---|---|---|---|
| graph[5].out | PASS | | proven (CENTRIFUGE_COLLECT_PRECIPITATE_V1 1.1.0) |
| graph[6].in | PASS | | proven (inheritance) |
| graph[6].out | PASS | | proven (REDISPERSION_V1 1.1.0) |
| graph[7].in (ms7a.in) | PASS (new) | | proven (multi-hop inheritance) |
| ms7a.out (graph[7].out) | BLOCKED | retained_object_mention_precedes_operation | evidence gap — second centrifugation retained phase |
| ms7b.in (graph[8].in) | BLOCKED | retained_object_mention_precedes_operation | dependency cascade, caused_by ms7a.out |
| ms7b.out (graph[8].out) | BLOCKED | parent cascade + independent liquid diagnostic | cascade + diagnostic |
| graph[9].in | BLOCKED | retained_object_mention_precedes_operation | dependency cascade |
| graph[9].out | BLOCKED | retained_object_mention_precedes_operation | dependency cascade |

Blocked split: **2 genuine evidence gaps** (ms7a retained object; ms7b
liquid participation as an independent what-if diagnostic) vs
**3 dependency cascades** (ms7b.in, graph[9].in, graph[9].out).

## Release gate — the four items and their evidence

1. **Premise substitution rejected (PASS→FAIL).** Probe: PASS — the
   source_relation substitution into graph[6].out's parent_state role
   fails `proof_dag_parent_state_binding_mismatch` with honestly resealed
   content addresses; untouched DAG verifies. Substitution series: 5
   `PremiseBindingTest` acceptance tests, plus the r10 runner's
   real-chain probes — the unrelated-valid-node tamper on graph[6].out
   AND the correct-ancestor-wrong-level tamper on the new graph[7].in
   root (graph[5].out's valid state_change node claiming graph[5].out
   where graph[6].out is bound) both fail with the same code.
2. **Canonical chain (6.out via 6.in).** The graph[6].out root's
   parent_state premise is the inheritance node claiming
   `material_graph[6].material_inputs[0].state`
   (`PARENT_OUTPUT_STATE_INHERITANCE_V1`), which stands on the graph[5]
   output's proven state-change node — asserted in the r10 runner's
   canonical-chain item at every level (graph[5].out → literal input,
   graph[6].out → graph[6].in, graph[7].in → resolved graph[6].out port).
3. **graph[5] source mutation invalidates through graph[7].in.** The
   real-chain three-hop contamination demo mutates f_g5_out0_name's
   excerpt: all four pre-mutation DAGs (graph[5].out, graph[6].in,
   graph[6].out, graph[7].in) fail with `proof_dag_leaf_fact_mismatch`
   under the mutated facts; every rebuilt root id moves; no stale
   source_relation node is resurrected (id sets disjoint); rebuilt DAGs
   verify under the mutated facts; old DAGs still verify under the
   untouched facts.
4. **ms7a/ms7b attribution with cascade vs independent split.** Above:
   ms7a.out = retained-object evidence gap (primary, exact code shown);
   ms7b.out dual result = parent cascade (caused_by ms7a.out) +
   independent `convention_liquid_participation_missing` diagnostic;
   ms7b.in / graph[9].in / graph[9].out = dependency cascades.

## r10 regression

- Target set (`test_route_proof_dag`, `test_route_retained_object`,
  `test_route_retained_object_integration`,
  `test_route_convention_state_chain`): **139 passed, 0 failed** (the
  3C-0 PremiseBindingTest contributes 5 of the 21 DAG tests; no tests
  added in phase 2 — the real-chain r10 behavior is pinned by the r10
  runner's acceptance items).
- Full slice (`reaserch_agent/` + `chem_agent_contracts/`, minus
  llm_connectivity): **1209 ran, 30 failed** — 29 in reaserch_agent, all
  on `../baseline-research-fails.log` (30 entries; the one baseline
  failure not reproduced is the environment-state-dependent
  `test_b1_bootstrap_generates_initial_outputs`, which passes), 1 in
  chem_agent_contracts = the single `../baseline-contracts-fails.log`
  entry (`test_legacy_bound_package_hash_is_unchanged`). **Zero new
  failures.**
- The r7 and r8 runners re-run **byte-identical** (replay + audit
  JSONs); the r9 runner re-runs with all nine round-3B acceptance items
  PASS and byte-identical JSONs (its r8 proposal is untouched).
- The out-of-repo probe prints `PROBE: PASS`.

## Deliberately NOT done (exclusions)

- **Q1** population scope: typed `parent_ref` v1 allows only
  `material_instance`; `PopulationStateProofV1` is the planned Q1
  extension.
- **Protocol-reference inheritance as a proof class**: NOT introduced —
  the first protocol's retained semantics and liquid medium are never
  copied onto the "second … protocol".
- **Operation-precondition inference**: NOT introduced — a downstream
  redispersion never proves an upstream retained phase.
- **`admitted_protocols` stays 0**; production cache untouched; no global
  gate relaxation; no existing check weakened to make a node pass.
- **Resolver error-code refinement**
  (`retained_object_for_operation_unverified`): NOT shipped — r7/r8
  byte-identity forbids changing the recorded per-step issue strings; the
  honest `retained_object_mention_precedes_operation` is reported with
  the attribution reasoning instead.
- **O8 deferred**.
