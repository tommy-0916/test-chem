# Field Semantic Gate — Round-18 Checkpoint (2026-10-05)

Round 18 is an **acceptance-infrastructure round**: it tightens the runner's
subprocess judgement (the r17 self-check was shown to false-accept), fixes
the folio historical loader's skip/failure semantics, and root-causes two
previously unexplained observations. **No model is called; no chemistry
rule, conventions resource, protocol schema, or PDF-parsing production
logic changes** (the parser files stay pinned at their r17 hashes, asserted
inside the runner). Exactly three code files move, all test infrastructure:

- `reaserch_agent/route_test_harness.py` — **NEW**. Strict, testable
  judgement for unittest subprocess runs (`judge_unittest_run`), plus
  `audit_record` (full timing-normalized stdout/stderr, not substrings),
  `parse_unittest_summary`, `collect_unittest_skips`,
  `normalize_unittest_timing`. Pure test infrastructure; not imported by
  any production module.
- `reaserch_agent/test_route_test_harness.py` — **NEW**, 21 tests. Joins
  the target set (346 → **367**).
- `reaserch_agent/test_route_pdf_folio.py` — **MODIFIED, still 27 tests**.
  `_load_historical_source_module` now raises `HistoricalSourceUnavailable`
  (the skip channel) **only** for the two genuine causes — the git
  executable cannot be run (`OSError`/`SubprocessError`) or `git show`
  returns non-zero — with **distinct messages**; import/execution errors of
  a fetched historical module raise `RuntimeError` chaining the original
  exception, so they **fail** the test instead of being disguised as a git
  problem. The git subprocess also gets an explicit `env=dict(os.environ)`
  copy (diagnosis (a) fix, §3).

Artifacts: runner `result/operation-structure-20260928/local-revision-r18.py`;
replay `local-revision-r18-replay.json` (sha256
`sha256_f3295cb9152d0fa4313c32517dfd30aea26520ac9069947ab12249042d58a25c` over
UTF-8 bytes; two consecutive in-process builds byte-identical **and** two
separate process runs byte-identical, `cmp` clean on replay and run logs);
audit `local-revision-r18-audit.json`; run logs
`local-revision-r18-run1/2.log` (identical); discover logs
`local-revision-r18-discover-{reaserch_agent,chem_agent_contracts}.log`
(forensic, timing-normalized). The runner consumes zero tokens.

## 1. The r17 judgement gap (acceptance feedback)

The r17 runner accepted a unittest subprocess when its output contained the
substrings `Ran 346 tests` and `OK`. Acceptance probed that rule with
controlled return values and showed it false-accepts:

- **(a)** a non-zero `returncode` whose output tail carries the substrings;
- **(b)** `OK (skipped=2)` — `OK` is a substring;
- only **(c)** progress dots with no summary were rejected (by accident of
  the missing substring).

## 2. The r18 judgement contract (route_test_harness.py)

`judge_unittest_run(returncode, stdout, stderr, RunExpectation)` accepts
**only when all** criteria hold; every violated criterion appends one
structured rejection reason (failure is data, never an exception):

1. `returncode == 0` — else `nonzero_returncode:<rc>`;
2. exactly one summary line `Ran N tests in T s` with **N == expected** —
   else `summary_missing` / `summary_ambiguous:<k>` /
   `test_count_mismatch:ran=<n>,expected=<m>`;
3. the verdict line is exactly `OK` — `FAILED (...)` in any form rejects
   (`failed_verdict:<counts>`); a missing verdict rejects
   (`verdict_missing`);
4. must-run suites (`require_no_skips=True`): `OK (skipped=N)` with N>0
   rejects (`unexpected_skips:<N>`), as does any `skipped=N` nonzero
   anywhere;
5. belt-and-braces: `failures=[1-9]` / `errors=[1-9]` anywhere in the
   output rejects even if the verdict line was somehow missed
   (`failure_error_count_in_output:<hit>`).

Matching is line-anchored (`^...$`, MULTILINE), so prose containing "OK" or
"Ran N tests" can never pass. `audit_record` stores the returncode and the
**full** stdout/stderr (timing-normalized for byte stability) into the
replay/audit; on rejection the runner additionally writes
`local-revision-r18-failure-forensics.json` **before** failing.

### Negative/positive tests (all in `test_route_test_harness.py`, 21 OK)

