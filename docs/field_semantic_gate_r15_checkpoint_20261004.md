# Field Semantic Gate — Round-15 (G2a) Checkpoint (2026-10-04)

Round 15 (G2a) makes the **minimal ingestion repair** diagnosed in r14 —
validated folio recognition — plus structured abstention diagnostics, a
**frozen-rule eligibility pre-screen** for the W1/W2 candidates, and a
**design-only** SI grouping-boundary specification. No model is called, no
conventions/protocol resource changes, no v1 expansion. Exactly two existing
source files change, both in the PDF ingestion layer:

- `reaserch_agent/route_pdf_source.py` — adds `_validated_folio_lines`
  (three-factor folio recognition), `_read_pdf_blocks_with_report` (the
  existing reader plus a structured report), and an optional culprit sink on
  `_ordered_page_lines`. `_read_pdf_blocks` keeps its exact signature as a
  thin wrapper.
- `reaserch_agent/route_pdf_groups.py` — adds an optional `detail` field
  (default `""`) to `PdfGroupEnumerationDiagnosticV1`, filled with the JSON
  abstention record on parse failure. Every `reason_code` string is
  byte-identical; no existing assertion is rewritten (the exact-equality
  assertion on `["pdf_column_layout_ambiguous"]` in test_route_pdf_groups.py
  passes unchanged).

Artifacts: runner `result/operation-structure-20260928/local-revision-r15.py`;
replay `local-revision-r15-replay.json` (sha256
`sha256_655e24ae335b0b7a42e151ea0725943c2d5b4931ac49bbe06078845e53f8b2a6` over
UTF-8 bytes; two consecutive in-process builds byte-identical **and** two
separate process runs byte-identical, `cmp` clean); audit
`local-revision-r15-audit.json`; run logs `local-revision-r15-run1/2.log`.
The runner consumes zero tokens.

## 1. Folio repair design and location

A printed page number (folio) is page furniture only when **three factors
hold together** (`route_pdf_source.py::_validated_folio_lines`):

1. **Standalone folio form** — a short line fullmatching
   `[A-Za-z]{0,2}\s*[0-9]{1,4}` in the bottom band (`y0 > 0.90h`);
2. **Cross-page position consistency** — one uniform alphabetic prefix, y0
   within 4 pt of the median, **at most one candidate per page**, and at
   least three candidate pages;
3. **Page-order increment** — the numbers advance exactly with the page
   indices (gaps allowed; offset constant).

Nothing is deleted by digit count, font size, or a fixed page position
alone; the historical fast path (bottom margin `>0.94h` + `\d{3,}`) and the
repeated-margin-text rule are untouched. No page is removed, no PDF byte is
modified, nothing is stitched across missing regions.

Why the trigger was what it was: Wu SI pages 75/76/77 carry centered
`S 75`/`S 76`/`S 77` folio lines at y0=729.9 (0.922h) — below the legacy
0.94h margin zone and not pure-digit, so they survived the old furniture
rules and hit the center-middle ambiguity check inside
`_ordered_page_lines`. The whole-document census shows the real pattern:
**79** folio lines `S 2`..`S 80` at y0=729.9 with zero increment mismatches
— a textbook three-factor pass.

## 2. Parse results after the repair

| document | before (r14) | after (r15) |
| --- | --- | --- |
| Wu SI | `pdf_column_layout_ambiguous` | **parses: 1041 blocks**, 79 folios stripped; enumeration reaches `experimental_section_missing`, 0 groups |
| Wu main | `pdf_column_layout_ambiguous` | unchanged — culprit now recorded: p10, text `36`, x0=294.6, x1=299.9, y0=648.0, page_width=595.3 |
| A01 control | parses | **byte-identical**: 1289 blocks, block-list sha256 `911a6a29…06052f`, 0 folios stripped |

- **S14 evidence preserved**: the W1 sentence binds verbatim through the
  pipeline's own quote binder — needle
  `the precipitated Ni-Mo-O powder was collected, dried, and then reduced`
  at `pdf:p14:b9-p14:b10`, full sentence at `pdf:p14:b9-p14:b11` (the
  `Ni-`/`Mo-O` line seam is joined by the binder's existing
  `_SEAM_JOINERS` semantics; no character is repaired).
