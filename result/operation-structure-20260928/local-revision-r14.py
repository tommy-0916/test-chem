"""Round-14 (cross-paper real-input capability probe, Wu-2025).

This round TESTS capability on a real cross-paper input; it changes no
semantics.  No existing source file, test, or archive (r7-r13b,
chem_agent_contracts/, the 3E diagnostics module) is modified.  The round's
new inputs live in ``result/wu2025-real-input-20261004/`` (signed KB, trust
config, identity evidence, connectivity record).

What this runner establishes, honestly and deterministically:

1. **Baseline freeze** — HEAD `44a39d9` (the G1 follow-up commit), the
   conventions rule resource sha256, the prompt-builder module sha256, the
   A01 source digest, the Wu-2025 PDF digests, and the configured model
   name/endpoint (no secrets).  Live drifts of the pinned resources fail
   this runner.

2. **Source identity (attestation layer) works cross-paper.**  The Wu-2025
   main paper (`primary_paper`) and its SI (`supporting_information`, bound
   via ``parent_doi`` — the toolchain supports SI natively; the often-quoted
   exclusion concerns *text/markdown derivatives*, not SI PDFs) both verify
   under a fresh Ed25519 key whose private part lives outside the repo.
   Identity evidence was fetched live on 2026-10-04 and both publisher
   downloads hash byte-identical to the local PDFs (evidence/provenance).

3. **PDF ingestion abstains on the whole local cross-paper pool.**  The Wu
   main paper and SI both enumerate ZERO groups with
   ``pdf_column_layout_ambiguous`` (Nature two-column layout; a single
   ambiguous page aborts the whole document, route_pdf_source.py).  The same
   census over every other local candidate PDF (Zhang-2017 main/SI, the three
   chem_kb papers, one upload) abstains likewise (`experimental_section_missing`
   for Zhang SI, `pdf_source_too_large` for the PBA review).  The W1 target
   group (SI page S14, Figure S13 caption paragraph) therefore never reaches
   the formal entry — its evidence sentence exists in the source text but the
   pipeline's own extractor cannot reach it.

4. **Model layer was unavailable this round.**  The configured channel (k3 @
   api.kimi.com/coding/v1) and the codex-responses wire both return HTTP 403
   account weekly-quota exhaustion (raw responses archived in
   connectivity.json).  Real proposal generation is recorded as NOT REACHED
   for two independent reasons (no parseable group upstream; quota
   downstream).  Nothing was replayed or hand-constructed as a substitute.

5. **Anchors.**  (a) Source mutation: a byte-flipped copy of the Wu SI PDF
   fails attestation (`source_digest_or_format_mismatch`) while the untampered
   control verifies.  (b) Wrong binding: an event carrying a wrong document
   digest fails attestation lookup, and a registry record whose DOI no longer
   matches the SI attestation's ``parent_doi`` drops the source entirely.
   (c) A01 regression: the r13b runner is re-executed as a subprocess and its
   archive pins must hold (ms7a.out stays
   ``retained_object_mention_precedes_operation`` BLOCKED with its cascade).
   (d) 3E guard: the diagnostics module source keeps zero proof dependency
   (string audit; the behavioral guard tests already exist and are not
   rebuilt here).

Layer discipline: field-proof success, group-level receipt pass, protocol
admission and publication are counted separately; this round observes NONE of
them on the cross-paper input and claims none.

The runner is deterministic: two in-memory builds are byte-identical; all
inputs are local archived bytes.  Zero tokens are consumed by this runner.
"""
import json
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "result" / "operation-structure-20260928"
WU = ROOT / "result" / "wu2025-real-input-20261004"

FROZEN_HEAD = "44a39d9b70b65f4daab462aa98fd588a2ff01a74"
CONVENTIONS_SHA256 = (
    "39fb6e77c7e40dc30db6819d9ae9e2823f5120f3ff165207668c22b464e8267b"
)
PROMPT_BUILDER_FILE = "reaserch_agent/route_pdf_group_extraction.py"
PROMPT_BUILDER_SHA256 = (
    "24864d9af522666b15a1690b6c9ba829bebfa3047545d82adc3e3dff331486f7"
)
A01_SOURCE_DIGEST = (
    "sha256_2e9435bccb2e2e281edc0fcbd0ff27ffe48b8c0babcc5e346fbdc0d679df12c8"
)
WU_MAIN_SHA256 = "0f1be588bb891a4e955e5133731295120818a84f00a3750e22cf8504d81f1819"
WU_SI_SHA256 = "cdd842d87e34a8dc70d87e5999e8665f5fe054e66a22f0908b72ea2953f365d4"
PAPER_ID = "doi_10_1038_s41467_025_58320_5"