| case | input | verdict |
| --- | --- | --- |
| negative (a) | rc=1 + "Ran 346 tests" + "OK" | **rejected** (`nonzero_returncode:1`) |
| negative (b) | rc=0 + "OK (skipped=2)", must-run | **rejected** (`unexpected_skips:2`) |
| negative (c) | dots only, no summary | **rejected** (`summary_missing` + `verdict_missing`) |
| negative | "FAILED (failures=1)" | rejected (`failed_verdict:failures=1`) |
| negative | failures=/errors= nonzero without parsed verdict | rejected |
| negative | "Ran 345 tests" vs expected 346 | rejected (`test_count_mismatch`) |
| negative | prose containing "OK"/"Ran 346 tests" | rejected (no anchored lines) |
| negative | two "Ran N tests" summaries | rejected (`summary_ambiguous:2`) |
| positive | rc=0 + "Ran N tests" + "OK", zero skips | **accepted** |
| positive | "OK (skipped=2)" with skips allowed (discover context) | accepted |
| loader | git executable missing (FileNotFoundError) | `HistoricalSourceUnavailable` ("git executable unavailable") — honest skip channel |
| loader | commit missing (rc=128, "bad object") | `HistoricalSourceUnavailable` ("git show failed … deadbeef") — distinguished |
| loader | historical module raises on exec | `RuntimeError` chaining the original `ValueError` — **test fails**, no disguise |
| loader | integration: real `git show 491ec37` loads and exposes `_read_pdf_blocks` | passes (skips only via the distinguished channel) |
| loader | git call carries an explicit `env` copy | asserted |

## 3. Diagnosis (a): full-slice discover skip=2 — root-caused, fixed

**Symptom.** Full-slice `unittest discover` reported `OK (skipped=2)`: the
folio `test_prer15/prer17_block_semantics_comparison` tests skipped with
"git or the commit unavailable", while passing standalone (27/27).

**Root cause chain (every link measured).**

1. The agent shell exports `GIT_CONFIG_COUNT=2`,
   `GIT_CONFIG_KEY_0=credential.helper`, **`GIT_CONFIG_VALUE_0=` (EMPTY)**,
   `GIT_CONFIG_KEY_1/VALUE_1` (credential helper pinning).
2. The first `mock.patch.dict(os.environ, ...)` round-trip in the suite —
   alphabetically `test_action_evidence_selection.ActionEvidenceSelectionTest.test_action_design_prompt_lists_all_four_explicit_sources_without_recipe_numbers`
   — restores via `os.environ.clear()` + `update()`. On Windows, re-setting
   an **empty-valued** variable deletes it from the **Win32 kernel
   environment block** (SetEnvironmentVariableW semantics), while
   `os.environ` keeps showing `''`.
3. Children spawned with `env=None` inherit that kernel block:
   `GIT_CONFIG_VALUE_0` is **missing** while `GIT_CONFIG_COUNT=2`/`KEY_0`
   remain → git exits **rc=128 "missing config value GIT_CONFIG_VALUE_0"**.
4. The old loader mapped *any* git failure to `skipTest`.

**Evidence.** Instrumented loader inside discover: `git rc=128` with the
exact error, while the same call with `env=dict(os.environ)` returns rc=0
in the same polluted state; kernel-level env sniffing around every one of
the 1229 tests shows **exactly one** flip `present:'' → MISSING`, at that
first patch.dict test, never recovering; a five-line micro-repro (one
`patch.dict(os.environ)` round-trip) flips the kernel state and makes
`git rev-parse` fail rc=128 with `env=None` and succeed with `env=copy`;
full-slice discover with `GIT_CONFIG_*` stripped runs **1229 tests, zero
skips**; with the loader fix, full-slice discover in the polluted shell
runs **1250 tests, zero skips**.

**Fix placement (deliberate).** The polluter's `patch.dict(os.environ)` is
a legitimate, correct pattern; the trigger is the shell's empty
`GIT_CONFIG_VALUE_0`. The fix lands in the loader (explicit `env` copy +
honest skip/failure separation), not in 100+ legitimate patch.dict users.
The discover skip list is now recorded as data in the runner (never
asserted zero).

## 4. Diagnosis (b): intermittent runner abort — NOT reproduced; forensics built in

**Symptom (r17, before this round).** One of six full runner runs failed
because the target-set subprocess output stopped at progress dots with no
summary; isolated re-runs of the same call passed 15/15, direct module
runs 5/5.

**Reproduction attempts this round (identical invocation, same env
construction as the runner):** 60 clean sequential + 18 three-way parallel
+ 6 under ~8 GB memory pressure = **102 invocations, 0 anomalies** — every
run rc=0 with a complete summary; elapsed 12–16 s regardless of load (host
had 18 GB free physical / 18.7 GB free commit during the probes).

