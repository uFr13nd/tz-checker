"""SQLite-хранилище TZ Checker: библиотека СТО, прогоны, кэш эмбеддингов, настройки."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import secrets
from app.config import CONFIG
from app.textutils import fts_query

DATA_DIR: Path = CONFIG.data_dir
DB_PATH: Path = DATA_DIR / "tz_checker.sqlite3"
SETTINGS_PATH: Path = DATA_DIR / "settings.json"
DEFAULT_SPREADSHEET_ID = "1Jj-fjdu6ZtfWtieAoF4PFr9b24cdbxfVDyN1acE79HY"

DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4.1-mini",
    "api_key": "",
    "google_client_id": "",
    "spreadsheet_id": DEFAULT_SPREADSHEET_ID,
    "use_embeddings": True,
    "embedding_model": CONFIG.embedding_model,
    "embedding_base_url": "",
    "embedding_api_key": "",
    "concurrency": CONFIG.concurrency,
    "price_input": CONFIG.price_input,
    "price_output": CONFIG.price_output,
}

RUN_FIELDS = (
    "status",
    "coverage_json",
    "warnings_json",
    "result_count",
    "result_json",
    "progress_json",
    "error_json",
    "usage_json",
    "started_at",
    "finished_at",
    "title",
    "params_json",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextmanager
def _db_session():
    """Соединение на время операции: commit при успехе, close всегда."""
    conn = connect_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    tz_filename TEXT NOT NULL,
    standard_sources_json TEXT NOT NULL,
    context TEXT NOT NULL,
    status TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    analysis_mode TEXT NOT NULL DEFAULT 'quick',
    model TEXT NOT NULL DEFAULT '',
    provider_base_url TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    progress_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    params_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS standard_documents (
    source_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    source_url TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    revision TEXT NOT NULL DEFAULT '',
    applicability TEXT NOT NULL DEFAULT '',
    character_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS standard_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    locator TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_standard_chunks_source ON standard_chunks(source_id);
CREATE TABLE IF NOT EXISTS run_batches (
    run_id TEXT NOT NULL,
    batch_index INTEGER NOT NULL,
    tz_locators TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    results_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, batch_index)
);
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, model)
);
CREATE TABLE IF NOT EXISTS context_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_batches_run ON run_batches(run_id);
"""

SCHEMA_VERSION = 2


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    ensure_data_dir()
    with _db_session() as conn:
        conn.executescript(BASE_SCHEMA)
        # Миграции для баз, созданных до 0.3.0.
        _ensure_column(conn, "runs", "analysis_mode", "TEXT NOT NULL DEFAULT 'quick'")
        _ensure_column(conn, "runs", "model", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "provider_base_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "started_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "finished_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "progress_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "runs", "error_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "runs", "usage_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "runs", "params_json", "TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS standard_chunks_fts USING fts5("
                "text, source_id UNINDEXED, filename UNINDEXED, locator UNINDEXED, "
                "tokenize='unicode61 remove_diacritics 2')"
            )
        except sqlite3.OperationalError:
            pass
    migrate_settings_file()


# --------------------------------------------------------------------------
# Библиотека СТО
# --------------------------------------------------------------------------


