"""Source receipt producer tests use PDF bytes, not mocked attestations."""

from __future__ import annotations

import base64
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_candidate import RouteCandidateV1
from chem_agent_contracts.v2 import canonical_digest
from reaserch_agent.route_attestation import attested_route_sources
from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups
from reaserch_agent.route_receipt_producer import (
    FetchedDocumentV1,
    _trust_config,
    acquire_source_identity,
    audit_literal_pdf_fields,
    crosscheck_local_document,
    sign_source_identity,
)
from reaserch_agent.route_signed_event import verify_signed_trusted_acquisition_event

try:
    import fitz
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    fitz = None
    Ed25519PrivateKey = None


@unittest.skipUnless(fitz is not None and Ed25519PrivateKey is not None,
                     "PyMuPDF and cryptography required")
class RouteReceiptProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "kb"
        self.root.mkdir()
        self.doi = "10.1000/example.42"
        self.title = "Wet Solid Route From Nitrate Precursors"
        self.pdf_url = "https://publisher.example/paper.pdf"
        self.landing_url = "https://publisher.example/doi/example.42"
        document = fitz.open()
        page = document.new_page()
        for index, (text, size, font) in enumerate((
            (self.title, 18, "hebo"),
            ("Huang et al. DOI " + self.doi, 10, "helv"),
            ("Methods", 16, "hebo"),
            ("Control route", 14, "hebo"),
            ("Mix 2 mmol salt in water.", 10, "helv"),
            ("Recover retained wet solid.", 10, "helv"),
        )):
            page.insert_text((72, 40 + 46 * index), text, fontsize=size,
                             fontname=font)
        self.raw_pdf = document.tobytes()
        document.close()
        self.landing_html = (
            f'<html><title>{self.title}</title><p>{self.doi}</p>'
            f'<a href="{self.pdf_url}">PDF</a></html>'
        ).encode()

    def _fetch(self, url: str, maximum_bytes: int) -> FetchedDocumentV1:
        responses = {
            f"https://doi.org/{self.doi}":
                FetchedDocumentV1(self.landing_url, self.landing_html),
            self.landing_url:
                FetchedDocumentV1(self.landing_url, self.landing_html),
            self.pdf_url:
                FetchedDocumentV1(self.pdf_url, self.raw_pdf),
        }
        response = responses[url]
        self.assertLessEqual(len(response.body), maximum_bytes)
        return response

    def _acquire(self):
        return acquire_source_identity(
            kb_root=self.root, doi=self.doi, title=self.title,
            campaign_id="campaign", document_kind="primary_paper",
            evidence_url=self.landing_url, pdf_url=self.pdf_url,
            issuer="automated-source-verifier", fetch=self._fetch,
        )

    def _sign(self, receipt):
        private_key = Ed25519PrivateKey.generate()
        envelope, public = sign_source_identity(
            receipt, kb_root=self.root, private_key=private_key,
            key_id="source-verifier-1", fetch=self._fetch,
        )
        return envelope, public

    def test_publisher_pdf_produces_signed_source_only_and_literal_review_queue(self) -> None:
        receipt = self._acquire()
        self.assertEqual(receipt, self._acquire())
        self.assertEqual(receipt.status, "identity_verified_unsigned")
        self.assertEqual(receipt.method, "doi_resolver_publisher_link_exact_pdf_bytes_v1")
        self.assertEqual(receipt.verification_mode, "automated")
        self.assertIn("independent_route_semantics_review_required", receipt.review_queue)
        envelope, public = self._sign(receipt)
        verified = verify_signed_trusted_acquisition_event(
            envelope=envelope,
            trusted_public_keys={"source-verifier-1": public},
        )
        self.assertTrue(verified.verified)
        sources = attested_route_sources(
            self.root, [envelope],
            trusted_public_keys={"source-verifier-1": public},
        )
        self.assertEqual(len(sources[receipt.paper_id]), 1)
        self.assertEqual(sources[receipt.paper_id][0].document_digest,
                         receipt.document_digest)
        config = _trust_config(
            self.root.parent / "route-trust.json", envelope=envelope,
            key_id="source-verifier-1", public=public,
        )
        self.assertEqual(config["signed_route_signature_reviews"], [])
        self.assertEqual(config["trusted_route_group_roles_by_group"], [])

        path = self.root / receipt.kb_relative_path
        groups = enumerate_pdf_experimental_groups(
            {receipt.paper_id: path}, source_root=self.root,
        )
        self.assertEqual(groups.diagnostics, [])
        self.assertEqual(len(groups.groups), 1)
        group = groups.groups[0]
        excerpt = "Mix 2 mmol salt in water."
        field_scope = {
            **group.source_scope.model_dump(mode="json"),
            "locator": next(block.locator for block in group.blocks
                            if excerpt in block.text),
        }
        candidate = RouteCandidateV1.model_validate({
            "route_id": "route-1",
            "target": {
                "material": "product", "desired_state": "retained_wet_solid",
                "objective": "prepare product",
            },
            "source_scope": group.source_scope.model_dump(mode="json"),
            "route_signature": {
                "route_family": "precipitation",
                "target_transformation": "solution_to_wet_solid",
                "operations": ["mix"], "endpoint_state": "retained_wet_solid",
            },
            "evidence_bundle": [{
                "evidence_id": "E1", "excerpt": excerpt, "doi": self.doi,
            }],
            "evidence_matrix": [{
                "field_path": "material_graph[0].material_inputs[0].quantity.value",
                "value": 2, "unit": "mmol", "status": "supported",
                "evidence_id": "E1", "source_scope": field_scope,
                "provenance": {
                    "kind": "paper", "reference": "E1",
                    "evidence_class": "paper_explicit",
                    "source_path": "evidence_bundle.items[0].excerpt",
                    "source_digest": canonical_digest(excerpt),
                    "excerpt": excerpt,
                },
            }],
            "material_graph": [{
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "mix", "sample_id": "sample-1",
                "provenance": {
                    "kind": "paper", "reference": "E1",
                    "evidence_class": "paper_explicit",
                    "source_path": "evidence_bundle.items[0].excerpt",
                    "source_digest": canonical_digest(excerpt),
                    "excerpt": excerpt,
                },
                "material_inputs": [{
                    "material_id": "salt", "material_instance_id": "salt-1",
                    "name": "salt", "state": "solution",
                    "material_origin": "external_inventory",
                    "quantity": {"mode": "exact", "value": 2, "unit": "mmol"},
                    "provenance": {
                        "kind": "paper", "reference": "E1",
                        "evidence_class": "paper_explicit",
                        "source_path": "evidence_bundle.items[0].excerpt",
                        "source_digest": canonical_digest(excerpt),
                        "excerpt": excerpt,
                    },
                }],
            }],
            "origin": "paper_experimental_group",
        })
        field_receipt = audit_literal_pdf_fields(
            candidate, kb_root=self.root, signed_event=envelope,
            key_id="source-verifier-1", public_key=public,
        )
        self.assertEqual(field_receipt.status, "literal_fields_verified")
        self.assertEqual(len(field_receipt.verified_field_paths), 1)
        self.assertEqual(field_receipt.verified_evidence_ids, ("E1",))
        request = field_receipt.independent_review_request
        self.assertEqual(request["status"], "pending_independent_chemical_review")
        self.assertEqual(request["expected_scope"]["source_attestation_digest"],
                         receipt.attestation_digest)
        self.assertNotIn("signature", asdict(field_receipt))
        self.assertNotIn("signature", request)

    def test_repository_crossref_metadata_mode_and_tamper_rejected(self) -> None:
        repository_pdf_url = "https://repository.example/study.pdf"
        crossref_url = "https://api.crossref.org/works/10.1000%2Fexample.42"
        metadata = json.dumps({"message": {
            "DOI": self.doi, "title": [self.title],
            "author": [{"family": "Huang"}],
        }}).encode()

        def fetch(url: str, maximum_bytes: int) -> FetchedDocumentV1:
            responses = {
                crossref_url: FetchedDocumentV1(crossref_url, metadata),
                repository_pdf_url: FetchedDocumentV1(repository_pdf_url, self.raw_pdf),
            }
            response = responses[url]
            self.assertLessEqual(len(response.body), maximum_bytes)
            return response

        receipt = acquire_source_identity(
            kb_root=self.root, doi=self.doi, title="", campaign_id="repo",
            document_kind="primary_paper", evidence_url=crossref_url,
            pdf_url=repository_pdf_url, issuer="automated-source-verifier",
            trusted_source_hosts=["repository.example"], fetch=fetch,
        )
        self.assertEqual(receipt.status, "identity_verified_unsigned")
        self.assertEqual(receipt.method,
                         "configured_source_host_crossref_pdf_match_v1")
        self.assertEqual(receipt, acquire_source_identity(
            kb_root=self.root, doi=self.doi, title="", campaign_id="repo",
            document_kind="primary_paper", evidence_url=crossref_url,
            pdf_url=repository_pdf_url, issuer="automated-source-verifier",
            trusted_source_hosts=["repository.example"], fetch=fetch,
        ))
        envelope, public = sign_source_identity(
            receipt, kb_root=self.root,
            private_key=Ed25519PrivateKey.generate(), key_id="repository-key",
            trusted_source_hosts=["repository.example"], fetch=fetch,
        )
        self.assertTrue(verify_signed_trusted_acquisition_event(
            envelope=envelope, trusted_public_keys={"repository-key": public},
        ).verified)

        bad_metadata = json.dumps({"message": {
            "DOI": self.doi, "title": ["Different chemistry paper"],
            "author": [{"family": "Other"}],
        }}).encode()

        def tampered(url: str, maximum_bytes: int) -> FetchedDocumentV1:
            return (FetchedDocumentV1(crossref_url, bad_metadata)
                    if url == crossref_url else fetch(url, maximum_bytes))

        with self.assertRaisesRegex(ValueError, "pdf_title_doi_metadata_mismatch"):
            sign_source_identity(
                receipt, kb_root=self.root,
                private_key=Ed25519PrivateKey.generate(), key_id="repository-key",
                trusted_source_hosts=["repository.example"], fetch=tampered,
            )

    def test_repository_pdf_citing_target_is_not_the_target_paper(self) -> None:
        repository_pdf_url = "https://repository.example/citing-study.pdf"
        crossref_url = "https://api.crossref.org/works/10.1000%2Fexample.42"
        metadata = json.dumps({"message": {
            "DOI": self.doi, "title": [self.title],
            "author": [{"family": "Huang"}],
        }}).encode()
        citing = fitz.open()
        page = citing.new_page()
        page.insert_text((72, 40), "An Unrelated Study", fontsize=18, fontname="hebo")
        page.insert_text((72, 85), "Other Author", fontsize=11)
        page.insert_text((72, 180), "References", fontsize=14, fontname="hebo")
        page.insert_text((72, 220), self.title, fontsize=10)
        page.insert_text((72, 245), "Huang et al. DOI " + self.doi, fontsize=10)
        citing_pdf = citing.tobytes()
        citing.close()

        def fetch(url: str, maximum_bytes: int) -> FetchedDocumentV1:
            responses = {
                crossref_url: FetchedDocumentV1(crossref_url, metadata),
                repository_pdf_url: FetchedDocumentV1(repository_pdf_url, citing_pdf),
            }
            response = responses[url]
            self.assertLessEqual(len(response.body), maximum_bytes)
            return response

        receipt = acquire_source_identity(
            kb_root=self.root, doi=self.doi, title="", campaign_id="citation",
            document_kind="primary_paper", evidence_url=crossref_url,
            pdf_url=repository_pdf_url, issuer="automated-source-verifier",
            trusted_source_hosts=["repository.example"], fetch=fetch,
        )
        self.assertEqual(receipt.status, "identity_review_pending")
        self.assertEqual(receipt.reason_codes,
                         ("pdf_title_doi_metadata_mismatch",))
        self.assertFalse((self.root / "registry" / "papers.jsonl").exists())

    def test_unlinked_or_untrusted_pdf_yields_pending_without_attestation(self) -> None:
        def unlinked(url: str, maximum_bytes: int) -> FetchedDocumentV1:
            if url in {f"https://doi.org/{self.doi}", self.landing_url}:
                return FetchedDocumentV1(
                    self.landing_url,
                    f"<html>{self.doi}<a href='/other.pdf'>PDF</a></html>".encode(),
                )
            return FetchedDocumentV1(self.pdf_url, self.raw_pdf)

        receipt = acquire_source_identity(
            kb_root=self.root, doi=self.doi, title=self.title,
            campaign_id="campaign", document_kind="primary_paper",
            evidence_url=self.landing_url, pdf_url=self.pdf_url,
            issuer="automated-source-verifier", fetch=unlinked,
        )
        self.assertEqual(receipt.status, "identity_review_pending")
        self.assertEqual(receipt.reason_codes, ("pdf_not_linked_from_identity_page",))
        self.assertFalse((self.root / "registry" / "papers.jsonl").exists())
        self.assertIn("source_identity_independent_review_required",
                      receipt.review_queue)

    def test_local_pdf_metadata_match_stays_pending_origin_review(self) -> None:
        local = self.root.parent / "historical-copy.pdf"
        local.write_bytes(self.raw_pdf)
        crossref_url = "https://api.crossref.org/works/10.1000%2Fexample.42"
        metadata = json.dumps({"message": {
            "DOI": self.doi, "title": [self.title],
            "author": [{"family": "Huang"}],
        }}).encode()

        def fetch(url: str, maximum_bytes: int) -> FetchedDocumentV1:
            self.assertEqual(url, crossref_url)
            return FetchedDocumentV1(crossref_url, metadata)

        receipt = crosscheck_local_document(
            doi=self.doi, local_pdf=local,
            historical_acquisition_url="https://repository.example/old.pdf",
            fetch=fetch,
        )
        self.assertTrue(receipt.metadata_match)
        self.assertEqual(receipt.status, "identity_review_pending")
        self.assertEqual(receipt.reason_codes,
                         ("remote_acquisition_proof_missing",))
        self.assertEqual(receipt.document_digest,
                         "sha256_" + sha256(self.raw_pdf).hexdigest())
        self.assertFalse(receipt.historical_url_fetched_this_run)
        self.assertFalse((self.root / "registry" / "papers.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
