# Field Semantic Gate — Round-14 Checkpoint (2026-10-04)

Round 14 is a **cross-paper real-input capability probe**, not a gate round.
The plan was: freeze the baseline, sign a second real paper (Wu 2025, Nature
Communications, DOI `10.1038/s41467-025-58320-5`, main text **and** Supporting
Information) into a fresh knowledge base, run real model generation, and then
replay the accepted layers (source evidence → model proposal → layer results →
DAG consumption → first blocker). The honest outcome is a **capability gap at
the PDF ingestion layer**: the signed sources verify, but the block extractor
abstains on the two-column Nature layout — as it does on **every** local
cross-paper candidate — so no experimental group is enumerated and nothing
downstream is reached. The model layer is independently unreachable this round
(weekly quota exhausted, HTTP 403 on both configured channels). No existing
source file was modified; no PASS of any kind is claimed.

Artifacts: runner `result/operation-structure-20260928/local-revision-r14.py`;
replay `local-revision-r14-replay.json` (sha256
`sha256_8431bca4bfd0621b9f265541f6d5493adaa52bda15fae7f7fbb824d2fb179e70` over
LF bytes; two consecutive in-process serializations byte-identical **and** two
full separate process runs byte-identical, `cmp` clean); audit
`local-revision-r14-audit.json` (LF sha256
`be2da0fd87f9c033f416e75166141c0f5c2c21588525c6cd077edf0b4a3c2553`). The
runner consumes zero tokens; it replays only local archived bytes and pinned
subprocess outcomes. Nothing in the replay or audit is model-generated.

## Frozen baseline

- HEAD `44a39d9b70b65f4daab462aa98fd588a2ff01a74` (G1 follow-up), branch
  `hyt_main`, remote `tommy`; no existing source file is modified this round.
- `chem_resources/chemistry_conventions/conventions.json` sha256
  `39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b`.
- Prompt-builder module `reaserch_agent/route_pdf_group_extraction.py`
  (`_build_prompt` at L303) sha256
  `24864d9af522666b15a1690b6c9ba829bebfa3047545d82adc3e3dff331486f7`.
- A01 control source digest `sha256_2e9435bccb…12c8` (unchanged; r13b pins it).
- Wu-2025 PDFs: main `0f1be588bb891a4e955e5133731295120818a84f00a3750e22cf8504d81f1819`,
  SI `cdd842d87e34a8dc70d87e5999e8665f5fe054e66a22f0908b72ea2953f365d4`.
- Model identity recorded as `k3` @ `https://api.kimi.com/coding/v1`; the API
  key is not recorded anywhere in the repo.

## Source evidence (W1, text-level)

Group candidate **W1** — powdered Ni-Mo nanoparticles — lives in the Wu SI,
page label **S14** (PDF page 14), in the Supplementary Fig. S13 caption
paragraph. Verbatim:

> "After the hydrothermal reaction, the precipitated Ni-Mo-O powder was
> collected, dried, and then reduced at 500 °C for 6 h under a mixed
> hydrogen-nitrogen flow to obtain Ni-Mo nanoparticles."

(recon text `si-search/pdf-recon/text/B01-Wu-2025-NatCommun-SI__cdd842d8.txt`
lines 162–168). Upstream the same caption chain carries the hydrothermal step
(6 mmol Ni(NO3)2·6H2O + 0.75 mmol (NH4)6Mo7O24·4H2O + 9 mmol CO(NH2)2, 60 mL
DI water, 120 °C 12 h), so the text-level chain is dissolve → autoclave →
collect/dry → reduce (phase change to Ni-Mo nanoparticles).
`source_evidence.sufficient_at_text_level: true`, but
`reachable_by_pipeline_extractor: false` — the evidence sentence is real and
located, and the pipeline never reads it. Pool-honesty statement: **no
candidate-pool source uses centrifugation**; retained-phase wording across the
pool is collection/filtration.

## KB and signature scheme (SI binding)

