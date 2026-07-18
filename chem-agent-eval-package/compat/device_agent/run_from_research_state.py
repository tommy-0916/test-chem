"""Run device agent from a saved research-agent state JSON."""

from __future__ import annotations

import os
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Load a research-agent state JSON, extract its macro_plan, and run "
            "the device adaptation workflow."
        )
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
            "workstations_new layout and the new lab-design-main layout."
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
        "--max-tokens",
        type=int,
        default=16384,
        help=(
            "Max completion tokens for each device-agent LLM call. Default: "
            "16384 (multi-step workflows with strict validation easily exceed "
            "the old 4096 and would truncate into JSON parse failures)."
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

    return {
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
                "Maintain one continuous, supported container path across reaction, aging/resting, purification, washing, drying, and testing; if a vessel change is required, it must be backed by an explicit supported transfer/vessel-change operation.",
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
                "macro routes that require an unsupported transfer or vessel change between incompatible container classes",
            ],
        },
    }


def device_input_package_to_text(package: Dict[str, Any]) -> str:
    return json.dumps(package, ensure_ascii=False, indent=2)


def configure_model_env(args: argparse.Namespace) -> None:
    if args.model_name:
        os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    if args.api_key:
        os.environ["REFINER_LLM_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    if args.wire_api == "codex_responses":
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
        os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
    if args.workstations_dir:
        os.environ["CHEM_WORKSTATIONS_NEW_DIR"] = str(Path(args.workstations_dir).expanduser())
    if args.full_workstations:
        os.environ["CHEM_DEVICE_AGENT_FULL_WORKSTATIONS"] = "1"
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


def main() -> int:
    args = build_parser().parse_args()
    configure_model_env(args)

    from utils.llm_factory import LLMFactory
    from single_agent import SingleDeviceAgent

    research_state = load_json_object(args.research_state)
    macro_plan = extract_macro_plan(research_state)
    device_input_package = build_device_agent_input_package(research_state, macro_plan)
    macro_plan_text = device_input_package_to_text(device_input_package)

    print(
        "starting device agent: "
        f"research_state={args.research_state}, macro_steps={len(macro_plan)}, "
        "mode=single_agent, "
        f"model={args.model_name or os.getenv('REFINER_LLM_MODEL_NAME', 'env/default')}, "
        f"wire_api={args.wire_api}, "
        f"workstations_dir={args.workstations_dir or os.getenv('CHEM_WORKSTATIONS_NEW_DIR', 'default')}",
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
    )
    state = workflow.run_state(device_input_package, exp_id=args.exp_id)
    state_dict = state.to_dict()
    package = state.terminal_package or {}
    status = state.status
    verification_result = (
        "refused" if package.get("status") in {"feasibility_error", "failed"} else "accepted"
    )
    error_package = (
        package.get("error_package")
        if isinstance(package.get("error_package"), dict)
        else {}
    )
    if package.get("status") == "feasibility_error":
        verification_category = str(
            error_package.get("type") or "physical_infeasible"
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
