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

from .route_attestation import (
    AttestedRouteSourceV1, TrustedAcquisitionEventV1, attested_route_sources,
)
from .route_device import preflight_route_capabilities
from .route_discovery import (
    RouteDiscoveryDiagnosticV1, RouteDiscoveryResultV1,
    discover_route_candidates,
)
from .route_group_compiler import compile_experimental_group_protocols
from .route_pdf_groups import (
    audit_pdf_group_extraction_coverage,
    audit_pdf_group_roles,
    enumerate_attested_pdf_experimental_groups,
)
from .route_pdf_source import verify_route_pdf_source
from .route_science import audit_route_candidate_science
from .route_signature_review import (
    ReviewedRouteSignatureScopeV1, verify_reviewed_route_signature_manifest,
)
from .route_signed_event import (
    TrustedIssuerPublicKeyV1, verify_signed_trusted_acquisition_event,
)
from .route_source import RouteSourceVerificationV1, verify_route_source
from .tools.paper_registry import PaperRegistry


_TRUSTED_IDENTITY_STATUSES = frozenset({
    "verified_doi", "verified_arxiv", "verified_semantic_scholar", "local_file",
})
_PARSED_TEXT_STATUSES = frozenset({"parsed", "local_parsed"})
RouteSourceInput = str | Path | AttestedRouteSourceV1
RouteSourceIndex = Mapping[
    str, Sequence[RouteSourceInput] | RouteSourceInput
]


class RoutePipelineResultV1(StrictModel):
    goal: RouteGoalV1
    discovery: RouteDiscoveryResultV1
    decision: RouteDecisionV1
    compilation_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    validation_diagnostics: dict[str, list[str]] = Field(default_factory=dict)


def trusted_route_text_sources(kb_dir: str | Path) -> dict[str, list[Path]]:
    """Index registered locatable source files inside the KB root.

    The legacy function name is retained for existing callers; supported
    formats are UTF-8 text and PDF. Registry metadata alone does not prove
    that a file is a publisher original or a reviewed source annotation.
    """
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
        for raw in list(record.get("corpus_files", []) or []) + list(
            record.get("pdf_files", []) or []
        ):
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = Path(raw).expanduser().resolve(strict=True)
            except (OSError, ValueError):
                continue
            if path.is_file() and path.is_relative_to(root) and path.suffix.lower() in {".txt", ".md", ".pdf"}:
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
            if path.suffix.lower() not in {".txt", ".md", ".pdf"}:
                continue
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            if "sha256_" + sha256(path.read_bytes()).hexdigest() == scope.source_digest:
                matches.append(path)
        except (OSError, ValueError):
            continue
    return matches[0] if len(matches) == 1 else None


