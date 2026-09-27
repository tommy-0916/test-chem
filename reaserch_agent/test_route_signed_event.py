"""Offline verification of externally signed acquisition event envelopes."""

from __future__ import annotations

import base64
from unittest import TestCase, main, skipIf
from unittest.mock import patch

from reaserch_agent import route_signed_event
from reaserch_agent.route_attestation import TrustedAcquisitionEventV1
from reaserch_agent.route_signed_event import (
    SIGNED_EVENT_SCHEMA_VERSION_V1,
    TrustedIssuerPublicKeyV1,
    signed_trusted_acquisition_event_message_v1,
    verify_signed_trusted_acquisition_event,
)

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    serialization = None
    Ed25519PrivateKey = None


def _event() -> dict[str, str]:
    return TrustedAcquisitionEventV1(
        schema_version="trusted_acquisition_event_v1",
        paper_id="doi_10_1000_paper",
        kb_relative_path="_pdf_sources/campaign/paper.pdf",
        document_digest="sha256_" + "a" * 64,
        document_kind="primary_paper",
        attestation_digest="sha256_" + "b" * 64,
        issuer="independent-acquisition-service",
        identity_verdict="primary_verified",
    ).model_dump(mode="json")


class SignedEventMissingDependencyTest(TestCase):
    def test_missing_cryptography_fails_closed_without_returning_event(self) -> None:
        envelope = {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": "issuer-key-2026",
            "event": _event(),
            "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
        }
        with patch.object(route_signed_event, "Ed25519PublicKey", None):
            result = verify_signed_trusted_acquisition_event(
                envelope=envelope,
                trusted_public_keys={
                    "issuer-key-2026": TrustedIssuerPublicKeyV1(
                        public_key_bytes=b"\x00" * 32,
                        allowed_issuer=_event()["issuer"],
                    )
                },
            )
        self.assertFalse(result.verified)
        self.assertIsNone(result.event)
        self.assertEqual(result.reason_code, "ed25519_unavailable")


