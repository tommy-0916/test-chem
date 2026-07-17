#!/usr/bin/env python3
"""Run a closed-loop chemistry campaign: research ⇄ device ⇄ execution.

Automates the loop until goal reached / manual takeover / feasibility
deadlock / max iterations. Real lab dispatch stays blocked; results come
back through the mock / manual / listen execution adapters.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.execution_adapters import build_adapter  # noqa: E402
from orchestrator.runner import CampaignConfig, CampaignRunner  # noqa: E402


EXIT_CODES = {
    "goal_reached": 0,
    "max_iterations": 2,
    "manual_required": 3,
    "feasibility_deadlock": 4,
    "device_error": 5,
    "research_error": 6,
    "scientific_review_required": 7,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Drive one autonomous-chemistry campaign end to end: research B1 -> "
            "device agent -> execution adapter -> research B2 -> ... until the "
            "goal is reached or the iteration budget is exhausted."
        )
    )
    parser.add_argument("--query", required=True, help="Research goal for the campaign.")
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="Reference material (local PDF/TXT/MD/JSON path, DOI, arXiv id, or title). Repeatable.",
    )
    parser.add_argument("--campaign-id", help="Campaign id. Auto-generated when omitted.")
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum device⇄research iterations after bootstrap. Default: 10.",
    )
    parser.add_argument(
        "--feasibility-deadlock-limit",
        type=int,
        default=3,
        help="Stop after this many consecutive device feasibility errors. Default: 3.",
    )
    parser.add_argument(
        "--campaigns-root",
        help="Directory holding campaign run artifacts. Default: <repo>/campaigns.",
    )

    execution = parser.add_argument_group("execution boundary")
    execution.add_argument(
        "--execution-adapter",
        choices=["mock", "manual", "listen", "real"],
        default="manual",
        help=(
            "How machine results come back: mock (simulated), manual (wait for "
            "observation_in.json), listen (HTTP POST /observation), real (blocked). "
            "Default: manual."
        ),
    )
    execution.add_argument(
        "--mock-observation-file",
        help="JSON object or array of objects used by the mock adapter.",
    )
    execution.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="Listen adapter bind host. Default: 127.0.0.1.",
    )
    execution.add_argument(
        "--listen-port",
        type=int,
        default=8899,
        help="Listen adapter port. Default: 8899.",
    )
    execution.add_argument(
        "--result-timeout-seconds",
        type=float,
        default=3600.0,
        help="Manual/listen adapters wait at most this long per iteration. Default: 3600.",
    )

    research = parser.add_argument_group("research agent passthrough")
    research.add_argument("--disable-llm", action="store_true", help="Heuristic research mode.")
    research.add_argument("--include-device-context", action="store_true")
    research.add_argument("--enable-memory", action="store_true")
    research.add_argument("--knowledge-base-dir")
    research.add_argument("--online-literature", action="store_true")
    research.add_argument("--no-online-literature", action="store_true")
    research.add_argument("--download-pdfs", action="store_true")
    research.add_argument("--web-search", action="store_true")
    research.add_argument("--no-web-search", action="store_true")

    device = parser.add_argument_group("device agent passthrough")
    device.add_argument("--workstations-dir")
    device.add_argument("--full-workstations", action="store_true")
    parser.add_argument(
        "--device-status-json",
        help=(
            "Live station availability JSON applied to BOTH layers: research avoids "
            "unavailable stations at planning time; the device agent must not select "
            "them."
        ),
    )

    llm = parser.add_argument_group("shared LLM configuration")
    llm.add_argument("--model-name")
    llm.add_argument("--base-url")
    llm.add_argument("--wire-api", choices=["chat", "codex_responses"], default="chat")
    llm.add_argument("--reasoning-effort", default="xhigh")
    return parser


def build_step_args(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    research_args: list[str] = []
    if args.disable_llm:
        research_args.append("--disable-llm")
    if args.include_device_context:
        research_args.append("--include-device-context")
    if args.enable_memory:
        research_args.append("--enable-memory")
    if args.knowledge_base_dir:
        research_args += ["--knowledge-base-dir", args.knowledge_base_dir]
    if args.online_literature:
        research_args.append("--online-literature")
    if args.no_online_literature:
        research_args.append("--no-online-literature")
    if args.download_pdfs:
        research_args.append("--download-pdfs")
    if args.web_search:
        research_args.append("--web-search")
    if args.no_web_search:
        research_args.append("--no-web-search")

    device_args: list[str] = []
    if args.workstations_dir:
        device_args += ["--workstations-dir", args.workstations_dir]
    if args.full_workstations:
        device_args.append("--full-workstations")
    if args.device_status_json:
        research_args += ["--device-status-json", args.device_status_json]
        device_args += ["--device-status-json", args.device_status_json]

    for shared in (research_args, device_args):
        if args.model_name:
            shared += ["--model-name", args.model_name]
        if args.base_url:
            shared += ["--base-url", args.base_url]
        if args.wire_api == "codex_responses":
            shared += ["--wire-api", "codex_responses", "--reasoning-effort", args.reasoning_effort]
    return research_args, device_args


def main() -> int:
    args = build_parser().parse_args()
    research_args, device_args = build_step_args(args)

    adapter = build_adapter(
        args.execution_adapter,
        mock_observation_file=args.mock_observation_file,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        timeout_seconds=args.result_timeout_seconds,
    )
    config = CampaignConfig(
        query=args.query,
        campaign_id=(args.campaign_id or "").strip(),
        references=list(args.reference),
        max_iterations=args.max_iterations,
        feasibility_deadlock_limit=args.feasibility_deadlock_limit,
        campaigns_root=Path(args.campaigns_root).expanduser().resolve()
        if args.campaigns_root
        else None,
        research_args=research_args,
        device_args=device_args,
    )

    result = CampaignRunner(config, adapter).run()

    print("\n=== campaign result ===")
    print(f"campaign_id: {result.campaign_id}")
    print(f"stop_reason: {result.stop_reason}")
    print(f"goal_reached: {result.goal_reached}")
    print(f"iterations_run: {result.iterations_run}")
    print(f"campaign_dir: {result.campaign_dir}")
    print(f"final_report: {result.final_report_path}")
    return EXIT_CODES.get(result.stop_reason, 1)


if __name__ == "__main__":
    sys.exit(main())
