"""Offline integration of trusted source, science, and device receipts."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1, RouteGoalV1, RouteSignatureV1, RouteTargetV1,
)
from chem_agent_contracts.v2 import ScientificCompletenessV2, canonical_digest
from reaserch_agent.route_attestation import (
    AttestedRouteSourceV1, SourceDocumentAttestationV1,
    TrustedAcquisitionEventV1, attestation_path_for,
)
from reaserch_agent.route_pipeline import (
    _evaluate_route_decision_v1_impl, evaluate_route_decision_v1,
    trusted_route_text_sources,
)
from reaserch_agent.route_pdf_groups import (
    PdfExperimentalGroupV1, PdfGroupEnumerationResultV1,
)
from reaserch_agent.route_signed_event import (
    SIGNED_EVENT_SCHEMA_VERSION_V1, SignedEventVerificationV1,
    TrustedIssuerPublicKeyV1, signed_trusted_acquisition_event_message_v1,
)
from reaserch_agent.tools.paper_registry import PaperRegistry
from reaserch_agent.state import ResearchAgentState, ResearchEvent
from reaserch_agent.workflow import ResearchAgent


class RoutePipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "primary_methods.md"
        self.signature = RouteSignatureV1(
            route_family="precipitation",
            target_transformation="solution_to_wet_solid",
            precursor_roles=["metal_salt"],
            reagent_roles=["base"],
            operations=["dissolve", "precipitate"],
            control_modes=["pH_feedback"],
            endpoint_state="retained_wet_solid",
        )
        self.excerpt = (
            "Mix 2 mmol metal salt with base to pH 10; precipitation "
            "solution_to_wet_solid metal_salt base dissolve precipitate "
            "pH_feedback retained_wet_solid."
        )
        self.source.write_text("\n".join([
            "# Primary study", "## Methods", "### control", self.excerpt,
            "```chem-agent-route-signature-v1",
            json.dumps(self.signature.model_dump(mode="json"), sort_keys=True),
            "```", "Wash the control precipitate with water.",
        ]) + "\n", encoding="utf-8")
        self.digest = "sha256_" + hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.target = RouteTargetV1(
            material="product", desired_state="retained_wet_solid",
            objective="prepare product",
        )
        self.goal = RouteGoalV1(
            goal_id="goal-1", target=self.target, constraint="open",
            required_fields=["material_graph[0].material_inputs[0].quantity.value"],
        )

    def _protocol(self) -> dict:
        scope = {
            "paper_id": "paper-1", "experimental_group_id": "control",
            "section": "Methods", "source_digest": self.digest,
        }
        field_path = self.goal.required_fields[0]
        provenance = {
            "kind": "paper", "reference": "E1", "evidence_class": "paper_explicit",
            "source_path": "evidence_bundle.items[0].excerpt", "excerpt": self.excerpt,
            "source_digest": canonical_digest(self.excerpt),
        }
        signature_fields = {
            "route_signature.route_family": "precipitation",
            "route_signature.target_transformation": "solution_to_wet_solid",
            "route_signature.precursor_roles[0]": "metal_salt",
            "route_signature.reagent_roles[0]": "base",
            "route_signature.operations[0]": "dissolve",
            "route_signature.operations[1]": "precipitate",
            "route_signature.control_modes[0]": "pH_feedback",
            "route_signature.endpoint_state": "retained_wet_solid",
        }
        return {
            "paper_id": "paper-1", "experimental_group_id": "control",
            "group_role": "synthesis",
            "source": {
                "source_document": str(self.source), "section": "Methods",
                "locator": "lines:3-8", "source_digest": self.digest,
            },
            "target": self.target.model_dump(mode="json"),
            "route_signature": self.signature.model_dump(mode="json"),
            "evidence_bundle": [{"evidence_id": "E1", "excerpt": self.excerpt}],
            "evidence_matrix": [{
                "field_path": field_path, "value": 2, "unit": "mmol",
                "status": "supported", "evidence_id": "E1",
                "source_scope": {**scope, "locator": "lines:4-4"},
                "provenance": provenance,
            }] + [{
                "field_path": path, "value": value,
                "status": "supported", "evidence_id": "E1",
                "source_scope": {**scope, "locator": "lines:4-4"},
                "provenance": provenance,
            } for path, value in signature_fields.items()],
            "material_graph": [{
                "macro_step_id": "S1", "macro_action_id": "A1", "sequence": 1,
                "operation": "precipitate", "sample_id": "sample-1",
                "provenance": provenance,
                "material_inputs": [{
                    "material_id": "salt", "material_instance_id": "salt-1",
                    "name": "salt", "state": "solution", "material_origin": "external_inventory",
                    "quantity": {"mode": "exact", "value": 2, "unit": "mmol"},
                    "provenance": provenance,
                }],
                "material_outputs": [{
                    "material_id": "product", "material_instance_id": "product-1",
                    "name": "product", "state": "retained_wet_solid",
                    "provenance": provenance,
                }],
            }],
            "required_capabilities": ["capability-1"],
        }

    def test_registry_only_exposes_parsed_local_text(self) -> None:
        registry = PaperRegistry(self.root)
        registry.upsert(
            title="Primary study", verification_status="local_file",
            full_text_status="parsed", corpus_file=str(self.source),
        )
        indexed = trusted_route_text_sources(self.root)
        self.assertEqual(len(indexed), 1)
        self.assertEqual(next(iter(indexed.values())), [self.source])

    def test_path_only_source_is_not_accepted_by_default(self) -> None:
        result = evaluate_route_decision_v1(
            self.goal, [self._protocol()], source_root=self.root,
        )
        self.assertEqual(result.decision.status, "unresolved")
        self.assertEqual(result.discovery.candidates, [])
        self.assertIn(
            "trusted_source_event_missing", result.decision.decision_reasons
        )
        with self.assertRaises(TypeError):
            evaluate_route_decision_v1(
                self.goal, [self._protocol()], source_root=self.root,
                trusted_source_paths={"paper-1": [self.source]},
                require_attested_sources=False,
            )

    @unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
    def test_registered_pdf_fields_verify_but_unsigned_route_abstains(self) -> None:
        import fitz

        pdf = self.root / "_pdf_sources" / "campaign" / "primary_methods.pdf"
        pdf.parent.mkdir(parents=True)
        pdf_excerpt = "Mix 2 mmol metal salt with base to pH 10."
        document = fitz.open()
        page = document.new_page()
        for line_index, (body, size, font) in enumerate([
            ("Methods", 16, "hebo"),
            ("control", 14, "hebo"),
            (pdf_excerpt, 10, "helv"),
            ("Wash the control precipitate with water.", 10, "helv"),
        ]):
            page.insert_text(fitz.Point(72, 70 + line_index * 48), body,
                             fontsize=size, fontname=font)
        document.save(str(pdf))
        document.close()
        digest = "sha256_" + hashlib.sha256(pdf.read_bytes()).hexdigest()
        protocol = self._protocol()
        protocol["evidence_matrix"] = protocol["evidence_matrix"][:1]
        protocol["evidence_bundle"][0]["excerpt"] = pdf_excerpt
        protocol["evidence_matrix"][0]["provenance"]["excerpt"] = (
            protocol["evidence_bundle"][0]["excerpt"]
        )
        protocol["evidence_matrix"][0]["provenance"]["source_digest"] = canonical_digest(
            protocol["evidence_bundle"][0]["excerpt"]
        )
        protocol["material_graph"][0]["provenance"] = protocol["evidence_matrix"][0]["provenance"]
        protocol["material_graph"][0]["material_inputs"][0]["provenance"] = protocol["evidence_matrix"][0]["provenance"]
        protocol["material_graph"][0]["material_outputs"][0]["provenance"] = protocol["evidence_matrix"][0]["provenance"]
        protocol["source"].update({
            "source_document": str(pdf), "source_digest": digest,
            "locator": "pdf:p1:b2-p1:b4",
        })
        protocol["evidence_matrix"][0]["source_scope"].update({
            "source_digest": digest, "locator": "pdf:p1:b3-p1:b3",
        })
        registry = PaperRegistry(self.root)
        registry.upsert(
            title="PDF primary study", verification_status="local_file",
            full_text_status="parsed", pdf_file=str(pdf),
        )
        sources = trusted_route_text_sources(self.root)
        self.assertIn(pdf, next(iter(sources.values())))
        science = {
            "scientific_completeness": ScientificCompletenessV2(),
            "scientific_gate_issues": [],
            "audited_field_paths": self.goal.required_fields,
            "verified_graph_step_ids": ["S1"],
            "verified_runtime_resolution_fields": [],
            "verified_convention_field_paths": [],
        }
        device = {
            "status": "preflight_supported", "missing_capabilities": [],
            "unresolved_capabilities": [], "checked_capabilities": ["capability-1"],
            "snapshot_id": "snapshot-1", "reasons": [],
        }
        with patch("reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            result = _evaluate_route_decision_v1_impl(
                self.goal, [protocol], source_root=self.root,
                trusted_source_paths={"paper-1": [pdf]},
                require_attested_sources=False,
            )
        self.assertTrue(result.decision.candidates, result.discovery.diagnostics)
        receipt = result.decision.candidates[0].validation
        self.assertTrue(receipt.source_scope_verified, result.validation_diagnostics)
        self.assertEqual(receipt.verified_field_paths, self.goal.required_fields)
        self.assertIsNone(receipt.source_route_signature)
        self.assertIn("route_signature_unverified", result.decision.candidates[0].reasons)
        self.assertEqual(result.decision.status, "unresolved")

        receipt = AttestedRouteSourceV1(
            paper_id="paper-1", path=pdf, document_digest=digest,
            document_kind="primary_paper", doi="10.1000/paper",
            attestation_digest="sha256_" + "a" * 64,
        )
        protocol["evidence_bundle"][0]["doi"] = "10.1000/paper"
        with patch("reaserch_agent.route_pipeline.attested_route_sources", return_value={"paper-1": [receipt]}), patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            matched = evaluate_route_decision_v1(
                self.goal, [protocol], source_root=self.root,
            )
        verified = matched.decision.candidates[0].validation
        self.assertTrue(verified.source_scope_verified)
        self.assertEqual(verified.source_identity_doi, "10.1000/paper")
        self.assertEqual(verified.source_document_kind, "primary_paper")
        self.assertEqual(verified.source_attestation_digest, receipt.attestation_digest)
        self.assertEqual(verified.device_preflight.status, "unknown")
        self.assertIn(
            "capability_requirements_unverified",
            verified.device_preflight.reasons,
        )
        with patch("reaserch_agent.route_pipeline.attested_route_sources", return_value={"paper-1": [receipt]}), patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            mapped = evaluate_route_decision_v1(
                self.goal, [protocol], source_root=self.root,
                verified_capabilities_by_group={
                    ("paper-1", "control", digest): ["capability-1"]
                },
            )
        self.assertEqual(
            mapped.decision.candidates[0].validation.device_preflight.status,
            "preflight_supported",
        )

        extra_group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-1", experimental_group_id="treated",
                section="Methods", locator="pdf:p1:b4-p1:b4",
                source_digest=digest,
            ),
            source_document=str(pdf), blocks=(),
        )
        with patch("reaserch_agent.route_pipeline.attested_route_sources", return_value={"paper-1": [receipt]}), patch(
            "reaserch_agent.route_pipeline.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=[extra_group]),
        ), patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            missing_sibling = evaluate_route_decision_v1(
                self.goal, [protocol], source_root=self.root,
            )
        self.assertIn(
            "attested_group_not_extracted",
            missing_sibling.decision.decision_reasons,
        )
        control_group = PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id="paper-1", experimental_group_id="control",
                section="Methods", locator="pdf:p1:b2-p1:b4",
                source_digest=digest,
            ),
            source_document=str(pdf), blocks=(),
        )
        protocol["group_role"] = "characterization"
        with patch("reaserch_agent.route_pipeline.attested_route_sources", return_value={"paper-1": [receipt]}), patch(
            "reaserch_agent.route_pipeline.enumerate_attested_pdf_experimental_groups",
            return_value=PdfGroupEnumerationResultV1(groups=[control_group]),
        ):
            false_exclusion = evaluate_route_decision_v1(
                self.goal, [protocol], source_root=self.root,
                verified_group_roles_by_group={
                    ("paper-1", "control", digest): "synthesis"
                },
            )
        self.assertIn(
            "extracted_group_role_mismatch",
            false_exclusion.decision.decision_reasons,
        )
        protocol["group_role"] = "synthesis"
        protocol["evidence_bundle"][0]["doi"] = "10.1000/other"
        with patch("reaserch_agent.route_pipeline.attested_route_sources", return_value={"paper-1": [receipt]}), patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            wrong_doi = evaluate_route_decision_v1(
                self.goal, [protocol], source_root=self.root,
            )
        self.assertFalse(wrong_doi.decision.candidates[0].validation.source_scope_verified)
        self.assertIn(
            "source:evidence_doi_attestation_mismatch",
            wrong_doi.validation_diagnostics[wrong_doi.decision.candidates[0].route_id],
        )

        protocol["evidence_bundle"][0]["doi"] = "10.1000/paper"
        pdf.write_bytes(pdf.read_bytes() + b"\nchanged")
        changed_digest = "sha256_" + hashlib.sha256(pdf.read_bytes()).hexdigest()
        protocol["source"]["source_digest"] = changed_digest
        protocol["evidence_matrix"][0]["source_scope"]["source_digest"] = changed_digest
        with patch("reaserch_agent.route_pipeline.attested_route_sources", return_value={"paper-1": [receipt]}), patch(
            "reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science,
        ), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            changed_bytes = evaluate_route_decision_v1(
                self.goal, [protocol], source_root=self.root,
            )
        self.assertFalse(changed_bytes.decision.candidates[0].validation.source_scope_verified)
        self.assertIn(
            "source:source_attestation_digest_mismatch",
            changed_bytes.validation_diagnostics[changed_bytes.decision.candidates[0].route_id],
        )

    def test_independent_receipts_reach_deterministic_decider(self) -> None:
        protocol = self._protocol()
        protocol["evidence_bundle"][0]["verification_status"] = "verified_doi"
        protocol["evidence_bundle"][0]["full_text_status"] = "parsed"
        science = {
            "scientific_completeness": ScientificCompletenessV2(),
            "scientific_gate_issues": [],
            "audited_field_paths": [self.goal.required_fields[0]],
            "verified_graph_step_ids": ["S1"],
            "verified_runtime_resolution_fields": [],
            "verified_convention_field_paths": [],
        }
        device = {
            "status": "preflight_supported", "missing_capabilities": [],
            "unresolved_capabilities": [], "checked_capabilities": ["capability-1"],
            "snapshot_id": "snapshot-1", "reasons": [],
        }
        # The two adapter units have their own real-gate tests. These mocks
        # check receipt composition while SourceVerifier reads actual bytes.
        with patch("reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science) as science_mock, patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            result = _evaluate_route_decision_v1_impl(
                self.goal, [protocol], source_root=self.root,
                trusted_source_paths={"paper-1": [self.source]},
                require_attested_sources=False,
            )
        self.assertEqual(result.decision.status, "selected_for_planning")
        receipt = result.decision.candidates[0].validation
        self.assertTrue(receipt.source_scope_verified)
        self.assertTrue(set(self.goal.required_fields).issubset(receipt.verified_field_paths))
        self.assertIn("route_signature.operations[0]", receipt.verified_field_paths)
        self.assertEqual(receipt.source_route_signature, self.signature)
        science_input = science_mock.call_args.args[0]
        self.assertEqual(science_input.evidence_bundle[0].verification_status, "local_file")
        self.assertEqual(science_input.evidence_bundle[0].full_text_status, "local_parsed")

    def test_changed_original_source_prevents_candidate_discovery(self) -> None:
        protocol = self._protocol()
        self.source.write_text(self.source.read_text(encoding="utf-8") + "amended\n", encoding="utf-8")
        result = _evaluate_route_decision_v1_impl(
            self.goal, [protocol], source_root=self.root,
            trusted_source_paths={"paper-1": [self.source]},
            require_attested_sources=False,
        )
        self.assertEqual(result.decision.status, "unresolved")
        self.assertEqual(result.discovery.candidates, [])
        self.assertEqual(result.discovery.diagnostics[0].reason_code, "source_digest_mismatch")

    def test_model_claimed_verification_is_not_passed_to_science(self) -> None:
        protocol = self._protocol()
        protocol["evidence_bundle"][0]["excerpt"] += " FABRICATED GRAPH FACT 777 C"
        protocol["evidence_bundle"][0]["verification_status"] = "verified_doi"
        protocol["evidence_bundle"][0]["full_text_status"] = "parsed"
        with patch("reaserch_agent.route_pipeline.audit_route_candidate_science") as science_mock:
            science_mock.return_value = {
                "scientific_completeness": None, "scientific_gate_issues": [],
                "audited_field_paths": [], "verified_graph_step_ids": [],
                "verified_runtime_resolution_fields": [],
                "verified_convention_field_paths": [],
            }
            result = _evaluate_route_decision_v1_impl(
                self.goal, [protocol], source_root=self.root,
                trusted_source_paths={"paper-1": [self.source]},
                require_attested_sources=False,
            )
        science_input = science_mock.call_args.args[0]
        self.assertEqual(science_input.evidence_bundle[0].verification_status, "unknown")
        self.assertEqual(science_input.evidence_bundle[0].full_text_status, "unknown")
        self.assertEqual(result.decision.status, "unresolved")

    def test_unresolved_discovered_group_prevents_selecting_other_route(self) -> None:
        science = {
            "scientific_completeness": ScientificCompletenessV2(),
            "scientific_gate_issues": [],
            "audited_field_paths": self.goal.required_fields,
            "verified_graph_step_ids": ["S1"],
            "verified_runtime_resolution_fields": [],
            "verified_convention_field_paths": [],
        }
        device = {
            "status": "preflight_supported", "missing_capabilities": [],
            "unresolved_capabilities": [], "checked_capabilities": ["capability-1"],
            "snapshot_id": "snapshot-1", "reasons": [],
        }
        incomplete = {"source_title": "Unparsed second study", "steps": [{"操作": "mix"}]}
        with patch("reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            result = _evaluate_route_decision_v1_impl(
                self.goal, [self._protocol(), incomplete], source_root=self.root,
                trusted_source_paths={"paper-1": [self.source]},
                require_attested_sources=False,
            )
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIsNone(result.decision.selected_route_id)
        self.assertIn("candidate_discovery_incomplete", result.decision.decision_reasons)
        self.assertEqual(result.decision.candidates[0].status, "unresolved")

    def test_conflicting_route_fact_compilation_cannot_leave_selected_candidate(self) -> None:
        protocol = self._protocol()
        protocol["route_facts"] = [{"fact_id": "unbound"}]
        science = {
            "scientific_completeness": ScientificCompletenessV2(),
            "scientific_gate_issues": [],
            "audited_field_paths": self.goal.required_fields,
            "verified_graph_step_ids": ["S1"],
            "verified_runtime_resolution_fields": [],
            "verified_convention_field_paths": [],
        }
        device = {
            "status": "preflight_supported", "missing_capabilities": [],
            "unresolved_capabilities": [], "checked_capabilities": ["capability-1"],
            "snapshot_id": "snapshot-1", "reasons": [],
        }
        with patch("reaserch_agent.route_pipeline.audit_route_candidate_science", return_value=science), patch(
            "reaserch_agent.route_pipeline.preflight_route_capabilities", return_value=device,
        ):
            result = _evaluate_route_decision_v1_impl(
                self.goal, [protocol], source_root=self.root,
                trusted_source_paths={"paper-1": [self.source]},
                require_attested_sources=False,
            )
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIsNone(result.decision.selected_route_id)
        self.assertIn("route_fact_evidence_conflict", result.decision.decision_reasons)
        self.assertEqual(
            result.compilation_diagnostics[0]["reason_code"],
            "route_fact_evidence_conflict",
        )

    def test_research_agent_api_persists_decision_without_publishing_plan(self) -> None:
        agent = ResearchAgent(
            model=object(), use_llm=False, knowledge_base_dir=str(self.root),
            memory_dir=str(self.root), enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2", extracted_protocols=[self._protocol()],
        )
        with patch(
            "reaserch_agent.route_pipeline.attested_route_sources",
            return_value={"paper-1": [self.source]},
        ):
            result = agent.evaluate_route_decision_v1(state, self.goal)
        self.assertEqual(state.route_decision_v1["decision_id"], result.decision.decision_id)
        self.assertEqual(state.route_compilation_diagnostics_v1, result.compilation_diagnostics)
        self.assertEqual(
            state.persistent_outputs["化学路线决策 V1"]["decision_id"],
            result.decision.decision_id,
        )
        self.assertEqual(state.macro_plan, [])
        self.assertEqual(state.research_action_package_v2, {})

    def test_research_agent_registry_file_without_external_event_abstains(self) -> None:
        PaperRegistry(self.root).upsert(
            title="Primary study", doi="10.1000/local",
            verification_status="verified_doi", full_text_status="parsed",
            corpus_file=str(self.source),
        )
        agent = ResearchAgent(
            model=object(), use_llm=False, knowledge_base_dir=str(self.root),
            memory_dir=str(self.root), enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2",
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2", extracted_protocols=[self._protocol()],
        )
        result = agent.evaluate_route_decision_v1(state, self.goal)
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIsNone(result.decision.selected_route_id)
        self.assertEqual(result.discovery.candidates, [])
        self.assertIn(
            "trusted_source_event_missing", result.decision.decision_reasons
        )
        self.assertEqual(state.macro_plan, [])

    def test_unsigned_event_object_cannot_authorize_production_source(self) -> None:
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1", paper_id="paper-1",
            kb_relative_path="_pdf_sources/campaign/paper.pdf",
            document_digest="sha256_" + "a" * 64,
            document_kind="primary_paper",
            attestation_digest="sha256_" + "b" * 64,
            issuer="untrusted-input", identity_verdict="primary_verified",
        )
        with patch(
            "reaserch_agent.route_pipeline.attested_route_sources",
            return_value={},
        ) as indexed:
            result = evaluate_route_decision_v1(
                self.goal, [self._protocol()], source_root=self.root,
                trusted_source_events=[event],
            )
        self.assertIsNone(indexed.call_args.args[1])
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIn(
            "unsigned_trusted_source_event_rejected",
            result.decision.decision_reasons,
        )

    def test_signed_envelope_reaches_independently_verifying_index(self) -> None:
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1", paper_id="paper-1",
            kb_relative_path="_pdf_sources/campaign/paper.pdf",
            document_digest="sha256_" + "a" * 64,
            document_kind="primary_paper",
            attestation_digest="sha256_" + "b" * 64,
            issuer="reviewed-issuer", identity_verdict="primary_verified",
        )
        signed = {"signed": "opaque input"}
        keys = {"reviewed-key": TrustedIssuerPublicKeyV1(
            public_key_bytes=b"x" * 32, allowed_issuer="reviewed-issuer",
        )}
        with patch(
            "reaserch_agent.route_pipeline.verify_signed_trusted_acquisition_event",
            return_value=SignedEventVerificationV1(
                verified=True, event=event,
            ),
        ) as verifier, patch(
            "reaserch_agent.route_pipeline.attested_route_sources",
            return_value={},
        ) as indexed:
            evaluate_route_decision_v1(
                self.goal, [], source_root=self.root,
                signed_source_events=[signed], trusted_public_keys=keys,
            )
        verifier.assert_called_once_with(envelope=signed, trusted_public_keys=keys)
        self.assertEqual(indexed.call_args.args[1], [signed])
        self.assertEqual(indexed.call_args.kwargs["trusted_public_keys"], keys)

        with patch(
            "reaserch_agent.route_pipeline.verify_signed_trusted_acquisition_event",
            return_value=SignedEventVerificationV1(
                reason_code="signature_mismatch",
            ),
        ), patch(
            "reaserch_agent.route_pipeline.attested_route_sources",
            return_value={},
        ) as indexed:
            rejected = evaluate_route_decision_v1(
                self.goal, [], source_root=self.root,
                signed_source_events=[signed], trusted_public_keys=keys,
            )
        self.assertEqual(indexed.call_args.args[1], [signed])
        self.assertIn(
            "trusted_source_event_signature_mismatch",
            rejected.decision.decision_reasons,
        )

    def test_research_agent_uses_constructor_keys_not_workflow_state(self) -> None:
        signed = {"signed": "deployment input"}
        keys = {"deployment-key": TrustedIssuerPublicKeyV1(
            public_key_bytes=b"x" * 32, allowed_issuer="reviewed-issuer",
        )}
        agent = ResearchAgent(
            model=object(), use_llm=False, knowledge_base_dir=str(self.root),
            memory_dir=str(self.root), enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[signed],
            trusted_route_public_keys=keys,
        )
        state = ResearchAgentState(
            event=ResearchEvent(
                event_type="bootstrap", query="prepare product",
                constraints={"trusted_route_public_keys": {"attacker": "fake"}},
            ),
            contract_version="v2", extracted_protocols=[self._protocol()],
        )
        with patch(
            "reaserch_agent.route_pipeline.verify_signed_trusted_acquisition_event",
            return_value=SignedEventVerificationV1(
                reason_code="signature_mismatch",
            ),
        ) as verifier:
            result = agent.evaluate_route_decision_v1(state, self.goal)
        verifier.assert_called_once_with(envelope=signed, trusted_public_keys=keys)
        self.assertIn(
            "trusted_source_event_signature_mismatch",
            result.decision.decision_reasons,
        )

    @unittest.skipUnless(
        importlib.util.find_spec("fitz") and importlib.util.find_spec("cryptography"),
        "PyMuPDF and cryptography required",
    )
    def test_real_signed_pdf_reaches_research_group_coverage_audit(self) -> None:
        import fitz
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        pdf = self.root / "_pdf_sources" / "campaign" / "paper.pdf"
        pdf.parent.mkdir(parents=True)
        document = fitz.open()
        page = document.new_page()
        for index, (line, size, font) in enumerate((
            ("Methods", 16, "hebo"),
            ("control", 14, "hebo"),
            ("Mix 2 mmol salt in water.", 10, "helv"),
            ("Recover the control precipitate.", 10, "helv"),
        )):
            page.insert_text(fitz.Point(72, 70 + 48 * index), line,
                             fontsize=size, fontname=font)
        document.save(str(pdf))
        document.close()
        digest = "sha256_" + hashlib.sha256(pdf.read_bytes()).hexdigest()
        paper_id = "doi_10_1000_example"
        attestation = SourceDocumentAttestationV1(
            schema_version="source_document_attestation_v1",
            paper_id=paper_id, kb_relative_path="_pdf_sources/campaign/paper.pdf",
            document_digest=digest, document_kind="primary_paper",
            doi="10.1000/example", parent_doi="",
            acquisition_url="https://publisher.example/article/example",
            request_url="https://publisher.example/paper.pdf",
            final_url="https://publisher.example/paper.pdf",
            identity_evidence_type="publisher_doi_link",
            identity_evidence_url="https://publisher.example/article/example",
            identity_evidence_digest="sha256_" + "a" * 64,
            identity_status="verified", issuer="independent-reviewer",
            issued_at="2026-09-27T12:00:00Z",
        )
        attestation_path = attestation_path_for(self.root, paper_id, digest)
        attestation_path.parent.mkdir(parents=True)
        attestation_path.write_text(
            json.dumps(attestation.model_dump(mode="json")), encoding="utf-8",
        )
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id=paper_id, kb_relative_path=attestation.kb_relative_path,
            document_digest=digest, document_kind="primary_paper",
            attestation_digest=canonical_digest(attestation.model_dump(mode="json")),
            issuer="independent-reviewer", identity_verdict="primary_verified",
        )
        private_key = Ed25519PrivateKey.generate()
        key_id = "reviewer-1"
        event_json = event.model_dump(mode="json")
        envelope = {
            "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
            "key_id": key_id, "event": event_json,
            "signature": base64.b64encode(private_key.sign(
                signed_trusted_acquisition_event_message_v1(
                    key_id=key_id, event=event_json,
                )
            )).decode("ascii"),
        }
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        PaperRegistry(self.root).upsert(
            title="Primary study", doi="10.1000/example",
            verification_status="local_file", full_text_status="parsed",
            pdf_file=str(pdf),
        )
        agent = ResearchAgent(
            model=object(), use_llm=False, knowledge_base_dir=str(self.root),
            memory_dir=str(self.root), enable_memory=False,
            enable_online_literature=False, enable_web_search=False,
            contract_version="v2", signed_route_source_events=[envelope],
            trusted_route_public_keys={
                key_id: TrustedIssuerPublicKeyV1(
                    public_key_bytes=public_key,
                    allowed_issuer="independent-reviewer",
                ),
            },
        )
        state = ResearchAgentState(
            event=ResearchEvent(event_type="bootstrap", query="prepare product"),
            contract_version="v2", extracted_protocols=[],
        )
        result = agent.evaluate_route_decision_v1(state, self.goal)
        self.assertEqual(result.decision.status, "unresolved")
        self.assertIn("attested_group_not_extracted", result.decision.decision_reasons)
        self.assertNotIn("attested_source_unavailable", result.decision.decision_reasons)
        self.assertNotIn("trusted_source_event_missing", result.decision.decision_reasons)
        self.assertEqual(state.route_decision_v1["decision_id"], result.decision.decision_id)


if __name__ == "__main__":
    unittest.main()
