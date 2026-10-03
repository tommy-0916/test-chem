"""Round-11 (Round 3D phase 2): real-chain acceptance of the narrowly-scoped
protocol-reference proof class for the inherited liquid medium.

Round 3D phase 1 added the contract machinery: ``route_protocol_reference``
(protocol-definition/v1 with a CLOSED slot set — ``operation_sequence`` +
``liquid_medium`` only; execution counts are invocation-local annotations;
10 forbidden result/object slots rejected by schema validation), the
``protocol_reference`` DAG node (``protocol-reference-proof/v1``, premises
``definition_evidence`` + ``reference_evidence``), the
``_VerifiedLiquidMedium`` capability token discharging ONLY the flat
engine's liquid-participation gate for the exact affirmed operation
binding, and the ``liquid_medium`` premise role with its binding invariant
(``proof_dag_liquid_medium_binding_mismatch``) plus the retained-object
scope guard (``proof_dag_retained_object_binding_mismatch``).

Phase 2 (this runner) runs that machinery over the REAL signed NiFe
Control group on the UNCHANGED round-10 proposal (read-only): the
protocol definition is graph[5]'s operation fact excerpt ("...a
centrifugation−redispersion protocol using deionized water three times,
which was the first centrifugation−redispersion protocol", U+2212); the
reference is the graph[7]/graph[8] operation fact ("Finally, after a
second centrifugation−redispersion protocol one time...").  The runner:

- builds and verifies ``protocol_reference`` DAG nodes for
  graph[7].operation AND graph[8].operation against the real signed
  blocks (span-resolver and blocks-only verification), emitting the full
  traceable resolution record (evidence ids, ordinals, digests, locators,
  group scope) and asserting the definition record carries NO
  execution-count slot — "three times"/"one time" live only as
  invocation-local annotations on the resolution record;
- re-asserts the carried-forward r10 state table: all nine nodes keep
  their r10 verdicts (a recording builder proves the protocol-reference
  machinery never auto-engages anywhere on this chain — the compound
  reference steps hit the 3C compound-operation guard before the liquid
  gate, exactly the documented v1 boundary);
- ms7a no-leak: the resolver issue and the flat derive issue for
  ms7a.out are byte-identical to r10's committed replay, and no
  protocol_reference node or liquid token participates in ms7a's
  derivation;
- updates the ms7b dual result: the independent liquid-participation
  diagnostic is now PASS via the verified protocol_reference node (liquid
  token minted in the diagnostic harness ONLY, labelled
  diagnostics_only / feeds_verdict false), while the honest engine
  what-if (parent + liquid tokens minted) stays BLOCKED on the surviving
  retained-object/composite-arbitration gap for the second protocol;
- counter-example stress tests on real signed data: the nearer
  non-definition medium phrase ("dispersed in 30 mL of water and aged")
  never enters the candidate-definition set, and the paper's Etching
  group's "after the first centrifugation−redispersion protocol"
  reference fails closed (scope mismatch) instead of binding the Control
  group's definition — cross-group isolation in both directions;
- tamper items for the new proof class on the real chain: definition
  quote mutation, reference quote mutation, and a resealed substitution
  of an unrelated valid node into the definition_evidence role.

Anything not proven is reported BLOCKED with the exact issue, never as a
pass.  The r7/r8 runners re-run byte-identical; r9/r10 re-run with all
acceptance items PASS and byte-identical replay/audit JSONs (the r10
ms7b liquid FAIL string is historical 3C content and must not change).
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import re
import sys
from unittest import mock

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from chem_agent_contracts import route_convention_basis
from chem_agent_contracts.route_convention_basis import (
    _LIQUID_EXCERPT_TOKENS,
    _affirmative_pattern_match,
    _literal_in_quote,
    _mint_diagnostic_liquid_medium_assumption,
    _mint_diagnostic_parent_state_assumption,
    _resolve_fact_provenance,
    _rule_liquid_participation,
    _rule_resource,
    _step_has_liquid_participation,
    convention_fact_evidence_by_id,
    derive_unreviewed_output_state,
    verify_bound_output_state,
)
from chem_agent_contracts.route_proof_dag import (
    STATE_PROOF_DAG_SCHEMA,
    StateProofDagVerifier,
    _DagBuilder,
    _protocol_reference_candidates,
    build_state_proof_dag,
    leaf_id_for,
    node_id_for,
    verify_state_proof_dag,
)
from chem_agent_contracts.route_protocol_reference import (
    PROTOCOL_DEFINITION_SCHEMA,
    PROTOCOL_REFERENCE_PROOF_SCHEMA,
    PROTOCOL_REFERENCE_RULE_ID,
    PROTOCOL_REFERENCE_RULE_VERSION,
    extract_protocol_definition,
    protocol_definition_digest,
    resolve_protocol_reference,
    validate_protocol_definition,
)
from chem_agent_contracts.route_retained_object import (
    build_excerpt_span_resolver,
    derive_post_operation_retained_object,
)
from chem_agent_contracts.v2 import (
    LineageRelationV2,
    LogicalContainerV2,
    MaterialOperationSegmentV2,
    MaterialRelationV2,
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
R10_REPLAY_PATH = out_dir / "local-revision-r10-replay.json"

COMPOUND_OPERATION = "second centrifugation−redispersion protocol"

G5_OUT_STATE = "material_graph[5].material_outputs[0].state"
G6_IN_STATE = "material_graph[6].material_inputs[0].state"
G6_OUT_STATE = "material_graph[6].material_outputs[0].state"
G7_IN_STATE = "material_graph[7].material_inputs[0].state"
G7_OUT_STATE = "material_graph[7].material_outputs[0].state"
G8_IN_STATE = "material_graph[8].material_inputs[0].state"
G8_OUT_STATE = "material_graph[8].material_outputs[0].state"
G9_IN_STATE = "material_graph[9].material_inputs[0].state"
G9_OUT_STATE = "material_graph[9].material_outputs[0].state"
G5_OPERATION = "material_graph[5].operation"
G6_OPERATION = "material_graph[6].operation"
G5_OUT_NAME = "material_graph[5].material_outputs[0].name"

RETAINED_GAP = "retained_object_mention_precedes_operation"
BINDING_MISMATCH = "proof_dag_parent_state_binding_mismatch"

DAG_TARGETS = {
    "graph5_output": G5_OUT_STATE,
    "graph6_input": G6_IN_STATE,
    "graph6_output": G6_OUT_STATE,
    "graph7_input": G7_IN_STATE,
}

# Nodes that must NOT build: the honest blocker and its cascades.
BLOCKED_TARGETS = {
    "graph7_output": G7_OUT_STATE,
    "graph8_input": G8_IN_STATE,
    "graph8_output": G8_OUT_STATE,
    "graph9_input": G9_IN_STATE,
    "graph9_output": G9_OUT_STATE,
}

NODE_LABELS = {
    "graph5_output": "graph[5].out",
    "graph6_input": "graph[6].in",
    "graph6_output": "graph[6].out",
    "graph7_input": "graph[7].in (ms7a.in)",
    "graph7_output": "ms7a.out (graph[7].out)",
    "graph8_input": "ms7b.in (graph[8].in)",
    "graph8_output": "ms7b.out (graph[8].out)",
    "graph9_input": "graph[9].in",
    "graph9_output": "graph[9].out",
}


def _fail(message: str) -> None:
    raise SystemExit(f"local-revision-r11 contract/preflight failure: {message}")


# ---------------------------------------------------------------------------
# Preflight (identical in shape to the r9 runner).
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
                "label": record["output"]["label"],
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


# ---------------------------------------------------------------------------
# DAG helpers (identical to the r9 runner).
# ---------------------------------------------------------------------------


def _node_summary(node: dict) -> dict:
    summary = {
        "node_id": node["node_id"],
        "node_type": node["node_type"],
        "schema_version": node["schema_version"],
        "premise_roles": [premise["role"] for premise in node["premises"]],
    }
    if node["node_type"] == "paper_literal":
        summary["leaf_field_path"] = node["leaf"]["field_path"]
        summary["leaf_claim_value"] = node["leaf"].get("claim_value")
        summary["leaf_locator"] = node["leaf"].get("locator", "")
        summary["leaf_char_span"] = node["leaf"].get("char_span", [])
    else:
        summary["rule_id"] = node.get("rule_id", "")
        summary["rule_version"] = node.get("rule_version", "")
        summary["claim"] = node.get("claim", {})
    return summary


def _dag_summary(dag: dict) -> dict:
    root = dag["nodes"][dag["root_id"]]
    return {
        "schema_version": dag["schema_version"],
        "root_id": dag["root_id"],
        "root_node_type": root["node_type"],
        "root_schema": root["schema_version"],
        "root_rule_id": root.get("rule_id", ""),
        "root_rule_version": root.get("rule_version", ""),
        "context": dag["context"],
        "node_count": len(dag["nodes"]),
        "nodes": [_node_summary(node) for node in dag["nodes"].values()],
    }


def _chain_summary(dag: dict) -> list[dict]:
    """Premise tree from the root down, one row per node (first visit)."""
    rows: list[dict] = []
    seen: set[str] = set()

    def walk(node_id: str, role: str, depth: int) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = dag["nodes"][node_id]
        row = {
            "depth": depth,
            "role_to_parent": role,
            "node_type": node["node_type"],
            "rule_id": node.get("rule_id", ""),
            "claim_field_path": node.get("claim", {}).get("field_path", ""),
            "claim_target_state": node.get("claim", {}).get(
                "target_state", ""),
        }
        if node["node_type"] == "paper_literal":
            row["leaf_field_path"] = node["leaf"]["field_path"]
        rows.append(row)
        for premise in node["premises"]:
            walk(premise["node_id"], premise["role"], depth + 1)

    walk(dag["root_id"], "(root)", 0)
    return rows


def _dependents(dag: dict, node_id: str) -> list[str]:
    """Every node that transitively stands on ``node_id`` as a premise."""
    reverse: dict[str, list[str]] = {}
    for other_id, node in dag["nodes"].items():
        for premise in node["premises"]:
            reverse.setdefault(premise["node_id"], []).append(other_id)
    seen: set[str] = set()
    stack = list(reverse.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(reverse.get(current, []))
    return sorted(seen)


def _reseal(dag: dict, target_id: str, mutate) -> dict:
    """Mutate one node, then honestly recompute every dependent address.

    The same cascade a determined forger with access to this module's own
    hashing must perform: after touching any node, every content address up
    the premise chain to the root is recomputed.
    """
    dag = deepcopy(dag)
    mutate(dag["nodes"][target_id])
    while True:
        changed = False
        for node_id in list(dag["nodes"]):
            node = dag["nodes"][node_id]
            if isinstance(node.get("leaf"), dict):
                leaf_id = leaf_id_for(node["leaf"])
                if leaf_id != node["leaf"].get("leaf_id"):
                    node["leaf"]["leaf_id"] = leaf_id
            recomputed = node_id_for(node)
            if recomputed == node_id:
                continue
            del dag["nodes"][node_id]
            node["node_id"] = recomputed
            dag["nodes"][recomputed] = node
            for other in dag["nodes"].values():
                for premise in other["premises"]:
                    if premise["node_id"] == node_id:
                        premise["node_id"] = recomputed
            if dag["root_id"] == node_id:
                dag["root_id"] = recomputed
            changed = True
            break
        if not changed:
            return dag


def _build_all(graph, facts, scope, span_of) -> dict:
    dags: dict[str, dict] = {}
    for name, path in DAG_TARGETS.items():
        dag, issue = build_state_proof_dag(
            graph, facts, path, span_of=span_of, **scope)
        if dag is None:
            _fail(f"proof DAG for {path} did not build: {issue}")
        dags[name] = dag
    return dags


def _build_blocked(graph, facts, scope, span_of) -> dict:
    """Build the nodes that must NOT build; record (dag, issue) per node."""
    builds: dict[str, dict] = {}
    for name, path in BLOCKED_TARGETS.items():
        dag, issue = build_state_proof_dag(
            graph, facts, path, span_of=span_of, **scope)
        builds[name] = {"field_path": path, "dag": dag, "build_issue": issue}
    return builds


def _verify_both(dag, graph, facts, scope, span_of, blocks, captions) -> dict:
    span_issue = verify_state_proof_dag(
        dag, graph, facts, span_of=span_of, **scope)
    blocks_issue = StateProofDagVerifier(
        graph, facts, blocks=blocks, caption_block_locators=captions,
        **scope).verify(dag)
    return {"verify_with_span_resolver": span_issue,
            "verify_blocks_only": blocks_issue}


# ---------------------------------------------------------------------------
# 3C-1 acceptance: the r10 proposal is r8 with ONLY the graph[7]/graph[8]
# representation fix, and the new compound operation evidence binds.
# ---------------------------------------------------------------------------


def _diff_paths(a, b, path: str = "") -> list[str]:
    if type(a) is not type(b):
        return [path or "(root)"]
    if isinstance(a, dict):
        paths: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                paths.append(f"{path}.{key}" if path else str(key))
            else:
                paths.extend(_diff_paths(a[key], b[key],
                                         f"{path}.{key}" if path else str(key)))
        return paths
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}[len {len(a)}!={len(b)}]"]
        paths = []
        for index, (item_a, item_b) in enumerate(zip(a, b)):
            paths.extend(_diff_paths(item_a, item_b, f"{path}[{index}]"))
        return paths
    return [] if a == b else [path or "(root)"]


def _accept_representation(proposal: dict, group) -> dict:
    """3C-1: graph[7]/graph[8] mirror graph[5]'s compound representation."""
    r8 = json.loads(R8_PROPOSAL_PATH.read_text(encoding="utf-8"))
    r10 = json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))
    changed = _diff_paths(r8, r10)
    allowed_prefixes = (
        "proposals[0].material_graph[7].operation",
        "proposals[0].material_graph[7].operation_segments",
        "proposals[0].material_graph[8].operation",
        "proposals[0].material_graph[8].operation_segments",
    )
    fact_prefixes = tuple(
        f"proposals[0].route_facts[{index}].value"
        for index, fact in enumerate(r8["proposals"][0]["route_facts"])
        if fact.get("fact_id") in ("f_g7a_op", "f_g7b_op"))
    only_expected = all(
        path.startswith(allowed_prefixes + fact_prefixes)
        for path in changed)
    facts_path_changed = all(
        any(path.startswith(prefix) for prefix in changed)
        for path in fact_prefixes)

    graph = proposal["material_graph"]
    by_fact = {fact["fact_id"]: fact for fact in proposal["route_facts"]}
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]

    step_rows: list[dict] = []
    for index, step_id, fact_id, claimed in (
        (7, "ms7a", "f_g7a_op", "centrifugation"),
        (8, "ms7b", "f_g7b_op", "redispersion"),
    ):
        step = graph[index]
        segments = step.get("operation_segments", [])
        segment_pairs = [
            (segment["segment_id"], segment["material_effect"],
             segment["provenance"]) for segment in segments]
        relation_refs = [relation["source_operation_ref"]
                         for relation in step["material_relations"]]
        fact = by_fact[fact_id]
        binding, issue = bind_pdf_quote(
            blocks, fact.get("excerpt"), caption_block_locators=captions)
        sentence_blocks = [
            {"locator": block.locator, "text": block.text}
            for block in group.blocks
            if "second centrifugation" in block.text
            or "protocol one time" in block.text
        ]
        step_ok = (
            step["operation"] == COMPOUND_OPERATION
            and step["provenance"]
                == {"kind": "paper", "reference": f"fact:{fact_id}"}
            and [pair[0] for pair in segment_pairs] == [
                f"{step_id}-seg-centrifugation",
                f"{step_id}-seg-redispersion"]
            and dict((pair[0], pair[1]) for pair in segment_pairs) == {
                f"{step_id}-seg-{claimed}": "transform_material",
                f"{step_id}-seg-{'redispersion' if claimed == 'centrifugation' else 'centrifugation'}": "unknown",
            }
            and all(pair[2] == {"kind": "paper",
                                "reference": f"fact:{fact_id}"}
                    for pair in segment_pairs)
            and relation_refs == [f"{step_id}-seg-{claimed}"]
            and fact["value"] == COMPOUND_OPERATION
            and _literal_in_quote(fact["value"], fact["excerpt"])
            and issue == ""
            and binding is not None
        )
        step_rows.append({
            "step_index": index,
            "macro_step_id": step_id,
            "operation": step["operation"],
            "operation_fact_id": fact_id,
            "operation_fact_value": fact["value"],
            "operation_fact_excerpt": fact["excerpt"],
            "operation_fact_excerpt_locator": (
                binding.locator if binding else ""),
            "operation_fact_binding_issue": issue,
            "value_literal_in_quote": _literal_in_quote(
                fact["value"], fact["excerpt"]),
            "segments": [
                {"segment_id": pair[0], "material_effect": pair[1]}
                for pair in segment_pairs],
            "relation_source_operation_ref": relation_refs,
            "signed_blocks_covering_sentence": sentence_blocks,
            "status": "PASS" if step_ok else "FAIL",
        })

    ok = (only_expected and facts_path_changed and changed
          and all(row["status"] == "PASS" for row in step_rows))
    return {
        "status": "PASS" if ok else "FAIL",
        "representation": ("graph[7] (ms7a) and graph[8] (ms7b) source "
                           "operation is the compound 'second "
                           "centrifugation−redispersion protocol' (U+2212), "
                           "mirroring graph[5]: two operation_segments "
                           "(centrifugation + redispersion), provenance "
                           "kind=paper -> the step's operation evidence id, "
                           "material_relations[*].source_operation_ref = the "
                           "claimed segment_id; the claimed segment keeps "
                           "material_effect transform_material, the "
                           "unclaimed segment is unknown"),
        "rationale": ("the bare-word representation (operation "
                      "'centrifugation'/'redispersion' standing alone on the "
                      "compound quote) bypassed the compound-operation guard "
                      "and misattributed blockers; this is an "
                      "evidence-semantics precondition, not cosmetic"),
        "changed_paths_vs_r8": changed,
        "only_graph7_graph8_changed": only_expected,
        "operation_facts_value_updated": facts_path_changed,
        "steps": step_rows,
    }


