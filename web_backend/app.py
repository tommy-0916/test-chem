"""Hardened FastAPI gateway for the Chem Agent web demo.

This service is intentionally conservative:
- requests need a shared access token;
- uploads are limited by extension and size;
- calls are rate-limited per client IP;
- agent work is concurrency-limited and time-limited.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".json"}
DEFAULT_ALLOWED_ORIGINS = "https://echo-hyt.github.io"


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        return max(minimum, int(raw_value))
    except ValueError:
        return default


ACCESS_TOKEN = os.getenv("WEB_ACCESS_TOKEN", "")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS).split(",")
    if origin.strip()
]
MAX_UPLOAD_BYTES = read_int_env("MAX_UPLOAD_MB", 20) * 1024 * 1024
MAX_INSTRUCTION_CHARS = read_int_env("MAX_INSTRUCTION_CHARS", 6000)
RATE_LIMIT_PER_MINUTE = read_int_env("RATE_LIMIT_PER_MINUTE", 3)
AGENT_TIMEOUT_SECONDS = read_int_env("AGENT_TIMEOUT_SECONDS", 180)
MAX_CONCURRENT_TASKS = read_int_env("MAX_CONCURRENT_TASKS", 1)
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"


app = FastAPI(title="Chem Agent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "X-Access-Code"],
)

task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
request_history: dict[str, Deque[float]] = defaultdict(deque)


def get_client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


async def require_access_code(x_access_code: str = Header(default="")) -> None:
    if not ACCESS_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="WEB_ACCESS_TOKEN is not configured on the server.",
        )
    if not secrets.compare_digest(x_access_code, ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid access code.")


async def enforce_rate_limit(request: Request) -> None:
    client_ip = get_client_ip(request)
    now = time.monotonic()
    window_start = now - 60
    history = request_history[client_ip]

    while history and history[0] < window_start:
        history.popleft()

    if len(history) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait before trying again.",
        )

    history.append(now)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/run-agent", dependencies=[Depends(require_access_code), Depends(enforce_rate_limit)])
async def run_agent_endpoint(
    instruction: str = Form(...),
    paper: UploadFile | None = File(default=None),
) -> dict[str, str]:
    clean_instruction = instruction.strip()
    if not clean_instruction:
        raise HTTPException(status_code=400, detail="Instruction is required.")
    if len(clean_instruction) > MAX_INSTRUCTION_CHARS:
        raise HTTPException(status_code=413, detail="Instruction is too long.")

    async with task_semaphore:
        with tempfile.TemporaryDirectory(prefix="chem-agent-upload-") as temp_dir:
            paper_path = await save_upload_safely(paper, Path(temp_dir)) if paper else None
            try:
                result = await asyncio.wait_for(
                    run_agent_safely(clean_instruction, paper_path),
                    timeout=AGENT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(status_code=504, detail="Agent execution timed out.") from exc

    return {"result": result}


async def save_upload_safely(paper: UploadFile, temp_dir: Path) -> Path:
    original_name = Path(paper.filename or "uploaded_file").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    output_path = temp_dir / f"paper{suffix}"
    total_size = 0
    with output_path.open("wb") as output_file:
        while True:
            chunk = await paper.read(1024 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Uploaded file is too large.")
            output_file.write(chunk)

    await paper.close()
    return output_path


async def run_agent_safely(instruction: str, paper_path: Path | None) -> str:
    """Call the real agent here after deployment is stable.

    Keep this function free of shell string concatenation. Prefer importing Python
    modules directly, or use subprocess with a fixed argv list and a strict timeout.
    """
    await asyncio.sleep(0.2)
    file_line = f"上传文献: {paper_path.name}" if paper_path else "未上传文献"
    return "\n".join(
        [
            "安全后端已收到请求。",
            "",
            f"任务指令: {instruction}",
            file_line,
            "",
            "当前是安全网关 mock 模式。下一步可以在 run_agent_safely() 中接入真实 chem-agent。",
        ]
    )
