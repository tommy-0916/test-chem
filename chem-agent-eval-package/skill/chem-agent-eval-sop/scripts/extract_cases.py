#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

CASE_RE = re.compile(r"^(A01|A02|B01|B02|C01|C02|D01|D02)\b")
EXPECTED = ["A01", "A02", "B01", "B02", "C01", "C02", "D01", "D02"]
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{WORD_NS}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{WORD_NS}t" and node.text:
                pieces.append(node.text)
            elif node.tag == f"{WORD_NS}tab":
                pieces.append("\t")
            elif node.tag == f"{WORD_NS}br":
                pieces.append("\n")
        paragraphs.append("".join(pieces).strip())
    return paragraphs


def extract_cases(path: Path) -> list[dict[str, str]]:
    paragraphs = docx_paragraphs(path)
    cases: list[dict[str, str]] = []
    index = 0
    while index < len(paragraphs):
        heading = paragraphs[index]
        match = CASE_RE.match(heading)
        if not match:
            index += 1
            continue
        case_id = match.group(1)
        query_index = index + 1
        while query_index < len(paragraphs) and not paragraphs[query_index]:
            query_index += 1
        if query_index >= len(paragraphs) or CASE_RE.match(paragraphs[query_index]):
            raise ValueError(f"missing Query after {case_id}")
        query = paragraphs[query_index]
        cases.append(
            {
                "case_id": case_id,
                "title": heading,
                "query": query,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
        )
        index = query_index + 1

    found = [case["case_id"] for case in cases]
    if found != EXPECTED:
        raise ValueError(f"expected {EXPECTED}, found {found}")
    if len({case["query"] for case in cases}) != 8:
        raise ValueError("the DOCX does not contain eight unique Queries")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = extract_cases(args.docx.expanduser().resolve())
    payload = json.dumps(cases, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
