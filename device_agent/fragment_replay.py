"""Offline replay of recorded feasibility fragment attempts.

Reads a saved Device state JSON (``feasibility_progress[].fragment_attempts``)
and re-runs every recorded fragment through the deterministic merger and the
diagnostic collector against the recorded clean prefix — no model, no devices,
no network.

Purpose: distinguish "the rejected candidate was wrong on a clean prefix"
from "the accepted prefix had already drifted before this chunk started".
The chain check re-merges each chunk's accepted fragment offline and compares
the running result with the next chunk's recorded prefix snapshot; a digest
mismatch means the runtime prefix diverged from what the deterministic
merger produces from the recorded fragments.

Usage:
    python device_agent/fragment_replay.py --device-state path/to/device_state.json
    python device_agent/fragment_replay.py --device-state ... --progress-index 1 --output report.json
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from feasibility_fragments import (
        FeasibilityFragmentError,
        _digest,
        diagnose_fragment,
        merge_fragment,
    )
else:
    from .feasibility_fragments import (
        FeasibilityFragmentError,
        _digest,
        diagnose_fragment,
        merge_fragment,
    )


def _chunk_index(step_name: str) -> int | None:
    marker = "_chunk_"
    if marker not in step_name:
        return None
    tail = step_name.rsplit(marker, 1)[1]
    try:
        return int(tail.split("_of_", 1)[0])
    except ValueError:
        return None


def replay_progress(progress: dict[str, Any]) -> dict[str, Any]:
    """Replay every recorded attempt of one feasibility_progress entry."""
    attempts = progress.get("fragment_attempts")
    if not isinstance(attempts, list) or not attempts:
        return {
            "progress_status": progress.get("status"),
            "error": "no fragment_attempts recorded for this progress entry",
        }
    order: list[str] = []
    chunks: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        name = str(attempt.get("step_name") or "")
        if name not in chunks:
            chunks[name] = []
            order.append(name)
        chunks[name].append(attempt)

    chunk_reports: list[dict[str, Any]] = []
    consumer_conflicts: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    chain_matches = True
    running_prefix: dict[str, Any] | None = None
    for name in order:
        group = chunks[name]
        base = next((item for item in group if isinstance(item.get("prefix"), dict)), None)
        if base is None:
            chain_matches = False
            chunk_reports.append({"step_name": name, "error": "no recorded prefix snapshot"})
            continue
        prefix = base["prefix"]
        matrix = base.get("matrix")
        macro_ids = base.get("all_macro_ids")
        if matrix is None or not isinstance(macro_ids, list) or not macro_ids:
            chain_matches = False
            chunk_reports.append({
                "step_name": name,
                "error": "incomplete replay context (matrix/all_macro_ids missing from the first attempt record)",
            })
            continue
        prefix_check = None
        if running_prefix is not None:
            prefix_check = _digest(running_prefix) == base.get("prefix_sha256")
            if not prefix_check:
                chain_matches = False
        attempt_reports: list[dict[str, Any]] = []
        for attempt in group:
            report: dict[str, Any] = {
                "attempt": attempt.get("attempt"),
                "recorded_ok": bool(attempt.get("ok")),
            }
            recorded_error = attempt.get("error") if isinstance(attempt.get("error"), dict) else {}
            if recorded_error:
                report["recorded_error_code"] = recorded_error.get("code")
            fragment = attempt.get("fragment")
            current = attempt.get("current_macro_id")
            if fragment is None:
                report["note"] = "no raw fragment recorded; nothing to replay"
                attempt_reports.append(report)
                continue
            if not current:
                report["error"] = "no current_macro_id recorded; cannot replay"
                attempt_reports.append(report)
                continue
            try:
                merged = merge_fragment(
                    prefix, fragment, str(current), list(macro_ids), matrix
                )
            except FeasibilityFragmentError as exc:
                report["replay_ok"] = False
                report["replay_error"] = {
                    "code": exc.code,
                    "path": exc.path,
                    "message": str(exc),
                    "details": exc.details,
                }
                if "existing_consumers" in exc.details:
                    conflict = {
                        "step_name": name,
                        "attempt": attempt.get("attempt"),
                        "batch_id": exc.details.get("batch_id"),
                        "existing_consumers": exc.details.get("existing_consumers"),
                        "added_consumers": exc.details.get("added_consumers"),
                    }
                    report["consumer_conflict"] = conflict
                    consumer_conflicts.append(conflict)
            else:
                report["replay_ok"] = True
                report["replay_plan_step_count"] = len(merged.get("device_plan", []))
            replayed_code = (report.get("replay_error") or {}).get("code")
            matches = report.get("replay_ok") == report["recorded_ok"] and (
                not recorded_error or recorded_error.get("code") == replayed_code
            )
            report["matches_record"] = matches
            if matches is False:
                mismatches.append({"step_name": name, **report})
            try:
                report["diagnostics"] = diagnose_fragment(
                    prefix, fragment, str(current), list(macro_ids), matrix
                )
            except Exception as diagnostic_error:  # replay evidence must never fail
                report["diagnostics_error"] = type(diagnostic_error).__name__
            attempt_reports.append(report)
        chunk_report: dict[str, Any] = {
            "step_name": name,
            "chunk_prefix_matches_running_chain": prefix_check,
            "attempts": attempt_reports,
        }
        chunk_reports.append(chunk_report)
        # Advance the chain with the fragment the runtime actually accepted
        # (the last ok attempt carrying a raw fragment).
        accepted = next(
            (
                item
                for item in reversed(group)
                if item.get("ok") and isinstance(item.get("fragment"), dict) and item.get("current_macro_id")
            ),
            None,
        )
        if accepted is not None:
            base_prefix = (
                running_prefix
                if isinstance(running_prefix, dict)
                else copy.deepcopy(prefix)
            )
            try:
                running_prefix = merge_fragment(
                    base_prefix,
                    accepted["fragment"],
                    str(accepted["current_macro_id"]),
                    list(macro_ids),
                    matrix,
                )
            except FeasibilityFragmentError as exc:
                chain_matches = False
                chunk_report["chain_error"] = (
                    f"accepted fragment fails offline merge: [{exc.code}] {exc}"
                )
        elif running_prefix is None:
            running_prefix = copy.deepcopy(prefix)
    return {
        "progress_status": progress.get("status"),
        "chain_matches": chain_matches,
        "chunks": chunk_reports,
        "summary": {
            "chunks": len(chunk_reports),
            "attempts_replayed": sum(
                1
                for chunk in chunk_reports
                for item in chunk.get("attempts", [])
                if "replay_ok" in item
            ),
            "record_mismatches": len(mismatches),
            "mismatches": mismatches,
            "consumer_conflicts": consumer_conflicts,
        },
    }


def replay_state(
    state: dict[str, Any], progress_indexes: list[int] | None = None
) -> list[dict[str, Any]]:
    """Replay selected (or all) feasibility_progress entries of a saved state."""
    progress_entries = state.get("feasibility_progress")
    if not isinstance(progress_entries, list):
        return [{"error": "state carries no feasibility_progress list"}]
    reports = []
    for index, progress in enumerate(progress_entries):
        if progress_indexes is not None and index not in progress_indexes:
            continue
        if not isinstance(progress, dict):
            continue
        report = replay_progress(progress)
        report["progress_index"] = index
        reports.append(report)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-state", required=True, help="Path to a saved device_state.json")
    parser.add_argument(
        "--progress-index",
        type=int,
        action="append",
        dest="progress_indexes",
        help="Replay only this feasibility_progress index (repeatable); default: all",
    )
    parser.add_argument("--output", help="Optional path for the JSON replay report")
    args = parser.parse_args(argv)
    state = json.loads(Path(args.device_state).read_text(encoding="utf-8"))
    reports = replay_state(state, args.progress_indexes)
    payload = json.dumps(reports, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
