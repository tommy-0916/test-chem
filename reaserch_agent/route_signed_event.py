"""Offline Ed25519 verification for externally issued acquisition events.

The caller supplies a trusted ``key_id -> (public key, allowed issuer)`` mapping
out of band.  Neither an envelope nor a source attestation can add a trusted
key or authorize an issuer.  This module verifies an issuer's signature over
the complete event; it does not decide whether the issuer reviewed source
identity well.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import re
from typing import Any, Mapping

from pydantic import ValidationError

from .route_attestation import TrustedAcquisitionEventV1

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # The verifier must remain importable, but never accept a signature.
    InvalidSignature = None  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]


SIGNED_EVENT_SCHEMA_VERSION_V1 = "signed_trusted_acquisition_event_v1"
_SIGNATURE_DOMAIN_V1 = b"chem-agent.trusted-acquisition-event.v1\x00"
_ENVELOPE_FIELDS = frozenset({"schema_version", "key_id", "event", "signature"})
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ED25519_PUBLIC_KEY_BYTES = 32
_ED25519_SIGNATURE_BYTES = 64


@dataclass(frozen=True)
class TrustedIssuerPublicKeyV1:
    """Deployment-owned Ed25519 key authorized for exactly one event issuer."""

    public_key_bytes: bytes
    allowed_issuer: str


@dataclass(frozen=True)
class SignedEventVerificationV1:
    verified: bool = False
    event: TrustedAcquisitionEventV1 | None = None
    reason_code: str = ""


def _valid_key_id(key_id: object) -> bool:
    return isinstance(key_id, str) and _KEY_ID.fullmatch(key_id) is not None


def signed_trusted_acquisition_event_message_v1(
    *, key_id: str, event: Mapping[str, Any]
) -> bytes:
    """Return the exact domain-separated bytes an external issuer must sign.

    The UTF-8 JSON uses sorted keys, compact separators and no ASCII escaping.
    Its top-level object includes the envelope version, key ID and the entire
    strictly validated ``TrustedAcquisitionEventV1`` payload.  Signature bytes
    and any envelope metadata are never part of the signed message.
    """

    if not _valid_key_id(key_id):
        raise ValueError("invalid key_id")
    if not isinstance(event, Mapping):
        raise ValueError("event must be an object")
    validated = TrustedAcquisitionEventV1.model_validate(dict(event), strict=True)
    payload = {
        "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
        "key_id": key_id,
        "event": validated.model_dump(mode="json"),
    }
    return _SIGNATURE_DOMAIN_V1 + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def verify_signed_trusted_acquisition_event(
    *,
    envelope: Mapping[str, Any],
    trusted_public_keys: Mapping[str, TrustedIssuerPublicKeyV1 | bytes],
) -> SignedEventVerificationV1:
    """Authenticate one envelope and return its event only after verification.

    ``trusted_public_keys`` must come from caller-controlled configuration.
    Each entry binds raw key bytes to one exact allowed issuer.  Legacy bare
    key bytes are rejected until the caller supplies that issuer binding.  A
    string or key embedded in the envelope is never used as a public key.
    Failure always returns ``verified=False`` and ``event=None``.
    """

    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS:
        return SignedEventVerificationV1(reason_code="envelope_invalid")
    if envelope["schema_version"] != SIGNED_EVENT_SCHEMA_VERSION_V1:
        return SignedEventVerificationV1(reason_code="schema_version_unsupported")
    key_id = envelope["key_id"]
    if not _valid_key_id(key_id):
        return SignedEventVerificationV1(reason_code="key_id_invalid")
    event_payload = envelope["event"]
    if not isinstance(event_payload, Mapping):
        return SignedEventVerificationV1(reason_code="event_invalid")
    try:
        event = TrustedAcquisitionEventV1.model_validate(
            dict(event_payload), strict=True
        )
        message = signed_trusted_acquisition_event_message_v1(
            key_id=key_id, event=event.model_dump(mode="python")
        )
    except (ValidationError, ValueError, TypeError, UnicodeError):
        return SignedEventVerificationV1(reason_code="event_invalid")

    encoded_signature = envelope["signature"]
    if not isinstance(encoded_signature, str):
        return SignedEventVerificationV1(reason_code="signature_encoding_invalid")
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, binascii.Error):
        return SignedEventVerificationV1(reason_code="signature_encoding_invalid")
    if base64.b64encode(signature).decode("ascii") != encoded_signature:
        return SignedEventVerificationV1(reason_code="signature_encoding_invalid")
    if len(signature) != _ED25519_SIGNATURE_BYTES:
        return SignedEventVerificationV1(reason_code="signature_length_invalid")

    if not isinstance(trusted_public_keys, Mapping) or key_id not in trusted_public_keys:
        return SignedEventVerificationV1(reason_code="trusted_key_unknown")
    trusted_key = trusted_public_keys[key_id]
    if isinstance(trusted_key, bytes):
        return SignedEventVerificationV1(reason_code="trusted_key_scope_missing")
    if not isinstance(trusted_key, TrustedIssuerPublicKeyV1):
        return SignedEventVerificationV1(reason_code="trusted_key_invalid")
    public_key_bytes = trusted_key.public_key_bytes
    if type(public_key_bytes) is not bytes or len(public_key_bytes) != _ED25519_PUBLIC_KEY_BYTES:
        return SignedEventVerificationV1(reason_code="trusted_key_invalid")
    allowed_issuer = trusted_key.allowed_issuer
    if not isinstance(allowed_issuer, str) or not allowed_issuer.strip():
        return SignedEventVerificationV1(reason_code="trusted_key_scope_invalid")
    if Ed25519PublicKey is None or InvalidSignature is None:
        return SignedEventVerificationV1(reason_code="ed25519_unavailable")

    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature, message)
    except Exception as exc:
        if isinstance(exc, InvalidSignature):
            return SignedEventVerificationV1(reason_code="signature_mismatch")
        return SignedEventVerificationV1(reason_code="ed25519_verification_error")
    if event.issuer != allowed_issuer:
        return SignedEventVerificationV1(reason_code="issuer_not_authorized")
    return SignedEventVerificationV1(verified=True, event=event)


__all__ = [
    "SIGNED_EVENT_SCHEMA_VERSION_V1", "SignedEventVerificationV1",
    "TrustedIssuerPublicKeyV1",
    "signed_trusted_acquisition_event_message_v1",
    "verify_signed_trusted_acquisition_event",
]
