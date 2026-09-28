"""Join source-enumerated PDF groups to untrusted per-group proposals.

The trusted group inventory provides every source identity and locator. A
proposal may identify one enumerated group and suggest scientific structure
plus exact, short layout-block quotations. It cannot provide or override source scope,
digest, evidence status, group role, or a claim about source authenticity.
This is a pure association step, not chemistry interpretation or candidate
admission.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .route_pdf_groups import PdfExperimentalGroupV1
from .route_pdf_quote_binding import bind_pdf_quote
from .route_pdf_verification_context import verification_context_reason


_ALLOWED_PROPOSAL_KEYS = frozenset({
    "source_group_ref", "role_hint", "target", "route_signature",
    "material_graph", "route_facts",
})
_ALLOWED_FACT_KEYS = frozenset({
    "fact_id", "field_path", "value", "unit", "excerpt", "block_locator",
    "required", "verification_excerpt",
})
_SOURCE_REF_KEYS = frozenset({
    "paper_id", "experimental_group_id", "source_digest",
})
_REVIEWED_GROUP_ROLES = frozenset({
    "synthesis", "material_processing", "characterization", "testing",
    "performance_testing", "non_procedural",
})
_ROUTE_GROUP_ROLES = frozenset({"synthesis", "material_processing"})


@dataclass(frozen=True)
class PdfGroupProposalDiagnosticV1:
    reason_code: str
    paper_id: str = ""
    experimental_group_id: str = ""
    proposal_index: int = -1


@dataclass
class PdfGroupProposalAssociationResultV1:
    protocols: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[PdfGroupProposalDiagnosticV1] = field(default_factory=list)
    # Producer-only, unsigned audit data. This is never a reviewed protocol.
    locator_production: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identity(value: Mapping[str, Any]) -> tuple[str, str, str] | None:
    if set(value) != _SOURCE_REF_KEYS:
        return None
    identity = (
        _text(value.get("paper_id")),
        _text(value.get("experimental_group_id")),
        _text(value.get("source_digest")),
    )
    return identity if all(identity) else None


def associate_pdf_group_proposals(
    enumerated_groups: Sequence[PdfExperimentalGroupV1],
    proposals: Sequence[Mapping[str, Any]],
    *,
    required_capabilities_by_group: Mapping[
        tuple[str, str, str], Sequence[str]
    ] | None = None,
    group_roles_by_group: Mapping[
        tuple[str, str, str], str
    ] | None = None,
) -> PdfGroupProposalAssociationResultV1:
    """Build protocol proposals only when every known group is accounted for.

    A caller must supply groups from the attested PDF enumeration boundary.
    This pure function cannot itself authenticate a ``PdfExperimentalGroupV1``
    object or approve its proposed chemistry. It deliberately makes no
    partial batch when group coverage or quote binding is ambiguous. The two
    optional maps must come from independent reviewed code/data, never model
    output; without a group-role mapping Discovery sees ``unclassified`` and
    remains unresolved.
    """
    result = PdfGroupProposalAssociationResultV1()
    known: dict[tuple[str, str, str], PdfExperimentalGroupV1] = {}
    for group in enumerated_groups:
        if not isinstance(group, PdfExperimentalGroupV1):
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                reason_code="enumerated_group_invalid",
            ))
            continue
        scope = group.source_scope
        key = (scope.paper_id, scope.experimental_group_id, scope.source_digest)
        if key in known:
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                reason_code="enumerated_group_duplicate",
                paper_id=scope.paper_id,
                experimental_group_id=scope.experimental_group_id,
            ))
        known[key] = group
    if not known:
        result.diagnostics.append(PdfGroupProposalDiagnosticV1(
            reason_code="enumerated_group_inventory_empty",
        ))
    matched: set[tuple[str, str, str]] = set()
    prepared: dict[tuple[str, str, str], dict[str, Any]] = {}
    for proposal_index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                reason_code="proposal_invalid", proposal_index=proposal_index,
            ))
            continue
        source_ref = proposal.get("source_group_ref")
        key = _identity(source_ref) if isinstance(source_ref, Mapping) else None
        if key is None:
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                reason_code="proposal_group_ref_invalid",
                proposal_index=proposal_index,
            ))
            continue
        paper_id, group_id, _digest = key

        def diagnose(reason_code: str) -> None:
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                reason_code=reason_code,
                paper_id=paper_id,
                experimental_group_id=group_id,
                proposal_index=proposal_index,
            ))

        if set(proposal) - _ALLOWED_PROPOSAL_KEYS:
            diagnose("proposal_source_or_status_field_forbidden")
            continue
        group = known.get(key)
        if group is None:
            diagnose("proposal_group_not_enumerated")
            continue
        if key in matched:
            diagnose("duplicate_group_proposal")
            continue
        matched.add(key)
        group_role = (
            group_roles_by_group.get(key, "unclassified")
            if group_roles_by_group is not None else "unclassified"
        )
        if group_role not in _REVIEWED_GROUP_ROLES | {"unclassified"}:
            diagnose("trusted_group_role_invalid")
            continue
        role_hint = proposal.get("role_hint", "")
        if not isinstance(role_hint, str):
            diagnose("proposal_role_hint_invalid")
            continue
        facts = proposal.get("route_facts", [])
        if not isinstance(facts, list):
            diagnose("proposal_route_facts_invalid")
            continue
        blocks = [(block.locator, block.text) for block in group.blocks]
        caption_locators = {
            block.locator for block in group.blocks if block.caption
        }
        prepared_facts: list[dict[str, Any]] = []
        fact_issue = ""
        for raw_fact in facts:
            if not isinstance(raw_fact, Mapping):
                fact_issue = "proposal_route_fact_invalid"
                break
            if set(raw_fact) - _ALLOWED_FACT_KEYS:
                fact_issue = "proposal_fact_source_or_status_field_forbidden"
                break
            locator = _text(raw_fact.get("block_locator"))
            excerpt = raw_fact.get("excerpt")
            binding, fact_issue = bind_pdf_quote(
                blocks, excerpt, asserted_block_locator=locator,
                caption_block_locators=caption_locators,
            )
            if fact_issue:
                break
            # The full verification context must bind to this same group and
            # contain the located excerpt before any downstream check may
            # trust it; a forged or foreign context blocks the proposal here.
            context_issue = verification_context_reason(
                raw_fact, blocks, caption_locators,
            )
            if context_issue:
                fact_issue = f"proposal_{context_issue}"
                break
            assert binding is not None
            prepared_fact = deepcopy(dict(raw_fact))
            del prepared_fact["block_locator"]
            prepared_fact["source"] = {
                "paper_id": paper_id,
                "experimental_group_id": group_id,
                "section": group.source_scope.section,
                "locator": binding.locator,
                "source_digest": group.source_scope.source_digest,
            }
            prepared_facts.append(prepared_fact)
        if fact_issue:
            diagnose(fact_issue)
            continue

        protocol: dict[str, Any] = {
            "paper_id": paper_id,
            "experimental_group_id": group_id,
            "group_role": group_role,
            "role_hint": role_hint.strip(),
            "source": {
                "source_document": group.source_document,
                "section": group.source_scope.section,
                "locator": group.source_scope.locator,
                "source_digest": group.source_scope.source_digest,
            },
        }
        # Keep a reviewed non-route group in the inventory for coverage and
        # role audit, but do not ask the route-fact compiler to compile it.
        # An empty fact list is correct for a reviewed non-route or context
        # section, whereas it is unresolved extraction for a synthesis group.
        if group_role in _ROUTE_GROUP_ROLES or group_role == "unclassified":
            protocol["route_facts"] = prepared_facts
        for key_name in (
            "target", "route_signature", "material_graph",
        ):
            if key_name in proposal:
                protocol[key_name] = deepcopy(proposal[key_name])
        if required_capabilities_by_group is not None and key in required_capabilities_by_group:
            capabilities = required_capabilities_by_group[key]
            if (
                isinstance(capabilities, (str, bytes))
                or not isinstance(capabilities, Sequence)
                or any(not isinstance(item, str) or not item.strip()
                       for item in capabilities)
            ):
                diagnose("trusted_capability_mapping_invalid")
                continue
            protocol["required_capabilities"] = list(capabilities)
        prepared[key] = protocol

    for key, group in known.items():
        if key not in matched:
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                reason_code="enumerated_group_proposal_missing",
                paper_id=group.source_scope.paper_id,
                experimental_group_id=group.source_scope.experimental_group_id,
            ))
    if not result.diagnostics:
        result.protocols = [prepared[key] for key in known]
    return result


__all__ = [
    "PdfGroupProposalDiagnosticV1",
    "PdfGroupProposalAssociationResultV1",
    "associate_pdf_group_proposals",
]
