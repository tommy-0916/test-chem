"""Shared typed JSON-scalar identity and V2 wire encoding helpers.

Runtime identity is type-sensitive: JSON number ``1`` is distinct from JSON
string ``"1"``.  The current V2 models expose identity fields as strings, so
numeric identities use a reversible, explicitly marked wire representation.
Ordinary legacy string identifiers remain byte-for-byte unchanged.
"""

from __future__ import annotations

import base64
import json
import math
import re
from typing import Any, Tuple, Union


IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1 = "typed-json-scalar-v1"
IDENTITY_WIRE_PREFIX = "~mid:v1:"

JsonScalarIdentity = Union[str, int, float]
IdentityKey = Tuple[str, Union[str, int, float]]

_INTEGER_TOKEN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


class IdentityContractError(ValueError):
    """An identifier is absent, malformed, or not a supported JSON scalar."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message} ({code.lower()})")


def normalize_json_scalar_identity(
    value: Any,
    path: str = "identity",
) -> JsonScalarIdentity:
    """Validate one opaque identity without parsing or changing its type."""

    if value is None or isinstance(value, bool):
        raise IdentityContractError(
            "INVALID_IDENTITY",
            path,
            "expected a non-boolean JSON string or finite number",
        )
    if isinstance(value, str):
        if not value.strip():
            raise IdentityContractError(
                "INVALID_IDENTITY", path, "expected a nonempty string identity"
            )
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise IdentityContractError(
        "INVALID_IDENTITY",
        path,
        "expected a JSON string or finite number, not an array/object",
    )


def json_scalar_identity_key(
    value: Any,
    path: str = "identity",
) -> IdentityKey:
    """Return a type-tagged key so integer, float, and string IDs never merge."""

    normalized = normalize_json_scalar_identity(value, path)
    if isinstance(normalized, str):
        return ("string", normalized)
    if isinstance(normalized, int):
        return ("integer", normalized)
    return ("number", normalized)


def _base64url_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _base64url_decode(token: str, path: str) -> str:
    if not token:
        raise IdentityContractError(
            "INVALID_WIRE_IDENTITY", path, "missing escaped string payload"
        )
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        )
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise IdentityContractError(
            "INVALID_WIRE_IDENTITY", path, "invalid escaped string payload"
        ) from exc


def encode_json_scalar_identity(
    value: Any,
    path: str = "identity",
) -> str:
    """Encode a typed scalar into the string-only V2 identity wire format.

    Legacy strings stay unchanged unless they occupy the reserved codec prefix;
    those strings are escaped so they cannot collide with encoded numbers.
    """

    normalized = normalize_json_scalar_identity(value, path)
    if isinstance(normalized, str):
        if normalized.startswith(IDENTITY_WIRE_PREFIX):
            return f"{IDENTITY_WIRE_PREFIX}s:{_base64url_encode(normalized)}"
        return normalized
    if isinstance(normalized, int):
        return f"{IDENTITY_WIRE_PREFIX}i:{normalized}"
    # Python (and the existing typed identity key) considers -0.0 and 0.0 the
    # same numeric identity, so the wire form must canonicalize them equally.
    if normalized == 0.0:
        normalized = 0.0
    token = json.dumps(normalized, allow_nan=False, separators=(",", ":"))
    return f"{IDENTITY_WIRE_PREFIX}f:{token}"


def decode_json_scalar_identity(
    value: Any,
    path: str = "identity",
) -> JsonScalarIdentity:
    """Decode an identity from a package marked with this codec.

    Unprefixed strings are legacy-compatible literal strings.  Callers must
    only invoke this decoder when the containing package declares
    ``identity_encoding=typed-json-scalar-v1``; an unmarked historical package
    treats every string literally, including strings that resemble the prefix.
    """

    if not isinstance(value, str) or not value:
        raise IdentityContractError(
            "INVALID_WIRE_IDENTITY", path, "expected a nonempty V2 string identity"
        )
    if not value.startswith(IDENTITY_WIRE_PREFIX):
        return value
    tagged = value[len(IDENTITY_WIRE_PREFIX):]
    kind, separator, token = tagged.partition(":")
    if not separator:
        raise IdentityContractError(
            "INVALID_WIRE_IDENTITY", path, "missing wire identity type tag"
        )
    if kind == "s":
        decoded = _base64url_decode(token, path)
        if not decoded.startswith(IDENTITY_WIRE_PREFIX):
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY",
                path,
                "escaped strings are reserved for prefix-bearing identifiers",
            )
        normalized = normalize_json_scalar_identity(decoded, path)
        if encode_json_scalar_identity(normalized, path) != value:
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY", path, "non-canonical escaped string payload"
            )
        return normalized
    if kind == "i":
        if not _INTEGER_TOKEN.fullmatch(token):
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY", path, "invalid canonical integer payload"
            )
        decoded_integer = int(token)
        if encode_json_scalar_identity(decoded_integer, path) != value:
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY", path, "non-canonical integer payload"
            )
        return decoded_integer
    if kind == "f":
        try:
            decoded = json.loads(token)
        except json.JSONDecodeError as exc:
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY", path, "invalid floating-point payload"
            ) from exc
        if not isinstance(decoded, float) or not math.isfinite(decoded):
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY", path, "expected a finite JSON float payload"
            )
        if encode_json_scalar_identity(decoded, path) != value:
            raise IdentityContractError(
                "INVALID_WIRE_IDENTITY", path, "non-canonical floating-point payload"
            )
        return decoded
    raise IdentityContractError(
        "INVALID_WIRE_IDENTITY", path, f"unknown wire identity type tag {kind!r}"
    )


def decode_package_identity(
    value: Any,
    identity_encoding: str | None,
    path: str = "identity",
) -> JsonScalarIdentity:
    """Decode marked packages while preserving all strings in legacy packages."""

    if identity_encoding == IDENTITY_ENCODING_TYPED_JSON_SCALAR_V1:
        return decode_json_scalar_identity(value, path)
    if not isinstance(value, str) or not value:
        raise IdentityContractError(
            "INVALID_WIRE_IDENTITY",
            path,
            "legacy V2 packages require a nonempty string identity",
        )
    return value
