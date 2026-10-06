"""Independent source acquisition and literal-evidence receipt production.

The planning agent never imports this module. A deployment-operated process
may acquire a PDF, verify a DOI-linked landing page and exact PDF bytes, then
sign a *source identity* event with an external key. That signature says
nothing about route chemistry or permission to run hardware. Chemical route
signatures remain pending independent review under the existing publish gate.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
from statistics import median
import tempfile
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from chem_agent_contracts.route_candidate import RouteCandidateV1
from chem_agent_contracts.v2 import canonical_digest

from .route_attestation import (
    SourceDocumentAttestationV1,
    TrustedAcquisitionEventV1,
    attestation_path_for,
    attested_route_sources,
    verify_source_document_attestation,
)
from .route_pdf_source import verify_route_pdf_source
from .route_signed_event import (
    SIGNED_EVENT_SCHEMA_VERSION_V1,
    TrustedIssuerPublicKeyV1,
    signed_trusted_acquisition_event_message_v1,
    verify_signed_trusted_acquisition_event,
)
from .tools.paper_registry import PaperRegistry


_DOI = re.compile(r"10\.[0-9]{4,9}/\S+\Z", re.IGNORECASE)
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
_MAX_HTML_BYTES = 2 * 1024 * 1024
_MAX_PDF_BYTES = 16 * 1024 * 1024
_CONFIG_FIELDS = frozenset({
    "schema_version", "signed_route_source_events", "trusted_route_public_keys",
    "signed_route_signature_reviews", "trusted_route_signature_public_keys",
    "trusted_route_capabilities_by_group", "trusted_route_group_roles_by_group",
})


@dataclass(frozen=True)
class FetchedDocumentV1:
    final_url: str
    body: bytes


@dataclass(frozen=True)
class SourceIdentityReceiptV1:
    schema_version: str
    verification_mode: str
    method: str
    status: str
    reason_codes: tuple[str, ...]
    paper_id: str
    document_kind: str
    doi: str
    kb_relative_path: str
    document_digest: str
    acquisition_url: str
    request_url: str
    final_url: str
    identity_evidence_url: str
    identity_evidence_digest: str
    attestation_digest: str
    issuer: str
    review_queue: tuple[str, ...]


@dataclass(frozen=True)
class LiteralFieldReceiptV1:
    schema_version: str
    verification_mode: str
    method: str
    status: str
    candidate_digest: str
    document_digest: str
    source_attestation_digest: str
    source_scope: dict[str, Any]
    verified_field_paths: tuple[str, ...]
    verified_evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    review_queue: tuple[str, ...]
    independent_review_request: dict[str, Any]


@dataclass(frozen=True)
class LocalDocumentCrosscheckReceiptV1:
    """A local metadata match that deliberately cannot authorize source use."""

    schema_version: str
    verification_mode: str
    status: str
    metadata_match: bool
    reason_codes: tuple[str, ...]
    doi: str
    local_document_path: str
    document_digest: str
    metadata_url: str
    metadata_digest: str
    historical_acquisition_url: str
    historical_url_fetched_this_run: bool
    review_queue: tuple[str, ...]


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"a", "link"} and values.get("href"):
            self.links.append(str(values["href"]))
        if tag == "meta" and values.get("content") and str(values.get("name", "")).casefold() in {
            "citation_pdf_url", "dc.identifier.uri",
        }:
            self.links.append(str(values["content"]))


def _public_https(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            return False
        host = parts.hostname.casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return _HOST.fullmatch(host) is not None and "." in host
        return address.is_global
    except ValueError:
        return False


def _public_dns_host(url: str) -> bool:
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(
            ipaddress.ip_address(item[4][0]).is_global for item in addresses
        )
    except (OSError, ValueError):
        return False


class _PublicHttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> Request | None:
        if not _public_https(newurl) or not _public_dns_host(newurl):
            raise ValueError("source_redirect_not_public_https")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_https(url: str, maximum_bytes: int) -> FetchedDocumentV1:
    """Fetch bounded HTTPS bytes; reject insecure or local redirect targets."""
    if not _public_https(url) or not _public_dns_host(url):
        raise ValueError("source_url_not_public_https")
    request = Request(url, headers={"User-Agent": "ChemAgentSourceVerifier/1.0"})
    with build_opener(_PublicHttpsRedirect()).open(request, timeout=20) as response:
        final_url = response.geturl()
        if not _public_https(final_url):
            raise ValueError("source_redirect_not_public_https")
        body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ValueError("source_response_too_large")
    return FetchedDocumentV1(final_url=final_url, body=body)


def _digest(raw: bytes) -> str:
    return "sha256_" + sha256(raw).hexdigest()


def _linked_pdf_urls(html: bytes, base_url: str) -> set[str]:
    parser = _Links()
    parser.feed(html.decode("utf-8", errors="replace"))
    return {
        urljoin(base_url, link)
        for link in parser.links
        if _public_https(urljoin(base_url, link))
    }


def _doi_in_page(raw: bytes, doi: str) -> bool:
    text = raw.decode("utf-8", errors="replace").casefold()
    return doi.casefold() in text or quote(doi, safe="").casefold() in text


def _doi_in_pdf(raw: bytes, doi: str) -> bool:
    text = _pdf_identity_text(raw)
    return doi.casefold() in text.casefold()


def _pdf_identity_text(raw: bytes) -> str:
    try:
        import fitz
        with fitz.open(stream=raw, filetype="pdf") as document:
            if document.needs_pass or not document:
                return ""
            return "\n".join(document[index].get_text()
                             for index in range(min(len(document), 2)))
    except Exception:
        return ""


def _tokens(text: str) -> list[str]:
    clean = re.sub(r"<[^>]*>", " ", unescape(text))
    return re.findall(r"[\w]+", clean.casefold(), flags=re.UNICODE)


def _first_page_title_author_blocks(
    pdf: bytes, title: str, author_family: str,
) -> tuple[bool, bool]:
    """Require a title block followed by an author block in the page header.

    Mere occurrence of metadata in the first two pages can be a citation to
    another paper. Ambiguous layouts abstain here and need source review.
    """
    try:
        import fitz
        with fitz.open(stream=pdf, filetype="pdf") as document:
            if document.needs_pass or not document:
                return False, False
            page = document[0]
            data = page.get_text("dict")
            text_blocks: list[tuple[float, float, str, float]] = []
            all_font_sizes: list[float] = []
            for block in data.get("blocks", []):
                if not isinstance(block, dict) or not isinstance(block.get("lines"), list):
                    continue
                chunks: list[str] = []
                sizes: list[float] = []
                for line in block["lines"]:
                    for span in line.get("spans", []):
                        value = str(span.get("text", "")).strip()
                        size = span.get("size")
                        if value and isinstance(size, (float, int)) and 5 <= size <= 40:
                            chunks.append(value)
                            sizes.append(float(size))
                if chunks and sizes:
                    text_blocks.append((
                        float(block["bbox"][1]), float(block["bbox"][3]),
                        " ".join(chunks), median(sizes),
                    ))
                    all_font_sizes.extend(sizes)
            if not all_font_sizes:
                return False, False
            body_size = median(all_font_sizes)
            header_limit = page.rect.height * 0.30
            title_tokens = _tokens(title)
            author_tokens = [item for item in _tokens(author_family) if len(item) >= 3]
            title_blocks = [
                item for item in text_blocks
                if item[0] <= header_limit
                and item[3] >= body_size * 1.10
                and _tokens(item[2]) == title_tokens
            ]
            if not title_blocks:
                return False, False
            for title_block in title_blocks:
                following = sorted((
                    item for item in text_blocks
                    if item[0] >= title_block[1] - 1
                    and item[0] - title_block[1] <= 80
                    and item[0] <= page.rect.height * 0.45
                ), key=lambda item: item[0])[:2]
                if any(
                    author_tokens and all(token in _tokens(item[2]) for token in author_tokens)
                    for item in following
                ):
                    return True, True
            return True, False
    except Exception:
        return False, False


def _crossref_identity(raw: bytes, doi: str, pdf: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
        message = payload["message"]
        if not isinstance(message, dict) or message["DOI"].casefold() != doi.casefold():
            raise ValueError
        title = message["title"][0]
        author = message["author"][0]["family"]
        if not isinstance(title, str) or not isinstance(author, str):
            raise ValueError
    except (KeyError, IndexError, TypeError, UnicodeError, ValueError):
        raise ValueError("doi_metadata_incomplete_or_mismatch") from None
    text = _pdf_identity_text(pdf)
    if doi.casefold() not in text.casefold():
        raise ValueError("doi_missing_from_primary_pdf")
    title_tokens = [item for item in _tokens(title) if len(item) >= 3]
    if len(title_tokens) < 4:
        raise ValueError("pdf_title_doi_metadata_mismatch")
    title_header, author_header = _first_page_title_author_blocks(pdf, title, author)
    if not title_header:
        raise ValueError("pdf_title_doi_metadata_mismatch")
    if not author_header:
        raise ValueError("pdf_author_doi_metadata_mismatch")
    return title


def _inspect_source(
    *, doi: str, document_kind: str, evidence_url: str,
    pdf_url: str, trusted_source_hosts: Sequence[str],
    fetch: Callable[[str, int], FetchedDocumentV1],
) -> tuple[str, str, FetchedDocumentV1, FetchedDocumentV1, str]:
    if _DOI.fullmatch(doi) is None or document_kind not in {
        "primary_paper", "supporting_information",
    }:
        raise ValueError("document_identity_input_invalid")
    if not _public_https(evidence_url) or not _public_https(pdf_url):
        raise ValueError("source_url_not_public_https")
    allowed_hosts = {
        item.casefold() for item in trusted_source_hosts
        if isinstance(item, str) and _HOST.fullmatch(item.casefold())
    }
    crossref_url = "https://api.crossref.org/works/" + quote(doi, safe="")
    if evidence_url == crossref_url:
        if document_kind != "primary_paper":
            raise ValueError("repository_metadata_mode_requires_primary_pdf")
        pdf_host = urlsplit(pdf_url).hostname.casefold()
        if pdf_host not in allowed_hosts:
            raise ValueError("source_pdf_host_untrusted")
        evidence = fetch(crossref_url, _MAX_HTML_BYTES)
        pdf = fetch(pdf_url, _MAX_PDF_BYTES)
        if (urlsplit(evidence.final_url).hostname.casefold() != "api.crossref.org"
            or urlsplit(pdf.final_url).hostname.casefold() not in allowed_hosts
            or any(not _public_https(item.final_url) for item in (evidence, pdf))):
            raise ValueError("source_identity_redirect_untrusted")
        if not pdf.body.startswith(b"%PDF-"):
            raise ValueError("linked_file_not_pdf")
        title = _crossref_identity(evidence.body, doi, pdf.body)
        return (
            "configured_source_host_crossref_pdf_match_v1",
            "source_host_doi_metadata_match", evidence, pdf, title,
        )
    resolver_url = "https://doi.org/" + quote(doi, safe="/")
    resolver = fetch(resolver_url, _MAX_HTML_BYTES)
    evidence = (
        resolver if evidence_url == resolver.final_url
        else fetch(evidence_url, _MAX_HTML_BYTES)
    )
    pdf = fetch(pdf_url, _MAX_PDF_BYTES)
    if any(not _public_https(item.final_url) for item in (resolver, evidence, pdf)):
        raise ValueError("source_redirect_not_public_https")
    resolver_host = urlsplit(resolver.final_url).hostname.casefold()
    evidence_host = urlsplit(evidence.final_url).hostname.casefold()
    if evidence_host == resolver_host:
        method = "doi_resolver_publisher_link_exact_pdf_bytes_v1"
        evidence_type = (
            "publisher_doi_link" if document_kind == "primary_paper"
            else "publisher_si_link"
        )
    else:
        raise ValueError("identity_evidence_host_not_doi_publisher")
    if not _doi_in_page(evidence.body, doi):
        raise ValueError("doi_missing_from_identity_page")
    if pdf_url not in _linked_pdf_urls(evidence.body, evidence.final_url):
        raise ValueError("pdf_not_linked_from_identity_page")
    if not pdf.body.startswith(b"%PDF-"):
        raise ValueError("linked_file_not_pdf")
    if document_kind == "primary_paper" and not _doi_in_pdf(pdf.body, doi):
        raise ValueError("doi_missing_from_primary_pdf")
    return method, evidence_type, evidence, pdf, ""


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError("controlled_artifact_conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".source-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".trust-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _pending_identity(
    *, doi: str, document_kind: str, evidence_url: str, pdf_url: str,
    issuer: str, reason: str,
) -> SourceIdentityReceiptV1:
    return SourceIdentityReceiptV1(
        schema_version="source_identity_receipt_v1", verification_mode="automated",
        method="doi_landing_pdf_link_probe_v1", status="identity_review_pending",
        reason_codes=(reason,), paper_id="", document_kind=document_kind,
        doi=doi, kb_relative_path="", document_digest="",
        acquisition_url=evidence_url, request_url=pdf_url, final_url="",
        identity_evidence_url=evidence_url, identity_evidence_digest="",
        attestation_digest="", issuer=issuer,
        review_queue=("source_identity_independent_review_required",
                      "independent_route_semantics_review_required",
                      "execution_authorization_not_granted"),
    )


def crosscheck_local_document(
    *, doi: str, local_pdf: str | Path, historical_acquisition_url: str,
    fetch: Callable[[str, int], FetchedDocumentV1] = fetch_https,
) -> LocalDocumentCrosscheckReceiptV1:
    """Match DOI metadata to existing bytes without claiming remote origin.

    The old acquisition URL is a historical assertion only. A title, DOI,
    and author printed inside a local PDF can be copied; therefore even a
    successful match stays pending source-identity review and issues no event.
    """
    if _DOI.fullmatch(doi) is None or (
        historical_acquisition_url and not _public_https(historical_acquisition_url)
    ):
        raise ValueError("local_document_crosscheck_input_invalid")
    path = Path(local_pdf).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.casefold() != ".pdf" or (
        path.stat().st_size > _MAX_PDF_BYTES
    ):
        raise ValueError("local_pdf_unavailable_or_too_large")
    raw = path.read_bytes()
    if len(raw) > _MAX_PDF_BYTES or not raw.startswith(b"%PDF-"):
        raise ValueError("local_pdf_format_invalid")
    metadata_url = "https://api.crossref.org/works/" + quote(doi, safe="")
    metadata_digest = ""
    reasons: tuple[str, ...]
    try:
        metadata = fetch(metadata_url, _MAX_HTML_BYTES)
        if not _public_https(metadata.final_url) or (
            urlsplit(metadata.final_url).hostname.casefold() != "api.crossref.org"
        ):
            raise ValueError("doi_metadata_redirect_untrusted")
        metadata_digest = _digest(metadata.body)
        _crossref_identity(metadata.body, doi, raw)
    except ValueError as exc:
        reasons = (str(exc), "remote_acquisition_proof_missing")
    except OSError:
        reasons = ("doi_metadata_fetch_failed", "remote_acquisition_proof_missing")
    else:
        reasons = ("remote_acquisition_proof_missing",)
    return LocalDocumentCrosscheckReceiptV1(
        schema_version="local_document_crosscheck_receipt_v1",
        verification_mode="local_document_metadata_crosscheck",
        status="identity_review_pending", metadata_match=len(reasons) == 1,
        reason_codes=reasons, doi=doi, local_document_path=str(path),
        document_digest=_digest(raw), metadata_url=metadata_url,
        metadata_digest=metadata_digest,
        historical_acquisition_url=historical_acquisition_url,
        historical_url_fetched_this_run=False,
        review_queue=("independent_source_origin_review_required",
                      "independent_route_semantics_review_required",
                      "execution_authorization_not_granted"),
    )


def acquire_source_identity(
    *, kb_root: str | Path, doi: str, title: str, campaign_id: str,
    document_kind: str, evidence_url: str, pdf_url: str, issuer: str,
    trusted_source_hosts: Sequence[str] = (),
    fetch: Callable[[str, int], FetchedDocumentV1] = fetch_https,
) -> SourceIdentityReceiptV1:
    """Download, register, and attest one PDF only after deterministic checks.

    The returned receipt is automated source identity, not chemical review.
    No signature is generated here. A separate source issuer must recheck the
    remote proof and sign the attestation-bound event.
    """
    root = Path(kb_root).expanduser().resolve()
    if not root.is_dir() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", campaign_id):
        raise ValueError("kb_root_or_campaign_invalid")
    if not issuer.strip() or issuer != issuer.strip():
        raise ValueError("issuer_invalid")
    try:
        method, evidence_type, evidence, pdf, crossref_title = _inspect_source(
            doi=doi, document_kind=document_kind, evidence_url=evidence_url,
            pdf_url=pdf_url, trusted_source_hosts=trusted_source_hosts,
            fetch=fetch,
        )
    except ValueError as exc:
        return _pending_identity(
            doi=doi, document_kind=document_kind, evidence_url=evidence_url,
            pdf_url=pdf_url, issuer=issuer, reason=str(exc),
        )
    except OSError:
        return _pending_identity(
            doi=doi, document_kind=document_kind, evidence_url=evidence_url,
            pdf_url=pdf_url, issuer=issuer, reason="source_fetch_failed",
        )
    document_digest = _digest(pdf.body)
    relative = f"_pdf_sources/{campaign_id}/{document_digest[7:]}.pdf"
    source_path = (root / relative).resolve()
    if not source_path.is_relative_to(root):
        raise ValueError("source_outside_kb")
    _atomic_write(source_path, pdf.body)
    record, _ = PaperRegistry(root).upsert(
        title=crossref_title or title.strip() or doi, doi=doi,
        source=("source_host_crossref" if evidence_type == "source_host_doi_metadata_match"
                else "doi_resolver"),
        url=evidence.final_url, pdf_url=pdf_url,
        verification_status="verified_doi", full_text_status="parsed",
        pdf_file=str(source_path), campaign_id=campaign_id,
    )
    paper_id = record["paper_id"]
    issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    artifact = attestation_path_for(root, paper_id, document_digest)
    if artifact.is_file():
        previous = SourceDocumentAttestationV1.model_validate_json(
            artifact.read_text(encoding="utf-8"), strict=True,
        )
        if (
            previous.paper_id != paper_id
            or previous.kb_relative_path != relative
            or previous.document_digest != document_digest
            or previous.document_kind != document_kind
            or (previous.doi or previous.parent_doi) != doi
            or previous.request_url != pdf_url
            or previous.final_url != pdf.final_url
            or previous.identity_evidence_type != evidence_type
            or previous.identity_evidence_url != evidence.final_url
            or previous.identity_evidence_digest != _digest(evidence.body)
            or previous.issuer != issuer
        ):
            raise ValueError("controlled_attestation_conflict")
        issued_at = previous.issued_at
    attestation = SourceDocumentAttestationV1(
        schema_version="source_document_attestation_v1",
        paper_id=paper_id, kb_relative_path=relative,
        document_digest=document_digest, document_kind=document_kind,
        doi=doi if document_kind == "primary_paper" else "",
        parent_doi=doi if document_kind == "supporting_information" else "",
        acquisition_url=pdf_url, request_url=pdf_url,
        final_url=pdf.final_url, identity_evidence_type=evidence_type,
        identity_evidence_url=evidence.final_url,
        identity_evidence_digest=_digest(evidence.body),
        identity_status="verified", issuer=issuer, issued_at=issued_at,
    )
    attestation_payload = attestation.model_dump(mode="json")
    _atomic_write(artifact, json.dumps(
        attestation_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8"))
    evidence_artifact = (
        root / "registry" / "route_source_identity_evidence_v1"
        / f"{_digest(evidence.body)[7:]}.html"
    )
    _atomic_write(evidence_artifact, evidence.body)
    return SourceIdentityReceiptV1(
        schema_version="source_identity_receipt_v1", verification_mode="automated",
        method=method, status="identity_verified_unsigned", reason_codes=(),
        paper_id=paper_id, document_kind=document_kind, doi=doi,
        kb_relative_path=relative, document_digest=document_digest,
        acquisition_url=pdf_url, request_url=pdf_url,
        final_url=pdf.final_url, identity_evidence_url=evidence.final_url,
        identity_evidence_digest=_digest(evidence.body),
        attestation_digest=canonical_digest(attestation_payload), issuer=issuer,
        review_queue=("independent_route_semantics_review_required",
                      "execution_authorization_not_granted"),
    )


def sign_source_identity(
    receipt: SourceIdentityReceiptV1, *, kb_root: str | Path,
    private_key: Any, key_id: str, trusted_source_hosts: Sequence[str] = (),
    fetch: Callable[[str, int], FetchedDocumentV1] = fetch_https,
) -> tuple[dict[str, Any], TrustedIssuerPublicKeyV1]:
    """Independent issuer re-fetches bytes and signs only source identity."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("source_issuer_key_invalid")
    if receipt.schema_version != "source_identity_receipt_v1" or (
        receipt.verification_mode != "automated"
        or receipt.status != "identity_verified_unsigned"
    ):
        raise ValueError("source_receipt_not_verified")
    method, evidence_type, evidence, pdf, _ = _inspect_source(
        doi=receipt.doi, document_kind=receipt.document_kind,
        evidence_url=receipt.identity_evidence_url,
        pdf_url=receipt.request_url,
        trusted_source_hosts=trusted_source_hosts, fetch=fetch,
    )
    if (
        method != receipt.method
        or evidence.final_url != receipt.identity_evidence_url
        or pdf.final_url != receipt.final_url
        or _digest(evidence.body) != receipt.identity_evidence_digest
        or _digest(pdf.body) != receipt.document_digest
    ):
        raise ValueError("source_identity_recheck_mismatch")
    event = TrustedAcquisitionEventV1(
        schema_version="trusted_acquisition_event_v1",
        paper_id=receipt.paper_id, kb_relative_path=receipt.kb_relative_path,
        document_digest=receipt.document_digest,
        document_kind=receipt.document_kind,
        attestation_digest=receipt.attestation_digest, issuer=receipt.issuer,
        identity_verdict=(
            "primary_verified" if receipt.document_kind == "primary_paper"
            else "si_verified"
        ),
    )
    check = verify_source_document_attestation(
        kb_root=kb_root, trusted_event=event,
    )
    if not check.verified:
        raise ValueError("source_attestation_recheck_failed:" + ",".join(check.reasons))
    root = Path(kb_root).expanduser().resolve()
    artifact = attestation_path_for(root, receipt.paper_id, receipt.document_digest)
    attestation = SourceDocumentAttestationV1.model_validate_json(
        artifact.read_text(encoding="utf-8"), strict=True,
    )
    if (
        (attestation.doi or attestation.parent_doi) != receipt.doi
        or attestation.kb_relative_path != receipt.kb_relative_path
        or attestation.document_kind != receipt.document_kind
        or attestation.request_url != receipt.request_url
        or attestation.final_url != receipt.final_url
        or attestation.identity_evidence_url != receipt.identity_evidence_url
        or attestation.identity_evidence_digest != receipt.identity_evidence_digest
        or attestation.identity_evidence_type != evidence_type
        or attestation.acquisition_url != receipt.acquisition_url
        or attestation.issuer != receipt.issuer
    ):
        raise ValueError("source_receipt_attestation_mismatch")
    stored_evidence = (
        root / "registry" / "route_source_identity_evidence_v1"
        / f"{receipt.identity_evidence_digest[7:]}.html"
    )
    if not stored_evidence.is_file() or stored_evidence.read_bytes() != evidence.body:
        raise ValueError("source_identity_evidence_artifact_mismatch")
    payload = event.model_dump(mode="json")
    signature = private_key.sign(signed_trusted_acquisition_event_message_v1(
        key_id=key_id, event=payload,
    ))
    envelope = {
        "schema_version": SIGNED_EVENT_SCHEMA_VERSION_V1,
        "key_id": key_id, "event": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    public = TrustedIssuerPublicKeyV1(
        public_key_bytes=private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
        allowed_issuer=receipt.issuer,
    )
    verified = verify_signed_trusted_acquisition_event(
        envelope=envelope, trusted_public_keys={key_id: public},
    )
    if not verified.verified:
        raise ValueError("new_source_signature_invalid")
    return envelope, public


def audit_literal_pdf_fields(
    candidate: RouteCandidateV1, *, kb_root: str | Path,
    signed_event: Mapping[str, Any], key_id: str,
    public_key: TrustedIssuerPublicKeyV1,
) -> LiteralFieldReceiptV1:
    """Recompute literal field/locator checks; never certify route semantics."""
    source_index = attested_route_sources(
        kb_root, [signed_event], trusted_public_keys={key_id: public_key},
    )
    scope = candidate.source_scope
    source = next((
        item for item in source_index.get(scope.paper_id, [])
        if scope and item.document_digest == scope.source_digest
    ), None) if scope else None
    reasons: tuple[str, ...]
    fields: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    if source is None:
        reasons = ("attested_pdf_source_missing",)
    else:
        result = verify_route_pdf_source(
            candidate, source_paths={scope.paper_id: source.path},
            source_root=kb_root,
        )
        reasons = result.reasons
        fields = result.verified_field_paths
        evidence_ids = result.verified_evidence_ids
    review_request: dict[str, Any] = {}
    if source and scope:
        review_request = {
            "schema_version": "independent_route_review_request_v1",
            "status": "pending_independent_chemical_review",
            "expected_scope": {
                "paper_id": scope.paper_id,
                "experimental_group_id": scope.experimental_group_id,
                "source_digest": scope.source_digest,
                "source_attestation_digest": source.attestation_digest,
                "group_locator": scope.locator,
                "section": scope.section,
                "document_kind": source.document_kind,
                "source_doi": source.doi,
                "target_material": candidate.target.material,
                "target_state": candidate.target.desired_state,
                "target_objective": candidate.target.objective,
            },
            "proposed_route_signature": candidate.route_signature.model_dump(mode="json"),
            "candidate_digest": canonical_digest(candidate),
            "automatic_literal_field_receipt_only": True,
            "required_review_checks": [
                "experimental_group_boundary_and_role",
                "route_signature_chemical_interpretation",
                "cross_group_parameter_isolation",
                "required_capabilities_and_adaptation",
            ],
        }
    return LiteralFieldReceiptV1(
        schema_version="literal_pdf_field_receipt_v1",
        verification_mode="automated",
        method="recompute_pdf_group_locator_excerpt_literal_quantity_v1",
        status=("literal_fields_verified" if source and not reasons and fields
                else "blocked"),
        candidate_digest=canonical_digest(candidate),
        document_digest=source.document_digest if source else "",
        source_attestation_digest=source.attestation_digest if source else "",
        source_scope=scope.model_dump(mode="json") if scope else {},
        verified_field_paths=fields, verified_evidence_ids=evidence_ids,
        reason_codes=(reasons if reasons else (() if fields else
                                    ("no_supported_pdf_fields",))),
        review_queue=("independent_route_semantics_review_required",
                      "execution_authorization_not_granted"),
        independent_review_request=review_request,
    )


def _replace_json(path: Path, value: Any) -> None:
    _atomic_replace(path, json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8"))


def _trust_config(
    path: Path, *, envelope: dict[str, Any],
    key_id: str, public: TrustedIssuerPublicKeyV1,
) -> dict[str, Any]:
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) != _CONFIG_FIELDS or (
            config["schema_version"] != "route-trust-config/v1"
        ):
            raise ValueError("existing_trust_config_invalid")
    else:
        config = {
            "schema_version": "route-trust-config/v1",
            "signed_route_source_events": [], "trusted_route_public_keys": {},
            "signed_route_signature_reviews": [],
            "trusted_route_signature_public_keys": {},
            "trusted_route_capabilities_by_group": [],
            "trusted_route_group_roles_by_group": [],
        }
    key_record = {
        "public_key_base64": base64.b64encode(public.public_key_bytes).decode("ascii"),
        "allowed_issuer": public.allowed_issuer,
    }
    existing = config["trusted_route_public_keys"].get(key_id)
    if existing is not None and existing != key_record:
        raise ValueError("trusted_key_id_conflict")
    config["trusted_route_public_keys"][key_id] = key_record
    identity = (envelope["event"]["paper_id"], envelope["event"]["document_digest"])
    events = config["signed_route_source_events"]
    if any((item.get("event", {}).get("paper_id"),
            item.get("event", {}).get("document_digest")) == identity
           for item in events):
        if envelope in events:
            return config
        raise ValueError("source_identity_already_in_trust_config")
    events.append(envelope)
    return config


def _load_external_source_key(path_text: str, kb_root: str | Path) -> Any:
    from cryptography.hazmat.primitives import serialization

    key_path = Path(path_text).expanduser().resolve(strict=True)
    kb_path = Path(kb_root).expanduser().resolve(strict=True)
    repo_root = Path(__file__).resolve().parent.parent
    if key_path.is_relative_to(kb_path) or key_path.is_relative_to(repo_root):
        raise ValueError("private_key_must_be_outside_kb_and_repository")
    return serialization.load_pem_private_key(key_path.read_bytes(), password=None)


def _write_source_trust_config(
    *, path_text: str, kb_root: str | Path, envelope: dict[str, Any],
    key_id: str, public: TrustedIssuerPublicKeyV1,
) -> Path:
    config_path = Path(path_text).expanduser().resolve()
    kb_path = Path(kb_root).expanduser().resolve(strict=True)
    if config_path.is_relative_to(kb_path):
        raise ValueError("trust_config_must_be_outside_kb")
    config = _trust_config(
        config_path, envelope=envelope, key_id=key_id, public=public,
    )
    _replace_json(config_path, config)
    from .run_research_agent import load_route_trust_config
    load_route_trust_config(str(config_path), str(kb_path))
    return config_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    acquire = commands.add_parser("acquire-source")
    for name in ("kb-root", "doi", "title", "campaign-id", "document-kind",
                 "evidence-url", "pdf-url", "issuer", "receipt-out"):
        acquire.add_argument("--" + name, required=True)
    acquire.add_argument("--trusted-source-host", action="append", default=[])
    prepare = commands.add_parser("prepare-source")
    for name in ("kb-root", "doi", "title", "campaign-id", "document-kind",
                 "evidence-url", "pdf-url", "issuer", "receipt-out",
                 "private-key", "key-id", "trust-config-out"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--trusted-source-host", action="append", default=[])
    sign = commands.add_parser("sign-source")
    for name in ("kb-root", "receipt", "private-key", "key-id", "trust-config-out"):
        sign.add_argument("--" + name, required=True)
    sign.add_argument("--trusted-source-host", action="append", default=[])
    audit = commands.add_parser("audit-fields")
    for name in ("kb-root", "candidate-json", "route-trust-config", "receipt-out"):
        audit.add_argument("--" + name, required=True)
    local = commands.add_parser("crosscheck-local")
    for name in ("doi", "local-pdf", "receipt-out"):
        local.add_argument("--" + name, required=True)
    local.add_argument("--historical-acquisition-url", default="")
    args = parser.parse_args(argv)
    try:
        if args.command in {"acquire-source", "prepare-source"}:
            receipt = acquire_source_identity(
                kb_root=args.kb_root, doi=args.doi, title=args.title,
                campaign_id=args.campaign_id, document_kind=args.document_kind,
                evidence_url=args.evidence_url, pdf_url=args.pdf_url,
                issuer=args.issuer,
                trusted_source_hosts=args.trusted_source_host,
            )
            _replace_json(Path(args.receipt_out), asdict(receipt))
            print(json.dumps({"status": receipt.status, "receipt": args.receipt_out}))
            if args.command == "prepare-source" and receipt.status == "identity_verified_unsigned":
                key = _load_external_source_key(args.private_key, args.kb_root)
                envelope, public = sign_source_identity(
                    receipt, kb_root=args.kb_root, private_key=key,
                    key_id=args.key_id,
                    trusted_source_hosts=args.trusted_source_host,
                )
                config_path = _write_source_trust_config(
                    path_text=args.trust_config_out, kb_root=args.kb_root,
                    envelope=envelope, key_id=args.key_id, public=public,
                )
                print(json.dumps({"status": "signed_source_identity",
                                  "trust_config": str(config_path),
                                  "chemical_review": "pending_independent_review"}))
            elif args.command == "prepare-source":
                return 2
        elif args.command == "sign-source":
            key = _load_external_source_key(args.private_key, args.kb_root)
            raw = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != set(SourceIdentityReceiptV1.__dataclass_fields__):
                raise ValueError("source_receipt_invalid")
            receipt = SourceIdentityReceiptV1(**raw)
            envelope, public = sign_source_identity(
                receipt, kb_root=args.kb_root, private_key=key, key_id=args.key_id,
                trusted_source_hosts=args.trusted_source_host,
            )
            config_path = _write_source_trust_config(
                path_text=args.trust_config_out, kb_root=args.kb_root,
                envelope=envelope, key_id=args.key_id, public=public,
            )
            print(json.dumps({"status": "signed_source_identity",
                              "trust_config": str(config_path),
                              "chemical_review": "pending_independent_review"}))
        elif args.command == "audit-fields":
            candidate = RouteCandidateV1.model_validate_json(
                Path(args.candidate_json).read_text(encoding="utf-8"), strict=True,
            )
            config = json.loads(Path(args.route_trust_config).read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) != _CONFIG_FIELDS:
                raise ValueError("route_trust_config_invalid")
            scope = candidate.source_scope
            matching = [
                event for event in config["signed_route_source_events"]
                if scope and isinstance(event, dict) and isinstance(event.get("event"), dict)
                and event["event"].get("paper_id") == scope.paper_id
                and event["event"].get("document_digest") == scope.source_digest
            ]
            if len(matching) != 1:
                raise ValueError("candidate_source_event_not_unique")
            envelope = matching[0]
            key_id = envelope["key_id"]
            key_record = config["trusted_route_public_keys"][key_id]
            public = TrustedIssuerPublicKeyV1(
                public_key_bytes=base64.b64decode(
                    key_record["public_key_base64"], validate=True,
                ),
                allowed_issuer=key_record["allowed_issuer"],
            )
            receipt = audit_literal_pdf_fields(
                candidate, kb_root=args.kb_root,
                signed_event=envelope, key_id=key_id, public_key=public,
            )
            _replace_json(Path(args.receipt_out), asdict(receipt))
            print(json.dumps({"status": receipt.status, "receipt": args.receipt_out,
                              "review": "pending_independent_chemical_review"}))
        else:
            receipt = crosscheck_local_document(
                doi=args.doi, local_pdf=args.local_pdf,
                historical_acquisition_url=args.historical_acquisition_url,
            )
            _replace_json(Path(args.receipt_out), asdict(receipt))
            print(json.dumps({"status": receipt.status, "receipt": args.receipt_out,
                              "metadata_match": receipt.metadata_match,
                              "reason_codes": receipt.reason_codes}))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "reason_code": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FetchedDocumentV1", "SourceIdentityReceiptV1", "LiteralFieldReceiptV1",
    "LocalDocumentCrosscheckReceiptV1", "crosscheck_local_document",
    "acquire_source_identity", "sign_source_identity", "audit_literal_pdf_fields",
    "fetch_https", "main",
]
