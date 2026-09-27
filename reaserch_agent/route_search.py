"""Budgeted, field-specific evidence acquisition for route decisions.

Acquisition only proposes more experimental groups.  Every round is evaluated
again by the trusted source/science/device pipeline; search results and model
claims never become verified facts by themselves.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from pydantic import Field

from chem_agent_contracts.route_candidate import RouteGoalV1
from chem_agent_contracts.route_decision import (
    RouteSearchBudgetV1,
    RouteSearchRoundV1,
    targeted_search_status,
)
from chem_agent_contracts.v2 import StrictModel, canonical_digest

from .route_pipeline import RoutePipelineResultV1
from .tools.query_sanitizer import sanitize_search_queries


@dataclass(frozen=True)
class RouteSearchAcquisitionV1:
    """A retrieval/extraction adapter's proposals and the distinct papers read."""

    protocols: tuple[Mapping[str, Any], ...] = ()
    paper_ids: tuple[str, ...] = ()


class RouteSearchTraceRoundV1(StrictModel):
    queries: dict[str, str] = Field(default_factory=dict)
    candidate_paper_ids: list[str] = Field(default_factory=list)
    budget_round: RouteSearchRoundV1
    decision_id: str


class RouteSearchResultV1(StrictModel):
    pipeline: RoutePipelineResultV1
    rounds: list[RouteSearchTraceRoundV1] = Field(default_factory=list)
    stop_reason: str = Field(min_length=1)


def verified_route_fact_ids(result: RoutePipelineResultV1) -> set[str]:
    """Count only source-verified fields also inspected by the science gate."""

    fact_ids: set[str] = set()
    for record in result.decision.candidates:
        receipt = record.validation
        if receipt is None or not receipt.source_scope_verified:
            continue
        verified_paths = set(receipt.verified_field_paths) & set(receipt.audited_field_paths)
        verified_evidence = set(receipt.verified_evidence_ids)
        for field in record.candidate.evidence_matrix:
            if (
                field.status != "supported"
                or field.field_path not in verified_paths
                or field.evidence_id not in verified_evidence
                or field.provenance is None
                or field.provenance.kind != "paper"
            ):
                continue
            fact_ids.add(canonical_digest({
                "source_scope": field.source_scope,
                "field_path": field.field_path,
                "value": field.value,
                "unit": field.unit,
                "evidence_id": field.evidence_id,
                "excerpt_digest": field.provenance.source_digest,
            }))
    return fact_ids


def missing_required_route_fields(
    goal: RouteGoalV1, result: RoutePipelineResultV1,
) -> list[str]:
    """List fields no candidate has both source and scientific verification for."""

    covered: set[str] = set()
    for record in result.decision.candidates:
        receipt = record.validation
        if receipt is None or not receipt.source_scope_verified:
            continue
        candidate = record.candidate
        if (
            candidate.target.material.casefold() != goal.target.material.casefold()
            or candidate.target.desired_state != goal.target.desired_state
        ):
            continue
        if goal.constraint == "locked_family" and (
            receipt.source_route_signature is None
            or receipt.source_route_signature.route_family != goal.locked_family
        ):
            continue
        if goal.constraint == "locked_experimental_group" and (
            candidate.source_scope is None or goal.locked_scope is None
            or candidate.source_scope.paper_id != goal.locked_scope.paper_id
            or candidate.source_scope.experimental_group_id
            != goal.locked_scope.experimental_group_id
        ):
            continue
        checked = set(receipt.verified_field_paths) & set(receipt.audited_field_paths)
        covered.update(
            field.field_path for field in candidate.evidence_matrix
            if field.field_path in checked and field.status == "supported"
            and field.evidence_id in receipt.verified_evidence_ids
        )
    return sorted(set(goal.required_fields) - covered)


def _queries_for_missing_fields(
    goal: RouteGoalV1,
    missing: Sequence[str],
    completed: Sequence[RouteSearchRoundV1],
    budget: RouteSearchBudgetV1,
) -> dict[str, str]:
    counts: Counter[str] = Counter()
    for round_ in completed:
        counts.update(round_.queries_by_field)
    family = goal.locked_family if goal.constraint == "locked_family" else ""
    queries: dict[str, str] = {}
    for field in missing:
        if counts[field] >= budget.max_queries_per_missing_field:
            continue
        proposed = " ".join(filter(None, (
            goal.target.material, family, goal.target.objective,
            "experimental procedure", field,
        )))
        sanitized = sanitize_search_queries([proposed])
        if sanitized:
            queries[field] = sanitized[0]
    return queries


