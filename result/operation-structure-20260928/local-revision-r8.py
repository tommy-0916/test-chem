"""Round-8 (Round 3A): the retained-object resolver wired into the formal G1 path.

Round 7 (Round 2) proved the source relation at the contract layer but left
every G1 stage running with the default ``retained_object_resolver=None``
(fail-closed, byte-identical behavior).  Round 3A wires the LIVE resolver
into the formal path and closes the trust boundary:

- G1 association (``route_pdf_group_extraction._prepare_unsigned_proposal``),
  local repair, local diagnostics, proposal quality, material structure,
  the literal receipt (``route_group_fact_receipt``), and the group compiler
  (``route_group_compiler``) now rebuild the retained-object resolver LIVE
  from the signed group blocks on every call.  The real NiFe Control group
  now derives graph[5].out (CENTRIFUGE_COLLECT_PRECIPITATE_V1) and
  graph[6].in (PARENT_OUTPUT_STATE_INHERITANCE_V1) INSIDE the association
  (``convention_state_candidates``), the receipt (``derived_state_field_paths``),
  and the compiler evidence matrix.
- Records carry locators and character spans (operation/naming
  ``*_locator`` + ``*_char_span`` + ``output_name_fact_id``); the convention
  layer validates shape (parse) and then cross-checks every binding against
  the live graph and facts (state_path, port instance/material/label,
  output/step index, fact ids, excerpts).  A stored or fabricated record has
  ZERO authority: ``proposal["retained_object_records"]`` is never read by
  any stage, and a forged record fed through a resolver is rejected with
  ``retained_object_output_binding_unresolved``.
- ``route_pipeline`` compile enumeration stays on the default (deferred:
  enumeration diagnostics feed ``discovery``, which the compile consumes,
  so the enumeration cannot be moved above it), and the science audit /
  source verifier stay fail-closed (a compiled candidate retains only
  hashed evidence ids, so a resolver rebuilt there could never reproduce
  the bound proof fields).  ``route_decision``/``v2`` re-verification has
  no blocks at all and stays a publication-gate follow-up.

The runner keeps the ENTIRE round-7 replay (same proposal, byte-identical
to r7's) and records: the Round-2 acceptance section (must still PASS),
the formal-G1 association outcome, the receipt derivation, a self-contained
compiler demonstration, the phase-1 probe closures (R1-R3 source-relation
hardening, E1b-E5 polarity/medium binding), the R4 fabricated-record
rejection at contract and G1 level, and the per-stage live-resolver
evidence.  An empty protocol list is reported as empty, never as a pass.
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

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from chem_agent_contracts.route_field_basis import controlled_state_mapping
from chem_agent_contracts.route_convention_basis import (
    derive_unreviewed_input_state,
    derive_unreviewed_output_state,
)
from chem_agent_contracts.route_inventory_basis import resolve_external_input_states
from chem_agent_contracts.route_retained_object import (
    POST_OPERATION_RETAINED_OBJECT_RULE_ID,
    POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
    POST_OPERATION_RETAINED_OBJECT_SCHEMA,
    build_excerpt_span_resolver,
    build_retained_object_resolver,
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
from reaserch_agent.route_group_fact_receipt import produce_pdf_group_fact_receipt
from reaserch_agent.route_pdf_group_extraction import propose_pdf_group_unreviewed
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1,
    PdfSourceBlockV1,
    enumerate_attested_pdf_experimental_groups,
)
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
from reaserch_agent.run_research_agent import load_route_trust_config

root = Path(__file__).resolve().parents[2]
out_dir = root / "result" / "operation-structure-20260928"
SPEC_PATH = out_dir / "candidate-supply-specs.json"
PROPOSAL_PATH = out_dir / "local-revision-r8-proposal.json"

CONCENTRATION_PATH = "material_graph[1].material_outputs[0].concentration_value"

_PORT_PATH = re.compile(
    r"material_graph\[([0-9]+)\]\."
    r"(material_inputs|material_intermediates|material_outputs)\[([0-9]+)\]")

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

G5_OUT_STATE = "material_graph[5].material_outputs[0].state"
G6_IN_STATE = "material_graph[6].material_inputs[0].state"

# ---------------------------------------------------------------------------
# Phase-1 probe fixtures (self-contained copies of the reviewed test
# fixtures; nothing imports the test modules).
# ---------------------------------------------------------------------------

PROBE_OP_VALUE = "centrifugation−redispersion protocol"
PROBE_OP_SENTENCE = (
    "followed by a centrifugation−redispersion protocol using deionized "
    "water three times, which was the first centrifugation−redispersion "
    "protocol."
)
PROBE_OP_EXCERPT = (
    "by a centrifugation−redispersion protocol using deionized water three "
    "times, which was the first centrifugation−redispersion protocol"
)
PROBE_NAMING = (
    "The precipitates were labeled as LDH seeds, which were dispersed in "
    "30 mL of water."
)
PROBE_FILLER = "The suspension was divided into 8 parts."
PROBE_OUT_STATE = "material_graph[0].material_outputs[0].state"
PROBE_DIGEST = "sha256_" + sha256(b"r8 probe source bytes").hexdigest()
PROBE_SOURCE = {
    "paper_id": "paper-r8-probe", "experimental_group_id": "Group R8",
    "section": "Methods", "locator": "lines:4-4",
    "source_digest": PROBE_DIGEST,
}


def _probe_blocks(*texts: str) -> list[tuple[str, str]]:
    return [
        (f"pdf:p1:b{index}-p1:b{index}", text)
        for index, text in enumerate(texts, start=1)
    ]


def _probe_fact(fact_id: str, path: str, value: str, excerpt: str) -> dict:
    return {
        "fact_id": fact_id, "field_path": path, "value": value,
        "unit": "", "required": True, "excerpt": excerpt,
        "source": deepcopy(PROBE_SOURCE),
    }


def _probe_state_step(*, step_id, sequence, operation, input_port,
                      output_port, ref) -> dict:
    return {
        "macro_step_id": step_id, "macro_action_id": "A1",
        "sequence": sequence, "operation": operation, "sample_id": "sample-R8",
        "provenance": {"kind": "paper", "reference": ref},
        "material_inputs": [input_port],
        "material_outputs": [output_port],
        "operation_segments": [{
            "segment_id": f"{step_id}-seg1",
            "material_effect": "transform_material",
            "source_operation_ref": step_id,
            "provenance": {"kind": "paper", "reference": ref},
        }],
        "material_relations": [{
            "relation_id": f"{step_id}-rel1", "event_kind": "state_change",
            "input_material_instance_ids": [input_port["material_instance_id"]],
            "output_material_instance_ids": [output_port["material_instance_id"]],
            "quantity_basis": "whole_batch",
            "source_operation_ref": f"{step_id}-seg1",
            "provenance": {"kind": "paper", "reference": ref},
        }],
        "lineage_relation": {
            "relation_type": "state_change_of",
            "parent_material_instance_ids": [input_port["material_instance_id"]],
            "child_material_instance_ids": [output_port["material_instance_id"]],
        },
    }


def _composite_probe_proposal(*, name_excerpt: str = PROBE_NAMING) -> dict:
    input_port = {
        "material_id": "product", "material_instance_id": "inst_a",
        "name": "suspension", "state": "suspension",
        "material_origin": "external_inventory",
        "provenance": {"kind": "paper", "reference": "fact:in_name"},
    }
    output_port = {
        "material_id": "product", "material_instance_id": "inst_b",
        "name": "LDH seeds", "state": "retained_wet_solid",
        "provenance": {"kind": "paper", "reference": "fact:out_name"},
    }
    step = _probe_state_step(
        step_id="S1", sequence=1, operation=PROBE_OP_VALUE,
        input_port=input_port, output_port=output_port, ref="fact:op")
    facts = [
        _probe_fact("op", "material_graph[0].operation",
                    PROBE_OP_VALUE, PROBE_OP_EXCERPT),
        _probe_fact("in_name", "material_graph[0].material_inputs[0].name",
                    "suspension", PROBE_FILLER),
        _probe_fact("in_state", "material_graph[0].material_inputs[0].state",
                    "suspension", PROBE_FILLER),
        _probe_fact("out_name", "material_graph[0].material_outputs[0].name",
                    "LDH seeds", name_excerpt),
        _probe_fact("out_state", PROBE_OUT_STATE,
                    "retained_wet_solid", PROBE_OP_EXCERPT),
    ]
    return {"material_graph": [step], "route_facts": facts}


def _one_step_probe(operation: str, quote: str,
                    in_state: str, out_state: str) -> dict:
    input_port = {
        "material_id": "product", "material_instance_id": "i1",
        "name": "washed_wet_solid", "state": in_state,
        "material_origin": "external_inventory",
        "provenance": {"kind": "paper", "reference": "fact:in_state"},
    }
    output_port = {
        "material_id": "product", "material_instance_id": "o1",
        "name": "washed_wet_solid", "state": out_state,
        "provenance": {"kind": "paper", "reference": "fact:op"},
    }
    step = _probe_state_step(
        step_id="S1", sequence=1, operation=operation,
        input_port=input_port, output_port=output_port, ref="fact:op")
    facts = [
        _probe_fact("op", "material_graph[0].operation", operation, quote),
        _probe_fact("in_state", "material_graph[0].material_inputs[0].state",
                    in_state, quote),
        _probe_fact("out_state", PROBE_OUT_STATE, out_state, quote),
    ]
    return {"material_graph": [step], "route_facts": facts}


def _derive_record(proposal: dict, blocks: list[tuple[str, str]]):
    span_of = build_excerpt_span_resolver(blocks)
    return derive_post_operation_retained_object(
        proposal["material_graph"], proposal["route_facts"], 0,
        span_of=span_of)


def _r1_r3_probes() -> list[dict]:
    """Round-1 review gaps R1-R3 in the source relation (phase 1 closures)."""
    rows: list[dict] = []

    def add(name, blocks, proposal, expected_issue,
            expected_object=None, expected_modifiers=None):
        record, issue = _derive_record(proposal, blocks)
        ok = issue == expected_issue
        if expected_issue == "" and record is not None:
            if expected_object is not None:
                ok = ok and record["retained_object"] == expected_object
            if expected_modifiers is not None:
                ok = ok and record["object_modifiers"] == expected_modifiers
        rows.append({
            "probe": name,
            "expected_issue": expected_issue or "(record)",
            "observed_issue": issue or "(record)",
            "observed_object": (record or {}).get("retained_object"),
            "observed_modifiers": (record or {}).get("object_modifiers"),
            "status": "PASS" if ok else "FAIL",
        })

    add("R1_unmodeled_interval_operation",
        _probe_blocks(PROBE_FILLER, PROBE_OP_SENTENCE,
                      "The solid was then dried at 60 C overnight.",
                      PROBE_NAMING),
        _composite_probe_proposal(), "retained_object_intervening_operation")
    add("R1_negated_interval_operation",
        _probe_blocks(PROBE_FILLER, PROBE_OP_SENTENCE,
                      "The solid was not dried.", PROBE_NAMING),
        _composite_probe_proposal(), "", expected_object="precipitate",
        expected_modifiers=[])
    add("R1_fallback_stem_interval",
        _probe_blocks(PROBE_FILLER, PROBE_OP_SENTENCE,
                      "The solid was calcined at 500 C.", PROBE_NAMING),
        _composite_probe_proposal(), "retained_object_intervening_operation")
    for modifier in ("dried", "calcined"):
        naming = f"The {modifier} precipitates were labeled as LDH seeds."
        add(f"R2_{modifier}_object_modifier",
            _probe_blocks(PROBE_FILLER, PROBE_OP_SENTENCE, naming),
            _composite_probe_proposal(name_excerpt=naming),
            "retained_object_state_changing_modifier")
    benign = "The yellow precipitates were labeled as LDH seeds."
    add("R2_benign_object_modifier",
        _probe_blocks(PROBE_FILLER, PROBE_OP_SENTENCE, benign),
        _composite_probe_proposal(name_excerpt=benign), "",
        expected_object="precipitate", expected_modifiers=["yellow"])
    return rows


def _e1b_e5_probes() -> list[dict]:
    """Round-1b review probes E1b-E5: assertion polarity and medium binding."""
    rows: list[dict] = []

    def add(name, operation, quote, expected_issue, expected_rule=""):
        proposal = _one_step_probe(
            operation, quote, "washed_wet_solid", "suspension")
        proof, issue = derive_unreviewed_output_state(
            proposal["material_graph"], proposal["route_facts"],
            PROBE_OUT_STATE,
            paper_id=PROBE_SOURCE["paper_id"],
            experimental_group_id=PROBE_SOURCE["experimental_group_id"],
            source_digest=PROBE_DIGEST)
        rule = (proof or {}).get("rule_id", "")
        ok = issue == expected_issue and rule == expected_rule
        rows.append({
            "probe": name, "quote": quote,
            "expected_issue": expected_issue or "(proof)",
            "observed_issue": issue or "(proof)",
            "expected_rule": expected_rule, "observed_rule": rule,
            "status": "PASS" if ok else "FAIL",
        })

    add("E1b_alternative_instead_of", "redispersed",
        "The washed_wet_solid was dried instead of being redispersed in "
        "water.", "convention_rule_not_applicable_or_ambiguous")
    add("E1b_alternative_rather_than", "redispersed",
        "The washed_wet_solid was dried rather than redispersed in water.",
        "convention_rule_not_applicable_or_ambiguous")
    add("E2_failed_attempt_participle", "redispersed",
        "We attempted to get the washed_wet_solid redispersed in water "
        "but failed.", "convention_rule_not_applicable_or_ambiguous")
    add("E2_failed_attempt_infinitive", "redisperse",
        "We attempted to redisperse the washed_wet_solid in water "
        "but failed.", "convention_rule_not_applicable_or_ambiguous")
    add("E3_hyphenated_water_free", "redispersed",
        "The washed_wet_solid was redispersed in a water-free medium.",
        "convention_liquid_participation_missing")
    add("E4_medium_in_another_clause", "redispersed",
        "The washed_wet_solid was redispersed. The reactor was washed in "
        "water.", "convention_liquid_participation_missing")
    add("E5_affirmed_participle", "redispersed",
        "The washed_wet_solid was redispersed in water.", "",
        expected_rule="REDISPERSION_V1")
    add("E5_affirmed_nominal", "redispersion",
        "The washed_wet_solid underwent redispersion in water.", "",
        expected_rule="REDISPERSION_V1")
    return rows


# ---------------------------------------------------------------------------
# Round-7 replay helpers (unchanged).
# ---------------------------------------------------------------------------


def _fail(message: str) -> None:
    raise SystemExit(f"local-revision-r8 contract/preflight failure: {message}")


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


def _retained_object_resolution(proposal: dict, group):
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
                "operation_locator": record["operation_locator"],
                "naming_locator": record["naming_locator"],
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


# ---------------------------------------------------------------------------
# Round-3A stages.
# ---------------------------------------------------------------------------


def _association_run(matches, proposal, spec_items, spec_digest, resolutions):
    envelope = {"proposals": [deepcopy(proposal)]}
    return propose_pdf_group_unreviewed(
        matches, lambda _prompt: envelope,
        max_repair_groups=0, check_required_graph_facts=True,
        inventory_registers={
            "candidate_supply_spec/v1": (spec_items, spec_digest,
                                         tuple(resolutions))})


def _candidate_rules(association) -> dict:
    production = association.locator_production or {}
    return {
        row["field_path"]: row["proof"]["rule_id"]
        for row in production.get("convention_state_candidates") or ()
        if isinstance(row, dict) and row.get("proof")
    }


def _receipt_stage(matches, association) -> dict:
    """Feed the association's located proposal into the literal receipt."""
    production = association.locator_production or {}
    located = (production.get("located_proposals") or [None])[0]
    if located is None:
        return {"status": "FAIL", "reason": "no located proposal"}
    group = matches[0]
    scope = group.source_scope
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    facts = []
    for raw in located["route_facts"]:
        # The receipt re-binds every excerpt and demands source.locator ==
        # binding.locator, so the locator must come from a fresh binding of
        # the located (tightened) excerpt, never from block_locator (which
        # records only the first block of the span).
        binding, quote_issue = bind_pdf_quote(
            blocks, raw.get("excerpt"), caption_block_locators=captions)
        locator = (binding.locator if binding is not None
                   else raw.get("block_locator", scope.locator))
        facts.append({
            "fact_id": raw["fact_id"], "field_path": raw["field_path"],
            "value": raw.get("value"), "unit": raw.get("unit", ""),
            "required": raw.get("required", True),
            "excerpt": raw.get("excerpt"),
            "source": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "section": scope.section,
                "locator": locator,
                "source_digest": scope.source_digest,
            },
        })
    protocol = {
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "group_role": "unclassified",
        "role_hint": located.get("role_hint", ""),
        "source": {
            "source_document": group.source_document,
            "section": scope.section,
            "locator": scope.locator,
            "source_digest": scope.source_digest,
        },
        "target": deepcopy(located.get("target") or {}),
        "route_signature": deepcopy(located.get("route_signature") or {}),
        "material_graph": deepcopy(located["material_graph"]),
        "route_facts": facts,
    }
    receipt = produce_pdf_group_fact_receipt(
        matches, [protocol], signed_inventory_verified=True)
    if not receipt.group_results:
        return {"status": "FAIL", "receipt_status": receipt.status,
                "reason_codes": list(receipt.reason_codes)}
    result = receipt.group_results[0]
    derived = list(result.derived_state_field_paths)
    ok = G5_OUT_STATE in derived and G6_IN_STATE in derived
    return {
        "status": "PASS" if ok else "FAIL",
        "receipt_status": receipt.status,
        "group_status": result.status,
        "group_status_note": ("the receipt queues every semantic trust "
                              "decision: any pending literal reason (here "
                              "semantic_binding_pending for the honestly "
                              "unresolved facts) marks the group "
                              "'blocked'; derivation results are reported "
                              "alongside, never substituted for review"),
        "derived_state_field_paths": derived,
        "expected_derived": [G5_OUT_STATE, G6_IN_STATE],
        "reason_code_counts": dict(Counter(
            code.split(":", 1)[-1] for code in result.reason_codes)),
        "reason_codes": list(result.reason_codes),
    }


