"""Fail-closed discovery of paper experimental-group route proposals.

This module does not verify that a quoted procedure belongs to a primary
paper, or that a proposed route is scientifically complete.  It only keeps
already structured groups separate and refuses to manufacture missing facts.
The later SourceVerifier must use the independently supplied source registry,
and RouteDecision must require its trusted validation receipt.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field, ValidationError

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1,
    RouteCandidateV1,
    RouteFieldEvidenceV1,
    RouteGoalV1,
    RouteSignatureV1,
    RouteTargetV1,
)
from chem_agent_contracts.v2 import EvidenceItemV2, MacroStepV2, StrictModel


_ROUTE_GROUP_ROLES = frozenset({"synthesis", "material_processing"})
_NON_ROUTE_GROUP_ROLES = frozenset(
    {"characterization", "testing", "performance_testing", "non_procedural"}
)


class RouteDiscoveryDiagnosticV1(StrictModel):
    protocol_index: int = Field(ge=0)
    group_index: int = Field(ge=0)
    paper_id: str = ""
    experimental_group_id: str = ""
    status: Literal["unresolved", "excluded"] = "unresolved"
    reason_code: str = Field(min_length=1)


class RouteDiscoveryResultV1(StrictModel):
    """Candidates are proposals; paths are hints, not source authority."""

    candidates: list[RouteCandidateV1] = Field(default_factory=list)
    diagnostics: list[RouteDiscoveryDiagnosticV1] = Field(default_factory=list)
    source_paths_by_digest: dict[str, str] = Field(default_factory=dict)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _source_values(group: Mapping[str, Any], parent: Mapping[str, Any]) -> dict[str, str]:
    source = group.get("source")
    if not isinstance(source, Mapping):
        source = {}
    return {
        "paper_id": _text(group.get("paper_id")) or _text(parent.get("paper_id")),
        "experimental_group_id": _text(group.get("experimental_group_id")),
        "source_document": (
            _text(source.get("source_document"))
            or _text(group.get("source_document"))
            or _text(parent.get("source_document"))
        ),
        "section": _text(source.get("section")) or _text(group.get("section")),
        "locator": _text(source.get("locator")) or _text(group.get("locator")),
        "source_digest": (
            _text(source.get("source_digest"))
            or _text(group.get("source_digest"))
        ),
    }


def _trusted_paths(
    paper_id: str,
    trusted_source_paths: Mapping[str, Sequence[str | Path] | str | Path] | None,
) -> set[Path]:
    if trusted_source_paths is None:
        return set()
    raw = trusted_source_paths.get(paper_id)
    if isinstance(raw, (str, Path)):
        paths: Sequence[str | Path] = [raw]
    elif isinstance(raw, Sequence):
        paths = raw
    else:
        return set()
    return {
        Path(item).expanduser().resolve()
        for item in paths
        if isinstance(item, (str, Path))
    }


def _group_entries(
    protocols: Sequence[Mapping[str, Any]],
) -> list[tuple[int, int, Mapping[str, Any], Mapping[str, Any]]]:
    entries: list[tuple[int, int, Mapping[str, Any], Mapping[str, Any]]] = []
    for protocol_index, protocol in enumerate(protocols):
        if not isinstance(protocol, Mapping):
            entries.append((protocol_index, 0, {}, {}))
            continue
        groups = protocol.get("experimental_groups")
        if isinstance(groups, list):
            if not groups:
                entries.append((protocol_index, 0, protocol, {}))
            else:
                for group_index, group in enumerate(groups):
                    entries.append(
                        (protocol_index, group_index, protocol,
                         group if isinstance(group, Mapping) else {})
                    )
        elif _text(protocol.get("experimental_group_id")):
            entries.append((protocol_index, 0, protocol, protocol))
        else:
            entries.append((protocol_index, 0, protocol, {}))
    return entries


def _scope_issue(
    raw_field: Mapping[str, Any],
    group_scope: ExperimentalGroupScopeV1,
) -> str:
    """Never infer a paper field's experimental group from its parent."""

    if raw_field.get("status") != "supported":
        return ""
    raw_scope = raw_field.get("source_scope")
    if not isinstance(raw_scope, Mapping):
        return "field_source_scope_missing"
    if (
        _text(raw_scope.get("paper_id")) != group_scope.paper_id
        or _text(raw_scope.get("experimental_group_id"))
        != group_scope.experimental_group_id
    ):
        return "experimental_group_mixing"
    if _text(raw_scope.get("source_digest")) != group_scope.source_digest:
        return "field_source_digest_mismatch"
    if not _text(raw_scope.get("locator")):
        return "field_locator_missing"
    provenance = raw_field.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get("kind") == "paper":
        # Source scope hashes the original document; V2 provenance hashes the
        # evidence excerpt field. The SourceVerifier later checks the exact
        # bundle index and the excerpt against the original group lines.
        excerpt_digest = _text(provenance.get("source_digest"))
        if not re.fullmatch(r"sha256_[0-9a-f]{64}", excerpt_digest):
            return "field_provenance_excerpt_digest_missing"
        reported_path = _text(provenance.get("source_path"))
        if not re.fullmatch(r"evidence_bundle\.items\[[0-9]+\]\.excerpt", reported_path):
            return "field_provenance_path_mismatch"
    return ""


