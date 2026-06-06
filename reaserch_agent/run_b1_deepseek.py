#!/usr/bin/env python3
"""Run B1 bootstrap with DeepSeek's OpenAI-compatible API."""

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


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"

# DEFAULT_BASE_URL = "https://api.ikuncode.cc/v1"
# DEFAULT_MODEL = "gpt-5.5"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the research agent B1 bootstrap path using DeepSeek."
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Human research query for B1 bootstrap.",
    )
    parser.add_argument(
        "--api-key",
        help=(
            "DeepSeek API key. Prefer setting DEEPSEEK_API_KEY or "
            "REFINER_LLM_API_KEY instead of passing it on the command line."
        ),
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL,
        help=f"DeepSeek model name. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI-compatible base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--constraints-json",
        default="{}",
        help='JSON object passed as event constraints, e.g. \'{"目标":"先生成首轮macro plan"}\'',
    )
    parser.add_argument(
        "--payload-json",
        default="{}",
        help="Optional JSON object passed as event payload.",
    )
    parser.add_argument(
        "--knowledge-base-dir",
        help="Directory containing knowledge-base PDF or JSON files. Defaults to reaserch_agent/chem_kb.",
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
        help="Optional path for saving the final B1 state JSON.",
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


def configure_deepseek_env(args: argparse.Namespace) -> None:
    api_key = (
        args.api_key
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("REFINER_LLM_API_KEY")
    )
    if not api_key:
        raise SystemExit(
            "缺少 DeepSeek API key。请设置 DEEPSEEK_API_KEY 或传入 --api-key。"
        )

    os.environ["REFINER_LLM_API_KEY"] = api_key
    os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name


def slugify_query(query: str, max_len: int = 48) -> str:
    normalized = re.sub(r"\s+", "_", query.strip())
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "", normalized)
    normalized = normalized.strip("_-")
    return (normalized[:max_len] or "query").strip("_-")


def build_state_json(state: Any) -> str:
    payload = state.debug_snapshot() if hasattr(state, "debug_snapshot") else state.to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_debug_log(state: Any) -> Path:
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    created_at = getattr(state, "created_at", "") or ""
    timestamp = created_at.replace(":", "-").replace("T", "_").split(".", 1)[0]
    filename = f"{timestamp}_{slugify_query(state.event.query)}_deepseek_b1.json"
    output_path = DEFAULT_LOG_DIR / filename
    output_path.write_text(build_state_json(state), encoding="utf-8")
    return output_path


def print_summary(state: Any) -> None:
    print(f"status: {state.status}")
    print(f"last_completed_branch: {state.last_completed_branch}")
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
        print(f"  {step.get('步骤序号', '?')}. {step.get('操作', '')}")
        print(f"     试剂/对象: {step.get('试剂/对象', '')}")
        print(f"     参数: {step.get('参数', '')}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_deepseek_env(args)
    constraints = parse_json_dict(args.constraints_json, "constraints_json")
    payload = parse_json_dict(args.payload_json, "payload_json")

    agent = ResearchAgent(
        model=None,
        use_llm=True,
        knowledge_base_dir=args.knowledge_base_dir,
        memory_dir=args.memory_dir,
        max_survey_rounds=args.max_survey_rounds,
        knowledge_top_k=args.knowledge_top_k,
        memory_top_k=args.memory_top_k,
        enable_memory=args.enable_memory,
    )

    state = agent.run(
        event_type="bootstrap",
        query=args.query.strip(),
        constraints=constraints,
        payload=payload,
    )

    print_summary(state)
    debug_log_path = write_debug_log(state)
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

    return 0 if state.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
