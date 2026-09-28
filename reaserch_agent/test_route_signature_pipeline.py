"""End-to-end trust boundary for a signed review of one PDF route signature."""

from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chem_agent_contracts.route_candidate import RouteGoalV1, RouteSignatureV1, RouteTargetV1
from chem_agent_contracts.v2 import ScientificCompletenessV2, canonical_digest
from reaserch_agent.route_attestation import (
    SourceDocumentAttestationV1,
    TrustedAcquisitionEventV1,
    attestation_path_for,
)
from reaserch_agent.route_pdf_groups import (
    enumerate_attested_pdf_experimental_groups,
    enumerate_pdf_experimental_groups,
)
from reaserch_agent.route_pipeline import evaluate_route_decision_v1
from reaserch_agent.route_signature_review import (
    REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1,
    SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
    TrustedIssuerPublicKeyV1,
    signed_reviewed_route_signature_message_v1,
)
from reaserch_agent.route_signed_event import (
    SIGNED_EVENT_SCHEMA_VERSION_V1,
    signed_trusted_acquisition_event_message_v1,
)
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.workflow import ResearchAgent

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    serialization = None
    Ed25519PrivateKey = None


@unittest.skipUnless(
    importlib.util.find_spec("fitz") and Ed25519PrivateKey is not None,
    "PyMuPDF and cryptography required",
)
class SignedRouteSignaturePipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pdf = self.root / "_pdf_sources" / "campaign" / "paper.pdf"
        self.pdf.parent.mkdir(parents=True)
        document = fitz.open()
        page = document.new_page()
        for index, (line, size, font) in enumerate((
            ("Methods", 16, "hebo"),
            ("control", 14, "hebo"),
            ("Mix 2 mmol salt in water.", 10, "helv"),
            ("Recover product wet solid.", 10, "helv"),
            ("characterization", 14, "hebo"),
            ("Measure the product signal.", 10, "helv"),
        )):
            page.insert_text(
                fitz.Point(72, 70 + 48 * index), line,
                fontsize=size, fontname=font,
            )
        document.save(str(self.pdf))
        document.close()

        self.source_doi = "10.1000/example"
        self.source_digest = "sha256_" + sha256(self.pdf.read_bytes()).hexdigest()
        record, _ = PaperRegistry(self.root).upsert(
            title="Primary study", doi=self.source_doi,
            verification_status="local_file", full_text_status="parsed",
            pdf_file=str(self.pdf),
        )
        self.paper_id = record["paper_id"]
        groups = enumerate_pdf_experimental_groups(
            {self.paper_id: self.pdf}, source_root=self.root,
        )
        self.assertEqual(groups.diagnostics, [])
        self.assertEqual(len(groups.groups), 2)
        self.scopes = {
            group.source_scope.experimental_group_id: group.source_scope
            for group in groups.groups
        }
        self.assertEqual(set(self.scopes), {"control", "characterization"})

        self.target = RouteTargetV1(
            material="product", desired_state="retained_wet_solid",
            objective="prepare product",
        )
        self.required_field = "material_graph[0].material_inputs[0].quantity.value"
        self.goal = RouteGoalV1(
            goal_id="goal-1", target=self.target, constraint="open",
            required_fields=[self.required_field],
        )
        self.signature = RouteSignatureV1(
            route_family="precipitation",
            target_transformation="solution_to_wet_solid",
            precursor_roles=["metal_salt"], reagent_roles=["base"],
            operations=["dissolve", "precipitate"],
            control_modes=["pH_feedback"], endpoint_state="retained_wet_solid",
        )
        self.protocols = [self._route_protocol(), self._sibling_protocol()]
        self.roles = {
            (self.paper_id, scope.experimental_group_id, self.source_digest): (
                "synthesis" if scope.experimental_group_id == "control"
                else "characterization"
            )
            for scope in self.scopes.values()
        }
        self.capabilities = {
            (self.paper_id, "control", self.source_digest): ["capability-1"],
        }

        relative_path = "_pdf_sources/campaign/paper.pdf"
        attestation = SourceDocumentAttestationV1(
            schema_version="source_document_attestation_v1",
            paper_id=self.paper_id, kb_relative_path=relative_path,
            document_digest=self.source_digest,
            document_kind="primary_paper", doi=self.source_doi, parent_doi="",
            acquisition_url="https://publisher.example/article/example",
            request_url="https://publisher.example/paper.pdf",
            final_url="https://publisher.example/paper.pdf",
            identity_evidence_type="publisher_doi_link",
            identity_evidence_url="https://publisher.example/article/example",
            identity_evidence_digest="sha256_" + "a" * 64,
            identity_status="verified", issuer="acquisition-reviewer",
            issued_at="2026-09-27T12:00:00Z",
        )
        attestation_json = attestation.model_dump(mode="json")
        artifact = attestation_path_for(self.root, self.paper_id, self.source_digest)
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps(attestation_json), encoding="utf-8")
        self.attestation_digest = canonical_digest(attestation_json)
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id=self.paper_id, kb_relative_path=relative_path,
            document_digest=self.source_digest, document_kind="primary_paper",
            attestation_digest=self.attestation_digest,
            issuer="acquisition-reviewer", identity_verdict="primary_verified",
        ).model_dump(mode="json")
        self.acquisition_key = Ed25519PrivateKey.generate()
        self.signed_event = self._sign_event(event, self.acquisition_key)
        self.acquisition_keys = {
            "acquisition-key": TrustedIssuerPublicKeyV1(
                self.acquisition_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ),
                "acquisition-reviewer",
            ),
        }
        self.review_key = Ed25519PrivateKey.generate()
        self.review_key_id = "route-review-key"
        self.review_keys = {
            self.review_key_id: TrustedIssuerPublicKeyV1(
                self.review_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ),
                "route-reviewer",
            ),
        }
        self.manifest = {
            "schema_version": REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1,
            "paper_id": self.paper_id,
            "experimental_group_id": "control",
            "source_digest": self.source_digest,
            "source_attestation_digest": self.attestation_digest,
            "group_locator": self.scopes["control"].locator,
            "section": self.scopes["control"].section,
            "document_kind": "primary_paper",
            "source_doi": self.source_doi,
            "target_material": self.target.material,
            "target_state": self.target.desired_state,
            "target_objective": self.target.objective,
            "issuer": "route-reviewer",
            "review_basis": "Reviewed original Methods control group",
            "route_signature": self.signature.model_dump(mode="json"),
        }
        self.review = self._sign_review(self.manifest)

    @staticmethod
    def _sign_event(event: dict, private_key: Ed25519PrivateKey) -> dict:
        key_id = "acquisition-key"
        return {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": key_id,
            "event": event,
            "signature": base64.b64encode(private_key.sign(
                signed_trusted_acquisition_event_message_v1(
                    key_id=key_id, event=event,
                )
            )).decode("ascii"),
        }

    def _sign_review(self, manifest: dict) -> dict:
        return {
            "schema_version": SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
            "key_id": self.review_key_id,
            "manifest": manifest,
            "signature": base64.b64encode(self.review_key.sign(
                signed_reviewed_route_signature_message_v1(
                    key_id=self.review_key_id, manifest=manifest,
                )
            )).decode("ascii"),
        }

    def _route_protocol(self) -> dict:
        scope = self.scopes["control"]
        excerpt = "Mix 2 mmol salt in water."
        provenance = {
            "kind": "paper", "reference": "E1",
            "evidence_class": "paper_explicit",
            "source_path": "evidence_bundle.items[0].excerpt",
            "excerpt": excerpt, "source_digest": canonical_digest(excerpt),
        }
        output_excerpt = "Recover product wet solid."
        output_provenance = {
            **provenance, "reference": "E2",
            "source_path": "evidence_bundle.items[1].excerpt",
            "excerpt": output_excerpt,
            "source_digest": canonical_digest(output_excerpt),
        }
        return {
            "paper_id": self.paper_id,
            "experimental_group_id": "control",
            "group_role": "synthesis",
            "source": {
                "source_document": str(self.pdf), "section": scope.section,
                "locator": scope.locator, "source_digest": self.source_digest,
            },
            "target": self.target.model_dump(mode="json"),
            "route_signature": self.signature.model_dump(mode="json"),
            "evidence_bundle": [{
                "evidence_id": "E1", "excerpt": excerpt, "doi": self.source_doi,
            }, {
                "evidence_id": "E2", "excerpt": output_excerpt,
                "doi": self.source_doi,
            }],
            "evidence_matrix": [{
                "field_path": self.required_field,
                "value": 2, "unit": "mmol", "status": "supported",
                "evidence_id": "E1",
                "source_scope": {
                    "paper_id": self.paper_id,
                    "experimental_group_id": "control",
                    "section": scope.section,
                    "locator": "pdf:p1:b3-p1:b3",
                    "source_digest": self.source_digest,
                },
                "provenance": provenance,
            }, {
                "field_path": "material_graph[0].material_outputs[0].name",
                "value": "product", "status": "supported",
                "evidence_id": "E2",
                "source_scope": {
                    "paper_id": self.paper_id,
                    "experimental_group_id": "control",
                    "section": scope.section,
                    "locator": "pdf:p1:b4-p1:b4",
                    "source_digest": self.source_digest,
                },
                "provenance": output_provenance,
            }],
            "material_graph": [{
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "precipitate", "sample_id": "sample-1",
                "provenance": provenance,
                "material_inputs": [{
                    "material_id": "salt", "material_instance_id": "salt-1",
                    "name": "salt", "state": "solution",
                    "material_origin": "external_inventory",
                    "quantity": {"mode": "exact", "value": 2, "unit": "mmol"},
                    "provenance": provenance,
                }],
                "material_outputs": [{
                    "material_id": "product", "material_instance_id": "product-1",
                    "name": "product", "state": "retained_wet_solid",
                    "provenance": output_provenance,
                }],
            }],
            "required_capabilities": ["capability-1"],
        }

    def _sibling_protocol(self) -> dict:
        scope = self.scopes["characterization"]
        return {
            "paper_id": self.paper_id,
            "experimental_group_id": "characterization",
            "group_role": "characterization",
            "source": {
                "source_document": str(self.pdf), "section": scope.section,
                "locator": scope.locator, "source_digest": self.source_digest,
            },
        }

    def _evaluate(
        self, *, protocols: list[dict] | None = None,
        review: dict | None = None, reviews: list[dict] | None = None,
        review_keys: dict | None = None, signed_events: list[dict] | None = None,
    ):
        science = {
            "scientific_completeness": ScientificCompletenessV2(),
            "scientific_gate_issues": [],
            "audited_field_paths": [
                self.required_field,
                "material_graph[0].material_outputs[0].name",
            ],
            "verified_graph_step_ids": ["S1"],
            "verified_runtime_resolution_fields": [],
            "verified_convention_field_paths": [],
        }
        device = {
            "status": "preflight_supported",
            "missing_capabilities": [], "unresolved_capabilities": [],
            "checked_capabilities": ["capability-1"],
            "snapshot_id": "snapshot-1", "reasons": [],
        }
        with patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science",
            return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities",
            return_value=device,
        ):
            return evaluate_route_decision_v1(
                self.goal,
                self.protocols if protocols is None else protocols,
                source_root=self.root,
                signed_source_events=(
                    [self.signed_event] if signed_events is None else signed_events
                ),
                trusted_public_keys=self.acquisition_keys,
                signed_route_signature_reviews=(
                    reviews if reviews is not None
                    else [self.review if review is None else review]
                ),
                trusted_route_signature_public_keys=(
                    self.review_keys if review_keys is None else review_keys
                ),
                verified_capabilities_by_group=self.capabilities,
                verified_group_roles_by_group=self.roles,
            )

    def _assert_review_not_admitted(self, result) -> None:
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIsNone(result.decision.selected_route_id)
        self.assertEqual(len(result.decision.candidates), 1)
        receipt = result.decision.candidates[0].validation
        self.assertIsNotNone(receipt)
        self.assertIsNone(receipt.source_route_signature)
        self.assertEqual(receipt.source_route_signature_review_digest, "")

    def test_valid_review_selects_route_and_binds_review_digest(self) -> None:
        result = self._evaluate()
        self.assertEqual(result.discovery.diagnostics[0].status, "excluded")
        self.assertEqual(result.decision.status, "selected_for_planning")
        receipt = result.decision.candidates[0].validation
        self.assertTrue(receipt.source_scope_verified)
        self.assertEqual(receipt.source_route_signature, self.signature)
        self.assertEqual(receipt.source_attestation_digest, self.attestation_digest)
        self.assertEqual(receipt.source_document_kind, "primary_paper")
        self.assertEqual(receipt.source_identity_doi, self.source_doi)
        canonical_envelope = json.dumps(
            self.review, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            receipt.source_route_signature_review_digest,
            "sha256_" + sha256(canonical_envelope).hexdigest(),
        )
        revised = self._sign_review({
            **self.manifest,
            "review_basis": "Reviewed original Methods control group again",
        })
        revised_result = self._evaluate(review=revised)
        self.assertEqual(revised_result.decision.status, "selected_for_planning")
        self.assertNotEqual(
            revised_result.decision.candidates[0].validation.source_route_signature_review_digest,
            receipt.source_route_signature_review_digest,
        )
        self.assertNotEqual(
            revised_result.decision.evidence_snapshot_hash,
            result.decision.evidence_snapshot_hash,
        )
        duplicate = self._evaluate(reviews=[self.review, deepcopy(self.review)])
        self.assertEqual(duplicate.decision.status, "selected_for_planning")
        self.assertEqual(
            duplicate.decision.candidates[0].validation.source_route_signature_review_digest,
            receipt.source_route_signature_review_digest,
        )
        conflicting = self._evaluate(reviews=[self.review, revised])
        self._assert_review_not_admitted(conflicting)

    def test_bad_signature_or_untrusted_review_key_cannot_authorize_route(self) -> None:
        signature = bytearray(base64.b64decode(self.review["signature"]))
        signature[0] ^= 1
        corrupted = {
            **self.review,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
        self._assert_review_not_admitted(self._evaluate(review=corrupted))
        self._assert_review_not_admitted(self._evaluate(review_keys={}))
        other_key = Ed25519PrivateKey.generate()
        wrong_keys = {
            self.review_key_id: TrustedIssuerPublicKeyV1(
                other_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ),
                "route-reviewer",
            ),
        }
        self._assert_review_not_admitted(self._evaluate(review_keys=wrong_keys))

    def test_every_review_scope_field_must_match_live_source_and_target(self) -> None:
        changes = {
            "paper_id": "another-paper",
            "experimental_group_id": "characterization",
            "source_digest": "sha256_" + "0" * 64,
            "source_attestation_digest": "sha256_" + "1" * 64,
            "group_locator": self.scopes["characterization"].locator,
            "section": "Experimental",
            "document_kind": "supporting_information",
            "source_doi": "10.1000/other",
            "target_material": "other product",
            "target_state": "dry_solid",
            "target_objective": "prepare another product",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = {**self.manifest, field: value}
                result = self._evaluate(review=self._sign_review(changed))
                self._assert_review_not_admitted(result)

    def test_stale_pdf_bytes_cannot_reuse_source_or_review_signature(self) -> None:
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\nchanged")
        result = self._evaluate()
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIsNone(result.decision.selected_route_id)
        self.assertEqual(result.discovery.candidates, [])
        self.assertIn("attested_source_unavailable", result.decision.decision_reasons)

    def test_second_signed_but_unattested_source_blocks_valid_route(self) -> None:
        # A valid event signature does not mean its PDF and attestation are
        # present in the registry. Source B might contain an unseen route.
        other_event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id="doi_10_1000_other",
            kb_relative_path="_pdf_sources/campaign/missing.pdf",
            document_digest="sha256_" + "b" * 64,
            document_kind="primary_paper",
            attestation_digest="sha256_" + "c" * 64,
            issuer="acquisition-reviewer",
            identity_verdict="primary_verified",
        ).model_dump(mode="json")
        signed_other = self._sign_event(other_event, self.acquisition_key)
        result = self._evaluate(signed_events=[self.signed_event, signed_other])
        self._assert_review_not_admitted(result)
        self.assertTrue(result.decision.candidates[0].validation.source_scope_verified)
        self.assertIn(
            "trusted_source_event_not_attested",
            result.decision.decision_reasons,
        )

    def test_source_disappearing_during_enumeration_blocks_valid_route(self) -> None:
        def enumerate_while_missing(*args, **kwargs):
            hidden = self.pdf.with_suffix(".hidden")
            self.pdf.rename(hidden)
            try:
                return enumerate_attested_pdf_experimental_groups(*args, **kwargs)
            finally:
                hidden.rename(self.pdf)

        with patch(
            "reaserch_agent.route_pipeline.enumerate_attested_pdf_experimental_groups",
            side_effect=enumerate_while_missing,
        ):
            result = self._evaluate()
        self._assert_review_not_admitted(result)
        self.assertTrue(result.decision.candidates[0].validation.source_scope_verified)
        self.assertIn(
            "attested_source_not_enumerated",
            result.decision.decision_reasons,
        )

    def test_missing_sibling_group_keeps_global_decision_unresolved(self) -> None:
        result = self._evaluate(protocols=self.protocols[:1])
        self.assertEqual(len(result.decision.candidates), 1)
        receipt = result.decision.candidates[0].validation
        self.assertTrue(receipt.source_scope_verified)
        self.assertIsNone(receipt.source_route_signature)
        self.assertEqual(receipt.source_route_signature_review_digest, "")
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIsNone(result.decision.selected_route_id)
        self.assertIn("attested_group_not_extracted", result.decision.decision_reasons)
        route_id = result.decision.candidates[0].route_id
        self.assertIn(
            "review:source_group_audit_incomplete",
            result.validation_diagnostics[route_id],
        )

    def test_review_does_not_override_failing_pdf_field_verifier(self) -> None:
        protocols = deepcopy(self.protocols)
        protocols[0]["evidence_matrix"][0]["source_scope"]["locator"] = (
            self.scopes["characterization"].locator
        )
        result = self._evaluate(protocols=protocols)
        self._assert_review_not_admitted(result)
        route_id = result.decision.candidates[0].route_id
        self.assertTrue(any(
            "field_locator_outside_group" in reason
            for reason in result.validation_diagnostics[route_id]
        ))

    def test_research_agent_uses_constructor_review_authority_not_state(self) -> None:
        def agent(*, with_review: bool) -> ResearchAgent:
            return ResearchAgent(
                model=object(), use_llm=False,
                knowledge_base_dir=str(self.root), memory_dir=str(self.root),
                enable_memory=False, enable_online_literature=False,
                enable_web_search=False, contract_version="v2",
                signed_route_source_events=[self.signed_event],
                trusted_route_public_keys=self.acquisition_keys,
                signed_route_signature_reviews=[self.review] if with_review else [],
                trusted_route_signature_public_keys=(
                    self.review_keys if with_review else {}
                ),
                trusted_route_capabilities_by_group=self.capabilities,
                trusted_route_group_roles_by_group=self.roles,
            )

        def state(constraints: dict) -> ResearchAgentState:
            return ResearchAgentState(
                event=ResearchEvent(
                    event_type="bootstrap", query="prepare product",
                    constraints=constraints,
                ),
                contract_version="v2", extracted_protocols=self.protocols,
            )

        science = {
            "scientific_completeness": ScientificCompletenessV2(),
            "scientific_gate_issues": [],
            "audited_field_paths": [
                self.required_field,
                "material_graph[0].material_outputs[0].name",
            ],
            "verified_graph_step_ids": ["S1"],
            "verified_runtime_resolution_fields": [],
            "verified_convention_field_paths": [],
        }
        device = {
            "status": "preflight_supported",
            "missing_capabilities": [], "unresolved_capabilities": [],
            "checked_capabilities": ["capability-1"],
            "snapshot_id": "snapshot-1", "reasons": [],
        }
        configured = agent(with_review=True)
        unconfigured = agent(with_review=False)
        # Proposal extraction is a separate LLM stage. Supply the same
        # protocols to exercise the constructor-to-pipeline trust wiring.
        with patch.object(
            configured, "_propose_attested_route_protocols",
            return_value=self.protocols,
        ), patch.object(
            unconfigured, "_propose_attested_route_protocols",
            return_value=self.protocols,
        ), patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science",
            return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities",
            return_value=device,
        ):
            configured_result = configured.evaluate_route_decision_v1(
                state({
                    "signed_route_signature_reviews": [{"untrusted": "tampered"}],
                    "trusted_route_signature_public_keys": {},
                }),
                self.goal,
            )
            unconfigured_result = unconfigured.evaluate_route_decision_v1(
                state({
                    "signed_route_signature_reviews": [self.review],
                    "trusted_route_signature_public_keys": self.review_keys,
                }),
                self.goal,
            )
        self.assertEqual(configured_result.decision.status, "selected_for_planning")
        self.assertEqual(unconfigured_result.decision.status, "unresolved")
        self.assertIsNone(
            unconfigured_result.decision.candidates[0].validation.source_route_signature
        )


if __name__ == "__main__":
    unittest.main()
