"""R1 ledger semantics tests: unknown != 0, forced provenance, placeholder
sanitation, production dedup, and audit gating by quantity_status.

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


def _plan(entries, batches=None, transitions=None):
    if batches is None:
        batches = [
            {
                "batch_id": "batch_000",
                "quantity_mode": "numeric_inventory",
                "material_id": "M1",
                "is_root_batch": True,
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
            }
        ]
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
                "material_event_kind": "none",
            }
        ],
        "quantity_adjustments": [],
        "batch_plan": batches,
        "material_transitions": transitions or [],
        "material_ledger": {"entries": entries},
    }


def _audit(entries, batches=None, transitions=None):
    normalized = _agent()._normalize_quantity_contract(
        _plan(entries, batches=batches, transitions=transitions)
    )
    return normalized["quantity_audit"], normalized["material_ledger"]


def _codes(audit):
    return {issue["code"] for issue in audit["issues"]}


def _entry(**overrides):
    base = {
        "entry_id": "ml_001",
        "material_id": "M1",
        "batch_id": "batch_000",
        "sample_id": "S1",
    }
    base.update(overrides)
    return base


def test_unknown_zero_flow_is_omitted_and_marked_unknown():
    audit, ledger = _audit(
        [
            _entry(
                produced={"value": 0, "unit": "mL"},
                consumed={"value": 0, "unit": "mL"},
                reserved={"value": 0, "unit": "mL"},
                balance={"value": 0, "unit": "mL"},
            )
        ]
    )
    (entry,) = ledger["entries"]
    assert entry["quantity_status"] == "unknown"
    for field in ("produced", "consumed", "reserved", "balance"):
        assert field not in entry
    assert "nonpositive_ledger_flow_quantity" not in _codes(audit)
    assert "material_quantity_insufficient" not in _codes(audit)


def test_no_flow_event_omits_zero_flow_fields_on_known_entry():
    audit, ledger = _audit(
        [
            _entry(
                produced={"value": 10, "unit": "mL"},
                consumed={"value": 0, "unit": "mL"},
                source_kind="research_explicit",
                source_refs=["macro_step:1"],
                calculation="produced 10 mL, nothing consumed yet",
            )
        ]
    )
    (entry,) = ledger["entries"]
    assert entry["quantity_status"] == "known"
    assert entry["produced"] == {"value": 10, "unit": "mL"}
    assert "consumed" not in entry
    assert "nonpositive_ledger_flow_quantity" not in _codes(audit)


def test_known_zero_with_flow_event_is_legal():
    audit, ledger = _audit(
        [
            _entry(
                produced={"value": 10, "unit": "mL"},
                consumed={"value": 0, "unit": "mL"},
                consumer_id="material_transition:mt_001",
                source_kind="device_measurement",
                source_refs=["observation:obs_1"],
                calculation="measured zero draw for mt_001",
            )
        ]
    )
    (entry,) = ledger["entries"]
    assert entry["quantity_status"] == "known"
    assert entry["consumed"] == {"value": 0, "unit": "mL"}
    assert "nonpositive_ledger_flow_quantity" not in _codes(audit)


def test_numeric_quantity_without_provenance_becomes_unknown():
    audit, ledger = _audit([_entry(produced={"value": 8, "unit": "mL"})])
    (entry,) = ledger["entries"]
    assert entry["quantity_status"] == "unknown"
    assert "produced" not in entry
    codes = _codes(audit)
    # No fake-complete-account findings against a repaired unknown entry.
    assert "missing_ledger_quantity_source" not in codes
    assert "missing_ledger_quantity_source_refs" not in codes
    assert "missing_ledger_quantity_calculation" not in codes
    assert "material_quantity_insufficient" not in codes


def test_missing_calculation_invalidates_provenance():
    audit, ledger = _audit(
        [
            _entry(
                produced={"value": 8, "unit": "mL"},
                source_kind="research_explicit",
                source_refs=["macro_step:1"],
            )
        ]
    )
    (entry,) = ledger["entries"]
    assert entry["quantity_status"] == "unknown"
    assert "produced" not in entry


def test_invalid_quantity_status_is_flagged():
    audit, ledger = _audit(
        [
            _entry(
                quantity_status="measured",
                source_kind="research_explicit",
                source_refs=["macro_step:1"],
                calculation="c",
            )
        ]
    )
    assert "invalid_ledger_quantity_status" in _codes(audit)
    (entry,) = ledger["entries"]
    assert entry["quantity_status"] == "unknown"


def test_placeholder_ids_are_sanitized():
    audit, ledger = _audit(
        [
            _entry(
                consumer_id="none",
                consumer_ids=["none", ""],
                source_refs=["none"],
                processing_step_refs=["none"],
                sample_id="",
                production_event_id="",
            )
        ]
    )
    (entry,) = ledger["entries"]
    for field in (
        "consumer_id",
        "sample_id",
        "production_event_id",
    ):
        assert field not in entry
    for field in ("consumer_ids", "source_refs", "processing_step_refs"):
        assert entry.get(field) in (None, [])
    assert audit["quantity_semantics"]["placeholders_sanitized"] >= 1
    event_codes = [
        event["code"] for event in audit["normalization_events"]
    ]
    assert "ledger_placeholder_ids_sanitized" in event_codes


def test_placeholder_consumer_rows_are_dropped():
    audit, ledger = _audit(
        [
            _entry(
                produced={"value": 10, "unit": "mL"},
                consumers=[
                    {"consumer_id": "none", "quantity": {"value": 4, "unit": "mL"}},
                    {"consumer_id": "c1", "quantity": {"value": 6, "unit": "mL"}},
                ],
                source_kind="research_explicit",
                source_refs=["macro_step:1"],
                calculation="produced 10 mL",
            )
        ]
    )
    (entry,) = ledger["entries"]
    assert [row["consumer_id"] for row in entry["consumers"]] == ["c1"]


def test_numeric_rules_skip_unknown_but_apply_to_known():
    known_audit, _ = _audit(
        [
            _entry(
                produced={"value": 2, "unit": "mL"},
                consumed={"value": 8, "unit": "mL"},
                consumer_id="c1",
                source_kind="research_explicit",
                source_refs=["macro_step:1"],
                calculation="declared 2 produced, 8 consumed",
            )
        ]
    )
    assert "material_quantity_insufficient" in _codes(known_audit)
    unknown_audit, _ = _audit(
        [_entry(produced={"value": 2, "unit": "mL"}, consumed={"value": 8, "unit": "mL"})]
    )
    codes = _codes(unknown_audit)
    assert "material_quantity_insufficient" not in codes
    assert "negative_ledger_quantity" not in codes


def test_negative_quantity_flagged_on_known_entry():
    audit, _ = _audit(
        [
            _entry(
                produced={"value": -1, "unit": "mL"},
                source_kind="device_measurement",
                source_refs=["observation:obs_9"],
                calculation="device reported -1 mL",
            )
        ]
    )
    assert "negative_ledger_quantity" in _codes(audit)


def test_duplicate_production_same_source_collapses():
    batches = [
        {
            "batch_id": "batch_000",
            "quantity_mode": "numeric_inventory",
            "material_id": "M1",
            "total_quantity": {"value": 8, "unit": "mL"},
            "source_kind": "research_explicit",
            "source_refs": ["macro_step:1"],
            "calculation": "c",
        }
    ]
    entries = [
        _entry(
            entry_id="ml_a",
            produced={"value": 8, "unit": "mL"},
            production_event_id="pe_001",
            source_kind="research_explicit",
            source_refs=["macro_step:1"],
            calculation="one production of 8 mL",
        ),
        _entry(
            entry_id="ml_b",
            produced={"value": 8, "unit": "mL"},
            production_event_id="pe_001",
            source_kind="research_explicit",
            source_refs=["macro_step:1"],
            calculation="one production of 8 mL",
        ),
    ]
    audit, ledger = _audit(entries, batches=batches)
    assert "duplicate_production_record" not in _codes(audit)
    semantics = audit["quantity_semantics"]
    assert semantics["duplicate_production_events_collapsed"] == 1
    aggregate = ledger["aggregates"][0]
    assert aggregate["produced"] == {"value": 8, "unit": "mL"}


def test_conflicting_production_same_source_is_flagged():
    batches = [
        {
            "batch_id": "batch_000",
            "quantity_mode": "numeric_inventory",
            "material_id": "M1",
            "total_quantity": {"value": 8, "unit": "mL"},
            "source_kind": "research_explicit",
            "source_refs": ["macro_step:1"],
            "calculation": "c",
        }
    ]
    entries = [
        _entry(
            entry_id="ml_a",
            produced={"value": 8, "unit": "mL"},
            production_event_id="pe_001",
            source_kind="research_explicit",
            source_refs=["macro_step:1"],
            calculation="first record",
        ),
        _entry(
            entry_id="ml_b",
            produced={"value": 9, "unit": "mL"},
            production_event_id="pe_001",
            source_kind="research_explicit",
            source_refs=["macro_step:1"],
            calculation="conflicting second record",
        ),
    ]
    audit, _ = _audit(entries, batches=batches)
    assert "duplicate_production_record" in _codes(audit)


def test_audit_summary_splits_known_and_unknown():
    audit, _ = _audit(
        [
            _entry(
                quantity_status="bogus",
                source_kind="research_explicit",
                source_refs=["macro_step:1"],
                calculation="c",
            )
        ]
    )
    semantics = audit["quantity_semantics"]
    assert semantics["unknown_quantity_unmarked"] == 1
    assert semantics["ledger_entries"] == 1
    assert semantics["ledger_status_counts"] == {"unknown": 1}
    assert "known_quantity_violations" in semantics


def test_legacy_source_kind_is_migrated():
    audit, ledger = _audit(
        [
            _entry(
                produced={"value": 5, "unit": "mL"},
                source_kind="derived",
                source_refs=["material_transition:mt_001"],
                calculation="legacy schema v1 value",
            )
        ]
    )
    (entry,) = ledger["entries"]
    assert entry["source_kind"] == "derived_from_parent"
    assert entry["quantity_status"] == "known"
    assert audit["quantity_semantics"]["legacy_source_kinds_migrated"] == 1


def test_aggregate_without_flow_is_not_zero_filled():
    audit, ledger = _audit([_entry(sample_id="S1")])
    aggregate = ledger["aggregates"][0]
    assert aggregate["quantity_status"] == "unknown"
    for field in ("produced", "consumed", "reserved", "balance"):
        assert field not in aggregate


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
