"""R2 tests: canonical material relation/event graph unifying the three
views (batch_plan / material_ledger / material_transitions), split/merge
conservation, and batch-level unknown alignment.

All tests are deterministic: the LLM model raises and no remote is called.
"""

from __future__ import annotations

import copy
import os

os.environ.setdefault("CHEM_DEVICE_CONTRACT_AUDIT", "off")
os.environ.setdefault("CHEM_DEVICE_WORKFLOW_VERIFICATION", "deterministic")
os.environ.setdefault("CHEM_DEVICE_LLM_SEMANTIC_ANALYSIS", "off")
os.environ.setdefault("CHEM_DEVICE_ALLOW_LEGACY_SEMANTICS_FOR_TESTS", "1")

from single_agent import SingleDeviceAgent
from test_single_agent import FakeWorkstationLoader, RaisingModel


def _agent() -> SingleDeviceAgent:
    return SingleDeviceAgent(
        model=RaisingModel(), workstation_loader=FakeWorkstationLoader()
    )


def _plan():
    """Two-step conserved transition: batch_000 (provenanced root, 10 mL)
    --mt_001--> batch_001 (child, 8 mL)."""
    return {
        "status": "device_plan",
        "device_plan": [
            {
                "plan_step": 1,
                "station_code": "X",
                "workstation": "w",
                "objective": "o",
                "operation_intent": "i",
                "key_values": {},
                "containers": {},
                "source_macro_step": 1,
                "material_event_kind": "state_change",
                "material_transition_ids": ["mt_001"],
            },
            {
                "plan_step": 2,
                "station_code": "X",
                "workstation": "w",
                "objective": "o2",
                "operation_intent": "i2",
                "key_values": {},
                "containers": {},
                "source_macro_step": 1,
                "material_event_kind": "state_change",
                "material_transition_ids": ["mt_001"],
            },
        ],
        "quantity_adjustments": [],
        "batch_plan": [
            {
                "batch_id": "batch_000",
                "quantity_mode": "numeric_inventory",
                "material_id": "M1",
                "is_root_batch": True,
                "total_quantity": {"value": 10, "unit": "mL"},
                "source_kind": "research_explicit",
                "source_refs": ["macro_step:1"],
                "calculation": "research states 10 mL",
                "research_source_refs": [
                    {
                        "source_path": "macro_action_steps[0].参数",
                        "source_macro_step": 1,
                        "source_field": "参数",
                        "source_context": "总量 10 mL",
                    }
                ],
                "sample_id": "S1",
                "consumer_ids": ["material_transition:mt_001"],
                "allocation": {
                    "material_transition:mt_001": {"value": 8, "unit": "mL"}
                },
            },
            {
                "batch_id": "batch_001",
                "quantity_mode": "numeric_inventory",
                "material_id": "M2",
                "is_root_batch": False,
                "transition_kind": "state_change",
                "total_quantity": {"value": 8, "unit": "mL"},
                "source_plan_steps": [1, 2],
                "source_macro_steps": [1],
                "sample_id": "S1",
                "consumer_ids": [],
                "source_kind": "derived_from_parent",
                "source_refs": ["material_transition:mt_001"],
                "calculation": "child of mt_001",
            },
        ],
        "material_transitions": [
            {
                "transition_id": "mt_001",
                "transition_kind": "process_same_material",
                "quantity_basis": "conserved_inventory",
                "parent_batch_ids": ["batch_000"],
                "child_batch_ids": ["batch_001"],
                "source_plan_steps": [1, 2],
                "source_macro_steps": [1],
                "input_allocations": [
                    {"batch_id": "batch_000", "quantity": {"value": 8, "unit": "mL"}}
                ],
                "output_allocations": [
                    {"batch_id": "batch_001", "quantity": {"value": 8, "unit": "mL"}}
                ],
                "before_material_state": "liquid",
                "after_material_state": "liquid",
                "calculation_or_basis": "conserved transfer",
            },
        ],
        "material_ledger": {"entries": []},
    }


def _audit(plan):
    return _agent()._normalize_quantity_contract(copy.deepcopy(plan))


def _codes(audit):
    return {issue["code"] for issue in audit["issues"]}


def _entries(out, batch_id):
    return [
        entry
        for entry in out["material_ledger"]["entries"]
        if entry.get("batch_id") == batch_id
    ]


def test_parent_lineage_derived_from_transition_graph():
    plan = _plan()
    plan["batch_plan"][1].pop("parent_batch_ids", None)
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "ml_child",
            "batch_id": "batch_001",
            "material_id": "M2",
            "sample_id": "S1",
            "produced": {"value": 8, "unit": "mL"},
            "source_kind": "derived_from_parent",
            "source_refs": ["material_transition:mt_001"],
            "calculation": "produced by mt_001",
        }
    ]
    out = _audit(plan)
    child = next(b for b in out["batch_plan"] if b["batch_id"] == "batch_001")
    assert child["parent_batch_ids"] == ["batch_000"]
    event_codes = [e["code"] for e in out["quantity_audit"]["normalization_events"]]
    assert "batch_parent_lineage_derived_from_relation_graph" in event_codes
    assert "material_transition_parent_mismatch" not in _codes(out["quantity_audit"])
    assert "missing_parent_batch_lineage" not in _codes(out["quantity_audit"])


