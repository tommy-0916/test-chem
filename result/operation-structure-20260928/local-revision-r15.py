"""Round-15 (G2a): SI folio recognition minimal fix + abstention diagnostics
+ frozen-rule eligibility pre-screen for W1/W2.  No model is called.

Scope of the code change (exactly two existing files, both in the PDF
ingestion layer):

- ``reaserch_agent/route_pdf_source.py`` gains *validated folio
  recognition*.  A printed page number is page furniture only when three
  factors hold together: (1) a standalone folio-shaped short line
  (``[A-Za-z]{0,2}\\s*[0-9]{1,4}``) in the bottom band (y0 > 0.90h); (2) a
  consistent vertical position across pages (median +/- 4 pt) with a uniform
  alphabetic prefix; (3) numbers advancing in lockstep with the page order,
  with at most one candidate per page and at least three candidate pages.
  Nothing is deleted by digit count, font size, or a fixed page position
  alone; the historical fast path (bottom margin >0.94h + 3+ digits, and the
  repeated-margin-text rule) is unchanged.  No page is removed, no PDF byte
  is modified, and nothing is stitched across missing regions.
- ``reaserch_agent/route_pdf_groups.py`` carries a new ``detail`` field on
  ``PdfGroupEnumerationDiagnosticV1`` (default ""), filled with a JSON
  object recording the abstention culprit (page, text, x0/x1/y0, page
  width).  ``reason_code`` strings are unchanged; no existing assertion is
  rewritten.

What this runner establishes, deterministically and token-free:

1. Baseline freeze: pre-round HEAD c429443, conventions.json sha256 (frozen,
   unchanged), prompt-builder sha256 (unchanged), A01 source digest, Wu-2025
   PDF digests, and the sha256 of the two changed source files.  Any drift
   fails the runner.
2. A01 parse preservation: the A01 control PDF parses byte-identically
   (1289 blocks, pinned block-list sha256; zero folios stripped -- its only
   bottom-band candidates are a three-candidate single page, rejected by the
   at-most-one-per-page guard).
3. Wu SI now parses: 1041 blocks, 79 validated folio lines stripped
   ("S 2".."S 80"); the W1 evidence sentence on page S14 binds verbatim via
   the pipeline's own quote binder (hyphen seam semantics); enumeration now
   reaches ``experimental_section_missing`` (0 groups) -- the SI has no
   Methods-style section, which is this round's allowed endpoint.
4. Wu main still abstains: the p10 chart tick "36" (0.819h, outside the
   folio band, never a candidate) still triggers
   ``pdf_column_layout_ambiguous``, now with structured culprit detail.
5. Pool census before/after (before = r14 archived replay): corrected
   framing -- 6 layout + 1 missing-section + 1 oversize before; 5 layout +
   2 missing-section + 1 oversize after (Wu SI moved from layout-abstention
   to section-missing).
6. Rule eligibility pre-screen (frozen conventions.json, 11 rules): W1 and
   W2 chains are scored step by step with the pipeline's own phrase-match
   semantics (casefolded substring; SPLIT_V1 tokenizer irrelevant here).
7. Anchors: r13b archive-pinned subprocess re-run (ms7a.out stays BLOCKED),
   3E diagnostics zero-proof-dependency audit, Wu attestation re-verify,
   and the new folio test module executed as a subprocess.

Terminology corrections recorded this round (r14 archive itself untouched):
the r14 pool census framing is corrected to "6 layout + 1 missing-section +
1 oversize"; Ni-Mo vs Ni-Fe share the Ni element ("zero element overlap" is
withdrawn); W1 is NOT presupposed a multi-hop positive -- its eligibility is
whatever the frozen-rule table says (it says: ineligible).

The runner is deterministic: two in-memory builds are byte-identical; all
inputs are local bytes.  Zero tokens are consumed by this runner.
"""
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "result" / "operation-structure-20260928"
WU = ROOT / "result" / "wu2025-real-input-20261004"
R14_REPLAY = OUT_DIR / "local-revision-r14-replay.json"

