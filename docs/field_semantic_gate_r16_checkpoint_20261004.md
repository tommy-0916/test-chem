# Field Semantic Gate — Round-16 (G2a follow-up) Checkpoint (2026-10-04)

Round 16 fixes the **folio misdelete** found in the r15 rule, corrects the
**parser-version** posture with empirical evidence, and supplements the
**caption-boundary design** with a verified block distribution. No model is
called, no conventions/protocol resource changes, no v1 expansion, no
caption release, no SI grouping. Exactly three tracked files change, all in
the PDF ingestion layer and its tests:

- `reaserch_agent/route_pdf_source.py` — validated folio recognition goes
  from three factors to **five**: the r15 rule deleted real experiment
  values; two new factors (horizontal anchor consistency, body isolation)
  close both reported shapes. Doubtful context keeps the line — keeping
  content is always the safe failure.
- `reaserch_agent/route_pdf_groups.py` — `PDF_GROUP_PARSER_VERSION` bumped
  to `route_pdf_groups/v2` (rationale comment at the constant).
- `reaserch_agent/test_route_pdf_folio.py` — new `MisdeleteRegressionTest`
  class (5 tests); the module now has 20 tests.

Artifacts: runner `result/operation-structure-20260928/local-revision-r16.py`;
replay `local-revision-r16-replay.json` (sha256
`sha256_6b36a10f564c2d77979fe840ed39686480aacd8e45132db287f842f0d9eae095` over
UTF-8 bytes; two consecutive in-process builds byte-identical **and** two
separate process runs byte-identical, `cmp` clean); audit
`local-revision-r16-audit.json`; run logs `local-revision-r16-run1/2.log`.
The runner consumes zero tokens.

## 1. Misdelete root cause and the five-factor design

**Root cause.** The r15 three-factor rule (folio form in the bottom band +
cross-page position consistency + page-order lockstep) validated *any* bare
incrementing number printed alone in the bottom band as a folio. A measured
value — e.g. a final-pH reading printed under its label on every page —
satisfied all three factors and was deleted from the parse.

**Fix location.** `route_pdf_source.py::_validated_folio_lines` — two new
factors join the existing three; all five must hold together, never a
subset:

1. **Standalone folio form** — fullmatch `[A-Za-z]{0,2}\s*[0-9]{1,4}` in the
   bottom band (`y0 > 0.90h`); at most one candidate per page; quorum of
   three pages;
2. **Uniform prefix + lockstep** — one alphabetic prefix form; numbers
   advance exactly with page indices;
3. **Vertical anchor** — y0 within 4 pt of the median;
4. **Horizontal anchor (new)** — the candidate line's *center x* agrees
   within 4 pt of the median across pages (`_FOLIO_X_TOLERANCE = 4.0`,
   same tolerance style as y0);
5. **Body isolation (new)** — the candidate is the only non-empty text line
   of its original PDF block (a value sharing a block with its label is
   body text, never a folio).

Nothing is hard-coded to the probe coordinates, page numbers, or the Wu
documents; no page is removed; no PDF byte is modified. When context is
doubtful the line is kept.

Each new factor is **load-bearing**: probe A passes every factor except the
horizontal anchor (y0 identical, prefix uniform, lockstep, quorum, one
candidate per page, sole line of its block — only x drifts 70/280/460);
probe B passes every factor except body isolation (x centers identical, but
label and value share one original block).

## 2. Probe before/after (in-memory, 612×792, deterministic)

| probe | pre-round code (89707da) | current code |
| --- | --- | --- |
| A: `Final pH:` label at (70,730); values 11/12/13 at baseline 747, x=70/280/460 per page | **stripped `['11','12','13']`**, 6 blocks, values gone | **values kept `['11','12','13']`**, 9 blocks, 0 stripped |
| B: one `insert_text` writes `Final pH:\n<value>` (label+value share one original block) | **stripped `['11','12','13']`**, 6 blocks, values gone | **values kept `['11','12','13']`**, 9 blocks, 0 stripped |
| control: probe-A shape with a consistent x anchor | (stripped, as a folio pattern) | still stripped `['11','12','13']`, 3 blocks |

The "before" column is produced by loading the pre-round
`route_pdf_source.py` via `git show 89707da` into a package-prefixed module
(`reaserch_agent._route_pdf_source_r15`) and running the same probe bytes —
the runner pins both directions. The control guards against
over-correction: a genuine x-consistent folio pattern still strips.

## 3. Parser-version determination (empirical, per the line-50 convention)

`route_pdf_groups.py` carries the convention: *bump when this enumerator or
route_pdf_source changes group block semantics*. r15 declined to bump on the
grounds that "no previously parseable document changes" and that bumping
"would falsify the pinned r13/r13b replay bytes". Both grounds are corrected
this round:

**Method.** The pre-r15 parser is loaded from `git show c429443` and five
synthetic inputs are parsed under both parsers (`_read_pdf_blocks`):

