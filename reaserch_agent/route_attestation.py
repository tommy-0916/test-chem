"""Independent source-identity receipts for raw primary-paper and SI PDFs.

PaperRegistry's ``verified_doi`` and ``local_file`` tags are metadata, not
evidence that particular PDF bytes are an original article or its supporting
information.  A controlled KB attestation is only usable when an independent
trusted acquisition/review event identifies the complete attestation digest.
The event must be supplied by a trust boundary outside model output, workflow
state, and PaperRegistry.  This module never mints such events or signatures.
It does not fetch identity_evidence_url or recheck identity_evidence_digest;
the external event issuer must have verified that source evidence before
authorizing the attestation digest.  An issuer name in the JSON is never proof.

Without an event, the route source index is empty.  Text/Markdown derivatives
are deliberately excluded until they can be bound to an attested parent PDF
and a separately reviewed route annotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from chem_agent_contracts.v2 import StrictModel, canonical_digest

from .tools.paper_registry import PaperRegistry

if TYPE_CHECKING:
    from .route_signed_event import TrustedIssuerPublicKeyV1


_DOCUMENT_DIGEST = re.compile(r"sha256_[0-9a-f]{64}\Z")
_DOI = re.compile(r"10\.[0-9]{4,9}/\S+\Z", re.IGNORECASE)
_MAX_PDF_BYTES = 8 * 1024 * 1024
_MAX_ATTESTATION_BYTES = 32 * 1024
_ATTESTATION_DIR = Path("registry") / "route_source_attestations_v1"


def _valid_relative_pdf_path(value: str) -> bool:
    if not value or "\\" in value or ":" in value:
        return False
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.suffix.lower() == ".pdf"
        and len(path.parts) >= 3
        and path.parts[0] == "_pdf_sources"
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _valid_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
    except ValueError:
        return False


class _AttestationStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceDocumentAttestationV1(_AttestationStrictModel):
    schema_version: Literal["source_document_attestation_v1"]
    paper_id: str = Field(min_length=1)
    kb_relative_path: str = Field(min_length=1)
    document_digest: str = Field(min_length=1)
    document_kind: Literal["primary_paper", "supporting_information"]
    doi: str = ""
    parent_doi: str = ""
    acquisition_url: str = Field(min_length=1)
    request_url: str = Field(min_length=1)
    final_url: str = Field(min_length=1)
    identity_evidence_type: Literal[
        "publisher_doi_link", "publisher_si_link", "reviewed_identity"
    ]
    identity_evidence_url: str = Field(min_length=1)
    identity_evidence_digest: str = Field(min_length=1)
    identity_status: Literal["verified", "unresolved"]
    issuer: str = Field(min_length=1)
    issued_at: str = Field(min_length=1)

    @field_validator("kb_relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if not _valid_relative_pdf_path(value):
            raise ValueError("source must be a KB-relative PDF under _pdf_sources")
        return value

    @field_validator("document_digest", "identity_evidence_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DOCUMENT_DIGEST.fullmatch(value) is None:
            raise ValueError("digest must be sha256_<64 lowercase hex chars>")
        return value

    @field_validator(
        "acquisition_url", "request_url", "final_url", "identity_evidence_url"
    )
    @classmethod
    def validate_https(cls, value: str) -> str:
        if not _valid_https_url(value):
            raise ValueError("source identity URLs must be HTTPS URLs")
        return value

    @model_validator(mode="after")
    def validate_document_identity(self) -> SourceDocumentAttestationV1:
        if self.doi and _DOI.fullmatch(self.doi) is None:
            raise ValueError("doi is invalid")
        if self.parent_doi and _DOI.fullmatch(self.parent_doi) is None:
            raise ValueError("parent_doi is invalid")
        if self.document_kind == "primary_paper":
            if not self.doi or self.parent_doi:
                raise ValueError("primary paper requires DOI and no parent DOI")
        elif not self.parent_doi:
            raise ValueError("supporting information requires parent DOI")
        if not self.issuer.strip() or not self.issued_at.strip():
            raise ValueError("issuer and issuance time are required")
        if self.identity_evidence_type == "publisher_si_link" and (
            self.document_kind != "supporting_information"
        ):
            raise ValueError("publisher SI link is only valid for SI")
        if self.document_kind == "supporting_information" and (
            self.identity_evidence_type not in {"publisher_si_link", "reviewed_identity"}
        ):
            raise ValueError("SI requires a publisher SI link or reviewed identity")
        return self


class TrustedAcquisitionEventV1(_AttestationStrictModel):
    """An independently provided trust anchor, never inferred from registry."""

    schema_version: Literal["trusted_acquisition_event_v1"]
    paper_id: str = Field(min_length=1)
    kb_relative_path: str = Field(min_length=1)
    document_digest: str = Field(min_length=1)
    document_kind: Literal["primary_paper", "supporting_information"]
    attestation_digest: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    identity_verdict: Literal["primary_verified", "si_verified"]

    @field_validator("kb_relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if not _valid_relative_pdf_path(value):
            raise ValueError("trusted event requires a KB-relative raw PDF")
        return value

    @field_validator("document_digest", "attestation_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DOCUMENT_DIGEST.fullmatch(value) is None:
            raise ValueError("digest must be sha256_<64 lowercase hex chars>")
        return value

    @model_validator(mode="after")
    def validate_verdict(self) -> TrustedAcquisitionEventV1:
        expected = (
            "primary_verified" if self.document_kind == "primary_paper"
            else "si_verified"
        )
        if self.identity_verdict != expected or not self.issuer.strip():
            raise ValueError("identity verdict or issuer does not match document kind")
        return self


@dataclass(frozen=True)
class SourceAttestationVerificationV1:
    verified: bool = False
    paper_id: str = ""
    source_path: str = ""
    document_digest: str = ""
    attestation_path: str = ""
    document_kind: str = ""
    doi: str = ""
    parent_doi: str = ""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttestedRouteSourceV1:
    """A source identity receipt kept with its exact original bytes digest."""

    paper_id: str
    path: Path
    document_digest: str
    document_kind: str
    doi: str
    attestation_digest: str


def attestation_path_for(
    kb_root: str | Path, paper_id: str, document_digest: str
) -> Path:
    """Derive an artifact name without using any candidate-provided path."""

    if not paper_id.strip() or _DOCUMENT_DIGEST.fullmatch(document_digest) is None:
        raise ValueError("paper ID and document digest are required")
    paper_key = sha256(paper_id.encode("utf-8")).hexdigest()
    return (
        Path(kb_root).expanduser().resolve()
        / _ATTESTATION_DIR
        / f"{paper_key}_{document_digest.removeprefix('sha256_')}.json"
    )


def verify_source_document_attestation(
    *,
    kb_root: str | Path,
    trusted_event: TrustedAcquisitionEventV1 | None,
) -> SourceAttestationVerificationV1:
    """Cross-check registered raw bytes and KB artifact with an external event.

    A caller must obtain ``trusted_event`` from a separate trusted acquisition
    or review channel.  Parsing it from candidate JSON, PaperRegistry, or the
    KB artifact itself defeats the independence condition and is unsupported.
    URL identity evidence is intentionally not fetched here; the trusted event
    binds the issuer's prior review of those claims by attestation digest.
    """

    if not isinstance(trusted_event, TrustedAcquisitionEventV1):
        return SourceAttestationVerificationV1(reasons=("trusted_event_missing",))
    try:
        event = TrustedAcquisitionEventV1.model_validate(
            trusted_event.model_dump(mode="python"), strict=True
        )
    except ValidationError:
        return SourceAttestationVerificationV1(reasons=("trusted_event_invalid",))
    try:
        root = Path(kb_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("KB root is not a directory")
        source = (root / Path(*PurePosixPath(event.kb_relative_path).parts)).resolve(
            strict=True
        )
        if not source.is_file() or not source.is_relative_to(root):
            return SourceAttestationVerificationV1(
                paper_id=event.paper_id, reasons=("source_outside_kb_or_not_file",)
            )
        if source.stat().st_size > _MAX_PDF_BYTES:
            return SourceAttestationVerificationV1(
                paper_id=event.paper_id, reasons=("source_too_large",)
            )
        raw = source.read_bytes()
        if len(raw) > _MAX_PDF_BYTES:
            return SourceAttestationVerificationV1(
                paper_id=event.paper_id, reasons=("source_too_large",)
            )
    except (OSError, ValueError, TypeError):
        return SourceAttestationVerificationV1(
            paper_id=event.paper_id, reasons=("source_unavailable",)
        )
    digest = "sha256_" + sha256(raw).hexdigest()
    if digest != event.document_digest or not raw.startswith(b"%PDF-"):
        return SourceAttestationVerificationV1(
            paper_id=event.paper_id, source_path=str(source),
            document_digest=digest, reasons=("source_digest_or_format_mismatch",)
        )
    artifact = attestation_path_for(root, event.paper_id, event.document_digest)
    try:
        controlled_dir = artifact.parent.resolve(strict=True)
        artifact_resolved = artifact.resolve(strict=True)
        if not controlled_dir.is_relative_to(root) or (
            not artifact_resolved.is_relative_to(controlled_dir)
        ) or (
            not artifact_resolved.is_file()
        ):
            return SourceAttestationVerificationV1(
                paper_id=event.paper_id, source_path=str(source),
                document_digest=digest, reasons=("attestation_outside_controlled_folder",)
            )
        if artifact_resolved.stat().st_size > _MAX_ATTESTATION_BYTES:
            return SourceAttestationVerificationV1(
                paper_id=event.paper_id, source_path=str(source),
                document_digest=digest, reasons=("attestation_too_large",)
            )
        raw_attestation = artifact_resolved.read_bytes()
        if len(raw_attestation) > _MAX_ATTESTATION_BYTES:
            return SourceAttestationVerificationV1(
                paper_id=event.paper_id, source_path=str(source),
                document_digest=digest, reasons=("attestation_too_large",)
            )
        payload = json.loads(raw_attestation.decode("utf-8"))
        attestation = SourceDocumentAttestationV1.model_validate(payload, strict=True)
    except FileNotFoundError:
        return SourceAttestationVerificationV1(
            paper_id=event.paper_id, source_path=str(source),
            document_digest=digest, reasons=("attestation_missing",)
        )
    except (OSError, ValueError, TypeError, UnicodeError, ValidationError):
        return SourceAttestationVerificationV1(
            paper_id=event.paper_id, source_path=str(source),
            document_digest=digest, reasons=("attestation_invalid",)
        )
    if canonical_digest(attestation.model_dump(mode="json")) != event.attestation_digest:
        return SourceAttestationVerificationV1(
            paper_id=event.paper_id, source_path=str(source),
            document_digest=digest, attestation_path=str(artifact_resolved),
            reasons=("attestation_digest_mismatch",)
        )
    if (
        attestation.paper_id != event.paper_id
        or attestation.kb_relative_path != event.kb_relative_path
        or attestation.document_digest != event.document_digest
        or attestation.document_kind != event.document_kind
        or attestation.issuer != event.issuer
        or attestation.identity_status != "verified"
    ):
        return SourceAttestationVerificationV1(
            paper_id=event.paper_id, source_path=str(source),
            document_digest=digest, attestation_path=str(artifact_resolved),
            reasons=("attestation_identity_mismatch",)
        )
    return SourceAttestationVerificationV1(
        verified=True, paper_id=event.paper_id, source_path=str(source),
        document_digest=digest, attestation_path=str(artifact_resolved),
        document_kind=event.document_kind, doi=attestation.doi,
        parent_doi=attestation.parent_doi,
    )


def _verified_events_from_signed_envelopes(
    signed_events: Sequence[Mapping[str, Any]] | None,
    trusted_public_keys: Mapping[str, TrustedIssuerPublicKeyV1] | None,
) -> tuple[TrustedAcquisitionEventV1, ...]:
    """Authenticate external envelopes before exposing their event payloads."""

    if not isinstance(signed_events, Sequence) or isinstance(
        signed_events, (str, bytes, bytearray)
    ):
        return ()
    # The verifier imports TrustedAcquisitionEventV1 from this module.
    try:
        from .route_signed_event import verify_signed_trusted_acquisition_event
    except ImportError:
        return ()
    keys = trusted_public_keys if trusted_public_keys is not None else {}
    verified_events: list[TrustedAcquisitionEventV1] = []
    for envelope in signed_events:
        if not isinstance(envelope, Mapping):
            continue
        verification = verify_signed_trusted_acquisition_event(
            envelope=envelope, trusted_public_keys=keys
        )
        if verification.verified and verification.event is not None:
            verified_events.append(verification.event)
    return tuple(verified_events)


def attested_route_sources(
    kb_dir: str | Path,
    signed_events: Sequence[Mapping[str, Any]] | None,
    *,
    trusted_public_keys: Mapping[str, TrustedIssuerPublicKeyV1] | None,
) -> dict[str, list[AttestedRouteSourceV1]]:
    """Index registered PDFs only after verifying external signed envelopes.

    Public key bindings must come from caller-controlled configuration. Raw
    ``TrustedAcquisitionEventV1`` objects are not accepted here.
    """

    events = _verified_events_from_signed_envelopes(
        signed_events, trusted_public_keys
    )
    if not events:
        return {}
    return _attested_route_sources_for_verified_events(kb_dir, events)


def _attested_route_sources_for_verified_events(
    kb_dir: str | Path,
    trusted_events: Sequence[TrustedAcquisitionEventV1],
) -> dict[str, list[AttestedRouteSourceV1]]:
    """Index PDF paths backed by registry association and verified events.

    The registry is a lookup, never an identity attester.  In particular,
    ``verified_doi`` and ``local_file`` alone yield no source. This private
    helper only receives events after the public boundary has authenticated
    their signed envelopes.
    """

    if not trusted_events:
        return {}
    root = Path(kb_dir).expanduser().resolve()
    if not root.is_dir():
        return {}
    registered: dict[str, set[tuple[Path, str]]] = {}
    for record in PaperRegistry(root).all():
        paper_id = record.get("paper_id")
        if not isinstance(paper_id, str) or not paper_id:
            continue
        pdf_files = record.get("pdf_files") or []
        if not isinstance(pdf_files, list):
            continue
        paths = registered.setdefault(paper_id, set())
        registry_doi = str(record.get("doi") or "").strip().lower()
        for raw in pdf_files:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                paths.add((Path(raw).expanduser().resolve(), registry_doi))
            except (OSError, ValueError):
                continue
    sources: dict[str, list[AttestedRouteSourceV1]] = {}
    for event in trusted_events:
        if not isinstance(event, TrustedAcquisitionEventV1):
            continue
        check = verify_source_document_attestation(
            kb_root=root, trusted_event=event
        )
        if not check.verified:
            continue
        path = Path(check.source_path)
        expected_doi = (
            check.parent_doi if check.document_kind == "supporting_information"
            else check.doi
        ).strip().lower()
        if (path, expected_doi) not in registered.get(event.paper_id, set()):
            continue
        receipt = AttestedRouteSourceV1(
            paper_id=event.paper_id,
            path=path,
            document_digest=check.document_digest,
            document_kind=check.document_kind,
            doi=expected_doi,
            attestation_digest=event.attestation_digest,
        )
        receipts = sources.setdefault(event.paper_id, [])
        if receipt not in receipts:
            receipts.append(receipt)
    return sources


__all__ = [
    "SourceDocumentAttestationV1", "TrustedAcquisitionEventV1",
    "SourceAttestationVerificationV1", "AttestedRouteSourceV1",
    "attestation_path_for",
    "verify_source_document_attestation", "attested_route_sources",
]
