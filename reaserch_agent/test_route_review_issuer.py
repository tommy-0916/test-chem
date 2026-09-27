"""Trust-boundary tests for the deployment-side group review issuer."""

from __future__ import annotations

import base64
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from chem_agent_contracts.route_candidate import RouteGoalV1, RouteTargetV1
from chem_agent_contracts.v2 import canonical_digest
from reaserch_agent.route_attestation import (
    SourceDocumentAttestationV1, TrustedAcquisitionEventV1,
    attestation_path_for,
)
from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups
from reaserch_agent.route_review_issuer import (
    issue_group_review, main, prepare_group_review_work_order,
)
from reaserch_agent.route_signed_event import (
    SIGNED_EVENT_SCHEMA_VERSION_V1,
    signed_trusted_acquisition_event_message_v1,
)
from reaserch_agent.run_research_agent import load_route_trust_config
from reaserch_agent.tools.paper_registry import PaperRegistry

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
class IndependentGroupReviewIssuerTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.kb = self.root / "kb"
        self.kb.mkdir()
        self.pdf = self.kb / "_pdf_sources" / "campaign" / "paper.pdf"
        self.pdf.parent.mkdir(parents=True)
        document = fitz.open()
        page = document.new_page()
        for index, (line, size, font) in enumerate((
            ("Methods", 16, "hebo"),
            ("Control synthesis", 14, "hebo"),
            ("Mix 2 mmol salt in water.", 10, "helv"),
            ("Recover a wet product.", 10, "helv"),
            ("Characterization", 14, "hebo"),
            ("Measure the product signal.", 10, "helv"),
        )):
            page.insert_text(
                fitz.Point(72, 70 + 48 * index), line,
                fontsize=size, fontname=font,
            )
        document.save(str(self.pdf))
        document.close()
        self.source_digest = "sha256_" + sha256(self.pdf.read_bytes()).hexdigest()
        self.doi = "10.1000/issuer-test"
        record, _ = PaperRegistry(self.kb).upsert(
            title="Issuer test paper", doi=self.doi,
            verification_status="local_file", full_text_status="parsed",
            pdf_file=str(self.pdf),
        )
        self.paper_id = record["paper_id"]
        groups = enumerate_pdf_experimental_groups(
            {self.paper_id: self.pdf}, source_root=self.kb,
        )
        self.assertFalse(groups.diagnostics)
        self.assertEqual(len(groups.groups), 2)
        self.group_ids = [item.source_scope.experimental_group_id for item in groups.groups]

        relative = "_pdf_sources/campaign/paper.pdf"
        attestation = SourceDocumentAttestationV1(
            schema_version="source_document_attestation_v1",
            paper_id=self.paper_id, kb_relative_path=relative,
            document_digest=self.source_digest,
            document_kind="primary_paper", doi=self.doi, parent_doi="",
            acquisition_url="https://publisher.example/article",
            request_url="https://publisher.example/paper.pdf",
            final_url="https://publisher.example/paper.pdf",
            identity_evidence_type="publisher_doi_link",
            identity_evidence_url="https://publisher.example/article",
            identity_evidence_digest="sha256_" + "a" * 64,
            identity_status="verified", issuer="source-issuer",
            issued_at="2026-09-27T12:00:00Z",
        ).model_dump(mode="json")
        artifact = attestation_path_for(self.kb, self.paper_id, self.source_digest)
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps(attestation), encoding="utf-8")
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id=self.paper_id, kb_relative_path=relative,
            document_digest=self.source_digest, document_kind="primary_paper",
            attestation_digest=canonical_digest(attestation),
            issuer="source-issuer", identity_verdict="primary_verified",
        ).model_dump(mode="json")
        source_key = Ed25519PrivateKey.generate()
        self.source_key = source_key
        source_key_bytes = source_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        source_envelope = {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": "source-key",
            "event": event,
            "signature": base64.b64encode(source_key.sign(
                signed_trusted_acquisition_event_message_v1(
                    key_id="source-key", event=event,
                )
            )).decode("ascii"),
        }
        self.config_path = self.root / "route-trust-config.json"
        self.config_path.write_text(json.dumps({
            "schema_version": "route-trust-config/v1",
            "signed_route_source_events": [source_envelope],
            "trusted_route_public_keys": {
                "source-key": {
                    "public_key_base64": base64.b64encode(source_key_bytes).decode("ascii"),
                    "allowed_issuer": "source-issuer",
                },
            },
            "signed_route_signature_reviews": [],
            "trusted_route_signature_public_keys": {},
            "trusted_route_capabilities_by_group": [],
            "trusted_route_group_roles_by_group": [],
        }), encoding="utf-8")
        self.review_key = Ed25519PrivateKey.generate()
        self.review_key_path = self.root / "review-key.pem"
        self.review_key_path.write_bytes(self.review_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        self.goal = RouteGoalV1(
            goal_id="goal-1",
            target=RouteTargetV1(
                material="product", desired_state="retained_wet_solid",
                objective="prepare product",
            ),
            constraint="open",
            required_fields=["material_graph[0].material_inputs[0].quantity.value"],
        )
        self.decision_path = self.root / "human-decision.json"

    def _order(self, group_id: str | None = None) -> dict:
        return prepare_group_review_work_order(
            kb_root=self.kb, trust_config=self.config_path, goal=self.goal,
            paper_id=self.paper_id,
            experimental_group_id=group_id or self.group_ids[0],
            source_digest=self.source_digest,
        )

    def _decision(self, order: dict, *, route: bool = True) -> dict:
        decision = {
            "schema_version": "independent_group_review_decision_v1",
            "work_order_digest": canonical_digest(order),
            "reviewer": "Dr Example",
            "review_method": "independent_human_pdf_review",
            "review_basis": "Read the original PDF group and checked each route claim",
            "group_role": "synthesis" if route else "non_procedural",
            "required_capabilities": ["mixing"] if route else [],
            "route_signature": ({
                "route_family": "precipitation",
                "target_transformation": "solution_to_wet_solid",
                "precursor_roles": ["metal_salt"],
                "reagent_roles": ["base"],
                "operations": ["dissolve", "precipitate"],
                "control_modes": ["pH_feedback"],
                "phase_transitions": [],
                "endpoint_state": "retained_wet_solid",
            } if route else None),
            "chemical_review_completed": True,
            "execution_authorized": False,
        }
        self.decision_path.write_text(json.dumps(decision), encoding="utf-8")
        return decision

    def _issue(self, order: dict, decision: dict, **overrides):
        kwargs = {
            "kb_root": self.kb,
            "trust_config": self.config_path,
            "goal": self.goal,
            "work_order": order,
            "decision": decision,
            "private_key_path": self.review_key_path,
            "decision_path": self.decision_path,
            "issuer": "chemical-review-issuer",
            "key_id": "review-key",
        }
        kwargs.update(overrides)
        return issue_group_review(**kwargs)

    def test_signed_review_scope_and_group_policy_pass_runtime_loader(self) -> None:
        order = self._order()
        self.assertFalse(order["human_chemical_review_completed"])
        self.assertFalse(order["execution_authorized"])
        decision = self._decision(order)
        config, receipt = self._issue(order, decision)
        self.assertFalse(receipt["execution_authorized"])
        self.assertEqual(receipt["group_role"], "synthesis")
        self.assertEqual(len(config["signed_route_signature_reviews"]), 1)
        self.assertEqual(len(config["trusted_route_capabilities_by_group"]), 1)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        loaded = load_route_trust_config(str(self.config_path), str(self.kb))
        self.assertEqual(len(loaded["signed_route_signature_reviews"]), 1)
        self.assertEqual(len(loaded["trusted_route_group_roles_by_group"]), 1)

    def test_nonroute_exclusion_is_explicit_and_has_no_route_signature(self) -> None:
        order = self._order(self.group_ids[1])
        decision = self._decision(order, route=False)
        config, receipt = self._issue(order, decision)
        self.assertIsNone(receipt["route_review_envelope"])
        self.assertEqual(config["signed_route_signature_reviews"], [])
        self.assertEqual(config["trusted_route_capabilities_by_group"], [])
        self.assertEqual(config["trusted_route_group_roles_by_group"][0]["group_role"],
                         "non_procedural")
        self.assertEqual(config["trusted_route_signature_public_keys"], {})
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        loaded = load_route_trust_config(str(self.config_path), str(self.kb))
        self.assertEqual(len(loaded["trusted_route_group_roles_by_group"]), 1)

    def test_stale_or_tampered_scope_is_rejected_before_signing(self) -> None:
        order = self._order()
        decision = self._decision(order)
        order["scope"]["group_locator"] = "pdf:p9:b1-p9:b2"
        with self.assertRaisesRegex(ValueError, "review_work_order_stale_or_untrusted"):
            self._issue(order, decision)

    def test_changed_original_pdf_or_goal_cannot_reuse_work_order(self) -> None:
        order = self._order()
        decision = self._decision(order)
        altered_goal = self.goal.model_copy(update={
            "target": RouteTargetV1(
                material="different product", desired_state="retained_wet_solid",
                objective="prepare product",
            ),
        })
        with self.assertRaisesRegex(ValueError, "review_work_order_stale_or_untrusted"):
            self._issue(order, decision, goal=altered_goal)
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n% changed")
        with self.assertRaisesRegex(ValueError, "signed_pdf_group_not_unique_or_not_enumerated"):
            self._issue(order, decision)

    def test_model_or_unsigned_automated_claim_cannot_issue_human_review(self) -> None:
        order = self._order()
        decision = self._decision(order)
        decision["review_method"] = "planning_agent_self_review"
        self.decision_path.write_text(json.dumps(decision), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported_chemical_review_method"):
            self._issue(order, decision)
        decision["review_method"] = "independent_human_pdf_review"
        decision["execution_authorized"] = True
        self.decision_path.write_text(json.dumps(decision), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review_cannot_authorize_execution"):
            self._issue(order, decision)

    def test_source_issuer_or_key_cannot_issue_chemical_review(self) -> None:
        order = self._order()
        decision = self._decision(order)
        with self.assertRaisesRegex(ValueError, "independent_of_source_issuer"):
            self._issue(order, decision, issuer="source-issuer")
        self.review_key_path.write_bytes(self.source_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        with self.assertRaisesRegex(ValueError, "independent_of_source_issuer"):
            self._issue(order, decision)

    def test_decision_file_and_required_capabilities_cannot_be_skipped(self) -> None:
        order = self._order()
        decision = self._decision(order)
        self.decision_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review_decision_artifact_mismatch"):
            self._issue(order, decision)
        decision["required_capabilities"] = []
        self.decision_path.write_text(json.dumps(decision), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "route_capability_review_incomplete"):
            self._issue(order, decision)

    def test_cli_preserves_config_on_rejected_review(self) -> None:
        order = self._order()
        order_path = self.root / "work-order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        decision = self._decision(order)
        decision["chemical_review_completed"] = False
        self.decision_path.write_text(json.dumps(decision), encoding="utf-8")
        goal_path = self.root / "goal.json"
        goal_path.write_text(self.goal.model_dump_json(), encoding="utf-8")
        original = self.config_path.read_bytes()
        status = main([
            "issue-review", "--kb-root", str(self.kb),
            "--route-trust-config", str(self.config_path),
            "--goal-json", str(goal_path),
            "--work-order", str(order_path),
            "--decision", str(self.decision_path),
            "--private-key", str(self.review_key_path),
            "--issuer", "chemical-review-issuer", "--key-id", "review-key",
            "--receipt-out", str(self.root / "issued-receipt.json"),
        ])
        self.assertEqual(status, 2)
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_cli_writes_verifiable_config_and_receipt(self) -> None:
        order = self._order()
        order_path = self.root / "work-order.json"
        order_path.write_text(json.dumps(order), encoding="utf-8")
        self._decision(order)
        goal_path = self.root / "goal.json"
        goal_path.write_text(self.goal.model_dump_json(), encoding="utf-8")
        receipt_path = self.root / "issued-receipt.json"
        status = main([
            "issue-review", "--kb-root", str(self.kb),
            "--route-trust-config", str(self.config_path),
            "--goal-json", str(goal_path),
            "--work-order", str(order_path),
            "--decision", str(self.decision_path),
            "--private-key", str(self.review_key_path),
            "--issuer", "chemical-review-issuer", "--key-id", "review-key",
            "--receipt-out", str(receipt_path),
        ])
        self.assertEqual(status, 0)
        loaded = load_route_trust_config(str(self.config_path), str(self.kb))
        self.assertEqual(len(loaded["signed_route_signature_reviews"]), 1)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["verification_mode"], "independent_human_pdf_review")
        self.assertFalse(receipt["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
