"""Atomic, input-bound checkpoints for Device planning (never dispatch)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def digest(value: Any) -> str:
    """Return a deterministic digest for JSON-compatible checkpoint data."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: str | Path) -> str:
    """Hash one implementation/contract file without interpreting its content."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def implementation_digest() -> str:
    """Bind reuse to every production Python module used by Device planning."""
    root = Path(__file__).resolve().parents[1]
    sources: dict[str, str] = {}
    for package in ("device_agent", "chem_agent_contracts", "agent_skills"):
        for path in sorted((root / package).rglob("*.py")):
            if path.name.startswith("test_") or "tests" in path.parts:
                continue
            sources[path.relative_to(root).as_posix()] = file_digest(path)
    return digest(sources)


def _atomic_json(path: Path, value: Any) -> None:
    """Durably replace one JSON file; interrupted writes leave no valid stage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DeviceCheckpointStore:
    """Store successful local planning computations under a frozen binding.

    Checkpoints are diagnostic/planning artifacts only.  Reading one never
    marks feasibility accepted and never creates a dispatchable workflow.
    """

    version = 1
    _ACTIVE = "active"
    _COMPLETE = "complete"

    def __init__(
        self,
        root: str | Path,
        binding: dict[str, Any],
        *,
        resume: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.binding = copy.deepcopy(binding)
        self.binding_digest = digest(binding)
        binding_directory = self.root / self.binding_digest
        binding_directory.mkdir(parents=True, exist_ok=True)
        active_path = binding_directory / "active.json"
        self.active_path = active_path
        active: dict[str, Any] | None = None
        if resume:
            try:
                candidate = json.loads(active_path.read_text(encoding="utf-8"))
                candidate_run_id = (
                    str(candidate.get("run_id", ""))
                    if isinstance(candidate, dict)
                    else ""
                )
                completion_path = (
                    binding_directory / candidate_run_id / "completion.json"
                )
                completed = False
                try:
                    completion = json.loads(
                        completion_path.read_text(encoding="utf-8")
                    )
                    completed = bool(
                        isinstance(completion, dict)
                        and completion.get("run_id") == candidate_run_id
                        and completion.get("binding") == self.binding
                        and completion.get("lifecycle_state") == self._COMPLETE
                    )
                except (OSError, ValueError, TypeError):
                    pass
                if (
                    isinstance(candidate, dict)
                    and candidate.get("binding") == self.binding
                    and re.fullmatch(
                        r"[a-f0-9]{32}", candidate_run_id
                    )
                    # Legacy envelopes did not have lifecycle_state and do
                    # not contain enough evidence to prove a terminal result.
                    # Treat them as incomplete to preserve safe recovery.
                    and candidate.get("lifecycle_state", self._ACTIVE)
                    == self._ACTIVE
                    # A matching run-local completion record is additional
                    # defense against an accidentally restored/stale active
                    # envelope. active.json remains the authoritative marker.
                    and not completed
                ):
                    active = candidate
            except (OSError, ValueError, TypeError):
                pass
        if active is None:
            active = {
                "run_id": uuid.uuid4().hex,
                "binding": self.binding,
                "metadata": copy.deepcopy(metadata or {}),
                "lifecycle_state": self._ACTIVE,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(active_path, active)
        self.run_id = str(active["run_id"])
        self.metadata = copy.deepcopy(active.get("metadata") or {})
        self.lifecycle_state = str(
            active.get("lifecycle_state", self._ACTIVE)
        )
        self.terminal_status = str(active.get("terminal_status", ""))
        self.dispatchable = bool(active.get("dispatchable", False))
        self.directory = binding_directory / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.available = (
            {path.name for path in self.directory.glob("*.json")}
            if resume
            else set()
        )
        self.occurrences: dict[str, int] = {}
        self.scopes: list[str] = []
        self.reused: list[str] = []
        self.latest: dict[str, Any] = {}

    def key(self, stage: str, inputs: Any) -> str:
        fingerprint = digest(
            {"stage": stage, "inputs": inputs, "scope": self.scopes}
        )
        occurrence = self.occurrences.get(fingerprint, 0)
        self.occurrences[fingerprint] = occurrence + 1
        safe_stage = re.sub(r"[^a-zA-Z0-9_-]", "_", stage)[:80]
        return f"{safe_stage}-{fingerprint}-{occurrence}.json"

    @contextmanager
    def scope(self, key: str) -> Iterator[None]:
        self.scopes.append(key)
        try:
            yield
        finally:
            self.scopes.pop()

    def confirm_reuse(self, key: str, stage: str) -> None:
        """Record reuse only after the caller's domain checks have passed."""
        self.reused.append(stage)
        self.latest = {"stage": stage, "path": str(self.directory / key)}

    def read(self, key: str, *, record_reuse: bool = True) -> Any:
        if key not in self.available:
            return None
        try:
            item = json.loads((self.directory / key).read_text(encoding="utf-8"))
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("stage"), str)
                or item.get("version") != self.version
                or item.get("binding") != self.binding
                or item.get("payload_sha256") != digest(item.get("payload"))
            ):
                return None
        except (OSError, ValueError, TypeError):
            return None
        if record_reuse:
            self.confirm_reuse(key, str(item["stage"]))
        return copy.deepcopy(item["payload"])

    def write(self, key: str, stage: str, payload: Any) -> None:
        _atomic_json(
            self.directory / key,
            {
                "version": self.version,
                "stage": stage,
                "binding": self.binding,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "payload_sha256": digest(payload),
                "payload": payload,
            },
        )
        self.available.add(key)
        self.latest = {"stage": stage, "path": str(self.directory / key)}
        _atomic_json(self.root / "latest.json", self.summary())

    def complete(
        self,
        *,
        terminal_status: str,
        dispatchable: bool = False,
    ) -> None:
        """Close this run so a later invocation cannot resume its identity.

        Stage files remain immutable audit artifacts.  ``active.json`` is
        atomically changed to a completed envelope; the next store opened for
        the same binding therefore creates a fresh run id and uses the new
        caller-provided metadata (including a new generated ``exp_id``).
        """
        normalized_status = str(terminal_status or "").strip().lower()
        if not normalized_status:
            raise ValueError("terminal_status is required to complete a run")
        if self.lifecycle_state == self._COMPLETE:
            if (
                self.terminal_status != normalized_status
                or self.dispatchable != bool(dispatchable)
            ):
                raise RuntimeError(
                    "checkpoint run was already completed with a different outcome"
                )
            return

        completed_at = datetime.now(timezone.utc).isoformat()
        envelope = {
            "run_id": self.run_id,
            "binding": copy.deepcopy(self.binding),
            "metadata": copy.deepcopy(self.metadata),
            "lifecycle_state": self._COMPLETE,
            "terminal_status": normalized_status,
            "dispatchable": bool(dispatchable),
            "completed_at": completed_at,
        }
        # active.json is authoritative: close it first so a crash can never
        # expose an already-terminal run as resumable.  The run-local copy is
        # audit-only and therefore best effort.
        _atomic_json(self.active_path, envelope)
        self.lifecycle_state = self._COMPLETE
        self.terminal_status = normalized_status
        self.dispatchable = bool(dispatchable)
        try:
            _atomic_json(self.directory / "completion.json", envelope)
        except OSError:
            pass
        _atomic_json(self.root / "latest.json", self.summary())

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "directory": str(self.directory),
            "binding_sha256": self.binding_digest,
            "binding": copy.deepcopy(self.binding),
            "lifecycle_state": self.lifecycle_state,
            "terminal_status": self.terminal_status,
            "dispatchable": self.dispatchable,
            "latest": copy.deepcopy(self.latest),
            "resumed_stages": list(self.reused),
        }
