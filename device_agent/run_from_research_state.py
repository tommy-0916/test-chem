"""Run device agent from a saved research-agent state JSON."""

from __future__ import annotations

import os
import argparse
import copy
import json
import sys
from pathlib import Path
from uuid import uuid4
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from .human_quantity_approval import (
        HumanQuantityApprovalError,
        file_sha256 as approval_request_sha256,
        validate_human_quantity_approvals,
    )
except ImportError:  # pragma: no cover - direct script execution
    from human_quantity_approval import (
        HumanQuantityApprovalError,
        file_sha256 as approval_request_sha256,
        validate_human_quantity_approvals,
    )

try:
    from .feasibility_certificate import (
        FEASIBILITY_CERTIFICATE_VERSION,
        FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS,
        strict_feasibility_certificate_version,
    )
except ImportError:  # pragma: no cover - direct script execution
    from feasibility_certificate import (  # type: ignore
        FEASIBILITY_CERTIFICATE_VERSION,
        FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS,
        strict_feasibility_certificate_version,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Load a research-agent state JSON, extract its macro_plan, and run "
            "the device adaptation workflow."
        )
    )
    parser.add_argument(
        "--contract-version",
        choices=["v1", "v2"],
        default="v2",
        help="Internal agent contract. Default: v2; use v1 for compatibility rollback.",
    )
    parser.add_argument(
        "--research-state",
        required=True,
        help="Path to the saved research agent JSON state.",
    )
    parser.add_argument(
        "--output",
        help="Optional path for saving the full device-agent state JSON.",
    )
    parser.add_argument(
        "--package-output",
        help="Optional path for saving only the terminal package JSON.",
    )
    parser.add_argument(
        "--checkpoint-dir",
        help=(
            "Directory for atomic Device semantic/feasibility checkpoints. "
            "Matching completed fragments resume automatically. Defaults to "
            "the repository-stable ignored result/device_checkpoints cache."
        ),
    )
    parser.add_argument(
        "--no-resume-checkpoints",
        action="store_true",
        help="Save fresh checkpoints without reusing completed stages.",
    )
    parser.add_argument(
        "--device-plan-override",
        help=(
            "Validated manual Device-plan override JSON. Used only with a frozen "
            "Device repair request; it never changes the Research macro route."
        ),
    )
    parser.add_argument(
        "--prior-repair-request",
        help=(
            "The device_repair_request.json that authorized --device-plan-override. "
            "The campaign orchestrator validates its frozen signatures before this "
            "entry point is invoked."
        ),
    )
    parser.add_argument(
        "--exp-id",
        help=(
            "Optional explicit local experiment identifier. Useful for concurrent "
            "planning runs; this does not create or dispatch a real laboratory task."
        ),
    )
    parser.add_argument(
        "--human-readable-output",
        help=(
            "Optional path for a unified user-readable extraction "
            "(issue 2 human_readable_result.json). Read-only; never mutates "
            "the research state or device package."
        ),
    )
    parser.add_argument(
        "--model-name",
        help="Optional model name override.",
    )
    parser.add_argument(
        "--api-key",
        help="Optional API key override. Prefer REFINER_LLM_API_KEY for shell history safety.",
    )
    parser.add_argument(
        "--base-url",
        help="Optional OpenAI-compatible endpoint URL override.",
    )
    parser.add_argument(
        "--wire-api",
        choices=["chat", "codex_responses"],
        default="chat",
        help="LLM wire API. Use codex_responses for the gpt-5.5 Codex-compatible endpoint.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="xhigh",
        help="Reasoning effort used when --wire-api codex_responses. Default: xhigh.",
    )
    parser.add_argument(
        "--workstations-dir",
        help=(
            "Optional path to workstation truth source. Supports the old "
            "workstations_new layout and the current lab-design-all layout."
        ),
    )
    parser.add_argument(
        "--full-workstations",
        action="store_true",
        help=(
            "Include the full loaded workstation truth source in the device-agent "
            "prompt instead of selecting only relevant workstations."
        ),
    )
    parser.add_argument(
        "--workflow-verification",
        choices=["llm", "deterministic"],
        default=None,
        help=(
            "Final workflow gate. Default: llm (review against complete workstation "
            "Skills through the Device-local repair state machine). Use deterministic "
            "to roll back to the "
            "legacy schema/formatter checks."
        ),
    )
    parser.add_argument(
        "--device-status-json",
        help=(
            "Optional JSON file with live station availability "
            '({"stations": {"<name-or-code>": "available|busy|offline"}} or a flat '
            "map). Unavailable stations are flagged in the prompt and must not be "
            "selected."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="LLM timeout seconds. Default: 600.",
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=8,
        help="Maximum LLM retries for the Device Agent and SDK transport. Default: 8.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help=(
            "Max completion tokens for each device-agent LLM call. Default: "
            "32768 (Responses counts reasoning and visible workflow JSON against "
            "the same limit; long multi-step workflows can truncate below this)."
        ),
    )
    parser.add_argument(
        "--print-package-json",
        action="store_true",
        help="Print the terminal package JSON.",
    )
    parser.add_argument(
        "--print-macro-plan",
        action="store_true",
        help="Print the extracted macro_plan before running device agent.",
    )
    parser.add_argument(
        "--relationship-bindings",
        help=(
            "Optional frozen, manually evidence-bound V2 material-operation "
            "authority JSON with automation_claim=false. Caller-supplied automated "
            "authorities are rejected; omit this option to use the internal "
            "deterministic resolver. The authority remains separate from the "
            "canonical Research handoff and is revalidated against the generated "
            "Device candidate and current workstation-truth digest before any "
            "relationship is compiled."
        ),
    )
    parser.add_argument(
        "--diagnostic-workflow-on-material-block",
        action="store_true",
        help=(
            "If a V2 material relationship audit stops the Device Plan, start "
            "an isolated workflow preview and its automatic scoped repair "
            "handoff from frozen saved files. This never accepts the Plan, "
            "issues a certificate, or dispatches a laboratory task."
        ),
    )
    parser.add_argument(
        "--diagnostic-workflow-output-dir",
        help="Fresh directory for the opt-in, non-dispatching workflow diagnostic.",
    )
    return parser


