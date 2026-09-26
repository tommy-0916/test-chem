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
    from macro_identity import MacroIdentityError, macro_id_key, normalize_macro_id
else:
    from .feasibility_fragments import (
        FeasibilityFragmentError,
        _digest,
        diagnose_fragment,
        merge_fragment,
    )
    from .macro_identity import MacroIdentityError, macro_id_key, normalize_macro_id


def _chunk_index(step_name: str) -> int | None:
    marker = "_chunk_"
    if marker not in step_name:
        return None
    tail = step_name.rsplit(marker, 1)[1]
    try:
        return int(tail.split("_of_", 1)[0])
    except ValueError:
        return None


def _normalize_replay_macro_ids(
    values: list[Any], path: str
) -> tuple[list[Any], set[tuple[Any, ...]]]:
    """Validate a captured macro sequence without changing scalar identities."""

    normalized: list[Any] = []
    keys: set[tuple[Any, ...]] = set()
    for index, value in enumerate(values):
        macro_id = normalize_macro_id(value, f"{path}[{index}]")
        key = macro_id_key(macro_id)
        if key in keys:
            raise MacroIdentityError(
                "DUPLICATE_MACRO_ID",
                f"{path}[{index}]",
                "expected unique frozen Research macro identifiers",
            )
        normalized.append(macro_id)
        keys.add(key)
    return normalized, keys


def _normalize_attempt_macro_id(attempt: dict[str, Any], path: str) -> Any:
    if "current_macro_id" not in attempt:
        raise MacroIdentityError(
            "MISSING_MACRO_ID", path, "missing current_macro_id in replay evidence"
        )
    return normalize_macro_id(attempt["current_macro_id"], path)


def _recorded_digest_matches(value: Any, recorded: Any) -> bool:
    """Compare historical bare digests and runtime ``sha256_`` digests.

    Fragment replay originally wrote its fixtures with ``_digest`` (bare hex),
    while the live Device progress recorder uses ``_stable_digest`` and stores
    the same hash as ``sha256_<hex>``.  The representation is metadata; the
    underlying digest must be identical.
    """

    actual = _digest(value)
    return str(recorded or "") in {actual, f"sha256_{actual}"}


def _optional_recorded_digest_match(
    record: dict[str, Any], field: str, value: Any
) -> bool | None:
    """Validate a digest when captured, while preserving legacy evidence.

    Older progress records predate some digest fields.  Absence therefore means
    "not recorded", not "invalid".  Once a field is present, however, even an
    empty value is an integrity claim and must match the captured value.
    """

    if field not in record:
        return None
    return _recorded_digest_matches(value, record.get(field))


def _same_value(left: Any, right: Any) -> bool:
    """Compare replay values through the same canonical JSON digest."""

    return _digest(left) == _digest(right)


