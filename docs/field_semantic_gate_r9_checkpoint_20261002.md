# Field Semantic Gate — Round-9 (Round 3B) Checkpoint (2026-10-02)

Round 3B composes the flat single-hop convention proofs into a typed,
content-addressed proof DAG and proves the graph[6] redispersion output on
the real signed NiFe Control group — the chain the flat engine cannot
prove, because the parent state (`retained_wet_solid`) never appears
literally in any excerpt.

Round 3A wired the live retained-object resolver into the formal G1 path;
Round 3B builds upward composition on top of it. The r8 proposal is reused
**unmodified** (read-only; sha256 recorded in the replay).

Artifacts: `result/operation-structure-20260928/local-revision-r9.py`
(runner), `local-revision-r9-replay.json` (replay),
`local-revision-r9-audit.json` (audit, schema `bounded_local_revision/v9`).
The r8 runner was re-run under the phase-0 + 3B-1 tree: its replay and
audit JSONs are **byte-identical** and round-2 / round-3A acceptance still
PASS, so no r8 artifact changed.

## What 3B added

New module `chem_agent_contracts/route_proof_dag.py`
(`state-proof-dag/v1`):

- **Typed DAG, five node schemas**: `paper-literal-proof/v1` (a source fact
  standing alone on its literal gates), `source-relation-proof/v1` (the
  post-operation retained-object relation on its naming / operation /
  output-name leaves), `same-state-convention-proof/v1` and
  `state-change-convention-proof/v1` (the convention rules), and
  `state-inheritance-proof/v1` (input state inherited from an upstream
  output, with a typed `parent_ref` — v1 allows only
  `{kind: material_instance, macro_step_id, material_instance_id}`).
- **`source-evidence-leaf/v1`**: every source fact projects into a leaf
  carrying paper/group scope, source digest, binding locator, projected
  character span, excerpt digest (over the normalized projection), field
  path, claim value, and a fact digest over the binding fields. With the
  signed blocks available the verifier **re-locates** each leaf from
  locator + char span alone and re-checks the excerpt digest — leaves are
  recomputable, not trusted.
- **Content-addressed ids**: `proof_node_*` / `source_leaf_*` are sha256 of
  the canonical JSON payload (sorted keys, `,`/`:` separators, id field
  excluded); premises are canonicalized by sorting on `role` before
  hashing. Any content change — including one premise id — changes the
  node's own id, so a tampered node either fails id recomputation or
  leaves a dangling premise reference.
- **Pinned-context verifier** (`StateProofDagVerifier`): the context
  (graph digest, paper/group scope, source digest, live rule-resource
  digest) is fixed at construction; a DAG built under any other context
  fails `proof_dag_context_mismatch`. Verification is structural first
  (premise existence, acyclicity, depth), then content: every node id is
  recomputed and every node type re-derived from the signed blocks, facts
  and versioned rules. Any failure invalidates the whole DAG — there is no
  partial pass.
- **Single-run memoization**: the builder memoizes per field path within
  one build, so shared premises (the graph[5] operation leaf serves both
  the source relation and the state-change node; the naming and
  output-name leaves dedupe onto one node) appear exactly once.
- **The `parent_state_proven` engine hook** (phase-0, in
  `route_convention_basis.py`): the flat engine's parent-state literal
  gate is skipped only when a composed proof graph sets the flag after the
  parent premise carries its own verified node in the DAG. Default False
  keeps legacy behavior byte-identical; the flat engine is reused, never
  reimplemented.

## Phase-0 prerequisite patch (E5 / O5)

Two engine preconditions the real graph[6].out proof needs, landed as a
separate preparatory patch with their own tests
(`reaserch_agent/test_route_convention_state_chain.py`):