def load_json_object(path_text: str) -> Dict[str, Any]:
    path = Path(path_text).expanduser()
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"research-state must be a JSON object: {path}")
    return data


def validate_repair_contract_boundary(
    request: Dict[str, Any],
    override: Dict[str, Any],
    runtime_contract_version: str,
) -> None:
    """Fail closed if a frozen repair crosses its selected contract path."""

    request_version = str(request.get("contract_version") or "").strip()
    override_version = str(override.get("contract_version") or "").strip()
    resolution = request.get("contract_resolution")
    resolution = resolution if isinstance(resolution, dict) else {}
    if request_version not in {"v1", "v2"}:
        raise SystemExit(
            "Device repair request lacks a valid frozen contract_version"
        )
    if override_version != request_version:
        raise SystemExit(
            "Device repair override contract_version does not match its request"
        )
    if runtime_contract_version != request_version:
        raise SystemExit(
            "runtime --contract-version does not match the frozen Device repair request"
        )
    if (
        str(resolution.get("requested") or "").strip() != request_version
        or str(resolution.get("effective") or "").strip() != request_version
        or resolution.get("requested_matches_effective") is not True
    ):
        raise SystemExit(
            "Device repair request contract_resolution is missing or inconsistent"
        )
    certificate = request.get("feasibility_certificate")
    certificate = certificate if isinstance(certificate, dict) else {}
    certificate_contract_version = str(
        certificate.get("contract_version") or ""
    ).strip()
    certificate_version = strict_feasibility_certificate_version(certificate)
    if (
        certificate_version in FULL_PLAN_CONTRACT_CERTIFICATE_VERSIONS
        and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
    ):
        raise SystemExit(
            "obsolete full-contract feasibility_certificate requires re-audit "
            f"and version {FEASIBILITY_CERTIFICATE_VERSION} re-issuance"
        )
    if (
        request_version == "v2"
        and certificate_version != FEASIBILITY_CERTIFICATE_VERSION
    ):
        raise SystemExit(
            "V2 repair request feasibility_certificate version is unsupported or missing"
        )
    if request_version == "v2" and certificate_contract_version != "v2":
        raise SystemExit(
            "V2 repair request feasibility_certificate lacks its frozen contract_version"
        )
    if (
        certificate_contract_version
        and certificate_contract_version != request_version
    ):
        raise SystemExit(
            "feasibility_certificate contract_version does not match the repair request"
        )


