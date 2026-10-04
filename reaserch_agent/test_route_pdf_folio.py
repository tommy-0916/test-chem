"""G2a folio-recognition tests: three-factor validation and abstention detail.

A folio (printed page number such as "S 75") is page furniture only when all
three factors hold together: a standalone folio-shaped short line in the
bottom band, a consistent vertical position across pages, and a number that
advances in lockstep with the page order.  Single-factor lookalikes (chart
ticks, table totals, one-page numbers) must survive, and genuine layout
ambiguity must still abstain -- now with structured culprit detail.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups
from reaserch_agent.route_pdf_source import (
    _read_pdf_blocks,
    _read_pdf_blocks_with_report,
)

_REPO = Path(__file__).resolve().parent.parent
_WU_MAIN = _REPO / "backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-main.pdf"
_WU_SI = _REPO / "backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-SI.pdf"
_A01 = _REPO / "result/a01-evidence-audit-20260927/huang-2023-institutional-copy.pdf"

# Default PyMuPDF page: 595.276 x 841.89 pt; the bottom band begins at 0.90h
# (757.7) and the historical margin zone at 0.94h (791.4).
_MIDDLE = 595.276 / 2
_FOLIO_BASELINE = 770.0   # bbox y0 ~ 761.9 -> 0.905h: inside the band, below 0.94h
_LEGACY_BASELINE = 805.0  # bbox y0 ~ 796.9 -> 0.946h: inside the margin zone


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class ValidatedFolioRecognitionTest(unittest.TestCase):
    def setUp(self) -> None:
        import fitz

        self._fitz = fitz

    def _centered(self, text: str, baseline: float, fontsize: int = 9):
        width = self._fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
        return (_MIDDLE - width / 2, baseline, text, fontsize, "helv")

    def _single_column_page(self, page_index: int, folio: str | None = "S"):
        lines = []
        for row in range(10):
            lines.append((
                50, 80 + row * 30,
                f"Page {page_index} observation {row} recorded.", 10, "helv",
            ))
        if folio is not None:
            label = f"{folio} {page_index}" if folio else str(page_index)
            lines.append(self._centered(label, _FOLIO_BASELINE))
        return lines

    def _two_column_page(self, page_index: int, folio: str | None = "S",
                         extra: list | None = None):
        lines = []
        for row in range(10):
            lines.append((
                50, 80 + row * 30,
                f"Page {page_index} observation {row} recorded.", 10, "helv",
            ))
            lines.append((
                320, 80 + row * 30,
                f"Page {page_index} measurement {row} noted.", 10, "helv",
            ))
        if folio is not None:
            label = f"{folio} {page_index}" if folio else str(page_index)
            lines.append(self._centered(label, _FOLIO_BASELINE))
        if extra:
            lines.extend(extra)
        return lines

    def _parse(self, pages: list[list]):
        document = self._fitz.open()
        for lines in pages:
            page = document.new_page()
            for x, y, text, size, font in lines:
                page.insert_text(self._fitz.Point(x, y), text, fontsize=size, fontname=font)
        raw = document.tobytes()
        document.close()
        return _read_pdf_blocks_with_report(raw)

    def test_consistent_si_folios_are_stripped(self) -> None:
        pages = [self._single_column_page(index) for index in range(1, 5)]
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        texts = [block.text for block in blocks]
        for index in range(1, 5):
            self.assertNotIn(f"S {index}", texts)
        self.assertIn("Page 3 observation 4 recorded.", texts)
        self.assertEqual(len(report["folios_stripped"]), 4)
        stripped = report["folios_stripped"][2]
        self.assertEqual(
            set(stripped),
            {"page", "text", "x0", "x1", "y0", "page_width"},
        )
        self.assertEqual(stripped["page"], 3)
        self.assertEqual(stripped["text"], "S 3")

    def test_bare_digit_folios_below_margin_zone_are_stripped(self) -> None:
        # One-digit folios at 0.905h are outside the historical fast path
        # (which needs >0.94h and 3+ digits); the three-factor rule covers them.
        pages = [self._single_column_page(index, folio="") for index in range(1, 5)]
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        texts = [block.text for block in blocks]
        for index in range(1, 5):
            self.assertNotIn(str(index), texts)
        self.assertEqual(len(report["folios_stripped"]), 4)

    def test_page_order_jump_breaks_validation(self) -> None:
        pages = [self._single_column_page(1), self._single_column_page(2),
                 self._single_column_page(9), self._single_column_page(4)]
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertIn("S 9", texts)
        self.assertIn("S 1", texts)

    def test_mixed_prefix_breaks_validation(self) -> None:
        pages = [self._single_column_page(1), self._single_column_page(2),
                 self._single_column_page(3, folio=""), self._single_column_page(4)]
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertIn("S 2", texts)
        self.assertIn("3", texts)

    def test_folio_requires_three_page_quorum(self) -> None:
        pages = [self._single_column_page(1), self._single_column_page(2)]
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        self.assertIn("S 1", [block.text for block in blocks])

    def test_page_with_two_candidates_contributes_none(self) -> None:
        pages = [self._single_column_page(index) for index in range(1, 5)]
        pages[2] = pages[2][:-1]  # replace the single folio on page 3
        pages[2].append(self._centered("S 3", _FOLIO_BASELINE))
        pages[2].append((60, _FOLIO_BASELINE, "3", 9, "helv"))
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        texts = [block.text for block in blocks]
        self.assertIn("S 3", texts)
        self.assertIn("3", texts)
        self.assertNotIn("S 1", texts)
        self.assertNotIn("S 4", texts)
        self.assertEqual(len(report["folios_stripped"]), 3)

    def test_single_page_lookalike_is_kept(self) -> None:
        pages = [self._single_column_page(index, folio=None) for index in range(1, 4)]
        pages[1].append((60, _FOLIO_BASELINE, "36", 9, "helv"))
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        self.assertIn("36", [block.text for block in blocks])

    def test_midpage_numeric_body_line_survives(self) -> None:
        pages = [self._single_column_page(index) for index in range(1, 4)]
        pages[1].append((120, 400, "36", 10, "helv"))
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        self.assertEqual(len(report["folios_stripped"]), 3)
        self.assertIn("36", [block.text for block in blocks])

    def test_legacy_margin_folio_fast_path_unchanged(self) -> None:
        # One single page, one "104" at 0.946h: the historical rule (bottom
        # margin + 3+ digits) strips it without any cross-page validation.
        lines = [(
            50, 80 + row * 30, f"Observation {row} recorded.", 10, "helv",
        ) for row in range(10)]
        lines.append(self._centered("104", _LEGACY_BASELINE))
        blocks, issue, report = self._parse([lines])
        self.assertIsNone(issue)
        self.assertNotIn("104", [block.text for block in blocks])
        # The three-factor rule did not fire (quorum unmet); the legacy
        # margin rule did the stripping and is not reported as a new folio.
        self.assertEqual(report["folios_stripped"], [])

    def test_centered_folio_unblocks_two_column_page(self) -> None:
        # The folio is the only center-middle line (the Wu SI p75-77 shape):
        # stripping it lets the page order its columns.
        pages = [self._two_column_page(index) for index in range(1, 4)]
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(issue)
        self.assertEqual(len(report["folios_stripped"]), 3)
        self.assertIn("Page 2 measurement 3 noted.", [block.text for block in blocks])

    def test_genuine_centered_content_still_abstains(self) -> None:
        pages = [self._two_column_page(index) for index in range(1, 4)]
        pages[1].append(self._centered("Calibration standard 36", 500.0, 10))
        blocks, issue, report = self._parse(pages)
        self.assertIsNone(blocks)
        self.assertEqual(issue, "pdf_column_layout_ambiguous")
        abstention = report["abstention"]
        self.assertEqual(abstention["reason"], "pdf_column_layout_ambiguous")
        self.assertEqual(len(abstention["culprits"]), 1)
        culprit = abstention["culprits"][0]
        self.assertEqual(culprit["page"], 2)
        self.assertEqual(culprit["text"], "Calibration standard 36")
        self.assertEqual(
            set(culprit), {"page", "text", "x0", "x1", "y0", "page_width"},
        )

    def test_enumeration_diagnostic_carries_structured_detail(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        path = root / "ambiguous.pdf"
        document = self._fitz.open()
        page = document.new_page()
        for x, y, text, size, font in self._two_column_page(1, folio=None):
            page.insert_text(self._fitz.Point(x, y), text, fontsize=size, fontname=font)
        tick = self._centered("36", 648.0, 10)
        page.insert_text(self._fitz.Point(tick[0], tick[1]), tick[2],
                         fontsize=tick[3], fontname=tick[4])
        document.save(str(path))
        document.close()
        result = enumerate_pdf_experimental_groups({"paper-1": path}, source_root=root)
        self.assertEqual(result.groups, [])
        self.assertEqual(
            [item.reason_code for item in result.diagnostics],
            ["pdf_column_layout_ambiguous"],
        )
        detail = json.loads(result.diagnostics[0].detail)
        self.assertEqual(detail["reason"], "pdf_column_layout_ambiguous")
        culprit = detail["culprits"][0]
        self.assertEqual(culprit["page"], 1)
        self.assertEqual(culprit["text"], "36")
        self.assertEqual(
            set(culprit), {"page", "text", "x0", "x1", "y0", "page_width"},
        )
        self.assertGreater(culprit["page_width"], 0)


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class RealPdfFolioAnchorTest(unittest.TestCase):
    def test_wu_main_still_abstains_on_chart_tick(self) -> None:
        if not _WU_MAIN.is_file():
            self.skipTest("Wu-2025 main PDF unavailable")
        raw = _WU_MAIN.read_bytes()
        blocks, issue, report = _read_pdf_blocks_with_report(raw)
        self.assertIsNone(blocks)
        self.assertEqual(issue, "pdf_column_layout_ambiguous")
        culprit = report["abstention"]["culprits"][0]
        self.assertEqual(culprit["page"], 10)
        self.assertEqual(culprit["text"], "36")
        # The chart tick sits at 0.819h: inside no folio band, never a
        # candidate, and still the exact abstention trigger.
        self.assertAlmostEqual(culprit["y0"], 648.0, places=1)

    def test_wu_si_parses_and_s14_sentence_binds(self) -> None:
        if not _WU_SI.is_file():
            self.skipTest("Wu-2025 SI PDF unavailable")
        from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
        from reaserch_agent.route_pdf_verification_context import (
            MAX_VERIFICATION_CONTEXT_BLOCKS,
        )

        raw = _WU_SI.read_bytes()
        blocks, issue, report = _read_pdf_blocks_with_report(raw)
        self.assertIsNone(issue)
        # Observed parser output for this document at this round.
        self.assertEqual(len(blocks), 1041)
        self.assertEqual(len(report["folios_stripped"]), 79)
        pairs = [
            (f"pdf:p{block.page}:b{block.number}-p{block.page}:b{block.number}", block.text)
            for block in blocks
        ]
        captions = {
            locator for locator, block in zip([item[0] for item in pairs], blocks)
            if block.caption
        }
        short = ("the precipitated Ni-Mo-O powder was collected, dried, "
                 "and then reduced")
        full = ("After the hydrothermal reaction, the precipitated Ni-Mo-O "
                "powder was collected, dried, and then reduced at 500 °C "
                "for 6 h under a mixed hydrogen-nitrogen flow to obtain "
                "Ni-Mo nanoparticles.")
        for needle in (short, full):
            binding, quote_issue = bind_pdf_quote(
                pairs, needle, caption_block_locators=captions,
                max_quote_blocks=MAX_VERIFICATION_CONTEXT_BLOCKS,
            )
            self.assertEqual(quote_issue, "")
            self.assertIsNotNone(binding)

    def test_a01_parse_unchanged(self) -> None:
        if not _A01.is_file():
            self.skipTest("A01 control PDF unavailable")
        raw = _A01.read_bytes()
        blocks, issue, report = _read_pdf_blocks_with_report(raw)
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        canon = json.dumps(
            [(b.page, b.number, b.text, b.font_size, b.bold, b.caption) for b in blocks],
            ensure_ascii=False, sort_keys=True,
        )
        self.assertEqual(len(blocks), 1289)
        self.assertEqual(
            hashlib.sha256(canon.encode()).hexdigest(),
            "911a6a29246b93cd6138bd63fcc8c2b0393de61c6ada7748ae1066b08d06052f",
        )


if __name__ == "__main__":
    unittest.main()
