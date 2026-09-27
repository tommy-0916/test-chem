"""Resolve a current observation point against controlled workstation Skills.

This is a static protocol check, not a claim that a measurement was performed.
Only reviewed capability-to-operation mappings in the workstation index may
select an operation.  Missing sample/container/volume facts stay pending for
Device initial-state and dispatch checks; paper results are not required.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from chem_agent_contracts.v2 import canonical_digest, normalize_material_state


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_INDEX = _REPO_ROOT / "chem_resources" / "workstation_capability_index.json"
_VOLUME_NUMBER = r"(\d+(?:\.\d+)?)\s*(?:mL|ml|毫升)"
_LOWER_WORDS = r"(?:不得少于|不少于|至少|不小于|不得低于)"
_UPPER_WORDS = r"(?:不得超过|不能超过|不超过|至多|不大于)"


@dataclass(frozen=True)
class ObservationProtocolResolutionV1:
    """A source-bound static check of one station operation and its inputs."""

    status: Literal["verified", "runtime_pending", "unsupported", "ambiguous"]
    observation_point: str
    protocol_id: str = ""
    protocol_digest: str = ""
    capability_id: str = ""
    station_code: str = ""
    operation_name: str = ""
    required_capabilities: tuple[str, ...] = ()
    accepted_sample_states: tuple[str, ...] = ()
    accepted_input_containers: tuple[str, ...] = ()
    required_predecessor_station_codes: tuple[str, ...] = ()
    minimum_liquid_volume_ml: float | None = None
    minimum_volume_strict: bool = False
    maximum_liquid_volume_ml: float | None = None
    maximum_volume_strict: bool = False
    source_skill_path: str = ""
    source_skill_sha256: str = ""
    capability_index_sha256: str = ""
    reason_codes: tuple[str, ...] = ()
    resolver_path: tuple[str, ...] = ()


def _word_match(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    if needle.isascii():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack))
    return needle in haystack


def _normal(text: str) -> str:
    return str(text or "").casefold().replace("–", "-").replace("—", "-").strip()


def _candidate_matches(observation: str, station: dict[str, Any], capability: dict[str, Any]) -> bool:
    terms = [capability.get("id", ""), station.get("station_code", "")]
    terms.extend(station.get("aliases", []))
    normalized = _normal(observation)
    return any(_word_match(normalized, _normal(term)) for term in terms if isinstance(term, str))


def _allowed_states(contract: dict[str, Any]) -> tuple[str, ...] | None:
    values = contract.get("input_sample_states", [])
    if not isinstance(values, list) or not values:
        return None
    result: set[str] = set()
    for text in values:
        if not isinstance(text, str):
            return None
        # A station may put an explicit volume qualifier before the state.
        phrase = re.sub(
            r"^(?:大于|超过|不少于|至少|不小于)\s*" + _VOLUME_NUMBER + r"的",
            "",
            text.strip(),
            flags=re.IGNORECASE,
        )
        for part in phrase.split("或"):
            normalized = normalize_material_state(part.strip())
            if normalized == "unknown":
                return None
            result.add(normalized)
    return tuple(sorted(result))


def _volume_bounds(station: dict[str, Any], contract: dict[str, Any]) -> tuple[tuple[float, bool] | None, tuple[float, bool] | None]:
    """Read only explicit comparisons in input states or hard constraints.

    Numeric operation setpoints (for example a drop volume) are deliberately
    excluded: they are not total liquid volume in the incoming container.
    """
    texts = list(contract.get("input_sample_states", [])) + list(station.get("critical_constraints", []))
    lower: list[tuple[float, bool]] = []
    upper: list[tuple[float, bool]] = []
    for raw in texts:
        if not isinstance(raw, str):
            continue
        text = re.sub(r"\*", "", raw)
        for match in re.finditer(r"(?:必须\s*)?(≥|>=|>|≤|<=|<)\s*" + _VOLUME_NUMBER, text, re.IGNORECASE):
            op, value = match.group(1), float(match.group(2))
            (lower if op in {"≥", ">=", ">"} else upper).append((value, op in {">", "<"}))
        for match in re.finditer(_LOWER_WORDS + r"\s*" + _VOLUME_NUMBER, text, re.IGNORECASE):
            lower.append((float(match.group(1)), False))
        for match in re.finditer(_UPPER_WORDS + r"\s*" + _VOLUME_NUMBER, text, re.IGNORECASE):
            upper.append((float(match.group(1)), False))
        for match in re.finditer(r"(?:大于|超过)\s*" + _VOLUME_NUMBER, text, re.IGNORECASE):
            lower.append((float(match.group(1)), True))
        for match in re.finditer(r"(?:小于|低于)\s*" + _VOLUME_NUMBER, text, re.IGNORECASE):
            upper.append((float(match.group(1)), True))
    strongest_lower = max(lower, default=None, key=lambda item: (item[0], item[1]))
    strongest_upper = min(upper, default=None, key=lambda item: (item[0], not item[1]))
    return strongest_lower, strongest_upper


def _source_path(skill_path: str) -> Path:
    path = Path(skill_path)
    if path.is_absolute():
        return path
    # The checked-in index is repository relative. A test index in another
    # directory can provide an absolute Skill path without any special case.
    return _REPO_ROOT / path


def resolve_observation_protocol(
    observation_point: str,
    *,
    capability_index_path: str | Path | None = None,
    sample_state: str | None = None,
    container_type: str | None = None,
    liquid_volume_ml: float | int | None = None,
) -> ObservationProtocolResolutionV1:
    """Resolve a unique observation protocol and check stated static inputs.

    ``verified`` means an explicit workstation Skill protocol accepts the
    supplied static inputs. It does not assert a complete preceding workflow,
    a measured result, runtime inventory, or authorization to dispatch.
    """
    point = str(observation_point or "").strip()
    index_path = Path(capability_index_path) if capability_index_path else _DEFAULT_INDEX
    try:
        index_bytes = index_path.read_bytes()
        index = json.loads(index_bytes)
    except OSError:
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_protocol_index_missing",))
    except (ValueError, TypeError):
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_protocol_index_invalid",))
    if not point or not isinstance(index, dict) or index.get("source_kind") != "lab-design-all" or not isinstance(index.get("workstations"), list):
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_protocol_index_invalid",))

    matches: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for station in index["workstations"]:
        if not isinstance(station, dict):
            continue
        for capability in station.get("experiment_capabilities", []):
            if not isinstance(capability, dict) or not _candidate_matches(point, station, capability):
                continue
            for operation_name in capability.get("operation_names", []):
                if isinstance(operation_name, str) and operation_name:
                    matches.append((station, capability, operation_name))
    matches = list({(station.get("station_code"), capability.get("id"), name): (station, capability, name) for station, capability, name in matches}.values())
    if not matches:
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_capability_unmapped",))
    if len(matches) != 1:
        return ObservationProtocolResolutionV1("ambiguous", point, reason_codes=("observation_capability_ambiguous",))

    station, capability, operation_name = matches[0]
    if capability.get("support_status") != "supported" or capability.get("operation_mapping_status") != "mapped":
        return ObservationProtocolResolutionV1("unsupported", point, capability_id=str(capability.get("id", "")), station_code=str(station.get("station_code", "")), reason_codes=("observation_capability_unsupported",))
    bindings = station.get("capability_operation_bindings")
    if not isinstance(bindings, dict) or operation_name not in bindings.get(capability.get("id"), []):
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_capability_binding_mismatch",))
    station_operations = station.get("operations")
    operations = [
        operation for operation in station_operations
        if isinstance(operation, dict) and operation.get("name") == operation_name
    ] if isinstance(station_operations, list) else []
    if len(operations) != 1 or not isinstance(operations[0].get("container_contract"), dict):
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_operation_contract_missing",))
    operation = operations[0]
    contract = operation["container_contract"]
    skill_path = str(operation.get("source", {}).get("skill", ""))
    if not skill_path:
        return ObservationProtocolResolutionV1("unsupported", point, reason_codes=("observation_skill_source_missing",))
    skill_file = _source_path(skill_path)
    try:
        # The index records the generator's decoded-text digest. ``read_text``
        # normalizes line endings in the same way as that generator.
        skill_sha = hashlib.sha256(skill_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    except (OSError, UnicodeError):
        return ObservationProtocolResolutionV1("unsupported", point, source_skill_path=skill_path, reason_codes=("observation_skill_source_missing",))
    evidence = capability.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("path") != skill_path or evidence.get("sha256") != skill_sha:
        return ObservationProtocolResolutionV1("unsupported", point, source_skill_path=skill_path, source_skill_sha256=skill_sha, reason_codes=("observation_skill_source_stale",))

    capability_id = str(capability["id"])
    station_code = str(station["station_code"])
    index_sha = hashlib.sha256(index_bytes).hexdigest()
    protocol_digest = canonical_digest({
        "schema": "observation-protocol/v1",
        "index_sha256": index_sha,
        "source_skill_sha256": skill_sha,
        "station_code": station_code,
        "capability_id": capability_id,
        "operation_name": operation_name,
        "container_contract": contract,
        "critical_constraints": station.get("critical_constraints", []),
    })
    common = {
        "observation_point": point,
        "protocol_id": f"observation-protocol/v1/{protocol_digest}",
        "protocol_digest": protocol_digest,
        "capability_id": capability_id,
        "station_code": station_code,
        "operation_name": operation_name,
        "required_capabilities": (capability_id,),
        "accepted_input_containers": tuple(str(item) for item in contract.get("accepted_input_containers", [])),
        "required_predecessor_station_codes": tuple(sorted({code for dependency in station.get("dependencies", []) if isinstance(dependency, dict) and dependency.get("resolution") == "referenced" for code in dependency.get("station_codes", []) if isinstance(code, str)})),
        "source_skill_path": skill_path,
        "source_skill_sha256": skill_sha,
        "capability_index_sha256": index_sha,
    }
    states = _allowed_states(contract)
    if states is None:
        return ObservationProtocolResolutionV1("unsupported", accepted_sample_states=(), reason_codes=("observation_sample_contract_unrecognized",), **common)
    lower, upper = _volume_bounds(station, contract)
    common.update({
        "accepted_sample_states": states,
        "minimum_liquid_volume_ml": lower[0] if lower else None,
        "minimum_volume_strict": lower[1] if lower else False,
        "maximum_liquid_volume_ml": upper[0] if upper else None,
        "maximum_volume_strict": upper[1] if upper else False,
    })

    reasons: list[str] = []
    resolvers: list[str] = []
    status: Literal["verified", "runtime_pending", "unsupported", "ambiguous"] = "verified"
    normalized_state = normalize_material_state(sample_state)
    if not sample_state or normalized_state == "unknown":
        status = "runtime_pending"
        reasons.append("observation_sample_state_pending")
        resolvers.append("device_agent.dispatch_checker.initial_state.containers[*].sample_state")
    elif normalized_state not in states:
        status = "unsupported"
        reasons.append("observation_sample_state_conflict")
    if not container_type:
        if status != "unsupported":
            status = "runtime_pending"
        reasons.append("observation_container_pending")
        resolvers.append("device_agent.dispatch_checker.operation_container_contract")
    elif container_type not in common["accepted_input_containers"]:
        status = "unsupported"
        reasons.append("observation_container_conflict")

    volume_required = bool({"solution", "suspension", "supernatant", "filtrate"} & set(states))
    if volume_required and liquid_volume_ml is None:
        if status != "unsupported":
            status = "runtime_pending"
        reasons.append("observation_volume_pending")
        resolvers.append("device_agent.dispatch_checker.initial_state.containers[*].volume_ml -> reaserch_agent.observation_protocol.resolve_observation_protocol")
    elif liquid_volume_ml is not None:
        if isinstance(liquid_volume_ml, bool) or not isinstance(liquid_volume_ml, (int, float)) or not math.isfinite(liquid_volume_ml) or liquid_volume_ml < 0:
            status = "unsupported"
            reasons.append("observation_volume_invalid")
        elif lower and (liquid_volume_ml < lower[0] or (lower[1] and liquid_volume_ml == lower[0])):
            status = "unsupported"
            reasons.append("observation_volume_below_minimum")
        elif upper and (liquid_volume_ml > upper[0] or (upper[1] and liquid_volume_ml == upper[0])):
            status = "unsupported"
            reasons.append("observation_volume_above_maximum")

    return ObservationProtocolResolutionV1(status, reason_codes=tuple(reasons), resolver_path=tuple(resolvers), **common)
