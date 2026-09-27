"""Compute unreviewed proposal coordinates from the enumerated PDF group.

The model's block locator is retained as a hint for audit, never used as a
source coordinate. This step copies proposals and changes only that locator.
It cannot approve a fact, a route, a review, or a published package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any

from .route_pdf_group_proposals import PdfGroupProposalDiagnosticV1
from .route_pdf_groups import PdfExperimentalGroupV1, PDF_GROUP_PARSER_VERSION
from .route_pdf_quote_binding import bind_pdf_quote, PDF_QUOTE_BINDING_VERSION


PDF_LOCATOR_PRODUCTION_VERSION = "pdf_proposal_locator/v1"


@dataclass
class PdfProposalLocatorProductionV1:
    proposals: list[Any] = field(default_factory=list)
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[PdfGroupProposalDiagnosticV1] = field(default_factory=list)
    raw_proposals_digest: str = ""
    located_proposals_digest: str = ""
    parser_version: str = PDF_GROUP_PARSER_VERSION
    binding_version: str = PDF_QUOTE_BINDING_VERSION
    production_version: str = PDF_LOCATOR_PRODUCTION_VERSION

    def audit_artifact(self) -> dict[str, Any]:
        """Serializable work product; never a route admission credential."""

        return {
            "status": "blocked" if self.diagnostics else "located_unreviewed",
            "located_proposals": deepcopy(self.proposals),
            "field_records": deepcopy(self.resolutions),
            "diagnostics": [vars(item).copy() for item in self.diagnostics],
            "raw_proposals_digest": self.raw_proposals_digest,
            "located_proposals_digest": self.located_proposals_digest,
            "parser_version": self.parser_version,
            "binding_version": self.binding_version,
            "production_version": self.production_version,
        }


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256_" + sha256(encoded).hexdigest()


def _group_key(group: PdfExperimentalGroupV1) -> tuple[str, str, str]:
    scope = group.source_scope
    return scope.paper_id, scope.experimental_group_id, scope.source_digest


def _proposal_key(proposal: Mapping[str, Any]) -> tuple[str, str, str] | None:
    source_ref = proposal.get("source_group_ref")
    if not isinstance(source_ref, Mapping) or set(source_ref) != {
        "paper_id", "experimental_group_id", "source_digest"
    }:
        return None
    values = tuple(source_ref.get(name) for name in (
        "paper_id", "experimental_group_id", "source_digest"
    ))
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values  # type: ignore[return-value]


def _group_layout_digest(
    group: PdfExperimentalGroupV1, *, parser_version: str,
    binding_version: str,
) -> str:
    scope = group.source_scope
    return _digest({
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "source_digest": scope.source_digest,
        "section": scope.section,
        "group_locator": scope.locator,
        "parser_version": parser_version,
        "binding_version": binding_version,
        "blocks": [
            {"locator": block.locator, "text": block.text, "caption": block.caption}
            for block in group.blocks
        ],
    })


def produce_pdf_proposal_locators(
    groups: Sequence[PdfExperimentalGroupV1],
    raw_proposals: Sequence[Mapping[str, Any]],
    *,
    parser_version: str = PDF_GROUP_PARSER_VERSION,
    binding_version: str = PDF_QUOTE_BINDING_VERSION,
) -> PdfProposalLocatorProductionV1:
    """Locate literal excerpts uniquely within each proposal's exact group.

    Callers may pass the returned proposals to strict association only when
    ``diagnostics`` is empty. Partial copies and records remain audit data.
    """

    if not parser_version or not binding_version:
        raise ValueError("parser_version and binding_version must be nonempty")
    result = PdfProposalLocatorProductionV1(
        proposals=deepcopy(list(raw_proposals)),
        parser_version=parser_version,
        binding_version=binding_version,
    )
    result.raw_proposals_digest = _digest(raw_proposals)
    known = {_group_key(group): group for group in groups}
    for proposal_index, proposal in enumerate(result.proposals):
        if not isinstance(proposal, Mapping):
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                "proposal_invalid", proposal_index=proposal_index,
            ))
            continue
        key = _proposal_key(proposal)
        if key is None:
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                "proposal_group_ref_invalid", proposal_index=proposal_index,
            ))
            continue
        paper_id, group_id, source_digest = key
        group = known.get(key)
        if group is None:
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                "proposal_group_not_enumerated", paper_id, group_id,
                proposal_index,
            ))
            continue
        facts = proposal.get("route_facts", [])
        if not isinstance(facts, list):
            result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                "proposal_route_facts_invalid", paper_id, group_id,
                proposal_index,
            ))
            continue
        blocks = [(block.locator, block.text) for block in group.blocks]
        captions = {block.locator for block in group.blocks if block.caption}
        layout_digest = _group_layout_digest(
            group, parser_version=parser_version,
            binding_version=binding_version,
        )
        for fact_index, fact in enumerate(facts):
            if not isinstance(fact, Mapping):
                result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                    "proposal_route_fact_invalid", paper_id, group_id,
                    proposal_index,
                ))
                continue
            raw_locator = fact.get("block_locator")
            binding, issue = bind_pdf_quote(
                blocks, fact.get("excerpt"), asserted_block_locator=None,
                caption_block_locators=captions,
            )
            resolved_locator = (
                group.blocks[binding.first_block_index].locator
                if binding is not None else ""
            )
            reason = issue or (
                "computed_from_source" if not raw_locator else
                "model_locator_confirmed" if raw_locator == resolved_locator else
                "model_locator_corrected"
            )
            result.resolutions.append({
                "status": "blocked" if issue else "located_unreviewed",
                "paper_id": paper_id,
                "experimental_group_id": group_id,
                "source_digest": source_digest,
                "group_locator": group.source_scope.locator,
                "group_layout_digest": layout_digest,
                "parser_version": parser_version,
                "binding_version": binding_version,
                "production_version": PDF_LOCATOR_PRODUCTION_VERSION,
                "proposal_index": proposal_index,
                "fact_index": fact_index,
                "fact_id": fact.get("fact_id", ""),
                "field_path": fact.get("field_path", ""),
                "raw_block_locator": raw_locator,
                "resolved_block_locator": resolved_locator,
                "resolved_span": binding.locator if binding is not None else "",
                "reason_code": reason,
            })
            if issue:
                result.diagnostics.append(PdfGroupProposalDiagnosticV1(
                    issue, paper_id, group_id, proposal_index,
                ))
            else:
                # Only the copied, unreviewed locator metadata may change.
                fact["block_locator"] = resolved_locator
    result.located_proposals_digest = _digest(result.proposals)
    return result


__all__ = [
    "PdfProposalLocatorProductionV1", "produce_pdf_proposal_locators",
    "PDF_GROUP_PARSER_VERSION", "PDF_QUOTE_BINDING_VERSION",
    "PDF_LOCATOR_PRODUCTION_VERSION",
]
