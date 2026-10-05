"""Round-17 (G2a follow-up-2): folio neighborhood body-text check + parser
version bump to v3 + SI design-note corrections.  No model is called.

Scope of the code change (exactly three tracked files, all in the PDF
ingestion layer and its tests):

- ``reaserch_agent/route_pdf_source.py`` adds the sixth validated-folio
  factor: a neighborhood body-text check.  The r16 five-factor rule still
  deleted real experiment values when a bare value (e.g. a final-pH reading)
  sat in its own original block directly under its label in an ADJACENT
  block -- sole block occupancy did not prove detachment from body text
  (acceptance probe C: label ``Final pH:`` at (280,729), value at (280,747),
  glyph-box gap ~4.26 pt at fontsize 10).  The new factor rejects a
  candidate when any other text line on the page overlaps it horizontally
  and stands within one standard line height (1.2 em of the candidate's own
  font size) of it, above or below.  The scale is derived from font metrics
  only; no probe text, coordinate, or page position is consulted.  Doubtful
  context keeps the line: keeping content is always the safe failure.
- ``reaserch_agent/route_pdf_groups.py`` bumps PDF_GROUP_PARSER_VERSION to
  ``route_pdf_groups/v3``: the neighborhood check changes block semantics
  for previously parseable inputs whose bottom band carries label-adjacent
  values (they were stripped under v2 and are kept under v3 -- empirically
  pinned below against the 491ec37 parser).  The r13/r13b archives pin no
  version string (re-asserted inside this runner), so the bump falsifies
  nothing.
- ``reaserch_agent/test_route_pdf_folio.py`` gains a
  NeighborhoodBodyRegressionTest class (7 tests): the probe-C shape in both
  vertical directions, both sides of the line-height scale boundary, a
  horizontal-overlap specificity guard, a real-folio-spacing positive in the
  Wu SI's geometry, and a pre-r17 old/new block-semantics comparison; the
  pre-r15 comparison gains the probe-C case; the version assertion moves to
  v3.  The module now has 27 tests.

What this runner establishes, deterministically and token-free:

1. Baseline freeze: pre-round HEAD 491ec37, conventions.json sha256 (frozen,
   unchanged), prompt-builder sha256 (unchanged), A01 source digest, Wu-2025
   PDF digests, the r16 checkpoint sha256 (untouched archive), and the
   sha256 of the three changed files.  Any drift fails the runner.
2. Neighborhood fix before/after: the pre-round code (loaded from git show
   491ec37) strips the values 11/12/13 in the probe-C shape; the current
   code keeps them in both vertical directions and below the line-height
   scale, and still strips beyond the scale, without horizontal overlap,
   and in the r16 control shape.  The new factor is load-bearing: probe C
   passes every r16 factor.
3. SI gap distribution: every one of the 79 validated SI folios is measured
   against its nearest horizontally-overlapping text line with the parser's
   own coordinates -- minimum gap 14.16 pt (page S 78), median ~372 pt,
   every gap comfortably above the 1.2 em scale (12.02 pt at the folio font
   size 10.02), while probe C sits at 4.26 pt.  The two populations are
   cleanly separated.
4. Version determination evidence: synthetic inputs parsed under the
   pre-r17 parser (git show 491ec37) and the current parser -- three
   label-adjacent shapes change block sequences (6 -> 9 blocks), isolated-
   folio and folio-free shapes are unchanged, and the real Wu SI and A01
   block sequences are byte-identical under both parsers.  Conclusion: bump
   to v3 per the route_pdf_groups.py line-50 convention.
5. Anchors held: A01 byte-identical (1289 blocks, pinned sha256, zero
   folios); Wu SI 1041 blocks / 79 folios with the W1 sentence binding
   verbatim (pdf:p14:b9-p14:b11); Wu main still abstains on the p10 chart
   tick "36"; the 8-candidate pool census is row-by-row identical to the
   r15 and r16 censuses; the Wu attested layer still verifies both documents
   and reaches the same endpoints.
6. Caption-boundary corrections (design, NOT implemented): the r16
   original-block numbering difference is traced to the extraction mode
   (the r16 runner's internal probe used sort=False, the production parser
   uses sort=True -- same lines, indices shifted by one); only b3/b4 are
   missed caption continuation lines (from b5 on the synthesis paragraph is
   body text that should stay non-caption anyway); the "visually
   indistinguishable" claim is withdrawn -- measured boundary signals exist
   (a 43.37 pt vertical gap and a 24.02 pt first-line indent) and are
   recorded as to-be-verified signals.  The binder's at-most-one-skipped-
   block hop limit is unchanged.
7. Regression anchors: r13b archive-pinned runner re-executed as a
   subprocess (replay matches the r13 archive, double-run byte-identical,
   ms7a.out stays BLOCKED), 3E diagnostics zero-proof-dependency source
   audit, the folio test module (27 tests) as a subprocess, and the 346
   target set as a subprocess.

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
from statistics import median

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "result" / "operation-structure-20260928"
WU = ROOT / "result" / "wu2025-real-input-20261004"
R15_REPLAY = OUT_DIR / "local-revision-r15-replay.json"
R16_REPLAY = OUT_DIR / "local-revision-r16-replay.json"

PRE_ROUND_HEAD = "491ec375dd4cd3b5d6b4160580ebc6a8516338e5"
CONVENTIONS_SHA256 = (
    "39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b"
)
PROMPT_BUILDER_FILE = "reaserch_agent/route_pdf_group_extraction.py"
PROMPT_BUILDER_SHA256 = (
    "24864d9af522666b15a1690b6c9ba829bebfa3047545d82adc3e3dff331486f7"
)
CHANGED_SOURCE_FILES = {
    "reaserch_agent/route_pdf_source.py": (
        "51b7be8b1e99847ea26d08568aef83b65b9168945082050e1b72eee3d0f51d7a"
    ),
    "reaserch_agent/route_pdf_groups.py": (
        "d8eecc83b1dc71fadc5541668f4ad3c3ea210a9a8c97ca31066a548795766094"
    ),
}
FOLIO_TEST_FILE = "reaserch_agent/test_route_pdf_folio.py"
FOLIO_TEST_SHA256 = (
    "e53c52cbf8bd5a99621792b12fae55e9e25e3b9781ae26b3cca2bed311ae017a"
)
FOLIO_TEST_COUNT = 27
R16_CHECKPOINT_FILE = "docs/field_semantic_gate_r16_checkpoint_20261004.md"
R16_CHECKPOINT_SHA256 = (
    "2cb78990ce6fa7d44300faa09801d25597ffaf4eff875965842fc5252637570e"
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
TARGET_SET_SIZE = 346


def _fail(message: str) -> None:
    raise SystemExit(f"r17 acceptance failed: {message}")


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
            _fail(f"{rel} drifted from the round-17 recorded state")
    if _sha256_file(ROOT / FOLIO_TEST_FILE) != FOLIO_TEST_SHA256:
        _fail("folio test file drifted from the round-17 recorded state")
    if _sha256_file(ROOT / R16_CHECKPOINT_FILE) != R16_CHECKPOINT_SHA256:
        _fail("r16 checkpoint drifted: prior checkpoints are append-only")
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
            "tracked modifications outside the three round-17 files: "
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
        "r16_checkpoint_sha256_untouched": R16_CHECKPOINT_SHA256,
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


def _label_value_bytes(label_baseline: float = 729.0,
                       value_baseline: float = 747.0,
                       label_x: float = 280.0) -> bytes:
    """Probe C family: label and value in two adjacent original blocks.

    Default geometry is the acceptance probe C: label at (280,729), value at
    (280,747), same x, glyph-box gap ~4.26 pt at fontsize 10.
    """
    import fitz

    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=612, height=792)
        page.insert_text((70, 400), "The sample was measured carefully.",
                         fontsize=10)
        page.insert_text((label_x, label_baseline), "Final pH:", fontsize=10)
        page.insert_text((280.0, value_baseline), str(11 + index),
                         fontsize=10)
    raw = document.tobytes()
    document.close()
    return raw


def _si_shaped_bytes() -> bytes:
    """The Wu SI's own folio geometry: a size-12 text line ~16.7 pt above a
    centered size-10 folio (the real SI minimum measured gap is 14.16 pt)."""
    import fitz

    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=612, height=792)
        page.insert_text((70, 400), "The sample was measured carefully.",
                         fontsize=10)
        page.insert_text((240, 716), "Electrolysis reference text.",
                         fontsize=12)
        folio = f"S {11 + index}"
        width = fitz.get_text_length(folio, fontsize=10)
        page.insert_text((306 - width / 2, 747), folio, fontsize=10)
    raw = document.tobytes()
    document.close()
    return raw


def _probe_outcome(module, raw: bytes) -> dict:
    blocks, issue, report = module._read_pdf_blocks_with_report(raw)
    texts = [block.text for block in blocks] if blocks else []
    joined = " ".join(texts)
    return {
        "issue": issue,
        "blocks": len(texts),
        "stripped": [item["text"] for item in report.get("folios_stripped", [])],
        "values_kept": [v for v in ("11", "12", "13") if v in joined],
        "s_folios_kept": [v for v in ("S 11", "S 12", "S 13") if v in joined],
    }


def _neighborhood_fix_verification() -> dict:
    from reaserch_agent import route_pdf_source as current

    pre_round = _load_historical_source_module(
        PRE_ROUND_HEAD, "reaserch_agent._route_pdf_source_r16")

    before = {
        "probe_c": _probe_outcome(pre_round, _label_value_bytes()),
    }
    outcome = before["probe_c"]
    if outcome["issue"] is not None or outcome["blocks"] != 6:
        _fail(f"pre-round probe C drifted: {outcome}")
    if outcome["stripped"] != ["11", "12", "13"] or outcome["values_kept"]:
        _fail("pre-round probe C must strip the values (the reported gap)")

    after = {
        "probe_c": _probe_outcome(current, _label_value_bytes()),
        "label_below_value": _probe_outcome(
            current, _label_value_bytes(label_baseline=747.0,
                                        value_baseline=729.0)),
        "gap_just_below_scale": _probe_outcome(
            current, _label_value_bytes(label_baseline=722.0)),
        "gap_just_above_scale": _probe_outcome(
            current, _label_value_bytes(label_baseline=721.0)),
        "non_overlapping_label": _probe_outcome(
            current, _label_value_bytes(label_x=70.0)),
        "si_shaped_spacing": _probe_outcome(current, _si_shaped_bytes()),
        "control_isolated": _probe_outcome(current, _probe_control_bytes()),
        "probe_a": _probe_outcome(current, _probe_a_bytes()),
        "probe_b": _probe_outcome(current, _probe_b_bytes()),
    }
    for name in ("probe_c", "label_below_value", "gap_just_below_scale",
                 "probe_a", "probe_b"):
        outcome = after[name]
        if outcome["issue"] is not None or outcome["blocks"] != 9:
            _fail(f"post-fix {name} drifted: {outcome}")
        if outcome["stripped"] or outcome["values_kept"] != ["11", "12", "13"]:
            _fail(f"post-fix {name} must keep every value")
    for name, stripped in (("gap_just_above_scale", ["11", "12", "13"]),
                           ("non_overlapping_label", ["11", "12", "13"]),
                           ("control_isolated", ["11", "12", "13"])):
        outcome = after[name]
        if outcome["stripped"] != stripped:
            _fail(f"post-fix {name} must still strip: {outcome}")
    for name, count in (("gap_just_above_scale", 6),
                        ("non_overlapping_label", 6),
                        ("control_isolated", 3),
                        ("si_shaped_spacing", 6)):
        if after[name]["blocks"] != count:
            _fail(f"post-fix {name} block count drifted: {after[name]}")
    si_shaped = after["si_shaped_spacing"]
    if si_shaped["stripped"] != ["S 11", "S 12", "S 13"]:
        _fail(f"SI-shaped spacing must still strip: {si_shaped}")

    return {
        "probes": {
            "probe_c": ("3 pages 612x792; 'Final pH:' label at (280,729) and "
                        "bare values 11/12/13 at (280,747) sit in two "
                        "ADJACENT original blocks at the same x; glyph-box "
                        "gap ~4.26 pt at fontsize 10 -- every r16 factor "
                        "passes; only the neighborhood check intercepts"),
            "label_below_value": ("probe C mirrored vertically: value at "
                                  "(280,729), label at (280,747) -- the "
                                  "check is bidirectional"),
            "gap_just_below_scale": ("baseline delta 25 pt -> glyph-box gap "
                                     "~11.3 pt, just under the 1.2 em scale "
                                     "(12.0 pt): still body-adjacent, kept"),
            "gap_just_above_scale": ("baseline delta 26 pt -> glyph-box gap "
                                     "~12.3 pt, just over the scale: the "
                                     "values validate as folios again"),
            "non_overlapping_label": ("label at x=70 vs value at x=280: no "
                                      "horizontal overlap, so the label is "
                                      "not a neighbor and the values strip"),
            "si_shaped_spacing": ("size-12 text ~16.7 pt above a centered "
                                  "size-10 folio (the real SI geometry): "
                                  "still stripped"),
            "control_isolated": ("r16 control: x-consistent bare values with "
                                 "no label nearby still strip"),
            "probe_a": "r16 probe A regression (x drift): values kept",
            "probe_b": "r16 probe B regression (shared block): values kept",
        },
        "before_pre_round_code": before,
        "after_current_code": after,
        "load_bearing": {
            "probe_c": ("neighborhood body-text check (factor 6) alone "
                        "intercepts: bottom band, one candidate per page, "
                        "quorum, uniform prefix, lockstep, y anchor, x "
                        "anchor, and same-block isolation all pass"),
        },
        "threshold_derivation": ("one standard line height = 1.2 x the "
                                 "candidate's own font size; no probe text, "
                                 "coordinate, or page position is consulted"),
    }


def _si_gap_distribution() -> dict:
    """Measure every validated SI folio against its nearest horizontally
    overlapping text line, using the parser's own extraction coordinates.

    The extraction below mirrors ``_read_pdf_blocks_with_report`` exactly
    (``page.get_text("dict", sort=True)``, same line filtering), keeping the
    full glyph bbox so the vertical gap is the parser's own geometry.  The
    candidate keys come from the CURRENT ``_validated_folio_lines``.
    """
    import fitz

    from reaserch_agent.route_pdf_source import (
        _FOLIO_BODY_GAP_FACTOR,
        _line_parts,
        _PdfLine,
        _validated_folio_lines,
    )

    raw = (ROOT / WU_SI_PDF).read_bytes()
    pages = []  # [(width, [records])] with record = {"line", "y1"}
    with fitz.open(stream=raw, filetype="pdf") as document:
        for page_number, page in enumerate(document, start=1):
            records = []
            for original_block, item in enumerate(
                    page.get_text("dict", sort=True).get("blocks", [])):
                if item.get("type") != 0:
                    continue
                for original_line, line in enumerate(item.get("lines", [])):
                    spans = [s for s in line.get("spans", [])
                             if str(s.get("text") or "")]
                    if not spans:
                        continue
                    text = "".join(str(s["text"]) for s in spans).strip()
                    if not text:
                        continue
                    heading, body, hsize, bsize = _line_parts(spans)
                    x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                    records.append({
                        "line": _PdfLine(
                            page=page_number, original_block=original_block,
                            original_line=original_line,
                            page_height=page.rect.height,
                            x0=float(x0), x1=float(x1), y0=float(y0),
                            y1=float(y1), text=text, heading=heading,
                            body=body, heading_size=hsize, body_size=bsize,
                        ),
                        "y1": float(y1),
                    })
            pages.append((page.rect.width, records))
    keys = _validated_folio_lines(
        [(w, [r["line"] for r in recs]) for w, recs in pages])
    if len(keys) != WU_SI_FOLIOS_STRIPPED:
        _fail(f"SI validated folio count drifted: {len(keys)}")

    def nearest_gap(cand, records):
        best = None
        for rec in records:
            if rec is cand:
                continue
            other, cl = rec["line"], cand["line"]
            if other.x0 >= cl.x1 or cl.x0 >= other.x1:
                continue  # no horizontal overlap
            if other.y0 >= cand["y1"]:
                gap, direction = other.y0 - cand["y1"], "below"
            elif rec["y1"] <= cl.y0:
                gap, direction = cl.y0 - rec["y1"], "above"
            else:
                gap, direction = 0.0, "overlap"
            if best is None or gap < best[0]:
                best = (gap, direction, other.text,
                        other.body_size or other.heading_size)
        return best

    rows = []
    for _w, records in pages:
        for rec in records:
            line = rec["line"]
            key = (line.page, line.original_block, line.original_line)
            if key not in keys:
                continue
            size = line.body_size or line.heading_size
            best = nearest_gap(rec, records)
            rows.append({
                "page": line.page,
                "folio": line.text,
                "folio_size": round(size, 2),
                "gap": None if best is None else round(best[0], 2),
                "direction": None if best is None else best[1],
                "neighbor": None if best is None else best[2][:60],
                "neighbor_size": None if best is None else round(best[3], 2),
                "threshold": round(_FOLIO_BODY_GAP_FACTOR * size, 3),
            })
    if any(row["gap"] is None for row in rows):
        _fail("every SI folio is expected to have an overlapping neighbor")
    gaps = sorted(row["gap"] for row in rows)
    min_gap, med_gap, max_gap = gaps[0], median(gaps), gaps[-1]
    if abs(min_gap - 14.16) > 0.05:
        _fail(f"SI minimum folio gap drifted: {min_gap}")
    if abs(med_gap - 372.04) > 0.5:
        _fail(f"SI median folio gap drifted: {med_gap}")
    for row in rows:
        if row["gap"] < row["threshold"]:
            _fail(f"SI folio {row['folio']} would newly fail the check: {row}")
    tightest = min(rows, key=lambda row: row["gap"])
    return {
        "folios_measured": len(rows),
        "gap_min": min_gap,
        "gap_median": med_gap,
        "gap_max": max_gap,
        "tightest_folio": tightest,
        "threshold_rule": ("gap >= 1.2 x folio font size required to strip; "
                           "all 79 measured folios clear it"),
        "folio_font_sizes": sorted({row["folio_size"] for row in rows}),
        "per_folio_thresholds": sorted({row["threshold"] for row in rows}),
        "probe_c_gap": 4.26,
        "separation_ratio_min_vs_probe_c": round(min_gap / 4.26, 2),
        "smallest_five": sorted(rows, key=lambda row: row["gap"])[:5],
    }


def _version_determination() -> dict:
    """Empirical old/new block-semantics comparison behind the v3 bump."""
    from reaserch_agent.route_pdf_source import _read_pdf_blocks

    pre_round = _load_historical_source_module(
        PRE_ROUND_HEAD, "reaserch_agent._route_pdf_source_v2")

    def build_no_folio() -> bytes:
        import fitz

        document = fitz.open()
        for index in range(4):
            page = document.new_page(width=612, height=792)
            page.insert_text((70, 400), f"Page {index} body text.",
                             fontsize=10)
        raw = document.tobytes()
        document.close()
        return raw

    cases = {
        # name: (raw, pre_r17_block_count, current_block_count)
        "probe_c_shape": (_label_value_bytes(), 6, 9),
        "label_below_value": (
            _label_value_bytes(label_baseline=747.0, value_baseline=729.0),
            6, 9),
        "gap_just_below_scale": (
            _label_value_bytes(label_baseline=722.0), 6, 9),
        "gap_just_above_scale": (
            _label_value_bytes(label_baseline=721.0), 6, 6),
        "non_overlapping_label": (_label_value_bytes(label_x=70.0), 6, 6),
        "si_shaped_spacing": (_si_shaped_bytes(), 6, 6),
        "probe_a_shape": (_probe_a_bytes(), 9, 9),
        "probe_b_shape": (_probe_b_bytes(), 9, 9),
        "control_isolated": (_probe_control_bytes(), 3, 3),
        "no_folio": (build_no_folio(), 4, 4),
    }
    rows = []
    for name, (raw, old_count, new_count) in cases.items():
        old_blocks, old_issue = pre_round._read_pdf_blocks(raw)
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
        if not same:
            # The old sequence is the new one minus the kept value lines.
            iterator = iter(new_texts)
            if not all(text in iterator for text in old_texts):
                _fail(f"comparison case {name}: old is not a subsequence")
        rows.append({
            "case": name,
            "pre_r17_blocks": old_count,
            "current_blocks": new_count,
            "block_sequence_identical": same,
        })
    changed = sorted(row["case"] for row in rows
                     if not row["block_sequence_identical"])
    if changed != ["gap_just_below_scale", "label_below_value",
                   "probe_c_shape"]:
        _fail(f"semantic-change set drifted: {changed}")

    # Real documents: the SI and A01 block sequences are byte-identical
    # under both parsers (the SI's folios all clear the new check; A01 has
    # no folios at all).
    documents = {}
    for tag, rel, count in (
            ("wu2025_si", WU_SI_PDF, WU_SI_BLOCK_COUNT),
            ("a01", A01_PDF, A01_BLOCK_COUNT)):
        raw = (ROOT / rel).read_bytes()
        old_blocks, old_issue = pre_round._read_pdf_blocks(raw)
        new_blocks, new_issue = _read_pdf_blocks(raw)
        if old_issue is not None or new_issue is not None:
            _fail(f"{tag} must parse under both parsers")
        if len(old_blocks) != count or len(new_blocks) != count:
            _fail(f"{tag} block count drifted under dual parse")
        old_pin, new_pin = _blocks_pin(old_blocks), _blocks_pin(new_blocks)
        if old_pin != new_pin:
            _fail(f"{tag} block sequence changed under the new parser")
        documents[tag] = {
            "blocks": count,
            "blocks_sha256_both_parsers": new_pin,
            "block_sequence_identical": True,
        }

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
    if PDF_GROUP_PARSER_VERSION != "route_pdf_groups/v3":
        _fail("PDF_GROUP_PARSER_VERSION must be bumped to v3")

    return {
        "method": ("pre-r17 parser loaded from git show 491ec37 into a "
                   "package-prefixed module; ten synthetic inputs parsed "
                   "under both parsers (_read_pdf_blocks) with block "
                   "sequences compared; both real anchor documents "
                   "dual-parsed and pinned"),
        "samples": rows,
        "real_documents": documents,
        "conclusion": {
            "semver_reading": ("route_pdf_groups.py line ~50: bump when the "
                               "enumerator or route_pdf_source changes group "
                               "block semantics"),
            "finding": ("previously parseable inputs whose bottom band "
                        "carries label-adjacent incrementing values produced "
                        "shorter block sequences under v2 (values stripped) "
                        "and produce longer ones under v3 (values kept); "
                        "isolated-folio, folio-free, and both real anchor "
                        "documents are byte-identical under both parsers"),
            "decision": "PDF_GROUP_PARSER_VERSION bumped to route_pdf_groups/v3",
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
            _fail(f"r17 census drifted for {row['candidate']}: {row['reasons']}")
    identical_to = {}
    for tag, replay_path in (("r15", R15_REPLAY), ("r16", R16_REPLAY)):
        identical = None
        if replay_path.is_file():
            archived = json.loads(replay_path.read_text(encoding="utf-8"))
            # The r15 archive stores the census under layer_results; r16
            # moved it to the top level.  Accept the location each round used.
            census_node = archived.get("pool_census") or (
                archived["layer_results"]["pool_census"])
            archived_rows = {
                row["candidate"]: row["reasons"]
                for row in census_node["rows"]
            }
            identical = all(
                row["reasons"] == archived_rows.get(row["candidate"])
                for row in rows)
            if not identical:
                _fail(f"r17 pool census differs from the {tag} census")
        identical_to[tag] = identical
    return {
        "rows": rows,
        "identical_to_r15_post_census": identical_to["r15"],
        "identical_to_r16_census": identical_to["r16"],
        "note": ("the neighborhood check changes no pool candidate: every SI "
                 "folio clears the new proximity scale (see "
                 "si_gap_distribution) and every abstaining candidate keeps "
                 "its reason"),
    }


def _caption_boundary_evidence() -> dict:
    """Re-derive the S14/S15 caption geometry under BOTH extraction modes and
    measure the whitespace/indent boundary signals (design corrections)."""
    import fitz

    from reaserch_agent.route_pdf_source import _read_pdf_blocks_with_report

    raw = (ROOT / WU_SI_PDF).read_bytes()
    blocks, issue, _ = _read_pdf_blocks_with_report(raw)
    if issue or not blocks:
        _fail("Wu SI must parse for the caption boundary evidence")
    final = {(b.page, b.number): b for b in blocks}
    document = fitz.open(stream=raw, filetype="pdf")
    pages = {}
    for page_index, page_no, fig, last_final in (
            (13, 14, "S13", 11), (14, 15, "S14", 12)):
        by_mode = {}
        for sort in (False, True):
            text_blocks = {}
            for bno, block in enumerate(
                    document[page_index].get_text("dict", sort=sort)
                    .get("blocks", [])):
                if block.get("type") != 0:
                    continue
                lines = [
                    "".join(span["text"] for span in line["spans"]).strip()
                    for line in block.get("lines", [])
                ]
                lines = [text for text in lines if text]
                if lines:
                    text_blocks[bno] = (lines, tuple(float(v) for v in block["bbox"]))
            head = next(
                (bno for bno, (lines, _b) in text_blocks.items()
                 if lines[0].startswith(f"Supplementary Fig. {fig}.")),
                None,
            )
            if head is None:
                _fail(f"caption head for {fig} not found on page {page_no}")
            by_mode[sort] = (text_blocks, head)
        unsorted_blocks, unsorted_head = by_mode[False]
        sorted_blocks, sorted_head = by_mode[True]
        if unsorted_head != sorted_head + 1:
            _fail(f"{fig}: sort-mode shift must be exactly one block "
                  f"(sort=False head {unsorted_head}, sort=True head "
                  f"{sorted_head})")
        # Same block grouping and the same line texts; only the enumeration
        # indices differ (sort=True moves the folio block from document-order
        # index 1 to vertical-order index 15, shifting the caption region
        # down by one).  The numbering difference is the extraction mode,
        # not the indexing origin.
        unsorted_texts = sorted(
            text for lines, _b in unsorted_blocks.values() for text in lines)
        sorted_texts = sorted(
            text for lines, _b in sorted_blocks.values() for text in lines)
        if unsorted_texts != sorted_texts:
            _fail(f"{fig}: sort modes must see the same line texts")
        if ([len(v[0]) for _k, v in sorted(unsorted_blocks.items())
                if v[0][0].startswith(("Supplementary Fig.", "To prepare",
                                       "To synthesize"))]
                != [len(v[0]) for _k, v in sorted(sorted_blocks.items())
                    if v[0][0].startswith(("Supplementary Fig.", "To prepare",
                                           "To synthesize"))]):
            _fail(f"{fig}: sort modes must group the caption region alike")

        # Production (sort=True) numbering: head=3, continuations 4/5,
        # synthesis paragraph from 7 on.
        head = sorted_head
        cont1, cont2 = head + 1, head + 2
        para = head + 4
        for bno in (head, cont1, cont2):
            if len(sorted_blocks[bno][0]) != 1:
                _fail(f"{fig} caption block {bno} must carry one line")
        f = {i: final[(page_no, i)].text for i in range(1, last_final + 1)}
        if f[1] != "Supplementary Fig.":
            _fail(f"{fig} final b1 must be the bold label split")
        if f"{f[1]} {f[2]}" != sorted_blocks[head][0][0]:
            _fail(f"{fig} final b1+b2 must recombine into the head line")
        if f[3] != sorted_blocks[cont1][0][0]:
            _fail(f"{fig} final b3 must come from original block {cont1}")
        if f[4] != sorted_blocks[cont2][0][0]:
            _fail(f"{fig} final b4 must come from original block {cont2}")
        paragraph_blocks = list(range(para, para + (last_final - 4)))
        for offset, bno in enumerate(paragraph_blocks):
            if len(sorted_blocks.get(bno, ([],))[0]) != 1:
                _fail(f"{fig} paragraph block {bno} must carry one line")
            if f[5 + offset] != sorted_blocks[bno][0][0]:
                _fail(f"{fig} final b{5 + offset} must come from block {bno}")

        # Boundary signals between the caption region and the synthesis
        # paragraph (production numbering; bboxes from the sort=True mode).
        cap_gap1 = sorted_blocks[cont1][1][1] - sorted_blocks[head][1][3]
        cap_gap2 = sorted_blocks[cont2][1][1] - sorted_blocks[cont1][1][3]
        boundary_gap = sorted_blocks[para][1][1] - sorted_blocks[cont2][1][3]
        para_gaps = [
            sorted_blocks[b][1][1] - sorted_blocks[b - 1][1][3]
            for b in paragraph_blocks[1:]
        ]
        indent = sorted_blocks[para][1][0] - sorted_blocks[head][1][0]
        if not (3.0 < cap_gap1 < 6.0 and 3.0 < cap_gap2 < 6.0):
            _fail(f"{fig} intra-caption gaps drifted: {cap_gap1}/{cap_gap2}")
        if abs(boundary_gap - 43.37) > 0.05:
            _fail(f"{fig} caption->paragraph gap drifted: {boundary_gap}")
        if any(gap > 7.0 for gap in para_gaps):
            _fail(f"{fig} intra-paragraph gaps drifted: {para_gaps}")
        if abs(indent - 24.02) > 0.05:
            _fail(f"{fig} paragraph first-line indent drifted: {indent}")
        pages[f"page_s{page_no}"] = {
            "sort_false_numbering": {
                "caption_head_original_block": unsorted_head,
                "continuation_original_blocks": [unsorted_head + 1,
                                                 unsorted_head + 2],
                "synthesis_paragraph_original_blocks": [
                    unsorted_head + 4,
                    unsorted_head + 4 + (last_final - 4) - 1,
                ],
                "note": ("the r16 runner's internal probe enumerated this "
                         "mode (get_text('dict') default sort=False)"),
            },
            "sort_true_numbering": {
                "caption_head_original_block": sorted_head,
                "continuation_original_blocks": [cont1, cont2],
                "synthesis_paragraph_original_blocks": [
                    paragraph_blocks[0], paragraph_blocks[-1]],
                "note": ("production parser mode (get_text('dict', "
                         "sort=True)); citations of production locators "
                         "must use this numbering"),
            },
            "final_mapping": {
                "b1_b2_from": head,
                "b3_from": cont1,
                "b4_from": cont2,
                "synthesis_paragraph_from": paragraph_blocks,
            },
            "caption_flag_on_region": False,
            "boundary_signals": {
                "intra_caption_gaps_pt": [round(cap_gap1, 2),
                                          round(cap_gap2, 2)],
                "caption_to_paragraph_gap_pt": round(boundary_gap, 2),
                "intra_paragraph_gaps_pt": [round(g, 2) for g in para_gaps],
                "paragraph_first_line_indent_pt": round(indent, 2),
            },
        }
    document.close()
    return {
        "verified": pages,
        "findings": [
            ("the sort=False/sort=True modes group the caption region into "
             "the same one-line blocks with the same line texts; only the "
             "enumeration indices differ because sort=True repositions the "
             "folio block (page S14: from document-order index 1 to "
             "vertical-order index 15), shifting the caption region down by "
             "one.  The r16 numbering came from the probe's sort=False "
             "mode; production numbering is sort=True -- the difference is "
             "the extraction mode, not the indexing origin"),
            ("the caption region is exactly head + 2 continuation lines "
             "(final b3/b4 in production numbering); from b5 on the "
             "synthesis paragraph is body text that should stay non-caption "
             "anyway"),
            ("the synthesis paragraph is NOT visually indistinguishable: a "
             "43.37 pt vertical gap (vs 3.97-6.63 pt inside both regions) "
             "and a 24.02 pt first-line indent separate it from the caption; "
             "these are recorded as to-be-verified boundary signals"),
        ],
    }


def _design_corrections() -> list:
    return [
        {"item": "original-block numbering basis",
         "r16_wording": ("the acceptance feedback's block numbers ('b1/b2 "
                         "from block 3, b3/b4 from block 4/5') differ by "
                         "indexing convention"),
         "corrected": ("the difference is the extraction mode: the r16 "
                       "runner's internal probe enumerated get_text('dict') "
                       "with the default sort=False while the production "
                       "parser uses sort=True -- same block grouping and "
                       "same lines, but sort=True repositions the folio "
                       "block, so caption-region indices shift by one "
                       "(page S14: caption head 4 vs 3, continuations 5/6 "
                       "vs 4/5, paragraph 8-14 vs 7-13; page S15: 8-15 vs "
                       "7-14).  It is not a different numbering origin; "
                       "production locator citations must use the sort=True "
                       "numbering"),
         "evidence": "caption_boundary_evidence.verified.*.sort_*_numbering"},
        {"item": "missed caption continuation scope",
         "r16_wording": ("the r15 'same-original-block continuations' draft "
                         "would miss b3, b4, and the whole synthesis "
                         "paragraph"),
         "corrected": ("only b3/b4 are missed caption continuation lines; "
                       "from b5 on the synthesis paragraph is body text "
                       "that should stay non-caption anyway, so keeping it "
                       "non-caption is correct behavior, not a miss"),
         "evidence": "caption_boundary_evidence.verified.*.final_mapping"},
        {"item": "visual distinguishability at the caption boundary",
         "r16_wording": ("the synthesis paragraph is visually "
                         "indistinguishable from caption prose"),
         "corrected": ("withdrawn: measured boundary signals exist -- a "
                       "43.37 pt vertical gap (vs 3.97-6.63 pt inside both "
                       "regions) and a 24.02 pt first-line indent.  They "
                       "are recorded as TO-BE-VERIFIED boundary signals, "
                       "not as an implemented rule; caption recognition "
                       "stays unimplemented and the binder's "
                       "at-most-one-skipped-block hop limit is unchanged"),
         "evidence": "caption_boundary_evidence.verified.*.boundary_signals"},
    ]


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

    verdict = ("W1 and W2 are both ineligible under the frozen v1 rule table; "
               "they are retained as ingestion samples and a rule-compatible "
               "source must be found separately (不补造离心/过滤, 不改写源摘录, "
               "不扩规则保正例)")
    return {
        "conventions_sha256": CONVENTIONS_SHA256,
        "rule_count": len(rules),
        "claims_verified": claims,
        "w1": {"source": "Wu-2025 SI page S14, Supplementary Fig. S13 caption "
                         "paragraph",
               "chain": "hydrothermal -> collect -> dry -> reduce (500 °C, "
                        "H2/N2, 6 h)"},
        "w2": {"source": "Wu-2025 SI page S15, Supplementary Fig. S14 caption "
                         "paragraph (powdered MoO2 nanorods)",
               "chain": "precipitate -> heat 50 °C 3 h -> filtration/washing "
                        "collection -> hydrogen reduction 500 °C 2 h"},
        "overall_verdict": verdict,
        "note": ("negative eligibility pre-screen carried unchanged from "
                 "r15/r16; the conventions resource is byte-identical, so "
                 "the outcome is unchanged and remains a pre-screen, not a "
                 "pass; the full step-level table is archived in the r16 "
                 "replay (rule_eligibility.w1/w2.steps)"),
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

    # (c) Folio test module (now 27 tests) and the 346 target set.
    folio_proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         "-m", "unittest", "reaserch_agent.test_route_pdf_folio"],
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env,
    )
    folio_out = (folio_proc.stderr or "") + (folio_proc.stdout or "")
    if f"Ran {FOLIO_TEST_COUNT} tests" not in folio_out or "OK" not in folio_out:
        _fail(f"folio tests failed: {folio_out[-400:]}")
    target_proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8",
         "-m", "unittest"] + TARGET_SET_MODULES,
        cwd=ROOT, capture_output=True, text=True, timeout=600, env=env,
    )
    target_out = (target_proc.stderr or "") + (target_proc.stdout or "")
    if f"Ran {TARGET_SET_SIZE} tests" not in target_out or "OK" not in target_out:
        _fail(f"346 target set failed: {target_out[-400:]}")

    return {
        "a01_regression": {
            "mechanism": "r13b runner re-executed as a subprocess",
            "double_run_byte_identical": True,
            "replay_matches_r13_archive": True,
            "ms7a_out": "BLOCKED (retained_object_mention_precedes_operation)",
        },
        "diagnostics_module_zero_proof_dependency": True,
        "folio_test_module_27_ok": True,
        "target_set_346_ok": True,
    }


def _build_replay() -> dict:
    baseline = _baseline()
    fix = _neighborhood_fix_verification()
    gaps = _si_gap_distribution()
    version = _version_determination()
    documents = _document_anchors()
    census = _pool_census()
    boundary = _caption_boundary_evidence()
    corrections = _design_corrections()
    eligibility = _rule_eligibility()
    attested = _wu_attested_layer()
    anchors = _anchors()
    return {
        "schema_version": "g2a_ingestion_folio/v17",
        "round": ("r17 (G2a follow-up-2): folio neighborhood body-text check "
                  "(sixth factor) + parser version bump (route_pdf_groups/v3) "
                  "+ SI caption-boundary design-note corrections"),
        "model_generated": False,
        "model_called": False,
        "baseline": baseline,
        "code_changes": {
            "files": CHANGED_SOURCE_FILES,
            "changed_test_file": {FOLIO_TEST_FILE: FOLIO_TEST_SHA256},
            "semantics_note": (
                "conventions, protocol schemas, and every rule resource are "
                "byte-identical to the frozen baseline; ingestion block "
                "semantics changed for previously parseable inputs whose "
                "bottom band carries label-adjacent values (they were "
                "stripped under v2 and are kept under v3 -- empirically "
                "pinned in version_determination), so "
                "PDF_GROUP_PARSER_VERSION is bumped to route_pdf_groups/v3; "
                "the A01 and Wu SI anchors are asserted byte-identical "
                "under both the pre-r17 and the current parser"),
        },
        "neighborhood_fix": {
            "root_cause": ("the r16 five-factor rule validated a bare "
                           "incrementing bottom-band value as a folio even "
                           "when its label sat in the ADJACENT original "
                           "block a few points away; body isolation checked "
                           "only same-block line counts, and sole occupancy "
                           "of a block does not prove detachment from body "
                           "text"),
            "six_factors": [
                "standalone folio form: fullmatch [A-Za-z]{0,2}\\s*[0-9]{1,4} "
                "in the bottom band (y0 > 0.90h)",
                "at most one candidate per page; quorum of three pages",
                "uniform alphabetic prefix and page-order lockstep increment",
                "vertical anchor: y0 within 4 pt of the median",
                "horizontal anchor: center x within 4 pt of the median",
                "body isolation inside the block: the candidate is the only "
                "non-empty line of its original PDF block",
                "body isolation across the neighborhood (new): no other "
                "text line on the page overlaps the candidate horizontally "
                "while standing within one standard line height (1.2 x the "
                "candidate's font size) of it, above or below",
            ],
            "doubt_keeps_content": ("when context is doubtful the line is "
                                    "kept: keeping content is always the "
                                    "safe failure; no page is removed, no "
                                    "PDF byte is modified"),
            "verification": fix,
        },
        "si_gap_distribution": gaps,
        "version_determination": version,
        "document_anchors": documents,
        "pool_census": census,
        "caption_boundary_evidence": boundary,
        "design_corrections": corrections,
        "rule_eligibility": eligibility,
        "layer_results": {
            "attestation": {"status": "verified", "documents": 2},
            "group_enumeration": {
                "status": "reached_section_gate",
                "groups": 0,
                "diagnostic_reasons": attested["diagnostic_reasons"],
                "meaning": ("unchanged from r15/r16: the SI parses and stops "
                            "at experimental_section_missing; the main paper "
                            "still abstains at ingestion"),
            },
            "proposal_extraction": {"status": "not_reached"},
            "local_revision": {"status": "not_reached"},
            "association": {"status": "not_reached"},
            "receipt": {"status": "not_reached"},
            "pool_census": census,
        },
        "dag_consumption": {
            "observed": False,
            "note": ("still unobservable (0 groups); W1/W2 stay ineligible "
                     "under the frozen v1 rules; nothing here is claimed as "
                     "a pass"),
        },
        "stays_closed_this_round": [
            "no caption-recognition change is implemented",
            "caption quotation is NOT opened",
            "no SI grouping implementation",
            "the workflow interception of incomplete sources is unchanged",
            "3E stays diagnostics_only with zero tokens",
            "protocol-definition/v1 not expanded",
            "A01 ms7a.out and its cascade stay BLOCKED",
        ],
        "anchors": anchors,
        "fixed_constraints": {
            "r7_to_r16_archives_untouched": True,
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
        "schema_version": "g2a_ingestion_folio/v17",
        "model_generated": False,
        "scope": ("r17 (G2a follow-up-2): folio neighborhood body-text check "
                  "in the PDF ingestion layer (five factors -> six), the "
                  "parser-version bump to route_pdf_groups/v3 with empirical "
                  "old/new evidence, and SI caption-boundary design-note "
                  "corrections; no model call, no conventions/protocol "
                  "semantic change, no caption release, no SI grouping"),
        "audit": [
            {"kind": "changed_files",
             "items": [
                 "reaserch_agent/route_pdf_source.py (six-factor validated "
                 "folio recognition: + neighborhood body-text check, scale "
                 "derived from the candidate's font metrics; _PdfLine keeps "
                 "the full glyph bbox)",
                 "reaserch_agent/route_pdf_groups.py (PDF_GROUP_PARSER_VERSION "
                 "bumped to route_pdf_groups/v3 with the rationale comment)",
                 "reaserch_agent/test_route_pdf_folio.py (+ "
                 "NeighborhoodBodyRegressionTest: 7 tests; pre-r15 "
                 "comparison gains the probe-C case; version assertion at "
                 "v3; module now 27 tests)",
                 "result/operation-structure-20260928/local-revision-r17.py",
                 "result/operation-structure-20260928/local-revision-r17-*.json",
                 "docs/field_semantic_gate_r17_checkpoint_20261005.md",
             ]},
            {"kind": "capability_change",
             "detail": ("previously parseable inputs whose bottom band "
                        "carries label-adjacent incrementing values (probe C "
                        "shape: label and value in adjacent original blocks "
                        "at the same x, glyph-box gap ~4.26 pt) now keep "
                        "those values instead of deleting them; genuine "
                        "folio patterns (consistent anchor, isolated line, "
                        "no horizontally-overlapping neighbor within one "
                        "line height) are still stripped -- Wu SI keeps its "
                        "1041 blocks / 79 folios and A01 is byte-identical "
                        "under both parsers")},
            {"kind": "version_handling",
             "detail": ("block semantics changed for previously parseable "
                        "inputs (three synthetic shapes change 6 -> 9 "
                        "blocks; both real anchor documents unchanged), so "
                        "PDF_GROUP_PARSER_VERSION moved v2 -> v3 per the "
                        "route_pdf_groups.py line-50 convention; the "
                        "r13/r13b archives pin no version string "
                        "(re-asserted in the runner), so no pin was "
                        "rewritten and historical archives keep their "
                        "original bytes")},
            {"kind": "honest_endpoints",
             "detail": ("W1/W2 ineligible under the frozen v1 rules; the "
                        "caption-boundary corrections are a recorded design "
                        "with measured geometry (43.37 pt gap, 24.02 pt "
                        "indent), not an implementation; dag_consumption "
                        "remains unobserved and unclaimed")},
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

    (OUT_DIR / "local-revision-r17-replay.json").write_text(
        replay_text, encoding="utf-8", newline="\n")
    (OUT_DIR / "local-revision-r17-audit.json").write_text(
        audit_text, encoding="utf-8", newline="\n")
    summary = {
        "probe_c_pre_round_stripped": (
            replay["neighborhood_fix"]["verification"]
            ["before_pre_round_code"]["probe_c"]["stripped"]),
        "probe_c_values_kept": (
            replay["neighborhood_fix"]["verification"]
            ["after_current_code"]["probe_c"]["values_kept"]),
        "label_below_values_kept": (
            replay["neighborhood_fix"]["verification"]
            ["after_current_code"]["label_below_value"]["values_kept"]),
        "gap_boundary_both_sides": [
            replay["neighborhood_fix"]["verification"]
            ["after_current_code"]["gap_just_below_scale"]["values_kept"],
            replay["neighborhood_fix"]["verification"]
            ["after_current_code"]["gap_just_above_scale"]["stripped"],
        ],
        "si_shaped_still_stripped": (
            replay["neighborhood_fix"]["verification"]
            ["after_current_code"]["si_shaped_spacing"]["stripped"]),
        "control_still_stripped": (
            replay["neighborhood_fix"]["verification"]
            ["after_current_code"]["control_isolated"]["stripped"]),
        "si_gap_min": replay["si_gap_distribution"]["gap_min"],
        "si_gap_median": replay["si_gap_distribution"]["gap_median"],
        "parser_version": "route_pdf_groups/v3",
        "semantic_change_cases": [
            row["case"] for row in
            replay["version_determination"]["samples"]
            if not row["block_sequence_identical"]],
        "real_documents_identical_under_both_parsers": {
            tag: row["block_sequence_identical"]
            for tag, row in replay["version_determination"]["real_documents"]
            .items()},
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
        "pool_census_identical_to_r15_r16": (
            replay["pool_census"]["identical_to_r15_post_census"]
            and replay["pool_census"]["identical_to_r16_census"]),
        "caption_corrections": [c["item"] for c in
                                replay["design_corrections"]],
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
