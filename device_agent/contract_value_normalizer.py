"""Strict, auditable normalization for contract-declared numeric scalars.

The normalizer is intentionally narrower than a general unit library.  It may
remove a unit suffix only when the field's contract declares a numeric JSON
type and the suffix is an alias of that exact declared unit.  It never performs
unit conversion, range interpretation, approximation, or value inference.

Callers keep the returned result as normalization evidence and use
``new_value`` only when ``accepted`` is true.  Rejected values are returned
unchanged so the existing validators can continue to fail closed.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


RULE_ID = "contract_unit_scalar/v1"

NUMERIC_DECLARED_TYPES = frozenset(
    {"int", "integer", "float", "number", "double"}
)
INTEGER_DECLARED_TYPES = frozenset({"int", "integer"})
FLOAT_DECLARED_TYPES = frozenset({"float", "double"})


# Canonical spellings are stable audit values, not instructions to convert one
# physical unit into another.  In particular, time, mass, and volume units stay
# separate even where a mathematical conversion would be straightforward.
_UNIT_ALIAS_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("rpm", ("rpm", "r/min", "转/分", "rev/min")),
    ("min", ("min", "minute", "minutes", "分钟", "分")),
    ("s", ("s", "sec", "secs", "second", "seconds", "秒")),
    ("h", ("h", "hr", "hrs", "hour", "hours", "小时")),
    ("°C", ("℃", "°C", "摄氏度", "c")),
    ("mL", ("mL", "ml", "毫升")),
    ("μL", ("μL", "µL", "uL", "ul", "微升")),
    ("L", ("L", "升")),
    ("nL", ("nL", "纳升")),
    ("g", ("g", "克")),
    ("mg", ("mg", "毫克")),
    ("kg", ("kg", "千克", "公斤")),
    ("W", ("W", "瓦")),
    ("mm", ("mm", "毫米")),
    ("cm", ("cm", "厘米")),
    ("nm", ("nm", "纳米")),
    ("fps", ("fps", "frame/s", "frames/s", "帧/秒")),
    ("°", ("°", "deg", "degree", "degrees", "度")),
    ("°/min", ("°/min", "deg/min", "degree/min", "度/分钟")),
    ("mL/min", ("mL/min", "毫升/分钟")),
    ("nm/min", ("nm/min", "纳米/分钟")),
    ("MPa", ("MPa", "兆帕")),
    ("mL/次", ("mL/次", "毫升/次")),
)


def _unit_lookup_key(unit: str) -> str:
    """Normalize typography only; this function never changes dimensions."""

    normalized = unicodedata.normalize("NFKC", unit.strip())
    return re.sub(r"\s+", "", normalized).casefold()


_alias_builder = {}
for _canonical, _aliases in _UNIT_ALIAS_GROUPS:
    for _alias in _aliases:
        _key = _unit_lookup_key(_alias)
        previous = _alias_builder.get(_key)
        if previous is not None and previous != _canonical:  # pragma: no cover
            raise RuntimeError(
                f"ambiguous contract unit alias {_alias!r}: "
                f"{previous!r} versus {_canonical!r}"
            )
        _alias_builder[_key] = _canonical
UNIT_ALIASES: Mapping[str, str] = MappingProxyType(_alias_builder)
del _alias_builder, _canonical, _aliases, _alias, _key, previous


_DECIMAL_LITERAL_RE = re.compile(
    r"^\s*(?P<number>[+-]?\d+(?:\.\d+)?)"
    r"(?:\s*(?P<unit>.*?))?\s*$"
)


@dataclass(frozen=True)
class ContractScalarNormalization:
    """Immutable evidence for one attempted scalar normalization."""

    original_value: Any
    new_value: Any
    changed: bool
    declared_type: str
    declared_unit: str
    input_unit: Optional[str]
    canonical_unit: Optional[str]
    rule_id: str = field(default=RULE_ID, init=False)
    applicable: bool = True
    accepted: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _UnitIdentity:
    comparison_key: str
    canonical: str


def _unit_identity(unit: str) -> _UnitIdentity:
    stripped = unit.strip()
    known = UNIT_ALIASES.get(_unit_lookup_key(stripped))
    if known is not None:
        return _UnitIdentity(f"known:{known}", known)

    # Unknown units are not guessed or case-folded.  Exact NFKC-equivalent
    # spelling can still pass, which keeps the helper usable for future Skills
    # without silently treating dimensionally different symbols as aliases.
    opaque = unicodedata.normalize("NFKC", stripped)
    return _UnitIdentity(f"opaque:{opaque}", opaque)


def canonicalize_contract_unit(
    unit: Any, *, lowercase_unknown: bool = False
) -> Optional[str]:
    """Return the stable spelling for a declared unit, or ``None`` if absent.

    ``lowercase_unknown`` preserves the historical validator behaviour for
    units that are not yet registered, while known physical units retain their
    audited canonical spelling (for example ``MPa`` and ``mL``).
    """

    if unit is None:
        return None
    text = str(unit).strip()
    if not text:
        return None
    identity = _unit_identity(text)
    if lowercase_unknown and identity.comparison_key.startswith("opaque:"):
        return identity.canonical.lower()
    return identity.canonical


def _result(
    original_value: Any,
    new_value: Any,
    *,
    declared_type: str,
    declared_unit: str,
    input_unit: Optional[str],
    canonical_unit: Optional[str],
    applicable: bool,
    accepted: bool,
    reason: str,
) -> ContractScalarNormalization:
    if type(original_value) is not type(new_value):
        changed = True
    elif original_value is new_value:
        # Rejected values are deliberately returned by identity.  This also
        # handles NaN, whose equality comparison with itself is false.
        changed = False
    else:
        changed = original_value != new_value
    return ContractScalarNormalization(
        original_value=original_value,
        new_value=new_value,
        changed=changed,
        declared_type=declared_type,
        declared_unit=declared_unit,
        input_unit=input_unit,
        canonical_unit=canonical_unit,
        applicable=applicable,
        accepted=accepted,
        reason=reason,
    )


def normalize_contract_scalar(
    value: Any,
    declared_type: Any,
    declared_unit: Any = None,
) -> ContractScalarNormalization:
    """Normalize one scalar under an explicit type-and-unit contract.

    Numeric values already represented as JSON numbers are validated for
    finiteness and otherwise retained.  Strings use a deliberately restricted
    decimal grammar: no exponent notation, ranges, comparisons, or prose.  An
    explicit suffix must be equivalent to the declared unit; ``120 s`` cannot
    satisfy a ``min`` contract.
    """

    declared_type_text = "" if declared_type is None else str(declared_type).strip()
    normalized_type = declared_type_text.casefold()
    declared_unit_text = "" if declared_unit is None else str(declared_unit).strip()
    declared_identity = (
        _unit_identity(declared_unit_text) if declared_unit_text else None
    )
    canonical_unit = (
        declared_identity.canonical if declared_identity is not None else None
    )

    base = {
        "declared_type": declared_type_text,
        "declared_unit": declared_unit_text,
        "input_unit": None,
        "canonical_unit": canonical_unit,
    }

    if normalized_type not in NUMERIC_DECLARED_TYPES:
        return _result(
            value,
            value,
            **base,
            applicable=False,
            accepted=False,
            reason="declared_type_not_numeric",
        )

    if isinstance(value, bool):
        return _result(
            value,
            value,
            **base,
            applicable=True,
            accepted=False,
            reason="boolean_not_numeric",
        )

    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return _result(
                value,
                value,
                **base,
                applicable=True,
                accepted=False,
                reason="nonfinite_number",
            )
        if normalized_type in INTEGER_DECLARED_TYPES:
            if isinstance(value, float) and not value.is_integer():
                return _result(
                    value,
                    value,
                    **base,
                    applicable=True,
                    accepted=False,
                    reason="fractional_value_for_integer",
                )
            new_value = int(value)
        elif normalized_type in FLOAT_DECLARED_TYPES:
            try:
                new_value = float(value)
            except OverflowError:
                return _result(
                    value,
                    value,
                    **base,
                    applicable=True,
                    accepted=False,
                    reason="nonfinite_number",
                )
            if not math.isfinite(new_value):
                return _result(
                    value,
                    value,
                    **base,
                    applicable=True,
                    accepted=False,
                    reason="nonfinite_number",
                )
        else:
            # Generic ``number`` preserves the caller's valid JSON number type.
            new_value = value
        return _result(
            value,
            new_value,
            **base,
            applicable=True,
            accepted=True,
            reason="normalized" if type(value) is not type(new_value) else "already_normalized",
        )

    if not isinstance(value, str):
        return _result(
            value,
            value,
            **base,
            applicable=True,
            accepted=False,
            reason="unsupported_value_type",
        )

    match = _DECIMAL_LITERAL_RE.fullmatch(value)
    if not match:
        return _result(
            value,
            value,
            **base,
            applicable=True,
            accepted=False,
            reason="invalid_numeric_literal",
        )

    number_token = match.group("number")
    raw_unit = (match.group("unit") or "").strip() or None
    base["input_unit"] = raw_unit

    if raw_unit is not None:
        input_identity = _unit_identity(raw_unit)
        if declared_identity is None:
            return _result(
                value,
                value,
                **base,
                applicable=True,
                accepted=False,
                reason="unexpected_unit",
            )
        if input_identity.comparison_key != declared_identity.comparison_key:
            return _result(
                value,
                value,
                **base,
                applicable=True,
                accepted=False,
                reason="unit_mismatch",
            )

    try:
        decimal_value = Decimal(number_token)
    except InvalidOperation:  # Defensive; the regex has already constrained it.
        return _result(
            value,
            value,
            **base,
            applicable=True,
            accepted=False,
            reason="invalid_numeric_literal",
        )

    if normalized_type in INTEGER_DECLARED_TYPES:
        if decimal_value != decimal_value.to_integral_value():
            return _result(
                value,
                value,
                **base,
                applicable=True,
                accepted=False,
                reason="fractional_value_for_integer",
            )
        new_value = int(decimal_value)
    elif normalized_type in FLOAT_DECLARED_TYPES:
        new_value = float(decimal_value)
        if not math.isfinite(new_value):
            return _result(
                value,
                value,
                **base,
                applicable=True,
                accepted=False,
                reason="nonfinite_number",
            )
    else:  # Generic ``number`` preserves integral literals as integers.
        if "." not in number_token:
            new_value = int(decimal_value)
        else:
            new_value = float(decimal_value)
            if not math.isfinite(new_value):
                return _result(
                    value,
                    value,
                    **base,
                    applicable=True,
                    accepted=False,
                    reason="nonfinite_number",
                )

    return _result(
        value,
        new_value,
        **base,
        applicable=True,
        accepted=True,
        reason="normalized",
    )


__all__ = [
    "ContractScalarNormalization",
    "NUMERIC_DECLARED_TYPES",
    "RULE_ID",
    "UNIT_ALIASES",
    "canonicalize_contract_unit",
    "normalize_contract_scalar",
]
