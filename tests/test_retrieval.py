from array import array

from app.retrieval import ChunkRecord, Retriever


def _chunk(chunk_id: int, source_id: str, text: str, locator: str = "п. 1") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        source_id=source_id,
        filename=f"{source_id}.pdf",
        source_url=None,
        status="ACTIVE",
        revision="2026",
        applicability="жилое",
        locator=locator,
        text=text,
    )


def _empty_fts(query: str, limit: int):
    return []


def test_synonyms_find_against_smoke_removal_terms():
    chunks = [
        _chunk(1, "STD-A", "Система противодымной вентиляции должна обеспечивать удаление продуктов горения."),
        _chunk(2, "STD-B", "Требования к отделке квартир и материалам."),
        _chunk(3, "STD-C", "Вентиляционная шахта должна иметь размер не менее 200 мм."),
    ]
    retriever = Retriever(chunks, _empty_fts)
    hits = retriever.search("дымоудаление из коридоров", limit=5)
    assert hits
    assert hits[0]["source_id"] == "STD-A"
    assert "противодымн" in hits[0]["text"]


def test_diversification_limits_chunks_per_source():
    chunks = [_chunk(index, "STD-A", f"Вентиляционная шахта {index} не менее {100 + index} мм.") for index in range(1, 7)]
    chunks.append(_chunk(7, "STD-B", "Вентиляционная шахта не менее 200 мм."))
    retriever = Retriever(chunks, _empty_fts)
    hits = retriever.search("вентиляционная шахта размер", limit=10, per_source=2)
    assert sum(1 for hit in hits if hit["source_id"] == "STD-A") <= 2
    assert any(hit["source_id"] == "STD-B" for hit in hits)


def test_embeddings_rerank_candidates():
    chunks = [
        _chunk(1, "STD-A", "Покрытие пола в коридорах."),
        _chunk(2, "STD-B", "Шум в инженерных системах."),
    ]
    retriever = Retriever(chunks, _empty_fts)
    retriever.attach_vectors({
        1: array("f", [1.0, 0.0]),
        2: array("f", [0.0, 1.0]),
    })
    hits = retriever.search("акустика", limit=2, vector=array("f", [0.0, 1.0]))
    assert hits[0]["source_id"] == "STD-B"


def test_no_match_returns_empty_list():
    retriever = Retriever([_chunk(1, "STD-A", "Отделка квартир.")], _empty_fts)
    assert retriever.search("сейсмостойкость фундамента", limit=5) == []