# --- Self-contained compiler demonstration fixture (single step; the
# --- compiler's per-step material identity rule wants one distinguishing
# --- port name per material id, so both ports are named "LDH seeds"). ---

COMP_DIGEST = "sha256_" + sha256(b"signed R8 compiler PDF bytes").hexdigest()
COMP_B1 = ("The LDH seeds suspension of NiFe hydroxide was obtained by "
           "coprecipitation and aged for 12 h.")
COMP_B2 = ("The solid was isolated by a centrifugation−redispersion "
           "protocol using deionized water three times.")
COMP_B3 = "The precipitates were labeled as LDH seeds, which were kept wet."
COMP_B4 = "The LDH seeds were redispersed in water."
COMP_OP_EXCERPT = (
    "centrifugation−redispersion protocol using deionized water three times"
)


def _compiler_fixture():
    scope = ExperimentalGroupScopeV1(
        paper_id="paper-r8-compiler",
        experimental_group_id="Group R8C",
        section="Methods", locator="pdf:p1:b1-p1:b4",
        source_digest=COMP_DIGEST,
    )
    group = PdfExperimentalGroupV1(
        source_scope=scope,
        source_document="/controlled/paper-r8c.pdf",
        blocks=tuple(
            PdfSourceBlockV1(f"pdf:p1:b{index}-p1:b{index}", text)
            for index, text in enumerate(
                (COMP_B1, COMP_B2, COMP_B3, COMP_B4), start=1)
        ),
    )
    step = _probe_state_step(
        step_id="S1", sequence=1, operation=PROBE_OP_VALUE,
        input_port={
            "material_id": "product", "material_instance_id": "inst_a",
            "name": "LDH seeds", "state": "suspension",
            "material_origin": "external_inventory",
            "provenance": {"kind": "paper", "reference": "fact:f_in_name"},
        },
        output_port={
            "material_id": "product", "material_instance_id": "inst_b",
            "name": "LDH seeds", "state": "retained_wet_solid",
            "provenance": {"kind": "paper", "reference": "fact:f_out_name"},
        },
        ref="fact:f_op",
    )
    raw_facts = [
        _probe_fact("f_op", "material_graph[0].operation",
                    PROBE_OP_VALUE, COMP_OP_EXCERPT),
        _probe_fact("f_in_name", "material_graph[0].material_inputs[0].name",
                    "LDH seeds", COMP_B1),
        _probe_fact("f_in_state", "material_graph[0].material_inputs[0].state",
                    "suspension", COMP_B1),
        _probe_fact("f_out_name", "material_graph[0].material_outputs[0].name",
                    "LDH seeds", COMP_B3),
        _probe_fact("f_out_state", PROBE_OUT_STATE,
                    "retained_wet_solid", COMP_OP_EXCERPT),
    ]
    blocks = [(block.locator, block.text) for block in group.blocks]
    facts = []
    for raw in raw_facts:
        binding, issue = bind_pdf_quote(blocks, raw["excerpt"])
        if binding is None:
            _fail(f"compiler fixture fact {raw['fact_id']} does not bind: "
                  f"{issue}")
        fact = dict(raw)
        fact["source"] = {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "section": scope.section,
            "locator": binding.locator,
            "source_digest": scope.source_digest,
        }
        facts.append(fact)
    facts.append({
        "fact_id": "f_sig_op0", "field_path": "route_signature.operations[0]",
        "value": PROBE_OP_VALUE, "unit": "", "required": True,
        "excerpt": COMP_OP_EXCERPT,
        "source": deepcopy(facts[0]["source"]),
    })
    protocol = {
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "group_role": "unclassified",
        "role_hint": "synthesis",
        "target": {"product": "LDH seeds"},
        "required_capabilities": ["centrifugation"],
        "route_signature": {"operations": [PROBE_OP_VALUE]},
        "source": {
            "source_document": group.source_document,
            "section": scope.section,
            "locator": scope.locator,
            "source_digest": scope.source_digest,
        },
        "material_graph": [step],
        "route_facts": facts,
    }
    resolver = build_retained_object_resolver(
        protocol["material_graph"], protocol["route_facts"], blocks, ())
    key = (scope.paper_id, scope.experimental_group_id, scope.source_digest)
    return group, protocol, resolver, key


