# Field-semantic gate checkpoint, 2026-09-29 (WIP)

Status: **work in progress**. The 10 saved-proposal field items pass the new
association rules and the name-consistency conflict is no longer the first
blocker, but completion is not claimed until the positive and negative
acceptance of the source-association rules below is reviewed. The span slice
stays closed; this slice owns only the 11 triaged field-semantic items (state
evidence roles, material-ID identity policy, quantity/concentration identity
matching). No route architecture was added.

Round 2 (same file, below): the split downstream reference is withdrawn
into an unresolved dependency instead of re-pointed to the parent; the
step-8 identity conflict is resolved through the source-local label; the
revision-damaged verified fields are restored under guards; and the
group-level structural reviser gains acceptance conditions.  The strict
association still admits nothing and G2 stays unentered until the external
reviews land — that is recorded as the current honest state, not a
regression.

## Design rule, corrected

**Retracted:** "reaction/solution/suspension should move to `state` as
stage descriptions instead of occupying `name`."

**Replacement:** decide what role the original expression plays at that
mention, then choose the field. Name, state, operation description and entity
identity never substitute for one another.

| Role of the source expression | Handling |
| --- | --- |
| Named material (a defined "Solution B") | Keep its source name; associate the entity separately |
| Batch reference ("the suspension") | Allowed as a sourced referential label; resolve which entity it points to |
| State description of the material | Written to `state` only after entity attribution holds |
| Reaction process / experiment stage | Stays in the operation/stage description; never written as a physical state |
| Undetermined reference | Stays ambiguous; never force-unified into a name or state |

`solution`/`suspension` inside a name is not inherently wrong: the paper may
refer to a product exactly that way, and the system keeps the source name
while proving its state separately. "One stable name per step" is an
implementation convenience, not a rule that overrides the source.

## Accuracy boundaries implemented (with negative tests)

Two boundaries from review are now enforced and tested; both were places
where the first cut of the new rules could have been read as letting the
model's own entity organization prove itself.

1. **Proposal-unique label ownership is not source evidence.**
   `attributed_state_mention()` requires every bare-label attribution to be
   anchored by a name fact of the same entity that quotes the same passage
   (`mention_anchor` in the binding record). "Unique in the label table"
   only means the current proposal collected no competing entity; it cannot
   show the model did not drop one or misattach an excerpt. Minimal negative
   test (`ProposalUniqueOwnerIsNotEvidenceTests`): same source, same field
   under test, competing entity's facts removed — the previously pending
   attribution stays pending.
2. **Empty distinguishing cores never prove identity.**
   `material_id_graph_issue()` allows one ID to carry several bare
   referential labels in a step only when continuity evidence says they are
   one batch: every diverse name must appear in the same ID's ports of a
   neighbouring step (earlier outputs or later inputs), unless every diverse
   port declares one shared instance ID. Minimal negative test
   (`test_bare_stage_labels_need_continuity_evidence`): two distinct
   bare-named materials merged into one ID in a single step still conflict.
