# Field Semantic Gate — Round-11 (Round 3D) Checkpoint (2026-10-03)

Round 3D adds exactly one narrowly-scoped proof class — a **protocol
reference** certifying that a step's affirmed operation references a
protocol defined earlier in the SAME experimental group, inheriting only
the definition's operation sequence and liquid medium — and runs it over
the real signed NiFe Control group on the **unchanged** round-10
proposal (read-only). The A01 acceptance row that changes is a single
independent diagnostic: ms7b's liquid participation goes FAIL → PASS
(medium = deionized water, inherited from the protocol definition).
Every verdict stays r10's: ms7a.out remains the retained-object evidence
gap, the cascades remain cascades, and ms7b.out's honest engine what-if
remains BLOCKED.

Artifacts: `result/operation-structure-20260928/local-revision-r11.py`
(runner), `local-revision-r11-replay.json` (replay),
`local-revision-r11-audit.json` (audit, schema
`bounded_local_revision/v11`). New/changed contract code:
`chem_agent_contracts/route_protocol_reference.py` (new),
`route_convention_basis.py` (`_VerifiedLiquidMedium` token + liquid-gate
hook), `route_proof_dag.py` (`protocol_reference` node, `liquid_medium`
premise role + binding invariant, retained-object scope guard). New
tests: `reaserch_agent/test_route_protocol_reference.py` (27). The r7/r8
runners re-run **byte-identical**; the r9/r10 runners re-run with all
acceptance items PASS and **byte-identical** replay/audit JSONs — the
committed r10 ms7b liquid FAIL string is historical 3C content and is
not touched.

## The owner's ruling

A paper may DEFINE a named protocol once — *"a centrifugation−redispersion
protocol using deionized water three times, which was the first
centrifugation−redispersion protocol"* — and later REFERENCE it by name
and ordinal — *"after a second centrifugation−redispersion protocol one
time"*. The ruling splits every phrase into three buckets:

- **Definition slots** — only `operation_sequence` (ordered
  `segment_identity`/`segment_order` pairs decomposed from the compound
  protocol name, the same decomposition the typed graph uses for
  graph[5]'s `ms5-seg-centrifugation`/`ms5-seg-redispersion`) and
  `liquid_medium` (the medium named by the `using` clause).
- **Invocation-local parameters** — execution counts ("three times" on
  the definition's own invocation, "one time" on the second) are
  recorded ONLY as annotations (`definition_execution_count` /
  `reference_execution_count`) on the resolution record. The definition
  record carries NO execution slot anywhere; the second invocation's
  count = 1 is a LOCAL value, never an override of anything on the
  definition.
- **Invocation results** — retained objects, output states, material
  identities, inter-segment flows are NEVER inherited. A protocol
  reference must never reintroduce "first run produced X ⇒ second run
  produces X"; the precipitate stays a first-invocation result.

**Syntactic-scope rationale.** The medium slot is the phrase the `using`
clause GOVERNS syntactically — it must modify the protocol mention
itself ("a centrifugation−redispersion protocol using deionized water").
A nearer medium phrase that modifies a different operation ("dispersed
in 30 mL of water and aged" governs the dispersing, not the protocol) is
never a candidate, however close it sits to the reference.

## The narrow slot boundary (schema-enforced)

`protocol-definition/v1` is a CLOSED slot set
(`validate_protocol_definition` rejects any key outside it with
`protocol_definition_unknown_key`): `schema_version`, `protocol_name`,
`operation_sequence`, `liquid_medium`, plus provenance annotations
(definition evidence id, ordinal anchor, paper/group scope, source
digest, locator, char span). Ten forbidden result/object slots are named
and each is rejected individually by test: `retained_object`,
`inter_segment_material_flow`, `output_state`,
`material_instance_identity`, `execution_count`,
`segment_input_material`, `segment_output_material`, `retained_phase`,
`inter_segment_flow`, `derived_state_transition`. The boundary lives in
the schema, not in docs (`DefinitionExtractionTest`, `ClosedSchemaTest`,
`EngineTokenTest`, `ProtocolReferenceDagTest` — 27 tests).

## Resolution rules v1

`resolve_protocol_reference` (`PROTOCOL_REFERENCE_V1`, version 1.0.0),
in order:

1. the reference itself must parse (ordinal + protocol name), else
   `protocol_reference_not_present`;
2. **same-group scope only** (paper id + group id + source digest): a
   name-matched definition existing only outside the pinned scope fails
   `protocol_reference_scope_mismatch`;
3. only mentions that EXTRACT as definitions count (governed `using`
   clause + `which was the <ordinal> <same name>` anchor);
4. normalized protocol-name equality (U+2212/dash variants, case,
   whitespace);
5. the definition must textually **precede** the reference (projected
   character offsets), else
   `protocol_reference_definition_after_reference`;
6. facts quoting the SAME mention dedupe to one (representative binding
   prefers the step operation fact); zero mentions →
   `protocol_reference_unresolved`, ≥2 distinct mentions →
   `protocol_reference_ambiguous`;
7. the reference ordinal must strictly follow the definition's anchor
   ordinal, else `protocol_reference_ordinal_mismatch`.

No proximity heuristics anywhere. The resolution record
(`protocol-reference-proof/v1`) is explicit and traceable: definition
evidence id + reference evidence id + both ordinals + both
invocation-local counts + group scope + the content digest of the
definition it stands on.

## DAG + engine wiring

`route_proof_dag` gains the `protocol_reference` node type (premises
`definition_evidence` + `reference_evidence`, both paper literals; claim
bound to the step's operation path with EMPTY state/material ids). The
dependent convention node may carry a `liquid_medium` premise bound to
THIS step's operation exactly (path, value, operation evidence id —
`proof_dag_liquid_medium_binding_mismatch` otherwise); the flat
recompute then receives a `_VerifiedLiquidMedium` capability token
(operation value + medium + definition digest) that discharges ONLY the
liquid-participation gate for that exact operation binding — the engine
re-checks the triple and fails closed to
`convention_liquid_participation_missing` on any mismatch. The
`retained_object` role is now scope-guarded: it accepts only
`source_relation` nodes (`proof_dag_retained_object_binding_mismatch`).
The builder attempts a protocol_reference node only when the flat derive
returns exactly `convention_liquid_participation_missing`.

## A01 acceptance (locked table) — results

All rows from the r11 replay (`round3d_acceptance`, status PASS):

| item | 3D expected | result |
|---|---|---|
| ProtocolReferenceProof | PASS (traceable resolution record) | **PASS** — nodes for graph[7].operation AND graph[8].operation built + verified both ways (span resolver and blocks-only) |
| operation sequence | PASS (basically no new info) | **PASS** — inherited sequence [centrifugation, redispersion] equals graph[7]/[8] segment identities (and graph[5]'s) exactly |
| second invocation repeat count | 1, LOCAL override (annotation only) | **PASS** — `reference_execution_count: "one time"` annotation; definition record JSON contains no "execution" key and no "three times" |
| liquid medium | PASS: inherited deionized water | **PASS** — `liquid_medium: "deionized water"`, evidence = definition fact |
| retained object of ms7a | BLOCKED (unchanged) | **BLOCKED** — `retained_object_mention_precedes_operation`, byte-identical to r10 |
| ms7a.out | BLOCKED (unchanged) | **BLOCKED** — build issue byte-identical to r10 |
| ms7b.in | BLOCKED, cascade (unchanged) | **BLOCKED** — cascade from ms7a.out |
| ms7b liquid independent diagnostic | PASS (inherited protocol slot) | **PASS** — source=inherited protocol slot, medium=deionized water, evidence=f_g5_op |
| ms7b.out overall | BLOCKED, still parent-state cascade | **BLOCKED** — `parent_state_unverified`, caused_by ms7a.out |
| graph[9].in/out | BLOCKED cascade (unchanged) | **BLOCKED** — cascades |

Key evidence ids (real signed Control group): definition evidence
`route_fact_7931591ece6209db307915ee` = f_g5_op
(`material_graph[5].operation`, locator `pdf:p2:b70-p2:b71`, char span
[974, 1107]); reference evidences
`route_fact_0161b46b7647e24611f477de` = f_g7a_op and
`route_fact_5e2ee4568f76f9c3558d8cc7` = f_g7b_op (locator
`pdf:p2:b74-p2:b75`, char span [1265, 1334]); ordinals first → second;
definition digest `sha256_c61cb9c1…f3820b7`.

**Carried-forward state table.** All nine r10 rows re-asserted and
byte-compared against the committed r10 replay JSON (verdict + issue +
attribution per node): unchanged. A recording builder proves the
protocol-reference machinery NEVER auto-engages anywhere on this chain
(zero `_build_protocol_reference` attempts across all nine builds) — the
compound reference steps hit the 3C compound-operation guard before the
liquid gate, exactly the documented v1 boundary.

**ms7a no-leak.** The resolver issue
(`retained_object_mention_precedes_operation`), the flat derive issue
(`convention_parent_state_unverified`), and the DAG build issue for
ms7a.out are byte-identical to r10's committed replay; a correctly-bound
`_VerifiedLiquidMedium` token for step 7's operation changes NOTHING in
ms7a's derivation (same issue with and without the token) — the token
never touches retained-object premises or the compound-operation guard.

## ms7b dual result — diff vs r10

Verbatim from the r11 replay:

```
ms7b.out
proof status: BLOCKED
primary blocker: parent_state_unverified  caused_by: ms7a.out
independent convention diagnostics:  (diagnostics_only, feeds_verdict: false)
    liquid_participation: PASS  source=inherited protocol slot  medium=deionized water  evidence=route_fact_7931591ece6209db307915ee (f_g5_op)
    engine_what_if: BLOCKED retained_object_mention_precedes_operation
```

The ONLY diff vs r10: `liquid_participation: FAIL
convention_liquid_participation_missing` → `PASS`. The literal gate
still fails (the second protocol's own evidence names no medium; ms7b
has no liquid-state input port) — the premise is discharged by the
verified protocol_reference node for graph[8].operation, with the liquid
token minted in the diagnostic harness only.

**Engine what-if surviving issue:** even with the parent assumed AND the
liquid medium inherited, the full engine stays BLOCKED on
`retained_object_mention_precedes_operation` — the compound reference
operation matches both the centrifugation and redispersion families, and
the second protocol has no post-operation retained-object source record,
so the compound-operation guard surfaces the resolver's recorded
blocker. That is the honest outcome: the liquid token discharges the
per-premise liquid diagnostic ONLY, never the overall derive — the
guard fires before the liquid gate for a compound operation (the
documented v1 boundary), and the compound-operation guard is NOT
weakened to make ms7b's full derive pass. The parent-only control
what-if reports the identical issue.

## Counter-example stress tests (real signed data)

Both counter-examples ran against REAL signed KB groups (the same
trust-config enumeration the preflight uses); no synthetic scope was
needed.

1. **"dispersed in 30 mL of water and aged"** (f_g6_op's excerpt, real
   Control group): sentence spans in the projected source are definition
   [16, 17] < nearer phrase [18, 19] < reference [20, 21] — the phrase
   sits BETWEEN the definition and the reference. It extracts as no
   definition (`protocol_definition_not_present`), never enters the
   candidate set, and the resolution — like the graph[8] DAG node —
   binds the medium evidence to the DEFINITION fact (f_g5_op), not the
   nearer phrase's fact. Proximity is never consulted.
2. **Etching group scope** (`Synthesis of the LDHs by an Etching Method
   of NiFe; Ey (y = 1−10).` — present in the signed KB, 10 blocks):
   resolving the Etching group's real reference "after the first
   centrifugation−redispersion protocol" inside the Etching scope, with
   the REAL Control definition visible as a foreign-tagged candidate,
   fails closed with `protocol_reference_scope_mismatch` — the Etching
   group contains no extractable definition of its own (its "second
   centrifugation−redispersion/washing protocol" sentence has no
   governed `using` clause and no ordinal anchor), and it does NOT bind
   the Control group's definition. Reverse isolation: mixing
   Etching-tagged carriers into the Control candidate set leaves the
   Control reference's resolution record byte-identical — Control never
   sees Etching candidates.

## Tamper items for the new proof class (real chain)

- **(a1) definition quote mutated on the definition evidence fact only**
  (a different real span of the same signed sentence): both old
  protocol_reference nodes fail verification
  (`proof_dag_leaf_fact_mismatch`); the rebuild RE-FAILS resolution
  honestly — the four sibling facts still quote the sentence at its
  original span, so two distinct in-scope mentions →
  `protocol_reference_ambiguous`. No stale node survives; no new node
  is minted.
- **(a2) all five facts quoting the definition sentence mutated
  consistently**: both old nodes fail; the rebuild RE-RESOLVES the
  single moved mention and verifies under the mutated facts with moved
  content addresses and a moved definition digest (locator/char span are
  part of the digested record); the old nodes still verify under the
  untouched facts.
- **(b) reference quotes mutated** (extended real span, still carrying
  the ordinal mention and "one time"): both old nodes fail; rebuilt
  nodes re-resolve and verify with moved content addresses; old nodes
  still verify under the untouched facts.
- **(c) resealed substitution** of the SAME DAG's (valid,
  self-verifying) reference_evidence paper literal into the
  definition_evidence role, every content address honestly recomputed:
  fails `proof_dag_protocol_reference_mismatch` (the role-fact
  binding); the untouched DAG still verifies.

## v1 boundary (documented)

The `_VerifiedLiquidMedium` token is NOT forwarded into
`_inheritance_proof_for_evidence` — inheritance re-derivation keeps its
literal liquid checks. Nothing in this chain needs the forward (the
only inherited medium in scope discharges a state-change node's
per-premise diagnostic), so the boundary is documented, not extended.

## Regression

- Target set (`test_route_proof_dag`, `test_route_retained_object`,
  `test_route_retained_object_integration`,
  `test_route_convention_state_chain`, `test_route_protocol_reference`):
  **166 passed, 0 failed** (27 new protocol-reference tests on top of
  3C's 139).
- Full slice (`reaserch_agent/` + `chem_agent_contracts/`, minus
  llm_connectivity): **1236 ran, 30 failed** — 29 in reaserch_agent, all
  on `../baseline-research-fails.log`, 1 in chem_agent_contracts = the
  single `../baseline-contracts-fails.log` entry
  (`test_legacy_bound_package_hash_is_unchanged`). The one baseline
  failure not reproduced is the environment-state-dependent
  `test_b1_bootstrap_generates_initial_outputs`, which passes.
  **Zero new failures.**
- The r7 and r8 runners re-run **byte-identical** (replay + audit
  JSONs); the r9 and r10 runners re-run with all acceptance items PASS
  and **byte-identical** JSONs (the committed r10 ms7b liquid FAIL
  string is historical 3C content, unchanged).
- The r11 runner is deterministic: two runs produce byte-identical
  replay and audit JSONs.

## Deliberately NOT done (exclusions)

- **Q1** population scope: typed `parent_ref` v1 allows only
  `material_instance`; `PopulationStateProofV1` is the planned Q1
  extension.
- **Retained-object inheritance of ANY kind**: NOT introduced — the
  first protocol's retained semantics are never copied onto the "second
  … protocol"; ms7a.out stays BLOCKED and the no-leak item pins it.
- **Operation-precondition inference**: NOT introduced — a downstream
  redispersion never proves an upstream retained phase.
- **Cross-group reference resolution**: NOT introduced — the Etching
  counter-example fails closed on scope.
- **Execution counts as definition slots**: NOT introduced —
  invocation-local annotations only.
- **`admitted_protocols` stays 0**; production cache untouched; no
  global gate relaxation; the compound-operation guard is NOT weakened
  to make ms7b's full derive pass; no existing check weakened.
- **Liquid token forwarding into inheritance re-derivation**: NOT
  shipped (documented v1 boundary, unused in this chain).
- **Resolver error-code refinement**
  (`retained_object_for_operation_unverified`): NOT shipped — r7/r8
  byte-identity forbids changing the recorded per-step issue strings.
- **O8 deferred**.