New KB at `result/wu2025-real-input-20261004/` (both PDFs stored by digest
under `kb/_pdf_sources/wu2025/`, one `registry/papers.jsonl` record with
`doi=10.1038/s41467-025-58320-5`, two attestations, `route-trust-config.json`,
publisher-identity evidence, connectivity record, honest inventory,
`build_kb.py`). Signing: `TrustedAcquisitionEventV1` →
`signed_trusted_acquisition_event_message_v1(key_id, event dict)` → Ed25519,
key_id `wu2025-r14-source-v1`, issuer `manual-source-review`; the **private
key lives outside the repo** and is never committed.

The attestation schema **natively supports SI**: `document_kind:
"supporting_information"` requires `parent_doi` (model validator) and
`identity_evidence_type` of `publisher_si_link` or `reviewed_identity`, with
verdict `si_verified`. The docstring's "SI excluded" wording concerns
text/markdown derivatives, not SI PDFs. Binding mechanics: after attestation
verification, `attested_route_sources` requires the PaperRegistry record's DOI
to equal the SI attestation's `parent_doi` and the PDF path to be registered;
a mismatch drops the source. Publisher provenance was re-verified on
2026-10-04: re-downloads from the publisher hashed **byte-identical** to the
local PDFs (main 2,248,077 bytes from nature.com; SI 7,684,073 bytes from
static-content MOESM1). Result: `attested_route_sources` returns **2 verified
documents** (primary_paper + supporting_information).

## Layer-by-layer result

| layer | status | detail |
| --- | --- | --- |
| attestation | verified | 2 documents; SI bound via parent_doi; registry DOI match enforced |
| group_enumeration | **abstained** | 0 groups; `pdf_column_layout_ambiguous` on both documents |
| proposal_extraction | not_reached | no group to propose from |
| local_revision | not_reached | — |
| association | not_reached | — |
| receipt | not_reached | — |
| model_proposal | not_reached | zero groups upstream AND quota exhausted downstream; nothing replayed or hand-built |

## First blocker: `pdf_column_layout_ambiguous` (pdf_ingestion)

`reaserch_agent/route_pdf_source.py::_ordered_page_lines` abstains when a page
is column-ambiguous (row centers within ±2 pt of the midline, a cross-column
barrier with ≤3 pt spacing, or column lines within ≤3 pt of the barrier), and
`_read_pdf_blocks` treats **one ambiguous page as aborting the whole
document**. The A01 main text is the only layout in the local corpus that
parses. Pool census (all 8 local cross-paper candidates, run inside the
replay):

| candidate | groups | reason |
| --- | --- | --- |
| wu2025-main | 0 | pdf_column_layout_ambiguous |
| wu2025-si | 0 | pdf_column_layout_ambiguous |
| zhang2017-main | 0 | pdf_column_layout_ambiguous |
| zhang2017-si | 0 | experimental_section_missing |
| chemkb-kion | 0 | pdf_column_layout_ambiguous |
| chemkb-pba-hosts | 0 | pdf_source_too_large (>8 MB) |
| chemkb-nife-pba | 0 | pdf_column_layout_ambiguous |
| upload-upl_1e79 | 0 | pdf_column_layout_ambiguous |

The fallback ladder (W1 → W2 → W3 → declare gap) collapses at the ingestion
layer for every rung, so the round is declared an honest gap round.

## Model layer

`k3` @ `https://api.kimi.com/coding/v1`, both wire formats tried (`chat`,
`codex_responses` direct): **HTTP 403, weekly (7-day) usage limit** — an
account-level quota exhaustion, not a wire bug. Raw records:
`result/wu2025-real-input-20261004/connectivity.json`. Real generation is
therefore not reached for two independent reasons (zero enumerated groups
upstream; quota downstream). No replayed proposal and no hand-built proposal
was substituted.

## DAG consumption: not observed (and not claimed)

