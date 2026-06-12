import pytest

from ollama_client import OllamaError, get_allowed_models, get_default_model


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


def test_get_default_model_missing_raises(monkeypatch):
    monkeypatch.setenv("OLLAMA_ALLOWED_MODELS", "alpha")
    monkeypatch.delenv("OLLAMA_DEFAULT_MODEL", raising=False)
    with pytest.raises(OllamaError, match="not set"):
        get_default_model()
