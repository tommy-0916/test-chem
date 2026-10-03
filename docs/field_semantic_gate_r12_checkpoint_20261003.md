# Field Semantic Gate — Round-12 (Round 3E) Checkpoint (2026-10-03)

Round 3E delivers the operation-precondition-inference **feasibility
study — diagnostics only** — chartered by
`docs/field_semantic_gate_3e_design_20261003.md`, on top of the
double-passed Round 3D (business semantics + safety closure). The
question under study: can a downstream operation's preconditions ever
diagnose the state of an upstream output — concretely, can ms7b (the
redispersion segment of the second centrifugation−redispersion
protocol) diagnose ms7a.out (the retained phase of the second
centrifugation)? The study runs over the real signed NiFe Control group
on the **unchanged** round-10 proposal (read-only) and answers with a
diagnostic record, never a proof. **Conclusion: `insufficient`.
ms7a.out stays BLOCKED. This is a COMPLETE research outcome, not a
failure** — it is exactly the calibration the charter expects ("the
Control group must be allowed to conclude `insufficient`").

Artifacts: `reaserch_agent/route_operation_precondition_diagnostic.py`
(diagnostic evaluator, `operation-precondition-diagnostic/v1` — pure
functions + frozen data classes, no engine derives, no tokens),
`reaserch_agent/test_route_operation_precondition_diagnostic.py` (40
tests), `result/operation-structure-20260928/local-revision-r12.py`
(runner), `local-revision-r12-replay.json` (replay),
`local-revision-r12-audit.json` (audit, schema
`bounded_local_revision/v12`). No `chem_agent_contracts/` changes, no
v1 schema changes, no engine/gate changes, no r7–r11 edits.

**Acceptance-probe revision (same day):** two reproducible holes found
by acceptance probing are fixed and covered by new cases/tests:

1. **Same-group cross-stage evidence was accepted.** `_check_item`
   compared paper/group/invocation but never `ScopeBindingV1.stage`, so
   same-group evidence from a different stage stood and qualified. New
   rejection code **`stage_mismatch_rejected`** (both stages non-empty
   and different, same paper/group) — distinct from the
   cross-paper/cross-group `scope_mismatch_rejected`.
2. **Blank, sourceless items were provable.** A `direct_evidence` item
   with empty content, no source identity, `scope=None`, and no
   provenance qualified on subject/instance match alone. New rejection
   code **`evidence_identity_missing_rejected`**: a `direct_evidence`
   submission must carry a checkable identity (non-empty content;
   `source_identity ∈ {paper_explicit, supplement_explicit,
   external_primary}`; scope with paper/group/stage filled; non-empty
   provenance), checked BEFORE staleness and scope comparison. Rule
   whitelists, proposal assertions, and assumptions carry no source
   identity by design and are exempt.

Three narrative corrections are folded in at the same time: (i) a
retained-phase statement supplies upstream state evidence but does NOT
by itself prove the state is a NECESSARY input of the redispersion —
P1 needs an independent necessity basis; (ii) the post-redispersion
suspension reading of "all the samples are collected" is COMPATIBLE
with a retained wet-solid intermediate (collection follows
redispersion) — the two readings are not mutually exclusive and
excluding the suspension reading is not a promotion precondition;
(iii) Case 8's source mutation moves only P3 (proven → unproven) —
P1/P2 are unproven by construction in that case, so the overall
conclusion is `insufficient` before and after; it never flips.

Two reviewer-checklist refinements folded in on the same pass: (iv) the
aliquot/portion alternative is an OPEN DETAIL, not a conflict — an
aliquot of THIS ms7a output is still this output's material and does
not violate "from this output"; only material from a different source
or branch would be a genuine conflict (runner alternative [1], this
file's alternatives list); (v) the citation check is opt-in via
`live_sources` — when an item's provenance is not among the supplied
live sources, NO source-content verification is claimed for it, and the
citation-check status (checked / not checkable / stale) stays distinct
from the proposition-support relation, which remains the submitter's
annotation until the citation is actually checked (module docstring,
audit `design_rules`).

**Acceptance-probe revision 2 (same day):** two further
scope-completeness holes found by acceptance probing are fixed and
covered by new tests:

1. **Evidence with no invocation binding was accepted.** The invocation
   comparison in `_check_item` was gated on BOTH sides non-empty, so a
   `direct_evidence` item with a blank `scope.invocation` skipped the
   comparison, stood, and qualified. New rejection code
   **`invocation_unbound_rejected`**: direct evidence that binds no
   protocol invocation cannot support an invocation-bound target
   (`second`) — an unbound invocation is not a matching invocation. The
   check fires AFTER the stage check, so a true cross-stage observation
   record (the E10 caption: its empty invocation is a real attribute,
   and its stage `etching` ≠ `etching_second_wash`) keeps
   `stage_mismatch_rejected` — no invocation is fabricated for it.
2. **An incomplete target scope was accepted.**
   `evaluate_candidate_model` performed no completeness check on the
   scope under diagnosis, so a model scope with a blank `stage` (or any
   other blank field) silently disabled the corresponding comparison.
   The entry now raises `ValueError` (listing the missing fields)
   unless the model scope binds all four fields — paper_id /
   experimental_group_id / stage / invocation, pure whitespace counting
   as missing: an unknown scope is never a matching scope, and the
   diagnostic target of this study always binds the invocation ordinal.

Two narrow wording corrections are folded in at the same time: (vi) the
P1 open item no longer says "the second centrifugation's input state" —
it now names the ms7b redispersion's necessary input, i.e. the ms7a
output state under proof (the runner override, the module
`_OPEN_ITEM_DEFAULTS`, and this file are synced); (vii) the exclusion
burden for alternative (2) (aliquot/portion flow) is narrowed: only a
conflict from a DIFFERENT source or branch must be excluded — a
confirmed-compatible same-output aliquot is not an exclusion target.
The 10-case counter-example matrix is unchanged (Case 6 still records
BOTH `scope_mismatch_rejected` and `stage_mismatch_rejected`; Case 9/10
unaffected); the new behavior is covered by 10 new unit tests (40
total in the diagnostic suite).

## The A01 diagnosis (the real Control case)

Candidate under diagnosis: `ms7a.out =
material_graph[7].material_outputs[0].state = retained_wet_solid`
(instance `inst_ldh_wet_2`), scope = NiFe Control / stage ms7a /
invocation second. The evaluator builds its own dependency view from
the proposal graph: `parents(ms7b.in) = {ms7a.out}`, and the downstream
closure of ms7a.out is `{ms7b.in, ms7b.out, graph[9].in, graph[9].out}`
— any citation of those as support is `circular_dependency_rejected`.
The three propositions are answered **separately**:

| proposition | real evidence | verdict |
|---|---|---|
| **P1 necessary_input_condition** | the operation NAME "Finally, after a second centrifugation−redispersion protocol one time" (`paper_explicit`, `direct_evidence` of the operation's **occurrence** only — fact `f_g7a_op`); SI = D (the three targeted evidence classes were not detected); the REDISPERSION_V1 whitelist enters ONLY as `rule_compatible_states` (conventions.json L72-95) with the non-inversion note | **unproven** — no independent necessity basis |
| **P2 this_material_flow** | the proposal's own `parent_output_refs` = `[{macro_step_id: ms7a, material_instance_id: inst_ldh_wet_2}]` (`proposal_assertion` — an edge the proposal drew itself); the verified 3D protocol_reference nodes `proof_node_1e3047294567ed7b6b467dde` (graph[7].operation) and `proof_node_181d2b69af719f90c74cf2c5` (graph[8].operation) (`paper_explicit`, subject `operation_sequence_order` — they prove operation ORDER only; `inter_segment_material_flow` is a forbidden slot, so they are constitutionally silent on material flow) | **unproven** — flow assumption recorded |
| **P3 material_instance_binding** | "after a second centrifugation−redispersion protocol one time, all the samples are collected" (`paper_explicit`, `direct_evidence`, fact `f_g8_in0_state`) — collective and non-individuating (no per-instance identity), AND cross-stage: it is the ms8 (final collection) step's input statement cited in the ms7a diagnosis, so it is rejected `stage_mismatch_rejected` (the only rejection the honest Control diagnosis records) | **unproven** — continuity assumption recorded |

Alternative explanations recorded: (1) the supernatant-retained-instead
reading (the text does not constrain which phase the second
centrifugation kept); (2) aliquot/portion flow among the 8 divided
parts — WHICH portion fed ms7b is unresolved, but an aliquot of THIS
ms7a output would still be this output's material (an open portion
detail, NOT a violation of "from this output"); a genuine conflict
would be material from a different source or branch, kept distinct; (3) `washed_wet_solid` vs `retained_wet_solid` whitelist
ambiguity; (4) "all the samples are collected" may describe the
post-redispersion suspensions — a reading COMPATIBLE with a retained
wet-solid intermediate (the collection follows the redispersion), so
the two readings can hold at the same time; (4) is not a mutually
exclusive alternative and excluding it is not a promotion precondition
— the quote merely fails to individuate the instance under proof.

Open items (what would close each proposition): P1 — an independent
necessity basis for the ms7b redispersion's necessary input — i.e. the
ms7a output state under proof (a paper/SI retained-phase statement
supplies upstream state evidence but does not by itself prove the state
is a NECESSARY input of the redispersion);
P2 — an explicit inter-segment material-flow statement binding ms7b.in
to THIS ms7a.out; P3 — instance-individuating language naming the
instance under proof.

**Conclusion: `insufficient`** (default — at least one proposition
unproven; here all three). The assumption-only model ceiling is
`conditional_constraint` and **still non-unique**: the compatible-state
set is `{retained_wet_solid, washed_wet_solid}`, so even under the
(rejected) inversion the model cannot single out `retained_wet_solid`.
`node_verdict_unchanged = BLOCKED` — the diagnostic never changes node
verdicts; the r10/r11 rows are byte-quoted from the committed r11
replay (`verdict BLOCKED`, `issue
retained_object_mention_precedes_operation`, attribution
`evidence_gap` for ms7a.out and `dependency_cascade` for
ms7b.in/ms7b.out).

The full record (charter paper-v1 schema: source + figure-snapshot
digest; group/stage/invocation; per-proposition evidence/assumptions/
verdicts; alternative explanations; dependency relations; open items;
constraints block) is embedded verbatim in the r12 replay under
`round3e_feasibility.a01_diagnosis.diagnostic_record`.

## The necessity-rule basis

`chem_resources/chemistry_conventions/conventions.json` L72-95:
`REDISPERSION_V1` version `1.1.0`, `allowed_input_states =
["retained_wet_solid", "washed_wet_solid"]`, `output_states =
["suspension"]`, `liquid_participation.required = true`. The
non-inversion analysis (replay section B): the whitelist can ground
`rule_compatible_states` (rule applicability) and nothing more — it
cannot ground **necessity** (that the upstream output is a necessary
input — that needs an independent basis), **membership** (that the
paper's redispersion actually had an input in the set), or
**uniqueness** (that the input was uniquely `retained_wet_solid`).

## Counter-example matrix (10 cases, all PASS)

| # | scenario | expected | actual |
|---|---|---|---|
| 1 | real Control (calibration anchor, section A restated) | `insufficient` | `insufficient` |
| 2 | whitelist inversion: `rule_compatible_states` presented as necessity basis | `inversion_rejected`, output non-authoritative | `inversion_rejected`; conclusion `insufficient` |
| 3 | two compatible states even under inversion | `non_unique`, cannot single out retained_wet_solid | `non_unique=true`, set `{retained_wet_solid, washed_wet_solid}` |
| 4 | instance/branch swap (evidence individuating `inst_ldh_aged` cited for `inst_ldh_wet_2`) | binding mismatch rejected | `binding_mismatch_rejected` |
| 5 | first/second invocation swap (real `f_g5_out0_name` "The precipitates were labeled as LDH seeds", invocation `first`, applied to the second) | `invocation_swap_rejected` — recorded as swap, NOT evidence | `invocation_swap_rejected`; item excluded from evidence |
| 6 | cross-group E10 export (real signed Etching group, 10 blocks: "the second centrifugation−redispersion/washing protocol was performed three times to neutralize the sample." + SI S5 Figure S1 caption "clear salt solution without precipitates", `supplement_explicit`, the caption keeping its TRUE post-etching stage `etching`) | `scope_mismatch_rejected` — Control's conditional constraint is NOT exported into the Etching scope (the Control-scoped operation-name item); `stage_mismatch_rejected` — the caption's true post-etching stage is not the `etching_second_wash` stage under diagnosis | `scope_mismatch_rejected` + `stage_mismatch_rejected`; diagnosis scope stays the Etching group / etching_second_wash / second; caption item stage `etching` |
| 7 | circular dependency (ms7b.in state cited as support for ms7a.out) | `circular_dependency_rejected` | `circular_dependency_rejected` |
| 8 | source mutation (`f_g5_out0_name` excerpt mutated to "NiFe hydroxide seeds") | every citing item recomputes/invalidates honestly — no stale citation; P1/P2 unproven by construction, so only P3 moves (proven → unproven) and the conclusion stays `insufficient` — no flip | `stale_source_invalidated`; P3 proven → unproven; conclusion `insufficient` before and after; a recomputed item validates under the mutated source with a moved digest; the untouched record is byte-identical on rerun |
| 9 | same-group cross-stage citation (the ms8 final-collection statement, real `f_g8_in0_state`, presented as instance-binding evidence for the ms7a diagnosis — same paper/group/invocation, different stage) | `stage_mismatch_rejected` (distinguishable from `scope_mismatch_rejected`) | `stage_mismatch_rejected`; P3 unproven |
| 10 | blank sourceless direct evidence (empty content, no source identity, `scope=None`, no provenance) submitted for all three propositions | `evidence_identity_missing_rejected` on each proposition; P1/P2/P3 unproven; conclusion `insufficient` | 3 × `evidence_identity_missing_rejected`; all unproven; `insufficient` |

## Gate to a formal proof class — UNMET

The charter's four gate items, assessed against this study:

1. **Necessary conditions carry independent basis — ✗ FAIL.** The
   paper offers only the operation name; SI = D; the whitelist may not
   be inverted.
2. **Invocation and material binding hold — ✗ FAIL.** P2 rests on a
   proposal-drawn edge plus an order-only protocol reference; P3 rests
   on a collective, non-individuating statement.
3. **Alternative explanations excluded — ✗ FAIL.** Four readings are
   recorded and none of the genuinely competing alternatives ((1)–(3))
   is excluded by evidence — for (2) (aliquot/portion flow) the
   exclusion burden is limited to material from a DIFFERENT source or
   branch; an aliquot confirmed compatible with THIS ms7a output is not
   an exclusion target; reading (4) (the post-redispersion
   suspension reading) is COMPATIBLE with a retained wet-solid
   intermediate — it is not a mutually exclusive alternative, and
   excluding it is not a promotion precondition.
4. **Counter-example tests pass — ✗ as a gate item (n-a for
   promotion).** The counter-example matrix itself passes all 10 cases
   as diagnostics, but the E10 case shows the same protocol language
   coexisting with a no-precipitate outcome in another group —
   so no cross-group support exists for promotion either.

Therefore ms7a.out stays BLOCKED and the study counts as a COMPLETE
research outcome (`insufficient`), exactly the chartered calibration.

**What evidence would change the verdict:** an independent necessity
basis for the ms7b redispersion's necessary input — i.e. the ms7a
output state under proof (closes P1 — a
paper/SI retained-phase statement would supply upstream state evidence
but does not by itself prove NECESSITY); an explicit
inter-segment material-flow statement binding ms7b.in to this ms7a.out
(closes P2); instance-individuating language naming the instance under
proof (closes P3); plus exclusion of the genuinely competing recorded
alternatives — for (2) only a different-source/branch conflict must be
excluded (a confirmed-compatible same-output aliquot is not an
exclusion target), and the compatible suspension reading (4) is not an
exclusion target either.

## Fixed-constraints audit (all PASS)

- **diagnostics_only / feeds_verdict**: every diagnostic output carries
  `diagnostics_only: true` and `feeds_verdict: false`; the constraints
  block is enforced by construction (a violated block raises).
- **Zero tokens minted**: the whole study ran inside a guard sealing
  every mint channel — `_VerifiedParentStateEvidence`,
  `_VerifiedLiquidMedium`, `_DiagnosticParentStateAssumption`,
  `_DiagnosticLiquidMediumAssumption`,
  `_mint_diagnostic_parent_state_assumption`,
  `_mint_diagnostic_liquid_medium_assumption`,
  `_mint_verified_parent_token`, `_mint_verified_liquid_token` (each
  probed before the study and raising `SystemExit`; the study then ran
  to completion under the guard). Source-level half: the diagnostic
  module references no token constructor and imports nothing from the
  contracts package (module sha256
  `81a2d8a099d897981bc5eaecf4983a2b8041890dcf926e977728ae62f534e317` — recomputed at
  acceptance-probe revision 2; the revision-1 digest was
  `fd0320457967f2ffa954a7edb8259f3f90d446546dcd387245107cb0243721de`).
- **v1 untouched**: `protocol-definition/v1` module digest
  `sha256_6109b007e823d9758228d8ef71ec5a1d18b4da5dede3867067b2a3347617805b`;
  `PROTOCOL_REFERENCE_V1 1.0.0`; the closed-slot probe
  (`inter_segment_material_flow`) still fails
  `protocol_definition_unknown_key`. No file under
  `chem_agent_contracts/` is modified.
- **Verdicts byte-quoted unchanged**: ms7a.out / ms7b.in / ms7b.out
  rows byte-quoted from the committed r11 replay — all `BLOCKED` with
  `retained_object_mention_precedes_operation` (evidence_gap /
  dependency_cascade), unchanged.

## Regression

- Target set (`test_route_proof_dag`, `test_route_retained_object`,
  `test_route_retained_object_integration`,
  `test_route_convention_state_chain`, `test_route_protocol_reference`,
  `test_route_operation_precondition_diagnostic`): **228 passed, 0
  failed** (188 carried + 40 in the diagnostic evaluator suite — the
  original 20, plus 10 added by the acceptance-probe revision, plus 10
  added by acceptance-probe revision 2).
- Full slice (`reaserch_agent/` + `chem_agent_contracts/`, minus
  llm_connectivity): **1298 ran, 30 failed** — 29 in reaserch_agent +
  1 in chem_agent_contracts, every one on
  `../baseline-research-fails.log` / `../baseline-contracts-fails.log`
  after `sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/'`
  normalization; the one baseline failure not reproduced is the
  environment-state-dependent
  `test_b1_bootstrap_generates_initial_outputs`, which passes. **Zero
  new failures.** (Re-verified after acceptance-probe revision 2.)
- The r7/r8/r9/r10/r11 runners re-run with all acceptance items PASS
  and **byte-identical** JSONs (`git status --short` /
  `git diff` on the tracked runner artifacts is empty).
- The r12 runner is deterministic: two runs produce byte-identical
  replay and audit JSONs (re-verified after the acceptance-probe
  revision, and again after revision 2).

## Deliberately NOT done (scope exclusions)

- No proof-class creation (the gate is UNMET — see above).
- No token minting of any kind (verified or diagnostic-assumption
  channel).
- No `chem_agent_contracts/` changes, no v1 schema changes, no engine
  changes.
- No r7–r11 runner or JSON edits (verdicts byte-quoted, never
  rewritten).
- ms7a.out is not presupposed to PASS and is not closed; no attempt to
  make the conclusion stronger than the evidence supports.
- No cross-group inference: the Etching counter-example keeps
  group/stage/invocation explicit and refuses the export of Control's
  conditional constraint.