def test_three_views_derive_from_single_graph_change():
    plan = _plan()
    # One fact changes in the graph (transition edge); the ledger view must
    # follow it, and batch/ledger/transition stay mutually consistent.
    plan["material_transitions"][0]["input_allocations"][0]["quantity"] = {
        "value": 6,
        "unit": "mL",
    }
    plan["material_transitions"][0]["output_allocations"][0]["quantity"] = {
        "value": 6,
        "unit": "mL",
    }
    plan["batch_plan"][1]["total_quantity"] = {"value": 6, "unit": "mL"}
    plan["batch_plan"][0]["allocation"] = {
        "material_transition:mt_001": {"value": 6, "unit": "mL"}
    }
    out = _audit(plan)
    audit = out["quantity_audit"]
    assert "transition_input_ledger_draw_missing_or_ambiguous" not in _codes(audit)
    assert "transition_output_ledger_source_missing_or_ambiguous" not in _codes(audit)
    assert "batch_consumer_allocation_missing_from_ledger" not in _codes(audit)
    draw = next(
        e for e in _entries(out, "batch_000")
        if e.get("consumer_id") == "material_transition:mt_001"
    )
    assert draw["consumed"] == {"value": 6, "unit": "mL"}
    production = next(
        e for e in _entries(out, "batch_001") if e.get("produced") is not None
    )
    assert production["produced"] == {"value": 6, "unit": "mL"}


def test_cross_view_drift_still_flagged():
    plan = _plan()
    # Ledger asserts a draw with full provenance but a different quantity
    # than the graph edge: drift must be reported, not repaired.
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "ml_draw",
            "batch_id": "batch_000",
            "material_id": "M1",
            "sample_id": "S1",
            "consumer_id": "material_transition:mt_001",
            "consumed": {"value": 7, "unit": "mL"},
            "consumers": [
                {
                    "consumer_id": "material_transition:mt_001",
                    "quantity": {"value": 7, "unit": "mL"},
                }
            ],
            "source_kind": "research_explicit",
            "source_refs": ["macro_step:1", "material_transition:mt_001"],
            "calculation": "declared draw of 7 mL",
        }
    ]
    out = _audit(plan)
    assert "transition_input_ledger_draw_mismatch" in _codes(out["quantity_audit"])


def test_draw_row_restored_after_provenance_strip():
    plan = _plan()
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "ml_stripped_draw",
            "batch_id": "batch_000",
            "material_id": "M1",
            "sample_id": "S1",
            "consumer_id": "material_transition:mt_001",
            "consumed": {"value": 8, "unit": "mL"},
        }
    ]
    out = _audit(plan)
    entry = next(e for e in _entries(out, "batch_000") if e["entry_id"] == "ml_stripped_draw")
    assert entry["quantity_status"] == "known"
    assert entry["consumed"] == {"value": 8, "unit": "mL"}
    assert "material_transition:mt_001" in entry["source_refs"]
    assert "ledger_draw_derived_from_relation_graph" in [
        e["code"] for e in out["quantity_audit"]["normalization_events"]
    ]


def test_production_row_derived_with_stable_event_id():
    plan = _plan()
    plan["material_ledger"]["entries"] = []
    out = _audit(plan)
    production = next(
        e for e in _entries(out, "batch_001") if e.get("produced") is not None
    )
    assert production["production_event_id"] == "MLPE_mt_001_batch_001"
    assert production["quantity_status"] == "known"
    # Re-audit keeps exactly one derived production row (idempotent).
    second = _audit(out)
    productions = [
        e
        for e in _entries(second, "batch_001")
        if e.get("produced") is not None
    ]
    assert len(productions) == 1
    assert productions[0]["entry_id"] == production["entry_id"]


def test_merge_conservation_violation_is_reported():
    plan = _plan()
    plan["material_transitions"][0]["transition_kind"] = "state_change"
    plan["material_transitions"][0]["output_allocations"][0]["quantity"] = {
        "value": 9,
        "unit": "mL",
    }
    plan["batch_plan"][1]["total_quantity"] = {"value": 9, "unit": "mL"}
    out = _audit(plan)
    violations = [
        issue
        for issue in out["quantity_audit"]["issues"]
        if issue["code"] == "material_relation_conservation_violation"
    ]
    assert len(violations) == 1
    assert violations[0]["input_total"] == 8.0
    assert violations[0]["output_total"] == 9.0


def test_lossy_merge_conservation_passes():
    plan = _plan()
    plan["material_transitions"][0]["output_allocations"][0]["quantity"] = {
        "value": 5,
        "unit": "mL",
    }
    plan["batch_plan"][1]["total_quantity"] = {"value": 5, "unit": "mL"}
    out = _audit(plan)
    assert "material_relation_conservation_violation" not in _codes(
        out["quantity_audit"]
    )


