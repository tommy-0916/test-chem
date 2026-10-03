# Round 3D safety closure 3 — verified minting bound to verified node and dependency snapshots

Date: 2026-10-03.  Commit: `Bind capability minting to verified node and dependency snapshots (Round 3D safety closure)`.
Predecessors (historical, unchanged): `field_semantic_gate_r11_safety_closure_20261003.md` (item 1, field deletion), `field_semantic_gate_r11_safety_closure2_20261003.md` (item 3, diagnostic-artifact rejection).  This document closes Round 3D acceptance **item 2** (verified minting must be bound to verified content), which the owner FAILED with a reproduced hole.

## 1. The reproduced hole

Reproduced pre-fix against HEAD `26af9d8` (owner + independent reviewer reproduction, independently re-run here; script discarded after the run, verdicts verbatim):

```
== Step 1: normal build + successful verify(dag) ==
build issue: ''  verify verdict: ''  (both clean)
target protocol_reference node_id: proof_node_ae01a7ed307d6b0ae1547e61
before mutation: liquid_medium='deionized water'

== Step 2: caller mutates IN PLACE liquid_medium / definition_digest
   on the caller-held DAG node (node_id untouched) ==
after mutation:  liquid_medium='liquid nitrogen'
                 definition_digest='sha256_ffff...ffff'
                 node_id (untouched): proof_node_ae01a7ed307d6b0ae1547e61

== Step 3: mint via BOTH hosts accepts the mutated values ==
BUILDER mint ACCEPTED the mutated node:
  token.operation_value    = 'second redispersion protocol'
  token.medium             = 'liquid nitrogen'
  token.definition_digest  = 'sha256_ffff...ffff'
VERIFIER mint ACCEPTED the mutated node:
  token.medium             = 'liquid nitrogen'
  token.definition_digest  = 'sha256_ffff...ffff'

== flat derive with the builder-minted mutated token ==
flat derive: issue='' rule_id='REDISPERSION_V1'

== full re-verify of the mutated DAG ==
re-verify verdict: 'proof_dag_node_id_mismatch'

== Variant B: target untouched, only its definition-evidence
   DEPENDENCY mutated -> mint still succeeds ==
VERIFIER mint on untouched target STILL ACCEPTED: medium='deionized water'
re-verify of that DAG: 'proof_dag_node_id_mismatch'
```

### Mechanism (confirmed by reading the pre-fix code)

1. **Shared mutable references, builder side** — pre-fix `_DagBuilder.build()`
   (old `route_proof_dag.py` ~L553) returned `"nodes": self.nodes`: the
   caller-held DAG *was* the builder's registry.  Mutating the returned
   DAG mutated the registry, so the builder-side membership/content check
   compared the node against itself.
2. **Shared mutable references, verifier side** — pre-fix
   `StateProofDagVerifier.verify()` (old ~L1270) stored the caller's node
   table BY REFERENCE in `self._active_nodes`.  Same consequence.
3. **Stale memo** — pre-fix `_structural_mint_binding` (old ~L230)
   compared the passed node against the host table — but both sides were
   the same shared object, so they changed together, and the memo entry
   still recorded the OLD content as verified clean (`""`).  The mint
   then extracted the (mutated) triple from the passed object itself.
4. **Dependency blind spot** — variant B: with the target untouched and
   only its definition-evidence dependency mutated in place, the mint
   still succeeded.  A hash check on the target node alone is NOT
   sufficient; the transitive premise closure must be covered.

## 2. The owner's invariant (design target, verbatim)

> 铸造依据必须是 host 持有的、已验证且不会随调用方修改而变化的节点及依赖快照。可采用独立不可变快照，或把铸造严格限制在有效验证期间并撤销过期上下文。

Implemented as **both**: an independent deep-copied snapshot AND a strict
minting window that revokes the context when verification/build returns.

## 3. The three layers (all in `chem_agent_contracts/route_proof_dag.py`)

### Layer 1 — no shared mutable references

