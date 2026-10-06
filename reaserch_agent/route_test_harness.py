"""Strict pass/fail judgement for unittest subprocess output (round 18).

Pure test infrastructure: this module is imported only by acceptance runners
and by its own test module.  It is NOT on any production path -- no
production module imports it, it consumes no tokens, and it touches no
chemistry rules or PDF parsing logic.

Why this exists (r17 lesson): the r17 runner accepted a subprocess whenever
the output contained the substrings "Ran 346 tests" and "OK".  Under that
rule all of the following were wrongly ACCEPTED:

- returncode != 0 with a tail that happens to contain "Ran 346 tests" / "OK"
  (e.g. an earlier successful segment, or a crashed-after-summary process);
- "OK (skipped=2)" -- must-run suites silently losing tests to skips;
- a progress-dots-only abort (no summary at all) was the only rejected
  shape -- but only by accident of the missing substring.

The judge below accepts a run only when ALL of the following hold:

1. returncode == 0;
2. the combined output carries exactly one summary line "Ran N tests in T s"
   and N equals the expected test count;
3. the final verdict line is exactly "OK" -- "FAILED (...)" in any form
   (failures=/errors= nonzero) rejects;
4. when the expectation requires zero skips, "OK (skipped=N)" with N > 0
   rejects (and any "skipped=" nonzero count anywhere rejects);
5. belt-and-braces: any failures=[1-9] / errors=[1-9] count anywhere in the
   output rejects even if the verdict line was somehow missed.

Every rejection carries a structured reason string so the runner's audit
log can record precisely why a run was refused, together with the full
stdout/stderr of the subprocess (no substring-only forensics).
"""
from dataclasses import dataclass, field
import re
from typing import Optional, Sequence, Tuple

_SUMMARY_RE = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*$", re.MULTILINE)
_VERDICT_OK_RE = re.compile(r"^OK(?: \(skipped=(\d+)\))?\s*$", re.MULTILINE)
_VERDICT_FAILED_RE = re.compile(r"^FAILED \(([^)]*)\)\s*$", re.MULTILINE)
_BAD_COUNT_RE = re.compile(r"\b(?:failures|errors)=[1-9]\d*\b")
_SKIPPED_COUNT_RE = re.compile(r"\bskipped=(\d+)\b")


@dataclass(frozen=True)
class RunExpectation:
    """What a judged subprocess run must look like to be accepted."""
    test_count: int
    require_no_skips: bool = False


@dataclass(frozen=True)
class RunVerdict:
    """Structured judgement; ``reasons`` is empty iff ``accepted``."""
    accepted: bool
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    ran_count: Optional[int] = None
    verdict_line: Optional[str] = None


def judge_unittest_run(returncode: Optional[int], stdout: Optional[str],
                       stderr: Optional[str],
                       expectation: RunExpectation) -> RunVerdict:
    """Judge one CompletedProcess-shaped unittest run.  Pure function.

    Pinned judging semantics (r19 hardening, acceptance gap 1):

    - **stderr is the only evidence stream.**  unittest's TextTestRunner
      always writes the progress dots, the ``Ran N tests`` summary and the
      verdict line (``OK`` / ``OK (skipped=N)`` / ``FAILED (...)``) to
      stderr.  Any summary/verdict-shaped line on stdout is ordinary test
      output (a test printing "OK") and never constitutes judgement
      evidence.  Judgement trusts only returncode + the structured stderr
      summary; log-content substrings on stdout never acquit a run.
    - Exactly one ``Ran N tests`` line must exist on stderr, with N equal
      to the expected count.
    - Exactly one verdict-shaped line must exist on stderr **after** the
      summary line; zero or multiple verdict lines are rejections
      (``verdict_missing`` / ``verdict_ambiguous:N``).
    - For must-run suites (``require_no_skips=True``), ANY nonzero
      ``skipped=N`` anywhere on stderr — the verdict line or elsewhere —
      is an unconditional rejection, regardless of any OK-shaped line.
    - A nonzero returncode is always a rejection; failure/error counts
      anywhere on stderr are a belt-and-braces rejection.

    All criteria must hold; every violated criterion appends one structured
    reason.  No exception is raised for a failing run -- failure is data.
    """
    err = stderr or ""
    reasons = []
    ran_count = None
    verdict_line = None

    # 1. exit status
    if returncode != 0:
        reasons.append(f"nonzero_returncode:{returncode}")

    # 2. exactly one summary line (stderr only) with the expected count
    summary_hits = list(_SUMMARY_RE.finditer(err))
    if not summary_hits:
        reasons.append("summary_missing")
    elif len(summary_hits) > 1:
        reasons.append(f"summary_ambiguous:{len(summary_hits)}")
    else:
        ran_count = int(summary_hits[0].group(1))
        if ran_count != expectation.test_count:
            reasons.append(
                f"test_count_mismatch:ran={ran_count},"
                f"expected={expectation.test_count}")

    # 3. verdict (stderr only): exactly one verdict-shaped line AFTER the
    #    last summary line
    verdict_hits = [(m.end(), "ok", m.group(1))
                    for m in _VERDICT_OK_RE.finditer(err)]
    verdict_hits += [(m.end(), "failed", m.group(1))
                     for m in _VERDICT_FAILED_RE.finditer(err)]
    if summary_hits:
        last_summary_end = summary_hits[-1].end()
        verdict_hits = [h for h in verdict_hits if h[0] > last_summary_end]
    if not verdict_hits:
        reasons.append("verdict_missing")
    elif len(verdict_hits) > 1:
        reasons.append(f"verdict_ambiguous:{len(verdict_hits)}")
    else:
        _, kind, detail = verdict_hits[0]
        if kind == "failed":
            verdict_line = f"FAILED ({detail})"
            reasons.append(f"failed_verdict:{detail}")
        else:
            verdict_line = ("OK" if detail is None
                            else f"OK (skipped={detail})")

    # 4. belt-and-braces: failure/error counts anywhere on stderr
    bad = _BAD_COUNT_RE.search(err)
    if bad is not None \
            and not any(r.startswith("failed_verdict:") for r in reasons):
        reasons.append(f"failure_error_count_in_output:{bad.group(0)}")

    # 5. must-run suites: ANY nonzero skipped=N on stderr rejects, no
    #    matter what verdict-shaped lines exist (dedup against criterion 3)
    if expectation.require_no_skips:
        for skip_hit in _SKIPPED_COUNT_RE.finditer(err):
            count = int(skip_hit.group(1))
            reason = f"unexpected_skips:{count}"
            if count > 0 and reason not in reasons:
                reasons.append(reason)
                break

    return RunVerdict(accepted=not reasons, reasons=tuple(reasons),
                      ran_count=ran_count, verdict_line=verdict_line)