| case | pre-r15 blocks | current blocks | identical? |
| --- | --- | --- | --- |
| `bare_digits_centered` (`1`..`4` centered at baseline 747) | 8 | 4 | **DIFF** |
| `s_prefix_centered` (`S 1`..`S 4` centered) | 8 | 4 | **DIFF** |
| `no_folio` (body text only) | 4 | 4 | same |
| `probe_a_shape` | 9 | 9 | same (both keep the values) |
| `probe_b_shape` | 9 | 9 | same |

**Conclusion.** Block semantics *did* change for previously parseable
folio-bearing inputs — the change predates r16 (the r15 rule already
stripped these), but the version record must reflect it:
`PDF_GROUP_PARSER_VERSION = "route_pdf_groups/v2"`.

**r13/r13b impact — verified none.** The four r13/r13b archive files
(`local-revision-r13{,b}-{replay,audit}.json`) contain **zero** occurrences
of `route_pdf_groups/v` (grep-verified, re-asserted inside the r16 runner),
and the r13/r13b runner sources never reference the parser-version constant.
The bump therefore falsifies no pinned replay bytes; no pin is rewritten,
no "expected-difference" mode is needed, and historical archives keep their
original bytes. The r13b runner is additionally re-executed as a subprocess
inside the r16 runner: replay matches the r13 archive, double-run
byte-identical, **ms7a.out stays BLOCKED**.

## 4. Wording correction (r15 archive untouched)

The r15 claim *"ingestion block semantics changed ONLY for inputs that
previously failed closed (no previously parseable document changes)"* is
**withdrawn as a blanket claim**. What holds is the asserted anchor: **A01
parses byte-identically** (1289 blocks, block-list sha256
`911a6a29…06052f`, 0 folios stripped). Other previously parseable
folio-bearing inputs change by design (shorter block sequences) — exactly
the semantic change the v2 bump records. The correction rides the r16
replay (`wording_corrections` + `terminology_corrections`); the r15 archive
itself is byte-untouched.

## 5. Caption-boundary design supplement (recorded, NOT implemented)

**Verified distribution** (re-derived inside the runner from the Wu SI
bytes). On SI pages S14 (physical index 13) and S15 (index 14), **every
line of the caption paragraph is its own original PyMuPDF block**:

- S14: caption head `Supplementary Fig. S13. SEM images of …` = original
  block 4 (one line); continuations `alloy nanoparticles. Insets …` and
  `Mo alloy nanoparticles, respectively.` = original blocks 5 and 6 (one
  line each); the synthesis paragraph `To prepare powdered Ni-Mo …` spans
  original blocks 8–14 (seven blocks, one line each).
- S15: same shape — head = block 4, continuations = blocks 5/6, synthesis
  paragraph = blocks 8–15 (eight blocks).
- Final-sequence mapping (page 14): `b1`+`b2` are the bold split of the head
  line (same original block 4), `b3` ← block 5, `b4` ← block 6, `b5..b11` ←
  blocks 8–14; page 15: `b5..b12` ← blocks 8–15. `caption_flag` is False on
  the whole region (r15 finding stands). The acceptance feedback's block
  numbers ("b1/b2 from block 3, b3/b4 from block 4/5") differ by indexing
  convention; the structural claim is confirmed as stated: continuation
  lines live in **different** original blocks from the head and from each
  other.

**Impact on the r15 draft phrase.** The r15 design drafted "same-original-
block continuations" as the caption-continuation rule. Under the verified
distribution that phrase would mark only final `b1`/`b2` as caption and miss
**every** continuation (`b3`, `b4`, and the whole synthesis paragraph) — the
draft is corrected here, in the design record only.

**Continuation and termination design (recorded).**

- Continuation binds by **vertical reading order across original blocks**:
  contiguous y progression, consistent font size, shared left edge — never
  by shared original block.
- Termination: the caption region ends at the first line that breaks the
  continuation pattern. On the verified pages the synthesis paragraph is
  visually indistinguishable from caption prose — which is exactly why
  recognition stays unimplemented this round.
- Quote binding keeps its **one-hop limit**: caption blocks stay
  non-quotable anchors and the binder may skip at most one block inside a
  span; it may not jump an entire caption region.

**Stays closed:** no caption-recognition change, no caption quotation, no SI
grouping implementation, no change to the workflow interception of
incomplete sources.

## 6. Document anchors held

- **A01 control**: byte-identical (1289 blocks, pinned sha256, 0 folios).
- **Wu SI**: 1041 blocks, 79 validated folios (`S 2`..`S 80`); the W1
  evidence sentence binds verbatim — needle at `pdf:p14:b9-p14:b10`, full
  sentence at `pdf:p14:b9-p14:b11` (both locators now pinned in the runner;
  the actual parse starts at b9).
- **Wu main**: still abstains `pdf_column_layout_ambiguous` on the p10
  chart tick `36` (0.819h, outside the folio band, never a candidate), with
  structured culprit detail.
- **Pool census (8 candidates)**: row-by-row identical to the r15 post-fix
  census (5 layout + 2 missing-section + 1 oversize, 0 groups); the runner
  cross-checks against the archived r15 replay.
- **Wu attested layer**: both signed documents verify; attested enumeration
  reaches the same endpoints with the structured culprit detail.

