"""Мини-провайдер для локальных смоук-тестов (не входит в runtime).

Запуск:
    .venv\\Scripts\\python.exe -m uvicorn tests.mock_provider:app --port 8899
"""

from __future__ import annotations

import hashlib
import json
import re

from fastapi import FastAPI, Request

app = FastAPI(title="TZ Checker mock provider")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    prompt = body["messages"][-1]["content"]
    match = re.search(r"STANDARD ID: (STD-[A-Z0-9]+)", prompt)
    standard_id = match.group(1) if match else "STD-UNKNOWN"
    payload = {
        "results": [
            {
                "category": "Конструкции",
                "section": "5.3 Стены",
                "parameter": "Толщина стены",
                "requirement_type": "numeric",
                "status": "COMPLIES",
                "severity": "minor",
                "confidence": 0.92,
                "finding_type": "соответствие",
                "applicability_assessment": "Жилое здание, стадия ЭП — применимо.",
                "semantic_reasoning": "Требование ТЗ о толщине стены совпадает по смыслу с положением СТО.",
                "tz_quote": "Толщина стены не менее 200 мм.",
                "tz_locator": "п. 5.3.2",
                "standard_source_id": standard_id,
                "standard_quote": "Толщина стены не менее 200 мм.",
                "standard_locator": "п. 5.3.2",
                "operator": ">=",
                "expected_value": 200,
                "actual_value": 200,
                "expected_unit": "мм",
                "actual_unit": "мм",
                "explanation": "Значения совпадают.",
                "technical_assessment": "Отклонений не выявлено.",
                "recommendation": "Подтвердить толщину расчётом.",
                "assumptions": "",
                "missing_data": "",
            }
        ]
    }
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 400, "total_tokens": 1600},
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inputs = body.get("input") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    vectors = []
    for text in inputs:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        vectors.append([byte / 255.0 for byte in digest[:8]])
    return {
        "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
        "usage": {"prompt_tokens": len(inputs) * 10, "total_tokens": len(inputs) * 10},
    }
