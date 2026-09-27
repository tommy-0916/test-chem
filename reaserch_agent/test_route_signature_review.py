"""Fail-closed tests for externally reviewed route-signature manifests."""

from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
import json
from unittest import TestCase, main, skipIf
from unittest.mock import patch

from chem_agent_contracts.route_candidate import RouteSignatureV1

from reaserch_agent import route_signature_review
from reaserch_agent.route_signature_review import (
    REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1,
    SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
    ReviewedRouteSignatureScopeV1,
    TrustedIssuerPublicKeyV1,
    signed_reviewed_route_signature_message_v1,
    verify_reviewed_route_signature_manifest,
)

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    serialization = None
    Ed25519PrivateKey = None


def _scope() -> ReviewedRouteSignatureScopeV1:
    return ReviewedRouteSignatureScopeV1(
        paper_id="paper-1",
        experimental_group_id="control synthesis",
        source_digest="sha256_" + "a" * 64,
        source_attestation_digest="sha256_" + "b" * 64,
        group_locator="pdf:p2:b4-p2:b9",
        section="Methods",
        document_kind="primary_paper",
        source_doi="10.1000/example",
        target_material="NiFe precipitate",
        target_state="retained_wet_solid",
        target_objective="prepare reference sample",
    )


def _manifest() -> dict:
    return {
        "schema_version": REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1,
        **_scope().model_dump(mode="json"),
        "issuer": "independent-route-reviewer",
        "review_basis": "Reviewed original PDF Methods, control synthesis, p. 2 blocks 4-9",
        "route_signature": {
            "route_family": "precipitation",
            "target_transformation": "solution_to_wet_solid",
            "precursor_roles": ["metal_salt"],
            "reagent_roles": ["base"],
            "operations": ["dissolve", "precipitate"],
            "control_modes": ["pH_feedback"],
            "phase_transitions": [{
                "before_state": "solution",
                "after_state": "retained_wet_solid",
                "confidence": "explicit",
            }],
            "endpoint_state": "retained_wet_solid",
        },
    }


class RouteSignatureReviewMissingDependencyTest(TestCase):
    def test_missing_cryptography_fails_closed(self) -> None:
        envelope = {
            "schema_version": SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
            "key_id": "review-key-2026",
            "manifest": _manifest(),
            "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
        }
        with patch.object(route_signature_review, "Ed25519PublicKey", None):
            result = verify_reviewed_route_signature_manifest(
                envelope=envelope,
                trusted_public_keys={
                    "review-key-2026": TrustedIssuerPublicKeyV1(
                        public_key_bytes=b"\x00" * 32,
                        allowed_issuer=_manifest()["issuer"],
                    )
                },
                expected_scope=_scope(),
            )
        self.assertFalse(result.verified)
        self.assertIsNone(result.receipt)
        self.assertEqual(result.reason_code, "ed25519_unavailable")


