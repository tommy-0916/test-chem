"""Trusted adapters joining route discovery to the deterministic decision.

The caller supplies a typed goal. Paper paths come from ingestion's registry,
never from a proposed candidate. A selection permits planning only; the
Research V2 publication and Device execution gates remain authoritative.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import Field

from chem_agent_contracts.route_candidate import RouteCandidateV1, RouteGoalV1
from chem_agent_contracts.route_decision import (
    RouteDecisionV1,
    RouteValidationReceiptV1,
    decide_routes,
)
from chem_agent_contracts.v2 import StrictModel, canonical_digest

from .route_device import preflight_route_capabilities
from .route_discovery import RouteDiscoveryResultV1, discover_route_candidates
from .route_science import audit_route_candidate_science
from .route_source import verify_route_source
from .tools.paper_registry import PaperRegistry


_TRUSTED_IDENTITY_STATUSES = frozenset({
    "verified_doi", "verified_arxiv", "verified_semantic_scholar", "local_file",
})
_PARSED_TEXT_STATUSES = frozenset({"parsed", "local_parsed"})


class RoutePipelineResultV1(StrictModel):
    goal: RouteGoalV1
    discovery: RouteDiscoveryResultV1
    decision: RouteDecisionV1
    validation_diagnostics: dict[str, list[str]] = Field(default_factory=dict)


def trusted_route_text_sources(kb_dir: str | Path) -> dict[str, list[Path]]:
    """Index only registered, parsed original text inside the KB root."""
    root = Path(kb_dir).expanduser().resolve()
    if not root.is_dir():
        return {}
    trusted: dict[str, list[Path]] = {}
    for record in PaperRegistry(root).all():
        paper_id = str(record.get("paper_id") or "").strip()
        if not paper_id or str(record.get("verification_status") or "") not in _TRUSTED_IDENTITY_STATUSES:
            continue
        if str(record.get("full_text_status") or "") not in _PARSED_TEXT_STATUSES:
            continue
        paths: list[Path] = []
        for raw in record.get("corpus_files", []) or []:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = Path(raw).expanduser().resolve(strict=True)
            except (OSError, ValueError):
                continue
            if path.is_file() and path.is_relative_to(root) and path.suffix.lower() in {".txt", ".md"}:
                if path not in paths:
                    paths.append(path)
        if paths:
            trusted[paper_id] = paths
    return trusted


def _matching_trusted_source(
    candidate: RouteCandidateV1,
    trusted_sources: Mapping[str, Sequence[str | Path] | str | Path],
    root: Path,
) -> Path | None:
    scope = candidate.source_scope
    if scope is None:
        return None
    raw = trusted_sources.get(scope.paper_id)
    names: Sequence[str | Path] = [raw] if isinstance(raw, (str, Path)) else raw or []
    matches: list[Path] = []
    for name in names:
        if not isinstance(name, (str, Path)):
            continue
        try:
            path = Path(name).expanduser().resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(root):
                continue
            if path.suffix.lower() not in {".txt", ".md"}:
                continue
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            if "sha256_" + sha256(path.read_bytes()).hexdigest() == scope.source_digest:
                matches.append(path)
        except (OSError, ValueError):
            continue
    return matches[0] if len(matches) == 1 else None


def evaluate_route_decision_v1(
    goal: RouteGoalV1,
    protocols: Sequence[Mapping[str, Any]],
    *,
    source_root: str | Path,
    trusted_source_paths: Mapping[str, Sequence[str | Path] | str | Path],
    device_context: dict[str, Any] | None = None,
    science_agent: Any = None,
) -> RoutePipelineResultV1:
    """Evaluate route proposals with independently constructed receipts."""
    root = Path(source_root).expanduser().resolve()
    scoped_sources: dict[str, list[Path]] = {}
    for paper_id, raw in trusted_source_paths.items():
        names: Sequence[str | Path] = [raw] if isinstance(raw, (str, Path)) else raw or []
        for name in names:
            if not isinstance(name, (str, Path)):
                continue
            try:
                path = Path(name).expanduser().resolve(strict=True)
                if (path.is_file() and path.is_relative_to(root)
                        and path.suffix.lower() in {".txt", ".md"}
                        and path.stat().st_size <= 8 * 1024 * 1024):
                    paths = scoped_sources.setdefault(paper_id, [])
                    if path not in paths:
                        paths.append(path)
            except (OSError, ValueError):
                continue
    discovery = discover_route_candidates(
        goal, protocols, trusted_source_paths=scoped_sources,
    )
    diagnostics: dict[str, list[str]] = {}

    def validate(candidate: RouteCandidateV1) -> RouteValidationReceiptV1:
        path = _matching_trusted_source(candidate, scoped_sources, root)
        source = verify_route_source(
            candidate,
            source_paths={candidate.source_scope.paper_id: path} if path and candidate.source_scope else {},
            source_root=root,
        )
        # Candidate evidence status is proposal data. Only the independently
        # verified source IDs may enter the existing V2 provenance gate as
        # parsed local evidence; all other item flags are stripped.
        science_candidate = candidate.model_copy(deep=True)
        verified_ids = set(source.verified_evidence_ids)
        for item in science_candidate.evidence_bundle:
            trusted = source.source_scope_verified and item.evidence_id in verified_ids
            item.verification_status = "local_file" if trusted else "unknown"
            item.full_text_status = "local_parsed" if trusted else "unknown"
        science = audit_route_candidate_science(science_candidate, agent=science_agent)
        device = preflight_route_capabilities(candidate.required_capabilities, device_context)
        diagnostics[candidate.route_id] = [
            *("source:" + reason for reason in source.reasons),
            *("science:" + issue for issue in science["scientific_gate_issues"]),
            *("device:" + reason for reason in device["reasons"]),
        ]
        return RouteValidationReceiptV1(
            route_id=candidate.route_id,
            candidate_digest=canonical_digest(candidate),
            source_scope_verified=source.source_scope_verified,
            source_route_signature=source.source_route_signature,
            verified_evidence_ids=list(source.verified_evidence_ids),
            verified_field_paths=list(source.verified_field_paths),
            audited_field_paths=list(science["audited_field_paths"]),
            verified_runtime_resolution_fields=list(science["verified_runtime_resolution_fields"]),
            verified_convention_field_paths=list(science["verified_convention_field_paths"]),
            verified_graph_step_ids=list(science["verified_graph_step_ids"]),
            scientific_completeness=science["scientific_completeness"],
            scientific_gate_issues=list(science["scientific_gate_issues"]),
            device_preflight=device,
            derived_scaled_parameter_count=sum(
                field.derived_or_scaled for field in candidate.evidence_matrix
            ),
            native_device_support=device["status"] == "preflight_supported",
        )

    decision = decide_routes(goal, discovery.candidates, validate)
    unresolved_discovery = [
        item for item in discovery.diagnostics if item.status == "unresolved"
    ]
    if unresolved_discovery:
        # An unparsed synthesis group can be a better candidate than the
        # current winner. Preserve the selector's conservative global rule
        # even when discovery could not construct that group's typed object.
        payload = decision.model_dump(mode="json", exclude={"decision_id"})
        payload["status"] = "unresolved"
        payload["selected_route_id"] = None
        payload["decision_reasons"] = [
            "candidate_discovery_incomplete",
            *sorted({item.reason_code for item in unresolved_discovery}),
        ]
        for record in payload["candidates"]:
            if record["status"] == "selected_for_planning":
                record["status"] = "unresolved"
                record["reasons"].append("candidate_discovery_incomplete")
        decision = RouteDecisionV1.model_validate({
            **payload, "decision_id": canonical_digest(payload),
        })
    return RoutePipelineResultV1(
        goal=goal,
        discovery=discovery,
        decision=decision,
        validation_diagnostics=diagnostics,
    )


__all__ = [
    "RoutePipelineResultV1", "trusted_route_text_sources", "evaluate_route_decision_v1",
]