# ---------------------------------------------------------------------------
# Round-3B acceptance items 1-9 (carried over verbatim from the r9 runner).
# ---------------------------------------------------------------------------


def _accept_1_2_3(dags, graph, facts, scope, span_of, blocks, captions):
    """Items 1-3: the three real DAGs build, verify, and carry the rules."""
    g5 = dags["graph5_output"]
    g5_root = g5["nodes"][g5["root_id"]]
    g5_roles = {premise["role"]: g5["nodes"][premise["node_id"]]["node_type"]
                for premise in g5_root["premises"]}
    v5 = _verify_both(g5, graph, facts, scope, span_of, blocks, captions)
    item1_ok = (
        g5_root["node_type"] == "state_change"
        and g5_root["schema_version"] == "state-change-convention-proof/v1"
        and g5_root["rule_id"] == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
        and g5_root["rule_version"] == "1.1.0"
        and g5_roles == {"operation": "paper_literal",
                         "parent_state": "paper_literal",
                         "retained_object": "source_relation"}
        and v5 == {"verify_with_span_resolver": "",
                   "verify_blocks_only": ""}
    )
    item1 = {
        "status": "PASS" if item1_ok else "FAIL",
        "field_path": G5_OUT_STATE,
        "root_node_type": g5_root["node_type"],
        "root_schema": g5_root["schema_version"],
        "rule_id": g5_root["rule_id"],
        "rule_version": g5_root["rule_version"],
        "premise_roles": g5_roles,
        "node_count": len(g5["nodes"]),
        **v5,
    }

    g6in = dags["graph6_input"]
    in_root = g6in["nodes"][g6in["root_id"]]
    parent_ref = in_root.get("parent_ref")
    in_parent = g6in["nodes"][in_root["premises"][0]["node_id"]]
    v6in = _verify_both(g6in, graph, facts, scope, span_of, blocks, captions)
    item2_ok = (
        in_root["node_type"] == "inheritance"
        and in_root["schema_version"] == "state-inheritance-proof/v1"
        and parent_ref == {"kind": "material_instance",
                           "macro_step_id": "ms5",
                           "material_instance_id": "inst_ldh_seeds"}
        and in_parent["node_type"] == "state_change"
        and v6in == {"verify_with_span_resolver": "",
                     "verify_blocks_only": ""}
    )
    item2 = {
        "status": "PASS" if item2_ok else "FAIL",
        "field_path": G6_IN_STATE,
        "root_node_type": in_root["node_type"],
        "root_schema": in_root["schema_version"],
        "rule_id": in_root["rule_id"],
        "parent_ref": parent_ref,
        "parent_premise_node_type": in_parent["node_type"],
        "parent_premise_rule_id": in_parent.get("rule_id", ""),
        **v6in,
    }

    g6out = dags["graph6_output"]
    out_root = g6out["nodes"][g6out["root_id"]]
    out_roles = {premise["role"]: g6out["nodes"][premise["node_id"]]
                 for premise in out_root["premises"]}
    in_node = out_roles["parent_state"]
    in_parent = g6out["nodes"][next(
        premise["node_id"] for premise in in_node["premises"]
        if premise["role"] == "parent_state")]
    v6out = _verify_both(g6out, graph, facts, scope, span_of, blocks, captions)
    chain = _chain_summary(g6out)
    item3_ok = (
        out_root["node_type"] == "state_change"
        and out_root["rule_id"] == "REDISPERSION_V1"
        and out_root["rule_version"] == "1.1.0"
        and in_node["node_type"] == "inheritance"
        and in_node["rule_id"] == "PARENT_OUTPUT_STATE_INHERITANCE_V1"
        and in_node["claim"]["field_path"] == G6_IN_STATE
        and in_node["claim"]["target_state"] == "retained_wet_solid"
        and in_parent["node_type"] == "state_change"
        and in_parent["rule_id"] == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
        and in_parent["claim"]["field_path"] == G5_OUT_STATE
        and v6out == {"verify_with_span_resolver": "",
                      "verify_blocks_only": ""}
    )
    item3 = {
        "status": "PASS" if item3_ok else "FAIL",
        "field_path": G6_OUT_STATE,
        "root_node_type": out_root["node_type"],
        "rule_id": out_root["rule_id"],
        "rule_version": out_root["rule_version"],
        "premise_chain_summary": {
            "description": (
                "paper literals (operation graph[6], operation graph[5], "
                "graph[5] input state, graph[5] output name) -> "
                "source_relation POST_OPERATION_RETAINED_OBJECT_V1 -> "
                "state_change CENTRIFUGE_COLLECT_PRECIPITATE_V1 "
                "(graph[5].out retained_wet_solid) -> inheritance "
                "PARENT_OUTPUT_STATE_INHERITANCE_V1 (graph[6].in "
                "retained_wet_solid) -> state_change REDISPERSION_V1 "
                "(graph[6].out suspension); the canonical chain: the "
                "graph[6].out root's parent_state premise is the graph[6] "
                "input's own inheritance node, which stands on the "
                "graph[5] output's proven state-change node — each node "
                "answers only its local question, composing the proof the "
                "flat engine could not derive"),
            "tree": chain,
        },
        "node_count": len(g6out["nodes"]),
        **v6out,
    }
    return item1, item2, item3


def _accept_4(dags, graph, facts, scope, resolver, span_of):
    """Item 4: no literal parent state; the flat engine stays blocked."""
    hits = [
        {"fact_id": fact.get("fact_id"), "field_path": fact.get("field_path")}
        for fact in facts
        if "retained_wet_solid" in str(fact.get("excerpt") or "")
    ]
    flat_proof, flat_issue = derive_unreviewed_output_state(
        graph, facts, G6_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver)
    dag_ok = verify_state_proof_dag(
        dags["graph6_output"], graph, facts, span_of=span_of, **scope) == ""
    ok = (not hits and flat_proof is None
          and flat_issue == "convention_parent_state_unverified" and dag_ok)
    return {
        "status": "PASS" if ok else "FAIL",
        "facts_scanned": len(facts),
        "excerpts_containing_retained_wet_solid_literal": hits,
        "flat_engine_graph6_output": {
            "proof": flat_proof,
            "issue": flat_issue,
            "expected_issue": "convention_parent_state_unverified",
        },
        "dag_graph6_output_verify": "" if dag_ok else "FAIL",
        "note": ("the flat engine's parent premise accepts only literal "
                 "gates, so graph[6].out stays honestly unverified there; "
                 "the DAG composes the parent output's own proof node "
                 "instead and verifies"),
    }


def _accept_5(dags, graph, facts, scope, span_of, blocks, captions):
    """Item 5: source, naming, or rule-byte tampering invalidates everything."""
    old_out = dags["graph6_output"]
    old_in = dags["graph6_input"]

    def dependents_of(dag, leaf_field_path):
        leaf_id = next(
            node_id for node_id, node in dag["nodes"].items()
            if node["node_type"] == "paper_literal"
            and node["leaf"]["field_path"] == leaf_field_path)
        return {
            "leaf_node_id": leaf_id,
            "downstream_nodes": [
                {"node_id": other, "node_type": dag["nodes"][other]["node_type"],
                 "rule_id": dag["nodes"][other].get("rule_id", "")}
                for other in _dependents(dag, leaf_id)
            ],
        }

    # (a) operation fact excerpt tampered (a different REAL span of the
    # same signed blocks, still containing the operation phrase and still
    # covering the full operation region, so a rebuilt DAG is derivable but
    # every content address moves).  The child state fact f_g5_out0_state
    # is bound to the SAME operation quote, so both excerpts move together
    # (the convention layer demands the child state quote be the operation
    # quote).
    tampered_a = deepcopy(facts)
    for fact in tampered_a:
        if fact.get("fact_id") in ("f_g5_op", "f_g5_out0_state"):
            fact["excerpt"] = (
                "followed by a centrifugation−redispersion protocol using "
                "deionized water three times, which was the first "
                "centrifugation−redispersion protocol")
    old_issue_a = verify_state_proof_dag(
        old_out, graph, tampered_a, span_of=span_of, **scope)
    old_in_issue_a = verify_state_proof_dag(
        old_in, graph, tampered_a, span_of=span_of, **scope)
    rebuilt_a, issue_a = build_state_proof_dag(
        graph, tampered_a, G6_OUT_STATE, span_of=span_of, **scope)
    a_ok = (
        old_issue_a != "" and old_in_issue_a != "" and rebuilt_a is not None
        and rebuilt_a["root_id"] != old_out["root_id"]
        and verify_state_proof_dag(
            rebuilt_a, graph, tampered_a, span_of=span_of, **scope) == ""
        and verify_state_proof_dag(
            old_out, graph, facts, span_of=span_of, **scope) == ""
    )
    demo_a = {
        "tamper": ("f_g5_op (material_graph[5].operation) and f_g5_out0_"
                   "state excerpts changed together to a different real "
                   "span of the same signed blocks (extended leftwards to "
                   "start at 'followed'; the child state quote must stay "
                   "the operation quote, and the span must keep covering "
                   "the full operation region or the retained-object "
                   "continuity check rejects the rebuild)"),
        "old_graph6_output_dag_verify_tampered_facts": old_issue_a,
        "old_graph6_input_dag_verify_tampered_facts": old_in_issue_a,
        "rebuild_issue": issue_a,
        "root_id_changed": (rebuilt_a is not None
                            and rebuilt_a["root_id"] != old_out["root_id"]),
        "rebuilt_dag_verifies_tampered_facts": (
            rebuilt_a is not None and verify_state_proof_dag(
                rebuilt_a, graph, tampered_a, span_of=span_of,
                **scope) == ""),
        "old_dag_still_verifies_untouched_facts": (
            verify_state_proof_dag(
                old_out, graph, facts, span_of=span_of, **scope) == ""),
        "downstream": dependents_of(old_out, G5_OPERATION),
        "downstream_note": (
            "verification is whole-DAG with premises verified before "
            "dependents and no partial pass: the failed operation leaf "
            "invalidates the source_relation node, the graph[5].out "
            "state_change node, the graph[6].in inheritance node (shown on "
            "the graph6_input DAG), and the graph[6].out root"),
        "status": "PASS" if a_ok else "FAIL",
    }

    # (b) naming fact excerpt tampered -> the recomputed retained-object
    # record changes -> the source-relation node id moves.
    tampered_b = deepcopy(facts)
    for fact in tampered_b:
        if fact.get("fact_id") == "f_g5_out0_name":
            fact["excerpt"] = "The precipitates were labeled as LDH seeds"
    old_issue_b = verify_state_proof_dag(
        old_out, graph, tampered_b, span_of=span_of, **scope)
    old_in_issue_b = verify_state_proof_dag(
        old_in, graph, tampered_b, span_of=span_of, **scope)
    rebuilt_b, issue_b = build_state_proof_dag(
        graph, tampered_b, G6_OUT_STATE, span_of=span_of, **scope)
    relation_ids_old = {
        node_id for node_id, node in old_out["nodes"].items()
        if node["node_type"] == "source_relation"}
    relation_ids_new = ({
        node_id for node_id, node in rebuilt_b["nodes"].items()
        if node["node_type"] == "source_relation"}
        if rebuilt_b is not None else set())
    b_ok = (
        old_issue_b != "" and old_in_issue_b != "" and rebuilt_b is not None
        and relation_ids_old.isdisjoint(relation_ids_new)
        and rebuilt_b["root_id"] != old_out["root_id"]
        and verify_state_proof_dag(
            rebuilt_b, graph, tampered_b, span_of=span_of, **scope) == ""
    )
    demo_b = {
        "tamper": ("f_g5_out0_name (material_graph[5].material_outputs[0]."
                   "name) excerpt shortened; the recomputed retained-object "
                   "record's naming span/digest changes"),
        "old_graph6_output_dag_verify_tampered_facts": old_issue_b,
        "old_graph6_input_dag_verify_tampered_facts": old_in_issue_b,
        "rebuild_issue": issue_b,
        "source_relation_node_id_changed": bool(
            rebuilt_b is not None
            and relation_ids_old.isdisjoint(relation_ids_new)),
        "root_id_changed": (rebuilt_b is not None
                            and rebuilt_b["root_id"] != old_out["root_id"]),
        "rebuilt_dag_verifies_tampered_facts": (
            rebuilt_b is not None and verify_state_proof_dag(
                rebuilt_b, graph, tampered_b, span_of=span_of,
                **scope) == ""),
        "downstream": dependents_of(old_out, G5_OUT_NAME),
        "status": "PASS" if b_ok else "FAIL",
    }

    # (c) rule bytes/version: the pinned context digest no longer matches a
    # verifier constructed under forged rule bytes; every layer is invalid.
    rules, digest = _rule_resource()
    forged = (rules, "sha256_" + "0" * 64)
    with mock.patch.object(
            route_convention_basis, "_rule_resource", return_value=forged):
        issue_c_out = verify_state_proof_dag(
            old_out, graph, facts, span_of=span_of, **scope)
        issue_c_in = verify_state_proof_dag(
            old_in, graph, facts, span_of=span_of, **scope)
        issue_c_g5 = verify_state_proof_dag(
            dags["graph5_output"], graph, facts, span_of=span_of, **scope)
    c_ok = (
        digest != forged[1]
        and issue_c_out == issue_c_in == issue_c_g5
            == "proof_dag_context_mismatch"
        and verify_state_proof_dag(
            old_out, graph, facts, span_of=span_of, **scope) == ""
    )
    demo_c = {
        "tamper": ("route_convention_basis._rule_resource mocked to return "
                   "the real rules with a forged resource digest "
                   "(sha256_00..00)"),
        "graph5_output_dag_verify_forged_rules": issue_c_g5,
        "graph6_input_dag_verify_forged_rules": issue_c_in,
        "graph6_output_dag_verify_forged_rules": issue_c_out,
        "dag_verifies_real_rules_after_mock": (
            verify_state_proof_dag(
                old_out, graph, facts, span_of=span_of, **scope) == ""),
        "downstream_note": (
            "the rule-resource digest is pinned into the DAG context at "
            "build time; a verifier under different rule bytes rejects the "
            "whole DAG (context mismatch) before any node is trusted, so "
            "the inheritance node and both state-change nodes fail with "
            "the root"),
        "status": "PASS" if c_ok else "FAIL",
    }

    ok = a_ok and b_ok and c_ok
    return {
        "status": "PASS" if ok else "FAIL",
        "operation_excerpt_tamper": demo_a,
        "naming_excerpt_tamper": demo_b,
        "rule_bytes_tamper": demo_c,
    }