@skipIf(Ed25519PrivateKey is None, "cryptography unavailable")
class RouteSignatureReviewVerificationTest(TestCase):
    def setUp(self) -> None:
        # Test-only ephemeral key. The production verifier only receives public bytes.
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = "review-key-2026"
        self.scope = _scope()
        self.manifest = _manifest()
        self.envelope = self._signed(self.manifest)
        self.keys = {self.key_id: TrustedIssuerPublicKeyV1(
            public_key_bytes=self.public_key,
            allowed_issuer=self.manifest["issuer"],
        )}

    def _signed(self, manifest: dict, key_id: str | None = None) -> dict:
        key_id = self.key_id if key_id is None else key_id
        message = signed_reviewed_route_signature_message_v1(
            key_id=key_id, manifest=manifest
        )
        return {
            "schema_version": SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
            "key_id": key_id,
            "manifest": manifest,
            "signature": base64.b64encode(self.private_key.sign(message)).decode("ascii"),
        }

    def _verify(self, envelope=None, keys=None, scope=None):
        return verify_reviewed_route_signature_manifest(
            envelope=self.envelope if envelope is None else envelope,
            trusted_public_keys=self.keys if keys is None else keys,
            expected_scope=self.scope if scope is None else scope,
        )

    def _reject(self, reason: str, envelope=None, keys=None, scope=None) -> None:
        result = self._verify(envelope, keys, scope)
        self.assertFalse(result.verified)
        self.assertIsNone(result.receipt)
        self.assertEqual(result.reason_code, reason)

    def test_verified_receipt_binds_entire_envelope_and_signature(self) -> None:
        result = self._verify()
        self.assertTrue(result.verified)
        self.assertEqual(result.reason_code, "")
        receipt = result.receipt
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.key_id, self.key_id)
        self.assertEqual(receipt.manifest.issuer, self.manifest["issuer"])
        self.assertIsInstance(receipt.route_signature, RouteSignatureV1)
        self.assertEqual(
            receipt.route_signature.model_dump(mode="json"),
            self.manifest["route_signature"],
        )
        canonical_envelope = json.dumps(
            self.envelope, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            receipt.review_digest,
            "sha256_" + sha256(canonical_envelope).hexdigest(),
        )

    def test_canonical_message_ignores_mapping_key_order_but_not_list_order(self) -> None:
        reordered_manifest = dict(reversed(list(self.manifest.items())))
        reordered_manifest["route_signature"] = dict(reversed(list(
            self.manifest["route_signature"].items()
        )))
        self.assertTrue(self._verify({**self.envelope, "manifest": reordered_manifest}).verified)
        message = signed_reviewed_route_signature_message_v1(
            key_id=self.key_id, manifest=reordered_manifest
        )
        self.assertTrue(message.startswith(b"chem-agent.reviewed-route-signature.v1\x00"))
        changed = deepcopy(self.manifest)
        changed["route_signature"]["operations"].reverse()
        self._reject("signature_mismatch", {**self.envelope, "manifest": changed})

    def test_every_signature_field_and_transition_content_is_signed(self) -> None:
        changes = {
            "route_family": "sol_gel",
            "target_transformation": "solution_to_dry_solid",
            "precursor_roles": ["different_precursor"],
            "reagent_roles": ["different_reagent"],
            "operations": ["mix", "precipitate"],
            "control_modes": ["temperature_feedback"],
            "phase_transitions": [{
                "before_state": "solution",
                "after_state": "dry_solid",
                "confidence": "explicit",
            }],
            "endpoint_state": "dry_solid",
        }
        for field, changed_value in changes.items():
            with self.subTest(field=field):
                changed = deepcopy(self.manifest)
                changed["route_signature"][field] = changed_value
                self._reject("signature_mismatch", {
                    **self.envelope, "manifest": changed,
                })

    def test_every_scope_leaf_is_signed_and_must_match_expected_scope(self) -> None:
        changes = {
            "paper_id": "paper-2",
            "experimental_group_id": "treated synthesis",
            "source_digest": "sha256_" + "c" * 64,
            "source_attestation_digest": "sha256_" + "d" * 64,
            "group_locator": "pdf:p2:b5-p2:b9",
            "section": "Supporting Methods",
            "document_kind": "supporting_information",
            "source_doi": "10.1000/other",
            "target_material": "wrong product",
            "target_state": "dry_solid",
            "target_objective": "different experiment",
        }
        for field, changed_value in changes.items():
            with self.subTest(field=field):
                changed = {**self.manifest, field: changed_value}
                self._reject(
                    "signature_mismatch", {**self.envelope, "manifest": changed}
                )
                self._reject("review_scope_mismatch", self._signed(changed))

    def test_missing_scope_leaf_or_review_basis_is_invalid(self) -> None:
        for field in (
            "paper_id", "experimental_group_id", "source_digest",
            "source_attestation_digest", "group_locator", "section",
            "document_kind", "source_doi", "target_material", "target_state",
            "target_objective",
            "review_basis", "issuer",
        ):
            with self.subTest(field=field):
                changed = {k: v for k, v in self.manifest.items() if k != field}
                self._reject("manifest_invalid", {**self.envelope, "manifest": changed})
        for field in ("group_locator", "section", "review_basis", "target_material"):
            with self.subTest(field=field):
                self._reject("manifest_invalid", {**self.envelope, "manifest": {
                    **self.manifest, field: "  ",
                }})

    def test_all_route_signature_fields_are_explicit_even_when_list_empty(self) -> None:
        for field in self.manifest["route_signature"]:
            with self.subTest(field=field):
                signature = {
                    key: value for key, value in self.manifest["route_signature"].items()
                    if key != field
                }
                self._reject("manifest_invalid", {
                    **self.envelope,
                    "manifest": {**self.manifest, "route_signature": signature},
                })
        changed = deepcopy(self.manifest)
        for field in (
            "precursor_roles", "reagent_roles", "control_modes", "phase_transitions"
        ):
            changed["route_signature"][field] = []
        self.assertTrue(self._verify(self._signed(changed)).verified)

    def test_empty_or_repeated_signature_content_is_invalid(self) -> None:
        cases = (
            ("route_family", " "),
            ("target_transformation", ""),
            ("endpoint_state", ""),
            ("operations", []),
            ("operations", ["dissolve", "dissolve"]),
            ("operations", ["dissolve", ""]),
            ("precursor_roles", ["metal_salt", "metal_salt"]),
            ("reagent_roles", ["base", " "]),
            ("control_modes", [" pH_feedback"]),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                changed = deepcopy(self.manifest)
                changed["route_signature"][field] = value
                self._reject("manifest_invalid", {
                    **self.envelope, "manifest": changed,
                })

    def test_transitions_require_all_nonempty_leaves_and_exact_state_tokens(self) -> None:
        for field in ("before_state", "after_state", "confidence"):
            with self.subTest(field=field):
                changed = deepcopy(self.manifest)
                del changed["route_signature"]["phase_transitions"][0][field]
                self._reject("manifest_invalid", {
                    **self.envelope, "manifest": changed,
                })
                changed = deepcopy(self.manifest)
                changed["route_signature"]["phase_transitions"][0][field] = ""
                self._reject("manifest_invalid", {
                    **self.envelope, "manifest": changed,
                })
        changed = deepcopy(self.manifest)
        changed["route_signature"]["phase_transitions"][0]["after_state"] = "湿固体"
        self._reject("manifest_invalid", {**self.envelope, "manifest": changed})
        changed = deepcopy(self.manifest)
        changed["route_signature"]["phase_transitions"].append(
            deepcopy(changed["route_signature"]["phase_transitions"][0])
        )
        self._reject("manifest_invalid", {**self.envelope, "manifest": changed})

    def test_signed_issuer_must_match_external_key_authorization(self) -> None:
        self._reject("issuer_not_authorized", keys={
            self.key_id: TrustedIssuerPublicKeyV1(self.public_key, "other-reviewer")
        })
        changed = {**self.manifest, "issuer": "other-reviewer"}
        self._reject("issuer_not_authorized", self._signed(changed))

    def test_raw_unsigned_embedded_key_and_bad_signature_are_rejected(self) -> None:
        self._reject("envelope_invalid", self.manifest)
        self._reject("envelope_invalid", {
            **self.envelope, "public_key": self.public_key,
        })
        self._reject("envelope_invalid", {
            key: value for key, value in self.envelope.items() if key != "signature"
        })
        self._reject("signature_length_invalid", {
            **self.envelope, "signature": "",
        })
        self._reject("signature_length_invalid", {
            **self.envelope, "signature": base64.b64encode(b"x" * 63).decode("ascii"),
        })
        changed = bytearray(base64.b64decode(self.envelope["signature"]))
        changed[0] ^= 1
        self._reject("signature_mismatch", {
            **self.envelope, "signature": base64.b64encode(changed).decode("ascii"),
        })

    def test_unknown_unscoped_or_changed_key_id_fails_closed(self) -> None:
        self._reject("trusted_key_unknown", keys={})
        self._reject("trusted_key_scope_missing", keys={self.key_id: self.public_key})
        self._reject("trusted_key_scope_invalid", keys={
            self.key_id: TrustedIssuerPublicKeyV1(self.public_key, " ")
        })
        self._reject("signature_mismatch", {
            **self.envelope, "key_id": "other-key",
        }, keys={"other-key": self.keys[self.key_id]})
        self._reject("trusted_key_invalid", keys={
            self.key_id: TrustedIssuerPublicKeyV1(b"x" * 31, self.manifest["issuer"])
        })

    def test_expected_scope_must_be_externally_supplied_typed_context(self) -> None:
        self._reject("expected_scope_invalid", scope=self.scope.model_dump(mode="json"))
        self._reject("review_scope_mismatch", scope=self.scope.model_copy(
            update={"target_material": "different material"}
        ))


if __name__ == "__main__":
    main()
