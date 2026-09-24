"""API tests using short-lived fake Agent commands only."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.job_manager import JobConflictError, JobManager, SAFE_ID_RE
from backend.models import CampaignCreate


FAKE_AGENT = r"""
import json
import sys
import time
from pathlib import Path

campaign_dir = Path(sys.argv[1])
mode = sys.argv[2]
query = sys.argv[3]

if "slow" in query:
    print("fake process started", flush=True)
    time.sleep(30)
    raise SystemExit(0)

if mode == "full_campaign" and "mismatch" in query:
    (campaign_dir / "campaign_summary.json").write_text(json.dumps({
        "campaign_id": campaign_dir.name,
        "stop_reason": "goal_reached",
        "goal_reached": True,
        "iterations_run": 0,
    }), encoding="utf-8")
    raise SystemExit(6)

if mode == "full_campaign" and "manual" in query:
    bootstrap = campaign_dir / "iteration_00"
    bootstrap.mkdir(parents=True, exist_ok=True)
    (bootstrap / "research_state.json").write_text(json.dumps({
        "event": {"query": query},
        "campaign_id": campaign_dir.name,
        "status": "completed",
        "current_stage": "synthesis",
        "stage_route": ["synthesis"],
        "macro_plan": [{"step": 1}],
    }), encoding="utf-8")
    iteration = campaign_dir / "iteration_01"
    iteration.mkdir(parents=True, exist_ok=True)
    (iteration / "device_package.json").write_text(json.dumps({
        "status": "success",
        "workflow_txt": "fake workflow",
        "workflow_json": {"steps": [{"step_number": 1}]},
    }), encoding="utf-8")
    (iteration / "AWAITING_OBSERVATION.md").write_text("waiting", encoding="utf-8")
    print("[manual-adapter] waiting for observation file", flush=True)
    observation = iteration / "observation_in.json"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not observation.exists():
        time.sleep(0.02)
    if not observation.exists():
        raise SystemExit(9)
    (iteration / "research_state.json").write_text(json.dumps({
        "event": {"query": query},
        "campaign_id": campaign_dir.name,
        "status": "completed",
        "current_stage": "synthesis",
        "stage_route": ["synthesis"],
        "macro_plan": [],
        "latest_observation": json.loads(observation.read_text(encoding="utf-8")),
    }), encoding="utf-8")
    (campaign_dir / "campaign_summary.json").write_text(json.dumps({
        "campaign_id": campaign_dir.name,
        "stop_reason": "goal_reached",
        "goal_reached": True,
        "iterations_run": 1,
    }), encoding="utf-8")
    (campaign_dir / "final_report.md").write_text("# Fake report\n", encoding="utf-8")
    print("campaign finished", flush=True)
    raise SystemExit(0)

