"""Value-anchored local-clause quotation shortening for unsigned proposals.

When a fact's excerpt is found uniquely in its exact experimental group but
exceeds the layout span budget, the local clause of that same excerpt that
contains the fact's literal value may become the located excerpt. The local
clause is the same unit the quantity and state attribution checks admit, so
qualifiers inside it (approximation, negation, ordering) are never cut. The
trim is used for location and display only: the fact keeps the full original
quotation as ``verification_excerpt`` for attribution, semantic, and review
checks. A clause is refused when negation or contrast scopes over the value
or when the deleted context carries a retraction, limitation, or hedge, and
when no clause binds uniquely within the budget. Fact values, units,
material identity and graph structure are never modified.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from . import route_pdf_operation_quote_tightening as _operation_tightening
from .route_group_compiler import _LOCAL_CLAUSE
from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import normalize_pdf_quote_whitespace
from .route_pdf_quote_markers import (
    NEGATION_OR_CONTRAST, RETRACTION_LIMITATION,
)


_SENTENCE_ENDINGS = (".", "!", "?", "。")


def _local_clauses(text: str) -> list[tuple[str, int, int]]:
    """Split normalized text into stripped clauses with source offsets."""

    clauses: list[tuple[str, int, int]] = []

    def _append(start: int, end: int) -> None:
        while start < end and text[start] == " ":
            start += 1
        while end > start and text[end - 1] == " ":
            end -= 1
        if end > start:
            clauses.append((text[start:end], start, end))

    cursor = 0
    for match in _LOCAL_CLAUSE.finditer(text):
        _append(cursor, match.start())
        cursor = match.end()
    _append(cursor, len(text))
    return clauses


def _value_match_start(clause: str, value: Any, unit: Any) -> int | None:
    """Start offset of the literal value, mirroring the anchor rule."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if not isinstance(unit, str):
        return None
    if isinstance(value, str):
        if unit or not value.strip():
            return None
        pattern = rf"(?<!\w){re.escape(value.strip())}(?!\w)"
    elif unit:
        pattern = rf"(?<!\w){re.escape(str(value))}\s*{re.escape(unit)}(?!\w)"
    else:
        pattern = rf"(?<!\w){re.escape(str(value))}(?!\w)"
    match = re.search(pattern, clause)
    return match.start() if match is not None else None


def _sentence_start(normalized: str, position: int) -> int:
    base = max(0, position - 300)
    prefix = normalized[base:position]
    return base + max(
        (prefix.rfind(mark) for mark in _SENTENCE_ENDINGS), default=-1,
    ) + 1


def _crosses_negation_or_contrast(
    normalized: str, clause_start: int, value_start: int,
) -> bool:
    """True when negation or contrast scopes over the value in its sentence.

    Markers before the value -- in an earlier clause or earlier in the same
    clause -- would either be cut from the shortened quote or anchor the
    claim on a negated mention, so such clauses are never used.
    """

    window = normalized[_sentence_start(normalized, clause_start):value_start]
    return NEGATION_OR_CONTRAST.search(window) is not None


def tighten_unreviewed_clause_quotes(
    proposal: Mapping[str, Any],
    group: PdfExperimentalGroupV1,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Tighten unique-but-overlong fact quotes to their value-bearing clause.

    Only an excerpt that already binds uniquely in the proposal's own group
    and fails with exactly ``fact_excerpt_span_too_long`` is a candidate. The
    replacement always comes from within the model's original quotation, is
    re-bound by the ordinary literal binder, and leaves every fact value,
    unit, entity and the material graph untouched.
    """

    prepared = deepcopy(dict(proposal))
    audit: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    ref, facts = prepared.get("source_group_ref"), prepared.get("route_facts")
    scope = group.source_scope
    scope_key = (scope.paper_id, scope.experimental_group_id, scope.source_digest)
    if (not isinstance(ref, Mapping)
            or tuple(ref.get(key) for key in (
                "paper_id", "experimental_group_id", "source_digest",
            )) != scope_key
            or not isinstance(facts, list)):
        return prepared, audit, issues
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        original = fact.get("excerpt")
        value, unit = fact.get("value"), fact.get("unit", "")
        if (not isinstance(original, str)
                or not _operation_tightening._fact_scope_ok(fact, group)
                or not _operation_tightening._literal_value_in_quote(
                    value, unit, original,
                )):
            # Without a literal value anchor no shorter quote can support the
            # same field; absent or fabricated values stay with the locator's
            # own diagnosis.
            continue
        _locator, original_issue = _operation_tightening._bound_quote(
            group, original,
        )
        if original_issue != "fact_excerpt_span_too_long":
            # Only the found-unique-but-over-budget case is in scope. Missing
            # or ambiguous originals keep their existing diagnosis, and short
            # quotes need no rewrite.
            continue
        normalized = normalize_pdf_quote_whitespace(original)
        bound: list[tuple[str, str, int, int]] = []
        refused_negation = False
        seen: set[str] = set()
        for clause, start, end in _local_clauses(normalized):
            if (clause in seen
                    or not _operation_tightening._literal_value_in_quote(
                        value, unit, clause,
                    )):
                continue
            seen.add(clause)
            relative_start = _value_match_start(clause, value, unit)
            if (relative_start is None
                    or _crosses_negation_or_contrast(
                        normalized, start, start + relative_start,
                    )):
                refused_negation = True
                continue
            dropped = f"{normalized[:start]}{normalized[end:]}"
            if (RETRACTION_LIMITATION.search(dropped)
                    or NEGATION_OR_CONTRAST.search(dropped)):
                refused_negation = True
                continue
            locator, issue = _operation_tightening._bound_quote(group, clause)
            if not issue:
                bound.append((clause, locator, start, end))
        fact_id = fact.get("fact_id")
        field_path = fact.get("field_path")
        if len(bound) == 1:
            produced, locator, _start, _end = bound[0]
            fact["excerpt"] = produced
            # Location and display use the trimmed clause; verification keeps
            # the full original quotation so downstream checks and reviewers
            # still see the context the trim did not prove irrelevant.
            fact["verification_excerpt"] = original
            audit.append({
                "version": "route_pdf_clause_quote_tightening/v1",
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
                "fact_id": fact_id,
                "field_path": field_path,
                "produced_locator": locator,
                "original_excerpt": original,
                "produced_excerpt": produced,
                "reason_code": "unique_local_clause_source_subquote",
            })
        elif bound:
            issues.append({
                "fact_id": fact_id,
                "field_path": field_path,
                "reason_code": "clause_subquote_ambiguous_in_group",
            })
        elif refused_negation:
            # Cutting the value out of a negated, contrasted, retracted, or
            # hedged sentence would change what the source says; keep the
            # fact pending with the untouched original quotation.
            issues.append({
                "fact_id": fact_id,
                "field_path": field_path,
                "reason_code": "clause_subquote_drops_qualifying_context",
            })
        else:
            # No value-bearing clause fits the budget (for example a clause
            # the PDF split across too many blocks). The fact stays pending
            # with the locator's own span diagnosis rather than losing
            # necessary context to force a pass.
            issues.append({
                "fact_id": fact_id,
                "field_path": field_path,
                "reason_code": "clause_subquote_not_uniquely_bound",
            })
    return prepared, audit, issues


__all__ = ["tighten_unreviewed_clause_quotes"]