def _evaluate_route_decision_v1_impl(
    goal: RouteGoalV1,
    protocols: Sequence[Mapping[str, Any]],
    *,
    source_root: str | Path,
    trusted_source_paths: RouteSourceIndex | None = None,
    trusted_source_events: Sequence[TrustedAcquisitionEventV1] | None = None,
    signed_source_events: Sequence[Mapping[str, Any]] | None = None,
    trusted_public_keys: Mapping[str, TrustedIssuerPublicKeyV1] | None = None,
    signed_route_signature_reviews: Sequence[Mapping[str, Any]] | None = None,
    trusted_route_signature_public_keys: Mapping[
        str, TrustedIssuerPublicKeyV1
    ] | None = None,
    verified_capabilities_by_group: Mapping[
        tuple[str, str, str], Sequence[str]
    ] | None = None,
    verified_group_roles_by_group: Mapping[
        tuple[str, str, str], str
    ] | None = None,
    device_context: dict[str, Any] | None = None,
    science_agent: Any = None,
    require_attested_sources: bool,
) -> RoutePipelineResultV1:
    """Shared evaluator; the unattested source mode is for offline fixtures only.

    ``trusted_source_paths`` is honored only with the explicit offline fixture
    opt-out. Production accepts only signed events verified against public
    keys supplied outside workflow/model/KB state. Legacy unsigned event
    objects are rejected at this boundary.
    """
    root = Path(source_root).expanduser().resolve()
    event_issues: list[str] = []
    verified_events: list[TrustedAcquisitionEventV1] = []
    if require_attested_sources:
        if trusted_source_events:
            event_issues.append("unsigned_trusted_source_event_rejected")
        for envelope in signed_source_events or ():
            verified = verify_signed_trusted_acquisition_event(
                envelope=envelope,
                trusted_public_keys=trusted_public_keys or {},
            )
            if not verified.verified or verified.event is None:
                event_issues.append(
                    "trusted_source_event_" + (verified.reason_code or "invalid")
                )
            else:
                verified_events.append(verified.event)
    source_index: RouteSourceIndex = (
        attested_route_sources(
            root, signed_source_events, trusted_public_keys=trusted_public_keys,
        )
        if require_attested_sources else trusted_source_paths or {}
    )
    scoped_sources: dict[str, list[Path]] = {}
    attested_identity: dict[
        tuple[str, Path, str], set[tuple[str, str, str]]
    ] = {}
    for paper_id, raw in source_index.items():
        names: Sequence[RouteSourceInput] = (
            [raw] if isinstance(raw, (str, Path, AttestedRouteSourceV1))
            else raw or []
        )
        for entry in names:
            is_attested = isinstance(entry, AttestedRouteSourceV1)
            if require_attested_sources and not is_attested:
                continue
            if is_attested and entry.paper_id != paper_id:
                continue
            name = entry.path if is_attested else entry
            if not isinstance(name, (str, Path)):
                continue
            try:
                path = Path(name).expanduser().resolve(strict=True)
                if (path.is_file() and path.is_relative_to(root)
                        and path.suffix.lower() in {".txt", ".md", ".pdf"}
                        and path.stat().st_size <= 8 * 1024 * 1024):
                    if is_attested:
                        if path.suffix.lower() != ".pdf":
                            continue
                        attested_identity.setdefault(
                            (paper_id, path, entry.document_digest), set()
                        ).add(
                            (
                                entry.doi.strip().lower(), entry.document_kind,
                                entry.attestation_digest,
                            )
                        )
                    paths = scoped_sources.setdefault(paper_id, [])
                    if path not in paths:
                        paths.append(path)
            except (OSError, ValueError):
                continue
    if require_attested_sources:
        # The source index intentionally drops stale or unregistered files.
        # A dropped signed source may contain another synthesis group, so it
        # must also make the whole candidate set incomplete.
        indexed_events = {
            (
                paper_id, path, digest, kind, attestation_digest
            )
            for (paper_id, path, digest), identities in attested_identity.items()
            for _, kind, attestation_digest in identities
        }
        for event in verified_events:
            try:
                event_path = root.joinpath(
                    *event.kb_relative_path.split("/")
                ).resolve()
            except (OSError, ValueError):
                event_issues.append("trusted_source_event_not_attested")
                continue
            event_key = (
                event.paper_id, event_path, event.document_digest,
                event.document_kind, event.attestation_digest,
            )
            if event_key not in indexed_events:
                event_issues.append("trusted_source_event_not_attested")
    compilation = compile_experimental_group_protocols(protocols)
    discovery = discover_route_candidates(
        goal, compilation.protocols, trusted_source_paths=scoped_sources,
    )
    for issue_index, reason_code in enumerate(sorted(set(event_issues))):
        discovery.diagnostics.append(RouteDiscoveryDiagnosticV1(
            protocol_index=len(protocols), group_index=issue_index,
            reason_code=reason_code,
        ))
    if require_attested_sources and not source_index:
        discovery.diagnostics.append(RouteDiscoveryDiagnosticV1(
            protocol_index=len(protocols), group_index=0,
            reason_code=(
                "trusted_source_event_missing"
                if not signed_source_events and not trusted_source_events
                else "attested_source_unavailable"
            ),
        ))
    enumerated_groups: dict[tuple[str, str, str], list[Any]] = {}
    group_audit_clean = False
    if require_attested_sources and source_index:
        enumerated = enumerate_attested_pdf_experimental_groups(
            root, signed_source_events, trusted_public_keys=trusted_public_keys,
        )
        coverage = audit_pdf_group_extraction_coverage(
            enumerated.groups, protocols
        )
        role_audit = audit_pdf_group_roles(
            enumerated.groups, protocols,
            group_roles_by_group=verified_group_roles_by_group,
        )
        enumerated_source_keys = set()
        for group in enumerated.groups:
            try:
                group_path = Path(group.source_document).resolve(strict=True)
            except (OSError, ValueError):
                continue
            enumerated_source_keys.add((
                group.source_scope.paper_id, group_path,
                group.source_scope.source_digest,
            ))
        missing_attested_sources = set(attested_identity) - enumerated_source_keys
        group_audit_clean = bool(enumerated.groups) and not (
            event_issues or enumerated.diagnostics or coverage.missing_scopes
            or role_audit.issues or missing_attested_sources
        )
        for group in enumerated.groups:
            group_scope = group.source_scope
            enumerated_groups.setdefault((
                group_scope.paper_id, group_scope.experimental_group_id,
                group_scope.source_digest,
            ), []).append(group)
        for index, diagnostic in enumerate(enumerated.diagnostics):
            discovery.diagnostics.append(RouteDiscoveryDiagnosticV1(
                protocol_index=len(protocols), group_index=index,
                paper_id=diagnostic.paper_id,
                experimental_group_id=diagnostic.experimental_group_id,
                reason_code="source_group_" + diagnostic.reason_code,
            ))
        for index, (paper_id, _, _) in enumerate(sorted(missing_attested_sources)):
            discovery.diagnostics.append(RouteDiscoveryDiagnosticV1(
                protocol_index=len(protocols),
                group_index=len(enumerated.diagnostics) + index,
                paper_id=paper_id,
                reason_code="attested_source_not_enumerated",
            ))
        for index, scope in enumerate(coverage.missing_scopes):
            discovery.diagnostics.append(RouteDiscoveryDiagnosticV1(
                protocol_index=len(protocols),
                group_index=len(enumerated.diagnostics) + index,
                paper_id=scope.paper_id,
                experimental_group_id=scope.experimental_group_id,
                reason_code="attested_group_not_extracted",
            ))
        for index, issue in enumerate(role_audit.issues):
            discovery.diagnostics.append(RouteDiscoveryDiagnosticV1(
                protocol_index=len(protocols),
                group_index=(
                    len(enumerated.diagnostics)
                    + len(coverage.missing_scopes) + index
                ),
                paper_id=issue.source_scope.paper_id,
                experimental_group_id=issue.source_scope.experimental_group_id,
                reason_code=issue.reason_code,
            ))
    diagnostics: dict[str, list[str]] = {}

    def validate(candidate: RouteCandidateV1) -> RouteValidationReceiptV1:
        path = _matching_trusted_source(candidate, scoped_sources, root)
        source_verifier = (
            verify_route_pdf_source if path and path.suffix.lower() == ".pdf"
            else verify_route_source
        )
        scope = candidate.source_scope
        identities = (
            attested_identity.get((scope.paper_id, path, scope.source_digest), set())
            if path and scope else set()
        )
        source_identity = next(iter(identities)) if len(identities) == 1 else None
        if require_attested_sources and path and len(identities) != 1:
            source = RouteSourceVerificationV1(
                reasons=("source_attestation_digest_mismatch",)
            )
        else:
            source = source_verifier(
                candidate,
                source_paths={scope.paper_id: path} if path and scope else {},
                source_root=root,
            )
        if source.source_scope_verified and require_attested_sources:
            attested_doi = source_identity[0] if source_identity else ""
            if any(
                item.doi.strip().lower() != attested_doi
                for item in candidate.evidence_bundle if item.doi.strip()
            ):
                source = RouteSourceVerificationV1(
                    document_digest=source.document_digest,
                    source_path=source.source_path,
                    reasons=("evidence_doi_attestation_mismatch",),
                )
        # The PDF verifier checks quoted fields, not a complete chemical
        # route. Production can obtain that signature only from a separately
        # authenticated review of this exact PDF experimental group.
        source_signature = None if require_attested_sources else source.source_route_signature
        signature_review_digest = ""
        review_reasons: list[str] = []
        if require_attested_sources:
            if not source.source_scope_verified or not path or not scope or not source_identity:
                review_reasons.append("source_unverified")
            elif not group_audit_clean:
                review_reasons.append("source_group_audit_incomplete")
            elif candidate.target != goal.target:
                review_reasons.append("target_goal_mismatch")
            else:
                group_key = (
                    scope.paper_id, scope.experimental_group_id, scope.source_digest
                )
                matched_groups = enumerated_groups.get(group_key, [])
                if len(matched_groups) != 1:
                    review_reasons.append("source_group_not_uniquely_enumerated")
                else:
                    group = matched_groups[0]
                    group_scope = group.source_scope
                    try:
                        group_path = Path(group.source_document).resolve(strict=True)
                    except (OSError, ValueError):
                        group_path = None
                    if (
                        group_path != path
                        or group_scope.section != scope.section
                        or group_scope.locator != scope.locator
                    ):
                        review_reasons.append("source_group_scope_mismatch")
                    else:
                        expected_review_scope = ReviewedRouteSignatureScopeV1(
                            paper_id=group_scope.paper_id,
                            experimental_group_id=group_scope.experimental_group_id,
                            source_digest=group_scope.source_digest,
                            source_attestation_digest=source_identity[2],
                            group_locator=group_scope.locator,
                            section=group_scope.section,
                            document_kind=source_identity[1],
                            source_doi=source_identity[0],
                            target_material=goal.target.material,
                            target_state=goal.target.desired_state,
                            target_objective=goal.target.objective,
                        )
                        verified_reviews = {}
                        review_failures: list[str] = []
                        for envelope in signed_route_signature_reviews or ():
                            manifest = (
                                envelope.get("manifest")
                                if isinstance(envelope, Mapping) else None
                            )
                            # Untrusted identity is used only to route reviews
                            # to the right group; the verifier authenticates
                            # every field before it can grant authority.
                            if isinstance(manifest, Mapping) and (
                                manifest.get("paper_id") != group_scope.paper_id
                                or manifest.get("experimental_group_id")
                                != group_scope.experimental_group_id
                            ):
                                continue
                            verification = verify_reviewed_route_signature_manifest(
                                envelope=envelope,
                                trusted_public_keys=(
                                    trusted_route_signature_public_keys or {}
                                ),
                                expected_scope=expected_review_scope,
                            )
                            if verification.verified and verification.receipt:
                                verified_reviews[
                                    verification.receipt.review_digest
                                ] = verification.receipt
                            elif verification.reason_code:
                                review_failures.append(verification.reason_code)
                        review_reasons.extend(review_failures)
                        if len(verified_reviews) == 1 and not review_failures:
                            only_review = next(iter(verified_reviews.values()))
                            source_signature = only_review.route_signature
                            signature_review_digest = only_review.review_digest
                        elif len(verified_reviews) > 1:
                            review_reasons.append("multiple_matching_reviews")
                        elif verified_reviews:
                            review_reasons.append("mixed_valid_invalid_reviews")
                        else:
                            review_reasons.append("review_missing_or_unverified")
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
        device = dict(preflight_route_capabilities(
            candidate.required_capabilities, device_context
        ))
        device["reasons"] = list(device.get("reasons", []))
        if require_attested_sources:
            group_key = (
                scope.paper_id, scope.experimental_group_id, scope.source_digest
            ) if scope else None
            trusted_capabilities = (
                verified_capabilities_by_group.get(group_key)
                if group_key and verified_capabilities_by_group else None
            )
            requirements_verified = (
                isinstance(trusted_capabilities, Sequence)
                and not isinstance(trusted_capabilities, (str, bytes))
                and bool(trusted_capabilities)
                and all(
                    isinstance(item, str) and item.strip() == item and item
                    for item in trusted_capabilities
                )
                and len(set(trusted_capabilities)) == len(trusted_capabilities)
                and set(trusted_capabilities) == set(candidate.required_capabilities)
            )
            if not requirements_verified:
                if device["status"] == "preflight_supported":
                    device["status"] = "unknown"
                device["reasons"].append("capability_requirements_unverified")
        diagnostics[candidate.route_id] = [
            *("source:" + reason for reason in source.reasons),
            *("review:" + reason for reason in sorted(set(review_reasons))),
            *("science:" + issue for issue in science["scientific_gate_issues"]),
            *("device:" + reason for reason in device["reasons"]),
        ]
        return RouteValidationReceiptV1(
            route_id=candidate.route_id,
            candidate_digest=canonical_digest(candidate),
            source_scope_verified=source.source_scope_verified,
            source_document_kind=(
                source_identity[1] if source.source_scope_verified and source_identity
                else ""
            ),
            source_identity_doi=(
                source_identity[0] if source.source_scope_verified and source_identity
                else ""
            ),
            source_attestation_digest=(
                source_identity[2] if source.source_scope_verified and source_identity
                else ""
            ),
            source_route_signature=source_signature,
            source_route_signature_review_digest=signature_review_digest,
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
    if unresolved_discovery or compilation.diagnostics:
        # An unparsed synthesis group can be a better candidate than the
        # current winner. Preserve the selector's conservative global rule
        # even when discovery could not construct that group's typed object.
        payload = decision.model_dump(mode="json", exclude={"decision_id"})
        payload["status"] = "unresolved"
        payload["selected_route_id"] = None
        payload["decision_reasons"] = [
            "candidate_discovery_incomplete",
            *sorted({item.reason_code for item in unresolved_discovery}),
            *sorted({item.reason_code for item in compilation.diagnostics}),
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
        compilation_diagnostics=[vars(item) for item in compilation.diagnostics],
        validation_diagnostics=diagnostics,
    )


def evaluate_route_decision_v1(
    goal: RouteGoalV1,
    protocols: Sequence[Mapping[str, Any]],
    *,
    source_root: str | Path,
    trusted_source_events: Sequence[TrustedAcquisitionEventV1] | None = None,
    signed_source_events: Sequence[Mapping[str, Any]] | None = None,
    trusted_public_keys: Mapping[str, TrustedIssuerPublicKeyV1] | None = None,
    signed_route_signature_reviews: Sequence[Mapping[str, Any]] | None = None,
    trusted_route_signature_public_keys: Mapping[
        str, TrustedIssuerPublicKeyV1
    ] | None = None,
    verified_capabilities_by_group: Mapping[
        tuple[str, str, str], Sequence[str]
    ] | None = None,
    verified_group_roles_by_group: Mapping[
        tuple[str, str, str], str
    ] | None = None,
    device_context: dict[str, Any] | None = None,
    science_agent: Any = None,
) -> RoutePipelineResultV1:
    """Evaluate candidate routes with signed source identity required.

    The public production entrypoint has no path-only or unattested source
    option. Legacy unsigned event objects are retained only to produce an
    explicit rejection diagnostic during migration.
    """
    return _evaluate_route_decision_v1_impl(
        goal,
        protocols,
        source_root=source_root,
        trusted_source_events=trusted_source_events,
        signed_source_events=signed_source_events,
        trusted_public_keys=trusted_public_keys,
        signed_route_signature_reviews=signed_route_signature_reviews,
        trusted_route_signature_public_keys=trusted_route_signature_public_keys,
        verified_capabilities_by_group=verified_capabilities_by_group,
        verified_group_roles_by_group=verified_group_roles_by_group,
        device_context=device_context,
        science_agent=science_agent,
        require_attested_sources=True,
    )


__all__ = [
    "RoutePipelineResultV1", "trusted_route_text_sources", "evaluate_route_decision_v1",
]
