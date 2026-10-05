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

    All criteria must hold; every violated criterion appends one structured
    reason.  No exception is raised for a failing run -- failure is data.
    """
    out = (stdout or "") + "\n" + (stderr or "")
    reasons = []
    ran_count = None
    verdict_line = None

    # 1. exit status
    if returncode != 0:
        reasons.append(f"nonzero_returncode:{returncode}")

    # 2. exactly one summary line with the expected count
    summaries = _SUMMARY_RE.findall(out)
    if not summaries:
        reasons.append("summary_missing")
    elif len(summaries) > 1:
        reasons.append(f"summary_ambiguous:{len(summaries)}")
    else:
        ran_count = int(summaries[0])
        if ran_count != expectation.test_count:
            reasons.append(
                f"test_count_mismatch:ran={ran_count},"
                f"expected={expectation.test_count}")

    # 3. verdict line: exactly OK (with optional zero-skip annotation)
    ok_match = _VERDICT_OK_RE.search(out)
    failed_match = _VERDICT_FAILED_RE.search(out)
    if failed_match is not None:
        verdict_line = f"FAILED ({failed_match.group(1)})"
        reasons.append(f"failed_verdict:{failed_match.group(1)}")
    elif ok_match is not None:
        skipped = ok_match.group(1)
        verdict_line = "OK" if skipped is None else f"OK (skipped={skipped})"
        if skipped is not None and expectation.require_no_skips \
                and int(skipped) > 0:
            reasons.append(f"unexpected_skips:{skipped}")
    else:
        reasons.append("verdict_missing")

    # 4. belt-and-braces: failure/error counts anywhere in the output
    bad = _BAD_COUNT_RE.search(out)
    if bad is not None and failed_match is None:
        reasons.append(f"failure_error_count_in_output:{bad.group(0)}")

    # 5. skipped= nonzero anywhere when the suite must run everything
    if expectation.require_no_skips:
        for skip_hit in _SKIPPED_COUNT_RE.finditer(out):
            if int(skip_hit.group(1)) > 0 and ok_match is None:
                reasons.append(f"unexpected_skips:{skip_hit.group(1)}")
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

    Returns a dict with ran_count (None when no summary line), summary_lines
    (number of "Ran N tests" lines seen), verdict ("OK" / "OK (skipped=N)" /
    "FAILED (...)" / None), and the failures/errors/skipped counts from the
    verdict line (None when absent).  Used for honest recording of suites
    whose failures are an expected baseline (full-slice discover).
    """
    out = (stdout or "") + "\n" + (stderr or "")
    summaries = _SUMMARY_RE.findall(out)
    verdict = None
    failures = errors = skipped = None
    failed_match = _VERDICT_FAILED_RE.search(out)
    ok_match = _VERDICT_OK_RE.search(out)
    if failed_match is not None:
        verdict = f"FAILED ({failed_match.group(1)})"
        counts = dict(re.findall(r"(\w+)=(\d+)", failed_match.group(1)))
        failures = int(counts.get("failures", 0))
        errors = int(counts.get("errors", 0))
        skipped = int(counts["skipped"]) if "skipped" in counts else None
    elif ok_match is not None:
        verdict = ("OK" if ok_match.group(1) is None
                   else f"OK (skipped={ok_match.group(1)})")
        failures = errors = 0
        skipped = (int(ok_match.group(1)) if ok_match.group(1) is not None
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

    Used for honest recording of full-slice discover skips (recorded as
    data, never asserted to be zero).
    """
    out = (stdout or "") + "\n" + (stderr or "")
    rows = []
    for line in out.splitlines():
        # unittest -v format: "test_x (pkg.mod.Case.test_x) ... skipped 'r'"
        match = re.match(r"^\w+ \(([^)]+)\) \.\.\. skipped (.*)$", line)
        if match:
            rows.append({
                "test": match.group(1),
                "reason": match.group(2).strip().strip("'"),
            })
    return rows