def add_standard_document(
    *,
    source_id: str,
    filename: str,
    source_url: str | None,
    sha256: str,
    imported_at: str,
    text: str,
    chunks: list[dict[str, str]],
    status: str = "PENDING_REVIEW",
    revision: str = "",
    applicability: str = "",
) -> dict[str, Any]:
    """Сохранить СТО и его поисковые фрагменты. Дубликат по sha256 не добавляется."""
    with _db_session() as conn:
        existing = conn.execute(
            "SELECT source_id, filename, chunk_count FROM standard_documents WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
        if existing:
            return {
                "source_id": existing["source_id"],
                "filename": existing["filename"],
                "chunk_count": existing["chunk_count"],
                "duplicate": True,
                "status": "DUPLICATE",
            }
        conn.execute(
            "INSERT INTO standard_documents(source_id, filename, source_url, sha256, imported_at,"
            " status, revision, applicability, character_count, chunk_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                filename,
                source_url,
                sha256,
                imported_at,
                status,
                revision,
                applicability,
                len(text),
                len(chunks),
            ),
        )
        for chunk in chunks:
            cursor = conn.execute(
                "INSERT INTO standard_chunks(source_id, locator, text) VALUES (?, ?, ?)",
                (source_id, chunk["locator"], chunk["text"]),
            )
            row_id = cursor.lastrowid
            try:
                conn.execute(
                    "INSERT INTO standard_chunks_fts(rowid, text, source_id, filename, locator)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (row_id, chunk["text"], source_id, filename, chunk["locator"]),
                )
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "INSERT INTO audit_log(timestamp, action, entity_id, details_json) VALUES (?, ?, ?, ?)",
            (
                imported_at,
                "STANDARD_IMPORTED",
                source_id,
                json.dumps(
                    {
                        "filename": filename,
                        "sha256": sha256,
                        "chunks": len(chunks),
                        "status": status,
                        "characters": len(text),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    return {
        "source_id": source_id,
        "filename": filename,
        "chunk_count": len(chunks),
        "duplicate": False,
        "status": status,
    }


def list_standard_documents(include_retired: bool = True) -> list[dict[str, Any]]:
    query = (
        "SELECT source_id, filename, source_url, sha256, imported_at, status, revision,"
        " applicability, character_count, chunk_count FROM standard_documents"
    )
    params: tuple[Any, ...] = ()
    if not include_retired:
        query += " WHERE status != ?"
        params = ("RETIRED",)
    query += " ORDER BY filename COLLATE NOCASE"
    with _db_session() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def standard_library_stats() -> dict[str, int]:
    with _db_session() as conn:
        docs = conn.execute(
            "SELECT COUNT(*) FROM standard_documents WHERE status != 'RETIRED'"
        ).fetchone()[0]
        chunks = conn.execute(
            "SELECT COUNT(*) FROM standard_chunks sc JOIN standard_documents sd"
            " ON sd.source_id = sc.source_id WHERE sd.status != 'RETIRED'"
        ).fetchone()[0]
        chars = conn.execute(
            "SELECT COALESCE(SUM(character_count), 0) FROM standard_documents"
            " WHERE status != 'RETIRED'"
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM standard_documents WHERE status = 'ACTIVE'"
        ).fetchone()[0]
    return {
        "documents": int(docs),
        "chunks": int(chunks),
        "characters": int(chars),
        "active": int(active),
    }


def update_standard_status(
    source_id: str,
    status: str | None = None,
    revision: str | None = None,
    applicability: str | None = None,
) -> bool:
    allowed = {"PENDING_REVIEW", "ACTIVE", "DRAFT", "RETIRED"}
    if status is not None and status not in allowed:
        raise ValueError("Unsupported standard status")
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if revision is not None:
        fields.append("revision = ?")
        values.append(revision.strip())
    if applicability is not None:
        fields.append("applicability = ?")
        values.append(applicability.strip())
    if not fields:
        raise ValueError("No metadata fields supplied")
    values.append(source_id)
    now = utcnow()
    with _db_session() as conn:
        cursor = conn.execute(
            "UPDATE standard_documents SET " + ", ".join(fields) + " WHERE source_id = ?",
            tuple(values),
        )
        if cursor.rowcount:
            conn.execute(
                "INSERT INTO audit_log(timestamp, action, entity_id, details_json)"
                " VALUES (?, ?, ?, ?)",
                (
                    now,
                    "STANDARD_METADATA_CHANGED",
                    source_id,
                    json.dumps(
                        {"status": status, "revision": revision, "applicability": applicability},
                        ensure_ascii=False,
                    ),
                ),
            )
        return bool(cursor.rowcount)


def bulk_update_standard_status(
    source_ids: list[str],
    status: str | None = None,
    revision: str | None = None,
    applicability: str | None = None,
) -> int:
    allowed = {"PENDING_REVIEW", "ACTIVE", "DRAFT", "RETIRED"}
    if status is not None and status not in allowed:
        raise ValueError("Unsupported standard status")
    changed = 0
    for source_id in dict.fromkeys(source_ids):
        if update_standard_status(source_id, status, revision, applicability):
            changed += 1
    if changed:
        log_audit(
            "STANDARD_BULK_CHANGED",
            ",".join(source_ids[:20]),
            {"count": changed, "status": status, "revision": revision},
        )
    return changed


def delete_standard_document(source_id: str) -> bool:
    now = utcnow()
    with _db_session() as conn:
        row = conn.execute(
            "SELECT filename FROM standard_documents WHERE source_id = ?", (source_id,)
        ).fetchone()
        if not row:
            return False
        chunk_ids = [
            item[0]
            for item in conn.execute(
                "SELECT id FROM standard_chunks WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]
        try:
            conn.execute("DELETE FROM standard_chunks_fts WHERE source_id = ?", (source_id,))
        except sqlite3.OperationalError:
            pass
        for chunk_id in chunk_ids:
            conn.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (chunk_id,))
        conn.execute("DELETE FROM standard_chunks WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM standard_documents WHERE source_id = ?", (source_id,))
        conn.execute(
            "INSERT INTO audit_log(timestamp, action, entity_id, details_json) VALUES (?, ?, ?, ?)",
            (
                now,
                "STANDARD_DELETED",
                source_id,
                json.dumps({"filename": row["filename"], "chunks": len(chunk_ids)}, ensure_ascii=False),
            ),
        )
    return True


def library_chunks(include_retired: bool = False) -> list[dict[str, Any]]:
    query = (
        "SELECT sc.id AS chunk_id, sc.source_id, sc.locator, sc.text, sd.filename,"
        " sd.source_url, sd.status, sd.revision, sd.applicability"
        " FROM standard_chunks sc JOIN standard_documents sd ON sd.source_id = sc.source_id"
    )
    if not include_retired:
        query += " WHERE sd.status != 'RETIRED'"
    query += " ORDER BY sc.source_id, sc.id"
    with _db_session() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]


def search_standard_chunks(query: str, limit: int = 32) -> list[dict[str, Any]]:
    """Полнотекстовый поиск FTS5 (один из сигналов гибридного ретрива)."""
    match_query = fts_query(query)
    if not match_query:
        return []
    safe_limit = max(1, min(int(limit), 200))
    with _db_session() as conn:
        try:
            rows = conn.execute(
                """SELECT sc.id AS chunk_id, sc.source_id, sd.filename, sd.source_url, sd.status,
                          sd.revision, sd.applicability, sc.locator, sc.text,
                          bm25(standard_chunks_fts) AS rank
                   FROM standard_chunks_fts
                   JOIN standard_chunks sc ON sc.id = standard_chunks_fts.rowid
                   JOIN standard_documents sd ON sd.source_id = sc.source_id
                   WHERE standard_chunks_fts MATCH ? AND sd.status != 'RETIRED'
                   ORDER BY rank LIMIT ?""",
                (match_query, safe_limit),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []


# --------------------------------------------------------------------------
# Эмбеддинги
# --------------------------------------------------------------------------


def get_embedding_map(model: str) -> dict[int, bytes]:
    with _db_session() as conn:
        rows = conn.execute(
            "SELECT chunk_id, vector FROM chunk_embeddings WHERE model = ?", (model,)
        ).fetchall()
    return {int(row["chunk_id"]): row["vector"] for row in rows}


def save_embeddings(items: list[tuple[int, bytes, int]], model: str) -> None:
    now = utcnow()
    with _db_session() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO chunk_embeddings(chunk_id, model, dim, vector, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(chunk_id, model, dim, vector, now) for chunk_id, vector, dim in items],
        )


# --------------------------------------------------------------------------
# Прогоны
# --------------------------------------------------------------------------


def create_run(run: dict[str, Any]) -> None:
    with _db_session() as conn:
        conn.execute(
            "INSERT INTO runs(run_id, created_at, title, tz_filename, standard_sources_json,"
            " context, status, coverage_json, warnings_json, result_count, result_json,"
            " analysis_mode, model, provider_base_url, started_at, finished_at, progress_json,"
            " error_json, usage_json, params_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run["run_id"],
                run["created_at"],
                run["title"],
                run["tz_filename"],
                json.dumps(run.get("standard_sources", []), ensure_ascii=False),
                run.get("context", ""),
                run.get("status", "QUEUED"),
                json.dumps(run.get("coverage", {}), ensure_ascii=False),
                json.dumps(run.get("warnings", []), ensure_ascii=False),
                len(run.get("results", [])),
                json.dumps(run.get("results", []), ensure_ascii=False),
                run.get("analysis_mode", "quick"),
                run.get("model", ""),
                run.get("provider_base_url", ""),
                run.get("started_at", ""),
                run.get("finished_at", ""),
                json.dumps(run.get("progress", {}), ensure_ascii=False),
                json.dumps(run.get("errors", []), ensure_ascii=False),
                json.dumps(run.get("usage", {}), ensure_ascii=False),
                json.dumps(run.get("params", {}), ensure_ascii=False),
            ),
        )


def update_run(run_id: str, **fields: Any) -> None:
    updates = {key: value for key, value in fields.items() if key in RUN_FIELDS}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with _db_session() as conn:
        conn.execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?",
            tuple(updates.values()) + (run_id,),
        )


def save_run_batch(
    run_id: str,
    batch_index: int,
    *,
    tz_locators: str,
    status: str,
    results: list[dict[str, Any]],
    error: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    duration_ms: int = 0,
) -> None:
    with _db_session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_batches(run_id, batch_index, tz_locators, status,"
            " results_json, error, prompt_tokens, completion_tokens, duration_ms, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                batch_index,
                tz_locators,
                status,
                json.dumps(results, ensure_ascii=False),
                error[:1000],
                prompt_tokens,
                completion_tokens,
                duration_ms,
                utcnow(),
            ),
        )


