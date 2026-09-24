"""Lightweight mem0-style local memory for chemistry agents.

The implementation keeps the public shape close to mem0's OSS API while
remaining dependency-free and deterministic for local tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


TOKEN_RE = re.compile(r"[A-Za-z0-9\-\+\./]+|[\u4e00-\u9fff]+")
ENTITY_FILTER_KEYS = {"user_id", "agent_id", "run_id"}


class ChemMemoryLayer:
    """Canonical memory layer names used by chem-agent."""

    EXPERIMENT = "experiment"
    LITERATURE = "literature"

    ALL = (EXPERIMENT, LITERATURE)

    @classmethod
    def normalize(cls, layer: str) -> str:
        value = (layer or "").strip().lower()
        aliases = {
            "exp": cls.EXPERIMENT,
            "experimental": cls.EXPERIMENT,
            "实验": cls.EXPERIMENT,
            "实验层": cls.EXPERIMENT,
            "paper": cls.LITERATURE,
            "papers": cls.LITERATURE,
            "literature": cls.LITERATURE,
            "文献": cls.LITERATURE,
            "文献层": cls.LITERATURE,
        }
        normalized = aliases.get(value, value)
        if normalized not in cls.ALL:
            raise ValueError(
                f"Unsupported memory layer {layer!r}; expected one of {cls.ALL}"
            )
        return normalized


@dataclass
class ChemMemoryRecord:
    """Stored memory item."""

    id: str
    layer: str
    memory: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    hash: str = ""
    scope_key: str = ""
    created_at: str = ""
    updated_at: str = ""
    score: Optional[float] = None
    matched_terms: List[str] = field(default_factory=list)

    def to_result(self, include_score: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "layer": self.layer,
            "memory": self.memory,
            "hash": self.hash,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_score and self.score is not None:
            payload["score"] = self.score
        if self.matched_terms:
            payload["matched_terms"] = list(self.matched_terms)
        return payload


class ChemMemoryStore:
    """SQLite-backed local memory with mem0-like operations.

    It deliberately stores the memory text and metadata in a simple table and
    uses deterministic token scoring rather than an external vector service.
    """

    def __init__(self, root_dir: str | Path | None = None, db_name: str = "chem_memory.sqlite") -> None:
        self.root_dir = Path(root_dir) if root_dir is not None else self.default_root()
        self.db_path = self.root_dir / db_name
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    @staticmethod
    def default_root() -> Path:
        return Path(__file__).resolve().parents[1] / "chem_memory"

    def add(
        self,
        messages: Any,
        *,
        layer: str,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        infer: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Create memories from text or chat messages.

        `infer` is accepted for API compatibility with mem0. This local
        version stores the supplied non-system messages directly.
        """

        normalized_layer = ChemMemoryLayer.normalize(layer)
        base_metadata = self._build_metadata(
            metadata=metadata,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
        )
        scope_key = self._build_scope_key(base_metadata)
        normalized_messages = self._normalize_messages(messages)

        results: List[Dict[str, Any]] = []
        for message in normalized_messages:
            if message.get("role") == "system":
                continue
            content = str(message.get("content", "")).strip()
            if not content:
                continue

            per_message_metadata = dict(base_metadata)
            role = message.get("role")
            if role:
                per_message_metadata["role"] = role
            actor_id = message.get("name") or message.get("actor_id")
            if actor_id:
                per_message_metadata["actor_id"] = actor_id

            record, event = self._insert_memory(
                normalized_layer,
                content,
                per_message_metadata,
                scope_key,
            )
            item = record.to_result(include_score=False)
            item["event"] = event
            results.append(item)

        return {"results": results}

    def get(self, memory_id: str) -> Dict[str, Any]:
        record = self._get_record(memory_id, include_deleted=False)
        if record is None:
            raise ValueError(f"Memory with id {memory_id} not found")
        return record.to_result(include_score=False)

    def get_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        layers: Optional[Sequence[str]] = None,
        top_k: int = 20,
    ) -> Dict[str, List[Dict[str, Any]]]:
        if top_k < 0:
            raise ValueError("top_k must be non-negative")

        connection = self._connect(create=False)
        if connection is None:
            return {"results": []}

        normalized_layers = self._normalize_layers(layers)
        records = self._load_records(connection, normalized_layers)
        filtered = [
            record
            for record in records
            if self._metadata_matches(record.metadata, filters or {})
        ]
        filtered.sort(key=lambda record: record.updated_at, reverse=True)
        return {
            "results": [
                record.to_result(include_score=False)
                for record in filtered[:top_k]
            ]
        }

    def search(
        self,
        query: str,
        *,
        layers: Optional[Sequence[str]] = None,
        top_k: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        threshold: float = 0.1,
    ) -> Dict[str, List[Dict[str, Any]]]:
        if top_k < 0:
            raise ValueError("top_k must be non-negative")
        if threshold < 0:
            raise ValueError("threshold must be non-negative")

        query_text = str(query or "").strip()
        if not query_text:
            return {"results": []}

        connection = self._connect(create=False)
        if connection is None:
            return {"results": []}

        normalized_layers = self._normalize_layers(layers)
        records = self._load_records(connection, normalized_layers)
        scored: List[ChemMemoryRecord] = []
        for record in records:
            if not self._metadata_matches(record.metadata, filters or {}):
                continue
            score, matched_terms = self._score_record(record, query_text)
            if score < threshold:
                continue
            record.score = score
            record.matched_terms = matched_terms
            scored.append(record)

        scored.sort(key=lambda record: (record.score or 0.0, record.updated_at), reverse=True)
        return {
            "results": [
                record.to_result(include_score=True)
                for record in scored[:top_k]
            ]
        }

    def update(
        self,
        memory_id: str,
        data: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        if data is None and metadata is None:
            raise ValueError("At least one of data or metadata must be provided")

        connection = self._connect(create=True)
        assert connection is not None
        with self._lock:
            record = self._get_record(memory_id, include_deleted=False)
            if record is None:
                raise ValueError(f"Memory with id {memory_id} not found")

            new_memory = record.memory if data is None else str(data).strip()
            if not new_memory:
                raise ValueError("memory data cannot be empty")
            new_metadata = dict(record.metadata)
            if metadata:
                new_metadata.update(metadata)
            now = self._now()
            new_hash = self._hash_text(new_memory)
            connection.execute(
                """
                UPDATE memories
                SET memory = ?, hash = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_memory,
                    new_hash,
                    self._dumps(new_metadata),
                    now,
                    memory_id,
                ),
            )
            self._add_history(
                connection,
                memory_id=memory_id,
                old_memory=record.memory,
                new_memory=new_memory,
                event="UPDATE",
                created_at=record.created_at,
                updated_at=now,
                actor_id=new_metadata.get("actor_id"),
                role=new_metadata.get("role"),
            )
            connection.commit()
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id: str) -> Dict[str, str]:
        connection = self._connect(create=True)
        assert connection is not None
        with self._lock:
            record = self._get_record(memory_id, include_deleted=False)
            if record is None:
                raise ValueError(f"Memory with id {memory_id} not found")
            now = self._now()
            connection.execute(
                "UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
            self._add_history(
                connection,
                memory_id=memory_id,
                old_memory=record.memory,
                new_memory=None,
                event="DELETE",
                created_at=record.created_at,
                updated_at=now,
                is_deleted=1,
                actor_id=record.metadata.get("actor_id"),
                role=record.metadata.get("role"),
            )
            connection.commit()
        return {"message": "Memory deleted successfully!"}

    def delete_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        layers: Optional[Sequence[str]] = None,
    ) -> Dict[str, str]:
        connection = self._connect(create=False)
        if connection is None:
            return {"message": "Memories deleted successfully!"}

        normalized_layers = self._normalize_layers(layers)
        with self._lock:
            records = self._load_records(connection, normalized_layers)
            target_records = [
                record
                for record in records
                if self._metadata_matches(record.metadata, filters or {})
            ]
            now = self._now()
            for record in target_records:
                connection.execute(
                    "UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?",
                    (now, record.id),
                )
                self._add_history(
                    connection,
                    memory_id=record.id,
                    old_memory=record.memory,
                    new_memory=None,
                    event="DELETE",
                    created_at=record.created_at,
                    updated_at=now,
                    is_deleted=1,
                    actor_id=record.metadata.get("actor_id"),
                    role=record.metadata.get("role"),
                )
            connection.commit()
        return {"message": "Memories deleted successfully!"}

    def history(self, memory_id: str) -> List[Dict[str, Any]]:
        connection = self._connect(create=False)
        if connection is None:
            return []
        cursor = connection.execute(
            """
            SELECT id, memory_id, old_memory, new_memory, event,
                   created_at, updated_at, is_deleted, actor_id, role
            FROM history
            WHERE memory_id = ?
            ORDER BY created_at ASC, updated_at ASC
            """,
            (memory_id,),
        )
        return [
            {
                "id": row[0],
                "memory_id": row[1],
                "old_memory": row[2],
                "new_memory": row[3],
                "event": row[4],
                "created_at": row[5],
                "updated_at": row[6],
                "is_deleted": bool(row[7]),
                "actor_id": row[8],
                "role": row[9],
            }
            for row in cursor.fetchall()
        ]

    def count(self, layers: Optional[Sequence[str]] = None) -> int:
        connection = self._connect(create=False)
        if connection is None:
            return 0
        normalized_layers = self._normalize_layers(layers)
        placeholders = ",".join("?" for _ in normalized_layers)
        cursor = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM memories
            WHERE is_deleted = 0 AND layer IN ({placeholders})
            """,
            tuple(normalized_layers),
        )
        return int(cursor.fetchone()[0])

    def reset(self) -> None:
        connection = self._connect(create=True)
        assert connection is not None
        with self._lock:
            connection.execute("DROP TABLE IF EXISTS memories")
            connection.execute("DROP TABLE IF EXISTS history")
            self._migrate(connection)
            connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _insert_memory(
        self,
        layer: str,
        memory: str,
        metadata: Dict[str, Any],
        scope_key: str,
    ) -> Tuple[ChemMemoryRecord, str]:
        connection = self._connect(create=True)
        assert connection is not None
        memory_hash = self._hash_text(memory)
        with self._lock:
            existing = connection.execute(
                """
                SELECT id, layer, memory, hash, metadata_json, scope_key, created_at, updated_at
                FROM memories
                WHERE is_deleted = 0 AND layer = ? AND hash = ? AND scope_key = ?
                """,
                (layer, memory_hash, scope_key),
            ).fetchone()
            if existing is not None:
                return self._record_from_row(existing), "EXISTS"

            now = self._now()
            memory_id = str(uuid.uuid4())
            stored_metadata = dict(metadata)
            stored_metadata["layer"] = layer
            connection.execute(
                """
                INSERT INTO memories (
                    id, layer, memory, hash, metadata_json, scope_key,
                    created_at, updated_at, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    memory_id,
                    layer,
                    memory,
                    memory_hash,
                    self._dumps(stored_metadata),
                    scope_key,
                    now,
                    now,
                ),
            )
            self._add_history(
                connection,
                memory_id=memory_id,
                old_memory=None,
                new_memory=memory,
                event="ADD",
                created_at=now,
                updated_at=now,
                actor_id=stored_metadata.get("actor_id"),
                role=stored_metadata.get("role"),
            )
            connection.commit()
            return (
                ChemMemoryRecord(
                    id=memory_id,
                    layer=layer,
                    memory=memory,
                    metadata=stored_metadata,
                    hash=memory_hash,
                    scope_key=scope_key,
                    created_at=now,
                    updated_at=now,
                ),
                "ADD",
            )

    def _connect(self, create: bool) -> Optional[sqlite3.Connection]:
        if self._connection is not None:
            return self._connection
        if not create and not self.db_path.exists():
            return None

        if create:
            self.root_dir.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate(self._connection)
        return self._connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                layer TEXT NOT NULL,
                memory TEXT NOT NULL,
                hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                scope_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                old_memory TEXT,
                new_memory TEXT,
                event TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                actor_id TEXT,
                role TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_layer_hash_scope
            ON memories(layer, hash, scope_key, is_deleted)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_layer_updated
            ON memories(layer, updated_at, is_deleted)
            """
        )
        connection.commit()

    def _add_history(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        old_memory: Optional[str],
        new_memory: Optional[str],
        event: str,
        created_at: str,
        updated_at: str,
        is_deleted: int = 0,
        actor_id: Optional[str] = None,
        role: Optional[str] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO history (
                id, memory_id, old_memory, new_memory, event,
                created_at, updated_at, is_deleted, actor_id, role
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                memory_id,
                old_memory,
                new_memory,
                event,
                created_at,
                updated_at,
                is_deleted,
                actor_id,
                role,
            ),
        )

    def _get_record(
        self,
        memory_id: str,
        *,
        include_deleted: bool,
    ) -> Optional[ChemMemoryRecord]:
        connection = self._connect(create=False)
        if connection is None:
            return None
        query = (
            """
            SELECT id, layer, memory, hash, metadata_json, scope_key, created_at, updated_at
            FROM memories
            WHERE id = ?
            """
        )
        params: Tuple[Any, ...] = (memory_id,)
        if not include_deleted:
            query += " AND is_deleted = 0"
        row = connection.execute(query, params).fetchone()
        return self._record_from_row(row) if row else None

    def _load_records(
        self,
        connection: sqlite3.Connection,
        layers: Sequence[str],
    ) -> List[ChemMemoryRecord]:
        placeholders = ",".join("?" for _ in layers)
        cursor = connection.execute(
            f"""
            SELECT id, layer, memory, hash, metadata_json, scope_key, created_at, updated_at
            FROM memories
            WHERE is_deleted = 0 AND layer IN ({placeholders})
            """,
            tuple(layers),
        )
        return [self._record_from_row(row) for row in cursor.fetchall()]

    def _record_from_row(self, row: Sequence[Any]) -> ChemMemoryRecord:
        metadata = self._loads(row[4])
        return ChemMemoryRecord(
            id=str(row[0]),
            layer=str(row[1]),
            memory=str(row[2]),
            hash=str(row[3]),
            metadata=metadata,
            scope_key=str(row[5] or ""),
            created_at=str(row[6]),
            updated_at=str(row[7]),
        )

    def _score_record(self, record: ChemMemoryRecord, query: str) -> Tuple[float, List[str]]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return 0.0, []

        metadata_text = self._metadata_search_text(record.metadata)
        memory_text = record.memory.lower()
        combined_text = f"{record.memory}\n{metadata_text}".lower()
        title_text = str(record.metadata.get("title", "")).lower()
        query_lower = query.lower()

        matched_terms: List[str] = []
        score = 0.0
        if query_lower in memory_text:
            score += 0.55
            matched_terms.append(query)
        elif query_lower in combined_text:
            score += 0.35
            matched_terms.append(query)
        if title_text and query_lower in title_text:
            score += 0.25

        unique_tokens = []
        seen = set()
        for token in query_tokens:
            if token not in seen:
                seen.add(token)
                unique_tokens.append(token)

        for token in unique_tokens:
            if token in combined_text:
                matched_terms.append(token)
                score += 1.0 / max(len(unique_tokens), 1)
                if token in title_text:
                    score += 0.08
                if token in memory_text:
                    score += 0.04

        if not matched_terms:
            return 0.0, []
        return round(min(score, 1.0), 4), self._dedupe_preserve_order(matched_terms)

    def _metadata_matches(self, metadata: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        if not filters:
            return True
        if "AND" in filters:
            conditions = filters["AND"]
            if not isinstance(conditions, list):
                return False
            return all(self._metadata_matches(metadata, item) for item in conditions)
        if "OR" in filters:
            conditions = filters["OR"]
            if not isinstance(conditions, list):
                return False
            return any(self._metadata_matches(metadata, item) for item in conditions)
        if "NOT" in filters:
            conditions = filters["NOT"]
            if not isinstance(conditions, list):
                return False
            return not any(self._metadata_matches(metadata, item) for item in conditions)

        for key, expected in filters.items():
            actual = metadata.get(key)
            if not self._value_matches(actual, expected):
                return False
        return True

    def _value_matches(self, actual: Any, expected: Any) -> bool:
        if expected == "*":
            return actual is not None
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "eq" and actual != value:
                    return False
                if operator == "ne" and actual == value:
                    return False
                if operator == "in" and actual not in set(value or []):
                    return False
                if operator == "nin" and actual in set(value or []):
                    return False
                if operator == "contains" and str(value) not in str(actual):
                    return False
                if operator == "icontains" and str(value).lower() not in str(actual).lower():
                    return False
                if operator in {"gt", "gte", "lt", "lte"}:
                    if not self._compare(actual, value, operator):
                        return False
            return True
        if isinstance(expected, (list, tuple, set)):
            return actual in expected
        return actual == expected

    def _compare(self, actual: Any, expected: Any, operator: str) -> bool:
        try:
            actual_value = float(actual)
            expected_value = float(expected)
        except (TypeError, ValueError):
            actual_value = str(actual)
            expected_value = str(expected)

        if operator == "gt":
            return actual_value > expected_value
        if operator == "gte":
            return actual_value >= expected_value
        if operator == "lt":
            return actual_value < expected_value
        if operator == "lte":
            return actual_value <= expected_value
        return False

    def _build_metadata(
        self,
        *,
        metadata: Optional[Dict[str, Any]],
        user_id: Optional[str],
        agent_id: Optional[str],
        run_id: Optional[str],
    ) -> Dict[str, Any]:
        merged = dict(metadata or {})
        if user_id:
            merged["user_id"] = user_id.strip()
        if agent_id:
            merged["agent_id"] = agent_id.strip()
        if run_id:
            merged["run_id"] = run_id.strip()
        return merged

    def _build_scope_key(self, metadata: Dict[str, Any]) -> str:
        parts = []
        for key in sorted(ENTITY_FILTER_KEYS):
            value = metadata.get(key)
            if value:
                parts.append(f"{key}={value}")
        return "&".join(parts)

    def _normalize_messages(self, messages: Any) -> List[Dict[str, Any]]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, dict):
            return [messages]
        if isinstance(messages, Iterable):
            normalized: List[Dict[str, Any]] = []
            for item in messages:
                if isinstance(item, str):
                    normalized.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    normalized.append(dict(item))
            return normalized
        raise ValueError("messages must be str, dict, or iterable of messages")

    def _normalize_layers(self, layers: Optional[Sequence[str]]) -> Tuple[str, ...]:
        if not layers:
            return ChemMemoryLayer.ALL
        return tuple(ChemMemoryLayer.normalize(layer) for layer in layers)

    def _metadata_search_text(self, metadata: Dict[str, Any]) -> str:
        preferred_keys = [
            "title",
            "source_title",
            "query",
            "problem",
            "hypothesis",
            "objective",
            "materials",
            "steps",
            "parameters",
            "observation",
            "result",
            "conclusion",
            "paper_title",
            "abstract",
            "keywords",
        ]
        segments: List[str] = []
        for key in preferred_keys:
            if key in metadata:
                segments.append(self._stringify(metadata[key]))
        if not segments:
            segments.append(self._stringify(metadata))
        return "\n".join(segment for segment in segments if segment)

    def _stringify(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _tokenize(self, text: str) -> List[str]:
        tokens: List[str] = []
        for match in TOKEN_RE.findall(text or ""):
            token = match.lower().strip()
            if not token:
                continue
            tokens.append(token)
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
                for size in (2, 3, 4):
                    if len(token) <= size:
                        continue
                    tokens.extend(token[index : index + size] for index in range(len(token) - size + 1))
        return tokens

    def _dedupe_preserve_order(self, values: Sequence[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _dumps(self, payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _loads(self, payload: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
