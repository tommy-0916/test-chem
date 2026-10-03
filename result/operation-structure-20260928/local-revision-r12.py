"""Round-12 (Round 3E): operation-precondition-inference FEASIBILITY STUDY.

Diagnostics only, per the archived charter
(``docs/field_semantic_gate_3e_design_20261003.md``).  Round 3D is
double-passed (business semantics + safety closure); this round delivers
the feasibility study and nothing else:

- A. the A01 diagnosis (the real signed NiFe Control group, unchanged
  r10 proposal, read-only): the three propositions (necessary input
  condition / this material flow / material instance binding) are
  evaluated SEPARATELY with REAL evidence — the paper offers only the
  operation NAME, SI = D (the three targeted evidence classes were not
  detected), the REDISPERSION_V1 whitelist enters ONLY as
  ``rule_compatible_states`` with the non-inversion note, the proposal's
  ``parent_output_refs`` is a ``proposal_assertion``, the 3D
  protocol_reference nodes prove operation ORDER only and are
  constitutionally silent on material flow, and "all the samples are
  collected" is collective, non-individuating, AND cross-stage (an ms8
  collection statement cited in the ms7a diagnosis —
  ``stage_mismatch_rejected``).  Conclusion:
  ``insufficient``; the assumption-only model ceiling is
  ``conditional_constraint`` and STILL non-unique (two compatible
  states).  ms7a.out stays BLOCKED (r10/r11 verdicts byte-quoted,
  unchanged).
- B. the necessity-rule basis: the conventions.json L72-95
  REDISPERSION_V1 extract plus the non-inversion analysis (what the
  whitelist can ground: rule applicability; what it cannot: necessity,
  membership, uniqueness).
- C. the counter-example matrix (10 cases): real Control (calibration
  anchor), whitelist inversion, two compatible states, instance/branch
  swap, first/second invocation swap, cross-group E10 export (real
  signed Etching group; the SI S5 caption keeps its TRUE post-etching
  stage), circular dependency, source mutation, same-group cross-stage
  citation (``stage_mismatch_rejected``), and blank sourceless direct
  evidence (``evidence_identity_missing_rejected``).
- D. the fixed-constraints audit: ``diagnostics_only=true`` /
  ``feeds_verdict=false`` on every record; the ZERO-token-mint assertion
  (the whole study runs inside a guard that fails the run on any call to
  the verified or diagnostic-assumption mint channels, plus a
  source-level check that the diagnostic module references no token
  constructor and imports no contracts module); the v1-untouched
  assertion (protocol-definition/v1 module digest + closed-slot probe);
  the ms7a.out / ms7b.in / ms7b.out verdicts byte-quoted from the
  committed r11 replay, unchanged.

The diagnostic prototype lives in
``reaserch_agent/route_operation_precondition_diagnostic.py`` (pure
functions + frozen data classes; no engine derives; no tokens).
``chem_agent_contracts/`` is not touched.  The runner is deterministic:
two runs produce byte-identical replay and audit JSONs.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest import mock
import json
import sys

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from chem_agent_contracts import route_convention_basis
from chem_agent_contracts import route_proof_dag
from chem_agent_contracts.route_protocol_reference import (
    PROTOCOL_DEFINITION_SCHEMA,
    PROTOCOL_REFERENCE_RULE_ID,
    PROTOCOL_REFERENCE_RULE_VERSION,
    validate_protocol_definition,
)
from chem_agent_contracts.route_retained_object import (
    build_excerpt_span_resolver,
)
from chem_agent_contracts.v2 import (
    LineageRelationV2,
    LogicalContainerV2,
    MaterialOperationSegmentV2,
    MaterialRelationV2,
)
from reaserch_agent.route_operation_precondition_diagnostic import (
    ASSUMPTION,
    BINDING_MISMATCH_REJECTED,
    CIRCULAR_DEPENDENCY_REJECTED,
    CONDITIONAL_CONSTRAINT,
    DIRECT_EVIDENCE,
    EVIDENCE_IDENTITY_MISSING_REJECTED,
    INSUFFICIENT,
    INVERSION_REJECTED,
    INVOCATION_SWAP_REJECTED,
    P1_NECESSARY_INPUT_CONDITION,
    P2_THIS_MATERIAL_FLOW,
    P3_MATERIAL_INSTANCE_BINDING,
    PAPER_EXPLICIT,
    PROPOSAL_ASSERTION,
    PROPOSITION_SUBJECTS,
    RULE_COMPATIBLE_STATES,
    SCOPE_MISMATCH_REJECTED,
    STAGE_MISMATCH_REJECTED,
    STALE_SOURCE_INVALIDATED,
    SUPPLEMENT_EXPLICIT,
    CandidateModelV1,
    DependencyRelationV1,
    EvidenceItemV1,
    PropositionClaimV1,
    ScopeBindingV1,
    SourceRefV1,
    build_dependency_view,
    evaluate_candidate_model,
    evidence_content_digest,
    record_to_dict,
)
from reaserch_agent.route_pdf_groups import (
    enumerate_attested_pdf_experimental_groups,
)
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
from reaserch_agent.run_research_agent import load_route_trust_config

root = Path(__file__).resolve().parents[2]
out_dir = root / "result" / "operation-structure-20260928"
PROPOSAL_PATH = out_dir / "local-revision-r10-proposal.json"
R8_PROPOSAL_PATH = out_dir / "local-revision-r8-proposal.json"
R11_REPLAY_PATH = out_dir / "local-revision-r11-replay.json"
CONVENTIONS_PATH = (root / "chem_resources" / "chemistry_conventions"
                    / "conventions.json")
DIAGNOSTIC_MODULE_PATH = (root / "reaserch_agent"
                          / "route_operation_precondition_diagnostic.py")
PROTOCOL_REFERENCE_MODULE_PATH = (root / "chem_agent_contracts"
                                  / "route_protocol_reference.py")

MS7A_OUT = "material_graph[7].material_outputs[0].state"
MS7B_IN = "material_graph[8].material_inputs[0].state"
MS7B_OUT = "material_graph[8].material_outputs[0].state"
G9_IN = "material_graph[9].material_inputs[0].state"
G9_OUT = "material_graph[9].material_outputs[0].state"

# The verified 3D protocol_reference node ids, byte-quoted from the
# committed r11 replay (they prove operation ORDER only).
PR_NODE_GRAPH7 = "proof_node_1e3047294567ed7b6b467dde"
PR_NODE_GRAPH8 = "proof_node_181d2b69af719f90c74cf2c5"

# The E10 counter-example (supplement_explicit): SI page S5, Figure S1
# caption — recorded in si-search/SI_SEARCH_MEMO.md.
E10_SI_CAPTION = ("(f) Optical image of NiFe E10, obtained after etching "
                  "with 10 mL nitric acid, showing clear salt solution "
                  "without precipitates.")
E10_SI_PROVENANCE = "si-search/SI_SEARCH_MEMO.md section 3 (SI S5, Figure S1 caption)"

NON_INVERSION_NOTE = (
    "the REDISPERSION_V1 allowed_input_states whitelist defines rule "
    "applicability only: it may be recorded as rule_compatible_states "
    "and proves neither that the actual input belongs to the set nor "
    "WHICH member it is; inverting it into a necessity basis is "
    "rejected as inversion_rejected")

# The mint channels this round must never touch (verified and
# diagnostic-assumption alike).
_MINT_TARGETS = (
    (route_convention_basis, "_VerifiedParentStateEvidence"),
    (route_convention_basis, "_VerifiedLiquidMedium"),
    (route_convention_basis, "_DiagnosticParentStateAssumption"),
    (route_convention_basis, "_DiagnosticLiquidMediumAssumption"),
    (route_convention_basis, "_mint_diagnostic_parent_state_assumption"),
    (route_convention_basis, "_mint_diagnostic_liquid_medium_assumption"),
    (route_proof_dag, "_mint_verified_parent_token"),
    (route_proof_dag, "_mint_verified_liquid_token"),
)


def _fail(message: str) -> None:
    raise SystemExit(f"local-revision-r12 contract/preflight failure: {message}")


def _mint_blocked(*args, **kwargs):
    raise SystemExit("local-revision-r12 zero-token-mint violated: a mint "
                     "channel was called during the Round 3E study")


# ---------------------------------------------------------------------------
# Preflight (identical in shape to the r11 runner; the r10 proposal stays
# read-only).
# ---------------------------------------------------------------------------


def _validate_contracts(proposal: dict) -> list[dict]:
    rows: list[dict] = []
    for step_index, step in enumerate(proposal["material_graph"]):
        step_id = step.get("macro_step_id", step_index)
        for relation in step.get("material_relations", []) or []:
            try:
                MaterialRelationV2.model_validate(relation, strict=True)
            except (TypeError, ValueError) as exc:
                _fail(f"material_graph[{step_index}] ({step_id}) relation "
                      f"{relation.get('relation_id')!r}: {exc}")
            rows.append({"step": step_id, "kind": "MaterialRelationV2",
                         "id": relation["relation_id"], "status": "valid"})
        for segment in step.get("operation_segments", []) or []:
            try:
                MaterialOperationSegmentV2.model_validate(segment,
                                                          strict=True)
            except (TypeError, ValueError) as exc:
                _fail(f"material_graph[{step_index}] ({step_id}) segment "
                      f"{segment.get('segment_id')!r}: {exc}")
            rows.append({"step": step_id,
                         "kind": "MaterialOperationSegmentV2",
                         "id": segment["segment_id"], "status": "valid"})
        lineage = step.get("lineage_relation")
        if lineage is not None:
            try:
                LineageRelationV2.model_validate(lineage, strict=True)
            except (TypeError, ValueError) as exc:
                _fail(f"material_graph[{step_index}] ({step_id}) lineage: {exc}")
            rows.append({"step": step_id, "kind": "LineageRelationV2",
                         "id": lineage["relation_type"], "status": "valid"})
        for container in step.get("logical_containers", []) or []:
            try:
                LogicalContainerV2.model_validate(container, strict=True)
            except (TypeError, ValueError) as exc:
                _fail(f"material_graph[{step_index}] ({step_id}) container "
                      f"{container.get('logical_container_id')!r}: {exc}")
            rows.append({"step": step_id, "kind": "LogicalContainerV2",
                         "id": container["logical_container_id"],
                         "status": "valid"})
    return rows


def _bind_all_excerpts(proposal: dict, group) -> list[dict]:
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    rows: list[dict] = []
    for fact in proposal["route_facts"]:
        binding, issue = bind_pdf_quote(
            blocks, fact.get("excerpt"), caption_block_locators=captions)
        if issue not in ("", "fact_excerpt_span_too_long"):
            _fail(f"fact {fact.get('fact_id')} ({fact.get('field_path')}) "
                  f"excerpt does not bind: {issue}")
        rows.append({"fact_id": fact["fact_id"],
                     "field_path": fact["field_path"],
                     "locator": binding.locator if binding else "",
                     "binding_issue": issue})
    return rows


# ---------------------------------------------------------------------------
# Section A: the A01 diagnosis (real signed Control group).
# ---------------------------------------------------------------------------


def _a01_diagnosis(proposal: dict, group, r11_replay: dict) -> dict:
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    by_id = {fact["fact_id"]: fact for fact in facts}
    ref = proposal["source_group_ref"]
    view = build_dependency_view(graph)

    control_scope = ScopeBindingV1(
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        stage="ms7a", invocation="second")
    source = SourceRefV1(
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        source_digest=ref["source_digest"],
        figure_snapshot_digest="")

    # --- P1 evidence: the operation NAME (paper_explicit, affirms the
    # operation's occurrence only) + the whitelist (rule_compatible_states
    # ONLY, with the non-inversion note).  SI = D: no retained-phase
    # statement for the second centrifugation exists anywhere.
    p1_evidence = (
        EvidenceItemV1(
            content=by_id["f_g7a_op"]["excerpt"],
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="fact:f_g7a_op",
            subject="operation_occurrence",
            scope=control_scope),
        EvidenceItemV1(
            content=('REDISPERSION_V1 1.1.0 allowed_input_states = '
                     '["retained_wet_solid", "washed_wet_solid"], '
                     'output_states = ["suspension"], '
                     'liquid_participation.required = true'),
            source_identity="",
            inference_nature=RULE_COMPATIBLE_STATES,
            provenance=("chem_resources/chemistry_conventions/"
                        "conventions.json L72-95"),
            subject="rule_applicability"),
    )
    # --- P2 evidence: the proposal's own parent_output_refs
    # (proposal_assertion) + the 3D protocol_reference nodes (prove
    # operation ORDER only; inter_segment_material_flow is a forbidden
    # slot, so they are constitutionally silent on material flow).
    parent_refs = graph[8]["material_inputs"][0]["parent_output_refs"]
    p2_evidence = (
        EvidenceItemV1(
            content=("material_graph[8].material_inputs[0]."
                     "parent_output_refs = " + json.dumps(
                         parent_refs, ensure_ascii=False, sort_keys=True)),
            source_identity="",
            inference_nature=PROPOSAL_ASSERTION,
            provenance="proposal:parent_output_refs",
            subject="proposal_drawn_edge",
            material_instance_id="inst_ldh_wet_2"),
        EvidenceItemV1(
            content=("protocol_reference nodes " + PR_NODE_GRAPH7 +
                     " (graph[7].operation) and " + PR_NODE_GRAPH8 +
                     " (graph[8].operation) inherit operation_sequence "
                     "[centrifugation, redispersion] and liquid_medium "
                     "deionized water from the SAME-group definition "
                     "(evidence route_fact_7931591ece6209db307915ee = "
                     "f_g5_op); the 3D charter forbids "
                     "inter_segment_material_flow as a slot, so the nodes "
                     "say NOTHING about material flow"),
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="r11:protocol_reference_nodes",
            subject="operation_sequence_order",
            scope=control_scope),
    )
    # --- P3 evidence: "all the samples are collected" — paper_explicit
    # but collective and non-individuating, AND cross-stage: the quote
    # is the ms8 (final collection) step's input statement, cited here
    # in the ms7a diagnosis, so the evaluator rejects it
    # (stage_mismatch_rejected) rather than letting it stand.
    p3_evidence = (
        EvidenceItemV1(
            content=by_id["f_g8_in0_state"]["excerpt"],
            source_identity=PAPER_EXPLICIT,
            inference_nature=DIRECT_EVIDENCE,
            provenance="fact:f_g8_in0_state",
            subject="collective_collection_statement",
            scope=ScopeBindingV1(
                paper_id=ref["paper_id"],
                experimental_group_id=ref["experimental_group_id"],
                stage="ms8", invocation="")),
    )

    model = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=control_scope,
        target_material_instance_id="inst_ldh_wet_2",
        compatible_states=("retained_wet_solid", "washed_wet_solid"),
        recorded_assumptions=(
            "flow assumption: ms7b.in is assumed to come from THIS "
            "ms7a.out (the text contains no explicit inter-segment "
            "material-flow statement)",
            "continuity assumption: the material instance under proof is "
            "assumed to be the same LDH-seeds batch across the second "
            "protocol invocation"),
        propositions=(
            PropositionClaimV1(
                P1_NECESSARY_INPUT_CONDITION,
                evidence=p1_evidence,
                assumptions=(
                    "SI = D: the three targeted evidence classes were not "
                    "detected — the SI neither supplies the retained "
                    "phase nor provides positive basis for reverse "
                    "inference",
                    "the paper names the operation only ('second "
                    "centrifugation−redispersion protocol'); no "
                    "retained-phase statement for the second "
                    "centrifugation exists in paper or SI",)),
            PropositionClaimV1(
                P2_THIS_MATERIAL_FLOW,
                evidence=p2_evidence,
                assumptions=(
                    "flow assumption: the proposal-drawn edge is taken "
                    "as the material flow",)),
            PropositionClaimV1(
                P3_MATERIAL_INSTANCE_BINDING,
                evidence=p3_evidence,
                assumptions=(
                    "continuity assumption: the collective 'all the "
                    "samples' statement is taken to cover the instance "
                    "under proof",)),
        ))

    record = evaluate_candidate_model(
        model,
        dependency_view=view,
        source=source,
        alternative_explanations=(
            "supernatant-retained-instead reading: the text does not "
            "constrain which phase the second centrifugation kept — the "
            "retained phase could be the supernatant rather than the "
            "solid",
            "aliquot/portion flow: the suspension was divided into 8 "
            "parts, so the ms7b input could be an aliquot or a "
            "recombination rather than the whole ms7a output",
            "whitelist ambiguity: washed_wet_solid is as compatible with "
            "REDISPERSION_V1 as retained_wet_solid — the candidate state "
            "is not singled out",
            "'all the samples are collected' may describe the "
            "post-redispersion suspensions — this reading is COMPATIBLE "
            "with a retained wet-solid intermediate (the collection "
            "follows the redispersion), so the two readings can hold at "
            "the same time; it is not a mutually exclusive alternative "
            "and excluding the suspension reading is not a promotion "
            "precondition — the quote merely fails to individuate the "
            "instance under proof",
        ),
        dependency_relations=(
            DependencyRelationV1(
                dependent=MS7B_IN, depends_on=MS7A_OUT,
                kind="dependency_cascade",
                note="ms7b.in <- ms7a.out (the cascade under diagnosis)"),
            DependencyRelationV1(
                dependent=MS7B_OUT, depends_on=MS7B_IN,
                kind="dependency_cascade",
                note="ms7b.out <- ms7b.in (within-step)"),
            DependencyRelationV1(
                dependent=G9_IN, depends_on=MS7B_OUT,
                kind="dependency_cascade",
                note="graph[9].in <- ms7b.out"),
            DependencyRelationV1(
                dependent="(this diagnosis)", depends_on="(downstream state)",
                kind="cycle_guard",
                note="the diagnosis cites NO downstream state as support "
                     "— the evaluator's own dependency view confirms "
                     "ms7b.in/ms7b.out/graph[9].in/graph[9].out all sit "
                     "in the downstream closure of ms7a.out and any "
                     "citation of them would be "
                     "circular_dependency_rejected"),
        ),
        open_item_overrides={
            P1_NECESSARY_INPUT_CONDITION: (
                "an independent necessity basis for the second "
                "centrifugation's input state (a paper/SI retained-phase "
                "statement supplies upstream state evidence but does not "
                "by itself prove the state is a NECESSARY input of the "
                "redispersion)"),
            P2_THIS_MATERIAL_FLOW: (
                "an explicit inter-segment material-flow statement "
                "binding ms7b.in to THIS ms7a.out"),
            P3_MATERIAL_INSTANCE_BINDING: (
                "instance-individuating language naming the instance "
                "under proof (not a collective 'all the samples' "
                "statement)"),
        },
        non_inversion_note=NON_INVERSION_NOTE)

    # The evaluated node's verdict is byte-quoted from the committed r11
    # replay — the diagnostic never changes it.
    r11_rows = {row["field_path"]: row for row in
                r11_replay["round3c_acceptance_carryover"]
                          ["final_state_table"]["nodes"]}
    byte_quoted = {
        path: {key: r11_rows[path][key]
               for key in ("node", "verdict", "issue", "attribution")}
        for path in (MS7A_OUT, MS7B_IN, MS7B_OUT)
    }

    record_payload = record_to_dict(record)
    # The ONLY rejection the honest Control diagnosis records: the ms8
    # collective collection statement is cross-stage evidence in the
    # ms7a diagnosis (same paper/group, different stage).
    ok = (
        record.conclusion == INSUFFICIENT
        and {d.proposition: d.verdict for d in record.propositions} == {
            P1_NECESSARY_INPUT_CONDITION: "unproven",
            P2_THIS_MATERIAL_FLOW: "unproven",
            P3_MATERIAL_INSTANCE_BINDING: "unproven"}
        and [r.code for r in record.rejections] == [
            STAGE_MISMATCH_REJECTED]
        and record.rejections[0].proposition \
            == P3_MATERIAL_INSTANCE_BINDING
        and record.rejections[0].provenance == "fact:f_g8_in0_state"
        and record.assumption_only_model["ceiling"] == CONDITIONAL_CONSTRAINT
        and record.assumption_only_model["non_unique"] is True
        and record.assumption_only_model["compatible_states"] == [
            "retained_wet_solid", "washed_wet_solid"]
        and record.node_verdict_unchanged == "BLOCKED"
        and all(row["verdict"] == "BLOCKED" for row in byte_quoted.values())
        and view.downstream_closure(MS7A_OUT) == tuple(sorted(
            [MS7B_IN, MS7B_OUT, G9_IN, G9_OUT]))
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "data_basis": ("REAL signed NiFe Control group on the unchanged "
                       "r10 proposal (read-only): 23 signed blocks, 87 "
                       "route facts; r11 protocol_reference node ids "
                       "byte-quoted from the committed r11 replay"),
        "target": {
            "state_path": MS7A_OUT,
            "node": "ms7a.out (graph[7].out)",
            "candidate_state_under_diagnosis": "retained_wet_solid",
            "experimental_group": ref["experimental_group_id"],
            "stage": "ms7a (second centrifugation segment, graph[7])",
            "invocation": "second",
        },
        "diagnostic_record": record_payload,
        "dependency_view": {
            "parents_of_ms7b_in": list(view.parents_of(MS7B_IN)),
            "downstream_closure_of_ms7a_out": list(
                view.downstream_closure(MS7A_OUT)),
            "cycle_guard": ("the evaluator builds its own dependency "
                            "view from the proposal graph; the diagnosis "
                            "cites no downstream state"),
        },
        "r10_r11_verdicts_byte_quoted_unchanged": byte_quoted,
        "conclusion": {
            "value": record.conclusion,
            "assumption_only_ceiling": CONDITIONAL_CONSTRAINT,
            "non_unique": True,
            "note": ("the study completes with insufficient — that is "
                     "the EXPECTED calibration, a complete research "
                     "outcome, not a failure; ms7a.out stays BLOCKED"),
        },
    }


# ---------------------------------------------------------------------------
# Section B: the necessity-rule basis (conventions.json L72-95).
# ---------------------------------------------------------------------------


def _necessity_rule_basis() -> dict:
    conventions = json.loads(CONVENTIONS_PATH.read_text(encoding="utf-8"))
    rules = conventions if isinstance(conventions, list) else (
        conventions.get("rules")
        or conventions.get("conventions")
        or next(v for v in conventions.values() if isinstance(v, list)))
    redispersion = next(
        rule for rule in rules if rule.get("rule_id") == "REDISPERSION_V1")
    extract = {
        "rule_id": redispersion["rule_id"],
        "version": redispersion["version"],
        "allowed_input_states": redispersion["allowed_input_states"],
        "output_states": redispersion["output_states"],
        "liquid_participation": redispersion["liquid_participation"],
    }
    lines = CONVENTIONS_PATH.read_text(encoding="utf-8").splitlines()
    quoted = [line for line in lines[71:95]]  # L72-95 (1-based)
    ok = (
        extract["rule_id"] == "REDISPERSION_V1"
        and extract["version"] == "1.1.0"
        and extract["allowed_input_states"] == [
            "retained_wet_solid", "washed_wet_solid"]
        and extract["output_states"] == ["suspension"]
        and extract["liquid_participation"]["required"] is True
        and any('"rule_id": "REDISPERSION_V1"' in line for line in quoted)
        and any('"allowed_input_states"' in line for line in quoted)
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "source": "chem_resources/chemistry_conventions/conventions.json "
                  "L72-95",
        "extract": extract,
        "quoted_lines_L72_95": quoted,
        "non_inversion_analysis": {
            "what_the_whitelist_can_ground": [
                "rule_compatible_states: the set of input states the "
                "REDISPERSION_V1 forward rule is defined for — rule "
                "applicability"],
            "what_it_cannot_ground": [
                "necessity: that the paper's second centrifugation "
                "output IS a necessary input of the downstream "
                "redispersion (that needs an independent basis)",
                "membership: that whatever the paper calls redispersion "
                "actually had an input in the set",
                "uniqueness: that the input was uniquely "
                "retained_wet_solid rather than washed_wet_solid"],
            "inversion_note": NON_INVERSION_NOTE,
        },
    }


# ---------------------------------------------------------------------------
# Section C: the counter-example matrix (10 cases).
# ---------------------------------------------------------------------------


def _case(scenario: str, expected: str, actual: str, ok: bool) -> dict:
    return {"scenario": scenario, "expected": expected, "actual": actual,
            "verdict": "PASS" if ok else "FAIL"}


def _counter_example_matrix(proposal: dict, group, inventory,
                            a01_record: dict) -> dict:
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    by_id = {fact["fact_id"]: fact for fact in facts}
    ref = proposal["source_group_ref"]
    view = build_dependency_view(graph)
    control_scope = ScopeBindingV1(
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        stage="ms7a", invocation="second")
    source = SourceRefV1(
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        source_digest=ref["source_digest"])
    whitelist = EvidenceItemV1(
        content=('REDISPERSION_V1 1.1.0 allowed_input_states = '
                 '["retained_wet_solid", "washed_wet_solid"]'),
        source_identity="",
        inference_nature=RULE_COMPATIBLE_STATES,
        provenance=("chem_resources/chemistry_conventions/"
                    "conventions.json L72-95"),
        subject="rule_applicability")
    cases: list[dict] = []

    # Case 1: real Control -> insufficient (section A restated as the
    # calibration anchor).
    cases.append(_case(
        scenario=("real Control group, candidate ms7a.out = "
                  "retained_wet_solid, honest evidence labelling "
                  "(section A restated)"),
        expected="insufficient",
        actual=a01_record["conclusion"],
        ok=a01_record["conclusion"] == INSUFFICIENT))

    # Case 2: whitelist inversion -> inversion_rejected (over-claim), the
    # output classified non-authoritative.
    inversion_model = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=control_scope,
        target_material_instance_id="inst_ldh_wet_2",
        compatible_states=("retained_wet_solid", "washed_wet_solid"),
        propositions=(
            PropositionClaimV1(
                P1_NECESSARY_INPUT_CONDITION,
                evidence=(EvidenceItemV1(
                    content=whitelist.content,
                    source_identity=whitelist.source_identity,
                    inference_nature=whitelist.inference_nature,
                    provenance=whitelist.provenance,
                    subject=whitelist.subject,
                    presented_as_necessity_basis=True),),
                asserted_verdict="proven"),))
    inversion_record = evaluate_candidate_model(
        inversion_model, dependency_view=view, source=source)
    inversion_codes = [r.code for r in inversion_record.rejections]
    cases.append(_case(
        scenario=("a model treating rule_compatible_states as the "
                  "necessity basis (forward-rule inversion)"),
        expected=("inversion_rejected; the model's output classified "
                  "non-authoritative; conclusion stays insufficient"),
        actual=(f"rejections={inversion_codes}; conclusion="
                f"{inversion_record.conclusion}"),
        ok=(INVERSION_REJECTED in inversion_codes
            and inversion_record.conclusion == INSUFFICIENT)))

    # Case 3: two compatible states -> non_unique; cannot single out
    # retained_wet_solid even under the (rejected) inversion.
    ceiling = inversion_record.assumption_only_model
    cases.append(_case(
        scenario=("two compatible states: even under inversion the "
                  "candidate set is {retained_wet_solid, "
                  "washed_wet_solid}"),
        expected=("non_unique — the model cannot single out "
                  "retained_wet_solid"),
        actual=(f"non_unique={ceiling['non_unique']}; compatible_states="
                f"{ceiling['compatible_states']}"),
        ok=(ceiling["non_unique"] is True
            and sorted(ceiling["compatible_states"]) == [
                "retained_wet_solid", "washed_wet_solid"])))

    # Case 4: instance/branch swap -> binding mismatch rejected.
    swap_scope = control_scope
    instance_swap = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=swap_scope,
        target_material_instance_id="inst_ldh_wet_2",
        propositions=(
            PropositionClaimV1(
                P3_MATERIAL_INSTANCE_BINDING,
                evidence=(
                    EvidenceItemV1(
                        content=("the ms7a INPUT instance (inst_ldh_aged, "
                                 "the aged LDH seeds suspension) — a "
                                 "different branch of the flow"),
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="proposal:material_graph[7]."
                                   "material_inputs[0]",
                        subject="material_instance_identity",
                        scope=swap_scope,
                        material_instance_id="inst_ldh_aged"),),
                asserted_verdict="proven"),))
    swap_record = evaluate_candidate_model(
        instance_swap, dependency_view=view, source=source)
    swap_codes = [r.code for r in swap_record.rejections]
    cases.append(_case(
        scenario=("instance/branch swap: evidence individuating "
                  "inst_ldh_aged (the ms7a INPUT, a different instance) "
                  "cited for the instance under proof inst_ldh_wet_2"),
        expected="binding_mismatch_rejected",
        actual=f"rejections={swap_codes}",
        ok=BINDING_MISMATCH_REJECTED in swap_codes))

    # Case 5: first/second invocation swap — the REAL first-invocation
    # naming evidence ("The precipitates were labeled as LDH seeds") is
    # applied to the SECOND invocation -> invocation_swap_rejected.
    naming_fact = by_id["f_g5_out0_name"]
    invocation_swap = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=control_scope,
        target_material_instance_id="inst_ldh_wet_2",
        propositions=(
            PropositionClaimV1(
                P3_MATERIAL_INSTANCE_BINDING,
                evidence=(
                    EvidenceItemV1(
                        content=naming_fact["excerpt"],
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="fact:f_g5_out0_name",
                        subject="material_instance_identity",
                        scope=ScopeBindingV1(
                            paper_id=ref["paper_id"],
                            experimental_group_id=
                            ref["experimental_group_id"],
                            stage="ms5", invocation="first"),
                        material_instance_id="inst_ldh_wet_2"),),
                asserted_verdict="proven"),))
    invocation_record = evaluate_candidate_model(
        invocation_swap, dependency_view=view, source=source)
    invocation_codes = [r.code for r in invocation_record.rejections]
    invocation_detail = next(
        (r.detail for r in invocation_record.rejections
         if r.code == INVOCATION_SWAP_REJECTED), "")
    cases.append(_case(
        scenario=("first-invocation retained-object evidence "
                  "('The precipitates were labeled as LDH seeds', real "
                  "signed fact f_g5_out0_name, invocation 'first') "
                  "applied to the SECOND invocation"),
        expected=("invocation_swap_rejected — result inheritance is "
                  "forbidden by 3D; recorded as a swap, NOT as evidence"),
        actual=f"rejections={invocation_codes}",
        ok=(INVOCATION_SWAP_REJECTED in invocation_codes
            and "NOT as evidence" in invocation_detail)))

    # Case 6: cross-group E10 export (REAL signed Etching group + SI S5
    # caption): the same protocol language coexists with a
    # no-precipitate outcome in another group; the evaluator refuses to
    # export Control's conditional constraint into the Etching scope and
    # keeps group/stage/invocation explicit.  The SI caption keeps its
    # TRUE stage: "obtained after etching with 10 mL nitric acid ...
    # without precipitates" documents the post-ETCHING outcome of E10 —
    # the etching step (signature-KB etching protocol blocks
    # pdf:p2:b85/b86; SI S5 Figure S1 caption context), NOT the
    # second-wash stage under diagnosis, and it is tied to no
    # centrifugation−redispersion invocation (invocation stays empty) —
    # so the caption is rejected stage_mismatch_rejected inside the
    # second-wash diagnosis.
    matches = [g for g in inventory.groups
               if "Etching Method" in g.source_scope.experimental_group_id]
    if len(matches) != 1:
        _fail(f"expected exactly one Etching group in the signed KB, "
              f"got {len(matches)}")
    etching = matches[0]
    etching_blocks = {block.locator: block.text for block in etching.blocks}
    etching_protocol_text = (
        etching_blocks.get("pdf:p2:b85-p2:b85", "") + " "
        + etching_blocks.get("pdf:p2:b86-p2:b86", ""))
    etching_scope = ScopeBindingV1(
        paper_id=etching.source_scope.paper_id,
        experimental_group_id=etching.source_scope.experimental_group_id,
        stage="etching_second_wash", invocation="second")
    e10_caption_scope = ScopeBindingV1(
        paper_id=etching.source_scope.paper_id,
        experimental_group_id=etching.source_scope.experimental_group_id,
        stage="etching", invocation="")
    # The export attempt: a Control-scoped conditional-constraint input
    # (the operation-name item, scope Control) is cited inside an
    # Etching-scoped diagnosis.
    export_model = CandidateModelV1(
        target_state_path="etching.second_wash.output.state",
        candidate_state="no_precipitate",
        scope=etching_scope,
        target_material_instance_id="inst_nife_e10",
        propositions=(
            PropositionClaimV1(
                P1_NECESSARY_INPUT_CONDITION,
                evidence=(
                    EvidenceItemV1(
                        content=by_id["f_g7a_op"]["excerpt"],
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="fact:f_g7a_op",
                        subject="operation_occurrence",
                        scope=control_scope),
                    EvidenceItemV1(
                        content=etching_protocol_text.strip(),
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance=("etching signed blocks "
                                    "pdf:p2:b85-p2:b85 + pdf:p2:b86-p2:b86"),
                        subject="operation_occurrence",
                        scope=etching_scope),
                    EvidenceItemV1(
                        content=E10_SI_CAPTION,
                        source_identity=SUPPLEMENT_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance=E10_SI_PROVENANCE,
                        subject="post_etching_outcome",
                        scope=e10_caption_scope),),
                asserted_verdict="unproven"),))
    export_record = evaluate_candidate_model(
        export_model, dependency_view=view,
        source=SourceRefV1(
            paper_id=etching.source_scope.paper_id,
            experimental_group_id=
            etching.source_scope.experimental_group_id,
            source_digest=etching.source_scope.source_digest,
            figure_snapshot_digest=evidence_content_digest(E10_SI_CAPTION)))
    export_codes = [r.code for r in export_record.rejections]
    export_detail = next(
        (r.detail for r in export_record.rejections
         if r.code == SCOPE_MISMATCH_REJECTED), "")
    caption_detail = next(
        (r.detail for r in export_record.rejections
         if r.code == STAGE_MISMATCH_REJECTED), "")
    e10_ok = (
        SCOPE_MISMATCH_REJECTED in export_codes
        and STAGE_MISMATCH_REJECTED in export_codes
        and "NiFe Control" in export_detail
        and "Etching Method" in export_detail
        and "etching" in caption_detail
        and "etching_second_wash" in caption_detail
        and export_record.scope.experimental_group_id
        == etching.source_scope.experimental_group_id
        and export_record.scope.invocation == "second"
        and export_record.conclusion == INSUFFICIENT
        and len(etching.blocks) == 10
        and "second centrifugation−redispersion/washing protocol"
        in etching_protocol_text
        and "to neutralize the sample" in etching_protocol_text)
    cases.append(_case(
        scenario=("cross-group E10 export: the REAL signed Etching "
                  "group (10 blocks: 'the second "
                  "centrifugation−redispersion/washing protocol was "
                  "performed three times to neutralize the sample') and "
                  "the SI S5 Figure S1 caption (E10: 'clear salt "
                  "solution without precipitates', supplement_explicit, "
                  "keeping its TRUE post-etching stage 'etching') — the "
                  "same protocol language coexists with a "
                  "no-precipitate outcome in another group"),
        expected=("scope_mismatch_rejected — Control's conditional "
                  "constraint is NOT exported into the Etching scope "
                  "(the Control-scoped operation-name item); "
                  "stage_mismatch_rejected — the caption's true "
                  "post-etching stage is not the second-wash stage "
                  "under diagnosis; group/stage/invocation stay "
                  "explicit"),
        actual=(f"rejections={export_codes}; diagnosis scope="
                f"{export_record.scope.experimental_group_id!r}/"
                f"{export_record.scope.stage}/"
                f"{export_record.scope.invocation}; caption item stage="
                f"{e10_caption_scope.stage!r}; conclusion="
                f"{export_record.conclusion}"),
        ok=e10_ok))

    # Case 7: circular dependency — a candidate model citing ms7b.in
    # state as support for ms7a.out.
    circular_model = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=control_scope,
        target_material_instance_id="inst_ldh_wet_2",
        propositions=(
            PropositionClaimV1(
                P2_THIS_MATERIAL_FLOW,
                evidence=(
                    EvidenceItemV1(
                        content=("ms7b.in state = retained_wet_solid "
                                 "(the downstream input the proposal "
                                 "declares)"),
                        source_identity="",
                        inference_nature=ASSUMPTION,
                        provenance="proposal:material_graph[8]."
                                   "material_inputs[0].state",
                        subject="inter_segment_material_flow",
                        scope=control_scope,
                        state_path=MS7B_IN),),
                asserted_verdict="proven"),))
    circular_record = evaluate_candidate_model(
        circular_model, dependency_view=view, source=source)
    circular_codes = [r.code for r in circular_record.rejections]
    cases.append(_case(
        scenario=("circular dependency: a candidate model citing "
                  "ms7b.in state (which already depends on ms7a.out) as "
                  "support for ms7a.out"),
        expected="circular_dependency_rejected",
        actual=f"rejections={circular_codes}",
        ok=CIRCULAR_DEPENDENCY_REJECTED in circular_codes))

    # Case 8: source mutation — mutate the "LDH seeds" naming evidence;
    # every diagnostic item citing it recomputes/invalidates honestly.
    naming_item = EvidenceItemV1(
        content=naming_fact["excerpt"],
        source_identity=PAPER_EXPLICIT,
        inference_nature=DIRECT_EVIDENCE,
        provenance="fact:f_g5_out0_name",
        subject="material_instance_identity",
        scope=ScopeBindingV1(
            paper_id=ref["paper_id"],
            experimental_group_id=ref["experimental_group_id"],
            stage="ms5", invocation="first"),
        material_instance_id="inst_ldh_seeds")
    naming_model = CandidateModelV1(
        target_state_path="material_graph[5].material_outputs[0].state",
        candidate_state="retained_wet_solid",
        scope=ScopeBindingV1(
            paper_id=ref["paper_id"],
            experimental_group_id=ref["experimental_group_id"],
            stage="ms5", invocation="first"),
        target_material_instance_id="inst_ldh_seeds",
        propositions=(
            PropositionClaimV1(
                P3_MATERIAL_INSTANCE_BINDING, evidence=(naming_item,)),))
    live_untouched = {"fact:f_g5_out0_name": naming_fact["excerpt"]}
    record_untouched = evaluate_candidate_model(
        naming_model, dependency_view=view, source=source,
        live_sources=live_untouched)
    mutated_excerpt = ("The precipitates were labeled as NiFe hydroxide "
                       "seeds, which were dispersed in 30")
    live_mutated = {"fact:f_g5_out0_name": mutated_excerpt}
    record_mutated = evaluate_candidate_model(
        naming_model, dependency_view=view, source=source,
        live_sources=live_mutated)
    p3_mutated = next(d for d in record_mutated.propositions
                      if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
    recomputed_item = EvidenceItemV1(
        content=mutated_excerpt,
        source_identity=naming_item.source_identity,
        inference_nature=naming_item.inference_nature,
        provenance=naming_item.provenance,
        subject=naming_item.subject,
        scope=naming_item.scope,
        material_instance_id=naming_item.material_instance_id)
    record_recomputed = evaluate_candidate_model(
        CandidateModelV1(
            target_state_path=naming_model.target_state_path,
            candidate_state=naming_model.candidate_state,
            scope=naming_model.scope,
            target_material_instance_id=
            naming_model.target_material_instance_id,
            propositions=(
                PropositionClaimV1(
                    P3_MATERIAL_INSTANCE_BINDING,
                    evidence=(recomputed_item,)),)),
        dependency_view=view, source=source, live_sources=live_mutated)
    p3_recomputed = next(d for d in record_recomputed.propositions
                         if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
    record_untouched_rerun = evaluate_candidate_model(
        naming_model, dependency_view=view, source=source,
        live_sources=live_untouched)
    case8_ok = (
        next(d for d in record_untouched.propositions
             if d.proposition == P3_MATERIAL_INSTANCE_BINDING).verdict
        == "proven"
        and record_untouched.conclusion == INSUFFICIENT
        and any(r.code == STALE_SOURCE_INVALIDATED
                for r in record_mutated.rejections)
        and p3_mutated.verdict == "unproven"
        and p3_mutated.evidence == ()
        and len(record_mutated.invalidated_items) == 1
        and record_mutated.conclusion == INSUFFICIENT
        and p3_recomputed.verdict == "proven"
        and p3_recomputed.evidence[0].content_digest
        == evidence_content_digest(mutated_excerpt)
        and record_to_dict(record_untouched_rerun)
        == record_to_dict(record_untouched))
    cases.append(_case(
        scenario=("source mutation: the 'LDH seeds' naming evidence "
                  "(real signed fact f_g5_out0_name) is mutated to "
                  "'NiFe hydroxide seeds'"),
        expected=("every diagnostic item citing it recomputes or "
                  "invalidates honestly — no stale citation survives; "
                  "the untouched record still verifies under the "
                  "untouched source.  In this construction P1/P2 are "
                  "unproven BY CONSTRUCTION (no evidence is submitted "
                  "for them), so the mutation moves ONLY P3 "
                  "(proven → unproven); the overall conclusion is "
                  "insufficient BEFORE and AFTER — it never flips"),
        actual=(f"untouched: p3 verdict=proven, conclusion="
                f"{record_untouched.conclusion} (P1/P2 unproven by "
                f"construction); mutated: invalidated_items="
                f"{len(record_mutated.invalidated_items)}, p3 verdict="
                f"{p3_mutated.verdict}, conclusion="
                f"{record_mutated.conclusion} — the mutation moves P3 "
                f"proven→unproven only, no conclusion flip; recomputed "
                f"item digest moved and validates; untouched record "
                f"byte-identical on rerun"),
        ok=case8_ok))

    # Case 9: same-group cross-stage citation (acceptance hole 1) — the
    # REAL collective collection quote (f_g8_in0_state, an ms8-stage
    # statement) presented as instance-binding evidence for the ms7a
    # diagnosis with the SAME paper/group/invocation but a DIFFERENT
    # stage -> stage_mismatch_rejected (distinct from the cross-group
    # scope_mismatch_rejected of Case 6).
    cross_stage_model = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=control_scope,
        target_material_instance_id="inst_ldh_wet_2",
        propositions=(
            PropositionClaimV1(
                P3_MATERIAL_INSTANCE_BINDING,
                evidence=(
                    EvidenceItemV1(
                        content=by_id["f_g8_in0_state"]["excerpt"],
                        source_identity=PAPER_EXPLICIT,
                        inference_nature=DIRECT_EVIDENCE,
                        provenance="fact:f_g8_in0_state",
                        subject="material_instance_identity",
                        scope=ScopeBindingV1(
                            paper_id=ref["paper_id"],
                            experimental_group_id=
                            ref["experimental_group_id"],
                            stage="ms8", invocation="second"),
                        material_instance_id="inst_ldh_wet_2"),),
                asserted_verdict="proven"),))
    cross_stage_record = evaluate_candidate_model(
        cross_stage_model, dependency_view=view, source=source)
    cross_stage_codes = [r.code for r in cross_stage_record.rejections]
    cross_stage_p3 = next(
        d for d in cross_stage_record.propositions
        if d.proposition == P3_MATERIAL_INSTANCE_BINDING)
    cases.append(_case(
        scenario=("same-group cross-stage: the ms8 final-collection "
                  "statement (real signed fact f_g8_in0_state) presented "
                  "as instance-binding evidence for the ms7a diagnosis "
                  "— same paper, same group, same invocation 'second', "
                  "DIFFERENT stage (ms8 vs ms7a)"),
        expected=("stage_mismatch_rejected (distinguishable from the "
                  "cross-group scope_mismatch_rejected); P3 unproven"),
        actual=(f"rejections={cross_stage_codes}; p3 verdict="
                f"{cross_stage_p3.verdict}"),
        ok=(STAGE_MISMATCH_REJECTED in cross_stage_codes
            and SCOPE_MISMATCH_REJECTED not in cross_stage_codes
            and cross_stage_p3.verdict == "unproven"
            and cross_stage_p3.evidence == ())))

    # Case 10: blank, sourceless direct evidence (acceptance hole 2) —
    # empty content, no source identity, scope None, empty provenance,
    # submitted as direct_evidence with matching subject/instance for
    # all three propositions -> evidence_identity_missing_rejected on
    # each; all three unproven; conclusion insufficient.
    def _blank_item(proposition: str) -> EvidenceItemV1:
        return EvidenceItemV1(
            content="", source_identity="",
            inference_nature=DIRECT_EVIDENCE, provenance="",
            subject=PROPOSITION_SUBJECTS[proposition], scope=None,
            material_instance_id="inst_ldh_wet_2")

    blank_model = CandidateModelV1(
        target_state_path=MS7A_OUT,
        candidate_state="retained_wet_solid",
        scope=control_scope,
        target_material_instance_id="inst_ldh_wet_2",
        propositions=tuple(
            PropositionClaimV1(p, evidence=(_blank_item(p),))
            for p in (P1_NECESSARY_INPUT_CONDITION,
                      P2_THIS_MATERIAL_FLOW,
                      P3_MATERIAL_INSTANCE_BINDING)))
    blank_record = evaluate_candidate_model(
        blank_model, dependency_view=view, source=source)
    blank_codes = [r.code for r in blank_record.rejections]
    blank_verdicts = {d.proposition: d.verdict
                      for d in blank_record.propositions}
    cases.append(_case(
        scenario=("blank sourceless direct evidence: empty content, no "
                  "source identity, no scope, no provenance, submitted "
                  "as direct_evidence for all three propositions"),
        expected=("evidence_identity_missing_rejected on each "
                  "proposition; P1/P2/P3 all unproven; conclusion "
                  "insufficient"),
        actual=(f"rejections={blank_codes}; verdicts={blank_verdicts}; "
                f"conclusion={blank_record.conclusion}"),
        ok=(blank_codes == [EVIDENCE_IDENTITY_MISSING_REJECTED] * 3
            and all(v == "unproven" for v in blank_verdicts.values())
            and all(d.evidence == () and d.qualifying_evidence == ()
                    for d in blank_record.propositions)
            and blank_record.conclusion == INSUFFICIENT)))

    status = "PASS" if all(case["verdict"] == "PASS" for case in cases) \
        else "FAIL"
    return {"status": status, "case_count": len(cases), "cases": cases}


# ---------------------------------------------------------------------------
# Section D: the fixed-constraints audit.
# ---------------------------------------------------------------------------


def _fixed_constraints_audit(records: list[dict]) -> dict:
    # 1. diagnostics_only / feeds_verdict on every diagnostic output.
    flag_rows = []
    for label, payload in records:
        constraints = payload["diagnostic_record"]["constraints"] \
            if "diagnostic_record" in payload else payload["constraints"]
        flag_rows.append({
            "record": label,
            "diagnostics_only": constraints["diagnostics_only"],
            "feeds_verdict": constraints["feeds_verdict"],
            "ok": (constraints["diagnostics_only"] is True
                   and constraints["feeds_verdict"] is False)})
    flags_ok = all(row["ok"] for row in flag_rows)

    # 2. Zero-token-mint: the whole study ran inside the guard (armed in
    # main); here the source-level half — the diagnostic module references
    # no token constructor and imports no contracts module.
    module_source = DIAGNOSTIC_MODULE_PATH.read_text(encoding="utf-8")
    forbidden_strings = (
        "_VerifiedParentStateEvidence", "_VerifiedLiquidMedium",
        "_DiagnosticParentStateAssumption",
        "_DiagnosticLiquidMediumAssumption", "_mint_verified_",
        "_mint_diagnostic_", "chem_agent_contracts")
    leaked = [token for token in forbidden_strings if token in module_source]
    zero_mint_ok = not leaked

    # 3. v1 untouched: module digest + schema version + the closed-slot
    # probe (a forbidden slot is still rejected).
    pr_source = PROTOCOL_REFERENCE_MODULE_PATH.read_bytes()
    probe = validate_protocol_definition({
        "schema_version": PROTOCOL_DEFINITION_SCHEMA,
        "protocol_name": "probe",
        "operation_sequence": [],
        "liquid_medium": "",
        "inter_segment_material_flow": "probe"})
    v1_ok = (
        PROTOCOL_DEFINITION_SCHEMA == "protocol-definition/v1"
        and probe == "protocol_definition_unknown_key")

    return {
        "status": "PASS" if (flags_ok and zero_mint_ok and v1_ok) else "FAIL",
        "diagnostics_only_feeds_verdict": {
            "ok": flags_ok,
            "fixed_values": {"diagnostics_only": True,
                             "feeds_verdict": False},
            "records": flag_rows,
        },
        "zero_token_mint": {
            "ok": zero_mint_ok,
            "runtime_guard": {
                "armed_around_whole_study": True,
                "channels_sealed": [
                    f"{module.__name__}.{name}"
                    for module, name in _MINT_TARGETS],
                "self_test": ("each sealed channel was probed before the "
                              "study and raised SystemExit; the study "
                              "then ran to completion under the guard — "
                              "no verified token and no "
                              "diagnostic-assumption token was minted"),
            },
            "source_level_check": {
                "module": "reaserch_agent/"
                          "route_operation_precondition_diagnostic.py",
                "module_sha256": "sha256_" + sha256(
                    DIAGNOSTIC_MODULE_PATH.read_bytes()).hexdigest(),
                "forbidden_strings_found": leaked,
                "note": ("the diagnostic prototype references no token "
                         "constructor and imports nothing from "
                         "chem_agent_contracts"),
            },
        },
        "v1_untouched": {
            "ok": v1_ok,
            "module": "chem_agent_contracts/route_protocol_reference.py",
            "module_sha256": "sha256_" + sha256(pr_source).hexdigest(),
            "protocol_definition_schema": PROTOCOL_DEFINITION_SCHEMA,
            "rule_id": PROTOCOL_REFERENCE_RULE_ID,
            "rule_version": PROTOCOL_REFERENCE_RULE_VERSION,
            "closed_slot_probe": {
                "forbidden_slot": "inter_segment_material_flow",
                "validation_issue": probe,
                "expected": "protocol_definition_unknown_key",
            },
            "note": ("no v1 expansion: protocol-definition/v1 keeps its "
                     "closed slot set; chem_agent_contracts/ is not "
                     "modified by this round"),
        },
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _run_study() -> dict:
    envelope = json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))
    proposal = deepcopy(envelope["proposals"][0])
    r11_replay = json.loads(R11_REPLAY_PATH.read_text(encoding="utf-8"))

    contract_rows = _validate_contracts(proposal)

    base = root / "result" / "a01-v5-real-input-20260927"
    trust = load_route_trust_config(
        str(base / "route-trust-config.json"), str(base / "kb"))
    inventory = enumerate_attested_pdf_experimental_groups(
        base / "kb", trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"])
    matches = [g for g in inventory.groups if "NiFe Control" in
               g.source_scope.experimental_group_id]
    if len(matches) != 1:
        _fail(f"expected exactly one NiFe Control group, got {len(matches)}")
    group = matches[0]

    binding_rows = _bind_all_excerpts(proposal, group)
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    build_excerpt_span_resolver(blocks, captions)  # preflight parity

    # --- A. A01 diagnosis -------------------------------------------------
    section_a = _a01_diagnosis(proposal, group, r11_replay)
    # --- B. necessity-rule basis -----------------------------------------
    section_b = _necessity_rule_basis()
    # --- C. counter-example matrix ---------------------------------------
    section_c = _counter_example_matrix(
        proposal, group, inventory, section_a["diagnostic_record"])
    # --- D. fixed-constraints audit --------------------------------------
    record_payloads = [("a01_diagnosis", section_a["diagnostic_record"])]
    section_d = _fixed_constraints_audit(record_payloads)

    # The three r11 verdict rows byte-quoted (section D, second half).
    r11_rows = {row["field_path"]: row for row in
                r11_replay["round3c_acceptance_carryover"]
                          ["final_state_table"]["nodes"]}
    verdict_quotes = {
        path: {key: r11_rows[path][key]
               for key in ("node", "verdict", "issue", "attribution")}
        for path in (MS7A_OUT, MS7B_IN, MS7B_OUT)
    }
    quotes_ok = all(
        row["verdict"] == "BLOCKED"
        and row["issue"] == "retained_object_mention_precedes_operation"
        for row in verdict_quotes.values())
    section_d["r11_verdicts_byte_quoted"] = {
        "ok": quotes_ok,
        "source": ("local-revision-r11-replay.json "
                   "round3c_acceptance_carryover.final_state_table.nodes "
                   "(committed, historical record)"),
        "rows": verdict_quotes,
        "note": ("ms7a.out / ms7b.in / ms7b.out stay BLOCKED with the "
                 "r10/r11 issues, byte-quoted and unchanged — the "
                 "feasibility study changes no node verdict"),
    }
    if not quotes_ok:
        section_d["status"] = "FAIL"

    status = "PASS" if all(
        section["status"] == "PASS"
        for section in (section_a, section_b, section_c, section_d)) \
        else "FAIL"
    return {
        "status": status,
        "a01_diagnosis": section_a,
        "necessity_rule_basis": section_b,
        "counter_example_matrix": section_c,
        "fixed_constraints_audit": section_d,
        "_preflight": {
            "contract_rows": contract_rows,
            "binding_rows": binding_rows,
            "group": group,
        },
    }


def main() -> None:
    # Arm the zero-token-mint guard BEFORE any study code runs: every
    # mint channel (verified capability tokens and diagnostic-assumption
    # tokens alike) is sealed for the whole study.
    guards = [mock.patch.object(module, name, _mint_blocked)
              for module, name in _MINT_TARGETS]
    for guard in guards:
        guard.start()
    try:
        # Self-test: every sealed channel must raise.
        for module, name in _MINT_TARGETS:
            try:
                getattr(module, name)()
            except SystemExit:
                pass
            else:  # pragma: no cover - the guard must hold
                _fail(f"zero-token-mint guard not armed for {name}")
        study = _run_study()
    finally:
        for guard in guards:
            guard.stop()

    group = study["_preflight"]["group"]
    group_scope = group.source_scope
    round3e = {key: study[key] for key in (
        "status", "a01_diagnosis", "necessity_rule_basis",
        "counter_example_matrix", "fixed_constraints_audit")}

    audit = [
        {"kind": "round3e_diagnostic_contract",
         "module": "reaserch_agent/route_operation_precondition_diagnostic.py",
         "module_sha256": "sha256_" + sha256(
             DIAGNOSTIC_MODULE_PATH.read_bytes()).hexdigest(),
         "schema_version": "operation-precondition-diagnostic/v1",
         "charter": "docs/field_semantic_gate_3e_design_20261003.md",
         "propositions": [
             "necessary_input_condition (P1): what independent basis "
             "proves the condition is necessary — the forward whitelist "
             "may only appear as rule_compatible_states with the "
             "non-inversion note",
             "this_material_flow (P2): does the downstream input really "
             "come from THIS upstream output",
             "material_instance_binding (P3): does the evidence "
             "individuate the instance under proof — not other "
             "materials, invocations, or branches",
         ],
         "conclusion_vocabulary": ["insufficient",
                                   "conditional_constraint"],
         "rejection_codes": [
             "inversion_rejected", "non_unique",
             "binding_mismatch_rejected", "invocation_swap_rejected",
             "scope_mismatch_rejected", "stage_mismatch_rejected",
             "evidence_identity_missing_rejected",
             "circular_dependency_rejected",
             "stale_source_invalidated", "over_claim_rejected"],
         "design_rules": [
             "source identity (paper_explicit / supplement_explicit / "
             "external_primary) is recorded separately from the "
             "inference nature (direct_evidence / "
             "rule_compatible_states / proposal_assertion / assumption)",
             "a direct_evidence submission must carry a checkable "
             "identity — non-empty content, a real source identity, its "
             "own scope (paper/group/stage), and a provenance — or it "
             "is evidence_identity_missing_rejected before any other "
             "check; rule whitelists, proposal assertions, and "
             "assumptions carry no source identity by design and are "
             "exempt",
             "scope is explicit everywhere: cross-paper/cross-group "
             "evidence is scope_mismatch_rejected; same-group "
             "cross-stage evidence is stage_mismatch_rejected",
             "the evaluator builds its OWN dependency view from the "
             "proposal graph (parent_output_refs + within-step "
             "input->output edges) and rejects any candidate model whose "
             "support includes a downstream node it feeds",
             "an operation name or a proposal-drawn edge can never "
             "answer any of the three propositions",
             "evidence items are content-addressed; a mutated live "
             "source invalidates every citing item honestly",
             "pure functions + frozen data classes; no engine derives; "
             "no token of any kind is minted",
         ]},
        {"kind": "trust_boundary",
         "rules": [
             "diagnostics_only=true and feeds_verdict=false on every "
             "diagnostic output (owner-locked)",
             "ZERO tokens minted anywhere in this round: the study runs "
             "inside a guard sealing every mint channel (verified "
             "capability tokens and diagnostic-assumption tokens), and "
             "the diagnostic module references no token constructor",
             "no v1 expansion, no engine/gate changes, no changes to "
             "chem_agent_contracts/ at all",
             "the evaluated node stays BLOCKED — the diagnostic never "
             "changes node verdicts; the r10/r11 verdicts are "
             "byte-quoted, not recomputed",
             "the study may complete with insufficient — that is the "
             "EXPECTED calibration, a complete research outcome, not a "
             "failure",
         ]},
        {"kind": "group_identity",
         "paper_id": group_scope.paper_id,
         "experimental_group_id": group_scope.experimental_group_id,
         "source_digest": group_scope.source_digest,
         "block_count": len(group.blocks),
         "caption_count": sum(
             1 for block in group.blocks if block.caption)},
        {"kind": "deliberately_not_done",
         "items": [
             "no proof-class creation (the gate to a formal proof class "
             "is UNMET: independent necessity basis, invocation+material "
             "binding, alternatives excluded, counter-examples passing)",
             "no token minting of any kind (neither verified nor "
             "diagnostic-assumption channel)",
             "no chem_agent_contracts/ changes; no v1 schema changes; "
             "no engine changes",
             "no r7-r11 runner or JSON edits (the verdicts are "
             "byte-quoted, never rewritten)",
             "ms7a.out is not presupposed to PASS and is not closed; "
             "no attempt to make the conclusion stronger than the "
             "evidence supports",
             "no cross-group inference: the Etching counter-example "
             "keeps group/stage/invocation explicit and refuses the "
             "export",
         ]},
        {"kind": "contract_validation",
         "rows": study["_preflight"]["contract_rows"]},
        {"kind": "excerpt_binding",
         "rows": study["_preflight"]["binding_rows"]},
        {"kind": "round3e_feasibility", "items": round3e},
    ]

    result = {
        "audit_rows": len(audit),
        "group_identity": {
            "paper_id": group_scope.paper_id,
            "experimental_group_id": group_scope.experimental_group_id,
            "source_digest": group_scope.source_digest,
            "block_count": len(group.blocks),
            "caption_count": sum(
                1 for block in group.blocks if block.caption),
        },
        "proposal_reuse": {
            "path": "result/operation-structure-20260928/"
                    "local-revision-r10-proposal.json",
            "modified": False,
            "read_only": True,
            "sha256": "sha256_" + sha256(
                PROPOSAL_PATH.read_bytes()).hexdigest(),
            "derived_from": "result/operation-structure-20260928/"
                            "local-revision-r8-proposal.json",
            "derived_from_sha256": "sha256_" + sha256(
                R8_PROPOSAL_PATH.read_bytes()).hexdigest(),
            "r11_replay_byte_quote_source": (
                "result/operation-structure-20260928/"
                "local-revision-r11-replay.json"),
            "r11_replay_sha256": "sha256_" + sha256(
                R11_REPLAY_PATH.read_bytes()).hexdigest(),
        },
        "round3e_feasibility": round3e,
        "excerpt_binding_issue_counts": dict(Counter(
            row["binding_issue"]
            for row in study["_preflight"]["binding_rows"])),
    }
    (out_dir / "local-revision-r12-audit.json").write_text(
        json.dumps({
            "schema_version": "bounded_local_revision/v12",
            "model_generated": False,
            "scope": ("Round 3E: operation-precondition-inference "
                      "feasibility study (diagnostics only) over the "
                      "real signed NiFe Control group on the unchanged "
                      "r10 proposal — the A01 diagnosis (three "
                      "propositions answered separately with real "
                      "evidence; conclusion insufficient; assumption-only "
                      "ceiling conditional_constraint, still non-unique), "
                      "the necessity-rule basis (conventions.json L72-95 "
                      "REDISPERSION_V1 + non-inversion analysis), the "
                      "10-case counter-example matrix (real Control, "
                      "whitelist inversion, two compatible states, "
                      "instance/branch swap, invocation swap, "
                      "cross-group E10 export, circular dependency, "
                      "source mutation, same-group cross-stage "
                      "citation, blank sourceless direct evidence), "
                      "and the fixed-constraints audit "
                      "(diagnostics_only/feeds_verdict, zero-token-mint "
                      "guard, v1-untouched, r11 verdicts byte-quoted "
                      "unchanged). ms7a.out stays BLOCKED — a complete "
                      "research outcome, not a failure."),
            "audit": audit,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "local-revision-r12-replay.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(json.dumps(round3e, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
