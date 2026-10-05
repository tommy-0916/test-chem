# Field Semantic Gate — Round-17 (G2a follow-up-2) Checkpoint (2026-10-05)

Round 17 closes the **remaining folio misdelete shape** reported back from
the r16 acceptance (variant C), bumps the **parser version to v3** on fresh
empirical evidence, and records **three corrections** to the SI
caption-boundary design notes. No model is called, no
conventions/protocol resource changes, no v1 expansion, no caption release,
no SI grouping. Exactly three tracked files change, all in the PDF
ingestion layer and its tests:

- `reaserch_agent/route_pdf_source.py` — validated folio recognition goes
  from five factors to **six**: the new neighborhood body-text check
  rejects a candidate when another text line overlaps it horizontally and
  stands within one standard line height of it (scale derived from the
  candidate's own font metrics). `_PdfLine` now keeps the full glyph bbox
  (`y1`) for this. Doubtful context keeps the line — keeping content is
  always the safe failure.
- `reaserch_agent/route_pdf_groups.py` — `PDF_GROUP_PARSER_VERSION` bumped
  to `route_pdf_groups/v3` (rationale comment at the constant).
- `reaserch_agent/test_route_pdf_folio.py` — new
  `NeighborhoodBodyRegressionTest` class (7 tests); the pre-r15 comparison
  gains the probe-C case; the version assertion moves to v3; the module now
  has **27 tests**.

Artifacts: runner `result/operation-structure-20260928/local-revision-r17.py`;
replay `local-revision-r17-replay.json` (sha256
`sha256_7c69421db76ad5013073d38e526e57553b8f7a078af12ffe2f561ee91c22503e` over
UTF-8 bytes; two consecutive in-process builds byte-identical **and** two
separate process runs byte-identical, `cmp` clean); audit
`local-revision-r17-audit.json`; run logs `local-revision-r17-run1/2.log`
(identical). The runner consumes zero tokens.

## 1. Root cause and the six-factor design

**Root cause (variant C).** The r16 five-factor rule checked body isolation
only *inside* the candidate's original block. A measured value printed in
its own original block directly under its label in an **adjacent** block —
label `Final pH:` at (280,729), value at (280,747), glyph-box gap **4.26
pt** at fontsize 10 — passed all five factors (bottom band, one candidate
per page, quorum, uniform prefix, lockstep, y anchor, x anchor, sole line
of its block) and was deleted. Sole block occupancy does not prove
detachment from body text.

**Fix location.** `route_pdf_source.py::_validated_folio_lines` (new
`_has_body_neighbor` helper). All six factors must hold together, never a
subset; factors 1–5 are unchanged from r16:

6. **Body isolation across the neighborhood (new)** — no other text line on
   the page overlaps the candidate horizontally (`x` ranges intersect)
   while standing within **one standard line height** of it, above or
   below. The proximity scale is `1.2 ×` the candidate's own font size
   (`_FOLIO_BODY_GAP_FACTOR = 1.2`, the typographic single line height);
   touching or vertically intersecting glyph boxes count as zero gap. The
   check is bidirectional and font-metric-derived — no probe text, page
   coordinate, or hard-coded value participates.

A page whose unique candidate fails the check simply contributes no
candidate (same per-page eligibility semantics as the existing zero-or-two
candidate rule); below the three-page quorum nothing is stripped.

## 2. Probe before/after (in-memory, 612×792, deterministic)

| probe | pre-round code (491ec37) | current code |
| --- | --- | --- |
| C: label (280,729) + value (280,747), adjacent blocks, same x, gap ≈4.26 pt | **stripped `['11','12','13']`**, 6 blocks, values gone | **values kept**, 9 blocks, 0 stripped |
| C mirrored: value (280,729) + label (280,747) (check is bidirectional) | stripped, 6 blocks | **values kept**, 9 blocks |
| boundary below scale: baseline delta 25 pt → gap ≈11.3 pt < 12.0 pt | stripped, 6 blocks | **values kept**, 9 blocks |
| boundary above scale: baseline delta 26 pt → gap ≈12.3 pt > 12.0 pt | stripped, 6 blocks | still stripped, 6 blocks |
| non-overlapping label (x=70 vs value x=280, gap 4.26 pt) | stripped, 6 blocks | still stripped, 6 blocks (horizontal overlap is required) |
| SI-shaped: size-12 line ≈16.7 pt above centered size-10 folio | stripped, 6 blocks | still stripped, 6 blocks |
| r16 control (x-consistent bare values, no label) | stripped, 3 blocks | still stripped, 3 blocks |
| r16 probe A (x drift) / probe B (shared block) | values kept, 9 blocks | unchanged: values kept, 9 blocks |

The "before" column is produced by loading the pre-round
`route_pdf_source.py` via `git show 491ec37` into a package-prefixed module
and running the same probe bytes — the runner pins both directions. The new
factor is **load-bearing** in probe C: every r16 factor passes there. The
boundary pair shows the check is not full-rejection: isolated folios keep
stripping.

## 3. SI gap distribution (measured with the parser's own coordinates)

Every one of the **79 validated Wu SI folios** was measured against its
nearest horizontally-overlapping text line, replicating the parser's
extraction exactly (`get_text("dict", sort=True)`, same line filtering):

- **min gap 14.16 pt** (folio `S 78`, size 10.02; nearest neighbor the
  size-12 reference line `Electrolysis. Adv Mater.. 33, e2101425 (2021).`),
  **median 372.04 pt**, max 534.04 pt; smallest five: 14.16 / 25.08 /
  36.92 / 63.02 / 81.32 pt.
- The stripping threshold at the folio font size is `1.2 × 10.02 = 12.024
  pt`; **all 79 folios clear it** (tightest margin 1.18×). Probe C sits at
  4.26 pt — the two populations are separated by 3.32× at the minimum.
- Consequence: the SI strips **exactly the same 79 folios** under the new
  rule (`S 2`–`S 80`), and its block sequence is byte-identical under the
  pre-r17 and current parsers (dual-parse pinned in the runner).

## 4. Parser-version determination (empirical, per the line-50 convention)

**Method.** The pre-r17 parser is loaded from `git show 491ec37`; ten
synthetic inputs plus both real anchor documents are parsed under both
parsers and the block sequences compared:

| case | pre-r17 blocks | current blocks | identical? |
| --- | --- | --- | --- |
| `probe_c_shape` | 6 | 9 | **DIFF** |
| `label_below_value` | 6 | 9 | **DIFF** |
| `gap_just_below_scale` | 6 | 9 | **DIFF** |
| `gap_just_above_scale` | 6 | 6 | same |
| `non_overlapping_label` | 6 | 6 | same |
| `si_shaped_spacing` | 6 | 6 | same |
| `probe_a_shape` | 9 | 9 | same |
| `probe_b_shape` | 9 | 9 | same |
| `control_isolated` | 3 | 3 | same |
| `no_folio` | 4 | 4 | same |
| Wu SI (real) | 1041 | 1041 | **byte-identical** (sha256 `4f82f5a1…769b6e`) |
| A01 (real) | 1289 | 1289 | **byte-identical** (sha256 `911a6a29…06052f`) |

**Conclusion.** Block semantics changed for previously parseable inputs
whose bottom band carries label-adjacent incrementing values (stripped
under v2, kept under v3): `PDF_GROUP_PARSER_VERSION =
"route_pdf_groups/v3"`.

**r13/r13b impact — verified none.** The four r13/r13b archive files still
contain **zero** occurrences of `route_pdf_groups/v` (re-asserted inside
the r17 runner), and the r13/r13b runner sources never reference the
parser-version constant. No pin is rewritten; historical archives keep
their original bytes. The r13b runner is additionally re-executed as a
subprocess inside the r17 runner: replay matches the r13 archive,
double-run byte-identical, **ms7a.out stays BLOCKED**.

## 5. Document anchors held

- **A01 control**: byte-identical (1289 blocks, pinned sha256, 0 folios).
- **Wu SI**: 1041 blocks, 79 validated folios (`S 2`..`S 80`); the W1
  evidence sentence binds verbatim — needle at `pdf:p14:b9-p14:b10`, full
  sentence at `pdf:p14:b9-p14:b11` (both locators pinned in the runner).
- **Wu main**: still abstains `pdf_column_layout_ambiguous` on the p10
  chart tick `36` (0.819h, outside the folio band, never a candidate), with
  structured culprit detail.
- **Pool census (8 candidates)**: row-by-row identical to the r15 post-fix
  and r16 censuses (5 layout + 2 missing-section + 1 oversize, 0 groups);
  the runner cross-checks against both archived replays.
- **Wu attested layer**: both signed documents verify; attested enumeration
  reaches the same endpoints with the structured culprit detail.

## 6. SI design-note corrections (recorded, NOT implemented)

Three corrections to the r16 Section 5 design supplement (the r16
checkpoint itself is untouched and sha256-pinned in the r17 runner):

1. **Original-block numbering basis.** The r16 block numbers ("caption head
   = block 4, continuations 5/6, paragraph 8–14") came from the r16
   runner's internal probe, which enumerated `get_text("dict")` with the
   default **sort=False**, while the production parser uses **sort=True**.
   Both modes group the region into the same one-line blocks with the same
   line texts; only the enumeration indices differ because sort=True
   repositions the folio block (page S14: folio at document-order index 1
   vs vertical-order index 15), shifting the caption region down by one
   (production: head = block 3, continuations 4/5, paragraph 7–13; page
   S15: 7–14). It is **not** a different numbering origin — citations of
   production locators must use the sort=True numbering.
2. **Missed caption continuation scope.** The r16 wording said the r15
   "same-original-block continuations" draft would miss "b3, b4, and the
   whole synthesis paragraph". Corrected: only **b3/b4** are missed caption
   continuation lines (production numbering; original blocks 4/5); from
   **b5** on, the synthesis paragraph is body text that should stay
   non-caption anyway — keeping it non-caption is correct behavior, not a
   miss.
3. **Visual distinguishability withdrawn.** The r16 claim that the
   synthesis paragraph is "visually indistinguishable from caption prose"
   is **withdrawn**: measured boundary signals exist on both verified pages
   — a **43.37 pt vertical gap** between the caption end and the paragraph
   start (vs 3.97–6.63 pt inside both regions) and a **24.02 pt first-line
   indent** (x0 114.02 vs 90.00). These are recorded as **to-be-verified
   boundary signals**, not as an implemented rule: caption recognition
   stays unimplemented, caption quotation stays closed, and the binder's
   at-most-one-skipped-block hop limit is unchanged.

## 7. Rule-eligibility pre-screen (carried, frozen conventions)

The conventions resource is byte-identical to the frozen baseline (sha256
`39fb6e77…e8267b`, 11 rules), so the r15/r16 pre-screen re-verifies
unchanged: **W1 and W2 are both ineligible** under the frozen v1 rule
table. This remains a **negative eligibility pre-screen**, not a pass
(不补造离心/过滤，不改写源摘录，不扩规则保正例); the full step-level table
rides the r16 replay.

## 8. Tests

`reaserch_agent/test_route_pdf_folio.py` now has **27 tests, all passing**
(the r15 15 and r16 5 unchanged in outcome, plus 7 new):

- `test_adjacent_block_label_keeps_values` — probe-C regression; the
  neighborhood check alone intercepts;
- `test_label_below_value_keeps_values` — the check is bidirectional;
- `test_gap_just_below_line_scale_keeps_values` /
  `test_gap_just_above_line_scale_strips` — both sides of the 1.2 em scale
  boundary (gaps ≈11.3 pt vs ≈12.3 pt at fontsize 10);
- `test_non_overlapping_label_does_not_block` — horizontal overlap is
  required (specificity guard);
- `test_real_folio_spacing_still_stripped` — positive control in the Wu
  SI's own geometry (size-12 line ≈16.7 pt above a centered size-10 folio);
- `test_prer17_block_semantics_comparison` — loads the pre-r17 parser from
  `git show 491ec37` and pins the five-shape old/new table (skips only if
  git or the commit is genuinely unavailable);
- the pre-r15 comparison gains `probe_c_shape` (the pre-r15 parser
  pre-dates folio stripping, so it keeps the values like the r17 parser
  does — the misdelete existed only in the r15/r16 window);
- `test_parser_version_bumped_to_v3` — pins `PDF_GROUP_PARSER_VERSION` at
  `route_pdf_groups/v3`.

## 9. Regression

- **Target set 346** (13 modules: the r16 339 + the 7 new folio tests):
  **346 passed, 0 failed** (re-run inside the r17 runner as a subprocess
  anchor; the folio module separately asserted at 27).
- Full-slice `unittest discover`: `reaserch_agent` **1229 tests** (1222 +
  7) — failure set has **29** FAIL/ERROR headers, i.e. the r16 set
  unchanged: normalized diff against the 30-header baseline shows exactly
  one delta, `test_b1_bootstrap_generates_initial_outputs`, which passes in
  this workspace (zero new, zero otherwise missing);
  `chem_agent_contracts` **116 tests** — the single failure
  (`test_legacy_bound_package_hash_is_unchanged`) is byte-identical to the
  baseline (normalized diff empty).
- r13b archive-pinned runner re-executed as a subprocess: replay matches the
  r13 archive, double-run byte-identical, **ms7a.out stays BLOCKED**
  (`retained_object_mention_precedes_operation`).
- 3E diagnostics module: zero proof dependency (source audit, runner
  anchor).
- Runner determinism: two consecutive in-process builds byte-identical;
  two separate process runs byte-identical (`cmp` clean on both replay and
  run logs); replay sha256
  `sha256_7c69421db76ad5013073d38e526e57553b8f7a078af12ffe2f561ee91c22503e`.

## 10. Fixed constraints

r7–r16 archives untouched (the r16 checkpoint is sha256-pinned inside the
r17 runner); `chem_agent_contracts/` untouched; conventions.json
byte-identical to the frozen sha256 (no v1 expansion); 3E diagnostics
semantics untouched (`diagnostics_only`, zero tokens);
protocol-definition/v1 not expanded; A01 ms7a.out and its cascade stay
BLOCKED; zero tokens consumed by the runner; no model started; Device not
run; tracked modifications limited to the three named files.

## 11. Design decisions (for the record)

1. **Six factors, never a subset — and doubt keeps content.** The new
   factor is geometric (horizontal overlap × font-metric proximity), not
   lexical: no digit count, no fixed position, no hard-coded text,
   coordinate, or document. The scale `1.2 ×` candidate font size is the
   standard single line height; it sits 2.8× above probe C's 4.26 pt gap
   and 1.18× below the SI's tightest 14.16 pt gap, with the safe-failure
   direction (keep content) on any doubt.
2. **Per-page eligibility, unchanged semantics.** A page whose candidate
   has a body neighbor contributes no candidate — exactly the existing
   semantics for pages with zero or two candidates — so cross-page
   consistency and quorum logic are untouched.
3. **Version bump on evidence, again.** v3 is pinned by a ten-shape
   synthetic table plus byte-identical dual parses of both real anchor
   documents; the r13/r13b archives pin no version string, so the bump
   falsifies nothing.
4. **Corrections are design-only.** The caption-boundary corrections change
   no code: caption recognition and SI grouping stay unimplemented, and
   the recorded boundary signals (43.37 pt gap, 24.02 pt indent) are
   marked to-be-verified, not promoted to a rule.

## 12. Open items

- Main-text chart-tick layouts (Wu main p10 class) — separately scheduled.
- SI caption-boundary grouping mode (corrected design in Section 6; the
  whitespace/indent boundary signals await verification across more
  documents before any implementation).
- A reduction/thermal-treatment rule family would be a v1-table decision;
  nothing is pre-committed.
- Real model generation remains blocked on the quota window (r14 record
  stands).