@skipIf(Ed25519PrivateKey is None, "cryptography unavailable")
class SignedEventVerificationTest(TestCase):
    def setUp(self) -> None:
        # The test key exists only in memory and is never written to the repo.
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = "issuer-key-2026"
        self.event = _event()
        message = signed_trusted_acquisition_event_message_v1(
            key_id=self.key_id, event=self.event
        )
        self.envelope = {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": self.key_id,
            "event": self.event,
            "signature": base64.b64encode(
                self.private_key.sign(message)
            ).decode("ascii"),
        }
        self.trusted_keys = {
            self.key_id: TrustedIssuerPublicKeyV1(
                public_key_bytes=self.public_key,
                allowed_issuer=self.event["issuer"],
            )
        }

    def _verify(self, envelope=None, trusted_keys=None):
        return verify_signed_trusted_acquisition_event(
            envelope=self.envelope if envelope is None else envelope,
            trusted_public_keys=self.trusted_keys if trusted_keys is None else trusted_keys,
        )

    def _assert_rejected(self, reason_code: str, envelope=None, trusted_keys=None) -> None:
        result = self._verify(envelope, trusted_keys)
        self.assertFalse(result.verified)
        self.assertIsNone(result.event)
        self.assertEqual(result.reason_code, reason_code)

    def test_valid_signature_returns_trusted_event(self) -> None:
        result = self._verify()
        self.assertTrue(result.verified)
        self.assertEqual(result.reason_code, "")
        self.assertIsInstance(result.event, TrustedAcquisitionEventV1)
        self.assertEqual(result.event.model_dump(mode="json"), self.event)

    def test_canonical_message_is_independent_of_event_key_order(self) -> None:
        reordered = dict(reversed(list(self.event.items())))
        result = self._verify({**self.envelope, "event": reordered})
        self.assertTrue(result.verified)
        message = signed_trusted_acquisition_event_message_v1(
            key_id=self.key_id, event=reordered
        )
        self.assertTrue(message.startswith(b"chem-agent.trusted-acquisition-event.v1\x00"))
        self.assertIn(b'"key_id":"issuer-key-2026"', message)

    def test_tampered_event_or_attestation_digest_is_rejected(self) -> None:
        for field, changed in (
            ("paper_id", "doi_10_1000_other"),
            ("attestation_digest", "sha256_" + "c" * 64),
        ):
            with self.subTest(field=field):
                tampered = {**self.event, field: changed}
                self._assert_rejected(
                    "signature_mismatch", {**self.envelope, "event": tampered}
                )

    def test_unknown_key_id_and_changed_key_id_are_rejected(self) -> None:
        self._assert_rejected(
            "trusted_key_unknown",
            {**self.envelope, "key_id": "unregistered-key"},
        )
        self._assert_rejected(
            "signature_mismatch",
            {**self.envelope, "key_id": "different-key"},
            {"different-key": self.trusted_keys[self.key_id]},
        )

    def test_signed_event_issuer_must_match_key_authorization(self) -> None:
        other_issuer = "different-acquisition-service"
        self._assert_rejected(
            "issuer_not_authorized",
            trusted_keys={
                self.key_id: TrustedIssuerPublicKeyV1(
                    public_key_bytes=self.public_key,
                    allowed_issuer=other_issuer,
                )
            },
        )

        # The same private key can sign another issuer's event.  Signature
        # validity alone must not grant that issuer authority.
        other_event = {**self.event, "issuer": other_issuer}
        other_message = signed_trusted_acquisition_event_message_v1(
            key_id=self.key_id, event=other_event
        )
        other_envelope = {
            **self.envelope,
            "event": other_event,
            "signature": base64.b64encode(
                self.private_key.sign(other_message)
            ).decode("ascii"),
        }
        self._assert_rejected("issuer_not_authorized", other_envelope)

    def test_key_without_valid_issuer_scope_fails_closed(self) -> None:
        self._assert_rejected(
            "trusted_key_scope_missing", trusted_keys={self.key_id: self.public_key}
        )
        for allowed_issuer in ("", "  ", None):
            with self.subTest(allowed_issuer=allowed_issuer):
                self._assert_rejected(
                    "trusted_key_scope_invalid",
                    trusted_keys={
                        self.key_id: TrustedIssuerPublicKeyV1(
                            public_key_bytes=self.public_key,
                            allowed_issuer=allowed_issuer,
                        )
                    },
                )

    def test_bad_signature_encoding_length_and_signature_are_rejected(self) -> None:
        self._assert_rejected(
            "signature_encoding_invalid",
            {**self.envelope, "signature": "!!not-base64!!"},
        )
        self._assert_rejected(
            "signature_encoding_invalid",
            {**self.envelope, "signature": self.envelope["signature"] + "\n"},
        )
        self._assert_rejected(
            "signature_length_invalid",
            {**self.envelope, "signature": base64.b64encode(b"\x00" * 63).decode("ascii")},
        )
        changed = bytearray(base64.b64decode(self.envelope["signature"]))
        changed[0] ^= 1
        self._assert_rejected(
            "signature_mismatch",
            {**self.envelope, "signature": base64.b64encode(changed).decode("ascii")},
        )

    def test_public_key_must_be_external_raw_32_bytes(self) -> None:
        self._assert_rejected(
            "trusted_key_invalid",
            trusted_keys={
                self.key_id: TrustedIssuerPublicKeyV1(b"x" * 31, self.event["issuer"])
            },
        )
        self._assert_rejected(
            "trusted_key_invalid",
            trusted_keys={
                self.key_id: TrustedIssuerPublicKeyV1(
                    base64.b64encode(self.public_key).decode("ascii"),
                    self.event["issuer"],
                )
            },
        )
        self._assert_rejected(
            "envelope_invalid",
            {**self.envelope, "public_key": self.public_key},
        )

    def test_version_and_event_structure_are_checked(self) -> None:
        self._assert_rejected(
            "schema_version_unsupported",
            {**self.envelope, "schema_version": "signed_trusted_acquisition_event_v2"},
        )
        self._assert_rejected(
            "event_invalid",
            {**self.envelope, "event": {**self.event, "attestation_digest": "bad"}},
        )
        self._assert_rejected(
            "envelope_invalid", {k: v for k, v in self.envelope.items() if k != "event"}
        )


if __name__ == "__main__":
    main()