def _completed_chunk_matches(
    completed: dict[str, Any], step_name: str, macro_id: Any
) -> bool:
    """Bind a completion record by every identity field it actually carries."""

    has_step = "step_name" in completed
    has_macro = "macro_id" in completed
    if not has_step and not has_macro:
        return False
    if has_step and str(completed.get("step_name") or "") != step_name:
        return False
    if has_macro:
        try:
            if macro_id_key(normalize_macro_id(completed.get("macro_id"))) != macro_id_key(
                normalize_macro_id(macro_id)
            ):
                return False
        except MacroIdentityError:
            return False
    return True


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
    completed_chunks_present = "completed_chunks" in progress
    completed_value = progress.get("completed_chunks")
    completed_records = (
        completed_value
        if isinstance(completed_value, list)
        else []
    )
    bindable_completed_records = [
        (index, item)
        for index, item in enumerate(completed_records)
        if isinstance(item, dict)
        and ("step_name" in item or "macro_id" in item)
    ]
    used_completed_indexes: set[int] = set()
    if completed_chunks_present and not isinstance(completed_value, list):
        chain_matches = False
        mismatches.append({
            "mismatch_type": "invalid_completed_chunks",
            "field": "completed_chunks",
        })
    elif completed_chunks_present:
        for completed_index, completed in enumerate(completed_records):
            if not isinstance(completed, dict):
                chain_matches = False
                mismatches.append({
                    "mismatch_type": "invalid_completed_chunk",
                    "field": "completed_chunks",
                    "completed_chunk_index": completed_index,
                })
            elif "step_name" not in completed and "macro_id" not in completed:
                chain_matches = False
                mismatches.append({
                    "mismatch_type": "invalid_completed_chunk_identity",
                    "field": "completed_chunks",
                    "completed_chunk_index": completed_index,
                })
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
        try:
            replay_macro_ids, replay_macro_keys = _normalize_replay_macro_ids(
                macro_ids, f"{name}.all_macro_ids"
            )
        except MacroIdentityError as exc:
            chain_matches = False
            chunk_reports.append({
                "step_name": name,
                "error": f"invalid replay macro context: [{exc.code}] {exc}",
            })
            continue
        recorded_prefix_digest_valid = _optional_recorded_digest_match(
            base, "prefix_sha256", prefix
        )
        if recorded_prefix_digest_valid is False:
            chain_matches = False
            mismatches.append({
                "mismatch_type": "digest",
                "field": "prefix_sha256",
                "step_name": name,
                "attempt": base.get("attempt"),
            })
        prefix_check = None
        if running_prefix is not None:
            prefix_check = _same_value(running_prefix, prefix)
            if not prefix_check:
                chain_matches = False
                mismatches.append({
                    "mismatch_type": "prefix_chain",
                    "field": "prefix",
                    "step_name": name,
                    "attempt": base.get("attempt"),
                })
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
            fragment_digest_valid = (
                False
                if "fragment_sha256" in attempt and fragment is None
                else _optional_recorded_digest_match(
                    attempt, "fragment_sha256", fragment
                )
            )
            if fragment_digest_valid is not None:
                report["recorded_fragment_digest_valid"] = fragment_digest_valid
            if fragment_digest_valid is False:
                chain_matches = False
                mismatches.append({
                    "mismatch_type": "digest",
                    "field": "fragment_sha256",
                    "step_name": name,
                    "attempt": attempt.get("attempt"),
                })
            if fragment is None:
                report["note"] = "no raw fragment recorded; nothing to replay"
                attempt_reports.append(report)
                continue
            try:
                current = _normalize_attempt_macro_id(
                    attempt, f"{name}.attempt[{attempt.get('attempt')}].current_macro_id"
                )
            except MacroIdentityError as exc:
                report["error"] = f"invalid current_macro_id; cannot replay: [{exc.code}] {exc}"
                attempt_reports.append(report)
                continue
            try:
                merged = merge_fragment(
                    prefix, fragment, current, list(replay_macro_ids), matrix
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
                    prefix, fragment, current, list(replay_macro_ids), matrix
                )
            except Exception as diagnostic_error:  # replay evidence must never fail
                report["diagnostics_error"] = type(diagnostic_error).__name__
            attempt_reports.append(report)
        chunk_report: dict[str, Any] = {
            "step_name": name,
            "recorded_prefix_digest_valid": recorded_prefix_digest_valid,
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
                if item.get("ok") and isinstance(item.get("fragment"), dict)
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
                accepted_current = _normalize_attempt_macro_id(
                    accepted, f"{name}.accepted.current_macro_id"
                )
                if macro_id_key(accepted_current) not in replay_macro_keys:
                    raise MacroIdentityError(
                        "UNKNOWN_MACRO_ID",
                        f"{name}.accepted.current_macro_id",
                        "accepted macro identifier is absent from frozen all_macro_ids",
                    )
                running_prefix = merge_fragment(
                    base_prefix,
                    accepted["fragment"],
                    accepted_current,
                    list(replay_macro_ids),
                    matrix,
                )
                matching_completed = [
                    (completed_index, completed)
                    for completed_index, completed in bindable_completed_records
                    if completed_index not in used_completed_indexes
                    and _completed_chunk_matches(
                        completed, name, accepted_current
                    )
                ]
                if matching_completed:
                    completed_index, completed = matching_completed[0]
                    used_completed_indexes.add(completed_index)
                    completed_report: dict[str, Any] = {
                        "record_index": completed_index,
                    }
                    fragment_digest_valid = _optional_recorded_digest_match(
                        completed,
                        "fragment_digest",
                        accepted["fragment"],
                    )
                    candidate_digest_valid = _optional_recorded_digest_match(
                        completed,
                        "candidate_digest",
                        running_prefix,
                    )
                    if fragment_digest_valid is not None:
                        completed_report["recorded_fragment_digest_valid"] = (
                            fragment_digest_valid
                        )
                    if candidate_digest_valid is not None:
                        completed_report["recorded_candidate_digest_valid"] = (
                            candidate_digest_valid
                        )
                    for field, valid in (
                        ("fragment_digest", fragment_digest_valid),
                        ("candidate_digest", candidate_digest_valid),
                    ):
                        if valid is False:
                            chain_matches = False
                            mismatches.append({
                                "mismatch_type": "digest",
                                "field": field,
                                "step_name": name,
                                "macro_id": accepted_current,
                                "completed_chunk_index": completed_index,
                            })
                    if len(matching_completed) > 1:
                        chain_matches = False
                        completed_report["ambiguous_record_count"] = len(
                            matching_completed
                        )
                        mismatches.append({
                            "mismatch_type": "ambiguous_completed_chunk",
                            "field": "completed_chunks",
                            "step_name": name,
                            "macro_id": accepted_current,
                        })
                    chunk_report["completed_chunk"] = completed_report
                elif completed_chunks_present:
                    # Once the modern completion sidecar is present, every
                    # accepted chunk must bind to exactly one entry.  Only total
                    # absence of the sidecar denotes the legacy format.
                    chain_matches = False
                    chunk_report["completed_chunk"] = {"matched": False}
                    mismatches.append({
                        "mismatch_type": "missing_completed_chunk",
                        "field": "completed_chunks",
                        "step_name": name,
                        "macro_id": accepted_current,
                    })
            except MacroIdentityError as exc:
                chain_matches = False
                chunk_report["chain_error"] = (
                    f"accepted fragment has invalid macro identity: [{exc.code}] {exc}"
                )
            except FeasibilityFragmentError as exc:
                chain_matches = False
                chunk_report["chain_error"] = (
                    f"accepted fragment fails offline merge: [{exc.code}] {exc}"
                )
        elif running_prefix is None:
            running_prefix = copy.deepcopy(prefix)
    if completed_chunks_present:
        for completed_index, completed in bindable_completed_records:
            if completed_index in used_completed_indexes:
                continue
            chain_matches = False
            mismatches.append({
                "mismatch_type": "unexpected_completed_chunk",
                "field": "completed_chunks",
                "completed_chunk_index": completed_index,
                "step_name": completed.get("step_name"),
                "macro_id": completed.get("macro_id"),
            })
    candidate_matches_running_chain = None
    if "candidate" in progress:
        candidate = progress.get("candidate")
        candidate_matches_running_chain = (
            isinstance(candidate, dict)
            and isinstance(running_prefix, dict)
            and _same_value(candidate, running_prefix)
        )
        if not candidate_matches_running_chain:
            chain_matches = False
            mismatches.append({
                "mismatch_type": "candidate_chain",
                "field": "candidate",
            })
    return {
        "progress_status": progress.get("status"),
        "chain_matches": chain_matches,
        "candidate_matches_running_chain": candidate_matches_running_chain,
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