3. **Proposal-internal consistency never overrides an explicit source
   statement.** `labels_explicitly_distinct()` recognizes predicative
   distinctness claims ("X and Y were two different materials", "X and Y
   are distinct", "X was separated from Y"; attributive forms like "added
   to different flasks" are deliberately not matched). When the anchoring
   passage states that two labels of one entity are not one material, every
   state fact quoting it stays pending — same ID, same instance, same
   quote, and neighbour-step chaining included (`SourceContradictsMergeTests`,
   with the conversion-statement positive control proving legitimate
   change-of-state passages still attribute).

   Scope of this guard, fixed: it is a **refutation check, not an entity
   resolver**.  Finding no distinctness claim does NOT by itself make two
   labels one material — positive binding still requires source definitions,
   concrete mentions or legal material relations.  Conversely, "X was
   separated from Y" states at most one separation event and is never
   promoted into "X and Y are different chemical substances"; the guard
   conservatively blocks unproven merges and changes no chemical identity
   on its own.

The state gate is three-way everywhere (`state_attribution_outcome`):
`binding` (exactly one mention binds, record written), `pending`
(association engaged but resolution failed — never falls back), `legacy`
(no name facts for the entity, or no label structure for this word in this
passage — only then may the old strictly-local matcher apply). "No
association context" and "association check failed" are distinct outcomes;
the discriminating case is locked by
`test_engaged_failure_does_not_fall_back_to_legacy_matcher`. Deleting a
required name fact cannot ease passage: the legacy matcher may answer the
orphaned state fact, but the strict entry still blocks the group for the
missing name claim (`LegacyEntryDoesNotEasePassageTests`).

## Mechanism map

- `chem_agent_contracts/route_source_labels.py`: scoped source-label
  context (labels from `.name` facts; surfaces incl. verified case
  variants; per-label and bare-state owner uniqueness with ambiguity
  drop; name anchors with passage identity), mention classification by
  longest containing span, explicit-distinctness detection, definition-site
  concentration pattern, three-way state gate.
- `chem_agent_contracts/route_candidate.py`: versioned
  `SourceLabelBindingV1` (rule, label, source surface, entity, and
  `mention_anchor` — the field path of the name fact that anchors the
  association at this quoted passage), written into the evidence matrix and
  recomputed/compared by the PDF verifier.
- `reaserch_agent/route_group_compiler.py`: `_fact_issue` state/quantity
  branches, `material_id_graph_issue` identity policy (cores + continuity),
  matrix records.
- Same context consumed by the literal fact receipt, the PDF source
  verifier, the route decision gate, and field diagnostics.

The audit chain the review asked for is therefore present per admitted
field: name fact at the definition/mention passage (`mention_anchor`, its
excerpt and locator in the same matrix) → the current fact's own excerpt and
locator → rule and surfaces used → verifier recompute of the same record.

## Replay status (saved proposals, zero LLM calls)

Replay: `replay-second-raw.py` on `new-real-with-inventory.json` (Huang 2023
NiFe Control), output `second-raw-replay-semantic-gate.json`; original inputs
and digests untouched.

- 9 `semantic_binding_pending` state facts pass through the anchored
  association: named solutions bind by label-embedded mention; the reaction
  mixture binds by the bare `the solution` mention anchored at the same
  quoted passage (f046/f048). The `1 M` concentration passes through the
  definition-site parenthetical with the `Solution B` surface.
- Declared literal coverage 60/70 → 70/70; no excerpt was rewritten, no
  label renamed or dropped, no ID minted.
- Still blocked: `route_group_material_instance_scope_conflict`. This check
  and error code predate this slice; the earlier name conflict simply
  masked it. The proposal models the three precursor solutions as separate
  sample arms and consumes their instances in `reaction_arm` without
  transfer structure — a proposal-organization/lineage defect, not a
  field-semantic one. Per the production contract, different solutions,
  containers and preparation steps do not by themselves make different
  experimental arms; the next step is to answer from the saved input whether
  these are precursor branches of one arm or a real cross-arm flow, then let
  the local revision pass produce a corrected proposal (original graph and
  change basis preserved) for the existing source/identity/lineage/quantity
  gates. Renaming real arms to silence the conflict is out.

Whether the 9 passes are "old matcher wrongly rejected clear source text"
rather than "rules relaxed into wrong acceptance" is exactly what the
negative tests above are for; that judgement stays open until the review of
this checkpoint.

## Sample-arm diagnosis for the saved proposal (read-only)

Inputs: the A01 task (NiFe catalyst campaign; this proposal is locked to
the "Pristine Ni3Fe LDH (NiFe Control)" target), the group's source text
(`pdf:p2:b54-p2:b76`), and the saved proposal's `sample_id` assignment
(`solution_a_arm`, `solution_b_arm`, `solution_c_arm`, `reaction_arm`).

Source facts that decide the organization question:

- The group synthesizes ONE product: "The pristine Ni3Fe LDH, labeled as
  NiFe Control, was synthesized by a classical pH-controlled
  coprecipitation and centrifugation protocol." No treatment comparison
  exists inside the group.
- The three solutions are preparation branches converging into one
  physical batch: "solution A is injected into solution C in a beaker ...
  The pH of the solution was monitored ... by dropwise adding solution
  B". One beaker, one batch.
- The "8 parts" are aliquots of one suspension under an identical
  downstream protocol ("divided into 8 parts, followed by ... three
  times ... labeled as LDH seeds ... aged for 20 h ... after a second
  centrifugation ... all the samples are collected") — no differential
  treatment among the parts.
- The comparative design of the paper (Control vs Etching Ey vs ER ERy)
  lives BETWEEN experimental groups, which are separately enumerated;
  "Control" is a between-group role, not an arm structure within this
  group.

Judgment, per the fixed rules: the three solutions are **precursor
preparation branches of one target sample**, not experimental arms. The
proposal mis-built preparation branches as arms, and the cross-arm
instance conflict is an artifact of that wrong partition, not a real
cross-arm flow. The 8 parts are aliquots, not arms.

Hierarchy boundary: the paper's "NiFe Control" designation does not by
itself assign this group to any particular control arm of the A01 task.
This revision fixes only the internal organization of this paper
experimental group; which A01 sample arm the resulting route ultimately
serves is decided by the task goal and route applicability, not by the
word "Control" in the paper.

Revision scope, fixed for the local repair:

- Allowed: the wrong `sample_arm` attributions and the structurally
  affected instance scoping, parent output references, operation
  relations and related structural references.
- Must be preserved: the original proposal, each solution's source and
  batch, every source quantity, the true operation order, the eight
  aliquots, and the downstream operations and endpoint requirements.
- Forbidden: merging different materials into one ID, redeclaring
  internal materials as external inventory, inventing transfer
  operations, or dropping samples/steps/required fields.
- IDs need not stay byte-identical; if scope rules require regeneration,
  an old→new mapping must be saved and every reference updated
  consistently, without silently changing chemical identity or batch
  relations.

Input for the local-revision pass:

- Reorganize to ONE sample arm for the NiFe Control product lineage
  (steps 1–7), keeping the three solution materials and their batches as
  distinct entities with consumption relations (A and C injected, B
  dosed) expressed as material flow inside the arm.
- Represent the 8-part division with the existing typed split structure
  under the same arm; the split sentence is already quoted (f050–f052).
- Do not merge genuinely distinct comparison groups — there are none
  inside this locked group; the between-group comparison is out of scope
  for this proposal.

## Test and comparison report (narrowed claims)

- New tests: `reaserch_agent/test_route_source_labels.py` (33) and
  `reaserch_agent/test_route_inventory_basis.py` (17) — context building,
  no cross-entity merge, owner-unique vs source-anchored attribution
  (positive and both negatives), mention classification,
  explicit-distinctness bounds, identity policy (continuity evidence,
  shared instance, distinct cores), concentration surfaces and
  definition-site guards, binding records, the no-fallback gate,
  legacy-entry strictness, the four inventory-resolution acceptance cases
  with compile/decision-gate integration, the G1 register exemption
  (positive and digest-mismatch negative), and the save/reload source
  re-verification.
- Full research suite: 885 tests ran (835 at the f957b64 baseline + 50
  new). The 29 failing test IDs are the same as in the f957b64 baseline
  run in the same environment; one of them
  (`test_concurrent_same_identity_records_do_not_overwrite`) has shown a
  FAIL/ERROR category difference between full-suite and isolated runs and
  is recorded separately — the isolated run reproduces the baseline
  assertion verbatim.  This neither proves a production regression nor
  re-classifies the old failures' root causes; it is tracked as a
  test-stability issue in parallel.
- Device suite: full acceptance not completed. Both the changed tree and
  the f957b64 baseline terminate at the same location
  (`single_agent.py:4623`, `feasibility certification requires a clean
  audit bound to the final candidate`); the earlier "985 tests, 50 known
  failures" fingerprint is not inherited by this run.

## Deterministic arm-organization repair (delivered)

Artifacts (next to the saved replay inputs):
`arm-organization-repaired-proposal.json`,
`arm-organization-repair-diff.json`, `arm-organization-repair-gates.json`;
producer: `repair-arm-organization.py`.

Nature: a deterministic, bounded local repair of the SAVED proposal per the
diagnosis — explicitly not a model-generated revision.  Change set: all 7
steps reassigned from `solution_a_arm`/`solution_b_arm`/`solution_c_arm`/
`reaction_arm` to one `nife_control_arm`; nothing else touched (graph and
facts digests identical except `sample_id`; material IDs, batches,
quantities, operation order, eight aliquots and all facts preserved; no ID
regeneration was needed).

Gate results at the production strict entry (re-run after the G1
alignment of this slice; the artifacts were refreshed):

- G1 field diagnostics (`propose_pdf_group_unreviewed`,
  `check_required_graph_facts=True`): the instance-scope conflict is
  resolved by the reorganization and all 70 declared facts still pass; the
  7 inventory states now surface as `required_graph_fact_missing`
  dependencies (previously invisible, see below) and the draft is not
  admitted while they are open — exactly the required-dependency display.
- G2 group compilation: not reached while the G1 dependencies are open;
  with inventory records supplied and resolutions attached, compilation
  accepts the resolved states as `inventory_record` evidence (see the
  external-input resolution slice below).

Context for the next decision (no gate changed): the first saved proposal
passed the same compilation depth only by filling non-vocabulary word-salad
states (`dissolved`, `water`, `yellow-green precipitate`, `samples`…), and
its replay ran with `check_required_graph_facts=False`, so its equally
wrong 4-arm organization was never checked.  As written, the state gate
pushes the model either to fabricate states or to stall — that trade-off is
recorded here for review; the revision pass needs either source-supported
states, an approved inventory-state convention, or a scoped gate decision,
none of which this slice changes on its own.

## External-input state source resolution (this slice)

Scope, fixed: where a material port's state comes from, and at which stage
it must be resolved — not a new chemistry system.  Evidence order: paper
fact first (unchanged gates); then an approved inventory / material-spec
record that uniquely binds the concrete material and its verified supply
form; otherwise the state stays an explicit unresolved dependency in the
unreviewed draft and formal publication keeps blocking.  No default states
are invented, the G2 state check is not relaxed, solvent roles are not
state tokens, and no inventory fact is ever dressed as paper or convention.

What changed:

- **G1 requirement alignment** (`_required_qualitative_paths`): name and
  state facts are now required for every material port regardless of
  whether the model filled a non-empty value.  A missing state surfaces as
  `required_graph_fact_missing` (an explicit dependency) instead of
  vanishing from coverage.  Consequence: incomplete drafts are no longer
  admitted at the strict entry — drafts stay preserved with their
  dependencies recorded, and local revision refuses to "repair" a missing
  required claim by rewording.
- **Contract extension** (`v2.py`): `EvidenceClassV2` gains
  `inventory_record`; `ProvenanceV2` gains kind `inventory` mapped to that
  class.  Inventory rows are never `paper_explicit` and never
  `chemistry_convention`; genuinely SOP-controlled information keeps
  `device_sop`.
- **Resolver** (`chem_agent_contracts/route_inventory_basis.py` +
  versioned resource `chem_resources/material_inventory/v1.json`, seeded
  empty until the lab provides verifiable records).  Every first-appearance
  input without upstream production evidence is classified:
  `resolved` (unique record, compatible spec), `spec_mismatch` (no,
  ambiguous, or spec-incompatible record — e.g. a stocked solution cannot
  satisfy a weighing amount), `source_missing` (no record), or
  `lineage_blocked` (upstream evidence contradicts an
  `external_inventory` label — re-labeling an intermediate cannot bypass
  upstream state obligations).  Eligibility never rests on the model's
  origin claim.
- **Gates**: the compiler consumes verified resolution records
  (`inventory_resolutions`, structurally validated, file-pure), writes
  matrix rows with inventory provenance, and discharges the paper-fact
  coverage for exactly those state paths.  The route decision gate accepts
  structurally sound inventory provenance on state paths only; the PDF
  source verifier re-verifies every inventory row against the current
  resource bytes (digest, item, supply form, record text); the science
  audit admits inventory-resolved state rows on equal value with the graph.

Acceptance (four cases, `test_route_inventory_basis.py`, 13 tests):
unique-record binding compiles with `inventory_record` provenance and
passes the decision gate; missing-record states keep G1 reporting the
dependency and block honestly; spec mismatch never picks a convenient
default; mislabeled intermediates are refused.  The NiFe replay
classification (`inventory-state-resolution-report.json`): all 7 ports
classify `source_missing` — the Materials list states no supply form and
no approved inventory record exists.  That is a deliverable input
dependency (lab inventory / material-spec records), not something an Agent
change can remove.

## Real-source resolution replay (this slice)

Inputs: `candidate-supply-specs.json` — verifiable candidate supply specs.
Each record cites a standard reference (CAS + PubChem/CRC-class
physical-state data) for the supply form it states, at the granularity the
source supports: the four solutes are recorded as `solid` (crystalline /
white solid), never refined to `powder` — dissolving a reagent states that
it is dissolved into water, not that it is supplied as powder (e.g.
water-soluble NaOH is sold as pellets), and the controlled vocabulary maps
`powder` to the device notion 粉末, not to solid in general.  Paper
operation sentences are carried as `operation_constraints` (pending, not
form evidence).  Water is recorded as `liquid` with the machine mapping
`liquid -> solution` stated explicitly as a mapping, not a source claim.
One shared water spec serves the three water ports; ports, batches and
consumption accounts stay separate.  Every record is
`candidate_supply_spec: true` / `stock_verified: false`: local planning
form support only, never a stock claim.  The alias resource gained one
consistent entry, `solid -> dry_solid` (固体), so source-granularity forms
are expressible without ontology changes.

Resolver refinements per review: incompatibility is judged from material
form and operation constraints (an explicit dissolving statement requires a
non-solution form), never from a bare amount unit; several
indistinguishable records are reported as `ambiguous`, distinct from
`spec_mismatch` and `source_missing`.  Registers are byte-verified bundles
`{register: (items, digest, resolutions)}` flowing through
`propose_pdf_group_unreviewed` → local revision → assessment; a resolution
discharges a G1 state requirement only while its cited digest matches the
bundled bytes.  Resolution records bind at the group boundary before G2
(the strict proposal envelope admits no extra keys).

Replay result (`real-source-resolution-replay.json`):

- All 7 external-input states resolve, each with three recorded layers —
  source-stated form, the alias mapping used, and the machine token: 4
  solutes `solid -> dry_solid`; 3 water ports `liquid -> solution` via one
  shared spec.  Each record carries its source citation and, where the
  paper constrains handling, a pending `operation_constraints` entry.
- **G1**: clean — legitimately record-proven states are no longer
  re-demanded as paper excerpts (the key consistency criterion).
- **G2**: compilation now stops at `route_group_required_capabilities_missing`.
  This is the next real boundary, not a defect: required capabilities are
  assigned by the independent role/capability review
  (`propose_pdf_group_protocols` with signed reviewed maps), never by the
  unreviewed proposal, and fabricating them is out of scope.  The accurate
  milestone: the proposal has entered G2 and stopped at a later
  prerequisite inside it; the group compilation is NOT complete and
  nothing past this point has been validated.
- Save/reload source re-verification is proven at the unit level
  (`test_save_reload_preserves_inventory_source_binding`): JSON round trip
  preserves the inventory provenance and the decision gate accepts it.

| Judgment | Recorded state |
| --- | --- |
| "dissolving → powder" over-inference | retracted; specs now cite reference physical-state data at source-supported granularity |
| 7 candidate state resolutions | resolved per local replay; sources are reference physical properties, NOT stock confirmation; the package is "candidate material forms supported by public reference data, for local planning" |
| `solid → dry_solid` | restricted machine representation mapping; see the mapping restriction in the review section |
| G1 strict entry | passed per local replay |
| G2 group compilation | entered, stopped at `route_group_required_capabilities_missing`; NOT complete, nothing past it validated |
| Independent review | work order ISSUED, human decision pending — the current named external dependency |
| Fresh generation, Research publication, Device validation | no pass result this round |

## Live-model runs after credential recovery (2026-09-29)

Auth fixed: the `.env` key is a Kimi Code key; endpoint
`https://api.kimi.com/coding/v1`, model `k3` (the factory strips the fixed
temperature only for the recognised pairing), wire=chat. Minimal probe via
the same `LLMFactory` path returns a normal response.

- Bounded model revision on the saved proposal: 0 revision calls — the
  machinery correctly classifies the arm/instance defect as a structural
  blocker and refuses field-level repair attempts
  (`bounded-model-revision.json`).
- Fresh generation from raw inputs (one live call, bounded repair budget):
  the full producer-gate chain ran and did NOT admit the proposal
  (`fresh-clause-tightening-proposal.json`, 94 facts). Honest profile:
  4-arm organization recurs (no instance conflict this time); 13 required
  state facts missing (null states on external inputs recur); 1 identity
  conflict (step 5 `suspension` vs `LDH seeds` under one ID; step 8
  `Ni3Fe LDH` vs `samples`); 8 `semantic_binding_pending`; 2 quantity
  attribution; 2 unit issues. Candidate-spec resolution would discharge
  the external-input states where the name matches (`deionized water` at
  step 5 has no matching record — naming finding); upstream-produced
  states and the identity conflict are proposal defects, blocked from
  field repair as structural. Verification of the generation stage stops
  here, at the unchanged review boundary.

- Budgeted group-level structural revision on the fresh proposal (exactly
  one live call, `group-structural-revision.py`): the diagnosed arm fix
  LANDED (one `sample_nife_control` arm), identity renames pass under the
  continuity policy ('suspension'->'LDH seeds'->'samples' chain across
  adjacent steps), no quantity value/unit changed (one source-backed
  quantity fact added), operation quotes identical, G1 field diagnostics
  clean with 8 candidate-spec state resolutions and the `deionized water`
  precise gap kept.  The revision is NOT admitted: the strict association
  blocks with `structure_split_count_or_children_unresolved` — the
  revision materialized the 8-part split as ONE output carrying quantity
  '8 parts', which the typed split constructor cannot expand into 8
  children (the fresh proposal had this split 'represented' by leaving
  outputs empty).  Budget spent; no resampling.  Remaining issue for the
  next budgeted round: represent the split with no outputs (program-built
  children) or explicit child ports covering all 8 parts.

- Deterministic split-event repair (`reaserch_agent/route_pdf_split_repair.py`,
  regression in `test_route_pdf_split_repair.py`): revokes a collapsed
  count-as-quantity split port into an audit record, preserves the source
  operation / unique parent / verified count, and lets the existing
  constructor rebuild the children in the current scope.  Applied to the
  arm-fixed proposal: the 8 children and the typed relation were rebuilt
  with fresh instance IDs, and — in its first version — the downstream
  reference was re-pointed to the split parent as a recorded
  batch-continuation mapping (5 audit rows, 0 model calls, 0 unresolved).
  **Superseded 2026-09-29 (round 2, below):** that re-pointing made the
  parent an executable downstream input while the children stayed recorded
  as available outputs; review rejected that semantics and the repair now
  withdraws the reference into an unresolved dependency instead.  The
  strict association still refused after the first repair, with the split
  structure sound: the first actual blocker was the step-8 identity
  conflict ('Ni3Fe LDH' vs 'samples', a named rename without continuity),
  and field-level defects the revision introduced (truncated quotes on the
  Solution B facts; the non-vocabulary state 'precipitate' on the LDH-seeds
  chain).  Nothing was admitted; nothing was compiled; empty results were
  reported as empty.

## Round 2 (same day): split downstream reference, step-8 identity, damaged fields

Scope, fixed: only the split downstream reference, the step-8 identity
relation, and the fields the structural candidate damaged.  The confirmed
arm organization, the split cardinality, source quantities and all
unaffected facts were locked.  Zero model calls; every change is
deterministic and audited (`result/operation-structure-20260928/local-revision-r2.py`,
artifacts `local-revision-r2-proposal.json` / `-audit.json` / `-replay.json`).

1. **Downstream reference: withdrawn, not re-pointed.** Review rejected
   "batch continuation to the split parent as an executable input while the
   8 children stay recorded as available outputs" (double-booked parent,
   children idle).  `repair_split_event_representation` now removes the
   revoked port's instance binding from downstream ports (name/state facts
   stay — they are source claims about WHAT is processed) and records an
   unresolved dependency naming the deterministic candidate set (the
   constructor's child IDs, verified byte-equal in
   `test_withdrawn_reference_names_constructor_children`).  No child is
   silently selected, no merge is assumed; the only defensible merge is one
   the source itself states.  Gate-constructed state, re-verified: 8
   children, relation `split_same_material` in=[inst_ldh_susp] out=8,
   **zero** downstream references to the parent (it survives only as the
   split relation input, i.e. lineage), `material_id_graph_issue` clean.