PRE_ROUND_HEAD = "c42944306d4cb6c2e6b0a20e909a40c812db6afc"
CONVENTIONS_SHA256 = (
    "39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b"
)
PROMPT_BUILDER_FILE = "reaserch_agent/route_pdf_group_extraction.py"
PROMPT_BUILDER_SHA256 = (
    "24864d9af522666b15a1690b6c9ba829bebfa3047545d82adc3e3dff331486f7"
)
CHANGED_SOURCE_FILES = {
    "reaserch_agent/route_pdf_source.py": (
        "7a919730d8ce031eb3f32ddd46b96e0c667347d2e6514503798824b0a1c0878c"
    ),
    "reaserch_agent/route_pdf_groups.py": (
        "3e21674524b0733c7ab757d22bf61c96a588a82eedd73b804fb96838f0f34156"
    ),
}
NEW_TEST_FILE = "reaserch_agent/test_route_pdf_folio.py"
NEW_TEST_SHA256 = (
    "7c2d6f9bd816ee8d2e15df495ca4e37b9f97920ea2df266eaf3ebe25366bf50d"
)
A01_PDF = "result/a01-evidence-audit-20260927/huang-2023-institutional-copy.pdf"
A01_SOURCE_DIGEST = (
    "sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8"
)
A01_BLOCK_COUNT = 1289
A01_BLOCKS_SHA256 = (
    "911a6a29246b93cd6138bd63fcc8c2b0393de61c6ada7748ae1066b08d06052f"
)
WU_MAIN_PDF = "backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-main.pdf"
WU_SI_PDF = "backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-SI.pdf"
WU_MAIN_SHA256 = "0f1be588bb891a4e955e5133731295120818a84f00a3750e22cf8504d81f1819"
WU_SI_SHA256 = "cdd842d87e34a8dc70d87e5999e8665f5fe054e66a22f0908b72ea2953f365d4"
WU_SI_BLOCK_COUNT = 1041
WU_SI_FOLIOS_STRIPPED = 79
PAPER_ID = "doi_10_1038_s41467_025_58320_5"

POOL = [
    ("wu2025-main", WU_MAIN_PDF),
    ("wu2025-si", WU_SI_PDF),
    ("zhang2017-main", "backend/data/reference_sources/B01/B01-Zhang-2017-NatCommun-main.pdf"),
    ("zhang2017-si", "backend/data/reference_sources/B01/B01-Zhang-2017-NatCommun-SI.pdf"),
    ("chemkb-kion", "reaserch_agent/chem_kb/High-Capacity Aqueous Potassium-Ion Batteries for Large-Scale Energy Storage.pdf"),
    ("chemkb-pba-hosts", "reaserch_agent/chem_kb/High-Entropy Prussian Blue Analogues and Their Oxide Family as Sulfur Hosts for Lithium-Sulfur Batteries.pdf"),
    ("chemkb-nife-pba", "reaserch_agent/chem_kb/Operando Spectroscopic Identification of Active Sites in NiFe Prussian Blue Analogues as Electrocatalysts- Activation of Oxygen Atoms for Oxygen Evolution Reaction.pdf"),
    ("upload-upl_1e79", "backend/data/uploads/upl_1e796859a263c3e5c502c488.pdf"),
]

EXPECTED_POST_CENSUS = {
    "wu2025-main": ["pdf_column_layout_ambiguous"],
    "wu2025-si": ["experimental_section_missing"],
    "zhang2017-main": ["pdf_column_layout_ambiguous"],
    "zhang2017-si": ["experimental_section_missing"],
    "chemkb-kion": ["pdf_column_layout_ambiguous"],
    "chemkb-pba-hosts": ["pdf_source_too_large"],
    "chemkb-nife-pba": ["pdf_column_layout_ambiguous"],
    "upload-upl_1e79": ["pdf_column_layout_ambiguous"],
}

W1_SENTENCE = (
    "After the hydrothermal reaction, the precipitated Ni-Mo-O powder was "
    "collected, dried, and then reduced at 500 °C for 6 h under a mixed "
    "hydrogen-nitrogen flow to obtain Ni-Mo nanoparticles."
)
W1_NEEDLE = ("the precipitated Ni-Mo-O powder was collected, dried, "
             "and then reduced")
W2_QUOTES = {
    "precipitate": ("1 M HCl was added dropwise into the solution until a "
                    "white precipitate was formed."),
    "heat": "the solution was heated at 50 °C for 3 h",
    "filter_wash_collect": ("the Mo-O precursor was collected by filtration "
                            "and washing"),
    "reduce": ("MoO2 nanorods were obtained through hydrogen reduction of "
               "the Mo-O precursor at 500 °C for 2 h."),
}


def _fail(message: str) -> None:
    raise SystemExit(f"r15 acceptance failed: {message}")


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _baseline() -> dict:
    conventions = ROOT / "chem_resources" / "chemistry_conventions" / "conventions.json"
    if _sha256_file(conventions) != CONVENTIONS_SHA256:
        _fail("conventions.json drifted from the frozen baseline")
    if _sha256_file(ROOT / PROMPT_BUILDER_FILE) != PROMPT_BUILDER_SHA256:
        _fail("prompt-builder module drifted from the frozen baseline")
    for rel, pinned in CHANGED_SOURCE_FILES.items():
        if _sha256_file(ROOT / rel) != pinned:
            _fail(f"{rel} drifted from the round-15 recorded state")
    if _sha256_file(ROOT / NEW_TEST_FILE) != NEW_TEST_SHA256:
        _fail("new folio test file drifted from the round-15 recorded state")
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        _fail(f"git status failed: {proc.stderr[-200:]}")
    tracked_modified = sorted(
        line[3:] for line in proc.stdout.splitlines()
        if line[:2] in {" M", "M ", "MM"}
    )
    if not set(tracked_modified) <= set(CHANGED_SOURCE_FILES):
        _fail(
            "tracked modifications outside the two ingestion files: "
            + json.dumps(tracked_modified)
        )
    return {
        "pre_round_head": PRE_ROUND_HEAD,
        "conventions_json_sha256": CONVENTIONS_SHA256,
        "prompt_builder_module": PROMPT_BUILDER_FILE,
        "prompt_builder_sha256": PROMPT_BUILDER_SHA256,
        "a01_source_digest": A01_SOURCE_DIGEST,
        "wu2025_main_pdf_sha256": WU_MAIN_SHA256,
        "wu2025_si_pdf_sha256": WU_SI_SHA256,
        "changed_source_files": CHANGED_SOURCE_FILES,
        "new_test_file": {NEW_TEST_FILE: NEW_TEST_SHA256},
        "tracked_modifications_subset_of_changed_files": True,
    }


