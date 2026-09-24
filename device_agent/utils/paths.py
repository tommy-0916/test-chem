"""
Project path helpers.
=====================

The original deployment used /workspace paths.  Local development on macOS
keeps the same resources next to device_agent, so these helpers provide one
place for resolving both layouts.
"""

import os
from pathlib import Path


def device_agent_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return device_agent_root().parent


def _env_path(name: str) -> Path:
    return Path(os.environ[name]).expanduser().resolve()


def chem_resources_root() -> Path:
    if os.getenv("CHEM_RESOURCES_DIR"):
        return _env_path("CHEM_RESOURCES_DIR")

    workspace_path = Path("/workspace/chem_resources")
    if workspace_path.exists():
        return workspace_path

    return repo_root() / "chem_resources"


def exp_logs_dir() -> Path:
    if os.getenv("CHEM_EXP_LOG_DIR"):
        return _env_path("CHEM_EXP_LOG_DIR")
    if os.getenv("CHEM_RESOURCES_EXP_LOG_DIR"):
        return _env_path("CHEM_RESOURCES_EXP_LOG_DIR")
    return chem_resources_root() / "exp_logs"


def workstation_dir(use_new_format: bool = True) -> Path:
    env_name = "CHEM_WORKSTATIONS_NEW_DIR" if use_new_format else "CHEM_WORKSTATIONS_DIR"
    if os.getenv(env_name):
        return _env_path(env_name)
    if not use_new_format:
        return chem_resources_root() / "workstations"

    for candidate in (
        chem_resources_root()
        / "lab-design-all"
        / "skills"
        / "chemistry-experiment-workstation",
        chem_resources_root()
        / "lab-design-main"
        / "skills"
        / "chemistry-experiment-workstation",
        chem_resources_root() / "workstations_new",
    ):
        if candidate.exists():
            return candidate
    return chem_resources_root() / "workstations_new"


def format_reference_path(kind: str) -> Path:
    if kind == "txt" and os.getenv("CHEM_TXT_FORMAT_REFERENCE"):
        return _env_path("CHEM_TXT_FORMAT_REFERENCE")
    if kind == "json" and os.getenv("CHEM_JSON_FORMAT_REFERENCE"):
        return _env_path("CHEM_JSON_FORMAT_REFERENCE")
    return chem_resources_root() / "format_reference" / f"reference.{kind}"


def knowledge_agent_dir() -> Path:
    if os.getenv("CHEM_KNOWLEDGE_AGENT_DIR"):
        return _env_path("CHEM_KNOWLEDGE_AGENT_DIR")
    return chem_resources_root() / "knowledge_agent"


def default_env_file() -> Path:
    if os.getenv("CHEM_AGENT_ENV_FILE"):
        return _env_path("CHEM_AGENT_ENV_FILE")

    for candidate in (
        device_agent_root() / ".env",
        repo_root() / ".env",
        Path("/workspace/.env"),
    ):
        if candidate.exists():
            return candidate
    return device_agent_root() / ".env"
