"""Verification mode for route fields; independent of evidence provenance."""

from __future__ import annotations

import re
from typing import Any

from .v2 import normalize_material_state


_GENERATED_MATERIAL_ID_PATH = re.compile(
    r"material_graph\[(?:0|[1-9][0-9]*)\]"
    r"(?:\.[a-z][a-z0-9_]*(?:\[(?:0|[1-9][0-9]*)\])?)*"
    r"\.(?:material_(?:instance_)?id|[a-z_]*material_instance_ids"
    r"\[(?:0|[1-9][0-9]*)\])\Z"
)
_CONTROLLED_STATE_PATH = re.compile(
    r"(?:material_graph\[(?:0|[1-9][0-9]*)\]"
    r"\.material_(?:inputs|intermediates|outputs)"
    r"\[(?:0|[1-9][0-9]*)\]\.state|"
    r"route_signature\.(?:endpoint_state|phase_transitions"
    r"\[(?:0|[1-9][0-9]*)\]\.(?:before_state|after_state)))\Z"
)
_CONTROLLED_OPERATION_PATH = re.compile(
    r"(?:material_graph\[(?:0|[1-9][0-9]*)\]\.operation|"
    r"route_signature\.operations\[(?:0|[1-9][0-9]*)\])\Z"
)
_CONTROLLED_MATERIAL_NAME_PATH = re.compile(
    r"material_graph\[(?:0|[1-9][0-9]*)\]\."
    r"material_(?:inputs|intermediates|outputs)"
    r"\[(?:0|[1-9][0-9]*)\]\.name\Z"
)


def classify_route_field_basis(field_path: str) -> str:
    """Select a proof method without asserting provenance or approval.

    The current proposal compiler admits source literals and generated IDs.
    Controlled terms without a verified mapping remain pending. Convention,
    derived and runtime values retain their existing separate gates. Device
    SOP is an evidence source class, not a field verification mode.
    """
    if _GENERATED_MATERIAL_ID_PATH.fullmatch(field_path):
        return "generated_id"
    if (_CONTROLLED_STATE_PATH.fullmatch(field_path)
            or _CONTROLLED_MATERIAL_NAME_PATH.fullmatch(field_path)
            or _CONTROLLED_OPERATION_PATH.fullmatch(field_path)):
        return "controlled_mapping"
    return "paper_literal"


def controlled_state_requires_mapping(field_path: str, value: Any) -> bool:
    """A literal alias cannot silently become a different V2 state token."""
    return bool(
        _CONTROLLED_STATE_PATH.fullmatch(field_path)
        and isinstance(value, str)
        and (
            normalize_material_state(value) != value
            or normalize_material_state(value) == "unknown"
        )
    )
