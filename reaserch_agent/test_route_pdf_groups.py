"""Original-PDF experimental group enumeration is boundary conservative."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reaserch_agent.route_attestation import AttestedRouteSourceV1
from reaserch_agent.route_pdf_groups import (
    audit_pdf_group_extraction_coverage,
    audit_pdf_group_roles,
    enumerate_attested_pdf_experimental_groups,
    enumerate_pdf_experimental_groups,
)


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class PdfExperimentalGroupEnumerationTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "source.pdf"
        self._fitz = fitz
        self.lines = [
            ("Methods", 16, "hebo"),
            ("control", 14, "hebo"),
            ("Mix 2 mmol salt in water.", 10, "helv"),
            ("Recover control precipitate.", 10, "helv"),
            ("treated", 14, "hebo"),
            ("Mix 9 mmol salt in water.", 10, "helv"),
        ]
        self._write_pdf()

    def _write_pdf(self, *, page_break_after: int = -1) -> None:
        document = self._fitz.open()
        page = document.new_page()
        line_index = 0
        for index, (body, size, font) in enumerate(self.lines):
            if index == page_break_after:
                page = document.new_page()
                line_index = 0
            page.insert_text(
                self._fitz.Point(72, 70 + line_index * 48), body,
                fontsize=size, fontname=font,
            )
            line_index += 1
        document.save(str(self.path))
        document.close()

    def _enumerate(self):
        return enumerate_pdf_experimental_groups(
            {"paper-1": self.path}, source_root=self.root,
        )

    def test_two_explicit_groups_keep_quotes_separate_and_original_digest(self) -> None:
        result = self._enumerate()
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(result.groups), 2)
        control, treated = result.groups
        self.assertEqual(control.source_scope.experimental_group_id, "control")
        self.assertEqual(control.source_scope.section, "Methods")
        self.assertEqual(control.source_scope.locator, "pdf:p1:b2-p1:b4")
        self.assertEqual(treated.source_scope.locator, "pdf:p1:b5-p1:b6")
        self.assertEqual(control.source_scope.source_digest,
                         "sha256_" + sha256(self.path.read_bytes()).hexdigest())
        self.assertIn("Mix 2 mmol", "\n".join(block.text for block in control.blocks))
        self.assertNotIn("9 mmol", "\n".join(block.text for block in control.blocks))
        self.assertEqual(control.blocks[1].locator, "pdf:p1:b3-p1:b3")
        self.assertEqual(control.source_document, str(self.path))

    def test_group_span_can_cross_pages(self) -> None:
        self.path.unlink()
        self._write_pdf(page_break_after=3)
        result = self._enumerate()
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(result.groups[0].source_scope.locator, "pdf:p1:b2-p2:b1")

    def test_duplicate_group_title_abstains_for_entire_section(self) -> None:
        self.lines.extend([
            ("control", 14, "hebo"),
            ("Mix 7 mmol salt in water.", 10, "helv"),
        ])
        self.path.unlink()
        self._write_pdf()
        result = self._enumerate()
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code, "duplicate_group_heading")

    def test_unmarked_peer_boundary_abstains(self) -> None:
        self.lines[4] = ("treated", 14, "helv")
        self.path.unlink()
        self._write_pdf()
        result = self._enumerate()
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code, "group_boundary_unmarked")

    def test_nested_heading_size_abstains(self) -> None:
        self.lines.insert(3, ("Workup", 12, "hebo"))
        self.path.unlink()
        self._write_pdf()
        result = self._enumerate()
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "nested_or_mixed_group_headings")

    def test_ambiguous_section_boundary_abstains(self) -> None:
        self.lines.insert(4, ("Large unstyled body", 16, "helv"))
        self.path.unlink()
        self._write_pdf()
        result = self._enumerate()
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "section_boundary_unmarked")

    def test_duplicate_section_heading_abstains(self) -> None:
        self.lines.extend([
            ("Methods", 16, "hebo"),
            ("second", 14, "hebo"),
            ("Text of second group.", 10, "helv"),
        ])
        self.path.unlink()
        self._write_pdf()
        result = self._enumerate()
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "duplicate_experimental_section")

    def test_outside_root_cannot_be_enumerated(self) -> None:
        other = self.root.parent / (self.root.name + "-other.pdf")
        other.write_bytes(self.path.read_bytes())
        self.addCleanup(other.unlink)
        result = enumerate_pdf_experimental_groups(
            {"paper-1": other}, source_root=self.root,
        )
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "pdf_source_unavailable_or_untrusted")

    def test_image_only_pdf_abstains(self) -> None:
        self.path.unlink()
        document = self._fitz.open()
        document.new_page()
        document.save(str(self.path))
        document.close()
        result = self._enumerate()
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code, "pdf_no_extractable_text")

    def _receipt(self) -> AttestedRouteSourceV1:
        return AttestedRouteSourceV1(
            paper_id="paper-1", path=self.path,
            document_digest="sha256_" + sha256(self.path.read_bytes()).hexdigest(),
            document_kind="primary_paper", doi="10.1000/example",
            attestation_digest="sha256_" + "a" * 64,
        )

    def test_attested_wrapper_preserves_only_matching_original_bytes(self) -> None:
        receipt = self._receipt()
        with patch(
            "reaserch_agent.route_pdf_groups.attested_route_sources",
            return_value={"paper-1": [receipt]},
        ):
            result = enumerate_attested_pdf_experimental_groups(self.root, [object()])
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(result.diagnostics, [])
        self.assertTrue(all(group.source_scope.source_digest == receipt.document_digest
                            for group in result.groups))

    def test_pdf_changed_between_attestation_and_enumeration_drops_groups(self) -> None:
        receipt = self._receipt()

        def attest_then_change(_root, _events):
            self.lines[2] = ("Mix 5 mmol salt in water.", 10, "helv")
            self.path.unlink()
            self._write_pdf()
            return {"paper-1": [receipt]}

        with patch(
            "reaserch_agent.route_pdf_groups.attested_route_sources",
            side_effect=attest_then_change,
        ):
            result = enumerate_attested_pdf_experimental_groups(self.root, [object()])
        self.assertEqual(result.groups, [])
        self.assertEqual(
            [item.reason_code for item in result.diagnostics],
            ["source_attestation_digest_mismatch"] * 2,
        )

    def test_missing_attestation_cannot_authorize_any_group(self) -> None:
        with patch(
            "reaserch_agent.route_pdf_groups.attested_route_sources",
            return_value={},
        ):
            result = enumerate_attested_pdf_experimental_groups(self.root, [])
        self.assertEqual(result.groups, [])
        self.assertEqual(result.diagnostics[0].reason_code,
                         "attested_pdf_source_missing")

    def test_extraction_coverage_reports_unextracted_sibling_group(self) -> None:
        enumerated = self._enumerate()
        self.assertEqual(len(enumerated.groups), 2)
        coverage = audit_pdf_group_extraction_coverage(
            enumerated.groups,
            [{
                "paper_id": "paper-1", "experimental_groups": [{
                    "experimental_group_id": "control",
                    "group_role": "synthesis",
                    "source": {"source_digest": enumerated.groups[0].source_scope.source_digest},
                }],
            }],
        )
        self.assertEqual(coverage.matched_group_count, 1)
        self.assertEqual(len(coverage.missing_scopes), 1)
        self.assertEqual(coverage.missing_scopes[0].experimental_group_id, "treated")

    def test_stale_digest_and_paper_level_summary_do_not_cover_a_group(self) -> None:
        enumerated = self._enumerate()
        coverage = audit_pdf_group_extraction_coverage(
            enumerated.groups,
            [{
                "paper_id": "paper-1", "experimental_group_id": "control",
                "source": {"source_digest": "sha256_" + "0" * 64},
            }, {
                "paper_id": "paper-1", "source_title": "summary",
                "source": {"source_digest": enumerated.groups[0].source_scope.source_digest},
            }],
        )
        self.assertEqual(coverage.matched_group_count, 0)
        self.assertEqual({scope.experimental_group_id for scope in coverage.missing_scopes},
                         {"control", "treated"})

    def _role_protocols(self, digest: str) -> list[dict]:
        return [{
            "paper_id": "paper-1",
            "experimental_groups": [
                {
                    "experimental_group_id": "control",
                    "group_role": "synthesis",
                    "source": {"source_digest": digest},
                },
                {
                    "experimental_group_id": "treated",
                    "group_role": "synthesis",
                    "source": {"source_digest": digest},
                },
            ],
        }]

    def test_independent_role_audit_accepts_only_complete_matching_map(self) -> None:
        groups = self._enumerate().groups
        digest = groups[0].source_scope.source_digest
        identities = [
            (group.source_scope.paper_id,
             group.source_scope.experimental_group_id,
             group.source_scope.source_digest)
            for group in groups
        ]
        result = audit_pdf_group_roles(
            groups, self._role_protocols(digest),
            group_roles_by_group={key: "synthesis" for key in identities},
        )
        self.assertEqual(result.verified_group_count, 2)
        self.assertEqual(result.issues, ())

    def test_model_cannot_hide_synthesis_group_as_characterization(self) -> None:
        groups = self._enumerate().groups
        digest = groups[0].source_scope.source_digest
        protocols = self._role_protocols(digest)
        protocols[0]["experimental_groups"][1]["group_role"] = "characterization"
        roles = {
            (group.source_scope.paper_id,
             group.source_scope.experimental_group_id,
             group.source_scope.source_digest): "synthesis"
            for group in groups
        }
        result = audit_pdf_group_roles(
            groups, protocols, group_roles_by_group=roles,
        )
        self.assertEqual(result.verified_group_count, 1)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].source_scope.experimental_group_id,
                         "treated")
        self.assertEqual(result.issues[0].reason_code,
                         "extracted_group_role_mismatch")
        self.assertEqual(result.issues[0].extracted_role, "characterization")

    def test_missing_trusted_role_or_group_is_reported(self) -> None:
        groups = self._enumerate().groups
        digest = groups[0].source_scope.source_digest
        protocols = self._role_protocols(digest)
        protocols[0]["experimental_groups"].pop()
        key = (
            groups[0].source_scope.paper_id,
            groups[0].source_scope.experimental_group_id,
            groups[0].source_scope.source_digest,
        )
        result = audit_pdf_group_roles(
            groups, protocols,
            group_roles_by_group={key: "synthesis"},
        )
        self.assertEqual(result.verified_group_count, 1)
        self.assertEqual(result.issues[0].reason_code,
                         "trusted_group_role_missing")
        complete_roles = {
            (group.source_scope.paper_id,
             group.source_scope.experimental_group_id,
             group.source_scope.source_digest): "synthesis"
            for group in groups
        }
        result = audit_pdf_group_roles(
            groups, protocols, group_roles_by_group=complete_roles,
        )
        self.assertEqual(result.issues[0].reason_code,
                         "experimental_group_not_extracted")


if __name__ == "__main__":
    unittest.main()
