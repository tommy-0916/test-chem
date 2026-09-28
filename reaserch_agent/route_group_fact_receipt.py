"""Produce unsigned literal-fact receipts before independent route review.

The caller must obtain the group inventory from signed PDF enumeration. This
module rechecks only the association's scope and literal excerpts against that
inventory. It neither authenticates PDF acquisition nor decides group roles,
chemical equivalence, capability completeness, or execution authorization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

from chem_agent_contracts.route_field_basis import (
    controlled_state_mapping, is_material_port_state_path,
    state_source_locally_attributed,
)

from .route_group_compiler import (
    _scoped_claim, classify_route_field_basis,
    dimensionless_numeric_field_pending, literal_quantity_present,
    material_identity_for_amount_path,
    quantity_has_local_attribution,
)
from .route_pdf_group_proposals import PdfGroupProposalAssociationResultV1
from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import (
    bind_pdf_quote, normalize_pdf_quote_whitespace,
)


_DIGEST = re.compile(r"sha256_[0-9a-f]{64}\Z")
_SOURCE_KEYS = frozenset({"source_document", "section", "locator", "source_digest"})
_FACT_SOURCE_KEYS = frozenset({
    "paper_id", "experimental_group_id", "section", "locator", "source_digest",
})
_PROTOCOL_KEYS = frozenset({
    "paper_id", "experimental_group_id", "group_role", "role_hint", "source",
    "route_facts", "target", "route_signature", "material_graph",
    "required_capabilities",
})
_FACT_KEYS = frozenset({
    "fact_id", "field_path", "value", "unit", "excerpt", "required", "source",
})


@dataclass(frozen=True)
class PdfGroupFactReceiptBudgetV1:
    max_groups: int = 32
    max_blocks: int = 1024
    max_facts: int = 1024
    max_total_chars: int = 300_000
    max_proposal_chars: int = 300_000

    def __post_init__(self) -> None:
        for name in (
            "max_groups", "max_blocks", "max_facts", "max_total_chars",
            "max_proposal_chars",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class PdfGroupLiteralStatusV1:
    paper_id: str
    experimental_group_id: str
    source_digest: str
    group_locator: str
    role_hint: str
    status: str
    verified_field_paths: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PdfGroupFactReceiptV1:
    schema_version: str
    verification_mode: str
    method: str
    status: str
    reason_codes: tuple[str, ...]
    group_results: tuple[PdfGroupLiteralStatusV1, ...]
    review_work_orders: tuple[dict[str, Any], ...]


def _work_order(
    group: PdfExperimentalGroupV1, *, role_hint: str, status: str,
    field_paths: Sequence[str], reasons: Sequence[str],
) -> dict[str, Any]:
    scope = group.source_scope
    return {
        "schema_version": "pdf_group_independent_review_work_order_v1",
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "section": scope.section,
        "group_locator": scope.locator,
        "source_digest": scope.source_digest,
        "role_hint_untrusted": role_hint,
        "proposed_field_paths": list(field_paths),
        "literal_check_status": status,
        "literal_reason_codes": list(reasons),
        "pending_checks": [
            "trusted_group_role_assignment",
            "trusted_capability_mapping_if_route_group",
            "independent_chemical_semantics_review",
            "execution_authorization_separate",
        ],
        "human_chemical_review_completed": False,
        "execution_authorized": False,
    }


def _blocked(
    reason: str, groups: Sequence[PdfExperimentalGroupV1] = (),
) -> PdfGroupFactReceiptV1:
    group_results = tuple(PdfGroupLiteralStatusV1(
        paper_id=group.source_scope.paper_id,
        experimental_group_id=group.source_scope.experimental_group_id,
        source_digest=group.source_scope.source_digest,
        group_locator=group.source_scope.locator,
        role_hint="", status="blocked", verified_field_paths=(),
        reason_codes=(reason,),
    ) for group in groups)
    return PdfGroupFactReceiptV1(
        schema_version="pdf_group_fact_receipt_v1",
        verification_mode="automated_unsigned",
        method="signed_inventory_scope_and_literal_quote_check_v1",
        status="blocked", reason_codes=(reason,), group_results=group_results,
        review_work_orders=tuple(_work_order(
            group, role_hint="", status="blocked", field_paths=(),
            reasons=(reason,),
        ) for group in groups),
    )


def _key(group: PdfExperimentalGroupV1) -> tuple[str, str, str]:
    scope = group.source_scope
    return scope.paper_id, scope.experimental_group_id, scope.source_digest


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_inventory(
    groups: Sequence[PdfExperimentalGroupV1], budget: PdfGroupFactReceiptBudgetV1,
) -> str:
    if not groups:
        return "enumerated_group_inventory_empty"
    if len(groups) > budget.max_groups:
        return "group_budget_exceeded"
    seen: set[tuple[str, str, str]] = set()
    names: set[tuple[str, str]] = set()
    block_count = 0
    total_chars = 0
    for group in groups:
        if not isinstance(group, PdfExperimentalGroupV1):
            return "enumerated_group_invalid"
        scope = group.source_scope
        key = _key(group)
        if not all(key) or not _DIGEST.fullmatch(scope.source_digest) or (
            not _text(scope.section) or not _text(scope.locator)
            or not _text(group.source_document) or not group.blocks
        ):
            return "enumerated_group_invalid"
        if key in seen or key[:2] in names:
            return "enumerated_group_duplicate"
        seen.add(key)
        names.add(key[:2])
        locators: set[str] = set()
        for block in group.blocks:
            if not _text(block.locator) or not _text(block.text):
                return "enumerated_block_invalid"
            if block.locator in locators:
                return "enumerated_block_duplicate"
            locators.add(block.locator)
            block_count += 1
            total_chars += len(block.text)
            if block_count > budget.max_blocks:
                return "block_budget_exceeded"
            if total_chars > budget.max_total_chars:
                return "source_char_budget_exceeded"
    return ""


def _proposal_key(protocol: Mapping[str, Any]) -> tuple[str, str, str] | None:
    source = protocol.get("source")
    if not isinstance(source, Mapping):
        return None
    key = (
        _text(protocol.get("paper_id")),
        _text(protocol.get("experimental_group_id")),
        _text(source.get("source_digest")),
    )
    return key if all(key) else None


def _literal_fact_reason(
    fact: Mapping[str, Any], group: PdfExperimentalGroupV1,
    blocks: Sequence[tuple[str, str]],
    *, graph: Any, facts: Sequence[Mapping[str, Any]],
) -> str:
    if set(fact) - _FACT_KEYS:
        return "fact_authority_field_forbidden"
    source = fact.get("source")
    if not isinstance(source, Mapping) or set(source) != _FACT_SOURCE_KEYS:
        return "fact_source_scope_invalid"
    scope = group.source_scope
    if any((source.get(name) != expected for name, expected in (
        ("paper_id", scope.paper_id),
        ("experimental_group_id", scope.experimental_group_id),
        ("section", scope.section),
        ("source_digest", scope.source_digest),
    ))):
        return "fact_source_scope_mismatch"
    locator = source.get("locator")
    possible_locators = {
        f"{blocks[first][0].split('-')[0]}-{blocks[last][0].split('-')[-1]}"
        for first in range(len(blocks))
        # Three quoted prose blocks may straddle one parser-marked caption.
        # The binder below still rejects four ordinary prose blocks.
        for last in range(first, min(first + 4, len(blocks)))
    }
    if not isinstance(locator, str) or locator not in possible_locators:
        return "fact_block_outside_group"
    excerpt = fact.get("excerpt")
    binding, quote_issue = bind_pdf_quote(
        blocks, excerpt,
        caption_block_locators={block.locator for block in group.blocks if block.caption},
    )
    if quote_issue:
        return quote_issue
    assert binding is not None
    if locator != binding.locator:
        return "fact_source_locator_mismatch"
    if not _text(fact.get("fact_id")) or not _text(fact.get("field_path")):
        return "fact_identity_missing"
    if classify_route_field_basis(_text(fact.get("field_path"))) == "generated_id":
        return "fact_generated_id_paper_fact_forbidden"
    value = fact.get("value")
    unit = fact.get("unit", "")
    if not isinstance(unit, str):
        return "fact_unit_invalid"
    field_path = _text(fact.get("field_path"))
    if is_material_port_state_path(field_path):
        scoped = _scoped_claim(
            graph if isinstance(graph, list) else [], {}, field_path,
        )
        if scoped is None:
            return "fact_graph_path_missing"
        _mapping, mapping_issue = controlled_state_mapping(
            field_path, value, scoped[0],
        )
        if mapping_issue:
            return mapping_issue
        owner = scoped[1]
        if (not isinstance(owner, Mapping)
                or not state_source_locally_attributed(
                    value, excerpt, owner.get("name"),
                )):
            return "semantic_binding_pending"
    if isinstance(value, bool):
        return "fact_value_type_unverifiable"
    if isinstance(value, (int, float)):
        if not unit.strip():
            return (
                "dimensionless_semantic_pending"
                if dimensionless_numeric_field_pending(field_path, graph)
                else "fact_numeric_unit_missing"
            )
        if not literal_quantity_present(excerpt, value, unit):
            return "fact_quantity_not_in_excerpt"
        identity, identity_required = material_identity_for_amount_path(
            _text(fact.get("field_path")), graph, facts,
        )
        if not quantity_has_local_attribution(
            excerpt, value, unit, identity=identity,
            identity_required=identity_required,
        ):
            return "fact_quantity_attribution_unresolved"
    elif isinstance(value, str):
        if unit.strip():
            return "fact_unit_non_numeric"
        literal = normalize_pdf_quote_whitespace(value)
        normalized_excerpt = normalize_pdf_quote_whitespace(excerpt)
        if not literal or re.search(
            rf"(?<!\w){re.escape(literal)}(?!\w)", normalized_excerpt,
        ) is None:
            return (
                "semantic_binding_pending"
                if classify_route_field_basis(_text(fact.get("field_path")))
                == "controlled_mapping"
                else "fact_value_not_in_excerpt"
            )
    else:
        return "fact_value_type_unverifiable"
    return ""


def produce_pdf_group_fact_receipt(
    enumerated_groups: Sequence[PdfExperimentalGroupV1],
    association: PdfGroupProposalAssociationResultV1 | Sequence[Mapping[str, Any]],
    *,
    signed_inventory_verified: bool,
    budget: PdfGroupFactReceiptBudgetV1 = PdfGroupFactReceiptBudgetV1(),
) -> PdfGroupFactReceiptV1:
    """Check only source-scoped literals and queue all semantic trust decisions.

    ``signed_inventory_verified`` is an assertion by the trusted caller that
    these exact groups came from ``enumerate_attested_pdf_experimental_groups``.
    It does not let this receipt assert human review or sign anything. Every
    enumerated group must have exactly one associated protocol; invalid or
    partial association blocks the whole receipt.
    """
    if not isinstance(budget, PdfGroupFactReceiptBudgetV1):
        raise TypeError("budget must be PdfGroupFactReceiptBudgetV1")
    if signed_inventory_verified is not True:
        return _blocked("signed_group_inventory_not_verified")
    if not isinstance(enumerated_groups, Sequence) or isinstance(
        enumerated_groups, (str, bytes, bytearray)
    ):
        return _blocked("enumerated_group_inventory_invalid")
    inventory_issue = _validate_inventory(enumerated_groups, budget)
    if inventory_issue:
        return _blocked(inventory_issue)
    if isinstance(association, PdfGroupProposalAssociationResultV1):
        if association.diagnostics:
            return _blocked("proposal_association_has_diagnostics", enumerated_groups)
        protocols = association.protocols
    else:
        protocols = association
    if not isinstance(protocols, Sequence) or isinstance(
        protocols, (str, bytes, bytearray)
    ):
        return _blocked("group_proposals_invalid", enumerated_groups)
    if len(protocols) != len(enumerated_groups):
        return _blocked("group_proposal_coverage_incomplete", enumerated_groups)
    try:
        proposal_chars = len(json.dumps(
            protocols, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return _blocked("group_proposals_not_json", enumerated_groups)
    if proposal_chars > budget.max_proposal_chars:
        return _blocked("proposal_char_budget_exceeded", enumerated_groups)
    known = {_key(group): group for group in enumerated_groups}
    proposals: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    total_facts = 0
    for protocol in protocols:
        if not isinstance(protocol, Mapping):
            return _blocked("group_proposal_invalid", enumerated_groups)
        if set(protocol) - _PROTOCOL_KEYS:
            return _blocked("group_proposal_authority_field_forbidden", enumerated_groups)
        key = _proposal_key(protocol)
        if key not in known:
            return _blocked("group_proposal_scope_not_enumerated", enumerated_groups)
        if key in proposals:
            return _blocked("group_proposal_duplicate", enumerated_groups)
        proposals[key] = protocol
        facts = protocol.get("route_facts", [])
        if not isinstance(facts, list):
            return _blocked("group_route_facts_invalid", enumerated_groups)
        total_facts += len(facts)
        if total_facts > budget.max_facts:
            return _blocked("fact_budget_exceeded", enumerated_groups)

    group_results: list[PdfGroupLiteralStatusV1] = []
    work_orders: list[dict[str, Any]] = []
    overall_reasons: list[str] = []
    for group in enumerated_groups:
        scope = group.source_scope
        protocol = proposals[_key(group)]
        source = protocol.get("source")
        reasons: list[str] = []
        if not isinstance(source, Mapping) or set(source) != _SOURCE_KEYS or any((
            source.get(name) != expected for name, expected in (
                ("source_document", group.source_document),
                ("section", scope.section),
                ("locator", scope.locator),
                ("source_digest", scope.source_digest),
            )
        )):
            reasons.append("group_source_scope_mismatch")
        role_hint = protocol.get("role_hint", "")
        if not isinstance(role_hint, str):
            reasons.append("group_role_hint_invalid")
            role_hint = ""
        blocks = [(block.locator, block.text) for block in group.blocks]
        facts = protocol.get("route_facts", [])
        # One quoted evidence item may support several distinct field paths.
        # Reusing its ID is valid only when both its literal quote and source
        # scope are unchanged, matching the route compiler's binding rule.
        fact_ids: dict[str, tuple[str, Any]] = {}
        field_paths: set[str] = set()
        verified_paths: list[str] = []
        for index, fact in enumerate(facts):
            if not isinstance(fact, Mapping):
                reasons.append(f"fact[{index}]:fact_invalid")
                continue
            fact_id = _text(fact.get("fact_id"))
            field_path = _text(fact.get("field_path"))
            identity = (_text(fact.get("excerpt")), fact.get("source"))
            if field_path in field_paths:
                reasons.append(f"fact[{index}]:fact_identity_duplicate")
                continue
            if fact_id in fact_ids and fact_ids[fact_id] != identity:
                reasons.append(f"fact[{index}]:fact_id_conflict")
                continue
            fact_ids[fact_id] = identity
            field_paths.add(field_path)
            reason = _literal_fact_reason(
                fact, group, blocks, graph=protocol.get("material_graph"),
                facts=facts,
            )
            if reason:
                reasons.append(f"fact[{index}]:{reason}")
            else:
                verified_paths.append(field_path)
        if not facts and not reasons:
            status = "no_facts_to_check_pending_role"
        else:
            status = "literal_facts_verified_pending_review" if not reasons else "blocked"
        if reasons:
            overall_reasons.append("group_literal_check_failed")
        group_results.append(PdfGroupLiteralStatusV1(
            paper_id=scope.paper_id,
            experimental_group_id=scope.experimental_group_id,
            source_digest=scope.source_digest,
            group_locator=scope.locator,
            role_hint=role_hint.strip(),
            status=status,
            verified_field_paths=tuple(verified_paths),
            reason_codes=tuple(reasons),
        ))
        work_orders.append(_work_order(
            group, role_hint=role_hint.strip(), status=status,
            field_paths=[
                _text(fact.get("field_path")) for fact in facts
                if isinstance(fact, Mapping)
            ],
            reasons=reasons,
        ))
    return PdfGroupFactReceiptV1(
        schema_version="pdf_group_fact_receipt_v1",
        verification_mode="automated_unsigned",
        method="signed_inventory_scope_and_literal_quote_check_v1",
        status=("blocked" if overall_reasons else (
            "literal_checks_completed_pending_review"
            if any(not protocol.get("route_facts") for protocol in proposals.values())
            else "literal_facts_verified_pending_review"
        )),
        reason_codes=tuple(dict.fromkeys(overall_reasons)),
        group_results=tuple(group_results),
        review_work_orders=tuple(work_orders),
    )


__all__ = [
    "PdfGroupFactReceiptBudgetV1", "PdfGroupLiteralStatusV1",
    "PdfGroupFactReceiptV1", "produce_pdf_group_fact_receipt",
]
