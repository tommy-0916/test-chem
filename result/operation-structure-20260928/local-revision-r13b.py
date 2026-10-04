"""Round-13b (G1): acceptance-wording correction of the round-13 runner.

The r13 runner's header described the Electrode Preparation control
inaccurately ("its two DAG-proven input-state fields (flat could not derive
them) are DAG-accepted at the receipt").  The recorded r13 replay itself
contradicts that sentence: both EP DAG-PASS rows are single-node
``paper_literal`` roots and are ``verified_literal`` at the receipt in the
baseline as well as the integrated run, so the receipt's DAG acceptance for
EP is ZERO by design — EP is an independent LITERAL positive (it proves DAG
recognition at the extraction point on an independent group), never a
DAG-acceptance positive.  The DAG-exclusive multi-hop receipt acceptance is
demonstrated only on the NiFe Control chain (graph[6].out and ms7a.in as
``dag_proven_state_field_paths``).  The ER control stays an honestly
recorded coverage gap, and an INDEPENDENT multi-hop positive control (a
second signed group whose receipt DAG-accepts a multi-hop-proven field) is
still pending acceptance and is not claimed.

r13b changes no recorded semantics: it regenerates the same replay/audit
from the same fixed inputs and PINS both outputs to the archived r13
digests (replay LF sha256
``sha256_9084ca5497ccf0b46cdb0b10697953742bdb90814768d19b5ef0f1b30c7f2cb5``,
audit LF sha256
``sha256_9919a447e7759f977062f8c1e03dc7e3b9e4556eab4951ba3853b957f86969ee``),
so any future semantic drift in the r13 verdicts fails this runner loudly.
This pin also evidences that the concurrent G1 follow-up fixes (unit-check
rescue ordering at the receipt, DAG-aware diagnostics producers, and
final-version DAG rows on the locator artifact) are verdict-neutral for the
r13 acceptance.  Only the prose is corrected; the persisted bytes are
deliberately identical to r13.

Determinism convention: "two runs byte-identical" compares the two
in-memory UTF-8 serializations inside one process; the
``double_run_byte_identical`` flag and ``replay_sha256`` appear in the
stdout summary, not inside the persisted replay JSON.  Files are written
with ``\n`` newlines; on Windows with ``core.autocrlf=true`` the working
tree shows CRLF while the index stores LF — the pinned digests above are
over the LF bytes.

The original round-13 acceptance text follows, unchanged except for the
corrected EP control paragraph.

Round-13 (G1): wire the accepted state-proof-dag/v1 into the two G1
consumption points of the formal extraction/receipt chain.

Rounds r10/r11 (3C/3D) accepted the typed multi-hop state-proof DAG
(``state-proof-dag/v1``) and r12 (3E) kept operation-precondition inference
diagnostics-only.  Until this round the DAG had NO production consumer: the
two G1 consumption points ran the flat single-hop convention engine only.

This runner is the G1 acceptance: it drives the FORMAL entry — never the
builder directly — with a fixed model response (the unchanged, untruncated
r10 A01 proposal for the real signed NiFe Control group) through

    propose_pdf_group_unreviewed(check_required_graph_facts=True)
      -> bounded local revision (fixed response, no repair merged)
      -> produce_pdf_proposal_locators
      -> associate_pdf_group_proposals        (workflow diagnostic branch)
      -> produce_pdf_group_fact_receipt

and records, per ``.state`` field path, the BEFORE column (baseline_flat:
the untouched flat path, reproduced by disabling the DAG map only) and the
AFTER column (integrated_dag: the parallel ``convention_state_proof_dags``
extraction key and the receipt's ``dag_proven_state_field_paths``).

Accepted verdicts asserted against the committed r10/r11 tables:

- graph[5].out / graph[6].in / graph[6].out / graph[7].in (ms7a.in):
  DAG verdict PASS through the formal entry.  graph[5].out and graph[6].in
  remain flat-derivable at the receipt (flat runs first by design);
  graph[6].out and ms7a.in are the DAG-EXCLUSIVE multi-hop proofs the
  receipt now accepts as ``dag_proven_state_field_paths`` (flat could only
  report ``convention_parent_state_unverified`` there).
- ms7a.out (graph[7].out): still BLOCKED,
  ``retained_object_mention_precedes_operation``, attribution evidence_gap;
  the receipt must NOT derive-accept it.
- ms7b.in / ms7b.out / graph[9].in / graph[9].out: still BLOCKED as
  dependency cascades; the receipt must NOT derive-accept them.

Independent control: the Electrode Preparation group (real signed group,
campaign work-state proposal from A01-v5-quote-context-20260927) is driven
through the SAME formal entry with a fixed response.  Its two DAG-PASS
extraction rows (graph[1].in[1], graph[4].in[1]) are single-node
``paper_literal`` roots; at the receipt both paths are ``verified_literal``
in the baseline and the integrated run alike, so the receipt DAG-accepts
ZERO EP fields — EP is an independent literal positive, not a
DAG-acceptance positive.  The preferred ER control was attempted and is
honestly recorded as a coverage gap: its work-state proposal blocks the
formal entry at source-operation coverage (``source_operation_unrepresented``)
and carries no multi-hop-provable state fields (no material relations in
the graph).  An independent multi-hop positive control remains pending
acceptance and is not claimed.

Negative battery: source mutation flips dual verification off; a
wrong-scope verifier reports ``proof_dag_context_mismatch``; forged or
cross-path DAG entries and 3E diagnostic records
(``operation-precondition-diagnostic/v1``) are rejected by the consumption
guard and by the receipt's derivation path.

Fixed constraints: 3E diagnostics semantics untouched (zero imports from
the consumption path), no capability token is minted or forwarded by the
integration (minting stays inside the contracts build/verify windows),
no new proof class, no rule or protocol-definition/v1 expansion, no Device
run, no eight-question rerun.  The runner is deterministic: two runs
produce byte-identical replay JSON.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest import mock
import json
import sys

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from chem_agent_contracts.route_proof_dag import (
    StateProofDagVerifier,
    verify_state_proof_dag,
)
from reaserch_agent.route_group_fact_receipt import (
    _literal_fact_reason,
    _state_derivation_proof,
    produce_pdf_group_fact_receipt,
)
from reaserch_agent.route_pdf_group_extraction import (
    propose_pdf_group_unreviewed,
)
from reaserch_agent.route_pdf_group_proposals import (
    associate_pdf_group_proposals,
)
from reaserch_agent.route_pdf_groups import (
    enumerate_attested_pdf_experimental_groups,
)
from reaserch_agent.route_state_proof_dag import (
    STATE_PROOF_DAG_DERIVATION,
    build_verified_state_proof_dags,
    dag_entry_derivation,
)
from reaserch_agent.run_research_agent import load_route_trust_config

out_dir = root / "result" / "operation-structure-20260928"
PROPOSAL_PATH = out_dir / "local-revision-r10-proposal.json"
R11_REPLAY_PATH = out_dir / "local-revision-r11-replay.json"
CAMPAIGN_STATE_PATH = (
    root / "result" / "a01-v5-real-input-20260927" / "campaigns"
    / "A01-v5-quote-context-20260927" / "iteration_00" / "research_state.json"
)
KB_BASE = root / "result" / "a01-v5-real-input-20260927"

NIFE_GROUP_ID = "Synthesis of the Pristine Ni3Fe LDHs (NiFe Control)."
ER_GROUP_ID = ("Synthesis of the LDHs by an Etching-and-Recrystallization "
               "(ER) Method of NiFe; ERy (y = 0−10).")
EP_GROUP_ID = "Electrode Preparation."

G5_OUT = "material_graph[5].material_outputs[0].state"
G6_IN = "material_graph[6].material_inputs[0].state"
G6_OUT = "material_graph[6].material_outputs[0].state"
G7_IN = "material_graph[7].material_inputs[0].state"
G7_OUT = "material_graph[7].material_outputs[0].state"
G8_IN = "material_graph[8].material_inputs[0].state"
G8_OUT = "material_graph[8].material_outputs[0].state"
G9_IN = "material_graph[9].material_inputs[0].state"
G9_OUT = "material_graph[9].material_outputs[0].state"

RETAINED_GAP = "retained_object_mention_precedes_operation"

# Archived r13 outputs (LF bytes) that this wording-correction round pins.
R13_ARCHIVE_REPLAY_SHA256 = (
    "sha256_9084ca5497ccf0b46cdb0b10697953742bdb90814768d19b5ef0f1b30c7f2cb5"
)
R13_ARCHIVE_AUDIT_SHA256 = (
    "sha256_9919a447e7759f977062f8c1e03dc7e3b9e4556eab4951ba3853b957f86969ee"
)

# The accepted r10/r11 verdict table for the nine chain-tail nodes, quoted
# from the committed r11 replay (carried_forward_state_table_3d2).
EXPECTED_NINE = {
    G5_OUT: ("graph[5].out", "PASS", "", "proven"),
    G6_IN: ("graph[6].in", "PASS", "", "proven"),
    G6_OUT: ("graph[6].out", "PASS", "", "proven"),
    G7_IN: ("graph[7].in (ms7a.in)", "PASS", "", "proven"),
    G7_OUT: ("ms7a.out (graph[7].out)", "BLOCKED", RETAINED_GAP,
             "evidence_gap"),
    G8_IN: ("ms7b.in (graph[8].in)", "BLOCKED", RETAINED_GAP,
            "dependency_cascade"),
    G8_OUT: ("ms7b.out (graph[8].out)", "BLOCKED", RETAINED_GAP,
             "dependency_cascade"),
    G9_IN: ("graph[9].in", "BLOCKED", RETAINED_GAP, "dependency_cascade"),
    G9_OUT: ("graph[9].out", "BLOCKED", RETAINED_GAP, "dependency_cascade"),
}

PASS_PATHS = (G5_OUT, G6_IN, G6_OUT, G7_IN)
BLOCKED_PATHS = (G7_OUT, G8_IN, G8_OUT, G9_IN, G9_OUT)
# Paths the receipt accepts ONLY through the multi-hop DAG (flat fails).
DAG_EXCLUSIVE_RECEIPT_PATHS = (G6_OUT, G7_IN)

DIAGNOSTIC_SCHEMA = "operation-precondition-diagnostic/v1"


def _fail(message: str) -> None:
    raise SystemExit(f"r13 acceptance failure: {message}")


def _sha256_bytes(data: bytes) -> str:
    return "sha256_" + sha256(data).hexdigest()


def _sha256_json(payload) -> str:
    return _sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8"))


def _load_inventory():
    trust = load_route_trust_config(
        str(KB_BASE / "route-trust-config.json"), str(KB_BASE / "kb"))
    inventory = enumerate_attested_pdf_experimental_groups(
        KB_BASE / "kb", trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"])
    if inventory.diagnostics:
        _fail(f"inventory diagnostics: {inventory.diagnostics}")
    return inventory


def _group(inventory, group_id: str):
    matches = [
        group for group in inventory.groups
        if group.source_scope.experimental_group_id == group_id
    ]
    if len(matches) != 1:
        _fail(f"expected exactly one group {group_id!r}, got {len(matches)}")
    return matches[0]


def _fixed_invoke(proposal: dict):
    def invoke_json(_prompt: str):
        return {"proposals": [deepcopy(proposal)]}
    return invoke_json


def _run_formal_entry(group, proposal: dict, *, dag_enabled: bool = True):
    """The G1 formal entry with a fixed model response.

    Mirrors workflow ``_propose_attested_route_protocols``: unreviewed
    extraction with required-fact checking, then the fact receipt — on the
    diagnostic branch (associated located proposals) exactly like the
    workflow's scoped work-order path when diagnostics exist.
    """
    extraction = propose_pdf_group_unreviewed(
        [group], _fixed_invoke(proposal), check_required_graph_facts=True,
    )
    artifact = deepcopy(extraction.locator_production)
    if dag_enabled:
        receipt_source = extraction
        if extraction.diagnostics:
            receipt_source = None
            if (artifact.get("status") == "located_unreviewed"
                    and isinstance(artifact.get("located_proposals"), list)):
                receipt_source = associate_pdf_group_proposals(
                    [group], artifact["located_proposals"])
        receipt = (
            produce_pdf_group_fact_receipt(
                [group], receipt_source, signed_inventory_verified=True)
            if receipt_source is not None else None
        )
        return extraction, artifact, receipt
    # Baseline (BEFORE): the same entry with ONLY the DAG map disabled at
    # both consumption points — byte-equivalent to the pre-G1 code path.
    with mock.patch(
        "reaserch_agent.route_pdf_group_extraction."
        "build_verified_state_proof_dags", lambda *args, **kwargs: {},
    ), mock.patch(
        "reaserch_agent.route_group_fact_receipt."
        "build_verified_state_proof_dags", lambda *args, **kwargs: {},
    ):
        extraction = propose_pdf_group_unreviewed(
            [group], _fixed_invoke(proposal), check_required_graph_facts=True,
        )
        artifact = deepcopy(extraction.locator_production)
        receipt_source = extraction
        if extraction.diagnostics:
            receipt_source = None
            if (artifact.get("status") == "located_unreviewed"
                    and isinstance(artifact.get("located_proposals"), list)):
                receipt_source = associate_pdf_group_proposals(
                    [group], artifact["located_proposals"])
        receipt = (
            produce_pdf_group_fact_receipt(
                [group], receipt_source, signed_inventory_verified=True)
            if receipt_source is not None else None
        )
    return extraction, artifact, receipt


def _receipt_field_map(receipt, protocol) -> dict:
    """Per-fact receipt classification: verified/derived_flat/dag_proven/
    blocked(+reason)."""
    group_result = receipt.group_results[0]
    facts = [
        fact for fact in protocol.get("route_facts", [])
        if isinstance(fact, dict)
    ]
    verdicts: dict[str, dict] = {}
    reason_by_index: dict[int, str] = {}
    for reason in group_result.reason_codes:
        prefix, _, code = reason.partition(":")
        if prefix.startswith("fact[") and prefix.endswith("]"):
            reason_by_index[int(prefix[5:-1])] = code
    for index, fact in enumerate(facts):
        path = str(fact.get("field_path") or "")
        if index in reason_by_index:
            verdicts[path] = {
                "verdict": "blocked", "reason": reason_by_index[index],
            }
        elif path in group_result.dag_proven_state_field_paths:
            verdicts[path] = {
                "verdict": "dag_proven", "derivation": STATE_PROOF_DAG_DERIVATION,
            }
        elif path in group_result.derived_state_field_paths:
            verdicts[path] = {"verdict": "derived_flat"}
        elif path in group_result.verified_field_paths:
            verdicts[path] = {"verdict": "verified_literal"}
    return verdicts


def _associated_protocol(artifact, group):
    assoc = associate_pdf_group_proposals([group], artifact["located_proposals"])
    if assoc.diagnostics or not assoc.protocols:
        _fail("association failed inside runner table construction")
    return assoc.protocols[0]


def _dag_key_rows(artifact) -> dict:
    rows = {}
    for row in artifact.get("convention_state_proof_dags", []):
        entry = {
            "field_path": row["field_path"],
            "status": row.get("status"),
            "verdict": row["verdict"],
            "attribution": row["attribution"],
            "build_issue": row["build_issue"],
            "verify_with_span_resolver": row["verify_with_span_resolver"],
            "verify_blocks_only": row["verify_blocks_only"],
            "dag_sha256": (
                _sha256_json(row["dag"]) if row.get("dag") is not None else ""
            ),
            "proof_dag_root_id": (
                row["dag"].get("root_id") if row.get("dag") else ""),
            "proof_dag_node_count": (
                len(row["dag"].get("nodes", {})) if row.get("dag") else 0),
        }
        if row.get("derivation"):
            entry["derivation"] = dict(row["derivation"])
        rows[row["field_path"]] = entry
    return rows


def _state_field_table(artifact, baseline_map, integrated_map) -> list[dict]:
    flat_candidates = {
        row["field_path"] for row in artifact.get("convention_state_candidates", [])
    }
    dag_rows = _dag_key_rows(artifact)
    rows: list[dict] = []
    for field_path in sorted(
            set(baseline_map) | set(integrated_map) | set(dag_rows),
            key=lambda path: (len(path), path)):
        if not field_path.endswith(".state"):
            continue
        rows.append({
            "field_path": field_path,
            "baseline_flat": baseline_map.get(field_path),
            "integrated_dag": integrated_map.get(field_path),
            "extraction_flat_candidate": field_path in flat_candidates,
            "extraction_dag": (
                {key: dag_rows[field_path][key] for key in (
                    "verdict", "attribution", "build_issue",
                    "verify_with_span_resolver", "verify_blocks_only",
                    "dag_sha256")}
                if field_path in dag_rows else None
            ),
        })
    return rows


# ---------------------------------------------------------------------------
# Negative battery (real signed NiFe Control group, r10 proposal).
# ---------------------------------------------------------------------------


def _negative_battery(group, proposal: dict) -> dict:
    graph = proposal["material_graph"]
    facts = proposal["route_facts"]
    ref = proposal["source_group_ref"]
    scope = {
        "paper_id": ref["paper_id"],
        "experimental_group_id": ref["experimental_group_id"],
        "source_digest": ref["source_digest"],
    }
    blocks = [(block.locator, block.text) for block in group.blocks]
    captions = [block.locator for block in group.blocks if block.caption]
    results: dict[str, Any] = {}

    clean = build_verified_state_proof_dags(
        graph, facts, **scope, blocks=blocks, caption_block_locators=captions)
    if clean[G6_OUT]["verdict"] != "PASS":
        _fail("negative battery precondition: graph[6].out must PASS clean")

    # 1. Source mutation: corrupt the signed block text carrying graph[6]'s
    # evidence; verifying the CLEAN DAGs against the mutated source must
    # fail leaf relocation / digest checks (span-resolver and blocks-only
    # modes alike), and the dependent ms7a.in DAG must fail too.
    dag = clean[G6_OUT]["dag"]
    leaf_locators = sorted({
        node["leaf"]["locator"]
        for node in dag["nodes"].values()
        if isinstance(node.get("leaf"), dict) and node["leaf"].get("locator")
    })
    if not leaf_locators:
        _fail("negative battery precondition: relocatable leaves expected")
    first_block = leaf_locators[0].split("-")[0]
    mutated_blocks = [
        (locator, ("MUTATED " + text) if locator.startswith(first_block) else text)
        for locator, text in blocks
    ]
    from chem_agent_contracts.route_retained_object import (
        build_excerpt_span_resolver as _build_span,
    )
    mutated_span = _build_span(mutated_blocks, captions)
    mutation_rows = {}
    for path in (G5_OUT, G6_IN, G6_OUT, G7_IN):
        clean_dag = clean[path]["dag"]
        mutation_rows[path] = {
            "verify_with_span_resolver": verify_state_proof_dag(
                clean_dag, graph, facts, span_of=mutated_span, **scope),
            "verify_blocks_only": StateProofDagVerifier(
                graph, facts, blocks=mutated_blocks,
                caption_block_locators=captions, **scope).verify(clean_dag),
        }
    results["source_mutation"] = {
        "mutated_block_prefix": first_block,
        "leaf_locators_in_g6_out_dag": leaf_locators,
        "rows": mutation_rows,
        "g6_out_flips_to_blocked": all(
            mutation_rows[G6_OUT][mode] != ""
            for mode in ("verify_with_span_resolver", "verify_blocks_only")),
        "g7_in_flips_to_blocked": all(
            mutation_rows[G7_IN][mode] != ""
            for mode in ("verify_with_span_resolver", "verify_blocks_only")),
    }

    # 2. Wrong scope / wrong instance binding at verification.
    wrong_scope_issue = StateProofDagVerifier(
        graph, facts, paper_id=scope["paper_id"],
        experimental_group_id=ER_GROUP_ID,
        source_digest=scope["source_digest"],
        blocks=blocks, caption_block_locators=captions,
    ).verify(dag)
    wrong_digest_issue = StateProofDagVerifier(
        graph, facts, paper_id=scope["paper_id"],
        experimental_group_id=scope["experimental_group_id"],
        source_digest="sha256_" + "0" * 64,
        blocks=blocks, caption_block_locators=captions,
    ).verify(dag)
    results["wrong_scope_or_instance"] = {
        "wrong_experimental_group_id": wrong_scope_issue,
        "wrong_source_digest": wrong_digest_issue,
    }

    # 3. Forged / cross-path consumption entries are rejected by the guard.
    forged = dict(clean[G6_OUT])
    forged["derivation"] = dict(forged["derivation"])
    forged["derivation"]["proof_dag_root_id"] = "proof_node_" + "0" * 24
    cross_path = dict(clean[G6_OUT])
    cross_path["field_path"] = G7_IN
    forged_verdict = dict(clean[G7_OUT])
    forged_verdict["verdict"] = "PASS"
    forged_verdict["verify_with_span_resolver"] = ""
    forged_verdict["verify_blocks_only"] = ""
    forged_verdict["derivation"] = {
        "derivation": STATE_PROOF_DAG_DERIVATION,
        "field_path": G7_OUT,
        "target_state": "dispersion",
        "proof_dag_root_id": "proof_node_" + "1" * 24,
    }
    results["forged_entries"] = {
        "tampered_root_id_rejected": dag_entry_derivation(forged) is None,
        "cross_path_rejected": dag_entry_derivation(cross_path) is None,
        "blocked_with_forged_pass_rejected": (
            dag_entry_derivation(forged_verdict) is None),
    }

    # 4. 3E diagnostic records never enter the consumption path: neither at
    # the guard nor at the receipt's derivation function.
    diagnostic_dict = {
        "schema_version": DIAGNOSTIC_SCHEMA,
        "diagnostics_only": True,
        "feeds_verdict": False,
        "conclusion": "insufficient",
    }
    diagnostic_entry = {
        "field_path": G6_OUT,
        "verdict": "PASS",
        "verify_with_span_resolver": "",
        "verify_blocks_only": "",
        "dag": diagnostic_dict,
        "derivation": {
            "derivation": STATE_PROOF_DAG_DERIVATION,
            "field_path": G6_OUT,
            "target_state": "dispersion",
            "proof_dag_root_id": "proof_node_" + "2" * 24,
        },
    }
    fact_g6_out = next(
        fact for fact in facts if fact["field_path"] == G6_OUT)
    receipt_rejection = _state_derivation_proof(
        fact_g6_out, facts, graph,
        type("Scope", (), scope)(),
        dag_proofs={G6_OUT: diagnostic_entry},
    )
    results["diagnostic_record_rejection"] = {
        "record_to_dict_form_rejected": (
            dag_entry_derivation(diagnostic_entry) is None),
        "non_mapping_record_rejected": (
            dag_entry_derivation({"field_path": G6_OUT, "dag": object(),
                                  "verdict": "PASS"}) is None),
        "receipt_derivation_rejects": receipt_rejection is None,
    }
    return results


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def _run_once() -> dict:
    inventory = _load_inventory()
    nife = _group(inventory, NIFE_GROUP_ID)

    envelope = json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))
    proposal = deepcopy(envelope["proposals"][0])

    extraction, artifact, receipt = _run_formal_entry(nife, proposal)
    if receipt is None:
        _fail("formal entry produced no receipt for the NiFe Control group")
    protocol = _associated_protocol(artifact, nife)

    # Baseline (BEFORE): the same formal entry with ONLY the DAG map
    # disabled — byte-equivalent to the pre-G1 code path.
    _extraction_b, artifact_baseline, receipt_baseline = _run_formal_entry(
        nife, proposal, dag_enabled=False)
    if receipt_baseline is None:
        _fail("baseline formal entry produced no receipt")

    # Extraction flat parity: disabling the DAG key changes nothing else.
    artifact_delta_keys = {
        key for key in set(artifact) | set(artifact_baseline)
        if artifact.get(key) != artifact_baseline.get(key)
    }
    if artifact_delta_keys != {"convention_state_proof_dags"}:
        _fail(f"extraction artifact drift beyond the DAG key: "
              f"{sorted(artifact_delta_keys)}")
    flat_parity = {
        "only_changed_key": "convention_state_proof_dags",
        "flat_candidates_identical": (
            artifact.get("convention_state_candidates")
            == artifact_baseline.get("convention_state_candidates")),
        "dag_key_absent_or_empty_in_baseline": (
            not artifact_baseline.get("convention_state_proof_dags")),
    }

    baseline_map = _receipt_field_map(receipt_baseline, protocol)
    integrated_map = _receipt_field_map(receipt, protocol)
    table = _state_field_table(artifact, baseline_map, integrated_map)

    # --- Acceptance 1: the nine-node verdict table through the FORMAL entry.
    dag_rows = _dag_key_rows(artifact)
    nine_rows = []
    for path, (label, verdict, issue, attribution) in EXPECTED_NINE.items():
        row = dag_rows.get(path)
        if row is None:
            _fail(f"extraction DAG key missing {label} ({path})")
        ok = (row["verdict"] == verdict and row["attribution"] == attribution
              and (row["build_issue"] if verdict == "BLOCKED"
                   else row["verify_with_span_resolver"]
                   + row["verify_blocks_only"]) == issue)
        if not ok:
            _fail(f"{label}: expected {(verdict, issue, attribution)}, got "
              f"{(row['verdict'], row['build_issue'], row['attribution'])}")
        nine_rows.append({
            "node": label, "field_path": path, "verdict": row["verdict"],
            "issue": row["build_issue"], "attribution": row["attribution"],
            "verify_with_span_resolver": row["verify_with_span_resolver"],
            "verify_blocks_only": row["verify_blocks_only"],
            "dag_sha256": row["dag_sha256"],
            "matches_r10_r11_table": True,
        })

    # --- Acceptance 2: receipt recognition of the DAG proofs.
    group_result = receipt.group_results[0]
    dag_proven = set(group_result.dag_proven_state_field_paths)
    derived_flat = set(group_result.derived_state_field_paths)
    for path in DAG_EXCLUSIVE_RECEIPT_PATHS:
        if path not in dag_proven:
            _fail(f"receipt did not DAG-accept multi-hop-proven {path}")
    for path in (G5_OUT, G6_IN):
        if path not in derived_flat | dag_proven:
            _fail(f"receipt lost the proven verdict for {path}")
    for path in BLOCKED_PATHS:
        if path in dag_proven or path in derived_flat:
            _fail(f"receipt must not derive-accept blocked {path}")
        if baseline_map.get(path, {}).get("verdict") != "blocked":
            _fail(f"baseline receipt unexpectedly accepted {path}")
        if integrated_map.get(path, {}) != baseline_map.get(path, {}):
            _fail(f"blocked verdict drifted for {path}")
    # Flat parity at the receipt: the ONLY deltas are the two DAG-exclusive
    # multi-hop acceptances (flat and the literal gates both failed there
    # pre-G1); literal-verified and flat-derived facts do not move.
    deltas = {
        path for path in set(baseline_map) | set(integrated_map)
        if baseline_map.get(path) != integrated_map.get(path)
    }
    if deltas != set(DAG_EXCLUSIVE_RECEIPT_PATHS):
        _fail(f"receipt deltas must be exactly the DAG-exclusive multi-hop "
              f"paths {sorted(DAG_EXCLUSIVE_RECEIPT_PATHS)}, got "
              f"{sorted(deltas)}")
    for path in deltas:
        if (integrated_map.get(path, {}).get("verdict") != "dag_proven"
                or baseline_map.get(path, {}).get("verdict") != "blocked"):
            _fail(f"unexpected verdict transition for {path}: "
                  f"{baseline_map.get(path)} -> {integrated_map.get(path)}")
    baseline_dag_only = {
        path: baseline_map[path] for path in sorted(deltas)
    }

    return {
        "inventory": inventory,
        "nife": {
            "group": nife,
            "artifact": artifact,
            "receipt": receipt,
            "receipt_baseline": receipt_baseline,
            "nine_rows": nine_rows,
            "table": table,
            "flat_parity": flat_parity,
            "receipt_deltas": {
                "dag_accepted_paths": sorted(deltas),
                "baseline_verdicts": baseline_dag_only,
            },
            "extraction_diagnostics": [
                vars(item) for item in extraction.diagnostics
            ],
        },
    }


def _run_controls(inventory) -> dict:
    """Independent control groups through the SAME formal entry."""
    state = json.loads(CAMPAIGN_STATE_PATH.read_text(encoding="utf-8"))
    proposals = state["raw_llm_outputs"]["route_pdf_group_propose"]["proposals"]

    controls: dict[str, Any] = {}

    # Preferred control: the ER group.
    er = _group(inventory, ER_GROUP_ID)
    er_proposal = next(
        item for item in proposals
        if item.get("source_group_ref", {}).get("experimental_group_id")
        == ER_GROUP_ID)
    er_extraction, er_artifact, er_receipt = _run_formal_entry(er, er_proposal)
    er_diagnostics = [vars(item) for item in er_extraction.diagnostics]
    controls["er_group"] = {
        "experimental_group_id": ER_GROUP_ID,
        "proposal_source": ("raw_llm_outputs.route_pdf_group_propose."
                            "proposals[3] of A01-v5-quote-context-20260927 "
                            "iteration_00 research_state.json"),
        "graph_steps": len(er_proposal["material_graph"]),
        "route_facts": len(er_proposal["route_facts"]),
        "state_facts": len([
            fact for fact in er_proposal["route_facts"]
            if str(fact.get("field_path", "")).endswith(".state")
        ]),
        "formal_entry_diagnostic_reasons": sorted({
            item["reason_code"] for item in er_diagnostics
        }),
        "artifact_status": (
            er_artifact.get("status")
            or er_artifact.get("structured_proposals_status")
        ),
        "coverage_gap": (
            "the ER work-state proposal never reaches locator production: "
            "the operation-coverage audit reports "
            "source_operation_unrepresented, the blocked branch carries no "
            "convention_state_proof_dags key by design (no partial batch "
            "advances), and the graph carries no material relations, so no "
            "state field is multi-hop-provable; repairing coverage means "
            "authoring new graph steps, which is not a field-level fix"
        ),
        "dag_key_rows": len(er_artifact.get("convention_state_proof_dags", [])),
    }

    # Walkable control: Electrode Preparation.
    ep = _group(inventory, EP_GROUP_ID)
    ep_proposal = next(
        item for item in proposals
        if item.get("source_group_ref", {}).get("experimental_group_id")
        == EP_GROUP_ID)
    ep_extraction, ep_artifact, ep_receipt = _run_formal_entry(ep, ep_proposal)
    ep_rows = _dag_key_rows(ep_artifact)
    ep_control: dict[str, Any] = {
        "experimental_group_id": EP_GROUP_ID,
        "proposal_source": ("raw_llm_outputs.route_pdf_group_propose."
                            "proposals[*] of A01-v5-quote-context-20260927 "
                            "iteration_00 research_state.json"),
        "graph_steps": len(ep_proposal["material_graph"]),
        "route_facts": len(ep_proposal["route_facts"]),
        "artifact_status": ep_artifact.get("status"),
        "dag_key_rows": len(ep_rows),
        "dag_pass_paths": sorted(
            path for path, row in ep_rows.items() if row["verdict"] == "PASS"),
    }
    if ep_receipt is not None:
        ep_result = ep_receipt.group_results[0]
        ep_protocol = _associated_protocol(ep_artifact, ep)
        _e, _a, ep_receipt_baseline = _run_formal_entry(
            ep, ep_proposal, dag_enabled=False)
        baseline_map = _receipt_field_map(ep_receipt_baseline, ep_protocol)
        integrated_map = _receipt_field_map(ep_receipt, ep_protocol)
        deltas = sorted(
            path for path in set(baseline_map) | set(integrated_map)
            if baseline_map.get(path) != integrated_map.get(path))
        ep_control["receipt"] = {
            "group_status": ep_result.status,
            "dag_proven_state_field_paths": list(
                ep_result.dag_proven_state_field_paths),
            "derived_state_field_paths": list(
                ep_result.derived_state_field_paths),
            "dag_pass_path_verdicts": {
                path: {"baseline": baseline_map.get(path),
                       "integrated": integrated_map.get(path)}
                for path in ep_control["dag_pass_paths"]
            },
            "baseline_vs_integrated_deltas": {
                path: {"baseline": baseline_map.get(path),
                       "integrated": integrated_map.get(path)}
                for path in deltas
            },
        }
    controls["electrode_preparation_group"] = ep_control
    return controls


def _build_replay_text() -> str:
    run = _run_once()
    inventory = run["inventory"]
    nife = run["nife"]
    group = nife["group"]

    # Negative battery on the real signed group + unchanged r10 proposal.
    proposal = deepcopy(json.loads(PROPOSAL_PATH.read_text(
        encoding="utf-8"))["proposals"][0])
    negatives = _negative_battery(group, proposal)
    if not negatives["source_mutation"]["g6_out_flips_to_blocked"]:
        _fail("source mutation must flip graph[6].out to BLOCKED")
    if not negatives["source_mutation"]["g7_in_flips_to_blocked"]:
        _fail("source mutation must cascade to ms7a.in")
    if negatives["wrong_scope_or_instance"]["wrong_experimental_group_id"] != (
            "proof_dag_context_mismatch"):
        _fail("wrong-scope verification must report context mismatch")
    if negatives["wrong_scope_or_instance"]["wrong_source_digest"] != (
            "proof_dag_context_mismatch"):
        _fail("wrong-digest verification must report context mismatch")
    if not all(negatives["forged_entries"].values()):
        _fail("forged consumption entries must be rejected")
    if not all(negatives["diagnostic_record_rejection"].values()):
        _fail("3E diagnostic records must be rejected by the consumption path")

    controls = _run_controls(inventory)

    # Fixed-constraints audit: 3E module untouched, no token minting by the
    # integration, v1 schemas unextended, ms7a.out BLOCKED.
    import reaserch_agent.route_operation_precondition_diagnostic as diag_mod
    diag_source = Path(diag_mod.__file__).read_text(encoding="utf-8")
    token_free = (
        "_VerifiedParentStateEvidence" not in diag_source
        and "_VerifiedLiquidMedium" not in diag_source
        and "route_state_proof_dag" not in diag_source
        and "route_proof_dag" not in diag_source
    )
    adapter_source = (
        root / "reaserch_agent" / "route_state_proof_dag.py"
    ).read_text(encoding="utf-8")
    adapter_mint_free = (
        "_mint_verified" not in adapter_source
        and "_VerifiedParentStateEvidence" not in adapter_source
        and "_VerifiedLiquidMedium" not in adapter_source
    )
    fixed_constraints = {
        "diagnostics_module_zero_proof_dependency": token_free,
        "adapter_mints_no_capability_token": adapter_mint_free,
        "no_new_proof_class": (
            "the only proof schema consumed is state-proof-dag/v1; the "
            "receipt marker is a consumption label, not a schema"),
        "protocol_definition_v1_untouched": True,
        "ms7a_out_blocked_issue": RETAINED_GAP,
        "device_not_run": True,
        "eight_question_rerun": False,
    }

    group_scope = group.source_scope
    replay = {
        "schema_version": "bounded_local_revision/v13",
        "round": "G1: state-proof-dag/v1 consumption by the formal "
                 "extraction/receipt chain",
        "model_generated": False,
        "group_identity": {
            "paper_id": group_scope.paper_id,
            "experimental_group_id": group_scope.experimental_group_id,
            "source_digest": group_scope.source_digest,
            "block_count": len(group.blocks),
            "caption_count": sum(1 for block in group.blocks if block.caption),
        },
        "proposal_reuse": {
            "path": "result/operation-structure-20260928/"
                    "local-revision-r10-proposal.json",
            "modified": False,
            "sha256": _sha256_bytes(PROPOSAL_PATH.read_bytes()),
            "injected_via": ("propose_pdf_group_unreviewed fixed invoke_json "
                             "(check_required_graph_facts=True, bounded local "
                             "revision retained)"),
        },
        "baseline_flat_vs_integrated_dag": {
            "per_state_field_table": nife["table"],
            "extraction_flat_parity": nife["flat_parity"],
            "receipt_dag_acceptances": nife["receipt_deltas"],
            "nine_node_table": nife["nine_rows"],
        },
        "receipt_group_result_integrated": {
            "status": nife["receipt"].group_results[0].status,
            "dag_proven_state_field_paths": list(
                nife["receipt"].group_results[0].dag_proven_state_field_paths),
            "derived_state_field_paths": list(
                nife["receipt"].group_results[0].derived_state_field_paths),
            "reason_codes": list(nife["receipt"].group_results[0].reason_codes),
        },
        "receipt_group_result_baseline": {
            "status": nife["receipt_baseline"].group_results[0].status,
            "derived_state_field_paths": list(
                nife["receipt_baseline"].group_results[0]
                .derived_state_field_paths),
            "reason_codes": list(
                nife["receipt_baseline"].group_results[0].reason_codes),
        },
        "negative_battery": negatives,
        "independent_controls": controls,
        "fixed_constraints": fixed_constraints,
        "terminology": (
            "this round is G1 consumption of multi-hop proofs: extraction "
            "records dual-verified state-proof DAGs as unreviewed candidates "
            "and the receipt accepts dual-verified DAG derivations after both "
            "flat derivations fail; nothing here is an A01 PASS, a new model "
            "generation pass, or a V2 publication gate"),
    }

    audit = [
        {"kind": "g1_integration_points",
         "adapter_module": "reaserch_agent/route_state_proof_dag.py",
         "consumption_points": [
             {"module": "reaserch_agent/route_pdf_group_extraction.py",
              "function": "_prepare_unsigned_proposal",
              "change": ("parallel key convention_state_proof_dags next to "
                         "the untouched flat convention_state_candidates; "
                         "carried into locator_production on the success/"
                         "locator path only")},
             {"module": "reaserch_agent/route_group_fact_receipt.py",
              "function": "_state_derivation_proof",
              "change": ("after BOTH flat derivations fail, consult the "
                         "per-group dual-verified DAG map (built from THIS "
                         "receipt's current signed blocks); accepted records "
                         "are marked derivation=state_proof_dag_v1 and "
                         "classified into the new PdfGroupLiteralStatusV1."
                         "dag_proven_state_field_paths (default () keeps the "
                         "frozen dataclass compatible)")},
         ]},
        {"kind": "trust_boundary",
         "rules": [
             "the receipt never reads the extraction artifact's DAGs; it "
             "rebuilds and dual-verifies from its own current signed blocks",
             "no DAG is verified after being handed to mutating code",
             "no capability token is minted, forwarded, or stored by the "
             "integration; minting stays inside the contracts build/verify "
             "windows (3D closure)",
             "3E diagnostic records are rejected by the consumption guard "
             "and by the receipt derivation path (type/schema guards)",
             "BLOCKED attribution is mechanical (evidence_gap when every "
             "direct upstream state dependency proves, dependency_cascade "
             "otherwise) and reproduces the committed r10/r11 table",
         ]},
        {"kind": "r10_r11_verdict_table_carryover",
         "source": ("result/operation-structure-20260928/"
                    "local-revision-r11-replay.json "
                    "round3d_acceptance.carried_forward_state_table_3d2"),
         "source_sha256": _sha256_bytes(R11_REPLAY_PATH.read_bytes()),
         "nine_node_rows": nife["nine_rows"]},
        {"kind": "deliberately_not_done",
         "items": [
             "compiler/science/source/V2 boundaries: NOT wired to the DAG "
             "in this round (G1 stops at extraction + receipt)",
             "admitted_protocols stays 0: G1 admission gating remains a "
             "separate milestone",
             "3E operation-precondition inference stays diagnostics-only "
             "(feeds_verdict=false); ms7a.out stays BLOCKED",
             "no new proof class, no rule additions, protocol-definition/v1 "
             "untouched, no Device run, no eight-question rerun",
             "the extraction DAG key is an unreviewed audit candidate "
             "(status unreviewed_prerequisites_only); it never signs or "
             "approves anything",
         ]},
    ]

    replay["audit_rows"] = len(audit)
    replay_text = json.dumps(replay, ensure_ascii=False, indent=2,
                             default=str)
    audit_text = json.dumps({
        "schema_version": "bounded_local_revision/v13",
        "model_generated": False,
        "scope": ("G1: the accepted state-proof-dag/v1 gains its two formal "
                  "consumption points (extraction candidates + fact "
                  "receipt); the r10/r11 verdict table is reproduced through "
                  "the formal entry; ms7a.out stays BLOCKED on the "
                  "retained-object evidence gap; 3E stays diagnostics-only"),
        "audit": audit,
    }, ensure_ascii=False, indent=2, default=str)
    summary = {
        "nine_node_table": nife["nine_rows"],
        "receipt_dag_accepted": nife["receipt_deltas"]["dag_accepted_paths"],
        "flat_parity": nife["flat_parity"],
        "negatives_ok": True,
        "controls": {
            "er_status": controls["er_group"]["artifact_status"],
            "ep_dag_proven": controls["electrode_preparation_group"].get(
                "receipt", {}).get("dag_proven_state_field_paths"),
        },
    }
    return replay_text, audit_text, summary


def main() -> None:
    # Determinism: two full runs must produce byte-identical artifacts.
    replay_text, audit_text, summary = _build_replay_text()
    replay_text_2, audit_text_2, _summary_2 = _build_replay_text()
    if replay_text != replay_text_2 or audit_text != audit_text_2:
        _fail("double run is not byte-identical")
    summary["double_run_byte_identical"] = True
    summary["replay_sha256"] = _sha256_bytes(replay_text.encode("utf-8"))

    # r13b wording-correction pin: the regenerated replay/audit must be
    # byte-identical (LF bytes) to the archived r13 outputs — the correction
    # is prose-only and the G1 follow-up fixes must not drift these verdicts.
    replay_sha = _sha256_bytes(replay_text.encode("utf-8"))
    audit_sha = _sha256_bytes(audit_text.encode("utf-8"))
    if replay_sha != R13_ARCHIVE_REPLAY_SHA256:
        _fail(f"r13b replay drifted from the r13 archive: {replay_sha}")
    if audit_sha != R13_ARCHIVE_AUDIT_SHA256:
        _fail(f"r13b audit drifted from the r13 archive: {audit_sha}")
    summary["wording_correction_only"] = True
    summary["replay_matches_r13_archive"] = True
    summary["audit_matches_r13_archive"] = True

    replay_path = out_dir / "local-revision-r13b-replay.json"
    audit_path = out_dir / "local-revision-r13b-audit.json"
    replay_path.write_text(replay_text, encoding="utf-8")
    audit_path.write_text(audit_text, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
