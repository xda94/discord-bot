import pytest

from ollama_client import OllamaError, get_allowed_models, get_default_model, get_mention_model


def test_get_allowed_models_from_env(monkeypatch):
    monkeypatch.setenv(
        "OLLAMA_ALLOWED_MODELS",
        "model-a, model-b ,model-c",
    )
    assert get_allowed_models() == ("model-a", "model-b", "model-c")


def test_get_default_model_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "beta")
    assert get_default_model() == "beta"


def test_get_default_model_not_in_allowed_raises(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "missing")
    with pytest.raises(OllamaError, match="not listed"):
        get_default_model()


def test_get_allowed_models_missing_raises(monkeypatch):
    monkeypatch.delenv("OLLAMA_ALLOWED_MODELS", raising=False)
    with pytest.raises(OllamaError, match="not set"):
        get_allowed_models()


def test_get_allowed_models_empty_raises(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "  ,  ")
    with pytest.raises(OllamaError, match="empty"):
        get_allowed_models()


def test_get_mention_model_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha,beta,gamma")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "alpha")
    monkeypatch.setenv("MENTION_OLLAMA_MODEL", "gamma")
    assert get_mention_model() == "gamma"


def test_get_mention_model_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "beta")
    monkeypatch.delenv("MENTION_OLLAMA_MODEL", raising=False)
    assert get_mention_model() == "beta"


def test_get_mention_model_not_in_allowed_raises(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "alpha")
    monkeypatch.setenv("MENTION_OLLAMA_MODEL", "missing")
    with pytest.raises(OllamaError, match="MENTION_OLLAMA_MODEL"):
        get_mention_model()


def test_get_default_model_missing_raises(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha")
    monkeypatch.delenv("OLLAMA_DEFAULT_MODEL", raising=False)
    with pytest.raises(OllamaError, match="not set"):
        get_default_model()


def test_query_ollama_sends_options(monkeypatch):
    import requests
    from ollama_client import query_ollama
    
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "alpha")
    
    payload_received = {}
    
    class MockResponse:
        status_code = 200
        ok = True
        def json(self):
            return {"response": "test response"}
            
    def mock_post(url, json, timeout):
        nonlocal payload_received
        payload_received = json
        return MockResponse()
        
    monkeypatch.setattr(requests, "post", mock_post)
    
    result = query_ollama("hello", options={"temperature": 0.8})
    assert result == "test response"
    assert payload_received.get("options") == {"temperature": 0.8}


def test_query_ollama_respects_keep_alive(monkeypatch):
    import requests
    from ollama_client import query_ollama
    
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha")
    monkeypatch.setenv("OLLAMA_DEFAULT_MODEL", "alpha")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "10m")
    
    payload_received = {}
    
    class MockResponse:
        status_code = 200
        ok = True
        def json(self):
            return {"response": "test response"}
            
    def mock_post(url, json, timeout):
        nonlocal payload_received
        payload_received = json
        return MockResponse()
        
    monkeypatch.setattr(requests, "post", mock_post)
    
    query_ollama("hello")
    assert payload_received.get("keep_alive") == "10m"
    
    # Test integer parsing:
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "-1")
    query_ollama("hello")
    assert payload_received.get("keep_alive") == -1
