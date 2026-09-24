"""Check a saved workflow against local device contracts without model calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from device_agent.dispatch_checker import check_dispatch_file, write_check_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministically check Device workflow and dispatch parameters."
    )
    parser.add_argument("--input", required=True, type=Path, help="Device package or workflow JSON.")
    parser.add_argument(
        "--report-dir", type=Path,
        help="Report directory (default: check-report beside the input file).",
    )
    parser.add_argument(
        "--workstations-dir", type=Path,
        help="Explicit workstation contract root; default is this project's lab-design-all.",
    )
    parser.add_argument(
        "--artifact-root", type=Path,
        help="Root for resolving workflow file dependencies (default: input directory).",
    )
    parser.add_argument(
        "--initial-state", type=Path,
        help="Optional initial container-state JSON object used by state checks.",
    )
    parser.add_argument(
        "--require-payload", action="store_true",
        help="Require and verify an actual dispatch_payload, as the execution gate does.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    return parser


def _preserve_input_files(report_dir: Path, inputs: list[Path]) -> None:
    """Refuse report names that resolve or hard-link to an input artifact."""

    for filename in ("workflow_check.json", "workflow_check.md"):
        target = (report_dir / filename).resolve()
        for source in inputs:
            source = source.expanduser().resolve()
            same_file = target == source or (
                target.exists() and source.exists() and target.samefile(source)
            )
            if same_file:
                raise ValueError(
                    f"Report path {target} would overwrite an input file; "
                    "choose a different --report-dir."
                )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve()
    report_dir = (args.report_dir or input_path.parent / "check-report").expanduser().resolve()
    initial_state = None
    try:
        _preserve_input_files(
            report_dir,
            [input_path] + ([args.initial_state] if args.initial_state else []),
        )
        if args.initial_state:
            initial_state = json.loads(args.initial_state.read_text(encoding="utf-8-sig"))
            if not isinstance(initial_state, dict):
                parser.error("--initial-state must contain a JSON object")
        report = check_dispatch_file(
            input_path,
            workstation_root=args.workstations_dir,
            artifact_root=args.artifact_root or input_path.parent,
            initial_state=initial_state,
            require_payload=args.require_payload,
        )
        paths = write_check_report(report, report_dir)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Workflow check could not complete: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"Workflow check: {report['status']}")
        print(f"Dispatchable: {report['dispatchable']}")
        print(f"Findings: {len(report.get('findings', []))}")
        for name, path in paths.items():
            print(f"{name}: {path}")
    return 0 if report.get("status") == "passed" and report.get("dispatchable") is True else 1


if __name__ == "__main__":
    # Preserve Chinese parameter names and paths in Windows pipe output too.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
