"""HTTP-слой TZ Checker: настройки, библиотека СТО, прогоны, экспорт, шаблоны."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from collections import deque

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app import llm, runner, storage
from app.analyzer import (
    ExtractedDocument,
    download_public_drive_document,
    estimate_run,
    extract_document,
)
from app.config import APP_VERSION, CONFIG

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _configure_logging() -> logging.Logger:
    """Единый журнал: консоль + data/tz-checker.log (ротация 2 МБ × 3)."""
    logger = logging.getLogger("tz-checker")
    if getattr(logger, "_tz_configured", False):
        return logger
    level = getattr(logging, CONFIG.log_level, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    try:
        CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            CONFIG.data_dir / "tz-checker.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        # Uvicorn настраивает свои логгеры отдельно и не пишет в наш файл.
        # uvicorn.error наследуется от uvicorn, поэтому обработчик вешаем
        # только на корневой uvicorn и на access — иначе строки дублируются.
        for name in ("uvicorn", "uvicorn.access"):
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.addHandler(file_handler)
            if uvicorn_logger.level == logging.NOTSET:
                uvicorn_logger.setLevel(level)
    except OSError:
        pass
    logger._tz_configured = True  # type: ignore[attr-defined]
    return logger


logger = _configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.init_db()
    stale = storage.mark_stale_runs()
    if stale:
        logger.warning("Помечено прерванных прогонов после перезапуска: %s", stale)
    logger.info(
        "TZ Checker %s запущен: data=%s, auth=%s, embeddings=%s",
        APP_VERSION,
        CONFIG.data_dir,
        bool(CONFIG.auth_token),
        CONFIG.use_embeddings,
    )
    yield
    logger.info("TZ Checker остановлен")


app = FastAPI(title="TZ Checker", version=APP_VERSION, lifespan=lifespan)


# --------------------------------------------------------------------------
# Защита локального/серверного API
# --------------------------------------------------------------------------


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site == "cross-site":
        return False
    if not origin:
        return True
    origin = origin.rstrip("/")
    if origin in CONFIG.allowed_origins:
        return True
    parsed = urlparse(origin)
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return bool(parsed.hostname) and parsed.hostname.lower() == host


@app.middleware("http")
async def guard_api(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api")
    if path == "/health":
        return await call_next(request)
    if protected and CONFIG.auth_token:
        supplied = (
            request.headers.get("x-api-token")
            or request.query_params.get("token")
            or ""
        )
        if not hmac.compare_digest(supplied, CONFIG.auth_token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Требуется токен доступа (X-API-Token)."},
            )
    if protected and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not _origin_allowed(request):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Запрос отклонён: недопустимый Origin. Откройте приложение по его адресу."
                },
            )
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception):
    logger.exception("Необработанная ошибка: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера. Подробности — в data/tz-checker.log."},
    )


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------


async def _read_upload(file: UploadFile) -> bytes:
    raw = await file.read(CONFIG.max_upload_bytes + 1)
    if len(raw) > CONFIG.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Файл {file.filename or ''} больше {CONFIG.max_upload_mb} МБ.",
        )
    return raw


async def _import_standard_upload(file: UploadFile) -> dict[str, Any]:
    filename = file.filename or "standard.txt"
    raw = await _read_upload(file)
    digest = hashlib.sha256(raw).hexdigest()
    source_id = "STD-" + digest[:16].upper()
    doc = extract_document(filename, raw, source_id)
    return storage.add_standard_document(
        source_id=source_id,
        filename=doc.filename,
        source_url=doc.source_url,
        sha256=digest,
        imported_at=storage.utcnow(),
        text=doc.text,
        chunks=[{"locator": chunk.locator, "text": chunk.text} for chunk in doc.chunks],
        status="PENDING_REVIEW",
    )


async def _import_standard_url(url: str) -> dict[str, Any]:
    doc = await download_public_drive_document(url, "TEMP")
    digest = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
    source_id = "STD-" + digest[:16].upper()
    doc.source_id = source_id
    return storage.add_standard_document(
        source_id=source_id,
        filename=doc.filename,
        source_url=doc.source_url or url,
        sha256=digest,
        imported_at=storage.utcnow(),
        text=doc.text,
        chunks=[{"locator": chunk.locator, "text": chunk.text} for chunk in doc.chunks],
        status="PENDING_REVIEW",
    )


def _analysis_context(context: str, instructions: str) -> str:
    return (
        "КОНТЕКСТ ПРОЕКТА:\n"
        + (context.strip() or "Не предоставлен.")
        + "\n\nДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ ПО ФОКУСУ ПРОВЕРКИ:\n"
        + (instructions.strip() or "Используй стандартный полный инженерный чек-лист.")
    )


def _standard_snapshot() -> list[dict[str, Any]]:
    documents = storage.list_standard_documents(include_retired=False)
    return [
        {
            "source_id": doc["source_id"],
            "filename": doc["filename"],
            "source_url": doc["source_url"],
            "status": doc["status"],
            "revision": doc["revision"],
            "characters": doc["character_count"],
            "chunks": doc["chunk_count"],
        }
        for doc in documents
    ]


def _estimate_for(
    tz_doc: ExtractedDocument,
    mode: str,
    context: str,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    settings = storage.get_settings()
    documents = storage.list_standard_documents(include_retired=False)
    if source_ids:
        allowed = set(source_ids)
        documents = [doc for doc in documents if doc["source_id"] in allowed]
    chunks = sum(int(doc["chunk_count"]) for doc in documents)
    characters = sum(int(doc["character_count"]) for doc in documents)
    return estimate_run(
        tz_doc,
        mode=mode,
        context=context,
        library_chunks=chunks,
        library_chars=characters,
        price_input=float(settings.get("price_input") or 0),
        price_output=float(settings.get("price_output") or 0),
    )


def _parse_source_ids(raw: str, documents: list[dict[str, Any]]) -> list[str]:
    if not (raw or "").strip():
        return []
    try:
        parsed = json.loads(raw)
        items = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        items = [part.strip() for part in raw.split(",")]
    known = {str(doc["source_id"]) for doc in documents}
    selected = [str(item) for item in items if str(item).strip()]
    unknown = [item for item in selected if item not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="Неизвестные ID СТО в ограничении области проверки: " + ", ".join(unknown[:10]),
        )
    return list(dict.fromkeys(selected))


def _validate_mode(mode: str) -> str:
    if mode not in {"quick", "extended", "full"}:
        raise HTTPException(
            status_code=422, detail="Режим анализа: quick, extended или full."
        )
    return mode


# --------------------------------------------------------------------------
# Служебные эндпоинты и настройки
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "TZ Checker",
        "version": APP_VERSION,
        "api_version": 3,
        "features": [
            "estimate",
            "background_runs",
            "progress",
            "cancel",
            "compare",
            "templates",
            "bulk_status",
            "embeddings",
            "xlsx_summary",
        ],
        "auth_required": bool(CONFIG.auth_token),
        "storage": "SQLite",
    }


@app.get("/api/settings")
def read_settings() -> dict[str, Any]:
    data = storage.public_settings()
    data["auth_required"] = bool(CONFIG.auth_token)
    data["encryption_available"] = storage.secrets.encryption_available()
    return data


@app.post("/api/settings")
def write_settings(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = str(payload.get("base_url", "")).strip()
    model = str(payload.get("model", "")).strip()
    if not base_url:
        raise HTTPException(status_code=422, detail="Укажите Base URL API.")
    if not model:
        raise HTTPException(status_code=422, detail="Укажите название модели.")
    try:
        base_url = llm.validate_base_url(base_url)
    except llm.ProviderError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    for field_name in ("api_key", "google_client_id", "spreadsheet_id", "embedding_model", "embedding_base_url", "embedding_api_key"):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"Поле {field_name} должно быть строкой.")
    embedding_base_url = str(payload.get("embedding_base_url") or "").strip()
    if embedding_base_url:
        try:
            embedding_base_url = llm.validate_base_url(embedding_base_url)
        except llm.ProviderError as exc:
            raise HTTPException(status_code=exc.status, detail=f"Base URL эмбеддингов: {exc.message}") from exc
    try:
        concurrency = int(payload.get("concurrency", CONFIG.concurrency))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Параллельность должна быть числом.") from exc
    if not 1 <= concurrency <= 8:
        raise HTTPException(status_code=422, detail="Параллельность: от 1 до 8.")
    try:
        price_input = float(payload.get("price_input", 0) or 0)
        price_output = float(payload.get("price_output", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Цена за токены должна быть числом.") from exc
    normalized = {
        "base_url": base_url,
        "model": model,
        "api_key": payload.get("api_key"),
        "clear_api_key": bool(payload.get("clear_api_key")),
        "google_client_id": payload.get("google_client_id"),
        "spreadsheet_id": payload.get("spreadsheet_id"),
        "use_embeddings": bool(payload.get("use_embeddings", True)),
        "embedding_model": payload.get("embedding_model") or CONFIG.embedding_model,
        "embedding_base_url": embedding_base_url,
        "embedding_api_key": payload.get("embedding_api_key"),
        "clear_embedding_api_key": bool(payload.get("clear_embedding_api_key")),
        "concurrency": concurrency,
        "price_input": price_input,
        "price_output": price_output,
    }
    result = storage.save_settings(normalized)
    result["auth_required"] = bool(CONFIG.auth_token)
    result["encryption_available"] = storage.secrets.encryption_available()
    logger.info(
        "Настройки обновлены: base_url=%s, model=%s, embeddings=%s, concurrency=%s",
        result["base_url"],
        result["model"],
        result["use_embeddings"],
        result["concurrency"],
    )
    return result


@app.post("/api/settings/test")
async def test_provider() -> dict[str, Any]:
    settings = storage.get_settings()
    if not settings.get("api_key") and not llm.is_local_provider(settings["base_url"]):
        raise HTTPException(
            status_code=422,
            detail="Для облачного провайдера сохраните API key. Для локальной Ollama ключ не нужен.",
        )
    try:
        return await llm.test_connection(settings, check_embeddings=True)
    except llm.ProviderError as exc:
        logger.warning("Проверка провайдера не прошла: %s", exc.message)
        raise HTTPException(status_code=502, detail=exc.message) from exc


# --------------------------------------------------------------------------
# Библиотека СТО
# --------------------------------------------------------------------------


@app.get("/api/library/standards")
def library_standards() -> dict[str, Any]:
    return {"stats": storage.standard_library_stats(), "documents": storage.list_standard_documents(True)}


@app.post("/api/library/import")
async def import_standards(
    standard_files: list[UploadFile] = File(default=[]),
    standard_urls: str = Form(default=""),
) -> dict[str, Any]:
    urls = [line.strip() for line in standard_urls.splitlines() if line.strip()]
    if not standard_files and not urls:
        raise HTTPException(
            status_code=422, detail="Выберите файлы СТО или добавьте ссылки, по одной на строку."
        )
    items: list[dict[str, Any]] = []
    for file in standard_files:
        try:
            items.append({**(await _import_standard_upload(file)), "input": file.filename or "файл"})
        except Exception as exc:  # noqa: BLE001 - импорт батча не должен падать целиком
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            items.append({"input": file.filename or "файл", "status": "ERROR", "error": detail})
    for url in urls:
        try:
            items.append({**(await _import_standard_url(url)), "input": url})
        except Exception as exc:  # noqa: BLE001
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            items.append({"input": url, "status": "ERROR", "error": detail})
    result = {
        "items": items,
        "imported": sum(1 for item in items if item.get("duplicate") is False),
        "duplicates": sum(1 for item in items if item.get("duplicate") is True),
        "errors": sum(1 for item in items if item.get("status") == "ERROR"),
        "stats": storage.standard_library_stats(),
    }
    logger.info(
        "Импорт библиотеки: файлов=%d, ссылок=%d, новых=%d, дубликатов=%d, ошибок=%d",
        len(standard_files),
        len(urls),
        result["imported"],
        result["duplicates"],
        result["errors"],
    )
    return result

@app.patch("/api/library/standards")
def bulk_standard_status(payload: dict[str, Any]) -> dict[str, Any]:
    source_ids = payload.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise HTTPException(status_code=422, detail="Передайте список source_ids.")
    status_value = payload.get("status")
    revision_value = payload.get("revision")
    applicability_value = payload.get("applicability")
    for value, label in (
        (status_value, "Статус"),
        (revision_value, "Редакция"),
        (applicability_value, "Область применения"),
    ):
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{label} должен быть текстом.")
    try:
        changed = storage.bulk_update_standard_status(
            [str(item) for item in source_ids], status_value, revision_value, applicability_value
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Статус должен быть PENDING_REVIEW, ACTIVE, DRAFT или RETIRED.",
        ) from exc
    return {"changed": changed, "status": status_value or "UNCHANGED"}


@app.patch("/api/library/standards/{source_id}")
def set_standard_status(source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    status_value = payload.get("status")
    revision_value = payload.get("revision")
    applicability_value = payload.get("applicability")
    for value, label in (
        (status_value, "Статус"),
        (revision_value, "Редакция"),
        (applicability_value, "Область применения"),
    ):
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{label} должен быть текстом.")
    try:
        changed = storage.update_standard_status(
            source_id, status_value, revision_value, applicability_value
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Статус должен быть PENDING_REVIEW, ACTIVE, DRAFT или RETIRED.",
        ) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="СТО не найден в библиотеке.")
    return {"source_id": source_id, "status": status_value or "UNCHANGED"}


@app.delete("/api/library/standards/{source_id}")
def remove_standard(source_id: str) -> dict[str, Any]:
    if not storage.delete_standard_document(source_id):
        raise HTTPException(status_code=404, detail="СТО не найден в библиотеке.")
    return {"deleted": True, "source_id": source_id}


# --------------------------------------------------------------------------
# Шаблоны контекста
# --------------------------------------------------------------------------


@app.get("/api/templates")
def templates() -> list[dict[str, Any]]:
    return storage.list_templates()


@app.post("/api/templates")
def create_template(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название шаблона.")
    if len(name) > 120:
        raise HTTPException(status_code=422, detail="Название шаблона слишком длинное.")
    return storage.save_template(
        name,
        str(payload.get("context", "") or "")[:50000],
        str(payload.get("instructions", "") or "")[:20000],
        str(payload.get("id")) if payload.get("id") else None,
    )


@app.delete("/api/templates/{template_id}")
def remove_template(template_id: str) -> dict[str, Any]:
    if not storage.delete_template(template_id):
        raise HTTPException(status_code=404, detail="Шаблон не найден.")
    return {"deleted": True}


# --------------------------------------------------------------------------
# Оценка и запуск анализа
# --------------------------------------------------------------------------


@app.post("/api/estimate")
async def estimate(
    tz_file: UploadFile | None = File(default=None),
    tz_url: str = Form(default=""),
    analysis_mode: str = Form(default="quick"),
    context: str = Form(default=""),
    standard_ids: str = Form(default=""),
) -> dict[str, Any]:
    mode = _validate_mode(analysis_mode)
    tz_url = tz_url.strip()
    if (tz_file is None and not tz_url) or (tz_file is not None and tz_url):
        raise HTTPException(
            status_code=422, detail="Выберите один способ для ТЗ: файл ИЛИ ссылку."
        )
    scope = _parse_source_ids(standard_ids, storage.list_standard_documents(False))
    if tz_file is not None:
        raw = await _read_upload(tz_file)
        tz_doc = extract_document(tz_file.filename or "tz.txt", raw, "TZ-01")
    else:
        tz_doc = await download_public_drive_document(tz_url, "TZ-01")
    return _estimate_for(tz_doc, mode, context, scope)


@app.post("/api/analyze", status_code=202)
async def analyze(
    tz_file: UploadFile | None = File(default=None),
    standard_files: list[UploadFile] = File(default=[]),
    tz_url: str = Form(default=""),
    standard_urls: str = Form(default=""),
    context: str = Form(default=""),
    instructions: str = Form(default=""),
    title: str = Form(default=""),
    analysis_mode: str = Form(default="quick"),
    standard_ids: str = Form(default=""),
) -> dict[str, Any]:
    mode = _validate_mode(analysis_mode)
    tz_url = tz_url.strip()
    standard_url_list = [line.strip() for line in standard_urls.splitlines() if line.strip()]
    if (tz_file is None and not tz_url) or (tz_file is not None and tz_url):
        raise HTTPException(
            status_code=422, detail="Выберите один способ для ТЗ: загрузить файл ИЛИ указать ссылку."
        )
    settings = storage.get_settings()
    if not settings.get("api_key") and not llm.is_local_provider(settings["base_url"]):
        raise HTTPException(
            status_code=503,
            detail="Сначала откройте «Настройки ИИ», сохраните API key и проверьте подключение.",
        )

    if tz_file is not None:
        raw = await _read_upload(tz_file)
        tz_doc = extract_document(tz_file.filename or "tz.txt", raw, "TZ-01")
    else:
        tz_doc = await download_public_drive_document(tz_url, "TZ-01")

    import_warnings: list[str] = []
    for file in standard_files:
        try:
            item = await _import_standard_upload(file)
            if item.get("duplicate"):
                import_warnings.append(
                    f"СТО {item.get('filename')} уже есть в библиотеке; повторно не импортировалось."
                )
        except Exception as exc:  # noqa: BLE001
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            import_warnings.append(f"Не удалось импортировать {file.filename or 'файл'}: {detail}")
    for url in standard_url_list:
        try:
            item = await _import_standard_url(url)
            if item.get("duplicate"):
                import_warnings.append(f"Документ по ссылке уже есть в библиотеке: {url}")
        except Exception as exc:  # noqa: BLE001
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            import_warnings.append(f"Не удалось импортировать СТО по ссылке {url}: {detail}")

    stats = storage.standard_library_stats()
    if stats["documents"] == 0:
        raise HTTPException(
            status_code=422,
            detail="Библиотека СТО пуста. Импортируйте документы в разделе «Библиотека СТО».",
        )
    scope = _parse_source_ids(standard_ids, storage.list_standard_documents(False))

    analysis_context = _analysis_context(context, instructions)
    run_id = (
        "RUN-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6].upper()
    )
    storage.create_run(
        {
            "run_id": run_id,
            "created_at": storage.utcnow(),
            "title": title.strip() or tz_doc.filename,
            "tz_filename": tz_doc.filename,
            "standard_sources": _standard_snapshot(),
            "context": analysis_context,
            "status": "QUEUED",
            "analysis_mode": mode,
            "model": settings.get("model", ""),
            "provider_base_url": settings.get("base_url", ""),
            "params": {
                "analysis_mode": mode,
                "concurrency": int(settings.get("concurrency") or CONFIG.concurrency),
                "embeddings": bool(settings.get("use_embeddings")),
                "embedding_model": settings.get("embedding_model"),
                "standard_scope": scope or "all",
            },
            "warnings": import_warnings,
        }
    )
    logger.info(
        "Прогон поставлен в очередь: run=%s, mode=%s, tz=%s, model=%s, scope=%s, chunks=%d",
        run_id,
        mode,
        tz_doc.filename,
        settings.get("model"),
        len(scope) if scope else "all",
        len(tz_doc.chunks),
    )
    runner.start(
        run_id,
        settings=settings,
        context=analysis_context,
        tz_doc=tz_doc,
        mode=mode,
        params={"analysis_mode": mode},
        source_ids=scope,
    )
    return {
        "run_id": run_id,
        "status": "QUEUED",
        "estimate": _estimate_for(tz_doc, mode, analysis_context, scope),
        "warnings": import_warnings,
        "progress_url": f"/api/runs/{run_id}/progress",
    }


@app.get("/api/runs/{run_id}/progress")
def run_progress(run_id: str) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    return {
        "run_id": run_id,
        "status": run["status"],
        "running": runner.is_running(run_id),
        "progress": runner.get_progress(run_id) or run.get("progress") or {},
        "warnings": run["warnings"],
        "errors": run["errors"],
        "result_count": run["result_count"],
    }


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    if not runner.is_running(run_id) or not runner.cancel(run_id):
        raise HTTPException(status_code=409, detail="Прогон уже завершён или не выполняется.")
    return {"cancelled": True, "run_id": run_id}


# --------------------------------------------------------------------------
# История, сравнение, экспорт
# --------------------------------------------------------------------------


@app.get("/api/runs")
def runs(limit: int = 100) -> list[dict[str, Any]]:
    return storage.list_runs(limit=max(1, min(limit, 500)))


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    return run


@app.get("/api/runs/{base_id}/compare/{other_id}")
def compare_runs(base_id: str, other_id: str) -> dict[str, Any]:
    base = storage.get_run(base_id)
    other = storage.get_run(other_id)
    if not base or not other:
        raise HTTPException(status_code=404, detail="Один из запусков не найден.")

    def key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            " ".join(str(item.get("category", "")).lower().split()),
            " ".join(str(item.get("parameter", "")).lower().split()),
            str(item.get("standard_source_id", "")),
        )

    base_items = {key(item): item for item in base["results"]}
    other_items = {key(item): item for item in other["results"]}
    items: list[dict[str, Any]] = []
    for map_key, before in base_items.items():
        after = other_items.get(map_key)
        if after is None:
            items.append(
                {
                    "kind": "removed",
                    "category": before.get("category"),
                    "parameter": before.get("parameter"),
                    "status_before": before.get("status"),
                    "status_after": "",
                    "severity_before": before.get("severity"),
                    "severity_after": "",
                    "tz_locator": before.get("tz_locator"),
                }
            )
        elif before.get("status") != after.get("status") or before.get("severity") != after.get("severity"):
            items.append(
                {
                    "kind": "changed",
                    "category": after.get("category"),
                    "parameter": after.get("parameter"),
                    "status_before": before.get("status"),
                    "status_after": after.get("status"),
                    "severity_before": before.get("severity"),
                    "severity_after": after.get("severity"),
                    "tz_locator": after.get("tz_locator"),
                }
            )
    for map_key, after in other_items.items():
        if map_key not in base_items:
            items.append(
                {
                    "kind": "added",
                    "category": after.get("category"),
                    "parameter": after.get("parameter"),
                    "status_before": "",
                    "status_after": after.get("status"),
                    "severity_before": "",
                    "severity_after": after.get("severity"),
                    "tz_locator": after.get("tz_locator"),
                }
            )
    order = {"changed": 0, "added": 1, "removed": 2}
    items.sort(key=lambda item: (order.get(item["kind"], 3), str(item.get("parameter"))))
    added_count = sum(1 for item in items if item["kind"] == "added")
    removed_count = sum(1 for item in items if item["kind"] == "removed")
    changed_count = sum(1 for item in items if item["kind"] == "changed")
    summary = {
        "added": added_count,
        "removed": removed_count,
        "changed": changed_count,
        "unchanged": max(0, len(other_items) - added_count),
    }
    return {
        "base": {
            "run_id": base_id,
            "created_at": base["created_at"],
            "title": base["title"],
            "status": base["status"],
            "result_count": base["result_count"],
        },
        "other": {
            "run_id": other_id,
            "created_at": other["created_at"],
            "title": other["title"],
            "status": other["status"],
            "result_count": other["result_count"],
        },
        "summary": summary,
        "items": items[:500],
    }


def summary_added(other_items: dict[Any, Any], base_items: dict[Any, Any]) -> int:
    return sum(1 for key in other_items if key not in base_items)


@app.get("/api/runs/{run_id}/export.json")
def export_json(run_id: str) -> JSONResponse:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    return JSONResponse(
        run, headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'}
    )


STATUS_LABELS = {
    "COMPLIES": "Соответствует",
    "NONCOMPLIANCE": "Несоответствие",
    "MISSING": "Не найдено в ТЗ",
    "EXPERT_REVIEW": "Экспертная оценка",
    "INSUFFICIENT_DATA": "Недостаточно данных",
    "SOURCE_CONFLICT": "Конфликт источников",
    "NOT_APPLICABLE": "Не применимо",
}
SEVERITY_LABELS = {"critical": "Критично", "major": "Существенно", "minor": "Незначительно", "info": "Информация"}
STATUS_FILLS = {
    "COMPLIES": "DCFAE6",
    "NONCOMPLIANCE": "FEE4E2",
    "MISSING": "FEE4E2",
    "EXPERT_REVIEW": "FEF0C7",
    "INSUFFICIENT_DATA": "FEF0C7",
    "SOURCE_CONFLICT": "FEF0C7",
    "NOT_APPLICABLE": "F2F4F7",
}
HEADER_FILL = PatternFill("solid", fgColor="101828")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
THIN = Side(style="thin", color="E4E7EC")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _safe_excel_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _style_header(ws, columns: int) -> None:
    for col in range(1, columns + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _autosize(ws, limit: int = 48) -> None:
    for column in ws.columns:
        letter = get_column_letter(column[0].column)
        width = 12
        for cell in column[:80]:
            value = str(cell.value or "")
            width = max(width, min(limit, len(value) + 2))
        ws.column_dimensions[letter].width = width


def _write_results_sheet(wb: Workbook, run: dict[str, Any]) -> None:
    ws = wb.active
    ws.title = "Результаты"
    headers = [
        "ID",
        "Статус",
        "Важность",
        "Уверенность",
        "Раздел",
        "Категория",
        "Параметр",
        "Тип требования",
        "Тип вывода",
        "Применимость",
        "Семантическое сопоставление",
        "Техническая оценка",
        "Файл ТЗ",
        "Пункт/страница ТЗ",
        "Цитата ТЗ",
        "Источник СТО",
        "Пункт/страница СТО",
        "Цитата СТО",
        "Оператор",
        "Эталон",
        "Факт",
        "Ед. эталона",
        "Ед. факта",
        "Обоснование",
        "Рекомендация",
        "Допущения",
        "Недостающие данные",
        "Цитаты подтверждены",
        "Пересчёт единиц",
        "Примечание проверки",
    ]
    ws.append(headers)
    for result in run["results"]:
        ws.append(
            [
                _safe_excel_text(result.get("result_id")),
                STATUS_LABELS.get(result.get("status"), result.get("status")),
                SEVERITY_LABELS.get(result.get("severity"), result.get("severity")),
                result.get("confidence"),
                _safe_excel_text(result.get("section")),
                _safe_excel_text(result.get("category")),
                _safe_excel_text(result.get("parameter")),
                _safe_excel_text(result.get("requirement_type")),
                _safe_excel_text(result.get("finding_type")),
                _safe_excel_text(result.get("applicability_assessment")),
                _safe_excel_text(result.get("semantic_reasoning")),
                _safe_excel_text(result.get("technical_assessment")),
                _safe_excel_text(result.get("tz_filename")),
                _safe_excel_text(result.get("tz_locator")),
                _safe_excel_text(result.get("tz_quote")),
                _safe_excel_text(result.get("standard_filename")),
                _safe_excel_text(result.get("standard_locator")),
                _safe_excel_text(result.get("standard_quote")),
                _safe_excel_text(result.get("operator")),
                result.get("expected_value"),
                result.get("actual_value"),
                _safe_excel_text(result.get("expected_unit")),
                _safe_excel_text(result.get("actual_unit")),
                _safe_excel_text(result.get("explanation")),
                _safe_excel_text(result.get("recommendation")),
                _safe_excel_text(result.get("assumptions")),
                _safe_excel_text(result.get("missing_data")),
                "да" if result.get("evidence_verified") else "нет",
                _safe_excel_text(result.get("conversion_note")),
                _safe_excel_text(result.get("review_note")),
            ]
        )
    _style_header(ws, len(headers))
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        fill = STATUS_FILLS.get(run["results"][row[0].row - 2].get("status", ""))
        if fill:
            row[1].fill = PatternFill("solid", fgColor=fill)
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER
    _autosize(ws)


def _write_issues_sheet(wb: Workbook, run: dict[str, Any]) -> None:
    ws = wb.create_sheet("Замечания")
    headers = [
        "Важность",
        "Статус",
        "Раздел",
        "Параметр",
        "Цитата ТЗ",
        "Пункт ТЗ",
        "Источник СТО",
        "Цитата СТО",
        "Пункт СТО",
        "Обоснование",
        "Рекомендация",
        "Уверенность",
    ]
    ws.append(headers)
    severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    issues = [
        item
        for item in run["results"]
        if item.get("status") not in {"COMPLIES", "NOT_APPLICABLE"}
    ]
    issues.sort(key=lambda item: (severity_order.get(item.get("severity"), 9), str(item.get("category"))))
    for result in issues:
        ws.append(
            [
                SEVERITY_LABELS.get(result.get("severity"), result.get("severity")),
                STATUS_LABELS.get(result.get("status"), result.get("status")),
                _safe_excel_text(result.get("category")),
                _safe_excel_text(result.get("parameter")),
                _safe_excel_text(result.get("tz_quote")),
                _safe_excel_text(result.get("tz_locator")),
                _safe_excel_text(result.get("standard_filename")),
                _safe_excel_text(result.get("standard_quote")),
                _safe_excel_text(result.get("standard_locator")),
                _safe_excel_text(result.get("explanation")),
                _safe_excel_text(result.get("recommendation")),
                result.get("confidence"),
            ]
        )
    _style_header(ws, len(headers))
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER
    _autosize(ws)


def _write_summary_sheet(wb: Workbook, run: dict[str, Any]) -> None:
    ws = wb.create_sheet("Сводка", 0)
    ws["A1"] = "TZ Checker — сводка проверки"
    ws["A1"].font = Font(bold=True, size=16, color="101828")
    ws.merge_cells("A1:D1")
    rows: list[tuple[str, Any]] = [
        ("ID запуска", run["run_id"]),
        ("Название", run["title"]),
        ("ТЗ", run["tz_filename"]),
        ("Дата", run["created_at"]),
        ("Статус", run["status"]),
        ("Режим", run.get("analysis_mode")),
        ("Модель", run.get("model")),
        ("Провайдер", run.get("provider_base_url")),
        ("Результатов", run["result_count"]),
    ]
    coverage = run.get("coverage") or {}
    for key in (
        "tz_chunks_total",
        "tz_chunks_analyzed",
        "tz_chunks_skipped",
        "llm_batches",
        "batches_completed",
        "batches_failed",
        "standard_documents_in_library",
        "standard_chunks_in_library",
        "standard_chunks_retrieved",
        "semantic_search",
        "embedding_model",
        "concurrency",
    ):
        if key in coverage:
            rows.append((key, coverage[key]))
    usage = run.get("usage") or {}
    if usage:
        rows.append(("Токенов (всего)", usage.get("total_tokens", 0)))
    row_index = 3
    for label, value in rows:
        ws.cell(row=row_index, column=1, value=label).font = Font(bold=True, size=10)
        ws.cell(row=row_index, column=2, value=_safe_excel_text(value))
        row_index += 1

    row_index += 1
    ws.cell(row=row_index, column=1, value="Распределение по статусам").font = Font(bold=True, size=11)
    row_index += 1
    for header, column in (("Статус", 1), ("Количество", 2)):
        cell = ws.cell(row=row_index, column=column, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = BORDER
    row_index += 1
    counts: dict[str, int] = {}
    for item in run["results"]:
        counts[item.get("status", "")] = counts.get(item.get("status", ""), 0) + 1
    for status_value, label in STATUS_LABELS.items():
        ws.cell(row=row_index, column=1, value=label)
        ws.cell(row=row_index, column=2, value=counts.get(status_value, 0))
        if STATUS_FILLS.get(status_value):
            ws.cell(row=row_index, column=1).fill = PatternFill(
                "solid", fgColor=STATUS_FILLS[status_value]
            )
        row_index += 1

    row_index += 1
    ws.cell(row=row_index, column=1, value="По важности").font = Font(bold=True, size=11)
    row_index += 1
    severity_counts: dict[str, int] = {}
    for item in run["results"]:
        severity_counts[item.get("severity", "")] = severity_counts.get(item.get("severity", ""), 0) + 1
    for key, label in SEVERITY_LABELS.items():
        ws.cell(row=row_index, column=1, value=label)
        ws.cell(row=row_index, column=2, value=severity_counts.get(key, 0))
        row_index += 1

    row_index += 1
    ws.cell(row=row_index, column=1, value="По категориям").font = Font(bold=True, size=11)
    row_index += 1
    category_counts: dict[str, int] = {}
    for item in run["results"]:
        category = str(item.get("category") or "Без категории")
        category_counts[category] = category_counts.get(category, 0) + 1
    for category, count in sorted(category_counts.items(), key=lambda pair: pair[1], reverse=True)[:25]:
        ws.cell(row=row_index, column=1, value=_safe_excel_text(category))
        ws.cell(row=row_index, column=2, value=count)
        row_index += 1

    row_index += 1
    ws.cell(row=row_index, column=1, value="Предупреждения").font = Font(bold=True, size=11)
    row_index += 1
    for warning in run["warnings"]:
        ws.cell(row=row_index, column=1, value=_safe_excel_text(warning))
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=4)
        ws.cell(row=row_index, column=1).alignment = Alignment(wrap_text=True, vertical="top")
        row_index += 1
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 22


@app.get("/api/runs/{run_id}/export.xlsx")
def export_xlsx(run_id: str) -> StreamingResponse:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    wb = Workbook()
    _write_results_sheet(wb, run)
    _write_issues_sheet(wb, run)
    _write_summary_sheet(wb, run)
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'},
    )


@app.get("/api/audit")
def audit() -> list[dict[str, Any]]:
    return storage.list_audit()


@app.get("/api/logs")
def read_logs(lines: int = 200) -> dict[str, Any]:
    path = CONFIG.data_dir / "tz-checker.log"
    count = max(1, min(int(lines), 1000))
    if not path.exists():
        return {"path": str(path), "size": 0, "lines": []}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        tail = list(deque(handle, maxlen=count))
    return {"path": str(path), "size": path.stat().st_size, "lines": tail}


@app.get("/api/logs/download")
def download_logs() -> Response:
    path = CONFIG.data_dir / "tz-checker.log"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Журнал ещё не создан.")
    # Читаем снимок файла: FileResponse считает Content-Length по размеру,
    # а журнал дописывается во время отдачи — получается рассинхронизация.
    content = path.read_bytes()
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tz-checker.log"'},
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