iteration = campaign_dir / "iteration_00"
iteration.mkdir(parents=True, exist_ok=True)
state = {
    "event": {"query": query},
    "campaign_id": campaign_dir.name,
    "status": "completed",
    "current_branch": "B0",
    "next_branch": "B0",
    "last_completed_branch": "B1",
    "route_message": "route selected from local evidence",
    "branch_history": ["B1"],
    "current_stage": "material synthesis",
    "stage_route": ["material synthesis", "characterization"],
    "current_stage_plan": "Prepare a fake sample.",
    "macro_plan": [{"step": 1, "operation": "fake"}],
    "survey_report": {"summary": "offline fake"},
    "stage_progress_status": "in_progress",
    "plan_revisions": [{"plan_version": 1, "event": "initial"}],
    "created_at": "2026-01-01T00:00:00",
    "future_agent_field": {"visible": True},
    "api_key": "must-not-leak",
    "raw_llm_outputs": {"hidden": True},
    "logs": ["fake research completed"],
}
print("starting research agent", flush=True)
(iteration / "research_state.json").write_text(json.dumps(state), encoding="utf-8")
print("fake preview completed", flush=True)
"""


def fake_command_builder(
    job_id: str,
    request: CampaignCreate,
    campaign_dir: Path,
) -> list[str]:
    del job_id
    return [
        sys.executable,
        "-u",
        "-c",
        FAKE_AGENT,
        str(campaign_dir),
        request.mode,
        request.query,
    ]


@pytest.fixture()
def api(tmp_path: Path) -> Iterator[Tuple[TestClient, JobManager]]:
    repo_root = Path(__file__).resolve().parents[1]
    manager = JobManager(
        repo_root=repo_root,
        data_dir=tmp_path / "data",
        command_builder=fake_command_builder,
        cancel_grace_seconds=0.2,
    )
    with TestClient(create_app(manager)) as client:
        yield client, manager


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def _create_body(**overrides) -> dict:
    body = {
        "query": "offline preview",
        "mode": "research_preview",
        "references": [],
        "execution_adapter": "mock",
        "max_iterations": 2,
        "feasibility_deadlock_limit": 2,
        "enable_memory": False,
        "include_device_context": True,
        "online_literature": False,
        "web_search": False,
        "download_pdfs": False,
        "full_workstations": False,
        "wire_api": "chat",
        "reasoning_effort": "xhigh",
    }
    body.update(overrides)
    return body


def test_health_and_workstation_asset(api: Tuple[TestClient, JobManager]) -> None:
    client, _ = api
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    asset = client.get("/api/v1/assets/workstation-map")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/png")
    assert "attachment" not in asset.headers.get("content-disposition", "").lower()
    assert asset.content.startswith(b"\x89PNG")


def test_research_preview_and_five_block_detail(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, manager = api
    created = client.post("/api/v1/campaigns", json=_create_body())
    assert created.status_code == 202
    payload = created.json()
    assert set(payload) == {"job", "research", "device", "campaign", "logs"}
    job_id = payload["job"]["id"]
    assert SAFE_ID_RE.match(job_id)

    manager.wait_for_terminal(job_id)
    detail = client.get(f"/api/v1/campaigns/{job_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["job"]["status"] == "completed"
    assert body["research"]["current_stage"] == "material synthesis"
    assert body["research"]["macro_plan"][0]["operation"] == "fake"
    assert body["research"]["last_completed_branch"] == "B1"
    assert body["research"]["stage_progress_status"] == "in_progress"
    assert body["research"]["plan_revisions"][0]["plan_version"] == 1
    assert body["research"]["extensions"]["future_agent_field"]["visible"] is True
    assert "raw_llm_outputs" not in body["research"]["extensions"]
    assert "api_key" not in body["research"]["extensions"]
    assert not any(key.startswith("A.") or key.startswith("B.") for key in body["research"])
    assert body["campaign"]["iterations"][0]["iteration"] == 0
    assert body["campaign"]["iterations"][0]["phase"] == "research_bootstrap"
    assert body["job"]["current_iteration"] == 0
    assert body["job"]["max_iterations"] == 2
    assert body["job"]["execution_adapter"] == "mock"

    listed = client.get("/api/v1/campaigns")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_research_preview_default_command_keeps_online_sources_disabled(
    tmp_path: Path,
) -> None:
    manager = JobManager(
        repo_root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / "data",
        python_executable="/fake/chemagent-python",
    )
    request = CampaignCreate(**_create_body())
    command = manager._build_command(  # noqa: SLF001 - verifies the CLI safety boundary
        "cmp_20260101_000000_0123456789",
        request,
        manager.campaigns_dir / "preview",
    )
    assert command[0] == "/fake/chemagent-python"
    for flag in (
        "--disable-llm",
        "--no-online-literature",
        "--no-web-search",
        "--no-ledger",
    ):
        assert flag in command
    assert "run_research_agent.py" in command[1]
    knowledge_base_flag = command.index("--knowledge-base-dir")
    assert command[knowledge_base_flag + 1] == str(
        manager.campaigns_dir / "preview" / "knowledge_base"
    )


@pytest.mark.parametrize("mode", ["research_preview", "full_campaign"])
def test_campaign_command_respects_external_source_options(
    tmp_path: Path,
    mode: str,
) -> None:
    manager = JobManager(
        repo_root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / mode,
        python_executable="/fake/chemagent-python",
    )
    request = CampaignCreate(
        **_create_body(
            mode=mode,
            online_literature=True,
            web_search=True,
            download_pdfs=True,
        )
    )

    command = manager._build_command(  # noqa: SLF001 - verifies CLI passthrough
        "cmp_20260101_000000_0123456789",
        request,
        manager.campaigns_dir / mode,
    )

    assert "--online-literature" in command
    assert "--no-online-literature" not in command
    assert "--web-search" in command
    assert "--no-web-search" not in command
    assert "--download-pdfs" in command
    knowledge_base_flag = command.index("--knowledge-base-dir")
    assert command[knowledge_base_flag + 1] == str(
        manager.campaigns_dir / mode / "knowledge_base"
    )


@pytest.mark.parametrize("mode", ["research_preview", "full_campaign"])
def test_campaign_command_omits_pdf_download_by_default(
    tmp_path: Path,
    mode: str,
) -> None:
    manager = JobManager(
        repo_root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / mode,
        python_executable="/fake/chemagent-python",
    )
    request = CampaignCreate(**_create_body(mode=mode))

    command = manager._build_command(  # noqa: SLF001 - verifies CLI passthrough
        "cmp_20260101_000000_0123456789",
        request,
        manager.campaigns_dir / mode,
    )

    assert "--no-online-literature" in command
    assert "--no-web-search" in command
    assert "--download-pdfs" not in command


def test_download_pdf_option_requires_online_literature(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, _ = api
    response = client.post(
        "/api/v1/campaigns",
        json=_create_body(download_pdfs=True, online_literature=False),
    )
    assert response.status_code == 422
    assert "download_pdfs requires online_literature" in response.text


@pytest.mark.parametrize("mode", ["research_preview", "full_campaign"])
def test_command_uses_equals_form_for_dash_prefixed_user_values(
    tmp_path: Path,
    mode: str,
) -> None:
    manager = JobManager(
        repo_root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / mode,
        python_executable="/fake/chemagent-python",
    )
    request = CampaignCreate(
        **_create_body(
            mode=mode,
            query="-dash-prefixed query",
            references=["-dash-prefixed reference"],
        )
    )

    command = manager._build_command(  # noqa: SLF001
        "cmp_20260101_000000_0123456789",
        request,
        manager.campaigns_dir / mode,
    )

    assert "--query=-dash-prefixed query" in command
    assert "--reference=-dash-prefixed reference" in command
    assert "-dash-prefixed query" not in command
    assert "-dash-prefixed reference" not in command


def test_preview_command_can_enable_web_without_scholarly_search(
    tmp_path: Path,
) -> None:
    manager = JobManager(
        repo_root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / "web-only",
        python_executable="/fake/chemagent-python",
    )
    request = CampaignCreate(
        **_create_body(web_search=True, online_literature=False)
    )

    command = manager._build_command(  # noqa: SLF001
        "cmp_20260101_000000_0123456789",
        request,
        manager.campaigns_dir / "web-only",
    )

    assert "--web-search" in command
    assert "--no-online-literature" in command


def test_manual_observation_is_written_atomically(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, manager = api
    created = client.post(
        "/api/v1/campaigns",
        json=_create_body(
            query="manual campaign",
            mode="full_campaign",
            execution_adapter="manual",
        ),
    )
    assert created.status_code == 202
    job_id = created.json()["job"]["id"]
    _wait_until(lambda: manager.find_waiting_iteration(job_id) is not None)

    submitted = client.post(
        f"/api/v1/campaigns/{job_id}/observations",
        json={
            "observation": {
                "summary": "XRD target phase detected",
                "observation_type": "xrd",
                "status": "success",
                "metrics": {"purity": 0.96},
                "notes": "fake result",
            }
        },
    )
    assert submitted.status_code == 202
    assert set(submitted.json()) == {"job", "research", "device", "campaign", "logs"}
    manager.wait_for_terminal(job_id)

    observation_path = (
        manager.campaigns_dir / job_id / "iteration_01" / "observation_in.json"
    )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    assert observation["metrics"]["purity"] == 0.96
    detail = client.get(f"/api/v1/campaigns/{job_id}").json()
    assert detail["campaign"]["summary"]["stop_reason"] == "goal_reached"
    assert detail["campaign"]["final_report"].startswith("# Fake report")

    duplicate = client.post(
        f"/api/v1/campaigns/{job_id}/observations",
        json={"observation": {"summary": "duplicate"}},
    )
    assert duplicate.status_code == 409


def test_process_group_job_can_be_cancelled(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, manager = api
    created = client.post(
        "/api/v1/campaigns",
        json=_create_body(query="slow campaign", mode="full_campaign"),
    )
    job_id = created.json()["job"]["id"]
    _wait_until(lambda: manager.get_job(job_id)["pid"] is not None)

    cancelled = client.post(f"/api/v1/campaigns/{job_id}/cancel")
    assert cancelled.status_code == 202
    terminal = manager.wait_for_terminal(job_id)
    assert terminal["status"] == "cancelled"
    assert terminal["return_code"] is None or terminal["return_code"] < 0


def test_concurrent_observation_has_one_winner(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, manager = api
    created = client.post(
        "/api/v1/campaigns",
        json=_create_body(
            query="manual concurrent campaign",
            mode="full_campaign",
            execution_adapter="manual",
        ),
    )
    job_id = created.json()["job"]["id"]
    _wait_until(lambda: manager.find_waiting_iteration(job_id) is not None)
    barrier = threading.Barrier(2)

    def submit(label: str) -> str:
        barrier.wait(timeout=2)
        try:
            manager.submit_observation(job_id, {"summary": label})
            return "accepted"
        except JobConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(submit, ["first", "second"]))

    assert outcomes == ["accepted", "conflict"]
    manager.wait_for_terminal(job_id)
    observation = json.loads(
        (
            manager.campaigns_dir
            / job_id
            / "iteration_01"
            / "observation_in.json"
        ).read_text(encoding="utf-8")
    )
    assert observation["summary"] in {"first", "second"}


def test_mismatched_domain_exit_code_is_failed(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, manager = api
    created = client.post(
        "/api/v1/campaigns",
        json=_create_body(query="mismatch campaign", mode="full_campaign"),
    )
    job_id = created.json()["job"]["id"]
    terminal = manager.wait_for_terminal(job_id)
    assert terminal["status"] == "failed"
    assert "does not match stop_reason" in terminal["error"]


def test_upload_validation_and_reference_boundary(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, _ = api
    uploaded = client.post(
        "/api/v1/uploads",
        files={"file": ("notes.txt", b"offline reference", "text/plain")},
    )
    assert uploaded.status_code == 201
    upload_body = uploaded.json()["upload"]
    assert Path(upload_body["reference"]).read_bytes() == b"offline reference"

    invalid = client.post(
        "/api/v1/uploads",
        files={"file": ("payload.exe", b"no", "application/octet-stream")},
    )
    assert invalid.status_code == 415

    forbidden = client.post(
        "/api/v1/campaigns",
        json=_create_body(references=["/etc/hosts"]),
    )
    assert forbidden.status_code == 400


def test_sse_uses_default_message_with_full_detail(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, manager = api
    created = client.post("/api/v1/campaigns", json=_create_body(query="sse preview"))
    job_id = created.json()["job"]["id"]
    manager.wait_for_terminal(job_id)

    response = client.get(f"/api/v1/campaigns/{job_id}/events")
    assert response.status_code == 200
    assert "event:" not in response.text
    data_lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert data_lines
    event_detail = json.loads(data_lines[-1])
    assert set(event_detail) == {"job", "research", "device", "campaign", "logs"}
    assert event_detail["job"]["status"] == "completed"
    assert event_detail["research"]["extensions"]["future_agent_field"] == {
        "visible": True
    }


def test_forbidden_execution_adapters_fail_validation(
    api: Tuple[TestClient, JobManager],
) -> None:
    client, _ = api
    for adapter in ("real", "listen"):
        response = client.post(
            "/api/v1/campaigns",
            json=_create_body(mode="full_campaign", execution_adapter=adapter),
        )
        assert response.status_code == 422