- **Builder**: `build()` (L681) returns `"nodes": deepcopy(self.nodes)`
  (L693).  The caller-held DAG never aliases the builder's registry;
  mutating the returned DAG cannot move what a later mint would extract.
- **Verifier**: `verify()` (L1419) deep-copies the incoming node table
  into `self._active_nodes` at install time and records
  `self._active_digests = {node_id: node_id_for(node)}` computed from the
  ORIGINAL passed nodes (install block inside `verify()`, L1478–L1492).

### Layer 2 — minting window

- **Verifier**: `_active_nodes` / `_active_memo` / `_active_digests` /
  `_active_source` are installed only while `verify()` runs and cleared
  in a `finally` on exit (`_clear_mint_context()`, L1386).  Any mint
  attempt before `verify()` starts or after it returns raises
  `ValueError`.  Failed verifications install nothing (the context is
  cleared at entry and the early returns happen before install).
- **Builder**: `_mint_depth` counter (L635), incremented at
  `build()`/`build_node()` entry and decremented in a `finally`
  (L681–L737); the structural mint requires `_mint_depth > 0`.  The
  owner's reproduction (builder mint AFTER build returned) now raises.
  Nested `build_node()` recursion is handled by the counter, not a flag.

### Layer 3 — extract from the snapshot, never from the caller's object

`_structural_mint_binding` (L260) now:

1. **Window check first** (per host, as above) — closed window →
   `ValueError`; a non-host still → `TypeError`.
2. **Identification + integrity**: the passed node is used ONLY for its
   `node_id` field and for an integrity check — `node_id_for(passed)` must
   equal the digest recorded for that id (verifier: `_active_digests`;
   builder: the registry key itself, which IS the honestly recomputed
   content address).  A mutated-in-place target fails here even though
   its `node_id` field was left untouched.
3. **Memo**: the verifier-side target node's memo entry must still be
   `""` (verified clean this run).  Unchanged from closure 2.
4. **Dependency closure** (`_mint_closure`, L227): walk the passed node's
   transitive premise closure via premise `node_id`s over the SNAPSHOT
   (missing premise id in the snapshot → `ValueError`).  For every
   ancestor: (a) the snapshot copy's own recomputed digest must equal the
   recorded one (host-side tamper guard), and (b) where the caller's side
   of the table is reachable — the verifier retains the caller's table as
   `_active_source` for the window duration ONLY — the caller-side
   ancestor's recomputed digest must equal the snapshot digest.  Any
   mismatch → `ValueError`.  This is what covers "target unchanged,
   dependency mutated".  For the builder the caller's table is not
   reachable, so (b) degenerates to the target check already done in
   step 2 — sound, because the builder mints exclusively from its own
   registry snapshot (Layer 1 guarantees the caller cannot have moved
   it): an in-window builder mint of an untouched target yields the
   ORIGINAL verified triple no matter what the caller did to their copy.
5. **Extraction**: the minted triple is extracted from the host-held
   snapshot copy (`_active_nodes` deep copy on the verifier; the registry
   on the builder) — the function returns `snapshot[node_id]`, never the
   passed object.

White-box TEST hooks (documented as such, used only by tests):
`_DagBuilder._mint_window_for_test()` (L638) opens the builder window by
hand; `StateProofDagVerifier._install_mint_context_for_test(nodes, memo)`
(L1393) installs exactly what `verify()` installs (deep-copied snapshot,
recorded digests, memo, window-scoped caller-table reference) and clears
it on exit.

## 4. Post-fix verdicts on the reproduction

```
POST-FIX builder mint: REJECTED -> ValueError: verified mint refused: the builder's minting window is closed — minting is valid only while build()/build_node() is executing
POST-FIX verifier mint: REJECTED -> ValueError: verified mint refused: no verification is in progress — the minting window is open only while verify() executes
POST-FIX re-verify mutated DAG: 'proof_dag_node_id_mismatch'
POST-FIX dependency-mutation mint: REJECTED -> ValueError (window revoked)
POST-FIX re-verify dependency-mutated DAG: 'proof_dag_node_id_mismatch'
```

