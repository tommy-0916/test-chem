#!/usr/bin/env python3
"""CLI for adding papers and external metadata to the research knowledge base."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent.tools.ingestion import ExternalKnowledgeClient, KnowledgeIngestion


DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "chem_kb"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest local PDF/JSON/TXT files or external scholarly metadata into "
            "the research agent knowledge-base JSON schema."
        )
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Local file or directory to ingest. Can be repeated.",
    )
    parser.add_argument(
        "--query",
        help="External paper search query. Searches selected --sources.",
    )
    parser.add_argument(
        "--sources",
        default="arxiv,crossref,semantic_scholar",
        help=(
            "Comma-separated external sources: arxiv, crossref, semantic_scholar, "
            "openalex, pubmed, google_scholar (google_scholar needs SERPER_API_KEY)."
        ),
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Maximum external results per source. Default: 5.",
    )
    parser.add_argument(
        "--download-pdfs",
        action="store_true",
        help="Download open PDFs when an external source exposes a PDF URL.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_KNOWLEDGE_DIR),
        help=f"Knowledge-base output directory. Default: {DEFAULT_KNOWLEDGE_DIR}",
    )
    parser.add_argument(
        "--memory-store-dir",
        help="Optional chem memory store root used with --add-to-memory.",
    )
    parser.add_argument(
        "--add-to-memory",
        action="store_true",
        help="Also add ingested protocols to LayeredChemMemory literature layer.",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recurse into local input directories.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned output paths without writing files.",
    )
    parser.add_argument(
        "--print-summary-json",
        action="store_true",
        help="Print machine-readable ingestion summary.",
    )
    return parser


def parse_sources(raw_sources: str) -> List[str]:
    sources = [source.strip() for source in raw_sources.split(",") if source.strip()]
    if not sources:
        raise SystemExit("--sources must contain at least one source")
    return sources


def main() -> int:
    args = build_parser().parse_args()
    if not args.input and not args.query:
        raise SystemExit("Provide at least one --input path or an external --query.")

    ingestion = KnowledgeIngestion(
        args.output_dir,
        memory_root=args.memory_store_dir,
        add_to_memory=args.add_to_memory,
        dry_run=args.dry_run,
    )

    local_written: List[Path] = []
    for input_path in args.input:
        local_written.extend(
            ingestion.ingest_path(
                input_path,
                recursive=not args.no_recursive,
            )
        )

    external_written: List[Path] = []
    external_count = 0
    external_errors: List[str] = []
    if args.query:
        client = ExternalKnowledgeClient(
            semantic_scholar_api_key=os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""),
            crossref_mailto=os.getenv("CROSSREF_MAILTO", ""),
        )
        papers = client.search(
            args.query,
            sources=parse_sources(args.sources),
            max_results=args.max_results,
        )
        external_count = len(papers)
        external_errors = list(client.last_errors)
        external_written.extend(
            ingestion.ingest_external_papers(
                papers,
                download_pdfs=args.download_pdfs,
                pdf_dir=Path(args.output_dir) / "_pdf_sources",
            )
        )

    summary = {
        "status": "dry_run" if args.dry_run else "completed",
        "output_dir": str(Path(args.output_dir).expanduser().resolve()),
        "local_records_written": [str(path) for path in local_written],
        "external_records_found": external_count,
        "external_records_written": [str(path) for path in external_written],
        "external_errors": external_errors,
        "add_to_memory": bool(args.add_to_memory),
    }

    print(
        "ingestion completed: "
        f"local={len(local_written)}, external={len(external_written)}, "
        f"output_dir={summary['output_dir']}",
        flush=True,
    )
    if args.print_summary_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    elif external_errors:
        print("external source warnings:", flush=True)
        for item in external_errors:
            print(f"- {item}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
