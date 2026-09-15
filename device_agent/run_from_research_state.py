"""Run device agent from a saved research-agent state JSON."""

from __future__ import annotations

import os
import argparse
import copy
import json
import sys
from pathlib import Path
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
    return parser


def load_json_object(path_text: str) -> Dict[str, Any]:
    path = Path(path_text).expanduser()
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"research-state must be a JSON object: {path}")
    return data


def extract_macro_plan(research_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    handoff = research_state.get("device_adaptation_handoff")
    if isinstance(handoff, dict):
        macro_plan = handoff.get("待执行 macro plan") or handoff.get("macro_plan")
        if isinstance(macro_plan, list) and macro_plan:
            return macro_plan

    external_handoff = research_state.get("B. 发给下游 device adaptation layer agent 的外部交接输出")
    if isinstance(external_handoff, dict):
        macro_plan = external_handoff.get("待执行 macro plan") or external_handoff.get("macro_plan")
        if isinstance(macro_plan, list) and macro_plan:
            return macro_plan

    macro_plan = research_state.get("macro_plan")
    if isinstance(macro_plan, list) and macro_plan:
        return macro_plan

    persistent_outputs = research_state.get("persistent_outputs")
    if isinstance(persistent_outputs, dict):
        macro_plan = persistent_outputs.get("待执行 macro plan") or persistent_outputs.get("macro_plan")
        if isinstance(macro_plan, list) and macro_plan:
            return macro_plan

    raise SystemExit(
        "Could not find a non-empty macro_plan in research state. Expected one of: "
        "device_adaptation_handoff['待执行 macro plan'], "
        "B. 发给下游 device adaptation layer agent 的外部交接输出['待执行 macro plan'], "
        "top-level macro_plan, or persistent_outputs['待执行 macro plan']."
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
        research_state.get("research_action_package_v2"),
        handoff.get("research_action_package_v2"),
    )
    if canonical:
        package["research_action_package_v2"] = copy.deepcopy(canonical)
        package["contract_version"] = "v2"
    return package


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
    if bool(device_plan_override) != bool(prior_repair_request):
        raise SystemExit(
            "--device-plan-override and --prior-repair-request must be provided together"
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
    device_input_package = build_device_agent_input_package(research_state, macro_plan)
    if args.contract_version == "v2":
        from chem_agent_contracts.adapters import research_state_to_v2

        canonical = research_state.get("research_action_package_v2")
        if not isinstance(canonical, dict):
            canonical = research_state_to_v2(research_state).model_dump(
                mode="json", exclude_none=True
            )
        device_input_package["contract_version"] = "v2"
        device_input_package["research_action_package_v2"] = copy.deepcopy(canonical)
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
