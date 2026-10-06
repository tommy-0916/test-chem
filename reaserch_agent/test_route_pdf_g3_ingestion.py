# -*- coding: utf-8 -*-
"""G3 ingestion-round acceptance tests (route_pdf_groups/v4).

Covers:
- real Du 2022 SI enumeration across pages 3-4 (the running-head truncation
  fix) with pinned group locators;
- furniture-heading and numbered-section/TOC unit behavior;
- per-group quote binding scope (S1 binds in the binary group but NOT in the
  HE-PBA group — the cross-group reference boundary stays);
- unified 16 MiB PDF budget across entry points, JSON/text budgets unchanged,
  over-limit still refused;
- A01/Wu regression (A01's eight groups byte-identical locators; Wu SI still
  abstains).
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from chem_agent_contracts.route_candidate import (
    ExperimentalGroupScopeV1, RouteCandidateV1, RouteSignatureV1,
    RouteTargetV1,
)
from reaserch_agent.route_pdf_groups import (
    PDF_GROUP_PARSER_VERSION,
    _experimental_section_heading,
    enumerate_pdf_experimental_groups,
)
from reaserch_agent.route_pdf_source import (
    _MAX_PDF_SOURCE_BYTES,
    _MAX_SOURCE_BYTES,
    _PdfBlock,
    _furniture_heading_texts,
    _group_range,
)
from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
from reaserch_agent import route_attestation, route_receipt_producer, route_source
from reaserch_agent.route_pipeline import _matching_trusted_source

_REPO = Path(__file__).resolve().parent.parent
_G3_ROOT = (
    _REPO / "result" / "g3-du2022-real-input-20261006" / "kb"
    / "_pdf_sources" / "du2022"
)
DU_SI = _G3_ROOT / (
    "9f05be9f4a5d5c607dbefdeb8ebb5c89ac5fb7ee4385534e174c88e921efc7ee.pdf"
)
DU_MAIN = _G3_ROOT / (
    "87552f9c65ef308a7e2dda502d9078bba5396fe716fff659a71ae962de51206b.pdf"
)
A01_PDF = (
    _REPO / "result" / "a01-evidence-audit-20260927"
    / "huang-2023-institutional-copy.pdf"
)
WU_SI = (
    _REPO / "result" / "wu2025-real-input-20261004" / "kb" / "_pdf_sources"
    / "wu2025"
    / "cdd842d87e34a8dc70d87e5999e8665f5fe054e66a22f0908b72ea2953f365d4.pdf"
)

S1 = ("The product was collected by centrifugation and washed repeatedly "
      "with deionized water and ethanol, respectively.")
S2 = "The obtained product was designated CoFe-PBA."
S3 = ("The synthesis steps of high-entropy PBA are similar with that of "
      "binary PBAs, except that five metal salts replace one metal salt.")
S3B = ("The CoNiCuMnZnFe-PBA were prepared with 0.4 mmol:0.4 mmol:0.4 "
       "mmol:0.4 mmol:0.4 mmol ratio of Co/Ni/Cu/Mn/Zn.")
# Completed excerpts (Codex item 4): S4 beyond "500", S5 beyond the grinding
# sentence, S6 the same-procedure reference, each scoped to its own group.
S4 = ("The NiFe-PBA, CoFe-PBA, CoNiFe-PBA, NiCuFe-PBA, NiZnFe-PBA, "
      "CuZnFe-PBA, CoMnFe-PBA, and CoNiCuMnZnFe-PBA were annealed at 500 °C "
      "(2 °C min-1) in air for 2 h, for obtaining")
S5 = ("First, 43 mg of NiFe-PBA powder and 100 mg of sublimed sulfur were "
      "ground for 30 minutes to obtain a fine mixture. Then the mixture was "
      "placed in a 50 mL sealed Teflon autoclave and heated up to 130 °C "
      "for 12 h to prepare the final NiFe-PBA-S composites.")
S6 = ("Moreover, NiCuFe-PBA-S, CoNiCuFe-PBA-S, and HE-PBA-S were also "
      "prepared via the same synthetic procedure.")
PBAS_GROUP = ("Synthesis of NiFe-PBA-S, NiCuFe-PBA-S, CoNiCuFe-PBA-S, and "
              "HE-PBA-S")


def _enumerate(pdf: Path, paper_id: str):
    pdf = pdf.resolve()
    return enumerate_pdf_experimental_groups(
        {paper_id: [pdf]}, source_root=pdf.parent)


@unittest.skipUnless(DU_SI.is_file(), "G3 Du SI fixture not ingested")
class G3RealSiEnumerationTest(unittest.TestCase):
    """Real Du 2022 SI (39 pages, 9,874,019 bytes > old 8 MiB gate)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = _enumerate(DU_SI, "du-2022-si")
        cls.groups = {
            g.source_scope.experimental_group_id: g for g in cls.result.groups
        }

    def test_parser_version_v4(self) -> None:
        self.assertEqual(PDF_GROUP_PARSER_VERSION, "route_pdf_groups/v4")

    def test_twelve_groups_no_diagnostics(self) -> None:
        self.assertEqual(self.result.diagnostics, [])
        self.assertEqual(len(self.result.groups), 12)

    def test_page3_group_locators(self) -> None:
        expected = {
            "Synthesis of binary PBAs": "pdf:p3:b22-p3:b29",
            "Synthesis of ternary PBAs": "pdf:p3:b30-p3:b35",
            "Synthesis of quaternary PBAs": "pdf:p3:b36-p3:b39",
            "Synthesis of high-entropy PBA": "pdf:p3:b40-p3:b42",
            "Synthesis of cubic metal oxides": "pdf:p3:b43-p3:b46",
        }
        for group_id, locator in expected.items():
            self.assertIn(group_id, self.groups)
            self.assertEqual(self.groups[group_id].source_scope.locator, locator)
            self.assertEqual(
                self.groups[group_id].source_scope.section,
                "Section S1 Experimental Procedures",
            )

    def test_page4_groups_not_truncated_by_running_head(self) -> None:
        # The 16pt "SUPPORTING INFORMATION" running head formerly terminated
        # Section S1 at the page-3 end; page-4 groups were silently lost.
        expected = {
            "Synthesis of NiFe-PBA-S, NiCuFe-PBA-S, CoNiCuFe-PBA-S, and "
            "HE-PBA-S": "pdf:p4:b2-p4:b6",
            "Adsorption and soaking tests": "pdf:p4:b7-p4:b11",
            "Electrochemical Measurements": "pdf:p4:b12-p4:b25",
            "In situ UV-vis measurement": "pdf:p4:b26-p4:b35",
            "In situ XRD measurement": "pdf:p4:b36-p5:b1",
        }
        for group_id, locator in expected.items():
            self.assertIn(group_id, self.groups)
            self.assertEqual(self.groups[group_id].source_scope.locator, locator)

    def test_group_range_verifier_agrees_for_he_pba(self) -> None:
        # Enumeration kept the group, so the re-verification range finder
        # (shared boundary semantics) must agree on the same span.
        from reaserch_agent.route_pdf_source import _read_pdf_blocks_with_report
        blocks, issue, _ = _read_pdf_blocks_with_report(DU_SI.read_bytes())
        self.assertIsNone(issue)
        span = _group_range(
            blocks, "Section S1 Experimental Procedures",
            "Synthesis of high-entropy PBA",
        )
        self.assertIsNotNone(span)
        first, last = span
        self.assertEqual(
            f"pdf:p{blocks[first].page}:b{blocks[first].number}"
            f"-p{blocks[last].page}:b{blocks[last].number}",
            "pdf:p3:b40-p3:b42",
        )