2. **Step-8 identity: source-local label, no rename dodge, no ID mint.**
   The collection passage (b75) names the gathered material "all the
   samples"; the paper-level designation "pristine Ni3Fe LDH" stays bound at
   `route_signature.target` (b56) and the shared material entity
   `mat_ni3fe_ldh` (b67 identifies the precipitate as Ni3Fe LDH at reaction
   start; b69 identifies the samples as that suspension).  The step-8
   output therefore carries the label the source uses at that position
   ('samples', instance `inst_nife_control` preserved).  A step-8-local
   rename to 'Ni3Fe LDH' had neither continuity nor a local mention — it is
   not forced.  `route_group_material_id_identity_conflict` is gone.

3. **Damaged fields restored from the verified basis, under guards.**
   Restoration applies only when document, group, entity, value and unit
   are unchanged (mechanical guard) and the verified quote binds to the
   current source under the verification budget
   (`MAX_VERIFICATION_CONTEXT_BLOCKS`, not the display budget — the
   verified split quote spans four content blocks).  28 excerpts restored;
   4 correctly refused (three operation labels and the merged step-3
   output naming, whose values differ from the verified basis — the
   correspondence changed, so nothing was copied).  The part count returned
   to the dedicated `count` field (verified representation) instead of a
   unit-less numeric parameter, clearing `fact_numeric_unit_missing` and
   letting the constructor validate the count fact against the operation.
   `deionized water` keeps its exact name and its separate gap
   (`source_missing` resolution + `required_graph_fact_missing`); it is not
   simplified to catch the water spec and not counted as resolved.