def _blocks_pin(blocks) -> str:
    canon = json.dumps(
        [(b.page, b.number, b.text, b.font_size, b.bold, b.caption) for b in blocks],
        ensure_ascii=False, sort_keys=True,
    )
    return sha256(canon.encode()).hexdigest()


def _folio_fix_verification() -> dict:
    from reaserch_agent.route_pdf_source import _read_pdf_blocks_with_report
    from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote
    from reaserch_agent.route_pdf_verification_context import (
        MAX_VERIFICATION_CONTEXT_BLOCKS,
    )

    # A01 preservation anchor.
    a01_blocks, a01_issue, a01_report = _read_pdf_blocks_with_report(
        (ROOT / A01_PDF).read_bytes())
    if a01_issue or len(a01_blocks) != A01_BLOCK_COUNT:
        _fail("A01 parse drifted (count)")
    if _blocks_pin(a01_blocks) != A01_BLOCKS_SHA256:
        _fail("A01 parse drifted (block sha256)")
    if a01_report["folios_stripped"]:
        _fail("A01 must have zero validated folios")

    # Wu SI now parses; the W1 sentence binds verbatim.
    si_blocks, si_issue, si_report = _read_pdf_blocks_with_report(
        (ROOT / WU_SI_PDF).read_bytes())
    if si_issue or len(si_blocks) != WU_SI_BLOCK_COUNT:
        _fail(f"Wu SI parse drifted: {si_issue}, {len(si_blocks or [])}")
    stripped = si_report["folios_stripped"]
    if len(stripped) != WU_SI_FOLIOS_STRIPPED:
        _fail("Wu SI folio strip count drifted")
    if stripped[0]["text"] != "S 2" or stripped[-1]["text"] != "S 80":
        _fail("Wu SI folio strip range drifted")
    pairs = [
        (f"pdf:p{b.page}:b{b.number}-p{b.page}:b{b.number}", b.text)
        for b in si_blocks
    ]
    captions = {
        locator for locator, block in zip([p[0] for p in pairs], si_blocks)
        if block.caption
    }
    bindings = {}
    for label, needle in (("needle", W1_NEEDLE), ("full_sentence", W1_SENTENCE)):
        binding, quote_issue = bind_pdf_quote(
            pairs, needle, caption_block_locators=captions,
            max_quote_blocks=MAX_VERIFICATION_CONTEXT_BLOCKS,
        )
        if quote_issue or binding is None:
            _fail(f"W1 {label} must bind in the parsed SI blocks")
        bindings[label] = binding.locator

    # Wu main still abstains on the p10 chart tick, with structured detail.
    main_blocks, main_issue, main_report = _read_pdf_blocks_with_report(
        (ROOT / WU_MAIN_PDF).read_bytes())
    if main_blocks is not None or main_issue != "pdf_column_layout_ambiguous":
        _fail("Wu main must still abstain at ingestion")
    abstention = main_report["abstention"]
    culprit = (abstention or {}).get("culprits", [{}])[0]
    if not (culprit.get("page") == 10 and culprit.get("text") == "36"):
        _fail("Wu main abstention culprit drifted")
    return {
        "a01": {
            "blocks": len(a01_blocks),
            "blocks_sha256": _blocks_pin(a01_blocks),
            "folios_stripped": 0,
            "note": ("A01 page 4 carries three bottom-band candidates on one "
                     "page; the at-most-one-per-page guard rejects them, so "
                     "nothing is stripped and the parse is byte-identical"),
        },
        "wu_si": {
            "blocks": len(si_blocks),
            "folios_stripped": len(stripped),
            "folio_first": stripped[0],
            "folio_last": stripped[-1],
            "w1_sentence_binding": bindings,
            "enumeration_endpoint": "experimental_section_missing (0 groups)",
        },
        "wu_main": {
            "issue": main_issue,
            "abstention": abstention,
            "note": ("the p10 chart tick '36' sits at 0.819h, outside the "
                     "0.90h folio band, and never becomes a candidate; the "
                     "two-column chart layout keeps abstaining, as intended"),
        },
    }


