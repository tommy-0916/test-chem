"""Verify an independent, externally signed review of one route signature.

The caller supplies the expected PDF/group/target scope and an issuer-scoped
public-key map from outside model output, workflow state, and the manifest.
The signed declaration records the reviewer's conclusion; cryptographic
verification authenticates that declaration, but cannot establish that the
reviewer's chemical interpretation is correct. No signing key or issuance
service belongs in this module.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Literal, Mapping

from pydantic import ConfigDict, Field, ValidationError, field_validator

from chem_agent_contracts.route_candidate import RouteSignatureV1
from chem_agent_contracts.v2 import StateTransitionV2, StrictModel

from .route_signed_event import TrustedIssuerPublicKeyV1

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # Importable without cryptography, but verification fails closed.
    InvalidSignature = None  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]


REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1 = (
    "reviewed_route_signature_manifest_v1"
)
SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1 = (
    "signed_reviewed_route_signature_manifest_v1"
)
_SIGNATURE_DOMAIN_V1 = b"chem-agent.reviewed-route-signature.v1\x00"
_ENVELOPE_FIELDS = frozenset({"schema_version", "key_id", "manifest", "signature"})
_SCOPE_FIELDS = frozenset({
    "paper_id", "experimental_group_id", "source_digest",
    "source_attestation_digest", "group_locator", "section", "document_kind",
    "source_doi", "target_material", "target_state", "target_objective",
})
_MANIFEST_FIELDS = _SCOPE_FIELDS | frozenset({
    "schema_version", "issuer", "review_basis", "route_signature",
})
_ROUTE_SIGNATURE_FIELDS = frozenset({
    "route_family", "target_transformation", "precursor_roles", "reagent_roles",
    "operations", "control_modes", "phase_transitions", "endpoint_state",
})
_TRANSITION_FIELDS = frozenset({"before_state", "after_state", "confidence"})
_STRING_LIST_FIELDS = (
    "precursor_roles", "reagent_roles", "operations", "control_modes",
)
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"sha256_[0-9a-f]{64}\Z")
_DOI = re.compile(r"10\.[0-9]{4,9}/\S+\Z", re.IGNORECASE)


class _ReviewStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReviewedRouteSignatureScopeV1(_ReviewStrictModel):
    """Expected identity supplied by the trusted PDF/attestation/goal caller.

    ``source_doi`` is the article DOI for a primary PDF and the parent article
    DOI for supporting information. The caller must take it from the verified
    source attestation, not from the proposed route.
    """

    paper_id: str = Field(min_length=1)
    experimental_group_id: str = Field(min_length=1)
    source_digest: str = Field(min_length=1)
    source_attestation_digest: str = Field(min_length=1)
    group_locator: str = Field(min_length=1)
    section: str = Field(min_length=1)
    document_kind: Literal["primary_paper", "supporting_information"]
    source_doi: str = Field(min_length=1)
    target_material: str = Field(min_length=1)
    target_state: str = Field(min_length=1)
    target_objective: str = Field(min_length=1)

    @field_validator(
        "paper_id", "experimental_group_id", "group_locator", "section",
        "target_material", "target_state", "target_objective",
    )
    @classmethod
    def validate_nonblank_exact_text(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("scope strings must be nonblank and unpadded")
        return value

    @field_validator("source_digest", "source_attestation_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("digest must be sha256_<64 lowercase hex chars>")
        return value

    @field_validator("source_doi")
    @classmethod
    def validate_source_doi(cls, value: str) -> str:
        if value != value.strip() or _DOI.fullmatch(value) is None:
            raise ValueError("source_doi must be an unpadded DOI")
        return value


class ReviewedRouteSignatureManifestV1(ReviewedRouteSignatureScopeV1):
    """An issuer's complete review declaration, scoped to one PDF group."""

    schema_version: Literal["reviewed_route_signature_manifest_v1"]
    issuer: str = Field(min_length=1)
    review_basis: str = Field(min_length=1)
    route_signature: RouteSignatureV1

    @field_validator("issuer", "review_basis")
    @classmethod
    def validate_review_text(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("review text must be nonblank and unpadded")
        return value


@dataclass(frozen=True)
class VerifiedRouteSignatureReviewV1:
    """Authenticated declaration bound to the caller's exact expected scope."""

    key_id: str
    review_digest: str
    manifest_digest: str
    manifest: ReviewedRouteSignatureManifestV1

    @property
    def route_signature(self) -> RouteSignatureV1:
        return self.manifest.route_signature


@dataclass(frozen=True)
class RouteSignatureReviewVerificationV1:
    verified: bool = False
    receipt: VerifiedRouteSignatureReviewV1 | None = None
    reason_code: str = ""


def _nonblank_unpadded(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _validated_manifest(
    manifest: Mapping[str, Any],
) -> tuple[ReviewedRouteSignatureManifestV1, dict[str, Any]]:
    """Require every signed leaf explicitly; forbid silent Pydantic defaults."""

    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != _MANIFEST_FIELDS
        or set(RouteSignatureV1.model_fields) != _ROUTE_SIGNATURE_FIELDS
        or set(StateTransitionV2.model_fields) != _TRANSITION_FIELDS
    ):
        raise ValueError("manifest fields invalid")
    raw = dict(manifest)
    signature = raw["route_signature"]
    if not isinstance(signature, Mapping) or set(signature) != _ROUTE_SIGNATURE_FIELDS:
        raise ValueError("route signature fields incomplete")
    signature = dict(signature)
    for field in ("route_family", "target_transformation", "endpoint_state"):
        if not _nonblank_unpadded(signature[field]):
            raise ValueError("route signature leaf empty")
    for field in _STRING_LIST_FIELDS:
        values = signature[field]
        if (
            not isinstance(values, list)
            or (field == "operations" and not values)
            or any(not _nonblank_unpadded(item) for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError("route signature list invalid")
    transitions = signature["phase_transitions"]
    if not isinstance(transitions, list):
        raise ValueError("phase transitions invalid")
    seen_transitions: set[tuple[str, str, str]] = set()
    for item in transitions:
        if not isinstance(item, Mapping) or set(item) != _TRANSITION_FIELDS:
            raise ValueError("phase transition fields incomplete")
        if any(not _nonblank_unpadded(item[field]) for field in _TRANSITION_FIELDS):
            raise ValueError("phase transition leaf empty")
        transition_key = (
            item["before_state"], item["after_state"], item["confidence"]
        )
        if transition_key in seen_transitions:
            raise ValueError("phase transition repeated")
        seen_transitions.add(transition_key)
    raw["route_signature"] = signature
    validated = ReviewedRouteSignatureManifestV1.model_validate(raw, strict=True)
    # RouteSignatureV1 normalizes states. Require the signed JSON to contain
    # the final controlled tokens so aliases cannot verify as different raw
    # statements with the same normalized model value.
    if validated.model_dump(mode="json") != raw:
        raise ValueError("manifest contains normalized or non-JSON values")
    return validated, raw


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def signed_reviewed_route_signature_message_v1(
    *, key_id: str, manifest: Mapping[str, Any]
) -> bytes:
    """Canonical domain-separated bytes for an external reviewer to sign.

    All route fields, the key ID, and every PDF/group/target binding are inside
    the signed bytes. This helper does not hold a private key or issue reviews.
    """

    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise ValueError("invalid key_id")
    _, raw = _validated_manifest(manifest)
    return _SIGNATURE_DOMAIN_V1 + _canonical_json_bytes({
        "schema_version": SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
        "key_id": key_id,
        "manifest": raw,
    })


def verify_reviewed_route_signature_manifest(
    *,
    envelope: Mapping[str, Any],
    trusted_public_keys: Mapping[str, TrustedIssuerPublicKeyV1],
    expected_scope: ReviewedRouteSignatureScopeV1,
) -> RouteSignatureReviewVerificationV1:
    """Verify the external signature and match every trusted scope field.

    ``expected_scope`` must come from the trusted PDF group enumeration,
    source attestation, and target goal. It must never be copied from the
    candidate, model proposal, or envelope being verified.
    """

    failure = RouteSignatureReviewVerificationV1
    if not isinstance(expected_scope, ReviewedRouteSignatureScopeV1):
        return failure(reason_code="expected_scope_invalid")
    try:
        scope = ReviewedRouteSignatureScopeV1.model_validate(
            expected_scope.model_dump(mode="python"), strict=True
        )
    except (ValidationError, ValueError, TypeError):
        return failure(reason_code="expected_scope_invalid")
    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS:
        return failure(reason_code="envelope_invalid")
    if envelope["schema_version"] != SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1:
        return failure(reason_code="schema_version_unsupported")
    key_id = envelope["key_id"]
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        return failure(reason_code="key_id_invalid")
    try:
        manifest, raw_manifest = _validated_manifest(envelope["manifest"])
        message = signed_reviewed_route_signature_message_v1(
            key_id=key_id, manifest=raw_manifest
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        return failure(reason_code="manifest_invalid")
    encoded_signature = envelope["signature"]
    if not isinstance(encoded_signature, str):
        return failure(reason_code="signature_encoding_invalid")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, binascii.Error):
        return failure(reason_code="signature_encoding_invalid")
    if base64.b64encode(signature).decode("ascii") != encoded_signature:
        return failure(reason_code="signature_encoding_invalid")
    if len(signature) != 64:
        return failure(reason_code="signature_length_invalid")
    if not isinstance(trusted_public_keys, Mapping) or key_id not in trusted_public_keys:
        return failure(reason_code="trusted_key_unknown")
    trusted_key = trusted_public_keys[key_id]
    if isinstance(trusted_key, bytes):
        return failure(reason_code="trusted_key_scope_missing")
    if not isinstance(trusted_key, TrustedIssuerPublicKeyV1):
        return failure(reason_code="trusted_key_invalid")
    if type(trusted_key.public_key_bytes) is not bytes or len(trusted_key.public_key_bytes) != 32:
        return failure(reason_code="trusted_key_invalid")
    if not _nonblank_unpadded(trusted_key.allowed_issuer):
        return failure(reason_code="trusted_key_scope_invalid")
    if Ed25519PublicKey is None or InvalidSignature is None:
        return failure(reason_code="ed25519_unavailable")
    try:
        Ed25519PublicKey.from_public_bytes(trusted_key.public_key_bytes).verify(
            signature, message
        )
    except Exception as exc:
        if isinstance(exc, InvalidSignature):
            return failure(reason_code="signature_mismatch")
        return failure(reason_code="ed25519_verification_error")
    if manifest.issuer != trusted_key.allowed_issuer:
        return failure(reason_code="issuer_not_authorized")
    if any(getattr(manifest, field) != getattr(scope, field) for field in _SCOPE_FIELDS):
        return failure(reason_code="review_scope_mismatch")
    return failure(
        verified=True,
        receipt=VerifiedRouteSignatureReviewV1(
            key_id=key_id,
            review_digest="sha256_" + sha256(_canonical_json_bytes({
                "schema_version": SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
                "key_id": key_id,
                "manifest": raw_manifest,
                "signature": encoded_signature,
            })).hexdigest(),
            manifest_digest="sha256_" + sha256(_canonical_json_bytes(raw_manifest)).hexdigest(),
            manifest=manifest,
        ),
    )


__all__ = [
    "REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1",
    "SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1",
    "ReviewedRouteSignatureManifestV1", "ReviewedRouteSignatureScopeV1",
    "RouteSignatureReviewVerificationV1", "VerifiedRouteSignatureReviewV1",
    "signed_reviewed_route_signature_message_v1",
    "verify_reviewed_route_signature_manifest",
]
