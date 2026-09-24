"""Strip task/device context out of literature search queries (issue 1).

User queries mix two kinds of information: the chemistry to research and the
lab-automation task context ("基于现有自动化化学工作站…下发到303实验室…").
The second kind must never reach scholarly search engines — it drags in
irrelevant papers (e.g. electrical-engineering "automation systems") and
dilutes ranking. This module removes ONLY the task/device context, never the
chemistry, and never mutates the original query stored in state.

Deterministic and dependency-free so both the LLM and heuristic planning
paths, plus the online literature-acquisition lines, share one guarantee.
"""

from __future__ import annotations

import re
from typing import Iterable, List

# Issue 8 P0-2: whole task-dispatch CLAUSES must go first — word-level removal
# alone leaves fragments like "请将实验 ，并返回该实验任务的 id。" behind, and
# those fragments still poison scholarly ranking. Clause patterns run before
# word patterns and delete from the clause opener to the sentence boundary.
_CLAUSE_PATTERNS: List[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # 请将实验下发至 303 实验室 / 请把任务提交到工作站 …(到句末)
        r"请?[将把]?\s*(?:该|本|此)?(?:实验|任务|方案)[^。！？!?；;]*?"
        r"(?:下发|发送|提交|派发|部署)[^。！？!?；;]*[。！？!?；;]?",
        # 并返回(该)实验任务的 id / 返回任务编号 …(到句末)
        r"并?且?\s*返回[^。！？!?；;]*?(?:任务|实验|id|ID|编号)[^。！？!?；;]*[。！？!?；;]?",
        # 请在 303 实验室(的自动化平台)上执行/运行/… — delete ONLY the
        # prefix + verb; the verb's object is often the chemistry itself
        # (…执行 NiCo EOR 对照实验) and must survive.
        r"请?在[^。！？!?；;]{0,20}实验室[^。！？!?；;]{0,15}?(?:执行|运行|完成|开展|进行)",
    )
]

# Longest-first so 自动化化学工作站 is removed as one phrase before 工作站/自动化
# would leave fragments behind. Chemistry survives by construction: e.g.
# 双工位电化学工作站 loses 双工位/工作站 but keeps 电化学.
_CONTEXT_PATTERNS: List[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # -- device / automation nouns --------------------------------------
        r"自动化化学工作站",
        r"化学自动化工作站",
        r"自动化工作站",
        # 电化学工作站 must survive as 电化学 → only strip 工作站 there;
        # the lookbehind keeps 化学工作站 from eating 电[化学工作站].
        r"(?<!电)化学工作站",
        r"智能化学工作站",
        r"工作站",
        r"自动化平台",
        r"自动化",
        r"智能科学家",
        r"机器化学家",
        r"机器人",
        r"机械臂",
        r"双工位",
        r"设备清单",
        r"设备",
        r"automated\s+chemistry\s+workstations?",
        r"chemistry\s+workstations?",
        r"workstations?",
        r"automation",
        r"automated",
        r"robotic\s+arms?",
        r"robotics?",
        r"robots?",
        r"devices?",
        # -- lab identifiers -------------------------------------------------
        r"[0-9]{1,4}\s*号?\s*实验室",
        r"实验室\s*[0-9]{1,4}\s*号?",
        r"实验室编号\s*[A-Za-z0-9-]*",
        r"lab(?:oratory)?\s*[0-9]{1,4}",
        r"[0-9]{1,4}\s*lab(?:oratory)?",
        # -- task dispatch / system instructions -----------------------------
        r"下发(?:任务|到|至)?",
        r"返回(?:实验)?任务\s*(?:id|ID|编号)?",
        r"(?:实验)?任务\s*(?:id|ID|编号)",
        r"task\s*id",
        r"dispatch(?:ed|ing)?",
        r"请(?:帮我)?(?:设计|规划|生成|给出|下发)(?:一个|一份|一条)?(?:实验方案|实验|方案|任务)?",
        r"帮我(?:设计|规划|生成)(?:一个|一份)?",
        r"我(?:希望|想要|想|需要)",
        r"设计(?:一个|一份|一条|一套)",
        r"基于(?:现有|当前|已有)",
        r"使用现有",
    )
]

# Punctuation/space runs left behind after removals.
_DANGLING_RE = re.compile(r"[，,、;；:：/|]{2,}")
_EDGE_PUNCT_RE = re.compile(r"^[\s，,、;；:：/|.·-]+|[\s，,、;；:：/|·-]+$")
_TRAILING_CONNECTOR_RE = re.compile(r"(?:并且|并|以及|及|和|与|的)+$")
_SPACE_RUN_RE = re.compile(r"\s{2,}")
_EMPTY_BRACKETS_RE = re.compile(r"[（(]\s*[)）]|\[\s*\]|【\s*】")

# Tokens that alone do not identify any chemistry — a query reduced to only
# these carries no material/reaction entity and must be dropped entirely.
_GENERIC_TOKENS = {
    "experiment", "experiments", "design", "designs", "plan", "planning",
    "optimization", "optimisation", "system", "systems", "task", "tasks",
    "study", "research", "method", "methods", "process",
    "实验", "设计", "方案", "优化", "系统", "任务", "研究", "过程", "方法",
    "合成实验", "上进行", "进行", "一个", "一份",
}


def removed_context_terms(text: str) -> List[str]:
    """Which context fragments would be stripped — for logs and tests."""
    found: List[str] = []
    remaining = str(text or "")
    for pattern in _CONTEXT_PATTERNS:
        for match in pattern.findall(remaining):
            if match and match not in found:
                found.append(match)
    return found


def sanitize_search_query(text: str) -> str:
    """Return a chemistry-only version of ``text`` for scholarly search.

    The input is never mutated; callers keep the original query intact for
    state/UI. Removal is phrase-based (longest first) plus punctuation
    cleanup, so chemistry entities (NiFe-PBA, 电化学, XRD…) survive.
    """
    cleaned = str(text or "")
    for pattern in _CLAUSE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    for pattern in _CONTEXT_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _EMPTY_BRACKETS_RE.sub(" ", cleaned)
    cleaned = _DANGLING_RE.sub("，", cleaned)
    cleaned = _SPACE_RUN_RE.sub(" ", cleaned)
    cleaned = _EDGE_PUNCT_RE.sub("", cleaned)
    cleaned = _TRAILING_CONNECTOR_RE.sub("", cleaned)
    return cleaned.strip()


def _has_chemistry_entity(query: str) -> bool:
    """True when at least one token is not generic filler.

    Tokens are ASCII words or CJK runs; ``NiFe``, ``XRD``, ``普鲁士蓝`` count,
    while a residue like "experiment design" does not.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9()\-]*|[一-鿿]+", query)
    for token in tokens:
        lowered = token.lower()
        if lowered in _GENERIC_TOKENS:
            continue
        # CJK runs may be a concatenation of generic fillers (设计实验 etc.)
        if re.fullmatch(r"[一-鿿]+", token):
            residue = token
            for generic in sorted(_GENERIC_TOKENS, key=len, reverse=True):
                residue = residue.replace(generic, "")
            if not residue:
                continue
        return True
    return False


def sanitize_search_queries(queries: Iterable[str]) -> List[str]:
    """Sanitize a query list: strip context, drop empties and duplicates.

    A query that sanitizes down to pure filler ("experiment design") carries
    no material/reaction entity and is dropped outright.
    """
    seen = set()
    result: List[str] = []
    for query in queries or []:
        cleaned = sanitize_search_query(query)
        if len(cleaned) < 2 or not _has_chemistry_entity(cleaned):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
