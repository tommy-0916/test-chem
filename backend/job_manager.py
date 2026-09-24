"""Persistent subprocess job manager for the ChemAgent HTTP service."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .models import CampaignCreate


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "awaiting_observation", "cancelling"}
SAFE_ID_RE = re.compile(r"^cmp_[0-9]{8}_[0-9]{6}_[a-f0-9]{10}$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
LOCAL_REFERENCE_SUFFIXES = {".pdf", ".txt", ".md", ".json"}
CAMPAIGN_EXIT_CODES = {
    "goal_reached": 0,
    "max_iterations": 2,
    "manual_required": 3,
    "feasibility_deadlock": 4,
    "device_error": 5,
    "research_error": 6,
}
RESEARCH_STABLE_FIELDS = {
    "campaign_id",
    "status",
    "current_branch",
    "next_branch",
    "last_completed_branch",
    "route_message",
    "branch_history",
    "current_stage",
    "stage_route",
    "current_stage_plan",
    "macro_plan",
    "stage_route_reason",
    "current_stage_reason",
    "survey_report",
    "knowledge_hits",
    "extracted_protocols",
    "latest_observation",
    "observations",
    "observation_stage_fit",
    "observation_interpretation",
    "stage_progress",
    "stage_progress_status",
    "post_observation_repair_path",
    "manual_handoff",
    "plan_revisions",
    "created_at",
    "errors",
    "logs",
}
RESEARCH_EXTENSION_EXCLUSIONS = {
    "event",
    "raw_llm_outputs",
    "persistent_outputs",
    "device_adaptation_handoff",
    "previous_macro_plan",
    "survey_queries",
    "survey_rounds",
    "A. research layer 内部持久化输出",
    "B. 发给下游 device adaptation layer agent 的外部交接输出",
}


class JobNotFoundError(KeyError):
    pass


class JobConflictError(RuntimeError):
    pass


class InvalidReferenceError(ValueError):
    pass


CommandBuilder = Callable[[str, CampaignCreate, Path], Sequence[str]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class JobManager:
    """Launch and supervise Agent CLI commands in isolated process groups."""

    def __init__(
        self,
        *,
        repo_root: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        python_executable: Optional[str] = None,
        command_builder: Optional[CommandBuilder] = None,
        cancel_grace_seconds: float = 3.0,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
        self.data_dir = (data_dir or Path(__file__).resolve().parent / "data").resolve()
        self.campaigns_dir = self.data_dir / "campaigns"
        self.uploads_dir = self.data_dir / "uploads"
        self.logs_dir = self.data_dir / "logs"
        self.db_path = self.data_dir / "jobs.sqlite3"
        self.python_executable = (
            python_executable
            or os.getenv("CHEMAGENT_PYTHON", "").strip()
            or sys.executable
        )
        self._command_builder = command_builder
        self.cancel_grace_seconds = max(0.1, cancel_grace_seconds)
        self._db_lock = threading.RLock()
        self._process_lock = threading.RLock()
        self._processes: Dict[str, subprocess.Popen[str]] = {}
        self._threads: Dict[str, threading.Thread] = {}

        for directory in (
            self.data_dir,
            self.campaigns_dir,
            self.uploads_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._initialize_database()
        self._recover_interrupted_jobs()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_database(self) -> None:
        with self._db_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    query TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    campaign_dir TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    pid INTEGER,
                    return_code INTEGER,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observation_claims (
                    job_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, iteration),
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id, seq)"
            )

    def _recover_interrupted_jobs(self) -> None:
        now = utc_now()
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running','awaiting_observation','cancelling')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='failed', phase='interrupted', error=?, updated_at=?, finished_at=?
                    WHERE id=?
                    """,
                    ("backend restarted while the job was active", now, now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO events(job_id,event_type,data_json,created_at) VALUES(?,?,?,?)",
                    (
                        row["id"],
                        "failed",
                        _json_dumps({"error": "backend restarted while the job was active"}),
                        now,
                    ),
                )

    @staticmethod
    def _new_job_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"cmp_{timestamp}_{secrets.token_hex(5)}"

    def validate_reference(self, reference: str) -> str:
        """Resolve local references without allowing paths outside managed roots."""
        value = reference.strip()
        if not value:
            raise InvalidReferenceError("reference must not be blank")
        if DOI_RE.match(value) or value.lower().startswith("arxiv:"):
            return value

        expanded = Path(value).expanduser()
        repo_candidate = expanded if expanded.is_absolute() else self.repo_root / expanded
        looks_local = (
            expanded.is_absolute()
            or value.startswith(("./", "../", "~/"))
            or repo_candidate.exists()
            or expanded.suffix.lower() in LOCAL_REFERENCE_SUFFIXES
        )
        if not looks_local:
            return value

        resolved = repo_candidate.resolve()
        if not resolved.exists():
            raise InvalidReferenceError(f"local reference does not exist: {value}")
        if not resolved.is_file():
            raise InvalidReferenceError("local references must be files")
        if not (
            _is_within(resolved, self.repo_root)
            or _is_within(resolved, self.uploads_dir)
        ):
            raise InvalidReferenceError("local references must stay inside the repository or uploads directory")
        return str(resolved)

    def create_campaign(self, request: CampaignCreate) -> Dict[str, Any]:
        references = [self.validate_reference(item) for item in request.references]
        normalized = request.model_copy(update={"references": references})
        job_id = self._new_job_id()
        if not SAFE_ID_RE.match(job_id):  # defensive invariant
            raise RuntimeError("failed to generate a safe campaign id")

        campaign_dir = (self.campaigns_dir / job_id).resolve()
        if not _is_within(campaign_dir, self.campaigns_dir):
            raise RuntimeError("generated campaign path escaped campaigns directory")
        campaign_dir.mkdir(parents=False, exist_ok=False)
        if normalized.mode == "research_preview":
            (campaign_dir / "iteration_00").mkdir()

        command = list(
            self._command_builder(job_id, normalized, campaign_dir)
            if self._command_builder is not None
            else self._build_command(job_id, normalized, campaign_dir)
        )
        if not command:
            raise RuntimeError("command builder returned an empty command")

        log_path = self.logs_dir / f"{job_id}.log"
        now = utc_now()
        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id,mode,status,phase,query,request_json,command_json,
                    campaign_dir,log_path,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    normalized.mode,
                    "queued",
                    "queued",
                    normalized.query,
                    _json_dumps(normalized.model_dump(mode="json")),
                    _json_dumps(command),
                    str(campaign_dir),
                    str(log_path),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO events(job_id,event_type,data_json,created_at) VALUES(?,?,?,?)",
                (job_id, "queued", _json_dumps({"mode": normalized.mode}), now),
            )

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, command),
            name=f"chemagent-{job_id}",
            daemon=True,
        )
        with self._process_lock:
            self._threads[job_id] = thread
        thread.start()
        return self.get_job(job_id)

    def _build_command(
        self,
        job_id: str,
        request: CampaignCreate,
        campaign_dir: Path,
    ) -> List[str]:
        knowledge_base_dir = campaign_dir / "knowledge_base"
        if request.mode == "research_preview":
            state_path = campaign_dir / "iteration_00" / "research_state.json"
            command = [
                self.python_executable,
                str(self.repo_root / "reaserch_agent" / "run_research_agent.py"),
                "--event-type",
                "bootstrap",
                f"--query={request.query}",
                "--campaign-id",
                job_id,
                "--knowledge-base-dir",
                str(knowledge_base_dir),
                "--save-state",
                str(state_path),
                "--disable-llm",
                "--no-ledger",
                "--wire-api",
                request.wire_api,
            ]
            if request.include_device_context:
                command.append("--include-device-context")
            if request.enable_memory:
                command.append("--enable-memory")
            command.append(
                "--online-literature"
                if request.online_literature
                else "--no-online-literature"
            )
            command.append("--web-search" if request.web_search else "--no-web-search")
            if request.download_pdfs:
                command.append("--download-pdfs")
            for reference in request.references:
                command.append(f"--reference={reference}")
            return command

        command = [
            self.python_executable,
            str(self.repo_root / "run_campaign.py"),
            f"--query={request.query}",
            "--campaign-id",
            job_id,
            "--campaigns-root",
            str(self.campaigns_dir),
            "--knowledge-base-dir",
            str(knowledge_base_dir),
            "--execution-adapter",
            request.execution_adapter,
            "--max-iterations",
            str(request.max_iterations),
            "--feasibility-deadlock-limit",
            str(request.feasibility_deadlock_limit),
            "--wire-api",
            request.wire_api,
        ]
        if request.wire_api == "codex_responses":
            command.extend(["--reasoning-effort", request.reasoning_effort])
        if request.include_device_context:
            command.append("--include-device-context")
        if request.enable_memory:
            command.append("--enable-memory")
        if request.full_workstations:
            command.append("--full-workstations")
        command.append("--online-literature" if request.online_literature else "--no-online-literature")
        command.append("--web-search" if request.web_search else "--no-web-search")
        if request.download_pdfs:
            command.append("--download-pdfs")
        for reference in request.references:
            command.append(f"--reference={reference}")
        return command

    def _run_job(self, job_id: str, command: Sequence[str]) -> None:
        if self._cancel_requested(job_id):
            self._finish_cancelled(job_id, return_code=None)
            return

        self._update_job(
            job_id,
            status="running",
            phase="starting",
            started_at=utc_now(),
            error=None,
        )
        self._append_event(job_id, "started", {"phase": "starting"})
        job = self.get_job(job_id)
        log_path = Path(job["log_path"])
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"

        process: Optional[subprocess.Popen[str]] = None
        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(self.repo_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                start_new_session=True,
            )
            with self._process_lock:
                self._processes[job_id] = process
            self._update_job(job_id, pid=process.pid, phase="running")
            self._append_event(job_id, "process_started", {"pid": process.pid})

            if self._cancel_requested(job_id):
                self._request_process_termination(job_id, process)

            current_phase = "running"
            with log_path.open("a", encoding="utf-8") as log_handle:
                if process.stdout is not None:
                    for raw_line in process.stdout:
                        line = raw_line.rstrip("\r\n")
                        log_handle.write(line + "\n")
                        log_handle.flush()
                        self._append_event(job_id, "log", {"line": line})
                        inferred = self._infer_phase(line)
                        if inferred and inferred != current_phase:
                            current_phase = inferred
                            status = (
                                "awaiting_observation"
                                if inferred == "awaiting_observation"
                                else "running"
                            )
                            if not self._cancel_requested(job_id):
                                self._update_job(job_id, phase=inferred, status=status)
                                self._append_event(
                                    job_id,
                                    "phase",
                                    {"phase": inferred, "status": status},
                                )

            return_code = process.wait()
            if self._cancel_requested(job_id):
                self._finish_cancelled(job_id, return_code=return_code)
                return

            completed, completion_error = self._validate_completion(job_id, return_code)
            if completed:
                self._update_job(
                    job_id,
                    status="completed",
                    phase="finished",
                    return_code=return_code,
                    finished_at=utc_now(),
                )
                self._append_event(
                    job_id,
                    "completed",
                    {"return_code": return_code},
                )
            else:
                error = completion_error or f"Agent process exited with code {return_code}"
                self._update_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    return_code=return_code,
                    error=error,
                    finished_at=utc_now(),
                )
                self._append_event(
                    job_id,
                    "failed",
                    {"return_code": return_code, "error": error},
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = "cancelled" if self._cancel_requested(job_id) else "failed"
            self._update_job(
                job_id,
                status=status,
                phase=status,
                error=error if status == "failed" else None,
                finished_at=utc_now(),
            )
            self._append_event(job_id, status, {"error": error})
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self._process_lock:
                self._processes.pop(job_id, None)
                self._threads.pop(job_id, None)

    @staticmethod
    def _infer_phase(line: str) -> Optional[str]:
        lowered = line.lower()
        if "waiting for observation" in lowered or "waiting for experiment result" in lowered:
            return "awaiting_observation"
        if "device mapping starts" in lowered or "starting device agent" in lowered:
            return "device_mapping"
        if "observation received" in lowered or "event_type=new_observation" in lowered:
            return "research_update"
        if "starting research agent" in lowered or "research bootstrap" in lowered:
            return "research"
        if "campaign finished" in lowered:
            return "finalizing"
        return None

    def _validate_completion(self, job_id: str, return_code: int) -> tuple[bool, str]:
        job = self.get_job(job_id)
        campaign_dir = Path(job["campaign_dir"])
        if job["mode"] == "research_preview":
            state = self._safe_json(campaign_dir / "iteration_00" / "research_state.json")
            if return_code != 0:
                return False, f"Research preview exited with code {return_code}"
            if not state:
                return False, "Research preview produced no valid state artifact"
            return True, ""

        summary = self._safe_json(campaign_dir / "campaign_summary.json")
        stop_reason = str(summary.get("stop_reason") or "")
        expected_code = CAMPAIGN_EXIT_CODES.get(stop_reason)
        if expected_code is None:
            return False, "Campaign produced no valid stop_reason summary"
        if return_code != expected_code:
            return (
                False,
                f"Campaign exit code {return_code} does not match stop_reason "
                f"{stop_reason!r} (expected {expected_code})",
            )
        return True, ""

    def _cancel_requested(self, job_id: str) -> bool:
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def _finish_cancelled(self, job_id: str, return_code: Optional[int]) -> None:
        self._update_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            return_code=return_code,
            error=None,
            finished_at=utc_now(),
        )
        self._append_event(job_id, "cancelled", {"return_code": return_code})

    def _update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "phase",
            "pid",
            "return_code",
            "error",
            "cancel_requested",
            "started_at",
            "finished_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [job_id]
        with self._db_lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                values,
            )
            if cursor.rowcount == 0:
                raise JobNotFoundError(job_id)

    def _append_event(self, job_id: str, event_type: str, data: Dict[str, Any]) -> int:
        with self._db_lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(job_id,event_type,data_json,created_at) VALUES(?,?,?,?)",
                (job_id, event_type, _json_dumps(data), utc_now()),
            )
            return int(cursor.lastrowid)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._db_lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        result = dict(row)
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        for key in ("request_json", "command_json"):
            try:
                result[key[:-5]] = json.loads(result.pop(key))
            except (TypeError, json.JSONDecodeError):
                result[key[:-5]] = {} if key == "request_json" else []
        return result

    def list_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self.get_job(str(row["id"])) for row in rows]

    def get_events(self, job_id: str, after: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        self.get_job(job_id)
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT seq,event_type,data_json,created_at
                FROM events WHERE job_id=? AND seq>? ORDER BY seq ASC LIMIT ?
                """,
                (job_id, max(0, after), max(1, min(limit, 1000))),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except json.JSONDecodeError:
                data = {"raw": row["data_json"]}
            events.append(
                {
                    "seq": row["seq"],
                    "type": row["event_type"],
                    "data": data,
                    "created_at": row["created_at"],
                }
            )
        return events

    def cancel(self, job_id: str) -> Dict[str, Any]:
        job = self.get_job(job_id)
        if job["status"] in TERMINAL_STATUSES:
            raise JobConflictError(f"job is already {job['status']}")
        if job["status"] == "cancelling":
            return job

        self._update_job(
            job_id,
            status="cancelling",
            phase="cancelling",
            cancel_requested=1,
        )
        self._append_event(job_id, "cancelling", {})
        with self._process_lock:
            process = self._processes.get(job_id)
        if process is not None:
            self._request_process_termination(job_id, process)
        return self.get_job(job_id)

    def _request_process_termination(
        self,
        job_id: str,
        process: subprocess.Popen[str],
    ) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            self._append_event(job_id, "signal", {"signal": "SIGTERM"})
        except ProcessLookupError:
            return

        def escalate() -> None:
            try:
                process.wait(timeout=self.cancel_grace_seconds)
                return
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                self._append_event(job_id, "signal", {"signal": "SIGKILL"})
            except ProcessLookupError:
                return

        threading.Thread(
            target=escalate,
            name=f"chemagent-cancel-{job_id}",
            daemon=True,
        ).start()

    def wait_for_terminal(self, job_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            if job["status"] in TERMINAL_STATUSES:
                return job
            time.sleep(0.02)
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    def health(self) -> bool:
        try:
            with self._db_lock, self._connect() as connection:
                value = connection.execute("SELECT 1").fetchone()[0]
            return value == 1
        except sqlite3.Error:
            return False

    def shutdown(self) -> None:
        with self._process_lock:
            active = list(self._processes.items())
        for job_id, process in active:
            try:
                self._update_job(
                    job_id,
                    status="cancelling",
                    phase="cancelling",
                    cancel_requested=1,
                )
                self._request_process_termination(job_id, process)
            except (JobNotFoundError, ProcessLookupError):
                continue

    @staticmethod
    def _safe_json(path: Path) -> Dict[str, Any]:
        if not path.exists() or not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _iteration_number(path: Path) -> Optional[int]:
        match = re.match(r"^iteration_(\d+)$", path.name)
        return int(match.group(1)) if match else None

    def _iteration_dirs(self, campaign_dir: Path) -> List[Path]:
        if not campaign_dir.exists():
            return []
        paths = [
            path
            for path in campaign_dir.iterdir()
            if path.is_dir() and self._iteration_number(path) is not None
        ]
        return sorted(paths, key=lambda path: self._iteration_number(path) or 0)

    def _latest_file(self, campaign_dir: Path, name: str) -> Optional[Path]:
        for iteration_dir in reversed(self._iteration_dirs(campaign_dir)):
            candidate = iteration_dir / name
            if candidate.exists():
                return candidate
        return None

    def find_waiting_iteration(self, job_id: str) -> Optional[Path]:
        job = self.get_job(job_id)
        campaign_dir = Path(job["campaign_dir"])
        for iteration_dir in reversed(self._iteration_dirs(campaign_dir)):
            marker = iteration_dir / "AWAITING_OBSERVATION.md"
            observation = iteration_dir / "observation_in.json"
            if marker.exists() and not observation.exists():
                return iteration_dir
        return None

    def submit_observation(
        self,
        job_id: str,
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        job = self.get_job(job_id)
        request = job.get("request") or {}
        if job["status"] not in ACTIVE_STATUSES or job["status"] == "cancelling":
            raise JobConflictError(f"job is not accepting observations: {job['status']}")
        if job["mode"] != "full_campaign" or request.get("execution_adapter") != "manual":
            raise JobConflictError("observations are accepted only by full_campaign manual jobs")

        iteration_dir = self.find_waiting_iteration(job_id)
        if iteration_dir is None:
            raise JobConflictError("campaign is not currently awaiting an observation")
        iteration = self._iteration_number(iteration_dir)
        if iteration is None:
            raise JobConflictError("campaign has no claimable observation iteration")

        destination = iteration_dir / "observation_in.json"
        temporary = iteration_dir / f".observation-{secrets.token_hex(6)}.tmp"
        claimed = False
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(observation, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            now = utc_now()
            with self._db_lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT status,cancel_requested FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if current is None:
                    raise JobNotFoundError(job_id)
                if (
                    current["status"] not in ACTIVE_STATUSES
                    or current["status"] == "cancelling"
                    or current["cancel_requested"]
                ):
                    raise JobConflictError(
                        f"job is not accepting observations: {current['status']}"
                    )
                try:
                    connection.execute(
                        "INSERT INTO observation_claims(job_id,iteration,claimed_at) "
                        "VALUES(?,?,?)",
                        (job_id, iteration, now),
                    )
                    claimed = True
                except sqlite3.IntegrityError as exc:
                    raise JobConflictError(
                        "an observation was already submitted for this iteration"
                    ) from exc
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='running', phase='observation_submitted', updated_at=?
                    WHERE id=?
                    """,
                    (now, job_id),
                )
                connection.execute(
                    "INSERT INTO events(job_id,event_type,data_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (
                        job_id,
                        "observation_submitted",
                        _json_dumps({"iteration": iteration}),
                        now,
                    ),
                )
            os.replace(temporary, destination)
        except Exception:
            if claimed and not destination.exists():
                with self._db_lock, self._connect() as connection:
                    connection.execute(
                        "DELETE FROM observation_claims WHERE job_id=? AND iteration=?",
                        (job_id, iteration),
                    )
            raise
        finally:
            if temporary.exists():
                temporary.unlink()

        return {
            "status": "accepted",
            "job_id": job_id,
            "iteration": iteration,
        }

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        return value if isinstance(value, list) else []

    @classmethod
    def _sanitize_extension_value(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 5:
            return "[truncated: maximum nesting depth]"
        if isinstance(value, str):
            return value if len(value) <= 8_000 else value[:8_000] + "...[truncated]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, list):
            return [
                cls._sanitize_extension_value(item, depth + 1)
                for item in value[:50]
            ]
        if isinstance(value, dict):
            sanitized: Dict[str, Any] = {}
            for key, item in list(value.items())[:50]:
                normalized_key = str(key).lower()
                if any(
                    marker in normalized_key
                    for marker in ("api_key", "password", "secret", "access_token", "authorization")
                ):
                    continue
                sanitized[str(key)] = cls._sanitize_extension_value(item, depth + 1)
            return sanitized
        return str(value)[:8_000]

    @classmethod
    def _extension_view(
        cls,
        state: Dict[str, Any],
        stable_fields: set[str],
        exclusions: set[str],
    ) -> Dict[str, Any]:
        extensions: Dict[str, Any] = {}
        total_chars = 0
        for key, value in state.items():
            normalized_key = str(key).lower()
            if key in stable_fields or key in exclusions:
                continue
            if any(
                marker in normalized_key
                for marker in ("api_key", "password", "secret", "access_token", "authorization")
            ):
                continue
            sanitized = cls._sanitize_extension_value(value)
            encoded_size = len(_json_dumps(sanitized))
            if total_chars + encoded_size > 50_000:
                extensions[str(key)] = "[omitted: extension payload exceeds budget]"
                continue
            extensions[str(key)] = sanitized
            total_chars += encoded_size
        return extensions

    def _job_view(self, job: Dict[str, Any]) -> Dict[str, Any]:
        waiting = self.find_waiting_iteration(job["id"]) is not None
        request = self._as_dict(job.get("request"))
        campaign_dir = Path(job["campaign_dir"])
        summary = self._safe_json(campaign_dir / "campaign_summary.json")
        iterations = self._iteration_dirs(campaign_dir)
        stop_reason = str(summary.get("stop_reason") or "") or None
        if stop_reason == "goal_reached":
            outcome_severity = "success"
        elif stop_reason in {"max_iterations", "manual_required", "feasibility_deadlock"}:
            outcome_severity = "warning"
        elif stop_reason in {"device_error", "research_error"} or job["status"] == "failed":
            outcome_severity = "danger"
        elif job["status"] in ACTIVE_STATUSES:
            outcome_severity = "info"
        else:
            outcome_severity = "neutral"
        return {
            "schema_version": "1.0",
            "id": job["id"],
            "mode": job["mode"],
            "status": job["status"],
            "phase": job["phase"],
            "query": job["query"],
            "execution_adapter": str(request.get("execution_adapter") or "mock"),
            "current_iteration": (
                self._iteration_number(iterations[-1]) if iterations else 0
            ),
            "max_iterations": int(request.get("max_iterations") or 1),
            "stop_reason": stop_reason,
            "goal_reached": (
                bool(summary.get("goal_reached")) if "goal_reached" in summary else None
            ),
            "outcome_severity": outcome_severity,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "return_code": job.get("return_code"),
            "error": job.get("error"),
            "can_cancel": job["status"] in {"queued", "running", "awaiting_observation"},
            "awaiting_observation": waiting and job["status"] in ACTIVE_STATUSES,
        }

    def list_job_views(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [self._job_view(job) for job in self.list_jobs(limit=limit)]

    def _research_view(self, campaign_dir: Path) -> Dict[str, Any]:
        state_path = self._latest_file(campaign_dir, "research_state.json")
        state = self._safe_json(state_path) if state_path is not None else {}
        event = self._as_dict(state.get("event"))
        extensions = self._extension_view(
            state,
            RESEARCH_STABLE_FIELDS,
            RESEARCH_EXTENSION_EXCLUSIONS,
        )
        return {
            "schema_version": "1.0",
            "iteration": self._iteration_number(state_path.parent) if state_path else None,
            "query": str(event.get("query") or state.get("query") or ""),
            "campaign_id": str(state.get("campaign_id") or ""),
            "status": str(state.get("status") or ""),
            "current_branch": str(state.get("current_branch") or ""),
            "next_branch": state.get("next_branch"),
            "last_completed_branch": state.get("last_completed_branch"),
            "route_message": str(state.get("route_message") or ""),
            "branch_history": self._as_list(state.get("branch_history")),
            "current_stage": str(state.get("current_stage") or ""),
            "stage_route": self._as_list(state.get("stage_route")),
            "current_stage_plan": str(state.get("current_stage_plan") or ""),
            "macro_plan": self._as_list(state.get("macro_plan")),
            "stage_route_reason": str(state.get("stage_route_reason") or ""),
            "current_stage_reason": str(state.get("current_stage_reason") or ""),
            "survey_report": self._as_dict(state.get("survey_report")),
            "knowledge_hits": self._as_list(state.get("knowledge_hits")),
            "extracted_protocols": self._as_list(state.get("extracted_protocols")),
            "latest_observation": self._as_dict(state.get("latest_observation")),
            "observations": self._as_list(state.get("observations")),
            "observation_stage_fit": self._as_dict(state.get("observation_stage_fit")),
            "observation_interpretation": self._as_dict(
                state.get("observation_interpretation")
            ),
            "stage_progress": self._as_dict(state.get("stage_progress")),
            "stage_progress_status": str(state.get("stage_progress_status") or ""),
            "post_observation_repair_path": str(
                state.get("post_observation_repair_path") or ""
            ),
            "manual_handoff": str(state.get("manual_handoff") or ""),
            "plan_revisions": self._as_list(state.get("plan_revisions")),
            "created_at": str(state.get("created_at") or ""),
            "errors": self._as_list(state.get("errors")),
            "logs": self._as_list(state.get("logs")),
            "extensions": extensions,
        }

    def _device_view(self, campaign_dir: Path) -> Dict[str, Any]:
        state_path = self._latest_file(campaign_dir, "device_state.json")
        package_path = self._latest_file(campaign_dir, "device_package.json")
        state = self._safe_json(state_path) if state_path is not None else {}
        package = self._safe_json(package_path) if package_path is not None else {}
        if not package:
            package = self._as_dict(state.get("terminal_package"))
        iteration_path = package_path or state_path
        return {
            "schema_version": "1.0",
            "iteration": (
                self._iteration_number(iteration_path.parent) if iteration_path else None
            ),
            "status": str(state.get("status") or package.get("status") or ""),
            "exp_id": str(state.get("exp_id") or package.get("exp_id") or ""),
            "workflow_txt": str(package.get("workflow_txt") or state.get("workflow_txt") or ""),
            "workflow_json": self._as_dict(
                package.get("workflow_json") or state.get("workflow_json")
            ),
            "terminal_package": package,
            "feasibility": self._as_dict(
                package.get("feasibility") or package.get("feasibility_assessment")
            ),
            "device_self_check": self._as_dict(package.get("device_self_check")),
            "reagent_slot_plan": self._as_list(package.get("reagent_slot_plan")),
            "container_plan": self._as_list(package.get("container_plan")),
            "errors": self._as_list(state.get("errors")),
            "logs": self._as_list(state.get("logs")),
            "extensions": self._extension_view(
                state,
                {
                    "status",
                    "exp_id",
                    "workflow_txt",
                    "workflow_json",
                    "terminal_package",
                    "errors",
                    "logs",
                },
                {"terminal_package"},
            ),
        }

    def _plan_revisions(self, campaign_dir: Path) -> List[Any]:
        ledger_path = campaign_dir / "plan_versions.jsonl"
        if not ledger_path.exists():
            return []
        revisions: List[Any] = []
        try:
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                revisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return revisions

    def _iteration_views(self, campaign_dir: Path) -> List[Dict[str, Any]]:
        views: List[Dict[str, Any]] = []
        for iteration_dir in self._iteration_dirs(campaign_dir):
            research = self._safe_json(iteration_dir / "research_state.json")
            device = self._safe_json(iteration_dir / "device_package.json")
            observation = self._safe_json(iteration_dir / "observation_in.json")
            artifacts = sorted(
                path.name for path in iteration_dir.iterdir() if path.is_file()
            )
            views.append(
                {
                    "iteration": self._iteration_number(iteration_dir),
                    "phase": (
                        "awaiting_observation"
                        if (iteration_dir / "AWAITING_OBSERVATION.md").exists()
                        and not observation
                        else "research_update"
                        if research and self._iteration_number(iteration_dir) != 0
                        else "research_bootstrap"
                        if research
                        else "device_mapping"
                        if device
                        else "pending"
                    ),
                    "status": str(
                        research.get("status")
                        or device.get("status")
                        or observation.get("status")
                        or ""
                    ),
                    "research_status": str(research.get("status") or ""),
                    "research_stage": str(research.get("current_stage") or ""),
                    "macro_steps": len(self._as_list(research.get("macro_plan"))),
                    "device_status": str(device.get("status") or ""),
                    "observation": observation,
                    "awaiting_observation": (
                        (iteration_dir / "AWAITING_OBSERVATION.md").exists()
                        and not (iteration_dir / "observation_in.json").exists()
                    ),
                    "artifacts": artifacts,
                }
            )
        return views

    @staticmethod
    def _read_text(path: Path, max_chars: int = 2_000_000) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            return ""

    def get_detail(self, job_id: str) -> Dict[str, Any]:
        job = self.get_job(job_id)
        campaign_dir = Path(job["campaign_dir"])
        waiting_dir = self.find_waiting_iteration(job_id)
        if (
            waiting_dir is not None
            and job["status"] == "running"
            and job["phase"] != "awaiting_observation"
        ):
            self._update_job(
                job_id,
                status="awaiting_observation",
                phase="awaiting_observation",
            )
            self._append_event(
                job_id,
                "phase",
                {
                    "phase": "awaiting_observation",
                    "iteration": self._iteration_number(waiting_dir),
                },
            )
            job = self.get_job(job_id)

        log_path = Path(job["log_path"])
        log_text = self._read_text(log_path)
        log_lines = log_text.splitlines()[-500:]
        try:
            cursor = log_path.stat().st_size if log_path.exists() else 0
        except OSError:
            cursor = 0

        return {
            "job": self._job_view(job),
            "research": self._research_view(campaign_dir),
            "device": self._device_view(campaign_dir),
            "campaign": {
                "schema_version": "1.0",
                "summary": self._safe_json(campaign_dir / "campaign_summary.json"),
                "plan_revisions": self._plan_revisions(campaign_dir),
                "iterations": self._iteration_views(campaign_dir),
                "final_report": self._read_text(campaign_dir / "final_report.md"),
            },
            "logs": {
                "lines": log_lines,
                "cursor": cursor,
            },
        }