4. **Group-revision acceptance conditions (prospective guard).**
   `group-structural-revision.py` now runs the gates on the candidate
   BEFORE any baseline write and accepts it only if (a) no structural
   blocker code remains — from both `association.diagnostics` and the
   local-revision final issues; producer-stage collapses surface in the
   former and would previously have slipped through — and (b)
   `fact_regressions` finds no rewritten/removed verified fact (name-fact
   value changes are admitted only as gate-proven rename candidates).
   Otherwise the candidate lands in `structural-revised-candidate-rejected.json`
   and the working baseline stands.  Retrospective verdict on last round's
   candidate: **REJECT** (`structure_split_count_or_children_unresolved`) —
   the guard would have kept the collapsed split out of the baseline.

Gate result after the round (same strict entry, zero repair budget):
**admitted = 0 → G2 not entered; nothing compiled; nothing reported as
passed.**  Remaining G1 items are honest and smaller: 7
`semantic_binding_pending` NAME facts (5× 'LDH seeds', 2× 'samples' — the
labeling/collection bare labels stay unresolved as names), 1
dimensionless-numeric pending (pH 10), 1 `fact_quantity_attribution_unresolved`
(the 1 M concentration — the verified arm-repaired basis shows the identical
single pending under the current gates, so this is an inherited genuine
gap, not round-2 damage), and the deionized-water state requirement.
`precipitate` received **no** alias: its state facts now bind through the
unchanged anchored-association rules; the token stays outside the
controlled vocabulary (`material-states/v1`), and any future mapping (e.g.
to `retained_wet_solid`) is a reviewed vocabulary decision, not this
round's.  Net: the two blockers this round targeted (identity conflict,
count representation) are resolved; the split downstream consumption is
explicitly unresolved rather than wrong; all preserved facts match the
verified basis or are declared model-chain gaps.

