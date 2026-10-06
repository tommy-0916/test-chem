"""Programmatic manual-supply channel for failed PDF acquisition.

When automated download fails, the workflow must tell the human exactly
what to fetch — title, DOI, document kind, accurate file name, the verified
URL, and why the download failed — then accept the hand-back through the
existing KB ``_pdf_sources`` inbox, verify the supplied bytes against the
request's identity, and only then re-enter the original ingestion entry.
Each stage is recorded separately: received, identity verified or rejected,
ingested or failed.  Rejection records name the failed checks.

This module never mints TrustedAcquisitionEventV1 signatures and never
feeds formal verdicts; it is an ingestion-side intake channel.  Identity
checks fail closed: ambiguity leaves the request unresolved instead of
unlocking ingestion.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from typing import Any, Literal, Mapping

from pydantic import ConfigDict, Field, field_validator, model_validator

from chem_agent_contracts.v2 import StrictModel, canonical_digest

from .route_attestation import _DOI, _valid_https_url
from .route_pdf_groups import enumerate_pdf_experimental_groups
from .route_pdf_source import (
    _furniture_heading_texts,
    _MAX_PDF_SOURCE_BYTES,
    _read_pdf_blocks,
)

_SUPPLY_DIR = Path("registry") / "manual_supply_v1"
# Publisher-neutral running heads that mark an SI document.  Absence fails
# closed: a document without such furniture cannot be accepted as SI here.
_SI_FURNITURE_MARKERS = (
    "supporting information",
    "supplementary information",
    "electronic supplementary",
)


class _SupplyStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ManualSupplyRequestV1(_SupplyStrictModel):
    """Everything a human needs to fetch the exact failed document."""

    schema_version: Literal["manual_supply_request_v1"]
    paper_id: str = Field(min_length=1)
    document_kind: Literal["primary_paper", "supporting_information"]
    title: str = ""
    doi: str = ""
    parent_doi: str = ""
    expected_file_name: str = Field(min_length=1)
    verified_url: str = Field(min_length=1)
    failure_reason: str = Field(min_length=1)

    @field_validator("verified_url")
    @classmethod
    def validate_https(cls, value: str) -> str:
        if not _valid_https_url(value):
            raise ValueError("verified URL must be an HTTPS URL")
        return value

    @model_validator(mode="after")
    def validate_identity_fields(self) -> ManualSupplyRequestV1:
        if self.doi and _DOI.fullmatch(self.doi) is None:
            raise ValueError("doi is invalid")
        if self.parent_doi and _DOI.fullmatch(self.parent_doi) is None:
            raise ValueError("parent_doi is invalid")
        if self.document_kind == "primary_paper":
            if not self.doi or self.parent_doi:
                raise ValueError("primary paper requires DOI and no parent DOI")
        elif not self.parent_doi:
            raise ValueError("supporting information requires parent DOI")
        if not self.failure_reason.strip():
            raise ValueError("failure reason is required")
        return self


class ManualSupplyCheckV1(_SupplyStrictModel):
    name: str = Field(min_length=1)
    outcome: Literal["pass", "fail", "absent"]
    detail: str = ""


class ManualSupplyRecordV1(_SupplyStrictModel):
    """One recorded stage of the manual-supply flow."""

    schema_version: Literal["manual_supply_record_v1"]
    request_digest: str = Field(min_length=1)
    stage: Literal[
        "received", "receive_rejected",
        "identity_verified", "identity_rejected",
        "ingested", "ingestion_failed",
    ]
    supplied_kb_path: str = ""
    supplied_digest: str = ""
    supplied_bytes: int = 0
    checks: tuple[ManualSupplyCheckV1, ...] = ()
    detail: str = ""
    ingested_groups: tuple[str, ...] = ()
    ingested_diagnostics: tuple[str, ...] = ()


def manual_supply_request_digest(request: ManualSupplyRequestV1) -> str:
    return canonical_digest(request.model_dump(mode="json"))


def _request_stem(request: ManualSupplyRequestV1) -> str:
    paper_key = sha256(request.paper_id.encode("utf-8")).hexdigest()
    digest = manual_supply_request_digest(request).removeprefix("sha256_")
    return f"{paper_key}_{digest}"


def write_manual_supply_request(
    kb_root: str | Path, request: ManualSupplyRequestV1
) -> Path:
    """Persist the supply request so the human sees exactly what to fetch."""

    directory = (
        Path(kb_root).expanduser().resolve() / _SUPPLY_DIR / "requests"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_request_stem(request)}.json"
    path.write_text(
        json.dumps(request.model_dump(mode="json"), ensure_ascii=False,
                   indent=2)
        + "\n",
        encoding="utf-8",
    )
    return path


def _append_record(
    kb_root: str | Path,
    request: ManualSupplyRequestV1,
    record: ManualSupplyRecordV1,
) -> Path:
    directory = Path(kb_root).expanduser().resolve() / _SUPPLY_DIR / "records"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_request_stem(request)}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
            + "\n"
        )
    return path


def _record(
    request: ManualSupplyRequestV1, stage: str, **fields: Any
) -> ManualSupplyRecordV1:
    return ManualSupplyRecordV1(
        schema_version="manual_supply_record_v1",
        request_digest=manual_supply_request_digest(request),
        stage=stage,
        **fields,
    )


def _si_furniture_present(furniture: frozenset) -> bool:
    return any(
        marker in text for text in furniture for marker in _SI_FURNITURE_MARKERS
    )


def receive_manual_supply(
    kb_root: str | Path,
    request: ManualSupplyRequestV1,
    supplied_path: str | Path,
) -> ManualSupplyRecordV1:
    """Accept a hand-back into the KB inbox and verify its identity.

    On success the file is copied to ``_pdf_sources/<paper_id>/<sha256>.pdf``
    (the existing upload layout) and the identity stage runs immediately;
    both stages are appended to the request's JSONL record.  A wrong
    document — for example the primary article returned for an SI request —
    is rejected at the identity stage and never unlocks ingestion.
    """

    root = Path(kb_root).expanduser().resolve()
    digest = manual_supply_request_digest(request)
    try:
        source = Path(supplied_path).expanduser().resolve(strict=True)
        raw = source.read_bytes() if source.is_file() else b""
    except (OSError, ValueError, TypeError):
        source, raw = None, b""
    if not raw:
        record = _record(request, "receive_rejected",
                         detail="supplied_file_unavailable")
        _append_record(root, request, record)
        return record
    if len(raw) > _MAX_PDF_SOURCE_BYTES or not raw.startswith(b"%PDF-"):
        record = _record(
            request, "receive_rejected",
            supplied_digest="sha256_" + sha256(raw).hexdigest(),
            supplied_bytes=len(raw),
            detail=(
                "supplied_file_too_large" if len(raw) > _MAX_PDF_SOURCE_BYTES
                else "supplied_file_not_pdf"
            ),
        )
        _append_record(root, request, record)
        return record
    supplied_digest = "sha256_" + sha256(raw).hexdigest()
    inbox = root / "_pdf_sources" / request.paper_id
    inbox.mkdir(parents=True, exist_ok=True)
    kb_path = inbox / f"{supplied_digest.removeprefix('sha256_')}.pdf"
    if not kb_path.is_file():
        shutil.copyfile(source, kb_path)
    received = _record(
        request, "received",
        supplied_kb_path=kb_path.relative_to(root).as_posix(),
        supplied_digest=supplied_digest,
        supplied_bytes=len(raw),
    )
    _append_record(root, request, received)

    blocks, issue = _read_pdf_blocks(raw)
    if blocks is None:
        record = _record(
            request, "identity_rejected",
            supplied_kb_path=received.supplied_kb_path,
            supplied_digest=supplied_digest, supplied_bytes=len(raw),
            detail=f"pdf_unreadable:{issue}",
        )
        _append_record(root, request, record)
        return record
    text = " ".join(block.text for block in blocks).casefold()
    furniture = _furniture_heading_texts(blocks)
    si_marker = _si_furniture_present(furniture)
    expected_doi = (
        request.parent_doi
        if request.document_kind == "supporting_information"
        else request.doi
    )
    checks: list[ManualSupplyCheckV1] = []
    if request.document_kind == "primary_paper":
        # The article must carry its own DOI and must not be the SI.
        checks.append(ManualSupplyCheckV1(
            name="doi_evidence",
            outcome="pass" if expected_doi.casefold() in text else "fail",
            detail=expected_doi,
        ))
        checks.append(ManualSupplyCheckV1(
            name="kind_marker_absent",
            outcome="fail" if si_marker else "pass",
            detail=";".join(sorted(furniture)),
        ))
    else:
        # Wiley-style SIs may omit the parent DOI; presence corroborates but
        # absence is not fatal.  The decisive check is SI furniture, which a
        # primary article does not carry.
        checks.append(ManualSupplyCheckV1(
            name="doi_evidence",
            outcome="pass" if expected_doi.casefold() in text else "absent",
            detail=expected_doi,
        ))
        checks.append(ManualSupplyCheckV1(
            name="kind_marker_present",
            outcome="pass" if si_marker else "fail",
            detail=";".join(sorted(furniture)),
        ))
    failed = any(check.outcome == "fail" for check in checks)
    record = _record(
        request,
        "identity_rejected" if failed else "identity_verified",
        supplied_kb_path=received.supplied_kb_path,
        supplied_digest=supplied_digest, supplied_bytes=len(raw),
        checks=tuple(checks),
        detail="identity_checks_failed" if failed else "",
    )
    _append_record(root, request, record)
    return record


def reenter_ingestion(
    kb_root: str | Path,
    request: ManualSupplyRequestV1,
    identity_record: ManualSupplyRecordV1,
) -> ManualSupplyRecordV1:
    """Re-enter the original ingestion entry after identity verification."""

    if (
        identity_record.stage != "identity_verified"
        or identity_record.request_digest != manual_supply_request_digest(request)
        or not identity_record.supplied_kb_path
    ):
        raise ValueError("ingestion re-entry requires a verified identity")
    root = Path(kb_root).expanduser().resolve()
    result = enumerate_pdf_experimental_groups(
        {request.paper_id: [root / identity_record.supplied_kb_path]},
        source_root=root,
    )
    record = _record(
        request,
        "ingested" if result.groups else "ingestion_failed",
        supplied_kb_path=identity_record.supplied_kb_path,
        supplied_digest=identity_record.supplied_digest,
        supplied_bytes=identity_record.supplied_bytes,
        ingested_groups=tuple(
            group.source_scope.experimental_group_id for group in result.groups
        ),
        ingested_diagnostics=tuple(
            diagnostic.reason_code for diagnostic in result.diagnostics
        ),
        detail="" if result.groups else "no_groups_enumerated",
    )
    _append_record(root, request, record)
    return record


__all__ = [
    "ManualSupplyCheckV1", "ManualSupplyRecordV1", "ManualSupplyRequestV1",
    "manual_supply_request_digest", "receive_manual_supply",
    "reenter_ingestion", "write_manual_supply_request",
]