def list_run_batches(run_id: str) -> list[dict[str, Any]]:
    with _db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM run_batches WHERE run_id = ? ORDER BY batch_index", (run_id,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["results"] = json.loads(item.pop("results_json") or "[]")
        result.append(item)
    return result


def finalize_run(
    run_id: str,
    *,
    status: str,
    coverage: dict[str, Any],
    warnings: list[str],
    results: list[dict[str, Any]],
    usage: dict[str, Any],
    errors: list[str],
) -> None:
    update_run(
        run_id,
        status=status,
        coverage_json=json.dumps(coverage, ensure_ascii=False),
        warnings_json=json.dumps(warnings, ensure_ascii=False),
        result_count=len(results),
        result_json=json.dumps(results, ensure_ascii=False),
        usage_json=json.dumps(usage, ensure_ascii=False),
        error_json=json.dumps(errors, ensure_ascii=False),
        finished_at=utcnow(),
    )
    log_audit(
        "ANALYSIS_FINISHED",
        run_id,
        {"status": status, "results": len(results), "errors": len(errors)},
    )


def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    with _db_session() as conn:
        rows = conn.execute(
            "SELECT run_id, created_at, title, tz_filename, status, result_count,"
            " analysis_mode, model, finished_at FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    with _db_session() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["standard_sources"] = json.loads(data.pop("standard_sources_json") or "[]")
    data["coverage"] = json.loads(data.pop("coverage_json") or "{}")
    data["warnings"] = json.loads(data.pop("warnings_json") or "[]")
    data["results"] = json.loads(data.pop("result_json") or "[]")
    data["progress"] = json.loads(data.pop("progress_json") or "{}")
    data["errors"] = json.loads(data.pop("error_json") or "[]")
    data["usage"] = json.loads(data.pop("usage_json") or "{}")
    data["params"] = json.loads(data.pop("params_json") or "{}")
    return data


def mark_stale_runs() -> int:
    """Прогоны, прерванные перезапуском сервера, помечаются как FAILED."""
    with _db_session() as conn:
        cursor = conn.execute(
            "UPDATE runs SET status = 'FAILED', finished_at = ?,"
            " error_json = ? WHERE status IN ('QUEUED', 'RUNNING')",
            (
                utcnow(),
                json.dumps(
                    ["Сервер был перезапущен во время проверки — запустите анализ повторно."],
                    ensure_ascii=False,
                ),
            ),
        )
        return cursor.rowcount


# --------------------------------------------------------------------------
# HTML/context шаблоны
# --------------------------------------------------------------------------


def list_templates() -> list[dict[str, Any]]:
    with _db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM context_templates ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def save_template(
    name: str,
    context: str,
    instructions: str,
    template_id: str | None = None,
) -> dict[str, Any]:
    now = utcnow()
    tid = template_id or "TPL-" + hashlib.sha1(f"{name}{now}".encode()).hexdigest()[:10].upper()
    with _db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM context_templates WHERE id = ?", (tid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE context_templates SET name = ?, context = ?, instructions = ?,"
                " updated_at = ? WHERE id = ?",
                (name, context, instructions, now, tid),
            )
        else:
            conn.execute(
                "INSERT INTO context_templates(id, name, context, instructions, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (tid, name, context, instructions, now, now),
            )
    return {"id": tid, "name": name, "context": context, "instructions": instructions}


