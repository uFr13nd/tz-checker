import asyncio

import pytest

from app import llm, runner, storage
from app.analyzer import Chunk, ExtractedDocument


def _library(count: int = 5) -> None:
    for index in range(count):
        storage.add_standard_document(
            source_id=f"STD-{index:03d}",
            filename=f"sto-{index}.md",
            source_url=None,
            sha256=f"hash-{index}",
            imported_at=storage.utcnow(),
            text="Толщина стены не менее 200 мм.",
            chunks=[{"locator": f"п. 5.{index}", "text": "Толщина стены не менее 200 мм."}],
            status="ACTIVE",
        )


def _tz(count: int = 6) -> ExtractedDocument:
    chunks = [Chunk(f"п. 1.{index}", "Толщина стены не менее 200 мм.") for index in range(count)]
    return ExtractedDocument("TZ-01", "tz.md", None, "\n".join(chunk.text for chunk in chunks), chunks)


def _start_run(run_id: str = "RUN-RUNNER") -> None:
    storage.create_run(
        {
            "run_id": run_id,
            "created_at": storage.utcnow(),
            "title": "runner test",
            "tz_filename": "tz.md",
            "standard_sources": [],
            "context": "",
            "status": "QUEUED",
            "analysis_mode": "quick",
        }
    )


def test_runner_completes_with_results_and_progress(store, monkeypatch):
    _library()
    _start_run()

    async def fake_chat(settings, system_prompt, user_prompt, **kwargs):
        return (
            {
                "results": [
                    {
                        "category": "Конструкции",
                        "parameter": "Толщина стены",
                        "status": "COMPLIES",
                        "finding_type": "соответствие",
                        "tz_quote": "Толщина стены не менее 200 мм.",
                        "standard_source_id": "STD-000",
                        "standard_quote": "Толщина стены не менее 200 мм.",
                        "explanation": "Совпадает",
                        "severity": "info",
                        "confidence": 0.9,
                    }
                ]
            },
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def fake_embed(settings, texts, model, **kwargs):
        return (
            [[1.0, 0.0, 0.0] for _ in texts],
            {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2},
        )

    monkeypatch.setattr(llm, "chat_json", fake_chat)
    monkeypatch.setattr(llm, "embed_texts", fake_embed)

    settings = {
        "base_url": "http://127.0.0.1:9/v1",
        "model": "test",
        "api_key": "",
        "use_embeddings": True,
        "embedding_model": "test-embed",
        "concurrency": 2,
    }
    progress = asyncio.run(
        runner.run_analysis(
            "RUN-RUNNER",
            settings=settings,
            context="",
            tz_doc=_tz(),
            mode="quick",
        )
    )
    run = storage.get_run("RUN-RUNNER")
    assert run["status"] in {"COMPLETED", "COMPLETED_WITH_REVIEW"}
    assert run["result_count"] == 1
    assert run["results"][0]["status"] == "COMPLIES"
    assert run["coverage"]["semantic_search"] is True
    assert run["coverage"]["llm_batches"] == 2
    assert run["coverage"]["batches_completed"] == 2
    assert progress["phase"] == "done"
    batches = storage.list_run_batches("RUN-RUNNER")
    assert len(batches) == 2
    assert all(batch["status"] == "DONE" for batch in batches)


def test_runner_respects_selected_standard_scope(store, monkeypatch):
    _library(3)
    _start_run("RUN-SCOPE")

    async def empty_chat(settings, system_prompt, user_prompt, **kwargs):
        return {"results": []}, {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}

    monkeypatch.setattr(llm, "chat_json", empty_chat)
    settings = {
        "base_url": "http://127.0.0.1:9/v1",
        "model": "test",
        "api_key": "",
        "use_embeddings": False,
        "embedding_model": "",
        "concurrency": 1,
    }
    asyncio.run(
        runner.run_analysis(
            "RUN-SCOPE",
            settings=settings,
            context="",
            tz_doc=_tz(3),
            mode="quick",
            source_ids=["STD-001"],
        )
    )
    run = storage.get_run("RUN-SCOPE")
    assert run["coverage"]["standard_scope"] == 1
    assert run["coverage"]["standard_chunks_retrieved"] <= 1
    assert all(
        result.get("standard_source_id") in {None, "", "STD-001"} for result in run["results"]
    )


def test_runner_falls_back_to_local_embeddings(store, monkeypatch):
    _library(2)
    _start_run("RUN-LOCAL")

    async def empty_chat(settings, system_prompt, user_prompt, **kwargs):
        return {"results": []}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    async def broken_embed(settings, texts, model, **kwargs):
        raise llm.ProviderError("Тестовый провайдер без /embeddings", status=502)

    monkeypatch.setattr(llm, "chat_json", empty_chat)
    monkeypatch.setattr(llm, "embed_texts", broken_embed)
    settings = {
        "base_url": "http://127.0.0.1:9/v1",
        "model": "test",
        "api_key": "",
        "use_embeddings": True,
        "embedding_model": "provider-embed",
        "concurrency": 1,
    }
    asyncio.run(
        runner.run_analysis(
            "RUN-LOCAL",
            settings=settings,
            context="",
            tz_doc=_tz(3),
            mode="quick",
        )
    )
    run = storage.get_run("RUN-LOCAL")
    assert run["coverage"]["embedding_mode"] == "local"
    assert run["coverage"]["semantic_search"] is True
    assert storage.get_embedding_map("local-hash-v1")
    assert any("локальные" in warning for warning in run["warnings"])


def test_runner_marks_failed_batches(store, monkeypatch):
    _library(2)
    _start_run("RUN-FAIL")

    async def failing_chat(settings, system_prompt, user_prompt, **kwargs):
        raise llm.ProviderError("Тестовая ошибка провайдера", status=502)

    async def fake_embed(settings, texts, model, **kwargs):
        return [[1.0, 0.0] for _ in texts], {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}

    monkeypatch.setattr(llm, "chat_json", failing_chat)
    monkeypatch.setattr(llm, "embed_texts", fake_embed)
    settings = {
        "base_url": "http://127.0.0.1:9/v1",
        "model": "test",
        "api_key": "",
        "use_embeddings": False,
        "embedding_model": "",
        "concurrency": 2,
    }
    asyncio.run(
        runner.run_analysis(
            "RUN-FAIL",
            settings=settings,
            context="",
            tz_doc=_tz(6),
            mode="quick",
        )
    )
    run = storage.get_run("RUN-FAIL")
    assert run["status"] == "FAILED"
    assert run["errors"]
    assert run["coverage"]["batches_failed"] == 2