def _accept_6(dags, graph, facts, scope, span_of):
    """Item 6: forged upstream node / tampered premise id fail closed."""
    dag = dags["graph6_output"]

    forged_dag = deepcopy(dag)
    donor = next(node for node in forged_dag["nodes"].values()
                 if node["node_type"] == "paper_literal")
    forged = deepcopy(donor)
    forged["claim"]["material_instance_id"] = "inst_zzz"
    forged["leaf"]["claim_value"] = "powder"
    # Valid shape, invented content, stale content address.
    forged_dag["nodes"][forged["node_id"]] = forged
    forged_issue = verify_state_proof_dag(
        forged_dag, graph, facts, span_of=span_of, **scope)

    tampered = deepcopy(dag)
    root_node = tampered["nodes"][tampered["root_id"]]
    root_node["premises"][0]["node_id"] = "proof_node_" + "f" * 24
    premise_issue = verify_state_proof_dag(
        tampered, graph, facts, span_of=span_of, **scope)

    ok = (forged_issue == "proof_dag_node_id_mismatch"
          and premise_issue == "proof_dag_premise_missing"
          and verify_state_proof_dag(dag, graph, facts, span_of=span_of,
                                     **scope) == "")
    return {
        "status": "PASS" if ok else "FAIL",
        "forged_upstream_node": {
            "forgery": ("hand-made paper_literal node with invented content "
                        "(material_instance_id inst_zzz, claim_value "
                        "powder) inserted under a stale content address"),
            "verify_issue": forged_issue,
            "expected": "proof_dag_node_id_mismatch",
        },
        "tampered_premise_id": {
            "forgery": ("graph[6].out root's first premise id replaced with "
                        "proof_node_ffff..ff"),
            "verify_issue": premise_issue,
            "expected": "proof_dag_premise_missing",
        },
        "untouched_dag_still_verifies": (
            verify_state_proof_dag(dag, graph, facts, span_of=span_of,
                                   **scope) == ""),
    }


def _accept_7(dags, graph, facts, scope, span_of):
    """Item 7: premise cycles and future references fail closed."""
    dag = dags["graph5_output"]
    cycled = {
        "schema_version": dag["schema_version"],
        "context": dag["context"],
        "root_id": "proof_node_" + "a" * 24,
        "nodes": {
            "proof_node_" + "a" * 24: {
                "schema_version": "paper-literal-proof/v1",
                "node_type": "paper_literal",
                "node_id": "proof_node_" + "a" * 24,
                "claim": {"field_path": "", "target_state": "",
                          "material_id": "", "material_instance_id": ""},
                "premises": [{"role": "parent_state",
                              "node_id": "proof_node_" + "b" * 24}],
                "rule_id": "", "rule_version": "",
                "rule_resource_digest": "",
            },
            "proof_node_" + "b" * 24: {
                "schema_version": "paper-literal-proof/v1",
                "node_type": "paper_literal",
                "node_id": "proof_node_" + "b" * 24,
                "claim": {"field_path": "", "target_state": "",
                          "material_id": "", "material_instance_id": ""},
                "premises": [{"role": "parent_state",
                              "node_id": "proof_node_" + "a" * 24}],
                "rule_id": "", "rule_version": "",
                "rule_resource_digest": "",
            },
        },
    }
    cycle_issue = verify_state_proof_dag(
        cycled, graph, facts, span_of=span_of, **scope)

    # An inheritance edge may only reach backwards: point graph[6]'s input
    # at a synthetic LATER step's matching output on a copy of the real
    # graph.
    future_graph = deepcopy(graph)
    future_graph.append({
        "macro_step_id": "ms_late", "macro_action_id": "A1", "sequence": 11,
        "operation": "held", "sample_id": "sample-r10-probe",
        "provenance": {"kind": "paper", "reference": "fact:f_g6_op"},
        "material_inputs": [{
            "material_id": "product",
            "material_instance_id": "inst_ldh_late_in",
            "name": "LDH seeds", "state": "retained_wet_solid",
            "material_origin": "external_inventory",
            "provenance": {"kind": "paper", "reference": "fact:f_g6_op"},
        }],
        "material_outputs": [{
            "material_id": "product",
            "material_instance_id": "inst_ldh_late",
            "name": "LDH seeds", "state": "retained_wet_solid",
            "provenance": {"kind": "paper", "reference": "fact:f_g6_op"},
        }],
        "operation_segments": [],
        "material_relations": [],
    })
    future_graph[6]["material_inputs"][0]["parent_output_refs"] = [
        {"macro_step_id": "ms_late", "material_instance_id": "inst_ldh_late"}]
    future_dag, future_issue = build_state_proof_dag(
        future_graph, facts, G6_IN_STATE, span_of=span_of, **scope)

    ok = (cycle_issue == "proof_dag_cycle" and future_dag is None
          and future_issue == "proof_dag_future_reference")
    return {
        "status": "PASS" if ok else "FAIL",
        "cycle": {
            "construction": ("hand-built 2-node mutual-premise DAG under "
                             "the real graph[5] context"),
            "verify_issue": cycle_issue,
            "expected": "proof_dag_cycle",
        },
        "future_reference": {
            "construction": ("copy of the real graph with graph[6] input[0] "
                             "parent_output_refs pointing at a synthetic "
                             "LATER step ms_late (index 10) whose output "
                             "port matches the reference"),
            "build_dag": future_dag,
            "build_issue": future_issue,
            "expected": "proof_dag_future_reference",
        },
    }


def _accept_8(dags, graph, facts, scope, blocks, captions):
    """Item 8: every leaf re-locates from the signed blocks alone."""
    verifier = StateProofDagVerifier(
        graph, facts, blocks=blocks, caption_block_locators=captions, **scope)
    per_dag: dict[str, dict] = {}
    leaves_ok = True
    for name, dag in dags.items():
        issue = verifier.verify(dag)
        leaves = [
            {"node_id": node["node_id"],
             "field_path": node["leaf"]["field_path"],
             "locator": node["leaf"]["locator"],
             "char_span": node["leaf"]["char_span"]}
            for node in dag["nodes"].values()
            if node["node_type"] == "paper_literal"
        ]
        located = all(leaf["locator"] and len(leaf["char_span"]) == 2
                      for leaf in leaves)
        leaves_ok = leaves_ok and bool(leaves) and located
        per_dag[name] = {
            "verify_blocks_only": issue,
            "leaf_count": len(leaves),
            "leaves": leaves,
        }

    # Tamper one leaf's char_span and reseal honestly: the content address
    # cascade is recomputed, yet source relocation still fails.
    dag = dags["graph6_output"]
    target = next(
        node_id for node_id, node in dag["nodes"].items()
        if node["node_type"] == "paper_literal"
        and node["leaf"]["field_path"] == G6_OPERATION)
    resealed = _reseal(
        dag, target, lambda node: node["leaf"].update(char_span=[0, 5]))
    tamper_issue = StateProofDagVerifier(
        graph, facts, blocks=blocks, caption_block_locators=captions,
        **scope).verify(resealed)
    unresealed = deepcopy(dag)
    unresealed["nodes"][target]["leaf"]["char_span"] = [0, 5]
    unresealed_issue = verifier.verify(unresealed)

    ok = (all(per_dag[name]["verify_blocks_only"] == "" for name in per_dag)
          and leaves_ok
          and tamper_issue == "proof_dag_leaf_relocation_mismatch"
          and unresealed_issue == "proof_dag_node_id_mismatch")
    return {
        "status": "PASS" if ok else "FAIL",
        "per_dag": per_dag,
        "tampered_char_span_resealed": {
            "target_leaf_field_path": G6_OPERATION,
            "forgery": ("leaf char_span replaced with [0, 5], then every "
                        "dependent content address honestly recomputed"),
            "verify_issue": tamper_issue,
            "expected": "proof_dag_leaf_relocation_mismatch",
        },
        "tampered_char_span_unresealed": {
            "verify_issue": unresealed_issue,
            "expected": "proof_dag_node_id_mismatch",
        },
        "note": ("the verifier was handed the signed blocks only (plus the "
                 "trust scope and leaf metadata); it rebuilt its own span "
                 "resolver and re-extracted every leaf from the projected "
                 "normalized source"),
    }


