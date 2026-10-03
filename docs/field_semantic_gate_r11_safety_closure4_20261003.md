# Round 3D safety closure 4 — verified mint context restricted to real build/verify flows

Date: 2026-10-03.  Commit: `Restrict verified mint context to real build/verify flows (Round 3D safety closure)`.
Predecessors (historical, unchanged): `field_semantic_gate_r11_safety_closure_20261003.md` (item 1, field deletion), `field_semantic_gate_r11_safety_closure2_20261003.md` (item 3, diagnostic-artifact rejection), `field_semantic_gate_r11_safety_closure3_20261003.md` (item 2, minting bound to verified snapshots).  This document closes the LAST remaining point of Round 3D acceptance **item 2** (verified minting bound to verified content), which the owner FAILED again on acceptance round 3 — not on the sealing mechanics (window revocation, snapshot isolation, node/dependency rewrite checks all PASS), but on the test entries themselves.

## 1. The finding — shipped test hooks ARE a verification-free minting path

Owner + independent review, dynamically confirmed against HEAD `902f27c`:

- `StateProofDagVerifier._install_mint_context_for_test`
  (`chem_agent_contracts/route_proof_dag.py`, pre-fix ~L1393) accepted
  caller nodes and filled the memo with `""` by default — **no
  verification performed**.  Any caller could install a "verified"
  context by hand and then mint.  The independent review's dynamic
  verdicts, verbatim: `verify_called: false` → mint through this entry →
  a genuine `_VerifiedLiquidMedium` → the flat derive passes
  (`rule_id='REDISPERSION_V1'`) where the honest issue was
  `convention_liquid_participation_missing`.  The closure-3 sealing
  (snapshot, digests, memo, closure) checked the INSTALLED context
  faithfully — but the installation itself required no verification, so
  the whole guarantee could be bypassed through a shipped method.
- `_DagBuilder._mint_window_for_test` (pre-fix ~L638) was the same class
  of problem: it opened the builder's minting window with no build in
  progress, again from production code, callable by anyone.

In short: the white-box TEST HOOKS shipped in the production module were
themselves a verification-free minting path.  Item 2 cannot be called
closed while a production attribute named `*_for_test` does what only
`build()`/`verify()` may do.

## 2. The fix

**Production (`chem_agent_contracts/route_proof_dag.py`)**:

- `_install_mint_context_for_test` and `_mint_window_for_test` are
  REMOVED entirely, together with the now-unused `contextlib.contextmanager`
  / `typing.Iterator` imports and the two comments that referenced the
  "white-box test hook".  A package-wide grep for
  `for_test|_test_only|white-box|test hook` over
  `chem_agent_contracts/` returns nothing.
- The production verified context may now be established ONLY by the
  actual build/verify flow: the builder window exists only while
  `build()`/`build_node()` executes (`_mint_depth`, L634), the verifier
  context only while `verify()` runs (install block inside `verify()`,
  revoked by `_clear_mint_context()`, L1369, in the `finally`).

