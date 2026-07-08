"""Campaign-aware paper registry: dedup, verification tags, provenance.

One JSONL file per knowledge base (``<kb_dir>/registry/papers.jsonl``).
A paper appears once; campaigns referencing it are appended as tags, so
the same literature can serve multiple campaigns without duplication.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

VERIFICATION_RANK = {
    "unverified": 0,
    "web_unverified": 0,
    "verified_semantic_scholar": 1,
    "verified_arxiv": 2,
    "verified_doi": 3,
    "local_file": 3,
}


def normalize_title(title: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", (title or "").lower())


class PaperRegistry:
    """JSONL-backed registry keyed by DOI -> arXiv id -> normalized title."""

    def __init__(self, kb_dir: str | Path) -> None:
        self.kb_dir = Path(kb_dir).expanduser().resolve()
        self.path = self.kb_dir / "registry" / "papers.jsonl"
        self._records: List[Dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        self._records = []
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                self._records.append(record)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(record, ensure_ascii=False)
            for record in self._records
        ]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return self.path

    # ------------------------------------------------------------------
    # lookup / upsert
    # ------------------------------------------------------------------

    def all(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._records]

    def count(self) -> int:
        return len(self._records)

    def find(
        self,
        *,
        doi: str = "",
        arxiv_id: str = "",
        title: str = "",
    ) -> Optional[Dict[str, Any]]:
        doi_key = (doi or "").strip().lower()
        arxiv_key = re.sub(r"v\d+$", "", (arxiv_id or "").strip().lower())
        title_key = normalize_title(title)
        for record in self._records:
            if doi_key and str(record.get("doi", "")).strip().lower() == doi_key:
                return record
            record_arxiv = re.sub(
                r"v\d+$", "", str(record.get("arxiv_id", "")).strip().lower()
            )
            if arxiv_key and record_arxiv == arxiv_key:
                return record
            if title_key and normalize_title(str(record.get("title", ""))) == title_key:
                return record
        return None

    def upsert(
        self,
        *,
        title: str,
        doi: str = "",
        arxiv_id: str = "",
        source: str = "",
        url: str = "",
        pdf_url: str = "",
        year: str = "",
        authors: Optional[List[str]] = None,
        verification_status: str = "unverified",
        full_text_status: str = "metadata_only",
        corpus_file: str = "",
        campaign_id: str = "",
        role: str = "",
        stage: str = "",
    ) -> tuple[Dict[str, Any], bool]:
        """Insert or merge one paper; returns (record, created)."""
        record = self.find(doi=doi, arxiv_id=arxiv_id, title=title)
        created = record is None
        if record is None:
            record = {
                "paper_id": self._make_paper_id(title=title, doi=doi, arxiv_id=arxiv_id),
                "title": title.strip(),
                "doi": (doi or "").strip(),
                "arxiv_id": (arxiv_id or "").strip(),
                "source": source,
                "url": url,
                "pdf_url": pdf_url,
                "year": year,
                "authors": list(authors or []),
                "verification_status": verification_status,
                "full_text_status": full_text_status,
                "corpus_files": [],
                "campaigns": {},
                "first_seen_at": self._now(),
            }
            self._records.append(record)
        else:
            for key, value in [
                ("doi", doi),
                ("arxiv_id", arxiv_id),
                ("source", source),
                ("url", url),
                ("pdf_url", pdf_url),
                ("year", year),
            ]:
                if value and not record.get(key):
                    record[key] = value
            if authors and not record.get("authors"):
                record["authors"] = list(authors)
            if self._verification_rank(verification_status) > self._verification_rank(
                str(record.get("verification_status", "unverified"))
            ):
                record["verification_status"] = verification_status
            if full_text_status == "parsed":
                record["full_text_status"] = "parsed"

        if corpus_file:
            corpus_files = record.setdefault("corpus_files", [])
            if corpus_file not in corpus_files:
                corpus_files.append(corpus_file)
            if record.get("full_text_status") != "parsed":
                record["full_text_status"] = full_text_status

        if campaign_id:
            campaigns = record.setdefault("campaigns", {})
            entry = campaigns.get(campaign_id) or {}
            entry.update(
                {
                    "role": role or entry.get("role", ""),
                    "stage": stage or entry.get("stage", ""),
                    "added_at": entry.get("added_at") or self._now(),
                }
            )
            campaigns[campaign_id] = entry

        self.save()
        return record, created

    def campaign_papers(self, campaign_id: str) -> List[Dict[str, Any]]:
        return [
            dict(record)
            for record in self._records
            if campaign_id in (record.get("campaigns") or {})
        ]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _verification_rank(status: str) -> int:
        return VERIFICATION_RANK.get(status, 0)

    @staticmethod
    def _make_paper_id(*, title: str, doi: str, arxiv_id: str) -> str:
        if doi.strip():
            slug = re.sub(r"[^0-9A-Za-z]+", "_", doi.strip()).strip("_")
            return f"doi_{slug}"[:100]
        if arxiv_id.strip():
            slug = re.sub(r"[^0-9A-Za-z.]+", "_", arxiv_id.strip()).strip("_")
            return f"arxiv_{slug}"[:100]
        digest = hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:12]
        return f"title_{digest}"

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