- **SI next layer**: `experimental_section_missing` — the SI is built from
  figure-caption paragraphs and has no Methods-style heading set; this is
  this round's allowed endpoint (0 groups).
- **Main-text abstention is intentional**: the p10 `36` is a chart tick at
  0.819h, inside no folio band, never a candidate; the two-column chart
  layout keeps abstaining. (Main-text chart-tick handling is a separately
  scheduled task, explicitly out of scope here.)
- **A01 protection**: the A01 page-4 bottom band carries *three* folio-form
  candidates on one page; the at-most-one-per-page guard rejects them and
  the three-page quorum is unmet, so nothing is stripped — the parse is
  byte-identical.

## 3. Abstention diagnostics refinement

Abstention now records the triggering page, text, and coordinates instead of
only a document-level reason string. The `reason_code` strings are stable;
the structured record rides the new `detail` field (JSON) on the enumeration
diagnostic, e.g. Wu main:

```json
{"reason": "pdf_column_layout_ambiguous", "culprits": [
  {"page": 10, "text": "36", "x0": 294.6, "x1": 299.9, "y0": 648.0,
   "page_width": 595.3}]}
```

All three ambiguity trigger kinds (center-middle line, barrier spacing,
barrier-adjacent column line) report their culprit line(s).

## 4. Pool census — with corrected framing

| candidate | reasons (before r15) | reasons (after r15) |
| --- | --- | --- |
| wu2025-main | pdf_column_layout_ambiguous | pdf_column_layout_ambiguous |
| wu2025-si | pdf_column_layout_ambiguous | **experimental_section_missing** |
| zhang2017-main | pdf_column_layout_ambiguous | pdf_column_layout_ambiguous |
| zhang2017-si | experimental_section_missing | experimental_section_missing |
| chemkb-kion | pdf_column_layout_ambiguous | pdf_column_layout_ambiguous |
| chemkb-pba-hosts | pdf_source_too_large | pdf_source_too_large |
| chemkb-nife-pba | pdf_column_layout_ambiguous | pdf_column_layout_ambiguous |
| upload-upl_1e79 | pdf_column_layout_ambiguous | pdf_column_layout_ambiguous |

Framing: **before = 6 layout + 1 missing-section + 1 oversize; after = 5
layout + 2 missing-section + 1 oversize.** Only the Wu SI changed category.

## 5. Rule-eligibility pre-screen (frozen conventions.json, 11 rules, sha256
`39fb6e77…8267`)

Phrase-hit cells are computed live with the pipeline's own semantics
(casefolded substring over operation/intent blobs; a rule hits only when
operation **and** intent **and** a proven input state hold together). The
four flagged claims verify programmatically: W1's collection method is
unstated (no `centrifug`/`filtrat` in the sentence); DRYING_V1 requires a
proven wet-solid input (`allowed_input_states = [retained_wet_solid,
washed_wet_solid]`); `dried` matches no DRYING_V1 pattern (`dry`/`drying`
are not substrings of `dried`); **no reduction rule exists** (no
`reduc`/`hydrogen`/`还原` pattern anywhere in the table).

### W1 — powdered Ni-Mo nanoparticles (Wu SI, page S14, Supplementary Fig. S13
caption paragraph)

Chain: hydrothermal (120 °C, 12 h) → collect → dry → reduce (500 °C, H2/N2,
6 h). Source slice: "the precipitated Ni-Mo-O powder was collected, dried,
and then reduced …".

| step | state to prove | candidate rule | operation hit | intent hit | input-state evidence | continuity | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| collect | retained_wet_solid | CENTRIFUGE_COLLECT_PRECIPITATE_V1 | ✗ | ✗ | absent — "precipitated" asserts no suspension state; no operation word | would parent drying; unprovable | **ineligible** |
| dry | dry_solid | DRYING_V1 | ✗ ("dried" unrecognized) | ✗ | requires proven wet-solid input; collection unprovable | broken at step 1 | **ineligible** |
| reduce | phase change to Ni-Mo alloy | — (no such rule) | ✗ on all rules | ✗ on all rules | n/a | n/a | **ineligible** |

### W2 — powdered MoO2 nanorods (Wu SI, page S15, Supplementary Fig. S14
caption paragraph)

Chain: precipitate ("until a white precipitate was formed") → heat
(50 °C, 3 h) → collect by filtration and washing → hydrogen reduction
(500 °C, 2 h).

