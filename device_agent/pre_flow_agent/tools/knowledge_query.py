"""
Knowledge Query 工具
====================

当前阶段尚未接入正式知识库服务。
这里使用临时知识文本快照作为替代数据源，并提供一个简单的 query -> 文本片段检索接口。
"""

import os
import re
from typing import Dict, List, Tuple


class KnowledgeQuery:
    KNOWLEDGE_DIR = "/workspace/chem_resources/knowledge_agent"
    SOURCE_FILES = {
        "knowledge": "knowledge.txt",
        "summary": "summary.txt",
        "paper_workflows": "expriment_workflow_paper.txt",
    }

    def __init__(self, use_knowledge_agent: bool = False):
        self._use_knowledge_agent = use_knowledge_agent

    def _read_file(self, filename: str) -> str:
        filepath = os.path.join(self.KNOWLEDGE_DIR, filename)
        if not os.path.exists(filepath):
            return ""
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()

    def get_knowledge(self) -> str:
        if self._use_knowledge_agent:
            raise NotImplementedError("Knowledge Agent not implemented yet")
        return self._read_file(self.SOURCE_FILES["knowledge"])

    def get_summary(self) -> str:
        if self._use_knowledge_agent:
            raise NotImplementedError("Knowledge Agent not implemented yet")
        return self._read_file(self.SOURCE_FILES["summary"])

    def get_paper_workflows(self) -> str:
        if self._use_knowledge_agent:
            raise NotImplementedError("Knowledge Agent not implemented yet")
        return self._read_file(self.SOURCE_FILES["paper_workflows"])

    def get_all(self) -> Dict[str, str]:
        return {
            "knowledge": self.get_knowledge(),
            "summary": self.get_summary(),
            "paper_workflows": self.get_paper_workflows(),
        }

    def search(self, query: str, max_chars: int = 5000, max_chunks: int = 5) -> str:
        if self._use_knowledge_agent:
            raise NotImplementedError("Knowledge Agent not implemented yet")

        sources = self.get_all()
        if not any(sources.values()):
            return ""

        tokens = self._tokenize(query)
        candidates: List[Tuple[int, str, str]] = []
        for source_name, text in sources.items():
            for chunk in self._split_chunks(text):
                score = self._score_chunk(chunk, tokens)
                if score > 0:
                    candidates.append((score, source_name, chunk))

        if not candidates:
            for source_name, text in sources.items():
                for chunk in self._split_chunks(text)[:2]:
                    candidates.append((0, source_name, chunk))

        candidates.sort(key=lambda item: (item[0], len(item[2])), reverse=True)
        selected = []
        total_chars = 0
        for _, source_name, chunk in candidates:
            block = f"### {source_name}\n{chunk.strip()}"
            if block in selected:
                continue
            if total_chars + len(block) > max_chars and selected:
                break
            selected.append(block)
            total_chars += len(block)
            if len(selected) >= max_chunks:
                break

        return "\n\n".join(selected)[:max_chars]

    def _tokenize(self, text: str) -> List[str]:
        raw_tokens = re.findall(r"[A-Za-z0-9_+\-.]+|[\u4e00-\u9fff]{1,8}", text or "")
        tokens = []
        for token in raw_tokens:
            lowered = token.lower().strip()
            if not lowered:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]{1}", lowered):
                continue
            tokens.append(lowered)
        return list(dict.fromkeys(tokens))

    def _split_chunks(self, text: str) -> List[str]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
        if paragraphs:
            return paragraphs
        return [line.strip() for line in (text or "").splitlines() if line.strip()]

    def _score_chunk(self, chunk: str, tokens: List[str]) -> int:
        if not chunk:
            return 0
        if not tokens:
            return 1
        lowered = chunk.lower()
        score = 0
        for token in tokens:
            if token in lowered:
                score += 3 if len(token) > 2 else 1
        return score