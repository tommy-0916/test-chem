# Field Semantic Gate — Round 3D Safety Closure (2026-10-03)

Round 3D shipped two capability tokens — `_VerifiedParentStateEvidence`
(r10) and `_VerifiedLiquidMedium` (r11) — that discharge a flat-engine
gate only when the engine's own recomputed binding matches the token's
triple exactly.  Until this closure, both classes still had a **public
`__init__`** and **mutable fields**: any importer could construct a token
directly or tamper with one after minting.  This round seals both tokens
to **verifier-only minting with immutable fields**.  No proof class, no
gate logic, no schema, and no r7–r11 acceptance content changes.

## What was sealed

`chem_agent_contracts/route_convention_basis.py`:

- **Module-private mint key** — `_MINT_KEY = object()` at module scope
  (L76), never exported, matched by identity.
- **Sealed constructors** — both token classes now take
  `__init__(self, <fields...>, *, _key=None)` and raise `TypeError`
  unless `_key is _MINT_KEY`
  (`_VerifiedParentStateEvidence` L79–124, `_VerifiedLiquidMedium`
  L126–177).
- **Immutable fields** — `__slots__` gained a `_sealed` slot; the fields
  are set, then `object.__setattr__(self, "_sealed", True)`, and a
  `__setattr__` override raises `AttributeError` once sealed (no
  `__dict__` anywhere).
- **Single construction path** — module-private mint functions,
  underscore-named, absent from `__all__`, docstrings marked
  "proof-DAG verifier/builder layer only":
  `_mint_verified_parent_state(field_path, state_value,
  material_instance_id)` (L180) and
  `_mint_verified_liquid_medium(operation_value, medium,
  definition_digest)` (L194).

Construction-site updates (the complete list, verified by grep):

- `chem_agent_contracts/route_proof_dag.py` — `_verified_parent_token`
  (L202) and `_verified_liquid_token` (L244), the legitimate
  verifier/builder minting path, now call the mint functions.
- `reaserch_agent/test_route_protocol_reference.py` — the
  `EngineTokenTest._token` fixture (L590) mints via
  `_mint_verified_liquid_medium`.
- `result/operation-structure-20260928/local-revision-r10.py` — import
  line + one construction (the what-if diagnostic token) swapped to
  `_mint_verified_parent_state`.
- `result/operation-structure-20260928/local-revision-r11.py` — import
  lines + four constructions (two parent, two liquid what-if tokens)
  swapped to the mint functions.

In both runners **only** the import lines and the constructor call
expressions changed (r10: 2 lines; r11: 6 lines).  No string literal was
touched: the JSON-embedded prose naming
`_VerifiedParentStateEvidence`/`_VerifiedLiquidMedium` stays
byte-identical and remains accurate — the diagnostic objects are still
exactly those token instances, now obtained via the private mint path.

## What did NOT change

- **Engine content checks remain the soundness guard.**  The parent gate
  is still skipped only when the token triple equals the engine's own
  computed binding (else fail closed
  `convention_parent_state_unverified`); the liquid gate still only when
  `token.operation_value` equals the affirmed operation and medium/digest
  are non-empty (else fail closed
  `convention_liquid_participation_missing`).  The sealing is defense in
  depth around those checks, not a replacement: even a sealed token with
  a mismatched triple fails closed, and the public
  `verified_parent_state=`/`verified_liquid_medium=` parameter
  signatures are unchanged.
- **The diagnostic harnesses stay diagnostic.**  r10/r11 still mint their
  what-if tokens (now via the private path, which this module shares with
  them) and remain labeled `diagnostics_only` / `feeds_verdict=false`;
  their verdicts are untouched.
- No proof-class changes, no v1 schema changes, no r7–r11 acceptance
  changes.

## Tests and regression

- **New sealing tests** — `CapabilityTokenSealingTest` in
  `reaserch_agent/test_route_protocol_reference.py` (5 tests): direct
  construction without `_key` → `TypeError` (both classes); wrong key →
  `TypeError` (both classes); field mutation after minting →
  `AttributeError` (both classes, every field including `_sealed`);
  minted tokens carry the exact triple and a correctly-minted liquid
  token still discharges the gate end-to-end.  The forged/mismatched
  fail-closed paths remain covered by the pre-existing `EngineTokenTest`.
- **Target set** (`test_route_proof_dag`,
  `test_route_protocol_reference`, `test_route_retained_object`,
  `test_route_retained_object_integration`,
  `test_route_convention_state_chain`): **171 ran (166 pre-change + 5
  new), 0 failures**.
- **Full slice** (`reaserch_agent/` + `chem_agent_contracts/`,
  `test_llm_connectivity` excluded, unittest discover): **1241 ran, 30
  failed** — every failure is baseline-listed
  (`baseline-research-fails.log` 30 entries, of which
  `test_b1_bootstrap_generates_initial_outputs` passes in the working
  tree as before; `baseline-contracts-fails.log` 1 entry).  **Zero new
  failures.**
- **Runner reruns** — all six runners re-run after the change: r7, r8,
  r9, r10, r11 exit with top-level `status: PASS` (the single
  premise-level `liquid_participation: FAIL` string inside the r10/r11
  rule-local probes is pre-existing diagnostic content), and every
  tracked replay/audit/proposal JSON is **byte-identical** (md5-verified
  pre/post rerun; `git status`/`git diff` on `result/` show only the two
  intended runner `.py` edits).

Round 3E (feasibility study) is next and is intentionally not part of
this closure.