**Conclusion (evidence-bounded, not "Windows 瞬态").** The failure was not
reproduced, so no root cause is asserted. What the observed shape does and
does not support:

- Dots-only-no-summary with a **completed** subprocess call implies the
  child interpreter **died mid-run with a non-zero exit** — a normal
  unittest exit always prints the summary, and a parent-side timeout would
  have raised `TimeoutExpired` in the runner instead of failing the check.
- Most consistent: a **hard child crash** (MuPDF/fitz is the only native
  library in the target set) or an **external kill** (AV/EDR). Not
  observed: resource exhaustion (18 GB free during probes), output-capture
  truncation (pipes don't drop a trailing summary while keeping earlier
  dots), or test pollution (the set is self-contained).
- With 1 failure in ~6 runner runs historically and 0/102 now, the true
  rate in this environment is low; the machine state at the original
  failure is unknown.

**Forensics now built into the r18 runner** (so the next occurrence is
self-documenting): every subprocess is spawned with `-X faulthandler` (a
native crash dumps the Python stack to stderr); every judged subprocess
records `returncode` + full timing-normalized stdout/stderr into the replay
anchors; on rejection the runner writes the forensics file **before**
failing. If it recurs: a `0xC0000005`-style returncode fingers a native
crash; a timeout/`rc=None` fingers pre-emption; correlate wall-clock with
host AV/EDR logs.

## 5. Tests

- **Folio suite: 27/27 OK**, judged by the strict harness as a subprocess
  with **zero skips required** (the two historical comparisons now really
  run inside discover too).
- **Target set: 367/367 OK** (the r17 13 modules = 346 tests, plus
  `test_route_test_harness` = 21), judged with zero skips required.
- **Full-slice discover**: `reaserch_agent` **1250 tests** (1229 + 21) —
  failure-header set, normalized with
  `sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/'`, diffs against
  the 30-entry baseline with **exactly one delta**:
  `test_b1_bootstrap_generates_initial_outputs` passes in this workspace
  (**zero new, zero otherwise missing**); skips **0** (recorded as data).
  `chem_agent_contracts` **116 tests** — the single failure
  (`test_legacy_bound_package_hash_is_unchanged`) is **byte-identical** to
  the baseline (normalized diff empty); skips **0**. Both header sets are
  additionally asserted byte-equal to embedded copies **inside** the
  runner, so the zero-new-failures gate is self-contained.
- r13b archive-pinned runner re-executed as a subprocess: replay matches
  the r13 archive, double-run byte-identical, **ms7a.out stays BLOCKED**
  (`retained_object_mention_precedes_operation`).
- 3E diagnostics module: zero proof dependency (source audit, runner
  anchor).

## 6. Anchors held (all re-verified inside the r18 runner)

- **A01 control**: byte-identical (1289 blocks, pinned sha256, 0 folios).
- **Wu SI**: 1041 blocks, 79 validated folios (`S 2`..`S 80`); W1 sentence
  binds verbatim (`pdf:p14:b9-p14:b10` needle / `pdf:p14:b9-p14:b11` full
  sentence).
- **Wu main**: still abstains `pdf_column_layout_ambiguous` on the p10
  chart tick `36`.
- **Pool census (8 candidates)**: row-by-row identical to the r15, r16,
  **and r17** censuses (cross-checked against all three archived replays).
- **r17 evidence re-verified unchanged**: neighborhood before/against the
  491ec37 parser, SI gap distribution (min 14.16 pt / median 372.04 pt),
  the ten-shape v3 comparison, dual-parse byte-identity of both real
  anchor documents, caption-boundary geometry (43.37 pt gap, 24.02 pt
  indent), rule-eligibility pre-screen (W1/W2 ineligible), attested layer
  (both signed documents verify, same endpoints).
- **Parser version**: stays `route_pdf_groups/v3` (no parser change);
  r13/r13b archives still pin **zero** version strings (re-asserted).
- **Runner determinism**: two in-process builds byte-identical; two
  separate process runs byte-identical (`cmp` clean on replay and run
  logs); replay sha256
  `sha256_f3295cb9152d0fa4313c32517dfd30aea26520ac9069947ab12249042d58a25c`;
  run-log sha256 `c9861c704bd787e9e8feb6ea5f494e21a7df4d232e5119a87b0ebc2e6f1a7f28`
  (both runs).

## 7. Fixed constraints

