"""Campaign-aware paper registry: dedup, verification tags, provenance.

One JSONL file per knowledge base (``<kb_dir>/registry/papers.jsonl``).
A paper appears once; campaigns referencing it are appended as tags, so
the same literature can serve multiple campaigns without duplication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import fcntl

VERIFICATION_RANK = {
    "unverified": 0,
    "web_unverified": 0,
    "verified_semantic_scholar": 1,
    "verified_arxiv": 2,
    "verified_doi": 3,
    "local_file": 3,
}
FULL_TEXT_RANK = {
    "metadata_only": 0,
    "download_planned": 0,
    "download_failed": 1,
    "parse_failed": 1,
    "parsed": 2,
}


def normalize_title(title: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", (title or "").lower())


class PaperRegistry:
    """JSONL-backed registry keyed by DOI -> arXiv id -> normalized title."""

    def __init__(self, kb_dir: str | Path) -> None:
        self.kb_dir = Path(kb_dir).expanduser().resolve()
        self.path = self.kb_dir / "registry" / "papers.jsonl"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
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
        with self._exclusive_lock():
            return self._save_unlocked()

    def _save_unlocked(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(record, ensure_ascii=False)
            for record in self._records
        ]
        payload = "\n".join(lines) + ("\n" if lines else "")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".papers.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)
        return self.path

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # lookup / upsert
    # ------------------------------------------------------------------

    def all(self) -> List[Dict[str, Any]]:
        self._load()
        return [dict(record) for record in self._records]

    def count(self) -> int:
        self._load()
        return len(self._records)

    def find(
        self,
        *,
        doi: str = "",
        arxiv_id: str = "",
        title: str = "",
        year: str = "",
        authors: Optional[List[str]] = None,
        source: str = "",
        verification_status: str = "",
    ) -> Optional[Dict[str, Any]]:
        self._load()
        return self._find_in_memory(
            doi=doi,
            arxiv_id=arxiv_id,
            title=title,
            year=year,
            authors=authors,
            source=source,
            verification_status=verification_status,
        )

    def _find_in_memory(
        self,
        *,
        doi: str = "",
        arxiv_id: str = "",
        title: str = "",
        year: str = "",
        authors: Optional[List[str]] = None,
        source: str = "",
        verification_status: str = "",
    ) -> Optional[Dict[str, Any]]:
        doi_key = (doi or "").strip().lower()
        arxiv_key = re.sub(r"v\d+$", "", (arxiv_id or "").strip().lower())
        title_key = normalize_title(title)
        requested_namespace = (
            self._identity_namespace(source, verification_status)
            if source or verification_status
            else ""
        )

        def namespace_matches(record: Dict[str, Any]) -> bool:
            if not requested_namespace:
                return True
            return self._identity_namespace(
                str(record.get("source") or ""),
                str(record.get("verification_status") or ""),
            ) == requested_namespace

        if doi_key:
            for record in self._records:
                if (
                    namespace_matches(record)
                    and str(record.get("doi", "")).strip().lower() == doi_key
                ):
                    return record
        if arxiv_key:
            for record in self._records:
                if not namespace_matches(record):
                    continue
                record_arxiv = re.sub(
                    r"v\d+$", "", str(record.get("arxiv_id", "")).strip().lower()
                )
                if record_arxiv != arxiv_key:
                    continue
                if self._record_identity_conflicts(record, doi=doi, arxiv_id=arxiv_id):
                    continue
                return record

        candidates: List[Dict[str, Any]] = []
        for record in self._records:
            if not namespace_matches(record):
                continue
            if not title_key or normalize_title(str(record.get("title", ""))) != title_key:
                continue
            if self._record_identity_conflicts(record, doi=doi, arxiv_id=arxiv_id):
                continue
            record_year = str(record.get("year") or "")
            if year and record_year and str(year) != record_year:
                continue
            incoming_author = self._first_author_key(authors or [])
            record_author = self._first_author_key(record.get("authors") or [])
            if incoming_author and record_author and incoming_author != record_author:
                continue
            candidates.append(record)
        return candidates[0] if len(candidates) == 1 else None

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
        pdf_file: str = "",
        download_attempts: Optional[List[Dict[str, Any]]] = None,
        campaign_id: str = "",
        role: str = "",
        stage: str = "",
    ) -> tuple[Dict[str, Any], bool]:
        """Atomically insert or merge one paper; returns (record, created)."""
        with self._exclusive_lock():
            self._load()
            result = self._upsert_unlocked(
                title=title,
                doi=doi,
                arxiv_id=arxiv_id,
                source=source,
                url=url,
                pdf_url=pdf_url,
                year=year,
                authors=authors,
                verification_status=verification_status,
                full_text_status=full_text_status,
                corpus_file=corpus_file,
                pdf_file=pdf_file,
                download_attempts=download_attempts,
                campaign_id=campaign_id,
                role=role,
                stage=stage,
            )
            self._save_unlocked()
            return result

    def _upsert_unlocked(
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
        pdf_file: str = "",
        download_attempts: Optional[List[Dict[str, Any]]] = None,
        campaign_id: str = "",
        role: str = "",
        stage: str = "",
    ) -> tuple[Dict[str, Any], bool]:
        record = self._find_in_memory(
            doi=doi,
            arxiv_id=arxiv_id,
            title=title,
            year=year,
            authors=authors,
            source=source,
            verification_status=verification_status,
        )
        created = record is None
        if record is None:
            record = {
                "paper_id": self._make_paper_id(
                    title=title,
                    doi=doi,
                    arxiv_id=arxiv_id,
                    source=source,
                    url=url,
                    verification_status=verification_status,
                ),
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
                "pdf_files": [],
                "download_attempts": [],
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
            current_full_text = str(record.get("full_text_status", "metadata_only"))
            if self._full_text_rank(full_text_status) > self._full_text_rank(
                current_full_text
            ):
                record["full_text_status"] = full_text_status

        if corpus_file:
            corpus_files = record.setdefault("corpus_files", [])
            if corpus_file not in corpus_files:
                corpus_files.append(corpus_file)
            if self._full_text_rank(full_text_status) >= self._full_text_rank(
                str(record.get("full_text_status", "metadata_only"))
            ):
                record["full_text_status"] = full_text_status

        if pdf_file:
            pdf_files = record.setdefault("pdf_files", [])
            if pdf_file not in pdf_files:
                pdf_files.append(pdf_file)

        if download_attempts:
            attempts = record.setdefault("download_attempts", [])
            for attempt in download_attempts:
                normalized = dict(attempt)
                if normalized not in attempts:
                    attempts.append(normalized)
            record["download_attempts"] = attempts[-50:]

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

        return record, created

    def campaign_papers(self, campaign_id: str) -> List[Dict[str, Any]]:
        self._load()
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
    def _full_text_rank(status: str) -> int:
        return FULL_TEXT_RANK.get(status, 0)

    @staticmethod
    def _record_identity_conflicts(
        record: Dict[str, Any],
        *,
        doi: str,
        arxiv_id: str,
    ) -> bool:
        record_doi = str(record.get("doi") or "").strip().lower()
        incoming_doi = str(doi or "").strip().lower()
        if record_doi and incoming_doi and record_doi != incoming_doi:
            return True
        record_arxiv = re.sub(
            r"v\d+$", "", str(record.get("arxiv_id") or "").strip().lower()
        )
        incoming_arxiv = re.sub(r"v\d+$", "", str(arxiv_id or "").strip().lower())
        return bool(record_arxiv and incoming_arxiv and record_arxiv != incoming_arxiv)

    @staticmethod
    def _first_author_key(authors: List[str]) -> str:
        if not authors:
            return ""
        tokens = re.findall(r"[0-9a-z一-鿿]+", str(authors[0]).lower())
        return tokens[-1] if tokens else ""

    @staticmethod
    def _identity_namespace(source: str, verification_status: str) -> str:
        normalized_source = str(source or "").strip().lower()
        normalized_verification = str(verification_status or "").strip().lower()
        if normalized_verification == "web_unverified" or normalized_source.startswith(
            "web:"
        ):
            return "web"
        if normalized_verification == "local_file" or normalized_source.startswith(
            "local"
        ):
            return "local"
        return "academic"

    @staticmethod
    def _make_paper_id(
        *,
        title: str,
        doi: str,
        arxiv_id: str,
        source: str = "",
        url: str = "",
        verification_status: str = "",
    ) -> str:
        if doi.strip():
            slug = re.sub(r"[^0-9A-Za-z]+", "_", doi.strip()).strip("_")
            return f"doi_{slug}"[:100]
        if arxiv_id.strip():
            slug = re.sub(r"[^0-9A-Za-z.]+", "_", arxiv_id.strip()).strip("_")
            return f"arxiv_{slug}"[:100]
        namespace = PaperRegistry._identity_namespace(source, verification_status)
        identity = url.strip().lower() if namespace == "web" and url.strip() else title
        normalized_identity = (
            identity if namespace == "web" else normalize_title(identity)
        )
        digest = hashlib.sha1(
            f"{namespace}:{normalized_identity}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{namespace}_title_{digest}"

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
