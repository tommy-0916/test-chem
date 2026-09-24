#!/usr/bin/env python3
"""Run a closed-loop chemistry campaign: research ⇄ device ⇄ execution.

Automates the loop until goal reached / manual takeover / feasibility
deadlock / max iterations. Real lab dispatch stays blocked; results come
back through the mock / manual / listen execution adapters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.execution_adapters import build_adapter  # noqa: E402
from orchestrator.runner import (  # noqa: E402
    MAX_CAMPAIGN_ITERATIONS,
    CampaignConfig,
    CampaignRunner,
    DeviceRepairResumeError,
)


EXIT_CODES = {
    "goal_reached": 0,
    "max_iterations": 2,
    "manual_required": 3,
    "feasibility_deadlock": 4,
    "device_error": 5,
    "research_error": 6,
    "scientific_review_required": 7,
    "terminal_unmappable": 8,
    "ready_for_dispatch": 0,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Drive one autonomous-chemistry campaign end to end: research B1 -> "
            "device agent -> execution adapter -> research B2 -> ... until the "
            "goal is reached or the iteration budget is exhausted."
        )
    )
    parser.add_argument(
        "--contract-version",
        choices=["v1", "v2"],
        default="v2",
        help="Internal agent contract. Default: v2; use v1 for compatibility rollback.",
    )
    parser.add_argument(
        "--query",
        help=(
            "Research goal for a new campaign. Required unless "
            "--resume-device-repair is used."
        ),
    )
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
        default=MAX_CAMPAIGN_ITERATIONS,
        help=(
            "Maximum device⇄research iterations after bootstrap. "
            f"Allowed range: 1-{MAX_CAMPAIGN_ITERATIONS}. "
            f"Default: {MAX_CAMPAIGN_ITERATIONS}."
        ),
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
    parser.add_argument(
        "--resume-device-repair",
        help=(
            "Resume from a device_repair_request.json without bootstrapping "
            "Research or consuming another Research↔Device iteration."
        ),
    )
    parser.add_argument(
        "--device-plan-override",
        help=(
            "Complete manual Device-plan override matching the frozen repair "
            "request. Route or sample-matrix drift is rejected."
        ),
    )
    parser.add_argument(
        "--forward-only",
        action="store_true",
        help=(
            "Run one Research -> Device forward pass and stop after a validated "
            "workflow reaches ready_for_dispatch."
        ),
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
    research.add_argument(
        "--include-device-context",
        action="store_true",
        help=(
            "Load the compact workstation capability index into the first "
            "Research plan. Enabled by default."
        ),
    )
    research.add_argument("--enable-memory", action="store_true")
    research.add_argument("--knowledge-base-dir")
    research.add_argument(
        "--online-literature",
        action="store_true",
        help="Enable scholarly retrieval before the first plan. Enabled by default.",
    )
    research.add_argument(
        "--no-online-literature",
        action="store_true",
        help=(
            "Disable scholarly network retrieval. For a new campaign this must "
            "be combined with --no-web-search and --knowledge-base-dir."
        ),
    )
    research.add_argument("--download-pdfs", action="store_true")
    research.add_argument(
        "--web-search",
        action="store_true",
        help="Enable open-web retrieval before the first plan. Enabled by default.",
    )
    research.add_argument(
        "--no-web-search",
        action="store_true",
        help=(
            "Disable open-web retrieval. For a new campaign this must be combined "
            "with --no-online-literature and --knowledge-base-dir."
        ),
    )

    device = parser.add_argument_group("device agent passthrough")
    device.add_argument("--workstations-dir")
    device.add_argument(
        "--full-workstations",
        action="store_true",
        help="Use every workstation Skill and audit rule. Enabled by default.",
    )
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
    llm.add_argument(
        "--llm-timeout-seconds",
        type=int,
        help=(
            "Per-call LLM timeout passed to both research and device agents. "
            "When omitted, each agent keeps its own default."
        ),
    )
    llm.add_argument(
        "--llm-max-retries",
        type=int,
        default=8,
        help="Maximum LLM retries passed to both Research and Device agents. Default: 8.",
    )
    parser.set_defaults(
        include_device_context=True,
        online_literature=True,
        web_search=True,
        full_workstations=True,
    )
    return parser


def validate_research_bootstrap_invariants(args: argparse.Namespace) -> None:
    """Enforce the public campaign's evidence-before-planning contract.

    A Device-repair resume deliberately skips Research bootstrap. New campaigns
    either use both online retrieval lines or explicitly select a local-only
    knowledge base. Partial network opt-out remains invalid because it makes the
    evidence provenance ambiguous.
    """
    if getattr(args, "resume_device_repair", None):
        return
    if not bool(getattr(args, "include_device_context", False)):
        raise ValueError(
            "new campaigns require the workstation capability context before "
            "Research bootstrap"
        )
    local_only = (
        bool(getattr(args, "no_online_literature", False))
        and bool(getattr(args, "no_web_search", False))
        and bool(str(getattr(args, "knowledge_base_dir", "") or "").strip())
    )
    if local_only:
        return
    if bool(getattr(args, "no_online_literature", False)) or not bool(
        getattr(args, "online_literature", False)
    ):
        raise ValueError(
            "new campaigns require online scholarly retrieval before the first "
            "Research plan; use the lower-level Research CLI for offline diagnostics"
        )
    if bool(getattr(args, "no_web_search", False)) or not bool(
        getattr(args, "web_search", False)
    ):
        raise ValueError(
            "new campaigns require open-web retrieval before the first Research "
            "plan; use the lower-level Research CLI for offline diagnostics"
        )


def build_step_args(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    validate_research_bootstrap_invariants(args)
    research_args: list[str] = ["--contract-version", args.contract_version]
    if args.disable_llm:
        research_args.append("--disable-llm")
    if args.include_device_context:
        research_args.append("--include-device-context")
    if args.enable_memory:
        research_args.append("--enable-memory")
    if args.knowledge_base_dir:
        research_args += ["--knowledge-base-dir", args.knowledge_base_dir]
    if args.no_online_literature:
        research_args.append("--no-online-literature")
    elif args.online_literature:
        research_args.append("--online-literature")
    if args.download_pdfs:
        research_args.append("--download-pdfs")
    if args.no_web_search:
        research_args.append("--no-web-search")
    elif args.web_search:
        research_args.append("--web-search")

    device_args: list[str] = ["--contract-version", args.contract_version]
    if args.workstations_dir:
        device_args += ["--workstations-dir", args.workstations_dir]
    if args.full_workstations:
        device_args.append("--full-workstations")
    if args.device_status_json:
        research_args += ["--device-status-json", args.device_status_json]
        device_args += ["--device-status-json", args.device_status_json]
    if args.llm_timeout_seconds is not None:
        timeout_text = str(args.llm_timeout_seconds)
        research_args += ["--llm-timeout-seconds", timeout_text]
        device_args += ["--timeout-seconds", timeout_text]

    retry_text = str(max(0, args.llm_max_retries))
    research_args += ["--llm-max-retries", retry_text]
    device_args += ["--llm-max-retries", retry_text]

    for shared in (research_args, device_args):
        if args.model_name:
            shared += ["--model-name", args.model_name]
        if args.base_url:
            shared += ["--base-url", args.base_url]
        if args.wire_api == "codex_responses":
            shared += ["--wire-api", "codex_responses", "--reasoning-effort", args.reasoning_effort]
    return research_args, device_args


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.max_iterations <= MAX_CAMPAIGN_ITERATIONS:
        parser.error(
            "--max-iterations must be between 1 and "
            f"{MAX_CAMPAIGN_ITERATIONS}"
        )
    if bool(args.resume_device_repair) != bool(args.device_plan_override):
        parser.error(
            "--resume-device-repair and --device-plan-override must be provided together"
        )
    if not args.resume_device_repair and not str(args.query or "").strip():
        parser.error("--query is required for a new campaign")

    resume_metadata: dict = {}
    if args.resume_device_repair:
        request_path = Path(args.resume_device_repair).expanduser().resolve()
        try:
            resume_metadata = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read --resume-device-repair: {exc}")
        if not isinstance(resume_metadata, dict):
            parser.error("--resume-device-repair must contain a JSON object")
    try:
        research_args, device_args = build_step_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    adapter = build_adapter(
        args.execution_adapter,
        mock_observation_file=args.mock_observation_file,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        timeout_seconds=args.result_timeout_seconds,
    )
    config = CampaignConfig(
        query=str(args.query or resume_metadata.get("query") or "").strip(),
        campaign_id=str(
            args.campaign_id or resume_metadata.get("campaign_id") or ""
        ).strip(),
        requested_contract_version=args.contract_version,
        references=list(args.reference),
        max_iterations=args.max_iterations,
        feasibility_deadlock_limit=args.feasibility_deadlock_limit,
        campaigns_root=Path(args.campaigns_root).expanduser().resolve()
        if args.campaigns_root
        else None,
        research_args=research_args,
        device_args=device_args,
        resume_device_repair=Path(args.resume_device_repair).expanduser().resolve()
        if args.resume_device_repair
        else None,
        device_plan_override=Path(args.device_plan_override).expanduser().resolve()
        if args.device_plan_override
        else None,
        forward_only=args.forward_only,
    )

    try:
        result = CampaignRunner(config, adapter).run()
    except DeviceRepairResumeError as exc:
        print(f"device repair resume rejected: {exc}", file=sys.stderr)
        return EXIT_CODES["device_error"]

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