@unittest.skipUnless(DU_SI.is_file(), "G3 Du SI fixture not ingested")
class G3PerGroupQuoteBindingTest(unittest.TestCase):
    """Quote binding is scoped to the actual group's blocks (Codex item 4)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.groups = {
            g.source_scope.experimental_group_id: g
            for g in _enumerate(DU_SI, "du-2022-si").groups
        }

    def _bind(self, group_id: str, quote: str):
        group = self.groups[group_id]
        pairs = [(block.locator, block.text) for block in group.blocks]
        captions = [block.locator for block in group.blocks if block.caption]
        return bind_pdf_quote(pairs, quote, caption_block_locators=captions)

    def test_s1_binds_in_binary_group(self) -> None:
        binding, issue = self._bind("Synthesis of binary PBAs", S1)
        self.assertIsNotNone(binding, issue)
        self.assertEqual(binding.locator, "pdf:p3:b26-p3:b27")
        binding2, issue2 = self._bind("Synthesis of binary PBAs", S2)
        self.assertIsNotNone(binding2, issue2)

    def test_s1_not_in_he_pba_group_scope(self) -> None:
        # The centrifugation protocol sentence lives in the binary group's
        # blocks; the HE-PBA group holds only the reference sentence.  Whole
        # -document binding success may not substitute for in-scope binding.
        binding, issue = self._bind("Synthesis of high-entropy PBA", S1)
        self.assertIsNone(binding)
        self.assertEqual(issue, "fact_excerpt_not_in_block")

    def test_he_pba_group_binds_reference_and_substitution(self) -> None:
        for quote in (S3, S3B):
            binding, issue = self._bind("Synthesis of high-entropy PBA", quote)
            self.assertIsNotNone(binding, f"{issue} for {quote[:40]}")

    def test_s4_complete_binds_in_oxide_group(self) -> None:
        binding, issue = self._bind("Synthesis of cubic metal oxides", S4)
        self.assertIsNotNone(binding, issue)
        self.assertEqual(binding.locator, "pdf:p3:b44-p3:b45")

    def test_s5_complete_binds_in_pbas_group(self) -> None:
        binding, issue = self._bind(PBAS_GROUP, S5)
        self.assertIsNotNone(binding, issue)
        self.assertEqual(binding.locator, "pdf:p4:b4-p4:b5")

    def test_s6_same_procedure_binds_in_pbas_group(self) -> None:
        binding, issue = self._bind(PBAS_GROUP, S6)
        self.assertIsNotNone(binding, issue)
        self.assertEqual(binding.locator, "pdf:p4:b6-p4:b6")

    def test_s5_not_in_binary_group_scope(self) -> None:
        # The sulfur-composite template lives in the PBA-S group; the binary
        # group's scope may not vouch for it.
        binding, issue = self._bind("Synthesis of binary PBAs", S5)
        self.assertIsNone(binding)
        self.assertEqual(issue, "fact_excerpt_not_in_block")


class FurnitureHeadingUnitTest(unittest.TestCase):
    """Synthetic block lists: running-head furniture vs mid-page repeats."""

    def _blocks(self):
        # Two pages, each starting with a large bold running head, then a
        # section heading (11pt) on p1 and group content spanning the seam.
        return [
            _PdfBlock(1, 1, "SUPPORTING INFORMATION", 16.0, True),
            _PdfBlock(1, 2, "Experimental Section", 11.0, True),
            _PdfBlock(1, 3, "Synthesis of X", 9.0, True),
            _PdfBlock(1, 4, "Body text for the group.", 9.0, False),
            _PdfBlock(2, 1, "SUPPORTING INFORMATION", 16.0, True),
            _PdfBlock(2, 2, "More body of the same group.", 9.0, False),
            _PdfBlock(2, 3, "Synthesis of Y", 9.0, True),
            _PdfBlock(2, 4, "Second group body.", 9.0, False),
            _PdfBlock(2, 5, "Results", 11.0, True),
            _PdfBlock(2, 6, "Results body.", 9.0, False),
        ]

    def test_furniture_detection_narrow(self) -> None:
        blocks = self._blocks()
        furniture = _furniture_heading_texts(blocks)
        self.assertIn("supporting information", furniture)
        self.assertNotIn("experimental section", furniture)

    def test_running_head_does_not_close_group(self) -> None:
        blocks = self._blocks()
        span = _group_range(blocks, "Experimental Section", "Synthesis of X")
        self.assertEqual(span, (2, 5))  # crosses the p2 running head

    def test_midpage_repeated_heading_still_boundary(self) -> None:
        # 'authors' at per-page numbers 12 and 55 (A01 shape) must stay a
        # boundary candidate: narrow furniture requires top-of-page (num<=2).
        blocks = [
            _PdfBlock(1, 1, "Experimental Section", 11.0, True),
            _PdfBlock(1, 2, "Group A", 9.0, True),
            _PdfBlock(1, 3, "body", 9.0, False),
            _PdfBlock(1, 12, "Authors", 11.0, True),
            _PdfBlock(2, 55, "Authors", 11.0, True),
        ]
        furniture = _furniture_heading_texts(blocks)
        self.assertNotIn("authors", furniture)


class NumberedSectionHeadingTest(unittest.TestCase):
    def test_numbered_section_accepted(self) -> None:
        self.assertTrue(
            _experimental_section_heading("Section S1 Experimental Procedures"))
        self.assertTrue(
            _experimental_section_heading("Section 2. Experimental Section"))

    def test_plain_base_phrases_unchanged(self) -> None:
        self.assertTrue(_experimental_section_heading("Experimental Section"))
        self.assertTrue(_experimental_section_heading("Materials and Methods"))
        self.assertFalse(_experimental_section_heading("Results and Discussion"))

    def test_toc_lookalikes_rejected(self) -> None:
        self.assertFalse(_experimental_section_heading(
            "Section S1 Experimental procedures.……….3"))
        self.assertFalse(_experimental_section_heading(
            "Experimental Section ...... 12"))
        self.assertFalse(_experimental_section_heading(
            "Section S1 Experimental Procedures.... 3"))

    def test_prefix_without_base_phrase_rejected(self) -> None:
        self.assertFalse(_experimental_section_heading("Section S1"))
        self.assertFalse(_experimental_section_heading("Section S2 Results"))


class PdfBudgetConsistencyTest(unittest.TestCase):
    MIB8 = 8 * 1024 * 1024
    MIB16 = 16 * 1024 * 1024

    def test_pdf_entry_constants_unified(self) -> None:
        self.assertEqual(_MAX_PDF_SOURCE_BYTES, self.MIB16)
        self.assertEqual(route_attestation._MAX_PDF_BYTES, self.MIB16)
        self.assertEqual(route_receipt_producer._MAX_PDF_BYTES, self.MIB16)

    def test_non_pdf_budgets_unchanged(self) -> None:
        # JSON candidate-supply register and text (.txt/.md) sources keep 8 MiB.
        self.assertEqual(_MAX_SOURCE_BYTES, self.MIB8)
        self.assertEqual(route_source._MAX_SOURCE_BYTES, self.MIB8)

    @unittest.skipUnless(DU_SI.is_file(), "G3 Du SI fixture not ingested")
    def test_du_si_between_old_and_new_budget_enumerates(self) -> None:
        size = DU_SI.stat().st_size
        self.assertGreater(size, self.MIB8)
        self.assertLess(size, self.MIB16)
        result = _enumerate(DU_SI, "du-2022-si")
        self.assertEqual(result.diagnostics, [])
        self.assertEqual(len(result.groups), 12)

    def test_over_16mib_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big = root / "big.pdf"
            with big.open("wb") as handle:
                handle.write(b"%PDF-1.7\n")
                handle.truncate(self.MIB16 + 1)
            result = _enumerate(big, "big")
            self.assertEqual(len(result.groups), 0)
            reasons = [d.reason_code for d in result.diagnostics]
            self.assertEqual(reasons, ["pdf_source_too_large"])

    def test_pipeline_matching_suffix_split_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "paper.pdf"
            txt = root / "notes.txt"
            payload = b"x" * (self.MIB8 + 1)  # between 8 and 16 MiB
            pdf.write_bytes(b"%PDF-1.7\n" + payload)
            txt.write_bytes(b"x" * (self.MIB8 + 1))

            def candidate_for(path: Path) -> RouteCandidateV1:
                digest = "sha256_" + hashlib.sha256(path.read_bytes()).hexdigest()
                return RouteCandidateV1(
                    route_id="r",
                    target=RouteTargetV1(
                        material="m", desired_state="solid", objective="o"),
                    route_signature=RouteSignatureV1(
                        route_family="f", target_transformation="t",
                        endpoint_state="solid"),
                    origin="paper_experimental_group",
                    source_scope=ExperimentalGroupScopeV1(
                        paper_id="p", experimental_group_id="g",
                        section="s", locator="pdf:p1:b1-p1:b1",
                        source_digest=digest,
                    ),
                )

            pdf_hit = _matching_trusted_source(
                candidate_for(pdf), {"p": [pdf]}, root)
            self.assertEqual(pdf_hit, pdf)
            txt_hit = _matching_trusted_source(
                candidate_for(txt), {"p": [txt]}, root)
            self.assertIsNone(txt_hit)  # text budget stays 8 MiB


@unittest.skipUnless(A01_PDF.is_file(), "A01 PDF fixture missing")
class A01RegressionTest(unittest.TestCase):
    def test_a01_eight_groups_unchanged(self) -> None:
        expected = {
            "Materials.": "pdf:p3:b44-p3:b53",
            "Synthesis of the Pristine Ni3Fe LDHs (NiFe Control).":
                "pdf:p3:b54-p3:b76",
            "Synthesis of the LDHs by an Etching Method of NiFe; Ey "
            "(y = 1−10).": "pdf:p3:b77-p3:b86",
            "Synthesis of the LDHs by an Etching-and-Recrystallization (ER) "
            "Method of NiFe; ERy (y = 0−10).": "pdf:p3:b87-p3:b99",
            "Characterization.": "pdf:p3:b100-p3:b134",
            "Electrode Preparation.": "pdf:p3:b135-p4:b21",
            "Electrochemical Characterization.": "pdf:p4:b22-p4:b46",
            "Calculation Methods.": "pdf:p4:b47-p5:b96",
        }
        result = _enumerate(A01_PDF, "a01")
        self.assertEqual(result.diagnostics, [])
        actual = {
            g.source_scope.experimental_group_id: g.source_scope.locator
            for g in result.groups
        }
        self.assertEqual(actual, expected)


@unittest.skipUnless(WU_SI.is_file(), "Wu SI fixture missing")
class WuRegressionTest(unittest.TestCase):
    def test_wu_si_still_abstains(self) -> None:
        result = _enumerate(WU_SI, "wu2025-si")
        self.assertEqual(len(result.groups), 0)
        reasons = [d.reason_code for d in result.diagnostics]
        self.assertEqual(reasons, ["experimental_section_missing"])


if __name__ == "__main__":
    unittest.main()
