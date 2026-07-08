"""Campaign orchestrator: research -> device -> execution -> research loop.

Automates the previously manual loop: save research state, run the device
agent, wait for the machine result (via an execution adapter), feed the
observation (or device feasibility error) back into research B2, and repeat
until the goal is reached, a human must take over, a feasibility deadlock is
detected, or the iteration budget is exhausted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reaserch_agent.plan_ledger import (  # noqa: E402
    PlanLedger,
    default_ledger_path,
    generate_campaign_id,
)

from .execution_adapters import BaseExecutionAdapter  # noqa: E402

ResearchStepFn = Callable[..., Dict[str, Any]]
DeviceStepFn = Callable[..., Dict[str, Any]]

STOP_GOAL_REACHED = "goal_reached"
STOP_MAX_ITERATIONS = "max_iterations"
STOP_MANUAL_REQUIRED = "manual_required"
STOP_FEASIBILITY_DEADLOCK = "feasibility_deadlock"
STOP_DEVICE_ERROR = "device_error"
STOP_RESEARCH_ERROR = "research_error"


@dataclass
class CampaignConfig:
    query: str
    campaign_id: str = ""
    references: List[str] = field(default_factory=list)
    max_iterations: int = 10
    feasibility_deadlock_limit: int = 3
    campaigns_root: Optional[Path] = None
    research_args: List[str] = field(default_factory=list)
    device_args: List[str] = field(default_factory=list)


@dataclass
class CampaignResult:
    campaign_id: str
    stop_reason: str
    iterations_run: int
    goal_reached: bool
    campaign_dir: str
    final_state_path: str
    final_report_path: str


class CampaignRunner:
    """Drive one campaign end to end with pluggable step functions."""

    def __init__(
        self,
        config: CampaignConfig,
        adapter: BaseExecutionAdapter,
        research_step: ResearchStepFn | None = None,
        device_step: DeviceStepFn | None = None,
    ) -> None:
        self.config = config
        if not self.config.campaign_id.strip():
            self.config.campaign_id = generate_campaign_id(config.query)
        self.campaigns_root = (
            Path(config.campaigns_root).expanduser().resolve()
            if config.campaigns_root
            else REPO_ROOT / "campaigns"
        )
        self.campaign_dir = self.campaigns_root / self.config.campaign_id
        self.adapter = adapter
        self._research_step = research_step or self._research_step_subprocess
        self._device_step = device_step or self._device_step_subprocess
        self._trace: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> CampaignResult:
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().isoformat(timespec="seconds")
        self._log(
            f"campaign {self.config.campaign_id} started: "
            f"max_iterations={self.config.max_iterations}, "
            f"adapter={self.adapter.name}, "
            f"references={len(self.config.references)}"
        )

        bootstrap_dir = self._iteration_dir(0)
        state = self._research_step(
            "bootstrap",
            query=self.config.query,
            previous_state_path=None,
            payload=None,
            iteration_dir=bootstrap_dir,
            references=list(self.config.references),
        )
        state_path = bootstrap_dir / "research_state.json"
        self._trace.append(
            {
                "iteration": 0,
                "phase": "research_bootstrap",
                "status": state.get("status", ""),
                "macro_steps": len(state.get("macro_plan") or []),
            }
        )

        stop_reason = STOP_MAX_ITERATIONS
        goal_reached = False
        iterations_run = 0

        if state.get("status") == "manual_required":
            stop_reason = STOP_MANUAL_REQUIRED
            self._log("bootstrap requires manual intervention; stopping")
        elif state.get("status") != "completed":
            stop_reason = STOP_RESEARCH_ERROR
            self._log(f"bootstrap ended with status={state.get('status')}; stopping")
        else:
            consecutive_feasibility = 0
            for iteration in range(1, self.config.max_iterations + 1):
                iterations_run = iteration
                iteration_dir = self._iteration_dir(iteration)
                self._log(f"iteration {iteration}: device mapping starts")

                package = self._device_step(state_path, iteration_dir)
                package_status = str(package.get("status", "")).strip()
                is_feasibility_error = (
                    package_status == "feasibility_error"
                    or package.get("feedback_type") == "device_feasibility_error"
                )

                if is_feasibility_error:
                    consecutive_feasibility += 1
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": "feasibility_error",
                            "consecutive": consecutive_feasibility,
                        }
                    )
                    self._log(
                        f"iteration {iteration}: device returned feasibility_error "
                        f"({consecutive_feasibility}/{self.config.feasibility_deadlock_limit})"
                    )
                    if consecutive_feasibility >= self.config.feasibility_deadlock_limit:
                        stop_reason = STOP_FEASIBILITY_DEADLOCK
                        self._log(
                            "feasibility deadlock detected; handing over to manual review"
                        )
                        break
                    payload = self._feasibility_payload(package)
                elif package_status == "success":
                    consecutive_feasibility = 0
                    observation = self.adapter.execute(package, iteration_dir)
                    observation_path = iteration_dir / "observation_in.json"
                    observation_path.write_text(
                        json.dumps(observation, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    summary_preview = str(observation.get("summary", ""))[:80]
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "execution",
                            "status": "observation_received",
                            "summary": summary_preview,
                        }
                    )
                    self._log(
                        f"iteration {iteration}: observation received "
                        f"({summary_preview or 'no summary'})"
                    )
                    payload = {"observation": observation}
                else:
                    stop_reason = STOP_DEVICE_ERROR
                    self._trace.append(
                        {
                            "iteration": iteration,
                            "phase": "device",
                            "status": package_status or "unknown",
                        }
                    )
                    self._log(
                        f"iteration {iteration}: device ended with "
                        f"status={package_status or 'unknown'}; stopping"
                    )
                    break

                state = self._research_step(
                    "new_observation",
                    query="",
                    previous_state_path=state_path,
                    payload=payload,
                    iteration_dir=iteration_dir,
                    references=[],
                )
                state_path = iteration_dir / "research_state.json"
                macro_steps = len(state.get("macro_plan") or [])
                self._trace.append(
                    {
                        "iteration": iteration,
                        "phase": "research",
                        "status": state.get("status", ""),
                        "macro_steps": macro_steps,
                        "repair_path": state.get("post_observation_repair_path", ""),
                    }
                )

                if state.get("status") == "manual_required":
                    stop_reason = STOP_MANUAL_REQUIRED
                    self._log(f"iteration {iteration}: research requires manual takeover")
                    break
                if state.get("status") != "completed":
                    stop_reason = STOP_RESEARCH_ERROR
                    self._log(
                        f"iteration {iteration}: research ended with "
                        f"status={state.get('status')}; stopping"
                    )
                    break
                if macro_steps == 0:
                    stop_reason = STOP_GOAL_REACHED
                    goal_reached = True
                    self._log(
                        f"iteration {iteration}: stage route closed with no further "
                        "macro plan; goal reached"
                    )
                    break

        finished_at = datetime.now().isoformat(timespec="seconds")
        report_path = self._write_final_report(
            stop_reason=stop_reason,
            goal_reached=goal_reached,
            iterations_run=iterations_run,
            final_state_path=state_path,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._log(f"campaign finished: stop_reason={stop_reason}")
        return CampaignResult(
            campaign_id=self.config.campaign_id,
            stop_reason=stop_reason,
            iterations_run=iterations_run,
            goal_reached=goal_reached,
            campaign_dir=str(self.campaign_dir),
            final_state_path=str(state_path),
            final_report_path=str(report_path),
        )

    # ------------------------------------------------------------------
    # default subprocess step implementations
    # ------------------------------------------------------------------

    def _research_step_subprocess(
        self,
        event_type: str,
        *,
        query: str,
        previous_state_path: Optional[Path],
        payload: Optional[Dict[str, Any]],
        iteration_dir: Path,
        references: List[str],
    ) -> Dict[str, Any]:
        state_path = iteration_dir / "research_state.json"
        ledger_path = default_ledger_path(
            self.config.campaign_id,
            campaigns_root=self.campaigns_root,
        )
        command = [
            sys.executable,
            str(REPO_ROOT / "reaserch_agent" / "run_research_agent.py"),
            "--event-type",
            event_type,
            "--campaign-id",
            self.config.campaign_id,
            "--ledger-path",
            str(ledger_path),
            "--save-state",
            str(state_path),
        ]
        if query:
            command += ["--query", query]
        if previous_state_path:
            command += ["--previous-state", str(previous_state_path)]
        if payload:
            command += ["--payload-json", json.dumps(payload, ensure_ascii=False)]
        for reference in references:
            command += ["--reference", reference]
        command += self.config.research_args

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        (iteration_dir / "research_step.log").write_text(
            completed.stdout + "\n--- stderr ---\n" + completed.stderr,
            encoding="utf-8",
        )
        if not state_path.exists():
            raise RuntimeError(
                "research step produced no state file "
                f"(exit={completed.returncode}): {completed.stderr[-800:]}"
            )
        return json.loads(state_path.read_text(encoding="utf-8"))

    def _device_step_subprocess(
        self,
        state_path: Path,
        iteration_dir: Path,
    ) -> Dict[str, Any]:
        package_path = iteration_dir / "device_package.json"
        device_state_path = iteration_dir / "device_state.json"
        command = [
            sys.executable,
            str(REPO_ROOT / "device_agent" / "run_from_research_state.py"),
            "--research-state",
            str(state_path),
            "--output",
            str(device_state_path),
            "--package-output",
            str(package_path),
        ]
        command += self.config.device_args

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        (iteration_dir / "device_step.log").write_text(
            completed.stdout + "\n--- stderr ---\n" + completed.stderr,
            encoding="utf-8",
        )
        if not package_path.exists():
            raise RuntimeError(
                "device step produced no package file "
                f"(exit={completed.returncode}): {completed.stderr[-800:]}"
            )
        return json.loads(package_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _iteration_dir(self, iteration: int) -> Path:
        path = self.campaign_dir / f"iteration_{iteration:02d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _feasibility_payload(package: Dict[str, Any]) -> Dict[str, Any]:
        """Trim the device error package into a research B2 payload."""
        trimmed = {
            key: value
            for key, value in package.items()
            if key not in {"macro_plan", "feasibility_assessment"}
        }
        trimmed.setdefault(
            "observation",
            {"summary": "设备适应层返回 feasibility_error，需要研究层修订 macro plan。"},
        )
        return trimmed

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(f"[campaign] {message}", flush=True)
        log_path = self.campaign_dir / "campaign_log.txt"
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _write_final_report(
        self,
        *,
        stop_reason: str,
        goal_reached: bool,
        iterations_run: int,
        final_state_path: Path,
        started_at: str,
        finished_at: str,
    ) -> Path:
        ledger_records = PlanLedger(
            default_ledger_path(
                self.config.campaign_id,
                campaigns_root=self.campaigns_root,
            )
        ).read_all()

        summary = {
            "campaign_id": self.config.campaign_id,
            "query": self.config.query,
            "stop_reason": stop_reason,
            "goal_reached": goal_reached,
            "iterations_run": iterations_run,
            "started_at": started_at,
            "finished_at": finished_at,
            "adapter": self.adapter.name,
            "plan_versions": len(ledger_records),
            "final_state_path": str(final_state_path),
            "trace": self._trace,
        }
        (self.campaign_dir / "campaign_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        lines: List[str] = [
            f"# Campaign Report: {self.config.campaign_id}",
            "",
            f"- query: {self.config.query}",
            f"- stop_reason: **{stop_reason}**",
            f"- goal_reached: {goal_reached}",
            f"- iterations_run: {iterations_run}",
            f"- execution_adapter: {self.adapter.name}",
            f"- started_at: {started_at}",
            f"- finished_at: {finished_at}",
            f"- final_state: {final_state_path}",
            "",
            "## 计划版本演化（含修改/放弃原因）",
            "",
        ]
        if ledger_records:
            lines.append("| 版本 | 事件 | 范围 | 触发 | 原因 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for record in ledger_records:
                reason = str(record.get("reason", "")).replace("|", "/")
                if len(reason) > 160:
                    reason = reason[:160] + "…"
                lines.append(
                    f"| v{record.get('plan_version')} "
                    f"| {record.get('event')} "
                    f"| {record.get('scope')} "
                    f"| {record.get('trigger')} "
                    f"| {reason} |"
                )
        else:
            lines.append("(台账为空)")

        lines += ["", "## 迭代轨迹", ""]
        if self._trace:
            for entry in self._trace:
                detail = ", ".join(
                    f"{key}={value}"
                    for key, value in entry.items()
                    if key not in {"iteration", "phase"}
                )
                lines.append(
                    f"- iteration {entry.get('iteration')} / "
                    f"{entry.get('phase')}: {detail}"
                )
        else:
            lines.append("(无迭代)")

        report_path = self.campaign_dir / "final_report.md"
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path
