"""Round-9 (Round 3B): typed state-proof DAGs on the real signed group.

Round 3A wired the live retained-object resolver into the formal G1 path.
Round 3B composes the flat single-hop convention proofs into a
content-addressed DAG (``state-proof-dag/v1``,
``chem_agent_contracts/route_proof_dag.py``): every node carries a typed
schema, a claim, and role-canonicalized premises referencing other nodes by
content address, and every source fact becomes a recomputable
``source-evidence-leaf/v1`` that the verifier re-locates inside the signed
group blocks from its binding locator and projected character span alone.

The runner reuses the round-8 proposal UNMODIFIED (read-only) and keeps the
same preflight (contract validation, trust config + signed KB enumeration
of the NiFe Control group, excerpt binding, span resolver, retained-object
records), then builds and verifies proof DAGs on the REAL signed group for:

- ``material_graph[5].material_outputs[0].state`` (composite protocol
  output, CENTRIFUGE_COLLECT_PRECIPITATE_V1 standing on a source-relation
  node),
- ``material_graph[6].material_inputs[0].state`` (LDH-seeds input,
  inheritance node with a typed parent_ref),
- ``material_graph[6].material_outputs[0].state`` (redispersion output,
  REDISPERSION_V1 whose parent_state premise is the graph[5] output's own
  proven state-change node — the chain the flat engine could not prove).

The replay JSON's ``round3b_acceptance`` section reports the nine
acceptance items with PASS/FAIL plus evidence; anything not proven is
reported as BLOCKED with the exact issue, never as a pass.
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
    _resolve_fact_provenance,
    convention_fact_evidence_by_id,
    derive_unreviewed_output_state,
    verify_bound_output_state,
)
from chem_agent_contracts.route_proof_dag import (
    STATE_PROOF_DAG_SCHEMA,
    StateProofDagVerifier,
    build_state_proof_dag,
    leaf_id_for,
    node_id_for,
    verify_state_proof_dag,
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
PROPOSAL_PATH = out_dir / "local-revision-r8-proposal.json"

G5_OUT_STATE = "material_graph[5].material_outputs[0].state"
G6_IN_STATE = "material_graph[6].material_inputs[0].state"
G6_OUT_STATE = "material_graph[6].material_outputs[0].state"
G5_OPERATION = "material_graph[5].operation"
G6_OPERATION = "material_graph[6].operation"
G5_OUT_NAME = "material_graph[5].material_outputs[0].name"

DAG_TARGETS = {
    "graph5_output": G5_OUT_STATE,
    "graph6_input": G6_IN_STATE,
    "graph6_output": G6_OUT_STATE,
}


def _fail(message: str) -> None:
    raise SystemExit(f"local-revision-r9 contract/preflight failure: {message}")


# ---------------------------------------------------------------------------
# Preflight (identical in shape to the r8 runner).
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
# DAG helpers.
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


def _verify_both(dag, graph, facts, scope, span_of, blocks, captions) -> dict:
    span_issue = verify_state_proof_dag(
        dag, graph, facts, span_of=span_of, **scope)
    blocks_issue = StateProofDagVerifier(
        graph, facts, blocks=blocks, caption_block_locators=captions,
        **scope).verify(dag)
    return {"verify_with_span_resolver": span_issue,
            "verify_blocks_only": blocks_issue}


# ---------------------------------------------------------------------------
# Acceptance items.
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
    v6out = _verify_both(g6out, graph, facts, scope, span_of, blocks, captions)
    chain = _chain_summary(g6out)
    item3_ok = (
        out_root["node_type"] == "state_change"
        and out_root["rule_id"] == "REDISPERSION_V1"
        and out_root["rule_version"] == "1.1.0"
        and out_roles["parent_state"]["node_type"] == "state_change"
        and out_roles["parent_state"]["rule_id"]
            == "CENTRIFUGE_COLLECT_PRECIPITATE_V1"
        and out_roles["parent_state"]["claim"]["field_path"] == G5_OUT_STATE
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
                "(graph[5].out retained_wet_solid) -> state_change "
                "REDISPERSION_V1 (graph[6].out suspension); the "
                "graph[6].out root's parent_state premise is the graph[5] "
                "output's own proven state-change node, composing the "
                "proof the flat engine could not derive"),
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
    rules, digest = route_convention_basis._rule_resource()
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
        "operation": "held", "sample_id": "sample-r9-probe",
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
            "absent from all three DAGs"
            if legacy_schema_absent else "PRESENT (regression)"),
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
    dag_summaries = {name: _dag_summary(dag) for name, dag in dags.items()}

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

    group_scope = group.source_scope
    audit = [
        {"kind": "round3b_typed_proof_dag",
         "module": "chem_agent_contracts/route_proof_dag.py",
         "node_schemas": [
             "paper-literal-proof/v1",
             "source-relation-proof/v1",
             "same-state-convention-proof/v1",
             "state-change-convention-proof/v1",
             "state-inheritance-proof/v1",
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
             "flat proof, with parent_state_proven set only after the "
             "parent premise node has its own verified node in the DAG",
             "the flat engine is reused, never reimplemented; legacy "
             "route-convention-state/v1 proofs are byte-compatible",
         ]},
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
             "reference (proof_dag_premise_missing)",
             "an inheritance edge may only reach backwards in the graph "
             "(proof_dag_future_reference); premise cycles fail "
             "proof_dag_cycle",
         ]},
        {"kind": "group_identity",
         "paper_id": group_scope.paper_id,
         "experimental_group_id": group_scope.experimental_group_id,
         "source_digest": group_scope.source_digest,
         "block_count": len(group.blocks),
         "caption_count": sum(1 for block in group.blocks if block.caption)},
        {"kind": "per_dag_summaries", "dags": dag_summaries},
        {"kind": "deliberately_not_done",
         "items": [
             "graph[7a]/graph[7b] real blockers untouched: no protocol "
             "inheritance, no new rules (graph[7a] centrifugation output "
             "and graph[7b] redispersion output stay honestly unresolved)",
             "Q1 population scope: typed parent_ref v1 allows only "
             "material_instance; PopulationStateProofV1 is the planned Q1 "
             "extension",
             "admitted_protocols stays 0: G1 admission gating is a "
             "separate milestone from the Q1 publication gate",
             "inheritance re-derivation for >2 consecutive non-literal "
             "hops (3B-1 noted follow-up)",
             "O8 deferred",
         ]},
        {"kind": "contract_validation", "rows": contract_rows},
        {"kind": "excerpt_binding", "rows": binding_rows},
        {"kind": "post_operation_retained_object_per_step",
         "rows": source_relation_rows},
        {"kind": "round3b_acceptance", "items": round3b_acceptance},
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
                    "local-revision-r8-proposal.json",
            "modified": False,
            "sha256": "sha256_" + sha256(
                PROPOSAL_PATH.read_bytes()).hexdigest(),
        },
        "round3b_acceptance": round3b_acceptance,
        "per_dag_summaries": dag_summaries,
        "post_operation_retained_object_per_step": source_relation_rows,
        "excerpt_binding_issue_counts": dict(Counter(
            row["binding_issue"] for row in binding_rows)),
    }
    (out_dir / "local-revision-r9-audit.json").write_text(
        json.dumps({
            "schema_version": "bounded_local_revision/v9",
            "model_generated": False,
            "scope": ("Round 3B: typed state-proof DAGs "
                      "(state-proof-dag/v1) composed from the flat "
                      "convention engine on the real signed NiFe Control "
                      "group; content-addressed nodes with "
                      "role-canonicalized premises, recomputable "
                      "source-evidence leaves, pinned-context whole-DAG "
                      "verification; the graph[6].out redispersion chain "
                      "now proves where the flat engine stays honestly "
                      "blocked; the r8 proposal is reused unmodified"),
            "audit": audit,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "local-revision-r9-replay.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(json.dumps(result["round3b_acceptance"], ensure_ascii=False,
                     indent=1, default=str))


if __name__ == "__main__":
    main()
