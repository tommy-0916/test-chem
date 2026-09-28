"""Narrow, auditable quotation shortening for unsigned operation proposals.

The parser's unique short source-operation quote may replace an overlong
model quote only when it is literal source text from that same quote. Values,
units, graph topology, fact IDs and scientific claims are never changed.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence

from chem_agent_contracts.route_field_basis import (
    affirmative_material_operation_span,
)

from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)


_OPERATION_PATH = re.compile(r"material_graph\[([0-9]+)\]\.operation\Z")
_SPLIT = re.compile(r"\b(?:split|splits|divided\s+into)\b", re.IGNORECASE)
_TRANSFER = re.compile(r"\b(?:transferred|transfers|transfer)\b", re.IGNORECASE)
_COUNT = re.compile(
    r"\b([1-9][0-9]*)\s+(?:parts|fractions|portions)\b", re.IGNORECASE,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bound_quote(group: PdfExperimentalGroupV1, excerpt: str) -> tuple[str, str]:
    binding, issue = bind_pdf_quote(
        [(block.locator, block.text) for block in group.blocks], excerpt,
        caption_block_locators=[
            block.locator for block in group.blocks if block.caption
        ],
    )
    return (binding.locator, "") if binding is not None else ("", issue)


def _fact_at(facts: Sequence[Any], path: str) -> dict[str, Any] | None:
    matches = [fact for fact in facts if isinstance(fact, dict)
               and fact.get("field_path") == path]
    return matches[0] if len(matches) == 1 else None


def _fact_scope_ok(fact: Mapping[str, Any], group: PdfExperimentalGroupV1) -> bool:
    source = fact.get("source")
    if source is None:
        return True
    scope = group.source_scope
    return isinstance(source, Mapping) and all((
        source.get("paper_id") == scope.paper_id,
        source.get("experimental_group_id") == scope.experimental_group_id,
        source.get("source_digest") == scope.source_digest,
        source.get("section") in (None, scope.section),
    ))


def _literal_value_in_quote(value: Any, unit: Any, quote: str) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    if not isinstance(unit, str):
        return False
    if isinstance(value, str):
        return bool(value.strip() and not unit and re.search(
            rf"(?<!\w){re.escape(value.strip())}(?!\w)", quote,
        ))
    if unit:
        return bool(re.search(
            rf"(?<!\w){re.escape(str(value))}\s*{re.escape(unit)}(?!\w)",
            quote,
        ))
    return bool(re.search(rf"(?<!\w){re.escape(str(value))}(?!\w)", quote))


def tighten_unreviewed_operation_quotes(
    proposal: Mapping[str, Any],
    group: PdfExperimentalGroupV1,
    candidate_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Tighten only operation, one parent name/state, and split-count facts.

    A source candidate does not authorize a route. This transform only changes
    the quotation of an already proposed fact after the original quotation
    uniquely matches the same group but exceeds the layout span limit.
    """
    prepared = deepcopy(dict(proposal))
    audit: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    ref, graph, facts = (prepared.get("source_group_ref"),
                         prepared.get("material_graph"), prepared.get("route_facts"))
    scope = group.source_scope
    scope_key = (scope.paper_id, scope.experimental_group_id, scope.source_digest)
    if (not isinstance(ref, Mapping)
            or tuple(ref.get(key) for key in (
                "paper_id", "experimental_group_id", "source_digest",
            )) != scope_key
            or not isinstance(graph, list) or not isinstance(facts, list)):
        return prepared, audit, issues
    candidates: list[Mapping[str, Any]] = []
    for row in candidate_rows:
        if (not isinstance(row, Mapping)
                or tuple(row.get(key) for key in (
                    "paper_id", "experimental_group_id", "source_digest",
                )) != scope_key):
            continue
        excerpt, kind = row.get("excerpt"), row.get("kind")
        if not isinstance(excerpt, str) or kind not in {"split", "transfer"}:
            continue
        locator, issue = _bound_quote(group, excerpt)
        if issue or locator != row.get("locator"):
            issues.append({
                "candidate_id": row.get("candidate_id"),
                "reason_code": "operation_subquote_not_uniquely_bound",
            })
            continue
        candidates.append(row)
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        operation = _text(step.get("operation"))
        operation_fact = _fact_at(facts, f"material_graph[{step_index}].operation")
        if operation_fact is None or operation_fact.get("value") != operation:
            continue
        matching: list[tuple[Mapping[str, Any], int]] = []
        for candidate in candidates:
            kind = candidate["kind"]
            quote = _text(candidate["excerpt"])
            if (kind == "split" and _SPLIT.search(operation) is None
                    or kind == "transfer" and _TRANSFER.search(operation) is None
                    or not _literal_value_in_quote(operation, "", quote)):
                continue
            inputs = step.get("material_inputs")
            if not isinstance(inputs, list):
                continue
            parent_indexes = [index for index, port in enumerate(inputs)
                              if isinstance(port, Mapping)
                              and affirmative_material_operation_span(
                                  quote, _text(port.get("name")), kind,
                              ) is not None]
            if len(parent_indexes) != 1:
                continue
            if kind == "split":
                counts = [int(item) for item in _COUNT.findall(quote)]
                if len(counts) != 1 or counts[0] != candidate.get("count"):
                    continue
            matching.append((candidate, parent_indexes[0]))
        if len(matching) != 1:
            if len(matching) > 1:
                issues.append({
                    "step_index": step_index,
                    "reason_code": "operation_subquote_event_ambiguous",
                })
            continue
        candidate, parent_index = matching[0]
        quote = _text(candidate["excerpt"])
        kind = candidate["kind"]
        permitted_paths = [
            f"material_graph[{step_index}].operation",
            f"material_graph[{step_index}].material_inputs[{parent_index}].name",
            f"material_graph[{step_index}].material_inputs[{parent_index}].state",
        ]
        if (kind == "split" and type(step.get("count")) is int
                and step.get("count") == candidate.get("count")):
            permitted_paths.append(f"material_graph[{step_index}].count")
        for path in permitted_paths:
            fact = _fact_at(facts, path)
            if fact is None or not _fact_scope_ok(fact, group):
                continue
            if path.endswith(".count") and fact.get("unit") != "":
                # A count is cardinality, not a unit-bearing material amount.
                continue
            original = fact.get("excerpt")
            if not isinstance(original, str) or original == quote:
                continue
            original_normalized = normalize_pdf_quote_whitespace(original)
            quote_normalized = normalize_pdf_quote_whitespace(quote)
            if (not quote_normalized or quote_normalized not in original_normalized
                    or not _literal_value_in_quote(
                        fact.get("value"), fact.get("unit", ""), quote,
                    )):
                continue
            _locator, original_issue = _bound_quote(group, original)
            if original_issue != "fact_excerpt_span_too_long":
                # Existing short quotes need no rewrite. Absent or ambiguous
                # original quotes cannot gain authority from a nearby event.
                continue
            fact["excerpt"] = quote
            audit.append({
                "version": "route_pdf_operation_quote_tightening/v1",
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
                "candidate_id": candidate.get("candidate_id"),
                "candidate_locator": candidate.get("locator"),
                "step_index": step_index,
                "fact_id": fact.get("fact_id"),
                "field_path": path,
                "original_excerpt": original,
                "produced_excerpt": quote,
                "reason_code": "unique_same_event_source_subquote",
            })
    return prepared, audit, issues


__all__ = ["tighten_unreviewed_operation_quotes"]