def audit_record(label: str, proc, verdict: RunVerdict,
                 expectation: RunExpectation,
                 normalize_timing: bool = False) -> dict:
    """JSON-serialisable audit record: full stdout/stderr, not substrings.

    With ``normalize_timing=True`` the elapsed-seconds field of the unittest
    summary line is replaced by a placeholder so records stay byte-stable
    across runs (used by the acceptance runner's deterministic artifacts).
    """
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if normalize_timing:
        stdout = normalize_unittest_timing(stdout)
        stderr = normalize_unittest_timing(stderr)
    return {
        "label": label,
        "expected_test_count": expectation.test_count,
        "require_no_skips": expectation.require_no_skips,
        "returncode": proc.returncode,
        "accepted": verdict.accepted,
        "rejection_reasons": list(verdict.reasons),
        "ran_count": verdict.ran_count,
        "verdict_line": verdict.verdict_line,
        "timing_normalized": normalize_timing,
        "stdout": stdout,
        "stderr": stderr,
    }


def normalize_unittest_timing(text: Optional[str]) -> str:
    """Replace the elapsed-seconds field of the unittest summary line(s) so
    recorded output is byte-stable across runs ("Ran 5 tests in 0.042s" ->
    "Ran 5 tests in <elapsed>s").  Nothing else is touched."""
    return re.sub(r"(Ran \d+ tests? in )[\d.]+s", r"\g<1><elapsed>s",
                  text or "")


def normalize_transient_paths(text: Optional[str]) -> str:
    """Replace run-varying path/timing fragments so recorded logs are
    byte-stable across runs:

    - tempfile random directory names (``tmp`` + 8 chars from [a-z0-9_]) in
      Temp paths, in raw (``Temp\\tmpXXXX``), escaped (``Temp\\\\tmpXXXX``)
      and POSIX (``Temp/tmpXXXX``) spellings -> ``tmp<random>``;
    - the jieba model-load timing line ("Loading model cost 0.364
      seconds.") -> "<elapsed> seconds";
    - the agent's per-step timing lines ("[research-agent] LLM step done:
      <step> (0.1s)") -> "(<elapsed>s)" — these leaked through at the
      r18 final verification (one discover-log line flipped 0.1s/0.0s
      between runs and dirtied the tracked log).

    Rationale (r18 clean-rerun fix): full-slice discover logs record
    tracebacks of the baseline failing tests, which embed random temporary
    directory names and the jieba load time; without normalization every
    runner execution rewrites the tracked discover logs with different
    bytes, so the runner's own cleanliness gate rejects the second build.
    """
    out = re.sub(r"(Temp(?:\\\\|\\|/))tmp[0-9a-z_]{8}\b",
                 r"\g<1>tmp<random>", text or "")
    out = re.sub(r"(Loading model cost )[\d.]+( seconds)",
                 r"\g<1><elapsed>\g<2>", out)
    out = re.sub(r"(LLM step done: \S+ \()[\d.]+(s\))",
                 r"\g<1><elapsed>\g<2>", out)
    return out