W1_EVIDENCE = {
    "group_candidate": "W1 (powdered Ni-Mo nanoparticles)",
    "document": "B01-Wu-2025-NatCommun-SI.pdf",
    "page_label": "S14",
    "page_number": 14,
    "figure": "Supplementary Fig. S13 caption paragraph",
    "verbatim": ("After the hydrothermal reaction, the precipitated Ni-Mo-O "
                 "powder was collected, dried, and then reduced at 500 °C "
                 "for 6 h under a mixed hydrogen-nitrogen flow to obtain "
                 "Ni-Mo nanoparticles."),
    "recon_text": ("si-search/pdf-recon/text/"
                   "B01-Wu-2025-NatCommun-SI__cdd842d8.txt lines 162-168"),
    "chain": ("hydrothermal dissolve -> autoclave 120 °C 12 h -> "
              "collect/dry -> reduce 500 °C 6 h (phase change to Ni-Mo "
              "nanoparticles)"),
    "pool_honesty": ("no candidate-pool source uses centrifugation; "
                     "retained-phase wording is collection/filtration"),
}

POOL = [
    ("wu2025-main", "backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-main.pdf"),
    ("wu2025-si", "backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-SI.pdf"),
    ("zhang2017-main", "backend/data/reference_sources/B01/B01-Zhang-2017-NatCommun-main.pdf"),
    ("zhang2017-si", "backend/data/reference_sources/B01/B01-Zhang-2017-NatCommun-SI.pdf"),
    ("chemkb-kion", "reaserch_agent/chem_kb/High-Capacity Aqueous Potassium-Ion Batteries for Large-Scale Energy Storage.pdf"),
    ("chemkb-pba-hosts", "reaserch_agent/chem_kb/High-Entropy Prussian Blue Analogues and Their Oxide Family as Sulfur Hosts for Lithium-Sulfur Batteries.pdf"),
    ("chemkb-nife-pba", "reaserch_agent/chem_kb/Operando Spectroscopic Identification of Active Sites in NiFe Prussian Blue Analogues as Electrocatalysts- Activation of Oxygen Atoms for Oxygen Evolution Reaction.pdf"),
    ("upload-upl_1e79", "backend/data/uploads/upl_1e796859a263c3e5c502c488.pdf"),
]


def _fail(message: str) -> None:
    raise SystemExit(f"r14 acceptance failed: {message}")


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _baseline() -> dict:
    conventions = ROOT / "chem_resources" / "chemistry_conventions" / "conventions.json"
    prompt_builder = ROOT / PROMPT_BUILDER_FILE
    if _sha256_file(conventions) != CONVENTIONS_SHA256:
        _fail("conventions.json drifted from the frozen baseline")
    if _sha256_file(prompt_builder) != PROMPT_BUILDER_SHA256:
        _fail("prompt-builder module drifted from the frozen baseline")
    return {
        "frozen_head": FROZEN_HEAD,
        "conventions_json_sha256": CONVENTIONS_SHA256,
        "prompt_builder_module": PROMPT_BUILDER_FILE,
        "prompt_builder_sha256": PROMPT_BUILDER_SHA256,
        "a01_source_digest": A01_SOURCE_DIGEST,
        "wu2025_main_pdf_sha256": WU_MAIN_SHA256,
        "wu2025_si_pdf_sha256": WU_SI_SHA256,
        "model_name": "k3",
        "model_endpoint": "https://api.kimi.com/coding/v1",
        "model_key": "not recorded",
    }


def _wu_kb_identity() -> dict:
    from reaserch_agent.route_attestation import attested_route_sources
    from reaserch_agent.run_research_agent import load_route_trust_config

    config_path = WU / "route-trust-config.json"
    kb = WU / "kb"
    trust = load_route_trust_config(str(config_path), str(kb))
    sources = attested_route_sources(
        kb, trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"],
    )
    found = sources.get(PAPER_ID, [])
    kinds = sorted(source.document_kind for source in found)
    if kinds != ["primary_paper", "supporting_information"]:
        _fail(f"Wu-2025 attestation must verify both documents, got {kinds}")
    return {
        "paper_id": PAPER_ID,
        "doi": "10.1038/s41467-025-58320-5",
        "key_id": "wu2025-r14-source-v1",
        "issuer": "manual-source-review",
        "private_key_location": "outside the repo (never committed)",
        "documents": [
            {"document_kind": s.document_kind,
             "document_digest": s.document_digest, "doi": s.doi}
            for s in sorted(found, key=lambda s: s.document_kind)
        ],
        "si_binding": ("supporting_information attestation carries "
                       "parent_doi=10.1038/s41467-025-58320-5; the registry "
                       "record DOI must match it or the source is dropped"),
        "provenance": ("publisher re-downloads on 2026-10-04 hashed "
                       "byte-identical to the local PDFs (main "
                       "nature.com/articles/...pdf; SI static-content "
                       "MOESM1, 7,684,073 bytes)"),
        "trust_config_sha256": "sha256_" + _sha256_file(config_path),
    }


