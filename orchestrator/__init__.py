"""Campaign orchestration package: closes the research ⇄ device ⇄ execution loop."""

from .execution_adapters import (
    BaseExecutionAdapter,
    ExecutionBlockedError,
    ListenExecutionAdapter,
    ManualExecutionAdapter,
    MockExecutionAdapter,
    RealExecutionAdapter,
    build_adapter,
)
from .runner import CampaignConfig, CampaignResult, CampaignRunner

__all__ = [
    "BaseExecutionAdapter",
    "CampaignConfig",
    "CampaignResult",
    "CampaignRunner",
    "ExecutionBlockedError",
    "ListenExecutionAdapter",
    "ManualExecutionAdapter",
    "MockExecutionAdapter",
    "RealExecutionAdapter",
    "build_adapter",
]