def parse_unittest_summary(stdout: Optional[str],
                           stderr: Optional[str]) -> dict:
    """Parse the unittest summary/verdict lines without judging them.

    stderr is the only evidence stream (same pinned semantics as
    ``judge_unittest_run``): the summary and verdict lines of a unittest
    run live on stderr; summary/verdict-shaped lines on stdout are ordinary
    test output and are ignored here.  When several verdict-shaped lines
    exist on stderr the LAST one is taken (it is the run's final verdict);
    the count of summary lines is reported honestly.

    Returns a dict with ran_count (None when no summary line), summary_lines
    (number of "Ran N tests" lines seen), verdict ("OK" / "OK (skipped=N)" /
    "FAILED (...)" / None), and the failures/errors/skipped counts from the
    verdict line (None when absent).  Used for honest recording of suites
    whose failures are an expected baseline (full-slice discover).
    """
    err = stderr or ""
    summaries = _SUMMARY_RE.findall(err)
    verdict = None
    failures = errors = skipped = None
    failed_matches = list(_VERDICT_FAILED_RE.finditer(err))
    ok_matches = list(_VERDICT_OK_RE.finditer(err))
    candidates = [(m.end(), "failed", m) for m in failed_matches]
    candidates += [(m.end(), "ok", m) for m in ok_matches]
    if candidates:
        _, kind, match = sorted(candidates)[-1]
        if kind == "failed":
            verdict = f"FAILED ({match.group(1)})"
            counts = dict(re.findall(r"(\w+)=(\d+)", match.group(1)))
            failures = int(counts.get("failures", 0))
            errors = int(counts.get("errors", 0))
            skipped = int(counts["skipped"]) if "skipped" in counts else None
        else:
            verdict = ("OK" if match.group(1) is None
                       else f"OK (skipped={match.group(1)})")
            failures = errors = 0
            skipped = (int(match.group(1)) if match.group(1) is not None
                       else None)
    return {
        "ran_count": int(summaries[-1]) if summaries else None,
        "summary_lines": len(summaries),
        "verdict": verdict,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def collect_unittest_skips(stdout: Optional[str],
                           stderr: Optional[str]) -> list:
    """Extract (test_id, reason) skip lines from -v unittest output.

    stderr is the only evidence stream (same pinned semantics as
    ``judge_unittest_run``): ``unittest -v`` writes per-test outcome lines
    to stderr, so skip-shaped lines on stdout are ordinary test output and
    are ignored.

    Used for honest recording of full-slice discover skips (recorded as
    data, never asserted to be zero).
    """
    err = stderr or ""
    rows = []
    for line in err.splitlines():
        # unittest -v format: "test_x (pkg.mod.Case.test_x) ... skipped 'r'"
        match = re.match(r"^\w+ \(([^)]+)\) \.\.\. skipped (.*)$", line)
        if match:
            rows.append({
                "test": match.group(1),
                "reason": match.group(2).strip().strip("'"),
            })
    return rows


# Full-slice discover keeps its historical (baseline-pinned) failures, so a
# healthy discover run exits 0 (all pass) or 1 (failures present).  Any
# other returncode -- 2 (unittest usage error), or crash/abort codes like
# 99/134/139/0xC000xxxx -- means the slice itself is broken and must be
# rejected no matter how normal the captured output looks.
DISCOVER_NORMAL_RETURNCODES = (0, 1)


def discover_returncode_reason(returncode: Optional[int]) -> Optional[str]:
    """Judge a full-slice discover returncode; None means acceptable.

    Acceptance gap 2 (r19): a discover slice that preserves its baseline
    failures is expected to exit 1, and a fully-green slice exits 0; every
    other exit status is rejected even when stdout/stderr carry a
    normal-looking summary.  The judgement trusts returncode + structured
    summary only -- log-content appearance never acquits a run.
    """
    if returncode in DISCOVER_NORMAL_RETURNCODES:
        return None
    return (f"unexpected_returncode:{returncode} "
            f"(unittest normal exit is 0=all-pass or 1=failures-present)")


def timeout_forensics_record(label: str, exc,
                             normalize_timing: bool = True) -> dict:
    """Build the forensics record for a subprocess timeout (acceptance gap
    4, r19): the timeout fact PLUS whatever partial stdout/stderr the child
    produced before being killed, so a hung run leaves evidence instead of
    an unrecorded exception.  ``exc`` is a subprocess.TimeoutExpired; its
    stdout/stderr may be str (text mode) or bytes -- both are handled.
    The runner writes this record to the forensics file and then fails;
    there is deliberately NO automatic retry.
    """
    def _text(payload) -> str:
        if payload is None:
            return ""
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)

    stdout, stderr = _text(exc.stdout), _text(exc.stderr)
    if normalize_timing:
        stdout = normalize_transient_paths(normalize_unittest_timing(stdout))
        stderr = normalize_transient_paths(normalize_unittest_timing(stderr))
    return {
        "label": label,
        "timed_out": True,
        "timeout_seconds": exc.timeout,
        "cmd": [str(part) for part in (exc.cmd or [])],
        "note": ("subprocess exceeded its timeout and was killed; partial "
                 "output below is everything captured before the kill; "
                 "the runner does NOT auto-retry timeouts"),
        "stdout_partial": stdout,
        "stderr_partial": stderr,
    }


def write_forensics_file(path, record: dict) -> None:
    """Write a forensics record as UTF-8 LF JSON (single canonical form so
    the runner and tests share one writer)."""
    import json as _json
    from pathlib import Path as _Path
    _Path(path).write_text(
        _json.dumps(record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8", newline="\n")
