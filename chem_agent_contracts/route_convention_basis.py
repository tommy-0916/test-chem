"""Exact, source-scoped proof for a state inherited through one material edge.

This does not review chemistry or create a material relation. It only checks a
relation already declared in a route graph against the versioned convention
resource and two independently locatable paper facts. The caller must still
verify those quotations against the original experimental group.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .route_field_basis import (
    affirmative_material_operation_span, controlled_state_mapping,
    split_rule_pattern_matches, state_source_locally_attributed,
)


_RESOURCE = Path(__file__).resolve().parents[1] / "chem_resources" / "chemistry_conventions" / "conventions.json"
_OUTPUT_STATE = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.material_outputs"
    r"\[(0|[1-9][0-9]*)\]\.state\Z"
)
_OUTPUT_QUANTITY = re.compile(
    r"material_graph\[(0|[1-9][0-9]*)\]\.material_outputs"
    r"\[(0|[1-9][0-9]*)\]\.quantity\.value\Z"
)
_SPLIT_COUNT_UNITS = frozenset({
    "part", "parts", "portion", "portions", "fraction", "fractions",
    "aliquot", "aliquots",
})
_CONCENTRATION_UNITS = frozenset({"M", "mM", "mol/L", "mmol/L", "mol l-1", "mmol l-1"})
_RULE_EVENTS = {
    "SPLIT_V1": ("split_same_material", "split_material", "split_from_parent"),
    "TRANSFER_V1": ("process_same_material", "transfer_material", "transfer_of"),
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [_mapping(item) for item in value] if isinstance(value, (list, tuple)) else []


def output_quantity_role_issue(field_path: str, graph: Sequence[Any], unit: str) -> bool:
    """A split count or concentration cannot be one output material's amount."""
    match = _OUTPUT_QUANTITY.fullmatch(field_path)
    if match is None:
        return False
    normalized = unit.strip()
    if normalized in _CONCENTRATION_UNITS:
        return True
    # A count of portions describes cardinality of an operation. It does not
    # specify how much material each output contains, even if the proposal
    # omitted its relation graph entirely.
    return normalized.casefold() in _SPLIT_COUNT_UNITS


def _rule_resource() -> tuple[dict[str, Mapping[str, Any]], str]:
    try:
        raw = _RESOURCE.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}, ""
    if (not isinstance(payload, dict)
            or payload.get("schema") != "chemistry-conventions/v1"
            or not isinstance(payload.get("rules"), list)):
        return {}, ""
    rules = {
        rule["rule_id"]: rule for rule in payload["rules"]
        if isinstance(rule, dict)
        and rule.get("rule_id") in _RULE_EVENTS
        and rule.get("numeric_generation_allowed") is False
        and isinstance(rule.get("version"), str)
        and rule.get("output_states") == ["same_as_input"]
    }
    return rules, "sha256_" + sha256(raw).hexdigest()


def _evidence_id(paper_id: str, group_id: str, fact_id: str) -> str:
    return "route_fact_" + sha256(
        f"{paper_id}\0{group_id}\0{fact_id}".encode("utf-8")
    ).hexdigest()[:24]


def _literal_in_quote(value: str, quote: str) -> bool:
    return bool(value and re.search(
        rf"(?<!\w){re.escape(value)}(?!\w)", quote,
        flags=re.IGNORECASE,
    ))