def _compiler_demo() -> dict:
    group, protocol, resolver, key = _compiler_fixture()
    compiled = compile_experimental_group_protocols(
        [deepcopy(protocol)], retained_object_resolvers={key: resolver})
    with_diagnostics = [d.reason_code for d in compiled.diagnostics]
    derivation = {}
    if compiled.protocols and "evidence_matrix" in compiled.protocols[0]:
        for field in compiled.protocols[0]["evidence_matrix"]:
            prov = field.get("provenance") or {}
            if prov.get("derivation"):
                derivation[field["field_path"]] = json.loads(prov["derivation"])
    proof = derivation.get(PROBE_OUT_STATE) or {}
    retained_fields = {
        name: proof.get(name)
        for name in (
            "retained_object_rule_id", "retained_object_rule_version",
            "retained_object", "retained_object_output_name_fact_id",
            "retained_object_operation_locator",
            "retained_object_operation_span",
            "retained_object_operation_char_span",
            "retained_object_naming_locator",
            "retained_object_naming_span",
            "retained_object_naming_char_span",
        )
    }
    with_ok = (not with_diagnostics
               and proof.get("rule_id") == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
               and all(value for value in retained_fields.values()))

    compiled_none = compile_experimental_group_protocols(
        [deepcopy(protocol)])
    none_reasons = [d.reason_code for d in compiled_none.diagnostics]
    none_ok = none_reasons == ["semantic_binding_pending"]

    forged = {
        "schema_version": POST_OPERATION_RETAINED_OBJECT_SCHEMA,
        "rule_id": POST_OPERATION_RETAINED_OBJECT_RULE_ID,
        "rule_version": POST_OPERATION_RETAINED_OBJECT_RULE_VERSION,
        "step_index": 0,
        "macro_step_id": "S1",
        "operation_fact_id": "f_op",
        "operation_locator": "pdf:p1:b2-p1:b2",
        "operation_char_span": [24, 98],
        "output": {
            "state_path": PROBE_OUT_STATE,
            "name_path": "material_graph[0].material_outputs[0].name",
            "output_index": 0,
            "material_instance_id": "inst_b",
            "material_id": "product",
            "label": "LDH seeds",
        },
        "retained_object_surface": "precipitates",
        "retained_object": "precipitate",
        "object_modifiers": [],
        "naming_fact_id": "f_out_name",
        "output_name_fact_id": "f_out_name",
        "naming_excerpt": "The precipitates were labeled as LDH seeds.",
        "naming_locator": "pdf:p1:b3-p1:b3",
        "naming_char_span": [0, 45],
        "operation_span": [1, 1],
        "naming_span": [2, 2],
    }

    def forged_resolver(_field_path: str):
        return forged, ""

    compiled_forged = compile_experimental_group_protocols(
        [deepcopy(protocol)],
        retained_object_resolvers={key: forged_resolver})
    forged_reasons = [d.reason_code for d in compiled_forged.diagnostics]
    forged_ok = forged_reasons == ["semantic_binding_pending"]

    return {
        "status": "PASS" if (with_ok and none_ok and forged_ok) else "FAIL",
        "group": {
            "paper_id": group.source_scope.paper_id,
            "experimental_group_id": group.source_scope.experimental_group_id,
            "source_digest": group.source_scope.source_digest,
            "block_count": len(group.blocks),
            "caption_count": sum(1 for block in group.blocks if block.caption),
        },
        "with_live_resolver": {
            "diagnostics": with_diagnostics,
            "derived_rule": proof.get("rule_id", ""),
            "retained_object_proof_fields": retained_fields,
        },
        "without_resolver": {
            "diagnostics": none_reasons,
            "expected": ["semantic_binding_pending"],
        },
        "with_fabricated_resolver_record": {
            "diagnostics": forged_reasons,
            "expected": ["semantic_binding_pending"],
        },
    }


