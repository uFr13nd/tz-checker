"""Конфигурация приложения TZ Checker.

Все параметры можно переопределить переменными окружения — это нужно для
развёртывания на сервере (Docker / systemd), где файл data/settings.json
может отсутствовать или быть недоступным для записи.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "0.3.0"
ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)).replace(",", "."))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    auth_token: str
    allowed_origins: tuple[str, ...]
    max_upload_mb: int
    concurrency: int
    log_level: str
    seed_base_url: str
    seed_model: str
    seed_api_key: str
    seed_embedding_base_url: str
    seed_embedding_api_key: str
    price_input: float
    price_output: float
    embedding_model: str
    use_embeddings: bool
    ocr_enabled: bool

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_config() -> Config:
    data_dir = Path(_env("TZ_CHECKER_DATA_DIR", str(ROOT / "data"))).expanduser()
    origins = tuple(
        item.strip().rstrip("/")
        for item in _env("TZ_CHECKER_ORIGINS", "").split(",")
        if item.strip()
    )
    return Config(
        data_dir=data_dir,
        auth_token=_env("TZ_CHECKER_TOKEN"),
        allowed_origins=origins,
        max_upload_mb=_env_int("TZ_CHECKER_MAX_UPLOAD_MB", 20, 1, 200),
        concurrency=_env_int("TZ_CHECKER_CONCURRENCY", 3, 1, 8),
        log_level=_env("TZ_CHECKER_LOG_LEVEL", "INFO").upper(),
        seed_base_url=_env("TZ_CHECKER_BASE_URL", ""),
        seed_model=_env("TZ_CHECKER_MODEL", ""),
        seed_api_key=_env("TZ_CHECKER_API_KEY"),
        seed_embedding_base_url=_env("TZ_CHECKER_EMBEDDING_BASE_URL"),
        seed_embedding_api_key=_env("TZ_CHECKER_EMBEDDING_API_KEY"),
        price_input=_env_float("TZ_CHECKER_PRICE_INPUT", 0.0),
        price_output=_env_float("TZ_CHECKER_PRICE_OUTPUT", 0.0),
        embedding_model=_env("TZ_CHECKER_EMBEDDING_MODEL", "text-embedding-3-small"),
        use_embeddings=_env_bool("TZ_CHECKER_USE_EMBEDDINGS", True),
        ocr_enabled=_env_bool("TZ_CHECKER_OCR", False),
    )


CONFIG = load_config()