def _blocker_signature(result: RoutePipelineResultV1) -> str:
    return canonical_digest({
        "decision_reasons": result.decision.decision_reasons,
        "candidate_reasons": sorted(
            (record.status, tuple(sorted(record.reasons)))
            for record in result.decision.candidates
        ),
        "discovery_reasons": sorted(
            item.reason_code for item in result.discovery.diagnostics
            if item.status == "unresolved"
        ),
    })


def search_route_evidence_v1(
    goal: RouteGoalV1,
    initial_protocols: Sequence[Mapping[str, Any]],
    *,
    evaluate: Callable[[Sequence[Mapping[str, Any]]], RoutePipelineResultV1],
    acquire: Callable[[Mapping[str, str], int], RouteSearchAcquisitionV1],
    budget: RouteSearchBudgetV1 | None = None,
) -> RouteSearchResultV1:
    """Acquire at most the configured number of papers and verified rounds.

    ``acquire`` must be an internal adapter that executes bounded retrieval and
    experimental-group extraction. Its output is untrusted proposal data. The
    ``evaluate`` callback must invoke the trusted route pipeline after each
    round, using a fresh source registry. Errors stop the loop and are distinct
    from missing scientific evidence.
    """

    limit = budget or RouteSearchBudgetV1()
    protocols = list(initial_protocols)
    current = evaluate(protocols)
    completed: list[RouteSearchRoundV1] = []
    trace: list[RouteSearchTraceRoundV1] = []
    seen_facts = verified_route_fact_ids(current)
    seen_blockers = {_blocker_signature(current)}
    while True:
        if current.decision.status != "unresolved":
            reason = "decision_reached"
            break
        missing = missing_required_route_fields(goal, current)
        status = targeted_search_status(limit, completed, missing)
        if status != "continue":
            reason = status
            break
        queries = _queries_for_missing_fields(goal, missing, completed, limit)
        if not queries:
            reason = "query_budget_exhausted"
            break
        remaining_papers = limit.max_candidate_papers - sum(
            round_.candidate_papers_seen for round_ in completed
        )
        if remaining_papers <= 0:
            reason = "paper_budget_exhausted"
            break
        try:
            batch = acquire(queries, remaining_papers)
            if not isinstance(batch, RouteSearchAcquisitionV1):
                raise TypeError("acquisition did not return RouteSearchAcquisitionV1")
            papers = tuple(dict.fromkeys(batch.paper_ids))
            if (
                len(papers) > remaining_papers
                or any(not isinstance(paper, str) or not paper.strip() for paper in papers)
            ):
                raise ValueError("acquisition paper budget or identity invalid")
            if batch.protocols and not papers:
                raise ValueError("acquisition protocols have no paper identities")
            if any(
                not isinstance(protocol, Mapping)
                or not isinstance(protocol.get("paper_id"), str)
                or protocol["paper_id"] not in papers
                for protocol in batch.protocols
            ):
                raise ValueError("acquisition protocol identity not counted in paper budget")
            proposed = protocols + list(batch.protocols)
            updated = evaluate(proposed)
        except Exception:
            reason = "search_service_or_extraction_failure"
            break
        new_facts = verified_route_fact_ids(updated) - seen_facts
        round_ = RouteSearchRoundV1(
            queries_by_field={field: 1 for field in queries},
            candidate_papers_seen=len(papers),
            new_verified_fact_ids=sorted(new_facts),
        )
        completed.append(round_)
        trace.append(RouteSearchTraceRoundV1(
            queries=queries,
            candidate_paper_ids=list(papers),
            budget_round=round_,
            decision_id=updated.decision.decision_id,
        ))
        protocols = proposed
        current = updated
        seen_facts.update(new_facts)
        if current.decision.status == "unresolved" and not new_facts:
            reason = "no_new_verified_facts"
            break
        blocker = _blocker_signature(current)
        if blocker in seen_blockers:
            reason = "blocker_signature_repeated"
            break
        seen_blockers.add(blocker)
    return RouteSearchResultV1(pipeline=current, rounds=trace, stop_reason=reason)


__all__ = [
    "RouteSearchAcquisitionV1", "RouteSearchTraceRoundV1", "RouteSearchResultV1",
    "verified_route_fact_ids", "missing_required_route_fields",
    "search_route_evidence_v1",
]