def _proof_for_evidence(
    graph: Sequence[Any], field_path: str, *,
    parent_source_value: str, parent_excerpt: str,
    operation_value: str, operation_excerpt: str,
    parent_evidence_id: str, operation_evidence_id: str,
    paper_id: str, experimental_group_id: str, source_digest: str,
) -> tuple[dict[str, str] | None, str]:
    """Recompute one exact proof; no candidate-provided rule identifier is used."""
    match = _OUTPUT_STATE.fullmatch(field_path)
    if match is None:
        return None, "semantic_binding_pending"
    step_index, output_index = int(match.group(1)), int(match.group(2))
    if step_index >= len(graph):
        return None, "convention_graph_path_missing"
    step = _mapping(graph[step_index])
    outputs, inputs = _items(step.get("material_outputs")), _items(step.get("material_inputs"))
    if output_index >= len(outputs):
        return None, "convention_graph_path_missing"
    child = outputs[output_index]
    target_state = _text(child.get("state"))
    child_id = _text(child.get("material_instance_id"))
    child_material_id = _text(child.get("material_id"))
    if not all((target_state, child_id, child_material_id)):
        return None, "convention_material_identity_missing"
    if not all((paper_id, experimental_group_id, source_digest,
                parent_evidence_id, operation_evidence_id)):
        return None, "convention_source_scope_missing"
    if not _literal_in_quote(operation_value, operation_excerpt):
        return None, "convention_operation_fact_unbound"
    if _text(step.get("operation")) != operation_value:
        return None, "convention_operation_mismatch"

    relations = [relation for relation in _items(step.get("material_relations"))
                 if child_id in relation.get("output_material_instance_ids", [])]
    if len(relations) != 1:
        return None, "convention_material_relation_missing_or_ambiguous"
    relation = relations[0]
    parent_ids = relation.get("input_material_instance_ids")
    if not isinstance(parent_ids, list) or len(parent_ids) != 1:
        return None, "convention_parent_relation_ambiguous"
    parent_id = _text(parent_ids[0])
    parents = [(index, port) for index, port in enumerate(inputs)
               if _text(port.get("material_instance_id")) == parent_id]
    if len(parents) != 1:
        return None, "convention_parent_material_missing"
    input_index, parent = parents[0]
    if (_text(parent.get("material_id")) != child_material_id
            or _text(parent.get("state")) != target_state):
        return None, "convention_parent_state_or_identity_mismatch"
    if _text(parent.get("material_origin")) == "upstream_output":
        references = parent.get("parent_output_refs")
        if not isinstance(references, list) or len(references) != 1:
            return None, "convention_upstream_reference_missing"
        reference = _mapping(references[0])
        upstream = [
            port for earlier in graph[:step_index]
            for port in _items(_mapping(earlier).get("material_outputs"))
            if (_text(_mapping(earlier).get("macro_step_id"))
                == _text(reference.get("macro_step_id"))
                and _text(port.get("material_instance_id"))
                == _text(reference.get("material_instance_id")))
        ]
        if (len(upstream) != 1
                or _text(upstream[0].get("material_id")) != child_material_id
                or _text(upstream[0].get("state")) != target_state):
            return None, "convention_upstream_reference_mismatch"
    parent_state_path = f"material_graph[{step_index}].material_inputs[{input_index}].state"
    _state_mapping, issue = controlled_state_mapping(
        parent_state_path, parent_source_value, parent.get("state"),
    )
    if issue or not state_source_locally_attributed(
        parent_source_value, parent_excerpt, parent.get("name"),
    ):
        return None, "convention_parent_state_unverified"

    relation_id = _text(relation.get("relation_id"))
    segment_id = _text(relation.get("source_operation_ref"))
    segments = [segment for segment in _items(step.get("operation_segments"))
                if _text(segment.get("segment_id")) == segment_id]
    if not relation_id or not segment_id or len(segments) != 1:
        return None, "convention_operation_segment_missing"
    from .v2 import MaterialOperationSegmentV2, MaterialRelationV2

    try:
        MaterialRelationV2.model_validate(relation, strict=True)
        MaterialOperationSegmentV2.model_validate(segments[0], strict=True)
    except (TypeError, ValueError):
        return None, "convention_material_relation_or_segment_invalid"
    relation_provenance = _mapping(relation.get("provenance"))
    segment_provenance = _mapping(segments[0].get("provenance"))
    if any(
        provenance.get("kind") != "paper"
        or _text(provenance.get("reference")) != operation_evidence_id
        for provenance in (relation_provenance, segment_provenance)
    ):
        return None, "convention_operation_evidence_missing"

    rules, resource_digest = _rule_resource()
    eligible: list[Mapping[str, Any]] = []
    for rule_id, (event_kind, material_effect, lineage_type) in _RULE_EVENTS.items():
        rule = rules.get(rule_id)
        if (rule is None or relation.get("event_kind") != event_kind
                or segments[0].get("material_effect") != material_effect
                or _mapping(rule.get("lineage_effect")).get("relation_type") != lineage_type):
            continue
        if rule_id == "TRANSFER_V1" and len(relation.get("output_material_instance_ids", [])) != 1:
            continue
        preconditions = _mapping(rule.get("preconditions"))
        operation_blob = operation_value.casefold()
        intent_blob = (operation_value + " " + operation_excerpt).casefold()
        patterns = preconditions.get("operation_patterns", [])
        intents = preconditions.get("intent_patterns", [])
        allowed_states = preconditions.get("input_states", [])
        pattern_matches = (
            split_rule_pattern_matches if rule_id == "SPLIT_V1"
            else lambda pattern, text: _text(pattern).casefold() in text
        )
        if (not isinstance(patterns, list) or not isinstance(intents, list)
                or not isinstance(allowed_states, list)
                or not any(pattern_matches(pattern, operation_blob)
                           for pattern in patterns if _text(pattern))
                or not any(pattern_matches(pattern, intent_blob)
                           for pattern in intents if _text(pattern))
                or target_state not in allowed_states
                or target_state not in rule.get("allowed_input_states", [])):
            continue
        eligible.append(rule)
    if len(eligible) != 1:
        return None, "convention_rule_not_applicable_or_ambiguous"
    rule = eligible[0]
    lineage = _mapping(step.get("lineage_relation"))
    expected_lineage_type = _RULE_EVENTS[rule["rule_id"]][2]
    if (lineage.get("relation_type") != expected_lineage_type
            or lineage.get("parent_material_instance_ids") != [parent_id]
            or lineage.get("child_material_instance_ids")
            != relation.get("output_material_instance_ids")):
        return None, "convention_lineage_relation_mismatch"
    operation_kind = "split" if rule["rule_id"] == "SPLIT_V1" else "transfer"
    if affirmative_material_operation_span(
        operation_excerpt, _text(parent.get("name")), operation_kind,
    ) is None:
        # A transferred vessel, electron, or unrelated material is not proof
        # that this exact parent material was transferred or split.
        return None, "convention_operation_material_attribution_unresolved"
    if rule["rule_id"] == "SPLIT_V1":
        child_ids = relation.get("output_material_instance_ids", [])
        resolved = [port for port in outputs
                    if _text(port.get("material_instance_id")) in child_ids]
        declared_parts = re.findall(
            r"\b([1-9][0-9]*)\s+(?:parts|fractions|portions)\b",
            operation_excerpt, flags=re.IGNORECASE,
        )
        if (len(child_ids) < 2 or len(set(child_ids)) != len(child_ids)
                or len(resolved) != len(child_ids)
                or len(declared_parts) != 1
                or int(declared_parts[0]) != len(child_ids)
                or any(_text(port.get("material_id")) != child_material_id
                       or _text(port.get("state")) != target_state
                       for port in resolved)):
            return None, "convention_split_children_incomplete"
    return {
        "schema_version": "route-convention-state/v1",
        "field_path": field_path,
        "target_state": target_state,
        "parent_state_path": parent_state_path,
        "parent_source_value": parent_source_value,
        "parent_evidence_id": parent_evidence_id,
        "operation_path": f"material_graph[{step_index}].operation",
        "operation_evidence_id": operation_evidence_id,
        "relation_id": relation_id,
        "parent_instance_id": parent_id,
        "child_instance_id": child_id,
        "rule_id": _text(rule.get("rule_id")),
        "rule_version": _text(rule.get("version")),
        "resource_digest": resource_digest,
        "paper_id": paper_id,
        "experimental_group_id": experimental_group_id,
        "source_digest": source_digest,
    }, ""