r7–r17 archives untouched (the r17 checkpoint is sha256-pinned inside the
r18 runner); `chem_agent_contracts/` untouched; conventions.json
byte-identical to the frozen sha256 (no v1 expansion); parser files
byte-identical to r17; 3E diagnostics semantics untouched
(`diagnostics_only`, zero tokens); protocol-definition/v1 not expanded; A01
ms7a.out and its cascade stay BLOCKED; zero tokens consumed by the runner;
no model started; Device not run; tracked modifications limited to the
three named files.

## 8. Design decisions (for the record)

1. **Judgement is a pure, tested function, not runner inline code.** The
   contract lives in `route_test_harness.py` with 21 pinning tests
   including the three acceptance negatives; the runner only feeds it
   CompletedProcesses. Reusable by future rounds; not on any production
   path.
2. **Failure is data.** The judge never raises: every rejection is a
   structured reason list, and full (timing-normalized) subprocess output
   lands in the audit/forensics files. Timing fields are normalized so all
   recorded artifacts stay byte-stable across runs.
3. **Skips are policy, not parser luck.** Must-run suites (folio, target
   set) require zero skips; full-slice discover records the skip list as
   data (currently zero) because legitimate environment skips can exist
   there — the hard gate on discover is the byte-equal failure-header set.
4. **Fix at the loader, not at the polluters.** Diagnosis (a)'s trigger is
   the shell's empty `GIT_CONFIG_VALUE_0`; `patch.dict(os.environ)` is
   legitimate. The loader's explicit `env` copy immunizes it against the
   whole kernel-block damage class.
5. **Not-reproduced is a result.** Diagnosis (b) is reported with 102
   clean invocations and a bounded differential; the runner now carries
   `-X faulthandler` and full-output forensics so a recurrence documents
   itself.

## 9. Open items / known boundaries

- Diagnosis (b) remains **unreproduced**; if the abort recurs, read
  `local-revision-r18-failure-forensics.json` and the run log first.
- The full-slice baselines live outside the repo
  (`../../baseline-{research,contracts}-fails.log` relative to the repo
  root); the runner embeds byte-equal copies so the gate is
  self-contained, and the acceptance diff against the external files is
  reproduced in §5.
- Discover raw logs include a jieba model-load timing line (harmless
  provenance noise); replay/audit content excludes it.
- Main-text chart-tick layouts, the SI caption-boundary grouping design
  (signals recorded, unimplemented), and the reduction rule family all
  stay as recorded in r16/r17 — nothing pre-committed.
- Real model generation remains blocked on the quota window (r14 record
  stands).

## 10. Clean-rerun defect found at acceptance and fixed (2026-10-05, same round)

**Defect (deterministically reproduced by acceptance).** From a clean tree
(`git checkout` restoring all tracked files), a single run of
`local-revision-r18.py` failed with
`r18 acceptance failed: tracked modifications outside the round-18 files: ["result/operation-structure-20260928/local-revision-r18-discover-reaserch_agent.log"]`.

**Root cause (one sentence).** The tracked discover logs were not
byte-stable — baseline-failure tracebacks embed random
`tempfile.TemporaryDirectory` names (`Temp\tmpXXXXXXXX`, plus the jieba
load-timing line) — **and** the logs were missing from the cleanliness
gate's round-file allowed set, so the second `_build_replay()`'s
`_baseline()` gate read its own first build's log rewrite as an
out-of-scope tracked modification. The original double-run passed only
because the logs were untracked at that moment; after committing them, any
re-run had to fail. Two self-check holes: (1) the logs were never
byte-stabilized (only the unittest summary timing was normalized), (2) the
allowed set was never updated when the logs became tracked artifacts.

