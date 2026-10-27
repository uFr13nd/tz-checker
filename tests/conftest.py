import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

# Логирование настраивается при импорте app.main, поэтому временную папку
# данных задаём до импорта тестовых модулей: журнал и SQLite тестов не должны
# попадать в рабочую data/.
TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="tz-checker-tests-"))
os.environ["TZ_CHECKER_DATA_DIR"] = str(TEST_DATA_ROOT)


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch):
    """Каждый тест работает в собственной временной папке данных.

    Autouse — страховка от случайной записи в реальные data/settings.json и
    data/tz_checker.sqlite3.
    """
    from app import storage

    base = Path(__file__).resolve().parent / "_runtime" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", base)
    monkeypatch.setattr(storage, "DB_PATH", base / "db.sqlite3")
    monkeypatch.setattr(storage, "SETTINGS_PATH", base / "settings.json")
    storage.init_db()
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def store(isolated_storage):
    return isolated_storage


@pytest.fixture()
def client(store):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