def _wu_enumeration() -> dict:
    from reaserch_agent.route_pdf_groups import (
        enumerate_attested_pdf_experimental_groups,
    )
    from reaserch_agent.run_research_agent import load_route_trust_config

    kb = WU / "kb"
    trust = load_route_trust_config(str(WU / "route-trust-config.json"),
                                    str(kb))
    result = enumerate_attested_pdf_experimental_groups(
        kb, trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"],
    )
    reasons = sorted({d.reason_code for d in result.diagnostics})
    if result.groups:
        _fail("Wu-2025 enumeration unexpectedly produced groups")
    if reasons != ["pdf_column_layout_ambiguous"]:
        _fail(f"Wu-2025 enumeration diagnostics drifted: {reasons}")
    return {
        "groups": 0,
        "diagnostic_reasons": reasons,
        "documents_flagged": len(result.diagnostics),
        "meaning": ("attestation verified and the signed sources reached the "
                    "parser; the two-column Nature layout abstains before any "
                    "experimental section is read"),
    }


def _pool_census() -> list:
    from reaserch_agent.route_pdf_groups import enumerate_pdf_experimental_groups

    rows = []
    for tag, rel in POOL:
        path = ROOT / rel
        result = enumerate_pdf_experimental_groups(
            {"pool_" + tag: path}, source_root=path.parent)
        rows.append({
            "candidate": tag,
            "file": rel,
            "groups": len(result.groups),
            "reasons": sorted({d.reason_code for d in result.diagnostics}),
        })
    if any(row["groups"] for row in rows):
        _fail("pool census unexpectedly produced a group")
    return rows


def _model_layer() -> dict:
    record = json.loads((WU / "connectivity.json").read_text(encoding="utf-8"))
    attempts = record["attempts"]
    if any(attempt.get("ok") for attempt in attempts):
        _fail("connectivity record unexpectedly reports a working channel")
    return {
        "model_name": record["model_name"],
        "endpoint": record["endpoint"],
        "channels_tried": [a["channel"] for a in attempts],
        "result": "http_403_weekly_quota_exhausted_on_both_channels",
        "raw_record": "result/wu2025-real-input-20261004/connectivity.json",
        "real_generation": (
            "not_reached: zero enumerated groups upstream AND model quota "
            "exhausted downstream; no replayed or hand-built proposal was "
            "substituted"),
    }


