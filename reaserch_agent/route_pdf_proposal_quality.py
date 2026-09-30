"""Audit the literal shape of unreviewed PDF facts without changing them.

These diagnostics help a bounded proposal producer identify fields that cannot
pass the existing literal receipt. They are not source verification, chemical
review, or route admission: quote scope, attribution, graph equality, and
publication remain the responsibility of their existing gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from chem_agent_contracts.route_field_basis import (
    controlled_state_mapping, is_material_port_state_path,
    output_state_parent_role_issue,
)
from chem_agent_contracts.route_convention_basis import derive_unreviewed_output_state

from .route_group_compiler import (
    _scoped_claim, classify_route_field_basis,
    dimensionless_labeled_value_verified, dimensionless_numeric_field_pending,
    literal_quantity_present, output_quantity_role_issue,
)
from .route_pdf_quote_binding import normalize_pdf_quote_whitespace


def assess_unreviewed_proposal_literal_shape(
    proposals: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return per-fact shape diagnostics; never mutate an unreviewed proposal.

    This only checks the value, unit, and proposed excerpt. It deliberately
    cannot establish that an excerpt occurs in the signed PDF or belongs to
    the claimed material and experimental group.
    """

    diagnostics: list[dict[str, Any]] = []
    for proposal_index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            diagnostics.append({
                "proposal_index": proposal_index, "fact_index": -1,
                "fact_id": "", "field_path": "",
                "reason_code": "proposal_invalid",
            })
            continue
        facts = proposal.get("route_facts", [])
        if not isinstance(facts, list):
            diagnostics.append({
                "proposal_index": proposal_index, "fact_index": -1,
                "fact_id": "", "field_path": "",
                "reason_code": "proposal_route_facts_invalid",
            })
            continue
        for fact_index, fact in enumerate(facts):
            fact_id = fact.get("fact_id") if isinstance(fact, Mapping) else None
            field_path = fact.get("field_path") if isinstance(fact, Mapping) else None
            record = {
                "proposal_index": proposal_index,
                "fact_index": fact_index,
                "fact_id": fact_id if isinstance(fact_id, str) else "",
                "field_path": field_path if isinstance(field_path, str) else "",
            }
            if not isinstance(fact, Mapping):
                diagnostics.append({**record, "reason_code": "proposal_route_fact_invalid"})
                continue
            if fact.get("required") is not True:
                diagnostics.append({
                    **record, "reason_code": "fact_required_must_be_true",
                })
            value = fact.get("value")
            unit = fact.get("unit", "")
            excerpt = fact.get("excerpt")
            scope = proposal.get("source_group_ref")
            scope = scope if isinstance(scope, Mapping) else {}
            derived, _ = derive_unreviewed_output_state(
                proposal.get("material_graph", []), facts, record["field_path"],
                paper_id=str(scope.get("paper_id") or ""),
                experimental_group_id=str(scope.get("experimental_group_id") or ""),
                source_digest=str(scope.get("source_digest") or ""),
            ) if record["field_path"].endswith(".state") else (None, "")
            if is_material_port_state_path(record["field_path"]):
                if derived is None:
                    graph = proposal.get("material_graph")
                    if output_state_parent_role_issue(
                        record["field_path"], graph, value, excerpt,
                    ):
                        diagnostics.append({
                            **record, "reason_code": "parent_state_not_child_evidence",
                        })
                    scoped = _scoped_claim(
                        graph if isinstance(graph, list) else [], {},
                        record["field_path"],
                    )
                    _mapping, issue = controlled_state_mapping(
                        record["field_path"], value,
                        scoped[0] if scoped is not None else None,
                    )
                    if issue:
                        diagnostics.append({**record, "reason_code": issue})
            if not isinstance(unit, str):
                diagnostics.append({**record, "reason_code": "fact_unit_invalid"})
            if isinstance(value, bool):
                diagnostics.append({
                    **record, "reason_code": "fact_value_type_unverifiable",
                })
            elif isinstance(value, (int, float)):
                if isinstance(unit, str):
                    if output_quantity_role_issue(
                            record["field_path"], proposal.get("material_graph"),
                            unit):
                        reason = "fact_quantity_role_mismatch"
                    elif not unit.strip():
                        if not dimensionless_numeric_field_pending(
                                record["field_path"],
                                proposal.get("material_graph")):
                            reason = "fact_numeric_unit_missing"
                        else:
                            reason = (
                                ""
                                if dimensionless_labeled_value_verified(
                                    value, excerpt, record["field_path"],
                                    proposal.get("material_graph"))
                                else "dimensionless_semantic_pending"
                            )
                    elif not literal_quantity_present(excerpt, value, unit):
                        reason = "fact_quantity_not_in_excerpt"
                    else:
                        reason = ""
                    if reason:
                        diagnostics.append({**record, "reason_code": reason})
            elif isinstance(value, str):
                if isinstance(unit, str) and unit.strip():
                    diagnostics.append({
                        **record, "reason_code": "fact_unit_non_numeric",
                    })
                literal = normalize_pdf_quote_whitespace(value)
                quote = (
                    normalize_pdf_quote_whitespace(excerpt)
                    if isinstance(excerpt, str) else ""
                )
                if derived is None and (not literal or re.search(
                    rf"(?<!\w){re.escape(literal)}(?!\w)", quote,
                ) is None):
                    diagnostics.append({
                        **record,
                        "reason_code": (
                            "semantic_binding_pending"
                            if classify_route_field_basis(record["field_path"])
                            == "controlled_mapping"
                            else "fact_value_not_in_excerpt"
                        ),
                    })
            else:
                diagnostics.append({
                    **record, "reason_code": "fact_value_type_unverifiable",
                })
    return diagnostics


__all__ = ["assess_unreviewed_proposal_literal_shape"]