Test suite (full research discovery, same environment): **949 tests**
ran on the changed tree (835 at the f957b64 baseline + 114 added since);
**8 failures + 21 errors, and the 29 failing test IDs are byte-identical to
the f957b64 baseline set** (compared both directions, zero differences) —
all added tests, including the split-repair regressions, pass.  Per the
standing caveat this fingerprint match does not by itself prove every
semantic change in this round correct; it bounds this round's blast radius
to zero new failing IDs.  The known flaky category difference on
`test_concurrent_same_identity_records_do_not_overwrite` (FAIL in the full
suite, reproducible verbatim when isolated) is part of that inherited set
and remains tracked as a test-stability issue.

## Independent review track (current external dependency)

Status: **materials ready; no independent reviewer has accepted the work
order yet**.  Artifacts:
`current-goal.json`, `group-review-work-order.json` (status
`pending_independent_chemical_review`, digest
`sha256_7566f8d48e13ac81622f25c0af4db7bcd5993cd29217b6f7046807ff50d6aaa0`),
and `review-bundle.json` — the consumable material package mapped to the
reviewer's documented duties (group boundary, route interpretation,
parameter isolation, capability completeness), including: fixed source
blocks and exact scope; original vs arm-repaired graphs with the diff
record; the 70-fact field basis with source-label bindings; the corrected
reference-form records with the three layers (source form / machine
mapping / token) and the pending operation constraints; the source-anchored
capability NEEDS of the route (explicitly not pre-matched to workstation
inventory and not to be shrunk to fit it); and the mapping restrictions
below.