def _anchors() -> dict:
    from reaserch_agent.route_attestation import (
        TrustedAcquisitionEventV1, attested_route_sources,
        verify_source_document_attestation,
    )
    from reaserch_agent.run_research_agent import load_route_trust_config

    kb = WU / "kb"
    trust = load_route_trust_config(str(WU / "route-trust-config.json"),
                                    str(kb))
    events = [
        TrustedAcquisitionEventV1.model_validate(env["event"])
        for env in trust["signed_route_source_events"]
    ]
    si_event = next(
        e for e in events if e.document_kind == "supporting_information")
    si_pdf = kb / si_event.kb_relative_path

    # (a) Source mutation: a byte-flipped copy must fail attestation.
    with tempfile.TemporaryDirectory() as tmp:
        tampered_kb = Path(tmp) / "kb"
        tampered_pdf = tampered_kb / si_event.kb_relative_path
        tampered_pdf.parent.mkdir(parents=True)
        raw = bytearray(si_pdf.read_bytes())
        raw[4096] ^= 0xFF
        tampered_pdf.write_bytes(bytes(raw))
        mutation = verify_source_document_attestation(
            kb_root=tampered_kb, trusted_event=si_event)
    control = verify_source_document_attestation(
        kb_root=kb, trusted_event=si_event)
    if not control.verified:
        _fail("untampered Wu SI must verify")
    if mutation.verified or mutation.reasons != (
            "source_digest_or_format_mismatch",):
        _fail(f"tampered Wu SI must fail attestation, got {mutation.reasons}")

    # (b1) Wrong binding: an event with a wrong document digest.
    wrong_digest_event = TrustedAcquisitionEventV1(
        schema_version="trusted_acquisition_event_v1",
        paper_id=si_event.paper_id,
        kb_relative_path=si_event.kb_relative_path,
        document_digest="sha256_" + "0" * 64,
        document_kind=si_event.document_kind,
        attestation_digest=si_event.attestation_digest,
        issuer=si_event.issuer,
        identity_verdict=si_event.identity_verdict,
    )
    wrong_digest = verify_source_document_attestation(
        kb_root=kb, trusted_event=wrong_digest_event)
    if wrong_digest.verified:
        _fail("wrong-digest event must not verify")

    # (b2) Wrong binding: registry DOI no longer matching the SI parent_doi.
    with tempfile.TemporaryDirectory() as tmp:
        reg_kb = Path(tmp) / "kb"
        for item in (kb / "_pdf_sources").rglob("*"):
            if item.is_file():
                target = reg_kb / item.relative_to(kb)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.read_bytes())
        att_dir = reg_kb / "registry" / "route_source_attestations_v1"
        att_dir.mkdir(parents=True)
        for item in (kb / "registry" / "route_source_attestations_v1").glob("*.json"):
            (att_dir / item.name).write_bytes(item.read_bytes())
        record = json.loads(
            (kb / "registry" / "papers.jsonl").read_text(encoding="utf-8"))
        record["doi"] = "10.0000/unrelated.doi"
        record["pdf_files"] = [
            str((reg_kb / "_pdf_sources" / "wu2025" / Path(p).name).resolve())
            for p in record["pdf_files"]
        ]
        (reg_kb / "registry" / "papers.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        rebound = attested_route_sources(
            reg_kb, trust["signed_route_source_events"],
            trusted_public_keys=trust["trusted_route_public_keys"],
        )
    if rebound.get(PAPER_ID):
        _fail("registry DOI mismatch must drop the attested sources")

    # (c) A01 regression anchor: re-execute the r13b archive-pinned runner.
    import os
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

    # (d) 3E diagnostics module: zero proof dependency (source audit).
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

    return {
        "source_mutation": {
            "control_untampered_verified": True,
            "tampered_si_reasons": list(mutation.reasons),
        },
        "wrong_binding": {
            "wrong_document_digest_reasons": list(wrong_digest.reasons),
            "registry_doi_mismatch_sources": len(rebound.get(PAPER_ID, [])),
        },
        "a01_regression": {
            "mechanism": "r13b runner re-executed as a subprocess",
            "double_run_byte_identical": True,
            "replay_matches_r13_archive": True,
            "ms7a_out": "BLOCKED (retained_object_mention_precedes_operation)",
        },
        "diagnostics_module_zero_proof_dependency": True,
    }


