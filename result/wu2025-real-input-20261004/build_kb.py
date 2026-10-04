"""Build the signed Wu-2025 KB for the r14 cross-paper capability round.

Creates result/wu2025-real-input-20261004/kb/ with:
- _pdf_sources/wu2025/<digest>.pdf  (publisher-byte-verified copies)
- registry/papers.jsonl           (one record binding both PDFs to the DOI)
- registry/route_source_identity_evidence_v1/<digest>.*  (fetched evidence)
- registry/route_source_attestations_v1/<paper_key>_<digest>.json  (2 docs)
- a fresh Ed25519 keypair; the PRIVATE key is written OUTSIDE the repo
  (../wu2025-r14-private-key.json, sibling of the repo root) and never
  committed; the public key goes into route-trust-config.json.

Identity evidence (fetched 2026-10-04, recorded under evidence/):
- main paper: Crossref works metadata for 10.1038/s41467-025-58320-5
  (source_host_doi_metadata_match), plus a re-download of
  https://www.nature.com/articles/s41467-025-58320-5.pdf whose sha256 equals
  the local main PDF byte-for-byte (0f1be588...).
- SI: the publisher article page https://www.nature.com/articles/
  s41467-025-58320-5 links 41467_2025_58320_MOESM1_ESM.pdf
  (publisher_si_link); a re-download of that static-content URL is
  byte-identical to the local SI PDF (cdd842d8..., 7,684,073 bytes).
"""
import base64
import json
import shutil
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, ".")
from reaserch_agent.route_attestation import (  # noqa: E402
    SourceDocumentAttestationV1, TrustedAcquisitionEventV1,
    attestation_path_for, attested_route_sources,
)
from reaserch_agent.route_signed_event import (  # noqa: E402
    signed_trusted_acquisition_event_message_v1,
)

ROOT = Path("result/wu2025-real-input-20261004")
KB = ROOT / "kb"
PAPER_ID = "doi_10_1038_s41467_025_58320_5"
DOI = "10.1038/s41467-025-58320-5"
ISSUER = "manual-source-review"
KEY_ID = "wu2025-r14-source-v1"
ISSUED_AT = "2026-10-04T21:30:00Z"

MAIN_SRC = Path("backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-main.pdf")
SI_SRC = Path("backend/data/reference_sources/B01/B01-Wu-2025-NatCommun-SI.pdf")
MAIN_URL = "https://www.nature.com/articles/s41467-025-58320-5.pdf"
SI_URL = ("https://static-content.springer.com/esm/art%3A10.1038%2F"
          "s41467-025-58320-5/MediaObjects/41467_2025_58320_MOESM1_ESM.pdf")
ARTICLE_PAGE = "https://www.nature.com/articles/s41467-025-58320-5"
CROSSREF_URL = "https://api.crossref.org/works/10.1038%2Fs41467-025-58320-5"


def digest_of(path: Path) -> str:
    return "sha256_" + sha256(path.read_bytes()).hexdigest()