def derive_unreviewed_output_state(
    graph: Sequence[Any], facts: Sequence[Any], field_path: str, *,
    paper_id: str, experimental_group_id: str, source_digest: str,
) -> tuple[dict[str, str] | None, str]:
    """Produce a proof only from existing graph edges and proposed source facts."""
    by_path: dict[str, Mapping[str, Any]] = {}
    for raw in facts:
        fact = _mapping(raw)
        path = _text(fact.get("field_path"))
        if not path or path in by_path:
            return None, "convention_fact_path_missing_or_duplicate"
        by_path[path] = fact
    child_fact = by_path.get(field_path)
    match = _OUTPUT_STATE.fullmatch(field_path)
    if child_fact is None or match is None:
        return None, "semantic_binding_pending"
    step_index, output_index = int(match.group(1)), int(match.group(2))
    try:
        step = _mapping(graph[step_index])
        child = _items(step.get("material_outputs"))[output_index]
    except (IndexError, TypeError):
        return None, "convention_graph_path_missing"
    if _text(child_fact.get("value")) != _text(child.get("state")):
        return None, "convention_child_state_value_mismatch"
    operation_path = f"material_graph[{step_index}].operation"
    operation_fact = by_path.get(operation_path)
    if operation_fact is None:
        return None, "convention_operation_fact_missing"
    if (_text(child_fact.get("excerpt")) != _text(operation_fact.get("excerpt"))
            or child_fact.get("source") != operation_fact.get("source")):
        return None, "convention_child_quote_not_operation_quote"
    relation_candidates = [relation for relation in _items(step.get("material_relations"))
                           if _text(child.get("material_instance_id"))
                           in relation.get("output_material_instance_ids", [])]
    if len(relation_candidates) != 1:
        return None, "convention_material_relation_missing_or_ambiguous"
    parent_ids = relation_candidates[0].get("input_material_instance_ids")
    if not isinstance(parent_ids, list) or len(parent_ids) != 1:
        return None, "convention_parent_relation_ambiguous"
    parent_ports = [(index, port) for index, port in enumerate(_items(step.get("material_inputs")))
                    if _text(port.get("material_instance_id")) == _text(parent_ids[0])]
    if len(parent_ports) != 1:
        return None, "convention_parent_material_missing"
    parent_state_path = f"material_graph[{step_index}].material_inputs[{parent_ports[0][0]}].state"
    parent_fact = by_path.get(parent_state_path)
    if parent_fact is None:
        return None, "convention_parent_state_fact_missing"
    support_sources = [_mapping(fact.get("source"))
                       for fact in (child_fact, parent_fact, operation_fact)]
    # Before quote association the unsigned model proposal has only one
    # source_group_ref. This is a proof candidate, never a verified locator.
    if any(support_sources):
        if not all(support_sources):
            return None, "convention_source_scope_mismatch"
        for source in support_sources:
            if (source.get("paper_id") != paper_id
                    or source.get("experimental_group_id") != experimental_group_id
                    or source.get("source_digest") != source_digest):
                return None, "convention_source_scope_mismatch"
    operation_id = _text(operation_fact.get("fact_id"))
    parent_id = _text(parent_fact.get("fact_id"))
    if not operation_id or not parent_id or not _text(child_fact.get("fact_id")):
        return None, "convention_support_fact_identity_missing"
    operation_evidence_id = _evidence_id(paper_id, experimental_group_id, operation_id)
    relation = relation_candidates[0]
    segment_id = _text(relation.get("source_operation_ref"))
    segments = [segment for segment in _items(step.get("operation_segments"))
                if _text(segment.get("segment_id")) == segment_id]
    if len(segments) != 1:
        return None, "convention_operation_segment_missing"
    # Unsigned graph placeholders are not paper evidence yet. The compiler
    # replaces them only after the cited literal operation fact is checked.
    for node in (relation, segments[0]):
        provenance = _mapping(node.get("provenance"))
        if provenance.get("kind") != "paper" or provenance.get("reference") != f"fact:{operation_id}":
            return None, "convention_operation_evidence_missing"
    prepared = json.loads(json.dumps(graph))
    prepared_step = prepared[step_index]
    for node in (*prepared_step.get("material_relations", []),
                 *prepared_step.get("operation_segments", [])):
        provenance = node.get("provenance")
        if isinstance(provenance, dict) and provenance.get("reference") == f"fact:{operation_id}":
            provenance["reference"] = operation_evidence_id
    return _proof_for_evidence(
        prepared, field_path,
        parent_source_value=_text(parent_fact.get("value")),
        parent_excerpt=_text(parent_fact.get("excerpt")),
        operation_value=_text(operation_fact.get("value")),
        operation_excerpt=_text(operation_fact.get("excerpt")),
        parent_evidence_id=_evidence_id(paper_id, experimental_group_id, parent_id),
        operation_evidence_id=operation_evidence_id,
        paper_id=paper_id, experimental_group_id=experimental_group_id,
        source_digest=source_digest,
    )


