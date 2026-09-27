"""Deterministic capability preflight for a proposed chemical route.

This is a planning check, not the Device hard gate.  A supported abstract
capability does not prove that a particular recipe, scale, container chain, or
set of machine parameters can be dispatched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from agent_skills.capabilities import (
    capability_snapshot_id,
    load_current_capability_index,
    project_device_context,
)


_CONFIRMED_AVAILABLE = frozenset({"available", "online", "ready", "idle", "可用", "空闲"})
_RESTRICTION_KEYS = (
    "restrictions",
    "excluded_capabilities",
    "allowed_capabilities",
    "allowed_operations",
)


def _canonical_json(value: Any) -> Any:
    """Make restriction sets stable without silently dropping unknown values."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("restriction keys must be strings")
        return {key: _canonical_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_json(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("restriction value is not JSON-compatible")


def _string_id_set(value: Any) -> set[str] | None:
    """Return None for a malformed allowlist or denylist; never ignore it."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in value
    ):
        return None
    return set(value)


def preflight_route_capabilities(
    required_capabilities: Sequence[str],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check exact capability IDs against one current, scoped Device context.

    ``preflight_supported`` means only that every declared abstract capability
    has a reviewed operation mapping at a station explicitly marked usable.
    The Device layer must still validate the complete plan before execution.
    """
    try:
        # An explicit context may restrict the roster or carry live status.
        # Preserve those restrictions; otherwise use the refreshed Skill index.
        source = context if context is not None else load_current_capability_index()
        base_snapshot_id = capability_snapshot_id(source)
        projected = project_device_context(source, "experiment")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        return {
            "status": "unknown",
            "missing_capabilities": [],
            "unresolved_capabilities": [],
            "checked_capabilities": [],
            "snapshot_id": "",
            "reasons": [f"device_truth_unavailable:{type(exc).__name__}"],
        }

    try:
        restrictions = _canonical_json({
            key: projected[key] for key in _RESTRICTION_KEYS if key in projected
        })
        payload = json.dumps(
            {"capability_snapshot_id": base_snapshot_id, "restrictions": restrictions},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        snapshot_id = "route_device_snapshot_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return {
            "status": "unknown",
            "missing_capabilities": [],
            "unresolved_capabilities": [],
            "checked_capabilities": [],
            "snapshot_id": "",
            "reasons": ["capability_restrictions_invalid"],
        }

    result: dict[str, Any] = {
        "status": "unknown",
        "missing_capabilities": [],
        "unresolved_capabilities": [],
        "checked_capabilities": [],
        "snapshot_id": snapshot_id,
        "reasons": [],
    }
    if isinstance(required_capabilities, (str, bytes)) or not isinstance(
        required_capabilities, Sequence
    ):
        result["reasons"].append("required_capabilities_invalid")
        return result

    required: list[str] = []
    for capability_id in required_capabilities:
        if not isinstance(capability_id, str) or not capability_id or capability_id != capability_id.strip():
            result["reasons"].append("required_capability_id_invalid")
            return result
        if capability_id not in required:
            required.append(capability_id)
    if not required:
        result["reasons"].append("required_capabilities_undeclared")
        return result

    records = [
        item for item in projected.get("capabilities", [])
        if isinstance(item, dict)
    ]
    excluded_ids = set()
    if "excluded_capabilities" in projected:
        excluded_ids = _string_id_set(projected["excluded_capabilities"])
    allowed_ids = None
    if "allowed_capabilities" in projected:
        allowed_ids = _string_id_set(projected["allowed_capabilities"])
    allowed_operations = True
    if "allowed_operations" in projected:
        allowed_operations = _string_id_set(projected["allowed_operations"])
    if excluded_ids is None or (
        "allowed_capabilities" in projected and allowed_ids is None
    ) or allowed_operations is None:
        result["reasons"].append("capability_restrictions_invalid")
        return result

    result["checked_capabilities"] = list(required)

    for capability_id in required:
        if capability_id in excluded_ids or (allowed_ids is not None and capability_id not in allowed_ids):
            result["missing_capabilities"].append(capability_id)
            result["reasons"].append(f"capability_disallowed:{capability_id}")
            continue

        matches = [item for item in records if item.get("id") == capability_id]
        if not matches:
            result["unresolved_capabilities"].append(capability_id)
            result["reasons"].append(f"capability_not_declared:{capability_id}")
            continue

        # Multiple workstations may provide one capability.  One verified,
        # currently usable implementation is enough; a merely similar name is
        # never evidence.  A truth gap cannot authorize a Device operation.
        if any(
            item.get("support_status") == "supported"
            and item.get("operation_mapping_status") == "mapped"
            and item.get("currently_usable") is True
            and str(item.get("availability") or "").strip().lower() in _CONFIRMED_AVAILABLE
            for item in matches
        ):
            # The experiment projection deliberately omits operation details.
            # An extra operation allowlist cannot be certified at this tier.
            if "allowed_operations" in projected:
                result["unresolved_capabilities"].append(capability_id)
                result["reasons"].append(f"operation_scope_requires_device_validation:{capability_id}")
            continue
        if any(
            item.get("support_status") == "supported"
            and item.get("operation_mapping_status") != "truth_gap"
            and item.get("currently_usable") is not False
            for item in matches
        ) or any(
            item.get("support_status") not in {"supported", "unsupported"}
            for item in matches
        ):
            result["unresolved_capabilities"].append(capability_id)
            result["reasons"].append(f"capability_mapping_or_availability_unknown:{capability_id}")
            continue

        result["missing_capabilities"].append(capability_id)
        if any(item.get("operation_mapping_status") == "truth_gap" for item in matches):
            reason = "capability_operation_truth_gap"
        elif any(item.get("support_status") == "supported" and item.get("currently_usable") is False for item in matches):
            reason = "capability_station_unavailable"
        else:
            reason = "capability_explicitly_unsupported"
        result["reasons"].append(f"{reason}:{capability_id}")

    if result["missing_capabilities"]:
        result["status"] = "blocked"
    elif not result["unresolved_capabilities"]:
        result["status"] = "preflight_supported"
    return result


__all__ = ["preflight_route_capabilities"]
