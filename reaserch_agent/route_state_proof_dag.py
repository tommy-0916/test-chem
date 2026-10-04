"""G1 adapter: state-proof-dag/v1 artifacts for the two G1 consumption points.

The typed proof DAG machinery (``chem_agent_contracts/route_proof_dag.py``)
was accepted in rounds r10/r11 (3C/3D) but had no production consumer: the
flat convention engine proves one state at a time and gates every parent on
literal checks, so a multi-hop chain whose intermediate states are not
paper-literal stays ``convention_parent_state_unverified`` even when every
hop is provable.  This adapter is the shared, minimal bridge used by the two
G1 consumption points:

1. extraction (``route_pdf_group_extraction._prepare_unsigned_proposal``):
   a parallel ``convention_state_proof_dags`` audit key next to the
   unchanged flat ``convention_state_candidates``;
2. receipt (``route_group_fact_receipt._state_derivation_proof``): a DAG
   derivation is accepted only after BOTH flat single-hop derivations fail,
   and only for a dual-verified entry (span-resolver mode AND blocks-only
   mode, empty issue strings), marked ``derivation="state_proof_dag_v1"`` —
   never disguised as a paper-literal or flat-derived fact.

The adapter builds the excerpt span resolver itself from the caller's
signed blocks and passes blocks/caption locators to the verifier (never an
r8-style retained-object resolver object).  Every emitted DAG is a plain
JSON-serializable dict: callers must not hold a DAG across stages and
verify it after handing it to mutating code — a consumer that received a
DAG from elsewhere re-verifies against the CURRENT signed blocks (the
receipt integration rebuilds from its own blocks and never reads the
extraction artifact).  No capability token is ever minted, forwarded, or
stored by this module; minting stays inside the contracts build/verify
windows.  3E diagnostic records (DiagnosticRecordV1 / record_to_dict form)
are never proof artifacts and are rejected by the consumption guard.

BLOCKED attribution is mechanical and mirrors the accepted r10/r11 verdict
table: a blocked path whose direct upstream state dependencies are all
proven is an ``evidence_gap`` (its own evidence is missing); otherwise it
is a ``dependency_cascade``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from chem_agent_contracts.route_proof_dag import (
    STATE_PROOF_DAG_SCHEMA,
    StateProofDagVerifier,
    build_state_proof_dag,
    verify_state_proof_dag,
)
from chem_agent_contracts.route_retained_object import build_excerpt_span_resolver

# Derivation marker carried by DAG-accepted receipt records.  Not a proof
# class: the proof artifact is the DAG itself (schema state-proof-dag/v1);
# this label only classifies the consumption record's origin.
STATE_PROOF_DAG_DERIVATION = "state_proof_dag_v1"

_OUTPUT_STATE_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.material_outputs\[(0|[1-9][0-9]*)\]"
    r"\.state\Z"
)
_INPUT_STATE_PATH = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]"
    r"\.(material_inputs|material_intermediates)\[(0|[1-9][0-9]*)\]\.state\Z"
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_input_state_path(field_path: str) -> bool:
    return _INPUT_STATE_PATH.fullmatch(field_path) is not None


def state_fact_field_paths(facts: Any) -> list[str]:
    """Ordered, de-duplicated ``.state`` field paths carried by route facts."""
    paths: list[str] = []
    if not isinstance(facts, list):
        return paths
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        path = fact.get("field_path")
        if (isinstance(path, str) and path.endswith(".state")
                and path not in paths):
            paths.append(path)
    return paths


def _dependency_state_paths(graph: Any, field_path: str) -> list[str]:
    """Direct upstream state paths of ``field_path`` for BLOCKED attribution.

    An output state's dependencies are its own step's input/intermediate
    state paths (the flat engine's parent state is one of them); an input
    state's dependencies are the upstream output state paths its
    ``parent_output_refs`` resolve to.  Reporting only: verdicts never
    depend on this classification.
    """
    if not isinstance(graph, list):
        return []
    match = _OUTPUT_STATE_PATH.fullmatch(field_path)
    if match is not None:
        step_index = int(match.group(1))
        if step_index >= len(graph) or not isinstance(graph[step_index], Mapping):
            return []
        step = graph[step_index]
        deps: list[str] = []
        for collection in ("material_inputs", "material_intermediates"):
            ports = step.get(collection)
            if not isinstance(ports, list):
                continue
            for port_index in range(len(ports)):
                deps.append(
                    f"material_graph[{step_index}].{collection}"
                    f"[{port_index}].state"
                )
        return deps
    match = _INPUT_STATE_PATH.fullmatch(field_path)
    if match is None:
        return []
    step_index, collection, port_index = (
        int(match.group(1)), match.group(2), int(match.group(3)),
    )
    if step_index >= len(graph) or not isinstance(graph[step_index], Mapping):
        return []
    ports = graph[step_index].get(collection)
    if not isinstance(ports, list) or port_index >= len(ports):
        return []
    port = ports[port_index]
    if not isinstance(port, Mapping):
        return []
    references = port.get("parent_output_refs")
    if not isinstance(references, list):
        return []
    deps = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        ref_step = reference.get("macro_step_id")
        ref_instance = reference.get("material_instance_id")
        for earlier_index, earlier in enumerate(graph):
            if not isinstance(earlier, Mapping):
                continue
            if earlier.get("macro_step_id") != ref_step:
                continue
            outputs = earlier.get("material_outputs")
            if not isinstance(outputs, list):
                continue
            for output_index, output in enumerate(outputs):
                if (isinstance(output, Mapping)
                        and output.get("material_instance_id") == ref_instance):
                    deps.append(
                        f"material_graph[{earlier_index}]"
                        f".material_outputs[{output_index}].state"
                    )
    return deps


def build_verified_state_proof_dags(
    graph: Any,
    facts: Any,
    *,
    paper_id: str,
    experimental_group_id: str,
    source_digest: str,
    blocks: Sequence[Any],
    caption_block_locators: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    """Build + dual-verify a state-proof DAG for every ``.state`` fact path.

    Returns ``{field_path: entry}`` in fact order, each entry carrying
    ``field_path``, ``dag`` (the plain state-proof-dag/v1 dict or ``None``),
    ``build_issue``, ``verify_with_span_resolver``, ``verify_blocks_only``,
    ``verdict`` (``"PASS"`` only when the DAG built and BOTH verification
    modes return an empty issue), ``attribution`` (``proven`` /
    ``evidence_gap`` / ``dependency_cascade``), and — for PASS entries — a
    ``derivation`` marker record for the receipt consumption point.  A DAG
    that built but failed verification is BLOCKED with the first non-empty
    verify issue surfaced in ``build_issue``'s place staying empty and the
    verify fields carrying the issues.
    """
    results: dict[str, dict[str, Any]] = {}
    if not isinstance(graph, list) or not isinstance(facts, list) or not blocks:
        return results
    scope = {
        "paper_id": paper_id,
        "experimental_group_id": experimental_group_id,
        "source_digest": source_digest,
    }
    span_of = build_excerpt_span_resolver(blocks, caption_block_locators)
    for field_path in state_fact_field_paths(facts):
        entry: dict[str, Any] = {
            "field_path": field_path,
            "dag": None,
            "build_issue": "",
            "verify_with_span_resolver": "",
            "verify_blocks_only": "",
            "verdict": "BLOCKED",
            "attribution": "",
        }
        try:
            dag, build_issue = build_state_proof_dag(
                graph, facts, field_path, span_of=span_of, **scope,
            )
        except Exception as exc:  # fail closed: no DAG proof, flat untouched
            entry["build_issue"] = f"proof_dag_adapter_exception:{type(exc).__name__}"
            results[field_path] = entry
            continue
        entry["build_issue"] = build_issue
        if dag is not None:
            entry["dag"] = dag
            entry["verify_with_span_resolver"] = verify_state_proof_dag(
                dag, graph, facts, span_of=span_of, **scope,
            )
            entry["verify_blocks_only"] = StateProofDagVerifier(
                graph, facts, blocks=blocks,
                caption_block_locators=caption_block_locators, **scope,
            ).verify(dag)
            if (entry["verify_with_span_resolver"] == ""
                    and entry["verify_blocks_only"] == ""):
                entry["verdict"] = "PASS"
                root = dag["nodes"][dag["root_id"]]
                claim = root.get("claim") if isinstance(root, Mapping) else {}
                claim = claim if isinstance(claim, Mapping) else {}
                entry["derivation"] = {
                    "derivation": STATE_PROOF_DAG_DERIVATION,
                    "field_path": field_path,
                    "target_state": _text(claim.get("target_state")),
                    "material_instance_id": _text(
                        claim.get("material_instance_id")),
                    "proof_dag_root_id": dag["root_id"],
                    "proof_dag_node_count": len(dag["nodes"]),
                    "root_node_type": _text(root.get("node_type")),
                }
        results[field_path] = entry
    # Mechanical BLOCKED attribution (mirrors the accepted r10/r11 table):
    # a blocked path whose direct state dependencies all prove is an
    # evidence gap of its own; anything else cascaded from upstream.
    for field_path, entry in results.items():
        if entry["verdict"] == "PASS":
            entry["attribution"] = "proven"
            continue
        dependencies = _dependency_state_paths(graph, field_path)
        if all(results.get(dep, {}).get("verdict") == "PASS"
               for dep in dependencies):
            entry["attribution"] = "evidence_gap"
        else:
            entry["attribution"] = "dependency_cascade"
    return results


def dag_entry_derivation(entry: Any) -> dict[str, Any] | None:
    """Consumption guard: the derivation record of a dual-verified entry.

    Returns a copy of the entry's ``derivation`` marker only when the entry
    carries a genuine state-proof-dag/v1 artifact that PASSED both
    verification modes.  Anything else — a 3E diagnostic record
    (``DiagnosticRecordV1`` instance or its ``record_to_dict`` form), a
    forged PASS verdict over a non-DAG artifact, a verdict/verify mismatch —
    returns ``None`` so the caller's existing literal gates stay in charge.
    """
    if not isinstance(entry, Mapping):
        return None
    dag = entry.get("dag")
    if not isinstance(dag, Mapping) or (
        dag.get("schema_version") != STATE_PROOF_DAG_SCHEMA
    ):
        return None
    if entry.get("verdict") != "PASS":
        return None
    if (entry.get("verify_with_span_resolver") != ""
            or entry.get("verify_blocks_only") != ""):
        return None
    derivation = entry.get("derivation")
    if not isinstance(derivation, Mapping) or (
        derivation.get("derivation") != STATE_PROOF_DAG_DERIVATION
    ):
        return None
    if derivation.get("proof_dag_root_id") != dag.get("root_id"):
        return None
    if derivation.get("field_path") != entry.get("field_path"):
        return None
    return dict(derivation)


__all__ = [
    "STATE_PROOF_DAG_DERIVATION",
    "build_verified_state_proof_dags",
    "dag_entry_derivation",
    "is_input_state_path",
    "state_fact_field_paths",
]
