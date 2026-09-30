# Pending Cause Triage — Evidence Gap vs Capability Gap (2026-09-30)

This document answers, for every remaining pending item, the deciding
question: **is the evidence insufficient, or does Chem-Agent already have
the evidence but cannot yet represent or derive the conclusion?** Gate
outcomes cited below were reproduced by executing the repo's own code
against the unchanged r3 proposal (`.venv/Scripts/python.exe`), not
estimated from reading.

Classification:

- **EVIDENCE** — the deciding premise is missing from the source; path is
  targeted supplementation (SI / cited methods / original protocol), review,
  or an explicitly labeled new protocol decision.
- **CAPABILITY** — the premises exist in source + proposal, but the pipeline
  cannot express or derive the conclusion; path is a bounded engineering fix.
- **MIXED** — both; the evidence part still goes to review/supplementation.

## A. The seven pending state facts

| Fact | Value | Cause | Verified gate outcome | Missing premise / missing capability |
|---|---|---|---|---|
| graph[5].outputs[0].state | precipitate | **MIXED** | `controlled_state_mapping` fail (`precipitate`→unknown, double-gated: `material_states/v1.json` + `_SAFE_STATE_ALIASES` route_field_basis.py:16-20); attribution `legacy` then plural-only quote (b72 "precipitates" ≠ singular value) | Capability: no state-change derivation is wired (`derive_unreviewed_output_state` only handles SPLIT/TRANSFER, route_convention_basis.py:37-40, 292-385); CENTRIFUGE_COLLECT_* rules exist in conventions.json:6-46 but are never consulted. Evidence/review: canonical token per step (retained vs washed wet solid) is Q4 — no global alias. |
| graph[6].inputs[0].state | precipitate | **CAPABILITY** (cascade) | input paths are never derivable (derive is outputs-only, route_convention_basis.py:305) | Needs the graph[5] output resolved + typed input inheritance (`material_origin`/`parent_output_refs`, v2.py:467-468). No source premise missing. |
| graph[8].outputs[0].state | precipitate | **EVIDENCE** (+capability) | port name "samples" absent from the only singular quote (b67); b67 describes the ms3-time observation ("once the reaction started"), not the post-ms7 collected batch | No source sentence ties the collected batch to any state word. Machinery (vocab, derivation) also missing, but even with it there is nothing to bind. Q3/Q4. |
| graph[6].outputs[0].state | suspension | **CAPABILITY** | attribution `pending` — the word never occurs after b69; association engaged because "suspension" is an entity bare-state | The operation evidence EXISTS: b72-73 "dispersed in 30 mL of water". What is missing is a rule producing `suspension` from wet-solid + water (no REDISPERSION rule in conventions.json) and state-change derivation wiring. |
| graph[7].inputs[0].state | suspension | **CAPABILITY** (cascade) | same as above, input path | Inheritance chain from graph[6] output (see F2 row). |
| graph[7].outputs[0].state | suspension | **EVIDENCE** (+review) | same pending branch | Chemically the retained phase after a second centrifugation is a wet solid, so the asserted value itself is in question; no post-change source passage uses any state word. The value needs Q4 review, not machinery. |
| graph[7].outputs… graph[8].inputs[0].state | suspension | **CAPABILITY** (cascade) | same pending branch | Inheritance from graph[7] output (value subject to Q4). |

Cascade facts (3 of 7) carry no independent evidence problem: they unblock
when their parent output resolves plus the typed inheritance chain exists.

## B. Q1–Q4 reclassified

**Q1 操作作用域 (per-part vs batch downstream)** — **MIXED, leaning capability.**
The split event itself is fully representable today: 8 child output ports +
`split_same_material` relation + `split_from_parent` lineage +
operation fact quoting "divided into 8 parts" satisfy SPLIT_V1 (shape proven
by test_route_convention_state_chain.py:97-137). What is missing: (a) a way
for a downstream port to consume the child *set* or per-part tracks —
`_set_parent_origin` requires an exact single 4-tuple match
(route_pdf_material_structure.py:130-176) and abstains on the child set;
(b) the withdrawn binding must not be silently replaced — per-part fan-out
or batch consumption must be typed from source evidence. Whether the source
determines the scope (context + applicable rules) or genuinely leaves two
executable readings is exactly what the answer must state; only the second
case stays open as ambiguity.

**Q2 数量作用域 (30 mL per part or total)** — **EVIDENCE, representation now
expressible.** The Methods sentence alone does not state the scope, and no
signing can certify what the authors did not record. This round adds the
missing representation: `QuantityV2.scope: per_part | total | unspecified`
(v2.py) — the field records only what a source supports, and an
unspecified scope must not be read as either. Paths once scope is known:
source states it → automatic; SI/protocol supplies it → targeted
supplementation; a scope is chosen for the experiment → labeled new
protocol decision with applicability verification.

**Q3 终点集合 (collection vs physical merge)** — **CAPABILITY (representation
delivered this round).**
"All the samples are collected" is a set record; it is not a merge
operation. The schema today cannot say that: no set/collection construct —
`LogicalContainerV2` is physical (v2.py:651-658), merge exists only as
effect/lineage (v2.py:332-343, 743-749), so the pipeline is forced to
choose N outputs or one implicitly merged instance — a chemical decision
the data structure manufactures on its own. Minimal addition (no chemistry
prejudice): `collected_set_of` lineage literal + `collect_material` effect +
matching event kind + a producer sibling of the split constructor that
wires the child set into step 8 only on a source-verified collection
sentence. Only if a later operation requires one mixed batch does a merge
need its own source or protocol-decision basis.

