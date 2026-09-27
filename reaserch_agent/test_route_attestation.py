"""Independent route PDF identity receipt checks."""

from __future__ import annotations

import base64
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from chem_agent_contracts.v2 import canonical_digest
from reaserch_agent.route_attestation import (
    SourceDocumentAttestationV1,
    TrustedAcquisitionEventV1,
    attestation_path_for,
    attested_route_sources,
    verify_source_document_attestation,
)
from reaserch_agent import route_signed_event
from reaserch_agent.route_signed_event import (
    SIGNED_EVENT_SCHEMA_VERSION_V1,
    TrustedIssuerPublicKeyV1,
    signed_trusted_acquisition_event_message_v1,
)
from reaserch_agent.tools.paper_registry import PaperRegistry

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    serialization = None
    Ed25519PrivateKey = None


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class RouteAttestationTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pdf = self.root / "_pdf_sources" / "campaign" / "paper.pdf"
        self.pdf.parent.mkdir(parents=True)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(fitz.Point(72, 72), "Primary article methods and DOI")
        doc.save(str(self.pdf))
        doc.close()
        self.document_digest = "sha256_" + sha256(self.pdf.read_bytes()).hexdigest()
        self.paper_id = "doi_10_1000_paper"
        self.issuer = "independent-acquisition-service"

    def _attestation(self, *, kind: str = "primary_paper") -> SourceDocumentAttestationV1:
        is_si = kind == "supporting_information"
        return SourceDocumentAttestationV1(
            schema_version="source_document_attestation_v1",
            paper_id=self.paper_id,
            kb_relative_path="_pdf_sources/campaign/paper.pdf",
            document_digest=self.document_digest,
            document_kind=kind,
            doi="" if is_si else "10.1000/paper",
            parent_doi="10.1000/paper" if is_si else "",
            acquisition_url="https://publisher.example/article/10.1000/paper",
            request_url="https://publisher.example/files/paper.pdf",
            final_url="https://cdn.publisher.example/files/paper.pdf",
            identity_evidence_type="publisher_si_link" if is_si else "publisher_doi_link",
            identity_evidence_url="https://publisher.example/article/10.1000/paper",
            identity_evidence_digest="sha256_" + "a" * 64,
            identity_status="verified",
            issuer=self.issuer,
            issued_at="2026-09-27T12:00:00Z",
        )

    def _write_attestation(
        self, attestation: SourceDocumentAttestationV1
    ) -> tuple[Path, TrustedAcquisitionEventV1]:
        path = attestation_path_for(
            self.root, attestation.paper_id, attestation.document_digest
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(attestation.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id=attestation.paper_id,
            kb_relative_path=attestation.kb_relative_path,
            document_digest=attestation.document_digest,
            document_kind=attestation.document_kind,
            attestation_digest=canonical_digest(attestation.model_dump(mode="json")),
            issuer=attestation.issuer,
            identity_verdict=(
                "si_verified" if attestation.document_kind == "supporting_information"
                else "primary_verified"
            ),
        )
        return path, event

    def _register(self, *, status: str = "verified_doi") -> None:
        PaperRegistry(self.root).upsert(
            title="Primary study", doi="10.1000/paper",
            verification_status=status,
            full_text_status="parsed",
            pdf_file=str(self.pdf),
        )

    def _signed_event(
        self, event: TrustedAcquisitionEventV1
    ) -> tuple[dict, dict[str, TrustedIssuerPublicKeyV1]]:
        if Ed25519PrivateKey is None or serialization is None:
            self.skipTest("cryptography unavailable")
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        key_id = "independent-issuer-key"
        payload = event.model_dump(mode="json")
        message = signed_trusted_acquisition_event_message_v1(
            key_id=key_id, event=payload
        )
        envelope = {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": key_id,
            "event": payload,
            "signature": base64.b64encode(private_key.sign(message)).decode("ascii"),
        }
        keys = {
            key_id: TrustedIssuerPublicKeyV1(
                public_key_bytes=public_key, allowed_issuer=self.issuer
            )
        }
        return envelope, keys

    def test_independent_event_and_artifact_bind_exact_pdf_and_registry(self) -> None:
        attestation = self._attestation()
        path, event = self._write_attestation(attestation)
        self._register()
        check = verify_source_document_attestation(
            kb_root=self.root, trusted_event=event
        )
        self.assertTrue(check.verified, check.reasons)
        self.assertEqual(check.document_digest, self.document_digest)
        self.assertEqual(check.attestation_path, str(path))
        envelope, keys = self._signed_event(event)
        indexed = attested_route_sources(
            self.root, [envelope], trusted_public_keys=keys
        )
        self.assertEqual(len(indexed[self.paper_id]), 1)
        self.assertEqual(indexed[self.paper_id][0].path, self.pdf)
        self.assertEqual(
            indexed[self.paper_id][0].document_digest, self.document_digest
        )
        self.assertEqual(indexed[self.paper_id][0].doi, "10.1000/paper")
        self.assertEqual(
            indexed[self.paper_id][0].attestation_digest,
            event.attestation_digest,
        )

    def test_verified_doi_and_local_file_without_event_do_not_index(self) -> None:
        self._register()
        self.assertEqual(
            attested_route_sources(self.root, [], trusted_public_keys={}), {}
        )
        self.assertEqual(
            attested_route_sources(self.root, None, trusted_public_keys={}), {}
        )
        self.assertEqual(
            attested_route_sources(
                self.root, [{"issuer": self.issuer}], trusted_public_keys={}
            ), {}
        )
        self.assertIn(
            "trusted_event_missing",
            verify_source_document_attestation(
                kb_root=self.root, trusted_event=None
            ).reasons,
        )
        self._register(status="local_file")
        self.assertEqual(
            attested_route_sources(self.root, [], trusted_public_keys={}), {}
        )

    def test_self_asserted_issuer_in_artifact_does_not_create_trust(self) -> None:
        self._write_attestation(self._attestation())
        self._register()
        self.assertEqual(
            attested_route_sources(self.root, [], trusted_public_keys={}), {}
        )

    def test_tampered_attestation_is_rejected_by_external_event_digest(self) -> None:
        path, event = self._write_attestation(self._attestation())
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["final_url"] = "https://attacker.example/wrong.pdf"
        path.write_text(json.dumps(payload), encoding="utf-8")
        check = verify_source_document_attestation(
            kb_root=self.root, trusted_event=event
        )
        self.assertFalse(check.verified)
        self.assertIn("attestation_digest_mismatch", check.reasons)

    def test_changed_pdf_bytes_cannot_reuse_event(self) -> None:
        _, event = self._write_attestation(self._attestation())
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\nchanged")
        check = verify_source_document_attestation(
            kb_root=self.root, trusted_event=event
        )
        self.assertFalse(check.verified)
        self.assertIn("source_digest_or_format_mismatch", check.reasons)

    def test_wrong_registry_association_cannot_index_document(self) -> None:
        _, event = self._write_attestation(self._attestation())
        envelope, keys = self._signed_event(event)
        self.assertEqual(
            attested_route_sources(self.root, [envelope], trusted_public_keys=keys), {}
        )
        PaperRegistry(self.root).upsert(
            title="Other study", doi="10.1000/other",
            verification_status="verified_doi", full_text_status="parsed",
            pdf_file=str(self.pdf),
        )
        self.assertEqual(
            attested_route_sources(self.root, [envelope], trusted_public_keys=keys), {}
        )

    def test_registry_doi_must_match_attested_document_identity(self) -> None:
        _, event = self._write_attestation(self._attestation())
        envelope, keys = self._signed_event(event)
        self._register()
        registry_path = self.root / "registry" / "papers.jsonl"
        record = json.loads(registry_path.read_text(encoding="utf-8").splitlines()[0])
        record["doi"] = "10.1000/different"
        registry_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.assertEqual(
            attested_route_sources(self.root, [envelope], trusted_public_keys=keys), {}
        )

    def test_public_index_rejects_raw_event_and_tampered_envelope(self) -> None:
        _, event = self._write_attestation(self._attestation())
        self._register()
        envelope, keys = self._signed_event(event)
        self.assertEqual(
            attested_route_sources(self.root, [event], trusted_public_keys=keys), {}
        )
        tampered = {
            **envelope,
            "event": {**envelope["event"], "attestation_digest": "sha256_" + "0" * 64},
        }
        self.assertEqual(
            attested_route_sources(self.root, [tampered], trusted_public_keys=keys), {}
        )
        self.assertEqual(
            attested_route_sources(self.root, [envelope], trusted_public_keys={}), {}
        )

    def test_public_index_fails_closed_when_ed25519_unavailable(self) -> None:
        _, event = self._write_attestation(self._attestation())
        self._register()
        envelope, keys = self._signed_event(event)
        with patch.object(route_signed_event, "Ed25519PublicKey", None):
            indexed = attested_route_sources(
                self.root, [envelope], trusted_public_keys=keys
            )
        self.assertEqual(indexed, {})

    def test_supporting_information_requires_parent_doi_and_si_evidence(self) -> None:
        attestation = self._attestation(kind="supporting_information")
        _, event = self._write_attestation(attestation)
        self._register()
        check = verify_source_document_attestation(
            kb_root=self.root, trusted_event=event
        )
        self.assertTrue(check.verified, check.reasons)
        self.assertEqual(check.document_kind, "supporting_information")
        payload = attestation.model_dump(mode="python")
        payload["parent_doi"] = ""
        with self.assertRaises(ValidationError):
            SourceDocumentAttestationV1.model_validate(payload, strict=True)

    def test_unresolved_identity_and_wrong_event_verdict_cannot_pass(self) -> None:
        attestation = self._attestation()
        attestation.identity_status = "unresolved"
        _, event = self._write_attestation(attestation)
        result = verify_source_document_attestation(
            kb_root=self.root, trusted_event=event
        )
        self.assertFalse(result.verified)
        self.assertIn("attestation_identity_mismatch", result.reasons)
        payload = event.model_dump(mode="python")
        payload["identity_verdict"] = "si_verified"
        with self.assertRaises(ValidationError):
            TrustedAcquisitionEventV1.model_validate(payload, strict=True)

    def test_absolute_traversal_or_markdown_source_cannot_be_attested(self) -> None:
        for raw in [
            "../outside.pdf",
            "_pdf_sources/campaign/../other.pdf",
            "C:/outside.pdf",
            "_pdf_sources/campaign/source.md",
        ]:
            payload = self._attestation().model_dump(mode="python")
            payload["kb_relative_path"] = raw
            with self.assertRaises(ValidationError, msg=raw):
                SourceDocumentAttestationV1.model_validate(payload, strict=True)

    def test_missing_artifact_and_wrong_paper_event_fail_closed(self) -> None:
        _, event = self._write_attestation(self._attestation())
        other = event.model_copy(update={"paper_id": "different-paper"})
        result = verify_source_document_attestation(
            kb_root=self.root, trusted_event=other
        )
        self.assertFalse(result.verified)
        self.assertIn("attestation_missing", result.reasons)

    def test_model_copy_cannot_bypass_trusted_event_validation(self) -> None:
        _, event = self._write_attestation(self._attestation())
        invalid = event.model_copy(update={
            "kb_relative_path": "../outside.pdf"
        })
        result = verify_source_document_attestation(
            kb_root=self.root, trusted_event=invalid
        )
        self.assertFalse(result.verified)
        self.assertIn("trusted_event_invalid", result.reasons)


if __name__ == "__main__":
    unittest.main()
