"""Optional jieba + BM25 retrieval scoring with deterministic fallbacks.

``jieba`` and ``rank-bm25`` are declared in requirements but may be absent
in minimal environments; every entry point degrades gracefully so offline
heuristic runs and tests behave identically without them.

Chemical formulas (``K3Fe(CN)6``, ``mAh g-1``) are kept as single latin
tokens so they never get shredded by CJK segmentation.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

try:  # pragma: no cover - environment dependent
    import jieba  # type: ignore
except ImportError:  # pragma: no cover
    jieba = None

try:  # pragma: no cover - environment dependent
    from rank_bm25 import BM25Okapi  # type: ignore
except ImportError:  # pragma: no cover
    BM25Okapi = None

_LATIN_TOKEN_RE = re.compile(r"[0-9a-z][0-9a-z()\-\.·]{1,}")
_CJK_CHUNK_RE = re.compile(r"[一-鿿]+")


def jieba_available() -> bool:
    return jieba is not None


def bm25_available() -> bool:
    return BM25Okapi is not None


def tokenize_mixed(text: str) -> List[str]:
    """Latin/chemical tokens via regex; CJK via jieba (bigrams fallback)."""
    lowered = (text or "").lower()
    tokens: List[str] = _LATIN_TOKEN_RE.findall(lowered)
    for chunk in _CJK_CHUNK_RE.findall(lowered):
        tokens.extend(_tokenize_cjk_chunk(chunk))
    return tokens


def _tokenize_cjk_chunk(chunk: str) -> List[str]:
    if jieba is not None:
        words = [word.strip() for word in jieba.cut(chunk) if len(word.strip()) >= 2]
        if words:
            return words
    if len(chunk) < 2:
        return [chunk] if chunk else []
    return [chunk[index : index + 2] for index in range(len(chunk) - 1)]


def build_bm25_boosts(
    queries: Sequence[str],
    documents: Sequence[str],
    *,
    max_boost: float = 6.0,
) -> Optional[List[float]]:
    """Per-document BM25 boost normalized to [0, max_boost].

    Returns None when rank-bm25 is unavailable so callers keep their
    existing deterministic scoring unchanged.
    """
    if BM25Okapi is None or not documents:
        return None
    query_tokens: List[str] = []
    for query in queries:
        query_tokens.extend(tokenize_mixed(query))
    if not query_tokens:
        return None
    corpus_tokens = [tokenize_mixed(document) or [""] for document in documents]
    index = BM25Okapi(corpus_tokens)
    raw_scores = list(index.get_scores(query_tokens))
    top = max(raw_scores) if raw_scores else 0.0
    if top <= 0:
        query_set = set(query_tokens)
        raw_scores = [
            float(len(query_set.intersection(document_tokens)))
            for document_tokens in corpus_tokens
        ]
        top = max(raw_scores) if raw_scores else 0.0
        if top <= 0:
            return [0.0] * len(documents)
    return [max_boost * (score / top) for score in raw_scores]