## 7. Rule-eligibility pre-screen (carried, frozen conventions)

The conventions resource is byte-identical to the frozen baseline (sha256
`39fb6e77…e8267b`, 11 rules), so the r15 pre-screen re-verifies unchanged:
**W1 and W2 are both ineligible** under the frozen v1 rule table (no
collection operation word, DRYING_V1 input gate unprovable, no
reduction/heating rule family; the only phrase-level hit — W2 `wash` on
WASHING_V1 — fails the input-state gate). This remains a **negative
eligibility pre-screen**, not a pass: the chains are retained as ingestion
samples and a rule-compatible source must be found separately (不补造离心/
过滤，不改写源摘录，不扩规则保正例).

## 8. Tests

`reaserch_agent/test_route_pdf_folio.py` now has **20 tests, all passing**
(the 15 r15 tests unchanged, plus 5 new):

- `test_cross_page_x_drift_keeps_values` — probe-A regression; the
  horizontal anchor alone intercepts;
- `test_shared_original_block_keeps_values` — probe-B regression; body
  isolation alone intercepts;
- `test_x_consistent_numeric_set_still_stripped` — over-correction control;
- `test_prer15_block_semantics_comparison` — loads the pre-r15 parser from
  `git show c429443` and pins the five-sample old/new table (skips only if
  git or the commit is genuinely unavailable);
- `test_parser_version_bumped_to_v2` — pins `PDF_GROUP_PARSER_VERSION` at
  `route_pdf_groups/v2`.

## 9. Regression

- **Target set 339** (13 modules: the r15 277 — `test_route_proof_dag`,
  `test_route_retained_object`, `test_route_retained_object_integration`,
  `test_route_convention_state_chain`, `test_route_protocol_reference`,
  `test_route_operation_precondition_diagnostic`, `test_route_pdf_source`,
  `test_route_pdf_groups`, `test_route_pdf_local_revision_merge` — plus
  `test_route_pdf_folio` 20, `test_route_pdf_group_extraction` 19,
  `test_route_pdf_group_proposals` 14, `test_route_real_a01_material_fragment`
  9): **339 passed, 0 failed** (also re-run inside the r16 runner as a
  subprocess anchor).
- Route slice (48 `test_route*` modules): **761 passed, 0 failed** (756 +
  the 5 new folio tests).
- Full-slice `unittest discover`: `reaserch_agent` **1222 tests** (1217 +
  5) — failure set **byte-identical to the r15 set** (same 29
  FAIL/ERROR headers, normalized; zero new, zero missing);
  `chem_agent_contracts` **116 tests** — the single failure
  (`test_legacy_bound_package_hash_is_unchanged`) is identical to the r15
  baseline.
- r13b archive-pinned runner re-executed as a subprocess: replay matches the
  r13 archive, double-run byte-identical, **ms7a.out stays BLOCKED**
  (`retained_object_mention_precedes_operation`).
- 3E diagnostics module: zero proof dependency (source audit, runner
  anchor).
- Runner determinism: two consecutive in-process builds byte-identical;
  two separate process runs byte-identical (`cmp` clean on both replay and
  audit); replay sha256
  `sha256_6b36a10f564c2d77979fe840ed39686480aacd8e45132db287f842f0d9eae095`.

## 10. Fixed constraints

r7–r15 archives untouched; `chem_agent_contracts/` untouched;
conventions.json byte-identical to the frozen sha256 (no v1 expansion);
3E diagnostics semantics untouched; zero tokens consumed by the runner; no
model started; Device not run; ms7a.out cascade stays BLOCKED; tracked
modifications limited to the three named files.

## 11. Design decisions (for the record)

1. **Five factors, never a subset — and doubt keeps content.** Both new
   factors are geometric/structural, not lexical: no digit count, no font
   size, no fixed position, no hard-coded coordinates or page numbers. The
   probes prove each new factor is load-bearing in its own shape, and the
   control proves a genuine folio pattern still strips.
2. **Version bump on evidence, not on posture.** r15's no-bump rested on a
   blanket claim that was only ever anchored by A01, and on a second claim
   ("bumping would falsify the pinned r13/r13b replay bytes") that is
   factually wrong — the pins carry no version string. r16 pins the
   semantic difference empirically (two DIFF cases out of five), bumps to
   v2 per the line-50 convention, and leaves every archived byte untouched.
3. **Boundary supplement is design-only.** The verified distribution
   invalidates the r15 draft phrase, so the corrected
   continuation/termination design is recorded with its evidence — and
   nothing is implemented: caption quotation stays closed, SI grouping
   stays deferred.
4. **Honest endpoints unchanged.** W1/W2 remain ineligible under the frozen
   rules; the SI still stops at `experimental_section_missing`; the main
   paper still abstains at ingestion. No counter moved.

## 12. Open items

- Main-text chart-tick layouts (Wu main p10 class) — separately scheduled.
- SI caption-boundary grouping mode (corrected design in Section 5).
- A reduction/thermal-treatment rule family would be a v1-table decision;
  nothing is pre-committed.
- Real model generation remains blocked on the quota window (r14 record
  stands).
