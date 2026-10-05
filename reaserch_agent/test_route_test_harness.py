"""Tests for reaserch_agent.route_test_harness (round 18).

Covers the strict subprocess-judgement contract with the three r17
negative shapes supplied by acceptance, a positive case, defence-in-depth
cases, and the folio historical-loader's revised skip/failure semantics
(import/exec errors must FAIL with the original error chained; only genuine
git/commit absence may skip, with the two causes distinguished).

No model is called; git is invoked only in the real-git round-trip test,
which skips (with the distinguished reason) when git or the pinned commit is
genuinely unavailable.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from reaserch_agent.route_test_harness import (
    RunExpectation,
    audit_record,
    collect_unittest_skips,
    judge_unittest_run,
)

_REPO = Path(__file__).resolve().parents[1]


class JudgeContractTest(unittest.TestCase):
    """The r18 acceptance gate: all criteria must hold together."""

    def test_positive_clean_run_accepted(self) -> None:
        stderr = ".....\n----------------------------------------------------------------------\nRan 5 tests in 0.042s\n\nOK\n"
        verdict = judge_unittest_run(
            0, "", stderr, RunExpectation(test_count=5, require_no_skips=True))
        self.assertTrue(verdict.accepted, verdict.reasons)
        self.assertEqual(verdict.reasons, ())
        self.assertEqual(verdict.ran_count, 5)
        self.assertEqual(verdict.verdict_line, "OK")

    def test_negative_nonzero_returncode_with_ok_rejected(self) -> None:
        # Acceptance negative (a): returncode != 0 but the output contains
        # "Ran 346 tests" and "OK" -- the r17 substring rule accepted this.
        stderr = ("........................................\n"
                  "----------------------------------------------------------------------\n"
                  "Ran 346 tests in 11.319s\n\nOK\n")
        verdict = judge_unittest_run(
            1, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertIn("nonzero_returncode:1", verdict.reasons)

    def test_negative_ok_with_skips_rejected(self) -> None:
        # Acceptance negative (b): "OK (skipped=2)" on a must-run suite --
        # the r17 substring rule accepted this ("OK" is a substring).
        stderr = ("...................\n"
                  "----------------------------------------------------------------------\n"
                  "Ran 27 tests in 1.730s\n\nOK (skipped=2)\n")
        verdict = judge_unittest_run(
            0, "", stderr,
            RunExpectation(test_count=27, require_no_skips=True))
        self.assertFalse(verdict.accepted)
        self.assertIn("unexpected_skips:2", verdict.reasons)

    def test_negative_dots_only_no_summary_rejected(self) -> None:
        # Acceptance negative (c): progress dots only, no summary line --
        # the shape of the intermittent r17 runner abort.
        stderr = "." * 200 + "\n"
        verdict = judge_unittest_run(
            None, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertIn("nonzero_returncode:None", verdict.reasons)
        self.assertIn("summary_missing", verdict.reasons)
        self.assertIn("verdict_missing", verdict.reasons)

    def test_negative_failed_verdict_rejected(self) -> None:
        stderr = ("F...\n"
                  "----------------------------------------------------------------------\n"
                  "Ran 346 tests in 11.3s\n\nFAILED (failures=1)\n")
        verdict = judge_unittest_run(
            1, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertIn("failed_verdict:failures=1", verdict.reasons)

    def test_negative_failure_counts_anywhere_rejected(self) -> None:
        # Belt-and-braces: failures=/errors= nonzero anywhere rejects even
        # without a parsed FAILED verdict line.
        stderr = "...\nRan 346 tests in 11.3s\n\nFAILED (errors=2, skipped=1)\n"
        verdict = judge_unittest_run(
            1, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertTrue(any(r.startswith("failed_verdict:")
                            for r in verdict.reasons))

    def test_negative_wrong_test_count_rejected(self) -> None:
        stderr = "...\nRan 345 tests in 11.3s\n\nOK\n"
        verdict = judge_unittest_run(
            0, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertIn("test_count_mismatch:ran=345,expected=346",
                      verdict.reasons)

    def test_negative_prose_containing_ok_not_accepted(self) -> None:
        # The substring trap: prose containing "OK" and "Ran 346 tests" but
        # no real summary/verdict lines must not pass.
        stderr = ("note: earlier Ran 346 tests looks OK-ish\n"
                  "everything is OK now\n")
        verdict = judge_unittest_run(
            0, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertIn("summary_missing", verdict.reasons)
        self.assertIn("verdict_missing", verdict.reasons)

    def test_positive_skips_allowed_when_not_required(self) -> None:
        # Full-slice discover context: skips are recorded, not forbidden.
        stderr = ("...\nRan 1229 tests in 244.8s\n\nOK (skipped=2)\n")
        verdict = judge_unittest_run(
            0, "", stderr,
            RunExpectation(test_count=1229, require_no_skips=False))
        self.assertTrue(verdict.accepted, verdict.reasons)

    def test_negative_ambiguous_summary_rejected(self) -> None:
        stderr = ("Ran 346 tests in 11.3s\n\nOK\n"
                  "Ran 346 tests in 11.4s\n\nOK\n")
        verdict = judge_unittest_run(
            0, "", stderr, RunExpectation(test_count=346))
        self.assertFalse(verdict.accepted)
        self.assertIn("summary_ambiguous:2", verdict.reasons)

    def test_audit_record_keeps_full_output(self) -> None:
        stderr = "F\nRan 1 test in 0.0s\n\nFAILED (failures=1)\n"
        proc = subprocess.CompletedProcess(args=["x"], returncode=1,
                                           stdout="full-stdout",
                                           stderr=stderr)
        verdict = judge_unittest_run(1, "full-stdout", stderr,
                                     RunExpectation(test_count=1))
        record = audit_record("label", proc, verdict,
                              RunExpectation(test_count=1))
        self.assertEqual(record["returncode"], 1)
        self.assertEqual(record["stdout"], "full-stdout")
        self.assertEqual(record["stderr"], stderr)
        self.assertFalse(record["accepted"])
        self.assertIn("failed_verdict:failures=1", record["rejection_reasons"])

    def test_collect_unittest_skips_parses_verbose_lines(self) -> None:
        verbose = ("test_a (pkg.mod.Case.test_a) ... skipped 'why one'\n"
                   "test_b (pkg.mod.Case.test_b) ... ok\n"
                   "test_c (pkg.mod2.Case.test_c) ... skipped 'why two'\n")
        rows = collect_unittest_skips("", verbose)
        self.assertEqual(
            rows,
            [{"test": "pkg.mod.Case.test_a", "reason": "why one"},
             {"test": "pkg.mod2.Case.test_c", "reason": "why two"}])

    def test_parse_unittest_summary_failed_with_skip_count(self) -> None:
        from reaserch_agent.route_test_harness import parse_unittest_summary
        parsed = parse_unittest_summary(
            "", "...\nRan 1229 tests in 244.8s\n\n"
                "FAILED (failures=8, errors=21, skipped=2)\n")
        self.assertEqual(parsed["ran_count"], 1229)
        self.assertEqual(parsed["summary_lines"], 1)
        self.assertEqual(parsed["verdict"],
                         "FAILED (failures=8, errors=21, skipped=2)")
        self.assertEqual(parsed["failures"], 8)
        self.assertEqual(parsed["errors"], 21)
        self.assertEqual(parsed["skipped"], 2)

    def test_parse_unittest_summary_ok(self) -> None:
        from reaserch_agent.route_test_harness import parse_unittest_summary
        parsed = parse_unittest_summary(
            "", ".\nRan 1 test in 0.0s\n\nOK\n")
        self.assertEqual(
            {k: parsed[k] for k in ("ran_count", "verdict", "failures",
                                    "errors", "skipped")},
            {"ran_count": 1, "verdict": "OK", "failures": 0, "errors": 0,
             "skipped": None})

    def test_normalize_unittest_timing_makes_output_byte_stable(self) -> None:
        from reaserch_agent.route_test_harness import normalize_unittest_timing
        a = "...\nRan 363 tests in 11.319s\n\nOK\n"
        b = "...\nRan 363 tests in 12.871s\n\nOK\n"
        self.assertEqual(normalize_unittest_timing(a),
                         normalize_unittest_timing(b))
        self.assertIn("<elapsed>s", normalize_unittest_timing(a))
        self.assertEqual(normalize_unittest_timing(None), "")

    def test_audit_record_normalizes_timing_on_request(self) -> None:
        stderr = ".\nRan 1 test in 0.042s\n\nOK\n"
        proc = subprocess.CompletedProcess(args=["x"], returncode=0,
                                           stdout="", stderr=stderr)
        verdict = judge_unittest_run(0, "", stderr,
                                     RunExpectation(test_count=1))
        record = audit_record("label", proc, verdict,
                              RunExpectation(test_count=1),
                              normalize_timing=True)
        self.assertIn("<elapsed>s", record["stderr"])
        self.assertTrue(record["accepted"])


class HistoricalLoaderContractTest(unittest.TestCase):
    """The folio loader: genuine git absence -> skip; import errors -> fail."""

    def test_git_executable_missing_yields_distinguished_unavailable(self):
        from reaserch_agent.test_route_pdf_folio import (
            HistoricalSourceUnavailable,
            _load_historical_source_module,
        )
        with mock.patch(
                "reaserch_agent.test_route_pdf_folio.subprocess.run",
                side_effect=FileNotFoundError("git not found")):
            with self.assertRaises(HistoricalSourceUnavailable) as ctx:
                _load_historical_source_module("491ec37")
        self.assertIn("git executable unavailable", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, FileNotFoundError)

    def test_git_commit_missing_yields_distinguished_unavailable(self):
        from reaserch_agent.test_route_pdf_folio import (
            HistoricalSourceUnavailable,
            _load_historical_source_module,
        )
        fake = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="",
            stderr="fatal: bad object deadbeef")
        with mock.patch(
                "reaserch_agent.test_route_pdf_folio.subprocess.run",
                return_value=fake):
            with self.assertRaises(HistoricalSourceUnavailable) as ctx:
                _load_historical_source_module("deadbeef")
        message = str(ctx.exception)
        self.assertIn("git show failed", message)
        self.assertIn("deadbeef", message)
        self.assertIn("bad object", message)

    def test_import_error_fails_with_original_exception_chained(self):
        # A syntactically valid historical module whose exec raises must NOT
        # be disguised as a git problem: the loader raises and chains the
        # original exception so the test FAILS with real information.
        from reaserch_agent.test_route_pdf_folio import (
            HistoricalSourceUnavailable,
            _load_historical_source_module,
        )
        broken_source = "raise ValueError('historical parser blew up')\n"
        fake = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=broken_source, stderr="")
        with mock.patch(
                "reaserch_agent.test_route_pdf_folio.subprocess.run",
                return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                _load_historical_source_module("whatever0")
        self.assertIn("failed to import/execute", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertIn("historical parser blew up", str(ctx.exception.__cause__))
        # And it must not be the skip channel.
        self.assertNotIsInstance(ctx.exception, HistoricalSourceUnavailable)

    def test_loader_passes_explicit_environment_to_git(self):
        # r18 diagnosis (a): with env=None the child inherits the Win32
        # kernel environment block, which a mock.patch.dict(os.environ)
        # round-trip can silently strip of empty-valued variables
        # (GIT_CONFIG_VALUE_0) -> git rc=128 -> disguised skip.  The loader
        # must pass an explicit env copy.
        from reaserch_agent.test_route_pdf_folio import (
            _load_historical_source_module,
        )
        fake = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fatal: x")
        with mock.patch(
                "reaserch_agent.test_route_pdf_folio.subprocess.run",
                return_value=fake) as run_mock:
            with self.assertRaises(Exception):
                _load_historical_source_module("491ec37")
        kwargs = run_mock.call_args.kwargs
        self.assertIn("env", kwargs)
        self.assertIsNotNone(kwargs["env"])

    def test_real_git_roundtrip_loads_prer17_module(self):
        # Integration sanity: with git genuinely available the loader
        # returns the historical module (skips only via the distinguished
        # unavailable channel).
        from reaserch_agent.test_route_pdf_folio import (
            HistoricalSourceUnavailable,
            _load_historical_source_module,
        )
        try:
            loaded = _load_historical_source_module("491ec37")
        except HistoricalSourceUnavailable as exc:
            self.skipTest(str(exc))
        module, temporary = loaded
        self.addCleanup(temporary.cleanup)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        self.assertTrue(hasattr(module, "_read_pdf_blocks"))
        self.assertEqual(module.__name__,
                         "reaserch_agent._route_pdf_source_491ec37")


if __name__ == "__main__":
    unittest.main()