| step | state to prove | candidate rule | operation hit | intent hit | input-state evidence | continuity | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| precipitate | suspension/precipitate | — (no precipitation rule) | ✗ | ✗ | mixture is literally called "solution"; no "suspension" wording | would parent filtration | **ineligible** |
| heat | (no state token) | — (no heating/aging rule) | ✗ on all rules | ✗ | n/a | n/a | **ineligible** |
| filter/wash/collect | retained→washed wet solid | WASHING_V1 | **✓** ("wash" in "washing") | **✓** ("washing") | needs proven wet-solid input; FILTRATION_COLLECT_RETAINED_V1 hits operation ("filtrat") but **misses intent** ("collected by filtration" ≠ "collect the solid"/"collect retained"), and suspension is unproven | broken before washing | **ineligible** |
| reduce | phase change to MoO2 | — (no such rule) | ✗ on all rules | ✗ on all rules | n/a | n/a | **ineligible** |

**Overall verdict: W1 and W2 are both ineligible under the frozen v1 rule
table.** They are retained as ingestion samples; a rule-compatible source
must be found separately. Nothing was fabricated (no centrifugation/filtration
invented for W1), no source excerpt rewritten, no rule extended to force a
positive. The single phrase-level hit in either chain (W2 washing) is
recorded as exactly that — a phrase hit that still fails the input gate.

## 6. SI grouping-boundary design (recorded, NOT implemented)

Observed S14-region block sequence (real parse, all blocks font_size 12.0):

| locator | bold | caption flag | kind | text head |
| --- | --- | --- | --- | --- |
| p14:b1 | ✓ | ✗ | figure label | `Supplementary Fig.` |
| p14:b2–b4 | ✗ | ✗ | caption prose | `S13. SEM images of (a-b) the Ni-Mo-O precursor …` |
| p14:b5–b11 | ✗ | ✗ | synthesis paragraph | `To prepare powdered Ni-Mo nanoparticles, …` |

Boundary gap: the parser-owned caption predicate `_caption_text` matches
`^(figure|fig\.?|table|scheme)\s*s?\d+`; the SI split form (bold
`Supplementary Fig.` + separate `S13. …` prose) matches neither branch, so
SI captions are currently indistinguishable from prose (`caption_flag` False
on every S14-region block).

Design (three consumers share ONE parser-owned predicate by construction):

1. **Single predicate**: extend `_caption_text` in `route_pdf_source.py` so
   enumeration grouping (`_caption`/`_group_boundary`), quote binding
   (`caption_block_locators` from `_quote_caption_locators`), and source
   re-verification (`_group_range`) all see the same boundary; proposal
   metadata never supplies caption identity.
2. **Recognition rule (designed)**: a bold block fullmatching
   `Supplementary (Fig|Figure|Table|Scheme)\.?` immediately followed by a
   body block starting `S\d+.` marks both blocks — and their
   same-original-block continuations — as captions.
3. **Grouping consequence (designed)**: captions never open or close a
   group; a synthesis paragraph between captions is the groupable unit. The
   caption-boundary grouping mode deferred from r14 remains required before
   W1-class enumeration.
4. **Quote-binding consequence (designed)**: caption blocks stay non-quotable
   as anchors; the at-most-one-skipped-caption rule is unchanged — newly
   flagged SI captions would simply become skippable furniture inside a span.

Stays closed this round: no caption-recognition change is implemented (it
would alter block semantics on caption-flagged regions), caption quotation
is **not** opened, and the workflow's interception of incomplete sources is
unchanged.

## 7. Terminology corrections (r14 archive untouched)

1. **Pool census framing** — r14's "every candidate abstains at ingestion"
   framed all eight as layout problems; corrected: **6 layout + 1 missing
   experimental section + 1 oversize** (and after the folio fix: 5 + 2 + 1).
   Reflected in the r15 replay `pool_census.framing_corrected`.
2. **Element overlap** — "zero element overlap" between Ni–Mo and Ni–Fe is
   withdrawn: they are **different material systems sharing the Ni element**.
3. **W1 presupposition** — W1 is no longer treated as the prospective
   multi-hop positive; its eligibility is whatever the frozen-rule table
   says, and the table says **ineligible** (Section 5).
   `dag_consumption` stays unobserved and unclaimed.

## 8. Tests

