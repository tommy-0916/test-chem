"""Append-only plan version ledger for campaign audit trails.

Every research-layer plan event (initial design, advancement, revision,
closure, abandonment) is persisted as one JSONL line so a human can audit
why each plan version was created, changed, or dropped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGNS_ROOT = REPO_ROOT / "campaigns"

PLAN_EVENT_TYPES = {"initial", "advanced", "revised", "closure", "abandoned"}


def generate_campaign_id(query: str, *, now: datetime | None = None) -> str:
    """Deterministic per query/day: cmp_<YYYYMMDD>_<sha1(query)[:8]>."""
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    digest = hashlib.sha1((query or "").strip().encode("utf-8")).hexdigest()[:8]
    return f"cmp_{stamp}_{digest}"


def default_campaign_dir(
    campaign_id: str,
    campaigns_root: str | Path | None = None,
) -> Path:
    root = (
        Path(campaigns_root).expanduser().resolve()
        if campaigns_root
        else DEFAULT_CAMPAIGNS_ROOT
    )
    return root / campaign_id


def default_ledger_path(
    campaign_id: str,
    campaigns_root: str | Path | None = None,
) -> Path:
    return default_campaign_dir(campaign_id, campaigns_root) / "plan_versions.jsonl"


class PlanLedger:
    """JSONL ledger: one line per plan event."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def append(self, record: Dict[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self.path

    def append_many(self, records: List[Dict[str, Any]]) -> Path:
        for record in records:
            self.append(record)
        return self.path

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            records.append(json.loads(stripped))
        return records
