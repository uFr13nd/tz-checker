import json
import sqlite3

from app import secrets, storage


def test_settings_roundtrip_and_public_view():
    saved = storage.save_settings(
        {
            "base_url": "https://provider.example/v1",
            "model": "model-x",
            "api_key": "test-secret",
            "google_client_id": "client-id",
            "spreadsheet_id": "sheet-1",
            "concurrency": 4,
            "use_embeddings": False,
        }
    )
    assert saved["has_api_key"] is True
    assert "api_key" not in saved
    assert saved["concurrency"] == 4
    assert storage.get_settings()["api_key"] == "test-secret"
    raw = json.loads(storage.SETTINGS_PATH.read_text(encoding="utf-8"))
    if secrets.encryption_available():
        assert "api_key" not in raw
        assert raw["api_key_enc"]
    else:
        assert raw["api_key"] == "test-secret"


def test_clear_api_key():
    storage.save_settings({"base_url": "https://x/v1", "model": "m", "api_key": "secret"})
    result = storage.save_settings({"base_url": "https://x/v1", "model": "m", "clear_api_key": True})
    assert result["has_api_key"] is False
    assert storage.get_settings()["api_key"] == ""


def test_separate_embedding_provider_settings():
    saved = storage.save_settings(
        {
            "base_url": "https://ollama.com/v1",
            "model": "chat-model",
            "api_key": "chat-secret",
            "use_embeddings": True,
            "embedding_base_url": "http://127.0.0.1:11434/v1",
            "embedding_model": "nomic-embed-text",
            "embedding_api_key": "",
        }
    )
    assert saved["embedding_base_url"] == "http://127.0.0.1:11434/v1"
    assert saved["has_embedding_api_key"] is False
    settings = storage.get_settings()
    assert settings["embedding_base_url"] == "http://127.0.0.1:11434/v1"
    assert settings["base_url"] == "https://ollama.com/v1"


def test_library_import_search_status_and_embeddings():
    now = storage.utcnow()
    added = storage.add_standard_document(
        source_id="STD-001",
        filename="sto.md",
        source_url=None,
        sha256="hash-1",
        imported_at=now,
        text="Требования к вентиляционной шахте.",
        chunks=[{"locator": "п. 5.3.2", "text": "Толщина стенки вентиляционной шахты не менее 200 мм."}],
    )
    assert added["duplicate"] is False
    assert storage.standard_library_stats()["documents"] == 1
    hits = storage.search_standard_chunks("толщина шахта 200 мм", limit=5)
    assert hits and hits[0]["source_id"] == "STD-001"
    assert hits[0]["chunk_id"]

    assert storage.update_standard_status("STD-001", "ACTIVE") is True
    assert storage.list_standard_documents()[0]["status"] == "ACTIVE"
    changed = storage.bulk_update_standard_status(["STD-001"], revision="2026")
    assert changed == 1
    assert storage.list_standard_documents()[0]["revision"] == "2026"

    storage.save_embeddings([(hits[0]["chunk_id"], b"\x00\x00\x80\x3f", 1)], "test-embed")
    assert hits[0]["chunk_id"] in storage.get_embedding_map("test-embed")

    duplicate = storage.add_standard_document(
        source_id="STD-002", filename="dup.md", source_url=None, sha256="hash-1",
        imported_at=now, text="same", chunks=[],
    )
    assert duplicate["duplicate"] is True

    assert storage.delete_standard_document("STD-001") is True
    assert storage.standard_library_stats()["documents"] == 0


def test_run_lifecycle_and_batches():
    run_id = "RUN-TEST-1"
    storage.create_run(
        {
            "run_id": run_id,
            "created_at": storage.utcnow(),
            "title": "test",
            "tz_filename": "tz.md",
            "standard_sources": [{"source_id": "STD-1"}],
            "context": "ctx",
            "status": "QUEUED",
            "analysis_mode": "full",
            "model": "m",
        }
    )
    storage.update_run(run_id, status="RUNNING", progress_json=json.dumps({"phase": "analysis"}))
    storage.save_run_batch(
        run_id, 0, tz_locators="п. 1", status="DONE",
        results=[{"result_id": "RES-0001", "status": "COMPLIES"}],
        prompt_tokens=10, completion_tokens=5,
    )
    batches = storage.list_run_batches(run_id)
    assert batches[0]["status"] == "DONE"
    assert batches[0]["results"][0]["status"] == "COMPLIES"

    storage.finalize_run(
        run_id,
        status="COMPLETED",
        coverage={"tz_chunks_analyzed": 1},
        warnings=[],
        results=[{"result_id": "RES-0001", "status": "COMPLIES"}],
        usage={"total_tokens": 15},
        errors=[],
    )
    run = storage.get_run(run_id)
    assert run["status"] == "COMPLETED"
    assert run["results"][0]["result_id"] == "RES-0001"
    assert run["coverage"]["tz_chunks_analyzed"] == 1
    assert run["usage"]["total_tokens"] == 15
    assert storage.list_runs()[0]["run_id"] == run_id


def test_templates_crud():
    template = storage.save_template("ЖК", "контекст", "инструкции")
    assert template["id"]
    assert storage.list_templates()[0]["name"] == "ЖК"
    assert storage.delete_template(template["id"]) is True
    assert storage.list_templates() == []


def test_migration_from_old_schema(store):
    old_db = storage.DB_PATH
    if old_db.exists():
        old_db.unlink()
    with sqlite3.connect(old_db) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL,
                tz_filename TEXT NOT NULL, standard_sources_json TEXT NOT NULL, context TEXT NOT NULL,
                status TEXT NOT NULL, coverage_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
                result_count INTEGER NOT NULL, result_json TEXT NOT NULL
            );
            CREATE TABLE standard_documents (
                source_id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_url TEXT,
                sha256 TEXT NOT NULL UNIQUE, imported_at TEXT NOT NULL, status TEXT NOT NULL,
                revision TEXT NOT NULL, applicability TEXT NOT NULL, character_count INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL
            );
            CREATE TABLE standard_chunks (id INTEGER PRIMARY KEY, source_id TEXT, locator TEXT, text TEXT);
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY, timestamp TEXT, action TEXT, entity_id TEXT, details_json TEXT
            );
            INSERT INTO runs VALUES ('RUN-OLD','2026-01-01','old','tz.md','[]','ctx','COMPLETED',
                '{}','[]',0,'[]');
            """
        )
    storage.init_db()
    run = storage.get_run("RUN-OLD")
    assert run is not None
    assert run["analysis_mode"] == "quick"
    assert run["progress"] == {}
