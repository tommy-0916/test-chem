# Field Semantic Gate — Round 3D Safety Closure 2 (2026-10-03)

The first safety closure (`field_semantic_gate_r11_safety_closure_20261003.md`,
historical, unmodified) sealed the two capability tokens to "verifier-only
minting" with immutable fields.  Independent review reproduced TWO holes
in that closure; this second closure hardens the sealing against both.
No gate-logic changes, no proof-class changes, no schema changes, and no
r7–r11 acceptance content changes.

## The two reproduced review findings

**Finding 1 — deletion bypass.**  Only `__setattr__` was intercepted;
`__delattr__` was not overridden.  Because `__slots__` includes
`_sealed`, deleting it reopened mutation:

```python
token = <verified liquid token>   # minted via the private mint
del token._sealed                 # NOT intercepted — succeeded
token.medium = "changed"          # now succeeds: token tampered
```

The same held for `_VerifiedParentStateEvidence.state_value` etc.
"Immutable" covered assignment but not deletion.

**Finding 2 — bare-string minting.**  `_mint_verified_parent_state` and
`_mint_verified_liquid_medium` accepted bare strings with no verification
proof or context — "verifier-only" was only a calling convention.  With
NO DAG build/verify at all:

```python
# liquid half: arbitrary medium + non-empty fake digest
token = _mint_verified_liquid_medium(
    operation_value=<step operation>,       # the one checked field
    medium="anything non-empty",
    definition_digest="sha256_" + "f" * 64, # fabricated
)
derive_unreviewed_output_state(..., REF_OUT_STATE,
                               verified_liquid_medium=token)
# → issue="" and rule REDISPERSION_V1 → "suspension" instead of
#   convention_liquid_participation_missing
```

and likewise a parent token minted with matching fields after stripping
the valid parent citation made the flat derive pass instead of failing
closed with `convention_parent_state_unverified`.  (The engine's content
checks still gate *what* the token can claim — operation binding equal,
fields non-empty, triple equal to the engine's own computed binding —
but nothing gate-kept WHO could mint a "verified" token.)

## The hardening design

### Immutable means immutable

All four classes (the two verified tokens and the two new diagnostic
assumptions) now override `__delattr__` to raise `AttributeError`
UNCONDITIONALLY — nothing may ever delete a token field, including
`_sealed` (`route_convention_basis.py` L131, L198, L249, L297).  The
existing `__setattr__`-once-sealed behavior is unchanged.

### Verified minting is now STRUCTURAL

`_mint_verified_parent_state` and `_mint_verified_liquid_medium` are
REMOVED from `route_convention_basis` — no bare-string verified-mint API
remains anywhere.  `_MINT_KEY` stays module-private (L77), and the only
verified-token construction happens in `route_proof_dag` via
`_basis._VerifiedParentStateEvidence(..., _key=_basis._MINT_KEY)` /
`_basis._VerifiedLiquidMedium(..., _key=_basis._MINT_KEY)`, inside two
private helpers that take NO caller-supplied value strings:

- `_mint_verified_parent_token(host, node)` (L259) and
  `_mint_verified_liquid_token(host, node)` (L324) extract the triple
  FROM THE NODE (parent: `claim.field_path` / `claim.target_state` /
  `claim.material_instance_id`; liquid: the protocol_reference node's
  `operation_value` / `liquid_medium` / `definition_digest`).
- `_structural_mint_binding(host, node)` (L203) binds the mint to the
  host: `host` must be a `_DagBuilder` or `StateProofDagVerifier`
  instance (else `TypeError`).  For a builder, `node["node_id"]` must be
  in the builder's own node table with identical content (else
  `ValueError`).  For a verifier, the node must be in the DAG currently
  under verification with identical content AND must already have
  verified clean — `verify()` installs `self._active_nodes` /
  `self._active_memo` once the structural checks pass (L1231–L1232,
  L1270–L1271; initialized in `__init__` L1189–L1190), and the memo
  entry for `node["node_id"]` must be `""` (else `ValueError`).
  Premises verify before the nodes standing on them, so the memo entry
  is already clean at every legitimate mint inside `verify()`.
- The four existing call sites go through the structural helpers with
  `(self, premise_node)`: builder `_build_output_state` (parent L892,
  liquid L945), builder `_build_input_state` (grandparent L1060),
  verifier `_verify_convention` (parent L1675, liquid L1659), verifier
  `_verify_inheritance` (grandparent L1797).

### Proof layer rejects diagnostic artifacts

The flat engine is also the consumption point of the r10/r11 what-if
diagnostic harnesses, which legitimately pose assumptions WITHOUT
verification — that is what a what-if is.  The honest channel:

- New underscore-private classes `_DiagnosticParentStateAssumption`
  (L206) and `_DiagnosticLiquidMediumAssumption` (L256) — same field
  triples, same sealing (`__setattr__` + `__delattr__`), absent from
  `__all__`, docstringed "diagnostic what-if assumption; never a proof
  artifact; never produced or consumed by the proof-DAG layer".
- New module-private mints
  `_mint_diagnostic_parent_state_assumption(...)` (L304) and
  `_mint_diagnostic_liquid_medium_assumption(...)` (L322) take bare
  strings BY DESIGN — this is the labeled assumption channel.
- Engine (`_proof_for_evidence`): the parent gate accepts
  `isinstance(verified_parent_state, (_VerifiedParentStateEvidence,
  _DiagnosticParentStateAssumption))` (L903) and the liquid gate the two
  liquid types (L1068).  **Content checks are UNCHANGED** and identical
  for both accepted types; no other engine change.
