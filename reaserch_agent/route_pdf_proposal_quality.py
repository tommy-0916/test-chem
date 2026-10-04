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
from chem_agent_contracts.route_convention_basis import (
    derive_unreviewed_input_state, derive_unreviewed_output_state,
)
from chem_agent_contracts.route_retained_object import (
    build_retained_object_resolver,
)

from .route_group_compiler import (
    _scoped_claim, classify_route_field_basis,
    dimensionless_labeled_value_verified, dimensionless_numeric_field_pending,
    literal_quantity_present, output_quantity_role_issue,
)
from .route_pdf_quote_binding import normalize_pdf_quote_whitespace
from .route_state_proof_dag import (
    build_verified_state_proof_dags, dag_entry_derivation, is_input_state_path,
)


def _state_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def assess_unreviewed_proposal_literal_shape(
    proposals: Sequence[Mapping[str, Any]],
    *,
    groups: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Return per-fact shape diagnostics; never mutate an unreviewed proposal.

    This only checks the value, unit, and proposed excerpt. It deliberately
    cannot establish that an excerpt occurs in the signed PDF or belongs to
    the claimed material and experimental group.

    When the signed ``groups`` inventory is supplied, a retained-object
    resolver is rebuilt LIVE from each proposal's signed group blocks and
    threaded into the convention derivations (output state first, then
    parent-output input-state inheritance), so a convention-derived state
    is no longer reported ``semantic_binding_pending`` here.  The same live
    blocks also feed the shared G1 adapter: each proposal's
    state-proof-DAG map is built and dual-verified on the spot, and a
    DAG-proven state (guard-checked via ``dag_entry_derivation``, with the
    input-path value match mirroring the receipt) likewise lifts only the
    state-literal-evidence diagnostics — the independent unit and
    value-type checks below still fire.  Without ``groups`` the behavior is
    byte-identical to before.
    """

    group_by_key: dict[tuple[str, str, str], Any] = {}
    for group in groups or ():
        group_scope = getattr(group, "source_scope", None)
        if group_scope is None or not getattr(group, "blocks", None):
            continue
        key = (
            group_scope.paper_id, group_scope.experimental_group_id,
            group_scope.source_digest,
        )
        if all(isinstance(value, str) and value for value in key):
            group_by_key.setdefault(key, group)
    resolvers: dict[int, Any] = {}

    def _signed_group_for(proposal: Mapping[str, Any]) -> Any:
        if not group_by_key:
            return None
        ref = proposal.get("source_group_ref")
        if not isinstance(ref, Mapping):
            return None
        return group_by_key.get(tuple(
            ref.get(name) for name in (
                "paper_id", "experimental_group_id", "source_digest",
            )
        ))

    def retained_object_resolver_for(
        proposal_index: int, proposal: Mapping[str, Any],
    ) -> Any:
        if not group_by_key:
            return None
        if proposal_index not in resolvers:
            resolver = None
            group = _signed_group_for(proposal)
            graph = proposal.get("material_graph")
            facts = proposal.get("route_facts")
            if (group is not None and isinstance(graph, list)
                    and isinstance(facts, list)):
                resolver = build_retained_object_resolver(
                    graph, facts,
                    [(block.locator, block.text) for block in group.blocks],
                    [block.locator for block in group.blocks
                     if getattr(block, "caption", False)],
                )
            resolvers[proposal_index] = resolver
        return resolvers[proposal_index]

    dag_maps: dict[int, Any] = {}

    def dag_proofs_for(proposal_index: int, proposal: Mapping[str, Any]) -> Any:
        """Per-proposal LIVE G1 state-proof-DAG map from the signed blocks.

        Same construction the receipt and the locator diagnostics consume:
        built and dual-verified here from the group's current signed blocks,
        never from a stored artifact.  ``None`` when no signed group is
        available (byte-identical pre-G1 behavior in that case).
        """
        if proposal_index not in dag_maps:
            dag_proof_map = None
            group = _signed_group_for(proposal)
            graph = proposal.get("material_graph")
            facts = proposal.get("route_facts")
            if (group is not None and isinstance(graph, list)
                    and isinstance(facts, list)):
                scope = group.source_scope
                dag_proof_map = build_verified_state_proof_dags(
                    graph, facts,
                    paper_id=scope.paper_id,
                    experimental_group_id=scope.experimental_group_id,
                    source_digest=scope.source_digest,
                    blocks=[(block.locator, block.text) for block in group.blocks],
                    caption_block_locators=[
                        block.locator for block in group.blocks
                        if getattr(block, "caption", False)
                    ],
                )
            dag_maps[proposal_index] = dag_proof_map
        return dag_maps[proposal_index]

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
            scope_dict = {
                "paper_id": str(scope.get("paper_id") or ""),
                "experimental_group_id": str(scope.get("experimental_group_id") or ""),
                "source_digest": str(scope.get("source_digest") or ""),
            }
            resolver = retained_object_resolver_for(proposal_index, proposal)
            derived, _ = derive_unreviewed_output_state(
                proposal.get("material_graph", []), facts, record["field_path"],
                paper_id=scope_dict["paper_id"],
                experimental_group_id=scope_dict["experimental_group_id"],
                source_digest=scope_dict["source_digest"],
                retained_object_resolver=resolver,
            ) if record["field_path"].endswith(".state") else (None, "")
            if (derived is None and group_by_key
                    and record["field_path"].endswith(".state")):
                # Input/intermediate inheritance is only attempted when the
                # signed groups were supplied; without them the diagnostics
                # are byte-identical to before.
                derived, _inherit_issue = derive_unreviewed_input_state(
                    proposal.get("material_graph", []), facts,
                    record["field_path"], scope_dict,
                    retained_object_resolver=resolver,
                )
            dag_derivation = None
            if derived is None and record["field_path"].endswith(".state"):
                # G1 multi-hop rescue, consulted only after BOTH flat
                # derivations failed.  The guarded derivation lifts only the
                # state-literal-evidence diagnostics below; the independent
                # unit and value-type checks still fire.  Input paths
                # additionally require the fact value to equal the DAG root
                # claim's target state, mirroring the receipt.
                dag_map = dag_proofs_for(proposal_index, proposal)
                if isinstance(dag_map, Mapping):
                    candidate = dag_entry_derivation(
                        dag_map.get(record["field_path"]))
                    if (candidate is not None
                            and is_input_state_path(record["field_path"])
                            and _state_text(value)
                            != _state_text(candidate.get("target_state"))):
                        candidate = None
                    dag_derivation = candidate
            if is_material_port_state_path(record["field_path"]):
                if derived is None and dag_derivation is None:
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
                if derived is None and dag_derivation is None and (
                    not literal or re.search(
                        rf"(?<!\w){re.escape(literal)}(?!\w)", quote,
                    ) is None
                ):
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
