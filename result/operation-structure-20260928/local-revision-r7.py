"""Round-7 (Round 2) retained-object source relation on graph[5]; same replay.

Round 6 left graph[5] (ms5) at ``convention_rule_not_applicable_or_ambiguous``:
the paper's composite operation ("centrifugation−redispersion protocol ...
three times") matches two convention rule families, the proposal named no
verbatim collect-precipitate intent, and no generic composite rule was —
or is — allowed.  Round 2 keeps every one of those hardening rules and adds
exactly one new, separate evidence layer:

- graph[5] (ms5) now declares the operation as the verbatim composite
  ``centrifugation−redispersion protocol`` (U+2212, an exact substring of
  the unchanged f_g5_op excerpt b70-b71), keeps repetitions="three" bound
  to the whole protocol by the text, and documents the composite's
  containment with a SECOND operation segment ``ms5-seg-redispersion``
  (material_effect ``unknown``: the composite contains redispersion, but no
  micro-sequence endpoint is claimed).  The state_change relation still
  references only ``ms5-seg-centrifugation``.
- A new SOURCE relation (``post-operation-retained-object/v1``,
  ``chem_agent_contracts/route_retained_object.py``) derives — from the
  signed group blocks only — that the naming sentence "The precipitates
  were labeled as LDH seeds" (b71-b72) FOLLOWS the composite operation
  (b70-b71) and binds the retained-object noun "precipitates" to the
  step's output label "LDH seeds".  The module performs no chemistry: it
  never maps the object to a state and never picks a rule.
- The convention layer (``route_convention_basis``) consumes a validated
  record only through an explicit per-rule declaration
  (CENTRIFUGE_COLLECT_PRECIPITATE_V1 v1.1.0,
  ``accept_post_operation_retained_object: true``): the record discharges
  the verbatim-intent premise and supplies the retained object, the
  composite family guard arbitrates to record-consistent rules only, and
  any conflict between the record and a family endpoint stays pending —
  nothing may stand on segment order or one family's premises alone.
  Without a valid record, composite operations stay ambiguous (the round-6
  hardening is unchanged, and the resolver defaults to off everywhere
  else: G1 receipt/compile plumbing is deliberately NOT wired in this
  round).

The runner first validates every authored relation/segment/lineage/
container against the V2 contract models (strict) and binds every fact
excerpt to the signed group blocks, failing loudly on either; then it
derives the retained-object record for EVERY graph step, replays the
unchanged G1 strict association path, and records: the Round-2 acceptance
section (composite operation value, source-relation record, precipitates
↔ LDH seeds label binding, graph[5] canonical output state), the per-graph
convention derivation outcomes (proof rule_id or honest issue code),
admitted_protocols, and the full diagnostic triage.  An empty protocol
list is reported as empty, never as a pass.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import json
import re
import sys

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from chem_agent_contracts.route_field_basis import controlled_state_mapping
from chem_agent_contracts.route_convention_basis import (
    derive_unreviewed_input_state,
    derive_unreviewed_output_state,
)
from chem_agent_contracts.route_inventory_basis import resolve_external_input_states
from chem_agent_contracts.route_retained_object import (
    build_excerpt_span_resolver,
    derive_post_operation_retained_object,
)
from chem_agent_contracts.route_source_labels import (
    build_source_label_context,
    competing_quantity_identity_surfaces,
    definition_site_concentration_binding,
    quantity_identity_surfaces,
    state_attribution_outcome,
)
from chem_agent_contracts.v2 import (
    LineageRelationV2,
    LogicalContainerV2,
    MaterialOperationSegmentV2,
    MaterialRelationV2,
)
from reaserch_agent.route_group_compiler import compile_experimental_group_protocols
from reaserch_agent.route_pdf_group_extraction import propose_pdf_group_unreviewed
from reaserch_agent.route_pdf_groups import enumerate_attested_pdf_experimental_groups
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
from reaserch_agent.run_research_agent import load_route_trust_config

root = Path(__file__).resolve().parents[2]
out_dir = root / "result" / "operation-structure-20260928"
SPEC_PATH = out_dir / "candidate-supply-specs.json"
PROPOSAL_PATH = out_dir / "local-revision-r7-proposal.json"

CONCENTRATION_PATH = "material_graph[1].material_outputs[0].concentration_value"

_PORT_PATH = re.compile(
    r"material_graph\[([0-9]+)\]\."
    r"(material_inputs|material_intermediates|material_outputs)\[([0-9]+)\]")

# Every material-port state fact in the restructured tail (graph[5..9]).
# graph[5].material_inputs[0].state (deionized water, null) and
# graph[6].material_inputs[1].state (water, null) are external-input fields
# discharged by the candidate supply-spec register, not paper facts.
STATE_FACT_PATHS = (
    "material_graph[5].material_inputs[1].state",
    "material_graph[5].material_outputs[0].state",
    "material_graph[6].material_inputs[0].state",
    "material_graph[6].material_outputs[0].state",
    "material_graph[7].material_inputs[0].state",
    "material_graph[7].material_outputs[0].state",
    "material_graph[8].material_inputs[0].state",
    "material_graph[8].material_outputs[0].state",
    "material_graph[9].material_inputs[0].state",
    "material_graph[9].material_outputs[0].state",
)

GRAPH_LABELS = {
    "material_graph[5].material_outputs[0].state": "graph[5] composite protocol output",
    "material_graph[6].material_inputs[0].state": "graph[6] LDH-seeds input (inheritance)",
    "material_graph[6].material_outputs[0].state": "graph[6] redispersion output",
    "material_graph[7].material_inputs[0].state": "graph[7a] aged input (inheritance)",
    "material_graph[7].material_outputs[0].state": "graph[7a] centrifugation output",
    "material_graph[8].material_inputs[0].state": "graph[7b] wet-solid input (inheritance)",
    "material_graph[8].material_outputs[0].state": "graph[7b] redispersion output",
    "material_graph[9].material_inputs[0].state": "graph[8] samples input (inheritance)",
    "material_graph[9].material_outputs[0].state": "graph[8] collected output",
}


def _fail(message: str) -> None:
    raise SystemExit(f"local-revision-r7 contract/preflight failure: {message}")


def _validate_contracts(proposal: dict) -> list[dict]:
    """Strict V2 validation of every authored typed structure; fail loudly."""
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
                MaterialOperationSegmentV2.model_validate(segment, strict=True)
            except (TypeError, ValueError) as exc:
                _fail(f"material_graph[{step_index}] ({step_id}) segment "
                      f"{segment.get('segment_id')!r}: {exc}")
            rows.append({"step": step_id, "kind": "MaterialOperationSegmentV2",
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
    """Bind every fact excerpt to the signed group blocks; fail loudly."""
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    rows: list[dict] = []
    for fact in proposal["route_facts"]:
        binding, issue = bind_pdf_quote(
            blocks, fact.get("excerpt"), caption_block_locators=captions)
        # An over-span excerpt that still binds uniquely (four prose blocks)
        # is exactly the case the association's bounded clause tightening
        # already handles, as it did for the unchanged round-3 quotes in
        # rounds 4-6; only a missing or ambiguous quote is a preflight
        # failure.  The issue is recorded per fact either way.
        if issue not in ("", "fact_excerpt_span_too_long"):
            _fail(f"fact {fact.get('fact_id')} ({fact.get('field_path')}) "
                  f"excerpt does not bind: {issue}")
        rows.append({"fact_id": fact["fact_id"],
                     "field_path": fact["field_path"],
                     "locator": binding.locator if binding else "",
                     "binding_issue": issue})
    return rows


def _retained_object_resolution(proposal: dict, group):
    """Derive the post-operation retained-object record for EVERY graph step.

    Returns (resolver, rows): the resolver maps an output state field path
    to (record, issue); rows are the audit view, one per step.
    """
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    span_of = build_excerpt_span_resolver(blocks, captions)
    records_by_state_path: dict[str, dict] = {}
    issues_by_state_path: dict[str, str] = {}
    rows: list[dict] = []
    for step_index, step in enumerate(graph):
        record, issue = derive_post_operation_retained_object(
            graph, facts, step_index, span_of=span_of)
        outputs = step.get("material_outputs") or []
        fallback_path = (
            f"material_graph[{step_index}].material_outputs[0].state"
            if outputs else ""
        )
        if record is not None:
            records_by_state_path[record["output"]["state_path"]] = record
            rows.append({
                "step_index": step_index,
                "macro_step_id": step.get("macro_step_id", ""),
                "status": "record",
                "state_path": record["output"]["state_path"],
                "retained_object": record["retained_object"],
                "retained_object_surface": record["retained_object_surface"],
                "label": record["output"]["label"],
                "operation_span": record["operation_span"],
                "naming_span": record["naming_span"],
            })
        else:
            if fallback_path:
                issues_by_state_path[fallback_path] = issue
            rows.append({
                "step_index": step_index,
                "macro_step_id": step.get("macro_step_id", ""),
                "status": f"issue: {issue}",
                "state_path": fallback_path,
            })

    def resolver(field_path: str):
        record = records_by_state_path.get(field_path)
        if record is not None:
            return record, ""
        return None, issues_by_state_path.get(field_path, "")

    return resolver, rows


def _port_at(graph, field_path):
    match = _PORT_PATH.fullmatch(field_path.rsplit(".", 1)[0])
    if match is None:
        return None
    step, kind, index = int(match.group(1)), match.group(2), int(match.group(3))
    try:
        return graph[step][kind][index]
    except (IndexError, KeyError, TypeError):
        return None


def _literal_present(value, excerpt):
    if not isinstance(value, str) or not isinstance(excerpt, str):
        return False
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", excerpt) is not None


def _proof_table(proposal: dict, final_issues: list[dict],
                 retained_object_resolver=None) -> list[dict]:
    """Per state fact: exact convention derivation outcome + receipt gates."""
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    ref = proposal["source_group_ref"]
    scope = {
        "paper_id": ref["paper_id"],
        "experimental_group_id": ref["experimental_group_id"],
        "source_digest": ref["source_digest"],
    }
    context = build_source_label_context(graph, facts)
    by_path = {}
    for fact in facts:
        if isinstance(fact, dict) and fact.get("field_path"):
            by_path.setdefault(fact["field_path"], fact)
    issue_by_fact = {}
    for item in final_issues or []:
        if isinstance(item, dict) and item.get("fact_id"):
            issue_by_fact.setdefault(item["fact_id"], item.get("reason_code", ""))
    rows = []
    for field_path in STATE_FACT_PATHS:
        fact = by_path.get(field_path, {})
        value = fact.get("value")
        excerpt = fact.get("excerpt")
        port = _port_at(graph, field_path)
        graph_state = (port or {}).get("state")
        output_proof, output_issue = derive_unreviewed_output_state(
            graph, facts, field_path,
            paper_id=scope["paper_id"],
            experimental_group_id=scope["experimental_group_id"],
            source_digest=scope["source_digest"],
            retained_object_resolver=retained_object_resolver)
        input_proof, input_issue = derive_unreviewed_input_state(
            graph, facts, field_path, scope,
            retained_object_resolver=retained_object_resolver)
        mapping, mapping_issue = controlled_state_mapping(
            field_path, value, graph_state)
        outcome, _binding = state_attribution_outcome(
            value, excerpt, field_path, graph, context)
        derivation = ""
        rule = ""
        if output_proof is not None:
            derivation, rule = "output_state_proof", output_proof["rule_id"]
        elif input_proof is not None:
            derivation, rule = "input_state_inheritance", input_proof["rule_id"]
        else:
            derivation = output_issue or input_issue or "no_derivation_path"
            rule = ""
        rows.append({
            "field_path": field_path,
            "graph_element": GRAPH_LABELS.get(field_path, ""),
            "fact_id": fact.get("fact_id", ""),
            "port_name": (port or {}).get("name", ""),
            "state_value": value,
            "graph_state": graph_state,
            "material_origin": (port or {}).get("material_origin"),
            "derivation": derivation,
            "rule_id": rule,
            "output_derivation_issue": output_issue,
            "input_derivation_issue": input_issue,
            "receipt_gate_outcome": issue_by_fact.get(fact.get("fact_id", ""),
                                                      "passes_literal_gates"),
            "gates": {
                "controlled_state_mapping": "fail" if mapping_issue else "pass",
                "state_attribution_outcome": outcome,
                "state_word_literal_in_excerpt": _literal_present(
                    str(value or ""), excerpt),
            },
        })
    return rows


def _concentration_binding_evidence(proposal):
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    context = build_source_label_context(graph, facts)
    fact = next((f for f in facts if isinstance(f, dict)
                 and f.get("field_path") == CONCENTRATION_PATH), None)
    assert fact is not None, "1 M concentration fact missing"
    binding = definition_site_concentration_binding(
        fact.get("excerpt"), fact.get("value"), fact.get("unit"),
        quantity_identity_surfaces(CONCENTRATION_PATH, graph, context),
        competing_surfaces=competing_quantity_identity_surfaces(
            CONCENTRATION_PATH, graph, context),
    )
    return fact, binding


def main() -> None:
    envelope = json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))
    proposal = deepcopy(envelope["proposals"][0])

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

    binding_rows = _bind_all_excerpts(proposal, matches[0])

    resolver, source_relation_rows = _retained_object_resolution(
        proposal, matches[0])

    revised_envelope = {"proposals": [proposal]}
    spec_items = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["items"]
    spec_digest = "sha256_" + sha256(SPEC_PATH.read_bytes()).hexdigest()
    records = resolve_external_input_states(
        proposal["material_graph"], spec_items, proposal.get("route_facts", []))
    resolutions = [{
        "field_path": r["field_path"], "status": "resolved",
        "item_id": r["item_id"], "supply_form": r["supply_form"],
        "state": r["state"], "record": r["record"],
        "register": "candidate_supply_spec/v1",
        "register_digest": spec_digest, "candidate": True,
    } for r in records if r["status"] == "resolved"]

    association = propose_pdf_group_unreviewed(
        matches, lambda _prompt: revised_envelope,
        max_repair_groups=0, check_required_graph_facts=True,
        inventory_registers={
            "candidate_supply_spec/v1": (spec_items, spec_digest,
                                         tuple(resolutions))})
    admitted = len(association.protocols)
    local_revision = (association.locator_production or {}).get(
        "local_revision") or {}
    g1 = local_revision.get("final_issues")
    convention_candidates = (association.locator_production or {}).get(
        "convention_state_candidates")

    proofs = _proof_table(proposal, g1 or [],
                          retained_object_resolver=resolver)
    conc_fact, conc_binding = _concentration_binding_evidence(proposal)

    # Round-2 acceptance: the four required real results for graph[5].
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    ref = proposal["source_group_ref"]
    g5_state_path = "material_graph[5].material_outputs[0].state"
    g5_name_path = "material_graph[5].material_outputs[0].name"
    g5_name_fact = next((f for f in facts if isinstance(f, dict)
                         and f.get("field_path") == g5_name_path), {})
    g5_name_value = g5_name_fact.get("value", "")
    g5_record, g5_record_issue = resolver(g5_state_path)
    g5_proof, g5_proof_issue = derive_unreviewed_output_state(
        graph, facts, g5_state_path,
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        source_digest=ref["source_digest"],
        retained_object_resolver=resolver)
    label_binding_pass = bool(
        g5_record and g5_record["output"]["label"] == g5_name_value)
    round2_acceptance = {
        "graph5_macro_operation": graph[5].get("operation", ""),
        "post_operation_retained_object": (
            {"status": "PASS", "record": g5_record}
            if g5_record is not None else
            {"status": f"BLOCKED: {g5_record_issue or 'no_record'}",
             "record": None}
        ),
        "output_label_binding": (
            {"status": "PASS",
             "retained_object": g5_record["retained_object"],
             "label": g5_record["output"]["label"],
             "output_name": g5_name_value}
            if label_binding_pass else
            {"status": f"BLOCKED: {g5_record_issue or 'label_mismatch'}",
             "output_name": g5_name_value}
        ),
        "graph5_output_canonical_state": (
            {"status": f"PASS: {g5_proof['rule_id']} {g5_proof['rule_version']}",
             "proof": g5_proof}
            if g5_proof is not None else
            {"status": f"BLOCKED: {g5_proof_issue}", "proof": None}
        ),
    }

    g2 = None
    if admitted:
        for protocol in association.protocols:
            if isinstance(protocol, dict) and "route_facts" in protocol:
                protocol["inventory_resolutions"] = resolutions
        compiled = compile_experimental_group_protocols(association.protocols)
        g2 = [asdict(d)["reason_code"] for d in compiled.diagnostics]

    diagnostics = dict(Counter(d.reason_code for d in association.diagnostics))
    audit = [
        {"kind": "round2_post_operation_retained_object",
         "step": "ms5",
         "structure": ("operation value is now the verbatim composite "
                       "'centrifugation−redispersion protocol' (U+2212, exact "
                       "substring of the unchanged f_g5_op excerpt b70-b71); "
                       "repetitions='three' stays bound to the whole protocol; "
                       "a second segment ms5-seg-redispersion "
                       "(material_effect=unknown) documents containment "
                       "without claiming a micro-sequence endpoint; the "
                       "state_change relation still references only "
                       "ms5-seg-centrifugation"),
         "source_relation": ("post-operation-retained-object/v1 derives, from "
                             "the signed blocks only, that 'The precipitates "
                             "were labeled as LDH seeds' (b71-b72) follows the "
                             "composite operation (b70-b71) and binds "
                             "'precipitates' to the output label 'LDH seeds'; "
                             "the module performs no chemistry and never maps "
                             "the object to a state"),
         "ordering_note": ("the naming sentence's first word ('The') is the "
                           "last word of physical block b71, the same block "
                           "where the operation sentence ends; the "
                           "block-index ordering guard is therefore "
                           "tie-broken by exact character spans inside the "
                           "shared block (operation quote's last character "
                           "precedes the naming quote's first: '...protocol. "
                           "The precipitates...').  A textually preceding "
                           "mention is never admitted"),
         "expectation": ("with the validated record, the composite family "
                         "guard arbitrates to record-consistent rules only: "
                         "CENTRIFUGE_COLLECT_PRECIPITATE_V1 v1.1.0 (declared "
                         "accept_post_operation_retained_object) derives "
                         "retained_wet_solid; REDISPERSION_V1's endpoint "
                         "conflicts and stays out; without the record the "
                         "composite stays convention_rule_not_applicable_or_"
                         "ambiguous")},
        {"kind": "graph6_input_inheritance_unblocked",
         "step": "ms6",
         "structure": ("unchanged from round 6: inst_ldh_seeds input "
                       "material_origin=upstream_output + one "
                       "parent_output_ref to the ms5 output"),
         "expectation": ("with the ms5 output state now proven, "
                         "PARENT_OUTPUT_STATE_INHERITANCE_V1 proves the ms6 "
                         "input state; without the resolver it stays "
                         "convention_parent_state_unverified")},
        {"kind": "deliberately_not_done",
         "items": [
             "G1 _prepare_unsigned_proposal / compiler / receipt plumbing of "
             "the resolver (they run with the default None: byte-identical "
             "behavior; next integration step)",
             "Chinese C1 naming/discard aliases (Round 2 is English-only)",
             "no generic centrifugation-redispersion x3 -> washed_wet_solid "
             "rule and no composite-endpoint rule was added",
             "graph[6] output / multi-hop chains / Q1 are not solved",
             "duplicate container ID question deferred",
         ]},
        {"kind": "post_operation_retained_object_per_step",
         "rows": source_relation_rows},
        {"kind": "contract_validation",
         "rows": contract_rows},
        {"kind": "excerpt_binding",
         "rows": binding_rows},
        {"kind": "per_graph_proofs",
         "items": proofs},
        {"kind": "round2_acceptance",
         "items": round2_acceptance},
    ]

    result = {
        "audit_rows": len(audit),
        "round2_acceptance": round2_acceptance,
        "post_operation_retained_object_per_step": source_relation_rows,
        "resolution_status": dict(Counter(r["status"] for r in records)),
        "strict_association": {
            "admitted_protocols": admitted,
            "diagnostics": diagnostics,
            "g1_final_issue_reasons": dict(Counter(
                i["reason_code"] for i in (g1 or []))),
            "g1_final_issues": g1 or [],
            "convention_state_candidates": convention_candidates or [],
            "per_graph_proofs": proofs,
            "concentration_fact": {
                "field_path": CONCENTRATION_PATH,
                "fact_id": conc_fact.get("fact_id", ""),
                "excerpt": conc_fact.get("excerpt", ""),
                "binding": conc_binding,
                "diagnostic_present": "fact_quantity_attribution_unresolved"
                                      in diagnostics,
            },
        },
        "g2_compile_reasons": g2,
        "g2_note": ("no admitted protocols; nothing was compiled"
                    if not admitted else
                    "compiled the admitted group; see reasons"),
    }
    (out_dir / "local-revision-r7-audit.json").write_text(
        json.dumps({
            "schema_version": "bounded_local_revision/v7",
            "model_generated": False,
            "scope": ("Round 2: graph[5] (ms5) declares the verbatim composite "
                      "operation 'centrifugation−redispersion protocol' plus a "
                      "containment segment (unknown effect); a new "
                      "post-operation-retained-object/v1 SOURCE relation "
                      "derives precipitates ↔ 'LDH seeds' from the signed "
                      "blocks; the convention layer consumes the validated "
                      "record only via the reviewed "
                      "accept_post_operation_retained_object declaration on "
                      "CENTRIFUGE_COLLECT_PRECIPITATE_V1 v1.1.0, with "
                      "composite-guard arbitration fail-closed on conflict.  "
                      "graph[0..4] and graph[6..9], all other facts, G1 "
                      "receipt/compile plumbing, and every hardening default "
                      "are unchanged; diagnostics are reported honestly, "
                      "never pre-empted"),
            "audit": audit,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "local-revision-r7-replay.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