- Proof-layer rejection: every internal derive call site in
  `route_proof_dag` that passes a token is guarded by an exact-type
  assertion — `_assert_verified_parent_token` (L281) /
  `_assert_verified_liquid_token` (L346) require
  `type(token) is _basis._VerifiedParentStateEvidence` /
  `_VerifiedLiquidMedium` (`None` passes: it is the legacy literal-gate
  default), else `TypeError`.  Guarded sites: L911, L942/L944 (builder
  output), L1081 (builder input), L1674/L1676 (verifier convention),
  L1817 (verifier inheritance).  The structural mints only ever produce
  the verified types, so the guard fires only on tampering — e.g. a
  diagnostic assumption smuggled into the proof consumption path.

## Tests — the three acceptance behaviors

`reaserch_agent/test_route_protocol_reference.py`:

1. **Field deletion** — `CapabilityTokenSealingTest.
   test_token_fields_cannot_be_deleted` (L931): `del` on every field
   including `_sealed` raises `AttributeError` on all four classes; a
   mutation attempt after a failed deletion still raises; field values
   unchanged.  Sealed-construction and setattr immutability coverage
   extended to the diagnostic classes
   (`test_direct_construction_without_key_raises`,
   `test_direct_construction_with_wrong_key_raises`,
   `test_minted_parent_token_fields_are_immutable`,
   `test_minted_liquid_token_fields_are_immutable`); verified tokens in
   these tests are minted structurally from a real builder, and
   `test_structurally_minted_tokens_discharge_gates_end_to_end` pins the
   end-to-end discharge through the structural path.
2. **Unverified minting** — `StructuralMintingTest` (L981):
   `test_bare_string_verified_mint_api_is_gone` (both names absent from
   the module), `test_structural_mints_reject_a_non_host` (`TypeError`),
   `test_builder_mint_rejects_unregistered_or_tampered_nodes`
   (`ValueError`), `test_verifier_mint_requires_a_clean_memo_entry`
   (no context / failed verification / non-clean memo entry / foreign or
   tampered node all `ValueError`),
   `test_builder_and_verifier_minted_triples_agree`.
3. **Diagnostic-artifact rejection** — `DiagnosticChannelTest` (L1101):
   `test_liquid_assumption_channel_is_honestly_typed` (the owner's
   experiment, honestly channeled: without assumption →
   `convention_liquid_participation_missing`; with an arbitrary medium +
   fake non-empty digest → derive passes REDISPERSION_V1 → suspension,
   and the object is not an instance of the verified liquid type; a
   wrong operation binding still fails closed);
   `test_parent_assumption_channel_with_stripped_citation` (stripped
   citation → `convention_parent_state_unverified`; parent assumption
   discharges exactly the parent gate — the liquid gate still fails
   closed alone — and both assumptions together make the derive pass;
   mismatched triple fails closed; type-distinct from verified);
   `test_proof_layer_exact_type_guards_reject_assumptions` (both guards
   raise `TypeError` on both diagnostic types, cross-wired too; `None`
   and genuine verified tokens pass);
   `test_public_entrypoints_expose_no_token_parameters`
   (`inspect.signature` audit of `build_state_proof_dag`,
   `verify_state_proof_dag`, `StateProofDagVerifier.verify`).

The pre-existing `EngineTokenTest` (engine fail-closed content checks
with arbitrary triples) now poses its triples through the diagnostic
mint — the engine behavior under test is unchanged.

## Runners — prose-naming note

r10 and r11 switched their imports and constructor call expressions to
the diagnostic mints (r10: 1 import + 1 construction; r11: 2 imports +
4 constructions).  **Only** those lines changed; every string literal
stays byte-identical.  In particular the replay/audit prose keeps
naming the historical class names `_VerifiedParentStateEvidence` /
`_VerifiedLiquidMedium` — that prose is committed historical content —
while the objects the harnesses now hold are honestly typed
`_DiagnosticParentStateAssumption` / `_DiagnosticLiquidMediumAssumption`
instances.  The engine's content checks are identical for both types, so
every diagnostic result is unchanged.

## Regression

- **Target set** (`test_route_proof_dag`, `test_route_protocol_reference`,
  `test_route_retained_object`, `test_route_retained_object_integration`,
  `test_route_convention_state_chain`): **181 ran (171 pre-change + 10
  new), 0 failures** (protocol-reference module: 42 = 32 pre-change
  reworked + 10 new).
- **Full slice** (`reaserch_agent/` + `chem_agent_contracts/`,
  `test_llm_connectivity` contributes no collected tests, unittest
  discover): **1251 ran, 30 failed** — every failure is baseline-listed
  (`baseline-research-fails.log` 30 entries, of which
  `test_b1_bootstrap_generates_initial_outputs` passes in the working
  tree as before; `baseline-contracts-fails.log` 1 entry, exact match).
  Failure lists diffed after
  `sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/'`
  normalization.  **Zero new failures.**
- **Runner reruns** — all runners re-run after the change (r11 twice):
  r7, r8, r9, r10, r11 acceptance items exit PASS (the single
  premise-level `liquid_participation: FAIL` string inside the r10/r11
  rule-local probes is pre-existing diagnostic content), every tracked
  replay/audit/proposal JSON is **byte-identical** (md5-verified
  pre/post rerun; runner stdout byte-identical pre/post change;
  `git status`/`git diff` on `result/` show only the two intended
  runner `.py` edits), and the two r11 runs are byte-identical to each
  other.

Round 3E remains a paper feasibility study
(`field_semantic_gate_3e_design_20261003.md`), intentionally not part of
this closure.
