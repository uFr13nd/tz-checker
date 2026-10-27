from app.llm import _extract_json, normalize_base_url, validate_base_url


def test_extract_json_with_reasoning_prefix_and_suffix():
    content = (
        "Сначала подумаю, какой ответ нужен.\n"
        '{"results":[{"category":"Стены","parameter":"Толщина","status":"COMPLIES"}]}\n'
        "Готово."
    )
    data = _extract_json(content)
    assert data["results"][0]["status"] == "COMPLIES"


def test_extract_json_allows_trailing_commas():
    content = '{"results":[{"category":"Стены","parameter":"Толщина","status":"COMPLIES",},],}'
    data = _extract_json(content)
    assert data["results"][0]["parameter"] == "Толщина"


def test_extract_json_merges_multiple_objects():
    content = (
        '{"results":[{"category":"A","parameter":"p1","status":"COMPLIES"}]}\n'
        '{"results":[{"category":"B","parameter":"p2","status":"NONCOMPLIANCE"}]}'
    )
    data = _extract_json(content)
    assert len(data["results"]) == 2


def test_extract_json_wraps_single_result_and_array():
    assert _extract_json('{"category":"A","parameter":"p","status":"COMPLIES"}')["results"]
    assert len(_extract_json('[{"category":"A","parameter":"p","status":"COMPLIES"}]')["results"]) == 1


def test_extract_json_rejects_garbage():
    import pytest

    with pytest.raises(ValueError):
        _extract_json("никакого json здесь нет")


def test_ollama_native_path_is_normalized_to_v1():
    assert normalize_base_url("http://localhost:11434/api/chat") == "http://localhost:11434/v1"
    assert normalize_base_url("http://127.0.0.1:11434/api") == "http://127.0.0.1:11434/v1"
    assert normalize_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"


def test_ollama_cloud_root_is_normalized():
    assert normalize_base_url("https://ollama.com") == "https://ollama.com/v1"


def test_openai_style_urls_are_unchanged():
    assert normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert normalize_base_url("https://gateway.example/proxy/v1/") == "https://gateway.example/proxy/v1"


def test_validate_rejects_insecure_remote_url():
    import pytest

    from app.llm import ProviderError

    with pytest.raises(ProviderError):
        validate_base_url("http://example.com/v1")
