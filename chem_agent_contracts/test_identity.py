"""Regression tests for the shared typed JSON-scalar identity wire codec."""

from __future__ import annotations

import pytest

from chem_agent_contracts.identity import (
    IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
    IDENTITY_WIRE_PREFIX,
    IdentityContractError,
    decode_json_scalar_identity,
    decode_package_identity,
    encode_json_scalar_identity,
    json_scalar_identity_key,
)


def test_wire_codec_round_trips_types_and_escapes_reserved_prefix_strings():
    reserved_string = f"{IDENTITY_WIRE_PREFIX}i:1"
    values = [0, 1, -2, 0.0, 1.0, 1.5, "1", "001", "1,2", reserved_string]

    encoded = [encode_json_scalar_identity(value) for value in values]
    decoded = [decode_json_scalar_identity(value) for value in encoded]

    assert encoded[6:9] == ["1", "001", "1,2"]
    assert encoded[-1].startswith(f"{IDENTITY_WIRE_PREFIX}s:")
    assert [json_scalar_identity_key(value) for value in decoded] == [
        json_scalar_identity_key(value) for value in values
    ]
    assert len(set(encoded)) == len(encoded)


def test_float_zero_wire_form_matches_typed_key_semantics():
    assert json_scalar_identity_key(-0.0) == json_scalar_identity_key(0.0)
    assert encode_json_scalar_identity(-0.0) == encode_json_scalar_identity(0.0)
    assert decode_json_scalar_identity(encode_json_scalar_identity(-0.0)) == 0.0


def test_unmarked_legacy_package_treats_reserved_prefix_as_literal_string():
    legacy = f"{IDENTITY_WIRE_PREFIX}i:1"
    assert decode_package_identity(legacy, None) == legacy
    assert decode_package_identity(
        encode_json_scalar_identity(1),
        IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1,
    ) == 1


@pytest.mark.parametrize("value", [None, True, False, [], {}, float("inf"), float("nan")])
def test_invalid_runtime_identities_are_rejected(value):
    with pytest.raises(IdentityContractError):
        encode_json_scalar_identity(value)


@pytest.mark.parametrize(
    "wire",
    [
        f"{IDENTITY_WIRE_PREFIX}i:01",
        f"{IDENTITY_WIRE_PREFIX}i:-0",
        f"{IDENTITY_WIRE_PREFIX}f:1",
        f"{IDENTITY_WIRE_PREFIX}f:-0.0",
        f"{IDENTITY_WIRE_PREFIX}s:not-base64!",
        f"{IDENTITY_WIRE_PREFIX}unknown:value",
    ],
)
def test_noncanonical_or_unknown_wire_values_are_rejected(wire):
    with pytest.raises(IdentityContractError):
        decode_json_scalar_identity(wire)