New file `reaserch_agent/test_route_pdf_folio.py` (15 tests, all passing):

- three-factor positives: consistent `S n` folios stripped; bare-digit
  folios below the margin zone stripped (extension beyond the legacy rule);
- factor negatives: page-order jump rejected; mixed prefix rejected; quorum
  (<3 pages) rejected; two-candidates-on-one-page kept; single-page
  lookalike (`36`) kept; mid-page numeric body line survives;
- compatibility: legacy margin fast path (>0.94h + 3+ digits) unchanged;
- ambiguity discipline: a centered folio as the *only* trigger unblocks a
  two-column page; a genuine centered content line still abstains and is
  reported as the culprit; enumeration diagnostics carry the structured
  detail JSON;
- real-file anchors (skip-gated): Wu main abstains on the p10 `36` tick; Wu
  SI parses to 1041 blocks with 79 folios and both S14 needles bind; A01
  parse byte-identical (1289 blocks, pinned sha256, zero folios).

## 9. Regression

- **Target set 277** (`test_route_proof_dag`, `test_route_retained_object`,
  `test_route_retained_object_integration`, `test_route_convention_state_chain`,
  `test_route_protocol_reference`, `test_route_operation_precondition_diagnostic`,
  `test_route_pdf_source`, `test_route_pdf_groups`,
  `test_route_pdf_local_revision_merge`): **277 passed, 0 failed** (also
  re-run inside the r15 runner as a subprocess anchor).
- Route slice (48 `test_route*` modules incl. the new folio module):
  **756 passed, 0 failed**.
- Full-slice `unittest discover`: `reaserch_agent` **1217 tests** — failure
  set identical to the r14 set (baseline minus the known flaky
  `test_b1_bootstrap_generates_initial_outputs`), **zero new failures**;
  `chem_agent_contracts` **116 tests** — failure set identical to baseline.
- r13b archive-pinned runner re-executed as a subprocess: replay matches the
  r13 archive, double-run byte-identical, **ms7a.out stays BLOCKED**
  (`retained_object_mention_precedes_operation`).
- 3E diagnostics module: zero proof dependency (source audit, runner anchor).

## 10. Fixed constraints

r7–r14 archives untouched; `chem_agent_contracts/` untouched;
conventions.json byte-identical to the frozen sha256 (no v1 expansion);
3E diagnostics semantics untouched; zero tokens consumed by the runner; no
model called this round; Device not run; eight-question campaign not rerun;
ms7a.out cascade still BLOCKED; tracked modifications limited to the two
ingestion files (runner-enforced). Separate counters all zero: field proofs
0, group receipts 0, protocols admitted 0, published 0.

## 11. Design decisions (for the record)

1. **Three factors, never one.** The folio rule deliberately rejects every
   single-factor shortcut: digit count, font size, and fixed page position
   alone delete nothing. The Wu main `36` tick (one page, wrong band) and
   the A01 page-4 trio (one page, three candidates) are the two local proofs
   that the conservative path stays conservative.
2. **Reason codes frozen; detail rides a new field.** Contracts are frozen
   and existing tests assert exact reason lists, so the structured culprit
   record lives on a new optional `detail` field of the mutable
   enumeration-diagnostic dataclass. The frozen `RouteSourceVerificationV1`
   surface is untouched — its `reasons` stay exactly the code strings.
3. **`PDF_GROUP_PARSER_VERSION` deliberately not bumped.** Block semantics
   changed only for inputs that previously failed closed (no groups, no
   artifacts, nothing cached); every previously parseable input is asserted
   byte-identical (A01). Bumping the version string would falsify the pinned
   r13/r13b replay bytes without invalidating any real cache.
4. **Honest endpoint.** The SI now parses and stops at
   `experimental_section_missing`; W1/W2 are ineligible under the frozen
   rules. Both facts are recorded as the round's result rather than worked
   around — the remaining gaps (SI caption-boundary grouping, per-page
   abstention for figure-heavy pages, a reduction/heating rule family) are
   next-round decisions, not this round's scope.

## 12. Open items

- Main-text chart-tick layouts (Wu main p10 class) — separately scheduled.
- SI caption-boundary grouping mode (design in Section 6).
- A reduction/thermal-treatment rule family would be a v1-table decision;
  nothing is pre-committed.
- Real model generation remains blocked on the quota window (r14 record
  stands).
