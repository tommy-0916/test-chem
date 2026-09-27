"""Compile explicitly extracted facts within one experimental group.

This is a *proposal* compiler, not a paper verifier.  A model may identify a
fact and a field in an explicitly supplied material graph; the compiler binds
the proposed excerpt, graph claim and group scope without inventing a route,
material edge, capability, locator or quantity.  SourceVerifier later checks
the excerpt and locators against independently registered original text.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import math
import re
from typing import Any, Mapping, Sequence

from chem_agent_contracts.v2 import canonical_digest, evidence_contains_exact_quantity


_PATH_PART = re.compile(r"([a-z][a-z0-9_]*)(?:\[(0|[1-9][0-9]*)\])?\Z")
_GRAPH_ROOT = re.compile(r"material_graph\[(0|[1-9][0-9]*)\]\Z")
_SOURCE_LOCATOR = re.compile(
    r"(?:lines:[1-9][0-9]*-[1-9][0-9]*|"
    r"pdf:p[1-9][0-9]*:b[1-9][0-9]*-p[1-9][0-9]*:b[1-9][0-9]*)\Z"
)
_DOCUMENT_DIGEST = re.compile(r"sha256_[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RouteGroupCompilationDiagnosticV1:
    protocol_index: int
    group_index: int
    paper_id: str
    experimental_group_id: str
    reason_code: str


@dataclass
class RouteGroupCompilationResultV1:
    protocols: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[RouteGroupCompilationDiagnosticV1] = field(default_factory=list)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _scoped_claim(
    graph: list[Any], signature: Mapping[str, Any], path: str,
) -> tuple[Any, Mapping[str, Any] | None, str] | None:
    """Resolve an exact graph or route-signature field, without fuzzy matching."""
    chunks = path.split(".")
    match = _GRAPH_ROOT.fullmatch(chunks[0]) if chunks else None
    if len(chunks) < 2:
        return None
    if match is None:
        if chunks[0] != "route_signature":
            return None
        node: Any = signature
        owner: Mapping[str, Any] | None = None
    else:
        step_index = int(match.group(1))
        if step_index >= len(graph) or not isinstance(graph[step_index], Mapping):
            return None
        node = graph[step_index]
        owner = node
    unit = ""
    for chunk in chunks[1:]:
        item = _PATH_PART.fullmatch(chunk)
        if item is None or item.group(1) == "provenance" or not isinstance(node, Mapping):
            return None
        key = item.group(1)
        if key not in node:
            return None
        if key == "concentration_value":
            unit = _text(node.get("concentration_unit"))
        elif key == "value":
            unit = _text(node.get("unit"))
        node = node[key]
        if item.group(2) is not None:
            index = int(item.group(2))
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
        if isinstance(node, Mapping) and isinstance(node.get("provenance"), Mapping):
            owner = node
    return node, owner, unit


def _required_qualitative_paths(
    graph: list[Any], signature: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    """Paper-derived chemistry must have literal same-group quotations.

    Structural IDs, graph relations and device capability selectors are
    checked by later gates.  This deliberately demands a literal quote for
    the route's defining signature and for each paper-claimed operation and
    material identity/state.  A synonym is not promoted into evidence here.
    """
    signature_paths: set[str] = set()
    for key in ("route_family", "target_transformation", "endpoint_state"):
        if _text(signature.get(key)):
            signature_paths.add(f"route_signature.{key}")
    for key in ("precursor_roles", "reagent_roles", "operations", "control_modes"):
        items = signature.get(key)
        if isinstance(items, list):
            signature_paths.update(
                f"route_signature.{key}[{index}]"
                for index, value in enumerate(items) if _text(value)
            )
    transitions = signature.get("phase_transitions")
    if isinstance(transitions, list):
        for index, transition in enumerate(transitions):
            if isinstance(transition, Mapping):
                for key in ("before_state", "after_state", "confidence"):
                    if _text(transition.get(key)):
                        signature_paths.add(
                            f"route_signature.phase_transitions[{index}].{key}"
                        )

    graph_paths: set[str] = set()
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        prefix = f"material_graph[{step_index}]"
        for key in ("operation", "reagent_or_object"):
            if _text(step.get(key)):
                graph_paths.add(f"{prefix}.{key}")
        for port_kind in ("material_inputs", "material_intermediates", "material_outputs"):
            ports = step.get(port_kind)
            if not isinstance(ports, list):
                continue
            for port_index, port in enumerate(ports):
                if not isinstance(port, Mapping):
                    continue
                for key in ("name", "material_id", "state"):
                    if _text(port.get(key)):
                        graph_paths.add(f"{prefix}.{port_kind}[{port_index}].{key}")
    return signature_paths, graph_paths


def _numeric_leaves(graph: list[Any]) -> set[str]:
    found: set[str] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "provenance" or (
                    key == "sequence" and _GRAPH_ROOT.fullmatch(path)
                ):
                    continue
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            found.add(path)
        elif isinstance(value, str) and re.search(r"\d", value) and re.search(
            r"(?:\.value|_value)(?:\[[0-9]+\])?\Z", path
        ):
            found.add(path)

    for index, step in enumerate(graph):
        walk(step, f"material_graph[{index}]")
    return found


def _paper_placeholders(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "provenance" and isinstance(child, Mapping):
                if child.get("kind") == "paper":
                    found.append(child)
            else:
                found.extend(_paper_placeholders(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_paper_placeholders(child))
    return found


def _replace_paper_placeholders(
    value: Any, provenance_by_fact_id: Mapping[str, dict[str, Any]],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "provenance" and isinstance(child, dict) and child.get("kind") == "paper":
                fact_id = str(child["reference"])[len("fact:"):]
                value[key] = deepcopy(provenance_by_fact_id[fact_id])
            else:
                _replace_paper_placeholders(child, provenance_by_fact_id)
    elif isinstance(value, list):
        for child in value:
            _replace_paper_placeholders(child, provenance_by_fact_id)


def _fact_issue(
    raw: Any, *, paper_id: str, group_id: str, section: str,
    document_digest: str, graph: list[Any], signature: Mapping[str, Any],
) -> str:
    if not isinstance(raw, Mapping):
        return "route_fact_invalid"
    fact_id = _text(raw.get("fact_id"))
    field_path = _text(raw.get("field_path"))
    excerpt = _text(raw.get("excerpt"))
    unit = _text(raw.get("unit"))
    source = raw.get("source")
    if not fact_id or not field_path:
        return "route_fact_identity_missing"
    if not excerpt:
        return "route_fact_excerpt_missing"
    if raw.get("required", True) is not True:
        return "route_fact_required_must_be_true"
    if not isinstance(source, Mapping) or (
        _text(source.get("paper_id")) != paper_id
        or _text(source.get("experimental_group_id")) != group_id
        or _text(source.get("section")) != section
        or _text(source.get("source_digest")) != document_digest
    ):
        return "route_fact_experimental_group_mismatch"
    if _SOURCE_LOCATOR.fullmatch(_text(source.get("locator"))) is None:
        return "route_fact_locator_missing"
    claim = _scoped_claim(graph, signature, field_path)
    if claim is None:
        return "route_fact_graph_path_missing"
    actual, owner, graph_unit = claim
    claimed = raw.get("value")
    if isinstance(claimed, bool) or not isinstance(claimed, (int, float, str)):
        return "route_fact_value_invalid"
    if isinstance(claimed, float) and not math.isfinite(claimed):
        return "route_fact_value_invalid"
    if isinstance(actual, bool) or actual != claimed or unit != graph_unit:
        return "route_fact_graph_value_mismatch"
    if isinstance(claimed, (int, float)):
        if not unit:
            return "route_fact_numeric_unit_missing"
        if not evidence_contains_exact_quantity(excerpt, claimed, unit):
            return "route_fact_quantity_absent_from_excerpt"
    elif not claimed.strip() or re.search(
        rf"(?<!\w){re.escape(claimed.strip())}(?!\w)", excerpt
    ) is None:
        return "route_fact_value_absent_from_excerpt"
    if owner is not None:
        provenance = owner.get("provenance")
        if provenance != {"kind": "paper", "reference": f"fact:{fact_id}"}:
            return "route_fact_graph_provenance_mismatch"
    return ""


def _compile_group(group: dict[str, Any], parent: Mapping[str, Any]) -> str:
    """Mutate only a detached group copy; return a diagnostic on abstention."""
    raw_facts = group.get("route_facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        return "route_facts_missing_or_invalid"
    if "evidence_bundle" in group or "evidence_matrix" in group:
        return "route_fact_evidence_conflict"
    paper_id = _text(group.get("paper_id")) or _text(parent.get("paper_id"))
    group_id = _text(group.get("experimental_group_id"))
    source = group.get("source")
    if not isinstance(source, Mapping):
        return "route_group_source_missing"
    section = _text(source.get("section"))
    document_digest = _text(source.get("source_digest"))
    if (
        not paper_id or not group_id or not section
        or _DOCUMENT_DIGEST.fullmatch(document_digest) is None
    ):
        return "route_group_scope_missing"
    graph = group.get("material_graph")
    if not isinstance(graph, list) or not graph:
        return "route_group_material_graph_missing"
    for step in graph:
        if not isinstance(step, Mapping):
            return "route_group_material_graph_invalid"
        for port_kind in ("material_inputs", "material_intermediates", "material_outputs"):
            ports = step.get(port_kind, [])
            if not isinstance(ports, list):
                return "route_group_material_graph_invalid"
            for port in ports:
                if not isinstance(port, Mapping) or _text(port.get("state")) in {"", "unknown"}:
                    return "route_group_material_state_missing"
    for name in ("target", "route_signature", "required_capabilities"):
        if not group.get(name):
            return f"route_group_{name}_missing"
    signature = group["route_signature"]
    if not isinstance(signature, Mapping):
        return "route_group_route_signature_invalid"

    seen_paths: set[str] = set()
    excerpts_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for raw in raw_facts:
        issue = _fact_issue(
            raw, paper_id=paper_id, group_id=group_id,
            section=section, document_digest=document_digest, graph=graph,
            signature=signature,
        )
        if issue:
            return issue
        fact_id = raw["fact_id"].strip()
        field_path = raw["field_path"].strip()
        if field_path in seen_paths:
            return "route_fact_field_path_duplicate"
        seen_paths.add(field_path)
        earlier = excerpts_by_id.get(fact_id)
        identity = (raw["excerpt"].strip(), raw["source"])
        if earlier is not None and earlier != identity:
            return "route_fact_id_conflict"
        excerpts_by_id[fact_id] = identity

    if not _numeric_leaves(graph).issubset(seen_paths):
        return "route_fact_numeric_graph_claim_missing"
    signature_paths, graph_paths = _required_qualitative_paths(graph, signature)
    if not signature_paths.issubset(seen_paths):
        return "route_fact_signature_claim_missing"
    if not graph_paths.issubset(seen_paths):
        return "route_fact_qualitative_graph_claim_missing"
    for placeholder in _paper_placeholders(graph):
        reference = _text(placeholder.get("reference"))
        if set(placeholder) != {"kind", "reference"} or not reference.startswith("fact:"):
            return "route_fact_graph_provenance_mismatch"
        if reference[len("fact:"):] not in excerpts_by_id:
            return "route_fact_graph_reference_missing"

    evidence_bundle: list[dict[str, Any]] = []
    provenance_by_fact_id: dict[str, dict[str, Any]] = {}
    for fact_id, (excerpt, _source) in excerpts_by_id.items():
        evidence_id = "route_fact_" + sha256(
            f"{paper_id}\0{group_id}\0{fact_id}".encode("utf-8")
        ).hexdigest()[:24]
        evidence_index = len(evidence_bundle)
        evidence_bundle.append({
            "evidence_id": evidence_id,
            "title": _text(parent.get("source_title")) or _text(group.get("source_title")),
            "verification_status": "unassessed",
            "full_text_status": "unknown",
            "excerpt": excerpt,
        })
        provenance_by_fact_id[fact_id] = {
            "kind": "paper",
            "reference": evidence_id,
            "evidence_class": "paper_explicit",
            "source_path": f"evidence_bundle.items[{evidence_index}].excerpt",
            "excerpt": excerpt,
            "source_digest": canonical_digest(excerpt),
        }

    matrix: list[dict[str, Any]] = []
    for raw in raw_facts:
        fact_id = raw["fact_id"].strip()
        source = raw["source"]
        matrix.append({
            "field_path": raw["field_path"].strip(),
            "value": raw["value"],
            "unit": _text(raw.get("unit")),
            "required": True,
            "status": "supported",
            "provenance": deepcopy(provenance_by_fact_id[fact_id]),
            "evidence_id": provenance_by_fact_id[fact_id]["reference"],
            "source_scope": {
                "paper_id": paper_id,
                "experimental_group_id": group_id,
                "section": section,
                "locator": _text(source.get("locator")),
                "source_digest": document_digest,
            },
        })
    _replace_paper_placeholders(graph, provenance_by_fact_id)
    group["evidence_bundle"] = evidence_bundle
    group["evidence_matrix"] = matrix
    return ""


def compile_experimental_group_protocols(
    protocols: Sequence[Mapping[str, Any]],
) -> RouteGroupCompilationResultV1:
    """Bind explicit route facts; preserve incomplete groups for discovery.

    A missing ``route_facts`` list is legacy extraction and is passed through
    untouched.  Groups that declare route facts but fail any binding check are
    also passed through unchanged, with an explicit diagnostic.  The function
    is pure with respect to the caller's protocol objects and reads no files.
    """
    result = RouteGroupCompilationResultV1()
    for protocol_index, original in enumerate(protocols):
        if not isinstance(original, Mapping):
            continue
        protocol = deepcopy(dict(original))
        groups = protocol.get("experimental_groups")
        if isinstance(groups, list):
            entries = [
                (group_index, group) for group_index, group in enumerate(groups)
                if isinstance(group, dict) and "route_facts" in group
            ]
        elif "route_facts" in protocol:
            entries = [(0, protocol)]
        else:
            entries = []
        for group_index, group in entries:
            trial = deepcopy(group)
            reason = _compile_group(trial, protocol)
            if reason:
                result.diagnostics.append(RouteGroupCompilationDiagnosticV1(
                    protocol_index=protocol_index,
                    group_index=group_index,
                    paper_id=_text(group.get("paper_id")) or _text(protocol.get("paper_id")),
                    experimental_group_id=_text(group.get("experimental_group_id")),
                    reason_code=reason,
                ))
            elif group is protocol:
                protocol = trial
            else:
                groups[group_index] = trial
        result.protocols.append(protocol)
    return result


__all__ = [
    "RouteGroupCompilationDiagnosticV1",
    "RouteGroupCompilationResultV1",
    "compile_experimental_group_protocols",
]
