"""Locate source split/transfer mentions and audit unsigned proposal coverage.

An inventory row is a parser observation, not a route or chemical approval.
Only a uniquely bound, affirmative operation on a proposed material can
require graph coverage. This module never constructs material identities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import re
from typing import Any

from chem_agent_contracts.route_field_basis import (
    affirmative_material_operation_span,
)

from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)


_EVENT = re.compile(
    r"\b(?:split|splits|divided\s+into|transferred|transfers|transfer)\b",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"\b(?:split|splits|divided\s+into)\b", re.IGNORECASE)
_TRANSFER = re.compile(
    r"\b(?:transferred|transfers|transfer)\b", re.IGNORECASE,
)
_COUNT = re.compile(
    r"\b([1-9][0-9]*)\s+(?:parts|fractions|portions)\b", re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?;])\s+")
_MAX_BLOCK_WINDOW = 3
_MAX_INVENTORY_ROWS = 64
_CONTAINER_SUBJECT = re.compile(
    r"\b(?:vessel|container|bottle|tube|flask|vial|reactor)\s+"
    r"(?:containing|holding|with)\s+(?:the\s+|a\s+)?$",
    re.IGNORECASE,
)
_SUBJECT_STOPWORDS = frozenset({
    "a", "an", "and", "divided", "into", "of", "split", "splits",
    "the", "then", "to", "transfer", "transferred", "transfers",
    "was", "were", "with",
})


def _scope_key(group: PdfExperimentalGroupV1) -> tuple[str, str, str]:
    scope = group.source_scope
    return scope.paper_id, scope.experimental_group_id, scope.source_digest


def _blocks(group: PdfExperimentalGroupV1) -> list[tuple[str, str]]:
    return [(block.locator, block.text) for block in group.blocks]


def _bound_sentence(
    group: PdfExperimentalGroupV1, sentence: str,
) -> tuple[str, str]:
    binding, issue = bind_pdf_quote(
        _blocks(group), sentence,
        caption_block_locators=[
            block.locator for block in group.blocks if block.caption
        ],
    )
    return (binding.locator, "") if binding is not None else ("", issue)


def _event_count(sentence: str, match: re.Match[str]) -> int | None:
    """Accept only a count that is the split's own ``into N parts`` object."""
    tail = sentence[match.end():match.end() + 80]
    prefix = r"\s*" if match.group().casefold().startswith("divided") else r"\s+into\s+"
    count = re.match(
        prefix + r"([1-9][0-9]*)\s+(?:parts|fractions|portions)\b",
        tail, flags=re.IGNORECASE,
    )
    return int(count.group(1)) if count is not None else None


