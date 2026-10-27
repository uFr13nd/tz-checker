"""Фоновый прогон анализа: пул воркеров, прогресс, частичное сохранение.

Каждый пакет ТЗ обрабатывается отдельной задачей, результат сразу пишется
в run_batches. Сбой одного пакета не уничтожает остальные: прогон
завершается статусом PARTIAL. Прогресс доступен через
GET /api/runs/{run_id}/progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from app import llm, storage
from app.analyzer import (
    BATCH_SIZE,
    SYSTEM_PROMPT,
    Chunk,
    ExtractedDocument,
    MAX_STANDARD_EXCERPTS_PER_TZ_CHUNK,
    make_prompt,
    select_tz_chunks,
    verify_result,
)
from app.config import CONFIG
from app.retrieval import Retriever, decode_vector, encode_vector, load_retriever, normalize_vector
from app.textutils import mostly_english

MAX_EXCERPTS_PER_BATCH = 10
EMBED_BATCH = 32

logger = logging.getLogger("tz-checker.runner")

_tasks: dict[str, asyncio.Task[Any]] = {}
_cancel_events: dict[str, asyncio.Event] = {}
_progress: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_running(run_id: str) -> bool:
    task = _tasks.get(run_id)
    return bool(task and not task.done())


def get_progress(run_id: str) -> dict[str, Any]:
    data = _progress.get(run_id)
    if data is not None:
        return dict(data)
    run = storage.get_run(run_id)
    return dict(run.get("progress") or {}) if run else {}


def cancel(run_id: str) -> bool:
    event = _cancel_events.get(run_id)
    if event is None:
        return False
    event.set()
    progress = _progress.get(run_id)
    if progress is not None:
        progress["message"] = "Останавливаю после текущих пакетов…"
    return True


def start(run_id: str, **kwargs: Any) -> None:
    if is_running(run_id):
        return

    async def _wrapper() -> None:
        try:
            await run_analysis(run_id, **kwargs)
        except asyncio.CancelledError:  # pragma: no cover - остановка сервера
            raise
        except Exception as exc:  # pragma: no cover - защитный сценарий
            storage.update_run(
                run_id,
                status="FAILED",
                finished_at=_now(),
                error_json=json.dumps([f"Внутренняя ошибка: {exc}"], ensure_ascii=False),
            )
        finally:
            _tasks.pop(run_id, None)
            _cancel_events.pop(run_id, None)
            _progress.pop(run_id, None)

    _cancel_events[run_id] = asyncio.Event()
    _tasks[run_id] = asyncio.create_task(_wrapper())


async def run_analysis(
    run_id: str,
    *,
    settings: dict[str, Any],
    context: str,
    tz_doc: ExtractedDocument,
    mode: str,
    params: dict[str, Any] | None = None,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    started_clock = time.monotonic()
    started_at = _now()
    existing = storage.get_run(run_id) or {}
    conflict = _cancel_events.setdefault(run_id, asyncio.Event())
    cancel_event = conflict
    warnings: list[str] = list(existing.get("warnings") or [])
    errors: list[str] = []
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    batch_results: dict[int, list[dict[str, Any]]] = {}
    retrieved_keys: set[tuple[str, str]] = set()
    stop_all = False

    selected_tz, skipped = (
        (list(tz_doc.chunks), 0)
        if mode == "full"
        else select_tz_chunks(tz_doc, max_chunks={"quick": 60, "extended": 150}.get(mode, 60))
    )
    if not selected_tz:
        raise ValueError("Не удалось выделить фрагменты ТЗ для проверки.")
    batches_total = max(1, math.ceil(len(selected_tz) / BATCH_SIZE))

    progress: dict[str, Any] = {
        "phase": "indexing",
        "message": "Готовлю поисковый индекс…",
        "batches_total": batches_total,
        "batches_done": 0,
        "batches_failed": 0,
        "tz_chunks_total": len(tz_doc.chunks),
        "tz_chunks_analyzed": len(selected_tz),
        "tz_chunks_skipped": skipped,
        "results_count": 0,
        "started_at": started_at,
        "elapsed_sec": 0,
        "tokens": usage,
        "errors": [],
    }
    _progress[run_id] = progress
    storage.update_run(
        run_id,
        status="RUNNING",
        started_at=started_at,
        progress_json=json.dumps(progress, ensure_ascii=False),
    )

    def update_progress(**fields: Any) -> None:
        progress.update(fields)
        progress["elapsed_sec"] = round(time.monotonic() - started_clock, 1)
        progress["tokens"] = dict(usage)
        progress["errors"] = list(errors[-5:])
        storage.update_run(
            run_id, progress_json=json.dumps(progress, ensure_ascii=False)
        )

    # 1. Загрузка библиотеки и (опционально) эмбеддингов.
    retriever: Retriever = await asyncio.to_thread(
        load_retriever, storage.search_standard_chunks, source_ids
    )
    if not retriever.chunks:
        message = (
            "Выбранные СТО не найдены в библиотеке или не содержат текста."
            if source_ids
            else "Библиотека СТО пуста."
        )
        storage.finalize_run(
            run_id,
            status="FAILED",
            coverage={},
            warnings=[message],
            results=[],
            usage=usage,
            errors=[message],
        )
        progress.update(phase="failed", message=message)
        storage.update_run(run_id, progress_json=json.dumps(progress, ensure_ascii=False))
        return progress

    tz_vectors: dict[int, Any] = {}
    embedding_mode = "off"
    embedding_model = ""
    if settings.get("use_embeddings"):
        try:
            tz_vectors, embedding_mode, embedding_note = await _prepare_embeddings(
                run_id, settings, retriever, selected_tz, usage, update_progress
            )
            if embedding_note:
                warnings.append(embedding_note)
        except Exception as exc:  # noqa: BLE001 - деградация до локальных эмбеддингов
            tz_vectors = _local_embeddings(retriever, selected_tz)
            embedding_mode = "local"
            warnings.append(
                f"Эмбеддинги провайдера недоступны ({type(exc).__name__}); "
                "использованы локальные хэш-эмбеддинги."
            )
        embedding_model = (
            str(settings.get("embedding_model") or CONFIG.embedding_model)
            if embedding_mode == "provider"
            else "local-hash-v1"
        )

    if skipped:
        warnings.append(
            f"Выбрано {len(selected_tz)} из {len(tz_doc.chunks)} фрагментов ТЗ; ещё {skipped} не обработаны. "
            "Это частичная проверка, не заключение о полном соответствии."
        )
    if mode == "quick":
        warnings.append(
            "Использован быстрый режим. Для объёмного ТЗ выберите расширенный или полный режим."
        )
    if any("ПРЕДУПРЕЖДЕНИЕ" in chunk.locator for chunk in tz_doc.chunks):
        warnings.append("В ТЗ есть страницы без извлечённого текста; проверка может быть неполной.")

    update_progress(phase="analysis", message="Анализирую пакеты ТЗ…")

    library_stats = storage.standard_library_stats()

    # 2. Очередь пакетов и пул воркеров.
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(batches_total):
        queue.put_nowait(index)

    concurrency = max(1, min(8, int(settings.get("concurrency") or CONFIG.concurrency)))
    concurrency = min(concurrency, batches_total)
    logger.info(
        "RUN %s: старт mode=%s, фрагментов ТЗ %d из %d, пакетов %d, параллельность %d, "
        "эмбеддинги %s (%s), scope=%s",
        run_id,
        mode,
        len(selected_tz),
        len(tz_doc.chunks),
        batches_total,
        concurrency,
        embedding_mode,
        embedding_model or "—",
        len(source_ids) if source_ids else "all",
    )
    counters = {"done": 0, "failed": 0}

    def _abort_reason(message: str) -> bool:
        text = message.lower()
        return any(marker in text for marker in ("http 401", "http 403", "invalid api key", "unauthorized"))

    async def worker() -> None:
        nonlocal stop_all
        while True:
            try:
                batch_index = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if cancel_event.is_set() or stop_all:
                queue.task_done()
                continue
            tz_batch = selected_tz[batch_index * BATCH_SIZE : (batch_index + 1) * BATCH_SIZE]
            locators = "; ".join(chunk.locator for chunk in tz_batch)
            batch_started = time.monotonic()
            try:
                excerpts = await asyncio.to_thread(
                    _retrieve_excerpts, retriever, tz_batch, tz_vectors
                )
                if not excerpts:
                    counters["done"] += 1
                    storage.save_run_batch(
                        run_id,
                        batch_index,
                        tz_locators=locators,
                        status="SKIPPED",
                        results=[],
                        error="Нет релевантных фрагментов СТО",
                    )
                    update_progress(
                        batches_done=counters["done"],
                        current_batch=batch_index + 1,
                        current_locators=locators,
                        message=f"Пакет {batch_index + 1}: релевантные положения СТО не найдены.",
                    )
                    continue
                for doc, chunk in excerpts:
                    retrieved_keys.add((doc.source_id, chunk.locator))
                prompt = make_prompt(
                    context,
                    tz_doc,
                    tz_batch,
                    excerpts,
                    analysis_mode=mode,
                    library_stats=library_stats,
                    total_chunks=len(tz_doc.chunks),
                    analyzed_chunks=len(selected_tz),
                )
                data, batch_usage = await llm.chat_json(settings, SYSTEM_PROMPT, prompt)
                raw_results = data.get("results", [])
                if not isinstance(raw_results, list):
                    raw_results = []
                raw_results = raw_results[:12]
                standard_map = _standard_map(excerpts)
                verified = [
                    verify_result(item, tz_doc, standard_map, tz_batch, "TEMP")
                    for item in raw_results
                    if isinstance(item, dict)
                ]
                verified = _deduplicate(verified)
                batch_results[batch_index] = verified
                duration_ms = int((time.monotonic() - batch_started) * 1000)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage[key] += int(batch_usage.get(key, 0))
                counters["done"] += 1
                storage.save_run_batch(
                    run_id,
                    batch_index,
                    tz_locators=locators,
                    status="DONE",
                    results=verified,
                    prompt_tokens=int(batch_usage.get("prompt_tokens", 0)),
                    completion_tokens=int(batch_usage.get("completion_tokens", 0)),
                    duration_ms=duration_ms,
                )
                update_progress(
                    batches_done=counters["done"],
                    results_count=sum(len(items) for items in batch_results.values()),
                    current_batch=batch_index + 1,
                    current_locators=locators,
                    message=f"Обработано пакетов: {counters['done']} из {batches_total}.",
                )
            except llm.ProviderError as exc:
                counters["failed"] += 1
                errors.append(f"Пакет {batch_index + 1}: {exc.message}")
                logger.warning("RUN %s: пакет %d не выполнен: %s", run_id, batch_index + 1, exc.message)
                storage.save_run_batch(
                    run_id,
                    batch_index,
                    tz_locators=locators,
                    status="ERROR",
                    results=[],
                    error=exc.message,
                )
                if _abort_reason(exc.message):
                    stop_all = True
                update_progress(
                    batches_failed=counters["failed"],
                    message=f"Ошибка пакета {batch_index + 1}: {exc.message[:180]}",
                )
            except Exception as exc:  # noqa: BLE001 - пакет не должен ронять прогон
                counters["failed"] += 1
                errors.append(f"Пакет {batch_index + 1}: {type(exc).__name__}: {exc}")
                logger.exception("RUN %s: пакет %d упал с ошибкой", run_id, batch_index + 1)
                storage.save_run_batch(
                    run_id,
                    batch_index,
                    tz_locators=locators,
                    status="ERROR",
                    results=[],
                    error=f"{type(exc).__name__}: {exc}",
                )
                update_progress(
                    batches_failed=counters["failed"],
                    message=f"Ошибка пакета {batch_index + 1}.",
                )
            finally:
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(concurrency)))

    # 3. Сборка результатов.
    ordered: list[dict[str, Any]] = []
    seen_signature: set[tuple[str, str, str, str]] = set()
    for index in sorted(batch_results):
        for item in batch_results[index]:
            signature = (
                _norm(item.get("tz_quote")),
                _norm(item.get("standard_quote")),
                str(item.get("standard_source_id")),
                _norm(item.get("parameter")),
            )
            if signature in seen_signature:
                continue
            seen_signature.add(signature)
            ordered.append(item)
    for position, item in enumerate(ordered, 1):
        item["result_id"] = f"RES-{position:04d}"

    if cancel_event.is_set():
        status = "CANCELLED"
    elif not ordered and errors:
        status = "FAILED"
    elif errors:
        status = "PARTIAL"
    elif any(item["status"] not in {"COMPLIES", "NONCOMPLIANCE"} for item in ordered) or warnings:
        status = "COMPLETED_WITH_REVIEW"
    else:
        status = "COMPLETED"

    if not ordered and not errors:
        warnings.append(
            "Модель не сформировала результатов. Это не означает, что ТЗ соответствует СТО."
        )
    if errors:
        warnings.append(
            f"Часть пакетов завершилась ошибкой ({counters['failed']} из {batches_total}). "
            "Результаты неполные; запустите проверку повторно для оставшихся пакетов."
        )
    if any(not item.get("evidence_verified") for item in ordered):
        warnings.append(
            "Есть результаты без подтверждённых цитат из обоих источников; они требуют экспертной проверки."
        )
    english_fields = (
        "category",
        "section",
        "parameter",
        "applicability_assessment",
        "semantic_reasoning",
        "explanation",
        "technical_assessment",
        "recommendation",
    )
    english_results = [
        item
        for item in ordered
        if any(mostly_english(str(item.get(field, ""))) for field in english_fields)
    ]
    if english_results:
        warnings.append(
            f"В {len(english_results)} выводах текст остался на английском. Модель проигнорировала "
            "языковую инструкцию — проверьте выводы или повторите пакет на модели с надёжным русским."
        )
    if counters["done"] and counters["done"] < batches_total:
        warnings.append(
            f"Обработано {counters['done']} из {batches_total} пакетов (остановка/ошибки)."
        )

    stats = storage.standard_library_stats()
    coverage = {
        "analysis_mode": mode,
        "tz_chunks_total": len(tz_doc.chunks),
        "tz_chunks_analyzed": len(selected_tz),
        "tz_chunks_skipped": skipped,
        "llm_batches": batches_total,
        "batches_completed": counters["done"],
        "batches_failed": counters["failed"],
        "standard_documents_in_library": stats["documents"],
        "standard_chunks_in_library": stats["chunks"],
        "standard_chunks_retrieved": len(retrieved_keys),
        "standard_scope": len(source_ids) if source_ids else "all",
        "semantic_search": bool(tz_vectors),
        "embedding_mode": embedding_mode,
        "embedding_model": embedding_model,
        "concurrency": concurrency,
        "tokens": usage,
    }
    storage.finalize_run(
        run_id,
        status=status,
        coverage=coverage,
        warnings=warnings,
        results=ordered,
        usage=usage,
        errors=errors,
    )
    progress.update(
        phase="done",
        status=status,
        message={
            "COMPLETED": "Проверка завершена.",
            "COMPLETED_WITH_REVIEW": "Проверка завершена, есть выводы для экспертной оценки.",
            "PARTIAL": "Проверка завершена частично: часть пакетов не обработана.",
            "FAILED": "Проверка не выполнена.",
            "CANCELLED": "Проверка остановлена пользователем.",
        }.get(status, "Готово."),
        batches_done=counters["done"],
        results_count=len(ordered),
        elapsed_sec=round(time.monotonic() - started_clock, 1),
        status_detail=status,
    )
    storage.update_run(run_id, progress_json=json.dumps(progress, ensure_ascii=False))
    logger.info(
        "RUN %s: завершён status=%s, результатов=%d, ошибок пакетов=%d, "
        "батчей %d/%d, времени %.1f c, токенов=%d",
        run_id,
        status,
        len(ordered),
        counters["failed"],
        counters["done"],
        batches_total,
        time.monotonic() - started_clock,
        usage.get("total_tokens", 0),
    )
    return progress


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        signature = (
            _norm(item.get("tz_quote")),
            _norm(item.get("standard_quote")),
            str(item.get("standard_source_id")),
            _norm(item.get("parameter")),
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def _retrieve_excerpts(
    retriever: Retriever,
    tz_batch: list[Chunk],
    vectors: dict[int, Any],
) -> list[tuple[ExtractedDocument, Chunk]]:
    picked: list[tuple[ExtractedDocument, Chunk]] = []
    seen: set[tuple[str, str]] = set()
    docs: dict[str, ExtractedDocument] = {}
    for chunk in tz_batch:
        hits = retriever.search(
            chunk.text,
            limit=32,
            vector=vectors.get(id(chunk)),
            per_source=3,
        )
        per_chunk = 0
        for hit in hits:
            key = (str(hit["source_id"]), str(hit["locator"]))
            if key in seen:
                continue
            if len(picked) >= MAX_EXCERPTS_PER_BATCH:
                break
            seen.add(key)
            doc = docs.get(hit["source_id"])
            if doc is None:
                doc = ExtractedDocument(
                    source_id=str(hit["source_id"]),
                    filename=str(hit["filename"]),
                    source_url=hit.get("source_url"),
                    text="",
                    chunks=[],
                    status=str(hit.get("status") or "PENDING_REVIEW"),
                    revision=str(hit.get("revision") or ""),
                    applicability=str(hit.get("applicability") or ""),
                )
                docs[hit["source_id"]] = doc
            std_chunk = Chunk(str(hit["locator"]), str(hit["text"]))
            doc.chunks.append(std_chunk)
            doc.text = (doc.text + "\n\n" + std_chunk.text).strip()
            picked.append((doc, std_chunk))
            per_chunk += 1
            if per_chunk >= MAX_STANDARD_EXCERPTS_PER_TZ_CHUNK:
                break
    return picked


def _standard_map(
    excerpts: list[tuple[ExtractedDocument, Chunk]]
) -> dict[str, ExtractedDocument]:
    return {doc.source_id: doc for doc, _chunk in excerpts}


async def _prepare_embeddings(
    run_id: str,
    settings: dict[str, Any],
    retriever: Retriever,
    selected_tz: list[Chunk],
    usage: dict[str, int],
    update_progress,
) -> tuple[dict[int, Any], str, str]:
    """Вернуть (векторы ТЗ, режим, предупреждение).

    Сначала пробуем эмбеддинги провайдера, при ошибке — локальные
    хэш-эмбеддинги, чтобы семантический сигнал работал всегда.
    """
    model = str(settings.get("embedding_model") or CONFIG.embedding_model).strip()
    if not model:
        model = CONFIG.embedding_model
    try:
        tz_vectors = await _provider_embeddings(
            settings, retriever, selected_tz, usage, update_progress, model
        )
        return tz_vectors, "provider", ""
    except llm.ProviderError as exc:
        tz_vectors = _local_embeddings(retriever, selected_tz)
        return (
            tz_vectors,
            "local",
            f"Провайдер не дал эмбеддинги ({exc.message}). Включены локальные "
            "хэш-эмбеддинги: поиск работает без внешней модели.",
        )


async def _provider_embeddings(
    settings: dict[str, Any],
    retriever: Retriever,
    selected_tz: list[Chunk],
    usage: dict[str, int],
    update_progress,
    model: str,
) -> dict[int, Any]:
    cached = storage.get_embedding_map(model)
    missing = [chunk.chunk_id for chunk in retriever.chunks if chunk.chunk_id not in cached]
    total_missing = len(missing)
    done = 0
    while missing:
        batch_ids = missing[:EMBED_BATCH]
        missing = missing[EMBED_BATCH:]
        texts = [str(retriever.by_id[chunk_id].text) for chunk_id in batch_ids]
        vectors, embed_usage = await llm.embed_texts(settings, texts, model)
        if len(vectors) != len(batch_ids):
            raise llm.ProviderError("Провайдер вернул неверное число эмбеддингов.", status=502)
        items = []
        for chunk_id, vector in zip(batch_ids, vectors):
            normalized = normalize_vector(vector)
            items.append((chunk_id, encode_vector(normalized), len(normalized)))
        storage.save_embeddings(items, model)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage[key] += int(embed_usage.get(key, 0))
        done += len(batch_ids)
        update_progress(
            embedding_done=done,
            embedding_total=total_missing,
            message=f"Индексирую библиотеку для семантического поиска: {done} из {total_missing}.",
        )

    all_vectors = storage.get_embedding_map(model)
    decoded = {
        chunk_id: decode_vector(raw)
        for chunk_id, raw in all_vectors.items()
        if chunk_id in retriever.by_id
    }
    retriever.attach_vectors(decoded)
    return await _tz_vectors_provider(settings, selected_tz, model, usage)


async def _tz_vectors_provider(
    settings: dict[str, Any], selected_tz: list[Chunk], model: str, usage: dict[str, int]
) -> dict[int, Any]:
    tz_vectors: dict[int, Any] = {}
    tz_texts = [chunk.text for chunk in selected_tz]
    for start in range(0, len(tz_texts), EMBED_BATCH):
        batch = tz_texts[start : start + EMBED_BATCH]
        vectors, embed_usage = await llm.embed_texts(settings, batch, model)
        if len(vectors) != len(batch):
            raise llm.ProviderError("Провайдер вернул неверное число эмбеддингов ТЗ.", status=502)
        for offset, vector in enumerate(vectors):
            normalized = normalize_vector(vector)
            tz_vectors[id(selected_tz[start + offset])] = normalized
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage[key] += int(embed_usage.get(key, 0))
    return tz_vectors


def _local_embeddings(
    retriever: Retriever, selected_tz: list[Chunk]
) -> dict[int, Any]:
    from app.embeddings import LOCAL_MODEL, local_embedding

    cached = storage.get_embedding_map(LOCAL_MODEL)
    missing = [chunk for chunk in retriever.chunks if chunk.chunk_id not in cached]
    if missing:
        items = []
        for chunk in missing:
            vector = local_embedding(chunk.text)
            items.append((chunk.chunk_id, encode_vector(vector), len(vector)))
        storage.save_embeddings(items, LOCAL_MODEL)
    all_vectors = storage.get_embedding_map(LOCAL_MODEL)
    decoded = {
        chunk_id: decode_vector(raw)
        for chunk_id, raw in all_vectors.items()
        if chunk_id in retriever.by_id
    }
    retriever.attach_vectors(decoded)
    return {id(chunk): local_embedding(chunk.text) for chunk in selected_tz}
