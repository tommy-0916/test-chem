"""Full verification context must be source-bound at every consumption boundary.

Four acceptance cases:

1. a real located excerpt with a forged or cross-group context is blocked
   before any semantic verification;
2. a real context that does not contain the located excerpt is blocked;
3. a legitimate context spanning more than three blocks verifies normally
   with the short located excerpt, without re-triggering the old span
   refusal;
4. after compile, save, and rebuild, the same context is still what the
   evidence carries, and a modified context cannot silently degrade to the
   short excerpt.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import unittest

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1
from reaserch_agent.route_group_compiler import _fact_issue
from reaserch_agent.route_group_fact_receipt import _literal_fact_reason
from reaserch_agent.route_pdf_clause_quote_tightening import (
    tighten_unreviewed_clause_quotes,
)
from reaserch_agent.route_pdf_group_proposals import (
    associate_pdf_group_proposals,
)
from reaserch_agent.route_pdf_groups import PdfExperimentalGroupV1, PdfSourceBlockV1
from reaserch_agent.route_pdf_locator_production import produce_pdf_proposal_locators
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote


class PdfVerificationContextBindingTest(unittest.TestCase):
    BLOCKS = (
        "The feed was prepared in a beaker.",
        "After a total reaction time of 1 h, around 200 mL of",
        "the suspension (~500 mg samples) was divided into 8 parts, followed",
        "by a washing protocol using deionized water three",
        "times before the portions were collected.",
    )
    LONG = " ".join(BLOCKS)
    CLAUSE = (
        "around 200 mL of the suspension (~500 mg samples) was divided "
        "into 8 parts"
    )

    @staticmethod
    def _group(
        blocks: tuple[str, ...], *, paper_id: str,
        group_id: str,
    ) -> PdfExperimentalGroupV1:
        digest = "sha256_" + sha256(
            (paper_id + "\0" + group_id + "\0" + " ".join(blocks))
            .encode("utf-8")
        ).hexdigest()
        return PdfExperimentalGroupV1(
            source_scope=ExperimentalGroupScopeV1(
                paper_id=paper_id,
                experimental_group_id=group_id,
                section="Methods",
                locator=f"pdf:p2:b1-p2:b{len(blocks)}",
                source_digest=digest,
            ),
            source_document="/attested/context-binding.pdf",
            blocks=tuple(
                PdfSourceBlockV1(f"pdf:p2:b{i}-p2:b{i}", text)
                for i, text in enumerate(blocks, start=1)
            ),
        )

    @classmethod
    def _group_a(cls) -> PdfExperimentalGroupV1:
        return cls._group(cls.BLOCKS, paper_id="paper-context",
                          group_id="Context group")

    @classmethod
    def _proposal(cls, group: PdfExperimentalGroupV1, fact: dict) -> dict:
        scope = group.source_scope
        return {
            "source_group_ref": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
            },
            "material_graph": [{
                "step_index": 0,
                "operation": "divided into 8 parts",
                "material_outputs": [{"name": "suspension"}],
            }],
            "route_facts": [fact],
        }

    @classmethod
    def _fact(cls, group: PdfExperimentalGroupV1, **overrides) -> dict:
        scope = group.source_scope
        fact = {
            "fact_id": "f-op",
            "field_path": "route_signature.operations[0]",
            "value": "divided into 8 parts",
            "unit": "",
            "excerpt": cls.CLAUSE,
            "required": True,
        }
        # Association normally runs on located proposals; supply the located
        # anchor the locator production would have written (the first block
        # of the resolved span).
        binding, issue = bind_pdf_quote(cls._blocks_of(group), cls.CLAUSE)
        assert issue == "" and binding is not None
        fact["block_locator"] = group.blocks[binding.first_block_index].locator
        fact.update(overrides)
        return fact

    @staticmethod
    def _blocks_of(group: PdfExperimentalGroupV1):
        return [(block.locator, block.text) for block in group.blocks]

    @classmethod
    def _located_source(cls, group: PdfExperimentalGroupV1) -> dict:
        scope = group.source_scope
        binding, issue = bind_pdf_quote(cls._blocks_of(group), cls.CLAUSE)
        assert issue == "" and binding is not None
        return {
            "paper_id": scope.paper_id,
            "experimental_group_id": scope.experimental_group_id,
            "section": scope.section,
            "locator": binding.locator,
            "source_digest": scope.source_digest,
        }

    def test_foreign_or_forged_context_blocks_before_semantic_checks(self) -> None:
        group = self._group_a()
        other = self._group(
            ("The other arm suspension was divided into 4 parts and kept.",
             "It was stored cold before use."),
            paper_id="paper-context", group_id="Other arm",
        )
        foreign_context = " ".join(block.text for block in other.blocks)
        forged_context = (
            "The feed was prepared in a beaker, and 500 mL of acid was "
            "added to shift the material balance."
        )
        for context, label in ((foreign_context, "foreign group"),
                               (forged_context, "invented text")):
            with self.subTest(context=label):
                fact = self._fact(group, verification_excerpt=context)
                proposal = self._proposal(group, fact)
                associated = associate_pdf_group_proposals([group], [proposal])
                self.assertEqual(associated.protocols, [])
                self.assertEqual(
                    [d.reason_code for d in associated.diagnostics],
                    ["proposal_verification_context_not_in_source_group"],
                )
                # The receipt predicate must refuse the context as well, even
                # when the located excerpt itself is genuine.
                receipt_fact = {
                    **fact, "source": self._located_source(group),
                }
                receipt_fact.pop("block_locator", None)
                reason = _literal_fact_reason(
                    receipt_fact, group, self._blocks_of(group),
                    graph=proposal["material_graph"], facts=[receipt_fact],
                )
                self.assertEqual(
                    reason, "verification_context_not_in_source_group")

    def test_real_context_without_located_excerpt_blocks_association(self) -> None:
        group = self._group_a()
        real_context = self.BLOCKS[0]  # genuine, unique -- but unrelated
        fact = self._fact(group, verification_excerpt=real_context)
        proposal = self._proposal(group, fact)
        associated = associate_pdf_group_proposals([group], [proposal])
        self.assertEqual(associated.protocols, [])
        self.assertEqual(
            [d.reason_code for d in associated.diagnostics],
            ["proposal_verification_context_missing_located_excerpt"],
        )
        receipt_fact = {**fact, "source": self._located_source(group)}
        receipt_fact.pop("block_locator", None)
        reason = _literal_fact_reason(
            receipt_fact, group, self._blocks_of(group),
            graph=proposal["material_graph"], facts=[receipt_fact],
        )
        self.assertEqual(reason, "verification_context_missing_located_excerpt")
        compile_reason = _fact_issue(
            receipt_fact, paper_id=group.source_scope.paper_id,
            group_id=group.source_scope.experimental_group_id,
            section="Methods", document_digest=group.source_scope.source_digest,
            graph=[], signature={},
        )
        self.assertEqual(
            compile_reason, "verification_context_missing_located_excerpt")

    def test_legitimate_long_context_verifies_without_span_refusal(self) -> None:
        group = self._group_a()
        proposal = self._proposal(
            group, self._fact(group, excerpt=self.LONG),
        )
        tightened, audit, issues = tighten_unreviewed_clause_quotes(
            proposal, group,
        )
        self.assertEqual(issues, [])
        self.assertEqual(len(audit), 1)
        fact = tightened["route_facts"][0]
        self.assertEqual(fact["excerpt"], self.CLAUSE)
        self.assertEqual(fact["verification_excerpt"], self.LONG)

        located = produce_pdf_proposal_locators([group], [tightened])
        self.assertEqual(located.diagnostics, [])
        associated = associate_pdf_group_proposals([group], located.proposals)
        self.assertEqual(associated.diagnostics, [])
        self.assertEqual(len(associated.protocols), 1)
        protocol_fact = associated.protocols[0]["route_facts"][0]
        # The associated protocol still carries the full context, and the
        # semantic predicate verifies against it (context spans four
        # blocks; the display budget is not re-imposed on verification).
        self.assertEqual(
            protocol_fact["verification_excerpt"], self.LONG)
        reason = _literal_fact_reason(
            protocol_fact, group, self._blocks_of(group),
            graph=tightened["material_graph"],
            facts=associated.protocols[0]["route_facts"],
        )
        self.assertEqual(reason, "")


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class PdfVerificationContextRebuildTest(unittest.TestCase):
    """Compile/save/rebuild re-verification keeps the same full context."""

    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "context_methods.pdf"
        self.long_context = (
            "After 1 h around 200 mL of the suspension (~500 mg samples) "
            "was divided into 8 parts, followed by washing with water "
            "three times in total."
        )
        self.short_located = (
            "around 200 mL of the suspension (~500 mg samples) was divided "
            "into 8 parts"
        )
        self.lines = [
            ("Methods", 16, "hebo"),
            ("control", 14, "hebo"),
            ("After 1 h around 200 mL of the", 10, "helv"),
            ("suspension (~500 mg samples) was divided into 8", 10, "helv"),
            ("parts, followed by washing with water", 10, "helv"),
            ("three times in total.", 10, "helv"),
        ]
        self._fitz = fitz
        doc = self._fitz.open()
        page = doc.new_page()
        for index, (body, size, font) in enumerate(self.lines):
            page.insert_text(
                self._fitz.Point(72, 70 + index * 48), body,
                fontsize=size, fontname=font,
            )
        doc.save(str(self.path))
        doc.close()
        self.digest = "sha256_" + sha256(
            self.path.read_bytes()).hexdigest()

    def _candidate(self):
        from chem_agent_contracts.route_candidate import (
            RouteCandidateV1, RouteFieldEvidenceV1, RouteSignatureV1,
            RouteTargetV1,
        )
        from chem_agent_contracts.v2 import (
            EvidenceItemV2, ProvenanceV2, canonical_digest,
        )

        group_scope = ExperimentalGroupScopeV1(
            paper_id="paper-1",
            experimental_group_id="control",
            section="Methods",
            locator="pdf:p1:b2-p1:b6",
            source_digest=self.digest,
        )
        # The stored field locator anchors the short located excerpt
        # (three blocks); the carried evidence text is the full context
        # (four blocks), exactly what semantic verification used.
        field_scope = group_scope.model_copy(
            update={"locator": "pdf:p1:b3-p1:b5"})
        return RouteCandidateV1(
            route_id="route-1",
            target=RouteTargetV1(
                material="target material",
                desired_state="retained_wet_solid",
                objective="synthesize target material",
            ),
            source_scope=group_scope,
            route_signature=RouteSignatureV1(
                route_family="precipitation",
                target_transformation="precursor_to_wet_solid",
                precursor_roles=["metal_salt"],
                reagent_roles=[],
                operations=["split"],
                control_modes=[],
                endpoint_state="retained_wet_solid",
            ),
            evidence_bundle=[EvidenceItemV2(
                evidence_id="E-1",
                excerpt=self.long_context,
                verification_status="verified_doi",
                full_text_status="parsed",
            )],
            evidence_matrix=[RouteFieldEvidenceV1(
                field_path="precursor.amount",
                value=200,
                unit="mL",
                status="supported",
                source_scope=field_scope,
                evidence_id="E-1",
                provenance=ProvenanceV2(
                    kind="paper",
                    reference="E-1",
                    evidence_class="paper_explicit",
                    source_path="evidence_bundle.items[0].excerpt",
                    excerpt=self.long_context,
                    source_digest=canonical_digest(self.long_context),
                ),
            )],
            origin="paper_experimental_group",
        )

    def _verify(self, candidate):
        from reaserch_agent.route_pdf_source import verify_route_pdf_source

        return verify_route_pdf_source(
            candidate,
            source_paths={"paper-1": self.path},
            source_root=self.root,
        )

    def test_rebuild_verifies_same_full_context_and_rejects_tampering(self) -> None:
        candidate = self._candidate()
        ok = self._verify(candidate)
        self.assertTrue(ok.source_scope_verified, ok.reasons)
        self.assertEqual(ok.verified_evidence_ids, ("E-1",))

        # The carried evidence text is the full context, not the short one.
        self.assertEqual(candidate.evidence_bundle[0].excerpt,
                         self.long_context)
        self.assertEqual(
            candidate.evidence_matrix[0].provenance.excerpt,
            self.long_context,
        )

        # A modified context (no silent fallback to the short excerpt):
        # the recorded digest no longer matches the carried text.
        tampered = self._candidate()
        tampered.evidence_matrix[0].provenance.excerpt = (
            self.long_context.replace("200 mL", "300 mL"))
        bad = self._verify(tampered)
        self.assertFalse(bad.source_scope_verified)
        self.assertTrue(any(
            "field_provenance_excerpt_digest_mismatch" in reason
            or "field_excerpt_not_in_source_group" in reason
            for reason in bad.reasons
        ), bad.reasons)

        # Replacing the context with the short excerpt without re-recording
        # the digest is equally rejected, not silently accepted.
        shortened = self._candidate()
        shortened.evidence_bundle[0].excerpt = self.short_located
        shortened.evidence_matrix[0].provenance.excerpt = self.short_located
        worse = self._verify(shortened)
        self.assertFalse(worse.source_scope_verified)
        self.assertTrue(any(
            "field_provenance_excerpt_digest_mismatch" in reason
            for reason in worse.reasons
        ), worse.reasons)


from pathlib import Path  # noqa: E402  (kept close to the fitz-gated class)
import tempfile  # noqa: E402


if __name__ == "__main__":
    unittest.main()
