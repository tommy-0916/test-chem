"""Round-18: acceptance-script hardening + two root-cause diagnoses.
No model is called; no chemistry rule or PDF-parsing production logic moves.

Scope of the code change (three files, all test infrastructure):

- ``reaserch_agent/route_test_harness.py`` (NEW): strict, testable judgement
  for unittest subprocess runs.  A run is accepted only when ALL criteria
  hold: returncode == 0; exactly one "Ran N tests" summary line with N equal
  to the expected count; the verdict line is exactly "OK" (any FAILED form,
  or failures=/errors= nonzero anywhere, rejects); for must-run suites any
  skipped= nonzero rejects.  Every rejection carries a structured reason,
  and audit_record keeps the FULL stdout/stderr (timing-normalized) instead
  of searched substrings.  The r17 substring rule ("Ran 346 tests" + "OK"
  in output) accepted (a) nonzero-returncode runs whose tail carried the
  substrings and (b) "OK (skipped=2)"; only a dots-only abort was rejected.
  All three negative shapes are pinned by unit tests in
  ``reaserch_agent/test_route_test_harness.py`` (NEW, 41 tests), which joins
  the target set (346 -> 387).
- ``reaserch_agent/test_route_pdf_folio.py`` (MODIFIED, still 27 tests):
  ``_load_historical_source_module`` now raises HistoricalSourceUnavailable
  (the skip channel) ONLY for the two genuine causes -- the git executable
  cannot be run, or ``git show`` returns non-zero -- with distinct messages;
  import/execution errors of a fetched historical module raise RuntimeError
  chaining the original exception, so they FAIL the test instead of being
  disguised as a git problem.  The git subprocess also receives an explicit
  ``env=dict(os.environ)`` copy (diagnosis (a) fix, below).

Diagnosis (a) -- full-slice discover skip=2 (root-caused, fixed):
  the two folio historical-comparison tests skipped with "git or the commit
  unavailable" only inside full-slice discover.  Measured chain:
  the agent shell exports GIT_CONFIG_COUNT=2 / GIT_CONFIG_KEY_0=
  credential.helper / GIT_CONFIG_VALUE_0= (EMPTY); the first
  ``mock.patch.dict(os.environ)`` round-trip in the suite (alphabetically
  test_action_evidence_selection) restores os.environ via clear()+update(),
  and re-setting an empty-valued variable DELETES it from the Win32 kernel
  environment block while os.environ keeps showing '' (micro-reproduced);
  children spawned with env=None then inherit a block where
  GIT_CONFIG_VALUE_0 is missing while COUNT=2/KEY_0 remain, so git exits
  rc=128 "missing config value GIT_CONFIG_VALUE_0" and the old loader mapped
  that to skipTest.  Confirmed end-to-end: kernel-level env sniffing shows
  exactly one flip (present:'' -> MISSING) at that first test and never
  recovers; re-probing git with env=dict(os.environ) inside the polluted
  state succeeds (rc=0); full-slice discover with the GIT_CONFIG_* variables
  stripped runs 1229 tests with zero skips; with the loader fix, full-slice
  discover in the polluted shell runs 1252 tests with zero skips.

Diagnosis (b) -- intermittent runner abort (NOT reproduced; forensics built
  in): the r17 runner once failed because the target-set subprocess output
  stopped at progress dots with no summary.  This round ran the identical
  invocation 78 times clean, 18 times three-way parallel, and 6 times under
  ~8 GB memory pressure: 102 invocations, zero anomalies, every rc=0 with a
  complete summary.  Differential and next-step forensics are recorded in
  the r18 checkpoint; every subprocess this runner spawns now carries
  ``-X faulthandler`` and its returncode + full (timing-normalized)
  stdout/stderr are audit-recorded, and on any judged rejection the runner
  writes local-revision-r18-failure-forensics.json BEFORE failing.

What this runner establishes, deterministically and token-free:

1. Baseline freeze: pre-round HEAD 7b6a1b4, conventions.json sha256 (frozen,
   unchanged), prompt-builder sha256 (unchanged), the two parser files pinned
   at their r17 hashes (forbidden zone, unchanged), the r18 hashes of the
   folio test file and the two harness files, the r17 checkpoint sha256
   (untouched archive), A01 source digest and Wu-2025 PDF digests.
2. All r17 anchors re-verified unchanged: neighborhood fix before/after
   against the 491ec37 parser, SI gap distribution (79 folios, min 14.16 pt),
   v3 version determination evidence, A01 1289 blocks pinned sha256, Wu SI
   1041 blocks / 79 folios with the W1 sentence binding, Wu main abstaining
   on the p10 chart tick, pool census identical to r15/r16/r17, caption
   boundary evidence, rule eligibility, attested layer, version string
   absent from r13/r13b archives.
3. Judged subprocesses (strict harness): r13b archive-pinned runner
   (replay matches the r13 archive, double-run byte-identical, ms7a.out
   BLOCKED), folio module (27 tests, zero skips required), target set
   (13 r17 modules + the harness module = 387 tests, zero skips required).
4. Full-slice discover recorded honestly: reaserch_agent 1270 tests and
   chem_agent_contracts 116 tests; the normalized failure-header sets are
   asserted byte-equal to the embedded baselines (29 research entries = the
   30-entry baseline minus test_b1_bootstrap_generates_initial_outputs,
   which passes in this workspace; 1 contracts entry); the skip list is
   recorded as data (never asserted zero).

Clean-rerun fix (acceptance feedback on the first r18 commit): the tracked
discover logs embed random tempfile names from baseline-failure tracebacks
(plus the jieba load-timing line), so every execution rewrote them with
different bytes and the cleanliness gate in _baseline rejected the second
build once the logs were tracked.  The logs are now written through
normalize_transient_paths (stable placeholders) AND named in the gate's
allowed set (DISCOVER_LOG_FILES).  Invariant: a fresh double run from a
clean tracked tree passes and leaves `git status` clean.

The runner is deterministic: two in-memory builds are byte-identical; all
recorded subprocess output is timing-normalized.  Zero tokens are consumed.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from statistics import median

sys.path.insert(0, ".")

from reaserch_agent.route_test_harness import (  # noqa: E402
    RunExpectation,
    audit_record,
    collect_unittest_skips,
    discover_returncode_reason,
    judge_unittest_run,
    normalize_transient_paths,
    normalize_unittest_timing,
    parse_unittest_summary,
    timeout_forensics_record,
    write_forensics_file,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "result" / "operation-structure-20260928"
WU = ROOT / "result" / "wu2025-real-input-20261004"
R15_REPLAY = OUT_DIR / "local-revision-r15-replay.json"
R16_REPLAY = OUT_DIR / "local-revision-r16-replay.json"
R17_REPLAY = OUT_DIR / "local-revision-r17-replay.json"
FORENSICS = OUT_DIR / "local-revision-r18-failure-forensics.json"

PRE_ROUND_HEAD = "7b6a1b4fcdcd69e465d353f501109e70502e18e8"
PRER17_HEAD = "491ec375dd4cd3b5d6b4160580ebc6a8516338e5"
CONVENTIONS_SHA256 = (
    "39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b"
)
PROMPT_BUILDER_FILE = "reaserch_agent/route_pdf_group_extraction.py"
PROMPT_BUILDER_SHA256 = (
    "24864d9af522666b15a1690b6c9ba829bebfa3047545d82adc3e3dff331486f7"
)
PARSER_FILES = {
    "reaserch_agent/route_pdf_source.py": (
        "51b7be8b1e99847ea26d08568aef83b65b9168945082050e1b72eee3d0f51d7a"
    ),
    "reaserch_agent/route_pdf_groups.py": (
        "d8eecc83b1dc71fadc5541668f4ad3c3ea210a9a8c97ca31066a548795766094"
    ),
}
FOLIO_TEST_FILE = "reaserch_agent/test_route_pdf_folio.py"
FOLIO_TEST_SHA256 = (
    "bbf2aa16354a9d476f97a867a1c08bad8fd668a6c49734a3c28fbb78ed978ff7"
)
FOLIO_TEST_COUNT = 27
HARNESS_FILE = "reaserch_agent/route_test_harness.py"
HARNESS_SHA256 = (
    "a65f3c28473a55679311db178a206211f54281f6754e851374d27d99fd85ddf1"
)
HARNESS_TEST_FILE = "reaserch_agent/test_route_test_harness.py"
HARNESS_TEST_SHA256 = (
    "f4312d48a836c1ca9883ec4574cd182ff91956ecbf9966eea379901b22576e1c"
)
HARNESS_TEST_COUNT = 41
R17_CHECKPOINT_FILE = "docs/field_semantic_gate_r17_checkpoint_20261005.md"
R17_CHECKPOINT_SHA256 = (
    "efb133c860e6a369fbb8f227132e3b71573364edc0fd185b5bf8f8a620ade50b"
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
    # r18 addition: the strict-judgement harness contract tests.
    "reaserch_agent.test_route_test_harness",
]
TARGET_SET_SIZE = 387

DISCOVER_RESEARCH_COUNT = 1270
DISCOVER_CONTRACTS_COUNT = 116

# Round-18 tracked artifacts the runner itself rewrites on every execution;
# they are legitimate products of this round, and their recorded content is
# byte-stable (timing + transient-path normalized), so a re-run from a clean
# tree leaves `git status` clean.  (r18 clean-rerun fix: the discover logs
# were missing from this set and carried random tempfile names.  The run
# logs are included because capturing stdout onto the tracked artifact name
# truncates the file for the whole run; replay/audit are included because
# the runner rewrites them at the end of every execution.)
DISCOVER_LOG_FILES = {
    "reaserch_agent": (
        "result/operation-structure-20260928/"
        "local-revision-r18-discover-reaserch_agent.log"),
    "chem_agent_contracts": (
        "result/operation-structure-20260928/"
        "local-revision-r18-discover-chem_agent_contracts.log"),
}
ROUND_ARTIFACT_FILES = set(DISCOVER_LOG_FILES.values()) | {
    "result/operation-structure-20260928/local-revision-r18-replay.json",
    "result/operation-structure-20260928/local-revision-r18-audit.json",
    "result/operation-structure-20260928/local-revision-r18-run1.log",
    "result/operation-structure-20260928/local-revision-r18-run2.log",
}

# Full-slice baselines, normalized with
# sed -E 's/\((reaserch_agent|chem_agent_contracts)\./(/' : the research set
# is the 30-entry baseline minus test_b1_bootstrap_generates_initial_outputs
# (which passes in this workspace); the contracts set is the single baseline
# entry.  Byte-equal sets are asserted; zero new failures is a hard gate.
EXPECTED_RESEARCH_FAILURES = sorted([
    "ERROR: test_bootstrap_accepts_first_valid_candidate_without_retry (test_logical_container_contract.LogicalContainerContractTests.test_bootstrap_accepts_first_valid_candidate_without_retry)",
    "ERROR: test_bootstrap_repairs_invalid_lid_once (test_logical_container_contract.LogicalContainerContractTests.test_bootstrap_repairs_invalid_lid_once)",
    "ERROR: test_compact_context_contains_evidence_packet (test_evidence_provenance.EvidencePacketAndRefsTest.test_compact_context_contains_evidence_packet)",
    "ERROR: test_compact_state_context_includes_campaign_memory (test_campaign_memory.RecallContextTest.test_compact_state_context_includes_campaign_memory)",
    "ERROR: test_cross_campaign_layer_only_on_abnormal_signal (test_campaign_memory.RecallContextTest.test_cross_campaign_layer_only_on_abnormal_signal)",
    "ERROR: test_device_adaptation_accepts_first_valid_candidate_without_retry (test_logical_container_contract.LogicalContainerContractTests.test_device_adaptation_accepts_first_valid_candidate_without_retry)",
    "ERROR: test_device_adaptation_exhaustion_does_not_publish (test_logical_container_contract.LogicalContainerContractTests.test_device_adaptation_exhaustion_does_not_publish)",
    "ERROR: test_device_adaptation_repairs_invalid_lid_once (test_logical_container_contract.LogicalContainerContractTests.test_device_adaptation_repairs_invalid_lid_once)",
    "ERROR: test_every_macro_step_has_source_annotation (test_evidence_provenance.MacroPlanSourceTest.test_every_macro_step_has_source_annotation)",
    "ERROR: test_evidence_refs_flow_into_ledger_and_memory (test_evidence_provenance.EvidencePacketAndRefsTest.test_evidence_refs_flow_into_ledger_and_memory)",
    "ERROR: test_layered_memory_add_search_update_history (test_memory_layers.ChemMemoryLayerTests.test_layered_memory_add_search_update_history)",
    "ERROR: test_nodes_written_per_turn (test_campaign_memory.TrajectoryNodeTest.test_nodes_written_per_turn)",
    "ERROR: test_post_observation_accepts_first_valid_candidate_without_retry (test_logical_container_contract.LogicalContainerContractTests.test_post_observation_accepts_first_valid_candidate_without_retry)",
    "ERROR: test_post_observation_repairs_invalid_lid_once (test_logical_container_contract.LogicalContainerContractTests.test_post_observation_repairs_invalid_lid_once)",
    "ERROR: test_protocols_carry_registry_identity (test_evidence_provenance.ProtocolProvenanceTest.test_protocols_carry_registry_identity)",
    "ERROR: test_query_tools_use_separate_experiment_and_literature_layers (test_memory_layers.ChemMemoryLayerTests.test_query_tools_use_separate_experiment_and_literature_layers)",
    "ERROR: test_recall_layers_and_budgets (test_campaign_memory.RecallBuilderTest.test_recall_layers_and_budgets)",
    "ERROR: test_research_agent_memory_uses_experiment_layer (test_memory_layers.ChemMemoryLayerTests.test_research_agent_memory_uses_experiment_layer)",
    "ERROR: test_stage_rollup_written_on_closure_or_stage_change (test_campaign_memory.TrajectoryNodeTest.test_stage_rollup_written_on_closure_or_stage_change)",
    "ERROR: test_valid_none_carrier_can_be_published_without_changing_bottle_lids (test_logical_container_contract.LogicalContainerContractTests.test_valid_none_carrier_can_be_published_without_changing_bottle_lids)",
    "ERROR: test_web_and_parse_failed_sources_are_explicit_known_gaps (test_evidence_provenance.EvidencePacketAndRefsTest.test_web_and_parse_failed_sources_are_explicit_known_gaps)",
    "FAIL: test_bootstrap_exhaustion_does_not_publish (test_logical_container_contract.LogicalContainerContractTests.test_bootstrap_exhaustion_does_not_publish)",
    "FAIL: test_concurrent_same_identity_records_do_not_overwrite (test_ingestion.KnowledgeIngestionTests.test_concurrent_same_identity_records_do_not_overwrite)",
    "FAIL: test_default_ledger_path_layout (test_plan_ledger.PlanLedgerFileTest.test_default_ledger_path_layout)",
    "FAIL: test_final_publication_rejects_invalid_lid_with_original_context (test_logical_container_contract.LogicalContainerContractTests.test_final_publication_rejects_invalid_lid_with_original_context)",
    "FAIL: test_macro_step_prompt_projection_keeps_contract_without_audit_noise (test_v2_contract.ResearchV2ContractTest.test_macro_step_prompt_projection_keeps_contract_without_audit_noise)",
    "FAIL: test_missing_container_requirements_remains_a_supported_empty_default (test_logical_container_contract.LogicalContainerContractTests.test_missing_container_requirements_remains_a_supported_empty_default)",
    "FAIL: test_post_observation_exhaustion_does_not_publish (test_logical_container_contract.LogicalContainerContractTests.test_post_observation_exhaustion_does_not_publish)",
    "FAIL: test_quality_gate_locates_original_step_and_container_without_mutation (test_logical_container_contract.LogicalContainerContractTests.test_quality_gate_locates_original_step_and_container_without_mutation)",
])
EXPECTED_CONTRACTS_FAILURES = [
    "FAIL: test_legacy_bound_package_hash_is_unchanged (test_route_binding_v2.RouteBindingV2Test.test_legacy_bound_package_hash_is_unchanged)",
]

_FAILURE_HEADER_RE = re.compile(r"^(?:FAIL|ERROR): .*$", re.MULTILINE)
_NORMALIZE_RE = re.compile(r"\((?:reaserch_agent|chem_agent_contracts)\.")


def _fail(message: str) -> None:
    raise SystemExit(f"r18 acceptance failed: {message}")


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _baseline() -> dict:
    conventions = ROOT / "chem_resources" / "chemistry_conventions" / "conventions.json"
    if _sha256_file(conventions) != CONVENTIONS_SHA256:
        _fail("conventions.json drifted from the frozen baseline")
    if _sha256_file(ROOT / PROMPT_BUILDER_FILE) != PROMPT_BUILDER_SHA256:
        _fail("prompt-builder module drifted from the frozen baseline")
    for rel, pinned in PARSER_FILES.items():
        if _sha256_file(ROOT / rel) != pinned:
            _fail(f"{rel} drifted from the r17-pinned state (forbidden zone)")
    if _sha256_file(ROOT / FOLIO_TEST_FILE) != FOLIO_TEST_SHA256:
        _fail("folio test file drifted from the round-18 recorded state")
    if _sha256_file(ROOT / HARNESS_FILE) != HARNESS_SHA256:
        _fail("harness module drifted from the round-18 recorded state")
    if _sha256_file(ROOT / HARNESS_TEST_FILE) != HARNESS_TEST_SHA256:
        _fail("harness test module drifted from the round-18 recorded state")
    if _sha256_file(ROOT / R17_CHECKPOINT_FILE) != R17_CHECKPOINT_SHA256:
        _fail("r17 checkpoint drifted: prior checkpoints are append-only")
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT,
        capture_output=True, text=True, timeout=120, env=dict(os.environ),
    )
    if proc.returncode != 0:
        _fail(f"git status failed: {proc.stderr[-200:]}")
    tracked_modified = sorted(
        line[3:] for line in proc.stdout.splitlines()
        if line[:2] in {" M", "M ", "MM"}
    )
    allowed = ({FOLIO_TEST_FILE, HARNESS_FILE, HARNESS_TEST_FILE}
               | ROUND_ARTIFACT_FILES)
    if not set(tracked_modified) <= allowed:
        _fail(
            "tracked modifications outside the round-18 files: "
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
        "parser_files_pinned_at_r17": PARSER_FILES,
        "changed_test_file": {FOLIO_TEST_FILE: FOLIO_TEST_SHA256},
        "new_harness_files": {HARNESS_FILE: HARNESS_SHA256,
                              HARNESS_TEST_FILE: HARNESS_TEST_SHA256},
        "r17_checkpoint_sha256_untouched": R17_CHECKPOINT_SHA256,
        "tracked_modifications_subset_of_round18_files": True,
    }


def _blocks_pin(blocks) -> str:
    canon = json.dumps(
        [(b.page, b.number, b.text, b.font_size, b.bold, b.caption) for b in blocks],
        ensure_ascii=False, sort_keys=True,
    )
    return sha256(canon.encode()).hexdigest()


def _load_historical_source_module(commit: str, name: str):
    """Load route_pdf_source.py as of a commit (for before/after evidence).

    The git subprocess gets an explicit env copy (never env=None): the Win32
    kernel environment block can silently lose empty-valued variables after
    any mock.patch.dict(os.environ) round-trip in the caller's process
    history (r18 diagnosis a).
    """
    proc = subprocess.run(
        ["git", "show", f"{commit}:reaserch_agent/route_pdf_source.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env=dict(os.environ),
    )
    if proc.returncode != 0 or not proc.stdout:
        _fail(f"git show {commit}:route_pdf_source.py failed: "
              f"{(proc.stderr or '')[-200:]}")
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
    """Probe C family: label and value in two adjacent original blocks."""
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
    """r17 anchor re-verified: the six-factor rule still behaves as recorded."""
    from reaserch_agent import route_pdf_source as current

    pre_r17 = _load_historical_source_module(
        PRER17_HEAD, "reaserch_agent._route_pdf_source_r16")

    before = {
        "probe_c": _probe_outcome(pre_r17, _label_value_bytes()),
    }
    outcome = before["probe_c"]
    if outcome["issue"] is not None or outcome["blocks"] != 6:
        _fail(f"pre-r17 probe C drifted: {outcome}")
    if outcome["stripped"] != ["11", "12", "13"] or outcome["values_kept"]:
        _fail("pre-r17 probe C must strip the values (the r17-recorded gap)")

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
            _fail(f"current {name} drifted: {outcome}")
        if outcome["stripped"] or outcome["values_kept"] != ["11", "12", "13"]:
            _fail(f"current {name} must keep every value")
    for name, stripped in (("gap_just_above_scale", ["11", "12", "13"]),
                           ("non_overlapping_label", ["11", "12", "13"]),
                           ("control_isolated", ["11", "12", "13"])):
        outcome = after[name]
        if outcome["stripped"] != stripped:
            _fail(f"current {name} must still strip: {outcome}")
    for name, count in (("gap_just_above_scale", 6),
                        ("non_overlapping_label", 6),
                        ("control_isolated", 3),
                        ("si_shaped_spacing", 6)):
        if after[name]["blocks"] != count:
            _fail(f"current {name} block count drifted: {after[name]}")
    si_shaped = after["si_shaped_spacing"]
    if si_shaped["stripped"] != ["S 11", "S 12", "S 13"]:
        _fail(f"SI-shaped spacing must still strip: {si_shaped}")

    return {
        "carried_from": ("r17 replay neighborhood_fix.verification; the r18 "
                         "round changes no parser code, so this section is a "
                         "pure regression re-verification"),
        "before_prer17_code": before,
        "after_current_code": after,
    }


def _si_gap_distribution() -> dict:
    """r17 anchor: every validated SI folio clears the neighborhood scale."""
    import fitz

    from reaserch_agent.route_pdf_source import (
        _FOLIO_BODY_GAP_FACTOR,
        _line_parts,
        _PdfLine,
        _validated_folio_lines,
    )

    raw = (ROOT / WU_SI_PDF).read_bytes()
    pages = []
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
                continue
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
            _fail(f"SI folio {row['folio']} would fail the check: {row}")
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
    """r17 anchor: v3 semantics unchanged; r13/r13b pin no version string."""
    from reaserch_agent.route_pdf_source import _read_pdf_blocks

    pre_r17 = _load_historical_source_module(
        PRER17_HEAD, "reaserch_agent._route_pdf_source_v2")

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
        old_blocks, old_issue = pre_r17._read_pdf_blocks(raw)
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

    documents = {}
    for tag, rel, count in (
            ("wu2025_si", WU_SI_PDF, WU_SI_BLOCK_COUNT),
            ("a01", A01_PDF, A01_BLOCK_COUNT)):
        raw = (ROOT / rel).read_bytes()
        old_blocks, old_issue = pre_r17._read_pdf_blocks(raw)
        new_blocks, new_issue = _read_pdf_blocks(raw)
        if old_issue is not None or new_issue is not None:
            _fail(f"{tag} must parse under both parsers")
        if len(old_blocks) != count or len(new_blocks) != count:
            _fail(f"{tag} block count drifted under dual parse")
        old_pin, new_pin = _blocks_pin(old_blocks), _blocks_pin(new_blocks)
        if old_pin != new_pin:
            _fail(f"{tag} block sequence changed under the current parser")
        documents[tag] = {
            "blocks": count,
            "blocks_sha256_both_parsers": new_pin,
            "block_sequence_identical": True,
        }

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
        _fail("PDF_GROUP_PARSER_VERSION must stay at route_pdf_groups/v3")

    return {
        "carried_from": ("r17 replay version_determination; re-verified "
                         "unchanged against the same pre-r17 parser"),
        "samples": rows,
        "real_documents": documents,
        "parser_version": PDF_GROUP_PARSER_VERSION,
        "r13_r13b_impact": {
            "version_string_occurrences_in_r13_archives": 0,
            "r13_r13b_runners_reference_parser_version": False,
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
            _fail(f"r18 census drifted for {row['candidate']}: {row['reasons']}")
    identical_to = {}
    for tag, replay_path in (("r15", R15_REPLAY), ("r16", R16_REPLAY),
                             ("r17", R17_REPLAY)):
        identical = None
        if replay_path.is_file():
            archived = json.loads(replay_path.read_text(encoding="utf-8"))
            # r15 stores the census under layer_results; r16/r17 at the top
            # level AND under layer_results.  Accept the recorded locations.
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
                _fail(f"r18 pool census differs from the {tag} census")
        identical_to[tag] = identical
    return {
        "rows": rows,
        "identical_to_r15_post_census": identical_to["r15"],
        "identical_to_r16_census": identical_to["r16"],
        "identical_to_r17_census": identical_to["r17"],
        "note": ("the r18 round changes no parser code; every candidate "
                 "keeps its r15/r16/r17 reason set"),
    }


def _caption_boundary_evidence() -> dict:
    """Re-derive the S14/S15 caption geometry under BOTH extraction modes and
    measure the whitespace/indent boundary signals (r17 design corrections,
    re-verified unchanged)."""
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
            },
            "sort_true_numbering": {
                "caption_head_original_block": sorted_head,
                "continuation_original_blocks": [cont1, cont2],
                "synthesis_paragraph_original_blocks": [
                    paragraph_blocks[0], paragraph_blocks[-1]],
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
        "carried_from": ("r17 replay caption_boundary_evidence; re-verified "
                         "unchanged (design record, NOT implemented)"),
        "verified": pages,
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

    verdict = ("W1 and W2 are both ineligible under the frozen v1 rule table; "
               "they are retained as ingestion samples and a rule-compatible "
               "source must be found separately (不补造离心/过滤, 不改写源摘录, "
               "不扩规则保正例)")
    return {
        "conventions_sha256": CONVENTIONS_SHA256,
        "rule_count": len(rules),
        "claims_verified": claims,
        "overall_verdict": verdict,
        "note": ("negative eligibility pre-screen carried unchanged from "
                 "r15/r16/r17; the conventions resource is byte-identical, "
                 "so the outcome is unchanged and remains a pre-screen, not "
                 "a pass"),
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
    }


def _design_corrections() -> list:
    """Carried verbatim from the r17 replay (design record, NOT implemented)."""
    return [
        {"item": "original-block numbering basis",
         "corrected": ("the difference is the extraction mode: the r16 "
                       "runner's internal probe enumerated get_text('dict') "
                       "with the default sort=False while the production "
                       "parser uses sort=True -- same block grouping and "
                       "same lines, but sort=True repositions the folio "
                       "block, so caption-region indices shift by one; "
                       "production locator citations must use the sort=True "
                       "numbering"),
         "evidence": "caption_boundary_evidence.verified.*.sort_*_numbering"},
        {"item": "missed caption continuation scope",
         "corrected": ("only b3/b4 are missed caption continuation lines; "
                       "from b5 on the synthesis paragraph is body text "
                       "that should stay non-caption anyway, so keeping it "
                       "non-caption is correct behavior, not a miss"),
         "evidence": "caption_boundary_evidence.verified.*.final_mapping"},
        {"item": "visual distinguishability at the caption boundary",
         "corrected": ("withdrawn: measured boundary signals exist -- a "
                       "43.37 pt vertical gap (vs 3.97-6.63 pt inside both "
                       "regions) and a 24.02 pt first-line indent.  They "
                       "are recorded as TO-BE-VERIFIED boundary signals, "
                       "not as an implemented rule; caption recognition "
                       "stays unimplemented and the binder's "
                       "at-most-one-skipped-block hop limit is unchanged"),
         "evidence": "caption_boundary_evidence.verified.*.boundary_signals"},
    ]


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    return env


def _python_cmd() -> list:
    # -X faulthandler: a native crash in the child dumps the Python stack to
    # stderr (r18 diagnosis-b forensics, zero cost when nothing crashes).
    # pythonw.exe (GUI subsystem, no console): on this host, detached
    # console-subsystem python.exe processes are externally killed via
    # CTRL+C injection (observed exit code 0xC000013A); pythonw.exe
    # processes survive.  stdout/stderr are pipes/files supplied by the
    # parent, so the verdict-capture behaviour is unchanged.
    return [str(ROOT / ".venv" / "Scripts" / "pythonw.exe"),
            "-X", "utf8", "-X", "faulthandler"]


def _write_forensics(record: dict) -> None:
    write_forensics_file(FORENSICS, record)


def _judged_unittest(label: str, modules: list,
                     expectation: RunExpectation, timeout: int = 600) -> dict:
    """Run a unittest subprocess and judge it with the strict harness.

    On rejection the FULL returncode/stdout/stderr (timing-normalized) are
    written to the forensics file before the runner fails -- no
    substring-only forensics.  A subprocess timeout is likewise captured
    (timeout fact + partial output) into the forensics file BEFORE failing
    (r19 acceptance gap 4); timeouts are never auto-retried.
    """
    try:
        proc = subprocess.run(
            _python_cmd() + ["-m", "unittest"] + modules,
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        _write_forensics(timeout_forensics_record(label, exc))
        _fail(f"{label} timed out after {exc.timeout}s; partial-output "
              f"forensics written (no auto-retry)")
    verdict = judge_unittest_run(proc.returncode, proc.stdout, proc.stderr,
                                 expectation)
    record = audit_record(label, proc, verdict, expectation,
                          normalize_timing=True)
    if not verdict.accepted:
        _write_forensics(record)
        _fail(f"{label} rejected by the strict harness: "
              f"{list(verdict.reasons)}")
    return record


def _discover_slice(package: str, expected_count: int,
                    expected_failures: list,
                    expected_verdict_counts: dict) -> dict:
    """Full-slice discover, honestly recorded.

    The failure-header set (normalized with the same rule as the acceptance
    sed: strip the package prefix inside the parentheses) must equal the
    embedded baseline byte-for-byte -- zero new failures is a hard gate.
    The skip list is recorded as data (never asserted zero).  The raw
    output (timing- and transient-path-normalized: random tempfile names
    and the jieba load-timing line are replaced by stable placeholders, so
    the tracked log files stay byte-identical across runs and a re-run from
    a clean tree leaves `git status` clean) is written to a discover log.

    Returncode gate (r19 acceptance gap 2): a discover slice that keeps its
    baseline failures exits 1, a fully-green slice exits 0; EVERY other
    returncode (usage error, crash, abort) is rejected no matter how
    normal-looking the captured output is -- judgement trusts returncode +
    structured summary, never log-content appearance.  A subprocess timeout
    is captured into the forensics file (timeout fact + partial output)
    before failing (r19 acceptance gap 4); no auto-retry.
    """
    try:
        proc = subprocess.run(
            _python_cmd() + ["-m", "unittest", "discover", "-s", package,
                             "-t", ".", "-v"],
            cwd=ROOT, capture_output=True, text=True, timeout=900,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        _write_forensics(
            timeout_forensics_record(f"discover_{package}", exc))
        _fail(f"discover {package} timed out after {exc.timeout}s; "
              f"partial-output forensics written (no auto-retry)")
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    log_path = OUT_DIR / f"local-revision-r18-discover-{package}.log"
    log_path.write_text(
        normalize_transient_paths(normalize_unittest_timing(stdout))
        + "\n===== STDERR =====\n"
        + normalize_transient_paths(normalize_unittest_timing(stderr)),
        encoding="utf-8", newline="\n")

    summary = parse_unittest_summary(stdout, stderr)
    skips = collect_unittest_skips(stdout, stderr)
    headers = sorted(
        _NORMALIZE_RE.sub("(", line)
        for line in _FAILURE_HEADER_RE.findall(stderr)
    )
    record = {
        "package": package,
        "returncode": proc.returncode,
        "summary": summary,
        "skips": skips,
        "skip_count": len(skips),
        "failure_headers_normalized": headers,
        "failure_header_count": len(headers),
        "log_file": log_path.name,
    }
    problems = []
    rc_reason = discover_returncode_reason(proc.returncode)
    if rc_reason is not None:
        problems.append(rc_reason)
    if summary["ran_count"] != expected_count or summary["summary_lines"] != 1:
        problems.append(f"ran_count={summary['ran_count']},"
                        f"summary_lines={summary['summary_lines']}")
    for key, wanted in expected_verdict_counts.items():
        if summary.get(key) != wanted:
            problems.append(f"{key}={summary.get(key)},expected={wanted}")
    if headers != expected_failures:
        new = sorted(set(headers) - set(expected_failures))
        gone = sorted(set(expected_failures) - set(headers))
        problems.append(f"failure_header_set_drift:new={new},missing={gone}")
    if problems:
        record["rejection_reasons"] = problems
        _write_forensics(record)
        _fail(f"discover {package} drifted from the frozen baseline: "
              f"{problems}")
    return record


def _anchors() -> dict:
    # (a) A01 regression: r13b archive-pinned runner as a subprocess.
    try:
        proc = subprocess.run(
            _python_cmd() + [str(OUT_DIR / "local-revision-r13b.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=900,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        _write_forensics(
            timeout_forensics_record("r13b_archive_runner", exc))
        _fail(f"r13b rerun timed out after {exc.timeout}s; partial-output "
              f"forensics written (no auto-retry)")
    r13b_record = {
        "label": "r13b_archive_runner",
        "returncode": proc.returncode,
        "stdout": normalize_unittest_timing(proc.stdout),
        "stderr": normalize_unittest_timing(proc.stderr),
    }
    if proc.returncode != 0:
        _write_forensics(r13b_record)
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

    # (c) Folio test module (27 tests) and the 367 target set, judged by the
    # strict harness with zero-skip requirements.
    folio_record = _judged_unittest(
        "folio_test_module", ["reaserch_agent.test_route_pdf_folio"],
        RunExpectation(test_count=FOLIO_TEST_COUNT, require_no_skips=True))
    target_record = _judged_unittest(
        "target_set", TARGET_SET_MODULES,
        RunExpectation(test_count=TARGET_SET_SIZE, require_no_skips=True))

    # (d) Full-slice discover, honestly recorded (skips as data; failure
    # header sets asserted byte-equal to the embedded baselines).
    discover_research = _discover_slice(
        "reaserch_agent", DISCOVER_RESEARCH_COUNT, EXPECTED_RESEARCH_FAILURES,
        {"failures": 8, "errors": 21})
    discover_contracts = _discover_slice(
        "chem_agent_contracts", DISCOVER_CONTRACTS_COUNT,
        EXPECTED_CONTRACTS_FAILURES, {"failures": 1, "errors": 0})

    return {
        "a01_regression": {
            "mechanism": "r13b runner re-executed as a subprocess",
            "double_run_byte_identical": True,
            "replay_matches_r13_archive": True,
            "ms7a_out": "BLOCKED (retained_object_mention_precedes_operation)",
            "subprocess_record": r13b_record,
        },
        "diagnostics_module_zero_proof_dependency": True,
        "judgement": {
            "rule": ("accept only when returncode == 0 AND exactly one 'Ran N "
                     "tests' summary with N == expected AND the verdict line "
                     "is exactly OK AND no FAILED/failures=/errors= nonzero "
                     "anywhere AND (must-run suites) no skipped= nonzero"),
            "forensics_on_rejection": FORENSICS.name,
        },
        "folio_test_module": {
            "expected": FOLIO_TEST_COUNT, "accepted": True,
            "record": folio_record,
        },
        "target_set": {
            "modules": len(TARGET_SET_MODULES),
            "expected": TARGET_SET_SIZE, "accepted": True,
            "record": target_record,
        },
        "full_slice_discover": {
            "reaserch_agent": discover_research,
            "chem_agent_contracts": discover_contracts,
            "note": ("skip lists recorded as data (not asserted zero); "
                     "failure-header sets asserted byte-equal to the "
                     "embedded baselines (research: 30-entry baseline minus "
                     "test_b1_bootstrap_generates_initial_outputs, which "
                     "passes in this workspace; contracts: the single "
                     "baseline entry)"),
        },
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
        "schema_version": "g2a_acceptance_hardening/v18",
        "round": ("r18: acceptance-script hardening (strict subprocess "
                  "judgement + structured rejection + full-output audit) + "
                  "root-cause diagnoses of the full-slice skip=2 and the "
                  "intermittent runner abort"),
        "model_generated": False,
        "model_called": False,
        "baseline": baseline,
        "code_changes": {
            "new_files": {HARNESS_FILE: HARNESS_SHA256,
                          HARNESS_TEST_FILE: HARNESS_TEST_SHA256},
            "modified_files": {FOLIO_TEST_FILE: FOLIO_TEST_SHA256},
            "parser_files_pinned_at_r17": PARSER_FILES,
            "semantics_note": (
                "conventions, protocol schemas, parser code, and every rule "
                "resource are byte-identical to the r17 state; only test "
                "infrastructure changes (the strict judgement harness and "
                "the folio historical-loader's skip/failure semantics); "
                "PDF_GROUP_PARSER_VERSION stays route_pdf_groups/v3"),
        },
        "acceptance_hardening": {
            "r17_rule": ("accepted when the output contained the substrings "
                         "'Ran N tests' and 'OK'"),
            "r17_false_accepts": [
                "returncode != 0 with a tail containing the substrings",
                "OK (skipped=2) on a must-run suite",
            ],
            "r17_only_reject": "progress dots with no summary line",
            "r18_rule": ("returncode == 0 AND exactly one 'Ran N tests' "
                         "summary with N == expected AND verdict line "
                         "exactly OK AND no FAILED / failures= / errors= "
                         "nonzero anywhere AND zero skipped= on must-run "
                         "suites; every rejection carries structured "
                         "reasons and the full timing-normalized "
                         "stdout/stderr is audit-recorded"),
            "negative_tests": [
                "nonzero returncode + 'Ran 346 tests' + 'OK' -> rejected "
                "(nonzero_returncode:1)",
                "'OK (skipped=2)' on a must-run suite -> rejected "
                "(unexpected_skips:2)",
                "progress dots only, no summary -> rejected "
                "(summary_missing + verdict_missing)",
            ],
            "positive_tests": [
                "returncode 0 + 'Ran N tests' + 'OK' + zero skips -> "
                "accepted",
                "loader import/exec error -> RuntimeError chaining the "
                "original exception (test FAILS, no disguise)",
                "loader git executable/commit genuinely unavailable -> "
                "HistoricalSourceUnavailable with distinguished messages "
                "(honest skip)",
            ],
        },
        "diagnosis_a_full_slice_skip": {
            "symptom": ("full-slice discover reported OK (skipped=2): the "
                        "folio pre-r15/pre-r17 comparisons skipped with 'git "
                        "or the commit unavailable' while passing standalone"),
            "root_cause_chain": [
                "the agent shell exports GIT_CONFIG_COUNT=2, "
                "GIT_CONFIG_KEY_0=credential.helper and an EMPTY "
                "GIT_CONFIG_VALUE_0",
                "the first mock.patch.dict(os.environ) round-trip in the "
                "suite (alphabetically in test_action_evidence_selection) "
                "restores via os.environ.clear()+update(); re-setting an "
                "empty-valued variable DELETES it from the Win32 kernel "
                "environment block while os.environ keeps showing '' "
                "(micro-reproduced)",
                "children spawned with env=None inherit that block: "
                "GIT_CONFIG_VALUE_0 missing with COUNT=2/KEY_0 present -> "
                "git exits rc=128 'missing config value GIT_CONFIG_VALUE_0'",
                "the old loader mapped any git failure to skipTest",
            ],
            "evidence": [
                "verbose discover: both skips carry 'git or the pre-r1[57] "
                "commit unavailable'",
                "instrumented loader inside discover: git rc=128 "
                "'missing config value GIT_CONFIG_VALUE_0', while the same "
                "call with env=dict(os.environ) returns rc=0",
                "kernel-level env sniff: exactly one flip present:'' -> "
                "MISSING, at the first patch.dict(os.environ) test, never "
                "recovers",
                "micro-repro: one patch.dict(os.environ) round-trip flips "
                "GIT_CONFIG_VALUE_0 from present:'' to kernel-MISSING and "
                "git env=None fails rc=128 while env=copy succeeds",
                "full-slice discover with GIT_CONFIG_* stripped: 1229 "
                "tests, zero skips",
                "with the loader fix, full-slice discover in the polluted "
                "shell: 1252 tests, zero skips",
            ],
            "fix": ("loader passes env=dict(os.environ) to git and skips "
                    "only on genuine git/commit absence "
                    "(HistoricalSourceUnavailable); import/exec errors fail "
                    "the test with the original exception chained"),
            "polluter_not_changed": ("test_action_evidence_selection's "
                                     "patch.dict(os.environ) is a legitimate "
                                     "pattern; the trigger is the shell's "
                                     "empty GIT_CONFIG_VALUE_0, so the fix "
                                     "lands in the loader, not in 100+ "
                                     "patch.dict users"),
        },
        "diagnosis_b_intermittent_abort": {
            "symptom": ("one of six r17 runner runs failed because the "
                        "target-set subprocess output stopped at progress "
                        "dots with no summary; isolated re-runs of the same "
                        "call passed 15/15 and direct module runs 5/5"),
            "reproduction_attempts": {
                "identical_invocation_clean": 60,
                "three_way_parallel": 18,
                "under_8gb_memory_pressure": 6,
                "anomalies": 0,
            },
            "conclusion": ("NOT reproduced in 102 invocations (0 anomalies, "
                           "every rc=0 with a complete summary); the "
                           "dots-only-no-summary shape implies the child "
                           "python.exe died mid-run with a non-zero exit "
                           "(a normal unittest exit always prints the "
                           "summary), most consistent with a hard child "
                           "crash or an external kill; no resource, patch, "
                           "or output-capture defect was observed"),
            "forensics_built_in": [
                "every subprocess this runner spawns carries -X faulthandler "
                "(a native crash would dump the Python stack to stderr)",
                "every judged subprocess records returncode + full "
                "timing-normalized stdout/stderr into the replay anchors",
                "on rejection the runner writes "
                "local-revision-r18-failure-forensics.json BEFORE failing",
            ],
            "next_steps_if_recurs": [
                "read the forensics file: a 0xC0000005-style returncode "
                "fingers a native crash (MuPDF is the only native library "
                "in the target set); a missing process with rc=None/"
                "timeout fingers resource pre-emption",
                "correlate the wall-clock with external process logs "
                "(AV/EDR) on the host",
            ],
        },
        "neighborhood_fix": fix,
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
                "meaning": ("unchanged from r15/r16/r17: the SI parses and "
                            "stops at experimental_section_missing; the "
                            "main paper still abstains at ingestion"),
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
            "no parser/PDF-ingestion production code change",
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
            "r7_to_r17_archives_untouched": True,
            "contracts_tree_untouched": True,
            "diagnostics_module_semantics_untouched": True,
            "conventions_no_v1_expansion": True,
            "parser_files_byte_identical_to_r17": True,
            "tokens_consumed_by_this_runner": 0,
            "model_not_called": True,
            "device_not_run": True,
            "ms7a_cascade_still_blocked": True,
            "tracked_modifications_limited_to_round18_files": True,
        },
    }


def _audit() -> dict:
    return {
        "schema_version": "g2a_acceptance_hardening/v18",
        "model_generated": False,
        "scope": ("r18: acceptance-script hardening (strict unittest "
                  "subprocess judgement in reaserch_agent/route_test_harness."
                  "py, exercised by this runner) and two root-cause "
                  "diagnoses; the folio historical loader now skips only on "
                  "genuine git/commit absence and fails loudly with the "
                  "original error otherwise; no model call, no "
                  "conventions/protocol/parser semantic change"),
        "audit": [
            {"kind": "changed_files",
             "items": [
                 "reaserch_agent/route_test_harness.py (NEW: strict "
                 "judgement + audit_record + skip collector; pure test "
                 "infrastructure, not on any production path)",
                 "reaserch_agent/test_route_test_harness.py (NEW: 41 tests "
                 "-- the three r17 false-accept shapes rejected, positive "
                 "accept, defence-in-depth, loader exception/skip "
                 "semantics)",
                 "reaserch_agent/test_route_pdf_folio.py (MODIFIED, still 27 "
                 "tests: loader raises HistoricalSourceUnavailable only for "
                 "genuine git/commit absence with distinguished messages, "
                 "fails loudly on import/exec errors, and passes an explicit "
                 "env copy to git)",
                 "result/operation-structure-20260928/local-revision-r18.py",
                 "result/operation-structure-20260928/local-revision-r18-*.json",
                 "result/operation-structure-20260928/local-revision-r18-discover-*.log",
                 "docs/field_semantic_gate_r18_checkpoint_20261005.md",
             ]},
            {"kind": "capability_change",
             "detail": ("none in production code; acceptance capability "
                        "changes: subprocess verdicts are now judged on "
                        "returncode + exact summary count + exact OK verdict "
                        "+ zero failure/error counts + zero skips for "
                        "must-run suites, with structured rejection reasons "
                        "and full-output forensics")},
            {"kind": "diagnoses",
             "detail": ("(a) full-slice skip=2 root-caused to an empty "
                        "GIT_CONFIG_VALUE_0 exported by the agent shell x "
                        "mock.patch.dict(os.environ) restore semantics on "
                        "Windows (empty-valued variables are deleted from "
                        "the kernel environment block) x env=None child "
                        "inheritance -> git rc=128 -> disguised skip; fixed "
                        "at the loader (explicit env copy) with skip/"
                        "failure semantics separated; (b) the intermittent "
                        "abort was NOT reproduced in 102 subprocess "
                        "invocations under clean/parallel/memory-pressure "
                        "conditions; -X faulthandler and full-output audit "
                        "recording are built into the runner for the next "
                        "occurrence")},
            {"kind": "honest_endpoints",
             "detail": ("W1/W2 ineligible under the frozen v1 rules; "
                        "caption-boundary corrections remain a recorded "
                        "design, not an implementation; dag_consumption "
                        "remains unobserved and unclaimed; diagnosis (b) is "
                        "reported as not-reproduced, not explained away")},
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

    (OUT_DIR / "local-revision-r18-replay.json").write_text(
        replay_text, encoding="utf-8", newline="\n")
    (OUT_DIR / "local-revision-r18-audit.json").write_text(
        audit_text, encoding="utf-8", newline="\n")
    research = replay["anchors"]["full_slice_discover"]["reaserch_agent"]
    contracts = replay["anchors"]["full_slice_discover"]["chem_agent_contracts"]
    summary = {
        "acceptance_hardening": {
            "folio_module_judged_ok": FOLIO_TEST_COUNT,
            "target_set_judged_ok": TARGET_SET_SIZE,
            "zero_skip_required_and_met": True,
        },
        "diagnosis_a": {
            "root_cause": ("empty GIT_CONFIG_VALUE_0 (shell) x "
                           "patch.dict(os.environ) restore (Windows kernel "
                           "env block loses empty values) x env=None "
                           "inheritance -> git rc=128 -> disguised skip"),
            "fixed_at_loader": True,
            "research_discover_skips_now": research["skip_count"],
        },
        "diagnosis_b": {
            "reproduced": False,
            "attempts": replay["diagnosis_b_intermittent_abort"]
            ["reproduction_attempts"],
            "forensics_built_in": True,
        },
        "full_slice": {
            "research_tests": research["summary"]["ran_count"],
            "research_failure_headers": research["failure_header_count"],
            "research_failure_set_matches_baseline": True,
            "research_skips": research["skip_count"],
            "contracts_tests": contracts["summary"]["ran_count"],
            "contracts_failure_headers": contracts["failure_header_count"],
            "contracts_failure_set_matches_baseline": True,
            "contracts_skips": contracts["skip_count"],
        },
        "parser_version": "route_pdf_groups/v3",
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
        "pool_census_identical_to_r15_r16_r17": (
            replay["pool_census"]["identical_to_r15_post_census"]
            and replay["pool_census"]["identical_to_r16_census"]
            and replay["pool_census"]["identical_to_r17_census"]),
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
