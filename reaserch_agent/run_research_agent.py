#!/usr/bin/env python3
"""CLI entrypoint for running the research agent bootstrap flow."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent import ResearchAgent
from reaserch_agent.tools import load_device_context
from reaserch_agent.utils.llm_factory import LLMFactory


DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_DEVICE_WORKSTATIONS_DIR = Path(__file__).resolve().parents[1] / "chem_resources" / "workstations_new"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the research agent. Bootstrap and new observation events "
            "are implemented end-to-end."
        )
    )
    parser.add_argument(
        "--event-type",
        default="bootstrap",
        help="Event type to run. Default: bootstrap",
    )
    parser.add_argument(
        "--query",
        help="Human query for the research agent. If omitted, the script will ask interactively.",
    )
    parser.add_argument(
        "--constraints-json",
        default="{}",
        help='JSON object passed as event constraints, e.g. \'{"目标":"先生成首轮macro plan"}\'',
    )
    parser.add_argument(
        "--payload-json",
        default="{}",
        help='JSON object passed as event payload.',
    )
    parser.add_argument(
        "--previous-state",
        help="Path to a saved state JSON used as context for new observation events.",
    )
    parser.add_argument(
        "--observation",
        help=(
            "Convenience text observation. For new observation events, this is stored "
            "as payload.observation.summary unless payload-json already contains one."
        ),
    )
    parser.add_argument(
        "--knowledge-base-dir",
        help="Directory containing knowledge-base PDF or JSON files. Defaults to reaserch_agent/chem_kb.",
    )
    parser.add_argument(
        "--device-workstations-dir",
        help=(
            "Optional workstation description directory passed to B1 as device_context. "
            f"Default when --include-device-context is used: {DEFAULT_DEVICE_WORKSTATIONS_DIR}."
        ),
    )
    parser.add_argument(
        "--device-context-json",
        help="Optional JSON object/string for device_context; overrides --device-workstations-dir.",
    )
    parser.add_argument(
        "--include-device-context",
        action="store_true",
        help="Load current device capabilities into B1 constraints before the first LLM call.",
    )
    parser.add_argument(
        "--memory-dir",
        help="Optional directory for memory retrieval. Defaults to the knowledge base directory.",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable memory retrieval. By default memory is disabled.",
    )
    parser.add_argument(
        "--model-name",
        help="Optional model name. If provided, it will override REFINER_LLM_MODEL_NAME.",
    )
    parser.add_argument(
        "--api-key",
        help="Optional API key. If provided, it will override REFINER_LLM_API_KEY.",
    )
    parser.add_argument(
        "--base-url",
        help="Optional model endpoint URL. If provided, it will override REFINER_LLM_ENDPOINT_URL.",
    )
    parser.add_argument(
        "--wire-api",
        choices=["chat", "codex_responses"],
        default="chat",
        help=(
            "LLM wire API. Use codex_responses for the gpt-5.5 Codex-compatible endpoint."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        default="xhigh",
        help="Reasoning effort used when --wire-api codex_responses. Default: xhigh.",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=240.0,
        help="LLM timeout seconds. Default: 240.",
    )
    parser.add_argument(
        "--disable-llm",
        action="store_true",
        help="Force heuristic mode even if model credentials are configured.",
    )
    parser.add_argument(
        "--max-survey-rounds",
        type=int,
        default=2,
        help="Maximum number of B1 survey rounds. Default: 2",
    )
    parser.add_argument(
        "--knowledge-top-k",
        type=int,
        default=5,
        help="Number of knowledge hits to keep per round. Default: 5",
    )
    parser.add_argument(
        "--memory-top-k",
        type=int,
        default=3,
        help="Number of memory hits to keep. Default: 3",
    )
    parser.add_argument(
        "--print-state-json",
        action="store_true",
        help="Print the full returned state as JSON.",
    )
    parser.add_argument(
        "--save-state",
        help="Optional path for saving the final state JSON.",
    )
    return parser


def parse_json_dict(raw_text: str, label: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} 不是合法 JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} 必须是 JSON object。")
    return parsed


def attach_device_context(args: argparse.Namespace, constraints: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(constraints or {})
    if args.device_context_json:
        parsed = parse_json_dict(args.device_context_json, "device_context_json")
        updated["device_context"] = parsed
        return updated

    if args.include_device_context or args.device_workstations_dir:
        workstations_dir = (
            Path(args.device_workstations_dir).expanduser().resolve()
            if args.device_workstations_dir
            else DEFAULT_DEVICE_WORKSTATIONS_DIR
        )
        updated["device_workstations_dir"] = str(workstations_dir)
        updated["device_context"] = load_device_context(workstations_dir)

    return updated


def configure_model_env(args: argparse.Namespace) -> None:
    if args.model_name:
        os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    if args.api_key:
        os.environ["REFINER_LLM_API_KEY"] = args.api_key
        os.environ["OPENAI_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    os.environ["REFINER_LLM_TIMEOUT_SECONDS"] = str(args.llm_timeout_seconds)
    if args.wire_api == "codex_responses":
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
        os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
    else:
        os.environ.pop("REFINER_LLM_WIRE_API", None)


def get_query(args: argparse.Namespace) -> str:
    if args.query and args.query.strip():
        return args.query.strip()

    normalized_event_type = re.sub(r"[\s\-]+", "_", args.event_type.strip().lower())
    if args.previous_state and normalized_event_type in {
        "new_observation",
        "post_observation",
        "observation",
        "observation_returned",
    }:
        return ""

    query = input("请输入 bootstrap query: ").strip()
    if not query:
        raise SystemExit("query 不能为空。")
    return query


def print_summary(state: Any) -> None:
    print(f"status: {state.status}")
    print(f"current_branch: {state.current_branch}")
    print(f"next_branch: {state.next_branch}")
    print(f"stage_route: {state.stage_route}")
    print(f"current_stage: {state.current_stage}")
    print(f"current_stage_plan: {state.current_stage_plan}")

    if state.knowledge_hits:
        print(f"top_knowledge_hit: {state.knowledge_hits[0].title}")

    print("macro_plan:")
    if not state.macro_plan:
        print("  (empty)")
        return

    for step in state.macro_plan:
        step_no = step.get("步骤序号", "?")
        operation = step.get("操作", "")
        target = step.get("试剂/对象", "")
        parameters = step.get("参数", "")
        print(f"  {step_no}. {operation}")
        print(f"     试剂/对象: {target}")
        print(f"     参数: {parameters}")


def slugify_query(query: str, max_len: int = 48) -> str:
    normalized = re.sub(r"\s+", "_", query.strip())
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "", normalized)
    normalized = normalized.strip("_-")
    return (normalized[:max_len] or "query").strip("_-")


def build_state_json(state: Any) -> str:
    if hasattr(state, "debug_snapshot"):
        payload = state.debug_snapshot()
    else:
        payload = state.to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_previous_state(path_text: str | None) -> Dict[str, Any] | None:
    if not path_text:
        return None
    path = Path(path_text).expanduser().resolve()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"previous-state 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("previous-state 必须是 JSON object。")
    return parsed


def write_debug_log(state: Any, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    created_at = getattr(state, "created_at", "") or ""
    timestamp = created_at.replace(":", "-").replace("T", "_").split(".", 1)[0]
    filename = f"{timestamp}_{slugify_query(state.event.query)}.json"
    output_path = log_dir / filename
    output_path.write_text(build_state_json(state), encoding="utf-8")
    return output_path


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_model_env(args)
    query = get_query(args)
    constraints = attach_device_context(
        args,
        parse_json_dict(args.constraints_json, "constraints_json"),
    )
    payload = parse_json_dict(args.payload_json, "payload_json")
    if args.observation and "observation" not in payload:
        payload["observation"] = {"summary": args.observation.strip()}
    previous_state = load_previous_state(args.previous_state)

    print(
        "starting research agent: "
        f"event_type={args.event_type}, "
        f"model={args.model_name or 'env/default'}, "
        f"wire_api={args.wire_api}, "
        f"device_context={'on' if constraints.get('device_context') else 'off'}, "
        f"memory={'on' if args.enable_memory else 'off'}",
        flush=True,
    )
    if args.wire_api == "codex_responses":
        print(
            "codex_responses is non-streaming here; the terminal may stay quiet "
            "until each LLM step finishes.",
            flush=True,
        )

    model = None
    if not args.disable_llm:
        model = LLMFactory.create_or_none(
            model_name=args.model_name,
            api_key=args.api_key,
            base_url=args.base_url,
        )
        if model is None:
            raise SystemExit("模型没有创建成功，请检查 model/base_url/api_key/wire-api 配置。")

    agent = ResearchAgent(
        model=model,
        use_llm=not args.disable_llm,
        knowledge_base_dir=args.knowledge_base_dir,
        memory_dir=args.memory_dir,
        max_survey_rounds=args.max_survey_rounds,
        knowledge_top_k=args.knowledge_top_k,
        memory_top_k=args.memory_top_k,
        enable_memory=args.enable_memory,
    )

    state = agent.run(
        event_type=args.event_type,
        query=query,
        constraints=constraints,
        payload=payload,
        previous_state=previous_state,
    )

    print_summary(state)
    debug_log_path = write_debug_log(state, DEFAULT_LOG_DIR)
    print(f"\ndebug_state_log: {debug_log_path}")

    if args.print_state_json or args.save_state:
        state_json = build_state_json(state)
        if args.print_state_json:
            print("\nfull_state_json:")
            print(state_json)
        if args.save_state:
            output_path = Path(args.save_state).expanduser().resolve()
            output_path.write_text(state_json, encoding="utf-8")
            print(f"\nstate JSON saved to: {output_path}")

    return 0 if state.status in {"completed", "not_implemented"} else 1


if __name__ == "__main__":
    sys.exit(main())