def test_unknown_parent_batch_keeps_no_fake_numeric_rules():
    plan = _plan()
    child = plan["batch_plan"][1]
    for field in ("source_kind", "source_refs", "calculation"):
        child.pop(field)
    child["per_batch_quantity"] = {"value": 3, "unit": "mL"}
    child["multiplicity"] = 9  # would violate total != per_batch * multiplicity
    out = _audit(plan)
    audited_child = next(b for b in out["batch_plan"] if b["batch_id"] == "batch_001")
    assert audited_child["quantity_status"] == "unknown"
    assert audited_child["declared_total_quantity"] == {"value": 8, "unit": "mL"}
    assert "total_quantity" not in audited_child
    codes = _codes(out["quantity_audit"])
    assert not any(code.startswith("missing_batch_quantity") for code in codes)
    assert "batch_quantity_mismatch" not in codes
    assert "missing_transition_batch_total_quantity" not in codes


def test_role_aware_processing_step_refs():
    plan = _plan()
    plan["batch_plan"][1]["parent_batch_ids"] = ["batch_000"]
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "ml_production",
            "batch_id": "batch_001",
            "material_id": "M2",
            "sample_id": "S1",
            "processing_step_refs": [1, 2],
            "produced": {"value": 8, "unit": "mL"},
            "source_kind": "derived_from_parent",
            "source_refs": ["material_transition:mt_001"],
            "calculation": "produced by mt_001",
        },
        {
            "entry_id": "ml_draw",
            "batch_id": "batch_000",
            "material_id": "M1",
            "sample_id": "S1",
            "processing_step_refs": [1, 2],
            "consumer_id": "material_transition:mt_001",
            "consumed": {"value": 8, "unit": "mL"},
            "source_kind": "research_explicit",
            "source_refs": ["macro_step:1"],
            "calculation": "draw for mt_001",
        },
    ]
    out = _audit(plan)
    assert "ledger_processing_step_refs_mismatch" not in _codes(out["quantity_audit"])

    drifted = _plan()
    drifted["material_ledger"]["entries"] = [
        {
            "entry_id": "ml_bad_refs",
            "batch_id": "batch_001",
            "material_id": "M2",
            "sample_id": "S1",
            "processing_step_refs": [99],
            "produced": {"value": 8, "unit": "mL"},
            "source_kind": "derived_from_parent",
            "source_refs": ["material_transition:mt_001"],
            "calculation": "produced by mt_001",
        }
    ]
    out2 = _audit(drifted)
    assert "ledger_processing_step_refs_mismatch" in _codes(out2["quantity_audit"])


def test_missing_processing_step_refs_derived_from_graph():
    plan = _plan()
    plan["batch_plan"][1]["parent_batch_ids"] = ["batch_000"]
    plan["material_ledger"]["entries"] = [
        {
            "entry_id": "ml_no_refs",
            "batch_id": "batch_001",
            "material_id": "M2",
            "sample_id": "S1",
            "produced": {"value": 8, "unit": "mL"},
            "source_kind": "derived_from_parent",
            "source_refs": ["material_transition:mt_001"],
            "calculation": "produced by mt_001",
        }
    ]
    out = _audit(plan)
    entry = next(e for e in _entries(out, "batch_001") if e["entry_id"] == "ml_no_refs")
    assert entry["processing_step_refs"] == [1, 2]
    assert "missing_ledger_processing_step_refs" not in _codes(out["quantity_audit"])


def test_split_equality_rule_is_untouched():
    plan = _plan()
    plan["material_transitions"][0]["transition_kind"] = "split_same_material"
    plan["material_ledger"]["entries"] = []
    out = _audit(plan)
    assert "invalid_conserved_transition_quantity_basis" not in _codes(
        out["quantity_audit"]
    )
    codes = _codes(out["quantity_audit"])
    assert "conserved_transition_quantity_mismatch" not in codes

    bad_split = _plan()
    bad_split["material_transitions"][0]["transition_kind"] = "split_same_material"
    bad_split["material_transitions"][0]["output_allocations"][0]["quantity"] = {
        "value": 12,
        "unit": "mL",
    }
    bad_split["batch_plan"][1]["total_quantity"] = {"value": 12, "unit": "mL"}
    out2 = _audit(bad_split)
    assert "conserved_transition_quantity_mismatch" in _codes(out2["quantity_audit"])


def test_no_double_spend_when_transition_unbound():
    plan = _plan()
    # batch binds its quantity to a different consumer; the transition edge
    # must NOT be backfilled into the ledger (double spend stays visible).
    plan["batch_plan"][0]["consumer_ids"] = ["XPS"]
    plan["batch_plan"][0]["allocation"] = {"XPS": {"value": 8, "unit": "mL"}}
    out = _audit(plan)
    assert "transition_input_ledger_draw_missing_or_ambiguous" in _codes(
        out["quantity_audit"]
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    raise SystemExit(1 if failures else 0)