def main() -> None:
    main_digest = digest_of(MAIN_SRC)
    si_digest = digest_of(SI_SRC)
    assert main_digest == ("sha256_0f1be588bb891a4e955e5133731295120818a84"
                           "f00a3750e22cf8504d81f1819"), main_digest
    assert si_digest == ("sha256_cdd842d87e34a8dc70d87e5999e8665f5fe054e66"
                         "a22f0908b72ea2953f365d4"), si_digest

    pdf_dir = KB / "_pdf_sources" / "wu2025"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    main_kb = pdf_dir / (main_digest.removeprefix("sha256_") + ".pdf")
    si_kb = pdf_dir / (si_digest.removeprefix("sha256_") + ".pdf")
    shutil.copyfile(MAIN_SRC, main_kb)
    shutil.copyfile(SI_SRC, si_kb)

    # Registry record: one paper, both PDFs, DOI = parent DOI of the SI.
    registry = KB / "registry"
    (registry / "route_source_attestations_v1").mkdir(parents=True,
                                                      exist_ok=True)
    evidence_dir = registry / "route_source_identity_evidence_v1"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "paper_id": PAPER_ID,
        "title": ("Photothermal-promoted anion exchange membrane seawater "
                  "electrolysis on a nickel-molybdenum-based catalyst"),
        "doi": DOI,
        "arxiv_id": "",
        "source": "manual_source_review",
        "url": ARTICLE_PAGE,
        "pdf_url": MAIN_URL,
        "year": "2025",
        "authors": [],
        "verification_status": "verified_doi",
        "full_text_status": "parsed",
        "corpus_files": [],
        "pdf_files": [str(main_kb.resolve()), str(si_kb.resolve())],
        "download_attempts": [],
        "campaigns": {"wu2025_r14": {"role": "", "stage": "",
                                     "added_at": "2026-10-04"}},
        "first_seen_at": "2026-10-04T21:30:00Z",
    }
    (registry / "papers.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    # Identity evidence artifacts, named by their own digest.
    crossref = ROOT / "evidence" / "crossref-works-s41467-025-58320-5.json"
    nature = ROOT / "evidence" / "nature-article-s41467-025-58320-5.html"
    crossref_digest = digest_of(crossref)
    nature_digest = digest_of(nature)
    shutil.copyfile(crossref, evidence_dir / (
        crossref_digest.removeprefix("sha256_") + ".json"))
    shutil.copyfile(nature, evidence_dir / (
        nature_digest.removeprefix("sha256_") + ".html"))

    attestations = []
    for kind, kb_pdf, doc_digest, urls, ev_type, ev_url, ev_digest, doi, pdoi in (
        ("primary_paper", main_kb, main_digest, (ARTICLE_PAGE, MAIN_URL, MAIN_URL),
         "source_host_doi_metadata_match", CROSSREF_URL, crossref_digest, DOI, ""),
        ("supporting_information", si_kb, si_digest, (ARTICLE_PAGE, SI_URL, SI_URL),
         "publisher_si_link", ARTICLE_PAGE, nature_digest, "", DOI),
    ):
        rel = "_pdf_sources/wu2025/" + kb_pdf.name
        att = SourceDocumentAttestationV1(
            schema_version="source_document_attestation_v1",
            paper_id=PAPER_ID, kb_relative_path=rel, document_digest=doc_digest,
            document_kind=kind, doi=doi, parent_doi=pdoi,
            acquisition_url=urls[0], request_url=urls[1], final_url=urls[2],
            identity_evidence_type=ev_type, identity_evidence_url=ev_url,
            identity_evidence_digest=ev_digest, identity_status="verified",
            issuer=ISSUER, issued_at=ISSUED_AT,
        )
        path = attestation_path_for(KB, PAPER_ID, doc_digest)
        path.write_text(json.dumps(att.model_dump(mode="json"),
                                   ensure_ascii=False), encoding="utf-8")
        attestations.append((kind, att))

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

    events = []
    for kind, att in attestations:
        from chem_agent_contracts.v2 import canonical_digest
        att_digest = canonical_digest(att.model_dump(mode="json"))
        event = TrustedAcquisitionEventV1(
            schema_version="trusted_acquisition_event_v1",
            paper_id=PAPER_ID,
            kb_relative_path=att.kb_relative_path,
            document_digest=att.document_digest,
            document_kind=kind,
            attestation_digest=att_digest,
            issuer=ISSUER,
            identity_verdict=(
                "primary_verified" if kind == "primary_paper" else "si_verified"),
        )
        message = signed_trusted_acquisition_event_message_v1(
            key_id=KEY_ID, event=event.model_dump(mode="json"))
        events.append({
            "schema_version": "signed_trusted_acquisition_event_v1",
            "key_id": KEY_ID,
            "event": event.model_dump(mode="json"),
            "signature": base64.b64encode(key.sign(message)).decode(),
        })

    config = {
        "schema_version": "route-trust-config/v1",
        "signed_route_signature_reviews": [],
        "signed_route_source_events": events,
        "trusted_route_capabilities_by_group": [],
        "trusted_route_group_roles_by_group": [],
        "trusted_route_public_keys": {
            KEY_ID: {"allowed_issuer": ISSUER, "public_key_base64": pub_b64},
        },
        "trusted_route_signature_public_keys": {},
    }
    (ROOT / "route-trust-config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")

    # Private key OUTSIDE the repo (sibling of the repo root), never committed.
    priv_path = Path("..") / "wu2025-r14-private-key.json"
    priv_path.write_text(json.dumps({
        "key_id": KEY_ID,
        "private_key_base64": base64.b64encode(key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())).decode(),
        "public_key_base64": pub_b64,
        "note": "r14 Wu-2025 KB signing key; outside the repo; never commit",
    }, indent=2), encoding="utf-8")

    # Verify the whole chain end-to-end.
    from reaserch_agent.route_signed_event import TrustedIssuerPublicKeyV1
    trusted_keys = {
        KEY_ID: TrustedIssuerPublicKeyV1(
            public_key_bytes=base64.b64decode(pub_b64), allowed_issuer=ISSUER),
    }
    sources = attested_route_sources(
        KB, config["signed_route_source_events"],
        trusted_public_keys=trusted_keys,
    )
    found = sources.get(PAPER_ID, [])
    print(json.dumps({
        "attested_sources": [
            {"kind": s.document_kind, "digest": s.document_digest,
             "doi": s.doi, "path": Path(s.path).name}
            for s in found
        ],
        "count": len(found),
    }, indent=2))
    assert len(found) == 2, "both Wu-2025 documents must verify"


if __name__ == "__main__":
    main()
