"""Клиент OpenAI-совместимого провайдера: chat/completions и embeddings.

Содержит повторы с экспоненциальной задержкой (429/5xx/сеть), безопасный
разбор JSON и учёт токенов. Один и тот же модуль используют и фоновый
прогон, и проверка подключения в настройках.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

RETRIABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}

logger = logging.getLogger("tz-checker.llm")


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 502, retriable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retriable = retriable


def _base_url(settings: dict[str, Any]) -> str:
    return str(settings.get("base_url") or "https://api.openai.com/v1").strip().rstrip("/")


def _headers(settings: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = str(settings.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def is_local_provider(base_url: str) -> bool:
    value = (base_url or "").strip().lower()
    return value.startswith("http://127.0.0.1") or value.startswith("http://localhost")


def normalize_base_url(base_url: str) -> str:
    """Привести частые ошибки к OpenAI-совместимому адресу.

    Приложение само добавляет /chat/completions и /embeddings, поэтому
    native-пути Ollama (/api/chat, /api) нужно заменить на /v1.
    """
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return value
    parsed = urlparse(value)
    path = (parsed.path or "").rstrip("/")
    for suffix in ("/api/chat", "/api/generate", "/api/embed", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    host = (parsed.hostname or "").lower()
    if not path:
        if (host in {"127.0.0.1", "localhost"} and parsed.port == 11434) or host == "ollama.com":
            path = "/v1"
    return urlunparse(parsed._replace(path=path))


def validate_base_url(base_url: str) -> str:
    value = normalize_base_url(base_url)
    if not value:
        raise ProviderError("Укажите Base URL API.", status=422)
    if not value.startswith("https://") and not is_local_provider(value):
        raise ProviderError("URL API должен использовать HTTPS; HTTP разрешён только для localhost.", status=422)
    return value


def _clean_json_text(content: str) -> str:
    text = (content or "").strip().lstrip("\ufeff")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .strip()
    )


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Частая проблема reasoning-моделей — висячая запятая перед } или ].
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(cleaned)


def _balanced_objects(text: str) -> list[str]:
    """Вытащить все сбалансированные JSON-объекты из текста с рассуждениями."""
    result: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                result.append(text[start : index + 1])
                start = None
    return result


def _as_result_object(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        return {"results": data}
    if not isinstance(data, dict):
        raise ValueError("Ответ модели не является JSON-объектом")
    results = data.get("results")
    if isinstance(results, str):
        try:
            data["results"] = _loads_lenient(results)
        except json.JSONDecodeError:
            pass
    if "results" in data:
        return data
    # Модель иногда возвращает один результат без обёртки.
    if {"category", "parameter", "status"} & set(data.keys()):
        return {"results": [data]}
    return data


def _extract_json(content: str) -> dict[str, Any]:
    text = _clean_json_text(content)
    try:
        return _as_result_object(_loads_lenient(text))
    except (json.JSONDecodeError, ValueError):
        pass

    parsed: list[Any] = []
    for candidate in _balanced_objects(text):
        try:
            parsed.append(_loads_lenient(candidate))
        except json.JSONDecodeError:
            continue
    if parsed:
        merged: list[Any] = []
        for item in parsed:
            try:
                data = _as_result_object(item)
            except ValueError:
                continue
            if isinstance(data.get("results"), list):
                merged.extend(data["results"])
        if merged:
            return {"results": merged}
        if len(parsed) == 1:
            return _as_result_object(parsed[0])
    raise ValueError("В ответе модели не найден JSON-объект")


def _usage(body: dict[str, Any]) -> dict[str, int]:
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


async def _post(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    return await client.post(url, headers=headers, json=payload)


async def chat_json(
    settings: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 8000,
    temperature: float = 0.0,
    attempts: int = 4,
) -> tuple[dict[str, Any], dict[str, int]]:
    base_url = validate_base_url(_base_url(settings))
    model = str(settings.get("model") or "").strip()
    if not model:
        raise ProviderError("Не задано название модели.", status=422)
    url = base_url + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    # gpt-oss и reasoning-модели тратят много токенов на рассуждения и чаще
    # обрывают JSON. Пониженный reasoning_effort делает ответ короче и стабильнее.
    if re.search(r"gpt-oss|gpt-5|^o[1-4]", model, re.IGNORECASE):
        payload["reasoning_effort"] = "low"
    last_error = "неизвестная ошибка"
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=20.0)) as client:
        for attempt in range(attempts):
            try:
                response = await _post(client, url, _headers(settings), payload)
                if response.status_code == 400:
                    # Не все OpenAI-совместимые провайдеры поддерживают
                    # json_object или reasoning_effort — убираем и повторяем.
                    text = response.text.lower()
                    removed = False
                    if "response_format" in payload and "response_format" in text:
                        payload.pop("response_format", None)
                        removed = True
                    elif "reasoning_effort" in payload and "reasoning" in text:
                        payload.pop("reasoning_effort", None)
                        removed = True
                    if removed:
                        response = await _post(client, url, _headers(settings), payload)
                if response.status_code >= 400:
                    detail = response.text[:400]
                    if response.status_code == 404:
                        lowered = detail.lower()
                        if "model" in lowered and "not found" in lowered:
                            raise ProviderError(
                                "Модель не найдена у провайдера. Для Ollama выполните "
                                f"`ollama pull {model}` и повторите проверку.",
                                status=502,
                            )
                        raise ProviderError(
                            "HTTP 404: проверьте Base URL. Для Ollama нужен OpenAI-совместимый "
                            "адрес http://127.0.0.1:11434/v1 (не /api/chat).",
                            status=502,
                        )
                    retriable = response.status_code in RETRIABLE_STATUSES
                    last_error = f"HTTP {response.status_code}: {detail}"
                    if not retriable:
                        raise ProviderError(
                            f"API провайдера вернул HTTP {response.status_code}: {detail}",
                            status=502,
                        )
                    raise _Retryable(last_error)
                body = response.json()
                choice = body["choices"][0]
                content = choice["message"].get("content") or ""
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length" and attempt < attempts - 1:
                    previous_tokens = max_tokens
                    max_tokens = min(16000, max(2000, max_tokens * 2))
                    payload["max_tokens"] = max_tokens
                    logger.warning(
                        "Модель %s обрезала ответ по лимиту токенов (%d); увеличиваю лимит до %d",
                        model,
                        previous_tokens,
                        max_tokens,
                    )
                    raise _Retryable("Ответ обрезан по лимиту токенов")
                try:
                    data = _extract_json(content)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "Модель %s вернула не-JSON: длина=%d, finish_reason=%s, начало=%r",
                        model,
                        len(content),
                        finish_reason,
                        content[:120],
                    )
                    if attempt < attempts - 1:
                        payload["messages"][-1]["content"] = (
                            user_prompt
                            + "\n\nВАЖНО: верни ТОЛЬКО корректный JSON-объект без markdown, "
                            "рассуждений и комментариев. Все текстовые поля — на русском языке."
                        )
                        raise _Retryable("Модель вернула не-JSON ответ")
                    raise ProviderError(
                        "Ответ модели не является корректным JSON после повторов.", status=502
                    ) from None
                return data, _usage(body)
            except _Retryable as exc:
                last_error = str(exc)
                logger.warning(
                    "Повтор запроса к LLM %d/%d, model=%s: %s",
                    attempt + 1,
                    attempts,
                    model,
                    last_error[:300],
                )
            except ProviderError:
                raise
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == attempts - 1:
                    raise ProviderError(
                        f"Не удалось получить ответ провайдера: {last_error}", status=502
                    ) from exc
            if attempt < attempts - 1:
                delay = min(20.0, 1.5 * (2**attempt)) + random.random()
                await asyncio.sleep(delay)
    raise ProviderError(f"Провайдер не ответил после повторов: {last_error}", status=502)


class _Retryable(Exception):
    """Внутренний маркер повторной попытки."""


async def embed_texts(
    settings: dict[str, Any],
    texts: list[str],
    model: str,
    *,
    attempts: int = 3,
) -> tuple[list[list[float]], dict[str, int]]:
    if not texts:
        return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    chat_base = validate_base_url(_base_url(settings))
    embedding_base = str(settings.get("embedding_base_url") or "").strip()
    base_url = validate_base_url(embedding_base) if embedding_base else chat_base
    url = base_url + "/embeddings"
    # Ключ эмбеддингов отдельный; если используется тот же адрес, что и чат,
    # допускаем основной ключ. Для отдельного localhost-эндпоинта ключ не нужен.
    embedding_key = str(settings.get("embedding_api_key") or "").strip()
    if not embedding_key and base_url.rstrip("/") == chat_base.rstrip("/"):
        embedding_key = str(settings.get("api_key") or "").strip()
    headers = {"Content-Type": "application/json"}
    if embedding_key:
        headers["Authorization"] = "Bearer " + embedding_key
    payload = {"model": model, "input": texts}
    last_error = "неизвестная ошибка"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
        for attempt in range(attempts):
            try:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    detail = response.text[:300]
                    if response.status_code == 404:
                        lowered = detail.lower()
                        if "model" in lowered and "not found" in lowered:
                            raise ProviderError(
                                f"Модель эмбеддингов {model} не найдена. Для Ollama выполните "
                                f"`ollama pull {model}`.",
                                status=502,
                            )
                        raise ProviderError(
                            "HTTP 404 для эмбеддингов: проверьте Base URL эмбеддингов "
                            "(для Ollama нужен http://127.0.0.1:11434/v1).",
                            status=502,
                        )
                    if response.status_code in RETRIABLE_STATUSES:
                        raise _Retryable(f"HTTP {response.status_code}: {detail}")
                    logger.error("Embeddings HTTP %s: %s", response.status_code, detail[:200])
                    raise ProviderError(
                        f"Провайдер не поддерживает embeddings (HTTP {response.status_code}): {detail}",
                        status=502,
                    )
                body = response.json()
                data = body.get("data") or []
                if not isinstance(data, list) or not data:
                    raise ProviderError("Пустой ответ embeddings от провайдера.", status=502)
                data = sorted(data, key=lambda item: item.get("index", 0))
                vectors = [list(map(float, item["embedding"])) for item in data]
                return vectors, _usage(body)
            except _Retryable as exc:
                last_error = str(exc)
                logger.warning(
                    "Повтор запроса embeddings %d/%d, model=%s: %s",
                    attempt + 1,
                    attempts,
                    model,
                    last_error[:300],
                )
            except ProviderError:
                raise
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(min(10.0, 1.2 * (2**attempt)) + random.random())
    raise ProviderError(f"Не удалось получить эмбеддинги: {last_error}", status=502)


async def test_connection(settings: dict[str, Any], check_embeddings: bool = False) -> dict[str, Any]:
    data, usage = await chat_json(
        settings,
        "Ты — сервис проверки. Отвечай одним JSON-объектом без пояснений.",
        'Верни JSON: {"status":"ok"}',
        max_tokens=300,
        attempts=2,
    )
    result: dict[str, Any] = {
        "status": "ok",
        "message": "Подключение к API работает.",
        "model": settings.get("model"),
        "usage": usage,
        "json_mode": isinstance(data, dict),
    }
    if check_embeddings and settings.get("use_embeddings"):
        try:
            vectors, embed_usage = await embed_texts(
                settings, ["проверка связи"], settings.get("embedding_model", ""), attempts=2
            )
            result["embeddings"] = {
                "ok": bool(vectors and vectors[0]),
                "dim": len(vectors[0]) if vectors else 0,
                "model": settings.get("embedding_model"),
                "usage": embed_usage,
            }
        except ProviderError as exc:
            result["embeddings"] = {"ok": False, "error": exc.message}
    return result