def _pool_census() -> dict:
    from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups

    before = {}
    if R14_REPLAY.is_file():
        archived = json.loads(R14_REPLAY.read_text(encoding="utf-8"))
        for row in archived["layer_results"]["pool_census"]:
            before[row["candidate"]] = row["reasons"]
    rows = []
    for tag, rel in POOL:
        path = ROOT / rel
        result = enumerate_pdf_experimental_groups(
            {"pool_" + tag: path}, source_root=path.parent)
        reasons = sorted({d.reason_code for d in result.diagnostics})
        rows.append({
            "candidate": tag,
            "file": rel,
            "groups": len(result.groups),
            "reasons_before_r15": before.get(tag),
            "reasons": reasons,
        })
    if any(row["groups"] for row in rows):
        _fail("pool census unexpectedly produced a group")
    for row in rows:
        if row["reasons"] != EXPECTED_POST_CENSUS[row["candidate"]]:
            _fail(f"post-fix census drifted for {row['candidate']}: {row['reasons']}")
        if row["reasons_before_r15"] is not None and row["candidate"] != "wu2025-si":
            if row["reasons"] != row["reasons_before_r15"]:
                _fail(f"non-SI candidate changed behavior: {row['candidate']}")

    def _count(key):
        return {
            "layout_ambiguous": sum("pdf_column_layout_ambiguous" in r[key] for r in rows),
            "missing_section": sum("experimental_section_missing" in r[key] for r in rows),
            "oversize": sum("pdf_source_too_large" in r[key] for r in rows),
        }

    return {
        "rows": rows,
        "framing_corrected": {
            "before_r15": "6 layout + 1 missing-section + 1 oversize",
            "after_r15": "5 layout + 2 missing-section + 1 oversize",
            "counts_after": _count("reasons"),
        },
    }


