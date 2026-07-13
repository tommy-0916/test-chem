"""FastAPI application for the separated ChemAgent backend."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .job_manager import (
    JobConflictError,
    JobManager,
    JobNotFoundError,
    InvalidReferenceError,
    TERMINAL_STATUSES,
)
from .models import (
    CampaignCreate,
    CampaignDetail,
    CampaignList,
    ObservationSubmit,
    UploadResponse,
)


API_PREFIX = "/api/v1"
UPLOAD_EXTENSIONS = {".pdf", ".txt", ".md", ".json"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


def _cors_origins() -> list[str]:
    raw = os.getenv("CHEMAGENT_CORS_ORIGINS", "").strip()
    if raw:
        return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]


def _default_manager() -> JobManager:
    data_value = os.getenv("CHEMAGENT_BACKEND_DATA_DIR", "").strip()
    data_dir = Path(data_value).expanduser().resolve() if data_value else None
    return JobManager(data_dir=data_dir)


def create_app(manager: Optional[JobManager] = None) -> FastAPI:
    resolved_manager = manager or _default_manager()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        resolved_manager.shutdown()

    application = FastAPI(
        title="ChemAgent Backend",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.job_manager = resolved_manager
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    def get_manager() -> JobManager:
        return application.state.job_manager

    def detail_or_404(job_id: str) -> dict:
        try:
            return get_manager().get_detail(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc

    @application.get(f"{API_PREFIX}/health")
    def health() -> dict:
        healthy = get_manager().health()
        if not healthy:
            raise HTTPException(status_code=503, detail="job database is unavailable")
        return {
            "status": "ok",
            "service": "chemagent-backend",
            "modes": ["research_preview", "full_campaign"],
        }

    @application.get(f"{API_PREFIX}/campaigns", response_model=CampaignList)
    def list_campaigns(limit: int = Query(default=100, ge=1, le=500)) -> dict:
        items = get_manager().list_job_views(limit=limit)
        return {"items": items, "total": len(items)}

    @application.post(
        f"{API_PREFIX}/campaigns",
        response_model=CampaignDetail,
        status_code=202,
    )
    def create_campaign(payload: CampaignCreate) -> dict:
        try:
            job = get_manager().create_campaign(payload)
            return get_manager().get_detail(job["id"])
        except InvalidReferenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="campaign id collision") from exc

    @application.get(
        f"{API_PREFIX}/campaigns/{{job_id}}",
        response_model=CampaignDetail,
    )
    def get_campaign(job_id: str) -> dict:
        return detail_or_404(job_id)

    @application.post(
        f"{API_PREFIX}/campaigns/{{job_id}}/cancel",
        response_model=CampaignDetail,
        status_code=202,
    )
    def cancel_campaign(job_id: str) -> dict:
        try:
            get_manager().cancel(job_id)
            return get_manager().get_detail(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        f"{API_PREFIX}/campaigns/{{job_id}}/observations",
        response_model=CampaignDetail,
        status_code=202,
    )
    def submit_observation(job_id: str, payload: ObservationSubmit) -> dict:
        try:
            get_manager().submit_observation(
                job_id,
                payload.observation.model_dump(mode="json"),
            )
            return get_manager().get_detail(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get(f"{API_PREFIX}/campaigns/{{job_id}}/events")
    async def campaign_events(
        request: Request,
        job_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            get_manager().get_job(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc

        cursor = after
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                pass

        async def event_stream() -> AsyncIterator[str]:
            nonlocal cursor
            previous_payload = ""
            heartbeat_at = asyncio.get_running_loop().time()
            while True:
                if await request.is_disconnected():
                    break
                events = get_manager().get_events(job_id, after=cursor, limit=500)
                if events:
                    cursor = int(events[-1]["seq"])
                detail = get_manager().get_detail(job_id)
                serialized = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
                if events or serialized != previous_payload:
                    previous_payload = serialized
                    # No custom `event:` field: frontend EventSource.onmessage receives this.
                    yield f"id: {cursor}\ndata: {serialized}\n\n"
                    heartbeat_at = asyncio.get_running_loop().time()
                if detail["job"]["status"] in TERMINAL_STATUSES:
                    break
                now = asyncio.get_running_loop().time()
                if now - heartbeat_at >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat_at = now
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.post(
        f"{API_PREFIX}/uploads",
        response_model=UploadResponse,
        status_code=201,
    )
    async def upload_reference(file: UploadFile = File(...)) -> dict:
        original_name = Path(file.filename or "").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in UPLOAD_EXTENSIONS:
            await file.close()
            raise HTTPException(
                status_code=415,
                detail="only pdf, txt, md, and json uploads are accepted",
            )

        upload_id = f"upl_{secrets.token_hex(12)}"
        destination = get_manager().uploads_dir / f"{upload_id}{suffix}"
        temporary = get_manager().uploads_dir / f".{upload_id}.tmp"
        size = 0
        try:
            with temporary.open("xb") as handle:
                while True:
                    chunk = await file.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="upload exceeds the 25 MB limit",
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size == 0:
                raise HTTPException(status_code=400, detail="upload must not be empty")
            os.replace(temporary, destination)
        finally:
            await file.close()
            if temporary.exists():
                temporary.unlink()

        return {
            "upload": {
                "id": upload_id,
                "name": original_name,
                "size": size,
                "content_type": file.content_type or "application/octet-stream",
                "reference": str(destination.resolve()),
            }
        }

    @application.get(f"{API_PREFIX}/assets/workstation-map")
    def workstation_map() -> FileResponse:
        image_path = (
            get_manager().repo_root
            / "chem_resources"
            / "lab-design-main"
            / "skills"
            / "chemistry-experiment-workstation"
            / "img.png"
        )
        if not image_path.exists():
            raise HTTPException(status_code=404, detail="workstation map is unavailable")
        return FileResponse(image_path, media_type="image/png")

    return application


app = create_app()
