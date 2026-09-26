"""Focused offline tests for contract scalar normalization."""

from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError

try:
    from .contract_value_normalizer import (
        RULE_ID,
        canonicalize_contract_unit,
        normalize_contract_scalar,
    )
except ImportError:  # Direct script compatibility.
    from contract_value_normalizer import (  # type: ignore
        RULE_ID,
        canonicalize_contract_unit,
        normalize_contract_scalar,
    )


class ContractValueNormalizerTests(unittest.TestCase):
    def test_required_unit_aliases_normalize_without_conversion(self) -> None:
        cases = [
            ("600 转/分", "int", "r/min", 600, "rpm"),
            ("2 分钟", "int", "min", 2, "min"),
            ("30 秒", "int", "s", 30, "s"),
            ("25 摄氏度", "int", "℃", 25, "°C"),
            ("4.5 毫升", "float", "mL", 4.5, "mL"),
            ("50 uL", "int", "μL", 50, "μL"),
        ]
        for value, type_name, unit, expected, canonical in cases:
            with self.subTest(value=value, unit=unit):
                result = normalize_contract_scalar(value, type_name, unit)
                self.assertTrue(result.accepted)
                self.assertTrue(result.changed)
                self.assertEqual(expected, result.new_value)
                self.assertEqual(canonical, result.canonical_unit)
                self.assertEqual(RULE_ID, result.rule_id)

    def test_current_skill_unit_set_has_stable_aliases(self) -> None:
        cases = [
            ("90 °", "°", "°"),
            ("5 °/min", "度/分钟", "°/min"),
            ("30 FPS", "FPS", "fps"),
            ("2 g", "g", "g"),
            ("450 nm", "nm", "nm"),
            ("3 mL/min", "毫升/分钟", "mL/min"),
            ("1.5 MPa", "兆帕", "MPa"),
            ("20 nm/min", "纳米/分钟", "nm/min"),
        ]
        for value, declared_unit, canonical in cases:
            with self.subTest(value=value, declared_unit=declared_unit):
                result = normalize_contract_scalar(value, "number", declared_unit)
                self.assertTrue(result.accepted)
                self.assertEqual(canonical, result.canonical_unit)

    def test_metadata_preserves_original_and_declared_spellings(self) -> None:
        result = normalize_contract_scalar(" 600 r/min ", " int ", " 转/分 ")
        self.assertEqual(" 600 r/min ", result.original_value)
        self.assertEqual(600, result.new_value)
        self.assertEqual("int", result.declared_type)
        self.assertEqual("转/分", result.declared_unit)
        self.assertEqual("r/min", result.input_unit)
        self.assertEqual("rpm", result.canonical_unit)
        self.assertEqual("contract_unit_scalar/v1", result.rule_id)

    def test_non_numeric_declared_types_are_untouched(self) -> None:
        for declared in ("string", "bool", "array", "object", "file", ""):
            with self.subTest(declared=declared):
                result = normalize_contract_scalar("600 rpm", declared, "rpm")
                self.assertFalse(result.applicable)
                self.assertFalse(result.accepted)
                self.assertFalse(result.changed)
                self.assertEqual("600 rpm", result.new_value)
                self.assertEqual("declared_type_not_numeric", result.reason)

    def test_boolean_is_never_treated_as_an_integer(self) -> None:
        result = normalize_contract_scalar(True, "int")
        self.assertTrue(result.applicable)
        self.assertFalse(result.accepted)
        self.assertFalse(result.changed)
        self.assertIs(result.new_value, True)
        self.assertEqual("boolean_not_numeric", result.reason)

    def test_full_string_parser_rejects_ambiguous_literals(self) -> None:
        rejected = [
            "约2 min",
            "2 min左右",
            "2-3 min",
            "2 至 3 min",
            ">2 min",
            ">=2 min",
            "NaN min",
            "Inf min",
            "Infinity min",
            "1e3 min",
            "2 min extra",
            ".5 min",
            "2. min",
        ]
        for value in rejected:
            with self.subTest(value=value):
                result = normalize_contract_scalar(value, "number", "min")
                self.assertFalse(result.accepted)
                self.assertFalse(result.changed)
                self.assertEqual(value, result.new_value)

    def test_no_cross_unit_conversion(self) -> None:
        result = normalize_contract_scalar("120 s", "int", "min")
        self.assertFalse(result.accepted)
        self.assertFalse(result.changed)
        self.assertEqual("120 s", result.new_value)
        self.assertEqual("s", result.input_unit)
        self.assertEqual("min", result.canonical_unit)
        self.assertEqual("unit_mismatch", result.reason)

    def test_unit_suffix_requires_a_declared_unit(self) -> None:
        result = normalize_contract_scalar("2 min", "int", None)
        self.assertFalse(result.accepted)
        self.assertEqual("unexpected_unit", result.reason)
        self.assertIsNone(result.canonical_unit)

        unitless = normalize_contract_scalar("2", "int", None)
        self.assertTrue(unitless.accepted)
        self.assertEqual(2, unitless.new_value)

    def test_integer_requires_an_exact_integral_value(self) -> None:
        exact_text = normalize_contract_scalar("2.0 min", "int", "分钟")
        self.assertTrue(exact_text.accepted)
        self.assertEqual(2, exact_text.new_value)
        self.assertIs(type(exact_text.new_value), int)

        fraction_text = normalize_contract_scalar("2.5 min", "int", "min")
        self.assertFalse(fraction_text.accepted)
        self.assertEqual("fractional_value_for_integer", fraction_text.reason)

        exact_float = normalize_contract_scalar(2.0, "integer", "min")
        self.assertTrue(exact_float.accepted)
        self.assertTrue(exact_float.changed)
        self.assertIs(type(exact_float.new_value), int)

        fraction_float = normalize_contract_scalar(2.5, "integer", "min")
        self.assertFalse(fraction_float.accepted)
        self.assertEqual(2.5, fraction_float.new_value)

        platform_float = normalize_contract_scalar(2, "float", "min")
        self.assertTrue(platform_float.accepted)
        self.assertTrue(platform_float.changed)
        self.assertIs(type(platform_float.new_value), float)
        self.assertEqual(2.0, platform_float.new_value)

    def test_nonfinite_native_numbers_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=repr(value)):
                result = normalize_contract_scalar(value, "number")
                self.assertFalse(result.accepted)
                self.assertFalse(result.changed)
                self.assertEqual("nonfinite_number", result.reason)

        overflowing_integer = 10**400
        result = normalize_contract_scalar(overflowing_integer, "float")
        self.assertFalse(result.accepted)
        self.assertFalse(result.changed)
        self.assertIs(result.new_value, overflowing_integer)
        self.assertEqual("nonfinite_number", result.reason)

    def test_exact_unknown_unit_is_allowed_but_not_guessed(self) -> None:
        exact = normalize_contract_scalar("3 custom-unit", "int", "custom-unit")
        self.assertTrue(exact.accepted)
        self.assertEqual("custom-unit", exact.canonical_unit)

        different_case = normalize_contract_scalar("3 CUSTOM-UNIT", "int", "custom-unit")
        self.assertFalse(different_case.accepted)
        self.assertEqual("unit_mismatch", different_case.reason)

    def test_result_is_frozen(self) -> None:
        result = normalize_contract_scalar("2 min", "int", "min")
        with self.assertRaises(FrozenInstanceError):
            result.new_value = 3  # type: ignore[misc]

    def test_normalization_is_idempotent(self) -> None:
        first = normalize_contract_scalar("600 rpm", "int", "r/min")
        second = normalize_contract_scalar(first.new_value, "int", "r/min")
        third = normalize_contract_scalar(second.new_value, "int", "r/min")
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertFalse(third.changed)
        self.assertEqual(first.new_value, second.new_value)
        self.assertEqual(second.new_value, third.new_value)
        self.assertTrue(second.accepted)
        self.assertEqual("already_normalized", second.reason)

    def test_canonicalize_contract_unit_is_alias_only(self) -> None:
        self.assertEqual("mL", canonicalize_contract_unit(" 毫升 "))
        self.assertEqual("μL", canonicalize_contract_unit("µL"))
        self.assertEqual("MPa", canonicalize_contract_unit("mpa"))
        self.assertEqual("unregistered", canonicalize_contract_unit("unregistered"))
        self.assertEqual(
            "custom-unit",
            canonicalize_contract_unit("CUSTOM-UNIT", lowercase_unknown=True),
        )
        self.assertIsNone(canonicalize_contract_unit(""))


if __name__ == "__main__":
    unittest.main()
