"""CLI for the minimal paper knowledge-base MVP."""

from __future__ import annotations

import argparse
import json

from .mvp import DEFAULT_OUTPUT_DIR, KnowledgeBaseBuilder, KnowledgeBaseStore


def build_command(args: argparse.Namespace) -> int:
    builder = KnowledgeBaseBuilder(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        selection_file=args.selection_file,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        min_chunk_chars=args.min_chunk_chars,
    )
    result = builder.build()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def search_command(args: argparse.Namespace) -> int:
    store = KnowledgeBaseStore.load(args.output_dir)
    if args.render:
        print(
            store.render_search_result(
                args.query,
                top_k=args.top_k,
                max_chars=args.max_chars,
                max_hits_per_doc=args.max_hits_per_doc,
            )
        )
        return 0
    print(
        json.dumps(
            store.search(
                args.query,
                top_k=args.top_k,
                max_hits_per_doc=args.max_hits_per_doc,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def stats_command(args: argparse.Namespace) -> int:
    store = KnowledgeBaseStore.load(args.output_dir)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def inspect_chunk_command(args: argparse.Namespace) -> int:
    store = KnowledgeBaseStore.load(args.output_dir)
    chunk = store.get_chunk(args.chunk_id)
    print(json.dumps(chunk, ensure_ascii=False, indent=2))
    return 0


def inspect_doc_command(args: argparse.Namespace) -> int:
    store = KnowledgeBaseStore.load(args.output_dir)
    doc = store.get_doc(args.doc_id)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal paper knowledge-base MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build the MVP knowledge base")
    build_parser.add_argument("--raw-dir", default=None)
    build_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    build_parser.add_argument("--selection-file", default=None)
    build_parser.add_argument("--chunk-size", type=int, default=1100)
    build_parser.add_argument("--chunk-overlap", type=int, default=180)
    build_parser.add_argument("--min-chunk-chars", type=int, default=160)
    build_parser.set_defaults(func=build_command)

    search_parser = subparsers.add_parser("search", help="Search the MVP knowledge base")
    search_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.add_argument("--max-hits-per-doc", type=int, default=2)
    search_parser.add_argument("--render", action="store_true")
    search_parser.add_argument("--max-chars", type=int, default=1200)
    search_parser.set_defaults(func=search_command)

    stats_parser = subparsers.add_parser("stats", help="Show knowledge base stats")
    stats_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    stats_parser.set_defaults(func=stats_command)

    inspect_chunk_parser = subparsers.add_parser("inspect-chunk", help="Inspect one chunk")
    inspect_chunk_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    inspect_chunk_parser.add_argument("--chunk-id", required=True)
    inspect_chunk_parser.set_defaults(func=inspect_chunk_command)

    inspect_doc_parser = subparsers.add_parser("inspect-doc", help="Inspect one document")
    inspect_doc_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    inspect_doc_parser.add_argument("--doc-id", required=True)
    inspect_doc_parser.set_defaults(func=inspect_doc_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "build" and getattr(args, "raw_dir", None) is None:
        args.raw_dir = str(KnowledgeBaseBuilder().raw_dir)
    if args.command == "build" and getattr(args, "selection_file", None) is None:
        args.selection_file = str(KnowledgeBaseBuilder().selection_file)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