- **E5 coordination boundary**: `_liquid_medium_for_operation` now also
  splits a clause at a *coordinated* boundary with its own new subject and
  finite predicate (", and the catalyst ink was prepared in water") — that
  medium never wets the first clause's operation. Coordinated predicates
  of one subject (", and aged for 20 h") do not split. Without this, the
  real graph[6] excerpt ("…were dispersed in 30 mL of water and aged for
  20 h at room temperature under a stirring…") bound the medium across the
  coordination.
- **O5 bounded medium phrase**: `_liquid_medium_in_excerpt` accepts an
  optional quantity + volume unit (+ optional "of") before the liquid
  token — "in 30 mL of water" — while still rejecting `-free`/`-less`
  compounds and substrate/bath readings.
- The r8 checkpoint doc's replay section was corrected in passing: the
  remaining 7 `semantic_binding_pending` facts are exactly
  `f_g6_out0_state` and the ms7a/ms7b/ms8 cascade; graph[5]'s suspension
  input passes the literal gates on its own.

## The chain that now works

On the real signed group (`doi_10_1021_acsami_3c11651`, "Synthesis of the
Pristine Ni3Fe LDHs (NiFe Control).", 23 blocks):

```
paper literals ── graph[5].operation, graph[5].in[1].state, graph[5].out[0].name
        │
        ▼
source_relation  POST_OPERATION_RETAINED_OBJECT_V1  (naming/operation/output_name leaves)
        │
        ▼
state_change     CENTRIFUGE_COLLECT_PRECIPITATE_V1 1.1.0   graph[5].out = retained_wet_solid
        │                                   │
        ▼ (parent_state premise)            ▼ (parent_state premise)
state_change     REDISPERSION_V1 1.1.0      inheritance  PARENT_OUTPUT_STATE_INHERITANCE_V1
graph[6].out = suspension                   graph[6].in = retained_wet_solid
(root of the graph6_output DAG,             (root of the graph6_input DAG,
 premises {operation, parent_state})         typed parent_ref → ms5 / inst_ldh_seeds)
```

The graph[6].out root's `parent_state` premise is the graph[5] output's own
proven state-change node — the paper literal → state change → (inheritance)
→ state change composition the flat engine structurally cannot express.

## Round-3B acceptance (verbatim statuses from local-revision-r9-replay.json)

```json
{
  "graph5_output_typed_dag": {
    "status": "PASS",
    "root_node_type": "state_change",
    "root_schema": "state-change-convention-proof/v1",
    "rule_id": "CENTRIFUGE_COLLECT_PRECIPITATE_V1", "rule_version": "1.1.0",
    "premise_roles": {"operation": "paper_literal", "parent_state": "paper_literal", "retained_object": "source_relation"},
    "node_count": 5, "verify_with_span_resolver": "", "verify_blocks_only": ""
  },
  "graph6_input_inheritance_dag": {
    "status": "PASS",
    "root_node_type": "inheritance",
    "root_schema": "state-inheritance-proof/v1",
    "rule_id": "PARENT_OUTPUT_STATE_INHERITANCE_V1",
    "parent_ref": {"kind": "material_instance", "macro_step_id": "ms5", "material_instance_id": "inst_ldh_seeds"},
    "parent_premise_node_type": "state_change",
    "parent_premise_rule_id": "CENTRIFUGE_COLLECT_PRECIPITATE_V1",
    "verify_with_span_resolver": "", "verify_blocks_only": ""
  },
  "graph6_output_redispersion_dag": {
    "status": "PASS",
    "root_node_type": "state_change",
    "rule_id": "REDISPERSION_V1", "rule_version": "1.1.0",
    "node_count": 7, "verify_with_span_resolver": "", "verify_blocks_only": "",
    "premise_chain_summary": "paper literals -> source_relation POST_OPERATION_RETAINED_OBJECT_V1 -> state_change CENTRIFUGE_COLLECT_PRECIPITATE_V1 (graph[5].out retained_wet_solid) -> state_change REDISPERSION_V1 (graph[6].out suspension)"
  },
  "no_literal_parent_state": {
    "status": "PASS",
    "facts_scanned": 87,
    "excerpts_containing_retained_wet_solid_literal": [],
    "flat_engine_graph6_output": {"proof": null, "issue": "convention_parent_state_unverified"},
    "dag_graph6_output_verify": ""
  },
  "invalidation": {
    "status": "PASS",
    "operation_excerpt_tamper": {
      "status": "PASS",
      "old_graph6_output_dag_verify_tampered_facts": "proof_dag_leaf_fact_mismatch",
      "old_graph6_input_dag_verify_tampered_facts": "proof_dag_leaf_fact_mismatch",
      "root_id_changed": true, "rebuilt_dag_verifies_tampered_facts": true,
      "old_dag_still_verifies_untouched_facts": true,
      "downstream": "source_relation + CENTRIFUGE state_change + REDISPERSION root all invalid (whole-DAG, no partial pass)"
    },
    "naming_excerpt_tamper": {
      "status": "PASS",
      "old_graph6_output_dag_verify_tampered_facts": "proof_dag_leaf_fact_mismatch",
      "old_graph6_input_dag_verify_tampered_facts": "proof_dag_leaf_fact_mismatch",
      "source_relation_node_id_changed": true, "root_id_changed": true
    },
    "rule_bytes_tamper": {
      "status": "PASS",
      "graph5_output_dag_verify_forged_rules": "proof_dag_context_mismatch",
      "graph6_input_dag_verify_forged_rules": "proof_dag_context_mismatch",
      "graph6_output_dag_verify_forged_rules": "proof_dag_context_mismatch",
      "dag_verifies_real_rules_after_mock": true
    }
  },
  "forged_upstream_node": {
    "status": "PASS",
    "forged_upstream_node": {"verify_issue": "proof_dag_node_id_mismatch"},
    "tampered_premise_id": {"verify_issue": "proof_dag_premise_missing"},
    "untouched_dag_still_verifies": true
  },
  "cycle_future_reference": {
    "status": "PASS",
    "cycle": {"verify_issue": "proof_dag_cycle"},
    "future_reference": {"build_issue": "proof_dag_future_reference"}
  },
  "leaf_relocation": {
    "status": "PASS",
    "per_dag": "all three DAGs verify from the signed blocks alone (3/3/4 leaves, each with locator + char_span)",
    "tampered_char_span_resealed": {"verify_issue": "proof_dag_leaf_relocation_mismatch"},
    "tampered_char_span_unresealed": {"verify_issue": "proof_dag_node_id_mismatch"}
  },
  "legacy_regression": {
    "status": "PASS",
    "legacy_flat_proof": {"schema_version": "route-convention-state/v1", "rule_id": "CENTRIFUGE_COLLECT_PRECIPITATE_V1", "rule_version": "1.1.0", "verify_bound_output_state_issue": ""},
    "legacy_schema_in_dag_nodes": "absent from all three DAGs"
  },
  "status": "PASS"
}
```

(Full evidence — per-node ids, leaf locators/spans, downstream node lists,
tamper constructions — is in the replay JSON; the premise tree per DAG is
under `per_dag_summaries`.)

## Notes on the acceptance evidence

- **Item 4 is the hard signal**: scanning all 87 route facts, no excerpt
  contains the literal `retained_wet_solid`; the flat engine still returns
  `convention_parent_state_unverified` for graph[6].out while the DAG
  verifies. The DAG did not weaken the gate — it discharged it with a
  proven parent node.
- **Item 5(a)** tampers the graph[5] operation/state quotes together (the
  convention layer demands the child state quote be the operation quote),
  shifting them leftwards within the same signed blocks so a rebuilt DAG
  is derivable: every content address moves, the old DAGs fail with
  `proof_dag_leaf_fact_mismatch` under the tampered facts, and the rebuilt
  DAG verifies. A naive shortening tamper was tried first and the rebuild
  was honestly rejected with `retained_object_intervening_operation` (the
  dropped tail reads as an interval operation) — the R1 continuity
  hardening working as intended.
- **Item 5(c)** mocks only the rule-resource digest; the pinned context
  rejects all three DAGs before any node is trusted.
- **Item 8** hands the verifier the signed blocks only: it rebuilds its own
  span resolver and re-extracts every leaf. A resealed char_span forgery
  (all dependent content addresses honestly recomputed) still fails at
  source relocation.

## r9 regression

- Full slice (`reaserch_agent/ chem_agent_contracts/`, minus
  llm_connectivity): **1196 passed, 30 failed** — every failure already on
  the known baseline lists (`../baseline-research-fails.log`,
  `../baseline-contracts-fails.log`, 31 entries); **zero new failures**
  (one baseline failure stays fixed since r8:
  `test_b1_bootstrap_generates_initial_outputs`).
- The new `reaserch_agent/test_route_proof_dag.py` contributes 16 tests
  (build/verify, invalidation, forgery, cycle/future, leaf relocation,
  legacy regression) — all pass.
- The r8 runner re-run produces byte-identical replay/audit JSONs;
  round-2 and round-3A acceptance still PASS on the phase-0 + 3B-1 tree.

## Deliberately NOT done

- **graph[7a]/graph[7b] real blockers untouched**: no protocol inheritance
  and no new rules. graph[7a] (ms7a centrifugation) output and graph[7b]
  (ms7b redispersion) output stay honestly unresolved — the DAG composes
  proofs that exist; it does not manufacture them.
- **Q1 population scope**: typed `parent_ref` v1 allows only
  `material_instance`; `PopulationStateProofV1` is the planned Q1
  extension.
- **`admitted_protocols` stays 0**: G1 admission gating is a separate
  milestone from the Q1 publication gate and is reported as empty, never
  as a pass.
- **3B-1 noted follow-up**: inheritance re-derivation for >2 consecutive
  non-literal hops (the current inheritance node re-derives from its
  parent's evidence; longer non-literal chains need recursive
  re-derivation).
- **O8 deferred**.
