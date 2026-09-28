"""Per-field, audit-only feedback for an unreviewed PDF proposal batch.

The strict association and literal receipt remain the only admission path.
This module can inspect a partially located batch so a bounded producer can
repair local mistakes without treating its diagnostic output as approval.
The caller must supply the fixed PDF group inventory; this module does not
authenticate its origin or assert that any reviewer approved the chemistry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from chem_agent_contracts.route_field_basis import (
    controlled_state_mapping, is_material_port_state_path,
)

from .route_group_compiler import (
    _numeric_leaves, _required_qualitative_paths, _scoped_claim,
    classify_route_field_basis, material_id_graph_issue,
)
from .route_group_fact_receipt import _literal_fact_reason
from .route_pdf_group_proposals import associate_pdf_group_proposals
from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_locator_production import (
    PdfProposalLocatorProductionV1, produce_pdf_proposal_locators,
)
from .route_pdf_proposal_quality import assess_unreviewed_proposal_literal_shape


@dataclass
class PdfProposalFieldAssessmentV1:
    """Diagnostic work product; ``located`` is not a reviewed protocol."""

    located: PdfProposalLocatorProductionV1
    issues: list[dict[str, Any]] = field(default_factory=list)
    passing_fact_slots: tuple[tuple[int, int], ...] = ()
    group_issue_indexes: tuple[int, ...] = ()
    field_requirements: list[dict[str, Any]] = field(default_factory=list)


def _group_key(group: PdfExperimentalGroupV1) -> tuple[str, str, str]:
    scope = group.source_scope
    return scope.paper_id, scope.experimental_group_id, scope.source_digest


def _proposal_key(proposal: Any) -> tuple[str, str, str] | None:
    if not isinstance(proposal, Mapping):
        return None
    reference = proposal.get("source_group_ref")
    if not isinstance(reference, Mapping) or set(reference) != {
        "paper_id", "experimental_group_id", "source_digest",
    }:
        return None
    key = tuple(reference.get(name) for name in (
        "paper_id", "experimental_group_id", "source_digest",
    ))
    return key if all(isinstance(value, str) and value for value in key) else None


def _fact_identity(proposals: Sequence[Any], proposal_index: int,
                   fact_index: int) -> tuple[str, str]:
    if not 0 <= proposal_index < len(proposals):
        return "", ""
    proposal = proposals[proposal_index]
    if not isinstance(proposal, Mapping):
        return "", ""
    facts = proposal.get("route_facts")
    if not isinstance(facts, list) or not 0 <= fact_index < len(facts):
        return "", ""
    fact = facts[fact_index]
    if not isinstance(fact, Mapping):
        return "", ""
    fact_id, field_path = fact.get("fact_id"), fact.get("field_path")
    return (fact_id if isinstance(fact_id, str) else "",
            field_path if isinstance(field_path, str) else "")


def assess_pdf_group_proposal_fields(
    groups: Sequence[PdfExperimentalGroupV1],
    proposals: Sequence[Mapping[str, Any]],
    *,
    check_required_graph_facts: bool = False,
) -> PdfProposalFieldAssessmentV1:
    """Inspect every fact independently, retaining all original proposal data.

    Successful location is checked against the same private literal predicate
    used by the formal receipt. Unlike a partial receipt, this retains all
    sibling facts when evaluating material quantity attribution. The returned
    status never substitutes for full-batch association and receipt checks.
    """

    located = produce_pdf_proposal_locators(groups, proposals)
    source_proposals = list(proposals)
    result = PdfProposalFieldAssessmentV1(located=located)
    seen: set[tuple[int, int, str, str]] = set()

    def add_issue(proposal_index: int, fact_index: int, reason: str,
                  field_path: str = "") -> None:
        identity = (proposal_index, fact_index, reason, field_path)
        if identity in seen:
            return
        seen.add(identity)
        fact_id, fact_path = _fact_identity(
            source_proposals, proposal_index, fact_index,
        )
        result.issues.append({
            "proposal_index": proposal_index,
            "fact_index": fact_index,
            "fact_id": fact_id,
            "field_path": field_path or fact_path,
            "reason_code": reason,
        })

    located_failures: dict[tuple[int, str], int] = {}
    for record in located.resolutions:
        if record["status"] == "blocked":
            proposal_index = record["proposal_index"]
            reason = record["reason_code"]
            add_issue(proposal_index, record["fact_index"], reason)
            key = (proposal_index, reason)
            located_failures[key] = located_failures.get(key, 0) + 1
    for diagnostic in located.diagnostics:
        key = (diagnostic.proposal_index, diagnostic.reason_code)
        if located_failures.get(key, 0):
            located_failures[key] -= 1
        else:
            add_issue(diagnostic.proposal_index, -1, diagnostic.reason_code)

    for item in assess_unreviewed_proposal_literal_shape(source_proposals):
        add_issue(item["proposal_index"], item["fact_index"],
                  item["reason_code"])

    for proposal_index, proposal in enumerate(source_proposals):
        if not isinstance(proposal, Mapping):
            continue
        facts = proposal.get("route_facts")
        if not isinstance(facts, list):
            continue
        for fact_index, fact in enumerate(facts):
            if (isinstance(fact, Mapping)
                    and classify_route_field_basis(str(fact.get("field_path") or ""))
                    == "generated_id"):
                add_issue(proposal_index, fact_index,
                          "fact_generated_id_paper_fact_forbidden")

    if check_required_graph_facts:
        for proposal_index, proposal in enumerate(source_proposals):
            if not isinstance(proposal, Mapping):
                continue
            facts = proposal.get("route_facts")
            if not isinstance(facts, list):
                continue
            graph = proposal.get("material_graph")
            signature = proposal.get("route_signature")
            graph = graph if isinstance(graph, list) else []
            signature = signature if isinstance(signature, Mapping) else {}
            sig_paths, graph_paths = _required_qualitative_paths(
                graph, signature,
            )
            required_paths = sig_paths | graph_paths | _numeric_leaves(graph)
            fact_paths = {
                fact.get("field_path") for fact in facts
                if isinstance(fact, Mapping)
                and isinstance(fact.get("field_path"), str)
                and fact.get("required") is True
            }
            for path in sorted(required_paths - fact_paths):
                add_issue(proposal_index, -1, "required_graph_fact_missing",
                          path)
            for path in sorted(required_paths):
                result.field_requirements.append({
                    "proposal_index": proposal_index,
                    "field_path": path,
                    "verification_mode": classify_route_field_basis(path),
                    "paper_fact_present": path in fact_paths,
                })
            def record_ids(node: Any, path: str) -> None:
                if isinstance(node, Mapping):
                    for key, child in node.items():
                        child_path = f"{path}.{key}"
                        if classify_route_field_basis(child_path) == "generated_id":
                            result.field_requirements.append({
                                "proposal_index": proposal_index,
                                "field_path": child_path,
                                "verification_mode": "generated_id",
                                "paper_fact_present": child_path in fact_paths,
                            })
                        else:
                            record_ids(child, child_path)
                elif isinstance(node, list):
                    for index, child in enumerate(node):
                        child_path = f"{path}[{index}]"
                        if classify_route_field_basis(child_path) == "generated_id":
                            result.field_requirements.append({
                                "proposal_index": proposal_index,
                                "field_path": child_path,
                                "verification_mode": "generated_id",
                                "paper_fact_present": child_path in fact_paths,
                            })
                        else:
                            record_ids(child, child_path)
            record_ids(graph, "material_graph")
            identity_issue = material_id_graph_issue(graph)
            if identity_issue:
                add_issue(proposal_index, -1, identity_issue)
            for fact_index, fact in enumerate(facts):
                if not isinstance(fact, Mapping):
                    continue
                path = fact.get("field_path")
                if not isinstance(path, str) or not path:
                    continue
                claim = _scoped_claim(graph, signature, path)
                if claim is None:
                    add_issue(proposal_index, fact_index,
                              "fact_graph_path_missing")
                else:
                    actual, _owner, graph_unit = claim
                    fact_unit = fact.get("unit", "")
                    state_match = False
                    if is_material_port_state_path(path):
                        _mapping, state_issue = controlled_state_mapping(
                            path, fact.get("value"), actual,
                        )
                        state_match = not state_issue
                    if ((not state_match and actual != fact.get("value"))
                            or (isinstance(fact_unit, str)
                                and graph_unit != fact_unit.strip())):
                        add_issue(proposal_index, fact_index,
                                  "fact_graph_value_mismatch")

    known = {_group_key(group): group for group in groups}
    locatable_slots: set[tuple[int, int]] = set()
    for record in located.resolutions:
        if record["status"] != "located_unreviewed":
            continue
        proposal_index, fact_index = record["proposal_index"], record["fact_index"]
        if not 0 <= proposal_index < len(source_proposals):
            continue
        proposal = source_proposals[proposal_index]
        group = known.get(_proposal_key(proposal))
        if group is None or not isinstance(proposal, Mapping):
            continue
        facts = proposal.get("route_facts")
        if not isinstance(facts, list) or not 0 <= fact_index < len(facts):
            continue
        fact = facts[fact_index]
        if not isinstance(fact, Mapping):
            continue
        locatable_slots.add((proposal_index, fact_index))
        scope = group.source_scope
        diagnostic_fact = deepcopy(dict(fact))
        diagnostic_fact.pop("block_locator", None)
        diagnostic_fact["source"] = {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "section": scope.section,
            "locator": record["resolved_span"],
            "source_digest": scope.source_digest,
        }
        reason = _literal_fact_reason(
            diagnostic_fact, group,
            [(block.locator, block.text) for block in group.blocks],
            graph=proposal.get("material_graph"), facts=facts,
        )
        if reason:
            add_issue(proposal_index, fact_index, reason)

    # Capture coverage/shape issues from the existing strict association, but
    # never expose its protocols as candidates from this diagnostic function.
    association = associate_pdf_group_proposals(groups, located.proposals)
    quote_reasons = {
        "fact_excerpt_not_in_block", "fact_excerpt_ambiguous_in_group",
        "fact_excerpt_span_too_long", "fact_block_locator_not_in_excerpt_span",
        "fact_block_outside_group",
    }
    locator_failed_groups = {
        record["proposal_index"] for record in located.resolutions
        if record["status"] == "blocked"
    }
    for diagnostic in association.diagnostics:
        # Strict association aborts on the first bad fact in each group. If
        # location already identified that group's bad slots, association's
        # quote error is a derived group-level symptom, not an extra defect.
        if (diagnostic.reason_code in quote_reasons
                and diagnostic.proposal_index in locator_failed_groups):
            continue
        add_issue(diagnostic.proposal_index, -1, diagnostic.reason_code)

    failing_slots = {
        (item["proposal_index"], item["fact_index"])
        for item in result.issues if item["fact_index"] >= 0
    }
    result.passing_fact_slots = tuple(sorted(locatable_slots - failing_slots))
    result.group_issue_indexes = tuple(sorted({
        item["proposal_index"] for item in result.issues
    }))
    return result


__all__ = ["PdfProposalFieldAssessmentV1", "assess_pdf_group_proposal_fields"]
