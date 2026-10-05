"""G2a folio-recognition tests: six-factor validation and abstention detail.

A folio (printed page number such as "S 75") is page furniture only when all
six factors hold together: a standalone folio-shaped short line in the
bottom band, a consistent vertical position across pages, a number that
advances in lockstep with the page order, a consistent horizontal anchor,
body isolation inside its block (the candidate is the only non-empty line of
its original PDF block), and body isolation across the block neighborhood
(no horizontally overlapping line stands within body line spacing of it).
Single-factor lookalikes (chart ticks, table totals, one-page numbers,
measured values sitting under or next to their labels) must survive, and
genuine layout ambiguity must still abstain -- now with structured culprit
detail.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reaserch_agent.route_pdf_groups import (
    PDF_GROUP_PARSER_VERSION,
    enumerate_pdf_experimental_groups,
)
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


_PRER15_COMMIT = "c429443"
_PRER17_COMMIT = "491ec37"


class HistoricalSourceUnavailable(Exception):
    """The skip channel: git itself or the pinned commit is genuinely
    unavailable.  Import/execution errors of a fetched historical module do
    NOT use this channel -- they raise (and fail the test) with the original
    exception chained."""


def _load_historical_source_module(commit: str):
    """Load route_pdf_source as of a commit for old/new comparison.

    Raises HistoricalSourceUnavailable only for the two genuine causes --
    the git executable cannot be run (OSError/SubprocessError) or ``git
    show`` returns non-zero (commit/object missing) -- with distinct
    messages.  Any import/execution error of the fetched source raises
    RuntimeError chaining the original exception, so a broken historical
    module FAILS the test instead of being disguised as a git problem.

    The git subprocess receives an explicit ``env=dict(os.environ)`` copy:
    never env=None.  On Windows, env=None makes the child inherit the Win32
    kernel environment block, which a ``mock.patch.dict(os.environ)``
    round-trip anywhere earlier in the same test process silently strips of
    empty-valued variables (SetEnvironmentVariableW semantics delete
    ``NAME=`` entries) -- e.g. a shell-injected empty GIT_CONFIG_VALUE_0,
    after which git exits rc=128 ("missing config value
    GIT_CONFIG_VALUE_0") and the old code wrongly skipped (r18 diagnosis a).
    Re-serialising os.environ restores the empty value for the child.

    The module name carries the package prefix so its relative imports work.
    """
    try:
        proc = subprocess.run(
            ["git", "show",
             f"{commit}:reaserch_agent/route_pdf_source.py"],
            cwd=_REPO, capture_output=True, text=True, timeout=60,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HistoricalSourceUnavailable(
            f"git executable unavailable: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise HistoricalSourceUnavailable(
            f"git show failed for commit {commit} "
            f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}")
    temporary = tempfile.TemporaryDirectory()
    path = Path(temporary.name) / f"_route_pdf_source_{commit}.py"
    path.write_text(proc.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        f"reaserch_agent._route_pdf_source_{commit}", path)
    if spec is None or spec.loader is None:
        temporary.cleanup()
        raise RuntimeError(
            f"could not build an import spec for historical "
            f"route_pdf_source @ {commit}")
    module = importlib.util.module_from_spec(spec)
    try:
        # Register before exec: the module's dataclass processing resolves
        # cls.__module__ through sys.modules.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        temporary.cleanup()
        raise RuntimeError(
            f"historical route_pdf_source @ {commit} failed to "
            f"import/execute") from exc
    return module, temporary


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class MisdeleteRegressionTest(unittest.TestCase):
    """r16: the folio recognizer must not delete experiment values.

    Probes A/B reproduce the two reported misdelete shapes on 612x792 pages;
    each new factor is the load-bearing one in its probe.  A control guards
    against over-correction, and an old/new comparison pins the empirical
    basis for the parser-version bump.
    """

    def setUp(self) -> None:
        import fitz

        self._fitz = fitz

    def _probe_a(self) -> bytes:
        # "Final pH:" label at (70,730); bare values 11/12/13 at baseline 747
        # with x drifting 70/280/460 across the three pages.
        document = self._fitz.open()
        for index, x in enumerate((70, 280, 460)):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), "The sample was measured carefully.",
                             fontsize=10)
            page.insert_text((70, 730), "Final pH:", fontsize=10)
            page.insert_text((x, 747), str(11 + index), fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    def _probe_b(self) -> bytes:
        # One insert_text writes "Final pH:\n<value>": label and value share
        # the same original block; x is consistent across pages.
        document = self._fitz.open()
        for index in range(3):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), "The sample was measured carefully.",
                             fontsize=10)
            page.insert_text((280, 734), f"Final pH:\n{11 + index}",
                             fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    def _probe_control(self) -> bytes:
        # Probe A shape with a consistent x and no label nearby.
        document = self._fitz.open()
        for index in range(3):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), "The sample was measured carefully.",
                             fontsize=10)
            page.insert_text((280, 747), str(11 + index), fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    def _probe_c(self) -> bytes:
        # "Final pH:" label at (280,729) and the bare value at (280,747) sit
        # in two adjacent original blocks sharing one x; the glyph-box gap
        # is ~4.26 pt at fontsize 10.  Every r16 factor passes for the value
        # line -- only the neighborhood body-text check intercepts.
        document = self._fitz.open()
        for index in range(3):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), "The sample was measured carefully.",
                             fontsize=10)
            page.insert_text((280, 729), "Final pH:", fontsize=10)
            page.insert_text((280, 747), str(11 + index), fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    def test_cross_page_x_drift_keeps_values(self) -> None:
        # Probe A: every other factor passes (bottom band, exactly one
        # candidate per page, quorum 3, uniform empty prefix, lockstep
        # 11->12->13, identical y0, sole non-empty line of its block); the
        # horizontal anchor alone intercepts.
        blocks, issue, report = _read_pdf_blocks_with_report(self._probe_a())
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 9)
        for value in ("11", "12", "13"):
            self.assertIn(value, texts)

    def test_shared_original_block_keeps_values(self) -> None:
        # Probe B: x centers agree across pages; the label line shares the
        # value's original block, so the isolation factor alone intercepts.
        blocks, issue, report = _read_pdf_blocks_with_report(self._probe_b())
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 9)
        for value in ("11", "12", "13"):
            self.assertIn(value, texts)

    def test_x_consistent_numeric_set_still_stripped(self) -> None:
        # Control against over-correction: the probe-A shape with a
        # consistent x anchor validates and strips.
        blocks, issue, report = _read_pdf_blocks_with_report(
            self._probe_control())
        self.assertIsNone(issue)
        self.assertEqual(
            [item["text"] for item in report["folios_stripped"]],
            ["11", "12", "13"],
        )
        self.assertEqual(
            [block.text for block in blocks],
            ["The sample was measured carefully."] * 3,
        )

    def test_prer15_block_semantics_comparison(self) -> None:
        # Empirical basis for the version bumps: the pre-r15 parser keeps
        # folio-shaped bottom-band lines that the current parser strips, so
        # previously parseable folio-bearing inputs produce shorter block
        # sequences (v2); the pre-r15 parser also strips label-adjacent
        # bottom-band values that the current parser keeps, so those inputs
        # produce longer block sequences (v3); folio-free inputs and the
        # probe A/B shapes are unchanged.
        try:
            loaded = _load_historical_source_module(_PRER15_COMMIT)
        except HistoricalSourceUnavailable as exc:
            self.skipTest(str(exc))
        module, temporary = loaded
        self.addCleanup(temporary.cleanup)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        fitz = self._fitz

        def build(pages: list[list[str]]) -> bytes:
            document = fitz.open()
            for index, folios in enumerate(pages):
                page = document.new_page(width=612, height=792)
                page.insert_text((70, 400), f"Page {index} body text.",
                                 fontsize=10)
                if folios:
                    folio = folios[0]
                    width = fitz.get_text_length(folio, fontsize=10)
                    page.insert_text((306 - width / 2, 747), folio,
                                     fontsize=10)
            raw = document.tobytes()
            document.close()
            return raw

        cases = {
            # name: (raw, old_block_count, new_block_count)
            "bare_digits_centered": (
                build([[str(i)] for i in range(1, 5)]), 8, 4),
            "s_prefix_centered": (
                build([[f"S {i}"] for i in range(1, 5)]), 8, 4),
            "no_folio": (build([[] for _ in range(4)]), 4, 4),
            "probe_a_shape": (self._probe_a(), 9, 9),
            "probe_b_shape": (self._probe_b(), 9, 9),
            # The pre-r15 parser pre-dates folio stripping entirely, so it
            # keeps the label-adjacent values exactly like the r17 parser
            # does; the misdelete existed only in the r15/r16 window.
            "probe_c_shape": (self._probe_c(), 9, 9),
        }
        for name, (raw, old_count, new_count) in cases.items():
            old_blocks, old_issue = module._read_pdf_blocks(raw)
            new_blocks, new_issue = _read_pdf_blocks(raw)
            self.assertIsNone(old_issue, name)
            self.assertIsNone(new_issue, name)
            old_texts = [block.text for block in old_blocks]
            new_texts = [block.text for block in new_blocks]
            self.assertEqual(len(old_texts), old_count, name)
            self.assertEqual(len(new_texts), new_count, name)
            if old_count == new_count:
                self.assertEqual(old_texts, new_texts, name)
            else:
                self.assertNotEqual(old_texts, new_texts, name)
                # The shorter sequence is the longer one minus the lines the
                # two parsers treat differently (stripped folios or kept
                # label-adjacent values), in order.
                shorter, longer = (
                    (new_texts, old_texts)
                    if len(new_texts) < len(old_texts)
                    else (old_texts, new_texts)
                )
                iterator = iter(longer)
                self.assertTrue(
                    all(text in iterator for text in shorter), name)

    def test_parser_version_bumped_to_v3(self) -> None:
        # Block semantics changed again for previously parseable inputs at
        # r17: bottom-band values adjacent to their labels were stripped
        # under v2 and are kept under v3 (pinned by the r17 replay's
        # old/new comparison against the 491ec37 parser), so the enumerator
        # version had to move.  The r13/r13b archives pin no version string
        # (grep-verified), so the bump falsifies no pinned replay bytes.
        self.assertEqual(PDF_GROUP_PARSER_VERSION, "route_pdf_groups/v3")


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF unavailable")
class NeighborhoodBodyRegressionTest(unittest.TestCase):
    """r17: body-adjacent bottom-band values are kept; isolated folios strip.

    Probe C shape: the label and its value are two adjacent original blocks
    at the same x with a ~4.26 pt glyph-box gap at fontsize 10.  All five
    r16 factors pass for the value line, so the neighborhood body-text check
    (factor 6) is the load-bearing one.  Its proximity scale is derived from
    the candidate's own font metrics (one standard line height, 1.2 em) --
    no probe text, coordinate, or page position is consulted.
    """

    def setUp(self) -> None:
        import fitz

        self._fitz = fitz

    def _label_value_pages(self, label_baseline: float = 729.0,
                           value_baseline: float = 747.0,
                           label_x: float = 280.0, value_x: float = 280.0,
                           ) -> bytes:
        document = self._fitz.open()
        for index in range(3):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), "The sample was measured carefully.",
                             fontsize=10)
            page.insert_text((label_x, label_baseline), "Final pH:",
                             fontsize=10)
            page.insert_text((value_x, value_baseline), str(11 + index),
                             fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    def test_adjacent_block_label_keeps_values(self) -> None:
        # Probe C: label and value in adjacent original blocks, same x,
        # glyph-box gap ~4.26 pt -- the neighborhood check alone intercepts.
        blocks, issue, report = _read_pdf_blocks_with_report(
            self._label_value_pages())
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 9)
        for value in ("11", "12", "13"):
            self.assertIn(value, texts)

    def test_label_below_value_keeps_values(self) -> None:
        # The neighborhood check is bidirectional: a label sitting just
        # below the value line also makes it body text.  (Both baselines
        # stay above the 0.94h margin zone, so the legacy margin rule does
        # not interfere.)
        blocks, issue, report = _read_pdf_blocks_with_report(
            self._label_value_pages(label_baseline=747.0,
                                    value_baseline=729.0))
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 9)
        for value in ("11", "12", "13"):
            self.assertIn(value, texts)

    def test_gap_just_below_line_scale_keeps_values(self) -> None:
        # Boundary case below the threshold: a 25 pt baseline delta at
        # fontsize 10 gives a glyph-box gap of ~11.3 pt, just under the
        # 1.2 em scale (12.0 pt); the values are still body-adjacent.
        blocks, issue, report = _read_pdf_blocks_with_report(
            self._label_value_pages(label_baseline=722.0))
        self.assertIsNone(issue)
        self.assertEqual(report["folios_stripped"], [])
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 9)
        for value in ("11", "12", "13"):
            self.assertIn(value, texts)

    def test_gap_just_above_line_scale_strips(self) -> None:
        # Boundary case above the threshold: a 26 pt baseline delta gives a
        # glyph-box gap of ~12.3 pt, just over the 1.2 em scale; the values
        # validate as folios again (anti full-rejection guard).
        blocks, issue, report = _read_pdf_blocks_with_report(
            self._label_value_pages(label_baseline=721.0))
        self.assertIsNone(issue)
        self.assertEqual(
            [item["text"] for item in report["folios_stripped"]],
            ["11", "12", "13"],
        )
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 6)
        self.assertIn("Final pH:", texts)

    def test_non_overlapping_label_does_not_block(self) -> None:
        # Specificity guard: a label close above the value but horizontally
        # disjoint from it (x=70 label vs x=280 value) is not a neighbor;
        # the values still validate as folios.
        blocks, issue, report = _read_pdf_blocks_with_report(
            self._label_value_pages(label_x=70.0))
        self.assertIsNone(issue)
        self.assertEqual(
            [item["text"] for item in report["folios_stripped"]],
            ["11", "12", "13"],
        )
        self.assertEqual(len([block.text for block in blocks]), 6)

    def test_real_folio_spacing_still_stripped(self) -> None:
        # Positive control in the Wu SI's own geometry: a size-12 text line
        # stands ~16.7 pt above the centered size-10 folio (the real SI's
        # minimum measured gap is 14.16 pt); the folios strip.
        fitz = self._fitz
        document = fitz.open()
        for index in range(3):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), "The sample was measured carefully.",
                             fontsize=10)
            page.insert_text((240, 716), "Electrolysis reference text.",
                             fontsize=12)
            folio = f"S {11 + index}"
            width = fitz.get_text_length(folio, fontsize=10)
            page.insert_text((306 - width / 2, 747), folio, fontsize=10)
        raw = document.tobytes()
        document.close()
        blocks, issue, report = _read_pdf_blocks_with_report(raw)
        self.assertIsNone(issue)
        self.assertEqual(
            [item["text"] for item in report["folios_stripped"]],
            ["S 11", "S 12", "S 13"],
        )
        texts = [block.text for block in blocks]
        self.assertEqual(len(texts), 6)
        self.assertIn("Electrolysis reference text.", texts)

    def test_prer17_block_semantics_comparison(self) -> None:
        # Empirical basis for the v3 bump: the r16 parser (git show 491ec37)
        # strips the label-adjacent values that the current parser keeps, so
        # previously parseable probe-C-shaped inputs produce longer block
        # sequences now; isolated-folio inputs are unchanged under both.
        try:
            loaded = _load_historical_source_module(_PRER17_COMMIT)
        except HistoricalSourceUnavailable as exc:
            self.skipTest(str(exc))
        module, temporary = loaded
        self.addCleanup(temporary.cleanup)
        self.addCleanup(sys.modules.pop, module.__name__, None)

        cases = {
            # name: (raw, prer17_block_count, current_block_count)
            "probe_c_shape": (self._label_value_pages(), 6, 9),
            "label_below_value": (
                self._label_value_pages(label_baseline=747.0,
                                        value_baseline=729.0), 6, 9),
            "gap_just_below_scale": (
                self._label_value_pages(label_baseline=722.0), 6, 9),
            "gap_just_above_scale": (
                self._label_value_pages(label_baseline=721.0), 6, 6),
            "non_overlapping_label": (
                self._label_value_pages(label_x=70.0), 6, 6),
        }
        for name, (raw, old_count, new_count) in cases.items():
            old_blocks, old_issue = module._read_pdf_blocks(raw)
            new_blocks, new_issue = _read_pdf_blocks(raw)
            self.assertIsNone(old_issue, name)
            self.assertIsNone(new_issue, name)
            old_texts = [block.text for block in old_blocks]
            new_texts = [block.text for block in new_blocks]
            self.assertEqual(len(old_texts), old_count, name)
            self.assertEqual(len(new_texts), new_count, name)
            if old_count == new_count:
                self.assertEqual(old_texts, new_texts, name)
            else:
                # The r16 sequence is the current one minus the kept values.
                iterator = iter(new_texts)
                self.assertTrue(
                    all(text in iterator for text in old_texts), name)


if __name__ == "__main__":
    unittest.main()