The acceptance criterion — at least one field that flat/literal cannot prove,
proven by the multi-hop DAG and accepted into
`dag_proven_state_field_paths` — is **unobservable this round**: with zero
groups enumerated, no DAG is ever built. `dag_consumption.observed: false`,
`dag_proven_state_field_paths: []`. This is recorded as a gap, not a pass.

## Acceptance anchors (all reproduced inside the replay)

- **Source mutation**: the untampered control verifies; a byte-tampered SI is
  rejected with `source_digest_or_format_mismatch`.
- **Wrong binding**: an acquisition event carrying the wrong document digest
  is rejected (`source_digest_or_format_mismatch`); a registry DOI that does
  not match the SI attestation's `parent_doi` yields **0** admitted sources.
- **A01 regression**: the r13b runner is re-executed as a subprocess — two
  runs byte-identical, replay matches the archived r13 digests, and
  **ms7a.out stays BLOCKED** (`retained_object_mention_precedes_operation`).
- **3E guard**: the diagnostics module keeps zero proof dependency.

## Fixed-constraints audit

No existing source file modified; r7–r13b archives untouched;
`chem_agent_contracts/` untouched (imported only); diagnostics-module
semantics untouched; no v1 expansion; the r14 runner consumes zero tokens;
Device not run; the eight-question campaign not rerun; ms7a.out cascade still
BLOCKED. Separate counters, all zero this round: field proofs succeeded 0,
group-receipt pass 0, protocol admitted 0, published 0. Nothing here is an
A01-style PASS, a new model generation result, or a V2 publication gate.

## Regression

- Route slice (47 `test_route*` modules): **741 passed, 0 failed**.
- Full slice (`unittest discover`): `reaserch_agent` **1202 tests**, failure
  set equals `../baseline-research-fails.log` after
  `sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/'` normalization
  minus the known environment-state-dependent
  `test_b1_bootstrap_generates_initial_outputs` (passes standalone) — **zero
  new failures**. `chem_agent_contracts` **116 tests**, failure set identical
  to `../baseline-contracts-fails.log` (the single
  `test_legacy_bound_package_hash_is_unchanged`).
- No new tests added: this round introduces no new code path.

## Minimal fix suggestions (recorded, deliberately NOT implemented)

1. **pdf_ingestion**: teach `_ordered_page_lines` to resolve — or skip with a
   diagnostic row — ambiguous two-column pages instead of aborting the whole
   document; the smallest honest step is per-page abstention that preserves
   page order so figure-heavy pages do not kill method pages.
2. **si_section_recognition**: SI PDFs use figure-caption paragraphs, not
   Methods-style group headings; a caption-boundary grouping mode is required
   before W1-class groups can enumerate.
3. **model_quota**: rerun the real generation once the weekly quota window
   resets (no code change needed).

Implementing any of these is a next-round decision, not part of this
capability probe.

## Design decisions (for the record)

1. **Honest gap round over code change.** The brief allows stopping and
   reporting instead of modifying source to force a pass; with the entire
   candidate pool abstaining at ingestion, the truthful result is the gap
   itself, pinned by the census table above.
2. **Private key outside the repo.** The KB contains attestations, the trust
   config (public key only), evidence, and build script; the Ed25519 private
   key never enters the tree.
3. **No replayed or hand-built proposal.** With real generation unreachable,
   the model-proposal layer is recorded as `not_reached` with both causes,
   rather than substituting a canned proposal that would fake the capability
   being probed.
4. **Byte-identity convention** (carried from r13): "two runs byte-identical"
   compares UTF-8 serializations; replay/audit files are written with `\n`
   newlines and digests are computed over LF bytes (the Windows working tree
   shows CRLF under `core.autocrlf=true`; the git index stores LF).

## Open items

- Rerun real generation after the model quota window resets.
- Decide whether to implement the ingestion fix (per-page abstention) and the
  SI caption-boundary grouping mode; both are capability work, not field
  patches.
- W1 remains a located-but-unreachable group candidate; its text-level
  evidence is archived in the replay for the next round.