def _accept_9(dags, graph, facts, scope, resolver):
    """Item 9: the flat legacy path is untouched by the DAG layer."""
    proof, issue = derive_unreviewed_output_state(
        graph, facts, G5_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver)
    if proof is None:
        return {"status": "FAIL", "legacy_derive_issue": issue}
    evidence = convention_fact_evidence_by_id(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"])
    prepared = _resolve_fact_provenance(
        graph, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        operation_evidence_id=proof["operation_evidence_id"])
    verify_issue = verify_bound_output_state(
        proof, prepared, evidence,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver)
    legacy_schema_absent = all(
        "route-convention-state/v1"
        not in json.dumps(dag["nodes"], ensure_ascii=False)
        for dag in dags.values())
    ok = (issue == "" and verify_issue == ""
          and proof["schema_version"] == "route-convention-state/v1"
          and proof["rule_id"] == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
          and legacy_schema_absent)
    return {
        "status": "PASS" if ok else "FAIL",
        "legacy_flat_proof": {
            "field_path": G5_OUT_STATE,
            "derive_issue": issue,
            "schema_version": proof["schema_version"],
            "rule_id": proof["rule_id"],
            "rule_version": proof["rule_version"],
            "verify_bound_output_state_issue": verify_issue,
        },
        "legacy_schema_in_dag_nodes": (
            "absent from all DAGs"
            if legacy_schema_absent else "PRESENT (regression)"),
    }


# ---------------------------------------------------------------------------
# Round-3C acceptance items.
# ---------------------------------------------------------------------------


def _accept_g7in(dags, graph, facts, scope, span_of, blocks, captions):
    """3C-2: graph[7].in (ms7a.in) — multi-hop inheritance composes."""
    dag = dags["graph7_input"]
    root_node = dag["nodes"][dag["root_id"]]
    parent = dag["nodes"][root_node["premises"][0]["node_id"]]
    grand = dag["nodes"][next(
        premise["node_id"] for premise in parent["premises"]
        if premise["role"] == "parent_state")]
    great = dag["nodes"][next(
        premise["node_id"] for premise in grand["premises"]
        if premise["role"] == "parent_state")]
    v = _verify_both(dag, graph, facts, scope, span_of, blocks, captions)
    chain = _chain_summary(dag)
    evidence = convention_fact_evidence_by_id(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"])
    g6_state_evidence = next(
        ev_id for ev_id, fact in evidence.items()
        if fact.get("fact_id") == "f_g6_in0_state")
    g6_op_evidence = next(
        ev_id for ev_id, fact in evidence.items()
        if fact.get("fact_id") == "f_g6_op")
    g5_fact_evidence_ids = {
        ev_id for ev_id, fact in evidence.items()
        if str(fact.get("fact_id", "")).startswith("f_g5")}
    # The graph[7].in node's OWN typed fields reference only graph[6]
    # evidence; graph[5] paper facts enter only as leaves of the nested,
    # independently verified premise subgraph — the verifier recurses.
    own_evidence = {root_node.get("parent_evidence_id"),
                    root_node.get("operation_evidence_id")}
    no_direct_g5 = own_evidence == {g6_state_evidence, g6_op_evidence}
    ok = (
        root_node["node_type"] == "inheritance"
        and root_node["schema_version"] == "state-inheritance-proof/v1"
        and root_node["rule_id"] == "PARENT_OUTPUT_STATE_INHERITANCE_V1"
        and root_node["claim"]["field_path"] == G7_IN_STATE
        and root_node["claim"]["target_state"] == "suspension"
        and root_node.get("parent_ref") == {
            "kind": "material_instance", "macro_step_id": "ms6",
            "material_instance_id": "inst_ldh_aged"}
        and parent["node_type"] == "state_change"
        and parent["rule_id"] == "REDISPERSION_V1"
        and parent["claim"]["field_path"] == G6_OUT_STATE
        and grand["node_type"] == "inheritance"
        and grand["claim"]["field_path"] == G6_IN_STATE
        and great["node_type"] == "state_change"
        and great["rule_id"] == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
        and great["claim"]["field_path"] == G5_OUT_STATE
        and no_direct_g5
        and own_evidence.isdisjoint(g5_fact_evidence_ids)
        and v == {"verify_with_span_resolver": "", "verify_blocks_only": ""}
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "field_path": G7_IN_STATE,
        "root_node_type": root_node["node_type"],
        "root_schema": root_node["schema_version"],
        "rule_id": root_node["rule_id"],
        "parent_ref": root_node.get("parent_ref"),
        "node_count": len(dag["nodes"]),
        "multi_hop_chain": (
            "inheritance graph[7].in -> state_change REDISPERSION_V1 "
            "graph[6].out -> inheritance graph[6].in -> state_change "
            "CENTRIFUGE_COLLECT_PRECIPITATE_V1 graph[5].out -> paper "
            "literals + source_relation; TWO consecutive non-literal "
            "inheritance hops compose through the capability token "
            "forwarded by the recursion (the 3B-1 limitation is resolved)"),
        "no_direct_graph5_facts": {
            "root_parent_evidence_id": root_node.get("parent_evidence_id"),
            "root_operation_evidence_id": root_node.get(
                "operation_evidence_id"),
            "both_are_graph6_evidence": no_direct_g5,
            "note": ("graph[5] paper facts appear only as leaves inside "
                     "the nested verified premise subgraph (graph[6].out "
                     "-> graph[6].in -> graph[5].out); the graph[7].in "
                     "node's own re-derivation consults graph[6] evidence "
                     "plus the verified grandparent token only"),
        },
        "premise_chain_tree": chain,
        **v,
    }


def _accept_ms7a(graph, facts, scope, span_of, resolver, blocked):
    """3C-3: ms7a.out BLOCKED — retained-object evidence gap, attributed."""
    build = blocked["graph7_output"]
    dag, issue = build["dag"], build["build_issue"]

    # The flat engine without any token is gated earlier (literal parent);
    # the DAG build discharges the parent through the GENUINE graph[7].in
    # proof, so the build issue is the node's own residual gap.
    flat_proof, flat_issue = derive_unreviewed_output_state(
        graph, facts, G7_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver)

    # What the resolver honestly records for the second centrifugation.
    resolver_record, resolver_issue = resolver(G7_OUT_STATE)
    # The candidate naming the resolver finds is the FIRST protocol's own
    # retained-object naming: its span equals graph[5]'s naming span, and
    # it textually precedes the second protocol's operation mention.
    g5_record, g5_issue = derive_post_operation_retained_object(
        graph, facts, 5, span_of=span_of)
    by_path = {fact["field_path"]: fact for fact in facts}
    candidate_naming_excerpt = by_path[
        "material_graph[7].material_outputs[0].name"]["excerpt"]
    candidate_span = span_of(candidate_naming_excerpt)
    operation_span = span_of(
        by_path["material_graph[7].operation"]["excerpt"])
    naming_is_g5_naming = bool(
        g5_record is not None and candidate_span is not None
        and list(candidate_span) == list(g5_record["naming_span"]))
    naming_precedes = bool(
        candidate_span is not None and operation_span is not None
        and candidate_span[0] < operation_span[1])

    ok = (
        dag is None and issue == RETAINED_GAP
        and flat_proof is None
        and flat_issue == "convention_parent_state_unverified"
        and resolver_record is None and resolver_issue == RETAINED_GAP
        and naming_is_g5_naming and naming_precedes
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "node": NODE_LABELS["graph7_output"],
        "field_path": G7_OUT_STATE,
        "verdict": "BLOCKED",
        "build_issue": issue,
        "attribution": "evidence_gap",
        "primary_attribution": "retained-object evidence gap",
        "reasoning": (
            "the paper affirms the second centrifugation−redispersion "
            "protocol happened but never says which phase the second "
            "centrifugation retained; the only 'LDH seeds' naming in the "
            "signed blocks is the FIRST protocol's retained-object naming "
            "(its span equals graph[5]'s naming span) and it textually "
            "precedes the second protocol's mention, so the resolver "
            "honestly records retained_object_mention_precedes_operation "
            "for ms7a — there is no retained-object mention FOR the "
            "second centrifugation at all"),
        "candidate_naming_span": list(candidate_span) if candidate_span else [],
        "graph5_naming_span": (list(g5_record["naming_span"])
                               if g5_record is not None else []),
        "candidate_naming_is_graph5_naming": naming_is_g5_naming,
        "second_protocol_operation_span": (
            list(operation_span) if operation_span else []),
        "resolver_issue": resolver_issue,
        "family_guard_note": (
            "with the compound representation the operation matches both "
            "the centrifugation and redispersion families; the guard "
            "surfaces the recorded source-relation blocker "
            f"({RETAINED_GAP}) and would fall back to "
            "convention_rule_not_applicable_or_ambiguous only if the "
            "resolver had recorded nothing — the retained-object evidence "
            "gap is the primary attribution, not 'rule not applicable'"),
        "error_code_note": (
            "the task-sanctioned refinement retained_object_for_operation_"
            "unverified was NOT shipped: the per-step resolver issues are "
            "recorded verbatim in the r7/r8 replay/audit JSONs and those "
            "runners must re-run byte-identical (hard constraint), so the "
            "resolver code stays untouched and the honest issue is "
            "reported with this reasoning"),
        "not_protocol_reference_inheritance": (
            "'which was the first ... protocol' + 'a second ... protocol' "
            "does NOT license copying the first protocol's retained "
            "semantics onto the second: protocol-reference inheritance is "
            "not a published proof class and no rule was added"),
        "not_operation_precondition_inference": (
            "'redispersion follows, so the solid must have been kept' is "
            "operation-precondition inference; no such proof type exists "
            "and none was added"),
        "flat_engine_without_token": {
            "proof": flat_proof,
            "issue": flat_issue,
            "note": ("the flat engine is gated earlier at the literal "
                     "parent gate; the DAG build discharges the parent "
                     "through the genuine graph[7].in inheritance proof, "
                     "so build_issue is the node's own residual gap"),
        },
    }


def _accept_ms7b(graph, facts, scope, span_of, resolver, blocked):
    """3C-4: ms7b.in cascade; ms7b.out dual result (blocker + diagnostics)."""
    in_build = blocked["graph8_input"]
    out_build = blocked["graph8_output"]
    ms7a_issue = blocked["graph7_output"]["build_issue"]

    # --- result 1: the proof blocker (shortest failure path) -------------
    proof_blocker = {
        "blocker": "parent_state_unverified",
        "build_issue": out_build["build_issue"],
        "caused_by": "ms7a.out (material_graph[7].material_outputs[0].state)",
        "root_gap": ms7a_issue,
        "path": ("ms7b.out parent_state -> ms7b.in (inheritance) -> "
                 "ms7a.out (BLOCKED: retained-object evidence gap)"),
    }

    # --- result 2: independent convention diagnostics (what-if) ----------
    # Diagnostic harness ONLY: a locally minted capability token discharges
    # the parent gate as if ms7b.in were satisfied; the results below are
    # diagnostics and never feed any proof or pass verdict.
    by_path = {fact["field_path"]: fact for fact in facts}
    operation_excerpt = by_path["material_graph[8].operation"]["excerpt"]
    step = graph[8]
    inputs = step.get("material_inputs") or []
    what_if_token = _mint_diagnostic_parent_state_assumption(
        field_path=G8_IN_STATE,
        state_value="retained_wet_solid",
        material_instance_id="inst_ldh_wet_2",
    )
    what_if_proof, what_if_issue = derive_unreviewed_output_state(
        graph, facts, G8_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver,
        verified_parent_state=what_if_token)

    # Rule-local probe scoped to the family the graph itself declares for
    # ms7b (the redispersion segment): which REDISPERSION_V1 premises fail?
    rules, _digest = _rule_resource()
    rule = rules["REDISPERSION_V1"]
    preconditions = rule["preconditions"]
    patterns = preconditions["operation_patterns"]
    intents = preconditions["intent_patterns"]
    operation_affirmed = any(
        _affirmative_pattern_match(pattern, operation_excerpt,
                                   reject_substrate=True)
        for pattern in patterns)
    intent_affirmed = any(
        _affirmative_pattern_match(pattern, operation_excerpt)
        for pattern in intents)
    liquid_premise = _rule_liquid_participation(rule)
    tokens = liquid_premise.get("excerpt_tokens") or _LIQUID_EXCERPT_TOKENS
    liquid_ok = _step_has_liquid_participation(
        inputs, operation_excerpt,
        liquid_premise.get("liquid_states", ["solution"]),
        tuple(tokens), operation_patterns=patterns)
    parent_state = step["material_inputs"][0]["state"]
    state_premise_ok = (
        parent_state in rule["allowed_input_states"]
        and parent_state in preconditions["input_states"]
        and step["material_outputs"][0]["state"] == rule["retained_output"])
    liquid_diagnostic = {
        "premise": "liquid_participation",
        "status": "PASS" if liquid_ok else "FAIL",
        "issue": "" if liquid_ok else "convention_liquid_participation_missing",
        "evidence": {
            "liquid_state_input_ports": [
                port.get("material_instance_id")
                for port in inputs
                if port.get("state") in
                tuple(liquid_premise.get("liquid_states", ["solution"]))],
            "operation_excerpt": operation_excerpt,
            "medium_phrase_in_excerpt": liquid_ok,
        },
        "reasoning": (
            "the second protocol's own evidence names no liquid medium "
            "and ms7b has no liquid-state input port; the FIRST protocol's "
            "water ('using deionized water three times') must NOT "
            "propagate to the 'second ... protocol' — no rule licenses "
            "cross-protocol liquid propagation"),
    }
    redispersion_probe = {
        "rule_id": "REDISPERSION_V1",
        "rule_version": rule["version"],
        "scoped_to_declared_segment": "ms7b-seg-redispersion",
        "premises": {
            "operation_affirmed": operation_affirmed,
            "intent_affirmed": intent_affirmed,
            "input_state_allowed": state_premise_ok,
            "liquid_participation": liquid_ok,
        },
        "only_failing_premise": (
            "liquid_participation" if (
                operation_affirmed and intent_affirmed
                and state_premise_ok and not liquid_ok) else ""),
    }

    diagnostics = {
        "diagnostics_only": True,
        "feeds_verdict": False,
        "harness": ("a local _VerifiedParentStateEvidence minted in the "
                    "runner discharges the parent gate as if ms7b.in were "
                    "satisfied; results below are this node's own gaps "
                    "under that assumption"),
        "engine_what_if": {
            "proof": what_if_proof,
            "issue": what_if_issue,
            "note": ("the full engine under the what-if parent token is "
                     "independently blocked by the compound-operation "
                     "guard (the second protocol has no retained-object "
                     "record either), surfacing the resolver's recorded "
                     "blocker; this is a SECOND independent gap of "
                     "ms7b.out, separate from the liquid premise below"),
        },
        "redispersion_rule_local_probe": redispersion_probe,
        "liquid_participation": liquid_diagnostic,
    }

    ok = (
        in_build["dag"] is None and in_build["build_issue"] == ms7a_issue
        and out_build["dag"] is None
        and out_build["build_issue"] == ms7a_issue
        and what_if_proof is None and what_if_issue == RETAINED_GAP
        and operation_affirmed and intent_affirmed and state_premise_ok
        and not liquid_ok
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "ms7b_in": {
            "node": NODE_LABELS["graph8_input"],
            "field_path": G8_IN_STATE,
            "verdict": "BLOCKED",
            "build_issue": in_build["build_issue"],
            "attribution": "dependency_cascade",
            "caused_by": "ms7a.out",
        },
        "ms7b_out": {
            "node": NODE_LABELS["graph8_output"],
            "field_path": G8_OUT_STATE,
            "proof status": "BLOCKED",
            "primary blocker": "parent_state_unverified",
            "caused_by": "ms7a.out",
            "proof_blocker": proof_blocker,
            "independent convention diagnostics": diagnostics,
        },
    }


def _accept_graph9(blocked):
    """graph[9].in / graph[9].out: pure dependency cascades."""
    ms7a_issue = blocked["graph7_output"]["build_issue"]
    rows = {}
    ok = True
    for name in ("graph9_input", "graph9_output"):
        build = blocked[name]
        row_ok = build["dag"] is None and build["build_issue"] == ms7a_issue
        ok = ok and row_ok
        rows[name] = {
            "node": NODE_LABELS[name],
            "field_path": build["field_path"],
            "verdict": "BLOCKED",
            "build_issue": build["build_issue"],
            "attribution": "dependency_cascade",
            "caused_by": "ms7a.out (via ms7b.out)",
        }
    return {"status": "PASS" if ok else "FAIL", **rows}


def _accept_contamination(dags, graph, facts, scope, span_of):
    """Real-chain three-hop contamination: mutate graph[5]'s retained-object
    naming evidence -> graph[5].out, graph[6].in, graph[6].out AND the new
    graph[7].in all fail; no stale node is resurrected."""
    tampered = deepcopy(facts)
    for fact in tampered:
        if fact.get("fact_id") == "f_g5_out0_name":
            fact["excerpt"] = "The precipitates were labeled as LDH seeds"

    per_node: dict[str, dict] = {}
    ok = True
    for name, path in DAG_TARGETS.items():
        old = dags[name]
        old_issue = verify_state_proof_dag(
            old, graph, tampered, span_of=span_of, **scope)
        rebuilt, build_issue = build_state_proof_dag(
            graph, tampered, path, span_of=span_of, **scope)
        old_relation_ids = {
            node_id for node_id, node in old["nodes"].items()
            if node["node_type"] == "source_relation"}
        new_relation_ids = ({
            node_id for node_id, node in rebuilt["nodes"].items()
            if node["node_type"] == "source_relation"}
            if rebuilt is not None else set())
        rebuilt_verifies = bool(
            rebuilt is not None
            and verify_state_proof_dag(
                rebuilt, graph, tampered, span_of=span_of, **scope) == "")
        old_still_verifies = (
            verify_state_proof_dag(
                old, graph, facts, span_of=span_of, **scope) == "")
        row_ok = (
            old_issue == "proof_dag_leaf_fact_mismatch"
            and rebuilt is not None
            and rebuilt["root_id"] != old["root_id"]
            and old_relation_ids.isdisjoint(new_relation_ids)
            and rebuilt_verifies and old_still_verifies
        )
        ok = ok and row_ok
        per_node[name] = {
            "node": NODE_LABELS[name],
            "field_path": path,
            "old_dag_verify_under_mutated_facts": old_issue,
            "rebuild_issue": build_issue,
            "root_id_changed": (rebuilt is not None
                                and rebuilt["root_id"] != old["root_id"]),
            "no_stale_source_relation_resurrected": (
                rebuilt is not None
                and old_relation_ids.isdisjoint(new_relation_ids)),
            "rebuilt_dag_verifies_mutated_facts": rebuilt_verifies,
            "old_dag_still_verifies_untouched_facts": old_still_verifies,
            "status": "PASS" if row_ok else "FAIL",
        }
    return {
        "status": "PASS" if ok else "FAIL",
        "tamper": ("f_g5_out0_name (material_graph[5].material_outputs[0]."
                   "name) excerpt shortened; the recomputed retained-object "
                   "record's naming span/digest changes, moving every "
                   "content address up the chain"),
        "hops_contaminated": ("graph[5].out -> graph[6].in -> graph[6].out "
                              "-> graph[7].in: three hops from the mutated "
                              "source leaf, including the new multi-hop "
                              "inheritance node"),
        "per_node": per_node,
    }


def _accept_binding(dags, graph, facts, scope, span_of):
    """3C-0 soundness on the real chain: probe-equivalent premise
    substitution plus the canonical-chain assertion."""

    def substitute_donor(dag, root_name, donor_predicate):
        donor_id = next(
            node_id for node_id, node in dag["nodes"].items()
            if donor_predicate(node))
        donor = dag["nodes"][donor_id]

        def substitute(node):
            for premise in node["premises"]:
                if premise["role"] == "parent_state":
                    premise["node_id"] = donor_id

        tampered = _reseal(dag, dag["root_id"], substitute)
        honest = node_id_for(
            tampered["nodes"][tampered["root_id"]]) == tampered["root_id"]
        verdict = verify_state_proof_dag(
            tampered, graph, facts, span_of=span_of, **scope)
        return {
            "target_root": root_name,
            "donor_node_type": donor["node_type"],
            "donor_rule_id": donor.get("rule_id", ""),
            "donor_claim_field_path": donor["claim"]["field_path"],
            "reseal_honest_content_addresses": honest,
            "verify_issue": verdict,
            "expected": BINDING_MISMATCH,
            "untouched_dag_still_verifies": (
                verify_state_proof_dag(
                    dag, graph, facts, span_of=span_of, **scope) == ""),
        }

    # (a) the probe-equivalent tamper on the graph[6].out root (the exact
    #     configuration the out-of-repo probe exercises on the fixture):
    #     an unrelated but VALID node of the same DAG (the source_relation
    #     node) in the parent_state role, honestly resealed.
    probe_a = substitute_donor(
        dags["graph6_output"], "graph[6].out",
        lambda node: node["node_type"] == "source_relation")
    # (b) the NEW graph[7].in root with the correct ancestor at the WRONG
    #     chain level: the graph[5].out state_change node verifies on its
    #     own inside this very DAG, but claims graph[5].out where the
    #     resolved upstream output port is graph[6].out — only the binding
    #     invariant rejects it.
    probe_b = substitute_donor(
        dags["graph7_input"], "graph[7].in",
        lambda node: (node["node_type"] == "state_change"
                      and node.get("rule_id")
                      == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"))

    # (c) canonical chain: every convention node's parent_state premise is
    #     the node for THIS step's own input state path — an inheritance
    #     node (graph[6].out, and graph[6].out inside the graph[7].in DAG)
    #     or a paper literal (graph[5].out) — never the upstream step's
    #     output node directly.
    g6out = dags["graph6_output"]
    g6out_root = g6out["nodes"][g6out["root_id"]]
    g6out_parent = g6out["nodes"][next(
        premise["node_id"] for premise in g6out_root["premises"]
        if premise["role"] == "parent_state")]
    g7in = dags["graph7_input"]
    g7in_root = g7in["nodes"][g7in["root_id"]]
    g7in_parent = g7in["nodes"][g7in_root["premises"][0]["node_id"]]
    nested_g6out_parent = g7in["nodes"][next(
        premise["node_id"] for premise in g7in_parent["premises"]
        if premise["role"] == "parent_state")]
    g5 = dags["graph5_output"]
    g5_root = g5["nodes"][g5["root_id"]]
    g5_parent = g5["nodes"][next(
        premise["node_id"] for premise in g5_root["premises"]
        if premise["role"] == "parent_state")]
    canonical = {
        "graph5_out_parent": {
            "node_type": g5_parent["node_type"],
            "claim_field_path": g5_parent["claim"]["field_path"],
            "is_this_steps_input_path": (
                g5_parent["claim"]["field_path"]
                == "material_graph[5].material_inputs[1].state"),
        },
        "graph6_out_parent": {
            "node_type": g6out_parent["node_type"],
            "rule_id": g6out_parent.get("rule_id", ""),
            "claim_field_path": g6out_parent["claim"]["field_path"],
            "is_this_steps_input_path": (
                g6out_parent["node_type"] == "inheritance"
                and g6out_parent["claim"]["field_path"] == G6_IN_STATE),
        },
        "graph7_in_parent": {
            "node_type": g7in_parent["node_type"],
            "rule_id": g7in_parent.get("rule_id", ""),
            "claim_field_path": g7in_parent["claim"]["field_path"],
            "is_resolved_upstream_output_port": (
                g7in_parent["node_type"] == "state_change"
                and g7in_parent["claim"]["field_path"] == G6_OUT_STATE),
        },
        "nested_graph6_out_parent": {
            "node_type": nested_g6out_parent["node_type"],
            "claim_field_path": nested_g6out_parent["claim"]["field_path"],
            "is_this_steps_input_path": (
                nested_g6out_parent["node_type"] == "inheritance"
                and nested_g6out_parent["claim"]["field_path"] == G6_IN_STATE),
        },
    }
    canonical_ok = all(
        row.get("is_this_steps_input_path",
                row.get("is_resolved_upstream_output_port"))
        for row in canonical.values())
    probes_ok = all(
        probe["verify_issue"] == BINDING_MISMATCH
        and probe["reseal_honest_content_addresses"]
        and probe["untouched_dag_still_verifies"]
        for probe in (probe_a, probe_b))
    return {
        "status": "PASS" if (probes_ok and canonical_ok) else "FAIL",
        "premise_substitution_probes": [probe_a, probe_b],
        "canonical_chain": canonical,
        "note": ("an otherwise-valid but unrelated node substituted into "
                 "the parent_state role fails "
                 f"{BINDING_MISMATCH} even with every content address "
                 "honestly resealed; the parent premise is always the "
                 "canonical chain node for the step's own input state "
                 "path (graph[6].out via graph[6].in)"),
    }


def _final_state_table(dags, blocked, graph, facts, scope, span_of, blocks,
                       captions):
    """The 9-node classification the round must emit, built from the actual
    build/verify results and checked against the expected table."""
    rows: list[dict] = []
    pass_evidence = {
        name: _verify_both(dag, graph, facts, scope, span_of, blocks,
                           captions)
        for name, dag in dags.items()
    }
    table_spec = [
        ("graph5_output", "PASS", "proven", ""),
        ("graph6_input", "PASS", "proven", ""),
        ("graph6_output", "PASS", "proven", ""),
        ("graph7_input", "PASS", "proven", ""),
        ("graph7_output", "BLOCKED", "evidence_gap", RETAINED_GAP),
        ("graph8_input", "BLOCKED", "dependency_cascade", RETAINED_GAP),
        ("graph8_output", "BLOCKED", "dependency_cascade", RETAINED_GAP),
        ("graph9_input", "BLOCKED", "dependency_cascade", RETAINED_GAP),
        ("graph9_output", "BLOCKED", "dependency_cascade", RETAINED_GAP),
    ]
    all_match = True
    for name, verdict, attribution, expected_issue in table_spec:
        if verdict == "PASS":
            evidence = pass_evidence[name]
            actual_ok = (evidence == {"verify_with_span_resolver": "",
                                      "verify_blocks_only": ""})
            issue = ""
        else:
            evidence = {}
            actual_ok = (blocked[name]["dag"] is None
                         and blocked[name]["build_issue"] == expected_issue)
            issue = blocked[name]["build_issue"]
        all_match = all_match and actual_ok
        rows.append({
            "node": NODE_LABELS[name],
            "field_path": (DAG_TARGETS.get(name)
                           or BLOCKED_TARGETS.get(name)),
            "verdict": verdict,
            "issue": issue,
            "attribution": attribution,
            "actual_matches_expected": actual_ok,
            **evidence,
        })
    ms7a_issue = blocked["graph7_output"]["build_issue"]
    split_ok = (
        rows[4]["attribution"] == "evidence_gap"
        and ms7a_issue == RETAINED_GAP
        and all(rows[index]["issue"] == ms7a_issue
                for index in (5, 6, 7, 8)))
    return {
        "status": "PASS" if (all_match and split_ok) else "FAIL",
        "nodes": rows,
        "blocked_split": {
            "genuine_evidence_gaps": [
                {"node": NODE_LABELS["graph7_output"],
                 "gap": "retained-object evidence gap (second "
                        "centrifugation retained phase)",
                 "issue": ms7a_issue},
                {"node": NODE_LABELS["graph8_output"],
                 "gap": "liquid participation (independent what-if "
                        "diagnostic — never feeds the verdict)",
                 "issue": "convention_liquid_participation_missing"},
            ],
            "dependency_cascades": [
                {"node": NODE_LABELS["graph8_input"],
                 "caused_by": "ms7a.out"},
                {"node": NODE_LABELS["graph9_input"],
                 "caused_by": "ms7a.out (via ms7b.out)"},
                {"node": NODE_LABELS["graph9_output"],
                 "caused_by": "ms7a.out (via ms7b.out)"},
            ],
        },
    }


# ---------------------------------------------------------------------------
# Round-3D acceptance: protocol-reference proof on the real chain.
# ---------------------------------------------------------------------------

DEFINITION_FACT_ID = "f_g5_op"
REFERENCE_FACT_IDS = {7: "f_g7a_op", 8: "f_g7b_op"}
DEFINITION_QUOTING_FACT_IDS = (
    "f_rs_op5", "f_g5_op", "f_g5_p0", "f_g5_in0_name", "f_g5_out0_state")
# A different REAL span of the same signed sentence (extended leftwards to
# start at "followed") — used by the definition-quote tamper.
DEFINITION_QUOTE_EXTENDED = (
    "followed by a centrifugation−redispersion protocol using deionized "
    "water three times, which was the first centrifugation−redispersion "
    "protocol")
REFERENCE_QUOTE_EXTENDED = (
    "Finally, after a second centrifugation−redispersion protocol one time"
    ", all the samples are collected for further characterization.")


class _RecordingDagBuilder(_DagBuilder):
    """A builder that records every automatic protocol-reference attempt."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.protocol_reference_attempts: list[int] = []

    def _build_protocol_reference(self, step_index):
        self.protocol_reference_attempts.append(step_index)
        return super()._build_protocol_reference(step_index)


def _build_protocol_reference_dag(graph, facts, scope, span_of, step_index):
    """Build the standalone protocol_reference DAG for one step's operation.

    The compound reference steps never reach the liquid gate inside
    ``_build_output_state`` (the 3C compound-operation guard fires first —
    the documented v1 boundary), so the node is built directly here; the
    DAG envelope pins exactly the context the verifier recomputes.
    """
    builder = _DagBuilder(graph, facts, span_of=span_of, **scope)
    node_id, issue = builder._build_protocol_reference(step_index)
    if node_id is None:
        return None, issue
    verifier = StateProofDagVerifier(graph, facts, span_of=span_of, **scope)
    dag = {
        "schema_version": STATE_PROOF_DAG_SCHEMA,
        "nodes": builder.nodes,
        "root_id": node_id,
        "context": verifier.context,
    }
    return dag, ""


def _resolution_record(node: dict, dag: dict, evidence_to_fact: dict) -> dict:
    """The traceable resolution record carried by a verified node."""
    definition_leaf = dag["nodes"][next(
        premise["node_id"] for premise in node["premises"]
        if premise["role"] == "definition_evidence")]["leaf"]
    reference_leaf = dag["nodes"][next(
        premise["node_id"] for premise in node["premises"]
        if premise["role"] == "reference_evidence")]["leaf"]
    return {
        "schema_version": node["schema_version"],
        "rule_id": node["rule_id"],
        "rule_version": node["rule_version"],
        "protocol_name": node["protocol_name"],
        "operation_sequence": node["operation_sequence"],
        "liquid_medium": node["liquid_medium"],
        "definition_evidence_id": node["definition_evidence_id"],
        "definition_fact_id": evidence_to_fact.get(
            node["definition_evidence_id"], ""),
        "reference_evidence_id": node["reference_evidence_id"],
        "reference_fact_id": evidence_to_fact.get(
            node["reference_evidence_id"], ""),
        "definition_ordinal_anchor": node["definition_ordinal_anchor"],
        "reference_ordinal": node["reference_ordinal"],
        # Invocation-local annotations, never definition slots.
        "definition_execution_count": node["definition_execution_count"],
        "reference_execution_count": node["reference_execution_count"],
        "definition_digest": node["definition_digest"],
        "definition_locator": definition_leaf.get("locator", ""),
        "definition_char_span": definition_leaf.get("char_span", []),
        "reference_locator": reference_leaf.get("locator", ""),
        "reference_char_span": reference_leaf.get("char_span", []),
        "paper_id": node["paper_id"],
        "experimental_group_id": node["experimental_group_id"],
        "source_digest": node["source_digest"],
    }


def _accept_protocol_reference(graph, facts, scope, span_of, blocks,
                               captions):
    """3D-1/3D-2/3D-4: build + verify both nodes; traceable records; the
    locked slot boundary (operation sequence + liquid medium, no execution
    slot anywhere on the definition)."""
    evidence = convention_fact_evidence_by_id(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"])
    evidence_to_fact = {ev_id: fact.get("fact_id", "")
                        for ev_id, fact in evidence.items()}
    by_id = {fact["fact_id"]: fact for fact in facts}
    definition_fact = by_id[DEFINITION_FACT_ID]
    definition_located = span_of.locate(definition_fact["excerpt"])
    definition_record, definition_issue = extract_protocol_definition(
        definition_fact["excerpt"],
        evidence_id=next(ev_id for ev_id, fact in evidence.items()
                         if fact.get("fact_id") == DEFINITION_FACT_ID),
        locator=definition_located[1],
        char_span=[definition_located[2][0], definition_located[2][1]],
        **scope)
    definition_blob = json.dumps(definition_record, ensure_ascii=False)

    per_step: dict[str, dict] = {}
    all_ok = definition_record is not None and definition_issue == ""
    for step_index in (7, 8):
        operation_path = f"material_graph[{step_index}].operation"
        step = graph[step_index]
        dag, build_issue = _build_protocol_reference_dag(
            graph, facts, scope, span_of, step_index)
        step_ok = dag is not None and build_issue == ""
        row: dict = {"step_index": step_index,
                     "macro_step_id": step.get("macro_step_id", ""),
                     "operation_path": operation_path,
                     "build_issue": build_issue}
        if dag is not None:
            node = dag["nodes"][dag["root_id"]]
            v = _verify_both(dag, graph, facts, scope, span_of, blocks,
                             captions)
            record = _resolution_record(node, dag, evidence_to_fact)
            segment_identities = [
                segment["segment_id"].rsplit("-seg-", 1)[-1]
                for segment in step.get("operation_segments", [])]
            sequence_identities = [
                segment["segment_identity"]
                for segment in node["operation_sequence"]]
            g5_identities = [
                segment["segment_id"].rsplit("-seg-", 1)[-1]
                for segment in graph[5].get("operation_segments", [])]
            # 3D-2: the inherited operation sequence is basically no new
            # information — it matches the step's own declared segments
            # (and graph[5]'s) exactly.
            sequence_ok = (
                sequence_identities == segment_identities
                and sequence_identities == g5_identities
                and [segment["segment_order"]
                     for segment in node["operation_sequence"]]
                == list(range(1, len(sequence_identities) + 1)))
            node_ok = (
                node["node_type"] == "protocol_reference"
                and node["schema_version"] == PROTOCOL_REFERENCE_PROOF_SCHEMA
                and node["rule_id"] == PROTOCOL_REFERENCE_RULE_ID
                and node["rule_version"] == PROTOCOL_REFERENCE_RULE_VERSION
                # The claim binds the step's operation path with EMPTY
                # state/material ids — never a state or object proposition.
                and node["claim"] == {
                    "field_path": operation_path, "target_state": "",
                    "material_id": "", "material_instance_id": ""}
                and node["operation_path"] == operation_path
                and node["operation_value"] == step["operation"]
                and {premise["role"] for premise in node["premises"]}
                == {"definition_evidence", "reference_evidence"}
                and node["liquid_medium"] == "deionized water"
                and node["protocol_name"]
                == "centrifugation-redispersion protocol"
                and node["definition_ordinal_anchor"] == "first"
                and node["reference_ordinal"] == "second"
                and node["definition_execution_count"] == "three times"
                and node["reference_execution_count"] == "one time"
                and node["definition_digest"]
                == protocol_definition_digest(definition_record)
                and record["definition_fact_id"] == DEFINITION_FACT_ID
                and record["reference_fact_id"] == REFERENCE_FACT_IDS[step_index]
                and record["definition_locator"]
                and len(record["definition_char_span"]) == 2
                and record["reference_locator"]
                and len(record["reference_char_span"]) == 2
                and v == {"verify_with_span_resolver": "",
                          "verify_blocks_only": ""}
                and sequence_ok
            )
            step_ok = step_ok and node_ok
            row.update({
                "node_id": dag["root_id"],
                "node_count": len(dag["nodes"]),
                "resolution_record": record,
                "operation_sequence_matches_step_segments": sequence_ok,
                "step_segment_identities": segment_identities,
                **v,
            })
        row["status"] = "PASS" if step_ok else "FAIL"
        per_step[f"graph{step_index}_operation"] = row
        all_ok = all_ok and step_ok

    # 3D-3: the locked slot boundary — the definition carries NO execution
    # slot; "three times"/"one time" exist only as invocation-local
    # annotations on the resolution records.
    slot_boundary_ok = (
        definition_record is not None
        and validate_protocol_definition(definition_record) == ""
        and "execution" not in definition_blob
        and "three times" not in definition_blob
        and set(definition_record) == {
            "schema_version", "protocol_name", "operation_sequence",
            "liquid_medium", "definition_evidence_id",
            "definition_ordinal_anchor", "paper_id",
            "experimental_group_id", "source_digest", "locator",
            "char_span"}
        and definition_record["schema_version"] == PROTOCOL_DEFINITION_SCHEMA
        and all(
            row.get("resolution_record", {}).get("definition_execution_count")
            == "three times"
            and row.get("resolution_record", {}).get(
                "reference_execution_count") == "one time"
            for row in per_step.values())
    )
    slot_boundary = {
        "status": "PASS" if slot_boundary_ok else "FAIL",
        "second_invocation_repeat_count": {
            "value": 1,
            "surface": "one time",
            "scope": "invocation-local annotation on the resolution record "
                     "(reference_execution_count); LOCAL to the second "
                     "invocation — it does NOT override, extend, or relax "
                     "any definition slot",
            "definition_carries_execution_slot": False,
        },
        "definition_record_keys": sorted(definition_record or {}),
        "definition_record_json_contains_execution": (
            "execution" in definition_blob),
        "definition_record_json_contains_three_times": (
            "three times" in definition_blob),
        "closed_slot_validation": validate_protocol_definition(
            definition_record) if definition_record else "no record",
        "note": ("the owner's ruling: the definition's slots are the "
                 "operation sequence and the liquid medium; 'three times' "
                 "is the DEFINITION invocation's own local annotation and "
                 "'one time' is the second invocation's — neither is a "
                 "definition slot, and no slot may carry execution counts, "
                 "retained objects, output states, or material identities"),
    }
    all_ok = all_ok and slot_boundary_ok

    return {
        "status": "PASS" if all_ok else "FAIL",
        "locked_table_rows": {
            "ProtocolReferenceProof": "PASS (traceable resolution record)",
            "operation sequence": ("PASS (basically no new info — matches "
                                   "graph[7]/[8] segments)"),
            "second invocation repeat count": ("1, LOCAL override "
                                               "(annotation only)"),
            "liquid medium": "PASS: inherited deionized water",
        },
        "definition_extraction_issue": definition_issue,
        "per_step": per_step,
        "slot_boundary": slot_boundary,
    }


def _accept_carried_forward_table(state_table):
    """3D: all nine nodes keep their r10 verdicts, byte-compared against
    the committed r10 replay JSON."""
    r10 = json.loads(R10_REPLAY_PATH.read_text(encoding="utf-8"))
    r10_rows = r10["round3c_acceptance"]["final_state_table"]["nodes"]
    r11_rows = state_table["nodes"]
    comparisons = []
    ok = state_table["status"] == "PASS" and len(r10_rows) == len(r11_rows)
    for old, new in zip(r10_rows, r11_rows):
        same = all(old[key] == new[key] for key in (
            "node", "field_path", "verdict", "issue", "attribution"))
        ok = ok and same
        comparisons.append({
            "node": new["node"], "verdict": new["verdict"],
            "issue": new["issue"], "attribution": new["attribution"],
            "matches_committed_r10_row": same,
        })
    return {
        "status": "PASS" if ok else "FAIL",
        "comparison_basis": ("local-revision-r10-replay.json "
                             "round3c_acceptance.final_state_table.nodes "
                             "(committed, historical 3C record)"),
        "nodes": comparisons,
        "note": ("the protocol-reference machinery flipped nothing: the "
                 "four proven nodes still verify, ms7a.out stays the "
                 "retained-object evidence gap, and the four cascades "
                 "stay cascades; the ONLY change vs r10 is the ms7b "
                 "independent liquid diagnostic (FAIL -> PASS via the "
                 "verified protocol_reference node), which is a "
                 "diagnostics_only row and never fed any verdict"),
    }


def _accept_ms7a_no_leak(graph, facts, scope, span_of, resolver, blocked):
    """3D: ms7a is untouched — no protocol_reference node or liquid token
    participates in its derivation, and its issues are byte-identical to
    the committed r10 replay."""
    # (a) The builder never auto-engages the protocol-reference path
    #     anywhere on this chain: run every target through a recording
    #     builder (the compound reference steps hit the 3C
    #     compound-operation guard before the liquid gate).
    attempts: dict[str, list[int]] = {}
    for name, path in {**DAG_TARGETS, **BLOCKED_TARGETS}.items():
        builder = _RecordingDagBuilder(graph, facts, span_of=span_of,
                                       **scope)
        builder.build(path)
        attempts[name] = list(builder.protocol_reference_attempts)
    never_engaged = all(not calls for calls in attempts.values())

    # (b) The resolver issue and the flat derive issue for ms7a.out are
    #     byte-identical to r10's committed replay.
    r10 = json.loads(R10_REPLAY_PATH.read_text(encoding="utf-8"))
    r10_ms7a = r10["round3c_acceptance"][
        "ms7a_output_retained_object_gap_3c3"]
    resolver_record, resolver_issue = resolver(G7_OUT_STATE)
    flat_proof, flat_issue = derive_unreviewed_output_state(
        graph, facts, G7_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver)
    issues_byte_identical = (
        resolver_issue == r10_ms7a["resolver_issue"]
        and flat_issue == r10_ms7a["flat_engine_without_token"]["issue"]
        and blocked["graph7_output"]["build_issue"]
        == r10_ms7a["build_issue"])

    # (c) Even a correctly-bound liquid token for step 7's operation
    #     discharges NOTHING in ms7a's derivation: the token never touches
    #     retained-object premises or the compound-operation guard.
    dag7, build7_issue = _build_protocol_reference_dag(
        graph, facts, scope, span_of, 7)
    token7 = None
    if dag7 is not None:
        node7 = dag7["nodes"][dag7["root_id"]]
        token7 = _mint_diagnostic_liquid_medium_assumption(
            operation_value=node7["operation_value"],
            medium=node7["liquid_medium"],
            definition_digest=node7["definition_digest"])
    token_proof, token_issue = derive_unreviewed_output_state(
        graph, facts, G7_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver,
        verified_liquid_medium=token7)
    token_changes_nothing = (
        token_proof is None and token_issue == flat_issue)

    ok = (never_engaged and issues_byte_identical
          and resolver_record is None and resolver_issue == RETAINED_GAP
          and flat_proof is None
          and blocked["graph7_output"]["dag"] is None
          and token_changes_nothing)
    return {
        "status": "PASS" if ok else "FAIL",
        "locked_table_rows": {
            "retained object of ms7a": ("BLOCKED (unchanged; "
                                        "protocol-reference contributes "
                                        "NOTHING here)"),
            "ms7a.out": "BLOCKED (unchanged)",
        },
        "builder_auto_engagement": {
            "attempts_per_target": attempts,
            "never_engaged_anywhere_on_chain": never_engaged,
            "note": ("_build_output_state attempts a protocol_reference "
                     "node only when the flat derive returns exactly "
                     "convention_liquid_participation_missing; on this "
                     "chain the compound reference steps hit the 3C "
                     "compound-operation guard (retained-object gap) "
                     "before the liquid gate, so the machinery never "
                     "engages automatically — every protocol_reference "
                     "node in this runner is built explicitly"),
        },
        "ms7a_issues_byte_identical_to_r10": {
            "resolver_issue": resolver_issue,
            "flat_derive_issue": flat_issue,
            "dag_build_issue": blocked["graph7_output"]["build_issue"],
            "byte_identical": issues_byte_identical,
        },
        "liquid_token_step7_no_discharge": {
            "token_bound_operation": (
                token7.operation_value if token7 is not None else ""),
            "flat_derive_with_token_issue": token_issue,
            "unchanged_vs_without_token": token_changes_nothing,
            "note": ("the _VerifiedLiquidMedium token is never wired into "
                     "retained-object or object-pattern premises; ms7a's "
                     "compound-operation guard and the resolver's "
                     "retained_object_mention_precedes_operation are "
                     "exactly as in r10"),
        },
    }


def _accept_ms7b_dual_3d(graph, facts, scope, span_of, resolver, blocked,
                         blocks, captions):
    """3D: the ms7b dual result, updated — liquid diagnostic PASS via the
    verified protocol_reference node; overall verdict still BLOCKED."""
    in_build = blocked["graph8_input"]
    out_build = blocked["graph8_output"]
    ms7a_issue = blocked["graph7_output"]["build_issue"]
    evidence = convention_fact_evidence_by_id(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"])
    definition_evidence_id = next(
        ev_id for ev_id, fact in evidence.items()
        if fact.get("fact_id") == DEFINITION_FACT_ID)

    # --- result 1: the proof blocker (unchanged) -------------------------
    proof_blocker = {
        "blocker": "parent_state_unverified",
        "build_issue": out_build["build_issue"],
        "caused_by": "ms7a.out (material_graph[7].material_outputs[0].state)",
        "root_gap": ms7a_issue,
        "path": ("ms7b.out parent_state -> ms7b.in (inheritance) -> "
                 "ms7a.out (BLOCKED: retained-object evidence gap)"),
    }

    # --- result 2: independent convention diagnostics (what-if) ----------
    # Diagnostic harness ONLY: locally minted capability tokens discharge
    # the parent gate as if ms7b.in were satisfied and the liquid gate as
    # certified by the verified protocol_reference node; the results below
    # are diagnostics and never feed any proof or pass verdict.
    by_path = {fact["field_path"]: fact for fact in facts}
    operation_excerpt = by_path["material_graph[8].operation"]["excerpt"]
    step = graph[8]
    inputs = step.get("material_inputs") or []
    parent_token = _mint_diagnostic_parent_state_assumption(
        field_path=G8_IN_STATE,
        state_value="retained_wet_solid",
        material_instance_id="inst_ldh_wet_2",
    )
    dag8, build8_issue = _build_protocol_reference_dag(
        graph, facts, scope, span_of, 8)
    node8 = dag8["nodes"][dag8["root_id"]] if dag8 is not None else None
    liquid_token = (
        _mint_diagnostic_liquid_medium_assumption(
            operation_value=node8["operation_value"],
            medium=node8["liquid_medium"],
            definition_digest=node8["definition_digest"])
        if node8 is not None else None)
    verify8 = (
        _verify_both(dag8, graph, facts, scope, span_of, blocks, captions)
        if dag8 is not None else {})

    rules, _digest = _rule_resource()
    rule = rules["REDISPERSION_V1"]
    preconditions = rule["preconditions"]
    patterns = preconditions["operation_patterns"]
    intents = preconditions["intent_patterns"]
    operation_affirmed = any(
        _affirmative_pattern_match(pattern, operation_excerpt,
                                   reject_substrate=True)
        for pattern in patterns)
    intent_affirmed = any(
        _affirmative_pattern_match(pattern, operation_excerpt)
        for pattern in intents)
    liquid_premise = _rule_liquid_participation(rule)
    tokens = liquid_premise.get("excerpt_tokens") or _LIQUID_EXCERPT_TOKENS
    literal_liquid_ok = _step_has_liquid_participation(
        inputs, operation_excerpt,
        liquid_premise.get("liquid_states", ["solution"]),
        tuple(tokens), operation_patterns=patterns)
    parent_state = step["material_inputs"][0]["state"]
    state_premise_ok = (
        parent_state in rule["allowed_input_states"]
        and parent_state in preconditions["input_states"]
        and step["material_outputs"][0]["state"] == rule["retained_output"])
    # The token discharges the per-premise liquid diagnostic exactly the
    # way the flat gate does: same operation binding, non-empty medium and
    # definition digest.
    token_discharges = (
        liquid_token is not None
        and liquid_token.operation_value == step["operation"]
        and bool(liquid_token.medium)
        and bool(liquid_token.definition_digest))
    liquid_ok = literal_liquid_ok or token_discharges

    what_if_parent_only = derive_unreviewed_output_state(
        graph, facts, G8_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver,
        verified_parent_state=parent_token)
    what_if_parent_liquid = derive_unreviewed_output_state(
        graph, facts, G8_OUT_STATE,
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"],
        retained_object_resolver=resolver,
        verified_parent_state=parent_token,
        verified_liquid_medium=liquid_token)

    liquid_diagnostic = {
        "premise": "liquid_participation",
        "status": "PASS" if liquid_ok else "FAIL",
        "issue": "" if liquid_ok else "convention_liquid_participation_missing",
        "source": "inherited protocol slot",
        "medium": node8["liquid_medium"] if node8 is not None else "",
        "evidence": definition_evidence_id,
        "evidence_fact_id": DEFINITION_FACT_ID,
        "certifying_node": {
            "node_type": "protocol_reference",
            "node_id": dag8["root_id"] if dag8 is not None else "",
            "verify": verify8,
        },
        "literal_gate_still_fails": not literal_liquid_ok,
        "reasoning": (
            "the second protocol's own evidence still names no liquid "
            "medium and ms7b still has no liquid-state input port — the "
            "LITERAL check fails exactly as in r10; the premise is now "
            "discharged by the verified protocol_reference node for "
            "graph[8].operation: the definition ('centrifugation−"
            "redispersion protocol using deionized water three times, "
            "which was the first ...', evidence "
            f"{definition_evidence_id} = {DEFINITION_FACT_ID}) governs "
            "the 'second centrifugation−redispersion protocol' reference "
            "by name and ordinal inside the same signed group, and the "
            "inherited slot is medium=deionized water"),
    }
    redispersion_probe = {
        "rule_id": "REDISPERSION_V1",
        "rule_version": rule["version"],
        "scoped_to_declared_segment": "ms7b-seg-redispersion",
        "premises": {
            "operation_affirmed": operation_affirmed,
            "intent_affirmed": intent_affirmed,
            "input_state_allowed": state_premise_ok,
            "liquid_participation_literal": literal_liquid_ok,
            "liquid_participation_with_verified_token": liquid_ok,
        },
        "only_failing_premise": (
            "" if (operation_affirmed and intent_affirmed
                   and state_premise_ok and liquid_ok)
            else "see premise map"),
    }

    diagnostics = {
        "diagnostics_only": True,
        "feeds_verdict": False,
        "harness": ("local capability tokens minted in the runner: a "
                    "_VerifiedParentStateEvidence discharging the parent "
                    "gate as if ms7b.in were satisfied, and a "
                    "_VerifiedLiquidMedium minted from the VERIFIED "
                    "protocol_reference node for graph[8].operation "
                    "(verified under the pinned context before minting); "
                    "results below are this node's own gaps under those "
                    "assumptions"),
        "liquid_participation": liquid_diagnostic,
        "redispersion_rule_local_probe": redispersion_probe,
        "engine_what_if": {
            "proof": what_if_parent_liquid[0],
            "issue": what_if_parent_liquid[1],
            "verdict": "BLOCKED",
            "surviving_issue": what_if_parent_liquid[1],
            "parent_only_control_issue": what_if_parent_only[1],
            "note": ("even with the parent assumed AND the liquid medium "
                     "inherited, the full engine stays BLOCKED: the "
                     "compound reference operation matches both the "
                     "centrifugation and redispersion families, and the "
                     "second protocol has no post-operation retained-"
                     "object source record, so the compound-operation "
                     "guard surfaces the resolver's recorded blocker "
                     "retained_object_mention_precedes_operation — the "
                     "retained-object/composite-arbitration gap for the "
                     "second protocol, unchanged from r10; the liquid "
                     "token discharges the per-premise liquid diagnostic "
                     "ONLY, never the overall derive (the documented v1 "
                     "boundary: the guard fires before the liquid gate "
                     "for a compound operation)"),
        },
    }

    ok = (
        in_build["dag"] is None and in_build["build_issue"] == ms7a_issue
        and out_build["dag"] is None
        and out_build["build_issue"] == ms7a_issue
        and dag8 is not None and build8_issue == ""
        and verify8 == {"verify_with_span_resolver": "",
                        "verify_blocks_only": ""}
        and operation_affirmed and intent_affirmed and state_premise_ok
        and not literal_liquid_ok and liquid_ok
        and node8["definition_evidence_id"] == definition_evidence_id
        and what_if_parent_only[0] is None
        and what_if_parent_only[1] == RETAINED_GAP
        and what_if_parent_liquid[0] is None
        and what_if_parent_liquid[1] == RETAINED_GAP
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "locked_table_rows": {
            "ms7b.in": "BLOCKED, cascade (unchanged)",
            "ms7b liquid independent diagnostic": (
                "PASS (source = inherited protocol slot, medium = "
                "deionized water)"),
            "ms7b.out overall": "BLOCKED, still parent-state cascade",
            "graph[9].in/out": "BLOCKED cascade (unchanged)",
        },
        "ms7b_in": {
            "node": NODE_LABELS["graph8_input"],
            "field_path": G8_IN_STATE,
            "verdict": "BLOCKED",
            "build_issue": in_build["build_issue"],
            "attribution": "dependency_cascade",
            "caused_by": "ms7a.out",
        },
        "ms7b_out": {
            "node": NODE_LABELS["graph8_output"],
            "field_path": G8_OUT_STATE,
            "proof status": "BLOCKED",
            "primary blocker": "parent_state_unverified",
            "caused_by": "ms7a.out",
            "proof_blocker": proof_blocker,
            "independent convention diagnostics": diagnostics,
        },
        "diff_vs_r10": {
            "liquid_participation": ("FAIL convention_liquid_participation_"
                                     "missing -> PASS (inherited protocol "
                                     "slot, medium=deionized water, "
                                     f"evidence={DEFINITION_FACT_ID})"),
            "everything_else": ("unchanged — proof blocker, cascades, "
                                "engine what-if surviving issue, and all "
                                "nine verdicts are r10's"),
        },
    }


def _accept_counter_nearer_phrase(graph, facts, scope, span_of):
    """3D counter-example 1 (real signed data): 'dispersed in 30 mL of
    water and aged' is NEARER to the reference and is never a candidate."""
    by_id = {fact["fact_id"]: fact for fact in facts}
    evidence = convention_fact_evidence_by_id(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"])
    ev_by_fact = {fact.get("fact_id"): ev_id
                  for ev_id, fact in evidence.items()}
    near_fact = by_id["f_g6_op"]
    def_fact = by_id[DEFINITION_FACT_ID]
    ref_fact = by_id["f_g7b_op"]
    near_span = span_of(near_fact["excerpt"])
    def_span = span_of(def_fact["excerpt"])
    ref_span = span_of(ref_fact["excerpt"])

    record, issue = extract_protocol_definition(near_fact["excerpt"])
    not_a_definition = record is None and issue != ""

    candidates = _protocol_reference_candidates(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"], span_of=span_of)
    ref_located = span_of.locate(ref_fact["excerpt"])
    resolution, resolve_issue = resolve_protocol_reference(
        ref_fact["value"], ref_fact["excerpt"],
        reference_evidence_id=ev_by_fact["f_g7b_op"],
        reference_locator=ref_located[1],
        reference_position=ref_located[2][0],
        candidates=candidates, **scope)

    dag8, build_issue = _build_protocol_reference_dag(
        graph, facts, scope, span_of, 8)
    node8 = dag8["nodes"][dag8["root_id"]] if dag8 is not None else {}

    nearer = bool(near_span and def_span and ref_span
                  and def_span[0] < near_span[0] < ref_span[0])
    ok = (
        not_a_definition and issue == "protocol_definition_not_present"
        and nearer
        and resolution is not None and resolve_issue == ""
        and resolution["definition_evidence_id"] == ev_by_fact[DEFINITION_FACT_ID]
        and resolution["definition_evidence_id"] != ev_by_fact["f_g6_op"]
        and node8.get("definition_evidence_id")
        == ev_by_fact[DEFINITION_FACT_ID]
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "data_basis": ("REAL signed NiFe Control group — the phrase is "
                       "f_g6_op's excerpt (material_graph[6].operation), "
                       "located in the signed blocks"),
        "phrase_excerpt": near_fact["excerpt"],
        "phrase_fact_id": "f_g6_op",
        "sentence_spans": {
            "definition_f_g5_op": list(def_span) if def_span else [],
            "nearer_phrase_f_g6_op": list(near_span) if near_span else [],
            "reference_f_g7b_op": list(ref_span) if ref_span else [],
            "nearer_to_reference_than_definition": nearer,
        },
        "extraction_of_phrase": {"record": record, "issue": issue},
        "resolution_binds": {
            "definition_evidence_id": (
                resolution or {}).get("definition_evidence_id"),
            "definition_fact_id": DEFINITION_FACT_ID,
            "is_not_nearer_phrase_fact": (
                (resolution or {}).get("definition_evidence_id")
                != ev_by_fact["f_g6_op"]),
        },
        "dag_node_medium_evidence": {
            "definition_evidence_id": node8.get("definition_evidence_id"),
            "is_definition_fact_not_nearer_phrase": (
                node8.get("definition_evidence_id")
                == ev_by_fact[DEFINITION_FACT_ID]),
        },
        "note": ("candidates are ONLY mentions that extract as "
                 "definitions (governed 'using' clause + ordinal anchor); "
                 "proximity is never consulted — a nearer non-definition "
                 "medium phrase cannot out-compete the farther real "
                 "definition"),
    }


def _accept_counter_etching(graph, facts, scope, span_of, inventory):
    """3D counter-example 2 (real signed data): the paper's Etching group
    reference 'after the first centrifugation−redispersion protocol' must
    NOT bind the Control group's definition — fail closed."""
    matches = [g for g in inventory.groups
               if "Etching Method" in g.source_scope.experimental_group_id]
    if len(matches) != 1:
        return {"status": "FAIL",
                "note": (f"expected exactly one Etching group in the "
                         f"signed KB, got {len(matches)}")}
    etching = matches[0]
    etching_scope = {
        "paper_id": etching.source_scope.paper_id,
        "experimental_group_id": etching.source_scope.experimental_group_id,
        "source_digest": etching.source_scope.source_digest,
    }
    eblocks = [(block.locator, block.text) for block in etching.blocks]
    ecaptions = [block.locator for block in etching.blocks if block.caption]
    espan_of = build_excerpt_span_resolver(eblocks, ecaptions)
    # The real Etching-group reference phrase (signed block pdf:p2:b80).
    eref_excerpt = ("the first centrifugation−redispersion protocol, "
                    "each sample was mixed")
    elocated = espan_of.locate(eref_excerpt)
    # Does the Etching group contain ANY extractable definition of its own?
    etching_definitions = []
    for locator, text in eblocks:
        located = espan_of.locate(text)
        record, issue = extract_protocol_definition(
            text, evidence_id=f"etching_block:{locator}",
            locator=locator,
            char_span=[located[2][0], located[2][1]] if located else [],
            **etching_scope)
        if record is not None:
            etching_definitions.append(record)

    evidence = convention_fact_evidence_by_id(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"])
    control_def_evidence = next(
        ev_id for ev_id, fact in evidence.items()
        if fact.get("fact_id") == DEFINITION_FACT_ID)
    by_id = {fact["fact_id"]: fact for fact in facts}
    def_fact = by_id[DEFINITION_FACT_ID]
    def_located = span_of.locate(def_fact["excerpt"])
    foreign_definition_carrier = {
        "excerpt": def_fact["excerpt"],
        "evidence_id": control_def_evidence,
        "field_path": "material_graph[5].operation",
        "locator": def_located[1],
        "char_span": [def_located[2][0], def_located[2][1]],
        **scope,  # tagged with the CONTROL scope — foreign to Etching
    }
    etching_ref_carrier = {
        "excerpt": eref_excerpt,
        "evidence_id": "etching_block:pdf:p2:b80-p2:b80",
        "field_path": "(etching group signed block)",
        "locator": elocated[1],
        "char_span": [elocated[2][0], elocated[2][1]],
        **etching_scope,
    }
    # Forward isolation: resolve the Etching "first protocol" reference
    # inside the Etching scope, with the REAL Control definition visible
    # as a (foreign) candidate.
    forward_record, forward_issue = resolve_protocol_reference(
        "", eref_excerpt,
        reference_evidence_id="etching_block:pdf:p2:b80-p2:b80",
        reference_locator=elocated[1],
        reference_position=elocated[2][0],
        candidates=[etching_ref_carrier, foreign_definition_carrier],
        **etching_scope)

    # Reverse isolation: the Control reference never sees Etching
    # candidates — mix Etching carriers (tagged Etching scope) into the
    # Control candidate set; the resolution record must be byte-identical
    # to the Control-only resolution.
    control_candidates = _protocol_reference_candidates(
        facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"], span_of=span_of)
    ref_fact = by_id["f_g7b_op"]
    ref_located = span_of.locate(ref_fact["excerpt"])
    ref_evidence = next(
        ev_id for ev_id, fact in evidence.items()
        if fact.get("fact_id") == "f_g7b_op")
    resolve_kwargs = dict(
        reference_evidence_id=ref_evidence,
        reference_locator=ref_located[1],
        reference_position=ref_located[2][0],
        paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest=scope["source_digest"])
    control_only, control_only_issue = resolve_protocol_reference(
        ref_fact["value"], ref_fact["excerpt"],
        candidates=control_candidates, **resolve_kwargs)
    mixed_record, mixed_issue = resolve_protocol_reference(
        ref_fact["value"], ref_fact["excerpt"],
        candidates=(control_candidates
                    + [etching_ref_carrier, foreign_definition_carrier]),
        **resolve_kwargs)
    records_identical = (
        json.dumps(control_only, ensure_ascii=False, sort_keys=True)
        == json.dumps(mixed_record, ensure_ascii=False, sort_keys=True))

    forward_ok = (
        forward_record is None
        and forward_issue == "protocol_reference_scope_mismatch"
        and not etching_definitions)
    reverse_ok = (
        control_only is not None and control_only_issue == ""
        and mixed_record is not None and mixed_issue == ""
        and records_identical
        and control_only["definition_evidence_id"] == control_def_evidence)
    return {
        "status": "PASS" if (forward_ok and reverse_ok) else "FAIL",
        "data_basis": ("REAL signed KB groups (same trust-config "
                       "enumeration as the preflight): the Etching "
                       "experimental group IS present — "
                       f"{etching_scope['experimental_group_id']!r} with "
                       f"{len(eblocks)} signed blocks; no synthetic scope "
                       "was needed"),
        "etching_group": {
            **etching_scope,
            "block_count": len(eblocks),
            "contains_first_protocol_reference": elocated is not None,
            "reference_excerpt": eref_excerpt,
            "reference_locator": elocated[1],
            "own_extractable_definitions": etching_definitions,
        },
        "forward_isolation": {
            "resolution_scope": "Etching group",
            "foreign_candidate": ("the REAL Control-group definition "
                                  f"(evidence {control_def_evidence}), "
                                  "tagged with the Control scope"),
            "record": forward_record,
            "issue": forward_issue,
            "expected": "protocol_reference_scope_mismatch",
            "note": ("the Etching group carries no extractable definition "
                     "of its own (its 'second centrifugation−redispersion/"
                     "washing protocol' sentence has no governed 'using' "
                     "clause and no ordinal anchor), so the name-matched "
                     "Control definition fails closed on scope — the "
                     "Etching reference never inherits the Control "
                     "group's deionized water"),
        },
        "reverse_isolation": {
            "resolution_scope": "Control group",
            "mixed_in_candidates": ["etching reference block carrier",
                                    "etching-tagged carriers"],
            "control_only_issue": control_only_issue,
            "mixed_issue": mixed_issue,
            "resolution_record_byte_identical": records_identical,
            "definition_evidence_id": (
                control_only or {}).get("definition_evidence_id"),
        },
    }


def _accept_tamper_protocol_reference(graph, facts, scope, span_of):
    """3D tamper items for the new proof class on the real chain."""
    dag7, _ = _build_protocol_reference_dag(graph, facts, scope, span_of, 7)
    dag8, _ = _build_protocol_reference_dag(graph, facts, scope, span_of, 8)

    # (a1) mutate ONLY the definition evidence fact's quote (a different
    #      real span, still a complete definition): both old nodes fail;
    #      the rebuild RE-FAILS resolution because the four sibling facts
    #      still quote the sentence at its original span — two distinct
    #      mentions, fail-closed ambiguous.
    tampered_a1 = deepcopy(facts)
    for fact in tampered_a1:
        if fact.get("fact_id") == DEFINITION_FACT_ID:
            fact["excerpt"] = DEFINITION_QUOTE_EXTENDED
    old7_a1 = verify_state_proof_dag(dag7, graph, tampered_a1,
                                     span_of=span_of, **scope)
    old8_a1 = verify_state_proof_dag(dag8, graph, tampered_a1,
                                     span_of=span_of, **scope)
    rebuilt7_a1, issue7_a1 = _build_protocol_reference_dag(
        graph, tampered_a1, scope, span_of, 7)
    rebuilt8_a1, issue8_a1 = _build_protocol_reference_dag(
        graph, tampered_a1, scope, span_of, 8)
    a1_ok = (old7_a1 != "" and old8_a1 != ""
             and rebuilt7_a1 is None and rebuilt8_a1 is None
             and issue7_a1 == issue8_a1 == "protocol_reference_ambiguous")
    demo_a1 = {
        "tamper": ("f_g5_op (the definition evidence) excerpt moved to a "
                   "different real span of the same signed sentence; the "
                   "four sibling facts quoting that sentence are left "
                   "untouched"),
        "old_graph7_node_verify": old7_a1,
        "old_graph8_node_verify": old8_a1,
        "rebuild_graph7": {"dag": None, "issue": issue7_a1},
        "rebuild_graph8": {"dag": None, "issue": issue8_a1},
        "honest_rebuilt_behavior": (
            "resolution FAILS closed: the mutated quote locates at a new "
            "span while the siblings still pin the original mention — "
            "two distinct in-scope definition mentions, "
            "protocol_reference_ambiguous; no stale node survives and no "
            "new node is minted"),
        "status": "PASS" if a1_ok else "FAIL",
    }

    # (a2) mutate ALL facts quoting the definition sentence consistently:
    #      old nodes fail; the rebuild re-resolves the single moved
    #      mention and verifies with moved content addresses and a moved
    #      definition digest.
    tampered_a2 = deepcopy(facts)
    for fact in tampered_a2:
        if fact.get("fact_id") in DEFINITION_QUOTING_FACT_IDS:
            fact["excerpt"] = DEFINITION_QUOTE_EXTENDED
    old7_a2 = verify_state_proof_dag(dag7, graph, tampered_a2,
                                     span_of=span_of, **scope)
    old8_a2 = verify_state_proof_dag(dag8, graph, tampered_a2,
                                     span_of=span_of, **scope)
    rebuilt7_a2, issue7_a2 = _build_protocol_reference_dag(
        graph, tampered_a2, scope, span_of, 7)
    rebuilt8_a2, issue8_a2 = _build_protocol_reference_dag(
        graph, tampered_a2, scope, span_of, 8)
    a2_ok = (
        old7_a2 != "" and old8_a2 != ""
        and rebuilt7_a2 is not None and rebuilt8_a2 is not None
        and rebuilt7_a2["root_id"] != dag7["root_id"]
        and rebuilt8_a2["root_id"] != dag8["root_id"]
        and rebuilt7_a2["nodes"][rebuilt7_a2["root_id"]]["definition_digest"]
        != dag7["nodes"][dag7["root_id"]]["definition_digest"]
        and verify_state_proof_dag(rebuilt7_a2, graph, tampered_a2,
                                   span_of=span_of, **scope) == ""
        and verify_state_proof_dag(rebuilt8_a2, graph, tampered_a2,
                                   span_of=span_of, **scope) == ""
        and verify_state_proof_dag(dag7, graph, facts, span_of=span_of,
                                   **scope) == ""
        and verify_state_proof_dag(dag8, graph, facts, span_of=span_of,
                                   **scope) == ""
    )
    demo_a2 = {
        "tamper": ("all five facts quoting the definition sentence moved "
                   "together to the extended real span"),
        "old_graph7_node_verify": old7_a2,
        "old_graph8_node_verify": old8_a2,
        "rebuild_issues": [issue7_a2, issue8_a2],
        "root_ids_moved": a2_ok,
        "honest_rebuilt_behavior": (
            "resolution RE-RESOLVES the single moved mention: rebuilt "
            "nodes verify under the mutated facts with moved content "
            "addresses and a moved definition digest (locator/char_span "
            "are part of the digested record); the old nodes still "
            "verify under the untouched facts"),
        "status": "PASS" if a2_ok else "FAIL",
    }

    # (b) mutate the reference quotes: both old nodes fail; rebuilt nodes
    #     re-resolve (the reference mention still parses at its position)
    #     and verify with moved content addresses.
    tampered_b = deepcopy(facts)
    for fact in tampered_b:
        if fact.get("fact_id") in ("f_g7a_op", "f_g7b_op"):
            fact["excerpt"] = REFERENCE_QUOTE_EXTENDED
    old7_b = verify_state_proof_dag(dag7, graph, tampered_b,
                                    span_of=span_of, **scope)
    old8_b = verify_state_proof_dag(dag8, graph, tampered_b,
                                    span_of=span_of, **scope)
    rebuilt7_b, issue7_b = _build_protocol_reference_dag(
        graph, tampered_b, scope, span_of, 7)
    rebuilt8_b, issue8_b = _build_protocol_reference_dag(
        graph, tampered_b, scope, span_of, 8)
    b_ok = (
        old7_b != "" and old8_b != ""
        and rebuilt7_b is not None and rebuilt8_b is not None
        and rebuilt7_b["root_id"] != dag7["root_id"]
        and rebuilt8_b["root_id"] != dag8["root_id"]
        and verify_state_proof_dag(rebuilt7_b, graph, tampered_b,
                                   span_of=span_of, **scope) == ""
        and verify_state_proof_dag(rebuilt8_b, graph, tampered_b,
                                   span_of=span_of, **scope) == ""
        and verify_state_proof_dag(dag7, graph, facts, span_of=span_of,
                                   **scope) == ""
    )
    demo_b = {
        "tamper": ("f_g7a_op / f_g7b_op (the reference evidence) excerpts "
                   "extended rightwards over the same signed sentence "
                   "(still containing the ordinal mention and the "
                   "invocation-local 'one time')"),
        "old_graph7_node_verify": old7_b,
        "old_graph8_node_verify": old8_b,
        "rebuild_issues": [issue7_b, issue8_b],
        "root_ids_moved": b_ok,
        "honest_rebuilt_behavior": (
            "resolution RE-RESOLVES (the extended quote still parses: "
            "ordinal 'second', count 'one time', position unchanged); "
            "rebuilt nodes verify with moved content addresses; the old "
            "nodes fail on the reference leaf digest"),
        "status": "PASS" if b_ok else "FAIL",
    }

    # (c) resealed substitution: an unrelated but VALID node of the same
    #     DAG (the reference_evidence paper literal, which verifies on
    #     its own) swapped into the definition_evidence role, every
    #     content address honestly recomputed -> role-fact binding
    #     failure.
    medium_id = dag8["root_id"]
    donor_id = next(
        premise["node_id"] for premise in dag8["nodes"][medium_id]["premises"]
        if premise["role"] == "reference_evidence")

    def substitute(node):
        for premise in node["premises"]:
            if premise["role"] == "definition_evidence":
                premise["node_id"] = donor_id

    resealed = _reseal(dag8, medium_id, substitute)
    resealed_medium = next(
        node for node in resealed["nodes"].values()
        if node["node_type"] == "protocol_reference")
    honest = node_id_for(resealed_medium) == resealed_medium["node_id"]
    verdict_c = verify_state_proof_dag(resealed, graph, facts,
                                       span_of=span_of, **scope)
    c_ok = (honest and verdict_c == "proof_dag_protocol_reference_mismatch"
            and verify_state_proof_dag(dag8, graph, facts, span_of=span_of,
                                       **scope) == "")
    demo_c = {
        "tamper": ("the graph[8] node's definition_evidence premise "
                   "replaced with the SAME DAG's reference_evidence paper "
                   "literal (a valid node that verifies on its own), then "
                   "every dependent content address honestly resealed"),
        "reseal_honest_content_addresses": honest,
        "verify_issue": verdict_c,
        "expected": "proof_dag_protocol_reference_mismatch",
        "untouched_dag_still_verifies": (
            verify_state_proof_dag(dag8, graph, facts, span_of=span_of,
                                   **scope) == ""),
        "status": "PASS" if c_ok else "FAIL",
    }

    ok = a1_ok and a2_ok and b_ok and c_ok
    return {
        "status": "PASS" if ok else "FAIL",
        "definition_quote_tamper_single_fact": demo_a1,
        "definition_quote_tamper_all_quoting_facts": demo_a2,
        "reference_quote_tamper": demo_b,
        "resealed_definition_role_substitution": demo_c,
    }


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


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
    group = matches[0]

    binding_rows = _bind_all_excerpts(proposal, group)
    resolver, source_relation_rows = _retained_object_resolution(
        proposal, group)

    graph = proposal["material_graph"]
    facts = proposal.get("route_facts", [])
    ref = proposal["source_group_ref"]
    scope = {
        "paper_id": ref["paper_id"],
        "experimental_group_id": ref["experimental_group_id"],
        "source_digest": ref["source_digest"],
    }
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    span_of = build_excerpt_span_resolver(blocks, captions)

    dags = _build_all(graph, facts, scope, span_of)
    blocked = _build_blocked(graph, facts, scope, span_of)
    dag_summaries = {name: _dag_summary(dag) for name, dag in dags.items()}

    item_repr = _accept_representation(proposal, group)
    item1, item2, item3 = _accept_1_2_3(
        dags, graph, facts, scope, span_of, blocks, captions)
    item4 = _accept_4(dags, graph, facts, scope, resolver, span_of)
    item5 = _accept_5(dags, graph, facts, scope, span_of, blocks, captions)
    item6 = _accept_6(dags, graph, facts, scope, span_of)
    item7 = _accept_7(dags, graph, facts, scope, span_of)
    item8 = _accept_8(dags, graph, facts, scope, blocks, captions)
    item9 = _accept_9(dags, graph, facts, scope, resolver)

    round3b_acceptance = {
        "graph5_output_typed_dag": item1,
        "graph6_input_inheritance_dag": item2,
        "graph6_output_redispersion_dag": item3,
        "no_literal_parent_state": item4,
        "invalidation": item5,
        "forged_upstream_node": item6,
        "cycle_future_reference": item7,
        "leaf_relocation": item8,
        "legacy_regression": item9,
    }
    round3b_acceptance["status"] = (
        "PASS" if all(
            item["status"] == "PASS"
            for key, item in round3b_acceptance.items())
        else "FAIL")

    item_g7in = _accept_g7in(
        dags, graph, facts, scope, span_of, blocks, captions)
    item_ms7a = _accept_ms7a(graph, facts, scope, span_of, resolver, blocked)
    item_ms7b = _accept_ms7b(graph, facts, scope, span_of, resolver, blocked)
    item_g9 = _accept_graph9(blocked)
    item_contam = _accept_contamination(dags, graph, facts, scope, span_of)
    item_binding = _accept_binding(dags, graph, facts, scope, span_of)
    state_table = _final_state_table(
        dags, blocked, graph, facts, scope, span_of, blocks, captions)

    round3c_acceptance = {
        "compound_operation_representation_3c1": item_repr,
        "round3b_carryover": round3b_acceptance,
        "graph7_input_multihop_inheritance_3c2": item_g7in,
        "ms7a_output_retained_object_gap_3c3": item_ms7a,
        "ms7b_dual_result_3c4": item_ms7b,
        "graph9_cascade": item_g9,
        "real_chain_three_hop_contamination": item_contam,
        "premise_binding_soundness_real_chain": item_binding,
        "final_state_table": state_table,
    }
    round3c_acceptance["status"] = (
        "PASS" if all(
            item["status"] == "PASS"
            for key, item in round3c_acceptance.items())
        else "FAIL")

    # --- Round 3D: protocol-reference proof for the inherited liquid ---
    item_pr = _accept_protocol_reference(
        graph, facts, scope, span_of, blocks, captions)
    item_table_3d = _accept_carried_forward_table(state_table)
    item_ms7a_noleak = _accept_ms7a_no_leak(
        graph, facts, scope, span_of, resolver, blocked)
    item_ms7b_3d = _accept_ms7b_dual_3d(
        graph, facts, scope, span_of, resolver, blocked, blocks, captions)
    item_ce_nearer = _accept_counter_nearer_phrase(
        graph, facts, scope, span_of)
    item_ce_etching = _accept_counter_etching(
        graph, facts, scope, span_of, inventory)
    item_tamper_3d = _accept_tamper_protocol_reference(
        graph, facts, scope, span_of)

    round3d_acceptance = {
        "protocol_reference_proof_real_chain_3d1": item_pr,
        "carried_forward_state_table_3d2": item_table_3d,
        "ms7a_no_leak_3d3": item_ms7a_noleak,
        "ms7b_dual_result_3d4": item_ms7b_3d,
        "counter_example_nearer_medium_phrase_3d5": item_ce_nearer,
        "counter_example_etching_group_scope_3d6": item_ce_etching,
        "tamper_protocol_reference_real_chain_3d7": item_tamper_3d,
    }
    round3d_acceptance["status"] = (
        "PASS" if all(
            item["status"] == "PASS"
            for key, item in round3d_acceptance.items())
        else "FAIL")

    group_scope = group.source_scope
    audit = [
        {"kind": "round3d_typed_proof_dag",
         "module": "chem_agent_contracts/route_proof_dag.py",
         "node_schemas": [
             "paper-literal-proof/v1",
             "source-relation-proof/v1",
             "same-state-convention-proof/v1",
             "state-change-convention-proof/v1",
             "state-inheritance-proof/v1",
             "protocol-reference-proof/v1",
         ],
         "leaf_schema": "source-evidence-leaf/v1",
         "dag_schema": STATE_PROOF_DAG_SCHEMA,
         "design_rules": [
             "node and leaf ids are content addresses (sha256 of canonical "
             "JSON, id field excluded; premises canonicalized by role "
             "before hashing)",
             "the verifier pins one context (graph digest, paper/group "
             "scope, source digest, live rule-resource digest) at "
             "construction; any other context fails "
             "proof_dag_context_mismatch",
             "verification is structural first (premise existence, "
             "acyclicity, depth), then content (every node id recomputed, "
             "every node type re-derived from the signed blocks, facts and "
             "versioned rules); any failure invalidates the whole DAG",
             "convention nodes lift typed fields from a freshly recomputed "
             "flat proof, with a _VerifiedParentStateEvidence capability "
             "token minted only after the parent premise node has its own "
             "verified node in the DAG and the premise claim's binding "
             "triple matches the dependent node's typed parent fields "
             "(proof_dag_parent_state_binding_mismatch otherwise); the "
             "parent premise is the canonical chain node for the step's own "
             "input state path",
             "the forwarded token certifies the GRANDPARENT binding inside "
             "inheritance re-derivation, so chains of more than two "
             "consecutive non-literal hops compose (the 3B-1 follow-up is "
             "resolved)",
             "a protocol_reference node certifies that a step's affirmed "
             "operation references a protocol defined earlier in the SAME "
             "experimental group and inherits exactly the definition's "
             "operation_sequence and liquid_medium; the dependent "
             "convention node binds the liquid_medium premise to its own "
             "operation (path, value, operation evidence id — "
             "proof_dag_liquid_medium_binding_mismatch otherwise), the "
             "flat recompute receives a _VerifiedLiquidMedium capability "
             "token discharging ONLY the liquid-participation gate for "
             "that exact operation binding, and the retained_object role "
             "stays exclusively bound to source_relation nodes "
             "(proof_dag_retained_object_binding_mismatch otherwise)",
             "the liquid token is NOT forwarded into inheritance "
             "re-derivation (the documented v1 boundary; nothing in this "
             "chain needs it)",
             "the flat engine is reused, never reimplemented; legacy "
             "route-convention-state/v1 proofs are byte-compatible",
         ]},
        {"kind": "round3d_protocol_reference_contract",
         "module": "chem_agent_contracts/route_protocol_reference.py",
         "definition_schema": PROTOCOL_DEFINITION_SCHEMA,
         "resolution_record_schema": PROTOCOL_REFERENCE_PROOF_SCHEMA,
         "rule_id": PROTOCOL_REFERENCE_RULE_ID,
         "rule_version": PROTOCOL_REFERENCE_RULE_VERSION,
         "owner_ruling": {
             "definition_slots": ["operation_sequence", "liquid_medium"],
             "invocation_local": ["execution counts ('three times' / "
                                  "'one time') live only as annotations "
                                  "on the resolution record"],
             "invocation_results_never_inherited": [
                 "retained_object", "inter_segment_material_flow",
                 "output_state", "material_instance_identity",
                 "execution_count", "segment_input_material",
                 "segment_output_material", "retained_phase",
                 "inter_segment_flow", "derived_state_transition"],
             "syntactic_scope_rationale": (
                 "the medium slot is the phrase the 'using' clause "
                 "GOVERNS syntactically — it must modify the protocol "
                 "mention itself; a nearer medium phrase that modifies a "
                 "different operation ('dispersed in 30 mL of water and "
                 "aged') is never a candidate"),
         },
         "resolution_rules_v1": [
             "same-group scope only (paper id + group id + source "
             "digest); a name-matched definition outside the pinned "
             "scope fails protocol_reference_scope_mismatch",
             "normalized protocol-name equality (U+2212/dash variants, "
             "case, whitespace)",
             "only mentions that EXTRACT as definitions count",
             "the definition must textually precede the reference "
             "(projected character offsets), else "
             "protocol_reference_definition_after_reference",
             "facts quoting the same mention dedupe to one; zero "
             "mentions -> protocol_reference_unresolved, two or more "
             "distinct mentions -> protocol_reference_ambiguous",
             "the reference ordinal must strictly follow the "
             "definition's anchor ordinal, else "
             "protocol_reference_ordinal_mismatch",
             "no proximity heuristics anywhere",
         ]},
        {"kind": "round3c_representation_fix",
         "proposal": "local-revision-r10-proposal.json",
         "derived_from": "local-revision-r8-proposal.json",
         "change": ("graph[7] (ms7a) and graph[8] (ms7b) source operation "
                    "is the compound 'second centrifugation−redispersion "
                    "protocol' (U+2212), mirroring graph[5]'s "
                    "composite-operation representation; operation facts "
                    "f_g7a_op/f_g7b_op carry the compound value on the "
                    "unchanged, still-binding excerpt; every other step "
                    "and fact is byte-identical to r8"),
         "rationale": ("the bare-word representation bypassed the "
                       "compound-operation guard and misattributed "
                       "blockers — an evidence-semantics precondition")},
        {"kind": "trust_boundary",
         "rules": [
             "source leaves are recomputable: the verifier re-locates every "
             "leaf inside the signed group blocks from locator + projected "
             "char span alone and re-checks the excerpt digest",
             "blocks available -> leaves source-verified; without blocks "
             "only internal consistency is verifiable and the leaf stays "
             "not source-verified",
             "a tampered node either fails id recomputation "
             "(proof_dag_node_id_mismatch) or leaves a dangling premise "
             "reference (proof_dag_premise_missing); an otherwise-valid "
             "node substituted into the parent_state role fails "
             "proof_dag_parent_state_binding_mismatch, into the "
             "liquid_medium role fails "
             "proof_dag_liquid_medium_binding_mismatch, into the "
             "retained_object role fails "
             "proof_dag_retained_object_binding_mismatch, and into the "
             "definition_evidence role fails "
             "proof_dag_protocol_reference_mismatch",
             "an inheritance edge may only reach backwards in the graph "
             "(proof_dag_future_reference); premise cycles fail "
             "proof_dag_cycle",
             "what-if diagnostics mint local capability tokens in the "
             "runner only (a _VerifiedParentStateEvidence and a "
             "_VerifiedLiquidMedium minted from the VERIFIED "
             "protocol_reference node); they are labelled diagnostics "
             "and never feed any proof or pass verdict",
         ]},
        {"kind": "group_identity",
         "paper_id": group_scope.paper_id,
         "experimental_group_id": group_scope.experimental_group_id,
         "source_digest": group_scope.source_digest,
         "block_count": len(group.blocks),
         "caption_count": sum(1 for block in group.blocks if block.caption)},
        {"kind": "per_dag_summaries", "dags": dag_summaries},
        {"kind": "blocked_nodes",
         "nodes": [
             {"node": NODE_LABELS[name],
              "field_path": blocked[name]["field_path"],
              "verdict": "BLOCKED",
              "build_issue": blocked[name]["build_issue"]}
             for name in BLOCKED_TARGETS],
         "attribution": ("ms7a.out is the root gap (retained-object "
                         "evidence gap for the second centrifugation); "
                         "ms7b.in, ms7b.out, graph[9].in and graph[9].out "
                         "are dependency cascades from it; ms7b.out "
                         "additionally carries the independent what-if "
                         "liquid-participation diagnostic")},
        {"kind": "deliberately_not_done",
         "items": [
             "Q1 population scope: typed parent_ref v1 allows only "
             "material_instance; PopulationStateProofV1 is the planned Q1 "
             "extension",
             "retained-object inheritance of ANY kind: NOT introduced — "
             "the first protocol's retained semantics are never copied "
             "onto the 'second ... protocol'; the precipitate stays a "
             "first-invocation result and ms7a.out stays BLOCKED (the "
             "no-leak item asserts a correctly-bound liquid token "
             "changes nothing in ms7a's derivation)",
             "operation-precondition inference: NOT introduced — a "
             "downstream redispersion never proves an upstream retained "
             "phase",
             "cross-group reference resolution: NOT introduced — the "
             "Etching group's reference fails closed on scope rather "
             "than binding the Control group's definition",
             "execution counts as definition slots: NOT introduced — "
             "'three times' / 'one time' are invocation-local "
             "annotations only",
             "admitted_protocols stays 0: G1 admission gating is a "
             "separate milestone from the Q1 publication gate",
             "production cache untouched; no global gate relaxation; "
             "the compound-operation guard is NOT weakened to make "
             "ms7b's full derive pass",
             "the liquid token is NOT forwarded into inheritance "
             "re-derivation (documented v1 boundary; unused in this "
             "chain)",
             "resolver error-code refinement "
             "(retained_object_for_operation_unverified): NOT shipped — "
             "the r7/r8 runners record the per-step issues verbatim and "
             "must re-run byte-identical",
             "O8 deferred",
         ]},
        {"kind": "contract_validation", "rows": contract_rows},
        {"kind": "excerpt_binding", "rows": binding_rows},
        {"kind": "post_operation_retained_object_per_step",
         "rows": source_relation_rows},
        {"kind": "round3c_acceptance_carryover", "items": round3c_acceptance},
        {"kind": "round3d_acceptance", "items": round3d_acceptance},
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
        },
        "round3c_acceptance_carryover": round3c_acceptance,
        "round3d_acceptance": round3d_acceptance,
        "per_dag_summaries": dag_summaries,
        "post_operation_retained_object_per_step": source_relation_rows,
        "excerpt_binding_issue_counts": dict(Counter(
            row["binding_issue"] for row in binding_rows)),
    }
    (out_dir / "local-revision-r11-audit.json").write_text(
        json.dumps({
            "schema_version": "bounded_local_revision/v11",
            "model_generated": False,
            "scope": ("Round 3D phase 2: real-chain acceptance of the "
                      "narrowly-scoped protocol-reference proof class — "
                      "verified protocol_reference nodes for "
                      "graph[7].operation and graph[8].operation on the "
                      "real signed NiFe Control group (unchanged r10 "
                      "proposal, read-only), the locked A01 acceptance "
                      "table (inherited liquid medium deionized water "
                      "PASS as an independent diagnostic; all nine r10 "
                      "verdicts unchanged), the ms7a no-leak assertion, "
                      "counter-example stress tests (the nearer "
                      "non-definition medium phrase; the Etching group's "
                      "cross-group isolation), tamper items for the new "
                      "proof class, and the updated ms7b dual result "
                      "whose honest engine what-if stays BLOCKED on the "
                      "second protocol's retained-object/composite-"
                      "arbitration gap"),
            "audit": audit,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "local-revision-r11-replay.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(json.dumps(result["round3d_acceptance"], ensure_ascii=False,
                     indent=1, default=str))


if __name__ == "__main__":
    main()