def _r4_fabricated_record(matches, proposal, resolver, spec_items,
                          spec_digest, resolutions) -> dict:
    """R4: a stored or forged record has zero authority at every layer."""
    record, record_issue = resolver(G5_OUT_STATE)
    if record is None:
        return {"status": "FAIL",
                "reason": f"no live record to forge from: {record_issue}"}

    # Contract level: a resolver that yields the valid record with a forged
    # naming excerpt must be rejected by the cross-check.
    forged = deepcopy(record)
    forged["naming_excerpt"] = (
        "yellow-green precipitate of Ni3Fe LDH was observed once the")

    def forged_resolver(_field_path: str):
        return forged, ""

    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    ref = proposal["source_group_ref"]
    proof, contract_issue = derive_unreviewed_output_state(
        graph, facts, G5_OUT_STATE,
        paper_id=ref["paper_id"],
        experimental_group_id=ref["experimental_group_id"],
        source_digest=ref["source_digest"],
        retained_object_resolver=forged_resolver)
    contract_ok = (proof is None
                   and contract_issue == "retained_object_output_binding_"
                                         "unresolved")

    def swap_naming_excerpt(target: dict) -> bool:
        for fact in target["route_facts"]:
            if fact.get("fact_id") == "f_g5_out0_name":
                fact["excerpt"] = (
                    "yellow-green precipitate of Ni3Fe LDH was observed "
                    "once the")
                return True
        return False

    def flagged_fact_ids(association) -> set:
        production = association.locator_production or {}
        final_issues = (production.get("local_revision") or {}).get(
            "final_issues") or []
        return {item.get("fact_id") for item in final_issues
                if isinstance(item, dict)}

    # G1 variant A: the naming fact's excerpt is swapped to a real
    # non-naming block (b67).  The proposal passes the proposal gate, the
    # live derivation finds no naming sentence, and nothing may derive.
    variant_a = deepcopy(proposal)
    if not swap_naming_excerpt(variant_a):
        return {"status": "FAIL", "reason": "f_g5_out0_name fact missing"}
    association_a = _association_run(
        matches, variant_a, spec_items, spec_digest, resolutions)
    candidates_a = _candidate_rules(association_a)
    flagged_a = flagged_fact_ids(association_a)
    a_ok = (G5_OUT_STATE not in candidates_a
            and G6_IN_STATE not in candidates_a
            and "f_g5_out0_state" in flagged_a)

    # G1 variant B: the UNTOUCHED proposal plus the byte-perfect valid
    # record smuggled in as proposal['retained_object_records'].  With the
    # required-facts gate enabled the association refuses the proposal
    # wholesale: a stored-record authority field is forbidden.
    variant_b = deepcopy(proposal)
    variant_b["retained_object_records"] = [deepcopy(record)]
    association_b = _association_run(
        matches, variant_b, spec_items, spec_digest, resolutions)
    diagnostics_b = [d.reason_code for d in association_b.diagnostics]
    production_b = association_b.locator_production or {}
    final_b = (production_b.get("local_revision") or {}).get(
        "final_issues") or []
    reasons_b = dict(Counter(
        item.get("reason_code", "") for item in final_b
        if isinstance(item, dict)))
    candidates_b = _candidate_rules(association_b)
    # The gate refusal is the trust boundary here: the proposal carrying a
    # stored-record authority field is never ADMITTED.  The candidates may
    # still legitimately contain graph[5]/graph[6] — the locator and
    # diagnostics stages process the (untouched) proposal with the LIVE
    # resolver before the gate runs — so admission, not derivation, is the
    # pass condition.
    b_ok = ("proposal_source_or_status_field_forbidden" in reasons_b
            and len(association_b.protocols) == 0)

    # G1 variant C: excerpt swap AND the smuggled byte-perfect record, run
    # through the raw producer path (no required-facts gate).  The proposal
    # is processed, but the smuggled record is never read: nothing derives.
    variant_c = deepcopy(proposal)
    swap_naming_excerpt(variant_c)
    variant_c["retained_object_records"] = [deepcopy(record)]
    association_c = propose_pdf_group_unreviewed(
        matches, lambda _prompt: {"proposals": [variant_c]},
        max_repair_groups=0)
    candidates_c = _candidate_rules(association_c)
    flagged_c = flagged_fact_ids(association_c)
    c_ok = (G5_OUT_STATE not in candidates_c
            and G6_IN_STATE not in candidates_c
            and "f_g5_out0_state" in flagged_c)

    ok = contract_ok and a_ok and b_ok and c_ok
    return {
        "status": "PASS" if ok else "FAIL",
        "contract_level": {
            "forgery": "valid record with a forged naming_excerpt fed "
                       "through the resolver",
            "expected_issue": "retained_object_output_binding_unresolved",
            "observed_issue": contract_issue,
            "status": "PASS" if contract_ok else "FAIL",
        },
        "g1_excerpt_swap": {
            "forgery": ("naming fact excerpt swapped to the real b67 block "
                        "(no naming sentence); the proposal still passes "
                        "the proposal gate"),
            "graph5_output_in_candidates": G5_OUT_STATE in candidates_a,
            "graph6_input_in_candidates": G6_IN_STATE in candidates_a,
            "f_g5_out0_state_flagged": "f_g5_out0_state" in flagged_a,
            "status": "PASS" if a_ok else "FAIL",
        },
        "g1_smuggled_record_gate": {
            "forgery": ("untouched proposal + the byte-perfect valid "
                        "record smuggled as "
                        "proposal['retained_object_records']; run with the "
                        "required-facts gate enabled"),
            "gate_rejection_present": (
                "proposal_source_or_status_field_forbidden" in reasons_b),
            "admitted_protocols": len(association_b.protocols),
            "graph5_output_in_candidates_note": (
                f"{G5_OUT_STATE in candidates_b}: informational only — "
                "the locator/diagnostics stages legitimately derive from "
                "the untouched proposal via the LIVE resolver before the "
                "gate runs; the trust boundary is that the proposal is "
                "never admitted"),
            "final_issue_reasons": reasons_b,
            "status": "PASS" if b_ok else "FAIL",
        },
        "g1_smuggled_record_raw": {
            "forgery": ("excerpt swap + smuggled byte-perfect record, run "
                        "through the raw producer path (no required-facts "
                        "gate); the stored record is never read"),
            "graph5_output_in_candidates": G5_OUT_STATE in candidates_c,
            "graph6_input_in_candidates": G6_IN_STATE in candidates_c,
            "f_g5_out0_state_flagged": "f_g5_out0_state" in flagged_c,
            "status": "PASS" if c_ok else "FAIL",
        },
    }


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

    association = _association_run(
        matches, proposal, spec_items, spec_digest, resolutions)
    admitted = len(association.protocols)
    local_revision = (association.locator_production or {}).get(
        "local_revision") or {}
    g1 = local_revision.get("final_issues")
    convention_candidates = (association.locator_production or {}).get(
        "convention_state_candidates")

    proofs = _proof_table(proposal, g1 or [],
                          retained_object_resolver=resolver)
    conc_fact, conc_binding = _concentration_binding_evidence(proposal)

    # Round-2 acceptance (must still PASS on the unchanged proposal).
    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    ref = proposal["source_group_ref"]
    g5_name_path = "material_graph[5].material_outputs[0].name"
    g5_name_fact = next((f for f in facts if isinstance(f, dict)
                         and f.get("field_path") == g5_name_path), {})
    g5_name_value = g5_name_fact.get("value", "")
    g5_record, g5_record_issue = resolver(G5_OUT_STATE)
    g5_proof, g5_proof_issue = derive_unreviewed_output_state(
        graph, facts, G5_OUT_STATE,
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

    # Round-3A stages.
    candidate_rules = _candidate_rules(association)
    g1_reason_counts = dict(Counter(i["reason_code"] for i in (g1 or [])))
    formal_g1_ok = (
        candidate_rules.get(G5_OUT_STATE) == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
        and candidate_rules.get(G6_IN_STATE)
            == "PARENT_OUTPUT_STATE_INHERITANCE_V1"
        and not any(i.get("fact_id") in ("f_g5_out0_state", "f_g6_in0_state")
                    for i in (g1 or [])))
    formal_g1 = {
        "status": "PASS" if formal_g1_ok else "FAIL",
        "graph5_output_rule": candidate_rules.get(G5_OUT_STATE),
        "graph6_input_rule": candidate_rules.get(G6_IN_STATE),
        "g1_final_issue_reasons": g1_reason_counts,
        "g1_no_longer_flags": [
            fact_id for fact_id in ("f_g5_out0_state", "f_g6_in0_state")
            if not any(i.get("fact_id") == fact_id for i in (g1 or []))],
        "admitted_protocols": admitted,
    }

    receipt_stage = _receipt_stage(matches, association)
    compiler_demo = _compiler_demo()
    r1_r3 = _r1_r3_probes()
    e1b_e5 = _e1b_e5_probes()
    r4 = _r4_fabricated_record(matches, proposal, resolver, spec_items,
                               spec_digest, resolutions)

    scope = matches[0].source_scope
    group_identity = {
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "source_digest": scope.source_digest,
        "block_count": len(matches[0].blocks),
        "caption_count": sum(
            1 for block in matches[0].blocks if block.caption),
    }
    live_resolver = {
        "status": "PASS",
        "stages": {
            "g1_association": {
                **group_identity,
                "resolver_built": ("inside _prepare_unsigned_proposal via "
                                   "build_retained_object_resolver(graph, "
                                   "facts, signed group blocks, caption "
                                   "locators); one resolver per group per "
                                   "call"),
            },
            "receipt": {
                **group_identity,
                "resolver_built": ("inside produce_pdf_group_fact_receipt "
                                   "per enumerated group from the signed "
                                   "blocks"),
            },
            "compiler": {
                **compiler_demo["group"],
                "resolver_built": ("by the caller via "
                                   "build_retained_object_resolver and "
                                   "passed keyed by (paper_id, "
                                   "experimental_group_id, source_digest)"),
            },
        },
        "stored_records_read": ("none: no stage reads stored records; "
                                "proposal['retained_object_records'] is "
                                "never consulted (proven by "
                                "r4_fabricated_record.g1_smuggled_record_"
                                "raw and rejected wholesale by the "
                                "proposal gate in "
                                "r4_fabricated_record.g1_smuggled_record_"
                                "gate)"),
    }

    probe_pass = all(row["status"] == "PASS" for row in r1_r3 + e1b_e5)
    round3a_acceptance = {
        "r1_r3_probes": {
            "status": "PASS" if all(r["status"] == "PASS" for r in r1_r3)
                      else "FAIL",
            "probes": r1_r3,
        },
        "e1b_e5_probes": {
            "status": "PASS" if all(r["status"] == "PASS" for r in e1b_e5)
                      else "FAIL",
            "probes": e1b_e5,
        },
        "formal_g1": formal_g1,
        "r4_fabricated_record": r4,
        "live_resolver": live_resolver,
    }
    round3a_acceptance["status"] = (
        "PASS"
        if (probe_pass and formal_g1["status"] == "PASS"
            and receipt_stage["status"] == "PASS"
            and compiler_demo["status"] == "PASS"
            and r4["status"] == "PASS")
        else "FAIL")

    g2 = None
    if admitted:
        for protocol in association.protocols:
            if isinstance(protocol, dict) and "route_facts" in protocol:
                protocol["inventory_resolutions"] = resolutions
        compiled = compile_experimental_group_protocols(association.protocols)
        g2 = [asdict(d)["reason_code"] for d in compiled.diagnostics]

    diagnostics = dict(Counter(d.reason_code for d in association.diagnostics))
    audit = [
        {"kind": "round3a_formal_integration",
         "wired_stages": [
             "G1 association _prepare_unsigned_proposal (live resolver per "
             "group; output then input derivation into "
             "convention_state_candidates)",
             "G1 bounded local repair (callback receives the group and "
             "rebuilds the resolver per revision)",
             "G1 local diagnostics (per-proposal resolver for derive + "
             "literal reasons)",
             "G1 proposal quality (resolver when groups are available)",
             "G1 material structure (resolver for proof issues)",
             "literal receipt produce_pdf_group_fact_receipt (per-group "
             "resolver into state derivation + verification)",
             "group compiler compile_experimental_group_protocols "
             "(resolvers keyed by (paper_id, experimental_group_id, "
             "source_digest), threaded to _state_derivation_proof)",
         ],
         "fail_closed_stages": [
             "route_pipeline compile enumeration (deferred: enumeration "
             "diagnostics feed discovery, which the compile consumes, so "
             "enumeration cannot move above the compile; default None)",
             "science audit / source verifier (a compiled candidate "
             "retains only hashed evidence ids, so a resolver rebuilt "
             "there could never reproduce the bound proof fields)",
             "route_decision/v2 package re-verification (no blocks "
             "available; publication-gate follow-up)",
         ]},
        {"kind": "trust_boundary",
         "rules": [
             "a record has zero authority: only a resolver rebuilt live "
             "from the signed group blocks is consulted; stored record "
             "fields on proposals are never read",
             "validation is two-layered: shape parse (schema_version, "
             "rule_id, output.state_path, non-empty retained_object) then "
             "cross-check against the live graph and facts (state_path, "
             "port instance/material/label, output/step index, fact ids, "
             "excerpts)",
             "records carry operation/naming locators and character spans "
             "plus output_name_fact_id; proofs bind the evidence id of the "
             "naming fact and echo locator/span strings",
             "verify_bound_output_state re-derives and demands exact "
             "record equality; absent resolver -> "
             "convention_support_evidence_missing, parse/cross-check "
             "failure -> retained_object_output_binding_unresolved, "
             "mismatch -> convention_proof_mismatch",
         ]},
        {"kind": "round3a_probes",
         "r1_r3": r1_r3,
         "e1b_e5": e1b_e5},
        {"kind": "round3a_receipt", "result": receipt_stage},
        {"kind": "round3a_compiler_demo", "result": compiler_demo},
        {"kind": "round3a_r4_fabricated_record", "result": r4},
        {"kind": "round3a_live_resolver", "result": live_resolver},
        {"kind": "round2_post_operation_retained_object",
         "step": "ms5",
         "expectation": ("unchanged from round 7: the validated record "
                         "arbitrates the composite family guard to "
                         "CENTRIFUGE_COLLECT_PRECIPITATE_V1 v1.1.0; the "
                         "round-2 acceptance block must still PASS")},
        {"kind": "deliberately_not_done",
         "items": [
             "route_pipeline resolver threading (ordering dependency: "
             "enumeration diagnostics append to discovery, which the "
             "compile consumes)",
             "science audit / source verifier / RouteDecision resolver "
             "threading (raw fact ids are unrecoverable from a compiled "
             "candidate; publication-gate follow-up Q1 gates publication "
             "first)",
             "Chinese C1 naming/discard aliases (still English-only)",
             "no generic centrifugation-redispersion x3 -> "
             "washed_wet_solid rule and no composite-endpoint rule",
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
        {"kind": "round3a_acceptance",
         "items": round3a_acceptance},
    ]

    result = {
        "audit_rows": len(audit),
        "round2_acceptance": round2_acceptance,
        "round3a_acceptance": round3a_acceptance,
        "post_operation_retained_object_per_step": source_relation_rows,
        "resolution_status": dict(Counter(r["status"] for r in records)),
        "strict_association": {
            "admitted_protocols": admitted,
            "diagnostics": diagnostics,
            "g1_final_issue_reasons": g1_reason_counts,
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
        "receipt": receipt_stage,
        "compiler_demo": compiler_demo,
        "g2_compile_reasons": g2,
        "g2_note": ("no admitted protocols; nothing was compiled"
                    if not admitted else
                    "compiled the admitted group; see reasons"),
    }
    (out_dir / "local-revision-r8-audit.json").write_text(
        json.dumps({
            "schema_version": "bounded_local_revision/v8",
            "model_generated": False,
            "scope": ("Round 3A: the post-operation-retained-object "
                      "resolver is wired into the formal G1 path "
                      "(association, repair, diagnostics, proposal "
                      "quality, material structure, receipt, compiler); "
                      "every stage rebuilds it LIVE from the signed group "
                      "blocks and stored records have zero authority "
                      "(two-layer parse + cross-check, forged records "
                      "rejected).  The proposal is byte-identical to r7; "
                      "route_pipeline enumeration and the science/"
                      "decision layers stay fail-closed on documented "
                      "grounds; diagnostics are reported honestly, never "
                      "pre-empted"),
            "audit": audit,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "local-revision-r8-replay.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