def _rule_eligibility() -> dict:
    """Score W1/W2 against the frozen conventions with live phrase checks."""
    conventions = json.loads(
        (ROOT / "chem_resources" / "chemistry_conventions" / "conventions.json")
        .read_text(encoding="utf-8"))
    rules = conventions["rules"]

    def op_hit(rule, blob):
        patterns = (rule["preconditions"].get("operation_patterns") or [])
        return any(str(p).lower() in blob for p in patterns)

    def intent_hit(rule, blob):
        patterns = (rule["preconditions"].get("intent_patterns") or [])
        return any(str(p).lower() in blob for p in patterns)

    by_id = {rule["rule_id"]: rule for rule in rules}

    # The four user-flagged claims, verified programmatically.
    w1_sentence = W1_SENTENCE.lower()
    claims = {}
    claims["w1_collection_method_unstated"] = (
        "centrifug" not in w1_sentence and "filtrat" not in w1_sentence)
    drying = by_id["DRYING_V1"]
    claims["drying_v1_requires_proven_wet_solid_input"] = (
        sorted(drying["allowed_input_states"])
        == ["retained_wet_solid", "washed_wet_solid"])
    claims["dried_unrecognized_by_drying_v1"] = not (
        op_hit(drying, "dried") or intent_hit(drying, "dried"))
    claims["no_reduction_rule"] = not any(
        any(token in str(p).lower()
            for p in (rule["preconditions"].get("operation_patterns") or [])
            + (rule["preconditions"].get("intent_patterns") or []))
        for rule in rules
        for token in ("reduc", "还原", "hydrogen"))
    if not all(claims.values()):
        _fail("eligibility claim verification drifted: "
              + json.dumps(claims, ensure_ascii=False))

    def row(step, slice_text, state_to_prove, rule_id, input_evidence,
            continuity, verdict, note):
        rule = by_id[rule_id]
        blob = slice_text.lower()
        return {
            "step": step,
            "source_slice_verbatim": slice_text,
            "state_to_prove": state_to_prove,
            "candidate_rule": rule_id,
            "operation_phrase_hit": op_hit(rule, blob),
            "intent_phrase_hit": intent_hit(rule, blob),
            "input_state_evidence": input_evidence,
            "material_continuity": continuity,
            "verdict": verdict,
            "note": note,
        }

    w1_collect = "the precipitated Ni-Mo-O powder was collected"
    w1_dry = "dried"
    w1_reduce = ("reduced at 500 °C for 6 h under a mixed hydrogen-nitrogen "
                 "flow")
    w1 = [
        row("collect", w1_collect, "retained_wet_solid (collected precipitate)",
            "CENTRIFUGE_COLLECT_PRECIPITATE_V1",
            "absent: 'precipitated' implies a solid/liquid mixture but no "
            "suspension state is asserted, and no operation word exists",
            "would parent the drying step; unprovable here",
            "ineligible",
            "operation patterns require 'centrifug'; FILTRATION variant "
            "requires 'filtrat'; the bare word 'collected' matches no intent "
            "phrase either (no contiguous 'collect precipitate')"),
        row("dry", w1_dry, "dry_solid",
            "DRYING_V1",
            "requires a PROVEN retained_wet_solid/washed_wet_solid input; the "
            "collection step above is unprovable, so the input gate fails too",
            "parent state missing -> lineage broken at the first step",
            "ineligible",
            "double failure: 'dried' contains neither 'dry' nor 'drying' as a "
            "substring (phrase miss), and the wet-solid input is unproven"),
        row("reduce", w1_reduce, "phase change to Ni-Mo alloy nanoparticles",
            "DRYING_V1",
            "n/a",
            "n/a",
            "ineligible",
            "placeholder rule column: NO reduction/thermal rule exists in the "
            "frozen table (no 'reduc'/'hydrogen'/'还原' pattern anywhere); "
            "operation and intent both miss on every rule"),
    ]
    w2 = [
        row("precipitate", W2_QUOTES["precipitate"],
            "suspension/precipitate formation",
            "CENTRIFUGE_COLLECT_PRECIPITATE_V1",
            "the post-precipitation mixture is literally called 'solution' in "
            "the next slice; no 'suspension' wording",
            "would parent filtration; not rule-provable",
            "ineligible",
            "no precipitation rule exists; placeholder rule's operation misses "
            "(no 'centrifug'); precipitation is a reaction outcome, not a "
            "covered unit operation"),
        row("heat", W2_QUOTES["heat"], "heated mixture (no state token)",
            "DRYING_V1",
            "n/a",
            "n/a",
            "ineligible",
            "no heating/aging rule exists; every operation pattern misses on "
            "'heated' (placeholder column only)"),
        row("filter_wash_collect", W2_QUOTES["filter_wash_collect"],
            "retained_wet_solid -> washed_wet_solid",
            "WASHING_V1",
            "requires a proven wet-solid input; filtration cannot supply it "
            "because FILTRATION_COLLECT_RETAINED_V1's intent phrases miss "
            "('collected by filtration' is not 'collect the solid'/'collect "
            "retained'), and the suspension input is itself unproven",
            "filtration intent miss breaks the chain before washing",
            "ineligible",
            "phrase-level partial: 'wash'/'washing' hit operation+intent on "
            "WASHING_V1 (the only phrase-level hit in either chain), but the "
            "input-state gate fails; filtration hits operation ('filtrat') "
            "yet misses intent"),
        row("reduce", W2_QUOTES["reduce"],
            "phase change to MoO2 nanorods",
            "DRYING_V1",
            "n/a",
            "n/a",
            "ineligible",
            "no reduction rule exists; operation and intent miss on every "
            "rule (placeholder column only)"),
    ]
    verdict = ("W1 and W2 are both ineligible under the frozen v1 rule table; "
               "they are retained as ingestion samples and a rule-compatible "
               "source must be found separately (不补造离心/过滤, 不改写源摘录, "
               "不扩规则保正例)")
    return {
        "conventions_sha256": CONVENTIONS_SHA256,
        "rule_count": len(rules),
        "phrase_match_semantics": ("casefolded substring over operation/intent "
                                   "blobs (workflow.py); hit = operation + "
                                   "intent + proven input state jointly"),
        "claims_verified": claims,
        "w1": {"source": "Wu-2025 SI page S14, Supplementary Fig. S13 caption "
                         "paragraph",
               "chain": "hydrothermal -> collect -> dry -> reduce (500 °C, "
                        "H2/N2, 6 h)",
               "steps": w1},
        "w2": {"source": "Wu-2025 SI page S15, Supplementary Fig. S14 caption "
                         "paragraph (powdered MoO2 nanorods)",
               "chain": "precipitate -> heat 50 °C 3 h -> filtration/washing "
                        "collection -> hydrogen reduction 500 °C 2 h",
               "steps": w2},
        "overall_verdict": verdict,
    }


