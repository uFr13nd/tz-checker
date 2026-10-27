"""Гибридный поиск по библиотеке СТО.

Три сигнала объединяются через Reciprocal Rank Fusion:
1. FTS5 (BM25) — точные словоформы;
2. индекс стемов с синонимами — устойчив к падежам и терминологическим
   различиям («дымоудаление» ↔ «противодымная вентиляция»);
3. эмбеддинги провайдера (если включены) — семантическая близость.

Результат диверсифицируется по источникам, чтобы один длинный СТО не
занимал весь контекст модели.
"""

from __future__ import annotations

import math
import operator
import re
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from app.textutils import (
    STOP_WORDS,
    expand_synonyms,
    fold,
    query_terms,
    stem,
    tokenize,
)

RRF_K = 60
PER_SOURCE_LIMIT = 3
KEY_LENGTH = 7


def _key(word: str) -> str:
    return stem(word)[:KEY_LENGTH]


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: int
    source_id: str
    filename: str
    source_url: str | None
    status: str
    revision: str
    applicability: str
    locator: str
    text: str


def decode_vector(raw: bytes) -> array:
    vector = array("f")
    vector.frombytes(raw)
    return vector


def encode_vector(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


class Retriever:
    def __init__(
        self,
        chunks: list[ChunkRecord],
        fts_search: Callable[[str, int], list[dict[str, Any]]],
    ) -> None:
        self.chunks = chunks
        self.by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self.fts_search = fts_search
        self.vectors: dict[int, array] = {}
        self.postings: dict[str, list[int]] = defaultdict(list)
        self.doc_freq: Counter[str] = Counter()
        self._build_index()

    def _build_index(self) -> None:
        for chunk in self.chunks:
            stems = {
                _key(word)
                for word in tokenize(chunk.text)
                if word not in STOP_WORDS and len(stem(word)) >= 3
            }
            for item in stems:
                self.postings[item].append(chunk.chunk_id)
                self.doc_freq[item] += 1
        self.total = max(1, len(self.chunks))

    def attach_vectors(self, vectors: dict[int, array]) -> None:
        self.vectors = vectors

    def _stem_ranking(self, query: str) -> list[tuple[int, float]]:
        terms = query_terms(query)
        if not terms:
            return []
        query_keys = {_key(term) for term in terms}
        expanded = set(query_keys) | {_key(term) for term in expand_synonyms(terms)}
        scores: dict[int, float] = defaultdict(float)
        for key in expanded:
            ids = self.postings.get(key)
            if not ids:
                continue
            idf = math.log(1 + self.total / (1 + self.doc_freq.get(key, 1)))
            weight = idf * (1.0 if key in query_keys else 0.55)
            for chunk_id in ids:
                scores[chunk_id] += weight
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return ranked[:60]

    def _embedding_ranking(self, vector: array | None) -> list[tuple[int, float]]:
        if vector is None or not self.vectors:
            return []
        q = list(vector)
        q_norm = math.sqrt(sum(value * value for value in q)) or 1.0
        scored: list[tuple[int, float]] = []
        for chunk_id, vec in self.vectors.items():
            if chunk_id not in self.by_id:
                continue
            # Векторы хранятся нормализованными, но перестрахуемся для чужих моделей.
            v_norm = math.sqrt(sum(value * value for value in vec)) or 1.0
            dot = sum(map(operator.mul, vec, q))
            scored.append((chunk_id, dot / (v_norm * q_norm)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:60]

    def search(
        self,
        query: str,
        limit: int = 32,
        vector: array | None = None,
        per_source: int = PER_SOURCE_LIMIT,
    ) -> list[dict[str, Any]]:
        if not self.chunks:
            return []
        fused: dict[int, float] = defaultdict(float)

        try:
            fts_rows = self.fts_search(query, max(limit * 2, 32))
        except Exception:  # pragma: no cover - защитный сценарий
            fts_rows = []
        for rank, row in enumerate(fts_rows):
            chunk_id = row.get("chunk_id")
            if chunk_id is None:
                continue
            fused[int(chunk_id)] += 1.2 / (RRF_K + rank)

        for rank, (chunk_id, _score) in enumerate(self._stem_ranking(query)):
            fused[chunk_id] += 1.0 / (RRF_K + rank)

        for rank, (chunk_id, _score) in enumerate(self._embedding_ranking(vector)):
            fused[chunk_id] += 1.3 / (RRF_K + rank)

        if not fused:
            return []

        # Бонус за точное вхождение нормализованной фразы (короткие запросы).
        phrase = re.sub(r"\s+", " ", fold(query)).strip()
        if len(phrase) >= 12:
            for chunk_id in list(fused):
                chunk = self.by_id.get(chunk_id)
                if chunk and phrase in re.sub(r"\s+", " ", fold(chunk.text)):
                    fused[chunk_id] += 0.5

        ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
        selected: list[dict[str, Any]] = []
        per_source_count: Counter[str] = Counter()
        for chunk_id, score in ordered:
            chunk = self.by_id.get(chunk_id)
            if chunk is None:
                continue
            if per_source_count[chunk.source_id] >= per_source:
                continue
            per_source_count[chunk.source_id] += 1
            selected.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "source_id": chunk.source_id,
                    "filename": chunk.filename,
                    "source_url": chunk.source_url,
                    "status": chunk.status,
                    "revision": chunk.revision,
                    "applicability": chunk.applicability,
                    "locator": chunk.locator,
                    "text": chunk.text,
                    "score": round(score, 5),
                }
            )
            if len(selected) >= limit:
                break
        return selected


def load_retriever(
    fts_search: Callable[[str, int], list[dict[str, Any]]],
    source_ids: list[str] | None = None,
) -> Retriever:
    from app import storage

    allowed = set(source_ids or [])
    chunks = [
        ChunkRecord(
            chunk_id=int(row["chunk_id"]),
            source_id=str(row["source_id"]),
            filename=str(row["filename"]),
            source_url=row.get("source_url"),
            status=str(row.get("status") or "PENDING_REVIEW"),
            revision=str(row.get("revision") or ""),
            applicability=str(row.get("applicability") or ""),
            locator=str(row["locator"]),
            text=str(row["text"]),
        )
        for row in storage.library_chunks(include_retired=False)
        if not allowed or str(row["source_id"]) in allowed
    ]
    return Retriever(chunks, fts_search)