**Q4 状态规范化 (precipitate/suspension → machine state)** — **MIXED.**
Capability half: wire the existing CENTRIFUGE_COLLECT_*/WASHING_V1 rules
into derivation, add REDISPERSION_V1 (wet solid + water → suspension),
extend `_RULE_EVENTS` beyond SPLIT/TRANSFER, and un-double-gate vocabulary
additions. Review half: the canonical token **per step** (retained vs
washed; whether the centrifugation−redispersion retained phase is the LDH
seeds solid), and the graph[7] output value itself. The rules must state
their missing premises (retained phase? wash count? collection intent?)
instead of converting every failure into "human review".

## C. Engineering backlog (capability items, bounded, in order)

1. Quantity scope — **representation and consumer guard done this round**:
   `QuantityV2.scope` (per_part/total/unspecified) + tests, and
   `quantity_scope_allocation_issue` wired into the Phase-2 publish gate
   (`_v2_material_graph_issues`): an exact quantity whose scope is
   unresolved blocks ONLY the allocation that depends on it when the
   addressed population (≥2 instances) is determinable from existing refs
   (`parent_output_refs` or a `collect_same_material` member set); single
   objects and populations not yet determinable stay untouched. `per_part`
   permits per-part processing once the set is explicit; `total` records an
   identified-set total without inferring equal splits.
2. ~~Collection-set semantics for Q3~~ — **done this round (revised design)**:
   a collected set is member references, not another physical merge.
   `LogicalContainerV2` gains `member_material_instance_ids` (allowed only
   for `container_type="collected_set"`); the set adds no material, mass, or
   state and never forces a uniform member state. A collection is typed via
   `event_kind="collect_same_material"` + `material_effect="register_collection"`;
   the relation registers the same instances it references (input set ==
   output set), forbids quantity allocations, and is rejected in any
   transformation shape — bookkeeping never generates a device transfer or
   merge. The single-parent split/transfer/state-change checks are NOT
   globally relaxed; each relation kind keeps its own shape. Remaining: the
   source-verified producer that wires a collection sentence into this
   structure, and derivation's acceptance of a declared collection parent
   set (route_convention_basis.py:328-330).
3. State-change derivation wiring for Q4-capability: extend `_RULE_EVENTS`,
   add REDISPERSION_V1 to conventions.json with verified object/operation/
   applicability premises (never any solid-plus-water ⇒ suspension), and
   support `state_change_of` lineage in `derive_unreviewed_output_state`.
   Do this together with single-instance input inheritance (next slice).
4. ~~Vocabulary: global precipitate alias~~ — **removed per review**: the
   vocabulary layer only does morphological/terminological normalization
   (e.g. singular/plural); operation-dependent states ("separated and
   retained wet solid") must come from scoped derivation records with
   operation, input state, retained object, and time — never a global
   two-table alias added because one A01 field needs it.
5. Input-state inheritance: `material_origin` + `parent_output_refs`
   producer for downstream ports (v2.py:467-468), pending the Q1 answer
   (whole-set vs per-part).
6. Policy layer (explicit design decision, not an A01 backdoor): the
   admission gate currently accepts only `independent_human_pdf_review`
   (review-bundle status; issue-review rejects key reuse). To accept
   controlled auto-verification results as a *separately labeled* track —
   never interchangeable with human review, never a relabeling of a failed
   check — the trust configuration needs a deliberate two-track change.

## D. What stays human (after the split)

- Q4's per-step canonical state decisions and the graph[7] output value.
- Q2's scope fact (if not in SI) and Q1's scope (if the source leaves two
  executable readings).
- Any new protocol decision made to keep experimenting — labeled as such,
  applicability-verified, never retroactively the paper's fact.
- Real-device execution authorization (unchanged safety boundary).

Machine-side stopping rule: fix representation/derivation where premises
exist; targeted supplementation where evidence might exist elsewhere;
review/authorization where a decision is genuinely required. "Pending" is
no longer a single queue.

## E. Operation-boundary verification for the two conditional items

Verified against the paper's published Methods text (p.2, signed KB):

- **graph[7] (ms7)** covers the *composite* operation "a second
  centrifugation−redispersion protocol one time" — the node boundary is the
  composite endpoint (redispersed material), not the centrifugation
  instant. The retained-phase-is-wet-solid argument applies to the
  centrifugation sub-step only; the composite endpoint is consistent with a
  redispersed state. The source still never uses the word "suspension"
  after b69, so this output is a **derivation case** (needs the Q4
  capability wiring: redispersion rule with verified premises), not a
  quote-binding case. Reclassified: conditional evidence → capability,
  pending the state-change derivation slice.
- **graph[8] (ms8)** "all the samples are collected for further
  characterization" is a **collection-set endpoint, not a material output
  requiring a uniform state**. With this round's `collected_set`
  representation, what needs proving changes: member states are checked
  per member (from their own operation chains); the set itself carries no
  state. The current proposal's graph[8] output port with state
  "precipitate" quoting b67 (an ms3-time observation) is a modeling error
  to be corrected when the collection structure is wired — not an evidence
  gap requiring a state word for "the collection".

Also noted from the same Methods page: the parallel **Etching** variant
states "after the first centrifugation−redispersion protocol, *each
sample* was mixed with ..." — i.e. per-sample processing is explicit there
and absent in the pristine NiFe Control text. This is context for Q1, not
a substitute for the pristine group's own wording.