def verify_bound_output_state(
    proof: Any, graph: Sequence[Any], evidence_by_id: Mapping[str, Any], *,
    paper_id: str, experimental_group_id: str, source_digest: str,
) -> str:
    """Recompute a saved proof from the typed graph, evidence and rule bytes."""
    if not isinstance(proof, Mapping) or any(not isinstance(value, str)
                                             for value in proof.values()):
        return "convention_proof_invalid"
    if (proof.get("paper_id") != paper_id
            or proof.get("experimental_group_id") != experimental_group_id
            or proof.get("source_digest") != source_digest):
        return "convention_source_scope_mismatch"
    parent = _mapping(evidence_by_id.get(_text(proof.get("parent_evidence_id"))))
    operation = _mapping(evidence_by_id.get(_text(proof.get("operation_evidence_id"))))
    if not parent or not operation:
        return "convention_support_evidence_missing"
    field_path = _text(proof.get("field_path"))
    match = _OUTPUT_STATE.fullmatch(field_path)
    if match is None:
        return "convention_proof_invalid"
    step_index = int(match.group(1))
    if step_index >= len(graph):
        return "convention_graph_path_missing"
    step = _mapping(graph[step_index])
    expected, issue = _proof_for_evidence(
        graph, field_path,
        parent_source_value=_text(proof.get("parent_source_value")),
        parent_excerpt=_text(parent.get("excerpt")),
        operation_value=_text(step.get("operation")),
        operation_excerpt=_text(operation.get("excerpt")),
        parent_evidence_id=_text(proof.get("parent_evidence_id")),
        operation_evidence_id=_text(proof.get("operation_evidence_id")),
        paper_id=paper_id, experimental_group_id=experimental_group_id,
        source_digest=source_digest,
    )
    if issue:
        return issue
    return "" if dict(proof) == expected else "convention_proof_mismatch"


def canonical_state_derivation(proof: Mapping[str, str]) -> str:
    return json.dumps(dict(proof), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


__all__ = [
    "derive_unreviewed_output_state", "verify_bound_output_state",
    "canonical_state_derivation", "output_quantity_role_issue",
]