def _step_issue(group: Mapping[str, Any], group_id: str) -> str:
    steps = group.get("steps")
    if steps is None:
        return ""
    if not isinstance(steps, list):
        return "steps_invalid"
    for step in steps:
        if not isinstance(step, Mapping):
            return "steps_invalid"
        role = _text(step.get("step_role")) or _text(step.get("group_role"))
        if role and role not in _ROUTE_GROUP_ROLES:
            return "non_route_step_in_experimental_group"
        source = step.get("source")
        if isinstance(source, Mapping):
            step_group = _text(source.get("experimental_group_id"))
            if step_group and step_group != group_id:
                return "experimental_group_mixing"
    return ""


def discover_route_candidates(
    goal: RouteGoalV1,
    protocols: Sequence[Mapping[str, Any]],
    *,
    trusted_source_paths: Mapping[str, Sequence[str | Path] | str | Path] | None = None,
) -> RouteDiscoveryResultV1:
    """Create unassessed candidates only from explicitly scoped procedures.

    ``trusted_source_paths`` must come from a separate registry/ingestion
    boundary, never from model output.  A protocol-reported path is checked
    against that allowlist before it is opened.  Even a matching file hash is
    not a SourceVerifier verdict about quoted spans or experimental groups.
    """

    result = RouteDiscoveryResultV1()
    entries = _group_entries(protocols)
    identities = Counter(
        (
            _source_values(group, parent)["paper_id"],
            _source_values(group, parent)["experimental_group_id"],
        )
        for _, _, parent, group in entries
        if group and _source_values(group, parent)["paper_id"]
        and _source_values(group, parent)["experimental_group_id"]
    )

    def diagnose(
        protocol_index: int,
        group_index: int,
        paper_id: str,
        group_id: str,
        reason_code: str,
        *,
        status: Literal["unresolved", "excluded"] = "unresolved",
    ) -> None:
        result.diagnostics.append(RouteDiscoveryDiagnosticV1(
            protocol_index=protocol_index,
            group_index=group_index,
            paper_id=paper_id,
            experimental_group_id=group_id,
            status=status,
            reason_code=reason_code,
        ))

    for protocol_index, group_index, parent, group in entries:
        source = _source_values(group, parent)
        paper_id = source["paper_id"]
        group_id = source["experimental_group_id"]
        if not group:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "experimental_group_missing")
            continue
        if not paper_id or not group_id:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "experimental_group_identity_missing")
            continue
        if identities[(paper_id, group_id)] > 1:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "duplicate_experimental_group_scope")
            continue

        role = _text(group.get("group_role"))
        if role in _NON_ROUTE_GROUP_ROLES:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "non_route_experimental_group", status="excluded")
            continue
        if role not in _ROUTE_GROUP_ROLES:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "group_role_missing_or_unknown")
            continue
        step_issue = _step_issue(group, group_id)
        if step_issue:
            diagnose(protocol_index, group_index, paper_id, group_id, step_issue)
            continue

        document = source["source_document"]
        if not document or not source["locator"] or not source["source_digest"]:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "source_locator_or_digest_missing")
            continue
        allowed = _trusted_paths(paper_id, trusted_source_paths)
        document_path = Path(document).expanduser().resolve()
        if document_path not in allowed:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "trusted_source_document_missing")
            continue
        try:
            if document_path.stat().st_size > 8 * 1024 * 1024:
                diagnose(protocol_index, group_index, paper_id, group_id,
                         "source_too_large")
                continue
            document_digest = "sha256_" + sha256(document_path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "source_document_unavailable")
            continue
        if document_digest != source["source_digest"]:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "source_digest_mismatch")
            continue

        required = (
            "target", "route_signature", "evidence_bundle", "evidence_matrix",
            "material_graph", "required_capabilities",
        )
        missing = next(
            (key for key in required if not group.get(key)),
            "",
        )
        if missing:
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "structured_route_field_missing:" + missing)
            continue
        capabilities = group["required_capabilities"]
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item.strip() for item in capabilities
        ):
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "required_capabilities_invalid")
            continue
        try:
            group_scope = ExperimentalGroupScopeV1(
                paper_id=paper_id,
                experimental_group_id=group_id,
                section=source["section"],
                locator=source["locator"],
                source_digest=document_digest,
            )
            raw_fields = group["evidence_matrix"]
            if not isinstance(raw_fields, list):
                raise ValueError("evidence_matrix_invalid")
            field_issue = ""
            for field in raw_fields:
                if isinstance(field, Mapping):
                    field_issue = _scope_issue(field, group_scope)
                    if field_issue:
                        break
            if field_issue:
                diagnose(protocol_index, group_index, paper_id, group_id, field_issue)
                continue
            candidate = RouteCandidateV1(
                route_id="route_" + sha256(
                    f"{paper_id}\0{group_id}\0{document_digest}".encode("utf-8")
                ).hexdigest()[:24],
                target=RouteTargetV1.model_validate(group["target"]),
                source_scope=group_scope,
                route_signature=RouteSignatureV1.model_validate(
                    group["route_signature"]
                ),
                evidence_bundle=[EvidenceItemV2.model_validate(item)
                                 for item in group["evidence_bundle"]],
                evidence_matrix=[RouteFieldEvidenceV1.model_validate(item)
                                 for item in raw_fields],
                material_graph=[MacroStepV2.model_validate(item)
                                for item in group["material_graph"]],
                required_capabilities=capabilities,
                origin="paper_experimental_group",
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            diagnose(protocol_index, group_index, paper_id, group_id,
                     "candidate_contract_invalid")
            continue
        # This manifest is a trace hint.  It must never be reused as the
        # independent trusted path registry for SourceVerifier.
        result.source_paths_by_digest[document_digest] = str(document_path)
        result.candidates.append(candidate)
    return result


__all__ = [
    "RouteDiscoveryDiagnosticV1",
    "RouteDiscoveryResultV1",
    "discover_route_candidates",
]