Open manual todo, named: **assign an independent reviewer** (a human
chemistry reviewer holding a private key outside the repository and KB) to
accept the work order and produce the decision JSON.  Until a decision
exists, the work order must not be treated as review-in-progress; an
issued order is not a started review.  A "pass with conditions" verdict is
not automatically a publication license: conditions that affect the
current compilation or execution keep their blocks until met; conditions
that belong to real execution preparation stay behind the existing stage
boundary.  The decision step requires the reviewer's private key; the
issuer rejects key reuse and never forms chemical judgments.  When a signed
decision exists, the reviewed capability map feeds the SAME proposal and
G2 continues; the record will state whether compilation completes and what
blocks next.

Mapping restriction, recorded: `solid -> dry_solid` and `liquid -> solution`
are machine representation mappings only — the source proves "solid" /
"liquid" and nothing about dried/anhydrous/moisture/powder form or changed
identity/composition.  Checked consumer this round: `device_sample_state_map`
maps `dry_solid` to the device handling class 固体 and `powder` to 粉末; no
consumer there reads "drying completed" from `dry_solid`.  Any future
consumer that infers such meaning must not use the widened alias.

## Deferred

- Named manual todo: assign an independent reviewer to accept the issued
  work order (see the review track above).
- Credential recovery, then the model line: bounded revision and a fresh
  generation from raw inputs under the same field/source/lineage/coverage
  gates; replay and generation do not substitute for each other and may
  run in parallel.
- Sample-arm triage for the saved proposal and a local-revision pass
  (model credentials required), then re-verification by the unchanged gates.
- Fresh-generation acceptance after credential recovery
  (`generate-fresh-proposal.py`, then the authorized `run_campaign.py
  --forward-only` A01 pass) under the same field/attribution/coverage bars.