def inventory_pdf_group_operations(
    groups: Sequence[PdfExperimentalGroupV1],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find uniquely located short source mentions in their fixed PDF groups.

    This is deliberately a bounded lexical inventory. Ambiguous or overlong
    quotes are recorded as inventory issues, never silently given a locator.
    The later audit decides whether the mention describes a proposal material.
    """
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for group in groups:
        scope = group.source_scope
        content = [block for block in group.blocks if not block.caption]
        for start in range(len(content)):
            for width in range(1, _MAX_BLOCK_WINDOW + 1):
                window = content[start:start + width]
                if len(window) != width:
                    break
                # Do not join nonadjacent parser blocks across a caption.
                indexes = [group.blocks.index(block) for block in window]
                if indexes[-1] - indexes[0] != width - 1:
                    continue
                joined = normalize_pdf_quote_whitespace(
                    " ".join(block.text for block in window)
                )
                for sentence in _SENTENCE.split(joined):
                    sentence = sentence.strip()
                    if not sentence or len(sentence) > 1800:
                        continue
                    events = list(_EVENT.finditer(sentence))
                    if not events:
                        continue
                    # Two operations in one sentence may have different
                    # participants or order. A split without an exact source
                    # count cannot instantiate children. Retain both as
                    # explicit unresolved mentions instead of losing them.
                    if len(events) > 1:
                        kind, count = "multiple", None
                        unresolved = "source_operation_multiple_events_unresolved"
                    else:
                        event = events[0]
                        kind = ("split" if _SPLIT.fullmatch(event.group())
                                else "transfer")
                        count = (_event_count(sentence, event)
                                 if kind == "split" else None)
                        unresolved = (
                            "source_operation_count_unresolved"
                            if kind == "split" and count is None else ""
                        )
                    locator, binding_issue = _bound_sentence(group, sentence)
                    key = (*_scope_key(group), kind, sentence)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidate_id = "source_operation_" + sha256(
                        "\0".join((*_scope_key(group), kind, locator, sentence))
                        .encode("utf-8")
                    ).hexdigest()[:24]
                    if binding_issue:
                        issues.append({
                            "paper_id": scope.paper_id,
                            "experimental_group_id": scope.experimental_group_id,
                            "source_digest": scope.source_digest,
                            "reason_code": binding_issue,
                            "candidate_id": candidate_id,
                            "excerpt": sentence,
                        })
                        unresolved = binding_issue
                    rows.append({
                        "candidate_id": candidate_id,
                        "paper_id": scope.paper_id,
                        "experimental_group_id": scope.experimental_group_id,
                        "source_digest": scope.source_digest,
                        "kind": kind,
                        "count": count,
                        "excerpt": sentence,
                        "locator": locator,
                        **({"reason_code": unresolved} if unresolved else {}),
                    })
                    if unresolved:
                        issues.append({
                            "paper_id": scope.paper_id,
                            "experimental_group_id": scope.experimental_group_id,
                            "source_digest": scope.source_digest,
                            "reason_code": unresolved,
                            "candidate_id": candidate_id,
                            "locator": locator,
                        })
                    if len(rows) >= _MAX_INVENTORY_ROWS:
                        issues.append({
                            "paper_id": scope.paper_id,
                            "experimental_group_id": scope.experimental_group_id,
                            "source_digest": scope.source_digest,
                            "reason_code": "source_operation_inventory_budget_exceeded",
                        })
                        return rows, issues
    # A window can prepend a group heading to a complete one-block sentence.
    # Keep the shortest unique quotation for the same event; a genuine event
    # spanning two blocks has no shorter bound sentence and remains intact.
    minimal = [row for row in rows if not any(
        other is not row
        and (row["paper_id"], row["experimental_group_id"], row["source_digest"])
        == (other["paper_id"], other["experimental_group_id"], other["source_digest"])
        and row["kind"] == other["kind"] and row["count"] == other["count"]
        and _has_subject_qualified_event(other)
        and (
            # Remove a heading/context superset only when the shorter quote
            # still carries the material subject.
            (len(other["excerpt"]) < len(row["excerpt"])
             and other["excerpt"] in row["excerpt"])
            or
            # At a layout seam, a shorter verb-only quote can be uniquely
            # located yet cannot establish who was split/transferred.
            (not _has_subject_qualified_event(row)
             and len(other["excerpt"]) > len(row["excerpt"])
             and row["excerpt"] in other["excerpt"])
        )
        for other in rows
    )]
    return minimal, issues


def _port_names(graph: Sequence[Any]) -> set[str]:
    names: set[str] = set()
    for step in graph:
        if not isinstance(step, Mapping):
            continue
        for field in ("material_inputs", "material_intermediates", "material_outputs"):
            ports = step.get(field)
            if not isinstance(ports, list):
                continue
            for port in ports:
                if isinstance(port, Mapping) and isinstance(port.get("name"), str):
                    name = port["name"].strip()
                    if name:
                        names.add(name)
    return names


def _material_subject(excerpt: str, name: str, kind: str) -> bool:
    span = affirmative_material_operation_span(excerpt, name, kind)
    if span is None:
        return False
    clause = re.split(r"[.;:]", excerpt[:span[0]])[-1]
    return _CONTAINER_SUBJECT.search(clause) is None


def _has_subject_qualified_event(candidate: Mapping[str, Any]) -> bool:
    excerpt = candidate.get("excerpt")
    kind = candidate.get("kind")
    if not isinstance(excerpt, str):
        return False
    kinds = ("split", "transfer") if kind == "multiple" else (kind,)
    words = {
        match.group().casefold() for match in re.finditer(r"\b[A-Za-z][\w-]*\b", excerpt)
    } - _SUBJECT_STOPWORDS
    return any(
        _material_subject(excerpt, word, event_kind)
        for word in words for event_kind in kinds
    )


def _operation_fact(
    proposal: Mapping[str, Any], step_index: int, operation: str,
) -> Mapping[str, Any] | None:
    facts = proposal.get("route_facts")
    if not isinstance(facts, list):
        return None
    path = f"material_graph[{step_index}].operation"
    matches = [fact for fact in facts if isinstance(fact, Mapping)
               and fact.get("field_path") == path and fact.get("value") == operation]
    return matches[0] if len(matches) == 1 else None


def _represented(
    proposal: Mapping[str, Any], graph: Sequence[Any],
    candidate: Mapping[str, Any], parent_names: Sequence[str],
    group: PdfExperimentalGroupV1,
) -> bool:
    kind = candidate["kind"]
    candidate_excerpt = normalize_pdf_quote_whitespace(candidate["excerpt"])
    for step_index, step in enumerate(graph):
        if not isinstance(step, Mapping):
            continue
        operation = step.get("operation")
        if not isinstance(operation, str):
            continue
        if kind == "split":
            verb = _SPLIT.search(operation)
            count_match = _COUNT.search(operation)
            if (verb is None or count_match is None
                    or int(count_match.group(1)) != candidate["count"]):
                continue
        elif _TRANSFER.search(operation) is None:
            continue
        ports = step.get("material_inputs")
        if not isinstance(ports, list) or not any(
            isinstance(port, Mapping) and port.get("name") in parent_names
            for port in ports
        ):
            continue
        fact = _operation_fact(proposal, step_index, operation)
        if fact is None or not isinstance(fact.get("excerpt"), str):
            continue
        fact_excerpt = normalize_pdf_quote_whitespace(fact["excerpt"])
        if (not fact_excerpt or not (
            candidate_excerpt in fact_excerpt or fact_excerpt in candidate_excerpt
        )):
            continue
        locator, issue = _bound_sentence(group, fact["excerpt"])
        if not issue and locator and any(
            _material_subject(fact["excerpt"], name, kind)
            for name in parent_names
        ):
            return True
    return False


def audit_unreviewed_operation_coverage(
    proposal: Mapping[str, Any], group: PdfExperimentalGroupV1,
    candidate_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require a source operation on a proposed material to appear in graph.

    Graph output ports are considered: an omitted split may be quoted in the
    step that produces its parent, while the next step consumes that parent.
    Only an actual split/transfer input step represents the source operation.
    """
    graph = proposal.get("material_graph")
    if not isinstance(graph, list) or not graph:
        return [], []
    names = _port_names(graph)
    scope = _scope_key(group)
    audit: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for candidate in candidate_rows:
        if tuple(candidate.get(key) for key in (
            "paper_id", "experimental_group_id", "source_digest",
        )) != scope:
            continue
        excerpt = candidate.get("excerpt")
        kind = candidate.get("kind")
        if not isinstance(excerpt, str) or kind not in {
            "split", "transfer", "multiple",
        }:
            continue
        kinds = ("split", "transfer") if kind == "multiple" else (kind,)
        parent_names = sorted(name for name in names if any(
            _material_subject(excerpt, name, event_kind)
            for event_kind in kinds
        ))
        if not parent_names:
            continue
        unresolved = candidate.get("reason_code")
        represented = not unresolved and _represented(
            proposal, graph, candidate, parent_names, group,
        )
        row = {
            "candidate_id": candidate.get("candidate_id"),
            "paper_id": scope[0],
            "experimental_group_id": scope[1],
            "source_digest": scope[2],
            "kind": kind,
            "count": candidate.get("count"),
            "locator": candidate.get("locator"),
            "parent_names": parent_names,
            "status": ("unresolved" if unresolved else
                       "represented" if represented else "unrepresented"),
        }
        audit.append(row)
        if not represented:
            issues.append({
                **row,
                "reason_code": unresolved or "source_operation_unrepresented",
            })
    return audit, issues


__all__ = [
    "audit_unreviewed_operation_coverage", "inventory_pdf_group_operations",
]