## 5. Acceptance tests (new, in `reaserch_agent/test_route_protocol_reference.py`, class `VerifiedSnapshotMintingTest`)

1. `test_post_verification_target_mutation_never_mints` — after
   build+verify, post-window mint attempts on BOTH hosts raise
   `ValueError` even for the untouched node (the owner's reproduction
   path); after in-place mutation of `liquid_medium`/`definition_digest`,
   white-box in-window mints on both hosts raise `ValueError` on the
   digest mismatch; a full re-verify reports
   `proof_dag_node_id_mismatch`.  PASS.
2. `test_dependency_mutation_never_mints` — target untouched, the
   definition-evidence dependency mutated in place: white-box in-window
   verifier mint raises `ValueError` (closure check catches the moved
   caller-side ancestor while the target still digest-matches); the
   builder's in-window mint of the untouched target still carries the
   ORIGINAL verified triple (`deionized water`), proving extraction from
   the snapshot.  PASS.
3. `test_unmodified_proof_mints_normally_in_window` — `build()` returns a
   deep copy (`is not` + equality + per-node identity asserted);
   production in-window minting keeps `verify()` passing; white-box
   builder/verifier mints succeed and agree on both triples;
   caller-side edits of the returned DAG cannot reach the registry.
   PASS.

Adapted existing tests (all in the same file; reason: they minted tokens
AFTER `build()`/`verify()` returned, which the Layer-2 window now —
correctly — refuses; each now mints inside the documented white-box
window/context hook, so the tested semantics are unchanged):

- `CapabilityTokenSealingTest._verified_parent` / `_verified_liquid`
  (token fixtures) — builder mints wrapped in `_mint_window_for_test()`.
- `test_structurally_minted_tokens_discharge_gates_end_to_end` — same.
- `test_builder_mint_rejects_unregistered_or_tampered_nodes` — wrapped so
  the refusals come from the registration/integrity checks, not the
  closed window.
- `test_verifier_mint_requires_a_clean_memo_entry` — the post-verify
  mint now asserts REJECTION (window revoked); the memo-tampering cases
  run inside `_install_mint_context_for_test(...)`; an exit-revocation
  assertion was added.
- `test_builder_and_verifier_minted_triples_agree` — mints inside the
  respective white-box windows.
- `DiagnosticChannelTest.test_proof_layer_exact_type_guards_reject_assumptions`
  — builder mints wrapped in `_mint_window_for_test()`.

All closure-1/closure-2 sealing tests (deletion, non-host, unregistered,
diagnostic-channel rejection, signature audit) stay green.

## 6. Regression

- **Target set** (test_route_proof_dag + test_route_protocol_reference +
  test_route_retained_object + test_route_retained_object_integration +
  test_route_convention_state_chain): **184 ran** (181 pre-change + 3
  new), **0 failures**.
- **Full slice** (`reaserch_agent/` + `chem_agent_contracts/`, minus
  test_llm_connectivity): **1254 ran** (1251 reference + 3 new), **30
  failed**, all baseline-listed after
  `sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/'` normalization:
  the 30 match `baseline-research-fails.log` minus
  `test_b1_bootstrap_generates_initial_outputs` (passes in the working
  tree) plus `baseline-contracts-fails.log` (1) exactly — `diff` empty,
  **zero NEW failures**.
- **Runner reruns** (runners untouched by this change): r7, r8, r9, r10,
  r11 re-run, plus r11 a second time for determinism — acceptance items
  PASS; the single premise-level `liquid_participation: FAIL` string in
  the r10/r11 ms7b dual result is the documented historical 3C content;
  the two r11 runs are md5-identical; **`git status`/`git diff` on
  `result/` is EMPTY — every tracked replay/audit/proposal JSON is
  byte-identical** (the deep copies do not alter serialized DAG content).

## 7. Scope discipline

No gate-logic changes, no diagnostic-channel changes, no proof-class
changes, no 3E implementation, no runner edits, no edits to historical
closure docs 1–2.  The reproduction script was scratch and is not
committed; its verbatim output lives in sections 1 and 4.
