"""Enumerate unambiguous experimental groups in registered PDF originals.

The caller supplies the authenticated paper-ID-to-file index.  This module
validates paths under the supplied root, hashes original PDF bytes, and uses
the same page/block parser and heading boundaries as ``route_pdf_source``.
It does not identify a chemical route, assign a group role, or certify that
the local file is a publisher original.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from chem_agent_contracts.route_candidate import ExperimentalGroupScopeV1

from .route_attestation import (
    AttestedRouteSourceV1,
    TrustedAcquisitionEventV1,
    attested_route_sources,
)
from .route_pdf_source import (
    _MAX_SOURCE_BYTES,
    _PdfBlock,
    _group_range,
    _heading,
    _read_pdf_blocks,
)


_EXPERIMENTAL_SECTIONS = frozenset({
    "methods",
    "experimental",
    "experimental section",
    "experimental methods",
    "materials and methods",
    "materials & methods",
    "methods and materials",
})


@dataclass(frozen=True)
class PdfSourceBlockV1:
    locator: str
    text: str


@dataclass(frozen=True)
class PdfExperimentalGroupV1:
    source_scope: ExperimentalGroupScopeV1
    source_document: str
    blocks: tuple[PdfSourceBlockV1, ...]


@dataclass(frozen=True)
class PdfGroupEnumerationDiagnosticV1:
    paper_id: str
    source_document: str
    reason_code: str
    section: str = ""
    experimental_group_id: str = ""


@dataclass
class PdfGroupEnumerationResultV1:
    groups: list[PdfExperimentalGroupV1] = field(default_factory=list)
    diagnostics: list[PdfGroupEnumerationDiagnosticV1] = field(default_factory=list)


@dataclass(frozen=True)
class PdfGroupCoverageReportV1:
    matched_group_count: int
    missing_scopes: tuple[ExperimentalGroupScopeV1, ...]


def _block_locator(first: _PdfBlock, last: _PdfBlock) -> str:
    return f"pdf:p{first.page}:b{first.number}-p{last.page}:b{last.number}"


def _normalized_heading(text: str) -> str:
    return " ".join(text.casefold().split())


def _section_groups(
    paper_id: str, path: Path, digest: str,
    blocks: list[_PdfBlock], section_index: int,
) -> tuple[list[PdfExperimentalGroupV1], str]:
    """Return all groups for one section, or abstain for that whole section."""
    section_block = blocks[section_index]
    section_size = section_block.font_size
    section_end = next((
        index for index in range(section_index + 1, len(blocks))
        if blocks[index].font_size >= section_size - 0.1
    ), len(blocks))
    if section_end < len(blocks) and not _heading(blocks[section_end]):
        return [], "section_boundary_unmarked"

    group_indexes = [
        index for index in range(section_index + 1, section_end)
        if _heading(blocks[index])
        and blocks[index].font_size < section_size - 0.1
    ]
    if not group_indexes:
        return [], "group_heading_missing"
    group_sizes = {round(blocks[index].font_size, 1) for index in group_indexes}
    if len(group_sizes) != 1:
        return [], "nested_or_mixed_group_headings"
    group_names = [_normalized_heading(blocks[index].text) for index in group_indexes]
    if len(group_names) != len(set(group_names)):
        return [], "duplicate_group_heading"

    results: list[PdfExperimentalGroupV1] = []
    for group_index in group_indexes:
        heading = blocks[group_index]
        next_boundary = next((
            index for index in range(group_index + 1, section_end)
            if blocks[index].font_size >= heading.font_size - 0.1
        ), section_end)
        if next_boundary < section_end and next_boundary not in group_indexes:
            return [], "group_boundary_unmarked"
        end_index = next_boundary - 1
        if end_index <= group_index:
            return [], "group_body_missing"
        expected = _group_range(blocks, section_block.text, heading.text)
        if expected != (group_index, end_index):
            return [], "group_boundary_verifier_mismatch"
        scope = ExperimentalGroupScopeV1(
            paper_id=paper_id,
            experimental_group_id=heading.text,
            section=section_block.text,
            locator=_block_locator(heading, blocks[end_index]),
            source_digest=digest,
        )
        results.append(PdfExperimentalGroupV1(
            source_scope=scope,
            source_document=str(path),
            blocks=tuple(
                PdfSourceBlockV1(_block_locator(block, block), block.text)
                for block in blocks[group_index:end_index + 1]
            ),
        ))
    return results, ""


def enumerate_pdf_experimental_groups(
    source_paths: Mapping[str, Sequence[str | Path] | str | Path],
    *,
    source_root: str | Path,
) -> PdfGroupEnumerationResultV1:
    """Create source scopes only from explicitly indexed PDF files.

    The authenticated index must be assembled by trusted ingestion/registry
    code, never copied from model output.  Any ambiguous section is excluded
    in full; diagnostics let the caller preserve abstention across papers.
    """
    result = PdfGroupEnumerationResultV1()
    try:
        root = Path(source_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("source root is not a directory")
    except (OSError, ValueError, TypeError):
        result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
            paper_id="", source_document=str(source_root),
            reason_code="source_root_unavailable",
        ))
        return result

    seen_paths: set[tuple[str, Path]] = set()
    for paper_id, raw_paths in sorted(source_paths.items(), key=lambda item: str(item[0])):
        if not isinstance(paper_id, str) or not paper_id.strip():
            result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                paper_id="", source_document="", reason_code="paper_id_missing",
            ))
            continue
        paths: Sequence[str | Path]
        if isinstance(raw_paths, (str, Path)):
            paths = [raw_paths]
        elif isinstance(raw_paths, Sequence):
            paths = raw_paths
        else:
            paths = []
        if not paths:
            result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                paper_id=paper_id, source_document="",
                reason_code="pdf_source_missing",
            ))
            continue
        for raw_path in paths:
            if not isinstance(raw_path, (str, Path)):
                result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                    paper_id=paper_id, source_document="",
                    reason_code="pdf_path_invalid",
                ))
                continue
            try:
                path = Path(raw_path).expanduser().resolve(strict=True)
                if (paper_id, path) in seen_paths:
                    continue
                seen_paths.add((paper_id, path))
                if not path.is_file() or not path.is_relative_to(root):
                    raise ValueError("path outside source root")
                if path.suffix.lower() != ".pdf":
                    raise ValueError("not PDF extension")
                if path.stat().st_size > _MAX_SOURCE_BYTES:
                    raise OverflowError("source too large")
                raw = path.read_bytes()
                if len(raw) > _MAX_SOURCE_BYTES:
                    raise OverflowError("source too large")
            except OverflowError:
                reason = "pdf_source_too_large"
            except (OSError, ValueError, TypeError):
                reason = "pdf_source_unavailable_or_untrusted"
            else:
                reason = ""
            if reason:
                result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                    paper_id=paper_id, source_document=str(raw_path),
                    reason_code=reason,
                ))
                continue

            digest = "sha256_" + sha256(raw).hexdigest()
            blocks, parse_issue = _read_pdf_blocks(raw)
            if blocks is None:
                result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                    paper_id=paper_id, source_document=str(path),
                    reason_code=parse_issue or "pdf_unreadable",
                ))
                continue
            section_indexes = [
                index for index, block in enumerate(blocks)
                if _heading(block)
                and _normalized_heading(block.text) in _EXPERIMENTAL_SECTIONS
            ]
            if not section_indexes:
                result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                    paper_id=paper_id, source_document=str(path),
                    reason_code="experimental_section_missing",
                ))
                continue
            section_names = [_normalized_heading(blocks[index].text)
                             for index in section_indexes]
            if len(section_names) != len(set(section_names)):
                result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                    paper_id=paper_id, source_document=str(path),
                    reason_code="duplicate_experimental_section",
                ))
                continue
            for section_index in section_indexes:
                groups, issue = _section_groups(
                    paper_id, path, digest, blocks, section_index,
                )
                if issue:
                    result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                        paper_id=paper_id, source_document=str(path),
                        section=blocks[section_index].text,
                        reason_code=issue,
                    ))
                else:
                    result.groups.extend(groups)

    # Route discovery presently keys group identities by paper ID and the
    # original heading. An identically named group in another PDF/section is
    # not silently renamed or merged to make it selectable.
    identities: dict[tuple[str, str], int] = {}
    for group in result.groups:
        key = (group.source_scope.paper_id, group.source_scope.experimental_group_id)
        identities[key] = identities.get(key, 0) + 1
    duplicate_keys = {key for key, count in identities.items() if count > 1}
    if duplicate_keys:
        retained: list[PdfExperimentalGroupV1] = []
        for group in result.groups:
            key = (group.source_scope.paper_id, group.source_scope.experimental_group_id)
            if key in duplicate_keys:
                result.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                    paper_id=key[0], source_document=group.source_document,
                    section=group.source_scope.section,
                    experimental_group_id=key[1],
                    reason_code="duplicate_group_identity_across_sources",
                ))
            else:
                retained.append(group)
        result.groups = retained
    return result


def enumerate_attested_pdf_experimental_groups(
    kb_root: str | Path,
    trusted_events: Sequence[TrustedAcquisitionEventV1] | None,
) -> PdfGroupEnumerationResultV1:
    """Enumerate only groups whose bytes still match trusted receipts.

    ``attested_route_sources`` checks the registry association and an
    independently supplied acquisition/review event.  Enumeration opens the
    PDF again; this second digest check closes the changed-file interval
    between attestation and group parsing.  A path-only index is never source
    identity authority here.
    """
    receipts = attested_route_sources(kb_root, trusted_events)
    if not receipts:
        return PdfGroupEnumerationResultV1(diagnostics=[
            PdfGroupEnumerationDiagnosticV1(
                paper_id="", source_document="",
                reason_code="attested_pdf_source_missing",
            )
        ])
    paths: dict[str, list[Path]] = {}
    exact_receipts: set[tuple[str, Path, str]] = set()
    for paper_id, items in receipts.items():
        for receipt in items:
            if not isinstance(receipt, AttestedRouteSourceV1) or (
                receipt.paper_id != paper_id
            ):
                continue
            try:
                path = receipt.path.resolve(strict=True)
            except (OSError, ValueError):
                continue
            paths.setdefault(paper_id, []).append(path)
            exact_receipts.add((paper_id, path, receipt.document_digest))
    if not exact_receipts:
        return PdfGroupEnumerationResultV1(diagnostics=[
            PdfGroupEnumerationDiagnosticV1(
                paper_id="", source_document="",
                reason_code="attested_pdf_source_missing",
            )
        ])

    enumerated = enumerate_pdf_experimental_groups(paths, source_root=kb_root)
    retained: list[PdfExperimentalGroupV1] = []
    for group in enumerated.groups:
        scope = group.source_scope
        try:
            path = Path(group.source_document).resolve(strict=True)
        except (OSError, ValueError):
            path = Path(group.source_document)
        identity = (scope.paper_id, path, scope.source_digest)
        if identity in exact_receipts:
            retained.append(group)
        else:
            enumerated.diagnostics.append(PdfGroupEnumerationDiagnosticV1(
                paper_id=scope.paper_id,
                source_document=group.source_document,
                section=scope.section,
                experimental_group_id=scope.experimental_group_id,
                reason_code="source_attestation_digest_mismatch",
            ))
    enumerated.groups = retained
    return enumerated


def audit_pdf_group_extraction_coverage(
    enumerated_groups: Sequence[PdfExperimentalGroupV1],
    protocols: Sequence[Mapping[str, Any]],
) -> PdfGroupCoverageReportV1:
    """Report every known group absent from extracted protocol identities.

    This pure check uses only exact paper/group/document-digest triples. A
    paper-level protocol, a group name without a digest, or a group from an
    earlier PDF version cannot make a current source group disappear.
    Group-role classification is downstream; even a characterization group
    must be represented explicitly before it can be excluded as non-route.
    """
    extracted: set[tuple[str, str, str]] = set()
    for protocol in protocols:
        if not isinstance(protocol, Mapping):
            continue
        raw_groups = protocol.get("experimental_groups")
        groups: Sequence[Any] = (
            raw_groups if isinstance(raw_groups, list) else [protocol]
        )
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            paper_id = group.get("paper_id") or protocol.get("paper_id")
            group_id = group.get("experimental_group_id")
            source = group.get("source")
            digest = source.get("source_digest") if isinstance(source, Mapping) else None
            if all(isinstance(item, str) and item.strip()
                   for item in (paper_id, group_id, digest)):
                extracted.add((paper_id, group_id, digest))
    missing = tuple(
        group.source_scope for group in enumerated_groups
        if (
            group.source_scope.paper_id,
            group.source_scope.experimental_group_id,
            group.source_scope.source_digest,
        ) not in extracted
    )
    return PdfGroupCoverageReportV1(
        matched_group_count=len(enumerated_groups) - len(missing),
        missing_scopes=missing,
    )


__all__ = [
    "PdfSourceBlockV1", "PdfExperimentalGroupV1",
    "PdfGroupEnumerationDiagnosticV1", "PdfGroupEnumerationResultV1",
    "PdfGroupCoverageReportV1",
    "enumerate_pdf_experimental_groups",
    "enumerate_attested_pdf_experimental_groups",
    "audit_pdf_group_extraction_coverage",
]