def _boundary_design() -> dict:
    """Task 3: SI grouping boundary design (recorded, NOT implemented)."""
    from reaserch_agent.route_pdf_source import _read_pdf_blocks_with_report

    si_blocks, _, _ = _read_pdf_blocks_with_report((ROOT / WU_SI_PDF).read_bytes())
    region = [
        {
            "locator": f"p{b.page}:b{b.number}",
            "bold": b.bold, "caption_flag": b.caption,
            "font_size": b.font_size,
            "text_head": b.text[:72],
        }
        for b in si_blocks if b.page == 14
    ][:11]
    return {
        "s14_region_blocks_observed": region,
        "three_block_kinds": {
            "figure_label": ("bold 'Supplementary Fig.' block -- a bold-prefix "
                             "split artifact carrying no number"),
            "caption_prose": ("'S13. SEM images of ...' blocks -- the actual "
                              "caption sentences"),
            "synthesis_paragraph": ("'To prepare powdered Ni-Mo nanoparticles, "
                                    "...' -- standalone experimental prose "
                                    "embedded between captions"),
        },
        "current_boundary_gap": ("the parser-owned caption predicate "
                                 "_caption_text matches ^(figure|fig|table|"
                                 "scheme)\\s*s?\\d+; the SI split form "
                                 "('Supplementary Fig.' + 'S13. ...') matches "
                                 "neither, so SI captions are currently "
                                 "indistinguishable from prose (observed: "
                                 "caption_flag False on all S14-region "
                                 "blocks)"),
        "consistent_boundary_design": [
            ("single predicate: extend _caption_text in route_pdf_source.py "
             "so the three consumers -- enumeration grouping (_caption/"
             "_group_boundary), quote binding (caption_block_locators from "
             "_quote_caption_locators), and source re-verification "
             "(_group_range) -- keep sharing one parser-owned definition; no "
             "proposal-supplied caption metadata is ever accepted"),
            ("recognition rule (designed): a bold block fullmatching "
             "'Supplementary (Fig|Figure|Table|Scheme)\\.?' followed by a "
             "body block starting 'S\\d+.' marks both blocks, plus their "
             "same-original-block continuations, as captions"),
            ("grouping consequence (designed): caption blocks never open or "
             "close a group; a synthesis paragraph between captions is the "
             "groupable unit -- the caption-boundary grouping mode deferred "
             "from r14 remains required before W1-class enumeration"),
            ("quote-binding consequence (designed): caption blocks stay "
             "non-quotable anchors and the at-most-one-skipped-caption rule "
             "is unchanged; newly flagged SI captions simply become skippable "
             "furniture inside a span"),
        ],
        "stays_closed_this_round": [
            "no caption-recognition change is implemented (it would alter "
            "block semantics on caption-flagged regions)",
            "caption quotation is NOT opened",
            "the workflow interception of incomplete sources is unchanged",
        ],
    }


def _wu_attested_layer() -> dict:
    from reaserch_agent.route_attestation import attested_route_sources
    from reaserch_agent.route_pdf_groups import (
        enumerate_attested_pdf_experimental_groups,
    )
    from reaserch_agent.run_research_agent import load_route_trust_config

    kb = WU / "kb"
    trust = load_route_trust_config(str(WU / "route-trust-config.json"), str(kb))
    sources = attested_route_sources(
        kb, trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"],
    )
    kinds = sorted(s.document_kind for s in sources.get(PAPER_ID, []))
    if kinds != ["primary_paper", "supporting_information"]:
        _fail(f"Wu attestation must still verify both documents, got {kinds}")
    result = enumerate_attested_pdf_experimental_groups(
        kb, trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"],
    )
    reasons = sorted({d.reason_code for d in result.diagnostics})
    if result.groups:
        _fail("Wu attested enumeration unexpectedly produced groups")
    if reasons != ["experimental_section_missing", "pdf_column_layout_ambiguous"]:
        _fail(f"Wu attested enumeration drifted: {reasons}")
    detail = next(
        (d.detail for d in result.diagnostics
         if d.reason_code == "pdf_column_layout_ambiguous"), "")
    if '"page": 10' not in detail or '"text": "36"' not in detail:
        _fail("attested enumeration must carry the structured culprit detail")
    return {
        "attested_documents": 2,
        "groups": 0,
        "diagnostic_reasons": reasons,
        "main_abstention_detail": json.loads(detail),
        "meaning": ("the signed SI now parses and reaches the section gate; "
                    "the signed main paper still abstains on the two-column "
                    "chart layout"),
    }


def _anchors() -> dict:
    # (a) A01 regression: r13b archive-pinned runner as a subprocess.
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         str(OUT_DIR / "local-revision-r13b.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=900, env=env,
    )
    if proc.returncode != 0:
        _fail(f"r13b rerun failed: {proc.stderr[-400:]}")
    summary = json.loads(proc.stdout)
    if not (summary.get("replay_matches_r13_archive")
            and summary.get("double_run_byte_identical")):
        _fail("r13b archive pins must hold")
    ms7a_row = next(
        row for row in summary["nine_node_table"]
        if row.get("label") == "ms7a.out" or "graph[7].out" in str(row))
    if "BLOCKED" not in str(ms7a_row):
        _fail("ms7a.out must stay BLOCKED in the r13b table")

    # (b) 3E diagnostics module: zero proof dependency (source audit).
    diag_source = (
        ROOT / "reaserch_agent" / "route_operation_precondition_diagnostic.py"
    ).read_text(encoding="utf-8")
    token_free = (
        "_VerifiedParentStateEvidence" not in diag_source
        and "_VerifiedLiquidMedium" not in diag_source
        and "route_state_proof_dag" not in diag_source
        and "route_proof_dag" not in diag_source
    )
    if not token_free:
        _fail("3E diagnostics module must keep zero proof dependency")

    # (c) New folio test module and the 277 target set as subprocesses.
    test_proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         "-m", "unittest", "reaserch_agent.test_route_pdf_folio"],
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env,
    )
    if "OK" not in (test_proc.stderr or "") + (test_proc.stdout or ""):
        _fail(f"folio tests failed: {test_proc.stderr[-400:]}")
    target_modules = [
        "reaserch_agent.test_route_proof_dag",
        "reaserch_agent.test_route_retained_object",
        "reaserch_agent.test_route_retained_object_integration",
        "reaserch_agent.test_route_convention_state_chain",
        "reaserch_agent.test_route_protocol_reference",
        "reaserch_agent.test_route_operation_precondition_diagnostic",
        "reaserch_agent.test_route_pdf_source",
        "reaserch_agent.test_route_pdf_groups",
        "reaserch_agent.test_route_pdf_local_revision_merge",
    ]
    target_proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         "-m", "unittest"] + target_modules,
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env,
    )
    target_out = (target_proc.stderr or "") + (target_proc.stdout or "")
    if "Ran 277 tests" not in target_out or "OK" not in target_out:
        _fail(f"277 target set failed: {target_out[-400:]}")

    return {
        "a01_regression": {
            "mechanism": "r13b runner re-executed as a subprocess",
            "double_run_byte_identical": True,
            "replay_matches_r13_archive": True,
            "ms7a_out": "BLOCKED (retained_object_mention_precedes_operation)",
        },
        "diagnostics_module_zero_proof_dependency": True,
        "folio_test_module_ok": True,
        "target_set_277_ok": True,
    }