**What did NOT change** (per the owner's prescribed scope): the snapshot
isolation (`build()` deep copy; `verify()` deep-copied `_active_nodes`
plus recorded `_active_digests`), the dependency-closure check
(`_mint_closure` + caller-side digest re-check via `_active_source`), the
mint windows themselves, the `finally` cleanup, the memo gate, the
extraction-from-snapshot rule, the engine content checks, the diagnostic
channel, the proof classes, and every gate verdict.

**Test module (`reaserch_agent/test_route_protocol_reference.py`)** — the
verification-free installation logic moved HERE, as test-module-local
helpers (never production):

- `_builder_mint_window(builder)` (L865) — a test-file-local context
  manager that pokes the builder's private `_mint_depth` counter from
  test code, holding the window open around a post-build mint and
  restoring it in a `finally`.  Test-side stand-in for the removed
  production hook.
- `_KeepContextVerifier(StateProofDagVerifier)` (L882) — a test-file-local
  subclass that neutralizes `_clear_mint_context` so the context a REAL
  `verify()` run installs (deep-copied snapshot, recorded digests, the
  genuine per-node memo, the window-scoped caller-table reference)
  survives `verify()`'s return.  Every "verified" context in the tests
  below is therefore genuinely verification-produced — no synthetic
  install anywhere.  `release_mint_context()` (L899) performs the real
  clear (revocation).  Mixin factory `_keeper_for` (L609).

## 3. Reworked tests (semantic coverage preserved in every case)

- `CapabilityTokenSealingTest._verified_parent` / `_verified_liquid` —
  builder mints wrapped in `_builder_mint_window(builder)`.
- `CapabilityTokenSealingTest.test_structurally_minted_tokens_discharge_gates_end_to_end`
  — same; the engine-discharge assertions are unchanged.
- `StructuralMintingTest.test_builder_mint_rejects_unregistered_or_tampered_nodes`
  — wrapped in `_builder_mint_window(builder)` so the refusals still come
  from the registration/integrity checks, not the closed window.
- `StructuralMintingTest.test_verifier_mint_requires_a_clean_memo_entry` —
  the pre-verify / failed-verify / post-verify revocation checks keep
  using a plain verifier; the in-context section now runs on a
  `_KeepContextVerifier` whose context comes from a REAL clean
  `verify(dag)`, with the memo-tampering cases (`"proof_dag_node_mismatch"`,
  deleted entry, restored entry), the foreign-node case, and the
  tampered-content case poked from test code as before;
  `release_mint_context()` replaces the hook's exit-revocation
  assertion.
- `StructuralMintingTest.test_builder_and_verifier_minted_triples_agree` —
  builder mints inside `_builder_mint_window(builder)`; verifier mints
  from the keeper's real post-`verify(dag)` context; the triple-agreement
  assertions are unchanged.
- `VerifiedSnapshotMintingTest.test_post_verification_target_mutation_never_mints`
  — Layer-2 revocation checks unchanged (plain verifier); the Layer-3
  in-window digest-mismatch check now runs on a keeper that verified the
  still-pristine DAG for real BEFORE the in-place mutation — exactly the
  owner's scenario (mutate after verify → mint rejects), with no
  synthetic context.
- `VerifiedSnapshotMintingTest.test_dependency_mutation_never_mints` —
  the keeper verifies the pristine DAG for real; the dependency is
  mutated in place afterwards; the transitive-premise-closure check
  catches the moved caller-side ancestor (`ValueError`); the builder
  in-window mint of the untouched target still carries the ORIGINAL
  verified triple (`deionized water`).
- `VerifiedSnapshotMintingTest.test_unmodified_proof_mints_normally_in_window`
  — deep-copy/aliasing assertions unchanged; builder mints inside
  `_builder_mint_window(builder)`; verifier mints from the keeper's real
  verification context; agreement assertions unchanged.
- `DiagnosticChannelTest.test_proof_layer_exact_type_guards_reject_assumptions`
  — builder mints wrapped in `_builder_mint_window(builder)`; exact-type
  guard assertions unchanged.

## 4. New regression tests for THIS finding (`ShippedTestHookAuditTest`)

- `test_removed_test_hooks_are_gone_from_the_shipped_api` —
  `not hasattr(StateProofDagVerifier, "_install_mint_context_for_test")`
  and `not hasattr(_DagBuilder, "_mint_window_for_test")`.
- `test_no_for_test_or_context_installer_names_shipped` — inspect-level
  audit of `route_proof_dag` and `route_convention_basis`: no attribute
  name, public or private, on the module or on any class it defines, may
  match `for_test|_test_only|install.*context` (case-insensitive).
- `test_no_test_hook_defs_shipped_anywhere_in_the_package` — source-level
  audit across every non-test module of `chem_agent_contracts/`: no `def`
  whose name carries `for_test` / `_test_only` may ship.  (The
  pre-fix grep `grep -rn "_for_test\|for_test\|_test_only"
  chem_agent_contracts/` matched ONLY the two removed hooks; nothing else
  existed to move.)
- `test_a_verifier_that_never_verified_cannot_mint` — the owner's
  scenario as a dynamic negative: a verifier that never ran `verify()`
  and a builder whose `build()` already returned both refuse every mint
  (`ValueError`, window closed); the removed attack entries raise
  `AttributeError`; and the ONLY route to a verified token is a real
  `verify()` run (asserted positively via the keeper, then refused again
  after `release_mint_context()`).

## 5. Regression

- **Target set** (test_route_proof_dag + test_route_protocol_reference +
  test_route_retained_object + test_route_retained_object_integration +
  test_route_convention_state_chain): **188 ran** (184 pre-change + 4
  new), **0 failures**.
- **Full slice** (`reaserch_agent/` + `chem_agent_contracts/`, minus
  test_llm_connectivity): **1258 ran** (1254 reference + 4 new), **30
  failed**, all baseline-listed after
  `sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/'` normalization:
  the 30 match `baseline-research-fails.log` minus
  `test_b1_bootstrap_generates_initial_outputs` (passes in the working
  tree) plus `baseline-contracts-fails.log` (1) exactly — `diff` empty,
  **zero NEW failures**.
- **Runner reruns** (runners untouched by this change): r7, r8, r9, r10,
  r11 re-run, plus r11 a second time for determinism — all exit with
  top-level `status: PASS` (the single premise-level
  `liquid_participation: FAIL` string in the r10/r11 ms7b dual result is
  the documented historical 3C content); the two r11 runs are
  md5-identical; **md5 snapshots of all 370 tracked `result/` files are
  identical pre/post rerun and `git status`/`git diff` on `result/` are
  EMPTY** — every tracked replay/audit/proposal JSON is byte-identical
  (this change touches no runner and no DAG output path).

## 6. Scope discipline

No changes to the sealing mechanics themselves (snapshot/window/closure/
`finally` stay exactly as closure 3 shipped them), no gate-logic changes,
no diagnostic-channel changes, no proof-class changes, no 3E
implementation, no runner edits, no edits to historical closure docs 1–3.
The only production diff is the removal of the two test hooks and their
now-dead imports/comments; everything else moved into the test module.
