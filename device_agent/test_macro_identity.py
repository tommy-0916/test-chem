"""Regression tests for opaque, typed Research macro identifiers."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from chem_agent_contracts.identity import (
    encode_json_scalar_identity,
    json_scalar_identity_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from macro_identity import (
    MacroIdentityError,
    extract_source_macro_ids,
    macro_id_key,
    normalize_macro_id,
    semantic_macro_action_id,
    semantic_macro_id,
)


def test_scalar_ids_preserve_strings_without_numeric_or_delimiter_parsing():
    assert normalize_macro_id("001") == "001"
    assert normalize_macro_id("1,2") == "1,2"
    assert normalize_macro_id(" phase-alpha ") == " phase-alpha "
    assert macro_id_key(1) != macro_id_key("1")


def test_device_helpers_share_contract_keys_including_float_zero():
    for value in (0, 1, 1.0, -0.0, "1", "001", "1,2"):
        assert macro_id_key(value) == json_scalar_identity_key(value)
    assert macro_id_key(-0.0) == macro_id_key(0.0)
    assert encode_json_scalar_identity(-0.0) == encode_json_scalar_identity(0.0)


def test_only_real_arrays_expand_and_order_and_types_are_preserved():
    assert extract_source_macro_ids({"source_macro_step": "1,2"}) == ["1,2"]
    assert extract_source_macro_ids(
        {"source_macro_step": ["001", 1, "1", "phase-alpha"]}
    ) == ["001", 1, "1", "phase-alpha"]
    assert extract_source_macro_ids(
        {
            "source_macro_step": 1,
            "source_macro_steps": [1, "1", "001", "1,2"],
        }
    ) == [1, "1", "001", "1,2"]


def test_source_mirrors_require_typed_primary_and_complete_coverage_agreement():
    assert extract_source_macro_ids(
        {
            "source_macro_step_id": 0,
            "source_macro_step": 0,
            "source_macro_steps": [0, "1"],
        }
    ) == [0, "1"]

    with pytest.raises(MacroIdentityError) as caught:
        extract_source_macro_ids(
            {"source_macro_step_id": 1, "source_macro_steps": ["1"]},
            "workflow.steps[0]",
        )
    assert caught.value.code == "CONFLICTING_MACRO_SOURCE"

    with pytest.raises(MacroIdentityError) as caught:
        extract_source_macro_ids(
            {
                "source_macro_step": [1, "1"],
                "source_macro_steps": [1, "001"],
            },
            "workflow.steps[0]",
        )
    assert caught.value.code == "CONFLICTING_MACRO_SOURCE_COVERAGE"


def test_source_coverage_duplicates_and_explicit_null_fail_closed():
    with pytest.raises(MacroIdentityError) as caught:
        extract_source_macro_ids(
            {"source_macro_steps": [1, 1]}, "workflow.steps[0]"
        )
    assert caught.value.code == "DUPLICATE_MACRO_SOURCE_COVERAGE"

    with pytest.raises(MacroIdentityError) as caught:
        extract_source_macro_ids(
            {"source_macro_step_id": None}, "workflow.steps[0]"
        )
    assert caught.value.code == "INVALID_MACRO_ID"


@pytest.mark.parametrize("value", [True, False, None, {}, ()])
def test_non_scalar_or_boolean_ids_are_rejected(value):
    with pytest.raises(MacroIdentityError) as caught:
        normalize_macro_id(value, "macro.id")
    assert caught.value.code == "INVALID_MACRO_ID"
    assert caught.value.path == "macro.id"


def test_plural_source_requires_an_actual_array():
    with pytest.raises(MacroIdentityError) as caught:
        extract_source_macro_ids({"source_macro_steps": "1,2"}, "plan[0]")
    assert caught.value.code == "INVALID_MACRO_SOURCE"
    assert caught.value.path == "plan[0].source_macro_steps"


def test_semantic_step_id_requires_typed_mirror_agreement_and_preserves_zero():
    assert semantic_macro_id(
        {
            "macro_step_id": 0,
            "logical_step_id": 0,
            "macro_action_id": "action",
            "步骤序号": 7,
            "step": 9,
        }
    ) == 0

    with pytest.raises(MacroIdentityError) as caught:
        semantic_macro_id({"macro_step_id": 1, "logical_step_id": "1"})
    assert caught.value.code == "CONFLICTING_MACRO_ID_MIRRORS"

    with pytest.raises(MacroIdentityError) as caught:
        semantic_macro_id({"macro_step_id": "ok", "logical_step_id": False})
    assert caught.value.code == "INVALID_MACRO_ID"


def test_semantic_step_id_uses_legacy_ordinals_only_without_explicit_id():
    assert semantic_macro_id({"步骤序号": 7, "step": 7}) == 7
    with pytest.raises(MacroIdentityError) as caught:
        semantic_macro_id({"步骤序号": 7, "step": "7"})
    assert caught.value.code == "CONFLICTING_MACRO_ID_MIRRORS"

    # Action IDs occupy a different identity domain and cannot collapse every
    # step in one action into the same step ID.
    with pytest.raises(MacroIdentityError) as caught:
        semantic_macro_id({"macro_action_id": "action"})
    assert caught.value.code == "MISSING_MACRO_ID"
    assert semantic_macro_action_id({"macro_action_id": "action"}) == "action"

    record = {}
    with pytest.raises(MacroIdentityError) as caught:
        semantic_macro_id(record, "macro_action_steps[4]")
    assert caught.value.code == "MISSING_MACRO_ID"
    assert caught.value.path == "macro_action_steps[4]"