def _build_replay() -> dict:
    baseline = _baseline()
    folio = _folio_fix_verification()
    census = _pool_census()
    eligibility = _rule_eligibility()
    boundary = _boundary_design()
    attested = _wu_attested_layer()
    anchors = _anchors()
    return {
        "schema_version": "g2a_ingestion_folio/v15",
        "round": ("r15 (G2a): SI folio recognition minimal fix + abstention "
                  "diagnostics + W1/W2 frozen-rule eligibility pre-screen"),
        "model_generated": False,
        "model_called": False,
        "baseline": baseline,
        "code_changes": {
            "files": CHANGED_SOURCE_FILES,
            "new_test_file": {NEW_TEST_FILE: NEW_TEST_SHA256},
            "semantics_note": (
                "conventions, protocol schemas, and every rule resource are "
                "byte-identical to the frozen baseline; ingestion block "
                "semantics changed ONLY for inputs that previously failed "
                "closed (no previously parseable document changes: A01 "
                "asserted byte-identical), so no existing versioned artifact "
                "can be stale; PDF_GROUP_PARSER_VERSION is deliberately not "
                "bumped because bumping it would falsify the pinned r13/r13b "
                "replay bytes without invalidating any real cache"),
        },
        "folio_fix": {
            "design": {
                "three_factors": [
                    "standalone folio form: fullmatch [A-Za-z]{0,2}\\s*[0-9]{1,4} "
                    "in the bottom band (y0 > 0.90h)",
                    "cross-page consistency: uniform alphabetic prefix and y0 "
                    "within 4 pt of the median; at most one candidate per page",
                    "page-order increment: numbers advance exactly with page "
                    "indices; at least three candidate pages",
                ],
                "never_alone": ("no deletion by digit count, font size, or a "
                                "fixed page position; the legacy margin fast "
                                "path (>0.94h + 3+ digits) is unchanged"),
                "non_goals_kept": ["no page removal", "no PDF byte change",
                                   "no stitching across missing regions"],
            },
            "verification": folio,
        },
        "abstention_diagnostics": {
            "reason_code_stability": ("reason_code strings are unchanged; the "
                                      "structured culprit record rides the new "
                                      "detail field (JSON) on "
                                      "PdfGroupEnumerationDiagnosticV1"),
            "example_wu_main": folio["wu_main"]["abstention"],
        },
        "layer_results": {
            "attestation": {"status": "verified", "documents": 2},
            "group_enumeration": {
                "status": "reached_section_gate",
                "groups": 0,
                "diagnostic_reasons": attested["diagnostic_reasons"],
                "meaning": ("the SI now parses (1041 blocks) and stops at "
                            "experimental_section_missing -- the honest next "
                            "layer, and this round's allowed endpoint; the "
                            "main paper still abstains at ingestion"),
            },
            "proposal_extraction": {"status": "not_reached"},
            "local_revision": {"status": "not_reached"},
            "association": {"status": "not_reached"},
            "receipt": {"status": "not_reached"},
            "pool_census": census,
        },
        "rule_eligibility": eligibility,
        "boundary_design": boundary,
        "dag_consumption": {
            "observed": False,
            "note": ("still unobservable (0 groups), and W1 is no longer "
                     "presupposed a multi-hop positive: the frozen-rule "
                     "eligibility table scores both W1 and W2 ineligible; "
                     "nothing here is claimed as a pass"),
        },
        "terminology_corrections": [
            {"item": "pool census framing",
             "r14_wording": "every local cross-paper candidate abstains at "
                            "ingestion (framed as 8/8 layout problems)",
             "corrected": "6 layout (pdf_column_layout_ambiguous) + 1 missing "
                          "experimental section (zhang2017-si) + 1 oversize "
                          "(chemkb-pba-hosts); after the folio fix: 5 layout "
                          "+ 2 missing-section + 1 oversize"},
            {"item": "element overlap",
             "r14_wording": "zero element overlap between Ni-Mo and Ni-Fe",
             "corrected": "withdrawn: Ni-Mo and Ni-Fe are different material "
                          "systems SHARING the Ni element"},
            {"item": "W1 presupposition",
             "r14_wording": "W1 treated as the prospective multi-hop positive",
             "corrected": "W1 is not presupposed anything; the frozen-rule "
                          "eligibility table (rule_eligibility) scores W1 and "
                          "W2 ineligible, so neither can be a positive under "
                          "v1 rules"},
        ],
        "separate_counters": {
            "field_proofs_succeeded": 0,
            "group_receipt_pass": 0,
            "protocol_admitted": 0,
            "published": 0,
        },
        "anchors": anchors,
        "fixed_constraints": {
            "r7_to_r14_archives_untouched": True,
            "contracts_tree_untouched": True,
            "diagnostics_module_semantics_untouched": True,
            "conventions_no_v1_expansion": True,
            "tokens_consumed_by_this_runner": 0,
            "model_not_called": True,
            "device_not_run": True,
            "eight_question_rerun": False,
            "ms7a_cascade_still_blocked": True,
            "tracked_modifications_limited_to_two_ingestion_files": True,
        },
    }