def delete_template(template_id: str) -> bool:
    with _db_session() as conn:
        cursor = conn.execute("DELETE FROM context_templates WHERE id = ?", (template_id,))
        return bool(cursor.rowcount)


# --------------------------------------------------------------------------
# Аудит
# --------------------------------------------------------------------------


def log_audit(action: str, entity_id: str, details: dict[str, Any] | None = None) -> None:
    with _db_session() as conn:
        conn.execute(
            "INSERT INTO audit_log(timestamp, action, entity_id, details_json) VALUES (?, ?, ?, ?)",
            (utcnow(), action, entity_id, json.dumps(details or {}, ensure_ascii=False)),
        )


def list_audit(limit: int = 100) -> list[dict[str, Any]]:
    with _db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json"))
        except (ValueError, TypeError):
            item["details"] = {}
        result.append(item)
    return result


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


def _read_settings_file() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _decrypt_settings(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in data and data[key] is not None:
            result[key] = data[key]
    api_key = ""
    enc = data.get("api_key_enc")
    if isinstance(enc, str) and enc:
        api_key = secrets.decrypt_secret(enc) or ""
    elif isinstance(data.get("api_key"), str):
        api_key = data["api_key"]
    result["api_key"] = api_key

    embedding_key = ""
    embedding_enc = data.get("embedding_api_key_enc")
    if isinstance(embedding_enc, str) and embedding_enc:
        embedding_key = secrets.decrypt_secret(embedding_enc) or ""
    elif isinstance(data.get("embedding_api_key"), str):
        embedding_key = data["embedding_api_key"]
    result["embedding_api_key"] = embedding_key
    return result


def _apply_env(settings: dict[str, Any]) -> dict[str, Any]:
    result = dict(settings)
    if CONFIG.seed_base_url:
        result["base_url"] = CONFIG.seed_base_url
    if CONFIG.seed_model:
        result["model"] = CONFIG.seed_model
    if CONFIG.seed_api_key:
        result["api_key"] = CONFIG.seed_api_key
    if CONFIG.seed_embedding_base_url:
        result["embedding_base_url"] = CONFIG.seed_embedding_base_url
    if CONFIG.seed_embedding_api_key:
        result["embedding_api_key"] = CONFIG.seed_embedding_api_key
    if not CONFIG.use_embeddings:
        result["use_embeddings"] = False
    if CONFIG.price_input:
        result["price_input"] = CONFIG.price_input
    if CONFIG.price_output:
        result["price_output"] = CONFIG.price_output
    return result


def get_settings() -> dict[str, Any]:
    ensure_data_dir()
    return _apply_env(_decrypt_settings(_read_settings_file()))


def public_settings() -> dict[str, Any]:
    settings = get_settings()
    return {
        "base_url": settings["base_url"],
        "model": settings["model"],
        "has_api_key": bool(settings.get("api_key")),
        "api_key_from_env": bool(CONFIG.seed_api_key),
        "key_encrypted": secrets.encryption_available(),
        "google_client_id": settings.get("google_client_id", ""),
        "spreadsheet_id": settings.get("spreadsheet_id", DEFAULT_SPREADSHEET_ID),
        "use_embeddings": bool(settings.get("use_embeddings", True)),
        "embedding_model": settings.get("embedding_model", CONFIG.embedding_model),
        "embedding_base_url": settings.get("embedding_base_url", ""),
        "has_embedding_api_key": bool(settings.get("embedding_api_key")),
        "concurrency": int(settings.get("concurrency", CONFIG.concurrency) or CONFIG.concurrency),
        "price_input": float(settings.get("price_input", 0) or 0),
        "price_output": float(settings.get("price_output", 0) or 0),
    }


def _atomic_write(path: Path, text: str) -> None:
    ensure_data_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    # Для ключа берём именно файловое значение: переменная окружения
    # TZ_CHECKER_API_KEY не должна попадать на диск.
    file_settings = _decrypt_settings(_read_settings_file())
    old = get_settings()
    updated: dict[str, Any] = {
        "base_url": str(payload.get("base_url") or old["base_url"]).strip().rstrip("/"),
        "model": str(payload.get("model") or old["model"]).strip(),
        "google_client_id": (
            old.get("google_client_id", "")
            if payload.get("google_client_id") is None
            else str(payload.get("google_client_id")).strip()
        ),
        "spreadsheet_id": str(
            payload.get("spreadsheet_id") or old.get("spreadsheet_id") or DEFAULT_SPREADSHEET_ID
        ).strip(),
        "use_embeddings": bool(
            old.get("use_embeddings", True)
            if payload.get("use_embeddings") is None
            else payload.get("use_embeddings")
        ),
        "embedding_model": str(
            payload.get("embedding_model") or old.get("embedding_model") or CONFIG.embedding_model
        ).strip(),
        "embedding_base_url": str(
            payload.get("embedding_base_url")
            if payload.get("embedding_base_url") is not None
            else old.get("embedding_base_url", "")
        ).strip().rstrip("/"),
        "concurrency": max(
            1,
            min(
                8,
                int(
                    payload.get("concurrency")
                    or old.get("concurrency", CONFIG.concurrency)
                    or CONFIG.concurrency
                ),
            ),
        ),
        "price_input": float(payload.get("price_input", old.get("price_input", 0)) or 0),
        "price_output": float(payload.get("price_output", old.get("price_output", 0)) or 0),
    }

    api_key = file_settings.get("api_key", "")
    if payload.get("clear_api_key"):
        api_key = ""
    elif isinstance(payload.get("api_key"), str) and payload["api_key"].strip():
        api_key = payload["api_key"].strip()

    embedding_key = file_settings.get("embedding_api_key", "")
    if payload.get("clear_embedding_api_key"):
        embedding_key = ""
    elif isinstance(payload.get("embedding_api_key"), str) and payload["embedding_api_key"].strip():
        embedding_key = payload["embedding_api_key"].strip()

    stored: dict[str, Any] = dict(updated)
    encrypted = secrets.encrypt_secret(api_key) if api_key else None
    if encrypted:
        stored["api_key_enc"] = encrypted
    elif api_key:
        stored["api_key"] = api_key
    embedding_encrypted = secrets.encrypt_secret(embedding_key) if embedding_key else None
    if embedding_encrypted:
        stored["embedding_api_key_enc"] = embedding_encrypted
    elif embedding_key:
        stored["embedding_api_key"] = embedding_key
    _atomic_write(SETTINGS_PATH, json.dumps(stored, ensure_ascii=False, indent=2))
    if not secrets.encryption_available():
        try:
            SETTINGS_PATH.chmod(0o600)
        except OSError:
            pass
    return public_settings()


def migrate_settings_file() -> None:
    """Зашифровать ключи, сохранённые старой версией в открытом виде."""
    if not SETTINGS_PATH.exists() or not secrets.encryption_available():
        return
    data = _read_settings_file()
    changed = False
    for plain_name, enc_name in (("api_key", "api_key_enc"), ("embedding_api_key", "embedding_api_key_enc")):
        plaintext = data.get(plain_name)
        if not (isinstance(plaintext, str) and plaintext.strip()):
            continue
        encrypted = secrets.encrypt_secret(plaintext.strip())
        if not encrypted or secrets.decrypt_secret(encrypted) != plaintext.strip():
            continue
        data.pop(plain_name, None)
        data[enc_name] = encrypted
        changed = True
    if not changed:
        return
    try:
        _atomic_write(SETTINGS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    except OSError:
        pass