**Fix (minimal, inside the round's files).**

1. `route_test_harness.py` gains `normalize_transient_paths()` — Temp-path
   random segments (`Temp\tmp…` raw / `Temp\\tmp…` escaped / `Temp/tmp…`
   POSIX) → `tmp<random>`, and the jieba `Loading model cost X seconds`
   line → `<elapsed>`; pinned by two new harness tests (21 → **23**).
2. The runner's `_discover_slice` writes both discover logs through
   `normalize_unittest_timing` + `normalize_transient_paths`; the gate's
   allowed set now names every artifact the runner (or its stdout capture)
   rewrites — the two discover logs, the two run logs, and the
   replay/audit JSONs (`ROUND_ARTIFACT_FILES`) — so the gate keeps
   catching anything *outside* the round's own byte-stable products.
3. Pins/counts updated: harness sha256 `f5a5f88e…51aa7f3`, harness-test
   sha256 `6d734b90…04bdd3f9`, harness tests 23, target set **369**,
   research discover **1252**.

**Re-verified invariant.** From a clean tracked tree: two consecutive
process runs pass, run logs byte-identical, replay byte-identical to the
first run's, and **post-run `git status` shows zero tracked modifications**
(the discover logs are rewritten with byte-identical normalized content —
byte-stability was also verified directly: two standalone regenerations of
the research discover log are `cmp`-clean, 26 `tmp<random>` placeholders, 0
residual random names). The discover-log content pins nothing in the replay
(only headers/skips/counts ride it), so the replay sha256 changed only via
the updated harness pins and counts.

**Boundary note (standing convention, unchanged).** All runner sha256 pins
are computed over the LF working-copy bytes that match the committed blobs;
with `core.autocrlf=true` on this machine, a *forced* re-checkout of a
tracked file smudges it to CRLF and would desync any pin — the convention
(identical since r7) is that acceptance restores a clean tree without
re-smudging, under which every pin holds.

## 11. Host process-hunt found during final verification; runner adapted to pythonw.exe (2026-10-05, same round)

**Symptom.** While re-running the acceptance double run after the §10 fix,
every detached console-subsystem `python.exe` process on this host died
within seconds to ~2 minutes of launch (six detached/watchdog/scheduled-task
launch attempts, zero survivors), while the same interpreter ran to
completion when executed synchronously in the foreground (a 150 s probe
survived; the harness/369-target-set direct runs in §5 were also
foreground).

**Evidence collected.**

- A detached child `python.exe` spawned by a surviving parent exited with
  **0xC000013A (STATUS_CONTROL_C_EXIT)**, and one killed run log contained
  a literal `^C` byte — the kill mechanism is external CTRL+C injection,
  not resource exhaustion.
- The kills are **not** memory-correlated: processes died with
  phys_avail 18 GB / commit_avail 19 GB / load 43 %, and earlier deaths
  during a real memory crunch (commit_avail < 1 GB, load 97 %) had the
  same signature. The earlier memory correlation (historical successes at
  commit_avail ≥ 18 GB) is not explained; it may be coincidental or one of
  several trigger conditions. The hunter's identity is undetermined
  (Windows Defender shows no detections; no WER/Application Error events).
- `pythonw.exe` (GUI subsystem, no console) survives: a 200 s detached
  probe ran to normal exit, and a pythonw-parent → pythonw-child pair ran
  150 s to rc=0.

**Adaptation (this round's runner only, semantics unchanged).**
`_python_cmd()` and the detached launcher now use
`.venv/Scripts/pythonw.exe`. Verdict capture is unchanged: stdout/stderr
still arrive through pipes/files supplied by the parent, `-X faulthandler`
is still carried, and the strict harness judgement is untouched. The
runner remains a tracked round-18 file; this change is committed as part
of this round.

**Consequence for diagnosis (b) (§4).** The r17-era intermittent abort
shape (progress dots, no summary) is exactly what an externally
CTRL+C-killed child looks like, and §4 already concluded "most consistent
with ... an external kill" — the 0xC000013A evidence now names the kill
mechanism directly. The 102 non-reproductions in §4 were all synchronous
foreground invocations, which this hunt does not touch, so zero anomalies
there and the detached-run kills here are consistent. This remains a
strongly-supported candidate root cause, not a proven sole cause: the
hunter process itself was not identified.

## 12. Clean-rerun residual: LLM-step timing line normalized (2026-10-05, same round)

**Found by the first post-§11 double run.** The two runs were byte-identical
to each other (run logs and replay `cmp`-clean), but the tracked research
discover log differed from the committed byte-stable copy in exactly one
line: `[research-agent] LLM step done: macro_plan_design (0.1s)` vs
`(0.0s)`. The per-step agent timing lines were a transient fragment the
§10 normalization did not cover; the §10 byte-stability spot-check had
passed only because both sampled regenerations happened to print the same
value.

**Fix.** `normalize_transient_paths()` gains a third rule —
`LLM step done: <step> (X.Ys)` → `LLM step done: <step> (<elapsed>s)` —
pinned by a new harness test (23 → **24**). Only the discover log files
carry raw subprocess output; the replay embeds summaries/header sets/counts
only, so the double-run replay equality was never threatened by this line.
Pins/counts updated: harness sha256 `e671f247…b390b`, harness-test sha256
`4401b2fa…e31b24`, harness tests **24**, target set **370**,
research discover **1253**. (§5 and §10 keep their historical numbers,
which were true when written.)