def _audit() -> dict:
    return {
        "schema_version": "g2a_ingestion_folio/v15",
        "model_generated": False,
        "scope": ("r15 (G2a): minimal folio-recognition repair in the PDF "
                  "ingestion layer plus structured abstention diagnostics, "
                  "with a frozen-rule eligibility pre-screen for W1/W2 and a "
                  "design-only SI grouping boundary; no model call, no "
                  "conventions/protocol semantic change"),
        "audit": [
            {"kind": "changed_files",
             "items": [
                 "reaserch_agent/route_pdf_source.py (validated folio "
                 "recognition + _read_pdf_blocks_with_report; _read_pdf_blocks "
                 "signature preserved)",
                 "reaserch_agent/route_pdf_groups.py (new detail field on the "
                 "enumeration diagnostic; uses the report variant)",
                 "reaserch_agent/test_route_pdf_folio.py (new, 15 tests)",
                 "result/operation-structure-20260928/local-revision-r15.py",
                 "result/operation-structure-20260928/local-revision-r15-*.json",
                 "docs/field_semantic_gate_r15_checkpoint_20261004.md",
             ]},
            {"kind": "reason_code_stability",
             "detail": ("every existing reason_code string is byte-identical; "
                        "the structured abstention detail is a new optional "
                        "field, so the existing exact-equality assertion on "
                        "['pdf_column_layout_ambiguous'] in "
                        "test_route_pdf_groups.py keeps passing unchanged")},
            {"kind": "capability_change",
             "detail": ("the only behavioral change: documents whose sole "
                        "ambiguity trigger was a validated folio pattern now "
                        "parse; Wu SI goes from pdf_column_layout_ambiguous "
                        "to experimental_section_missing (0 groups).  A01 and "
                        "every other previously parseable input are asserted "
                        "byte-identical; every other abstaining input keeps "
                        "its r14 reason")},
            {"kind": "honest_endpoints",
             "detail": ("W1/W2 ineligible under the frozen v1 rules; SI "
                        "caption-boundary grouping stays a recorded design; "
                        "dag_consumption remains unobserved and unclaimed")},
        ],
    }


def main() -> None:
    replay = _build_replay()
    replay_2 = _build_replay()
    replay_text = json.dumps(replay, ensure_ascii=False, indent=2, default=str)
    replay_text_2 = json.dumps(replay_2, ensure_ascii=False, indent=2,
                               default=str)
    audit_text = json.dumps(_audit(), ensure_ascii=False, indent=2,
                            default=str)
    if replay_text != replay_text_2:
        _fail("double run is not byte-identical")

    (OUT_DIR / "local-revision-r15-replay.json").write_text(
        replay_text, encoding="utf-8", newline="\n")
    (OUT_DIR / "local-revision-r15-audit.json").write_text(
        audit_text, encoding="utf-8", newline="\n")
    summary = {
        "si_blocks": replay["folio_fix"]["verification"]["wu_si"]["blocks"],
        "si_folios_stripped": replay["folio_fix"]["verification"]["wu_si"]["folios_stripped"],
        "w1_sentence_binding": replay["folio_fix"]["verification"]["wu_si"]["w1_sentence_binding"],
        "main_still_abstains": (
            replay["folio_fix"]["verification"]["wu_main"]["issue"]
            == "pdf_column_layout_ambiguous"),
        "a01_parse_unchanged": (
            replay["folio_fix"]["verification"]["a01"]["blocks_sha256"]
            == A01_BLOCKS_SHA256),
        "w1_eligible": False,
        "w2_eligible": False,
        "anchors_ok": True,
        "double_run_byte_identical": True,
        "replay_sha256": "sha256_" + sha256(
            replay_text.encode("utf-8")).hexdigest(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