def _build_replay() -> dict:
    baseline = _baseline()
    identity = _wu_kb_identity()
    enumeration = _wu_enumeration()
    census = _pool_census()
    model_layer = _model_layer()
    anchors = _anchors()
    replay = {
        "schema_version": "cross_paper_capability_probe/v14",
        "round": ("r14: cross-paper real-input capability probe "
                  "(Wu-2025 NatCommun)"),
        "model_generated": False,
        "semantics_changed": False,
        "baseline": baseline,
        "source_evidence": {
            **W1_EVIDENCE,
            "sufficient_at_text_level": True,
            "reachable_by_pipeline_extractor": False,
        },
        "kb_identity": identity,
        "model_proposal": {
            "status": "not_reached",
            "graph_steps": None, "route_facts": None, "state_facts": None,
            "covers_collect_dry_reduce_chain": None,
            "model_layer": model_layer,
        },
        "layer_results": {
            "attestation": {"status": "verified",
                            "documents": 2,
                            "detail": identity["si_binding"]},
            "group_enumeration": {"status": "abstained", **enumeration},
            "proposal_extraction": {"status": "not_reached",
                                    "reason": "no group to propose from"},
            "local_revision": {"status": "not_reached"},
            "association": {"status": "not_reached"},
            "receipt": {"status": "not_reached"},
            "pool_census": census,
        },
        "dag_consumption": {
            "criterion": ("at least one field that flat/literal cannot prove, "
                          "proven by the multi-hop DAG and accepted by the "
                          "receipt as dag_proven_state_field_paths"),
            "observed": False,
            "dag_proven_state_field_paths": [],
            "note": ("the criterion is unobservable this round: no group "
                     "survives ingestion, so no DAG is ever built; this is "
                     "NOT claimed as a pass"),
        },
        "first_blocker": {
            "layer": "pdf_ingestion",
            "reason_code": "pdf_column_layout_ambiguous",
            "where": ("reaserch_agent/route_pdf_source.py "
                      "_ordered_page_lines: one ambiguous page aborts the "
                      "whole document"),
            "evidence": enumeration,
            "scope": ("every local cross-paper candidate abstains at "
                      "ingestion; see layer_results.pool_census"),
        },
        "separate_counters": {
            "field_proofs_succeeded": 0,
            "group_receipt_pass": 0,
            "protocol_admitted": 0,
            "published": 0,
        },
        "anchors": anchors,
        "fixed_constraints": {
            "existing_source_files_modified": False,
            "r7_to_r13b_archives_untouched": True,
            "contracts_tree_untouched": True,
            "diagnostics_module_semantics_untouched": True,
            "no_v1_expansion": True,
            "tokens_consumed_by_this_runner": 0,
            "device_not_run": True,
            "eight_question_rerun": False,
            "ms7a_cascade_still_blocked": True,
        },
        "minimal_fix_suggestions_not_implemented": [
            ("pdf_ingestion: teach _ordered_page_lines to resolve (or skip "
             "with a diagnostic row) ambiguous two-column pages instead of "
             "aborting the whole document; smallest honest step: per-page "
             "abstention that preserves page order so figure-heavy pages do "
             "not kill method pages"),
            ("si_section_recognition: SI PDFs use figure-caption paragraphs, "
             "not Methods-style group headings; a caption-boundary grouping "
             "mode would be required before W1-class groups can enumerate"),
            ("model_quota: rerun the real generation once the weekly quota "
             "window resets (no code change needed)"),
        ],
        "terminology": ("this round is a capability probe on real cross-paper "
                        "input; nothing here is an A01-style PASS, a new "
                        "model generation result, or a V2 publication gate"),
    }
    return replay


def _audit() -> dict:
    return {
        "schema_version": "cross_paper_capability_probe/v14",
        "model_generated": False,
        "scope": ("r14 capability probe: sign Wu-2025 (main + SI via native "
                  "parent_doi binding), run attested enumeration, and record "
                  "the honest first blocker (pdf_column_layout_ambiguous) "
                  "plus the model-layer quota exhaustion; no semantics "
                  "changed, no PASS claimed"),
        "audit": [
            {"kind": "new_files_only",
             "items": [
                 "result/wu2025-real-input-20261004/ (kb, trust config, "
                 "evidence, connectivity, inventory, build_kb.py)",
                 "result/operation-structure-20260928/local-revision-r14.py",
                 "result/operation-structure-20260928/local-revision-r14-*.json",
                 "docs/field_semantic_gate_r14_checkpoint_20261004.md",
             ]},
            {"kind": "si_binding_verdict",
             "detail": ("the attestation schema natively supports "
                        "supporting_information with a required parent_doi "
                        "and publisher_si_link/reviewed_identity evidence; "
                        "the docstring's exclusion concerns text/markdown "
                        "derivatives, not SI PDFs; the Wu SI verifies under "
                        "this binding")},
            {"kind": "capability_gap",
             "detail": ("the PDF block extractor abstains on every local "
                        "cross-paper candidate (two-column layouts, one "
                        "oversize review, one SI without a Methods-style "
                        "section); the G1 machinery below ingestion was not "
                        "exercised on new input this round")},
            {"kind": "model_layer",
             "detail": ("both configured channels returned HTTP 403 weekly "
                        "quota exhaustion; no proposal was generated, "
                        "replayed, or hand-built")},
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

    (OUT_DIR / "local-revision-r14-replay.json").write_text(
        replay_text, encoding="utf-8")
    (OUT_DIR / "local-revision-r14-audit.json").write_text(
        audit_text, encoding="utf-8")
    summary = {
        "groups_enumerated": replay["layer_results"]["group_enumeration"]["groups"],
        "attested_documents": replay["layer_results"]["attestation"]["documents"],
        "first_blocker": replay["first_blocker"]["reason_code"],
        "dag_consumption_observed": replay["dag_consumption"]["observed"],
        "anchors_ok": True,
        "double_run_byte_identical": True,
        "replay_sha256": "sha256_" + sha256(
            replay_text.encode("utf-8")).hexdigest(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
