"""Build unsigned split/transfer graph edges from one located PDF operation.

The model proposes the operation and co-referenced material ports. This
producer checks the source sentence and one parent before constructing the
repeated V2 edge structures. It does not authenticate a PDF, review chemistry,
or turn an unreviewed proposal into an admissible route.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import re
from typing import Any, Mapping

from chem_agent_contracts.route_convention_basis import (
    derive_unreviewed_output_state, output_quantity_role_issue,
)
from chem_agent_contracts.route_field_basis import (
    affirmative_material_operation_span, controlled_state_mapping,
    state_source_locally_attributed,
)
from chem_agent_contracts.route_retained_object import (
    build_retained_object_resolver,
)
from chem_agent_contracts.v2 import (
    LineageRelationV2, MaterialOperationSegmentV2, MaterialRelationV2,
)

from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)


_CANDIDATE_OPERATION = re.compile(
    r"\b(?:split|splits|divided\s+into|transfer|transfers|transferred)\b",
    re.IGNORECASE,
)
_PART_COUNT = re.compile(
    r"\b([1-9][0-9]*)\s+(?:parts|fractions|portions)\b", re.IGNORECASE,
)
_GENERIC_PART_NAME = frozenset({
    "part", "parts", "portion", "portions", "fraction", "fractions",
})
_AMBIGUOUS_PARENT_NAME = frozenset({
    "sample", "samples", "part", "parts", "portion", "portions",
    "fraction", "fractions", "material", "materials",
})
_MAX_CHILDREN = 64


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _id(scope: tuple[str, str, str], kind: str, step_id: str, ordinal: int = 0) -> str:
    seed = "\0".join((*scope, kind, step_id, str(ordinal)))
    return f"{kind}_" + sha256(seed.encode("utf-8")).hexdigest()[:24]


def _issue(code: str, step_index: int | None = None) -> dict[str, Any]:
    return {"reason_code": code, "step_index": step_index}


def _source_scope_ok(fact: Mapping[str, Any], group: PdfExperimentalGroupV1) -> bool:
    source = fact.get("source")
    if source is None:
        return True
    scope = group.source_scope
    return isinstance(source, Mapping) and all((
        source.get("paper_id") == scope.paper_id,
        source.get("experimental_group_id") == scope.experimental_group_id,
        source.get("source_digest") == scope.source_digest,
    ))


def _bound_fact(
    fact: Mapping[str, Any], group: PdfExperimentalGroupV1,
) -> bool:
    value, excerpt = fact.get("value"), fact.get("excerpt")
    if (not isinstance(value, str) or not value.strip()
            or not isinstance(excerpt, str)
            or re.search(rf"(?<!\w){re.escape(value.strip())}(?!\w)", excerpt) is None
            or not _source_scope_ok(fact, group)):
        return False
    return _bound_quote(excerpt, group)


def _bound_quote(excerpt: str, group: PdfExperimentalGroupV1) -> bool:
    binding, issue = bind_pdf_quote(
        [(block.locator, block.text) for block in group.blocks], excerpt,
        caption_block_locators=[block.locator for block in group.blocks if block.caption],
    )
    return binding is not None and not issue


def _same_operation_excerpt(candidate: str, operation_excerpt: str) -> bool:
    """A valid quote from another event cannot be rewritten as this event."""
    left = normalize_pdf_quote_whitespace(candidate)
    right = normalize_pdf_quote_whitespace(operation_excerpt)
    return bool(left and right and (left in right or right in left))


def _split_count_for_parent_operation(excerpt: str, parent_name: str) -> int | None:
    span = affirmative_material_operation_span(excerpt, parent_name, "split")
    if span is None:
        return None
    # Cardinality must be the direct complement of the split predicate.
    # A later phrase such as "washed with 3 parts water" is a ratio, not
    # proof that this parent yielded three child instances.
    direct = re.match(
        r"\s*(?:into\s+)?([1-9][0-9]*)\s+"
        r"(?:parts|fractions|portions)\b",
        excerpt[span[1]:], flags=re.IGNORECASE,
    )
    if direct is None:
        return None
    matches = list(_PART_COUNT.finditer(excerpt))
    if len(matches) != 1 or int(matches[0].group(1)) != int(direct.group(1)):
        return None
    return int(direct.group(1))


def _fact_at(
    facts: list[Any], path: str,
) -> Mapping[str, Any] | None:
    matches = [fact for fact in facts
               if isinstance(fact, Mapping) and fact.get("field_path") == path]
    return matches[0] if len(matches) == 1 else None


def _set_parent_origin(
    graph: list[Any], step_index: int, parent: dict[str, Any],
    step: Mapping[str, Any],
) -> str:
    """Fill only an exact, unique upstream reference; never invent inventory."""
    if parent.get("material_origin") == "upstream_output":
        refs = parent.get("parent_output_refs")
        if not isinstance(refs, list) or len(refs) != 1:
            return "structure_parent_upstream_reference_invalid"
    elif parent.get("material_origin") not in (None, "external_inventory"):
        return "structure_parent_origin_ambiguous"
    candidates: list[dict[str, str]] = []
    cross_arm_match = False
    for prior in graph[:step_index]:
        if not isinstance(prior, Mapping):
            continue
        if (type(prior.get("sequence")) is not int
                or type(step.get("sequence")) is not int
                or prior["sequence"] >= step["sequence"]):
            continue
        for output in prior.get("material_outputs", []) or []:
            if not isinstance(output, Mapping):
                continue
            if all(output.get(key) == parent.get(key) for key in (
                "material_id", "material_instance_id", "state", "name",
            )):
                if prior.get("sample_id") != step.get("sample_id"):
                    cross_arm_match = True
                else:
                    candidates.append({
                        "macro_step_id": _text(prior.get("macro_step_id")),
                        "material_instance_id": _text(output.get("material_instance_id")),
                    })
    if cross_arm_match:
        return "structure_parent_upstream_scope_ambiguous"
    if len(candidates) > 1:
        return "structure_parent_upstream_ambiguous"
    if candidates:
        if parent.get("material_origin") == "external_inventory":
            return "structure_parent_origin_conflicts_with_upstream"
        if parent.get("parent_output_refs") not in (None, [], candidates):
            return "structure_parent_upstream_reference_conflict"
        parent["material_origin"] = "upstream_output"
        parent["parent_output_refs"] = candidates
    elif parent.get("material_origin") == "upstream_output":
        return "structure_parent_upstream_reference_missing"
    return ""


def construct_unreviewed_split_transfer_structure(
    proposal: Mapping[str, Any], group: PdfExperimentalGroupV1,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return a detached proposal, construction audit, and abstention reasons.

    Only unsigned proposals are accepted by the caller. An explicit topology
    is never repaired or overwritten. The program may construct child ports
    from one verified parent and source count, or verify the proposed child
    descriptors. Their local symbols receive canonical IDs in the following
    existing ID-generation stage.
    """
    prepared = deepcopy(dict(proposal))
    audit: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    graph, facts, ref = (prepared.get("material_graph"),
                         prepared.get("route_facts"), prepared.get("source_group_ref"))
    if not isinstance(graph, list) or not isinstance(facts, list):
        return prepared, audit, issues
    scope = group.source_scope
    expected = (scope.paper_id, scope.experimental_group_id, scope.source_digest)
    if (not isinstance(ref, Mapping) or tuple(ref.get(key) for key in (
            "paper_id", "experimental_group_id", "source_digest")) != expected):
        return prepared, audit, [_issue("structure_source_group_mismatch")]
    for step_index, raw_step in enumerate(graph):
        if not isinstance(raw_step, dict):
            continue
        operation = _text(raw_step.get("operation"))
        if not _CANDIDATE_OPERATION.search(operation):
            continue
        if not _text(raw_step.get("macro_step_id")):
            issues.append(_issue("structure_step_id_missing", step_index))
            continue
        original_step = deepcopy(raw_step)
        original_facts = deepcopy(facts)
        operation_path = f"material_graph[{step_index}].operation"
        operation_fact = _fact_at(facts, operation_path)
        if (operation_fact is None or operation_fact.get("value") != operation
                or not _bound_fact(operation_fact, group)
                or not _text(operation_fact.get("fact_id"))):
            issues.append(_issue("structure_operation_fact_unbound", step_index))
            continue
        if any(raw_step.get(key) not in (None, [], {}) for key in (
            "operation_segments", "material_relations", "lineage_relation",
        )):
            # Topology asserted by the model may carry scientific meaning.
            # Never reinterpret a malformed edge as a successful edge.
            issues.append(_issue("structure_model_topology_requires_review", step_index))
            continue
        inputs = raw_step.get("material_inputs")
        outputs = raw_step.get("material_outputs")
        if (not isinstance(inputs, list) or not isinstance(outputs, list)
                or len(outputs) > _MAX_CHILDREN):
            issues.append(_issue("structure_participants_missing_or_excessive", step_index))
            continue
        matches: list[tuple[int, dict[str, Any], str]] = []
        for input_index, candidate in enumerate(inputs):
            if not isinstance(candidate, dict):
                continue
            name = _text(candidate.get("name"))
            kinds = [kind for kind in ("split", "transfer")
                     if affirmative_material_operation_span(
                         _text(operation_fact.get("excerpt")), name, kind,
                     ) is not None]
            if len(kinds) == 1:
                matches.append((input_index, candidate, kinds[0]))
        if len(matches) != 1:
            issues.append(_issue("structure_parent_material_ambiguous", step_index))
            continue
        input_index, parent, kind = matches[0]
        parent_name = _text(parent.get("name"))
        parent_id = _text(parent.get("material_instance_id"))
        material_id = _text(parent.get("material_id"))
        parent_state = _text(parent.get("state"))
        parent_name_fact = _fact_at(
            facts, f"material_graph[{step_index}].material_inputs[{input_index}].name",
        )
        parent_state_fact = _fact_at(
            facts, f"material_graph[{step_index}].material_inputs[{input_index}].state",
        )
        if (not all((parent_name, parent_id, material_id, parent_state))
                or parent_name.casefold() in _AMBIGUOUS_PARENT_NAME
                or parent_name_fact is None or parent_state_fact is None
                or parent_name_fact.get("value") != parent_name
                or not _bound_fact(parent_name_fact, group)
                or not _bound_fact(parent_state_fact, group)
                or controlled_state_mapping(
                    f"material_graph[{step_index}].material_inputs[{input_index}].state",
                    parent_state_fact.get("value"), parent_state,
                )[1]
                or not state_source_locally_attributed(
                    parent_state_fact.get("value"),
                    parent_state_fact.get("excerpt"), parent_name,
                )):
            issues.append(_issue("structure_parent_state_or_name_unverified", step_index))
            continue
        excerpt = _text(operation_fact.get("excerpt"))
        if kind == "split":
            count = _split_count_for_parent_operation(excerpt, parent_name)
            if (count is None or count < 2
                    or count > _MAX_CHILDREN
                    or (outputs and count != len(outputs))):
                issues.append(_issue("structure_split_count_or_children_unresolved", step_index))
                continue
            count_fact: Mapping[str, Any] | None = None
            if "count" in raw_step:
                count_path = f"material_graph[{step_index}].count"
                count_fact = _fact_at(facts, count_path)
                if (type(raw_step["count"]) is not int or raw_step["count"] != count
                        or count_fact is None or count_fact.get("value") != count
                        or count_fact.get("unit") != ""
                        or not isinstance(count_fact.get("excerpt"), str)
                        or not _source_scope_ok(count_fact, group)
                        or not _bound_quote(count_fact["excerpt"], group)
                        or not _same_operation_excerpt(
                            count_fact["excerpt"], excerpt,
                        )):
                    issues.append(_issue("structure_split_count_fact_invalid", step_index))
                    continue
        elif len(outputs) > 1:
            issues.append(_issue("structure_transfer_child_ambiguous", step_index))
            continue
        elif "count" in raw_step:
            issues.append(_issue("structure_transfer_count_unsupported", step_index))
            continue
        origin_issue = _set_parent_origin(graph, step_index, parent, raw_step)
        if origin_issue:
            raw_step.clear()
            raw_step.update(original_step)
            issues.append(_issue(origin_issue, step_index))
            continue
        generated_child_count = count if kind == "split" else 1
        field_changes: list[dict[str, Any]] = []
        if not outputs:
            # One source-backed material, a verified affirmative operation,
            # and the exact split cardinality suffice to instantiate opaque
            # child batches. No per-child amount or container is invented.
            for child_index in range(generated_child_count):
                child_path = f"material_graph[{step_index}].material_outputs[{child_index}]"
                child_id = _id(
                    expected, "child_local", _text(raw_step.get("macro_step_id")),
                    child_index,
                )
                name_fact_id = _id(
                    expected, "name_fact", _text(raw_step.get("macro_step_id")),
                    child_index,
                )
                outputs.append({
                    "material_id": material_id,
                    "material_instance_id": child_id,
                    "name": parent_name,
                    "state": parent_state,
                    "provenance": {"kind": "paper", "reference": f"fact:{name_fact_id}"},
                })
                facts.append({
                    "fact_id": name_fact_id,
                    "field_path": child_path + ".name",
                    "value": parent_name,
                    "unit": "",
                    "excerpt": parent_name_fact["excerpt"],
                    "required": True,
                })
                field_changes.append({
                    "field_path": child_path + ".material_instance_id",
                    "from": None, "to": child_id,
                    "basis": "verified_operation_cardinality_and_parent_identity",
                })
            generated_ports = True
        else:
            generated_ports = False
        child_ids: list[str] = []
        child_error = ""
        if generated_ports:
            field_changes.append({
                "field_path": f"material_graph[{step_index}].material_outputs",
                "from": [],
                "to": [port["material_instance_id"] for port in outputs],
                "basis": "verified_operation_cardinality_and_parent_identity",
            })
        for child_index, child in enumerate(outputs):
            if not isinstance(child, dict):
                child_error = "structure_child_port_invalid"
                break
            child_path = f"material_graph[{step_index}].material_outputs[{child_index}]"
            child_id = _text(child.get("material_instance_id"))
            if not child_id:
                child_id = _id(expected, "child_local", _text(raw_step.get("macro_step_id")), child_index)
                child["material_instance_id"] = child_id
                field_changes.append({"field_path": child_path + ".material_instance_id",
                                      "from": None, "to": child_id})
            if (child_id == parent_id or child_id in child_ids
                    or _text(child.get("material_id")) != material_id):
                child_error = "structure_child_identity_conflict"
                break
            child_ids.append(child_id)
            if child.get("quantity") is not None:
                quantity = child.get("quantity")
                unit = quantity.get("unit") if isinstance(quantity, Mapping) else None
                if isinstance(unit, str) and output_quantity_role_issue(
                    child_path + ".quantity.value", graph, unit,
                ):
                    child_error = "structure_child_quantity_role_mismatch"
                    break
            name = _text(child.get("name"))
            name_fact = _fact_at(facts, child_path + ".name")
            if name != parent_name:
                if (kind != "split" or name.casefold() not in _GENERIC_PART_NAME
                        or name_fact is None or name_fact.get("value") != name
                        or not _source_scope_ok(name_fact, group)
                        or not isinstance(name_fact.get("excerpt"), str)
                        or not _bound_quote(name_fact["excerpt"], group)
                        or not _same_operation_excerpt(
                            name_fact["excerpt"], excerpt,
                        )
                        or affirmative_material_operation_span(
                            name_fact["excerpt"], parent_name, "split",
                        ) is None
                        or len(_PART_COUNT.findall(name_fact["excerpt"])) != 1):
                    child_error = "structure_child_material_name_conflict"
                    break
                # A portion is a role, not a new chemical material. Preserve
                # the source-supported parent identity and audit the rewrite.
                old_name_fact = {
                    "fact_id": name_fact.get("fact_id"),
                    "value": name_fact.get("value"),
                    "excerpt": name_fact.get("excerpt"),
                }
                child["name"] = parent_name
                name_fact["value"] = parent_name
                name_fact["excerpt"] = parent_name_fact["excerpt"]
                field_changes.append({"field_path": child_path + ".name",
                                      "from": name, "to": parent_name,
                                      "basis": "same_material_split_role",
                                      "original_fact": old_name_fact,
                                      "produced_fact": {
                                          "fact_id": name_fact.get("fact_id"),
                                          "value": name_fact.get("value"),
                                          "excerpt": name_fact.get("excerpt"),
                                      }})
            if child.get("state") in (None, "", "unknown", "unresolved"):
                field_changes.append({"field_path": child_path + ".state",
                                      "from": child.get("state"), "to": parent_state,
                                      "basis": "same_state_convention_candidate"})
                child["state"] = parent_state
            elif child.get("state") != parent_state:
                child_error = "structure_child_state_conflicts_with_parent"
                break
            state_path = child_path + ".state"
            state_fact = _fact_at(facts, state_path)
            if state_fact is None:
                fact_id = _id(expected, "state_fact", _text(raw_step.get("macro_step_id")), child_index)
                facts.append({
                    "fact_id": fact_id, "field_path": state_path,
                    "value": parent_state, "unit": "", "excerpt": excerpt,
                    "required": True,
                })
                field_changes.append({"field_path": state_path,
                                      "from": None, "to": "operation-bound convention candidate"})
            elif state_fact.get("value") != parent_state:
                child_error = "structure_child_state_fact_conflict"
                break
            else:
                old_excerpt = state_fact.get("excerpt")
                if (not isinstance(old_excerpt, str)
                        or not _source_scope_ok(state_fact, group)
                        or not _bound_quote(old_excerpt, group)
                        or not _same_operation_excerpt(old_excerpt, excerpt)
                        or affirmative_material_operation_span(
                            old_excerpt, parent_name, kind,
                        ) is None):
                    child_error = "structure_child_state_quote_unbound"
                    break
                if old_excerpt != excerpt:
                    state_fact["excerpt"] = excerpt
                    field_changes.append({"field_path": state_path + ".excerpt",
                                          "from": old_excerpt, "to": excerpt,
                                          "basis": "operation_quote_for_convention"})
        if child_error:
            raw_step.clear()
            raw_step.update(original_step)
            facts[:] = original_facts
            issues.append(_issue(child_error, step_index))
            continue
        operation_fact_id = _text(operation_fact.get("fact_id"))
        step_id = _text(raw_step.get("macro_step_id"))
        segment_id = _id(expected, "segment", step_id)
        relation_id = _id(expected, "relation", step_id)
        evidence = {"kind": "paper", "reference": f"fact:{operation_fact_id}"}
        segment = {
            "segment_id": segment_id,
            "material_effect": "split_material" if kind == "split" else "transfer_material",
            "source_operation_ref": f"fact:{operation_fact_id}",
            "provenance": deepcopy(evidence),
        }
        relation = {
            "relation_id": relation_id,
            "event_kind": "split_same_material" if kind == "split" else "process_same_material",
            "input_material_instance_ids": [parent_id],
            "output_material_instance_ids": child_ids,
            "quantity_basis": "runtime_measurement_required",
            "source_operation_ref": segment_id,
            "provenance": deepcopy(evidence),
        }
        lineage = {
            "relation_type": "split_from_parent" if kind == "split" else "transfer_of",
            "parent_material_instance_ids": [parent_id],
            "child_material_instance_ids": child_ids,
        }
        try:
            MaterialOperationSegmentV2.model_validate(segment, strict=True)
            MaterialRelationV2.model_validate(relation, strict=True)
            LineageRelationV2.model_validate(lineage, strict=True)
        except (TypeError, ValueError):
            raw_step.clear()
            raw_step.update(original_step)
            facts[:] = original_facts
            issues.append(_issue("structure_constructed_contract_invalid", step_index))
            continue
        raw_step["operation_segments"] = [segment]
        raw_step["material_relations"] = [relation]
        raw_step["lineage_relation"] = lineage
        # Defensive: rebuild the retained-object resolver LIVE from this
        # group's signed blocks so a composite parent operation elsewhere in
        # the graph resolves exactly as in the formal receipt.
        retained_object_resolver = build_retained_object_resolver(
            graph, facts,
            [(block.locator, block.text) for block in group.blocks],
            [block.locator for block in group.blocks if block.caption],
        )
        proof_issues = [derive_unreviewed_output_state(
            graph, facts,
            f"material_graph[{step_index}].material_outputs[{index}].state",
            paper_id=scope.paper_id,
            experimental_group_id=scope.experimental_group_id,
            source_digest=scope.source_digest,
            retained_object_resolver=retained_object_resolver,
        )[1] for index in range(len(outputs))]
        if any(proof_issues):
            raw_step.clear()
            raw_step.update(original_step)
            facts[:] = original_facts
            issues.append(_issue(
                next(issue for issue in proof_issues if issue), step_index,
            ))
            continue
        if kind == "split" and "count" in raw_step:
            # MacroStepV2 has no count field. The exact source cardinality is
            # now represented by the number of child instances. Preserve the
            # original claim in this unsigned transformation audit, never as
            # an output material amount or allocation.
            asserted_count = raw_step.pop("count")
            asserted_fact = deepcopy(count_fact)
            facts.remove(count_fact)
            field_changes.append({
                "field_path": f"material_graph[{step_index}].count",
                "from": {"value": asserted_count, "fact": asserted_fact},
                "to": {"child_instance_count": len(child_ids)},
                "basis": "source_bound_split_cardinality",
            })
        audit.append({
            "version": "route_pdf_material_structure/v1",
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "source_digest": scope.source_digest,
            "step_index": step_index,
            "operation_fact_id": operation_fact_id,
            "operation_quote": excerpt,
            "parent_material_instance_symbol": parent_id,
            "child_material_instance_symbols": child_ids,
            "event_kind": relation["event_kind"],
            "relation_id": relation_id,
            "segment_id": segment_id,
            "quantity_basis": relation["quantity_basis"],
            "field_changes": field_changes,
        })
    return prepared, audit, issues


__all__ = ["construct_unreviewed_split_transfer_structure"]
