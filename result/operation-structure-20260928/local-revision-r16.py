"""Round-16 (G2a follow-up): folio misdelete tightening + parser-version
correction + caption-boundary design supplement.  No model is called.

Scope of the code change (exactly three tracked files, all in the PDF
ingestion layer and its tests):

- ``reaserch_agent/route_pdf_source.py`` tightens validated folio
  recognition from three factors to five.  The r15 rule deleted real
  experiment values whenever a bare value (e.g. a final-pH reading) happened
  to sit alone in the bottom band on 3+ pages with incrementing numbers.
  Two new factors close both reported shapes: (4) a consistent horizontal
  anchor (the candidate line's center x must agree within 4 pt of the median
  across pages), and (5) body isolation (the candidate must be the only
  non-empty text line of its original PDF block).  When context is doubtful
  the line is kept: keeping content is always the safe failure.  No page is
  removed, no PDF byte is modified.
- ``reaserch_agent/route_pdf_groups.py`` bumps PDF_GROUP_PARSER_VERSION to
  ``route_pdf_groups/v2``.  The r15 wording "no previously parseable
  document changes" is withdrawn: it is true of the A01 anchor (asserted
  byte-identical) but false in general -- any previously parseable document
  carrying a validated folio pattern now yields a shorter block sequence
  (empirically pinned below against the pre-r15 parser).  The bump is safe
  for the r13/r13b archives because they pin no version string (grep
  verified: zero occurrences of "route_pdf_groups/v" in the four r13/r13b
  archive files, and the r13/r13b runners never reference the constant).
- ``reaserch_agent/test_route_pdf_folio.py`` gains a MisdeleteRegressionTest
  class (5 tests): probe A/B regressions, an over-correction control, the
  old/new block-semantics comparison, and the version assertion.

What this runner establishes, deterministically and token-free:

1. Baseline freeze: pre-round HEAD 89707da, conventions.json sha256 (frozen,
   unchanged), prompt-builder sha256 (unchanged), A01 source digest, Wu-2025
   PDF digests, and the sha256 of the three changed files.  Any drift fails
   the runner.
2. Misdelete fix before/after: the pre-round code (loaded from git show
   89707da) strips the values 11/12/13 in both probe shapes; the current
   code keeps every value in both shapes and still strips an x-consistent
   control set.  Each new factor is load-bearing: probe A passes every
   factor except the horizontal anchor; probe B passes every factor except
   body isolation.
3. Version determination evidence: five synthetic inputs parsed under the
   pre-r15 parser (git show c429443) and the current parser -- two
   folio-bearing inputs change block sequences (8->4 blocks), a folio-free
   input and both probe shapes are unchanged.  Conclusion: bump to v2.
4. Anchors held: A01 byte-identical (1289 blocks, pinned sha256, zero
   folios); Wu SI 1041 blocks / 79 folios with the W1 sentence binding
   verbatim (pdf:p14:b9-p14:b11); Wu main still abstains on the p10 chart
   tick "36"; the 8-candidate pool census is row-by-row identical to the
   r15 post-fix census; the Wu attested layer still verifies both documents
   and reaches the same endpoints.
5. Caption-boundary supplement (design, NOT implemented): on Wu SI pages
   S14/S15 every caption-paragraph line is its own original PyMuPDF block
   (verified below), so r15's drafted "same-original-block continuations"
   phrase would keep only the caption head line; the recorded design now
   binds continuations by vertical reading order with the binder's
   at-most-one-skipped-block hop limit unchanged.
6. Regression anchors: r13b archive-pinned runner re-executed as a
   subprocess (replay matches the r13 archive, double-run byte-identical,
   ms7a.out stays BLOCKED), 3E diagnostics zero-proof-dependency source
   audit, the folio test module (20 tests) as a subprocess, and the 339
   target set as a subprocess.

Terminology corrections recorded this round (r15 archive itself untouched):
the r15 claim "no previously parseable document changes" is withdrawn and
scoped to the A01 anchor; the r15 terminology corrections stand.

The runner is deterministic: two in-memory builds are byte-identical; all
inputs are local bytes.  Zero tokens are consumed by this runner.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "result" / "operation-structure-20260928"
WU = ROOT / "result" / "wu2025-real-input-20261004"
R15_REPLAY = OUT_DIR / "local-revision-r15-replay.json"

PRE_ROUND_HEAD = "89707da6d101d427a692eb46d4399b1e9d35cd88"
PRER15_COMMIT = "c42944306d4cb6c2e6b0a20e909a40c812db6afc"
CONVENTIONS_SHA256 = (
    "39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b"
)
PROMPT_BUILDER_FILE = "reaserch_agent/route_pdf_group_extraction.py"
PROMPT_BUILDER_SHA256 = (
    "24864d9af522666b15a1690b6c9ba829bebfa3047545d82adc3e3dff331486f7"
)
CHANGED_SOURCE_FILES = {
    "reaserch_agent/route_pdf_source.py": (
        "aca1f4b3ae144b4ab4b03bc99350c96e6c24e0dd4eba92ac11f9864a78d91660"
    ),
    "reaserch_agent/route_pdf_groups.py": (
        "b918c82989c47ba6ae92e9769b040fe857c0963902a328f701ec837f0bc80dca"
    ),
}
FOLIO_TEST_FILE = "reaserch_agent/test_route_pdf_folio.py"
FOLIO_TEST_SHA256 = (
    "d1a122382c9ffbd428c0e721215abce74452a030f8f8cdf1fa82f662dfda810d"
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
W1_NEEDLE_LOCATOR = "pdf:p14:b9-p14:b10"
W1_SENTENCE_LOCATOR = "pdf:p14:b9-p14:b11"
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

EXPECTED_CENSUS = {
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

TARGET_SET_MODULES = [
    "reaserch_agent.test_route_proof_dag",
    "reaserch_agent.test_route_retained_object",
    "reaserch_agent.test_route_retained_object_integration",
    "reaserch_agent.test_route_convention_state_chain",
    "reaserch_agent.test_route_protocol_reference",
    "reaserch_agent.test_route_operation_precondition_diagnostic",
    "reaserch_agent.test_route_pdf_source",
    "reaserch_agent.test_route_pdf_groups",
    "reaserch_agent.test_route_pdf_local_revision_merge",
    "reaserch_agent.test_route_pdf_folio",
    "reaserch_agent.test_route_pdf_group_extraction",
    "reaserch_agent.test_route_pdf_group_proposals",
    "reaserch_agent.test_route_real_a01_material_fragment",
]
TARGET_SET_SIZE = 339


def _fail(message: str) -> None:
    raise SystemExit(f"r16 acceptance failed: {message}")


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
            _fail(f"{rel} drifted from the round-16 recorded state")
    if _sha256_file(ROOT / FOLIO_TEST_FILE) != FOLIO_TEST_SHA256:
        _fail("folio test file drifted from the round-16 recorded state")
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
    allowed = set(CHANGED_SOURCE_FILES) | {FOLIO_TEST_FILE}
    if not set(tracked_modified) <= allowed:
        _fail(
            "tracked modifications outside the three round-16 files: "
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
        "changed_test_file": {FOLIO_TEST_FILE: FOLIO_TEST_SHA256},
        "tracked_modifications_subset_of_changed_files": True,
    }


def _blocks_pin(blocks) -> str:
    canon = json.dumps(
        [(b.page, b.number, b.text, b.font_size, b.bold, b.caption) for b in blocks],
        ensure_ascii=False, sort_keys=True,
    )
    return sha256(canon.encode()).hexdigest()


def _load_historical_source_module(commit: str, name: str):
    """Load route_pdf_source.py as of a commit (for before/after evidence)."""
    proc = subprocess.run(
        ["git", "show", f"{commit}:reaserch_agent/route_pdf_source.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout:
        _fail(f"git show {commit}:route_pdf_source.py failed")
    temporary = tempfile.TemporaryDirectory()
    path = Path(temporary.name) / f"{name.rsplit('.', 1)[-1]}.py"
    path.write_text(proc.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        # Register before exec: dataclass processing resolves cls.__module__
        # through sys.modules.
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - defensive
        sys.modules.pop(name, None)
        _fail(f"historical module load failed: {exc}")
    return module


def _probe_a_bytes() -> bytes:
    """Values 11/12/13 at baseline 747, x drifting 70/280/460 per page."""
    import fitz

    document = fitz.open()
    for index, x in enumerate((70, 280, 460)):
        page = document.new_page(width=612, height=792)
        page.insert_text((70, 400), "The sample was measured carefully.",
                         fontsize=10)
        page.insert_text((70, 730), "Final pH:", fontsize=10)
        page.insert_text((x, 747), str(11 + index), fontsize=10)
    raw = document.tobytes()
    document.close()
    return raw


def _probe_b_bytes() -> bytes:
    """One insert_text writes 'Final pH:\\n<value>' per page: label and value
    share the same original block; x is consistent."""
    import fitz

    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=612, height=792)
        page.insert_text((70, 400), "The sample was measured carefully.",
                         fontsize=10)
        page.insert_text((280, 734), f"Final pH:\n{11 + index}", fontsize=10)
    raw = document.tobytes()
    document.close()
    return raw


def _probe_control_bytes() -> bytes:
    """Probe A shape with a consistent x anchor and no label nearby."""
    import fitz

    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=612, height=792)
        page.insert_text((70, 400), "The sample was measured carefully.",
                         fontsize=10)
        page.insert_text((280, 747), str(11 + index), fontsize=10)
    raw = document.tobytes()
    document.close()
    return raw


def _probe_outcome(module, raw: bytes) -> dict:
    blocks, issue, report = module._read_pdf_blocks_with_report(raw)
    texts = [block.text for block in blocks] if blocks else []
    return {
        "issue": issue,
        "blocks": len(texts),
        "stripped": [item["text"] for item in report.get("folios_stripped", [])],
        "values_kept": [v for v in ("11", "12", "13") if v in texts],
    }


def _misdelete_fix_verification() -> dict:
    from reaserch_agent import route_pdf_source as current

    pre_round = _load_historical_source_module(
        PRE_ROUND_HEAD, "reaserch_agent._route_pdf_source_r15")

    before = {
        "probe_a": _probe_outcome(pre_round, _probe_a_bytes()),
        "probe_b": _probe_outcome(pre_round, _probe_b_bytes()),
    }
    for name, outcome in before.items():
        if outcome["issue"] is not None or outcome["blocks"] != 6:
            _fail(f"pre-round probe {name} drifted: {outcome}")
        if outcome["stripped"] != ["11", "12", "13"] or outcome["values_kept"]:
            _fail(f"pre-round probe {name} must strip the values (misdelete)")

    after = {
        "probe_a": _probe_outcome(current, _probe_a_bytes()),
        "probe_b": _probe_outcome(current, _probe_b_bytes()),
        "control": _probe_outcome(current, _probe_control_bytes()),
    }
    for name in ("probe_a", "probe_b"):
        outcome = after[name]
        if outcome["issue"] is not None or outcome["blocks"] != 9:
            _fail(f"post-fix probe {name} drifted: {outcome}")
        if outcome["stripped"] or outcome["values_kept"] != ["11", "12", "13"]:
            _fail(f"post-fix probe {name} must keep every value")
    control = after["control"]
    if control["stripped"] != ["11", "12", "13"] or control["blocks"] != 3:
        _fail(f"x-consistent control must still strip: {control}")

    return {
        "probes": {
            "probe_a": ("3 pages 612x792; 'Final pH:' label at (70,730); bare "
                        "values 11/12/13 at baseline 747 fontsize 10 with x "
                        "drifting 70/280/460 -- every factor passes except "
                        "the horizontal anchor"),
            "probe_b": ("one insert_text at (280,734) writes 'Final pH:' and "
                        "the value on two lines of ONE original block; x is "
                        "consistent -- every factor passes except body "
                        "isolation"),
            "control": ("probe-A shape with a consistent x anchor: a genuine "
                        "folio pattern, still stripped (over-correction "
                        "guard)"),
        },
        "before_pre_round_code": before,
        "after_current_code": after,
        "load_bearing": {
            "probe_a": "horizontal anchor (factor 4) alone intercepts",
            "probe_b": "body isolation (factor 5) alone intercepts",
        },
    }


def _version_determination() -> dict:
    """Empirical old/new block-semantics comparison behind the v2 bump."""
    from reaserch_agent.route_pdf_source import _read_pdf_blocks
    import fitz

    prer15 = _load_historical_source_module(
        PRER15_COMMIT, "reaserch_agent._route_pdf_source_prer15")

    def build(pages: list[list[str]]) -> bytes:
        document = fitz.open()
        for index, folios in enumerate(pages):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), f"Page {index} body text.",
                             fontsize=10)
            if folios:
                folio = folios[0]
                width = fitz.get_text_length(folio, fontsize=10)
                page.insert_text((306 - width / 2, 747), folio, fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    cases = {
        "bare_digits_centered": (
            build([[str(i)] for i in range(1, 5)]), 8, 4),
        "s_prefix_centered": (
            build([[f"S {i}"] for i in range(1, 5)]), 8, 4),
        "no_folio": (build([[] for _ in range(4)]), 4, 4),
        "probe_a_shape": (_probe_a_bytes(), 9, 9),
        "probe_b_shape": (_probe_b_bytes(), 9, 9),
    }
    rows = []
    for name, (raw, old_count, new_count) in cases.items():
        old_blocks, old_issue = prer15._read_pdf_blocks(raw)
        new_blocks, new_issue = _read_pdf_blocks(raw)
        if old_issue is not None or new_issue is not None:
            _fail(f"comparison case {name} must parse under both parsers")
        old_texts = [block.text for block in old_blocks]
        new_texts = [block.text for block in new_blocks]
        if len(old_texts) != old_count or len(new_texts) != new_count:
            _fail(f"comparison case {name} drifted: "
                  f"{len(old_texts)}/{len(new_texts)}")
        same = old_texts == new_texts
        if same != (old_count == new_count):
            _fail(f"comparison case {name} same/diff expectation drifted")
        rows.append({
            "case": name,
            "pre_r15_blocks": old_count,
            "current_blocks": new_count,
            "block_sequence_identical": same,
        })
    changed = [row["case"] for row in rows if not row["block_sequence_identical"]]
    if changed != ["bare_digits_centered", "s_prefix_centered"]:
        _fail(f"semantic-change set drifted: {changed}")

    # r13/r13b impact audit: the archives pin no version string.
    archive_files = [
        OUT_DIR / "local-revision-r13-replay.json",
        OUT_DIR / "local-revision-r13-audit.json",
        OUT_DIR / "local-revision-r13b-replay.json",
        OUT_DIR / "local-revision-r13b-audit.json",
    ]
    for path in archive_files:
        if "route_pdf_groups/v" in path.read_text(encoding="utf-8"):
            _fail(f"{path.name} unexpectedly pins a parser version string")
    runner_sources = (
        (OUT_DIR / "local-revision-r13.py").read_text(encoding="utf-8")
        + (OUT_DIR / "local-revision-r13b.py").read_text(encoding="utf-8")
    )
    if "parser_version" in runner_sources or "PDF_GROUP_PARSER" in runner_sources:
        _fail("r13/r13b runners unexpectedly reference the parser version")

    from reaserch_agent.route_pdf_groups import PDF_GROUP_PARSER_VERSION
    if PDF_GROUP_PARSER_VERSION != "route_pdf_groups/v2":
        _fail("PDF_GROUP_PARSER_VERSION must be bumped to v2")

    return {
        "method": ("pre-r15 parser loaded from git show c429443 into a "
                   "package-prefixed module; five synthetic inputs parsed "
                   "under both parsers; block sequences compared"),
        "samples": rows,
        "conclusion": {
            "semver_reading": ("route_pdf_groups.py line ~50: bump when the "
                               "enumerator or route_pdf_source changes group "
                               "block semantics"),
            "finding": ("previously parseable folio-bearing inputs produce "
                        "shorter block sequences under the tightened rule, "
                        "so group block semantics DID change for previously "
                        "parseable inputs"),
            "decision": "PDF_GROUP_PARSER_VERSION bumped to route_pdf_groups/v2",
        },
        "r13_r13b_impact": {
            "version_string_occurrences_in_r13_archives": 0,
            "r13_r13b_runners_reference_parser_version": False,
            "pinning_change": ("none needed: the pinned replay bytes carry no "
                               "version string, so the bump falsifies nothing; "
                               "historical archives keep their original bytes"),
        },
    }


def _document_anchors() -> dict:
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

    # Wu SI still parses identically; the W1 sentence binds verbatim.
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
    if bindings["needle"] != W1_NEEDLE_LOCATOR:
        _fail(f"W1 needle locator drifted: {bindings['needle']}")
    if bindings["full_sentence"] != W1_SENTENCE_LOCATOR:
        _fail(f"W1 full-sentence locator drifted: {bindings['full_sentence']}")

    # Wu main still abstains on the p10 chart tick, with structured detail.
    main_blocks, main_issue, main_report = _read_pdf_blocks_with_report(
        (ROOT / WU_MAIN_PDF).read_bytes())
    if main_blocks is not None or main_issue != "pdf_column_layout_ambiguous":
        _fail("Wu main must still abstain at ingestion")
    abstention = (main_report["abstention"] or {})
    culprit = (abstention.get("culprits") or [{}])[0]
    if not (culprit.get("page") == 10 and culprit.get("text") == "36"):
        _fail("Wu main abstention culprit drifted")
    return {
        "a01": {
            "blocks": len(a01_blocks),
            "blocks_sha256": _blocks_pin(a01_blocks),
            "folios_stripped": 0,
            "note": ("the blanket r15 claim 'no previously parseable document "
                     "changes' is withdrawn; what holds is this anchor: A01 "
                     "parses byte-identically with zero folios stripped"),
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
                     "0.90h folio band, and never becomes a candidate"),
        },
    }


def _pool_census() -> dict:
    from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups

    rows = []
    for tag, rel in POOL:
        path = ROOT / rel
        result = enumerate_pdf_experimental_groups(
            {"pool_" + tag: path}, source_root=path.parent)
        reasons = sorted({d.reason_code for d in result.diagnostics})
        rows.append({"candidate": tag, "groups": len(result.groups),
                     "reasons": reasons})
    if any(row["groups"] for row in rows):
        _fail("pool census unexpectedly produced a group")
    for row in rows:
        if row["reasons"] != EXPECTED_CENSUS[row["candidate"]]:
            _fail(f"r16 census drifted for {row['candidate']}: {row['reasons']}")
    identical_to_r15 = None
    if R15_REPLAY.is_file():
        archived = json.loads(R15_REPLAY.read_text(encoding="utf-8"))
        r15_rows = {
            row["candidate"]: row["reasons"]
            for row in archived["layer_results"]["pool_census"]["rows"]
        }
        identical_to_r15 = all(
            row["reasons"] == r15_rows.get(row["candidate"]) for row in rows)
        if not identical_to_r15:
            _fail("r16 pool census differs from the r15 post-fix census")
    return {
        "rows": rows,
        "identical_to_r15_post_census": identical_to_r15,
        "note": ("the tightened folio rule changes no pool candidate: Wu SI "
                 "folios keep validating (consistent centered anchor, one "
                 "line per block) and every abstaining candidate keeps its "
                 "reason"),
    }


def _caption_block_distribution() -> dict:
    """Verify the S14/S15 caption continuation original-block distribution."""
    import fitz

    from reaserch_agent.route_pdf_source import _read_pdf_blocks_with_report

    raw = (ROOT / WU_SI_PDF).read_bytes()
    blocks, issue, _ = _read_pdf_blocks_with_report(raw)
    if issue or not blocks:
        _fail("Wu SI must parse for the caption distribution check")
    final = {(b.page, b.number): b for b in blocks}
    document = fitz.open(stream=raw, filetype="pdf")
    pages = {}
    for page_index, page_no, fig, last_final in (
            (13, 14, "S13", 11), (14, 15, "S14", 12)):
        text_blocks = {}
        for bno, block in enumerate(
                document[page_index].get_text("dict").get("blocks", [])):
            if block.get("type") != 0:
                continue
            lines = [
                "".join(span["text"] for span in line["spans"]).strip()
                for line in block.get("lines", [])
            ]
            lines = [text for text in lines if text]
            if lines:
                text_blocks[bno] = lines
        head = next(
            (bno for bno, lines in text_blocks.items()
             if lines[0].startswith(f"Supplementary Fig. {fig}.")),
            None,
        )
        if head is None:
            _fail(f"caption head for {fig} not found on page {page_no}")
        cont1, cont2 = head + 1, head + 2
        if not (len(text_blocks[head]) == len(text_blocks[cont1])
                == len(text_blocks[cont2]) == 1):
            _fail(f"{fig} caption blocks must carry exactly one line each")
        head_line = text_blocks[head][0]
        # Final-sequence mapping: b1/b2 are the bold-split head line, b3/b4
        # are the two continuation lines from the NEXT two original blocks.
        f = {i: final[(page_no, i)].text for i in range(1, last_final + 1)}
        if f[1] != "Supplementary Fig.":
            _fail(f"{fig} final b1 must be the bold label split")
        if f"{f[1]} {f[2]}" != head_line:
            _fail(f"{fig} final b1+b2 must recombine into the head line")
        if f[3] != text_blocks[cont1][0] or f[4] != text_blocks[cont2][0]:
            _fail(f"{fig} final b3/b4 must come from original blocks "
                  f"{cont1}/{cont2}")
        # The synthesis paragraph below is one original block per line too.
        paragraph_blocks = list(range(head + 4, head + 4 + (last_final - 4)))
        for offset, bno in enumerate(paragraph_blocks):
            if len(text_blocks.get(bno, [])) != 1:
                _fail(f"{fig} paragraph block {bno} must carry one line")
            if f[5 + offset] != text_blocks[bno][0]:
                _fail(f"{fig} final b{5 + offset} must come from block {bno}")
        pages[f"page_s{page_no}"] = {
            "caption_head_original_block": head,
            "continuation_original_blocks": [cont1, cont2],
            "lines_per_original_block": 1,
            "final_mapping": {
                "b1_b2_from": head,
                "b3_from": cont1,
                "b4_from": cont2,
                "synthesis_paragraph_from": paragraph_blocks,
            },
            "caption_flag_on_region": False,
        }
    document.close()
    return {
        "verified": pages,
        "finding": ("every line of the S14/S15 caption paragraph is its own "
                    "original PyMuPDF block; only the head line's bold split "
                    "(final b1+b2) shares an original block"),
        "r15_draft_phrase_impact": ("the 'same-original-block continuations' "
                                    "phrase drafted at r15 would mark only "
                                    "final b1/b2 as caption and miss b3, b4, "
                                    "and the whole synthesis paragraph"),
    }


def _rule_eligibility() -> dict:
    """Re-score W1/W2 against the frozen conventions (unchanged resource)."""
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
        "note": ("negative eligibility pre-screen carried from r15; the "
                 "conventions resource is byte-identical, so the outcome is "
                 "unchanged and remains a pre-screen, not a pass"),
    }


def _boundary_supplement(distribution: dict) -> dict:
    """Caption-boundary design supplement (recorded, NOT implemented)."""
    return {
        "verified_distribution": distribution,
        "continuation_design": [
            ("continuation binds by vertical reading order across original "
             "blocks: contiguous y progression, consistent font size, and a "
             "shared left edge -- never by shared original block, which the "
             "verified distribution shows would keep only the caption head"),
            ("termination: the caption region ends at the first line that "
             "breaks the continuation pattern; on the verified pages the "
             "synthesis paragraph is visually indistinguishable from caption "
             "prose, which is exactly why recognition stays unimplemented"),
            ("quote-binding consequence (unchanged): caption blocks stay "
             "non-quotable anchors and the binder may skip at most one block "
             "inside a span -- it may not jump an entire caption region"),
        ],
        "stays_closed_this_round": [
            "no caption-recognition change is implemented",
            "caption quotation is NOT opened",
            "no SI grouping implementation",
            "the workflow interception of incomplete sources is unchanged",
        ],
    }


def _anchors() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."

    # (a) A01 regression: r13b archive-pinned runner as a subprocess.
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

    # (c) Folio test module (now 20 tests) and the 339 target set.
    folio_proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         "-m", "unittest", "reaserch_agent.test_route_pdf_folio"],
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env,
    )
    folio_out = (folio_proc.stderr or "") + (folio_proc.stdout or "")
    if "Ran 20 tests" not in folio_out or "OK" not in folio_out:
        _fail(f"folio tests failed: {folio_out[-400:]}")
    target_proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         "-m", "unittest"] + TARGET_SET_MODULES,
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env,
    )
    target_out = (target_proc.stderr or "") + (target_proc.stdout or "")
    if f"Ran {TARGET_SET_SIZE} tests" not in target_out or "OK" not in target_out:
        _fail(f"339 target set failed: {target_out[-400:]}")

    return {
        "a01_regression": {
            "mechanism": "r13b runner re-executed as a subprocess",
            "double_run_byte_identical": True,
            "replay_matches_r13_archive": True,
            "ms7a_out": "BLOCKED (retained_object_mention_precedes_operation)",
        },
        "diagnostics_module_zero_proof_dependency": True,
        "folio_test_module_20_ok": True,
        "target_set_339_ok": True,
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
        "meaning": ("the signed SI still parses and reaches the section "
                    "gate; the signed main paper still abstains on the "
                    "two-column chart layout"),
    }


def _build_replay() -> dict:
    baseline = _baseline()
    misdelete = _misdelete_fix_verification()
    version = _version_determination()
    documents = _document_anchors()
    census = _pool_census()
    distribution = _caption_block_distribution()
    boundary = _boundary_supplement(distribution)
    eligibility = _rule_eligibility()
    attested = _wu_attested_layer()
    anchors = _anchors()
    return {
        "schema_version": "g2a_ingestion_folio/v16",
        "round": ("r16 (G2a follow-up): folio misdelete tightening (five "
                  "factors) + parser-version correction (v2) + caption "
                  "boundary design supplement"),
        "model_generated": False,
        "model_called": False,
        "baseline": baseline,
        "code_changes": {
            "files": CHANGED_SOURCE_FILES,
            "changed_test_file": {FOLIO_TEST_FILE: FOLIO_TEST_SHA256},
            "semantics_note": (
                "conventions, protocol schemas, and every rule resource are "
                "byte-identical to the frozen baseline; ingestion block "
                "semantics changed for previously parseable folio-bearing "
                "inputs (empirically pinned in version_determination), so "
                "PDF_GROUP_PARSER_VERSION is bumped to route_pdf_groups/v2; "
                "the A01 anchor is asserted byte-identical"),
        },
        "misdelete_fix": {
            "root_cause": ("the r15 three-factor rule validated any bare "
                           "incrementing number in the bottom band as a "
                           "folio; a measured value printed alone under its "
                           "label (or sharing the label's block) satisfied "
                           "all three and was deleted"),
            "five_factors": [
                "standalone folio form: fullmatch [A-Za-z]{0,2}\\s*[0-9]{1,4} "
                "in the bottom band (y0 > 0.90h)",
                "at most one candidate per page; quorum of three pages",
                "uniform alphabetic prefix and page-order lockstep increment",
                "vertical anchor: y0 within 4 pt of the median",
                "horizontal anchor: center x within 4 pt of the median (new)",
                "body isolation: the candidate is the only non-empty line "
                "of its original PDF block (new)",
            ],
            "doubt_keeps_content": ("when context is doubtful the line is "
                                    "kept: keeping content is always the "
                                    "safe failure; no page is removed, no "
                                    "PDF byte is modified"),
            "verification": misdelete,
        },
        "version_determination": version,
        "document_anchors": documents,
        "pool_census": census,
        "boundary_design_supplement": boundary,
        "rule_eligibility": eligibility,
        "layer_results": {
            "attestation": {"status": "verified", "documents": 2},
            "group_enumeration": {
                "status": "reached_section_gate",
                "groups": 0,
                "diagnostic_reasons": attested["diagnostic_reasons"],
                "meaning": ("unchanged from r15: the SI parses and stops at "
                            "experimental_section_missing; the main paper "
                            "still abstains at ingestion"),
            },
            "proposal_extraction": {"status": "not_reached"},
            "local_revision": {"status": "not_reached"},
            "association": {"status": "not_reached"},
            "receipt": {"status": "not_reached"},
        },
        "dag_consumption": {
            "observed": False,
            "note": ("still unobservable (0 groups); W1/W2 stay ineligible "
                     "under the frozen v1 rules; nothing here is claimed as "
                     "a pass"),
        },
        "wording_corrections": [
            {"item": "previously-parseable-input preservation",
             "r15_wording": ("'ingestion block semantics changed ONLY for "
                             "inputs that previously failed closed (no "
                             "previously parseable document changes)'"),
             "corrected": ("scoped to the asserted anchor: A01 parses "
                           "byte-identically; other previously parseable "
                           "folio-bearing inputs change by design (shorter "
                           "block sequences), which is exactly why "
                           "PDF_GROUP_PARSER_VERSION moved to v2"),
             "evidence": "version_determination.samples"},
        ],
        "terminology_corrections": [
            {"item": "pool census framing",
             "r14_wording": "every local cross-paper candidate abstains at "
                            "ingestion (framed as 8/8 layout problems)",
             "corrected": "6 layout + 1 missing-section + 1 oversize before "
                          "r15; 5 layout + 2 missing-section + 1 oversize "
                          "after r15; unchanged by r16"},
            {"item": "element overlap",
             "r14_wording": "zero element overlap between Ni-Mo and Ni-Fe",
             "corrected": "withdrawn: Ni-Mo and Ni-Fe are different material "
                          "systems SHARING the Ni element"},
            {"item": "W1 presupposition",
             "r14_wording": "W1 treated as the prospective multi-hop positive",
             "corrected": "W1 is not presupposed anything; the frozen-rule "
                          "eligibility table scores W1 and W2 ineligible, so "
                          "neither can be a positive under v1 rules"},
            {"item": "previously-parseable-input preservation (r15)",
             "r15_wording": "no previously parseable document changes",
             "corrected": "withdrawn as a blanket claim; holds for the A01 "
                          "anchor only, other folio-bearing inputs change by "
                          "design under v2"},
        ],
        "separate_counters": {
            "field_proofs_succeeded": 0,
            "group_receipt_pass": 0,
            "protocol_admitted": 0,
            "published": 0,
        },
        "anchors": anchors,
        "fixed_constraints": {
            "r7_to_r15_archives_untouched": True,
            "contracts_tree_untouched": True,
            "diagnostics_module_semantics_untouched": True,
            "conventions_no_v1_expansion": True,
            "tokens_consumed_by_this_runner": 0,
            "model_not_called": True,
            "device_not_run": True,
            "eight_question_rerun": False,
            "ms7a_cascade_still_blocked": True,
            "tracked_modifications_limited_to_three_files": True,
        },
    }


def _audit() -> dict:
    return {
        "schema_version": "g2a_ingestion_folio/v16",
        "model_generated": False,
        "scope": ("r16 (G2a follow-up): folio misdelete tightening in the "
                  "PDF ingestion layer (three factors -> five), the "
                  "parser-version correction to route_pdf_groups/v2 with "
                  "empirical old/new evidence, and a caption-boundary design "
                  "supplement; no model call, no conventions/protocol "
                  "semantic change, no caption release, no SI grouping"),
        "audit": [
            {"kind": "changed_files",
             "items": [
                 "reaserch_agent/route_pdf_source.py (five-factor validated "
                 "folio recognition: + horizontal anchor, + body isolation)",
                 "reaserch_agent/route_pdf_groups.py (PDF_GROUP_PARSER_VERSION "
                 "bumped to route_pdf_groups/v2 with the rationale comment)",
                 "reaserch_agent/test_route_pdf_folio.py (+ "
                 "MisdeleteRegressionTest: 5 tests; module now 20 tests)",
                 "result/operation-structure-20260928/local-revision-r16.py",
                 "result/operation-structure-20260928/local-revision-r16-*.json",
                 "docs/field_semantic_gate_r16_checkpoint_20261004.md",
             ]},
            {"kind": "capability_change",
             "detail": ("previously parseable inputs whose bottom band "
                        "carries a value pattern (incrementing bare numbers "
                        "with a drifting x anchor, or values sharing a block "
                        "with their label) now keep those values instead of "
                        "deleting them; genuine folio patterns (consistent "
                        "anchor, isolated line) are still stripped -- Wu SI "
                        "keeps its 1041 blocks / 79 folios and A01 is "
                        "byte-identical")},
            {"kind": "version_handling",
             "detail": ("block semantics changed for previously parseable "
                        "folio-bearing inputs, so PDF_GROUP_PARSER_VERSION "
                        "moved v1 -> v2 per the route_pdf_groups.py line-50 "
                        "convention; the r13/r13b archives pin no version "
                        "string (grep-verified), so no pin was rewritten and "
                        "historical archives keep their original bytes")},
            {"kind": "honest_endpoints",
             "detail": ("W1/W2 ineligible under the frozen v1 rules; the "
                        "caption continuation/termination boundary is a "
                        "recorded design with a verified block distribution, "
                        "not an implementation; dag_consumption remains "
                        "unobserved and unclaimed")},
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

    (OUT_DIR / "local-revision-r16-replay.json").write_text(
        replay_text, encoding="utf-8", newline="\n")
    (OUT_DIR / "local-revision-r16-audit.json").write_text(
        audit_text, encoding="utf-8", newline="\n")
    summary = {
        "probe_a_values_kept": (
            replay["misdelete_fix"]["verification"]
            ["after_current_code"]["probe_a"]["values_kept"]),
        "probe_b_values_kept": (
            replay["misdelete_fix"]["verification"]
            ["after_current_code"]["probe_b"]["values_kept"]),
        "pre_round_stripped_values": (
            replay["misdelete_fix"]["verification"]
            ["before_pre_round_code"]["probe_a"]["stripped"]),
        "control_still_stripped": (
            replay["misdelete_fix"]["verification"]
            ["after_current_code"]["control"]["stripped"]),
        "parser_version": "route_pdf_groups/v2",
        "semantic_change_cases": [
            row["case"] for row in
            replay["version_determination"]["samples"]
            if not row["block_sequence_identical"]],
        "r13_archives_pin_no_version": True,
        "si_blocks": replay["document_anchors"]["wu_si"]["blocks"],
        "si_folios_stripped": replay["document_anchors"]["wu_si"]["folios_stripped"],
        "w1_sentence_binding": (
            replay["document_anchors"]["wu_si"]["w1_sentence_binding"]),
        "main_still_abstains": (
            replay["document_anchors"]["wu_main"]["issue"]
            == "pdf_column_layout_ambiguous"),
        "a01_parse_unchanged": (
            replay["document_anchors"]["a01"]["blocks_sha256"]
            == A01_BLOCKS_SHA256),
        "pool_census_identical_to_r15": (
            replay["pool_census"]["identical_to_r15_post_census"]),
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