def extract_macro_plan(research_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    handoff = research_state.get("device_adaptation_handoff")
    if isinstance(handoff, dict):
        macro_plan = (
            handoff.get("待执行 macro plan")
            or handoff.get("macro_plan")
            or handoff.get("macro_action_steps")
        )
        if isinstance(macro_plan, list) and macro_plan:
            return macro_plan

    external_handoff = research_state.get("B. 发给下游 device adaptation layer agent 的外部交接输出")
    if isinstance(external_handoff, dict):
        macro_plan = (
            external_handoff.get("待执行 macro plan")
            or external_handoff.get("macro_plan")
            or external_handoff.get("macro_action_steps")
        )
        if isinstance(macro_plan, list) and macro_plan:
            return macro_plan

    macro_plan = research_state.get("macro_plan")
    if isinstance(macro_plan, list) and macro_plan:
        return macro_plan

    macro_action_steps = research_state.get("macro_action_steps")
    if isinstance(macro_action_steps, list) and macro_action_steps:
        return macro_action_steps

    persistent_outputs = research_state.get("persistent_outputs")
    if isinstance(persistent_outputs, dict):
        macro_plan = (
            persistent_outputs.get("待执行 macro plan")
            or persistent_outputs.get("macro_plan")
            or persistent_outputs.get("macro_action_steps")
        )
        if isinstance(macro_plan, list) and macro_plan:
            return macro_plan

    raise SystemExit(
        "Could not find a non-empty macro_plan in research state. Expected one of: "
        "device_adaptation_handoff['待执行 macro plan'], "
        "B. 发给下游 device adaptation layer agent 的外部交接输出['待执行 macro plan'], "
        "top-level macro_plan/macro_action_steps, or "
        "persistent_outputs['待执行 macro plan']."
    )


def first_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def truncate(value: Any, max_chars: int = 2400) -> Any:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if len(text) <= max_chars:
        return value
    return text[:max_chars] + "...[truncated]"


def summarize_knowledge_hits(hits: Any, limit: int = 5) -> List[Dict[str, Any]]:
    if not isinstance(hits, list):
        return []
    summarized: List[Dict[str, Any]] = []
    for hit in hits[:limit]:
        if not isinstance(hit, dict):
            continue
        summarized.append(
            {
                "title": hit.get("title", ""),
                "file_path": hit.get("file_path", ""),
                "score": hit.get("score", 0),
                "problem": truncate(hit.get("problem", ""), max_chars=500),
                "synthesis_summary": truncate(
                    hit.get("synthesis_summary", ""),
                    max_chars=700,
                ),
                "experiment_details": truncate(
                    hit.get("experiment_details", ""),
                    max_chars=900,
                ),
                "steps": truncate(hit.get("steps", [])[:8], max_chars=1200)
                if isinstance(hit.get("steps", []), list)
                else [],
            }
        )
    return summarized


def build_device_agent_input_package(
    research_state: Dict[str, Any],
    macro_plan: List[Dict[str, Any]],
    *,
    canonical_v2_package: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    persistent = first_dict(
        research_state.get("persistent_outputs"),
        research_state.get("A. research layer 内部持久化输出"),
    )
    handoff = first_dict(
        research_state.get("device_adaptation_handoff"),
        research_state.get("B. 发给下游 device adaptation layer agent 的外部交接输出"),
    )
    event = first_dict(research_state.get("event"))

    survey_report = first_non_empty(
        research_state.get("survey_report"),
        persistent.get("调研报告"),
    )
    extracted_protocols = first_non_empty(
        research_state.get("extracted_protocols"),
        persistent.get("从知识库论文抽取的实验过程"),
    )
    macro_action = first_dict(
        research_state.get("macro_action"),
        handoff.get("当前 macro action"),
        persistent.get("当前 macro action"),
    )

    package = {
        "contract_version": str(research_state.get("contract_version") or "v1"),
        "campaign_id": str(research_state.get("campaign_id") or ""),
        "handoff_type": "research_to_device_adaptation",
        "task": {
            "query": first_non_empty(
                handoff.get("query"),
                event.get("query"),
                research_state.get("query"),
            ),
            "stage_route": first_non_empty(
                handoff.get("stage 路线"),
                research_state.get("stage_route"),
                persistent.get("stage 路线"),
            ),
            "current_stage": first_non_empty(
                handoff.get("当前 stage"),
                research_state.get("current_stage"),
                persistent.get("当前 stage"),
            ),
            "current_stage_plan": first_non_empty(
                handoff.get("当前 stage 的完整化学语义实验计划"),
                research_state.get("current_stage_plan"),
                persistent.get("当前 stage 的完整化学语义实验计划"),
            ),
            "stage_route_reason": first_non_empty(
                handoff.get("stage路线设计理由"),
                research_state.get("stage_route_reason"),
                persistent.get("stage路线设计理由"),
            ),
            "current_stage_reason": first_non_empty(
                handoff.get("当前stage设计理由"),
                research_state.get("current_stage_reason"),
                persistent.get("当前stage设计理由"),
            ),
        },
        "macro_action_steps": macro_plan,
        "macro_action": macro_action,
        "current_evidence_bundle": copy.deepcopy(
            research_state.get("current_evidence_bundle") or {}
        ),
        # Quantity-bearing observations are immutable scientific evidence for
        # Device state-change yield checks.  Keep the structured records (and
        # their IDs/sample/material fields) intact; the truncated narrative
        # latest_observation below is display context only and must never fund
        # inventory.
        "observations": copy.deepcopy(
            research_state.get("observations", [])
            if isinstance(research_state.get("observations"), list)
            else []
        ),
        "research_context": {
            "survey_report": truncate(survey_report, max_chars=2600),
            "extracted_paper_protocols": truncate(extracted_protocols, max_chars=2600),
            "knowledge_hits": summarize_knowledge_hits(
                research_state.get("knowledge_hits", []),
                limit=5,
            ),
            "latest_observation": truncate(
                first_non_empty(
                    research_state.get("latest_observation"),
                    persistent.get("最新 observation"),
                ),
                max_chars=1600,
            ),
        },
        "device_agent_contract": {
            "research_macro_action_level": (
                "The macro_action_steps are chemical-semantic experiment actions. "
                "They specify what experiment to do and key chemical parameters, but "
                "they may intentionally omit concrete machine containers, workstation "
                "names, container IDs, reagent bottle slots, lid operations, balancing, "
                "and repeated device sub-steps."
            ),
            "device_agent_responsibilities": [
                "Select concrete supported containers and workstations from the loaded workstation truth source.",
                "Map liquid sources to reagent/original-solution bottle slots when appropriate.",
                "Expand macro actions into device-level operations such as lid handling, aliquoting, balancing, repeated purification, drying, and handoff notes.",
                "Treat workstation transport and material transfer as connected platform defaults. Preserve one sample/container lineage across reaction, aging/resting, purification, washing, drying, and testing; introduce a vessel change only when an operation input, capacity, split/merge, output format, or scientific condition requires it, and use the minimum supported transfer steps.",
                "Return device_feasibility_error only when no legal equipment/container/workstation mapping can realize the chemical action or a mandatory scientific condition is impossible.",
            ],
            "do_not_return_to_research_for": [
                "missing container IDs",
                "missing workstation names",
                "missing reagent bottle slot numbers",
                "missing open/close lid actions",
                "missing even-vial balancing details",
                "missing split/repeated wash substeps",
            ],
            "must_return_to_research_for": [
                "macro routes whose required chemical operation, target input state, capacity/safety range, or online workstation remains impossible after searching all workstation Skills and the connected transfer fabric",
            ],
        },
    }
    canonical = first_dict(
        canonical_v2_package,
        research_state.get("research_action_package_v2"),
        handoff.get("research_action_package_v2"),
    )
    if str(package["contract_version"]).strip().lower() == "v2" and not canonical:
        raise ValueError(
            "V2 Device input construction requires a validated canonical Research package"
        )
    if canonical:
        package["research_action_package_v2"] = copy.deepcopy(canonical)
        package["contract_version"] = "v2"
        # V2 callers receive only the signed package projection plus the two
        # digest-verified raw collections.  Do not let this compatibility
        # builder reintroduce unbound task/context/contract mirrors.
        from chem_agent_contracts.v2 import canonicalize_v2_device_handoff

        package = canonicalize_v2_device_handoff(package, package=canonical)
    return package


def validate_v2_research_handoff_consistency(
    research_state: Dict[str, Any],
    selected_macro_plan: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return one validated canonical package or fail on split-brain mirrors.

    A normal Research state persists the raw macro plan and canonical V2
    package at the top level and in its handoff snapshots.  Device historically
    selected the raw plan from the handoff first but the canonical package from
    the top level first, so a partially rewritten/restored file could make the
    generator and auditor consume different objects.  Compare every mirror
    that is actually present, then rebuild the canonical package from the
    top-level source when that normal source is available.

    Historical handoff-only states remain supported without requiring a
    synthetic top-level mirror, but any raw plan they carry is rebuilt through
    the same adapter and must agree with every canonical package mirror.  Raw
    and canonical plans are never allowed to feed generation and audit as two
    independent authorities.
    """

    from chem_agent_contracts.adapters import research_state_to_v2
    from chem_agent_contracts.v2 import (
        ResearchActionPackageV2,
        validate_raw_steps_against_canonical,
    )

    for container_key in (
        "device_adaptation_handoff",
        "persistent_outputs",
        "A. research layer 内部持久化输出",
        "B. 发给下游 device adaptation layer agent 的外部交接输出",
    ):
        if container_key in research_state and not isinstance(
            research_state[container_key], dict
        ):
            raise SystemExit(
                f"invalid V2 Research handoff container {container_key}: "
                "expected an object"
            )

    handoff = first_dict(research_state.get("device_adaptation_handoff"))
    external_handoff = first_dict(
        research_state.get(
            "B. 发给下游 device adaptation layer agent 的外部交接输出"
        )
    )
    persistent = first_dict(research_state.get("persistent_outputs"))
    internal_persistent = first_dict(
        research_state.get("A. research layer 内部持久化输出")
    )

    raw_plan_mirrors: List[tuple[str, List[Dict[str, Any]]]] = []
    raw_plan_locations = (
        ("macro_plan", research_state, "macro_plan"),
        ("macro_action_steps", research_state, "macro_action_steps"),
        (
            "device_adaptation_handoff.待执行 macro plan",
            handoff,
            "待执行 macro plan",
        ),
        ("device_adaptation_handoff.macro_plan", handoff, "macro_plan"),
        (
            "device_adaptation_handoff.macro_action_steps",
            handoff,
            "macro_action_steps",
        ),
        (
            "external_handoff.待执行 macro plan",
            external_handoff,
            "待执行 macro plan",
        ),
        ("external_handoff.macro_plan", external_handoff, "macro_plan"),
        (
            "external_handoff.macro_action_steps",
            external_handoff,
            "macro_action_steps",
        ),
        (
            "persistent_outputs.待执行 macro plan",
            persistent,
            "待执行 macro plan",
        ),
        ("persistent_outputs.macro_plan", persistent, "macro_plan"),
        (
            "persistent_outputs.macro_action_steps",
            persistent,
            "macro_action_steps",
        ),
        (
            "internal_persistent_outputs.待执行 macro plan",
            internal_persistent,
            "待执行 macro plan",
        ),
        (
            "internal_persistent_outputs.macro_plan",
            internal_persistent,
            "macro_plan",
        ),
        (
            "internal_persistent_outputs.macro_action_steps",
            internal_persistent,
            "macro_action_steps",
        ),
    )
    for label, container, key in raw_plan_locations:
        if key not in container:
            continue
        value = container[key]
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, dict) for item in value)
        ):
            raise SystemExit(
                f"invalid V2 Research raw macro-plan mirror {label}: expected "
                "a non-empty array of step objects"
            )
        raw_plan_mirrors.append((label, value))

    def canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    selected_json = canonical_json(selected_macro_plan)
    for label, value in raw_plan_mirrors:
        if canonical_json(value) != selected_json:
            raise SystemExit(
                "V2 Research handoff has divergent raw macro-plan mirrors: "
                f"selected plan does not match {label}"
            )

    canonical_mirrors: List[tuple[str, Dict[str, Any]]] = []
    canonical_locations = (
        (
            "research_action_package_v2",
            research_state,
            "research_action_package_v2",
        ),
        (
            "device_adaptation_handoff.research_action_package_v2",
            handoff,
            "research_action_package_v2",
        ),
        (
            "external_handoff.research_action_package_v2",
            external_handoff,
            "research_action_package_v2",
        ),
        (
            "persistent_outputs.research_action_package_v2",
            persistent,
            "research_action_package_v2",
        ),
        (
            "internal_persistent_outputs.research_action_package_v2",
            internal_persistent,
            "research_action_package_v2",
        ),
    )
    for label, container, key in canonical_locations:
        if key not in container:
            continue
        value = container[key]
        if not isinstance(value, dict) or not value:
            raise SystemExit(
                f"invalid V2 Research canonical package mirror {label}: "
                "expected a non-empty object"
            )
        if not str(value.get("research_contract_hash") or "").strip():
            raise SystemExit(
                f"invalid V2 Research canonical package mirror {label}: "
                "missing research_contract_hash"
            )
        canonical_mirrors.append((label, value))

    parsed_mirrors: List[tuple[str, ResearchActionPackageV2]] = []
    for label, value in canonical_mirrors:
        try:
            parsed_mirrors.append(
                (label, ResearchActionPackageV2.model_validate(value))
            )
        except Exception as exc:
            raise SystemExit(f"invalid {label}: {exc}") from exc

    if parsed_mirrors:
        expected_hash = parsed_mirrors[0][1].research_contract_hash
        for label, package in parsed_mirrors[1:]:
            if package.research_contract_hash != expected_hash:
                raise SystemExit(
                    "V2 Research handoff has divergent canonical package mirrors: "
                    f"{label} does not match {parsed_mirrors[0][0]}"
                )

        canonical_package = parsed_mirrors[0][1]
        if raw_plan_mirrors:
            try:
                validate_raw_steps_against_canonical(
                    selected_macro_plan, canonical_package
                )
            except Exception as exc:
                raise SystemExit(
                    f"V2 Research raw/canonical macro step binding failed: {exc}"
                ) from exc

    if raw_plan_mirrors:
        try:
            rebuild_source = copy.deepcopy(research_state)
            # The adapter's canonical source is explicit here.  Alias
            # locations were already checked byte-for-byte above, so this
            # avoids one extraction order feeding generation while another
            # feeds the hash rebuild.
            rebuild_source["macro_plan"] = copy.deepcopy(selected_macro_plan)
            rebuilt = research_state_to_v2(rebuild_source)
        except Exception as exc:
            raise SystemExit(
                f"cannot rebuild V2 Research package from persisted state: {exc}"
            ) from exc
        if parsed_mirrors and (
            rebuilt.research_contract_hash
            != parsed_mirrors[0][1].research_contract_hash
        ):
            raise SystemExit(
                "V2 Research canonical package does not match the persisted raw "
                "macro plan/action/evidence state"
            )
        return (
            parsed_mirrors[0][1].model_dump(mode="json", exclude_none=True)
            if parsed_mirrors
            else rebuilt.model_dump(mode="json", exclude_none=True)
        )

    if parsed_mirrors:
        return parsed_mirrors[0][1].model_dump(mode="json", exclude_none=True)

    # Compatibility fallback for a package-only V2 state that predates the
    # embedded canonical package.  In ordinary execution extract_macro_plan()
    # has already required a raw plan, so this branch is primarily available
    # to callers that validate an isolated authority object.  The adapter
    # preserves missing material completeness as unresolved; it never guesses
    # relationships here.
    try:
        return research_state_to_v2(research_state).model_dump(
            mode="json", exclude_none=True
        )
    except Exception as exc:
        raise SystemExit(f"cannot build V2 Research package from handoff: {exc}") from exc


def device_input_package_to_text(package: Dict[str, Any]) -> str:
    printable = copy.deepcopy(package)
    validation = printable.get("human_quantity_approval_validation")
    if isinstance(validation, dict):
        validation.pop("approval_capability_token", None)
    return json.dumps(printable, ensure_ascii=False, indent=2)


def configure_model_env(args: argparse.Namespace) -> None:
    if args.model_name:
        os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    if args.api_key:
        os.environ["REFINER_LLM_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    os.environ["REFINER_LLM_MAX_RETRIES"] = str(max(0, args.llm_max_retries))
    if args.wire_api == "codex_responses":
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
        os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
    if args.workstations_dir:
        os.environ["CHEM_WORKSTATIONS_NEW_DIR"] = str(Path(args.workstations_dir).expanduser())
    if args.full_workstations:
        os.environ["CHEM_DEVICE_AGENT_FULL_WORKSTATIONS"] = "1"
    if args.workflow_verification:
        os.environ["CHEM_DEVICE_WORKFLOW_VERIFICATION"] = args.workflow_verification
    if getattr(args, "device_status_json", None):
        os.environ["CHEM_DEVICE_STATUS_JSON"] = str(
            Path(args.device_status_json).expanduser()
        )


def dump_json(path_text: str | None, data: Any) -> None:
    if not path_text:
        return
    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def default_checkpoint_dir(_research_state_path: str) -> str:
    """Keep retries stable across timestamped Research and output paths.

    DeviceCheckpointStore partitions this shared ignored cache by the full
    Research-content binding, so a moved-but-identical handoff can resume while
    a changed campaign cannot.
    """
    return str((REPO_ROOT / "result" / "device_checkpoints").resolve())


def main() -> int:
    args = build_parser().parse_args()
    if args.diagnostic_workflow_on_material_block and (
        args.contract_version != "v2" or not args.output or not args.package_output
    ):
        raise SystemExit(
            "--diagnostic-workflow-on-material-block requires V2, --output, "
            "and --package-output so the blocked source files can be frozen"
        )
    os.environ["CHEM_LLM_COMPONENT"] = "device"
    configure_model_env(args)

    from utils.llm_factory import LLMFactory
    from single_agent import SingleDeviceAgent

    research_state = load_json_object(args.research_state)
    device_plan_override = (
        load_json_object(args.device_plan_override)
        if args.device_plan_override
        else None
    )
    prior_repair_request = (
        load_json_object(args.prior_repair_request)
        if args.prior_repair_request
        else None
    )
    relationship_binding_authority = (
        load_json_object(args.relationship_bindings)
        if args.relationship_bindings
        else None
    )
    if (
        relationship_binding_authority is not None
        and args.contract_version != "v2"
    ):
        raise SystemExit("--relationship-bindings requires --contract-version v2")
    if relationship_binding_authority is not None and (
        relationship_binding_authority.get("authoring_mode")
        != "manual_evidence_bound"
        or relationship_binding_authority.get("automation_claim") is not False
    ):
        raise SystemExit(
            "--relationship-bindings accepts only a manual_evidence_bound "
            "authority with automation_claim=false; omit the sidecar to use the "
            "internal automated resolver"
        )
    if bool(device_plan_override) != bool(prior_repair_request):
        raise SystemExit(
            "--device-plan-override and --prior-repair-request must be provided together"
        )
    if device_plan_override and prior_repair_request:
        validate_repair_contract_boundary(
            prior_repair_request,
            device_plan_override,
            args.contract_version,
        )
    approval_validation: Dict[str, Any] = {}
    validated_human_quantity_approvals: List[Dict[str, Any]] = []
    human_quantity_approval_bundle = None
    if device_plan_override and prior_repair_request:
        try:
            (
                device_plan_override,
                approval_validation,
                validated_human_quantity_approvals,
                human_quantity_approval_bundle,
            ) = validate_human_quantity_approvals(
                device_plan_override,
                prior_repair_request,
                repair_request_sha256=approval_request_sha256(
                    Path(args.prior_repair_request).expanduser()
                ),
                create_runtime_bundle=True,
            )
        except (HumanQuantityApprovalError, OSError) as exc:
            raise SystemExit(
                f"invalid human quantity approval in Device repair override: {exc}"
            ) from exc
    macro_plan = extract_macro_plan(research_state)
    validated_v2_research_package: Dict[str, Any] | None = None
    if args.contract_version == "v2":
        validated_v2_research_package = validate_v2_research_handoff_consistency(
            research_state,
            macro_plan,
        )
    device_input_package = build_device_agent_input_package(
        research_state,
        macro_plan,
        canonical_v2_package=validated_v2_research_package,
    )
    source_contract_version = str(
        device_input_package.get("contract_version")
        or research_state.get("contract_version")
        or ""
    ).strip()
    if args.contract_version == "v2":
        device_input_package["contract_version"] = "v2"
        device_input_package["research_action_package_v2"] = copy.deepcopy(
            validated_v2_research_package
        )
    else:
        # A canonical V2 source may be intentionally replayed through the V1
        # compatibility path.  Preserve its source version as evidence, but do
        # not let input metadata override the explicitly selected runtime.
        device_input_package["contract_version"] = "v1"
    device_input_package["contract_resolution"] = {
        "requested": args.contract_version,
        "source_input": source_contract_version,
        "effective": args.contract_version,
        "requested_matches_effective": True,
        "source_matches_effective": (
            not source_contract_version
            or source_contract_version == args.contract_version
        ),
    }
    if device_plan_override:
        device_input_package["device_repair_resume"] = {
            "request_id": prior_repair_request.get("request_id", ""),
            "frozen_route_signature": prior_repair_request.get(
                "frozen_route_signature", ""
            ),
            "frozen_sample_matrix_signature": prior_repair_request.get(
                "frozen_sample_matrix_signature", ""
            ),
            "device_snapshot_signature": prior_repair_request.get(
                "device_snapshot_signature", ""
            ),
            "human_quantity_approval_count": len(
                validated_human_quantity_approvals
            ),
        }
    macro_plan_text = device_input_package_to_text(device_input_package)
    checkpoint_dir = args.checkpoint_dir or default_checkpoint_dir(
        args.research_state
    )

    print(
        "starting device agent: "
        f"research_state={args.research_state}, macro_steps={len(macro_plan)}, "
        "mode=single_agent, "
        f"contract={args.contract_version}, "
        f"model={args.model_name or os.getenv('REFINER_LLM_MODEL_NAME', 'env/default')}, "
        f"wire_api={args.wire_api}, "
        f"workstations_dir={args.workstations_dir or os.getenv('CHEM_WORKSTATIONS_NEW_DIR', 'default')}, "
        f"checkpoint_dir={checkpoint_dir}, "
        f"resume_checkpoints={not args.no_resume_checkpoints}",
        flush=True,
    )
    if args.print_macro_plan:
        print(macro_plan_text, flush=True)

    model = LLMFactory.create(
        model_name=args.model_name,
        api_key=args.api_key,
        endpoint_url=args.base_url,
        timeout=args.timeout_seconds,
        max_tokens=args.max_tokens,
    )
    workflow = SingleDeviceAgent(
        model=model,
        use_new_format=True,
        contract_version=args.contract_version,
    )
    run_kwargs: Dict[str, Any] = {"exp_id": args.exp_id}
    run_kwargs.update(
        {
            "checkpoint_dir": checkpoint_dir,
            "resume_checkpoints": not args.no_resume_checkpoints,
        }
    )
    if device_plan_override:
        run_kwargs.update(
            {
                "device_plan_override": device_plan_override,
                "prior_repair_request": prior_repair_request,
            }
        )
        if human_quantity_approval_bundle is not None:
            run_kwargs["human_quantity_approval_bundle"] = (
                human_quantity_approval_bundle
            )
    if relationship_binding_authority is not None:
        run_kwargs["relationship_binding_authority"] = (
            relationship_binding_authority
        )
    state = workflow.run_state(device_input_package, **run_kwargs)
    state_dict = state.to_dict()
    package = state.terminal_package or {}
    status = state.status
    verification_result = (
        "refused"
        if package.get("status") in {
            "feasibility_error",
            "terminal_unmappable",
            "failed",
            "manual_required",
        }
        else "accepted"
    )
    error_package = (
        package.get("error_package")
        if isinstance(package.get("error_package"), dict)
        else {}
    )
    if package.get("status") in {"feasibility_error", "terminal_unmappable"}:
        verification_category = str(
            error_package.get("type") or "physical_infeasible"
        )
    elif package.get("status") == "manual_required":
        verification_category = str(
            error_package.get("type") or "human_review_required"
        )
    elif package.get("status") == "failed":
        verification_category = str(package.get("failure_stage") or "device_internal_error")
    else:
        verification_category = ""
    exp_id = state.exp_id
    blocking_constraints = error_package.get("blocking_constraints", [])

    dump_json(args.output, state_dict)
    dump_json(args.package_output, package)
    diagnostic_exit_code = 0
    if args.diagnostic_workflow_on_material_block:
        structured_errors = error_package.get("structured_errors")
        material_blocked = (
            package.get("status") == "manual_required"
            and isinstance(structured_errors, list)
            and any(
                isinstance(item, dict)
                and item.get("type") == "binding_ledger_unresolved"
                for item in structured_errors
            )
        )
        if material_blocked:
            diagnostic_dir = (
                Path(args.diagnostic_workflow_output_dir).expanduser().resolve()
                if args.diagnostic_workflow_output_dir
                else Path(args.package_output).expanduser().resolve().parent
                / ("device-workflow-diagnostic-" + uuid4().hex)
            )
            try:
                from diagnostic_from_blocked_run import run_from_blocked_files

                diagnostic = run_from_blocked_files(
                    research_state_path=Path(args.research_state).expanduser().resolve(),
                    device_state_path=Path(args.output).expanduser().resolve(),
                    package_path=Path(args.package_output).expanduser().resolve(),
                    output_dir=diagnostic_dir,
                    model_name=args.model_name or os.getenv("REFINER_LLM_MODEL_NAME", "kimi-k3"),
                    endpoint_url=args.base_url or os.getenv(
                        "REFINER_LLM_ENDPOINT_URL", "https://api.kimi.com/coding/v1"
                    ),
                    reasoning_effort=args.reasoning_effort,
                    timeout_seconds=args.timeout_seconds,
                    max_tokens=args.max_tokens,
                )
                diagnostic_status = str(diagnostic.get("status") or "unknown")
                diagnostic_reason = str(diagnostic.get("reason") or "")
                print(
                    "device workflow diagnostic: "
                    f"status={diagnostic_status}, reason={diagnostic_reason or 'none'}, "
                    f"output={diagnostic_dir}; "
                    "production Device package remains blocked",
                    flush=True,
                )
                if diagnostic_status != "diagnostic_blocked":
                    diagnostic_exit_code = 2
            except Exception as exc:
                # Provider errors may include credential-bearing request text.
                # Never print or persist the raw exception.
                print(
                    "device workflow diagnostic preparation failed: "
                    f"{type(exc).__name__}; production Device package remains blocked",
                    flush=True,
                )
                diagnostic_exit_code = 2
    if args.human_readable_output:
        try:
            import sys as _sys

            repo_root = str(Path(__file__).resolve().parents[1])
            if repo_root not in _sys.path:
                _sys.path.insert(0, repo_root)
            from orchestrator.human_readable import build_human_readable_result

            dump_json(
                args.human_readable_output,
                build_human_readable_result(research_state, package),
            )
            print(f"saved human readable result: {args.human_readable_output}", flush=True)
        except Exception as exc:  # extraction is optional; never fail the run
            print(f"human readable result generation failed: {exc}", flush=True)

    print(
        "device agent completed: "
        f"status={status}, verification={verification_result}/"
        f"{verification_category or 'none'}, exp_id={exp_id}",
        flush=True,
    )
    dispatch_formatting = package.get("dispatch_formatting")
    if isinstance(dispatch_formatting, dict):
        print(
            "dispatch formatting: "
            f"mapped={dispatch_formatting.get('mapped_steps', 0)}, "
            f"unmapped={dispatch_formatting.get('unmapped_steps', 0)}, "
            f"warnings={len(dispatch_formatting.get('warnings', []) or [])}",
            flush=True,
        )
    if blocking_constraints:
        print("blocking_constraints:", flush=True)
        for item in blocking_constraints:
            print(f"- {item}", flush=True)
    if args.output:
        print(f"saved device state: {args.output}", flush=True)
    if args.package_output:
        print(f"saved terminal package: {args.package_output}", flush=True)
    if args.print_package_json:
        print(json.dumps(package, ensure_ascii=False, indent=2), flush=True)

    return diagnostic_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
